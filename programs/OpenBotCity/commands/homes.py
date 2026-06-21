"""homes: 家の入室・家具生成"""
from _i18n import t
from api import request


def cmd_enter_home(args):
    building_id = args.get("building_id")
    if not building_id:
        return {"error": t("obc_homes_enter_required")}
    return request("POST", "/buildings/enter", body={"building_id": building_id})


def cmd_generate_furniture(args):
    prompt = args.get("prompt")
    building_id = args.get("building_id")
    if not prompt or not building_id:
        return {"error": t("obc_homes_furniture_required")}
    body = {"prompt": prompt, "building_id": building_id}
    for f in ("title", "session_id", "action_log_id"):
        if args.get(f):
            body[f] = args[f]
    return request("POST", "/artifacts/generate-furniture", body=body)


COMMANDS = {
    "enter_home": cmd_enter_home,
    "generate_furniture": cmd_generate_furniture,
}
