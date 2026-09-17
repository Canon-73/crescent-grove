# -*- coding: utf-8 -*-
"""
scripts/restore_layer0_verbatim.py の対応付けのテスト。

このスクリプトは柚月の会話履歴を書き換えるので、対応付けを間違えると「別のターンの発言」が
記憶に混ざる。Codex レビュー5巡で見つかった誤対応の並びを、そのまま回帰テストにしてある。

対応付けの考え方: 生ログを「ターン」に切らず、履歴の隣り合うターンの時刻で挟んだ範囲を
そのターンの範囲とする（履歴は旧 Layer0 の出力そのものなので、次のターンが始まるまでが
旧 Layer0 の 1 ターン）。

実行: venv\\Scripts\\python.exe tests\\test_restore_layer0_verbatim.py
（pytest からも実行可・ネットワーク不要・柚月のデータには触れない）
"""
import importlib.util
import io
import json
import sys
import tempfile
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_spec = importlib.util.spec_from_file_location("restore_l0", ROOT / "scripts" / "restore_layer0_verbatim.py")
rl = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rl)

PASSED = 0


def check(cond, label):
    global PASSED
    if not cond:
        raise AssertionError(label)
    PASSED += 1


def _dt(hhmmss):
    return datetime.strptime("2026-09-06 " + hhmmss, "%Y-%m-%d %H:%M:%S")


def _recs(items):
    """('HH:MM:SS', 種類, 本文) の列 → 生ログの記録列。"""
    return [(_dt(t), kind, body) for t, kind, body in items]


def _history(turns):
    """('HH:MM', user種別行, assistant本文) → 会話履歴。"""
    h = []
    for t, kind, a in turns:
        h.append({"role": "user", "content": f"2026-09-06 {t}\n20.0℃ 晴れ\n{kind}\n<!-- layer0 -->"})
        h.append({"role": "assistant", "content": a})
    return h


def _plan(history, records):
    orig = rl.load_raw_records
    rl.load_raw_records = lambda: records
    try:
        return rl.build_plan(history)
    finally:
        rl.load_raw_records = orig


MB = "[Moonbeat] おはようございます。好きに過ごしてください。"
REGEN = "[SYSTEM] 自動通知: 前回と似た内容が出力されたため、再生成されました。"


def test_basic_restore():
    hist = _history([("10:00", "moonbeat", "朝ですね。穏やかに過ごしています。ニュースも読みました。"), ("10:30", "moonbeat", "次のターンの言い直し")])
    recs = _recs([("10:00:02", "user_message", MB), ("10:00:09", "assistant_message", "朝ですね。穏やかに過ごしています。"),
                  ("10:30:01", "user_message", MB), ("10:30:08", "assistant_message", "次の原文")])
    plan, stat = _plan(hist, recs)
    check(len(plan) == 1 and plan[0]["new"] == "朝ですね。穏やかに過ごしています。" and plan[0]["idx"] == 1, f"1件戻す: {plan}")
    check(stat["最後のターン（範囲の終わりが決まらない）"] == 1, "最後のターンは範囲が決まらないので触らない")


def test_absorbed_regeneration_notice_is_joined():
    """再生成の通知をまたいだ 2 つの発言は、旧 Layer0 と同じく 1 ターンとしてつなぐ。"""
    hist = _history([("10:00", "moonbeat", "一つ目です。二つ目です。さらに書き足された文。"), ("10:30", "moonbeat", "次")])
    recs = _recs([("10:00:02", "user_message", MB), ("10:00:20", "assistant_message", "一つ目です。"),
                  ("10:00:26", "user_message", REGEN), ("10:00:35", "assistant_message", "二つ目です。"),
                  ("10:30:01", "user_message", MB), ("10:30:08", "assistant_message", "次の原文")])
    plan, _ = _plan(hist, recs)
    check(len(plan) == 1 and plan[0]["new"] == "一つ目です。\n\n二つ目です。", f"つないで戻す: {plan}")


def test_absorbed_external_notice_is_joined():
    """外部からの通知は旧 Layer0 が前のターンへ取り込んでいたので、その返答も含める。"""
    hist = _history([("10:00", "moonbeat", "朝です。返事を書きました。さらに書き足された文。"), ("10:30", "moonbeat", "次")])
    recs = _recs([("10:00:02", "user_message", MB), ("10:00:20", "assistant_message", "朝です。"),
                  ("10:05:00", "user_message", "[X] @someone から返信"), ("10:05:30", "assistant_message", "返事を書きました。"),
                  ("10:30:01", "user_message", MB), ("10:30:08", "assistant_message", "次の原文")])
    plan, _ = _plan(hist, recs)
    check(len(plan) == 1 and plan[0]["new"] == "朝です。\n\n返事を書きました。", f"取り込んだ返答も含める: {plan}")


def test_reply_finishing_in_next_minute_stays_with_its_turn():
    """前のターンの返答が、次のターンの分に入ってから終わった場合。

    範囲の端を「分」にすると、その返答が次のターンへ移り、前のターンからは欠ける。
    端は生ログに実在する始まりの実時刻（秒まで）で挟む。
    """
    hist = _history([("10:00", "moonbeat", "Aの前半です。Aの後半です。書き足し。"), ("10:30", "moonbeat", "Bの返答です。書き足し。"),
                     ("11:00", "moonbeat", "次")])
    recs = _recs([("10:00:02", "user_message", MB), ("10:00:20", "assistant_message", "Aの前半です。"),
                  ("10:30:05", "assistant_message", "Aの後半です。"),
                  ("10:30:20", "user_message", MB), ("10:30:40", "assistant_message", "Bの返答です。"),
                  ("11:00:02", "user_message", MB), ("11:00:10", "assistant_message", "次")])
    plan, _ = _plan(hist, recs)
    got = {p["time"]: p["new"] for p in plan}
    check(got.get("10:00") == "Aの前半です。\n\nAの後半です。", f"A に後半まで含める: {got}")
    check(got.get("10:30") == "Bの返答です。", f"B に A の発言を混ぜない: {got}")


def test_same_second_boundary_keeps_record_order():
    """前のターンの返答と、次のターンの始まりが同じ秒に記録された場合。

    時刻で挟むとどちらが先か決められず、返答が次のターンへ移る。生ログの記録順
    （読み込んだ並び）で挟むので、記録された順のとおりに分かれる。
    """
    hist = _history([("10:00", "moonbeat", "Aの前半です。Aの後半です。書き足し。"), ("10:30", "moonbeat", "Bの返答です。書き足し。"),
                     ("11:00", "moonbeat", "次")])
    recs = _recs([("10:00:02", "user_message", MB), ("10:00:20", "assistant_message", "Aの前半です。"),
                  ("10:30:20", "assistant_message", "Aの後半です。"),
                  ("10:30:20", "user_message", MB), ("10:30:40", "assistant_message", "Bの返答です。"),
                  ("11:00:02", "user_message", MB), ("11:00:10", "assistant_message", "次")])
    plan, _ = _plan(hist, recs)
    got = {p["time"]: p["new"] for p in plan}
    check(got.get("10:00") == "Aの前半です。\n\nAの後半です。", f"同じ秒でも A に残る: {got}")
    check(got.get("10:30") == "Bの返答です。", f"B に A の発言を混ぜない: {got}")


def test_clock_going_backwards_is_skipped():
    """記録順と時刻の向きが食い違う範囲（時計が巻き戻った箇所）は触らない。

    生ログは記録順のまま読む（時刻で並べ替えない）ので、こういう並びが来ても記録順は
    保たれる。ただし範囲の意味が疑わしいので、その範囲は戻さない。
    """
    hist = _history([("10:00", "moonbeat", "Aの前半です。Aの後半です。書き足し。"), ("10:30", "moonbeat", "Bの返答です。書き足し。"),
                     ("11:00", "moonbeat", "次")])
    recs = _recs([("10:00:02", "user_message", MB), ("10:00:20", "assistant_message", "Aの前半です。"),
                  ("10:30:20", "assistant_message", "Aの後半です。"),
                  ("10:30:19", "user_message", MB), ("10:30:40", "assistant_message", "Bの返答です。"),
                  ("11:00:02", "user_message", MB), ("11:00:10", "assistant_message", "次")])
    plan, stat = _plan(hist, recs)
    check(stat["記録順と時刻が食い違う"] >= 1, f"時刻の逆転を検出する: {dict(stat)}")
    check(all(p["time"] != "10:00" for p in plan), f"逆転をまたぐ範囲は戻さない: {plan}")


def test_window_crossing_midnight_is_skipped():
    """日付の変わり目に近い範囲は触らない。

    生ログは日別ファイルなので、日付をまたいで時計が巻き戻ると、連結したときに記録の
    前後が入れ替わる。入れ替わったことは時刻からは見分けられないので、変わり目をまたぐ
    範囲と、その次のターンまでに日付が変わる範囲をまとめて外す。"""
    hist = [{"role": "user", "content": "2026-09-05 23:50\n20.0℃ 晴れ\nmoonbeat\n<!-- layer0 -->"},
            {"role": "assistant", "content": "夜の旧本文"},
            {"role": "user", "content": "2026-09-06 00:00\n20.0℃ 晴れ\nmoonbeat\n<!-- layer0 -->"},
            {"role": "assistant", "content": "深夜の原文です。書き足された文。"},
            {"role": "user", "content": "2026-09-06 00:30\n20.0℃ 晴れ\nmoonbeat\n<!-- layer0 -->"},
            {"role": "assistant", "content": "次"}]
    d = lambda s: datetime.strptime(s, "%Y-%m-%d %H:%M:%S")
    recs = [(d("2026-09-05 23:50:02"), "user_message", MB), (d("2026-09-05 23:50:20"), "assistant_message", "夜の原文"),
            (d("2026-09-06 00:00:02"), "user_message", MB), (d("2026-09-06 00:00:20"), "assistant_message", "深夜の原文です。"),
            (d("2026-09-06 00:30:02"), "user_message", MB), (d("2026-09-06 00:30:20"), "assistant_message", "次の原文")]
    plan, stat = _plan(hist, recs)
    check(stat["日付の変わり目に近い"] >= 1, f"日付の変わり目を検出: {dict(stat)}")
    check(all(p["time"] != "23:50" for p in plan), f"変わり目をまたぐ範囲は戻さない: {plan}")


def test_tool_turn_is_skipped():
    hist = _history([("10:00", "moonbeat", "ツールを使った本文"), ("10:30", "moonbeat", "次")])
    recs = _recs([("10:00:02", "user_message", MB), ("10:00:10", "assistant_message", "読みます。"),
                  ("10:00:11", "tool_call", "run_program"), ("10:00:20", "assistant_message", "読みました。"),
                  ("10:30:01", "user_message", MB), ("10:30:08", "assistant_message", "次")])
    plan, stat = _plan(hist, recs)
    check(plan == [] and stat["ツールを使ったターン"] == 1, "ツールを使ったターンは戻さない")


def test_unchanged_is_skipped():
    hist = _history([("10:00", "moonbeat", "同じ本文"), ("10:30", "moonbeat", "次")])
    recs = _recs([("10:00:02", "user_message", MB), ("10:00:10", "assistant_message", "同じ本文"),
                  ("10:30:01", "user_message", MB), ("10:30:08", "assistant_message", "次")])
    plan, stat = _plan(hist, recs)
    check(plan == [] and stat["既に原文と同じ"] == 1, "既に原文と同じなら触らない")


# --- Codex レビューで挙がった誤対応の並び ---

def test_missing_history_turn_inside_window():
    """履歴に無いターン（UUID の貼り付けなど）が範囲の途中にあれば、範囲を信用しない。

    ここを見逃すと、別のターンの発言まで前のターンの本文に混ぜてしまう。
    """
    hist = _history([("10:00", "moonbeat", "旧本文"), ("10:30", "moonbeat", "次")])
    recs = _recs([("10:00:02", "user_message", MB), ("10:00:20", "assistant_message", "朝です。"),
                  ("10:10:00", "user_message", "d7ebcc40-a458-470d-bcd5-0f5ac49fce14"),
                  ("10:10:30", "assistant_message", "別のターンの発言"),
                  ("10:30:01", "user_message", MB), ("10:30:08", "assistant_message", "次")])
    plan, stat = _plan(hist, recs)
    check(plan == [] and stat["範囲の途中に別のターンがある"] == 1, f"別のターンが挟まれば触らない: {plan}")


def test_non_monotonic_history_is_skipped():
    """旧居の会話を履歴の別の位置に差し込んだ日のように、時刻が前へ戻る箇所。"""
    hist = _history([("21:00", "moonbeat", "A"), ("20:28", "moonbeat", "B"), ("21:30", "moonbeat", "C")])
    recs = _recs([("20:28:00", "user_message", MB), ("20:28:10", "assistant_message", "b"),
                  ("21:00:00", "user_message", MB), ("21:00:10", "assistant_message", "a"),
                  ("21:30:00", "user_message", MB), ("21:30:10", "assistant_message", "c")])
    plan, stat = _plan(hist, recs)
    check(stat["履歴の時刻が前へ戻っている"] == 1 and plan == [], f"時刻が戻る箇所は触らない: {plan}")


def test_start_must_exist_in_raw_log():
    """履歴の時刻に生ログの始まりが無ければ触らない（1 分ずれ・記録漏れ）。"""
    hist = _history([("10:00", "moonbeat", "旧本文"), ("10:30", "moonbeat", "次")])
    recs = _recs([("10:01:00", "user_message", MB), ("10:01:10", "assistant_message", "原文"),
                  ("10:30:01", "user_message", MB), ("10:30:08", "assistant_message", "次")])
    plan, stat = _plan(hist, recs)
    check(plan == [] and stat["生ログに始まりが見当たらない"] == 1, "始まりが無ければ触らない")


def test_ambiguous_start_minute_is_skipped():
    """同じ分に始まりが 2 つあれば、どちらが相手か決められないので触らない。"""
    hist = _history([("10:00", "moonbeat", "旧本文"), ("10:30", "moonbeat", "次")])
    recs = _recs([("10:00:02", "user_message", MB), ("10:00:10", "assistant_message", "一つ目"),
                  ("10:00:40", "user_message", MB), ("10:00:50", "assistant_message", "二つ目"),
                  ("10:30:01", "user_message", MB), ("10:30:08", "assistant_message", "次")])
    plan, stat = _plan(hist, recs)
    check(plan == [] and stat["生ログに始まりが見当たらない"] == 1, "同じ分に始まりが並べば触らない")


def test_kind_mismatch_is_skipped():
    hist = _history([("10:00", "user: ただいま", "旧本文"), ("10:30", "moonbeat", "次")])
    recs = _recs([("10:00:02", "user_message", MB), ("10:00:10", "assistant_message", "原文"),
                  ("10:30:01", "user_message", MB), ("10:30:08", "assistant_message", "次")])
    plan, stat = _plan(hist, recs)
    check(plan == [] and stat["種別が食い違う"] == 1, "種別が食い違えば触らない")


def test_human_message_kind_allows_romaji_log():
    """ご主人様の入力は、変換途中の綴りがログに残ることがある。種別が合えば戻す。"""
    hist = _history([("10:00", "user: ただいま！", "おかえりなさい！今日もお疲れさまでした。"), ("10:30", "moonbeat", "次")])
    recs = _recs([("10:00:02", "user_message", "tadaima!"), ("10:00:10", "assistant_message", "おかえりなさい！"),
                  ("10:30:01", "user_message", MB), ("10:30:08", "assistant_message", "次")])
    plan, _ = _plan(hist, recs)
    check(len(plan) == 1 and plan[0]["new"] == "おかえりなさい！", "綴りが違っても種別が合えば戻す")


def test_low_overlap_is_skipped():
    """いまの本文と言葉がまったく重ならない原文は戻さない。

    いまの本文は旧 Layer0 がその原文から作ったものなので、書き直されていても言葉はかなり残る
    （実測: 正しい対の中央値 0.60、無関係なターンとの突き合わせは 0.06）。重ならないなら
    対応付けが何らかの理由でずれている疑いがあるので、時刻の理屈とは別に落とす。
    """
    hist = _history([("10:00", "moonbeat", "まったく関係のない語ばかりの本文"), ("10:30", "moonbeat", "次")])
    recs = _recs([("10:00:02", "user_message", MB), ("10:00:10", "assistant_message", "朝ですね。穏やかに過ごしています。"),
                  ("10:30:01", "user_message", MB), ("10:30:08", "assistant_message", "次")])
    plan, stat = _plan(hist, recs)
    check(plan == [] and stat["いまの本文と言葉が重ならない"] == 1, f"重ならなければ戻さない: {plan}")
    check(rl.overlap("朝ですね。穏やかに過ごしています。", "朝ですね。穏やかに過ごしています。ニュースも読みました。") > 0.9,
          "言い直しでも言葉が残っていれば高い")
    check(rl.overlap("まったく別の話", "朝ですね。穏やかに過ごしています。") == 0.0, "無関係なら 0")


def test_corroborates_kinds():
    check(rl.corroborates("2026-09-06 10:00\nmoonbeat\n<!-- layer0 -->", MB), "moonbeat が一致")
    check(not rl.corroborates("2026-09-06 10:00\nmoonbeat\n<!-- layer0 -->", "ただいま"), "moonbeat と人の発言は不一致")
    check(rl.corroborates("2026-09-06 10:00\ntask: 【スケジュール\n<!-- layer0 -->", "【スケジュールタスク自動実行】"), "task が一致")
    check(rl.corroborates("2026-09-06 10:00\ncity_event: [OpenBotCity] x\n<!-- layer0 -->", "[OpenBotCity] x"), "city_event が一致")
    check(not rl.corroborates("2026-09-06 10:00\n<!-- layer0 -->", MB), "種別が読めなければ戻さない")


# --- 書き換え後の検査と安全装置 ---

def test_verify_catches_tampering():
    hist = _history([("10:00", "moonbeat", "旧"), ("10:05", "moonbeat", "旧2")])
    plan = [{"idx": 1, "date": "2026-09-06", "time": "10:00", "old": "旧", "new": "新"}]
    good = {"conversation_history": hist, "summary_layer1": "x"}
    after = json.loads(json.dumps(good))
    after["conversation_history"][1]["content"] = "新"
    check(rl._verify(good, after, plan) == [], "計画どおりなら問題なし")

    bad = json.loads(json.dumps(after))
    bad["conversation_history"][3]["content"] = "勝手に変えた"
    check(any("計画に無い位置" in p for p in rl._verify(good, bad, plan)), "計画外の変更を検出")

    bad2 = json.loads(json.dumps(after))
    bad2["conversation_history"][1]["content"] = "別の中身"
    check(any("本文が計画と違う" in p for p in rl._verify(good, bad2, plan)), "本文の食い違いを検出")

    bad3 = json.loads(json.dumps(after))
    bad3["summary_layer1"] = "y"
    check(any("キー" in p for p in rl._verify(good, bad3, plan)), "他のキーの変化を検出")

    bad4 = json.loads(json.dumps(after))
    del bad4["conversation_history"][3]
    check(any("メッセージ数" in p for p in rl._verify(good, bad4, plan)), "メッセージ数の変化を検出")


def test_apply_refuses_while_server_alive():
    import socket
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]
    old_port = rl.SERVER_PORT
    rl.SERVER_PORT = port
    try:
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "state.json"
            p.write_text(json.dumps({"conversation_history": []}, ensure_ascii=False), encoding="utf-8")
            sys.argv = ["rl", "--apply", "--state", str(p)]
            check(rl.main() == 2, "サーバが応答している間は --apply を拒否する")
    finally:
        rl.SERVER_PORT = old_port
        srv.close()


if __name__ == "__main__":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in tests:
        try:
            fn()
            print(f"ok   {fn.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"FAIL {fn.__name__}: {e}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"ERROR {fn.__name__}: {type(e).__name__}: {e}")
    print(f"\n{len(tests) - failed}/{len(tests)} tests, {PASSED} checks passed")
    sys.exit(1 if failed else 0)
