"""
Discord REST APIクライアント

- Bot Token は環境変数 CG_DISCORD_BOT_TOKEN から読む（.env / env_keeper で登録）
- Token は例外メッセージにも出力にも絶対に載せない（Authorizationヘッダ内に閉じる）
- 429 は retry_after に従って1回だけ自動リトライ
- HTTPを実際に行うのは _transport() 一箇所のみ。テストはここを差し替える
"""
import json
import socket
import time
import os
import urllib.request
import urllib.error

from _i18n import t


DISCORD_API = "https://discord.com/api/v10"
TOKEN_ENV = "CG_DISCORD_BOT_TOKEN"

# Discordのメッセージ本文上限（サーバー側の仕様）
MAX_MESSAGE_LENGTH = 2000


class APIError(Exception):
    """Discord APIのエラー。status=0 はネットワーク層の失敗を表す。"""

    def __init__(self, status, body, hint=None):
        self.status = status
        self.body = body
        self.hint = hint
        msg = f"HTTP {status}"
        if isinstance(body, dict):
            msg += f": {body.get('message') or body.get('error') or body}"
        elif body:
            msg += f": {body}"
        super().__init__(msg)


def get_token():
    """Bot Token を環境変数から読む（未設定なら空文字）。"""
    return os.environ.get(TOKEN_ENV, "").strip()


def _transport(method, path, body=None, token=None, timeout=20):
    """
    HTTPリクエストを実際に行う唯一の場所。

    戻り値: (status_code, parsed_body)
    HTTPエラー(4xx/5xx)も戻り値として返す。ネットワーク層の失敗のみ APIError(0) を送出する。
    テストからはこの関数を差し替えることで、実ネットワークに出ずに検証できる。
    """
    url = DISCORD_API + path
    headers = {
        "Accept": "application/json",
        "User-Agent": "CrescentGrove-DiscordSatellite/1.0",
    }
    if token:
        headers["Authorization"] = f"Bot {token}"

    data = None
    if body is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(body).encode("utf-8")

    req = urllib.request.Request(url, data=data, headers=headers, method=method)

    def _parse(raw):
        if not raw or not raw.strip():
            return {}
        try:
            return json.loads(raw)
        except Exception:
            return {"message": raw[:500]}

    try:
        with urllib.request.urlopen(req, timeout=timeout) as res:
            return res.status, _parse(res.read().decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as e:
        raw = ""
        try:
            raw = e.read().decode("utf-8", errors="replace")
        except Exception:
            pass
        return e.code, _parse(raw)
    except urllib.error.URLError as e:
        # タイムアウトは URLError.reason に包まれて来る経路がある
        if isinstance(e.reason, (socket.timeout, TimeoutError)):
            raise APIError(0, {"error": t("dsat_err_timeout", sec=timeout)},
                           hint=t("dsat_err_network_hint"))
        raise APIError(0, {"error": t("dsat_err_network", reason=e.reason)},
                       hint=t("dsat_err_network_hint"))
    except (socket.timeout, TimeoutError):
        raise APIError(0, {"error": t("dsat_err_timeout", sec=timeout)},
                       hint=t("dsat_err_network_hint"))
    except Exception as e:
        # SSL例外・レスポンス読込中の OSError など、上で拾い切れない通信系の失敗。
        # 生のまま main.py に漏らすと次の一手が無い一行になるので、APIError(0) に
        # 正規化して「送れたか不明→read で確認」の導線（network_hint）を必ず付ける。
        raise APIError(0, {"error": t("dsat_err_unexpected",
                                      kind=type(e).__name__, detail=str(e)[:200])},
                       hint=t("dsat_err_network_hint"))


def _hint_for(status):
    """HTTPステータスから、柚月が次の一手を選べるヒントを返す。"""
    return {
        401: t("dsat_hint_401"),
        403: t("dsat_hint_403"),
        404: t("dsat_hint_404"),
        429: t("dsat_hint_429"),
    }.get(status, t("dsat_hint_5xx") if status >= 500 else None)


def request(method, path, body=None, timeout=15, _retry=0):
    """
    Discord APIを呼ぶ。成功時はパース済みボディ（本文なしなら {}）を返す。

    Token未設定・HTTPエラーは APIError を送出する。呼び出し側は捕まえずに
    main.py まで投げてよい（main.py が自己回復ヒント付きJSONに整形する）。
    """
    token = get_token()
    if not token:
        raise APIError(0, {"error": t("dsat_err_no_token")}, hint=t("dsat_err_no_token_hint"))

    status, data = _transport(method, path, body=body, token=token, timeout=timeout)

    if 200 <= status < 300:
        return data

    # レート制限は retry_after に従って1回だけ待って再試行する
    if status == 429 and _retry == 0:
        retry_after = 1.0
        if isinstance(data, dict):
            try:
                retry_after = float(data.get("retry_after", 1.0))
            except (TypeError, ValueError):
                retry_after = 1.0
        # 外部から来る値なので範囲を検証する（負値は time.sleep が例外を出す。
        # NaN は比較が False になるのでこの条件式で自然に弾ける）
        if 0 <= retry_after <= 10:
            time.sleep(retry_after + 0.2)
            return request(method, path, body=body, timeout=timeout, _retry=1)

    raise APIError(status, data, hint=_hint_for(status))
