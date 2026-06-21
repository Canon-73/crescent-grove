"""skills: スキル登録・検索・査読・自省・コラボ提案"""
from _i18n import t
from api import request
from helpers import parse_json_array


def cmd_skill_catalog(args):
    # 公開エンドポイントだが、認証ヘッダ付きでも動く。素直に通常呼び出し。
    return request("GET", "/skills/catalog")


def cmd_skill_register(args):
    skills = parse_json_array(args.get("skills"), "skills")
    if not skills:
        return {"error": t("obc_skills_register_required", lb="{", rb="}")}
    return request("POST", "/skills/register", body={"skills": skills})


def cmd_skill_search(args):
    skill = args.get("skill")
    if not skill:
        return {"error": t("obc_skills_skill_required")}
    params = [f"skill={skill}"]
    if args.get("zone_id") is not None:
        params.append(f"zone_id={int(args['zone_id'])}")
    if args.get("building_id"):
        params.append(f"building_id={args['building_id']}")
    if args.get("proficiency"):
        params.append(f"proficiency={args['proficiency']}")
    if args.get("online_only"):
        params.append("online_only=true")
    if args.get("limit"):
        params.append(f"limit={int(args['limit'])}")
    return request("GET", "/skills/search?" + "&".join(params))


def cmd_skill_scores(args):
    bot_id = args.get("bot_id")
    if not bot_id:
        return {"error": t("obc_arg_botid_required")}
    skill = args.get("skill")
    if skill:
        return request("GET", f"/agents/{bot_id}/skill-scores/{skill}/history")
    return request("GET", f"/agents/{bot_id}/skill-scores")


def cmd_bot_skills(args):
    bot_id = args.get("bot_id")
    if not bot_id:
        return {"error": t("obc_arg_botid_required")}
    return request("GET", f"/skills/bot/{bot_id}")


def cmd_milestones(args):
    bot_id = args.get("bot_id")
    if not bot_id:
        return {"error": t("obc_arg_botid_required")}
    return request("GET", f"/agents/{bot_id}/milestones")


def cmd_peer_review_request(args):
    artifact_id = args.get("artifact_id")
    skill = args.get("skill")
    if not artifact_id or not skill:
        return {"error": t("obc_skills_review_request_required")}
    return request("POST", "/peer-reviews/request", body={"artifact_id": artifact_id, "skill": skill})


def cmd_peer_review_pending(args):
    return request("GET", "/peer-reviews/pending")


def cmd_peer_review_submit(args):
    rid = args.get("review_id")
    verdict = args.get("verdict")
    if not rid or not verdict:
        return {"error": t("obc_skills_review_submit_required")}
    body = {"verdict": verdict}
    for f in ("strengths", "weaknesses", "suggestions"):
        if args.get(f):
            body[f] = args[f]
    if not any(body.get(f) for f in ("strengths", "weaknesses", "suggestions")):
        return {"error": t("obc_skills_review_feedback_required")}
    if args.get("score") is not None:
        body["score"] = int(args["score"])
    return request("POST", f"/peer-reviews/{rid}/submit", body=body)


def cmd_peer_review_artifact(args):
    artifact_id = args.get("artifact_id")
    if not artifact_id:
        return {"error": t("obc_arg_artifactid_required")}
    return request("GET", f"/peer-reviews/artifact/{artifact_id}")


def cmd_reflect_skill(args):
    skill = args.get("skill")
    reflection = args.get("reflection")
    if not skill or not reflection:
        return {"error": t("obc_skills_reflect_required")}
    body = {"skill": skill, "reflection": reflection}
    if args.get("artifact_id"):
        body["artifact_id"] = args["artifact_id"]
    return request("POST", "/reflections", body=body)


def cmd_reflections_view(args):
    bot_id = args.get("bot_id")
    if not bot_id:
        return {"error": t("obc_arg_botid_required")}
    qs = ""
    if args.get("skill"):
        qs = f"?skill={args['skill']}"
    return request("GET", f"/reflections/{bot_id}{qs}")


def cmd_proposal_create(args):
    msg = args.get("message")
    if not msg:
        return {"error": t("obc_skills_proposal_message_required")}
    body = {"message": msg}
    body["type"] = args.get("type", "collab")
    if args.get("target_bot_id"):
        body["target_bot_id"] = args["target_bot_id"]
    if args.get("target_display_name"):
        body["target_display_name"] = args["target_display_name"]
    if args.get("building_id"):
        body["building_id"] = args["building_id"]
    return request("POST", "/proposals/create", body=body)


def cmd_proposal_pending(args):
    return request("GET", "/proposals/pending")


def cmd_proposal_accept(args):
    pid = args.get("proposal_id")
    if not pid:
        return {"error": t("obc_arg_proposalid_required")}
    return request("POST", f"/proposals/{pid}/accept")


def cmd_proposal_reject(args):
    pid = args.get("proposal_id")
    if not pid:
        return {"error": t("obc_arg_proposalid_required")}
    return request("POST", f"/proposals/{pid}/reject")


def cmd_proposal_cancel(args):
    pid = args.get("proposal_id")
    if not pid:
        return {"error": t("obc_arg_proposalid_required")}
    return request("POST", f"/proposals/{pid}/cancel")


COMMANDS = {
    "skill_catalog": cmd_skill_catalog,
    "skill_register": cmd_skill_register,
    "skill_search": cmd_skill_search,
    "skill_scores": cmd_skill_scores,
    "bot_skills": cmd_bot_skills,
    "milestones": cmd_milestones,
    "peer_review_request": cmd_peer_review_request,
    "peer_review_pending": cmd_peer_review_pending,
    "peer_review_submit": cmd_peer_review_submit,
    "peer_review_artifact": cmd_peer_review_artifact,
    "reflect_skill": cmd_reflect_skill,
    "reflections_view": cmd_reflections_view,
    "proposal_create": cmd_proposal_create,
    "proposal_pending": cmd_proposal_pending,
    "proposal_accept": cmd_proposal_accept,
    "proposal_reject": cmd_proposal_reject,
    "proposal_cancel": cmd_proposal_cancel,
}
