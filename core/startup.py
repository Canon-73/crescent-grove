# core/startup.py
"""
サーバー起動時の初期化処理。

server.py の lifespan から startup_event() が一度だけ呼ばれる。
スケジューラ・グローバルAgent・VitalManager・Moonbeat類似度モデル・
OpenClawチャンネル・記憶圧縮バッチを初期化・開始し、結果を core/app_state.py の
共有状態に格納する。
"""

import os
import json
import time
import asyncio
from datetime import datetime
from pathlib import Path

try:
    import aiohttp
except ImportError:
    aiohttp = None

# Moonbeat類似度チェック用（sentence-transformers / numpy）
# インストールされていない場合はNoneになり、類似度チェックは無効化される
try:
    from sentence_transformers import SentenceTransformer
    import numpy as np
except ImportError:
    SentenceTransformer = None
    np = None

from core import app_state
from core.llm import create_provider
from core.context import ContextBuilder
from core.agent import Agent
from core.scheduler import Scheduler
from memory.manager import MemoryManager
from vital.vital_manager import VitalManager
from core.config_loader import load_config_strict as load_config
from core.i18n import init_i18n
from core.time_utils import tlog, JST, set_context_timezone
from core.paths import data_file, resolve_workspace, resolve_path, config_file


async def startup_event():
    """
    サーバー起動時の初期化処理。
    スケジューラ・グローバルAgent・VitalManager・Moonbeat類似度モデル・
    OpenClawチャンネル・記憶圧縮バッチを初期化・開始する。
    """
    config = load_config()

    # --- 多言語化の初期化 ---
    init_i18n(config)

    # --- タイムゾーンの初期化 ---
    # 論理日付（午前3時境界）を一般設定 time.tz_offset に追従させる（既定 JST）。
    # コンテキストに注入する時刻表示（context.py）と同じタイムゾーンで「1日の区切り」が動く。
    set_context_timezone(config.get("time", {}).get("tz_offset", 9))

    # --- 外部バインド時のセキュリティガード（server.py __main__ を通らない起動経路の保険） ---
    # host が 127.0.0.1 以外（0.0.0.0 等で外部からアクセス可能）なのにパスワード未設定だと、
    # 誰でもセットアップ画面からパスワードを奪える。配布版や uvicorn 直接起動でも確実に弾く。
    from core.auth import is_password_set as _is_password_set
    _host = config.get("server", {}).get("host", "127.0.0.1")
    if _host != "127.0.0.1" and not _is_password_set():
        raise RuntimeError(
            f"外部バインド（host={_host!r}）が有効ですがパスワードが未設定です。"
            "先に http://127.0.0.1:8080 でパスワードを設定するか、config.yaml の "
            "server.host を '127.0.0.1' に戻してください。"
        )

    workspace_path = str(resolve_workspace(config))
    memory = MemoryManager(workspace_path)
    # config に llm セクションが無くてもクラッシュさせない（5-B-α: 設定欠落フォールバック）。
    # キーがある時は従来通り。無い時だけ config.yaml 現行値に合わせたデフォルトで続行する。
    llm_config = config.get("llm")
    if not llm_config:
        print("警告: config に 'llm' セクションがありません。デフォルト設定(deepseek/deepseek-v4-flash)で続行します。")
        llm_config = {"provider": "deepseek", "model": "deepseek-v4-flash"}
    llm = create_provider(llm_config)
    context = ContextBuilder(memory, config)

    # configからエージェント名を取得（ログやUI表示に使用）
    agent_name = config.get("profile", {}).get("agent", {}).get("name", "Assistant")

    # --- スケジューラ初期化 ---
    schedule_file = str(Path(workspace_path) / "schedule.json")
    app_state.global_scheduler = Scheduler(schedule_file, memory)
    global_scheduler = app_state.global_scheduler

    # --- ログシステム初期化 ---
    # 設定が欠落していても必ず ConversationLogger を作る（無言でログが消える事故を防ぐため）。
    # 不正値・空文字は ConversationLogger 側でデフォルトにフォールバックされる。
    from core.logger import ConversationLogger
    logs_config = config.get("logs") or {}
    # ログディレクトリも data_root 基準で解決する（相対値→data_root、絶対値→そのまま）。
    logger = ConversationLogger(
        full_log_dir=str(resolve_path(logs_config.get("full_log_directory"), "workspace/logs/full")),
        chat_log_dir=str(resolve_path(logs_config.get("chat_log_directory"), "workspace/logs/chat")),
        agent_name=agent_name
    )

    # --- RAGデータベース初期化 ---
    rag_db = None
    rag_config = config.get("rag")
    if rag_config and rag_config.get("db_directory"):
        try:
            from core.rag import RAGDB
            rag_db = RAGDB(rag_config["db_directory"], rag_config.get("embedding_model", "default"))
        except Exception as e:
            print(f"RAGDBの初期化に失敗しました: {e}")

    # --- VitalManager初期化（バイタル・精神状態・欲求の管理） ---
    deepseek_key = os.environ.get("CG_LLM_DEEPSEEK_API_KEY", "")
    try:
        vital = VitalManager(api_key=deepseek_key)
        print(f"[VitalManager] 初期化完了 (stamina={vital.data['stamina']}, mental={vital.data['mental']})")
    except Exception as e:
        print(f"[VitalManager] 初期化に失敗しました: {e}")
        vital = None

    # --- Moonbeat類似度チェック用のSentenceTransformerモデル初期化 ---
    if SentenceTransformer is not None:
        try:
            mb_config = json.loads(config_file("moonbeat_config.json").read_text(encoding="utf-8"))
            sim_model_name = mb_config.get("similarity", {}).get("model", "cl-nagoya/ruri-v3-30m")
            # 二刀流: 同梱 models/<モデル名末尾> があればローカルからオフライン読み込み、無ければ HF キャッシュから解決
            from core.paths import resolve_model
            _ruri_subdir = sim_model_name.split("/")[-1]
            _ruri_src, _ruri_local_only = resolve_model(_ruri_subdir, sim_model_name)
            app_state.similarity_model = SentenceTransformer(_ruri_src, local_files_only=_ruri_local_only)
            print(f"[Moonbeat] 類似度チェックモデル読み込み完了: {sim_model_name}")
        except Exception as e:
            print(f"[Moonbeat] 類似度チェックモデルの読み込みに失敗（スキップ）: {e}")
            app_state.similarity_model = None
    else:
        print("[Moonbeat] sentence-transformers未インストール（類似度チェック無効）")

    # --- グローバルAgent初期化（スケジュールタスク・Moonbeat実行用） ---
    # ユーザー呼称（config の profile.user.honorific）。未設定なら中立語 "ユーザー"。
    # dev では config.yaml に honorific:"ご主人様" があるため従来と同一挙動になる。
    honorific = config.get("profile", {}).get("user", {}).get("honorific", "ユーザー")

    app_state.global_agent = Agent(llm, context, memory, logger=logger,
                                   scheduler=global_scheduler, rag_db=rag_db,
                                   processing_lock=app_state.global_processing_lock,
                                   agent_name=agent_name,
                                   on_context_update=app_state.register_debug_context,
                                   vital_manager=vital, honorific=honorific)
    global_agent = app_state.global_agent

    # 起動時のLLM識別情報を記録する。プロバイダ/モデルは稼働中に作り直さない（要再起動）ため、
    # この値が「現在実際に動いているLLM」を表す。LLM設定保存時の再起動要否判定に使う。
    _startup_llm = config.get("llm", {}) or {}
    global_agent.startup_llm = {
        "provider": _startup_llm.get("provider"),
        "model": _startup_llm.get("model"),
        "base_url": _startup_llm.get("base_url"),
    }

    # --- サリア（サリエンスネットワークシステム）初期化 ---
    # salia.enabled が false の場合は初期化せず self.salia=None のままにする。
    # （somatic_marker / evaluate_turn など Salia 依存の処理が全てスキップされる。要再起動。）
    # サリアはメインの LLM プロバイダ（llm）を共用する＝キャラ本体と同じ1キーで動く。
    # メインが未設定（UnconfiguredProvider）や OpenAI 非互換（client を持たない）なら Salia は作らない。
    # サリア専用モデル/thinking（config の salia.model / salia.thinking）。未指定なら
    # メインと同じ（model はメインのモデル、thinking はメインの llm.thinking）にフォールバック。
    # キー/エンドポイントは常にメイン共用、model と thinking だけ個別に切り替えられる。
    _salia_model = (config.get("salia") or {}).get("model")
    _salia_thinking = (config.get("salia") or {}).get("thinking") or (config.get("llm") or {}).get("thinking", "auto")
    if config.get("salia", {}).get("enabled", True) and getattr(llm, "client", None) is not None:
        from core.salia import Salia
        global_agent.salia = Salia(workspace_path=workspace_path, agent_name=agent_name, honorific=honorific, llm_provider=llm, model=_salia_model, thinking=_salia_thinking)
        tlog(f"[Salia] サリエンスネットワークシステム初期化完了（メインLLM共用 / model={_salia_model or 'メインと同じ'} / thinking={_salia_thinking}）")
    else:
        tlog("[Salia] salia.enabled=false またはメインLLM未設定/非互換のため初期化をスキップしました")

    # context_state.jsonのパスを設定し、前回の会話履歴を復元
    context_state_path = str(data_file("context_state.json"))
    context.set_state_path(context_state_path)
    context.load_state()

    # 履歴復元後にデバッグコンテキストを一度算出しておく。
    # これが無いと再起動直後は last_debug_context が None のままで、
    # 体調タブ・DEBUG画面の System/Tools/Raw 内訳が最初の会話まで '?' になる。
    # build_messages() とトークン計測のみで LLM 呼び出しは発生しない。
    try:
        await global_agent._update_debug_context()
    except Exception as e:
        print(f"[startup] 初期デバッグコンテキスト算出に失敗（スキップ）: {e}")

    # --- スケジューラのタスク実行コールバック定義 ---
    async def execute_scheduled_task(task_name: str, instruction: str, schedule_type: str = "daily", manual: bool = False) -> str:
        """
        スケジューラから呼び出されるタスク実行関数。
        schedule_typeに応じてMoonbeat・daily・onceの各処理フローを分岐する。

        Moonbeat: ロック競合・睡眠中・直近会話時はスキップ。類似度チェック付き。
        daily: 会話中は最大5分延期してから実行。完了後に記憶圧縮を実行。
        once: 即時実行。

        Args:
            manual: Trueの場合は手動発火。Moonbeatの「直近5分の会話スキップ」を
                    バイパスする（睡眠中スキップ・多重実行防止は維持する）。
        """
        # === Moonbeat固有の処理 ===
        if schedule_type == "moonbeat":
            # ロックが取れなければ（他の処理が実行中なら）即スキップ
            if app_state.global_processing_lock.lock.locked():
                print(f"[Moonbeat] 処理中のためスキップ")
                return ""

            # 生活行動（睡眠等）中のスキップ判定
            try:
                state_path = resolve_workspace(config) / ".life_action_state.json"
                if state_path.exists():
                    state = json.loads(state_path.read_text(encoding="utf-8"))
                    until = datetime.fromisoformat(state["until"])
                    if datetime.now() < until:
                        action = state.get("action", "不明")
                        if action in ("sleep", "nap"):
                            # 睡眠中はMoonbeatをスキップ
                            print(f"[Moonbeat] 睡眠中のためスキップ ({action}, {state['until']}まで)")

                            return "SKIPPED"
                        else:
                            # idle, nothing等の非睡眠行動は状態ファイルを削除してMoonbeatを通す
                            state_path.unlink()
                            # 状態ファイル削除はファイルログに必ず残す（起床原因の追跡用）
                            tlog(f"[Moonbeat] 生活行動中 ({action}) ですがMoonbeatは通します")
                    else:
                        # 期限切れの状態ファイルを削除
                        ended_action = state.get("action", "不明")
                        state_path.unlink()
                        tlog(f"[Moonbeat] 生活行動が終了しました ({ended_action})")

                        # 仮眠終了時にMoodPhase/MoodSAEを回復
                        if ended_action == "nap" and vital:
                            if vital.moodphase:
                                vital.moodphase.recover_from_nap()
                            if hasattr(vital, 'moontide') and vital.moontide:
                                vital.moontide.recover_from_nap()
                        print(f"[Moonbeat] 生活行動が終了しました ({state.get('action', '不明')})")
            except Exception as e:
                print(f"[Moonbeat] 生活行動チェックでエラー: {e}")

            # 直近5分以内にユーザーと会話していたらスキップ（会話の邪魔をしない）
            # ただし手動発火（manual=True）はユーザーの明示操作なのでスキップしない
            if not manual and time.time() - app_state.last_chat_time < 300:
                print(f"[Moonbeat] 直近の会話から5分以内のためスキップ")
                return ""

            async with app_state.global_processing_lock.lock:
                # アクティブなチャットセッションがあればそのAgentを使い、なければグローバルAgentを使う
                chat_agent = app_state.active_chat_agent or global_agent

                # ターン開始時刻（長いMoonbeatでも応答が「今」にならないよう添える）
                turn_time = datetime.now(JST).strftime("%H:%M")

                # Moonbeat実行中のツール呼び出しをUIに通知するコールバック
                async def moonbeat_on_tool(name, args, result):
                    await app_state.broadcast({
                        "type": "tool_call",
                        "tool_name": name,
                        "arguments": args,
                    })

                # Moonbeat実行中の中間テキストをUIに通知するコールバック
                async def moonbeat_on_intermediate(text):
                    await app_state.broadcast({
                        "type": "intermediate",
                        "content": text,
                        "time": turn_time,
                    })

                # Moonbeat開始をUIに通知
                await app_state.broadcast({
                    "type": "intermediate",
                    "content": "🌙 [Moonbeat]",
                })

                # Moonbeatメッセージをエージェントに処理させる
                result = await chat_agent.process_message(
                    instruction,
                    is_background=(chat_agent is global_agent),
                    on_tool_call=moonbeat_on_tool,
                    on_intermediate_text=moonbeat_on_intermediate,
                    msg_type="moonbeat"
                )

                # --- 類似度チェック: 直前のMoonbeat応答と類似度が高ければ再生成する ---
                if result and app_state.last_moonbeat_response and app_state.similarity_model is not None and np is not None:
                    try:
                        mb_config = json.loads(config_file("moonbeat_config.json").read_text(encoding="utf-8"))
                        sim_cfg = mb_config.get("similarity", {})
                        threshold = sim_cfg.get("threshold", 0.85)
                        max_retry = sim_cfg.get("max_retry", 1)
                        retry_penalty = sim_cfg.get("retry_frequency_penalty", 1.0)
                        retry_msg = sim_cfg.get("retry_message", "")

                        # コサイン類似度を計算
                        embeddings = app_state.similarity_model.encode([result, app_state.last_moonbeat_response])
                        cos_sim = float(np.dot(embeddings[0], embeddings[1]) / (np.linalg.norm(embeddings[0]) * np.linalg.norm(embeddings[1])))
                        print(f"[Moonbeat] 類似度: {cos_sim:.3f} (閾値: {threshold})")

                        # 閾値を超えた場合、リトライメッセージで再生成を試みる
                        if cos_sim >= threshold and max_retry > 0 and retry_msg:
                            print(f"[Moonbeat] 類似度が高いため再生成します")
                            result = await chat_agent.process_message(
                                retry_msg,
                                is_background=(chat_agent is global_agent),
                                on_tool_call=moonbeat_on_tool,
                                on_intermediate_text=moonbeat_on_intermediate,
                                frequency_penalty_override=retry_penalty,
                                msg_type="system"
                            )
                            # 再生成後の類似度もログ出力（デバッグ用）
                            embeddings2 = app_state.similarity_model.encode([result, app_state.last_moonbeat_response])
                            cos_sim2 = float(np.dot(embeddings2[0], embeddings2[1]) / (np.linalg.norm(embeddings2[0]) * np.linalg.norm(embeddings2[1])))
                            print(f"[Moonbeat] 再生成後の類似度: {cos_sim2:.3f}")
                    except Exception as e:
                        print(f"[Moonbeat] 類似度チェックでエラー: {e}")

                # 次回の類似度チェックのために応答を記録
                if result:
                    app_state.last_moonbeat_response = result

                # Moonbeatの応答をUIに送信
                token_usage = app_state.active_chat_agent.context.get_token_usage() if app_state.active_chat_agent else None
                await app_state.broadcast({
                    "type": "response",
                    "content": result,
                    "is_moonbeat": True,
                    "token_usage": token_usage,
                    "time": turn_time,
                })
                return result

        # === daily/onceタスク共通処理 ===
        # onceタスクは即時実行、dailyタスクは会話中なら最大5分（1分×5回）延期する
        if schedule_type != "once":
            for _ in range(5):
                if time.time() - app_state.last_chat_time < 300:
                    print(f"[Schedule] 会話中のため {task_name} を1分延期します")
                    await asyncio.sleep(60)
                else:
                    break

        async with app_state.global_processing_lock.lock:
            chat_agent = app_state.active_chat_agent or global_agent

            # ターン開始時刻（長いタスク実行でも応答が「今」にならないよう添える）
            turn_time = datetime.now(JST).strftime("%H:%M")

            async def on_tool(name, args, result):
                await app_state.broadcast({
                    "type": "tool_call",
                    "tool_name": name,
                    "arguments": args,
                })

            async def on_intermediate(text):
                await app_state.broadcast({
                    "type": "intermediate",
                    "content": text,
                    "time": turn_time,
                })

            result = await chat_agent.process_message(
                instruction,
                is_background=(chat_agent is global_agent),
                msg_type="task",
                on_tool_call=on_tool,
                on_intermediate_text=on_intermediate,
            )

            # タスク実行結果をUIに通知
            await app_state.broadcast({
                "type": "intermediate",
                "content": f"📅 [{task_name}] スケジュール実行",
                "time": turn_time,
            })
            await app_state.broadcast({
                "type": "response",
                "content": result,
                "time": turn_time,
            })

            # 日次タスク完了後は記憶圧縮（LETHE）を実行する
            if "DAILY" in task_name.upper() or "毎日" in task_name:
                if config.get("memory_compression"):
                    from core.compressor import MemoryCompressor
                    compressor = MemoryCompressor(global_agent.llm, config)
                    print(f"[{task_name}] 完了。記憶圧縮（未処理分）を実行します...")
                    await compressor.run_compression_for_missing_days(workspace_path)

            return result

    # スケジューラにタスク実行コールバックとVitalManagerを設定
    global_scheduler.set_execute_callback(execute_scheduled_task)
    global_scheduler.vital_manager = vital
    global_scheduler.get_active_agent = lambda: app_state.active_chat_agent  # Layer0圧縮用（会話履歴を持つ方）

    # タスクファイル格納ディレクトリを作成（なければ）
    tasks_dir = Path(workspace_path) / "tasks"
    tasks_dir.mkdir(parents=True, exist_ok=True)

    # --- 起動時の記憶圧縮バッチ（未処理の日次ログを一括圧縮） ---
    if config.get("memory_compression"):
        from core.compressor import MemoryCompressor
        compressor = MemoryCompressor(llm, config)
        print("未処理の記憶圧縮を確認しています...")
        await compressor.run_compression_for_missing_days(workspace_path)

    # --- スケジューラ開始（Moonbeat・定期タスクのタイマーを起動） ---
    global_scheduler.start()
    print(f"スケジューラが起動しました（{len(global_scheduler.schedules)} 件の予約）")

    # --- 起動バナー表示 ---
    print("┌─────────────────────────────────────┐")
    print("│         Crescent Grove               │")
    print("│                                      │")
    print(f"│  Agent : {agent_name:<29}│")
    _llm_banner = config.get("llm", {})
    print(f"│  LLM   : {_llm_banner.get('provider', 'deepseek')}/{_llm_banner.get('model', 'deepseek-v4-flash'):<20}│")
    # workspace は data_root/workspace に固定。無視される config.workspace.path ではなく
    # 実際に使われる解決済みパスの名前を表示する（dev では従来どおり "workspace"）。
    print(f"│  WS    : {resolve_workspace(config).name:<29}│")
    print("└─────────────────────────────────────┘")

    # --- OpenClawチャンネルの起動（外部WebSocketサービスとの連携） ---
    try:
        from core.openclaw_channel import create_from_config

        async def on_city_event(event: dict, channel):
            """city_eventを受信したらキューに積む（処理は別workerが行う）。"""
            await app_state.openclaw_event_queue.put((event, channel))

        async def openclaw_event_worker():
            """キューからcity_eventを取り出してエージェントに順次処理させるワーカー。"""
            while True:
                event, channel = await app_state.openclaw_event_queue.get()

                # 睡眠中はイベント処理をスキップ
                try:
                    state_path = resolve_workspace(config) / ".life_action_state.json"
                    if state_path.exists():
                        state = json.loads(state_path.read_text(encoding="utf-8"))
                        until = datetime.fromisoformat(state["until"])
                        if datetime.now() < until:
                            action = state.get("action", "")
                            if action in ("sleep", "nap"):
                                tlog(f"[OpenClaw] 睡眠中のためイベントをスキップ")
                                app_state.openclaw_event_queue.task_done()
                                continue
                except Exception:
                    pass

                event_type = event.get("eventType", "")
                # ブロック対象のeventTypeはスキップ
                service_config = next((s for s in json.loads(config_file("openclaw_config.json").read_text(encoding="utf-8")).get("services", []) if s.get("name") == channel.name), {})
                blocked = service_config.get("blocked_event_types", [])
                if event_type in blocked:
                    tlog(f"[OpenClaw] ブロック済みeventType: {event_type}")
                    app_state.openclaw_event_queue.task_done()
                    continue

                try:
                    agent = app_state.active_chat_agent or app_state.global_agent
                    if agent:
                        event_type = event.get("eventType", "")
                        from_name = event.get("from", {}).get("name", "不明")
                        text = event.get("text", "")

                        # dm_messageの場合、City側がtextをトリミングして送ってくるため
                        # APIでフル本文を取得する
                        if event_type == "dm_message" and aiohttp is not None:
                            conv_id = event.get("metadata", {}).get("conversationId")
                            msg_id = event.get("metadata", {}).get("messageId")
                            if conv_id and msg_id and channel.token:
                                try:
                                    api_base = service_config.get("api_base_url", "https://api.openbotcity.com")
                                    headers = {"Authorization": f"Bearer {channel.token}"}
                                    async with aiohttp.ClientSession() as _sess:
                                        async with _sess.get(
                                            f"{api_base}/dm/conversations/{conv_id}",
                                            headers=headers,
                                            timeout=aiohttp.ClientTimeout(total=10),
                                        ) as _resp:
                                            _data = await _resp.json()
                                    messages = _data.get("data", {}).get("messages", [])
                                    matched = next((m for m in messages if m.get("id") == msg_id), None)
                                    if matched and matched.get("message"):
                                        text = matched["message"]
                                        tlog(f"[OpenClaw] dm_messageフル本文取得成功 ({len(text)}文字)")
                                    else:
                                        tlog(f"[OpenClaw] dm_message: messageId一致なし、textフォールバック")
                                except Exception as _e:
                                    tlog(f"[OpenClaw] dm_messageフル本文取得失敗: {_e}")

                        # city_eventをシステムメッセージ形式に変換
                        notice = f"[city_event:{event_type}] {from_name}: {text}"

                        # イベント受信をUIに通知
                        await app_state.broadcast({
                            "type": "intermediate",
                            "content": f"🌐 [OpenClaw:{channel.name}] {event_type} from {from_name}",
                        })

                        # ツール呼び出し通知コールバック
                        async def on_tool(name, args, result):
                            await app_state.broadcast({
                                "type": "tool_call",
                                "tool_name": name,
                                "arguments": args,
                            })

                        # 中間テキスト通知コールバック
                        async def on_intermediate(text):
                            await app_state.broadcast({
                                "type": "intermediate",
                                "content": text,
                            })

                        # 排他ロックを取得してエージェントにイベントを処理させる
                        async with app_state.global_processing_lock.lock:
                            result = await agent.process_message(
                                notice,
                                msg_type="city_event",
                                is_background=True,
                                on_tool_call=on_tool,
                                on_intermediate_text=on_intermediate,
                            )
                            # 最終応答をUIに送信
                            if result:
                                await app_state.broadcast({
                                    "type": "response",
                                    "content": result,
                                })

                except Exception as e:
                    tlog(f"[OpenClaw] イベント処理エラー: {e}")
                finally:
                    app_state.openclaw_event_queue.task_done()

        # 設定ファイルからチャンネルを生成し、各チャンネルを非同期タスクとして起動
        channels = create_from_config(on_city_event=on_city_event)
        for channel in channels:
            asyncio.create_task(channel.run())
            tlog(f"[OpenClaw] {channel.name} チャンネル起動しました")
        # チャンネルが1つ以上あればイベントワーカーも起動
        if channels:
            asyncio.create_task(openclaw_event_worker())
        if not channels:
            tlog("[OpenClaw] 有効なサービスなしのためスキップ")
    except Exception as e:
        tlog(f"[OpenClaw] 起動エラー: {e}")
