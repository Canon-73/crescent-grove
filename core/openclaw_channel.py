"""
core/openclaw_channel.py

OpenClaw互換WebSocketクライアント。
openclaw-channelプラグインと同等の機能をCrescent Groveに提供する。

設定: data/openclaw_config.json
  services配列で複数サービスを定義可能。各サービスは独立したインスタンスで動作する。
"""
from core.time_utils import tlog
import asyncio
import json
import os
import random
from pathlib import Path
from core.paths import data_file, config_file
from typing import Optional, Callable, Awaitable

try:
    import websockets
    import websockets.exceptions
except ImportError:
    websockets = None

try:
    import aiohttp
except ImportError:
    aiohttp = None


class OpenClawChannel:
    """
    OpenClaw互換WebSocketクライアント。
    接続・ping・ack・再接続・heartbeatループを管理し、
    city_eventをコールバックで通知する。
    """

    PING_INTERVAL = 15
    RECONNECT_BASE = 3
    RECONNECT_MAX = 300

    def __init__(
        self,
        name: str,
        ws_url: str,
        token: str,
        bot_id: str,
        heartbeat_url: Optional[str] = None,
        on_city_event: Optional[Callable[[dict, "OpenClawChannel"], Awaitable[None]]] = None,
    ):
        self.name = name
        self.ws_url = ws_url
        self.token = token
        self.bot_id = bot_id
        self.heartbeat_url = heartbeat_url
        self.on_city_event = on_city_event
        self._last_ack_seq: Optional[int] = None
        self._ws = None
        self._running = False
        self._attempt = 0

    def _log(self, msg: str):
        tlog(f"[OpenClaw:{self.name}] {msg}")

    def _build_url(self) -> str:
        url = f"{self.ws_url}?token={self.token}&botId={self.bot_id}"
        if self._last_ack_seq is not None:
            url += f"&lastAckSeq={self._last_ack_seq}"
        return url

    def _backoff(self) -> float:
        delay = min(self.RECONNECT_BASE * (2 ** self._attempt), self.RECONNECT_MAX)
        jitter = random.uniform(-0.3, 0.3)
        return delay * (1 + jitter)

    async def _ping_loop(self, ws):
        while self._running:
            try:
                await ws.send("ping")
            except Exception:
                break
            await asyncio.sleep(self.PING_INTERVAL)

    async def _heartbeat_loop(self):
        """HTTP heartbeatを定期的に叩いてオンライン状態を維持する"""
        if not self.heartbeat_url:
            return
        if aiohttp is None:
            self._log("aiohttpが未インストールのためheartbeatスキップ")
            return

        headers = {"Authorization": f"Bearer {self.token}"}
        self._log("heartbeatループ開始")
        async with aiohttp.ClientSession() as session:
            while self._running:
                try:
                    async with session.get(
                        self.heartbeat_url,
                        headers=headers,
                        timeout=aiohttp.ClientTimeout(total=10)
                    ) as resp:
                        data = await resp.json()
                        interval = data.get("next_heartbeat_interval", 60000) / 1000
                except Exception as e:
                    self._log(f"heartbeatエラー: {type(e).__name__}: {e}")
                    interval = 60
                await asyncio.sleep(interval)

    async def _handle_message(self, ws, raw: str):
        if raw == "pong":
            return

        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            return

        msg_type = msg.get("type")

        if msg_type == "welcome":
            location = msg.get("location", {})
            zone = location.get("zoneName", "不明")
            building = location.get("buildingName")
            loc_str = f"{building}内" if building else zone
            self._log(f"接続完了: {loc_str}")
            await ws.send("ping")

        elif msg_type == "city_event":
            seq = msg.get("seq")
            if seq is not None:
                await ws.send(json.dumps({"type": "ack", "seq": seq}))
                self._last_ack_seq = seq
            if self.on_city_event:
                try:
                    await self.on_city_event(msg, self)
                except Exception as e:
                    self._log(f"city_eventコールバックエラー: {e}")

        elif msg_type == "error":
            reason = msg.get("reason", "")
            self._log(f"サーバーエラー: {reason} - {msg.get('message', '')}")
            if reason in ("auth_failed", "token_expired"):
                self._log("認証エラーのため停止します")
                self._running = False

        elif msg_type in ("paused", "resumed", "action_result"):
            self._log(f"{msg_type}: {msg}")

    async def _connect(self):
        if websockets is None:
            self._log("websocketsパッケージが未インストールです")
            self._running = False
            return

        url = self._build_url()
        headers = {
            "Authorization": f"Bearer {self.token}",
            "X-Bot-Id": self.bot_id,
        }

        try:
            async with websockets.connect(url, additional_headers=headers) as ws:
                self._ws = ws
                self._attempt = 0
                ping_task = asyncio.create_task(self._ping_loop(ws))
                try:
                    async for raw in ws:
                        if not self._running:
                            break
                        await self._handle_message(ws, raw)
                finally:
                    ping_task.cancel()

        except websockets.exceptions.ConnectionClosedError as e:
            if hasattr(e, 'code') and e.code == 4000:
                self._log("別の接続に置き換えられました。再接続しません。")
                self._running = False
            else:
                self._log("接続切断: 再接続します")
        except Exception as e:
            self._log(f"接続エラー: {e}")

    async def run(self):
        """WebSocket接続とheartbeatループを並行して実行する"""
        self._running = True
        asyncio.create_task(self._heartbeat_loop())
        while self._running:
            await self._connect()
            if not self._running:
                break
            delay = self._backoff()
            self._attempt += 1
            self._log(f"{delay:.1f}秒後に再接続します")
            await asyncio.sleep(delay)

    def stop(self):
        self._running = False

    async def send_reply(self, action: str, **kwargs):
        """agent_replyフレームを送信する"""
        if self._ws and self._ws.open:
            payload = {"type": "agent_reply", "action": action, **kwargs}
            await self._ws.send(json.dumps(payload))


def create_from_config(on_city_event=None) -> list:
    """
    data/openclaw_config.jsonからOpenClawChannelのリストを生成する。
    トップレベルの "enabled"（機能全体のマスタースイッチ）が false の場合は
    サービス定義に関わらず一切接続しない。true（デフォルト・後方互換）の場合のみ、
    各 services[].enabled が true のサービスを返す。
    """
    config_path = config_file("openclaw_config.json")
    if not config_path.exists():
        return []

    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
    except Exception:
        return []

    # 機能全体のオンオフ（マスタースイッチ）。OFFなら全サービスを起動しない。
    if not config.get("enabled", True):
        tlog("[OpenClaw] 機能が無効化されています (enabled=false)")
        return []

    channels = []
    for svc in config.get("services", []):
        if not svc.get("enabled", False):
            continue

        name = svc.get("name", "Unknown")
        ws_url = svc.get("ws_url", "")
        heartbeat_url = svc.get("heartbeat_url")
        token_env = svc.get("token_env", "")
        bot_id_env = svc.get("bot_id_env", "")
        token = os.environ.get(token_env, "")
        bot_id = os.environ.get(bot_id_env, "")

        if not ws_url:
            tlog(f"[OpenClaw:{name}] ws_url 未設定のためスキップ（config を確認してください）")
            continue

        # 機能を有効化したのに認証情報が無い＝未セットアップ。無言スキップだと原因が
        # 分からないため、OpenBotCity の setup を促す案内を出す。
        if not token or not bot_id:
            missing = [e for e, v in ((token_env, token), (bot_id_env, bot_id)) if not v]
            tlog(
                f"[OpenClaw:{name}] 認証情報が未設定のため接続できません"
                f"（未設定: {', '.join(missing)}）。"
                f"OpenBotCity サテライトで setup を実行してください"
                f"（例: command=\"setup\" display_name=\"<名前>\" で新規登録、"
                f"または source_env=\"<既存JWT変数名>\" で引継ぎ）。"
            )
            continue

        channels.append(OpenClawChannel(
            name=name,
            ws_url=ws_url,
            token=token,
            bot_id=bot_id,
            heartbeat_url=heartbeat_url,
            on_city_event=on_city_event,
        ))

    return channels