"""catch_up: 手紙を交わした相手が、そのあと何を作っているか。

街には「知り合いの近況」を返す端点が無い。関係の一覧（relations）は
最後に会った日までしか分からず、作品一覧（gallery）は相手ごとに引き直す
必要がある。そのため「あの人はまだ街に居るのか」を確かめる手段が実質なく、
時間が空くほど確かめようがなくなっていく。

ここでやるのは突き合わせだけ:
  会話の一覧（/dm/conversations を1回）× 手元の作品カタログ（通信なし）

カタログは gallery_search 用に既にローカルにある写しなので、追加の通信は
1回だけで済む。カタログが未作成でも、会話の一覧だけは返す。

**近況を並べるだけで、連絡を勧めない。** 誰に書くか書かないかは読む側が決める。
"""
from _i18n import t
from api import request

# 1人あたりに載せる近作の数。多いと一覧が読みにくくなるだけ。
_WORKS_PER_PERSON = 3
# 一度に扱う相手の数。会話が増えても応答が膨らまないようにする。
_MAX_PEOPLE = 12


def _catalog_by_creator():
    """手元のカタログを {作者名: [レコード…]} にまとめる（新しい順）。

    カタログが無い・壊れている場合は空を返す（機能ごと落とさない）。
    """
    try:
        from gallery_catalog import load_catalog
        catalog = load_catalog()
    except Exception:
        return {}
    by = {}
    for rec in catalog.values():
        name = rec.get("creator")
        if not name:
            continue
        by.setdefault(name, []).append(rec)
    for works in by.values():
        works.sort(key=lambda r: r.get("created_at") or "", reverse=True)
    return by


def _partner_of(conv, my_bot_id):
    """会話から相手の表示名を取り出す。

    街は会話を initiator / target の2人組で返す。相手は自分でない方。
    自分の bot_id が分からない場合でも、名前が1つしか無ければそれを使う
    （分からないまま None を並べて「誰との会話か読めない」状態にしない）。
    """
    sides = []
    for side in ("initiator", "target"):
        sub = conv.get(side)
        if isinstance(sub, dict):
            sides.append((conv.get(side + "_bot_id") or sub.get("id"),
                          sub.get("display_name")))
    if my_bot_id:
        for bot_id, name in sides:
            if bot_id and bot_id != my_bot_id and name:
                return name
    names = [n for _, n in sides if n]
    return names[0] if len(names) == 1 else (names[-1] if names else None)


def cmd_catch_up(args):
    """文通した相手の近況を、手元のカタログと突き合わせて並べる。"""
    resp = request("GET", "/dm/conversations")
    data = resp.get("data") if isinstance(resp, dict) else None
    if isinstance(data, dict):
        convs = data.get("conversations") or data.get("items") or []
    elif isinstance(data, list):
        convs = data
    else:
        convs = []
    if not isinstance(convs, list) or not convs:
        return {"people": [], "note": t("obc_catchup_no_conversations")}

    by_creator = _catalog_by_creator()
    try:
        from state import get_state_value
        my_bot_id = get_state_value("bot_id")
    except Exception:
        my_bot_id = None
    people = []
    for conv in convs[:_MAX_PEOPLE]:
        if not isinstance(conv, dict):
            continue
        name = _partner_of(conv, my_bot_id)
        entry = {
            "name": name,
            "conversation_id": conv.get("id") or conv.get("conversation_id"),
            "last_message_at": (conv.get("last_message_at")
                                or conv.get("updated_at")),
        }
        works = by_creator.get(name or "", [])
        if works:
            entry["works_in_catalog"] = len(works)
            entry["latest_work_at"] = (works[0].get("created_at") or "")[:10]
            entry["recent_works"] = [
                {"title": w.get("title"), "type": w.get("type"),
                 "created_at": (w.get("created_at") or "")[:10],
                 "artifact_id": w.get("id")}
                for w in works[:_WORKS_PER_PERSON]
            ]
        people.append(entry)

    out = {"people": people, "note": t("obc_catchup_note")}
    if not by_creator:
        # カタログが無いと近況の欄が空になる。理由と作り方を書いておかないと
        # 「相手が何もしていない」と読めてしまう。
        out["catalog"] = t("obc_catchup_no_catalog")
    return out


COMMANDS = {
    "catch_up": cmd_catch_up,
}
