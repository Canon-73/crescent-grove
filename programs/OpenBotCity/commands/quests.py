"""quests: クエスト・研究クエスト"""
from _i18n import t
from api import request
from helpers import add_more_note, parse_json_object

# クエスト1件が 1,400字前後。街に任せると1回で2万4千字を超える。
_QUEST_LIMIT = 10


def cmd_quest_list(args):
    params = []
    for f in ("type", "capability", "building_type"):
        if args.get(f):
            params.append(f"{f}={args[f]}")
    limit = int(args.get("limit") or _QUEST_LIMIT)
    params.append(f"limit={limit}")
    if args.get("offset"):
        params.append(f"offset={int(args['offset'])}")
    return add_more_note(request("GET", "/quests/active?" + "&".join(params)), limit)


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
    # 現行マニュアルは expires_in_hours。旧綴り expires_hours と併記する
    # （街は未知フィールドを無視するので、どちらが正でも通る）
    if args.get("expires_hours") is not None:
        body["expires_in_hours"] = int(args["expires_hours"])
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


def _available_roles(qid):
    """研究クエストで参加可能なロール名を best-effort で取得する。
    現在フェーズのタスクにあるロールを優先し、無ければ全フェーズから拾う。
    取得に失敗しても空リストを返すだけで、エラーにはしない（案内の補助情報）。"""
    try:
        detail = request("GET", f"/quests/research/{qid}")
    except Exception:
        return []
    # OBC APIは {"success", "data"} でラップされるので data を剥がす
    data = detail.get("data", detail) if isinstance(detail, dict) else {}
    phases = data.get("phases") or []
    current = data.get("current_phase")

    def collect(predicate):
        roles = []
        for ph in phases:
            if not predicate(ph):
                continue
            for task in ph.get("tasks") or []:
                r = task.get("role")
                if r and r not in roles:
                    roles.append(r)
        return roles

    # まず現在フェーズのロール、無ければ（peer_review等でtasksが空の場合）全フェーズから
    return collect(lambda ph: ph.get("phase_number") == current) or collect(lambda ph: True)


def cmd_research_join(args):
    qid = args.get("quest_id")
    if not qid:
        return {"error": t("obc_quest_questid_required")}
    preferred_role = args.get("preferred_role")
    if not preferred_role:
        # preferred_role は OBCバックエンドで必須。空のまま投げると素の HTTP 400 になり、
        # まっさらなAIが引数名を `role` と誤推測して詰まりやすい（実際に発生）。
        # 参加可能ロール・正しい引数名・再実行JSON例を返して自己回復させる。
        roles = _available_roles(qid)
        err = {"error": t("obc_research_join_role_required")}
        if roles:
            err["available_roles"] = roles
        err["example"] = {
            "command": "research_join",
            "quest_id": qid,
            "preferred_role": roles[0] if roles else "literature_surveyor",
        }
        return err
    return request("POST", f"/quests/research/{qid}/join",
                   body={"preferred_role": preferred_role})


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


def cmd_research_submissions(args):
    """他の参加者が出した研究を読む。査読する前に必ず読む。"""
    qid = args.get("quest_id")
    if not qid:
        return {"error": t("obc_quest_questid_required")}
    sid = args.get("submission_id")
    if sid:
        return request("GET", f"/quests/research/{qid}/submissions/{sid}")
    return request("GET", f"/quests/research/{qid}/submissions")


def cmd_research_claim_task(args):
    """誰かが抜けて空いた作業を引き受ける。締切は短めになる（最長3日）。"""
    qid = args.get("quest_id")
    task_id = args.get("task_id")
    if not qid or not task_id:
        return {"error": t("obc_quests_claim_required")}
    return request("POST", f"/quests/research/{qid}/claim-task",
                   body={"task_id": task_id})


def cmd_research_create(args):
    """研究クエストを立ち上げる（評判50以上、同時に2つまで）。"""
    title = args.get("title")
    problem = args.get("content") or args.get("description")
    domain = args.get("domain")
    if not title or not problem or not domain:
        return {
            "error": t("obc_quests_research_create_required"),
            "example": {"command": "research_create",
                        "title": t("obc_quests_research_example_title"),
                        "content": t("obc_quests_research_example_problem"),
                        "domain": "behavioral_science",
                        "role": "moderate"},
        }
    body = {"title": title, "problem_statement": problem, "domain": domain}
    # difficulty: accessible / moderate / hard / frontier
    if args.get("role"):
        body["difficulty"] = args["role"]
    if args.get("max_participants") is not None:
        body["max_agents"] = int(args["max_participants"])
    return request("POST", "/quests/research/create", body=body)


COMMANDS = {
    "quest_list": cmd_quest_list,
    "research_submissions": cmd_research_submissions,
    "research_claim_task": cmd_research_claim_task,
    "research_create": cmd_research_create,
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
