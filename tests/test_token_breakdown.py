# -*- coding: utf-8 -*-
"""
体調タブのコンテキスト内訳（ContextBuilder.get_token_breakdown）のテスト。

背景: 以前は UI が Raw を「Total − 他項目」で逆算しており、System にはデバッグ用
スナップショットの system ロール合計（＝会話要約 Layer1/2 を含む）を使っていたため、
Layer1+Layer2 が二重に数えられて Raw がその分小さく出ていた。合計だけは常に合う。
ここでは「全項目が実測で、和が get_token_count() と一致し、System に要約が混ざらない」
ことを確認する。

実行: venv\\Scripts\\python.exe tests\\test_token_breakdown.py
（pytest からも実行可）
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.context import ContextBuilder            # noqa: E402
from core.tokens import (                          # noqa: E402
    count_message_tokens, count_messages_tokens, count_text_tokens,
)

L0 = "<!-- layer0 -->"


def _make_ctx(**over) -> ContextBuilder:
    """__init__（プロンプトファイル読込・MemoryManager 依存）を通さず素の状態を組む。"""
    ctx = ContextBuilder.__new__(ContextBuilder)
    ctx.system_top = "TOP prompt: 柚月の人格定義とツール説明。"
    ctx.system_memories = "IDENTITY.md / SOUL.md の中身。"
    ctx.system_bottom = ""
    ctx._summary_view = ""
    ctx.summary_layer1 = ""
    ctx.summary_layer2 = ""
    ctx._pending_prefill = ""
    ctx._pending_vital_prompt = ""
    ctx.conversation_history = []
    ctx.keep_recent_images = 4
    ctx.tools_tokens = 123
    ctx.max_tokens = 1_000_000
    for k, v in over.items():
        setattr(ctx, k, v)
    return ctx


def _history():
    return [
        {"role": "user", "content": f"<user_message>圧縮済み1</user_message>\n{L0}"},
        {"role": "assistant", "content": "圧縮済み返答1"},
        {"role": "user", "content": f"<user_message>圧縮済み2</user_message>\n{L0}"},
        {"role": "assistant", "content": "圧縮済み返答2"},
        {"role": "user", "content": "<user_message>生のターン。ツールも呼ぶ</user_message>"},
        {"role": "assistant", "content": None,
         "tool_calls": [{"id": "c1", "type": "function",
                         "function": {"name": "read_file", "arguments": "{\"path\": \"a.md\"}"}}]},
        {"role": "tool", "tool_call_id": "c1", "content": "ファイルの中身" * 20},
        {"role": "assistant", "content": "読みました。"},
        {"role": "user", "content": "<moonbeat_instruction>自由時間</moonbeat_instruction>"},
        {"role": "assistant", "content": "散歩します。"},
    ]


def test_total_matches_get_token_count():
    """内訳の和は build_messages 全体の実測と一致する（逆算していない証拠）。"""
    ctx = _make_ctx(
        conversation_history=_history(),
        summary_layer1="9/1 柚月はXで返信した。\n9/2 バックアップを確認した。",
        summary_layer2="8月: 街での交流が増えた。",
        system_bottom="BOTTOM prompt",
        _pending_prefill="はい、",
    )
    b = ctx.get_token_breakdown()
    assert b["total"] == ctx.get_token_count(), (b, ctx.get_token_count())
    assert b["tools"] == 123


def test_system_excludes_summary():
    """System は TOP＋記憶＋BOTTOM だけ。Layer1/2 を含まない（旧実装のバグ）。"""
    ctx = _make_ctx(
        summary_layer1="長めの Layer1 要約 " * 50,
        summary_layer2="長めの Layer2 要約 " * 50,
        system_bottom="BOTTOM",
    )
    b = ctx.get_token_breakdown()
    expected_system = (
        count_message_tokens({"role": "system", "content": ctx.system_top})
        + count_message_tokens({"role": "system", "content": ctx.system_memories})
        + count_message_tokens({"role": "system", "content": ctx.system_bottom})
    )
    assert b["system"] == expected_system, (b["system"], expected_system)
    assert b["layer1"] == count_text_tokens(ctx.summary_layer1)
    assert b["layer2"] == count_text_tokens(ctx.summary_layer2)
    # 要約の見出し・区切り・メッセージ overhead は other に落ちる（小さい正の値）
    assert 0 < b["other"] < 40, b["other"]
    # 要約が無ければ other は 0
    assert _make_ctx().get_token_breakdown()["other"] == 0


def test_raw_and_layer0_split():
    """履歴は Raw と Layer0 に漏れなく二分される。tool 呼び出し・結果は Raw 側。"""
    ctx = _make_ctx(conversation_history=_history())
    b = ctx.get_token_breakdown()
    hist = ctx.conversation_history
    assert b["raw"] + b["layer0"] == count_messages_tokens(hist)
    assert b["layer0"] == count_messages_tokens(hist[0:4])
    assert b["raw"] == count_messages_tokens(hist[4:])
    assert b["layer0_turns"] == 2
    assert b["raw_turns"] == 2
    # 旧 get_layer0_token_count は本文だけを数えて overhead が漏れていた。今は同じ値。
    assert ctx.get_layer0_token_count() == b["layer0"]


def test_summary_view_counted_separately():
    """summary_v2 のビューは Layer1/2 と別枠。System には混ざらない。"""
    ctx = _make_ctx(_summary_view="【これまでの会話の要約】\n2026-08-01 …\n2026-08-02 …")
    b = ctx.get_token_breakdown()
    assert b["summary_view"] == count_text_tokens(ctx._summary_view)
    assert b["system"] == (
        count_message_tokens({"role": "system", "content": ctx.system_top})
        + count_message_tokens({"role": "system", "content": ctx.system_memories})
    )
    assert b["total"] == ctx.get_token_count()


def test_token_usage_carries_breakdown():
    """UI が引き算せずに済むよう、get_token_usage が内訳キーを全部持つ。"""
    ctx = _make_ctx(conversation_history=_history(), summary_layer1="要約")
    u = ctx.get_token_usage()
    for k in ("used", "max", "ratio", "system", "tools", "raw", "raw_turns",
              "layer0", "layer0_turns", "layer1", "layer2", "summary_view", "other"):
        assert k in u, k
    assert u["used"] == ctx.get_token_count()
    assert u["used"] == (u["system"] + u["tools"] + u["raw"] + u["layer0"]
                         + u["layer1"] + u["layer2"] + u["summary_view"] + u["other"])


def test_empty_history_first_message_is_assistant():
    """先頭が user でない変則履歴でも落ちず、Raw 側に数える。"""
    ctx = _make_ctx(conversation_history=[{"role": "assistant", "content": "先頭"}])
    b = ctx.get_token_breakdown()
    assert b["raw"] == count_message_tokens(ctx.conversation_history[0])
    assert b["layer0"] == 0 and b["layer0_turns"] == 0


if __name__ == "__main__":
    if os.name == "nt":
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_") and callable(f)]
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"ok   {name}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"FAIL {name}: {e!r}")
    print(f"{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)
