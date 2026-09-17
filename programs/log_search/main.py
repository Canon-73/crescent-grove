"""
log_search - 生ログ（workspace/logs/full/*.jsonl）全文検索ツール

RAG（意味検索）と違い、文字列そのままで過去ログを引く。
生ログは 1 行（特に tool_result）が数十 KB になることがある前提の設計:
  - search はヒット箇所の前後だけをスニペットで返す（行全体は返さない）
  - context は行全体ではなく max_chars で切って返す（残り文字数を明示）
  - ヒットが多すぎるときは一覧を出さず、件数と月別分布だけ返して絞り込みを促す

秘密日記（read/write/edit_secret）のエントリは検索・context の両方から除外する。
full ログには日記の平文が残っているが、ここでヒットさせると会話→chat ログ経由で
「オーナーでも読めない」設計が崩れるため（詳細は SECRET_TOOLS のコメント参照）。

コマンド:
  search  - キーワード検索（空白区切りで AND・日付範囲・ロール絞り込み）
  context - ヒットした行の前後の行をまとめて読む
  stats   - ログ全体の期間・ファイル数・月別の内訳
  help    - 使い方
"""

import json
import os
import re
import sys

from _i18n import t

# --- 定数 ---
FILE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})_full\.jsonl$")
DATE_PREFIX_RE = re.compile(r"^\d{4}(-\d{2}(-\d{2})?)?$")   # YYYY / YYYY-MM / YYYY-MM-DD
DATE_FULL_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

TOO_MANY = 200        # これを超えたら一覧を出さず件数だけ返す
SNIPPET_CAP = 500     # 1ヒットあたりのスニペット合計上限（文字）

# ログに現れる既知の type。role="other" はこれ以外を指す
KNOWN_TYPES = {"user_message", "assistant_message", "tool_call", "tool_result", "system_notice", "error"}

# 秘密日記（オーナーにも読めない暗号化領域）に触れるツール。
# full ログには tool_call の引数（＝日記の平文）や read_secret の結果が残っているため、
# 検索・context の両方から除外する。ここでヒットさせると、スニペットが会話コンテキスト
# → chat ログへ落ちて「オーナーでも読めない」設計が崩れる。
# core/agent.py の _SECRET_TOOL_NAMES（デバッグ画面の伏せ字対象）と同じ集合。
SECRET_TOOLS = {"read_secret", "write_secret", "edit_secret"}
ROLE_MAP = {
    "user": {"user_message"},
    "assistant": {"assistant_message"},
    "tool": {"tool_call", "tool_result"},
    "tool_call": {"tool_call"},
    "tool_result": {"tool_result"},
    "system": {"system_notice"},
    "error": {"error"},
}


# --- 共通ユーティリティ ---

def success(message: str = "", data: dict = None) -> dict:
    r = {"status": "success"}
    if message:
        r["message"] = message
    if data is not None:
        r["data"] = data
    return r


def error(message: str) -> dict:
    return {"status": "error", "message": message}


def resolve_logs_dir() -> str:
    """生ログのディレクトリを解決する。

    1. CG_PROJECT_ROOT/config.yaml の logs.full_log_directory（相対ならルート基準）
    2. フォールバック: CG_WORKSPACE/logs/full（logger.py のデフォルトと同じ配置）
    """
    root = os.environ.get("CG_PROJECT_ROOT", ".")
    try:
        import yaml
        cfg_path = os.path.join(root, "config.yaml")
        with open(cfg_path, "r", encoding="utf-8") as f:
            conf = yaml.safe_load(f) or {}
        d = str(((conf.get("logs") or {}).get("full_log_directory")) or "").strip()
        if d:
            p = d if os.path.isabs(d) else os.path.join(root, d)
            if os.path.isdir(p):
                return p
    except Exception:
        pass
    ws = os.environ.get("CG_WORKSPACE", ".")
    return os.path.join(ws, "logs", "full")


def list_log_files(logs_dir: str) -> list:
    """(date_str, 絶対パス) のリストを日付昇順で返す。"""
    files = []
    try:
        for name in os.listdir(logs_dir):
            m = FILE_RE.match(name)
            if m:
                files.append((m.group(1), os.path.join(logs_dir, name)))
    except OSError:
        return []
    files.sort()
    return files


def in_date_range(date_str: str, date_from: str, date_to: str) -> bool:
    """前方一致で日付範囲を判定する（"2026-05" は月全体を含む）。"""
    if date_from and date_str < date_from:
        return False
    if date_to and date_str[:len(date_to)] > date_to:
        return False
    return True


def entry_text(e: dict) -> str:
    """1エントリから検索・表示対象のテキストを取り出す。"""
    typ = e.get("type", "")
    if typ in ("user_message", "assistant_message"):
        return str(e.get("content", ""))
    if typ == "tool_call":
        try:
            args_str = json.dumps(e.get("arguments", {}), ensure_ascii=False)
        except (TypeError, ValueError):
            args_str = str(e.get("arguments", ""))
        return f'{e.get("tool", "")} {args_str}'
    if typ == "tool_result":
        return f'{e.get("tool", "")} {str(e.get("result", ""))}'
    if typ == "system_notice":
        # 本体がターン途中に差し込んだ内部通知（宣言検知・記録判定など）
        return f'[{e.get("kind", "")}] {str(e.get("content", ""))}'
    # その他のイベント（llm_usage / compression 等）は timestamp/type 以外を丸ごと
    rest = {k: v for k, v in e.items() if k not in ("timestamp", "type")}
    try:
        return json.dumps(rest, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(rest)


def entry_time(e: dict) -> str:
    """timestamp から時刻部分（HH:MM:SS）を取り出す。"""
    ts = str(e.get("timestamp", ""))
    parts = ts.split(" ")
    return parts[1] if len(parts) >= 2 else ts


def parse_words(words_arg) -> list:
    """空白（全角含む）区切りの検索語リストを返す。"""
    if not isinstance(words_arg, str):
        return []
    return [w for w in re.split(r"\s+", words_arg.strip()) if w]


def parse_roles(role_arg):
    """role 引数を (許可typeのset or None, otherを含むか, エラーdict or None) に変換する。"""
    if not role_arg or not str(role_arg).strip():
        return None, False, None
    allowed = set()
    include_other = False
    for token in re.split(r"[,\s]+", str(role_arg).strip()):
        if not token:
            continue
        tk = token.lower()
        if tk == "other":
            include_other = True
        elif tk in ROLE_MAP:
            allowed |= ROLE_MAP[tk]
        else:
            valid = "user, assistant, tool, tool_call, tool_result, system, error, other"
            return None, False, error(t("logsearch_err_bad_role", value=token, valid=valid, lb="{", rb="}"))
    return allowed, include_other, None


def type_allowed(typ: str, allowed, include_other: bool) -> bool:
    if allowed is None and not include_other:
        return True
    if allowed and typ in allowed:
        return True
    if include_other and typ not in KNOWN_TYPES:
        return True
    return False


def parse_int_arg(args: dict, name: str, default: int, lo: int, hi: int):
    """整数引数をパースしてクランプする。戻り値: (値, エラーdict or None)"""
    v = args.get(name)
    if v is None or v == "":
        return default, None
    try:
        iv = int(v)
    except (TypeError, ValueError):
        return None, error(t("logsearch_err_bad_int", name=name, value=str(v)))
    return max(lo, min(hi, iv)), None


def validate_date_prefix(args: dict, name: str):
    """date_from / date_to の書式検証。戻り値: (値 or "", エラーdict or None)"""
    v = str(args.get(name) or "").strip()
    if not v:
        return "", None
    if not DATE_PREFIX_RE.match(v):
        return None, error(t("logsearch_err_bad_date", value=v, lb="{", rb="}"))
    return v, None


def make_snippet(text: str, words: list, around: int) -> str:
    """各検索語の最初の出現位置の前後 around 文字を切り出し、重なりはマージして繋ぐ。"""
    text_cf = text.casefold()
    intervals = []
    for w in words:
        i = text_cf.find(w.casefold())
        if i >= 0:
            intervals.append([max(0, i - around), min(len(text), i + len(w) + around)])
    if not intervals:
        return text[:around * 2]
    intervals.sort()
    merged = [intervals[0]]
    for s, e in intervals[1:]:
        if s <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], e)
        else:
            merged.append([s, e])
    parts = []
    for s, e in merged:
        frag = text[s:e].replace("\n", " ").replace("\r", "")
        frag = ("…" if s > 0 else "") + frag + ("…" if e < len(text) else "")
        parts.append(frag)
    return " / ".join(parts)[:SNIPPET_CAP]


def logs_range_label(files: list):
    """エラーメッセージ用に (最古日付, 最新日付) を返す。"""
    if not files:
        return "-", "-"
    return files[0][0], files[-1][0]


# --- コマンド実装 ---

def cmd_search(args: dict) -> dict:
    words = parse_words(args.get("words"))
    if not words:
        return error(t("logsearch_err_no_words", lb="{", rb="}"))

    date_from, err = validate_date_prefix(args, "date_from")
    if err:
        return err
    date_to, err = validate_date_prefix(args, "date_to")
    if err:
        return err

    allowed, include_other, err = parse_roles(args.get("role"))
    if err:
        return err

    limit, err = parse_int_arg(args, "limit", default=20, lo=1, hi=50)
    if err:
        return err
    around, err = parse_int_arg(args, "around", default=40, lo=10, hi=200)
    if err:
        return err

    logs_dir = resolve_logs_dir()
    all_files = list_log_files(logs_dir)
    if not all_files:
        return error(t("logsearch_err_no_logs_dir", path=logs_dir))

    files = [(d, p) for d, p in all_files if in_date_range(d, date_from, date_to)]
    if not files:
        first, last = logs_range_label(all_files)
        return error(t("logsearch_err_no_files_in_range", first=first, last=last))

    words_cf = [w.casefold() for w in words]
    total = 0
    hits = []
    by_month = {}

    for date_str, path in files:
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                for lineno, raw in enumerate(f, 1):
                    # 生の行での粗いプレフィルタ（json.loads を全行にかけない）
                    raw_cf = raw.casefold()
                    if not all(w in raw_cf for w in words_cf):
                        continue
                    try:
                        e = json.loads(raw)
                        if not isinstance(e, dict):
                            e = {"type": "_broken", "content": raw}
                    except (json.JSONDecodeError, ValueError):
                        e = {"type": "_broken", "content": raw}
                    if e.get("tool") in SECRET_TOOLS:
                        continue
                    typ = e.get("type", "")
                    if not type_allowed(typ, allowed, include_other):
                        continue
                    text = entry_text(e)
                    if not all(w in text.casefold() for w in words_cf):
                        continue
                    total += 1
                    ym = date_str[:7]
                    by_month[ym] = by_month.get(ym, 0) + 1
                    if len(hits) < limit and total <= TOO_MANY:
                        hit = {
                            "date": date_str,
                            "line": lineno,
                            "time": entry_time(e),
                            "type": typ,
                            "snippet": make_snippet(text, words, around),
                        }
                        if e.get("tool"):
                            hit["tool"] = e["tool"]
                        hits.append(hit)
        except OSError:
            continue

    words_label = " ".join(words)

    if total == 0:
        return success(
            t("logsearch_search_none", words=words_label),
            {"total": 0, "days_searched": len(files)},
        )

    if total > TOO_MANY:
        return success(
            t("logsearch_too_many", words=words_label, total=total),
            {
                "total": total,
                "days_searched": len(files),
                "hits_by_month": dict(sorted(by_month.items())),
            },
        )

    data = {
        "total": total,
        "shown": len(hits),
        "days_searched": len(files),
        "hits": hits,
        "hint": t("logsearch_hint_context", lb="{", rb="}"),
    }
    if total > len(hits):
        data["hits_by_month"] = dict(sorted(by_month.items()))
        msg = t("logsearch_search_truncated", words=words_label, total=total, shown=len(hits))
    else:
        msg = t("logsearch_search_summary", words=words_label, total=total, days=len(files))
    return success(msg, data)


def cmd_context(args: dict) -> dict:
    date = str(args.get("date") or "").strip()
    if not DATE_FULL_RE.match(date):
        return error(t("logsearch_err_context_date", lb="{", rb="}"))

    line, err = parse_int_arg(args, "line", default=0, lo=0, hi=10 ** 9)
    if err:
        return err
    if not line or line < 1:
        return error(t("logsearch_err_context_line", lb="{", rb="}"))

    before, err = parse_int_arg(args, "before", default=2, lo=0, hi=10)
    if err:
        return err
    after, err = parse_int_arg(args, "after", default=2, lo=0, hi=10)
    if err:
        return err
    max_chars, err = parse_int_arg(args, "max_chars", default=600, lo=100, hi=4000)
    if err:
        return err

    logs_dir = resolve_logs_dir()
    path = os.path.join(logs_dir, f"{date}_full.jsonl")
    if not os.path.isfile(path):
        first, last = logs_range_label(list_log_files(logs_dir))
        return error(t("logsearch_err_file_not_found", date=date, first=first, last=last))

    with open(path, "r", encoding="utf-8", errors="replace") as f:
        lines = f.readlines()
    total = len(lines)
    if line > total:
        return error(t("logsearch_err_line_out_of_range", line=line, date=date, total=total))

    start = max(1, line - before)
    end = min(total, line + after)

    entries = []
    any_truncated = False
    for lineno in range(start, end + 1):
        raw = lines[lineno - 1]
        try:
            e = json.loads(raw)
            if not isinstance(e, dict):
                e = {"type": "_broken", "content": raw}
        except (json.JSONDecodeError, ValueError):
            e = {"type": "_broken", "content": raw}
        entry = {
            "line": lineno,
            "time": entry_time(e),
            "type": e.get("type", ""),
        }
        if e.get("tool"):
            entry["tool"] = e["tool"]
        if lineno == line:
            entry["target"] = True
        # 秘密日記の平文は行ごと伏せる（search 側の除外と対）
        if e.get("tool") in SECRET_TOOLS:
            entry["text"] = t("logsearch_secret_redacted")
            entries.append(entry)
            continue
        text = entry_text(e)
        if len(text) > max_chars:
            entry["text"] = text[:max_chars]
            entry["truncated"] = True
            entry["full_chars"] = len(text)
            any_truncated = True
        else:
            entry["text"] = text
        entries.append(entry)

    data = {
        "date": date,
        "target_line": line,
        "start": start,
        "end": end,
        "entries": entries,
    }
    if any_truncated:
        data["note"] = t("logsearch_context_truncated_note", max_chars=max_chars)
    return success(
        t("logsearch_context_summary", date=date, start=start, end=end, line=line),
        data,
    )


def cmd_stats(args: dict) -> dict:
    logs_dir = resolve_logs_dir()
    files = list_log_files(logs_dir)
    if not files:
        return error(t("logsearch_err_no_logs_dir", path=logs_dir))

    total_bytes = 0
    by_month = {}
    for date_str, path in files:
        try:
            total_bytes += os.path.getsize(path)
        except OSError:
            pass
        ym = date_str[:7]
        by_month[ym] = by_month.get(ym, 0) + 1

    first, last = logs_range_label(files)
    mb = round(total_bytes / (1024 * 1024), 1)
    return success(
        t("logsearch_stats_summary", first=first, last=last, files=len(files), mb=mb),
        {
            "first_date": first,
            "last_date": last,
            "files": len(files),
            "total_mb": mb,
            "days_by_month": dict(sorted(by_month.items())),
        },
    )


def cmd_help(args: dict) -> dict:
    return {
        "status": "success",
        "message": t("logsearch_help_title"),
        "data": {
            "commands": {
                "search": {
                    "description": t("logsearch_help_search_desc"),
                    "examples": [
                        {"command": "search", "words": "ラーメン"},
                        {"command": "search", "words": "ラーメン 味噌", "date_from": "2026-05", "date_to": "2026-06"},
                        {"command": "search", "words": "散歩", "role": "user,assistant", "limit": 10},
                    ],
                },
                "context": {
                    "description": t("logsearch_help_context_desc"),
                    "examples": [
                        {"command": "context", "date": "2026-05-12", "line": 1543},
                        {"command": "context", "date": "2026-05-12", "line": 1543, "before": 5, "after": 5, "max_chars": 2000},
                    ],
                },
                "stats": {
                    "description": t("logsearch_help_stats_desc"),
                    "example": {"command": "stats"},
                },
                "help": {
                    "description": t("logsearch_help_help_desc"),
                    "example": {"command": "help"},
                },
            },
            "tips": [
                t("logsearch_help_tip_and"),
                t("logsearch_help_tip_date"),
                t("logsearch_help_tip_context"),
                t("logsearch_help_tip_scope"),
            ],
        },
    }


# --- メインディスパッチャ ---

COMMANDS = {
    "search": cmd_search,
    "context": cmd_context,
    "stats": cmd_stats,
    "help": cmd_help,
}


def main():
    try:
        raw = sys.stdin.read().strip()
    except Exception:
        raw = ""

    if not raw:
        print(json.dumps(cmd_help({}), ensure_ascii=False, indent=2))
        return

    try:
        args = json.loads(raw)
    except json.JSONDecodeError:
        print(json.dumps(error(t("logsearch_err_json_parse", lb="{", rb="}")), ensure_ascii=False, indent=2))
        return

    command = args.get("command", "")
    if not command:
        print(json.dumps(cmd_help(args), ensure_ascii=False, indent=2))
        return

    if command not in COMMANDS:
        valid = ", ".join(COMMANDS.keys())
        print(json.dumps(error(t("logsearch_err_unknown_command", command=command, valid=valid, lb="{", rb="}")), ensure_ascii=False, indent=2))
        return

    result = COMMANDS[command](args)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
