# -*- coding: utf-8 -*-
"""
Layer0 済みの会話履歴のうち、ツールを使っていないターンの本文を、生ログにある
柚月自身の発言へ戻す（オフライン用・一回限り）。

背景（2026-09-07 の Layer0 見直し）: 旧 Layer0 は、ツールを使っていないターンでも
LLM にターン全体を渡して「柚月の発言」を書き直させていた。その入力には self_memo・
flashback・内心などの注入文が含まれていたため、LLM がそれらを柚月の語りとして
言い直し、本人が言っていないことが履歴に残った（実測: ツール未使用ターンの本文の
うち本人の言葉に由来するのは 45%。プロンプトを丸写しした例もある）。

生ログ（workspace/logs/full）には柚月の発言が一字一句そのまま残っているので、
ツールを使っていないターンだけ、そこから本文を戻す。

  - 対象: Layer0 済み（<!-- layer0 --> 付き）で、生ログ側にツール呼び出しが無いターン
  - 対象外: ツールを使ったターン（LLM がツール結果を畳んだ本文は妥当）、生ログと
    確実に対応付けられないターン、生ログ側の発言が空のターン
  - 書き換えるのは assistant メッセージの本文だけ。メッセージ数・順序・user 側・
    <!-- layer0 --> マーカー・他のキーは触らない

**対応付けの規則**（Codex レビュー5巡で行き着いた形）:
  生ログを「ターン」に切り分けようとすると、切り方が旧 Layer0 と食い違ったときに気づけない。
  時刻の並びで検算する案も試したが、生ログ側で結合と分割が隣り合うと並びが一致してしまう
  反例が出た。そこで**生ログを切らない**。

  - 範囲は履歴側で決める。履歴の隣り合う 2 つのターンについて、それぞれの時刻に対応する
    生ログの user_message を見つけ、その**記録位置**で挟んだ範囲が、そのターンの範囲。
    履歴は旧 Layer0 の出力そのものなので、「次のターンが始まるまで」が旧 Layer0 が 1 つに
    畳んだ範囲と一致する。時刻ではなく記録位置で挟むのは、返答と次のターンの始まりが
    同じ秒に記録されたときに順序を取り違えないため
  - その範囲に入っている生ログの発言をつなぎ、ツール呼び出しがあれば対象外にする
  - 範囲を信用してよいかを確かめる: 履歴の時刻が前へ戻らない / 範囲の始まりと次の始まりに
    生ログの user_message が実在する / 範囲の途中に新しいターンの始まりに見えるものが無い /
    始まりの種別（moonbeat / task / city_event / ご主人様の発言）が履歴と一致する

使い方（**サーバ停止中に実行すること**。稼働中はメモリ側が正で、ファイルを直しても次の保存で戻る）:

  venv\\Scripts\\python.exe scripts\\restore_layer0_verbatim.py            # ドライラン（既定・読むだけ）
  venv\\Scripts\\python.exe scripts\\restore_layer0_verbatim.py --sample 5 # 変わる中身を5件見る
  venv\\Scripts\\python.exe scripts\\restore_layer0_verbatim.py --apply    # 控えを取ってから書き換え

--apply は 8080 番が応答する（＝サーバ稼働中）と拒否し、読み込みから書き込みまでの間に
ファイルが変わっていたら中止する。控えは data/context_state.json.before_restore_<日時>。
戻すときは控えを元の名前に戻すだけ。
"""
import argparse
import collections
import json
import os
import re
import shutil
import socket
import sys
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.tokens import count_text_tokens  # noqa: E402

STATE_PATH = ROOT / "data" / "context_state.json"
FULL_LOG_DIR = ROOT / "workspace" / "logs" / "full"
SERVER_PORT = 8080
LAYER0_MARKER = "<!-- layer0 -->"
HEAD_RE = re.compile(r'(\d{4}-\d{2}-\d{2}) (\d{2}:\d{2})')

# 生ログでは user_message として記録されるが、**旧** Layer0 のターン区切りでは
# 区切りにならないもの。ここを誤ると、旧 Layer0 が 1 つに畳んだ範囲と食い違う。
#   - 再生成の通知・記録判定・行動意思の確認: <system_notice> だけのメッセージ（当時も今も区切りでない）
#   - 外部からの通知（X の返信など）: 旧 extract_oldest_uncompressed_turn の境界リストに
#     <external_notice> が無く、前のターンへ取り込まれていた（2026-09-07 に修正済みだが、
#     いま履歴にある Layer0 済みターンは取り込まれた状態で作られている）
CONTINUATION_RE = re.compile(r'^\[SYSTEM\]\s*自動通知|^【記録判定|^直前の発言に|^\[X\]')


def _text(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(p.get("text", "") for p in content if isinstance(p, dict) and p.get("type") == "text")
    return str(content)


def _server_alive() -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", SERVER_PORT)) == 0


def load_raw_records():
    """生ログを (日時, 種類, 本文) の列にして**記録された順のまま**返す。ターンには切らない。

    ターンの切り方を推測すると、旧 Layer0 の切り方と食い違ったときに気づけない
    （Codex レビューで、結合と分割が隣り合うと時刻の並びが一致してしまう例が出た）。
    切らずに素の記録として持ち、範囲は履歴側の時刻で決める（build_plan を参照）。

    **時刻で並べ替えない。** 生ログは追記されるだけなので、ファイル順＝記録順。時刻で
    並べ替えると、時計が巻き戻ったときに記録順を壊し、別のターンの発言を混ぜてしまう。
    """
    recs = []
    for f in sorted(FULL_LOG_DIR.glob("2026-*_full.jsonl")):
        for line in f.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except Exception:
                continue
            t = r.get("type")
            if t not in ("user_message", "assistant_message", "tool_call"):
                continue
            try:
                dt = datetime.strptime(r.get("timestamp", ""), "%Y-%m-%d %H:%M:%S")
            except Exception:
                continue
            recs.append((dt, t, (r.get("content") or "").strip()))
    return recs


def corroborates(hist_user: str, raw_user: str) -> bool:
    """履歴の整形済み user 側と、生ログの user_message が同じターンを指しているか。

    時刻の対応付けだけだと、記録の抜けや同分の並びでずれても気づけない。種別が食い違う
    ものは戻さない。ご主人様の発言は、入力途中の綴りがログに残ることがある
    （「ただいま！」に対して "tadaima!" など）ので、種別が一致していれば本文までは求めない。
    """
    head = hist_user.split(LAYER0_MARKER)[0]
    if "\nmoonbeat" in head:
        return raw_user.startswith("[Moonbeat]")
    if "\ntask: " in head:
        return raw_user.startswith("【")
    if "\ncity_event: " in head:
        return raw_user.startswith(("[city_event:", "[OpenBotCity]"))
    if "\nexternal: " in head:
        return raw_user.startswith("[X]")
    if "\nuser: " in head:
        return not raw_user.startswith(("[Moonbeat]", "【", "[city_event:", "[OpenBotCity]", "[X]", "[SYSTEM]"))
    return False


MIN_OVERLAP = 0.15


def _shingles(s: str, n: int = 5) -> set:
    s = re.sub(r'\s', '', s)
    return {s[i:i + n] for i in range(max(0, len(s) - n + 1))}


def overlap(new: str, old: str) -> float:
    """戻そうとしている原文が、いまの本文とどれだけ言葉を共有しているか（0〜1）。

    いまの本文は旧 Layer0 が**その原文から**作ったものなので、書き直されていても言葉は
    かなり残る。実測: 正しい対の中央値 0.60・下位5% でも 0.21 に対し、無関係なターンと
    突き合わせた場合は中央値 0.06。これは時刻や順序の理屈とは独立した裏づけになるので、
    対応付けが何らかの理由でずれていれば、ここで落ちる可能性が高い。
    """
    a = _shingles(new)
    return len(a & _shingles(old)) / len(a) if a else 0.0


def _turn_start_kind(content: str) -> bool:
    """この生ログの user_message は「新しいターンの始まり」に見えるか。

    続き（区切りにならないもの）は、システムからの通知類と、外部からの通知
    （旧 Layer0 は <external_notice> を前のターンへ取り込んでいた）。
    ここでの誤りは**安全側にしか働かない**: 始まりに見えるものが区間の途中にあれば、
    そのターンは触らない。
    """
    return not CONTINUATION_RE.match(content)


def build_plan(history):
    """戻す対象を決める。戻り値: (plan, 内訳カウンタ)

    **区間の決め方**: 生ログをターンに切り分けるのではなく、履歴の隣り合う 2 つのターンの
    時刻で挟んだ範囲を、そのターンの範囲とする。履歴は旧 Layer0 の出力そのものなので、
    「次のターンが始まるまで」が旧 Layer0 が 1 つに畳んだ範囲と一致する。生ログ側の
    区切りを推測しないので、推測が旧 Layer0 と食い違って誤対応する余地が無い。

    範囲を信用するために、次を確かめる:
      - 履歴の時刻が前へ戻らないこと
      - 範囲の始まりと次の始まりに、生ログの user_message が実在すること（分まで一致）
      - 範囲の途中に「新しいターンの始まり」に見える user_message が無いこと
        （あれば履歴側に無いターンが挟まっているので、範囲を信用しない）
      - 始まりの user_message の種別が、履歴側の種別と一致すること
    """
    recs = load_raw_records()
    stat = collections.Counter()

    turns = []
    for i, msg in enumerate(history):
        if msg.get("role") != "user" or not isinstance(msg.get("content"), str):
            continue
        u = msg["content"]
        if LAYER0_MARKER not in u:
            continue
        m = HEAD_RE.match(u)
        if not m:
            stat["時刻が読めない"] += 1
            continue
        nxt = history[i + 1] if i + 1 < len(history) else None
        if not nxt or nxt.get("role") != "assistant" or not isinstance(nxt.get("content"), str):
            stat["assistant が無い"] += 1
            continue
        try:
            dt = datetime.strptime(f"{m.group(1)} {m.group(2)}", "%Y-%m-%d %H:%M")
        except ValueError:
            stat["時刻が読めない"] += 1
            continue
        turns.append({"idx": i + 1, "dt": dt, "date": m.group(1), "time": m.group(2),
                      "user": u, "old": nxt["content"]})

    # 分 → その分に記録された user_message の位置。記録順に並べ替えないので、
    # 時刻の昇順を前提にした探索（二分探索）は使わない
    starts_by_minute = collections.defaultdict(list)
    for i, (dt, t, c) in enumerate(recs):
        if t == "user_message" and _turn_start_kind(c):
            starts_by_minute[dt.replace(second=0)].append(i)

    def _user_at(dt):
        """その分に始まる生ログの user_message の**記録位置**を返す。

        範囲の端に時刻を使うと、返答と次のターンの始まりが同じ秒に記録されたときに
        どちらが先か決められない（時刻で切ると前のターンの返答が次へ移る）。生ログは
        記録された順に読んでいるので、位置で挟めば順序をそのまま尊重できる。
        同じ分に再生成の通知などが並ぶことがあるので、続き扱いのものは数えない。
        始まりに見えるものが 1 つでなければ、どれが相手か決められないので None。
        """
        starts = starts_by_minute.get(dt, [])
        return starts[0] if len(starts) == 1 else None

    plan = []
    for n, H in enumerate(turns):
        nxt = turns[n + 1] if n + 1 < len(turns) else None
        if nxt is None:
            stat["最後のターン（範囲の終わりが決まらない）"] += 1
            continue
        if nxt["dt"] <= H["dt"]:
            stat["履歴の時刻が前へ戻っている"] += 1
            continue
        begin = _user_at(H["dt"])
        end = _user_at(nxt["dt"])
        if begin is None or end is None:
            stat["生ログに始まりが見当たらない"] += 1
            continue
        if end <= begin:
            stat["履歴の時刻が前へ戻っている"] += 1
            continue
        if not corroborates(H["user"], recs[begin][2]):
            stat["種別が食い違う"] += 1
            continue
        # 範囲は、生ログに実在する 2 つの始まりの**記録位置**で挟む（両端は含まない）
        window = recs[begin + 1:end]
        # 記録順と時刻の向きが食い違う範囲は信用しない（時計が巻き戻った箇所）
        span = recs[begin:end + 1]
        if any(a[0] > b[0] for a, b in zip(span, span[1:])):
            stat["記録順と時刻が食い違う"] += 1
            continue
        # 日付の変わり目に近い範囲は信用しない。生ログはその日のファイルに追記されるので、
        # 日付をまたいで時計が巻き戻ると「ファイル順＝記録順」が崩れ、連結したときに
        # 記録の前後が入れ替わる。入れ替わったこと自体は時刻からは見分けられない
        # （実データには無いと確認済み: 日別ファイル 214 本で時刻範囲の重なり 0・
        # ファイル内の逆行 0）。見分けられない以上、**日付の変わり目をまたぐ範囲と、
        # その次のターンまでに日付が変わる範囲**をまとめて外す
        end2 = _user_at(turns[n + 2]["dt"]) if n + 2 < len(turns) else end
        look = recs[begin:max(end, end2 or end) + 1]
        if len({r[0].date() for r in look}) > 1:
            stat["日付の変わり目に近い"] += 1
            continue
        # 範囲の途中に、新しいターンの始まりに見えるものが無いこと
        if any(t == "user_message" and _turn_start_kind(c) for _, t, c in window):
            stat["範囲の途中に別のターンがある"] += 1
            continue
        # 範囲内の発言とツール呼び出しを集める
        asst, tools = [], 0
        for _, t, c in window:
            if t == "tool_call":
                tools += 1
            elif t == "assistant_message" and c:
                asst.append(c)
        if tools:
            stat["ツールを使ったターン"] += 1
            continue
        original = "\n\n".join(asst)
        if not original:
            stat["生ログ側の発言が空"] += 1
            continue
        if original == H["old"]:
            stat["既に原文と同じ"] += 1
            continue
        # 最後の裏づけ: いまの本文と言葉が共有されているか。時刻や順序の理屈とは
        # 独立した検査なので、対応付けが何らかの理由でずれていればここで落ちる
        if overlap(original, H["old"]) < MIN_OVERLAP:
            stat["いまの本文と言葉が重ならない"] += 1
            continue
        stat["戻す"] += 1
        plan.append({"idx": H["idx"], "date": H["date"], "time": H["time"],
                     "old": H["old"], "new": original})
    plan.sort(key=lambda p: p["idx"])
    return plan, stat


def _verify(old_state, new_state, plan) -> list:
    """書き換え後の状態を控えと突き合わせる。問題の説明を並べて返す（空なら健全）。"""
    problems = []
    if set(old_state.keys()) != set(new_state.keys()):
        problems.append("トップレベルのキーが変わった")
    for k in old_state:
        if k != "conversation_history" and old_state.get(k) != new_state.get(k):
            problems.append(f"conversation_history 以外のキー '{k}' が変わった")
    ob, nb = old_state["conversation_history"], new_state["conversation_history"]
    if len(ob) != len(nb):
        problems.append(f"メッセージ数が変わった {len(ob)} -> {len(nb)}")
        return problems
    by_idx = {p["idx"]: p for p in plan}
    for i, (a, b) in enumerate(zip(ob, nb)):
        if a == b:
            if i in by_idx:
                problems.append(f"[{i}] 戻したはずが変わっていない")
            continue
        p = by_idx.get(i)
        if p is None:
            problems.append(f"[{i}] 計画に無い位置が変わった")
            continue
        if b.get("role") != "assistant" or a.get("role") != "assistant":
            problems.append(f"[{i}] assistant ではない")
        if b.get("content") != p["new"]:
            problems.append(f"[{i}] 本文が計画と違う")
        if {k: v for k, v in a.items() if k != "content"} != {k: v for k, v in b.items() if k != "content"}:
            problems.append(f"[{i}] 本文以外の項目が変わった")
    return problems


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true", help="実際に書き換える（既定はドライラン）")
    ap.add_argument("--sample", type=int, default=0, help="変わる中身をこの件数だけ表示する")
    ap.add_argument("--state", default=str(STATE_PATH),
                    help="対象の context_state.json（生ログは常にこのリポジトリの workspace/logs/full を見る）")
    args = ap.parse_args()

    state_path = Path(args.state)
    if not state_path.exists():
        print(f"対象が見つかりません: {state_path}")
        return 1
    if args.apply and _server_alive():
        print(f"{SERVER_PORT} 番が応答しています（サーバ稼働中）。停止してから --apply してください。")
        return 2

    stat_before = state_path.stat()
    raw_text = state_path.read_text(encoding="utf-8")
    state = json.loads(raw_text)
    history = state.get("conversation_history", [])
    plan, stat = build_plan(history)

    before = sum(count_text_tokens(p["old"]) for p in plan)
    after = sum(count_text_tokens(p["new"]) for p in plan)
    print("内訳: " + " / ".join(f"{k} {v}" for k, v in stat.most_common()))
    print(f"戻す {len(plan)} 件 / トークン {before:,} → {after:,}（{after - before:+,}）")

    for p in sorted(plan, key=lambda x: count_text_tokens(x["new"]) / max(1, count_text_tokens(x["old"])))[:args.sample]:
        print(f"\n--- {p['date']} {p['time']}  {count_text_tokens(p['old'])} → {count_text_tokens(p['new'])} tok")
        print("[いま] " + p["old"][:200].replace("\n", " | "))
        print("[原文] " + p["new"][:200].replace("\n", " | "))

    if not args.apply:
        print("\n（ドライラン。--apply で書き換え）")
        return 0
    if not plan:
        print("戻す対象がないので何もしません。")
        return 0

    # 読み込みから書き込みまでの間にファイルが変わっていないか（稼働中の保存に上書きしない）
    now_stat = state_path.stat()
    if (now_stat.st_mtime_ns, now_stat.st_size) != (stat_before.st_mtime_ns, stat_before.st_size):
        print("読み込み後にファイルが変わりました。何も書き換えずに中止します。")
        return 5
    if _server_alive():
        print("処理中にサーバが起動しました。何も書き換えずに中止します。")
        return 2

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup = state_path.with_name(state_path.name + f".before_restore_{stamp}")
    backup.write_text(raw_text, encoding="utf-8")   # 計画を立てた時点の中身をそのまま控える

    for p in plan:
        msg = history[p["idx"]]
        if msg.get("role") != "assistant" or msg.get("content") != p["old"]:
            print(f"対象がずれています（idx {p['idx']} / {p['date']} {p['time']}）。何も書き換えずに中止します。")
            backup.unlink(missing_ok=True)
            return 3
        msg["content"] = p["new"]

    # 一時ファイル名はサーバの save_state（context_state.json.tmp）と衝突させない
    tmp = state_path.with_name(state_path.name + f".restore_{stamp}.tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, state_path)

    problems = _verify(json.loads(raw_text), json.loads(state_path.read_text(encoding="utf-8")), plan)
    if problems:
        print("\n検査で問題が見つかりました。控えから戻してください:")
        for p_ in problems[:10]:
            print("  ", p_)
        print(f"  控え: {backup}")
        return 4

    print(f"\n書き換えました（{len(plan)} 件・assistant 本文のみ）。控え: {backup}")
    print("検査: メッセージ数・ロール・本文以外の項目・他のキーは不変。"
          "変わったのは計画どおりの位置で、中身も計画どおり。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
