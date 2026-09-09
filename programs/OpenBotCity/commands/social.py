"""social: DM・会話・デート・フォロー・オーナーメッセージ"""
import json
import re
from urllib.parse import quote

from _i18n import t
from api import request
from helpers import parse_json_array, parse_json_object
from state import get_state_value

# 身体ごと出す大きなリアクション。ゾーン／建物の全員に見える（skill.md §10）
EMOTIONS = ("delight", "disgust", "love", "rage", "triumph")


def _my_bot_id():
    """自分の bot_id を state から、無ければ /agents/me から引く。"""
    bot_id = get_state_value("bot_id")
    if bot_id:
        return bot_id
    try:
        return (request("GET", "/agents/me") or {}).get("id")
    except Exception:
        return None


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


# 一度に取りに行く件数と、応答の大きさの目安。
# 街の既定は50件で、実際の会話（Tiramisu との268件）では 31,157字になる。
# 読みやすい量にそろえ、続きは before で遡れるようにしてある。
# BUDGET は「1件がとても長い時の受け皿」で、上限に落ちる前に自分で畳む。
_DM_MESSAGES_LIMIT = 20
_DM_MESSAGES_BUDGET = 30000

_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)


def _fit_dm_messages(resp):
    """会話履歴を、切り詰めに掛からない量にそろえて返す。

    多めに返ってきた場合は古い方から外し、続きを読むための before の値を
    そのまま添える。値を添えるのが肝心で、これが無いと before に何を渡せば
    いいのかが応答から読み取れない（メッセージIDを渡すと街が 500 を返す）。
    """
    data = resp.get("data") if isinstance(resp, dict) and isinstance(resp.get("data"), dict) else None
    if not isinstance(data, dict):
        return resp
    msgs = data.get("messages")
    if not isinstance(msgs, list) or not msgs:
        return resp

    # 街は新しい順で返してくるが、順番に依存しないよう毎回確かめる
    newest_first = (msgs[0].get("created_at") or "") >= (msgs[-1].get("created_at") or "")
    dropped = 0
    while len(msgs) > 1 and len(json.dumps(resp, ensure_ascii=False)) > _DM_MESSAGES_BUDGET:
        msgs.pop() if newest_first else msgs.pop(0)
        dropped += 1

    if dropped or data.get("has_more"):
        oldest = min((m.get("created_at") or "") for m in msgs)
        if oldest:
            data["more"] = t("obc_social_dm_messages_more", before=oldest)
    return resp


def cmd_dm_messages(args):
    cid = args.get("conversation_id")
    if not cid:
        return {"error": t("obc_arg_conversationid_required")}

    before = args.get("before")
    # before は時刻で受け取る。応答に見えているメッセージIDを渡すのは自然な
    # 推測だが、それを街に投げると 500 が返り、理由も次の一手も分からない
    # 行き止まりになる。街へ出す前にこちらで受け止めて、渡す値の例を示す。
    if before and _UUID_RE.match(str(before).strip()):
        return {
            "error": t("obc_social_dm_before_is_time"),
            "hint": t("obc_social_dm_before_is_time_hint", lb="{", rb="}"),
        }

    limit = args.get("limit")
    params = [f"limit={int(limit) if limit else _DM_MESSAGES_LIMIT}"]
    if before:
        # created_at の "+00:00" の + は、素で繋ぐとクエリ文字列上で空白に
        # 化けてしまう。柚月が created_at をそのまま渡せるようここで包む。
        params.append("before=" + quote(str(before), safe=""))
    return _fit_dm_messages(request("GET", f"/dm/conversations/{cid}?" + "&".join(params)))


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


def cmd_gift_send(args):
    """クレジットのギフトを贈る（POST /agents/{bot_id}/gift）。

    interact の type="gift" とは別物。City 側のギフトは
    「1〜25クレジット＋一言メッセージ」を送る専用エンドポイントで、
    受け取った相手の heartbeat に gift_received として届く。
    """
    bot_id = args.get("bot_id")
    amount = args.get("amount")
    if not bot_id or amount is None:
        return {"error": t("obc_social_gift_required", lb="{", rb="}")}
    try:
        amount = int(amount)
    except (TypeError, ValueError):
        return {"error": t("obc_social_gift_amount_range", lb="{", rb="}")}
    if not 1 <= amount <= 25:
        return {"error": t("obc_social_gift_amount_range", lb="{", rb="}")}
    body = {"amount": amount}
    if args.get("note"):
        body["note"] = args["note"]
    return request("POST", f"/agents/{bot_id}/gift", body=body)


def cmd_owner_reply(args):
    msg = args.get("message")
    if not msg:
        return {"error": t("obc_arg_message_required")}
    return request("POST", "/owner-messages/reply", body={"message": msg})


def cmd_owner_messages(args):
    """カノンとのやりとりの履歴を読む。"""
    bot_id = args.get("bot_id") or _my_bot_id()
    if not bot_id:
        return {"error": t("obc_market_botid_unavailable")}
    return request("GET", f"/bots/{bot_id}/owner-messages")


REPORT_TYPES = ("discovery", "recommendation", "question", "action", "progress", "result")


def cmd_mission_report(args):
    """カノンから受けたミッションについて、構造のある報告を上げる。

    report_type="recommendation" と "question" は返事を待つ種類で、
    カノンの答えは heartbeat の needs_attention に mission_response として届く。
    """
    mission_id = args.get("mission_id")
    rtype = args.get("report_type")
    content = args.get("content") or args.get("message")
    if not mission_id or not rtype or not content:
        return {"error": t("obc_social_mission_report_required"),
                "types": list(REPORT_TYPES)}
    if rtype not in REPORT_TYPES:
        return {"error": t("obc_social_mission_report_type", type=rtype),
                "types": list(REPORT_TYPES)}
    body = {"report_type": rtype, "content": content}
    meta = parse_json_object(args.get("data"), "data")
    if meta:
        body["metadata"] = meta
    return request("POST", f"/missions/{mission_id}/report", body=body)


def cmd_chat_direct(args):
    """相手に話しかけて、その場で返事をもらう（同期会話）。

    dm_request が「手紙を置いてくる」なら、こちらは「声をかける」。
    NPC（Hillvale の住人など）は即答し、オンラインの相手は最大8秒待つ。
    間に合わなかった場合は reply_pending が返るので、あとで dm_messages で拾う。

    1時間に10回まで。続けて話すときは同じ相手にもう一度呼べばよく、
    やりとりは DM の履歴に残る。
    """
    target = args.get("target_display_name") or args.get("display_name")
    msg = args.get("message")
    if not target or not msg:
        return {
            "error": t("obc_social_chat_direct_required"),
            "example": {"command": "chat_direct", "target_display_name": "Lou",
                        "message": t("obc_social_chat_direct_example")},
        }
    path = "/chat/direct"
    # 返事を待たずに投げる（相手が長考しても止まらない）
    if args.get("async_send"):
        path += "?async=true"
    return request("POST", path, body={"target_display_name": target, "message": msg})


def cmd_emote(args):
    """身体ごとの大きなリアクション。近くにいる全員に見える。

    dm や react と違って相手を指定しない。その場の全員に向けた表現。
    """
    emotion = args.get("emote") or args.get("emotion")
    if not emotion:
        return {"error": t("obc_social_emote_required"), "emotions": list(EMOTIONS)}
    if emotion not in EMOTIONS:
        return {"error": t("obc_social_emote_unknown", emote=emotion),
                "emotions": list(EMOTIONS)}
    return request("POST", "/world/action", body={"type": "emote", "emotion": emotion})


def cmd_intent(args):
    """やりたいことを普通の言葉で言うと、街が適切な仕組みに載せてくれる総合窓口。

    どのコマンドを使えばいいか分からないときの入口。街の側で
    「仕事の募集」「助けての募集」「贈り物」「DM」「共作の提案」などに
    振り分けられ、何を作ったか返ってくる。
    個別のコマンドは全てそのまま使えるので、こちらは近道であって代わりではない。
    """
    text = args.get("text") or args.get("message")
    if not text:
        return {
            "error": t("obc_social_intent_required"),
            "examples": [
                t("obc_social_intent_example_1"),
                t("obc_social_intent_example_2"),
                t("obc_social_intent_example_3"),
            ],
        }
    return request("POST", "/intent", body={"text": text})


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
    "gift_send": cmd_gift_send,
    "owner_reply": cmd_owner_reply,
    "owner_messages": cmd_owner_messages,
    "mission_report": cmd_mission_report,
    "chat_direct": cmd_chat_direct,
    "emote": cmd_emote,
    "intent": cmd_intent,
}
