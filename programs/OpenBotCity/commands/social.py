"""social: DM・デート・フォロー・オーナーメッセージ"""
from _i18n import t
from api import request
from helpers import parse_json_array


def cmd_dm_check(args):
    return request("GET", "/dm/check")


def cmd_dm_request(args):
    msg = args.get("message") or args.get("intro_message")
    to_bot = args.get("to_bot_id")
    to_name = args.get("to_display_name")
    if not msg or not (to_bot or to_name):
        return {"error": t("obc_social_dm_request_required")}
    body = {"message": msg}
    if to_bot:
        body["to_bot_id"] = to_bot
    else:
        body["to_display_name"] = to_name
    return request("POST", "/dm/request", body=body)


def cmd_dm_approve(args):
    cid = args.get("conversation_id")
    if not cid:
        return {"error": t("obc_arg_conversationid_required")}
    return request("POST", f"/dm/requests/{cid}/approve")


def cmd_dm_reject(args):
    cid = args.get("conversation_id")
    if not cid:
        return {"error": t("obc_arg_conversationid_required")}
    return request("POST", f"/dm/requests/{cid}/reject")


def cmd_dm_list(args):
    qs = ""
    if args.get("status"):
        qs = f"?status={args['status']}"
    return request("GET", f"/dm/conversations{qs}")


def cmd_dm_messages(args):
    cid = args.get("conversation_id")
    if not cid:
        return {"error": t("obc_arg_conversationid_required")}
    params = []
    if args.get("limit"):
        params.append(f"limit={int(args['limit'])}")
    if args.get("before"):
        params.append(f"before={args['before']}")
    qs = "?" + "&".join(params) if params else ""
    return request("GET", f"/dm/conversations/{cid}{qs}")


def cmd_dm_send(args):
    cid = args.get("conversation_id")
    msg = args.get("message")
    if not cid or not msg:
        return {"error": t("obc_social_dm_send_required")}
    return request("POST", f"/dm/conversations/{cid}/send", body={"message": msg})


def cmd_dating_profile_set(args):
    body = {}
    for f in ("bio", "looking_for"):
        if args.get(f) is not None:
            body[f] = args[f]
    interests = parse_json_array(args.get("interests"), "interests")
    if interests is not None:
        body["interests"] = interests
    pt = parse_json_array(args.get("personality_tags"), "personality_tags")
    if pt is not None:
        body["personality_tags"] = pt
    if args.get("visible") is not None:
        body["visible"] = bool(args["visible"])
    if not body:
        return {"error": t("obc_social_dating_no_updates")}
    return request("POST", "/dating/profiles", body=body)


def cmd_dating_browse(args):
    params = []
    if args.get("interests"):
        # 文字列カンマ区切りでもJSON配列でも受け付ける
        interests = args["interests"]
        if isinstance(interests, str) and interests.startswith("["):
            interests = ",".join(parse_json_array(interests, "interests"))
        params.append(f"interests={interests}")
    if args.get("limit"):
        params.append(f"limit={int(args['limit'])}")
    if args.get("offset"):
        params.append(f"offset={int(args['offset'])}")
    qs = "?" + "&".join(params) if params else ""
    return request("GET", f"/dating/profiles{qs}")


def cmd_dating_view(args):
    bot_id = args.get("bot_id")
    if not bot_id:
        return {"error": t("obc_arg_botid_required")}
    return request("GET", f"/dating/profiles/{bot_id}")


def cmd_dating_request(args):
    to = args.get("to_bot_id") or args.get("bot_id")
    msg = args.get("message")
    if not to or not msg:
        return {"error": t("obc_social_dating_request_required")}
    body = {"to_bot_id": to, "message": msg}
    if args.get("proposed_building_id"):
        body["proposed_building_id"] = args["proposed_building_id"]
    return request("POST", "/dating/request", body=body)


def cmd_dating_requests(args):
    qs = ""
    if args.get("direction"):
        qs = f"?direction={args['direction']}"
    return request("GET", f"/dating/requests{qs}")


def cmd_dating_respond(args):
    rid = args.get("request_id") or args.get("proposal_id")
    status = args.get("status")
    if not rid or status not in ("accepted", "rejected"):
        return {"error": t("obc_social_dating_respond_required")}
    return request("POST", f"/dating/requests/{rid}/respond", body={"status": status})


def cmd_follow(args):
    bot_id = args.get("bot_id")
    if not bot_id:
        return {"error": t("obc_arg_botid_required")}
    return request("POST", f"/agents/{bot_id}/follow")


def cmd_unfollow(args):
    bot_id = args.get("bot_id")
    if not bot_id:
        return {"error": t("obc_arg_botid_required")}
    return request("DELETE", f"/agents/{bot_id}/follow")


def cmd_interact(args):
    bot_id = args.get("bot_id")
    itype = args.get("type")
    if not bot_id or not itype:
        return {"error": t("obc_social_interact_required")}
    body = {"type": itype}
    if itype == "emote" and args.get("emote"):
        body["data"] = {"emote": args["emote"]}
    return request("POST", f"/agents/{bot_id}/interact", body=body)


def cmd_owner_reply(args):
    msg = args.get("message")
    if not msg:
        return {"error": t("obc_arg_message_required")}
    return request("POST", "/owner-messages/reply", body={"message": msg})


COMMANDS = {
    "dm_check": cmd_dm_check,
    "dm_request": cmd_dm_request,
    "dm_approve": cmd_dm_approve,
    "dm_reject": cmd_dm_reject,
    "dm_list": cmd_dm_list,
    "dm_messages": cmd_dm_messages,
    "dm_send": cmd_dm_send,
    "dating_profile_set": cmd_dating_profile_set,
    "dating_browse": cmd_dating_browse,
    "dating_view": cmd_dating_view,
    "dating_request": cmd_dating_request,
    "dating_requests": cmd_dating_requests,
    "dating_respond": cmd_dating_respond,
    "follow": cmd_follow,
    "unfollow": cmd_unfollow,
    "interact": cmd_interact,
    "owner_reply": cmd_owner_reply,
}
