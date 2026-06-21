"""
data/context_state.json の会話履歴に残る「入れ子タグ」を解消するワンショット変換スクリプト。

対象: <user_message> / <moonbeat_instruction> の内側に紛れ込んだ
      付加ブロック系タグ（flashback / tips / note_fragment）を、
      タグの外側へ兄弟要素として取り出す（入れ子防止）。

使い方:
    python scripts/unnest_context_state.py --dry-run   # 検証のみ（書き込みなし）
    python scripts/unnest_context_state.py             # 実変換（バックアップ作成）

注意: server.py が稼働中に実行しないこと（メモリ上の状態で上書きされ無意味）。
"""
import json
import re
import sys
import shutil
from datetime import datetime
from pathlib import Path

# 付加ブロック系タグ（core/context.py の _APPENDIX_TAGS と一致させる）
APPENDIX_TAGS = ("flashback", "tips", "note_fragment")

# 本文タグ（外側）
OUTER_TAGS = ("user_message", "moonbeat_instruction")

_appendix_re = re.compile(
    r"<(" + "|".join(APPENDIX_TAGS) + r")>.*?</\1>", re.DOTALL
)
_outer_re = re.compile(
    r"<(" + "|".join(OUTER_TAGS) + r")>\n?(.*?)\n?</\1>", re.DOTALL
)


def _unnest_text(text: str) -> str:
    """本文タグ内に入れ子になった付加ブロックを外へ出す。変更が無ければ原文を返す。"""

    def repl(m: "re.Match") -> str:
        tag = m.group(1)
        inner = m.group(2)
        found = [am.group(0) for am in _appendix_re.finditer(inner)]
        if not found:
            return m.group(0)
        cleaned = _appendix_re.sub("", inner).strip()
        rebuilt = f"<{tag}>\n{cleaned}\n</{tag}>"
        for block in found:
            rebuilt += "\n\n" + block
        return rebuilt

    return _outer_re.sub(repl, text)


def _iter_text_parts(content):
    """content（str または マルチモーダルのlist）からテキスト部分を (setter, text) で列挙。"""
    if isinstance(content, str):
        yield ("str", None, content)
    elif isinstance(content, list):
        for i, part in enumerate(content):
            if isinstance(part, dict) and part.get("type") == "text":
                yield ("list", i, part.get("text", ""))


def main():
    dry_run = "--dry-run" in sys.argv
    path = Path("data/context_state.json")

    data = json.loads(path.read_text(encoding="utf-8"))
    history = data.get("conversation_history", [])

    changed_msgs = 0
    for msg in history:
        content = msg.get("content")
        if isinstance(content, str):
            new = _unnest_text(content)
            if new != content:
                msg["content"] = new
                changed_msgs += 1
        elif isinstance(content, list):
            touched = False
            for part in content:
                if isinstance(part, dict) and part.get("type") == "text":
                    new = _unnest_text(part.get("text", ""))
                    if new != part.get("text", ""):
                        part["text"] = new
                        touched = True
            if touched:
                changed_msgs += 1

    print(f"history件数: {len(history)} / 変換対象メッセージ: {changed_msgs}")

    # 変換後にもう一度入れ子が残っていないか検証
    # （本文タグの内側に付加ブロックが残っているかを、外側ブロック単位で正確に確認）
    def count_remaining_nests(text: str) -> int:
        return sum(
            1 for m in _outer_re.finditer(text) if _appendix_re.search(m.group(2))
        )

    remain = 0
    for msg in history:
        content = msg.get("content")
        if isinstance(content, str):
            remain += count_remaining_nests(content)
        elif isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and part.get("type") == "text":
                    remain += count_remaining_nests(part.get("text", ""))
    print(f"変換後に残る入れ子: {remain} 件")

    if dry_run:
        print("[dry-run] 書き込みは行いませんでした。")
        return

    if changed_msgs == 0:
        print("変更なし。書き込みをスキップします。")
        return

    # JSON妥当性は load 済みなので保証。バックアップしてから書き込み。
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup = path.with_name(f"context_state.backup_{ts}.json")
    shutil.copy2(path, backup)
    print(f"バックアップ作成: {backup}")

    path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print("変換を書き込みました。")


if __name__ == "__main__":
    main()
