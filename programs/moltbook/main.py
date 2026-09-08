#!/usr/bin/env python3
"""
moltbook.com スキル - Crescent Grove 統合版
AIエージェント専用SNS「moltbook」の閲覧・投稿・コメント・通知確認
"""
import sys
import json
import os
import difflib
import urllib.request
import urllib.parse
import urllib.error
from datetime import datetime, timezone

from _i18n import t

API_BASE = "https://www.moltbook.com/api/v1"
DATA_DIR = os.path.dirname(os.path.abspath(__file__))

# 状態ファイルは workspace/program_data/moltbook/ に保存する。
# 旧バージョンはサテライト同梱フォルダ(DATA_DIR)に保存していたため、
# 読み込み時のみ旧パスにフォールバックして記録を引き継ぐ。
_WS = os.environ.get("CG_WORKSPACE", DATA_DIR)
_STATE_DIR = os.path.join(_WS, "program_data", "moltbook")
HISTORY_FILE = os.path.join(_STATE_DIR, "my_posts.json")
_HISTORY_FILE_OLD = os.path.join(DATA_DIR, "my_posts.json")


# ═══════════════════════════════════════════════════════════
# ユーティリティ
# ═══════════════════════════════════════════════════════════

def get_api_key():
    return os.environ.get("CG_MOLTBOOK_TOKEN", "")


def _safe_int(value, default):
    """int変換。None・空文字・変換失敗ならdefault"""
    if value is None or value == "":
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def api_request(method, path, body=None, query=None):
    """moltbook APIリクエスト。常にdictを返す（例外を外に漏らさない）"""
    api_key = get_api_key()
    url = API_BASE + path
    if query:
        params = "&".join(
            f"{urllib.parse.quote(str(k), safe='')}={urllib.parse.quote(str(v), safe='')}"
            for k, v in query.items()
            if v is not None and v != ""
        )
        if params:
            url += ("&" if "?" in url else "?") + params

    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    data = json.dumps(body).encode("utf-8") if body else None
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=20) as res:
            body_bytes = res.read()
            try:
                return json.loads(body_bytes.decode())
            except json.JSONDecodeError:
                return {"raw": body_bytes.decode(errors="replace")}
    except urllib.error.HTTPError as e:
        try:
            err_body = json.loads(e.read().decode())
        except Exception:
            err_body = e.reason
        return {"error": err_body, "http_code": e.code}
    except Exception as e:
        return {"error": str(e)}


def _quote_path(value):
    """URLパスセグメント用の安全エスケープ"""
    return urllib.parse.quote(str(value), safe='')


def _is_success(result):
    """API応答が成功かどうか判定。
    - 'error' キーがあれば失敗（HTTPError由来）
    - 'success' キーがあって False なら失敗（API層の論理エラー）
    - それ以外は成功扱い（'success' キーが無いエンドポイントも存在するため）
    """
    if "error" in result:
        return False
    if "success" in result and result["success"] is False:
        return False
    return True

# ═══════════════════════════════════════════════════════════
# ローカル履歴管理
# ═══════════════════════════════════════════════════════════

def load_history():
    # 新パス優先、無ければ旧パスにフォールバック
    path = HISTORY_FILE if os.path.exists(HISTORY_FILE) else _HISTORY_FILE_OLD
    if not os.path.exists(path):
        return {"posts": [], "comments": []}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"posts": [], "comments": []}


def save_history(data):
    os.makedirs(_STATE_DIR, exist_ok=True)
    with open(HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def record_post(post_id, title, submolt, content_preview,
                verification_code=None):
    """投稿を履歴に追加。verification_codeがあれば pending として記録"""
    data = load_history()
    data["posts"].append({
        "post_id": post_id,
        "title": title,
        "submolt": submolt,
        "preview": (content_preview or "")[:200],
        "verification_pending": bool(verification_code),
        "verification_code": verification_code,
        "at": datetime.now(timezone.utc).isoformat()
    })
    data["posts"] = data["posts"][-300:]
    save_history(data)


def record_comment(comment_id, post_id, content_preview,
                   parent_id=None, verification_code=None):
    """コメントを履歴に追加。comment_id自体を保存することでverify時に確実に逆引きできる"""
    data = load_history()
    data["comments"].append({
        "comment_id": comment_id,
        "post_id": post_id,
        "parent_id": parent_id,
        "preview": (content_preview or "")[:200],
        "verification_pending": bool(verification_code),
        "verification_code": verification_code,
        "at": datetime.now(timezone.utc).isoformat()
    })
    data["comments"] = data["comments"][-500:]
    save_history(data)


def clear_verification_pending(content_id=None, verification_code=None):
    """verify成功時、content_id か verification_code でマッチしたエントリのpendingを解除"""
    if not content_id and not verification_code:
        return
    data = load_history()
    changed = False
    for entry in data["posts"]:
        if not entry.get("verification_pending"):
            continue
        if (content_id and entry.get("post_id") == content_id) or \
           (verification_code and entry.get("verification_code") == verification_code):
            entry["verification_pending"] = False
            changed = True
    for entry in data["comments"]:
        if not entry.get("verification_pending"):
            continue
        if (content_id and entry.get("comment_id") == content_id) or \
           (verification_code and entry.get("verification_code") == verification_code):
            entry["verification_pending"] = False
            changed = True
    if changed:
        save_history(data)


# ═══════════════════════════════════════════════════════════
# コマンド実装
# ═══════════════════════════════════════════════════════════

def cmd_register(args):
    name = args.get("name")
    description = args.get("description")
    if not name or not description:
        return {"error": t("moltbook_err_register_args")}
    result = api_request("POST", "/agents/register", {
        "name": name,
        "description": description
    })
    if "error" not in result:
        result["_note"] = t("moltbook_register_note")
    return result


def cmd_whoami(args):
    return api_request("GET", "/agents/me")


def cmd_profile(args):
    target = args.get("target_name")
    if not target:
        return {"error": t("moltbook_err_target_name_required")}
    return api_request("GET", "/agents/profile", query={"name": target})


def cmd_update_profile(args):
    desc = args.get("description")
    if not desc:
        return {"error": t("moltbook_err_description_required")}
    return api_request("PATCH", "/agents/me", {"description": desc})


def cmd_home(args):
    return api_request("GET", "/home")


def cmd_list_submolts(args):
    return api_request("GET", "/submolts")


def cmd_submolt_info(args):
    submolt = args.get("submolt")
    if not submolt:
        return {"error": t("moltbook_err_submolt_required")}
    return api_request("GET", f"/submolts/{_quote_path(submolt)}")


def cmd_feed(args):
    q = {
        "sort": args.get("sort", "hot"),
        "limit": min(_safe_int(args.get("limit"), 25), 50),
        "cursor": args.get("cursor"),
    }
    return api_request("GET", "/feed", query=q)


def cmd_following_feed(args):
    q = {
        "filter": "following",
        "sort": args.get("sort", "new"),
        "limit": min(_safe_int(args.get("limit"), 25), 50),
        "cursor": args.get("cursor"),
    }
    return api_request("GET", "/feed", query=q)


def cmd_submolt_feed(args):
    submolt = args.get("submolt")
    if not submolt:
        return {"error": t("moltbook_err_submolt_required")}
    q = {
        "sort": args.get("sort", "new"),
        "limit": min(_safe_int(args.get("limit"), 25), 50),
        "cursor": args.get("cursor"),
    }
    return api_request("GET", f"/submolts/{_quote_path(submolt)}/feed", query=q)


def cmd_get_post(args):
    post_id = args.get("post_id")
    if not post_id:
        return {"error": t("moltbook_err_post_id_required")}
    pid = _quote_path(post_id)
    post = api_request("GET", f"/posts/{pid}")
    comments = api_request(
        "GET",
        f"/posts/{pid}/comments",
        query={
            "sort": args.get("sort", "best"),
            "limit": min(_safe_int(args.get("limit"), 35), 100),
        },
    )
    return {"post": post, "comments": comments}


def cmd_create_post(args):
    submolt = args.get("submolt")
    title = args.get("title")
    if not submolt or not title:
        return {"error": t("moltbook_err_submolt_title_required")}
    payload = {"submolt_name": submolt, "title": title}
    if args.get("content"):
        payload["content"] = args["content"]
    if args.get("url"):
        payload["url"] = args["url"]
        payload["type"] = "link"
    result = api_request("POST", "/posts", payload)

    if _is_success(result):  # ← 変更
        post = result.get("post") or {}
        pid = post.get("id")
        verification = post.get("verification") or {}
        vcode = verification.get("verification_code")
        if pid:
            record_post(
                post_id=pid,
                title=title,
                submolt=submolt,
                content_preview=args.get("content", "") or args.get("url", ""),
                verification_code=vcode,
            )
        if vcode:
            result["_note"] = t("moltbook_post_verification_required")
    return result


def cmd_create_link_post(args):
    if not args.get("url"):
        return {"error": t("moltbook_err_link_post_url_required")}
    return cmd_create_post(args)


def cmd_delete_post(args):
    post_id = args.get("post_id")
    if not post_id:
        return {"error": t("moltbook_err_post_id_required")}
    return api_request("DELETE", f"/posts/{_quote_path(post_id)}")


def cmd_comment(args):
    post_id = args.get("post_id")
    content = args.get("content")
    if not post_id or not content:
        return {"error": t("moltbook_err_post_id_content_required")}
    payload = {"content": content}
    if args.get("parent_id"):
        payload["parent_id"] = args["parent_id"]
    result = api_request("POST", f"/posts/{_quote_path(post_id)}/comments", payload)

    if _is_success(result):  # ← 変更
        comment_obj = result.get("comment") or {}
        cid = comment_obj.get("id")
        verification = comment_obj.get("verification") or result.get("verification") or {}
        vcode = verification.get("verification_code") if isinstance(verification, dict) else None
        record_comment(
            comment_id=cid,
            post_id=post_id,
            content_preview=content,
            parent_id=args.get("parent_id"),
            verification_code=vcode,
        )
        if vcode:
            result["_note"] = t("moltbook_comment_verification_required")
    return result


def cmd_upvote_post(args):
    post_id = args.get("post_id")
    if not post_id:
        return {"error": t("moltbook_err_post_id_required")}
    return api_request("POST", f"/posts/{_quote_path(post_id)}/upvote")


def cmd_downvote_post(args):
    post_id = args.get("post_id")
    if not post_id:
        return {"error": t("moltbook_err_post_id_required")}
    return api_request("POST", f"/posts/{_quote_path(post_id)}/downvote")


def cmd_upvote_comment(args):
    comment_id = args.get("comment_id")
    if not comment_id:
        return {"error": t("moltbook_err_comment_id_required")}
    return api_request("POST", f"/comments/{_quote_path(comment_id)}/upvote")


def cmd_follow(args):
    target = args.get("target_name")
    if not target:
        return {"error": t("moltbook_err_target_name_required")}
    return api_request("POST", f"/agents/{_quote_path(target)}/follow")


def cmd_unfollow(args):
    target = args.get("target_name")
    if not target:
        return {"error": t("moltbook_err_target_name_required")}
    return api_request("DELETE", f"/agents/{_quote_path(target)}/follow")


def cmd_subscribe(args):
    submolt = args.get("submolt")
    if not submolt:
        return {"error": t("moltbook_err_submolt_required")}
    return api_request("POST", f"/submolts/{_quote_path(submolt)}/subscribe")


def cmd_unsubscribe(args):
    submolt = args.get("submolt")
    if not submolt:
        return {"error": t("moltbook_err_submolt_required")}
    return api_request("DELETE", f"/submolts/{_quote_path(submolt)}/subscribe")


def cmd_create_submolt(args):
    submolt = args.get("submolt")
    display_name = args.get("display_name")
    if not submolt or not display_name:
        return {"error": t("moltbook_err_create_submolt_args")}
    payload = {"name": submolt, "display_name": display_name}
    if args.get("description"):
        payload["description"] = args["description"]
    if args.get("allow_crypto"):
        payload["allow_crypto"] = bool(args["allow_crypto"])
    return api_request("POST", "/submolts", payload)


def cmd_search(args):
    query = args.get("query")
    if not query:
        return {"error": t("moltbook_err_query_required")}
    q = {
        "q": query,
        "type": args.get("search_type", "all"),
        "limit": min(_safe_int(args.get("limit"), 20), 50),
        "cursor": args.get("cursor"),
    }
    return api_request("GET", "/search", query=q)


def cmd_notifications(args):
    return api_request("GET", "/notifications")


def cmd_mark_read(args):
    post_id = args.get("post_id")
    if not post_id:
        return {"error": t("moltbook_err_post_id_required")}
    return api_request("POST", f"/notifications/read-by-post/{_quote_path(post_id)}")


def cmd_mark_all_read(args):
    return api_request("POST", "/notifications/read-all")


def cmd_verify(args):
    code = args.get("verification_code")
    answer = args.get("answer")
    if not code or answer is None or answer == "":
        return {"error": t("moltbook_err_verify_args")}
    result = api_request("POST", "/verify", {
        "verification_code": code,
        "answer": str(answer),
    })
    if _is_success(result):  # ← 変更（result.get("success") の冗長な二重チェックも削除）
        clear_verification_pending(
            content_id=result.get("content_id"),
            verification_code=code,
        )
    return result


def cmd_check_replies(args):
    """/home から自分の投稿への新着レスだけを抜き出して整形"""
    home = api_request("GET", "/home")
    if "error" in home:
        return home

    activity = home.get("activity_on_your_posts", []) or []
    unread = home.get("your_account", {}).get("unread_notification_count", 0)

    if not activity:
        return {
            "message": t("moltbook_no_new_replies"),
            "unread_count": unread,
            "updates": []
        }

    updates = []
    total_new = 0
    for item in activity:
        new = item.get("new_notification_count", 0) or 0
        total_new += new
        updates.append({
            "post_id": item.get("post_id"),
            "title": item.get("post_title"),
            "submolt": item.get("submolt_name"),
            "new_replies": new,
            "latest_commenters": item.get("latest_commenters", []),
            "preview": item.get("preview"),
            "latest_at": item.get("latest_at"),
        })

    return {
        "message": t("moltbook_new_replies_summary", post_count=len(updates), reply_count=total_new),
        "unread_count": unread,
        "updates": updates,
        "hint": t("moltbook_check_replies_hint"),
    }


def cmd_my_history(args):
    """ローカル記録から自分の投稿・コメント履歴を表示"""
    data = load_history()
    posts = data.get("posts", [])
    comments = data.get("comments", [])

    posts_sorted = sorted(posts, key=lambda x: x.get("at", ""), reverse=True)
    comments_sorted = sorted(comments, key=lambda x: x.get("at", ""), reverse=True)

    pending_posts = [p for p in posts if p.get("verification_pending")]
    pending_comments = [c for c in comments if c.get("verification_pending")]

    limit = min(_safe_int(args.get("limit"), 20), 100)

    pending_note = None
    if pending_posts or pending_comments:
        pending_note = t("moltbook_pending_verify_note")

    return {
        "total_posts": len(posts),
        "total_comments": len(comments),
        "verification_pending": {
            "posts": len(pending_posts),
            "comments": len(pending_comments),
            "note": pending_note
        },
        "recent_posts": posts_sorted[:limit],
        "recent_comments": comments_sorted[:limit],
    }


# ═══════════════════════════════════════════════════════════
# ディスパッチャ
# ═══════════════════════════════════════════════════════════

COMMANDS = {
    "register": cmd_register,
    "whoami": cmd_whoami,
    "profile": cmd_profile,
    "update_profile": cmd_update_profile,
    "home": cmd_home,
    "list_submolts": cmd_list_submolts,
    "submolt_info": cmd_submolt_info,
    "feed": cmd_feed,
    "following_feed": cmd_following_feed,
    "submolt_feed": cmd_submolt_feed,
    "get_post": cmd_get_post,
    "create_post": cmd_create_post,
    "create_link_post": cmd_create_link_post,
    "delete_post": cmd_delete_post,
    "comment": cmd_comment,
    "upvote_post": cmd_upvote_post,
    "downvote_post": cmd_downvote_post,
    "upvote_comment": cmd_upvote_comment,
    "follow": cmd_follow,
    "unfollow": cmd_unfollow,
    "subscribe": cmd_subscribe,
    "unsubscribe": cmd_unsubscribe,
    "create_submolt": cmd_create_submolt,
    "search": cmd_search,
    "notifications": cmd_notifications,
    "mark_read": cmd_mark_read,
    "mark_all_read": cmd_mark_all_read,
    "verify": cmd_verify,
    "check_replies": cmd_check_replies,
    "my_history": cmd_my_history,
}

def _help_dict():
    return {
        "register": t("moltbook_help_register"),
        "home": t("moltbook_help_home"),
        "whoami": t("moltbook_help_whoami"),
        "profile": t("moltbook_help_profile"),
        "update_profile": t("moltbook_help_update_profile"),
        "list_submolts": t("moltbook_help_list_submolts"),
        "submolt_info": t("moltbook_help_submolt_info"),
        "feed": t("moltbook_help_feed"),
        "following_feed": t("moltbook_help_following_feed"),
        "submolt_feed": t("moltbook_help_submolt_feed"),
        "get_post": t("moltbook_help_get_post"),
        "create_post": t("moltbook_help_create_post"),
        "create_link_post": t("moltbook_help_create_link_post"),
        "delete_post": t("moltbook_help_delete_post"),
        "comment": t("moltbook_help_comment"),
        "upvote_post": t("moltbook_help_upvote_post"),
        "downvote_post": t("moltbook_help_downvote_post"),
        "upvote_comment": t("moltbook_help_upvote_comment"),
        "follow": t("moltbook_help_follow"),
        "unfollow": t("moltbook_help_unfollow"),
        "subscribe": t("moltbook_help_subscribe"),
        "unsubscribe": t("moltbook_help_unsubscribe"),
        "create_submolt": t("moltbook_help_create_submolt"),
        "search": t("moltbook_help_search"),
        "notifications": t("moltbook_help_notifications"),
        "mark_read": t("moltbook_help_mark_read"),
        "mark_all_read": t("moltbook_help_mark_all_read"),
        "verify": t("moltbook_help_verify"),
        "check_replies": t("moltbook_help_check_replies"),
        "my_history": t("moltbook_help_my_history"),
    }


# HELP は _help_dict() で都度生成（i18n 言語切り替えに追従）
# ただし _suggest_for_unknown / _build_suggestion が直接参照するため、互換のためアクセサも提供
class _HelpProxy(dict):
    def __getitem__(self, key):
        return _help_dict()[key]
    def get(self, key, default=""):
        return _help_dict().get(key, default)
    def keys(self):
        return _help_dict().keys()
    def items(self):
        return _help_dict().items()
    def __iter__(self):
        return iter(_help_dict())
HELP = _HelpProxy()


REQUIRED_ARGS = {
    "register": ["name", "description"],
    "profile": ["target_name"],
    "update_profile": ["description"],
    "submolt_info": ["submolt"],
    "submolt_feed": ["submolt"],
    "get_post": ["post_id"],
    "create_post": ["submolt", "title"],
    "create_link_post": ["submolt", "title", "url"],
    "delete_post": ["post_id"],
    "comment": ["post_id", "content"],
    "upvote_post": ["post_id"],
    "downvote_post": ["post_id"],
    "upvote_comment": ["comment_id"],
    "follow": ["target_name"],
    "unfollow": ["target_name"],
    "subscribe": ["submolt"],
    "unsubscribe": ["submolt"],
    "create_submolt": ["submolt", "display_name"],
    "search": ["query"],
    "mark_read": ["post_id"],
    "verify": ["verification_code", "answer"],
}


def _build_suggestion(cmd, provided_args=None):
    """コマンド名から、必須引数と即リトライ用のexampleを組み立てる"""
    req = REQUIRED_ARGS.get(cmd, [])
    example = {"command": cmd}
    for r in req:
        if provided_args and r in provided_args:
            example[r] = provided_args[r]
        else:
            example[r] = f"<{r}>"
    return {
        "command": cmd,
        "description": HELP.get(cmd, ""),
        "required_args": req or t("moltbook_none_label"),
        "retry_example": example,
    }


def _suggest_for_unknown(command, provided_args=None):
    """不明なコマンドに対し、類似コマンドを提案するエラー応答を組み立てる"""
    kw = command.lower()
    keyword_map = {
        "post": ["create_post", "comment"],
        "write": ["create_post", "comment"],
        "reply": ["comment"],
        "vote": ["upvote_post", "upvote_comment"],
        "like": ["upvote_post", "upvote_comment"],
        "sub": ["subscribe", "submolt_feed"],
        "login": ["register"],
        "signup": ["register"],
        "find": ["search"],
        "check": ["check_replies", "notifications"],
        "read": ["mark_read", "mark_all_read"],
        "delete": ["delete_post"],
        "remove": ["delete_post"],
        "list": ["list_submolts", "feed"],
        "timeline": ["feed", "following_feed"],
    }
    matches = []
    for key, cmds in keyword_map.items():
        if key in kw:
            matches = cmds
            break
    if not matches:
        matches = difflib.get_close_matches(command, COMMANDS.keys(), n=3, cutoff=0.4)

    result = {
        "status": "error",
        "message": t("moltbook_err_unknown_command", command=command),
        "available_commands": _help_dict(),
    }
    if matches:
        suggestions = [_build_suggestion(m, provided_args) for m in matches]
        result["did_you_mean"] = suggestions
        result["hint"] = t("moltbook_did_you_mean", names=', '.join(m['command'] for m in suggestions))
    else:
        result["hint"] = t("moltbook_no_match_hint")
    return result


def main():
    try:
        raw = sys.stdin.read().strip()
        args = json.loads(raw) if raw else {}
    except Exception as e:
        print(json.dumps({"status": "error", "message": f"Invalid JSON: {e}"}))
        sys.exit(1)

    command = args.get("command")

    if not command:
        print(json.dumps({
            "status": "ok",
            "data": {
                "message": t("moltbook_help_title"),
                "commands": _help_dict(),
                "tip": t("moltbook_help_tip"),
                "note": t("moltbook_help_note"),
            }
        }, ensure_ascii=False))
        return

    if command not in COMMANDS:
        other_args = {k: v for k, v in args.items() if k != "command"}
        print(json.dumps(
            _suggest_for_unknown(command, other_args or None),
            ensure_ascii=False,
        ))
        return

    req = REQUIRED_ARGS.get(command, [])
    missing = [r for r in req if not args.get(r)]
    if missing:
        suggestion = _build_suggestion(command, {k: v for k, v in args.items() if k != "command"})
        print(json.dumps({
            "status": "error",
            "message": t("moltbook_err_missing_args", command=command, args=', '.join(missing)),
            "retry_example": suggestion["retry_example"],
            "description": suggestion["description"],
        }, ensure_ascii=False))
        return

    if command != "register" and not get_api_key():
        print(json.dumps({
            "status": "error",
            "message": t("moltbook_err_no_api_key"),
            "retry_example": {"command": "register", "name": t("moltbook_placeholder_display_name"), "description": t("moltbook_placeholder_self_intro")},
        }, ensure_ascii=False))
        return

    try:
        result = COMMANDS[command](args)
        print(json.dumps({"status": "ok", "data": result}, ensure_ascii=False))
    except Exception as e:
        print(json.dumps({"status": "error", "message": str(e)}))
        sys.exit(1)


if __name__ == "__main__":
    main()
