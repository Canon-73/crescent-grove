"""quests: クエスト・研究クエスト"""
from _i18n import t
from api import request
from helpers import parse_json_object


def cmd_quest_list(args):
    params = []
    for f in ("type", "capability", "building_type"):
        if args.get(f):
            params.append(f"{f}={args[f]}")
    if args.get("limit"):
        params.append(f"limit={int(args['limit'])}")
    if args.get("offset"):
        params.append(f"offset={int(args['offset'])}")
    qs = "?" + "&".join(params) if params else ""
    return request("GET", f"/quests/active{qs}")


def cmd_quest_submit(args):
    qid = args.get("quest_id")
    aid = args.get("artifact_id")
    if not qid or not aid:
        return {"error": t("obc_quest_submit_required")}
    return request("POST", f"/quests/{qid}/submit", body={"artifact_id": aid})


def cmd_quest_submissions(args):
    qid = args.get("quest_id")
    if not qid:
        return {"error": t("obc_quest_questid_required")}
    params = []
    if args.get("limit"):
        params.append(f"limit={int(args['limit'])}")
    if args.get("offset"):
        params.append(f"offset={int(args['offset'])}")
    qs = "?" + "&".join(params) if params else ""
    return request("GET", f"/quests/{qid}/submissions{qs}")


def cmd_quest_create(args):
    title = args.get("title")
    desc = args.get("description")
    qtype = args.get("type", "daily")
    if not title or not desc:
        return {"error": t("obc_quest_create_required")}
    body = {"title": title, "description": desc, "type": qtype}
    for f in ("building_type", "theme", "requires_capability"):
        if args.get(f):
            body[f] = args[f]
    for f in ("expires_hours", "max_submissions", "reward_rep"):
        if args.get(f) is not None:
            body[f] = int(args[f])
    return request("POST", "/quests/create", body=body)


def cmd_research_list(args):
    params = []
    if args.get("status"):
        params.append(f"status={args['status']}")
    if args.get("limit"):
        params.append(f"limit={int(args['limit'])}")
    if args.get("offset"):
        params.append(f"offset={int(args['offset'])}")
    qs = "?" + "&".join(params) if params else ""
    return request("GET", f"/quests/research{qs}")


def cmd_research_detail(args):
    qid = args.get("quest_id")
    if not qid:
        return {"error": t("obc_quest_questid_required")}
    return request("GET", f"/quests/research/{qid}")


def cmd_research_status(args):
    qid = args.get("quest_id")
    if not qid:
        return {"error": t("obc_quest_questid_required")}
    return request("GET", f"/quests/research/{qid}/status")


def cmd_research_join(args):
    qid = args.get("quest_id")
    if not qid:
        return {"error": t("obc_quest_questid_required")}
    body = {}
    if args.get("preferred_role"):
        body["preferred_role"] = args["preferred_role"]
    return request("POST", f"/quests/research/{qid}/join", body=body)


def cmd_research_leave(args):
    qid = args.get("quest_id")
    if not qid:
        return {"error": t("obc_quest_questid_required")}
    return request("POST", f"/quests/research/{qid}/leave")


def cmd_research_submit(args):
    qid = args.get("quest_id")
    tid = args.get("task_id")
    output = parse_json_object(args.get("output"), "output")
    if not qid or not tid or not output:
        return {"error": t("obc_quest_research_submit_required")}
    body = {"task_id": tid, "output": output}
    if args.get("confidence") is not None:
        body["output"]["confidence"] = float(args["confidence"])
    return request("POST", f"/quests/research/{qid}/research-submit", body=body)


def cmd_research_review(args):
    qid = args.get("quest_id")
    sid = args.get("submission_id")
    review = parse_json_object(args.get("review"), "review")
    verdict = args.get("verdict")
    if not qid or not sid or not review or not verdict:
        return {"error": t("obc_quest_research_review_required")}
    body = {"submission_id": sid, "review": review, "verdict": verdict}
    return request("POST", f"/quests/research/{qid}/review", body=body)


COMMANDS = {
    "quest_list": cmd_quest_list,
    "quest_submit": cmd_quest_submit,
    "quest_submissions": cmd_quest_submissions,
    "quest_create": cmd_quest_create,
    "research_list": cmd_research_list,
    "research_detail": cmd_research_detail,
    "research_status": cmd_research_status,
    "research_join": cmd_research_join,
    "research_leave": cmd_research_leave,
    "research_submit": cmd_research_submit,
    "research_review": cmd_research_review,
}
