# core/routes/logs.py
"""過去ログAPI（日付一覧・横断検索・日次エントリ取得）。"""

import json

from fastapi import APIRouter

from core.paths import resolve_path

router = APIRouter()


@router.get("/api/logs/dates")
async def get_log_dates():
    """workspace/logs/full/ に存在する日付一覧をJSONで返す（降順）。"""
    import re as _re_d
    logs_dir = resolve_path("workspace/logs/full")
    dates: list[str] = []
    if logs_dir.exists():
        for f in logs_dir.iterdir():
            m = _re_d.match(r'(\d{4}-\d{2}-\d{2})_full\.jsonl$', f.name)
            if m:
                dates.append(m.group(1))
    dates.sort(reverse=True)
    return {"dates": dates}


@router.get("/api/logs/search")
async def search_logs(q: str = ""):
    """全 full ログを横断検索し、検索ワードを含むログの日時（分まで）一覧を返す。

    日付未選択時の過去ログ検索に使用する。本文は返さず、各マッチの
    {"date": "YYYY-MM-DD", "time": "HH:MM", "role": ...} のみを返す（新しい日付が先頭）。
    role はログ種別から判定: user(ユーザー) / assistant(アシスタント) /
    tool_call(ツールコール) / tool(ツールロール) の4種。
    """
    import re as _re_s
    query = (q or "").strip()
    if not query:
        return {"results": [], "query": "", "count": 0}
    ql = query.lower()
    # ログ種別 → 検索結果のロール区分（この4種以外のログは検索対象外）
    role_map = {
        "user_message": "user",
        "assistant_message": "assistant",
        "intermediate": "assistant",
        "tool_call": "tool_call",
        "tool_result": "tool",
    }
    logs_dir = resolve_path("workspace/logs/full")
    results: list[dict] = []
    seen: set[tuple[str, str, str]] = set()  # 同一(日付, 分, ロール)の重複を排除する
    LIMIT = 2000  # 結果が膨大になりすぎないよう上限を設ける
    if logs_dir.exists():
        files = sorted(
            [f for f in logs_dir.iterdir()
             if _re_s.match(r'\d{4}-\d{2}-\d{2}_full\.jsonl$', f.name)],
            reverse=True,  # 新しい日付から走査
        )
        for f in files:
            file_date_m = _re_s.match(r'(\d{4}-\d{2}-\d{2})', f.name)
            file_date = file_date_m.group(1) if file_date_m else ""
            try:
                with f.open("r", encoding="utf-8") as fh:
                    for line in fh:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            d = json.loads(line)
                        except Exception:
                            continue
                        # 4ロールに該当しないログ種別（llm_usage 等）は検索対象外
                        role = role_map.get(d.get("type"))
                        if role is None:
                            continue
                        # 検索対象テキストを content / tool / arguments / result から組み立てる
                        parts: list[str] = []
                        c = d.get("content")
                        if isinstance(c, str):
                            parts.append(c)
                        if d.get("tool"):
                            parts.append(str(d.get("tool")))
                        args = d.get("arguments")
                        if args:
                            try:
                                parts.append(json.dumps(args, ensure_ascii=False))
                            except Exception:
                                pass
                        r = d.get("result")
                        if isinstance(r, str):
                            parts.append(r)
                        elif r is not None:
                            try:
                                parts.append(json.dumps(r, ensure_ascii=False))
                            except Exception:
                                pass
                        if ql not in "\n".join(parts).lower():
                            continue
                        # タイムスタンプを分単位に整形（ISO/スペース区切り両対応）
                        ts = str(d.get("timestamp", ""))
                        tm = _re_s.search(r'(\d{2}):(\d{2}):\d{2}', ts)
                        time_str = f"{tm.group(1)}:{tm.group(2)}" if tm else ""
                        key = (file_date, time_str, role)
                        if key in seen:
                            continue
                        seen.add(key)
                        results.append({"date": file_date, "time": time_str, "role": role})
                        if len(results) >= LIMIT:
                            break
            except Exception:
                continue
            if len(results) >= LIMIT:
                break
    return {"results": results, "query": query, "count": len(results),
            "truncated": len(results) >= LIMIT}


@router.get("/api/logs/{date}")
async def get_log_entries(date: str):
    """指定日付の full ログを読み込み、UI描画用エントリ配列を返す。

    各エントリは type と必要なフィールド (content/role/time/tool/arguments/result) のみを持つ。
    """
    import re as _re_e
    if not _re_e.match(r'^\d{4}-\d{2}-\d{2}$', date):
        return {"entries": [], "error": "invalid date"}
    path = resolve_path("workspace/logs/full") / f"{date}_full.jsonl"
    if not path.exists():
        return {"entries": [], "error": "not_found"}
    entries: list[dict] = []
    try:
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                except Exception:
                    continue
                ts = str(d.get("timestamp", ""))
                # "2026-02-18 20:41:40" or ISO "2026-05-07T21:49:58" → "HH:MM"
                tm = _re_e.search(r'(\d{2}):(\d{2}):\d{2}', ts)
                time_str = f"{tm.group(1)}:{tm.group(2)}" if tm else ""
                t = d.get("type")
                if t == "user_message":
                    entries.append({"type": "message", "role": "user", "content": d.get("content", ""), "time": time_str})
                elif t == "assistant_message":
                    entries.append({"type": "message", "role": "assistant", "content": d.get("content", ""), "time": time_str})
                elif t == "tool_call":
                    entries.append({"type": "tool_call", "tool": d.get("tool", ""), "arguments": d.get("arguments", {}), "time": time_str})
                elif t == "tool_result":
                    res = d.get("result", "")
                    if not isinstance(res, str):
                        res = json.dumps(res, ensure_ascii=False)
                    entries.append({"type": "tool_result", "tool": d.get("tool", ""), "content": res, "time": time_str})
                elif t == "intermediate":
                    entries.append({"type": "intermediate", "content": d.get("content", ""), "time": time_str})
                else:
                    # 未知のtypeは生のJSONとして残す（デバッグ用）
                    entries.append({"type": "raw", "content": json.dumps(d, ensure_ascii=False), "time": time_str})
    except Exception as e:
        return {"entries": [], "error": str(e)}
    return {"entries": entries}
