# core/routes/ws.py
"""WebSocketエンドポイント（メインチャット /ws・デバッグ /ws/debug）。"""

import asyncio
import json
import time
from datetime import datetime

from fastapi import APIRouter, WebSocket, WebSocketDisconnect, status

from core import app_state
from core.auth import verify_session_token, get_cookie_name
from core.config_loader import load_config_strict as load_config
from core.i18n import t
from core.paths import data_file, resolve_workspace
from core.time_utils import JST, tlog

router = APIRouter()


# =============================================================================
# WebSocketエンドポイント（デバッグ用）
# =============================================================================

@router.websocket("/ws/debug")
async def websocket_debug_endpoint(websocket: WebSocket):
    """デバッグ情報（コンテキスト構成・トークン数等）をリアルタイム配信するWebSocket。"""
    # セッションCookieによる認証
    token = websocket.cookies.get(get_cookie_name(), "")
    if not verify_session_token(token):
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return

    await websocket.accept()
    app_state.debug_websockets.add(websocket)

    # 接続時に現在の最新デバッグ情報を即座に送信
    if app_state.global_last_debug_context:
        try:
            await websocket.send_text(json.dumps(app_state.global_last_debug_context, ensure_ascii=False))
        except Exception:
            pass

    try:
        while True:
            # DEBUG画面からのコマンドを受信（基本は切断検知だが、部分Layer0圧縮の指示を受ける）
            raw = await websocket.receive_text()
            try:
                msg = json.loads(raw)
            except (ValueError, TypeError):
                continue

            # === ループ検知: 強化ループの疑いがあるターンを抽出して返す ===
            if isinstance(msg, dict) and msg.get("type") == "detect_loops":
                agent = app_state.active_chat_agent or app_state.global_agent
                if agent is None:
                    await websocket.send_text(json.dumps(
                        {"type": "loops_detected", "loops": [], "message": t("agent_not_running")},
                        ensure_ascii=False))
                    continue
                try:
                    loops = agent.context.detect_loop_turns()
                    await websocket.send_text(json.dumps(
                        {"type": "loops_detected", "loops": loops}, ensure_ascii=False))
                except Exception as e:
                    print(f"ループ検知エラー: {e}")
                    await websocket.send_text(json.dumps(
                        {"type": "loops_detected", "loops": [], "message": f"{t('detect_error')}: {str(e)}"},
                        ensure_ascii=False))
                continue

            # === 部分Layer0圧縮: 狙ったターン（多ステップ）を1ターン1ステップに畳む ===
            # 記憶ファイル（ノート・手紙）には触れない。会話コンテキストのみ。
            if isinstance(msg, dict) and msg.get("type") == "compress_layer0_at":
                agent = app_state.active_chat_agent or app_state.global_agent
                if agent is None:
                    await websocket.send_text(json.dumps(
                        {"type": "compress_result", "success": False, "message": t("agent_not_running")},
                        ensure_ascii=False))
                    continue
                indices = msg.get("history_indices")
                if indices is None and msg.get("history_idx") is not None:
                    indices = [msg.get("history_idx")]
                indices = indices or []
                try:
                    # 通常ターン・バックグラウンドタスクと直列化（会話履歴の同時変更を防ぐ）
                    async with app_state.global_processing_lock.lock:
                        result = await agent.compress_layer0_at(indices)
                    # 結果をDEBUGページに通知（コンテキスト自体は_update_debug_context経由で自動再描画される）
                    await websocket.send_text(json.dumps({
                        "type": "compress_result",
                        "success": result["success"],
                        "message": result["message"],
                        "token_usage": result["token_usage"],
                    }, ensure_ascii=False))
                    # チャットUI側のトークン表示も更新
                    await app_state.broadcast({
                        "type": "token_update",
                        "token_usage": result["token_usage"],
                    })
                except Exception as e:
                    print(f"部分Layer0圧縮エラー: {e}")
                    await websocket.send_text(json.dumps({
                        "type": "compress_result",
                        "success": False,
                        "message": f"{t('compress_layer0_error')}: {str(e)}",
                    }, ensure_ascii=False))
                continue

            # === メッセージ本文のインライン編集 ===
            if isinstance(msg, dict) and msg.get("type") == "edit_message":
                agent = app_state.active_chat_agent or app_state.global_agent
                if agent is None:
                    await websocket.send_text(json.dumps(
                        {"type": "edit_result", "success": False, "message": t("agent_not_running")},
                        ensure_ascii=False))
                    continue
                try:
                    async with app_state.global_processing_lock.lock:
                        r = await agent.edit_history_message(
                            msg.get("history_idx"),
                            msg.get("new_content", ""),
                            msg.get("expected_content"),
                        )
                    await websocket.send_text(json.dumps({"type": "edit_result", **r}, ensure_ascii=False))
                    if r.get("success"):
                        await app_state.broadcast({"type": "token_update", "token_usage": r.get("token_usage")})
                except Exception as e:
                    print(f"メッセージ編集エラー: {e}")
                    await websocket.send_text(json.dumps(
                        {"type": "edit_result", "success": False, "message": f"{t('edit_error')}: {str(e)}"},
                        ensure_ascii=False))
                continue

            # === 検索置換のプレビュー（件数のみ・実体は変更しない / ロック不要）===
            if isinstance(msg, dict) and msg.get("type") == "replace_preview":
                agent = app_state.active_chat_agent or app_state.global_agent
                if agent is None:
                    await websocket.send_text(json.dumps(
                        {"type": "replace_preview_result", "success": False, "count": 0, "message": t("agent_not_running")},
                        ensure_ascii=False))
                    continue
                try:
                    r = await agent.replace_history_text(
                        find=msg.get("find", ""),
                        replacement=msg.get("replacement", ""),
                        start_idx=msg.get("start_idx"),
                        end_idx=msg.get("end_idx"),
                        use_regex=bool(msg.get("use_regex")),
                        case_sensitive=bool(msg.get("case_sensitive")),
                        dry_run=True,
                    )
                    await websocket.send_text(json.dumps({"type": "replace_preview_result", **r}, ensure_ascii=False))
                except Exception as e:
                    print(f"置換プレビューエラー: {e}")
                    await websocket.send_text(json.dumps(
                        {"type": "replace_preview_result", "success": False, "count": 0, "message": f"{t('preview_error')}: {str(e)}"},
                        ensure_ascii=False))
                continue

            # === 検索置換の実行（1件 or 範囲一括）===
            if isinstance(msg, dict) and msg.get("type") in ("replace_one", "replace_apply"):
                agent = app_state.active_chat_agent or app_state.global_agent
                if agent is None:
                    await websocket.send_text(json.dumps(
                        {"type": "edit_result", "success": False, "message": t("agent_not_running")},
                        ensure_ascii=False))
                    continue
                is_one = msg.get("type") == "replace_one"
                try:
                    async with app_state.global_processing_lock.lock:
                        if is_one:
                            r = await agent.replace_history_text(
                                find=msg.get("find", ""),
                                replacement=msg.get("replacement", ""),
                                start_idx=msg.get("history_idx"),
                                occurrence=msg.get("occurrence"),
                                use_regex=bool(msg.get("use_regex")),
                                case_sensitive=bool(msg.get("case_sensitive")),
                            )
                        else:
                            # 範囲一括: プレビュー件数（expected_count）と現状を再検証してから実置換
                            expected = msg.get("expected_count")
                            pre = await agent.replace_history_text(
                                find=msg.get("find", ""),
                                replacement=msg.get("replacement", ""),
                                start_idx=msg.get("start_idx"),
                                end_idx=msg.get("end_idx"),
                                use_regex=bool(msg.get("use_regex")),
                                case_sensitive=bool(msg.get("case_sensitive")),
                                dry_run=True,
                            )
                            if expected is not None and pre.get("count") != expected:
                                r = {"success": False, "stale": True,
                                     "message": t("count_changed_refresh"),
                                     "count": pre.get("count", 0),
                                     "token_usage": pre.get("token_usage")}
                            else:
                                r = await agent.replace_history_text(
                                    find=msg.get("find", ""),
                                    replacement=msg.get("replacement", ""),
                                    start_idx=msg.get("start_idx"),
                                    end_idx=msg.get("end_idx"),
                                    use_regex=bool(msg.get("use_regex")),
                                    case_sensitive=bool(msg.get("case_sensitive")),
                                )
                    await websocket.send_text(json.dumps({"type": "edit_result", **r}, ensure_ascii=False))
                    if r.get("success"):
                        await app_state.broadcast({"type": "token_update", "token_usage": r.get("token_usage")})
                except Exception as e:
                    print(f"置換エラー: {e}")
                    await websocket.send_text(json.dumps(
                        {"type": "edit_result", "success": False, "message": f"{t('replace_error')}: {str(e)}"},
                        ensure_ascii=False))
                continue
    except WebSocketDisconnect:
        pass
    finally:
        if websocket in app_state.debug_websockets:
            app_state.debug_websockets.remove(websocket)


# =============================================================================
# WebSocketエンドポイント（メインチャット）
# =============================================================================

@router.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    """
    メインのチャットWebSocketエンドポイント。
    ブラウザのチャットUIとエージェント間の双方向通信を処理する。

    受信メッセージ形式:
        {"type": "message", "content": "ユーザーの発言",
         "images": ["data:image/...;base64,..."],            # 画像（複数可）は任意
         "files": [{"name": "memo.txt", "content": "..."}]}  # テキストファイル（複数可）は任意
        # 旧形式 "image": "base64..." も後方互換で受け付ける
        {"type": "cancel"}    # 応答中断リクエスト
        {"type": "compress"}  # 手動コンテキスト圧縮リクエスト

    送信メッセージ形式:
        {"type": "response", "content": "エージェントの応答", "token_usage": {...}}
        {"type": "tool_call", "tool_name": "...", "arguments": {...}, "token_usage": {...}}
        {"type": "intermediate", "content": "ツールループ中の中間テキスト"}
        {"type": "system", "content": "システムメッセージ（自動続行通知等）"}
        {"type": "error", "content": "エラーメッセージ"}
        {"type": "token_update", "token_usage": {...}}
        {"type": "compress_result", "success": bool, "message": "...", "token_usage": {...}}
    """
    # --- Originヘッダー検証（Cross-Site WebSocket Hijacking対策） ---
    origin = websocket.headers.get("origin")
    config = load_config()
    server_host = config.get("server", {}).get("host", "127.0.0.1")
    server_port = config.get("server", {}).get("port", 8080)

    allowed_origins = [
        "http://localhost:8080",
        "http://127.0.0.1:8080",
        f"http://{server_host}:{server_port}",
    ]
    # マシン固有の追加許可Origin（Tailscale等のリモートアクセス用）は
    # settings.json / config.yaml の server.extra_allowed_origins で指定する。
    extra_origins = config.get("server", {}).get("extra_allowed_origins", [])
    if isinstance(extra_origins, list):
        allowed_origins.extend(str(o) for o in extra_origins)

    if not origin or origin not in allowed_origins:
        print(f"WebSocket接続拒否: 不正なOrigin ({origin})")
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return

    # --- セッションCookie検証 ---
    token = websocket.cookies.get(get_cookie_name(), "")
    if not verify_session_token(token):
        print("WebSocket接続拒否: 未認証")
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return

    await websocket.accept()
    app_state.active_chat_websockets.add(websocket)

    # --- 接続時に直近の会話履歴を送信（context_state.jsonベース） ---
    # 履歴をitems配列にまとめて1メッセージで一括送信する（旧: 1要素ずつ送信していたが、
    # 件数が多いとUIで「だーっと流れる」体感の悪さがあったため、バッチ化して高速化）。
    # 上限を設けて直近MAX件のみを返し、超過分は先頭にマーカーを置いて /logs へ誘導する。
    HISTORY_MAX_ITEMS = 200
    try:
        import json as _json
        import re as _re

        state_path = data_file("context_state.json")
        if state_path.exists():
            state = _json.loads(state_path.read_text(encoding="utf-8"))
            history = state.get("conversation_history", [])

            # [SYSTEM]ヘッダから時刻 (例: "15:03") を抽出する正規表現。
            # メッセージ自体にはタイムスタンプフィールドが無いため、直近の[SYSTEM]時刻を引き継ぐ。
            _time_re = _re.compile(r'\[SYSTEM\][^\n]*\n(\d{4})年(\d{2})月(\d{2})日[^\n]*?(\d{2}):(\d{2}):\d{2}')
            # Layer0圧縮済みメッセージは [SYSTEM] ヘッダを持たず、先頭行が
            # "YYYY-MM-DD HH:MM"（_format_user_for_layer0 の出力。秒なし・ダッシュ区切り）。
            # これを読めないと圧縮ターンの時刻が直前のRAW時刻を引き継いでしまう（タイムスリップ表示）。
            _time_re_layer0 = _re.compile(r'(?:^|\n)(\d{4})-(\d{2})-(\d{2})\s+(\d{2}):(\d{2})\b')
            current_time = None

            def _extract_time(text: str):
                m = _time_re.search(text)
                if m:
                    return f"{m.group(4)}:{m.group(5)}"
                # Layer0圧縮済みメッセージのみ、ダッシュ形式の時刻をフォールバックで読む
                if "<!-- layer0 -->" in text:
                    m2 = _time_re_layer0.search(text)
                    if m2:
                        return f"{m2.group(4)}:{m2.group(5)}"
                return None

            items: list[dict] = []
            for msg in history:
                role = msg.get("role")
                content_raw = msg.get("content", "")
                # content が None（ツールだけ使い発言しなかったターン）の場合、
                # str(None) は文字列 "None" を生むため空文字に正規化する。
                # これをしないと履歴再構築時に "None" の吹き出しが現れる。
                content_str = "" if content_raw is None else str(content_raw)

                if role == "user":
                    t = _extract_time(content_str)
                    if t:
                        current_time = t
                    if "<!-- layer0 -->" in content_str:
                        if "moonbeat" in content_str.lower():
                            items.append({"kind": "intermediate", "content": "🌙 [Moonbeat]"})
                        continue
                    if "<task_notice>" in content_str:
                        tm = _re.search(r'タスク名:\s*([^\n]+)', content_str)
                        task_name = tm.group(1).strip() if tm else "?"
                        items.append({"kind": "intermediate", "content": f"📅 [{task_name}] スケジュール実行"})
                        continue
                    if "<city_event_notice>" in content_str:
                        cm = _re.search(r'\[city_event:([^\]]+)\]\s*([^:\n]+?):', content_str)
                        if cm:
                            event_type = cm.group(1).strip()
                            from_name = cm.group(2).strip()
                            marker = f"🌐 [OpenClaw] {event_type} from {from_name}"
                        else:
                            marker = "🌐 [OpenClaw] event"
                        items.append({"kind": "intermediate", "content": marker})
                        continue
                    if "<moonbeat_instruction>" in content_str:
                        items.append({"kind": "intermediate", "content": "🌙 [Moonbeat]"})
                        continue
                    m = _re.search(r'<user_message>(.*?)</user_message>', content_str, _re.DOTALL)
                    if m:
                        items.append({
                            "kind": "message",
                            "role": "user",
                            "content": m.group(1).strip(),
                            "time": current_time,
                        })
                    continue

                if role == "assistant":
                    text = content_str.strip()
                    if text:
                        items.append({
                            "kind": "message",
                            "role": "assistant",
                            "content": text,
                            "time": current_time,
                        })
                    for tc in (msg.get("tool_calls") or []):
                        try:
                            fn = tc.get("function", {}) or {}
                            name = fn.get("name", "")
                            args_raw = fn.get("arguments", "")
                            try:
                                args = _json.loads(args_raw) if isinstance(args_raw, str) else (args_raw or {})
                            except Exception:
                                args = {}
                            items.append({
                                "kind": "tool_call",
                                "tool_name": name,
                                "arguments": args,
                            })
                        except Exception:
                            pass
                    continue

                # role == "tool" はスキップ（🔧 履歴行と情報が重複するため）

            # 直近のみに絞り、はみ出した場合は先頭に誘導マーカーを差し込む
            truncated = False
            if len(items) > HISTORY_MAX_ITEMS:
                items = items[-HISTORY_MAX_ITEMS:]
                truncated = True
            if truncated:
                items.insert(0, {
                    "kind": "marker",
                    "content": f"— これより前の会話は /logs で閲覧できます（直近{HISTORY_MAX_ITEMS}件のみ表示）—",
                })

            # クライアントはここで localStorage と表示をクリアする
            await websocket.send_text(json.dumps({"type": "history_start"}, ensure_ascii=False))
            await websocket.send_text(json.dumps({"type": "history_batch", "items": items}, ensure_ascii=False))
            await websocket.send_text(json.dumps({"type": "history_end"}, ensure_ascii=False))
    except Exception as e:
        print(f"[WS] 履歴送信エラー: {e}")

    # --- global_agentをチャットセッションのエージェントとして登録 ---
    app_state.active_chat_agent = app_state.global_agent
    agent = app_state.global_agent

    # 応答処理タスクの管理。
    # 処理を受信ループとは別のタスクで走らせることで、応答中でも同じクライアントから
    # cancel（停止ボタン）を即座に受信できる（従来はループ内で直接awaitしていたため、
    # 応答が終わるまでcancelフレームを読み取れず、停止ボタンが効かなかった）。
    chat_tasks: set = set()          # 実行中タスクへの参照（GC防止）
    pending_turns: list = []         # 未完了ターンの状態。ロック待ち中の取り消しに使う

    # デバッグ用のコンテキスト情報を初期送信
    await agent._update_debug_context()

    print(f"[WS] クライアント接続 ({app_state.global_agent.agent_name if app_state.global_agent else 'unknown'})")

    try:
        # 接続直後にトークン使用状況を送信（UIのトークンバー初期化用）
        token_usage = agent.context.get_token_usage()

        # --- メインメッセージ受信ループ ---
        while True:
            data = await websocket.receive_text()
            msg = json.loads(data)

            # === ユーザーメッセージの処理 ===
            if msg["type"] == "message":
                user_text = msg["content"]
                user_image = msg.get("image")  # 画像データ（base64 data URL、任意・後方互換の単一画像）
                user_images = msg.get("images")  # 画像データのリスト（複数画像添付、任意）
                user_files = msg.get("files")  # 添付テキストファイル [{"name","content"}, ...]（任意）
                app_state.last_chat_time = time.time()

                # ターン開始時刻（HH:MM）。長いターンでも応答が「今」ではなくターン時刻で表示されるよう、
                # ライブ送信イベントに添える。リロード後の履歴表示（[SYSTEM]ヘッダ由来）とも一致する。
                turn_time = datetime.now(JST).strftime("%H:%M")

                # ユーザーメッセージを全クライアントにbroadcast（スマホ↔PC同期）
                await app_state.broadcast({
                    "type": "user_message",
                    "content": user_text,
                    "msg_id": msg.get("msg_id", ""),
                })

                print(f"[WS] user_message broadcast: {len(app_state.active_chat_websockets)}クライアント")

                # 生活行動中（睡眠等）にユーザーが話しかけた場合、起床処理を行う
                try:
                    state_path = resolve_workspace(config) / ".life_action_state.json"
                    if state_path.exists():
                        state = json.loads(state_path.read_text(encoding="utf-8"))
                        until = datetime.fromisoformat(state["until"])
                        if datetime.now() < until and state.get("wake_on_user", True):
                            action = state.get("action", "不明")
                            state_path.unlink()
                            # 状態ファイル削除の瞬間はファイルログに必ず残す（print のみだと
                            # コンソールを閉じた後に「いつ・なぜ起床したか」を追跡できないため）
                            tlog(f"[LifeAction] ユーザーの呼びかけにより起床 ({action}, 予定では {state.get('until', '?')} まで) message={user_text[:50]!r}")
                            # 仮眠中の起床も気分回復対象
                            if action == "nap" and app_state.global_agent and app_state.global_agent.vital_manager:
                                vm = app_state.global_agent.vital_manager
                                if vm.moodphase:
                                    vm.moodphase.recover_from_nap()
                                if hasattr(vm, 'moontide') and vm.moontide:
                                    vm.moontide.recover_from_nap()
                except Exception as e:
                    print(f"[LifeAction] 起床チェックでエラー: {e}")

                # このターンの状態。ロック待ち中に停止ボタンが押された場合の取り消しに使う
                turn_state = {"cancelled": False}
                pending_turns.append(turn_state)

                # --- 応答処理タスク ---
                # 受信ループを塞がないよう別タスクで実行する。引数のデフォルト値で
                # ターン固有の値を束縛するのは、処理中に次のメッセージを受信すると
                # ループ変数（user_text等）が上書きされてしまうため。
                async def process_turn(user_text=user_text, user_image=user_image,
                                       user_images=user_images, user_files=user_files,
                                       turn_time=turn_time, turn_state=turn_state):
                    # ツール実行時にUIへ通知するコールバック
                    async def on_tool(name, args, result):
                        token_usage = agent.context.get_token_usage()
                        await app_state.broadcast({
                            "type": "tool_call",
                            "tool_name": name,
                            "arguments": args,
                            "token_usage": token_usage,
                        })

                    # システムメッセージ（自動続行通知等）をUIへ通知するコールバック
                    async def on_system(content):
                        await app_state.broadcast({
                            "type": "system",
                            "content": content,
                        })

                    # ツールループ中の中間テキスト（LLMがツール呼び出し前に出力したテキスト）をUIへ通知するコールバック
                    async def on_intermediate(text):
                        await app_state.broadcast({
                            "type": "intermediate",
                            "content": text,
                            "time": turn_time,
                        })

                    # ストリーミング配信コールバック。agent からのイベントを
                    # stream_begin / stream_delta / stream_reset / stream_end として全クライアントに流す。
                    # ストリームで流れるのはサニタイズ済みの暫定テキストで、直後に来る
                    # intermediate / response が正規テキスト（クライアント側で置換する）。
                    async def on_stream(ev: dict):
                        payload = {
                            "type": f"stream_{ev['event']}",
                            "stream_id": ev["stream_id"],
                            "time": turn_time,
                        }
                        if "text" in ev:
                            payload["content"] = ev["text"]
                        if ev.get("aborted"):
                            payload["aborted"] = True
                        await app_state.broadcast(payload)

                    plock = app_state.global_processing_lock
                    try:
                        # デバッグ: システムプロンプトの先頭100文字をコンソールに表示
                        msgs = agent.context.build_messages()
                        print(f"[DEBUG] system[0] 先頭100文字: {msgs[0]['content'][:100]}")

                        # バックグラウンドタスクが実行中であれば中断シグナルを送る
                        # （ユーザー会話の処理中には割り込まず、終了を待って順番に処理する）
                        if plock.lock.locked() and not plock.chat_turn_active:
                            plock.interrupt_flag = True
                            print("【システム】処理の中断を要求しました...待機します。")

                        # 排他ロックを取得して会話を処理する（バックグラウンドタスク完了を待つ）
                        async with plock.lock:
                            # ロック待ちの間に停止ボタンが押されていたら、このターンは処理しない
                            if turn_state["cancelled"]:
                                print("【システム】中断要求により、このターンの処理を取り消しました。")
                                return

                            # ユーザーの処理が中断されないようにフラグをリセット
                            plock.interrupt_flag = False
                            plock.chat_turn_active = True
                            try:
                                # エージェントにメッセージを処理させる（ツールループを含む）
                                response_text = await agent.process_message(
                                    user_text, image=user_image,
                                    images=user_images, files=user_files,
                                    on_tool_call=on_tool,
                                    on_system_message=on_system,
                                    on_intermediate_text=on_intermediate,
                                    on_stream_event=on_stream
                                )
                            finally:
                                plock.chat_turn_active = False

                        # トークン使用状況を取得（LLM送信時の正確な値があれば上書き）
                        token_usage = agent.context.get_token_usage()
                        if agent.last_debug_context and "total_tokens" in agent.last_debug_context:
                            token_usage["used"] = agent.last_debug_context["total_tokens"]

                        # 最終応答をUIに送信
                        await app_state.broadcast({
                            "type": "response",
                            "content": response_text,
                            "token_usage": token_usage,
                            "time": turn_time,
                        })

                    except Exception as e:
                        # エラーをUIに通知（チャットは継続可能）
                        print(f"エラー: {e}")
                        await app_state.broadcast({
                            "type": "error",
                            "content": f"エラーが発生しました: {str(e)}",
                        })
                    finally:
                        if turn_state in pending_turns:
                            pending_turns.remove(turn_state)

                task = asyncio.create_task(process_turn())
                chat_tasks.add(task)
                task.add_done_callback(chat_tasks.discard)

            # === ユーザーによる応答中断リクエスト ===
            elif msg["type"] == "cancel":
                print("【システム】ユーザーによる中断要求を受信しました。")
                # ロック待ち中（処理未開始）のターンは処理自体を取り消す
                for ts in pending_turns:
                    ts["cancelled"] = True
                # 実行中の処理があれば中断フラグを立てる。
                # 何も走っていない時に立てると、次のターンやバックグラウンドタスクを
                # 誤って中断してしまうため、ロック保持中に限定する。
                if app_state.global_processing_lock.lock.locked():
                    app_state.global_processing_lock.interrupt_flag = True

            # === 手動コンテキスト圧縮リクエスト ===
            elif msg["type"] == "compress":
                try:
                    compress_count = msg.get("count", None)
                    result = await agent.force_compress(count=compress_count)
                    token_usage = result["token_usage"]
                    if agent.last_debug_context and "total_tokens" in agent.last_debug_context:
                        token_usage["used"] = agent.last_debug_context["total_tokens"]

                    await app_state.broadcast({
                        "type": "compress_result",
                        "success": result["success"],
                        "message": result["message"],
                        "token_usage": token_usage,
                    })
                except Exception as e:
                    print(f"圧縮エラー: {e}")
                    token_usage = agent.context.get_token_usage()
                    if agent.last_debug_context and "total_tokens" in agent.last_debug_context:
                        token_usage["used"] = agent.last_debug_context["total_tokens"]

                    await app_state.broadcast({
                        "type": "compress_result",
                        "success": False,
                        "message": f"{t('compress_error')}: {str(e)}",
                        "token_usage": token_usage,
                    })

    except WebSocketDisconnect:
        app_state.active_chat_websockets.discard(websocket)
        print("クライアントが切断しました")
