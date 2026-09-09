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


def test_stale_summary_view_not_counted():
    """summary_v2 のビューは memory/layer1.md 経由で届くので、_summary_view に残った文字列は
    送られない。送らないものは数えない（total は get_token_count と一致し続ける）。"""
    ctx = _make_ctx(_summary_view="【これまでの会話の要約】\n2026-08-01 …\n2026-08-02 …")
    b = ctx.get_token_breakdown()
    assert "summary_view" not in b
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
              "layer0", "layer0_turns", "layer1", "layer2", "other", "system_files"):
        assert k in u, k
    assert u["used"] == ctx.get_token_count()
    assert u["used"] == (u["system"] + u["tools"] + u["raw"] + u["layer0"]
                         + u["layer1"] + u["layer2"] + u["other"])


def test_empty_history_first_message_is_assistant():
    """先頭が user でない変則履歴でも落ちず、Raw 側に数える。"""
    ctx = _make_ctx(conversation_history=[{"role": "assistant", "content": "先頭"}])
    b = ctx.get_token_breakdown()
    assert b["raw"] == count_message_tokens(ctx.conversation_history[0])
    assert b["layer0"] == 0 and b["layer0_turns"] == 0



def test_system_files_follow_config_order():
    """システムプロンプトのファイル別内訳は、config の system_prompts → boot_memories の順に
    ファイル名そのままで並ぶ（layer1.md 等の特別扱いは無い）。和は system と数トークン差。"""
    import tempfile
    from memory.manager import MemoryManager

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "prompts").mkdir()
        (root / "prompts" / "TOP.md").write_text("{{agent_name}} への約束。", encoding="utf-8")
        (root / "prompts" / "TOOLS.md").write_text("道具の説明。" * 5, encoding="utf-8")
        ws = root / "workspace"
        (ws / "memory").mkdir(parents=True)
        (ws / "SOUL.md").write_text("私は{{agent_name}}。", encoding="utf-8")
        (ws / "memory" / "layer1.md").write_text("2026-09-01 街へ行った。\n" * 30, encoding="utf-8")

        ctx = _make_ctx()
        ctx.config = {
            "system_prompts": {"directory": str(root / "prompts"), "files": ["TOP.md", "TOOLS.md"]},
            "boot_memories": ["SOUL.md", "memory/layer1.md", "missing.md"],
            "profile": {"agent": {"name": "柚月"}, "user": {"honorific": "ご主人様"}},
        }
        ctx.memory = MemoryManager(str(ws))
        ctx._rebuild_all()

        names = [f["name"] for f in ctx.system_files]
        assert names == ["TOP.md", "TOOLS.md", "SOUL.md", "memory/layer1.md", "missing.md"], names
        # プレースホルダ置換後の本文を数えている（{{agent_name}} が残っていない）
        assert "{{agent_name}}" not in ctx.system_top and "柚月" in ctx.system_top
        assert "私は柚月。" in ctx.system_memories
        # 結合結果は「従来の load_boot_memories → 置換」と同一（送る文字列を変えていない）
        from core.config_loader import apply_prompt_placeholders
        assert ctx.system_memories == apply_prompt_placeholders(
            ctx.memory.load_boot_memories(ctx.config["boot_memories"]), "柚月", "ご主人様")
        for f in ctx.system_files:
            assert f["tokens"] > 0, f
        b = ctx.get_token_breakdown()
        assert b["system_files"] == ctx.system_files
        assert b["system_files"] is not ctx.system_files  # 呼び出し側が壊せないようコピー
        total_files = sum(f["tokens"] for f in b["system_files"])
        # 本文の和 ≦ system（メッセージ overhead と区切りぶんだけ system が大きい）
        assert total_files <= b["system"] < total_files + 40, (total_files, b["system"])
        assert "system_files" in ctx.get_token_usage()


def test_system_files_absent_when_built_without_init():
    """__init__ を通さず組んだ ctx（既存テストの流儀）でも落ちず、空リストを返す。"""
    ctx = _make_ctx()
    assert ctx.get_token_breakdown()["system_files"] == []


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
