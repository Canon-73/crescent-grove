# core/routes/dashboard_api.py
"""ダッシュボード系API（記憶閲覧・設定情報・画像配信・手動Moonbeat・記憶再構成・デバッグ）。"""

import os
import json
import mimetypes
import urllib.parse
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from core import app_state
from core.i18n import t
from core.config_loader import load_config as core_load_config, load_config_strict as load_config
from core.paths import data_file, resolve_workspace, config_file

router = APIRouter()


# =============================================================================
# 記憶閲覧API
# =============================================================================

@router.get("/api/memory")
async def get_memory():
    """
    記憶ファイル群の内容をJSONで返すAPI。
    ダッシュボードの記憶パネルが定期的にポーリングして表示に使用する。

    返却するデータ:
        - letter: 自分への手紙（letter_for_me.md）
        - notes: 雑記帳の最新5件
        - letters: 日々の手紙（memory/letters/）の最新3件
        - today: 当日の活動ログ（memory/today.md）
        - preferences: 好悪ファイル（PREFERENCES.md）
        - vital: バイタル状態（data/vital.json）
        - moodphase: 気分位相状態（data/moodphase_state.json）
        - desire: 欲求状態（data/desire_state.json + desire_config.json）
        - life_action: 現在の生活行動状態（睡眠中など）
        - schedules: 登録済みスケジュール一覧（schedule.json）
        - token_usage: 現在のコンテキストトークン使用状況（リロード後の表示復元用）
    """
    config = load_config()
    workspace = resolve_workspace(config)
    result = {"letter": None, "notes": [], "letters": [], "today": None, "preferences": None, "vital": None, "schedules": []}

    # --- 自分への手紙（letter_file設定から動的にパスを取得） ---
    letter_file = config.get("letter_file", "memory/letter_for_me.md")
    letter_path = workspace / letter_file
    if letter_path.exists():
        try:
            result["letter"] = letter_path.read_text(encoding="utf-8")
        except Exception:
            result["letter"] = "(読み込みエラー)"

    # --- 雑記帳（notes/ 内のnote_YYYY-MM-DD.md形式のみ、最新5件） ---
    notes_dir = workspace / "notes"
    if notes_dir.exists():
        # note_YYYY-MM-DD.md 形式のファイルのみ対象にする（YUZUKI_NOTE.mdなどは除外）
        note_files = sorted(notes_dir.glob("note_*.md"), reverse=True)[:5]
        for f in note_files:
            try:
                result["notes"].append({
                    "name": f.name,
                    "content": f.read_text(encoding="utf-8")
                })
            except Exception:
                result["notes"].append({"name": f.name, "content": "(読み込みエラー)"})

    # --- 日々の手紙（memory/letters/ 内の最新3件） ---
    letters_dir = workspace / "memory" / "letters"
    if letters_dir.exists():
        letter_files = sorted(letters_dir.glob("*.md"), reverse=True)[:3]
        for f in letter_files:
            try:
                result["letters"].append({
                    "name": f.name,
                    "content": f.read_text(encoding="utf-8")
                })
            except Exception:
                result["letters"].append({"name": f.name, "content": "(読み込みエラー)"})

    # --- 今日の活動ログ（memory/today.md） ---
    today_path = workspace / "memory" / "today.md"
    if today_path.exists():
        try:
            result["today"] = today_path.read_text(encoding="utf-8")
        except Exception:
            result["today"] = "(読み込みエラー)"

    # --- 好悪ファイル（PREFERENCES.md） ---
    prefs_path = workspace / "memory" / "preferences" / "PREFERENCES.md"
    if prefs_path.exists():
        try:
            result["preferences"] = prefs_path.read_text(encoding="utf-8")
        except Exception:
            result["preferences"] = "(読み込みエラー)"

    # --- バイタル状態（data/vital.json） ---
    vital_path = data_file("vital.json")
    if vital_path.exists():
        try:
            import json as _json
            vital_data = _json.loads(vital_path.read_text(encoding="utf-8"))
            result["vital"] = vital_data
        except Exception:
            result["vital"] = None

    # --- 気分位相状態（data/moodphase_state.json） ---
    moodphase_path = data_file("moodphase_state.json")
    if moodphase_path.exists():
        try:
            result["moodphase"] = _json.loads(moodphase_path.read_text(encoding="utf-8"))
        except Exception:
            result["moodphase"] = None

    # --- MoodSAE状態 ---
    result["moodsae"] = None
    global_agent = app_state.global_agent
    if global_agent and hasattr(global_agent, 'vital_manager') and global_agent.vital_manager:
        vm = global_agent.vital_manager
        if hasattr(vm, 'moontide') and vm.moontide:
            state = vm.moontide.get_state()
            result["moodsae"] = {
                "primary": state[0]["name"] if state else "neutral",
                "active": [
                    {"state": s["label"], "name": s["name"], "activation": s["mass"]}
                    for s in state
                ],
            }

    # --- 欲求状態（data/desire_state.json + desire_config.json） ---
    desire_state_path = data_file("desire_state.json")
    desire_config_path = config_file("desire_config.json")
    if desire_state_path.exists():
        try:
            desire_state = _json.loads(desire_state_path.read_text(encoding="utf-8"))
            desire_config = {}
            if desire_config_path.exists():
                desire_config = _json.loads(desire_config_path.read_text(encoding="utf-8")).get("desires", {})
            result["desire"] = {"state": desire_state, "config": desire_config}
        except Exception:
            result["desire"] = None

    # --- 生活行動状態（睡眠中等。期限切れの場合はNullを返す） ---
    life_state_path = resolve_workspace(config) / ".life_action_state.json"
    if life_state_path.exists():
        try:
            life_state = _json.loads(life_state_path.read_text(encoding="utf-8"))
            from datetime import datetime as _dt
            until = _dt.fromisoformat(life_state["until"])
            if _dt.now() < until:
                result["life_action"] = life_state
            else:
                result["life_action"] = None
        except Exception:
            result["life_action"] = None
    else:
        result["life_action"] = None

    # --- 登録済みスケジュール一覧（schedule.json） ---
    schedule_path = workspace / "schedule.json"
    if schedule_path.exists():
        try:
            import json as _json
            sched_data = _json.loads(schedule_path.read_text(encoding="utf-8"))
            result["schedules"] = sched_data if isinstance(sched_data, list) else []
        except Exception:
            result["schedules"] = []
    # --- デバッグコンテキストからトークン情報を追加 ---
    if app_state.global_last_debug_context:
        debug = app_state.global_last_debug_context
        system_tokens = sum(
            m.get("tokens", 0) for m in debug.get("messages", [])
            if m.get("role") == "system"
        )
        result["context_tokens"] = {
            "system": system_tokens,
            "tools": debug.get("tools_tokens", 0),
        }

    # --- 現在のトークン使用状況（リロード直後でも体調タブに表示できるよう同梱） ---
    agent = app_state.active_chat_agent or app_state.global_agent
    if agent is not None and getattr(agent, "context", None):
        try:
            token_usage = agent.context.get_token_usage()
            # LLM送信時の正確な値があれば上書き（ws.py と同じ補正）
            ldc = getattr(agent, "last_debug_context", None)
            if ldc and "total_tokens" in ldc:
                token_usage["used"] = ldc["total_tokens"]
            result["token_usage"] = token_usage
        except Exception:
            pass
    return result


# =============================================================================
# 設定情報API
# =============================================================================

@router.get("/api/config")
async def get_config():
    """
    UI表示用の設定情報を返すAPI。
    主にプロファイル情報（名前、アイコンパス）を提供する。
    ローカルファイルパスのアバターは画像配信API経由のURLに自動変換する。
    """
    config = load_config()
    profile = config.get("profile", {
        "user": {"name": "User", "avatar": ""},
        "agent": {"name": "Agent", "avatar": ""}
    })

    # アバターのパス変換: ローカルパスを /api/local_image 経由のURLに変換する
    for role in ["user", "agent"]:
        if role in profile and "avatar" in profile[role]:
            path_str = profile[role]["avatar"]
            # http/https や /static/ 等で始まらない場合はローカルパスとみなす
            if path_str and not path_str.startswith(("http://", "https://", "/")):
                encoded = urllib.parse.quote(path_str)
                profile[role]["avatar"] = f"/api/local_image?path={encoded}"

    return {"profile": profile}


@router.get("/api/local_image")
async def get_local_image(path: str):
    """ローカルファイルシステム上の画像ファイルを配信する。アバター表示等に使用。"""
    try:
        # パスのクリーニング（引用符の除去）
        clean_path = path.strip('"\'')
        file_path = Path(clean_path)

        if not file_path.exists() or not file_path.is_file():
            raise HTTPException(status_code=404, detail="File not found")

        # 画像ファイルの拡張子のみ許可する
        if file_path.suffix.lower() not in ['.png', '.jpg', '.jpeg', '.gif', '.webp', '.bmp', '.svg']:
            raise HTTPException(status_code=400, detail="Invalid image type")

        media_type, _ = mimetypes.guess_type(file_path)
        return FileResponse(file_path, media_type=media_type or "application/octet-stream")
    except HTTPException:
        raise
    except Exception as e:
        print(f"Error serving image: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


# =============================================================================
# 手動Moonbeat発火
# =============================================================================

@router.post("/api/moonbeat/fire")
async def fire_moonbeat_manual():
    """ダッシュボードからの手動Moonbeat発火。

    enabled/活動時間帯/interval/体力のゲートを無視して即発火する。
    睡眠中はスキップ（起こさない）、他処理の実行中もスキップ。
    発火できた場合はスケジューラのタイマーをリセットし、次回の自動Moonbeatを
    発火時刻から interval 後に揃える。
    """
    if app_state.global_scheduler is None:
        raise HTTPException(status_code=500, detail="Scheduler not initialized")
    result = await app_state.global_scheduler.trigger_manual_moonbeat()
    # result: "fired" / "skipped"(睡眠) / "busy"(処理中) / "no_callback"
    return {"status": result}


# =============================================================================
# 記憶再構成APIエンドポイント
# =============================================================================

class MemoryRebuildRangeReq(BaseModel):
    """記憶再構成リクエストのボディスキーマ。日付範囲を指定する。"""
    start_date: str  # YYYY-MM-DD
    end_date: str    # YYYY-MM-DD


@router.post("/api/memory/rebuild-range")
async def rebuild_memory_range(req: MemoryRebuildRangeReq):
    """指定された日付範囲のログからLETHEによる記憶再圧縮を実行する。"""
    global_agent = app_state.global_agent
    if not global_agent:
        raise HTTPException(status_code=500, detail="Agent not initialized")

    try:
        start_date = datetime.strptime(req.start_date, "%Y-%m-%d").date()
        end_date = datetime.strptime(req.end_date, "%Y-%m-%d").date()
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid date format. Use YYYY-MM-DD.")

    if start_date > end_date:
        raise HTTPException(status_code=400, detail="Start date must be before or equal to end date.")

    from core.compressor import MemoryCompressor
    config = core_load_config()
    workspace_path = str(resolve_workspace(config))

    compressor = MemoryCompressor(global_agent.llm, config)
    processed = await compressor.process_range(start_date, end_date, workspace_path)

    return {"status": "success", "processed": processed}


@router.post("/api/open-workspace")
async def open_workspace():
    """workspace フォルダを OS 標準のファイルマネージャ（Windows ならエクスプローラ）で開く。"""
    config = core_load_config()
    workspace_path = resolve_workspace(config)
    if not workspace_path.exists():
        raise HTTPException(status_code=404, detail=t("workspace_not_found"))

    import sys
    import subprocess
    try:
        if sys.platform == "win32":
            os.startfile(str(workspace_path))  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(workspace_path)])
        else:
            subprocess.Popen(["xdg-open", str(workspace_path)])
    except Exception as e:
        raise HTTPException(status_code=500, detail=t("workspace_open_error").replace("{error}", str(e)))

    return {"status": "success", "path": str(workspace_path)}


# =============================================================================
# デバッグAPI
# =============================================================================

@router.get("/api/debug/context")
async def get_api_debug_context():
    """最後にLLMに送信されたコンテキスト情報をJSON形式で返す。"""
    if not app_state.global_last_debug_context:
        return {"messages": [], "total_tokens": 0, "error": "No context recorded yet"}

    return app_state.global_last_debug_context
