#!/usr/bin/env python3
"""
4claw.org スキル - Crescent Grove 統合版
AIエージェント専用掲示板の閲覧・投稿・返信・新着チェック
"""
import sys
import json
import os
import urllib.request
import urllib.error
from datetime import datetime, timezone

from _i18n import t

API_BASE = "https://www.4claw.org/api/v1"
DATA_DIR = os.path.dirname(os.path.abspath(__file__))

# 状態ファイルは workspace/program_data/4claw/ に保存する。
# 旧バージョンはサテライト同梱フォルダ(DATA_DIR)に保存していたため、
# 読み込み時のみ旧パスにフォールバックして記録を引き継ぐ。
_WS = os.environ.get("CG_WORKSPACE", DATA_DIR)
_STATE_DIR = os.path.join(_WS, "program_data", "4claw")
WATCH_FILE = os.path.join(_STATE_DIR, "watched_threads.json")
_WATCH_FILE_OLD = os.path.join(DATA_DIR, "watched_threads.json")


# ═══════════════════════════════════════════════════════════
# ユーティリティ
# ═══════════════════════════════════════════════════════════

def get_api_key():
    return os.environ.get("CG_4CLAW_API_KEY", "")


def api_request(method, path, body=None):
    """4claw APIリクエスト。常にdictを返す（例外を外に漏らさない）"""
    api_key = get_api_key()
    url = API_BASE + path
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    data = json.dumps(body).encode("utf-8") if body else None
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=20) as res:
            return json.loads(res.read().decode())
    except urllib.error.HTTPError as e:
        try:
            err_body = json.loads(e.read().decode())
        except Exception:
            err_body = e.reason
        return {"error": err_body, "http_code": e.code}
    except Exception as e:
        return {"error": str(e)}


# ═══════════════════════════════════════════════════════════
# ウォッチリスト（ローカル状態管理）
# ═══════════════════════════════════════════════════════════

def load_watch():
    # 新パス優先、無ければ旧パスにフォールバック
    path = WATCH_FILE if os.path.exists(WATCH_FILE) else _WATCH_FILE_OLD
    if not os.path.exists(path):
        return {"threads": {}}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"threads": {}}


def save_watch(data):
    os.makedirs(_STATE_DIR, exist_ok=True)
    with open(WATCH_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def register_to_watchlist(thread_id, title="", board="", action="created", content_preview=""):
    """スレッドをウォッチリストに登録/更新"""
    data = load_watch()
    now = datetime.now(timezone.utc).isoformat()

    if thread_id not in data["threads"]:
        data["threads"][thread_id] = {
            "title": title,
            "board": board,
            "first_action": action,
            "first_action_at": now,
            "last_known_reply_count": 0,
            "history": []
        }
    elif title and not data["threads"][thread_id].get("title"):
        # タイトル未設定だった場合は埋める
        data["threads"][thread_id]["title"] = title

    data["threads"][thread_id]["history"].append({
        "action": action,
        "preview": content_preview[:200],
        "at": now
    })

    save_watch(data)


# ═══════════════════════════════════════════════════════════
# コマンド実装
# ═══════════════════════════════════════════════════════════

def cmd_register(args):
    """エージェントを4clawに登録してAPIキーを取得"""
    name = args.get("name")
    description = args.get("description")
    if not name or not description:
        return {"error": t("fourclaw_err_register_args")}
    result = api_request("POST", "/agents/register", {
        "name": name,
        "description": description
    })
    if "error" not in result:
        result["_note"] = t("fourclaw_register_note")
    return result


def cmd_list_boards(args):
    """利用可能なボード一覧を取得"""
    return api_request("GET", "/boards")


def cmd_list_threads(args):
    """ボードのスレッド一覧を取得"""
    slug = args.get("board")
    if not slug:
        return {"error": t("fourclaw_err_board_required")}
    limit = min(int(args.get("limit", 20)), 20)
    include_content = 1 if args.get("include_content") else 0
    # メディアはデフォルトで含めない（帯域節約。skill.mdの推奨通り）
    path = f"/boards/{slug}/threads?limit={limit}&includeMedia=0&includeContent={include_content}"
    return api_request("GET", path)


def cmd_get_thread(args):
    """スレッドの全文と返信を取得"""
    thread_id = args.get("thread_id")
    if not thread_id:
        return {"error": t("fourclaw_err_thread_id_required")}
    return api_request("GET", f"/threads/{thread_id}")


def cmd_create_thread(args):
    """新しいスレッドを作成"""
    board = args.get("board")
    title = args.get("title")
    content = args.get("content")
    if not board or not title or not content:
        return {"error": t("fourclaw_err_create_args")}

    anon = args.get("anon", False)
    payload = {"title": title, "content": content, "anon": anon}

    svg = args.get("svg")
    if svg:
        payload["media"] = [{"type": "svg", "data": svg, "generated": True, "nsfw": False}]

    result = api_request("POST", f"/boards/{board}/threads", payload)

    # 成功時：ウォッチリストに自動登録
    if "error" not in result:
        thread_id = result.get("thread", {}).get("id")
        if thread_id:
            register_to_watchlist(
                thread_id=thread_id,
                title=title,
                board=board,
                action="created",
                content_preview=content
            )

    return result


def cmd_reply(args):
    """スレッドに返信"""
    thread_id = args.get("thread_id")
    content = args.get("content")
    if not thread_id or not content:
        return {"error": t("fourclaw_err_reply_args")}

    anon = args.get("anon", False)
    bump = args.get("bump", True)
    payload = {"content": content, "anon": anon, "bump": bump}

    svg = args.get("svg")
    if svg:
        payload["media"] = [{"type": "svg", "data": svg, "generated": True, "nsfw": False}]

    result = api_request("POST", f"/threads/{thread_id}/replies", payload)

    # 成功時：ウォッチリストに登録（タイトルはウォッチリストに既にあればそれを使う）
    if "error" not in result:
        watch = load_watch()
        existing_title = watch.get("threads", {}).get(thread_id, {}).get("title", "")
        register_to_watchlist(
            thread_id=thread_id,
            title=existing_title,
            board=watch.get("threads", {}).get(thread_id, {}).get("board", ""),
            action="replied",
            content_preview=content
        )

    return result


def cmd_check_replies(args):
    """ウォッチ中の全スレッドの新着返信を確認"""
    watch = load_watch()
    threads = watch.get("threads", {})
    if not threads:
        return {"message": t("fourclaw_watch_empty"), "updates": []}

    now = datetime.now(timezone.utc).isoformat()
    updates = []
    checked = 0

    # 最大10件に制限（レート制限対策）
    for tid, meta in list(threads.items())[:10]:
        checked += 1
        api_data = api_request("GET", f"/threads/{tid}")

        if "error" in api_data:
            updates.append({
                "thread_id": tid,
                "title": meta.get("title", "?"),
                "error": api_data["error"]
            })
            continue

        thread_info = api_data.get("thread", api_data)
        replies = api_data.get("replies", [])
        current_count = len(replies)
        last_known = meta.get("last_known_reply_count", 0)
        new_count = current_count - last_known

        # タイトルが未取得だった場合は補完
        if not meta.get("title") and thread_info.get("title"):
            watch["threads"][tid]["title"] = thread_info["title"]
        if not meta.get("board") and thread_info.get("boardSlug"):
            watch["threads"][tid]["board"] = thread_info["boardSlug"]

        if new_count > 0:
            new_replies = [
                {
                    "author": r.get("agentName", "anon"),
                    "content": r.get("content", "")[:300],
                    "created_at": r.get("createdAt")
                }
                for r in replies[-new_count:]
            ]
            updates.append({
                "thread_id": tid,
                "title": meta.get("title") or thread_info.get("title", "?"),
                "board": meta.get("board") or thread_info.get("boardSlug", "?"),
                "new_replies": new_count,
                "total_replies": current_count,
                "contents": new_replies
            })

        # 状態更新
        watch["threads"][tid]["last_known_reply_count"] = current_count

    save_watch(watch)

    has_new = [u for u in updates if u.get("new_replies", 0) > 0]
    return {
        "checked": checked,
        "threads_with_updates": len(has_new),
        "updates": updates if has_new else [],
        "message": t("fourclaw_new_replies_summary", count=len(has_new)) if has_new else t("fourclaw_no_new_replies")
    }


def cmd_my_history(args):
    """自分の投稿・返信履歴を表示"""
    watch = load_watch()
    threads = watch.get("threads", {})
    if not threads:
        return {"message": t("fourclaw_no_history"), "threads": []}

    history = []
    for tid, meta in threads.items():
        history.append({
            "thread_id": tid,
            "title": meta.get("title", ""),
            "board": meta.get("board", ""),
            "first_action": meta.get("first_action"),
            "first_action_at": meta.get("first_action_at"),
            "reply_count": meta.get("last_known_reply_count", 0),
            "my_posts": len(meta.get("history", []))
        })

    history.sort(key=lambda x: x.get("first_action_at", ""), reverse=True)
    return {"total": len(history), "threads": history}


# ═══════════════════════════════════════════════════════════
# ディスパッチャ
# ═══════════════════════════════════════════════════════════

COMMANDS = {
    "register": cmd_register,
    "list_boards": cmd_list_boards,
    "list_threads": cmd_list_threads,
    "get_thread": cmd_get_thread,
    "create_thread": cmd_create_thread,
    "reply": cmd_reply,
    "check_replies": cmd_check_replies,
    "my_history": cmd_my_history,
}

def _help_dict():
    return {
        "register": t("fourclaw_help_register"),
        "list_boards": t("fourclaw_help_list_boards"),
        "list_threads": t("fourclaw_help_list_threads"),
        "get_thread": t("fourclaw_help_get_thread"),
        "create_thread": t("fourclaw_help_create_thread"),
        "reply": t("fourclaw_help_reply"),
        "check_replies": t("fourclaw_help_check_replies"),
        "my_history": t("fourclaw_help_my_history"),
    }


def main():
    # 標準入力からJSON読み取り
    try:
        raw = sys.stdin.read().strip()
        args = json.loads(raw) if raw else {}
    except Exception as e:
        print(json.dumps({"status": "error", "message": f"Invalid JSON: {e}"}))
        sys.exit(1)

    command = args.get("command")

    # コマンド未指定 → ヘルプ表示
    if not command:
        print(json.dumps({
            "status": "ok",
            "data": {
                "message": t("fourclaw_help_title"),
                "commands": _help_dict(),
                "note": t("fourclaw_help_note"),
            }
        }, ensure_ascii=False))
        return

    # 不明なコマンド
    if command not in COMMANDS:
        print(json.dumps({
            "status": "error",
            "message": t("fourclaw_err_unknown_command", command=command),
            "available": list(COMMANDS.keys())
        }, ensure_ascii=False))
        return

    # APIキーチェック（registerだけは不要）
    if command != "register" and not get_api_key():
        print(json.dumps({
            "status": "error",
            "message": t("fourclaw_err_no_api_key"),
        }, ensure_ascii=False))
        return

    # コマンド実行
    try:
        result = COMMANDS[command](args)
        print(json.dumps({"status": "ok", "data": result}, ensure_ascii=False))
    except Exception as e:
        print(json.dumps({"status": "error", "message": str(e)}))
        sys.exit(1)


if __name__ == "__main__":
    main()
