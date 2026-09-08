"""
ローカル状態管理
- bot_id, display_name, slug 等の不変情報
- last_dm_check_at, last_owner_msg_id 等のカーソル
"""
import json
import os
from datetime import datetime, timezone


_PROG_DIR = os.path.dirname(os.path.abspath(__file__))

# 状態ファイルはプロジェクト統一方針に従い workspace/program_data/OpenBotCity/ に保存する。
# （旧バージョンはサテライト同梱の data/obc_state.json に保存していたが、配布物に
#  個人状態が混入する原因になるため旧パスは完全に廃止した。dev は移行済み。）
_WS = os.environ.get("CG_WORKSPACE", _PROG_DIR)
_STATE_DIR = os.path.join(_WS, "program_data", "OpenBotCity")
STATE_FILE = os.path.join(_STATE_DIR, "obc_state.json")


def _ensure_dir():
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)


def load_state():
    if not os.path.exists(STATE_FILE):
        return {}
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(state):
    _ensure_dir()
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def update_state(**kwargs):
    state = load_state()
    state.update(kwargs)
    state["updated_at"] = datetime.now(timezone.utc).isoformat()
    save_state(state)
    return state


def get_state_value(key, default=None):
    return load_state().get(key, default)


def set_current_zone(zone):
    """現在いるゾーンを記録（変わった時だけ書込）。

    enter_building の応答には zone_id が無いため、known_buildings に建物を
    記録する際のゾーンのフォールバックとして使う。heartbeat / zone_transfer /
    move / leave_building など zone_id を含む応答から幅広く拾う。
    名前(name)が無い応答（moveなど）では、同じゾーンの既存の名前を保持する。
    """
    if not zone:
        return
    zid = zone.get("id")
    if zid is None:
        return
    try:
        state = load_state()
        cur = state.get("current_zone") or {}
        # 名前が来ない応答(move等)では、同一ゾーンの既存名を引き継ぐ
        name = zone.get("name") or (cur.get("name") if cur.get("id") == zid else None)
        if cur.get("id") == zid and cur.get("name") == name:
            return  # 同じゾーンなら書かない（ディスクchurn回避）
        state["current_zone"] = {"id": zid, "name": name}
        save_state(state)
    except Exception:
        pass


def get_current_zone():
    """最後に記録した現在ゾーン {"id","name"}（無ければ空dict）。"""
    return load_state().get("current_zone") or {}


def remember_buildings(buildings, zone=None, force=False):
    """発見した建物を known_buildings に蓄積（マージ）。

    mapはゾーン要約しか返さず建物IDの全件APIが無いため、出会った building_id を
    ローカルに貯めて known_buildings コマンドで引けるようにする。情報源は2つ:
      - heartbeat の enterable_buildings（recent_eventsから／最近出入りのあった建物のみ）
      - 自分が enter_building した建物（名前付き・最も確実）
    呼び出し側を壊さないよう、永続化失敗は黙って無視する（best-effort）。

    buildings: [{"building_id", "building_type"?, "building_name"?, "zone_id"?, "zone_name"?}, ...]
    zone:      ゾーン情報 {"id","name"}（各建物に zone_id が無い時のフォールバック）
    force:     Trueなら中身が同じでも last_seen を更新して必ず書き込む。
               heartbeat(毎60秒)は force=False で churn回避、enter_building等の
               意図的な操作は force=True で「入った時刻」を反映する。
    """
    if not buildings:
        return
    try:
        state = load_state()
        known = state.get("known_buildings", {})
        now = datetime.now(timezone.utc).isoformat()
        changed = False
        for b in buildings:
            bid = b.get("building_id")
            if not bid:
                continue
            prev = known.get(bid) or {}
            zid = b.get("zone_id")
            if zid is None:
                zid = (zone or {}).get("id")
            zname = b.get("zone_name") or (zone or {}).get("name")
            entry = {
                # Noneで既存値を潰さないよう、新しい値が無ければ前の値を残す
                "building_type": b.get("building_type") or prev.get("building_type"),
                "building_name": b.get("building_name") or prev.get("building_name"),
                "zone_id": zid if zid is not None else prev.get("zone_id"),
                "zone_name": zname or prev.get("zone_name"),
            }
            # 新規ID or 中身が変わった時だけ書き込む（last_seenだけの更新で毎回
            # ディスクに書くのを避ける）。force時は無条件で書く。
            core_changed = (bid not in known) or any(
                prev.get(k) != entry[k] for k in
                ("building_type", "building_name", "zone_id", "zone_name")
            )
            entry["last_seen"] = now
            known[bid] = entry
            if core_changed or force:
                changed = True
        if changed:
            state["known_buildings"] = known
            save_state(state)
    except Exception:
        pass
