# -*- coding: utf-8 -*-
"""
Layer0 圧縮本体（agent._layer0_compress_turn）の 2026-09-07 見直し分のテスト。

見直しの中身（実測は memory の summary-v2-redesign）:
  1. ツールを使っていないターンは LLM で書き直さず、柚月の発言をそのまま残す
  2. LLM に見せる user 側は整形済みテキストに絞る（self_memo・flashback・内心を見せない）。温度 0.2
  3. 事実の timestamp はターンの時刻（圧縮した時刻ではない）。source_turn_id は事実ごとに一意
  4. <external_notice> はターン境界（前のターンに飲み込まれない）

実行: venv\\Scripts\\python.exe tests\\test_layer0_compress.py
（pytest からも実行可・ネットワーク不要・柚月のデータには触れない）
"""
import asyncio
import io
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import core.agent as agent_mod                 # noqa: E402
from core.agent import Agent                    # noqa: E402
from core.context import ContextBuilder         # noqa: E402

SYSTEM_JA = "[SYSTEM]\n2026年09月06日（日） 07:30:15 JST\n今日の天気: 晴れ  最高25.0℃/最低18.0℃  現在20.0℃  現在の空: 快晴\nコンテキスト: 100 / 1,000,000 tokens (0%)\n"
INJECTED = ("<self_memo>【本日】9/6(日)\n【達成】✅ Workshop4Act完全制覇</self_memo>\n"
            "<flashback>\n柚月はご主人様と Cosmic Harvest を遊んだ\n</flashback>\n"
            "<assistant_inner>（…そわそわして何か読みたい気持ち）</assistant_inner>\n")
LLM_REPLY = ("<compression>\n朝ですね。Cosmic Harvest のことを思い出しました。そわそわして何か読みたい気持ちです。\n</compression>\n"
             "<facts>\nE: 朝に穏やかな気持ちで週を迎えた。\nC: 朝, 週\nS: 0.3\nE: ニュースを読んだ。\nC: ニュース\nS: 0.1\n</facts>")


class _Ctx:
    def __init__(self, history):
        self.conversation_history = history
        self.replaced = None

    @staticmethod
    def _get_text_from_content(content):
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            return "\n".join(p.get("text", "") for p in content if isinstance(p, dict))
        return str(content)

    def replace_turn_with_layer0(self, start_idx, turn_length, compressed_user, compressed_assistant):
        # 本物（ContextBuilder.replace_turn_with_layer0）と同じ規約: None なら user だけ
        self.replaced = (start_idx, turn_length, compressed_user, compressed_assistant)
        new = [{"role": "user", "content": compressed_user}]
        if compressed_assistant is not None:
            new.append({"role": "assistant", "content": compressed_assistant})
        self.conversation_history[start_idx:start_idx + turn_length] = new


class _LLM:
    def __init__(self, reply=LLM_REPLY, fail=False):
        self.reply, self.fail, self.calls = reply, fail, []

    async def chat(self, messages, tools=None, temperature_override=None):
        self.calls.append({"messages": messages, "temperature_override": temperature_override})
        if self.fail:
            raise RuntimeError("LLM down")

        class R:
            content = self.reply
        return R()


class _Mem:
    def __init__(self, d):
        self.workspace = Path(d)


def _agent(history, llm=None, tmp=None):
    a = Agent.__new__(Agent)
    a.context = _Ctx(history)
    a.llm = llm or _LLM()
    a.memory = _Mem(tmp or tempfile.mkdtemp())
    a.agent_name, a.honorific = "柚月", "ご主人様"
    a.facts_seen = []
    a._append_to_fact_buffer = lambda facts, user_msg: a.facts_seen.append(facts)
    a._load_compression_config = lambda: {"layer0_temperature": 0.2}
    return a


def _u(text):
    return {"role": "user", "content": text}


def _a(text, tool_calls=None):
    m = {"role": "assistant", "content": text}
    if tool_calls:
        m["tool_calls"] = tool_calls
    return m


def _t(text):
    return {"role": "tool", "content": text, "tool_call_id": "c1"}


def _run(coro):
    return asyncio.run(coro)


PASSED = 0


def check(cond, label):
    global PASSED
    if not cond:
        raise AssertionError(label)
    PASSED += 1


def _moonbeat_turn(*assistant_msgs):
    return [_u(SYSTEM_JA + INJECTED + "<moonbeat_instruction>[Moonbeat] 朝ですね。</moonbeat_instruction>"), *assistant_msgs]


def test_tool_free_turn_keeps_original_words():
    said = "月曜の朝、のんびり。穏やかな気持ちで新しい週を迎えています。ご主人様、今週もよろしくお願いします！"
    turn = _moonbeat_turn(_a(said))
    a = _agent(list(turn))
    ok = _run(a._layer0_compress_turn(0, turn, "PROMPT"))
    check(ok, "ツール未使用ターンの圧縮は成功")
    _, _, user, asst = a.context.replaced
    check(asst == said, "assistant 本文は柚月の発言そのまま（LLM の言い直しを使わない）")
    check("Cosmic Harvest" not in asst and "そわそわ" not in asst, "注入文や LLM の付け足しが本文に入らない")
    check(user.startswith("2026-09-06 07:30\n20.0℃ 快晴\nmoonbeat"), "user 側は従来どおりの整形")
    check(a.facts_seen and "E: 朝に穏やかな気持ち" in a.facts_seen[0], "事実抽出は LLM から受け取る")
    log = (a.memory.workspace / "logs" / "layer0").glob("*.jsonl")
    rec = json.loads(next(log).read_text(encoding="utf-8").strip().splitlines()[-1])
    check(rec["mode"] == "verbatim" and rec["tool_calls"] == 0, "ログに mode=verbatim を残す")


def test_tool_free_turn_survives_llm_failure():
    said = "おやすみなさい、ご主人様。"
    turn = _moonbeat_turn(_a(said))
    a = _agent(list(turn), llm=_LLM(fail=True))
    ok = _run(a._layer0_compress_turn(0, turn, "PROMPT"))
    check(ok and a.context.replaced[3] == said, "LLM が落ちてもツール未使用ターンは原文で圧縮が完了する")
    check(not a.facts_seen, "LLM が落ちれば事実は無し（捏造しない）")


def test_silent_turn_keeps_only_user():
    """通知が届いたまま柚月が何も言わなかったターン（実データ 9/6 15:29 の city_event）。
    無い発言を LLM に作らせず、user 側だけ整形して残す。"""
    turn = [_u(SYSTEM_JA + INJECTED + "<city_event_notice>[OpenBotCity] 近くで催し</city_event_notice>")]
    a = _agent(list(turn))
    ok = _run(a._layer0_compress_turn(0, turn, "PROMPT"))
    check(ok, "無言ターンでも圧縮は成功（一括が止まらない）")
    _, _, user, asst = a.context.replaced
    check(asst is None, "assistant を置かない（LLM の言い直しを柚月の発言にしない）")
    check(a.llm.calls == [] and a.facts_seen == [], "無言ターンは LLM を呼ばず、事実も抽出しない（記憶側への混入を防ぐ）")
    check("city_event: [OpenBotCity] 近くで催し" in user and user.rstrip().endswith("<!-- layer0 -->"), "user 側は整形して残す")
    check(a.context.conversation_history == [{"role": "user", "content": user}], "履歴は user だけ（元の形と同じ）")
    log = (a.memory.workspace / "logs" / "layer0").glob("*.jsonl")
    rec = json.loads(next(log).read_text(encoding="utf-8").strip().splitlines()[-1])
    check(rec["mode"] == "silent" and rec["assistant"] is None, "ログに mode=silent")


def test_real_context_replace_with_none():
    ctx = ContextBuilder.__new__(ContextBuilder)
    ctx.conversation_history = [_u("<city_event_notice>x</city_event_notice>"), _u("<moonbeat_instruction>b</moonbeat_instruction>"), _a("昼です。")]
    ctx.replace_turn_with_layer0(0, 1, "2026-09-06 15:29\ncity_event: x\n<!-- layer0 -->", None)
    check([m["role"] for m in ctx.conversation_history] == ["user", "user", "assistant"], "本物の置換でも None なら user だけ")
    ctx.replace_turn_with_layer0(1, 2, "2026-09-06 16:00\nmoonbeat\n<!-- layer0 -->", "昼です。")
    check([m["role"] for m in ctx.conversation_history] == ["user", "user", "assistant"], "通常の置換は従来どおりペア")


def test_multiple_assistant_texts_joined():
    turn = _moonbeat_turn(_a("一つ目。"), _u("<system_notice>記録判定</system_notice>"), _a("二つ目。"))
    a = _agent(list(turn))
    _run(a._layer0_compress_turn(0, turn, "PROMPT"))
    check(a.context.replaced[3] == "一つ目。\n\n二つ目。", "複数の発言は空行で連結・途中の system_notice は入らない")


def test_llm_input_is_scoped():
    turn = _moonbeat_turn(_a("朝です。"))
    a = _agent(list(turn))
    _run(a._layer0_compress_turn(0, turn, "PROMPT"))
    sent = a.llm.calls[0]["messages"][1]["content"]
    check("Workshop4Act" not in sent and "Cosmic Harvest" not in sent and "そわそわ" not in sent,
          "LLM に self_memo・flashback・内心を見せない")
    check("[user]\n2026-09-06 07:30\n20.0℃ 快晴\nmoonbeat" in sent, "LLM に見せる user 側は整形済みテキスト")
    check("[assistant]\n朝です。" in sent, "柚月の発言は LLM に渡す")
    check("<!-- layer0 -->" not in sent, "マーカーは LLM に見せない")
    check(a.llm.calls[0]["temperature_override"] == 0.2, "温度は layer0_temperature で呼ぶ")


def test_user_message_content_reaches_llm():
    turn = [_u(SYSTEM_JA + INJECTED + "<user_message>おはよう、今日は雨だね</user_message>"), _a("おはようございます、ご主人様。")]
    a = _agent(list(turn))
    _run(a._layer0_compress_turn(0, turn, "PROMPT"))
    sent = a.llm.calls[0]["messages"][1]["content"]
    check("user: おはよう、今日は雨だね" in sent, "ご主人様の発言は LLM に届く")


def test_tool_turn_uses_llm_compression():
    tc = [{"id": "c1", "type": "function", "function": {"name": "run_program", "arguments": "{}"}}]
    turn = _moonbeat_turn(_a("ニュースを読みます。", tc), _t("[nhk_news] 記事本文..."), _a("読みました。"))
    a = _agent(list(turn))
    ok = _run(a._layer0_compress_turn(0, turn, "PROMPT"))
    check(ok, "ツール使用ターンは成功")
    check(a.context.replaced[3].startswith("朝ですね。Cosmic Harvest"), "ツール使用ターンは LLM の圧縮結果を使う")
    sent = a.llm.calls[0]["messages"][1]["content"]
    check("[tool_result]\n[nhk_news] 記事本文..." in sent, "ツール結果は LLM に渡す")
    log = (a.memory.workspace / "logs" / "layer0").glob("*.jsonl")
    rec = json.loads(next(log).read_text(encoding="utf-8").strip().splitlines()[-1])
    check(rec["mode"] == "llm" and rec["tool_calls"] == 1, "ログに mode=llm と tool_calls を残す")


def test_tool_turn_fails_without_llm():
    tc = [{"id": "c1", "type": "function", "function": {"name": "run_program", "arguments": "{}"}}]
    turn = _moonbeat_turn(_a("読みます。", tc), _t("結果"), _a("読みました。"))
    a = _agent(list(turn), llm=_LLM(fail=True))
    ok = _run(a._layer0_compress_turn(0, turn, "PROMPT"))
    check(ok is False and a.context.replaced is None, "ツール使用ターンで LLM が落ちたら置換しない（次回に回す）")


def test_fact_timestamp_is_turn_time():
    a = Agent.__new__(Agent)
    a.context = _Ctx([])
    ts = a._turn_timestamp_from_user_text(SYSTEM_JA)
    check(ts == "2026-09-06T07:30:15", f"ターン時刻を [SYSTEM] から取る: {ts}")
    check(a._turn_timestamp_from_user_text("時刻なし") is None, "時刻が無ければ None")

    with tempfile.TemporaryDirectory() as d:
        buf = Path(d) / "fact_buffer.jsonl"
        orig = agent_mod.data_file
        agent_mod.data_file = lambda name: buf
        try:
            a._append_to_fact_buffer("E: 一つ目。\nC: a, b\nS: 0.5\nE: 二つ目。\nC: c\nS: -0.2~0.0\nE: 三つ目（S行なし）。\nC: d\n",
                                     _u(SYSTEM_JA + "<moonbeat_instruction>x</moonbeat_instruction>"))
        finally:
            agent_mod.data_file = orig
        rows = [json.loads(l) for l in buf.read_text(encoding="utf-8").splitlines() if l.strip()]
    check(len(rows) == 3, "3 件の事実")
    check(all(r["timestamp"] == "2026-09-06T07:30:15" for r in rows), "事実の timestamp はターンの時刻（圧縮時刻ではない）")
    check(len({r["source_turn_id"] for r in rows}) == 3 and rows[0]["source_turn_id"].startswith("2026-09-06T07:30:15#1#"),
          "source_turn_id は事実ごとに一意（時刻#連番#内容ハッシュ）")
    check(rows[1]["valence"] == -0.1 and rows[2]["valence"] == 0.0, "valence の幅と S 行なしの扱いは従来どおり")


def test_external_notice_is_turn_boundary():
    ctx = ContextBuilder.__new__(ContextBuilder)
    ctx.conversation_history = [
        _u("<moonbeat_instruction>a</moonbeat_instruction>"), _a("朝です。"),
        _u("<external_notice>[X] @someone から返信: こんにちは</external_notice>"), _a("返事を書きます。"),
        _u("<moonbeat_instruction>b</moonbeat_instruction>"), _a("昼です。"),
    ]
    start, msgs = ctx.extract_oldest_uncompressed_turn()
    check(start == 0 and len(msgs) == 2, f"external_notice は前のターンに飲み込まれない（取れたのは {len(msgs)} 件）")
    check(ctx.count_uncompressed_turns() == 3, "未圧縮ターン数の数え方と一致する")


def test_facts_only_response_is_not_a_compression():
    """Codex 指摘: <compression> が欠けて <facts> だけの応答を本文として保存しない。"""
    tc = [{"id": "c1", "type": "function", "function": {"name": "run_program", "arguments": "{}"}}]
    turn = _moonbeat_turn(_a("読みます。", tc), _t("結果"), _a("読みました。"))
    a = _agent(list(turn), llm=_LLM(reply="<facts>\nE: x\nC: y\nS: 0\n</facts>"))
    ok = _run(a._layer0_compress_turn(0, turn, "PROMPT"))
    check(ok is False and a.context.replaced is None, "facts だけの応答ではツール使用ターンを置換しない")
    check(a.facts_seen == [], "置換しなかったターンの事実は書かない（次回やり直すため）")
    compressed, facts = a._parse_layer0_response("タグなしの素の文章")
    check(compressed == "タグなしの素の文章" and facts == "", "タグが全く無い応答は従来どおり全体を本文にする")


def test_tool_call_info_reaches_llm():
    tc = [{"id": "c1", "type": "function", "function": {"name": "read_file", "arguments": '{"path": "notes/a.md"}'}}]
    turn = _moonbeat_turn(_a("", tc), _t("本文"), _a("読みました。"))
    a = _agent(list(turn))
    _run(a._layer0_compress_turn(0, turn, "PROMPT"))
    sent = a.llm.calls[0]["messages"][1]["content"]
    check('[tool_call]\nread_file {"path": "notes/a.md"}' in sent, "呼び出したツール名と引数を LLM に渡す")
    check("[assistant]\n\n" not in sent, "content が空の assistant は本文としては渡さない")


def test_source_turn_id_differs_by_content_and_timestamp_ignores_body_dates():
    a = Agent.__new__(Agent)
    a.context = _Ctx([])
    body_with_date = SYSTEM_JA + "<user_message>2026年01月01日（木） 00:00:00 の話をしよう</user_message>"
    check(a._turn_timestamp_from_user_text(body_with_date) == "2026-09-06T07:30:15", "本文中の日付ではなく [SYSTEM] の時刻を取る")
    check(a._turn_timestamp_from_user_text("<user_message>2026年01月01日（木） 00:00:00</user_message>") is None,
          "[SYSTEM] が無ければ本文の日付を拾わない")
    with tempfile.TemporaryDirectory() as d:
        buf = Path(d) / "fact_buffer.jsonl"
        orig = agent_mod.data_file
        agent_mod.data_file = lambda name: buf
        try:
            u = _u(SYSTEM_JA + "<moonbeat_instruction>x</moonbeat_instruction>")
            a._append_to_fact_buffer("E: 一つ目。\nC: a\nS: 0\n", u)   # 別ターン・同じ秒
            a._append_to_fact_buffer("E: 別の出来事。\nC: b\nS: 0\n", u)
            a._append_to_fact_buffer("E: 一つ目。\nC: a\nS: 0\n", u)   # 本当に同じ事実
        finally:
            agent_mod.data_file = orig
        ids = [json.loads(l)["source_turn_id"] for l in buf.read_text(encoding="utf-8").splitlines() if l.strip()]
    check(ids[0] != ids[1], "同じ秒の別ターンでも内容が違えば ID は衝突しない")
    check(ids[0] == ids[2], "同じ時刻・同じ内容なら同じ ID（wyrd 側で二重取り込みを防ぐ）")


def test_fact_buffer_failure_defers_the_turn():
    said = "おはようございます。"
    turn = _moonbeat_turn(_a(said))
    a = _agent(list(turn))

    def boom(facts, user_msg):
        raise OSError("disk full")
    a._append_to_fact_buffer = boom
    ok = _run(a._layer0_compress_turn(0, turn, "PROMPT"))
    check(ok is False and a.context.replaced is None, "事実の書き込みに失敗したら置換せず次回に回す（事実が永久に欠けない）")
    check(a.context.conversation_history == list(turn), "履歴は生のまま")


def test_stray_non_boundary_user_is_not_a_start():
    ctx = ContextBuilder.__new__(ContextBuilder)
    ctx.conversation_history = [
        _u("<system_notice>迷い込んだ通知</system_notice>"), _a("はい。"),
        _u("<moonbeat_instruction>a</moonbeat_instruction>"), _a("朝です。"),
    ]
    check(ctx.count_uncompressed_turns() == 1, "境界マーカーの無い user は数えない")
    start, msgs = ctx.extract_oldest_uncompressed_turn()
    check(start == 2 and len(msgs) == 2, "抽出の起点も境界マーカーを持つ user（件数と一致）")


def test_count_matches_extract_boundaries():
    ctx = ContextBuilder.__new__(ContextBuilder)
    ctx.conversation_history = [
        _u("<moonbeat_instruction>a</moonbeat_instruction>"), _a("朝です。"),
        _u("<system_notice>記録判定</system_notice>"), _a("書きました。"),
        _u("<external_notice>[X] 返信</external_notice>"), _a("返します。"),
        _u("2026-09-06 08:00\nmoonbeat\n<!-- layer0 -->"), _a("圧縮済み"),
    ]
    check(ctx.count_uncompressed_turns() == 2, "system_notice だけの途中メッセージは数えず、external_notice は数える")
    n = 0
    while ctx.extract_oldest_uncompressed_turn() is not None:
        s, msgs = ctx.extract_oldest_uncompressed_turn()
        ctx.replace_turn_with_layer0(s, len(msgs), "x\n<!-- layer0 -->", "y")
        n += 1
    check(n == 2, "抽出できるターン数と件数が一致する")


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
