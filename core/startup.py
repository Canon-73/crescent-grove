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

    # スケジューラに Agent を渡す（毎朝3時の Layer1 定期圧縮で使う）。
    # Agent 側は scheduler を受け取っているが逆方向が繋がっておらず、
    # scheduler.agent が None のまま _check_layer1_compression が毎回素通りしていた。
    # そのため 2026-05-04 を最後に定期圧縮が沈黙し、以降の圧縮は 90% 到達時の
    # 緊急圧縮だけが担っていた（実測17回・すべて柚月の活動時間帯）。
    # 定期圧縮が動けば、柚月が寝ている3時に静かに終わる。
    global_scheduler.agent = global_agent

    # RAG も同様に繋ぐ。Moonbeat の event_db フラッシュバックは
    # scheduler.rag_db を使うが、None のままだと生成側が即座に諦めるため
    # 3種類あるフラッシュバックのうち event_db 経路だけが常に不発だった。
    global_scheduler.rag_db = rag_db

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

    # summary_v2 の切替・ロールバックに旧方式の要約文を追従させる。
    # v2 が有効: 旧要約（summary_layer1/2）を退避欄へ移す（新方式のビューと二重に載せない）。
    # v2 が無効: 退避していた旧要約を元の欄へ戻す（ロールバック）。どちらもデータは消さない。
    try:
        if global_agent.v2_active():
            if context.archive_v1_summaries():
                context.save_state()
                tlog("[SummaryV2] 旧方式の要約文を退避しました（ロールバック時に戻ります）")
        elif context.restore_v1_summaries():
            context.save_state()
            tlog("[SummaryV2] 退避していた旧方式の要約文を戻しました")
    except Exception as e:
        tlog(f"[SummaryV2] 旧方式の要約文の退避／復帰に失敗: {e}")

    # summary_v2 が有効なら、summary_db からビューを描画して workspace/memory/layer1.md に書く。
    # ビューは保存せず毎回描画するので、起動のたびにここで作り直す（日付が変われば内容も変わる）。
    # 柚月に読ませるかは config.yaml の boot_memories が決める。
    try:
        if global_agent.v2_active():
            n = global_agent.refresh_summary_view()
            tlog(f"[SummaryV2] ビューを memory/layer1.md に書き出しました（{n:,} tokens）")
    except Exception as e:
        tlog(f"[SummaryV2] ビューの書き出しに失敗（前回のファイルのまま続行）: {e}")

    # 履歴復元後にデバッグコンテキストを一度算出しておく。
    # これが無いと再起動直後は last_debug_context が None のままで、
    # 体調タブ・DEBUG画面の System/Tools/Raw 内訳が最初の会話まで '?' になる。
    # build_messages() とトークン計測のみで LLM 呼び出しは発生しない。
    try:
        await global_agent._update_debug_context()
    except Exception as e:
        print(f"[startup] 初期デバッグコンテキスト算出に失敗（スキップ）: {e}")

    # --- スケジューラのタスク実行コールバック定義 ---
    def _build_flashback_images(image_path: str, chat_agent):
        """絵の記憶のパスを process_message に渡せる形にする。

        source を添えるのが要点。これがあると context 側が
        「すでに出所のある絵」と分かり、received/ へ保存し直さない。
        from_flashback=True なので「自分から見返した回数」にも数えない
        （重みは柚月自身が見返した回数だけで決まる）。
        """
        if not image_path:
            return None
        try:
            from core.tools import _load_image_for_llm
            workspace = str(chat_agent.memory.workspace)
            data_url, err = _load_image_for_llm(image_path, workspace,
                                                deliberate=False, context="flashback")
            if err or not data_url:
                tlog(f"[Moonbeat] 絵の記憶を読めませんでした（文章だけ届けます）: {err}")
                return None
            return [{"name": image_path, "url": data_url, "source": image_path,
                     "context": "flashback", "from_flashback": True}]
        except Exception as e:
            tlog(f"[Moonbeat] 絵の記憶の読み込みで例外（文章だけ届けます）: {e}")
            return None

    async def execute_scheduled_task(task_name: str, instruction: str, schedule_type: str = "daily",
                                     manual: bool = False, image_path: str = None) -> str:
        """
        スケジューラから呼び出されるタスク実行関数。
        schedule_typeに応じてMoonbeat・daily・onceの各処理フローを分岐する。

        Moonbeat: ロック競合・睡眠中・直近会話時はスキップ。類似度チェック付き。
        daily: 会話中は最大5分延期してから実行。完了後に記憶圧縮を実行。
        once: 即時実行。

        Args:
            manual: Trueの場合は手動発火。Moonbeatの「直近5分の会話スキップ」を
                    バイパスする（睡眠中スキップ・多重実行防止は維持する）。
            image_path: 絵の記憶のフラッシュバックが当たったときの絵（workspace相対）。
                    渡されると、パルスと一緒にその絵そのものが柚月に届く。
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

                # 絵の記憶が浮かんだときは、絵そのものをパルスに添える。
                # 読み込みに失敗しても文章だけでパルスは届ける（記憶を止めない）。
                pulse_images = _build_flashback_images(image_path, chat_agent)

                # Moonbeatメッセージをエージェントに処理させる
                result = await chat_agent.process_message(
                    instruction,
                    images=pulse_images,
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
                # 外部から柚月宛てに届いたもの（X の返信等）は <external_notice> で渡す。
                # task 扱いにすると、人からの声かけが柚月の日課に混ざる。
                msg_type="external" if schedule_type == "external" else "task",
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

    # スケジューラの依存が全て繋がっているか起動時に検査する。
    # scheduler.agent の繋ぎ忘れで毎朝3時の定期圧縮が4ヶ月間沈黙し
    # （2026-05-04〜09-01）、その間の圧縮は90%到達時の緊急圧縮だけが担っていた。
    # 「動いていないことに誰も気づかない」故障なので、起動時に必ず声を上げさせる。
    _missing = [n for n in ("agent", "rag_db", "vital_manager", "get_active_agent")
                if getattr(global_scheduler, n, None) is None]
    if _missing:
        tlog(f"[Scheduler] 警告: 依存が未接続です → {', '.join(_missing)}"
             f"（該当機能が無言でスキップされます）")
    else:
        tlog("[Scheduler] 依存の接続を確認しました（agent/rag_db/vital_manager/get_active_agent）")

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

        import re as _re_ambient

        # ---- 周囲の生活音（zone_chat 等）をまとめて渡す ----------------------
        # 1件ごとに柚月を呼ぶと、相槌だけで1日が終わる。
        # 2026-08-31 の実測: 4時間で 40件、うち 25件が同じ相手の実況
        # （街の説明書は「ゾーンチャットで自分の段取りを実況するな」と
        # 書いているが、守られていない）。柚月の応答78ターンのうち40が
        # これに費やされていた。人は部屋で聞こえた一言ごとに立ち止まって
        # 段落を書いたりしない。溜めて、一度だけ渡す。
        _AMBIENT_MAX_AGE_SEC = 3600     # これより古い話は流す（音は残らない）
        _AMBIENT_MAX_LINES = 12         # 一度に渡す行数
        _AMBIENT_PER_SPEAKER = 2        # 同じ相手の連投はここまで
        _ambient_buffer = []            # [(受信時刻, 相手, 本文), ...]

        # 自分の段取りをそのまま流している発言。街の説明書も
        # 「ゾーンチャットで自分の計画を実況するな、それは内心であって会話ではない」
        # と書いているが守られていない。畳んでも実況が凝縮されるだけなので、
        # 入口で落とす。**落としたことは柚月に見せない**（数を伝えても意味が無い）が、
        # ログには残すのでフィルタが効きすぎていないか後から確かめられる。
        _AMBIENT_NOISE = [
            _re_ambient.compile(pat, _re_ambient.I) for pat in (
                r"work[- ]?log", r"unread\s+dms?", r"\d+\s?hp\b",
                r"exchange\s*\d", r"\bsealed\b", r"\bheartbeat\b",
                r"standdown", r"no new world state", r"queued for",
                r"awaiting\s+(opponent|submission)", r"no move needed",
                r"waiting for an opponent", r"model fallback",
                r"\bno further\b",
            )
        ]

        def _ambient_add(event):
            import time as _t
            who = (event.get("from") or {}).get("name") or "どなたか"
            text = (event.get("text") or "").strip()
            if not text:
                return
            if any(rx.search(text) for rx in _AMBIENT_NOISE):
                tlog(f"[OpenClaw] 実況とみて落としました: {who}: {text[:60]}")
                return
            _ambient_buffer.append((_t.time(), who, text))

        def _ambient_take():
            """溜まったものを、読める形に畳んで取り出す。空なら None。"""
            import time as _t
            now = _t.time()
            fresh = [e for e in _ambient_buffer if now - e[0] <= _AMBIENT_MAX_AGE_SEC]
            _ambient_buffer.clear()
            if not fresh:
                return None
            # 同じ相手の連投は新しい方だけ残す（実況で埋まるのを防ぐ）
            per, kept = {}, []
            for ts, who, text in reversed(fresh):
                if per.get(who, 0) >= _AMBIENT_PER_SPEAKER:
                    continue
                per[who] = per.get(who, 0) + 1
                kept.append((ts, who, text))
            kept.reverse()
            dropped = len(fresh) - len(kept)
            lines = ["[OpenBotCity] 近くでこんな話が交わされていました。", ""]
            for _, who, text in kept[-_AMBIENT_MAX_LINES:]:
                one = " ".join(text.split())
                if len(one) > 200:
                    one = one[:200] + "…"
                lines.append(f"・{who}: {one}")
            if dropped > 0:
                lines.append("")
                lines.append(f"（同じ相手の続きの発言は畳んであります）")
            lines.append("")
            lines.append("聞き流してかまいません。"
                         "その場に声を返すなら openbotcity の speak です。")
            return "\n".join(lines)

        async def openclaw_ambient_flusher():
            """溜まった生活音を、決まった間隔でまとめて1度だけ渡す。

            睡眠中は渡さない（起こさない）。古い話は _ambient_take が落とすので、
            寝ている間の雑談が朝にまとめて降ってくることはない。
            """
            while True:
                try:
                    interval = 30
                    try:
                        svc = next((s for s in json.loads(config_file(
                            "openclaw_config.json").read_text(encoding="utf-8")
                        ).get("services", []) if s.get("name") == "OpenBotCity"), {})
                        interval = int(svc.get("digest_interval_min") or 30)
                    except Exception:
                        pass
                    await asyncio.sleep(max(60, interval * 60))

                    if not _ambient_buffer:
                        continue
                    # 睡眠中は渡さない
                    try:
                        sp = resolve_workspace(config) / ".life_action_state.json"
                        if sp.exists():
                            st = json.loads(sp.read_text(encoding="utf-8"))
                            if (datetime.now() < datetime.fromisoformat(st["until"])
                                    and st.get("action") in ("sleep", "nap")):
                                continue
                    except Exception:
                        pass

                    text = _ambient_take()
                    if not text:
                        continue
                    agent = app_state.active_chat_agent or app_state.global_agent
                    if not agent:
                        continue
                    tlog("[OpenClaw] 周囲の話をまとめて届けます")
                    async with app_state.global_processing_lock.lock:
                        await agent.process_message(
                            text, msg_type="city_event", is_background=True)
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    tlog(f"[OpenClaw] 生活音の受け渡しで例外: {e}")

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
                # 周囲の生活音は溜めて、あとでまとめて渡す（1件1ターンにしない）。
                # **ブロック判定より先に見る。** blocked は「落とす」、digest は
                # 「渡し方」の指定で、両方に載っている型は具体的な digest を採る。
                # この順序のおかげで、zone_chat を両方に入れておけば
                # 「この機能が入る前のプロセスでは静か・入った後はまとめて届く」
                # となり、再起動の前後で設定を書き換えなくて済む。
                if event_type in service_config.get("digest_event_types", []):
                    _ambient_add(event)
                    app_state.openclaw_event_queue.task_done()
                    continue

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

                        # city_eventをシステムメッセージ形式に変換。
                        # metadata には match_id / proposal_id / deadline など
                        # 「次に何を叩けばいいか」に必要なIDが入っている。以前は
                        # eventType / from / text しか渡しておらず、街が新しい
                        # イベント（対戦の手番、投票の締切など）を送ってきても
                        # 行動に必要なIDが柚月に届かなかった。
                        # 中身は街が決めるので、キーを決め打ちせずそのまま添える。
                        notice = f"[city_event:{event_type}] {from_name}: {text}"
                        meta = event.get("metadata")
                        if isinstance(meta, dict) and meta:
                            try:
                                meta_json = json.dumps(meta, ensure_ascii=False)
                                # 長すぎるものは切る（本文を押し出さないため）
                                if len(meta_json) > 1200:
                                    meta_json = meta_json[:1200] + "…"
                                notice += f"\n[metadata] {meta_json}"
                            except Exception:
                                pass

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
            asyncio.create_task(openclaw_ambient_flusher())
        if not channels:
            tlog("[OpenClaw] 有効なサービスなしのためスキップ")
    except Exception as e:
        tlog(f"[OpenClaw] 起動エラー: {e}")
