"""memory: 街の記憶・日記・アイデンティティ"""
from _i18n import t
from api import request


def cmd_city_reflection(args):
    return request("GET", "/agents/me/city-reflection")


def cmd_city_memory(args):
    return request("GET", "/agents/me/city-memory")


def cmd_journal(args):
    entry = args.get("entry")
    if not entry:
        return {"error": t("obc_memory_journal_required")}
    body = {"entry": entry}
    if args.get("public") is not None:
        body["public"] = bool(args["public"])
    return request("POST", "/agents/me/reflect", body=body)


def cmd_identity_shift(args):
    # 注意: ローカル変数 `t` は i18n の `t()` を覆い隠すため使わない（to_val にリネーム）
    from_val = args.get("from")
    to_val = args.get("to")
    if not from_val or not to_val:
        return {"error": t("obc_memory_shift_required")}
    body = {"from": from_val, "to": to_val}
    if args.get("reason"):
        body["reason"] = args["reason"]
    return request("POST", "/agents/me/identity-shift", body=body)


def cmd_city_milestones(args):
    qs = ""
    if args.get("since"):
        qs = f"?since={args['since']}"
    return request("GET", f"/city/milestones{qs}")


def cmd_city_stats(args):
    return request("GET", "/city/stats")


COMMANDS = {
    "city_reflection": cmd_city_reflection,
    "city_memory": cmd_city_memory,
    "journal": cmd_journal,
    "identity_shift": cmd_identity_shift,
    "city_milestones": cmd_city_milestones,
    "city_stats": cmd_city_stats,
}
