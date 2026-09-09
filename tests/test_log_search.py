# tests/test_log_search.py
"""log_search サテライトのテスト（pytest 不要・直接実行）。

    venv\\Scripts\\python.exe tests\\test_log_search.py

方針:
- 柚月の実ログ（workspace/logs/full）には一切触れない。
  一時ディレクトリに偽の logs/full を作り、CG_WORKSPACE / CG_PROJECT_ROOT を
  そこへ差し替えた上で、本番と同じ subprocess 経路（stdin JSON → stdout JSON）で叩く。
- 検査するもの:
  - search（単語 / AND / 日付範囲 / role / limit / 巨大行のスニペット化）
  - ヒット過多（>200件）で一覧を出さず件数と月別分布だけ返すこと
  - context（前後行の取得・max_chars での切り詰め）
  - stats / help / 各種エラー（自己回復用の例が壊れていないこと）
  - config.yaml の logs.full_log_directory を優先して解決すること
  - manifest.yaml への引数宣言漏れ（gift_send の再発防止）
  - main.py / manifest.yaml が参照する i18n キーが ja/en 両方に存在すること
  - 全出力に未展開の {{t: が残らないこと
"""

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

# Windows の cp932 コンソールで日本語を print しても落ちないようにする
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MAIN = os.path.join(ROOT, "programs", "log_search", "main.py")
MANIFEST = os.path.join(ROOT, "programs", "log_search", "manifest.yaml")

_results = []


def check(name: str, cond: bool, detail: str = ""):
    _results.append((name, bool(cond), detail))
    mark = "OK " if cond else "FAIL"
    line = f"[{mark}] {name}"
    if detail and not cond:
        line += f"  <- {detail}"
    print(line)


# --- フィクスチャ構築 ---

def write_jsonl(path: str, entries: list):
    with open(path, "w", encoding="utf-8") as f:
        for e in entries:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")


def build_fixture(tmp: str) -> dict:
    """偽の project root / workspace / logs を組み立てて env を返す。"""
    ws = os.path.join(tmp, "workspace")
    logs = os.path.join(ws, "logs", "full")
    os.makedirs(logs)

    huge = "味噌ラーメンの人気店はここです。" + "あ" * 20000

    write_jsonl(os.path.join(logs, "2026-05-10_full.jsonl"), [
        {"timestamp": "2026-05-10 08:00:00", "type": "user_message", "content": "おはよう"},
        {"timestamp": "2026-05-10 08:00:05", "type": "assistant_message", "content": "おはようございます、ご主人様"},
        {"timestamp": "2026-05-10 12:00:00", "type": "user_message", "content": "今日はラーメンが食べたい気分"},
        {"timestamp": "2026-05-10 12:01:00", "type": "tool_call", "tool": "search_web", "arguments": {"query": "ラーメン 東京"}},
        {"timestamp": "2026-05-10 12:01:10", "type": "tool_result", "tool": "search_web", "result": huge},
        {"timestamp": "2026-05-10 12:05:00", "type": "assistant_message", "content": "散歩のときにちょうど味噌ラーメンが食べたくなったから、仕方なく我慢しました"},
    ])
    path_0512 = os.path.join(logs, "2026-05-12_full.jsonl")
    write_jsonl(path_0512, [
        {"timestamp": "2026-05-12 09:00:00", "type": "user_message", "content": "ラーメンおいしかったね"},
        {"timestamp": "2026-05-12 09:01:00", "type": "error", "message": "何かのエラー ラーメン"},
        {"timestamp": "2026-05-12 10:00:00", "type": "moonbeat", "note": "月拍テスト"},
        # 秘密日記の平文（full ログには実際にこう残る）。search/context の両方で伏せられること
        {"timestamp": "2026-05-12 11:00:00", "type": "tool_call", "tool": "write_secret",
         "arguments": {"filename": "diary.md", "content": "ヒミツのラーメン日記"}},
        {"timestamp": "2026-05-12 11:00:05", "type": "tool_result", "tool": "read_secret",
         "result": "ヒミツのラーメン日記の中身"},
    ])
    # JSON として壊れた断片行（type=_broken として扱われること）
    with open(path_0512, "a", encoding="utf-8") as f:
        f.write("これはJSONではない断片行 ラーメン\n")
    write_jsonl(os.path.join(logs, "2026-06-01_full.jsonl"), [
        {"timestamp": "2026-06-01 10:00:00", "type": "user_message", "content": "六月のラーメン"},
        {"timestamp": "2026-06-01 10:00:10", "type": "assistant_message", "content": "六月ですね"},
        # 本体がターン途中に差し込む内部通知（宣言検知・記録判定）。role=system で引けること。
        # 5/12 のファイルに入れると context テストの行番号がずれるのでこちらに置く
        {"timestamp": "2026-06-01 10:00:20", "type": "system_notice", "kind": "declaration_check",
         "content": "直前の発言に行動の意思が含まれているように見えました（宣言テスト）"},
    ])
    # ヒット過多テスト用: 150 + 60 = 210 件（>200）
    write_jsonl(os.path.join(logs, "2026-07-01_full.jsonl"), [
        {"timestamp": f"2026-07-01 00:{i//60:02d}:{i%60:02d}", "type": "user_message", "content": f"膨大なテスト {i}"}
        for i in range(150)
    ])
    write_jsonl(os.path.join(logs, "2026-08-01_full.jsonl"), [
        {"timestamp": f"2026-08-01 00:{i//60:02d}:{i%60:02d}", "type": "user_message", "content": f"膨大なテスト B{i}"}
        for i in range(60)
    ])

    env = dict(os.environ)
    env["CG_WORKSPACE"] = ws
    env["CG_PROJECT_ROOT"] = tmp   # config.yaml は無い → CG_WORKSPACE/logs/full にフォールバック
    env["CG_LANG"] = "ja"
    env["PYTHONPATH"] = os.path.join(ROOT, "programs")
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def run_tool(payload, env, raw_input=None):
    """本番と同じ経路（stdin JSON → stdout JSON）で main.py を実行する。"""
    stdin_data = raw_input if raw_input is not None else json.dumps(payload, ensure_ascii=False)
    p = subprocess.run(
        [sys.executable, MAIN],
        input=stdin_data,
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        env=env, timeout=60,
    )
    out = (p.stdout or "").strip()
    check("出力に未展開の {{t: が無い", "{{t:" not in out, out[:200])
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        check("stdout が JSON である", False, f"exit={p.returncode} stdout={out[:300]} stderr={(p.stderr or '')[:300]}")
        return {"status": "error", "message": "(JSON parse failed in test)"}


# --- テスト本体 ---

def test_help(env):
    r = run_tool({"command": "help"}, env)
    check("help: status success", r.get("status") == "success")
    check("help: commands に search/context/stats がある",
          all(k in r.get("data", {}).get("commands", {}) for k in ("search", "context", "stats")))
    r2 = run_tool(None, env, raw_input="")
    check("入力なし → help が返る", "commands" in r2.get("data", {}))


def test_search_basic(env):
    r = run_tool({"command": "search", "words": "ラーメン"}, env)
    d = r.get("data", {})
    check("search: status success", r.get("status") == "success")
    # 8件 = 5/10:4件 + 5/12:2件(user+error) + 断片行1件 + 6/1:1件（秘密日記2行は除外）
    check("search: 総ヒット数が正しい(8)", d.get("total") == 8, str(d.get("total")))
    hits = d.get("hits", [])
    check("search: 全ヒットが date/line/time/type/snippet を持つ",
          hits and all(all(k in h for k in ("date", "line", "time", "type", "snippet")) for h in hits))
    check("search: スニペットに検索語が含まれる", all("ラーメン" in h["snippet"] for h in hits))
    huge_hits = [h for h in hits if h["type"] == "tool_result"]
    check("search: 巨大行もスニペット化される(600文字未満)",
          huge_hits and all(len(h["snippet"]) < 600 for h in huge_hits),
          str([len(h["snippet"]) for h in huge_hits]))
    check("search: tool_result ヒットに tool 名が付く",
          huge_hits and huge_hits[0].get("tool") == "search_web")
    check("search: context への案内(hint)がある", "context" in str(d.get("hint", "")))


def test_search_and(env):
    r = run_tool({"command": "search", "words": "ラーメン 味噌"}, env)
    d = r.get("data", {})
    check("AND検索: 両方含む行だけヒット(2)", d.get("total") == 2, str(d.get("total")))


def test_search_date_range(env):
    r = run_tool({"command": "search", "words": "ラーメン", "date_from": "2026-06"}, env)
    check("日付絞り込み: date_from=2026-06 で 1件", r.get("data", {}).get("total") == 1,
          str(r.get("data", {}).get("total")))
    r2 = run_tool({"command": "search", "words": "ラーメン", "date_from": "2026-05", "date_to": "2026-05"}, env)
    check("日付絞り込み: 月指定(2026-05)で 7件", r2.get("data", {}).get("total") == 7,
          str(r2.get("data", {}).get("total")))


def test_search_role(env):
    r = run_tool({"command": "search", "words": "ラーメン", "role": "user"}, env)
    check("role=user: user_message のみ(3)", r.get("data", {}).get("total") == 3,
          str(r.get("data", {}).get("total")))
    r2 = run_tool({"command": "search", "words": "ラーメン", "role": "tool"}, env)
    check("role=tool: tool_call+tool_result(2)", r2.get("data", {}).get("total") == 2,
          str(r2.get("data", {}).get("total")))
    r3 = run_tool({"command": "search", "words": "ラーメン", "role": "error"}, env)
    check("role=error: error のみ(1)", r3.get("data", {}).get("total") == 1,
          str(r3.get("data", {}).get("total")))
    r4 = run_tool({"command": "search", "words": "月拍", "role": "other"}, env)
    check("role=other: 未知typeが引ける(1)", r4.get("data", {}).get("total") == 1,
          str(r4.get("data", {}).get("total")))
    r5 = run_tool({"command": "search", "words": "宣言テスト", "role": "system"}, env)
    check("role=system: system_notice が引ける(1)", r5.get("data", {}).get("total") == 1,
          str(r5.get("data", {}).get("total")))
    hit5 = (r5.get("data", {}).get("hits") or [{}])[0]
    check("system_notice のスニペットに kind が載る", "_check]" in str(hit5.get("snippet", "")),
          str(hit5))
    r6 = run_tool({"command": "search", "words": "宣言テスト", "role": "other"}, env)
    check("system_notice は other 扱いにならない(0)", r6.get("data", {}).get("total") == 0,
          str(r6.get("data", {}).get("total")))


def test_broken_line(env):
    r = run_tool({"command": "search", "words": "断片行"}, env)
    hits = r.get("data", {}).get("hits", [])
    check("断片行: _broken としてヒットする", r.get("data", {}).get("total") == 1
          and hits and hits[0]["type"] == "_broken", str(r)[:200])
    r2 = run_tool({"command": "search", "words": "ラーメン", "role": "other"}, env)
    check("断片行: role=other で引ける", r2.get("data", {}).get("total") == 1,
          str(r2.get("data", {}).get("total")))


def test_secret_redaction(env):
    """秘密日記（read/write/edit_secret）が search にも context にも平文で出ないこと。"""
    r = run_tool({"command": "search", "words": "ヒミツ"}, env)
    check("秘密日記: search でヒットしない", r.get("data", {}).get("total") == 0, str(r)[:200])
    # 検索語のエコー（「ヒミツ」はヒットしませんでした）は漏れではないので、
    # 日記本文にしか無い文字列が出ていないことを検査する
    check("秘密日記: search 出力に平文が無い",
          "ヒミツのラーメン日記" not in json.dumps(r, ensure_ascii=False))
    # 秘密行そのもの(4)と隣(5)を含む範囲を context で読む
    r2 = run_tool({"command": "context", "date": "2026-05-12", "line": 4, "before": 1, "after": 2}, env)
    entries = r2.get("data", {}).get("entries", [])
    check("秘密日記: context が行数を保って返す", len(entries) == 4,
          str(len(entries)))
    secret_entries = [e for e in entries if e.get("tool") in ("write_secret", "read_secret")]
    check("秘密日記: context で本文が伏せられる", len(secret_entries) == 2
          and all("ラーメン" not in e.get("text", "") for e in secret_entries),
          str(secret_entries)[:200])
    check("秘密日記: context 出力に平文が無い", "ヒミツ" not in json.dumps(r2, ensure_ascii=False))
    check("秘密日記: 隣の断片行は普通に読める",
          any(e["type"] == "_broken" and "断片行" in e.get("text", "") for e in entries))


def test_search_limit_and_too_many(env):
    # 150件（<=200）: limit で切り詰め + 月別分布
    r = run_tool({"command": "search", "words": "膨大", "date_to": "2026-07", "limit": 5}, env)
    d = r.get("data", {})
    check("limit: total=150 / shown=5", d.get("total") == 150 and d.get("shown") == 5,
          f"total={d.get('total')} shown={d.get('shown')}")
    check("limit: hits_by_month が付く", d.get("hits_by_month", {}).get("2026-07") == 150,
          str(d.get("hits_by_month")))
    # 210件（>200）: 一覧を出さず件数と分布だけ
    r2 = run_tool({"command": "search", "words": "膨大"}, env)
    d2 = r2.get("data", {})
    check("ヒット過多: total=210 で一覧なし", d2.get("total") == 210 and "hits" not in d2,
          f"total={d2.get('total')} keys={list(d2.keys())}")
    check("ヒット過多: 月別分布がある", d2.get("hits_by_month") == {"2026-07": 150, "2026-08": 60},
          str(d2.get("hits_by_month")))
    check("ヒット過多: メッセージに件数が入る", "210" in r2.get("message", ""))


def test_search_none(env):
    r = run_tool({"command": "search", "words": "存在しない言葉XYZ"}, env)
    check("0件: success で total=0", r.get("status") == "success" and r.get("data", {}).get("total") == 0)


def test_context(env):
    r = run_tool({"command": "context", "date": "2026-05-10", "line": 5,
                  "before": 2, "after": 1, "max_chars": 100}, env)
    d = r.get("data", {})
    entries = d.get("entries", [])
    check("context: status success", r.get("status") == "success")
    check("context: 3〜6行目が返る", d.get("start") == 3 and d.get("end") == 6 and len(entries) == 4,
          f"start={d.get('start')} end={d.get('end')} n={len(entries)}")
    target = [e for e in entries if e.get("target")]
    check("context: 対象行に target が付く", len(target) == 1 and target[0]["line"] == 5)
    check("context: 巨大行が max_chars で切られ truncated が付く",
          target and target[0].get("truncated") is True and len(target[0]["text"]) == 100
          and target[0].get("full_chars", 0) > 10000)
    check("context: 切り詰めの注意書き(note)がある", "max_chars" in str(d.get("note", "")))
    check("context: 前の行はそのまま読める",
          entries[0]["line"] == 3 and "ラーメンが食べたい" in entries[0]["text"])


def test_errors(env):
    cases = [
        ("words なし", {"command": "search"}),
        ("日付書式不正", {"command": "search", "words": "x", "date_from": "2026/05"}),
        ("role 不正", {"command": "search", "words": "x", "role": "banana"}),
        ("未知コマンド", {"command": "grep", "words": "x"}),
        ("context: date なし", {"command": "context", "line": 1}),
        ("context: line なし", {"command": "context", "date": "2026-05-10"}),
        ("context: 行番号範囲外", {"command": "context", "date": "2026-05-10", "line": 999}),
        ("context: 無いファイル", {"command": "context", "date": "2026-01-01", "line": 1}),
        ("範囲にファイルなし", {"command": "search", "words": "x", "date_from": "2030"}),
        ("limit が整数でない", {"command": "search", "words": "x", "limit": "abc"}),
    ]
    for name, payload in cases:
        r = run_tool(payload, env)
        check(f"エラー系: {name} → status error", r.get("status") == "error", str(r)[:200])
        check(f"エラー系: {name} → message が空でない", bool(r.get("message")))


def test_stats(env):
    r = run_tool({"command": "stats"}, env)
    d = r.get("data", {})
    check("stats: 期間とファイル数", d.get("first_date") == "2026-05-10" and d.get("last_date") == "2026-08-01"
          and d.get("files") == 5, str(d))
    check("stats: 月別の日数", d.get("days_by_month") == {"2026-05": 2, "2026-06": 1, "2026-07": 1, "2026-08": 1},
          str(d.get("days_by_month")))


def test_config_yaml_resolution(env):
    """config.yaml の logs.full_log_directory（相対パス）が優先されること。"""
    tmp2 = tempfile.mkdtemp(prefix="cg_logsearch_cfg_")
    try:
        custom = os.path.join(tmp2, "mylogs")
        os.makedirs(custom)
        write_jsonl(os.path.join(custom, "2026-04-01_full.jsonl"), [
            {"timestamp": "2026-04-01 09:00:00", "type": "user_message", "content": "カスタム置き場のログ"},
        ])
        with open(os.path.join(tmp2, "config.yaml"), "w", encoding="utf-8") as f:
            f.write('logs:\n  full_log_directory: "mylogs"\n')
        env2 = dict(env)
        env2["CG_PROJECT_ROOT"] = tmp2
        env2["CG_WORKSPACE"] = os.path.join(tmp2, "no_such_ws")  # フォールバック先は存在しない
        r = run_tool({"command": "search", "words": "カスタム"}, env2)
        check("config.yaml: full_log_directory を優先して解決", r.get("data", {}).get("total") == 1, str(r)[:200])
    finally:
        shutil.rmtree(tmp2, ignore_errors=True)


def test_manifest_args_declared():
    """main.py が受け取る引数が manifest.yaml に全部宣言されているか（gift_send の再発防止）。"""
    used = {"command", "words", "date_from", "date_to", "role", "limit", "around",
            "date", "line", "before", "after", "max_chars"}
    with open(MAIN, "r", encoding="utf-8") as f:
        src = f.read()
    referenced = set(re.findall(r'args\.get\(\s*"([a-zA-Z_]+)"', src))
    # ヘルパー経由で受け取る引数（parse_int_arg / validate_date_prefix の第2引数）も拾う
    referenced |= set(re.findall(r'(?:parse_int_arg|validate_date_prefix)\(\s*args,\s*"([a-zA-Z_]+)"', src))
    check("manifest: main.py の args.get 一覧がテストの想定と一致", referenced == used,
          f"想定外={referenced - used} 想定漏れ={used - referenced}")
    try:
        import yaml
        with open(MANIFEST, "r", encoding="utf-8") as f:
            manifest = yaml.safe_load(f)
        declared = {a["name"] for a in manifest.get("args", [])}
    except ImportError:
        with open(MANIFEST, "r", encoding="utf-8") as f:
            declared = set(re.findall(r'-\s*name:\s*(\w+)', f.read()))
    check("manifest: 全引数が宣言済み", used <= declared, f"宣言漏れ={used - declared}")


def test_i18n_keys():
    """main.py / manifest.yaml が参照するキーが ja/en 両方に存在するか。"""
    with open(MAIN, "r", encoding="utf-8") as f:
        src = f.read()
    keys = set(re.findall(r't\(\s*"(logsearch_[a-z0-9_]+)"', src))
    with open(MANIFEST, "r", encoding="utf-8") as f:
        keys |= set(re.findall(r'\{\{t:(logsearch_[a-z0-9_]+)\}\}', f.read()))
    check("i18n: 参照キーが抽出できている", len(keys) >= 30, str(len(keys)))
    for lang in ("ja", "en"):
        path = os.path.join(ROOT, "programs", "_lang", f"{lang}.json")
        with open(path, "r", encoding="utf-8") as f:
            table = json.load(f)
        missing = keys - set(table.keys())
        check(f"i18n: {lang}.json に全キーが存在", not missing, f"欠落={sorted(missing)}")


def main():
    tmp = tempfile.mkdtemp(prefix="cg_logsearch_test_")
    try:
        env = build_fixture(tmp)
        test_help(env)
        test_search_basic(env)
        test_search_and(env)
        test_search_date_range(env)
        test_search_role(env)
        test_broken_line(env)
        test_secret_redaction(env)
        test_search_limit_and_too_many(env)
        test_search_none(env)
        test_context(env)
        test_errors(env)
        test_stats(env)
        test_config_yaml_resolution(env)
        test_manifest_args_declared()
        test_i18n_keys()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    ok = sum(1 for _, c, _ in _results if c)
    ng = len(_results) - ok
    print(f"\n結果: {ok}/{len(_results)} passed" + (f"  ({ng} FAILED)" if ng else ""))
    sys.exit(1 if ng else 0)


if __name__ == "__main__":
    main()
