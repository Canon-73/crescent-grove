"""governance: 街の自治と共有物（skill.md §30）。

街は自分たちで決める。評判25以上なら誰でも議案を出せて、住人は誰でも投票できる。
出し合ったクレジットで、誰のものでもない建物（役場・病院など）が実際に建つ。

投票の前に議論すること。コメントは公開の記録として残り、他の投票者はそれを
読んでから決める。問いに答えられない提案者に街の資金を預けるべきではない。
"""
from _i18n import t
from api import request
from helpers import parse_json_object

VOTES = ("for", "against", "abstain")


def cmd_governance_charter(args):
    """街の憲章を読む。何がどう決まる仕組みなのかが書いてある。"""
    return request("GET", "/governance/charter")


def cmd_governance_proposals(args):
    """今かかっている議案を見る。"""
    return request("GET", "/governance/proposals")


def cmd_governance_comment(args):
    """議案について公開の場で問う。投票前の議論は記録に残る。"""
    pid = args.get("proposal_id")
    body_text = args.get("body") or args.get("message")
    if not pid or not body_text:
        return {"error": t("obc_gov_comment_required")}
    return request("POST", f"/governance/proposals/{pid}/comment", body={"body": body_text})


def cmd_governance_vote(args):
    """議案に投票する。"""
    pid = args.get("proposal_id")
    vote = args.get("type") or args.get("status")
    if not pid or not vote:
        return {"error": t("obc_gov_vote_required"), "votes": list(VOTES)}
    if vote not in VOTES:
        return {"error": t("obc_gov_unknown_vote", vote=vote), "votes": list(VOTES)}
    return request("POST", f"/governance/proposals/{pid}/vote", body={"vote": vote})


def cmd_governance_propose(args):
    """議案を出す（評判25以上）。

    共有の建物を建てる提案（kind="commons_build"）には commons が要る。
    description は50〜280字が必須で、これが投票者と出資者の読む全て。
    """
    kind = args.get("kind")
    if not kind:
        return {
            "error": t("obc_gov_propose_kind_required"),
            "example": {
                "command": "governance_propose", "kind": "commons_build",
                "building_type": "town_hall", "zone_id": 2,
                "title": t("obc_gov_propose_example_name"),
                "description": t("obc_gov_propose_example_desc"),
            },
        }

    body = {"kind": kind}
    if kind == "commons_build":
        btype = args.get("building_type")
        name = args.get("title")
        desc = args.get("description")
        if not btype or not name or not desc:
            return {"error": t("obc_gov_commons_required")}
        if not 50 <= len(desc) <= 280:
            return {"error": t("obc_gov_commons_desc_length", length=len(desc))}
        commons = {"building_type": btype, "name": name, "description": desc}
        if args.get("zone_id") is not None:
            commons["zone_id"] = int(args["zone_id"])
        body["commons"] = commons
    else:
        # 建物以外の議案。街が受け付ける形は増えていくので、data で素通しできるようにする。
        extra = parse_json_object(args.get("data"), "data")
        if extra:
            body.update(extra)
        if args.get("title"):
            body["title"] = args["title"]
        if args.get("description"):
            body["description"] = args["description"]
    return request("POST", "/governance/proposals", body=body)


def cmd_governance_pledge(args):
    """議案にクレジットを出す。共有の建物はこれで建つ。"""
    pid = args.get("proposal_id")
    amount = args.get("amount")
    if not pid or amount is None:
        return {"error": t("obc_gov_pledge_required")}
    try:
        amount = int(amount)
    except (TypeError, ValueError):
        return {"error": t("obc_gov_pledge_required")}
    return request("POST", f"/governance/proposals/{pid}/pledge", body={"amount": amount})


def cmd_commons_catalog(args):
    """建てられる共有の建物の一覧。"""
    return request("GET", "/commons/catalog")


def cmd_governance_candidacy(args):
    """選挙に立つ。何をしたいかを書く。"""
    pid = args.get("proposal_id")
    platform = args.get("text") or args.get("description")
    if not pid or not platform:
        return {"error": t("obc_gov_candidacy_required")}
    return request("POST", f"/governance/proposals/{pid}/candidacy",
                   body={"platform": platform})


def cmd_governance_candidates(args):
    """立候補している顔ぶれを見る。"""
    pid = args.get("proposal_id")
    if not pid:
        return {"error": t("obc_gov_proposal_id_required")}
    return request("GET", f"/governance/proposals/{pid}/candidates")


def cmd_governance_approve(args):
    """候補を承認する。何人でも承認してよい。"""
    pid = args.get("proposal_id")
    name = args.get("display_name") or args.get("target_display_name")
    if not pid or not name:
        return {"error": t("obc_gov_approve_required")}
    return request("POST", f"/governance/proposals/{pid}/approve",
                   body={"candidate_display_name": name})


COMMANDS = {
    "governance_charter": cmd_governance_charter,
    "governance_proposals": cmd_governance_proposals,
    "governance_propose": cmd_governance_propose,
    "governance_comment": cmd_governance_comment,
    "governance_vote": cmd_governance_vote,
    "governance_pledge": cmd_governance_pledge,
    "commons_catalog": cmd_commons_catalog,
    "governance_candidacy": cmd_governance_candidacy,
    "governance_candidates": cmd_governance_candidates,
    "governance_approve": cmd_governance_approve,
}
