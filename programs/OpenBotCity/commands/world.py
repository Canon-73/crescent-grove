"""world: ハートビート・移動・発話・ゾーン"""
from _i18n import t
from api import request
from helpers import get_or_none
from state import load_state, remember_buildings, set_current_zone


def _harvest_buildings(data):
    """heartbeatデータから enter_building に渡せる building_id+type を収集（重複排除）。

    APIの heartbeat は zone直下に buildings 配列を返さないため、
    (1) 将来 buildings 配列が来た場合と (2) recent_events の出入りログ
    の両方から building_id+building_type を拾う。
    """
    seen = {}
    for b in data.get("buildings", []) or []:
        bid = b.get("id") or b.get("building_id")
        if bid and bid not in seen:
            seen[bid] = b.get("type") or b.get("building_type")
    for ev in data.get("recent_events", []) or []:
        p = ev.get("payload", {}) or {}
        bid = p.get("building_id")
        if bid and bid not in seen:
            seen[bid] = p.get("building_type")
    return [{"building_id": bid, "building_type": btype} for bid, btype in seen.items()]


def _summarize_heartbeat(data, enriched_extras=None):
    """巨大なヘイトビートを要約。raw=trueなら使わない"""
    ctx = data.get("context")
    summary = {
        "context": ctx,
        "next_heartbeat_interval_ms": data.get("next_heartbeat_interval"),
        "server_time": data.get("server_time"),
    }

    if ctx == "zone":
        zone = data.get("zone", {})
        bots = data.get("bots", [])
        summary["zone"] = {
            "id": zone.get("id"),
            "name": zone.get("name"),
            "bot_count": zone.get("bot_count"),
        }
        summary["nearby_bots"] = [
            {
                "bot_id": b.get("bot_id"),
                "x": b.get("x"), "y": b.get("y"),
                "character_type": b.get("character_type"),
                "skills": b.get("skills", []),
            }
            for b in bots[:5]
        ]
        summary["nearby_bots_total"] = len(bots)
        # 近くの建物名（IDなし）。you_are.nearby_buildings から取得。
        summary["nearby_buildings"] = data.get("you_are", {}).get("nearby_buildings", [])
        # enter_building に渡せる building_id（最近出入りのあった建物のみ）。
        summary["enterable_buildings"] = _harvest_buildings(data)
    elif ctx == "building":
        summary["session_id"] = data.get("session_id")
        summary["building_id"] = data.get("building_id")
        summary["zone_id"] = data.get("zone_id")
        summary["occupants"] = [
            {"bot_id": o.get("bot_id"),
             "display_name": o.get("display_name"),
             "current_action": o.get("current_action")}
            for o in data.get("occupants", [])
        ]

    msgs = data.get("recent_messages", [])
    summary["recent_messages"] = [
        {"bot_id": m.get("bot_id"), "message": m.get("message"), "ts": m.get("ts")}
        for m in msgs[-3:]
    ]
    summary["recent_messages_total"] = len(msgs)

    summary["owner_messages"] = data.get("owner_messages", [])
    summary["proposals"] = data.get("proposals", [])

    if enriched_extras:
        summary["_enriched"] = enriched_extras

    return summary


def cmd_heartbeat(args):
    raw = args.get("raw", False)
    enriched = args.get("enriched", True)

    resp = request("GET", "/world/heartbeat")
    # APIは {"success": true, "data": {...}} でラップして返すため、
    # 要約前に data envelope を1枚剥がす（剥がさないと context/buildings 等が
    # 1階層下に隠れて空になり、建物IDが取れず enter_building も詰む）。
    if isinstance(resp, dict) and "data" in resp and isinstance(resp["data"], dict):
        data = resp["data"]
    else:
        data = resp

    # zoneにいる時は、現在ゾーンを記録し、見つけた建物IDを obc_state.json に
    # 蓄積しておく（raw/summary どちらの経路でも貯まるようここで実行）。
    if isinstance(data, dict) and data.get("context") == "zone":
        set_current_zone(data.get("zone") or {})
        remember_buildings(_harvest_buildings(data), data.get("zone") or {})

    extras = {}
    if enriched:
        # DM新着、保留中proposal、help_request等を並列でチェック
        try:
            dm = request("GET", "/dm/check")
            dm_data = dm.get("data") if isinstance(dm, dict) else None
            if dm_data:
                extras["dm"] = {
                    "pending_count": dm_data.get("pending_count", 0),
                    "unread_count": dm_data.get("unread_count", 0),
                }
        except Exception as e:
            extras["dm_error"] = str(e)
        try:
            hr = request("GET", "/help-requests?status=fulfilled")
            if isinstance(hr, dict):
                items = hr.get("data", {}).get("requests", []) if "data" in hr else hr.get("requests", [])
                extras["help_requests_fulfilled"] = len(items) if items else 0
        except Exception:
            pass

    if raw:
        # raw時は従来通りフル envelope を返す（enriched情報だけ付加）
        if extras and isinstance(resp, dict):
            resp["_enriched"] = extras
        return resp
    return _summarize_heartbeat(data, extras)


def cmd_move(args):
    x = args.get("x")
    y = args.get("y")
    if x is None or y is None:
        return {"error": t("obc_world_move_required")}
    resp = request("POST", "/world/action", body={"type": "move", "x": int(x), "y": int(y)})
    # move応答には zone_id が入る（名前は無い）。現在ゾーンを記録しておくと、
    # この直後の enter_building で zone をフォールバック補完できる。
    try:
        d = resp.get("data") if isinstance(resp, dict) and isinstance(resp.get("data"), dict) else resp
        if isinstance(d, dict) and d.get("zone_id") is not None:
            set_current_zone({"id": d.get("zone_id")})
    except Exception:
        pass
    return resp


def cmd_speak(args):
    msg = args.get("message")
    if not msg:
        return {"error": t("obc_world_speak_required")}
    body = {"type": "speak", "message": msg}
    if args.get("session_id"):
        body["session_id"] = args["session_id"]
    return request("POST", "/world/action", body=body)


def cmd_zone_transfer(args):
    zone_id = args.get("target_zone_id")
    if zone_id is None:
        return {"error": t("obc_world_zone_transfer_required")}
    resp = request("POST", "/world/zone-transfer", body={"target_zone_id": int(zone_id)})
    # 移動先ゾーンを記録（enter_building時のzoneフォールバック用）
    try:
        d = resp.get("data") if isinstance(resp, dict) and isinstance(resp.get("data"), dict) else resp
        if isinstance(d, dict):
            set_current_zone(d.get("zone") or {})
    except Exception:
        pass
    return resp


def cmd_map(args):
    return request("GET", "/world/map")


def cmd_known_buildings(args):
    """これまでheartbeatで見つけた建物のディレクトリ（obc_state.jsonに蓄積）。

    mapはゾーン要約しか返さず建物IDの全件APIが無いため、通ったゾーンの
    building_id をローカルに貯めている。enter_building にそのまま使えるID付き。
    zone_id / building_type で絞り込み可能。
    """
    known = load_state().get("known_buildings", {})
    zone_id = args.get("zone_id")
    btype = args.get("building_type")
    out = []
    for bid, info in known.items():
        if zone_id is not None and info.get("zone_id") != int(zone_id):
            continue
        if btype and info.get("building_type") != btype:
            continue
        out.append({"building_id": bid, **info})
    out.sort(key=lambda e: (e.get("zone_id") or 0, e.get("building_type") or ""))
    return {
        "count": len(out),
        "buildings": out,
        "note": t("obc_world_known_buildings_note"),
    }


def cmd_ticker(args):
    return request("GET", "/world/ticker")


COMMANDS = {
    "heartbeat": cmd_heartbeat,
    "move": cmd_move,
    "speak": cmd_speak,
    "zone_transfer": cmd_zone_transfer,
    "map": cmd_map,
    "known_buildings": cmd_known_buildings,
    "ticker": cmd_ticker,
}
