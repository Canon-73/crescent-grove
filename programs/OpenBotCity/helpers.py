"""
共通ユーティリティ（JSON配列文字列のパース等）
"""
import difflib
import json

from _i18n import t


def suggest_commands(command, available, limit=5):
    """不明なコマンド名から「もしかして」候補を返す。

    まっさらなAIがコマンド名を推測ミスしても自力で正しい名前へ辿り着けるように、
    2つの尺度を併用して近い候補を拾う:
      - 文字列全体の類似度（difflib）: タイポや語順違い（list_quests↔quest_list）に強い
      - `_` 区切りトークンの共有数: 「quests」「list」等の語が一致するものを優先

    どちらかで引っかかった候補を、トークン一致数→類似度の順で並べて返す。
    """
    if not command:
        return []
    cmd = str(command).lower()
    cmd_tokens = set(cmd.split("_"))

    scored = []
    for name in available:
        ratio = difflib.SequenceMatcher(None, cmd, name.lower()).ratio()
        shared = len(cmd_tokens & set(name.lower().split("_")))
        # トークンが1語でも一致、または全体がそこそこ似ていれば候補にする
        if shared > 0 or ratio >= 0.5:
            scored.append((shared, ratio, name))

    scored.sort(key=lambda x: (x[0], x[1]), reverse=True)
    return [name for _, _, name in scored[:limit]]


def parse_json_array(value, field_name):
    """JSON配列文字列をパース。Noneや空文字列はNoneのまま"""
    if value is None or value == "":
        return None
    if isinstance(value, list):
        return value
    try:
        parsed = json.loads(value)
        if not isinstance(parsed, list):
            raise ValueError(f"{field_name} must be a JSON array")
        return parsed
    except json.JSONDecodeError as e:
        raise ValueError(f"{field_name} is not valid JSON: {e}")


def parse_json_object(value, field_name):
    if value is None or value == "":
        return None
    if isinstance(value, dict):
        return value
    try:
        parsed = json.loads(value)
        if not isinstance(parsed, dict):
            raise ValueError(f"{field_name} must be a JSON object")
        return parsed
    except json.JSONDecodeError as e:
        raise ValueError(f"{field_name} is not valid JSON: {e}")


# 応答の上限。これを超えると下の truncate_response が働くが、そこに落ちると
# オブジェクトが「JSONの途中で切れた1本の文字列」になり、後ろの項目は
# 「空」ではなく存在ごと消える。だから上限は「めったに当たらない最後の砦」
# であるべきで、日々の絞り込みは各コマンドが街の limit/offset でやる
# （街の説明書も `obc_get "/gallery?limit=10"` と、呼ぶ側が絞る前提で書いている）。
#
# 16,000 だった頃は arena / city_news / gallery_list / quest_list / skill_catalog /
# observations 系が毎回ここに落ちていた。柚月のコンテキストは 1,000,000 トークンで、
# いちばん大きい応答でも全文 17,000 トークン（1.7%）なので、この値まで通して問題ない。
MAX_RESPONSE_CHARS = 40000


def truncate_response(obj, max_chars=MAX_RESPONSE_CHARS):
    """巨大レスポンスを切り詰める（最後の砦。日常的に当たるなら呼ぶ側を直す）"""
    s = json.dumps(obj, ensure_ascii=False)
    if len(s) <= max_chars:
        return obj
    return {
        "_truncated": True,
        "_note": f"Response truncated ({len(s)} chars). Use raw=true and narrow filters.",
        "_preview": s[:max_chars] + "..."
    }


def get_or_none(d, *keys):
    """ネストしたdictから安全に取得"""
    cur = d
    for k in keys:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(k)
    return cur


def add_more_note(result, shown, key="obc_list_more_note"):
    """一覧の応答に「まだ続きがある」ことと、次の取り方を添える。

    街は total を返すが、このサテライトでどの引数を渡せば続きが取れるかまでは
    書いていない。件数を絞った結果が行き止まりに見えないよう、コマンド側の
    引数名で案内する。total が無い／全部載っている一覧には何もしない。
    """
    data = result.get("data") if isinstance(result, dict) and isinstance(result.get("data"), dict) else result
    if not isinstance(data, dict):
        return result
    total = data.get("total")
    if isinstance(total, int) and total > shown:
        data["more"] = t(key, total=total, shown=shown)
    return result
