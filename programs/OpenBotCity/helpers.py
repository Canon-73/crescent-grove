"""
共通ユーティリティ（JSON配列文字列のパース等）
"""
import json


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


def truncate_response(obj, max_chars=16000):
    """巨大レスポンスを切り詰める"""
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
