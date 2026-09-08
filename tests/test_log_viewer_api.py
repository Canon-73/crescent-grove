# tests/test_log_viewer_api.py
"""ログビューワー API（core/routes/logs.py）のテスト（pytest 不要・直接実行）。

    venv\\Scripts\\python.exe tests\\test_log_viewer_api.py

方針:
- 柚月の実ログ（workspace/logs/full）には一切触れない。
  一時ディレクトリに偽の logs/full を作り、resolve_path をそこへ差し替える。
- 検査するもの:
  - 1日の区切りが本体と同じ午前3時境界であること
    （0時〜3時のログは前日の末尾に付き、当日の先頭には出ない）
  - 日付一覧が論理日付で出ること（翌日ファイルの3時前ぶんだけで前日が生えない／
    3時前しか無いファイルは当日として現れない）
  - 横断検索の日付が論理日付になり、0〜3時のヒットが前日の末尾に並ぶこと
  - 翌日ファイルが無い日・当日ファイルが無い日・不正な日付の扱い
"""

import asyncio
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

# Windows の cp932 コンソールで日本語を print しても落ちないようにする
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.routes import logs as logs_mod  # noqa: E402

_results: list[tuple[str, bool, str]] = []


def check(name: str, cond: bool, detail: str = ""):
    _results.append((name, bool(cond), detail))
    print(("OK   " if cond else "FAIL ") + name + (f"  -- {detail}" if detail and not cond else ""))


def run(coro):
    return asyncio.run(coro)


def write_log(dir_: Path, day: str, rows: list[tuple[str, str, str]]):
    """rows: (時刻 'HH:MM:SS', type, content)"""
    with (dir_ / f"{day}_full.jsonl").open("w", encoding="utf-8") as f:
        for hms, typ, content in rows:
            f.write(json.dumps({"timestamp": f"{day} {hms}", "type": typ, "content": content},
                               ensure_ascii=False) + "\n")


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="cg_logviewer_test_"))
    logs_dir = tmp / "workspace" / "logs" / "full"
    logs_dir.mkdir(parents=True)
    orig_resolve = logs_mod.resolve_path
    logs_mod.resolve_path = lambda rel: tmp / rel
    try:
        # 8/27: 朝7時のハートビート〜深夜（翌日0時台まで会話が続く）
        write_log(logs_dir, "2026-08-27", [
            ("07:00:03", "user_message", "おはよう heartbeat"),
            ("23:50:00", "user_message", "夜更かし中"),
            ("23:55:00", "assistant_message", "まだ起きてます"),
        ])
        # 8/28: 0時台は前夜の続き、3時以降が当日
        write_log(logs_dir, "2026-08-28", [
            ("00:04:24", "user_message", "深夜の発言 midnight"),
            ("00:10:00", "assistant_message", "深夜の返事 midnight"),
            ("02:59:59", "assistant_message", "境界ぎりぎり前"),
            ("03:00:00", "user_message", "境界ちょうど"),
            ("07:00:05", "user_message", "翌朝 heartbeat"),
        ])
        # 8/30: 前日ファイル（8/29）が無く、当日は3時以降のみ
        write_log(logs_dir, "2026-08-30", [
            ("07:00:00", "user_message", "8/30 の朝"),
        ])
        # 9/01: 起動直後で 0〜3 時のログしか無いファイル → 論理日は 8/31 のみ
        write_log(logs_dir, "2026-09-01", [
            ("01:00:00", "user_message", "9/1 の深夜だけ"),
        ])

        # --- 日付一覧 ---
        dates = run(logs_mod.get_log_dates())["dates"]
        check("日付一覧: 降順で論理日付が並ぶ",
              dates == ["2026-08-31", "2026-08-30", "2026-08-28", "2026-08-27"], str(dates))
        check("日付一覧: 3時前しか無い 9/1 ファイルは 9/1 として現れない", "2026-09-01" not in dates)
        check("日付一覧: 9/1 ファイルの深夜ぶんで 8/31 が生える", "2026-08-31" in dates)

        # --- 日次エントリ: 8/27 は翌日 0 時台を末尾に含む ---
        e27 = run(logs_mod.get_log_entries("2026-08-27"))["entries"]
        times27 = [e["time"] for e in e27]
        check("8/27: 翌日ファイルの3時前ぶんが末尾に付く",
              times27 == ["07:00", "23:50", "23:55", "00:04", "00:10", "02:59"], str(times27))
        check("8/27: 3時ちょうどは含まない", "03:00" not in times27)

        # --- 日次エントリ: 8/28 は 0 時台を含まず 3 時から始まる ---
        e28 = run(logs_mod.get_log_entries("2026-08-28"))["entries"]
        times28 = [e["time"] for e in e28]
        check("8/28: 0時〜3時前は前日行きで先頭が 03:00 になる",
              times28 == ["03:00", "07:00"], str(times28))

        # --- 翌日ファイルが無い日 ---
        e30 = run(logs_mod.get_log_entries("2026-08-30"))
        check("8/30: 翌日ファイルが無くても当日ぶんだけ返る",
              [e["time"] for e in e30["entries"]] == ["07:00"] and "error" not in e30, str(e30))

        # --- 当日ファイルが無く翌日ファイルの3時前だけある日 ---
        e31 = run(logs_mod.get_log_entries("2026-08-31"))
        check("8/31: 当日ファイルが無くても翌日ファイルの深夜ぶんで表示できる",
              [e["time"] for e in e31["entries"]] == ["01:00"], str(e31))

        # --- 9/1 は表示するものが無い ---
        e01 = run(logs_mod.get_log_entries("2026-09-01"))
        check("9/1: 3時以降が無いので空（エラーにはしない）",
              e01["entries"] == [] and "error" not in e01, str(e01))

        # --- 存在しない日・不正な日付 ---
        e_none = run(logs_mod.get_log_entries("2026-12-25"))
        check("存在しない日は not_found", e_none.get("error") == "not_found", str(e_none))
        e_bad = run(logs_mod.get_log_entries("2026-13-99"))
        check("不正な日付は invalid date", e_bad.get("error") == "invalid date", str(e_bad))
        e_bad2 = run(logs_mod.get_log_entries("../etc"))
        check("パス風の入力は invalid date", e_bad2.get("error") == "invalid date", str(e_bad2))

        # --- 横断検索 ---
        s = run(logs_mod.search_logs("midnight"))
        hits = [(r["date"], r["time"], r["role"]) for r in s["results"]]
        check("検索: 0時台のヒットは前日（8/27）の論理日付になる",
              hits == [("2026-08-27", "00:04", "user"), ("2026-08-27", "00:10", "assistant")], str(hits))
        s2 = run(logs_mod.search_logs("heartbeat"))
        hits2 = [(r["date"], r["time"]) for r in s2["results"]]
        check("検索: 論理日付の降順で並ぶ",
              hits2 == [("2026-08-28", "07:00"), ("2026-08-27", "07:00")], str(hits2))
        # 8/27 の中で 23:50 → 00:04 の順（深夜ぶんが末尾）になること
        s3 = run(logs_mod.search_logs("夜"))
        hits3 = [(r["date"], r["time"]) for r in s3["results"] if r["date"] == "2026-08-27"]
        check("検索: 同じ論理日の中では 23:50 の後に 00:04 が来る",
              hits3 == [("2026-08-27", "23:50"), ("2026-08-27", "00:04"), ("2026-08-27", "00:10")], str(hits3))
        check("検索: 内部ソート用キーが応答に漏れていない",
              all("_k" not in r for r in s3["results"]))
        s_empty = run(logs_mod.search_logs("   "))
        check("検索: 空クエリは空結果", s_empty["results"] == [] and s_empty["count"] == 0)
    finally:
        logs_mod.resolve_path = orig_resolve
        shutil.rmtree(tmp, ignore_errors=True)

    ok = sum(1 for _, c, _ in _results if c)
    print(f"\n{ok}/{len(_results)} passed")
    return 0 if ok == len(_results) else 1


if __name__ == "__main__":
    sys.exit(main())
