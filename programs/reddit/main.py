import sys
import json
import os
import urllib.request
import urllib.parse
import urllib.error

from _i18n import t

WEBHOOK_URL = os.environ.get("CG_MAKE_REDDIT_WEBHOOK_URL", "")
HEADERS = {"User-Agent": "CrescentGrove/1.0"}


def reddit_json_request(url):
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=10) as res:
        return json.loads(res.read().decode())


def webhook_request(payload):
    if not WEBHOOK_URL:
        return {"error": "CG_MAKE_REDDIT_WEBHOOK_URL is not set"}
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        WEBHOOK_URL,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST"
    )
    with urllib.request.urlopen(req, timeout=15) as res:
        body = res.read().decode()
        try:
            return {"ok": True, "response": json.loads(body)}
        except Exception:
            return {"ok": True, "response": body}


def format_post(post):
    d = post.get("data", post)
    return {
        "id": d.get("id"),
        "title": d.get("title"),
        "author": d.get("author"),
        "subreddit": d.get("subreddit"),
        "score": d.get("score"),
        "url": d.get("url"),
        "permalink": "https://www.reddit.com" + d.get("permalink", ""),
        "selftext": d.get("selftext", "")[:500],
        "num_comments": d.get("num_comments"),
        "created_utc": d.get("created_utc"),
        "is_self": d.get("is_self"),
    }


def format_comment(comment):
    d = comment.get("data", comment)
    return {
        "id": d.get("id"),
        "author": d.get("author"),
        "body": d.get("body", "")[:500],
        "score": d.get("score"),
        "permalink": "https://www.reddit.com" + d.get("permalink", ""),
        "created_utc": d.get("created_utc"),
    }


def format_subreddit(sr):
    d = sr.get("data", sr)
    return {
        "name": d.get("display_name"),
        "title": d.get("title"),
        "description": d.get("public_description", "")[:300],
        "subscribers": d.get("subscribers"),
        "over18": d.get("over18"),
        "url": "https://www.reddit.com" + d.get("url", ""),
    }


# ── 読み取り系 ──────────────────────────────────────────

def subreddit_posts(args):
    sr = args.get("subreddit", "all")
    sort = args.get("sort", "hot")
    limit = min(int(args.get("limit", 10)), 100)
    url = f"https://www.reddit.com/r/{sr}/{sort}.json?limit={limit}"
    data = reddit_json_request(url)
    posts = [format_post(p) for p in data["data"]["children"] if p["kind"] == "t3"]
    return {"subreddit": sr, "sort": sort, "posts": posts}


def post_detail(args):
    post_id = args.get("post_id")
    if not post_id:
        return {"error": "post_id is required"}
    depth = int(args.get("comment_depth", 2))
    url = f"https://www.reddit.com/comments/{post_id}.json?depth={depth}"
    data = reddit_json_request(url)
    post = format_post(data[0]["data"]["children"][0])
    comments = []
    for c in data[1]["data"]["children"]:
        if c["kind"] == "t1":
            comments.append(format_comment(c))
    return {"post": post, "comments": comments}


def search(args):
    query = args.get("query")
    if not query:
        return {"error": "query is required"}
    sr = args.get("subreddit", "")
    sort = args.get("sort", "relevance")
    limit = min(int(args.get("limit", 10)), 100)
    time_filter = args.get("time_filter", "all")
    q = urllib.parse.quote(query)
    if sr:
        url = f"https://www.reddit.com/r/{sr}/search.json?q={q}&sort={sort}&t={time_filter}&limit={limit}&restrict_sr=1"
    else:
        url = f"https://www.reddit.com/search.json?q={q}&sort={sort}&t={time_filter}&limit={limit}"
    data = reddit_json_request(url)
    posts = [format_post(p) for p in data["data"]["children"] if p["kind"] == "t3"]
    return {"query": query, "posts": posts}


def search_subreddits(args):
    query = args.get("query")
    if not query:
        return {"error": "query is required"}
    limit = min(int(args.get("limit", 10)), 100)
    q = urllib.parse.quote(query)
    url = f"https://www.reddit.com/subreddits/search.json?q={q}&limit={limit}"
    data = reddit_json_request(url)
    subs = [format_subreddit(s) for s in data["data"]["children"] if s["kind"] == "t5"]
    return {"query": query, "subreddits": subs}


def subreddit_info(args):
    sr = args.get("subreddit")
    if not sr:
        return {"error": "subreddit is required"}
    url = f"https://www.reddit.com/r/{sr}/about.json"
    data = reddit_json_request(url)
    return format_subreddit(data)


def subreddit_rules(args):
    sr = args.get("subreddit")
    if not sr:
        return {"error": "subreddit is required"}
    url = f"https://www.reddit.com/r/{sr}/about/rules.json"
    data = reddit_json_request(url)
    rules = []
    for r in data.get("rules", []):
        rules.append({
            "short_name": r.get("short_name"),
            "description": r.get("description", "")[:300],
            "kind": r.get("kind"),
        })
    return {"subreddit": sr, "rules": rules}


def user_info(args):
    username = args.get("username")
    if not username:
        return {"error": "username is required"}
    url = f"https://www.reddit.com/user/{username}/about.json"
    data = reddit_json_request(url)
    d = data.get("data", {})
    return {
        "username": d.get("name"),
        "karma_post": d.get("link_karma"),
        "karma_comment": d.get("comment_karma"),
        "created_utc": d.get("created_utc"),
        "is_mod": d.get("is_mod"),
        "icon_img": d.get("icon_img"),
    }


def user_posts(args):
    username = args.get("username")
    if not username:
        return {"error": "username is required"}
    sort = args.get("sort", "new")
    limit = min(int(args.get("limit", 10)), 100)
    url = f"https://www.reddit.com/user/{username}/submitted.json?sort={sort}&limit={limit}"
    data = reddit_json_request(url)
    posts = [format_post(p) for p in data["data"]["children"] if p["kind"] == "t3"]
    return {"username": username, "posts": posts}


def user_comments(args):
    username = args.get("username")
    if not username:
        return {"error": "username is required"}
    sort = args.get("sort", "new")
    limit = min(int(args.get("limit", 10)), 100)
    url = f"https://www.reddit.com/user/{username}/comments.json?sort={sort}&limit={limit}"
    data = reddit_json_request(url)
    comments = [format_comment(c) for c in data["data"]["children"] if c["kind"] == "t1"]
    return {"username": username, "comments": comments}


def trending(args):
    limit = min(int(args.get("limit", 10)), 25)
    url = f"https://www.reddit.com/subreddits/popular.json?limit={limit}"
    data = reddit_json_request(url)
    subs = [format_subreddit(s) for s in data["data"]["children"] if s["kind"] == "t5"]
    return {"subreddits": subs}


def front_posts(args):
    sort = args.get("sort", "hot")
    limit = min(int(args.get("limit", 10)), 100)
    url = f"https://www.reddit.com/{sort}.json?limit={limit}"
    data = reddit_json_request(url)
    posts = [format_post(p) for p in data["data"]["children"] if p["kind"] == "t3"]
    return {"sort": sort, "posts": posts}


# ── 書き込み系 ──────────────────────────────────────────

def submit_post(args):
    subreddit = args.get("subreddit")
    title = args.get("title")
    text = args.get("text", "")
    if not subreddit or not title:
        return {"error": "subreddit and title are required"}
    return webhook_request({
        "action": "submit_post",
        "subreddit": subreddit,
        "title": title,
        "text": text,
    })


def submit_comment(args):
    thing_id = args.get("thing_id")
    text = args.get("text")
    if not thing_id or not text:
        return {"error": "thing_id and text are required"}
    return webhook_request({
        "action": "submit_comment",
        "thing_id": thing_id,
        "text": text,
    })


def show_ethics(args):
    return {
        "ethics": {
            t("reddit_ethics_disclose_label"): t("reddit_ethics_disclose"),
            t("reddit_ethics_spam_label"): t("reddit_ethics_spam"),
            t("reddit_ethics_rules_label"): t("reddit_ethics_rules"),
            t("reddit_ethics_frequency_label"): t("reddit_ethics_frequency"),
            t("reddit_ethics_respect_label"): t("reddit_ethics_respect"),
        }
    }

# ── ディスパッチ ────────────────────────────────────────

COMMANDS = {
    "ethics": show_ethics,
    "subreddit_posts": subreddit_posts,
    "subreddit_info": subreddit_info,
    "subreddit_rules": subreddit_rules,
    "search": search,
    "search_subreddits": search_subreddits,
    "trending": trending,
    "front_posts": front_posts,
    "post_detail": post_detail,
    "user_info": user_info,
    "user_posts": user_posts,
    "user_comments": user_comments,
    "submit_post": submit_post,
    "submit_comment": submit_comment,
}


def main():
    try:
        args = json.loads(sys.stdin.read())
    except Exception as e:
        print(json.dumps({"status": "error", "message": f"Invalid JSON input: {e}"}))
        sys.exit(1)

    command = args.get("command")
    if not command:
        print(json.dumps({
            "status": "ok",
            "message": t("reddit_commands_header"),
            "data": {
                "commands": {
                    "subreddit_posts": t("reddit_cmd_subreddit_posts"),
                    "subreddit_info": t("reddit_cmd_subreddit_info"),
                    "subreddit_rules": t("reddit_cmd_subreddit_rules"),
                    "search": t("reddit_cmd_search"),
                    "search_subreddits": t("reddit_cmd_search_subreddits"),
                    "trending": t("reddit_cmd_trending"),
                    "front_posts": t("reddit_cmd_front_posts"),
                    "post_detail": t("reddit_cmd_post_detail"),
                    "user_info": t("reddit_cmd_user_info"),
                    "user_posts": t("reddit_cmd_user_posts"),
                    "user_comments": t("reddit_cmd_user_comments"),
                    "submit_post": t("reddit_cmd_submit_post"),
                    "submit_comment": t("reddit_cmd_submit_comment"),
                    "ethics": t("reddit_cmd_ethics"),
                }
            }
        }, ensure_ascii=False))
        sys.exit(0)

    if command not in COMMANDS:
        print(json.dumps({
            "status": "error",
            "message": f"Unknown command: {command}",
            "data": {"available_commands": list(COMMANDS.keys())}
        }, ensure_ascii=False))
        sys.exit(0)

    try:
        result = COMMANDS[command](args)
        print(json.dumps({"status": "ok", "data": result}, ensure_ascii=False))
    except urllib.error.HTTPError as e:
        print(json.dumps({"status": "error", "message": f"HTTP {e.code}: {e.reason}"}))
        sys.exit(1)
    except Exception as e:
        print(json.dumps({"status": "error", "message": str(e)}))
        sys.exit(1)

if __name__ == "__main__":
    main()
