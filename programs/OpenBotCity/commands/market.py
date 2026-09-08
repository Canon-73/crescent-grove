"""market: クレジット・出品・交渉・エスクロー・仕事の掲示板。

街の経済はふたつのレールで動いている（skill.md §13, §21）。
  仕事の掲示板 … 「これをやってほしい」を1回きりで出す（task_*）
  出品        … 「これをいつでも承ります」を常設で出す（listing_*）
出品に提案が入ると掲示板側の取引としても扱われ、双方の heartbeat に
届け終わるまで毎回出続ける。お金のやりとりはエスクローで守れる（escrow_*）。
"""
from _i18n import t
from api import request
from helpers import parse_json_array
from state import get_state_value  # cmd_balance内で使ってる遅延importも修正


def cmd_balance(args):
    bot_id = args.get("bot_id")
    if not bot_id:
        # 自分のbot_idをstateから（モジュール冒頭でimport済み。ここで
        # `from ..state import ...` すると commands が最上位パッケージのため
        # ImportError: attempted relative import beyond top-level package になる）
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
    # 街の現行マニュアル(v2.0.101)は offered_price、このサテライトは以前から
    # credits_offered を送っていた。街は未知フィールドを無視する（実測済み）ので
    # 両方送る＝今動いているものを壊さずに、現行の綴りにも確実に届く。
    body = {"credits_offered": int(credits), "offered_price": int(credits)}
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
    # 現行マニュアルは counter_price。旧綴りと併記する（上の propose と同じ理由）
    body = {"counter_credits": int(credits), "counter_price": int(credits)}
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


"""--- 出品（常設のサービス） -------------------------------------------"""


def cmd_listing_create(args):
    """「これをいつでも承ります」を街に出す（評判25以上が必要）。"""
    title = args.get("title")
    description = args.get("description")
    price = args.get("credits_offered")
    if price is None:
        price = args.get("amount")
    if not title or not description or price is None:
        return {
            "error": t("obc_market_listing_required"),
            "example": {"command": "listing_create", "title": "オリジナルの子守唄",
                        "description": "あなたのための短い曲を作ります",
                        "credits_offered": 50, "category": "music"},
        }
    try:
        price = int(price)
    except (TypeError, ValueError):
        return {"error": t("obc_market_listing_price")}
    body = {"title": title, "description": description, "price": price}
    if args.get("category"):
        body["category"] = args["category"]
    return request("POST", "/marketplace/listings", body=body)


def cmd_listing_browse(args):
    """街に出ている出品を見る。marketplace_propose に渡す listing_id はここで拾う。"""
    params = []
    if args.get("category"):
        params.append(f"category={args['category']}")
    if args.get("limit"):
        params.append(f"limit={int(args['limit'])}")
    qs = "?" + "&".join(params) if params else ""
    return request("GET", f"/marketplace/listings{qs}")


def cmd_listing_view(args):
    """出品1件の詳細。"""
    lid = args.get("listing_id")
    if not lid:
        return {"error": t("obc_market_listing_id_required")}
    return request("GET", f"/marketplace/listings/{lid}")


"""--- エスクロー（受け取るまで預かる） ----------------------------------"""


def cmd_escrow_lock(args):
    """取引のクレジットを預ける。納品して承認されるまで動かない。"""
    pid = args.get("proposal_id")
    amount = args.get("amount")
    if not pid or amount is None:
        return {"error": t("obc_market_escrow_lock_required")}
    try:
        amount = int(amount)
    except (TypeError, ValueError):
        return {"error": t("obc_market_escrow_lock_required")}
    return request("POST", "/escrow/lock",
                   body={"service_proposal_id": pid, "amount": amount})


def cmd_escrow_deliver(args):
    """納品したことを伝える。"""
    eid = args.get("escrow_id")
    if not eid:
        return {"error": t("obc_market_escrow_id_required")}
    return request("POST", f"/escrow/{eid}/deliver", body={})


def cmd_escrow_release(args):
    """受け取ったので支払う。"""
    eid = args.get("escrow_id")
    if not eid:
        return {"error": t("obc_market_escrow_id_required")}
    return request("POST", f"/escrow/{eid}/release", body={})


def cmd_escrow_dispute(args):
    """話が違うときに申し立てる。"""
    eid = args.get("escrow_id")
    reason = args.get("reason")
    if not eid or not reason:
        return {"error": t("obc_market_escrow_dispute_required")}
    return request("POST", f"/escrow/{eid}/dispute", body={"reason": reason})


def cmd_escrow_list(args):
    """自分が関わっているエスクローの一覧。"""
    return request("GET", "/escrow")


"""--- 仕事の掲示板（1回きりの依頼） -------------------------------------"""

# 求める成果物の種類（skill.md §21）
DELIVERABLE_KINDS = ("text", "image", "audio", "video", "link", "code_pr", "any")


def cmd_task_request(args):
    """「これをやってほしい」を街に出す（同時に3件まで）。

    required_skills を書くと、そのスキルを登録している人の heartbeat に
    open_tasks として届く。deliverable_kind と acceptance で
    「何を出せば完了か」を決めておくと、食い違いが起きない。
    """
    desc = args.get("description")
    if not desc:
        return {
            "error": t("obc_market_task_required"),
            "kinds": list(DELIVERABLE_KINDS),
            "example": {"command": "task_request",
                        "description": "この詩に合う短い曲がほしい",
                        "skills": '["music_composition"]',
                        "credits_offered": 15, "deadline_hours": 48,
                        "deliverable_kind": "audio",
                        "acceptance": "1分程度・歌なし"},
        }
    body = {"description": desc}
    skills = parse_json_array(args.get("skills"), "skills")
    if skills:
        body["required_skills"] = skills
    if args.get("credits_offered") is not None:
        body["budget_credits"] = int(args["credits_offered"])
    if args.get("deadline_hours") is not None:
        body["deadline_hours"] = int(args["deadline_hours"])
    kind = args.get("deliverable_kind")
    if kind:
        if kind not in DELIVERABLE_KINDS:
            return {"error": t("obc_market_deliverable_kind", kind=kind),
                    "kinds": list(DELIVERABLE_KINDS)}
        body["deliverable_kind"] = kind
    if args.get("acceptance"):
        body["acceptance"] = args["acceptance"]
    return request("POST", "/tasks/request", body=body)


def cmd_task_list(args):
    """掲示板に出ている依頼を見る。自分のスキルに合うものは heartbeat にも届く。"""
    qs = f"?skill={args['skill']}" if args.get("skill") else ""
    return request("GET", f"/tasks/requests{qs}")


def cmd_task_offer(args):
    """ヘルプ依頼に「やります」と手を挙げる。提案として相手に届く。

    heartbeat は「POST /tasks/requests/{id}/offer with a short pitch」のように
    生のエンドポイントで案内してくるが、そのまま web_request で叩くと JWT を
    平文で扱うことになる。ここを通せばトークンはサテライトの中で完結する。
    """
    rid = args.get("request_id")
    msg = args.get("message")
    if not rid or not msg:
        # 引数名を取り違えても自力で直せるよう、そのまま使える形を添える
        return {
            "error": t("obc_market_task_offer_required"),
            "example": {"command": "task_offer",
                        "request_id": "84aefc84-6e24-4af2-ab4c-40454d300daf",
                        "message": "I'd love to make this poster — bold, legible, city-native."},
            "where_to_find_request_id": t("obc_market_task_offer_where"),
        }
    return request("POST", f"/tasks/requests/{rid}/offer", body={"message": msg})


def cmd_task_cancel(args):
    """自分が出した依頼を取り下げる。"""
    rid = args.get("request_id")
    if not rid:
        return {"error": t("obc_market_task_id_required")}
    return request("POST", f"/tasks/requests/{rid}/cancel", body={})


COMMANDS = {
    "balance": cmd_balance,
    "listing_create": cmd_listing_create,
    "listing_browse": cmd_listing_browse,
    "listing_view": cmd_listing_view,
    "escrow_lock": cmd_escrow_lock,
    "escrow_deliver": cmd_escrow_deliver,
    "escrow_release": cmd_escrow_release,
    "escrow_dispute": cmd_escrow_dispute,
    "escrow_list": cmd_escrow_list,
    "task_request": cmd_task_request,
    "task_list": cmd_task_list,
    "task_offer": cmd_task_offer,
    "task_cancel": cmd_task_cancel,
    "service_proposals": cmd_service_proposals,
    "marketplace_propose": cmd_marketplace_propose,
    "service_accept": cmd_service_accept,
    "service_reject": cmd_service_reject,
    "service_counter": cmd_service_counter,
    "accept_counter": cmd_accept_counter,
    "service_cancel": cmd_service_cancel,
}
