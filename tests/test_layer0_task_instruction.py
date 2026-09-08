# -*- coding: utf-8 -*-
"""
Layer0 圧縮でスケジュールタスクの指示書本文を畳む処理のテスト。

背景: 毎日のタスクは同じ指示書ファイルの全文が毎回 <task_notice> に貼られるため、
Layer0 済み履歴の user 側に同一文章が何十部も並んでいた（実測 108k tok / 46日）。
agent._fold_repeated_task_instruction は「履歴の後ろ側に一字一句同じ本文が残っているか」
だけで畳むかを決める（中身の要否は判断しない）。ここではその境界条件を確認する。

実行: venv\\Scripts\\python.exe tests\\test_layer0_task_instruction.py
（pytest からも実行可・ネットワーク不要・柚月のデータには触れない）
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.agent import Agent  # noqa: E402

INSTRUCTION_A = "# 毎朝のニュースレビュー\n\n## 目的\n私が毎朝ニュースを見て、感想を書く。"
INSTRUCTION_B = "# 毎朝のニュースレビュー\n\n## 目的\n私が毎朝ニュースを見て、感想を書く。\n## 追記\n手順を変えた。"
NOTE = Agent._TASK_INSTRUCTION_FOLDED_NOTE


def _notice_new(instruction: str, when: str = "2026-09-04 08:00") -> str:
    """scheduler が組み立てる通知（新形式・タグ付き）と同じ形。"""
    body = (
        "【スケジュールタスク自動実行】\n"
        "タスク名: 毎朝のニュースレビュー\n"
        f"実行時刻: {when} JST\n"
        "\n"
        "以下の指示書に従って行動してください:\n\n"
        f"<task_instruction>\n---\n{instruction}\n---\n</task_instruction>"
    )
    return f"[SYSTEM]\n2026年09月04日（金） 08:00\n現在22.0℃ 晴れ\n現在の空: 晴れ\n<task_notice>\n{body}\n</task_notice>"


def _notice_legacy(instruction: str, when: str = "2026-09-03 08:00") -> str:
    """タグ導入前の scheduler が組み立てていた通知（旧形式）。"""
    body = (
        "【スケジュールタスク自動実行】\n"
        "タスク名: 毎朝のニュースレビュー\n"
        f"実行時刻: {when} JST\n"
        "\n"
        "以下の指示書に従って行動してください:\n\n"
        f"---\n{instruction}\n---"
    )
    return f"[SYSTEM]\n2026年09月03日（木） 08:00\n現在22.0℃ 晴れ\n現在の空: 晴れ\n<task_notice>\n{body}\n</task_notice>"


class _Ctx:
    """ContextBuilder のうち _fold_repeated_task_instruction が触る部分だけの代役。"""

    def __init__(self, history):
        self.conversation_history = history

    @staticmethod
    def _get_text_from_content(content):
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            return "\n".join(p.get("text", "") for p in content if isinstance(p, dict))
        return str(content)


def _agent(history) -> Agent:
    """__init__（LLM・MemoryManager 依存）を通さず素の状態を組む。"""
    a = Agent.__new__(Agent)
    a.context = _Ctx(history)
    return a


def _u(text):
    return {"role": "user", "content": text}


def _a(text="了解しました。"):
    return {"role": "assistant", "content": text}


PASSED = 0


def check(cond, label):
    global PASSED
    if not cond:
        raise AssertionError(label)
    PASSED += 1


def test_folds_when_same_body_exists_later():
    hist = [_u(_notice_new(INSTRUCTION_A, "2026-09-03 08:00")), _a(),
            _u(_notice_new(INSTRUCTION_A, "2026-09-04 08:00")), _a()]
    out = _agent(hist)._format_user_for_layer0(hist[0]["content"], start_idx=0)
    check(NOTE in out, "後ろに同じ本文があれば畳む")
    check("私が毎朝ニュースを見て" not in out, "畳んだ部に本文が残っていない")
    check("タスク名: 毎朝のニュースレビュー" in out, "見出し（タスク名）は残る")
    check("実行時刻: 2026-09-03 08:00 JST" in out, "見出し（実行時刻）は残る")
    check(out.startswith("2026-09-04 08:00\n22.0℃ 晴れ\ntask: "), "日時・天気・task: の整形は従来どおり")
    check(out.rstrip().endswith("<!-- layer0 -->"), "マーカーは付く")


def test_keeps_when_no_later_copy():
    hist = [_u(_notice_new(INSTRUCTION_A)), _a(), _u("<moonbeat_instruction>x</moonbeat_instruction>"), _a()]
    out = _agent(hist)._format_user_for_layer0(hist[0]["content"], start_idx=0)
    check(NOTE not in out, "後ろに無ければ畳まない")
    check("私が毎朝ニュースを見て" in out, "本文は全文残る（最後の1部）")


def test_keeps_when_later_copy_is_different_version():
    hist = [_u(_notice_new(INSTRUCTION_A)), _a(), _u(_notice_new(INSTRUCTION_B)), _a()]
    out = _agent(hist)._format_user_for_layer0(hist[0]["content"], start_idx=0)
    check(NOTE not in out, "指示書を書き換えた版は一字一句一致しないので残す")
    check("私が毎朝ニュースを見て" in out, "旧版の全文が残る")


def test_earlier_copy_does_not_count():
    hist = [_u(_notice_new(INSTRUCTION_A)), _a(), _u(_notice_new(INSTRUCTION_A)), _a()]
    out = _agent(hist)._format_user_for_layer0(hist[2]["content"], start_idx=2)
    check(NOTE not in out, "前にしか無い場合は畳まない（最新の1部は必ず全文）")


def test_legacy_format_folds_against_new_format():
    hist = [_u(_notice_legacy(INSTRUCTION_A)), _a(), _u(_notice_new(INSTRUCTION_A)), _a()]
    out = _agent(hist)._format_user_for_layer0(hist[0]["content"], start_idx=0)
    check(NOTE in out, "旧形式の通知も、新形式の後続と本文が同じなら畳める（移行期）")
    check("私が毎朝ニュースを見て" not in out, "旧形式でも本文が残らない")


def test_legacy_format_kept_alone():
    hist = [_u(_notice_legacy(INSTRUCTION_A)), _a()]
    out = _agent(hist)._format_user_for_layer0(hist[0]["content"], start_idx=0)
    check("私が毎朝ニュースを見て" in out, "旧形式でも後ろに無ければ全文")


def test_no_start_idx_keeps_full():
    hist = [_u(_notice_new(INSTRUCTION_A)), _a(), _u(_notice_new(INSTRUCTION_A)), _a()]
    out = _agent(hist)._format_user_for_layer0(hist[0]["content"])
    check(NOTE not in out and "私が毎朝ニュースを見て" in out, "start_idx 無しなら従来どおり全文")


def test_later_layer0_turn_with_full_body_counts():
    """後ろの部が既に Layer0 化されていても、全文のまま残っているなら同一とみなす。"""
    later_l0 = f"2026-09-04 08:00\n22.0℃ 晴れ\ntask: 【スケジュールタスク自動実行】\n---\n{INSTRUCTION_A}\n---\n<!-- layer0 -->"
    hist = [_u(_notice_new(INSTRUCTION_A)), _a(), _u(later_l0), _a()]
    out = _agent(hist)._format_user_for_layer0(hist[0]["content"], start_idx=0)
    check(NOTE in out, "Layer0 済みの全文コピーも後続として数える")


def test_folded_note_does_not_chain():
    """後ろの部が畳まれた参照だけなら、本文は「残っている」と言えないので畳まない。"""
    later_folded = f"2026-09-04 08:00\n22.0℃ 晴れ\ntask: 【スケジュールタスク自動実行】\n{NOTE}\n<!-- layer0 -->"
    hist = [_u(_notice_new(INSTRUCTION_A)), _a(), _u(later_folded), _a()]
    out = _agent(hist)._format_user_for_layer0(hist[0]["content"], start_idx=0)
    check(NOTE not in out and "私が毎朝ニュースを見て" in out, "参照しか無い後続では畳まない")


def test_quote_in_other_kind_does_not_count():
    """同じ本文がご主人様の発言や街の出来事に引用されていても、それは一致相手にしない
    （Codex 指摘: 一致相手をタスク通知に限定していないと最新の通知まで畳み得る）。"""
    quoted_user = f"<user_message>これ読んで\n---\n{INSTRUCTION_A}\n---</user_message>"
    quoted_city = f"<city_event_notice>---\n{INSTRUCTION_A}\n---</city_event_notice>"
    for later in (quoted_user, quoted_city):
        hist = [_u(_notice_new(INSTRUCTION_A)), _a(), _u(later), _a()]
        out = _agent(hist)._format_user_for_layer0(hist[0]["content"], start_idx=0)
        check(NOTE not in out and "私が毎朝ニュースを見て" in out, "タスク通知以外の引用では畳まない")


def test_scheduler_builds_tagged_notice():
    """scheduler.py の組み立て文字列が本当に <task_instruction> で囲んでいるか（ソース検査）。"""
    src = (Path(__file__).resolve().parent.parent / "core" / "scheduler.py").read_text(encoding="utf-8")
    check('f"<task_instruction>\\n---\\n{instruction}\\n---\\n</task_instruction>"' in src,
          "scheduler が指示書本文をタグで囲んでいる")


def test_other_tags_untouched():
    raw = ("[SYSTEM]\n2026年09月04日（金） 08:00\n現在22.0℃ 晴れ\n現在の空: 晴れ\n"
           "<user_message>おはよう</user_message>\n<system_notice>内部</system_notice>")
    hist = [_u(raw), _a()]
    out = _agent(hist)._format_user_for_layer0(raw, start_idx=0)
    check("user: おはよう" in out and "内部" not in out, "他の種別の整形に影響しない")


if __name__ == "__main__":
    import io
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
    print(f"\n{len(tests) - failed}/{len(tests)} tests, {PASSED} checks passed")
    sys.exit(1 if failed else 0)
