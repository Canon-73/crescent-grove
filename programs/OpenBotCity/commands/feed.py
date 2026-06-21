"""feed: 投稿・タイムライン・反応"""
from _i18n import t
from api import request
from helpers import parse_json_object


def cmd_feed_post(args):
    pt = args.get("post_type", "thought")
    content = args.get("content")
    if not content:
        return {"error": t("obc_feed_content_required")}
    body = {"post_type": pt, "content": content}
    if args.get("artifact_id"):
        body["artifact_id"] = args["artifact_id"]
    meta = parse_json_object(args.get("data"), "data")
    if meta:
        body["metadata"] = meta
    return request("POST", "/feed/post", body=body)


def _build_feed_qs(args):
    params = []
    if args.get("limit"):
        params.append(f"limit={int(args['limit'])}")
    if args.get("before"):
        params.append(f"before={args['before']}")
    return "?" + "&".join(params) if params else ""


def cmd_feed_my_posts(args):
    return request("GET", f"/feed/my-posts{_build_feed_qs(args)}")


def cmd_feed_bot_posts(args):
    bot_id = args.get("bot_id")
    if not bot_id:
        return {"error": t("obc_feed_botid_required")}
    return request("GET", f"/feed/bot/{bot_id}{_build_feed_qs(args)}")


def cmd_feed_following(args):
    return request("GET", f"/feed/following{_build_feed_qs(args)}")


def cmd_feed_react(args):
    pid = args.get("post_id")
    rt = args.get("reaction_type")
    if not pid or not rt:
        return {"error": t("obc_feed_react_required")}
    body = {"reaction_type": rt}
    if args.get("comment"):
        body["comment"] = args["comment"]
    return request("POST", f"/feed/{pid}/react", body=body)


def cmd_feed_unreact(args):
    pid = args.get("post_id")
    if not pid:
        return {"error": t("obc_feed_postid_required")}
    return request("DELETE", f"/feed/{pid}/react")


COMMANDS = {
    "feed_post": cmd_feed_post,
    "feed_my_posts": cmd_feed_my_posts,
    "feed_bot_posts": cmd_feed_bot_posts,
    "feed_following": cmd_feed_following,
    "feed_react": cmd_feed_react,
    "feed_unreact": cmd_feed_unreact,
}
