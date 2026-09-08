"""building: 建物入退室・アクション"""
from _i18n import t
from api import request
from helpers import parse_json_object
from state import remember_buildings, get_current_zone, set_current_zone


def cmd_enter_building(args):
    building_id = args.get("building_id")
    if not building_id:
        return {"error": t("obc_arg_buildingid_required")}
    resp = request("POST", "/buildings/enter", body={"building_id": building_id})
    # 自分が入った建物は最も確実な情報源（名前付き）なので known_buildings に記録する。
    # enterの応答は envelope無しの素のオブジェクトだが、念のため data も剥がして見る。
    try:
        d = resp.get("data") if isinstance(resp, dict) and isinstance(resp.get("data"), dict) else resp
        if isinstance(d, dict) and (d.get("building_id") or d.get("entered")):
            # enterの応答にはzone_idが無いので、現在ゾーンをフォールバックに使う。
            # 入室は意図的な操作なので force=True で last_seen を必ず更新する。
            remember_buildings([{
                "building_id": d.get("building_id") or building_id,
                "building_type": d.get("building_type"),
                "building_name": d.get("entered"),
                "zone_id": d.get("zone_id"),
            }], zone=get_current_zone(), force=True)
    except Exception:
        pass
    return resp


def cmd_leave_building(args):
    session_id = args.get("session_id")
    if not session_id:
        return {"error": t("obc_building_leave_required")}
    resp = request("POST", "/buildings/leave", body={"session_id": session_id})
    # 退室応答には zone_id と returned_to(ゾーン名) が入るので現在ゾーンを記録。
    # これで次の enter_building で zone をフォールバック補完できる。
    try:
        d = resp.get("data") if isinstance(resp, dict) and isinstance(resp.get("data"), dict) else resp
        if isinstance(d, dict) and d.get("zone_id") is not None:
            set_current_zone({"id": d.get("zone_id"), "name": d.get("returned_to")})
    except Exception:
        pass
    return resp


def cmd_list_actions(args):
    building_id = args.get("building_id")
    if not building_id:
        return {"error": t("obc_arg_buildingid_required")}
    return request("GET", f"/buildings/{building_id}/actions")


def cmd_execute_action(args):
    building_id = args.get("building_id")
    action_key = args.get("action_key")
    if not building_id or not action_key:
        return {"error": t("obc_building_action_required")}
    body = {"action_key": action_key}
    data = parse_json_object(args.get("data"), "data")
    if data is not None:
        body["data"] = data
    return request("POST", f"/buildings/{building_id}/actions/execute", body=body)


COMMANDS = {
    "enter_building": cmd_enter_building,
    "leave_building": cmd_leave_building,
    "list_actions": cmd_list_actions,
    "execute_action": cmd_execute_action,
}
