# core/routes/logs.py
"""過去ログAPI（日付一覧・横断検索・日次エントリ取得）。

「1日」の区切りは本体（core/time_utils.get_logical_date）と同じ午前3時境界。
ファイル自体は logger がカレンダー日付（0時境界）で切っているので、
論理日 D の表示は「D のファイルの3時以降 ＋ D+1 のファイルの3時より前」を結合して作る。
ディスク上のファイル名・中身は一切変えない（バックアップ・log_search・Layer再構築の
読み方に影響を出さないため）。
"""

import json
import re
from datetime import date as _date, timedelta
from pathlib import Path
from typing import Iterator

from fastapi import APIRouter

from core.paths import resolve_path

router = APIRouter()

# 論理日の開始時刻（core/time_utils と同じ 3 時）
LOGICAL_DAY_START_HOUR = 3

_FILE_RE = re.compile(r'(\d{4}-\d{2}-\d{2})_full\.jsonl$')
_TS_RE = re.compile(r'(\d{2}):(\d{2}):\d{2}')


def _logs_dir() -> Path:
    return resolve_path("workspace/logs/full")


def _ts_hour_min(ts: str) -> tuple[int, int] | None:
    """timestamp 文字列から (時, 分) を取り出す。取れなければ None。"""
    m = _TS_RE.search(str(ts))
    if not m:
        return None
    return int(m.group(1)), int(m.group(2))


def _is_before_day_start(ts: str) -> bool:
    """タイムスタンプが論理日の開始（3時）より前なら True。時刻不明は False（当日扱い）。"""
    hm = _ts_hour_min(ts)
    return hm is not None and hm[0] < LOGICAL_DAY_START_HOUR


def _logical_date_of(file_date: str, ts: str) -> str:
    """ファイルのカレンダー日付とタイムスタンプから論理日付を返す。3時前なら前日。"""
    if not _is_before_day_start(ts):
        return file_date
    try:
        return (_date.fromisoformat(file_date) - timedelta(days=1)).isoformat()
    except ValueError:
        return file_date


def _time_str(ts: str) -> str:
    hm = _ts_hour_min(ts)
    return f"{hm[0]:02d}:{hm[1]:02d}" if hm else ""


def _time_sort_key(ts: str) -> int:
    """論理日の中での並び順。3時前は前日の末尾に来るよう +24 時間で扱う。"""
    hm = _ts_hour_min(ts)
    if hm is None:
        return -1
    h = hm[0] + (24 if hm[0] < LOGICAL_DAY_START_HOUR else 0)
    return h * 60 + hm[1]


def _iter_entries(path: Path) -> Iterator[dict]:
    """JSONL を1行ずつ dict にして返す。壊れた行・空行は黙って飛ばす。"""
    if not path.exists():
        return
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except Exception:
                continue
            if isinstance(d, dict):
                yield d


def _first_last_timestamp(path: Path) -> tuple[str, str]:
    """ファイルの先頭行と末尾行のタイムスタンプを返す（全行を読まずに済ませる）。"""
    first = last = ""
    try:
        with path.open("rb") as fh:
            head = fh.readline()
            fh.seek(0, 2)
            size = fh.tell()
            # 末尾から最大 64KB だけ読んで最後の非空行を取る
            fh.seek(max(0, size - 65536))
            tail_lines = [ln for ln in fh.read().splitlines() if ln.strip()]
        tail = tail_lines[-1] if tail_lines else b""
        for raw, which in ((head, "first"), (tail, "last")):
            try:
                ts = str(json.loads(raw.decode("utf-8", errors="replace")).get("timestamp", ""))
            except Exception:
                ts = ""
            if which == "first":
                first = ts
            else:
                last = ts
    except Exception:
        pass
    return first, last


@router.get("/api/logs/dates")
async def get_log_dates():
    """存在する論理日付（3時境界）の一覧をJSONで返す（降順）。

    ファイル D は「D の論理日」に加え、先頭が3時より前なら「D-1 の論理日」にも
    ログを供給する。末尾まで3時前しか無いファイル（起動直後など）は D を含めない。
    """
    logs_dir = _logs_dir()
    dates: set[str] = set()
    if logs_dir.exists():
        for f in logs_dir.iterdir():
            m = _FILE_RE.match(f.name)
            if not m:
                continue
            file_date = m.group(1)
            first_ts, last_ts = _first_last_timestamp(f)
            if _is_before_day_start(first_ts):
                dates.add(_logical_date_of(file_date, first_ts))
            if not _is_before_day_start(last_ts):
                dates.add(file_date)
    return {"dates": sorted(dates, reverse=True)}


@router.get("/api/logs/search")
async def search_logs(q: str = ""):
    """全 full ログを横断検索し、検索ワードを含むログの日時（分まで）一覧を返す。

    日付未選択時の過去ログ検索に使用する。本文は返さず、各マッチの
    {"date": "YYYY-MM-DD", "time": "HH:MM", "role": ...} のみを返す（新しい日付が先頭）。
    date は論理日付（3時境界）。0時〜3時のログは前日の末尾として並ぶ。
    role はログ種別から判定: user(ユーザー) / assistant(アシスタント) /
    tool_call(ツールコール) / tool(ツールロール) の4種。
    """
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
    logs_dir = _logs_dir()
    results: list[dict] = []
    seen: set[tuple[str, str, str]] = set()  # 同一(論理日付, 分, ロール)の重複を排除する
    LIMIT = 2000  # 結果が膨大になりすぎないよう上限を設ける
    if logs_dir.exists():
        files = sorted(
            [f for f in logs_dir.iterdir() if _FILE_RE.match(f.name)],
            reverse=True,  # 新しい日付から走査
        )
        for f in files:
            file_date_m = _FILE_RE.match(f.name)
            file_date = file_date_m.group(1) if file_date_m else ""
            try:
                for d in _iter_entries(f):
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
                    ts = str(d.get("timestamp", ""))
                    logical = _logical_date_of(file_date, ts)
                    time_str = _time_str(ts)
                    key = (logical, time_str, role)
                    if key in seen:
                        continue
                    seen.add(key)
                    results.append({"date": logical, "time": time_str, "role": role,
                                    "_k": _time_sort_key(ts)})
                    if len(results) >= LIMIT:
                        break
            except Exception:
                continue
            if len(results) >= LIMIT:
                break
    # ファイル単位の走査だと 0〜3 時ぶんが前日グループから離れて出るので、
    # 論理日付の降順・その日の中では時刻昇順に並べ直す
    results.sort(key=lambda r: (r["date"], -r["_k"]), reverse=True)
    for r in results:
        r.pop("_k", None)
    return {"results": results, "query": query, "count": len(results),
            "truncated": len(results) >= LIMIT}


def _to_ui_entry(d: dict) -> dict:
    """生ログ1件を UI 描画用エントリに変換する。"""
    time_str = _time_str(str(d.get("timestamp", "")))
    t = d.get("type")
    if t == "user_message":
        return {"type": "message", "role": "user", "content": d.get("content", ""), "time": time_str}
    if t == "assistant_message":
        return {"type": "message", "role": "assistant", "content": d.get("content", ""), "time": time_str}
    if t == "tool_call":
        return {"type": "tool_call", "tool": d.get("tool", ""), "arguments": d.get("arguments", {}), "time": time_str}
    if t == "tool_result":
        res = d.get("result", "")
        if not isinstance(res, str):
            res = json.dumps(res, ensure_ascii=False)
        return {"type": "tool_result", "tool": d.get("tool", ""), "content": res, "time": time_str}
    if t == "intermediate":
        return {"type": "intermediate", "content": d.get("content", ""), "time": time_str}
    # 未知のtypeは生のJSONとして残す（デバッグ用）
    return {"type": "raw", "content": json.dumps(d, ensure_ascii=False), "time": time_str}


@router.get("/api/logs/{date}")
async def get_log_entries(date: str):
    """指定した論理日付（3時境界）の full ログを読み込み、UI描画用エントリ配列を返す。

    「date のファイルの3時以降」＋「翌日ファイルの3時より前」を時系列に結合する。
    各エントリは type と必要なフィールド (content/role/time/tool/arguments/result) のみを持つ。
    """
    if not re.match(r'^\d{4}-\d{2}-\d{2}$', date):
        return {"entries": [], "error": "invalid date"}
    try:
        next_date = (_date.fromisoformat(date) + timedelta(days=1)).isoformat()
    except ValueError:
        return {"entries": [], "error": "invalid date"}
    logs_dir = _logs_dir()
    today_path = logs_dir / f"{date}_full.jsonl"
    next_path = logs_dir / f"{next_date}_full.jsonl"
    if not today_path.exists() and not next_path.exists():
        return {"entries": [], "error": "not_found"}
    entries: list[dict] = []
    try:
        for d in _iter_entries(today_path):
            if _is_before_day_start(str(d.get("timestamp", ""))):
                continue  # 前日の論理日に属するぶん
            entries.append(_to_ui_entry(d))
        for d in _iter_entries(next_path):
            if not _is_before_day_start(str(d.get("timestamp", ""))):
                break  # 3時を過ぎたら以降は翌日の論理日（ファイルは時系列順）
            entries.append(_to_ui_entry(d))
    except Exception as e:
        return {"entries": [], "error": str(e)}
    if not entries and not today_path.exists():
        return {"entries": [], "error": "not_found"}
    return {"entries": entries}
