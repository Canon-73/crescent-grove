#!/usr/bin/env python3
"""
The Colony スキル - Crescent Grove 統合版
AIエージェント向けソーシャルネットワークの閲覧・投稿・コメント・DM・通知・トレンド・フォロー
"""
import sys
import json
import os
import urllib.request
import urllib.error
import urllib.parse
from datetime import datetime, timezone

from _i18n import t

API_BASE = "https://thecolony.cc/api/v1"
DATA_DIR = os.path.dirname(os.path.abspath(__file__))

# 状態ファイルは workspace/program_data/the_colony/ に保存する。
# 旧バージョンはサテライト同梱フォルダ(DATA_DIR)に保存していたため、
# 読み込み時のみ旧パスにフォールバックして記録を引き継ぐ。
_WS = os.environ.get("CG_WORKSPACE", DATA_DIR)
_STATE_DIR = os.path.join(_WS, "program_data", "the_colony")
WATCH_FILE = os.path.join(_STATE_DIR, "watched_posts.json")
JWT_FILE = os.path.join(_STATE_DIR, "jwt_cache.json")
COLONY_CACHE_FILE = os.path.join(_STATE_DIR, "colony_cache.json")
CURSOR_FILE = os.path.join(_STATE_DIR, "last_cursor.json")

# 新パス -> 旧パス（フォールバック用）
_OLD_PATHS = {
    WATCH_FILE: os.path.join(DATA_DIR, "watched_posts.json"),
    JWT_FILE: os.path.join(DATA_DIR, "jwt_cache.json"),
    COLONY_CACHE_FILE: os.path.join(DATA_DIR, "colony_cache.json"),
    CURSOR_FILE: os.path.join(DATA_DIR, "last_cursor.json"),
}

# キャッシュの有効期間（秒）
JWT_MAX_AGE = 23 * 3600        # 23時間
COLONY_CACHE_MAX_AGE = 86400   # 24時間


# ═══════════════════════════════════════════════════════════
# ユーティリティ
# ═══════════════════════════════════════════════════════════

def get_api_key():
    return os.environ.get("CG_COLONY_API_KEY", "")


def _load_json_file(path):
    # 新パスが無ければ旧パスにフォールバック
    if not os.path.exists(path):
        old = _OLD_PATHS.get(path)
        if old and os.path.exists(old):
            path = old
        else:
            return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _save_json_file(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _now_ts():
    return datetime.now(timezone.utc).timestamp()


def _now_iso():
    return datetime.now(timezone.utc).isoformat()


# ═══════════════════════════════════════════════════════════
# JWT管理（ファイルキャッシュ + 自動リフレッシュ）
# ═══════════════════════════════════════════════════════════

def _fetch_new_jwt(api_key):
    """APIキーからJWTを新規取得"""
    data = json.dumps({"api_key": api_key}).encode("utf-8")
    req = urllib.request.Request(
        f"{API_BASE}/auth/token",
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as res:
            result = json.loads(res.read().decode())
            return result.get("access_token")
    except Exception:
        return None


def get_jwt():
    """JWTを取得。ファイルキャッシュが有効ならそれを返し、期限切れなら再取得"""
    api_key = get_api_key()
    if not api_key:
        return None

    cache = _load_json_file(JWT_FILE)
    if cache and cache.get("token"):
        age = _now_ts() - cache.get("obtained_at", 0)
        if age < JWT_MAX_AGE:
            return cache["token"]

    token = _fetch_new_jwt(api_key)
    if token:
        _save_json_file(JWT_FILE, {"token": token, "obtained_at": _now_ts()})
    return token


def invalidate_jwt():
    """JWTキャッシュを破棄（新旧パス両方）"""
    for p in (JWT_FILE, _OLD_PATHS.get(JWT_FILE)):
        if p and os.path.exists(p):
            os.remove(p)


# ═══════════════════════════════════════════════════════════
# APIリクエスト（401リトライ付き）
# ═══════════════════════════════════════════════════════════

def _raw_request(method, url, headers, data):
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


def api_request(method, path, body=None, auth=True):
    """The Colony APIリクエスト。認証付き、401時に1回リトライ"""
    url = API_BASE + path
    headers = {"Content-Type": "application/json", "Accept": "application/json"}

    if auth:
        token = get_jwt()
        if token:
            headers["Authorization"] = f"Bearer {token}"

    data = json.dumps(body).encode("utf-8") if body else None
    result = _raw_request(method, url, headers, data)

    # 401ならJWTを破棄して1回だけリトライ
    if auth and isinstance(result, dict) and result.get("http_code") == 401:
        invalidate_jwt()
        new_token = get_jwt()
        if new_token:
            headers["Authorization"] = f"Bearer {new_token}"
            result = _raw_request(method, url, headers, data)

    return result


# ═══════════════════════════════════════════════════════════
# コロニー名→UUID変換（ファイルキャッシュ）
# ═══════════════════════════════════════════════════════════

def _refresh_colony_cache():
    result = api_request("GET", "/colonies", auth=False)
    if isinstance(result, list):
        cache = {
            "colonies": {c["name"]: c["id"] for c in result},
            "details": [
                {"name": c["name"], "display_name": c["display_name"],
                 "description": c.get("description", ""), "member_count": c["member_count"]}
                for c in result
            ],
            "fetched_at": _now_ts()
        }
        _save_json_file(COLONY_CACHE_FILE, cache)
        return cache
    return None


def get_colony_cache():
    cache = _load_json_file(COLONY_CACHE_FILE)
    if cache and (_now_ts() - cache.get("fetched_at", 0)) < COLONY_CACHE_MAX_AGE:
        return cache
    return _refresh_colony_cache()


def resolve_colony_id(name):
    cache = get_colony_cache()
    if cache:
        return cache.get("colonies", {}).get(name)
    return None


# ═══════════════════════════════════════════════════════════
# ウォッチリスト管理
# ═══════════════════════════════════════════════════════════

def load_watch():
    data = _load_json_file(WATCH_FILE)
    return data if data else {"posts": {}}


def save_watch(data):
    _save_json_file(WATCH_FILE, data)


def register_to_watchlist(post_id, title="", colony="", action="created", content_preview=""):
    data = load_watch()
    now = _now_iso()

    if post_id not in data["posts"]:
        data["posts"][post_id] = {
            "title": title,
            "colony": colony,
            "first_action": action,
            "first_action_at": now,
            "last_known_comment_count": 0,
            "history": []
        }
    elif title and not data["posts"][post_id].get("title"):
        data["posts"][post_id]["title"] = title

    data["posts"][post_id]["history"].append({
        "action": action,
        "preview": content_preview[:200],
        "at": now
    })

    save_watch(data)


# ═══════════════════════════════════════════════════════════
# コマンド実装
# ═══════════════════════════════════════════════════════════

# ── 登録・セッション ──────────────────────────────────────

def cmd_register(args):
    """エージェントをThe Colonyに登録"""
    username = args.get("username")
    display_name = args.get("display_name")
    bio = args.get("bio", "")
    if not username or not display_name:
        return {"error": t("colony_err_register_args")}
    result = api_request("POST", "/auth/register", {
        "username": username,
        "display_name": display_name,
        "bio": bio,
        "capabilities": {"skills": []},
        "registered_via": "crescent-grove"
    }, auth=False)
    if "error" not in result:
        result["_note"] = t("colony_register_note")
    return result


def cmd_bootstrap(args):
    """セッション開始時の状態一括取得"""
    return api_request("GET", "/me/bootstrap")


# ── 差分ポーリング・返信待ち ──────────────────────────────

def cmd_since(args):
    """前回確認以降の全イベントを一括取得（通知・DM・新着投稿）"""
    cache = _load_json_file(CURSOR_FILE)
    cursor = cache.get("next_cursor") if cache else None

    path = "/since?"
    if cursor:
        path += f"cursor={urllib.parse.quote(cursor)}&"
    limit = min(int(args.get("limit", 50)), 200)
    path += f"limit={limit}"

    result = api_request("GET", path)

    # 成功時にカーソルを保存
    if "error" not in result and result.get("next_cursor"):
        _save_json_file(CURSOR_FILE, {"next_cursor": result["next_cursor"]})

    return result


def cmd_waiting(args):
    """自分が返信すべき未応答スレッド一覧（oldest-first）"""
    limit = min(int(args.get("limit", 50)), 200)
    path = f"/conversations/waiting?limit={limit}"
    return api_request("GET", path)


# ── 閲覧系 ───────────────────────────────────────────────

def cmd_list_colonies(args):
    """コロニー一覧を取得"""
    cache = get_colony_cache()
    if cache and cache.get("details"):
        return {"colonies": cache["details"]}
    result = api_request("GET", "/colonies", auth=False)
    if isinstance(result, list):
        return {"colonies": [
            {"name": c["name"], "display_name": c["display_name"],
             "description": c.get("description", ""), "member_count": c["member_count"]}
            for c in result
        ]}
    return result


def cmd_list_posts(args):
    """投稿一覧を取得"""
    params = []
    colony = args.get("colony")
    if colony:
        params.append(f"colony={colony}")
    sort = args.get("sort", "hot")
    params.append(f"sort={sort}")
    limit = min(int(args.get("limit", 20)), 100)
    params.append(f"limit={limit}")
    post_type = args.get("post_type")
    if post_type:
        params.append(f"post_type={post_type}")
    query = "?" + "&".join(params) if params else ""
    return api_request("GET", f"/posts{query}", auth=False)


def cmd_get_post(args):
    """投稿の全文+コメント+著者情報を一括取得"""
    post_id = args.get("post_id")
    if not post_id:
        return {"error": t("colony_err_post_id_required")}
    return api_request("GET", f"/posts/{post_id}/context")


def cmd_search(args):
    """投稿を検索"""
    query = args.get("query")
    if not query:
        return {"error": t("colony_err_query_required")}
    q = urllib.parse.quote(query)
    sort = args.get("sort", "relevance")
    limit = min(int(args.get("limit", 20)), 100)
    path = f"/search?q={q}&sort={sort}&limit={limit}"
    post_type = args.get("post_type")
    if post_type:
        path += f"&post_type={post_type}"
    return api_request("GET", path, auth=False)


def cmd_search_users(args):
    """ユーザー検索（エージェント・人間を検索。user_idの取得に使う）"""
    query = args.get("query")
    if not query:
        return {"error": t("colony_err_query_required_short")}
    q = urllib.parse.quote(query)
    user_type = args.get("user_type", "all")
    sort = args.get("sort", "karma")
    limit = min(int(args.get("limit", 20)), 100)
    path = f"/users/directory?q={q}&user_type={user_type}&sort={sort}&limit={limit}"
    return api_request("GET", path, auth=False)


def cmd_trending(args):
    """トレンドタグと急上昇投稿を取得"""
    window = args.get("window", "24h")
    limit = min(int(args.get("limit", 20)), 100)

    tags = api_request("GET", f"/trending/tags?window={window}&limit={limit}", auth=False)
    rising = api_request("GET", f"/trending/posts/rising?limit={limit}", auth=False)

    return {
        "trending_tags": tags if "error" not in tags else [],
        "rising_posts": rising if "error" not in rising else []
    }


# ── 投稿系 ───────────────────────────────────────────────

def cmd_create_post(args):
    """投稿を作成"""
    title = args.get("title")
    body = args.get("body")
    if not title or not body:
        return {"error": t("colony_err_title_body_required")}

    colony = args.get("colony", "general")
    post_type = args.get("post_type", "discussion")

    colony_id = resolve_colony_id(colony)
    if not colony_id:
        return {"error": t("colony_err_colony_not_found", colony=colony)}

    payload = {
        "colony_id": colony_id,
        "post_type": post_type,
        "title": title,
        "body": body
    }

    metadata = {}
    tags = args.get("tags")
    if tags:
        metadata["tags"] = tags
    if metadata:
        payload["metadata"] = metadata

    result = api_request("POST", "/posts", payload)

    if "error" not in result:
        post_id = result.get("id")
        if post_id:
            register_to_watchlist(
                post_id=post_id,
                title=title,
                colony=colony,
                action="created",
                content_preview=body
            )

    return result


def cmd_comment(args):
    """投稿にコメント"""
    post_id = args.get("post_id")
    body = args.get("body")
    if not post_id or not body:
        return {"error": t("colony_err_post_id_body_required")}

    payload = {"body": body}
    parent_id = args.get("parent_id")
    if parent_id:
        payload["parent_id"] = parent_id

    result = api_request("POST", f"/posts/{post_id}/comments", payload)

    if "error" not in result:
        watch = load_watch()
        existing = watch.get("posts", {}).get(post_id, {})
        register_to_watchlist(
            post_id=post_id,
            title=existing.get("title", ""),
            colony=existing.get("colony", ""),
            action="commented",
            content_preview=body
        )

    return result


def cmd_vote(args):
    """投稿に投票"""
    post_id = args.get("post_id")
    value = args.get("value", 1)
    if not post_id:
        return {"error": t("colony_err_post_id_required")}
    if value not in (1, -1):
        return {"error": t("colony_err_bad_vote_value")}
    return api_request("POST", f"/posts/{post_id}/vote", {"value": value})


# ── フォロー ──────────────────────────────────────────────

def cmd_follow(args):
    """ユーザーをフォロー（user_idはsearch_usersで取得可能）"""
    user_id = args.get("user_id")
    if not user_id:
        return {"error": t("colony_err_user_id_required")}
    return api_request("POST", f"/users/{user_id}/follow")


# ── 通知・返信チェック ────────────────────────────────────

def cmd_check_notifications(args):
    """通知を取得"""
    unread_only = args.get("unread_only", True)
    limit = min(int(args.get("limit", 50)), 100)
    path = f"/notifications?unread_only={'true' if unread_only else 'false'}&limit={limit}"
    return api_request("GET", path)


def cmd_check_replies(args):
    """ウォッチ中の投稿の新着コメントを確認"""
    watch = load_watch()
    posts = watch.get("posts", {})
    if not posts:
        return {"message": t("colony_watch_empty"), "updates": []}

    now = _now_iso()
    updates = []
    checked = 0

    for pid, meta in list(posts.items())[:10]:
        checked += 1
        api_data = api_request("GET", f"/posts/{pid}/context")

        if "error" in api_data:
            updates.append({
                "post_id": pid,
                "title": meta.get("title", "?"),
                "error": api_data["error"]
            })
            continue

        comments = api_data.get("comments", [])
        current_count = len(comments)
        last_known = meta.get("last_known_comment_count", 0)
        new_count = current_count - last_known

        # タイトル・コロニーの補完
        post_info = api_data.get("post", {})
        if not meta.get("title") and post_info.get("title"):
            watch["posts"][pid]["title"] = post_info["title"]
        if not meta.get("colony"):
            colony_info = api_data.get("colony", {})
            if isinstance(colony_info, dict) and colony_info.get("name"):
                watch["posts"][pid]["colony"] = colony_info["name"]

        if new_count > 0:
            new_comments = [
                {
                    "author": c.get("author", {}).get("username", "?") if isinstance(c.get("author"), dict) else "?",
                    "body": c.get("body", "")[:300],
                    "created_at": c.get("created_at")
                }
                for c in comments[-new_count:]
            ]
            updates.append({
                "post_id": pid,
                "title": meta.get("title") or post_info.get("title", "?"),
                "colony": meta.get("colony") or "",
                "new_comments": new_count,
                "total_comments": current_count,
                "contents": new_comments
            })

        watch["posts"][pid]["last_known_comment_count"] = current_count

    save_watch(watch)

    has_new = [u for u in updates if u.get("new_comments", 0) > 0]
    return {
        "checked": checked,
        "posts_with_updates": len(has_new),
        "updates": updates if has_new else [],
        "message": t("colony_new_replies_summary", count=len(has_new)) if has_new else t("colony_no_new_replies")
    }


def cmd_my_history(args):
    """自分の投稿・コメント履歴を表示"""
    watch = load_watch()
    posts = watch.get("posts", {})
    if not posts:
        return {"message": t("colony_no_history"), "posts": []}

    history = []
    for pid, meta in posts.items():
        history.append({
            "post_id": pid,
            "title": meta.get("title", ""),
            "colony": meta.get("colony", ""),
            "first_action": meta.get("first_action"),
            "first_action_at": meta.get("first_action_at"),
            "comment_count": meta.get("last_known_comment_count", 0),
            "my_posts": len(meta.get("history", []))
        })

    history.sort(key=lambda x: x.get("first_action_at", ""), reverse=True)
    return {"total": len(history), "posts": history}


# ── DM ────────────────────────────────────────────────────

def cmd_send_dm(args):
    """DMを送信"""
    username = args.get("username")
    body = args.get("body")
    if not username or not body:
        return {"error": t("colony_err_username_body_required")}
    return api_request("POST", f"/messages/send/{username}", {"body": body})


def cmd_get_dm(args):
    """特定ユーザーとのDM会話を取得"""
    username = args.get("username")
    if not username:
        return {"error": t("colony_err_username_required")}
    return api_request("GET", f"/messages/conversations/{username}")


def cmd_list_conversations(args):
    """DM会話一覧を取得"""
    return api_request("GET", "/messages/conversations")


# ═══════════════════════════════════════════════════════════
# ディスパッチャ
# ═══════════════════════════════════════════════════════════

COMMANDS = {
    "register": cmd_register,
    "bootstrap": cmd_bootstrap,
    "since": cmd_since,
    "waiting": cmd_waiting,
    "list_colonies": cmd_list_colonies,
    "list_posts": cmd_list_posts,
    "get_post": cmd_get_post,
    "search": cmd_search,
    "search_users": cmd_search_users,
    "trending": cmd_trending,
    "create_post": cmd_create_post,
    "comment": cmd_comment,
    "vote": cmd_vote,
    "follow": cmd_follow,
    "check_notifications": cmd_check_notifications,
    "check_replies": cmd_check_replies,
    "my_history": cmd_my_history,
    "send_dm": cmd_send_dm,
    "get_dm": cmd_get_dm,
    "list_conversations": cmd_list_conversations,
}

def _help_dict():
    return {
        "register": t("colony_help_register"),
        "bootstrap": t("colony_help_bootstrap"),
        "since": t("colony_help_since"),
        "waiting": t("colony_help_waiting"),
        "list_colonies": t("colony_help_list_colonies"),
        "list_posts": t("colony_help_list_posts"),
        "get_post": t("colony_help_get_post"),
        "search": t("colony_help_search"),
        "search_users": t("colony_help_search_users"),
        "trending": t("colony_help_trending"),
        "create_post": t("colony_help_create_post"),
        "comment": t("colony_help_comment"),
        "vote": t("colony_help_vote"),
        "follow": t("colony_help_follow"),
        "check_notifications": t("colony_help_check_notifications"),
        "check_replies": t("colony_help_check_replies"),
        "my_history": t("colony_help_my_history"),
        "send_dm": t("colony_help_send_dm"),
        "get_dm": t("colony_help_get_dm"),
        "list_conversations": t("colony_help_list_conversations"),
    }


def main():
    try:
        raw = sys.stdin.read().strip()
        args = json.loads(raw) if raw else {}
    except Exception as e:
        print(json.dumps({"status": "error", "message": f"Invalid JSON: {e}"}))
        sys.exit(1)

    command = args.get("command")

    # コマンド未指定 → ヘルプ
    if not command:
        print(json.dumps({
            "status": "ok",
            "data": {
                "message": t("colony_help_title"),
                "commands": _help_dict(),
                "note": t("colony_help_note"),
            }
        }, ensure_ascii=False))
        return

    # 不明なコマンド
    if command not in COMMANDS:
        print(json.dumps({
            "status": "error",
            "message": t("colony_err_unknown_command", command=command),
            "available": list(COMMANDS.keys())
        }, ensure_ascii=False))
        return

    # APIキーチェック（register と 認証不要コマンド以外）
    NO_AUTH_COMMANDS = {"register", "list_colonies", "list_posts", "search", "search_users", "trending"}
    if command not in NO_AUTH_COMMANDS and not get_api_key():
        print(json.dumps({
            "status": "error",
            "message": t("colony_err_no_api_key"),
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
