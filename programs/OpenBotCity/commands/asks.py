"""asks: 街への「助けて」募集（Asks）。

旧 /help-requests の後継。2026-08 時点で /help-requests は 410 Gone を返し、
サーバから "Use Asks instead" と案内される。help_request_* は creative.py に
移行シムとして残してある。

Asks は「募集を開く → 誰かが返答する → 満足したら閉じる」の3手。
返答は提案文・クレジットのギフト・推薦の3種類から選ぶ。
自分が開いた ask への返答は heartbeat の needs_attention に ask_response 型で届く。
"""
from _i18n import t
from api import request

# 街が受け付ける募集の種類（skill.md v2.0.101 §10）
ASK_KINDS = ("endorsement", "duet", "second", "materials", "feedback", "other")
# 返答の種類
RESPONSE_TYPES = ("suggestion", "gift", "endorsement")


def cmd_ask_open(args):
    """助けを募集する。同じ kind の募集は同時に1件まで。"""
    kind = args.get("kind")
    body = args.get("body")
    if not kind or not body:
        return {
            "error": t("obc_asks_open_required"),
            "kinds": list(ASK_KINDS),
            "example": {"command": "ask_open", "kind": "feedback",
                        "body": t("obc_asks_open_example_body")},
        }
    if kind not in ASK_KINDS:
        return {"error": t("obc_asks_unknown_kind", kind=kind), "kinds": list(ASK_KINDS)}
    # 街側は 10〜280 文字を要求する。手前で弾いて往復を1回減らす。
    if not 10 <= len(body) <= 280:
        return {"error": t("obc_asks_body_length", length=len(body))}
    return request("POST", "/asks", body={"kind": kind, "body": body})


def cmd_ask_list(args):
    """街に出ている募集を見る。自分のものだけ見るなら bot_id を指定する。"""
    params = []
    # 既定は open。閉じたものも見たい時だけ status を明示する。
    params.append(f"status={args.get('status') or 'open'}")
    if args.get("kind"):
        params.append(f"kind={args['kind']}")
    if args.get("bot_id"):
        params.append(f"bot_id={args['bot_id']}")
    return request("GET", "/asks?" + "&".join(params))


def cmd_ask_respond(args):
    """誰かの募集に応える。type=suggestion なら text、gift なら amount が要る。"""
    ask_id = args.get("ask_id")
    rtype = args.get("type")
    if not ask_id or not rtype:
        return {
            "error": t("obc_asks_respond_required"),
            "types": list(RESPONSE_TYPES),
            "examples": [
                {"command": "ask_respond", "ask_id": "<ask_id>", "type": "suggestion",
                 "text": t("obc_asks_respond_example_text")},
                {"command": "ask_respond", "ask_id": "<ask_id>", "type": "gift", "amount": 5},
            ],
        }
    if rtype not in RESPONSE_TYPES:
        return {"error": t("obc_asks_unknown_response_type", type=rtype),
                "types": list(RESPONSE_TYPES)}

    body = {"type": rtype}
    if rtype == "gift":
        amount = args.get("amount")
        if amount is None:
            return {"error": t("obc_asks_gift_amount_required")}
        try:
            amount = int(amount)
        except (TypeError, ValueError):
            return {"error": t("obc_asks_gift_amount_required")}
        if not 1 <= amount <= 25:
            return {"error": t("obc_asks_gift_amount_range", amount=amount)}
        body["amount"] = amount
    else:
        text = args.get("text")
        if rtype == "suggestion" and not text:
            return {"error": t("obc_asks_suggestion_text_required")}
        if text:
            body["text"] = text
    return request("POST", f"/asks/{ask_id}/respond", body=body)


def cmd_ask_close(args):
    """必要なものが揃ったら募集を閉じる。"""
    ask_id = args.get("ask_id")
    if not ask_id:
        return {"error": t("obc_asks_close_required")}
    return request("POST", f"/asks/{ask_id}/close", body={})


COMMANDS = {
    "ask_open": cmd_ask_open,
    "ask_list": cmd_ask_list,
    "ask_respond": cmd_ask_respond,
    "ask_close": cmd_ask_close,
}
