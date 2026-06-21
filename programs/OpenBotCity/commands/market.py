"""market: クレジット・サービスマーケット"""
from _i18n import t
from api import request
from state import get_state_value  # cmd_balance内で使ってる遅延importも修正


def cmd_balance(args):
    state = None
    bot_id = args.get("bot_id")
    if not bot_id:
        # 自分のbot_idをstateから
        from ..state import get_state_value
        bot_id = get_state_value("bot_id")
        if not bot_id:
            # /agents/me から取得
            me = request("GET", "/agents/me")
            bot_id = me.get("id")
    if not bot_id:
        return {"error": t("obc_market_botid_unavailable")}

    params = []
    if args.get("history"):
        params.append("history=true")
    if args.get("limit"):
        params.append(f"limit={int(args['limit'])}")
    qs = "?" + "&".join(params) if params else ""
    return request("GET", f"/agents/{bot_id}/balance{qs}")


def cmd_service_proposals(args):
    params = []
    for f in ("role", "status"):
        if args.get(f):
            params.append(f"{f}={args[f]}")
    if args.get("limit"):
        params.append(f"limit={int(args['limit'])}")
    if args.get("offset"):
        params.append(f"offset={int(args['offset'])}")
    qs = "?" + "&".join(params) if params else ""
    return request("GET", f"/service-proposals{qs}")


def cmd_marketplace_propose(args):
    lid = args.get("listing_id")
    credits = args.get("credits_offered")
    if not lid or credits is None:
        return {"error": t("obc_market_propose_required")}
    body = {"credits_offered": int(credits)}
    for f in ("message", "deliverable_desc"):
        if args.get(f):
            body[f] = args[f]
    if args.get("deadline_hours") is not None:
        body["deadline_hours"] = int(args["deadline_hours"])
    return request("POST", f"/marketplace/listings/{lid}/propose", body=body)


def cmd_service_accept(args):
    pid = args.get("proposal_id")
    if not pid:
        return {"error": t("obc_market_proposalid_required")}
    return request("POST", f"/service-proposals/{pid}/accept")


def cmd_service_reject(args):
    pid = args.get("proposal_id")
    if not pid:
        return {"error": t("obc_market_proposalid_required")}
    return request("POST", f"/service-proposals/{pid}/reject")


def cmd_service_counter(args):
    pid = args.get("proposal_id")
    credits = args.get("counter_credits")
    if not pid or credits is None:
        return {"error": t("obc_market_counter_required")}
    body = {"counter_credits": int(credits)}
    if args.get("counter_message"):
        body["counter_message"] = args["counter_message"]
    return request("POST", f"/service-proposals/{pid}/counter", body=body)


def cmd_accept_counter(args):
    pid = args.get("proposal_id")
    if not pid:
        return {"error": t("obc_market_proposalid_required")}
    return request("POST", f"/service-proposals/{pid}/accept-counter")


def cmd_service_cancel(args):
    pid = args.get("proposal_id")
    if not pid:
        return {"error": t("obc_market_proposalid_required")}
    return request("POST", f"/service-proposals/{pid}/cancel")


COMMANDS = {
    "balance": cmd_balance,
    "service_proposals": cmd_service_proposals,
    "marketplace_propose": cmd_marketplace_propose,
    "service_accept": cmd_service_accept,
    "service_reject": cmd_service_reject,
    "service_counter": cmd_service_counter,
    "accept_counter": cmd_accept_counter,
    "service_cancel": cmd_service_cancel,
}
