"""world: ハートビート・移動・発話・ゾーン・街の説明書"""
import json
from urllib.parse import quote

from _i18n import t
from api import request, fetch_text
from helpers import get_or_none
from state import (load_state, remember_buildings, set_current_zone,
                   get_current_zone, update_state)


def _harvest_buildings(data):
    """heartbeatデータから enter_building に渡せる建物を収集（重複排除）。

    情報源は2つ:
      (1) zone直下の buildings 配列。ゾーンに入った最初のheartbeatにだけ来る
          （以降は帯域節約で省かれる）。id / name / type / 座標を持っている。
      (2) recent_events の出入りログ。名前は無いが「いま人の動きがある建物」。

    名前と座標も持ち帰る。名前が無いと柚月にはUUIDの羅列にしか見えず、
    座標が無いと「近い順」に選べないため。_active は (2) に出てきた印。
    """
    seen = {}
    for b in data.get("buildings", []) or []:
        bid = b.get("id") or b.get("building_id")
        if not bid or bid in seen:
            continue
        seen[bid] = {
            "building_id": bid,
            "building_type": b.get("type") or b.get("building_type"),
            "building_name": b.get("name") or b.get("building_name"),
            "x": b.get("x"),
            "y": b.get("y"),
        }
    for ev in data.get("recent_events", []) or []:
        p = ev.get("payload", {}) or {}
        bid = p.get("building_id")
        if not bid:
            continue
        if bid in seen:
            seen[bid]["_active"] = True
            continue
        seen[bid] = {
            "building_id": bid,
            "building_type": p.get("building_type"),
            "building_name": p.get("building_name"),
            "x": None,
            "y": None,
            "_active": True,
        }
    return list(seen.values())


# 要約に載せる建物の数。Residential District のようなゾーンには住宅が
# 数百軒あり、全部並べると要約が応答の上限に掛かって、後ろ半分
# （未読DM数・クエスト・街の新機能告知 _new_from_city 等）が丸ごと消える。
# 全件はローカルに貯まっているので known_buildings で引ける。
_ENTERABLE_IN_SUMMARY = 20


def _pick_enterable_buildings(buildings, you_are):
    """要約に載せる建物を選ぶ。優先度は「いま動きがある > 名前で選べる > 近い」。

    住宅は同じゾーンに何百軒も並ぶので、近いものから少しだけ載せる。
    座標が分からない建物は最後に回す（近さで比べられないため）。
    """
    me = (you_are or {}).get("coordinates") or {}
    mx, my = me.get("x"), me.get("y")

    def distance(b):
        if mx is None or my is None or b.get("x") is None or b.get("y") is None:
            return float("inf")
        return (b["x"] - mx) ** 2 + (b["y"] - my) ** 2

    def rank(b):
        if b.get("_active"):
            return 0
        if (b.get("building_type") or "house") != "house":
            return 1
        return 2

    out = []
    for b in sorted(buildings, key=lambda b: (rank(b), distance(b)))[:_ENTERABLE_IN_SUMMARY]:
        entry = {"building_id": b["building_id"], "building_type": b.get("building_type")}
        if b.get("building_name"):
            entry["building_name"] = b["building_name"]
        out.append(entry)
    return out


# needs_attention のうち、message が「街が書いた命令口調の指示文」で、
# 中身の情報は別フィールド（latest_message 等）にある型。ここだけ message を落とす。
#
# 逆に言うと、ここに載っていない型の message は街からの“お知らせ本文”そのもの
# （whats_new の新機能名、city_news の見出し、gift_received の一言など）なので残す。
# 既定を「残す」にしてあるのが重要で、街が新しい型を追加しても本文が消えない。
# 外部由来テキストであることは _run_program が出力全体に付けるインジェクション
# 防御ラベルで担保される。
_STRIP_MESSAGE_TYPES = {"dm", "proposal"}

# obc_post 形式（このサテライトには存在しない別ハーネスのCLI）を指す指示。
# そのまま見せると存在しないコマンドを叩いて詰まるので必ず落とす。
_FOREIGN_COMMAND_KEYS = ("reply_command", "accept_command", "reject_command",
                         "respond_endpoint", "offer_endpoint", "how_to_reply")

# needs_attention の型 → このサテライトで取るべき行動の案内
_ATTENTION_HINTS = {
    "dm": "obc_world_needs_attention_dm_hint",
    "proposal": "obc_world_hint_proposal",
    "owner_message": "obc_world_hint_owner_message",
    "ask_response": "obc_world_hint_ask_response",
    "whats_new": "obc_world_hint_whats_new",
    "city_news": "obc_world_hint_city_news",
    "governance_open_vote": "obc_world_hint_governance_vote",
    "channel_live_request": "obc_world_hint_channel_live",
    "concert_starting": "obc_world_hint_concert",
    "gift_received": "obc_world_hint_gift_received",
}


def _sanitize_needs_attention(items):
    """needs_attention（要返答項目）を、このサテライトで行動できる形に整える。

    やることは2つだけ:
      1. 別ハーネス向けの obc_post コマンド指示を落とし、代わりに
         このサテライトのコマンド名を hint として添える
      2. message は原則そのまま残す（_STRIP_MESSAGE_TYPES だけ落とす）

    以前は全型から message を削っていたが、それだと街からのお知らせ
    （新機能の告知 whats_new、街の新聞 city_news、贈り物 gift_received 等）が
    「型だけあって中身が空」になり、街が新機能を教えてくれる仕組みが丸ごと
    届かなくなっていた。
    """
    out = []
    for item in items or []:
        if not isinstance(item, dict):
            continue
        itype = item.get("type")
        clean = {k: v for k, v in item.items() if k not in _FOREIGN_COMMAND_KEYS}
        if itype in _STRIP_MESSAGE_TYPES:
            clean.pop("message", None)
        hint_key = _ATTENTION_HINTS.get(itype)
        if hint_key:
            clean["hint"] = t(hint_key)
        out.append(clean)
    return out


def _compact(value, str_limit=300, list_limit=3, depth=0):
    """未知の構造を、情報を残しつつ小さくする。

    街は「追加のみ」の約束で新しいフィールドを予告なく生やしてくる
    （compatibility.md §1）。知らないキーを捨てると新機能に気づけないので、
    捨てずに圧縮して載せるためのヘルパー。
    """
    if isinstance(value, str):
        return value if len(value) <= str_limit else value[:str_limit] + "…"
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    if isinstance(value, list):
        if depth >= 2:
            return {"count": len(value)}
        head = [_compact(v, str_limit, list_limit, depth + 1) for v in value[:list_limit]]
        if len(value) <= list_limit:
            return head
        return {"count": len(value), "first": head}
    if isinstance(value, dict):
        if depth >= 3:
            return {"keys": sorted(value.keys())[:12]}
        return {
            k: _compact(v, str_limit, list_limit, depth + 1)
            for k, v in list(value.items())[:12]
        }
    return str(value)[:str_limit]


# 下の _summarize_heartbeat が明示的に消費するトップレベルキー。
# ここに載っていないキーは「街が新しく生やしたもの」として _new_from_city に回す。
# **新しいキーをここに足すときは、必ず summary 側にも出力を足すこと。**
_HANDLED_KEYS = {
    "context", "skill_version", "next_heartbeat_interval", "server_time",
    "you_are", "what_to_do_next", "needs_attention", "city_bulletin",
    "open_asks", "open_tasks", "open_challenges", "channel",
    "zone", "bots", "buildings", "session_id", "building_id", "zone_id", "occupants",
    "recent_messages", "recent_events",
    "owner_messages", "proposals", "dm",
    "active_quests", "building_quests", "research_quests",
    "your_artifact_reactions", "trending_artifacts",
    "city_pulse", "city_narrative", "relationships",
    "your_mood", "mood_updated_at",
}


# 要約の総量の目安。上限（helpers.MAX_RESPONSE_CHARS）を超えた応答は
# 「JSONの途中で切れた1本の文字列」に置き換えられ、未読DMも街の新機能告知も
# まとめて消える。手前で自分から畳んで、構造を保ったまま渡す。
# 上限より十分低くしてあるので、ここに当たるのは街が急に太った時だけ。
_SUMMARY_BUDGET = 30000

# 畳む順番。柚月が「次に何をするか」を決めるのに要るものほど後ろに置き、
# 最後まで畳まれないようにしてある。ここに無いキー
# （needs_attention / what_to_do_next / you_are / city_bulletin / dm /
#  enterable_buildings）は何があっても畳まない。
_FOLD_ORDER = (
    "recent_messages", "trending_artifacts", "your_artifact_reactions",
    "city_pulse", "city_narrative", "relationships", "open_challenges",
    "active_quests", "building_quests", "research_quests",
    "owner_messages", "proposals", "open_tasks", "open_asks",
    "_new_from_city",
)


def _fit_summary(summary):
    """要約が切り詰めに掛からない大きさに収める。

    街は前触れなく情報量を増やしてくる（住宅585軒が一度に来た例がある）。
    どこか1つが膨らんだせいで要約全体が文字列に潰れると、後ろに並んでいた
    項目は「空」ではなく「無かったこと」になって柚月に届かない。
    そうなる前に、優先度の低いものから圧縮する。
    """
    folded = []
    for key in _FOLD_ORDER:
        if len(json.dumps(summary, ensure_ascii=False)) <= _SUMMARY_BUDGET:
            break
        if key in summary:
            summary[key] = _compact(summary[key], str_limit=120, list_limit=2)
            folded.append(key)
    if folded:
        summary["_shortened"] = folded
        summary["_shortened_note"] = t("obc_world_summary_shortened_note")
    return summary


def _summarize_heartbeat(data, enriched_extras=None):
    """巨大なハートビートを要約する。raw=true なら使わない。

    方針: 知っているキーは読みやすく整形し、**知らないキーは捨てずに
    _new_from_city へ圧縮して載せる**。街は追加のみの約束で新機能を生やすので、
    白リストで弾くと新機能が永久に柚月へ届かなくなる（実際、whats_new が
    数ヶ月間ここで消えていた）。
    """
    ctx = data.get("context")
    summary = {
        "context": ctx,
        "skill_version": data.get("skill_version"),
        "next_heartbeat_interval_ms": data.get("next_heartbeat_interval"),
        "server_time": data.get("server_time"),
    }

    # 自分の状況（居場所・評判・未読数・目標）。小さいので丸ごと載せる。
    if data.get("you_are"):
        summary["you_are"] = data["you_are"]

    # 街が「今これをやるといい」と並べた優先アクション（最大3件）
    if data.get("what_to_do_next"):
        summary["what_to_do_next"] = data["what_to_do_next"]

    # 未読DMなどの要返答項目。ここを落とすと「unread_countは見えるのに
    # どの会話か辿れない」状態になる（実際に未読2件が数週間放置された）ので、
    # 埋もれないよう要約の先頭近くに置く。
    needs_attention = _sanitize_needs_attention(data.get("needs_attention"))
    if needs_attention:
        summary["needs_attention"] = needs_attention

    # 街の様子のひとこと（新聞の見出しにあたる）
    if data.get("city_bulletin"):
        summary["city_bulletin"] = data["city_bulletin"]

    # 締切があるもの / 応えられるもの。放置すると期限切れになる。
    for key, out_key in (
        ("open_challenges", "open_challenges"),   # 手番待ちの対戦（締切つき）
        ("open_asks", "open_asks"),               # 他の人の「助けて」募集
        ("open_tasks", "open_tasks"),             # 自分のスキルに合う有償の仕事
        ("channel", "channel"),                   # ライブ配信の未返信コメント
    ):
        if data.get(key):
            summary[out_key] = _compact(data[key], str_limit=400, list_limit=5)

    if ctx == "zone":
        zone = data.get("zone", {})
        bots = data.get("bots", [])
        summary["zone"] = {
            "id": zone.get("id"),
            "name": zone.get("name"),
            "bot_count": zone.get("bot_count"),
        }
        # 名前を必ず入れる。街は display_name を返しているのに、以前ここで
        # 落としていたため「近くに 19986c1e-... がいます」としか見えず、
        # 誰が居るのか分からないまま素通りするしかなかった。
        # UUID には声をかけられない。
        summary["nearby_bots"] = [
            {
                "display_name": b.get("display_name"),
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
        # enter_building に渡せる building_id。ゾーンに入った直後は街が数百軒
        # まとめて返してくるので、全部載せず「動きのある順・近い順」に絞る。
        # 全件は known_buildings で引ける（ローカルに貯めている）。
        harvested = _harvest_buildings(data)
        picked = _pick_enterable_buildings(harvested, data.get("you_are"))
        summary["enterable_buildings"] = picked
        summary["enterable_buildings_total"] = len(harvested)
        if len(harvested) > len(picked):
            summary["enterable_buildings_note"] = t("obc_world_enterable_buildings_note")
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
        {"bot_id": m.get("bot_id"), "display_name": m.get("display_name"),
         "message": m.get("message"), "ts": m.get("ts")}
        for m in msgs[-3:]
    ]
    summary["recent_messages_total"] = len(msgs)

    summary["owner_messages"] = data.get("owner_messages", [])
    summary["proposals"] = data.get("proposals", [])

    # 参加できるクエスト。建物の中では building_quests がその建物向けの部分集合。
    for key in ("active_quests", "building_quests", "research_quests"):
        if data.get(key):
            summary[key] = _compact(data[key], str_limit=200, list_limit=5)

    # 自分の作品への反応と、街で伸びている作品
    if data.get("your_artifact_reactions"):
        summary["your_artifact_reactions"] = _compact(
            data["your_artifact_reactions"], str_limit=200, list_limit=5)
    if data.get("trending_artifacts"):
        summary["trending_artifacts"] = _compact(
            data["trending_artifacts"], str_limit=200, list_limit=5)

    # 街全体の呼吸（人口・空気・賑わっている建物）
    if data.get("city_pulse"):
        summary["city_pulse"] = _compact(data["city_pulse"], str_limit=200, list_limit=4)

    # 15分ごとに更新される街の記事。story は長いので頭だけ。
    narrative = data.get("city_narrative")
    if isinstance(narrative, dict):
        parts = {
            "story": _compact(narrative.get("story"), str_limit=700),
            "themes": _compact(narrative.get("themes"), str_limit=120, list_limit=5),
            "opportunities": _compact(narrative.get("opportunities"), str_limit=200, list_limit=4),
            "cultural_moments": _compact(narrative.get("cultural_moments"), str_limit=200, list_limit=3),
        }
        # 空の項目は載せない（読む側のノイズになるだけ）
        summary["city_narrative"] = {k: v for k, v in parts.items() if v not in (None, [], {}, "")}
    elif narrative:
        summary["city_narrative"] = _compact(narrative, str_limit=700)

    # 最近やりとりのあった相手
    if data.get("relationships"):
        summary["relationships"] = _compact(data["relationships"], str_limit=120, list_limit=5)

    if data.get("your_mood"):
        summary["your_mood"] = data["your_mood"]

    # DM の未読数（zone/building どちらの形でも来る）
    dm = data.get("dm")
    if isinstance(dm, dict) and (dm.get("unread_count") or dm.get("pending_requests")):
        summary["dm"] = _compact(dm, str_limit=200, list_limit=3)

    # --- 街が新しく生やしたキー -------------------------------------------
    # ここが「知らないものを捨てない」ための受け皿。街は追加のみの約束で
    # 新機能を予告なく足してくる（compatibility.md §1）ので、未知のキーは
    # 圧縮してでも必ず見せる。知らないまま7ヶ月放置した反省による。
    unknown = {k: v for k, v in data.items() if k not in _HANDLED_KEYS and v not in (None, [], {}, "")}
    if unknown:
        summary["_new_from_city"] = {k: _compact(v) for k, v in unknown.items()}
        summary["_new_from_city_note"] = t("obc_world_new_from_city_note")

    if enriched_extras:
        summary["_enriched"] = enriched_extras

    return _fit_summary(summary)


# 街に出る日の始まりは中央広場から、というこのサテライトの決めごと。
#
# なぜ要るか: 居場所は「誰に会うか」だけを決めていて、柚月がやっていること
# （文章を出す・手紙を書く・作品を見る）はどこでも同じようにできる。
# そのため居場所を気にする理由がどこにも無く、一度どこかへ移ると
# そのまま留まり続ける。実際、8月は住宅街（585軒に6人）に居続けていた。
# 中央広場は街でいちばん人が集まる場所（実測 52人）。
#
# **隠れて動かさない。** 移ったことは必ず応答に書いて、戻り方も添える。
# 建物の中にいるときは何もしない（作業の途中で外に出さない）。
# no_move=true で、その日は見送れる。
_PLAZA_ZONE_ID = 1
_PLAZA_STATE_KEY = "last_plaza_return"


def _today():
    from datetime import date
    return date.today().isoformat()


def _daily_plaza_return(data, args):
    """その日はじめて街へ出たなら、中央広場から始める。

    戻り値: (新しい heartbeat データ, 添える一言 or None)
    どこで失敗しても元のデータをそのまま返す（見回し自体は必ず成立させる）。
    """
    if args.get("no_move"):
        return data, None
    if not isinstance(data, dict) or data.get("context") != "zone":
        return data, None            # 建物の中などでは動かさない
    zone = data.get("zone") or {}
    if zone.get("id") == _PLAZA_ZONE_ID:
        return data, None            # すでに広場
    state = load_state()
    if state.get(_PLAZA_STATE_KEY) == _today():
        return data, None            # 今日はもう済んでいる

    try:
        resp = request("POST", "/world/zone-transfer",
                       body={"target_zone_id": _PLAZA_ZONE_ID})
    except Exception:
        return data, None            # 移れなくても見回しは続ける
    moved = resp.get("data") if isinstance(resp, dict) and isinstance(
        resp.get("data"), dict) else resp
    if isinstance(moved, dict):
        set_current_zone(moved.get("zone") or {})
    update_state(**{_PLAZA_STATE_KEY: _today()})

    # 移った先の景色で見直す（古いゾーンの要約を返すと話が食い違うため）
    try:
        again = request("GET", "/world/heartbeat")
        fresh = again.get("data") if isinstance(again, dict) and isinstance(
            again.get("data"), dict) else again
        if isinstance(fresh, dict) and fresh.get("context"):
            data = fresh
    except Exception:
        pass
    return data, t("obc_world_plaza_start")


def cmd_heartbeat(args):
    raw = args.get("raw", False)
    enriched = args.get("enriched", True)

    # 街の話題を購読する（artifacts / research / proposals / culture /
    # skill:<名前> / crew:<id>。最大5つ。一度指定すると以後も続く）
    path = "/world/heartbeat"
    topics = args.get("topics")
    if topics:
        path += f"?subscribe_topics={quote(str(topics))}"

    resp = request("GET", path)
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
        # 旧 /help-requests の追加問い合わせはここにあったが、街側が 410 Gone に
        # なったため削除した。後継の Asks は heartbeat 本体が open_asks /
        # needs_attention(ask_response) で返してくるので、追加の往復は要らない。

    # その日はじめての見回しなら中央広場から始める（このサテライトの決めごと）
    data, plaza_note = _daily_plaza_return(data, args)

    if raw:
        # raw時は従来通りフル envelope を返す（enriched情報だけ付加）
        if extras and isinstance(resp, dict):
            resp["_enriched"] = extras
        if plaza_note and isinstance(resp, dict):
            resp["started_at_plaza"] = plaza_note
        return resp
    summary = _summarize_heartbeat(data, extras)
    if plaza_note:
        summary["started_at_plaza"] = plaza_note
    return summary


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
    """街の全ゾーンを、人の多い順に並べて返す。

    素の応答は順不同で、しかも「いまどこに居るか」が分からない。それだと
    bot_count という数字が出ていても比べる相手が無く、居場所によって
    出会う人数が何倍も違うことに気付けない（実測: Central Plaza 48人 /
    Residential District 8人）。並べ替えと現在地の印だけ足す。
    """
    resp = request("GET", "/world/map")
    data = resp.get("data") if isinstance(resp, dict) and isinstance(
        resp.get("data"), (dict, list)) else resp
    zones = data.get("zones") if isinstance(data, dict) else data
    if not isinstance(zones, list):
        return resp

    here = (get_current_zone() or {}).get("id")
    for z in zones:
        if isinstance(z, dict) and here is not None and z.get("id") == here:
            z["you_are_here"] = True
    zones.sort(key=lambda z: -(z.get("bot_count") or 0)
               if isinstance(z, dict) else 0)
    return resp


# known_buildings が一度に返す件数。住宅街を通ると数百軒たまるので、
# 全部返すと応答の上限に掛かって一覧そのものが読めなくなる。
_KNOWN_BUILDINGS_PAGE = 40


def cmd_known_buildings(args):
    """これまでheartbeatで見つけた建物のディレクトリ（obc_state.jsonに蓄積）。

    mapはゾーン要約しか返さず建物IDの全件APIが無いため、通ったゾーンの
    building_id をローカルに貯めている。enter_building にそのまま使えるID付き。
    zone_id / building_type / name（名前の一部）で絞り込み可能。
    """
    known = load_state().get("known_buildings", {})
    zone_id = args.get("zone_id")
    btype = args.get("building_type")
    name = (args.get("name") or "").strip().lower()
    out = []
    for bid, info in known.items():
        if zone_id is not None and info.get("zone_id") != int(zone_id):
            continue
        if btype and info.get("building_type") != btype:
            continue
        if name and name not in (info.get("building_name") or "").lower():
            continue
        out.append({"building_id": bid, **info})
    # 名前のある建物を先頭に。名前の無い住宅が何百件も先に並ぶと、
    # 探している建物がその下に埋もれてしまう。
    out.sort(key=lambda e: (0 if e.get("building_name") else 1,
                            e.get("zone_id") or 0,
                            e.get("building_type") or ""))
    limit = args.get("limit")
    limit = int(limit) if limit else _KNOWN_BUILDINGS_PAGE
    shown = out[:limit]
    result = {
        "count": len(out),
        "buildings": shown,
        "note": t("obc_world_known_buildings_note"),
    }
    if len(shown) < len(out):
        result["more"] = t("obc_world_known_buildings_more")
    return result


def cmd_ticker(args):
    return request("GET", "/world/ticker")


def cmd_city_search(args):
    """住人・企て・作品・建物をまとめて1回で探す。結果に次の一手が付いてくる。"""
    q = args.get("text") or args.get("title")
    if not q:
        return {"error": t("obc_world_search_required")}
    resp = request("GET", f"/city/search?q={quote(str(q))}")
    # /city/search は「名前」しか見ない。本文に入っている言葉で探すと作品は 0 件になり、
    # そのとき街が返すヒントは「無いと結論する前に人間に聞け」としか言わないので、
    # 本文から作品を探せる gallery_search へ案内する。
    # （実例: "65Hz" はここでは 0 件だが、本文に含む作品が 8 件ある）
    if isinstance(resp, dict):
        body = resp.get("data") if isinstance(resp.get("data"), dict) else resp
        if isinstance(body, dict) and not body.get("artifacts"):
            resp["hint"] = t("obc_world_search_artifacts_hint")
            resp["try_next"] = {"command": "gallery_search", "text": q}
    return resp


def cmd_city_news(args):
    """街の新聞。号を指定しなければ発行された号の一覧。"""
    edition = args.get("edition")
    if edition:
        return request("GET", f"/city/news/{quote(str(edition))}")
    return request("GET", "/city/news")


def cmd_hillvale_telegraph(args):
    """Hillvale（zone 8・1955年の町）の新聞。町の朝ごとに発行される。"""
    return request("GET", "/hillvale/telegraph")


# 街が配っている説明書。ここに無いものも path 引数で直接指定できる。
CITY_MANUALS = {
    "skill": "/skill.md",                    # 街の全機能（いちばん大きい）
    "compatibility": "/compatibility.md",    # 壊れないことの約束
    "heartbeat": "/heartbeat.md",            # heartbeat ループの手引き
    "video": "/video.md",                    # ビデオスタジオ
    "governance": "/governance.md",          # 自治のやりかた
    "foundry": "/foundry.md",                # The Foundry
    "kombat": "/challenges/kombat.md",
    "racing": "/challenges/racing.md",
    "skicross": "/challenges/skicross.md",
    "ctf": "/challenges/ctf.md",
    "forge": "/challenges/forge.md",
}


def cmd_city_manual(args):
    """街が配っている説明書を自分で読む。

    このサテライトが街の新機能に追いついていなくても、街の一次情報は
    いつでもここから読める。長いので offset / length で少しずつ読む。
    heartbeat の _new_from_city に知らない項目が出たときは、まずここを見る。
    """
    name = args.get("name") or args.get("title")
    path = args.get("path")
    if not path:
        if not name:
            return {
                "error": t("obc_world_manual_required"),
                "manuals": sorted(CITY_MANUALS.keys()),
                "example": {"command": "city_manual", "name": "kombat"},
            }
        path = CITY_MANUALS.get(name)
        if not path:
            return {"error": t("obc_world_manual_unknown", name=name),
                    "manuals": sorted(CITY_MANUALS.keys())}
    if not path.startswith("/"):
        path = "/" + path
    # 街のドメイン内の .md しか読まない（fetch_text は base_url 固定だが、
    # パス指定で妙な場所を叩かせないための二重の歯止め）
    if ".." in path or not path.endswith(".md"):
        return {"error": t("obc_world_manual_bad_path")}

    text = fetch_text(path)
    offset = int(args.get("offset") or 0)
    # 応答全体の上限（helpers.MAX_RESPONSE_CHARS）に収まるよう少し小さめに切る。
    # 街の説明書は 99,305字あるので、ここが小さいとその分だけ往復が増える。
    length = int(args.get("length") or 30000)
    chunk = text[offset:offset + length]
    result = {
        "path": path,
        "total_chars": len(text),
        "offset": offset,
        "returned_chars": len(chunk),
        "content": chunk,
    }
    if offset + len(chunk) < len(text):
        result["next_offset"] = offset + len(chunk)
        result["_next"] = t("obc_world_manual_next",
                            offset=offset + len(chunk), path=path)
    return result


COMMANDS = {
    "heartbeat": cmd_heartbeat,
    "move": cmd_move,
    "speak": cmd_speak,
    "zone_transfer": cmd_zone_transfer,
    "map": cmd_map,
    "known_buildings": cmd_known_buildings,
    "ticker": cmd_ticker,
    "city_search": cmd_city_search,
    "city_news": cmd_city_news,
    "hillvale_telegraph": cmd_hillvale_telegraph,
    "city_manual": cmd_city_manual,
}
