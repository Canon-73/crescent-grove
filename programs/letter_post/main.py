"""
letter_post - 明日の自分への手紙管理サテライト
"""
import json
import sys
import os
import re
from datetime import datetime, timezone, timedelta

from _i18n import t

JST = timezone(timedelta(hours=9))
LETTER_FILE = "memory/letter_for_me.md"
LETTERS_DIR = "memory/letters"
MAX_ENTRIES = 10


def load_entries(content: str) -> list[str]:
    """手紙本文部分から感情エントリを抽出する"""
    entries = []
    for line in content.splitlines():
        line = line.strip()
        if re.match(r'^\d+\.', line):
            entries.append(line)
    return entries


def strip_number(entry: str) -> str:
    """番号を除いた本文を返す"""
    return re.sub(r'^\d+\.\s*', '', entry)


def rebuild_content(header: str, entries: list[str]) -> str:
    """ヘッダーとエントリリストからファイル内容を再構築する"""
    numbered = [f"{i+1}. {strip_number(e)}" for i, e in enumerate(entries)]
    return header + "\n".join(numbered) + "\n"


def get_header(content: str) -> str:
    """ルールヘッダー部分を取得する。
    旧 ja 環境で書かれた既存ファイルとの互換のため、構造マーカー文字列自体は
    翻訳しない（コード共通の構造目印として扱う）。
    """
    marker = "### 明日の私への手紙"
    idx = content.find(marker)
    if idx == -1:
        return content
    return content[:idx + len(marker)] + "\n"


def main():
    args = json.loads(sys.stdin.read())
    workspace = os.environ.get("CG_WORKSPACE", ".")

    letter_path = os.path.join(workspace, LETTER_FILE)
    letters_dir = os.path.join(workspace, LETTERS_DIR)
    os.makedirs(letters_dir, exist_ok=True)

    # ファイル読み込み（初回起動時はまだ存在しないため、その場合は空扱いにする。
    # 配布版のクリーンな workspace では letter_for_me.md が未作成で FileNotFoundError
    # になり exit 1・無出力でクラッシュしていた事故への対処）。
    if os.path.exists(letter_path):
        with open(letter_path, "r", encoding="utf-8") as f:
            content = f.read()
    else:
        content = ""

    header = get_header(content)
    entries = load_entries(content)

    delete = args.get("delete")
    title = args.get("title")
    emoji = args.get("emoji", "")
    body = args.get("body")

    # 引数なし: 表示モード
    if title is None and delete is None:
        lines = [f"{i+1}. {strip_number(e)}" for i, e in enumerate(entries)]
        msg = "\n".join(lines) if lines else t("letter_post_no_entries")
        if len(entries) >= MAX_ENTRIES:
            # JSON 例の { } を含むので lb / rb で素通しさせる
            msg += t("letter_post_full_notice", max=MAX_ENTRIES, lb="{", rb="}")
        else:
            msg += t("letter_post_room_notice", count=len(entries), max=MAX_ENTRIES, lb="{", rb="}")
        print(json.dumps({"status": "ok", "message": msg}, ensure_ascii=False))
        return

    # 削除処理
    archived = None
    if delete is not None:
        if not (1 <= delete <= len(entries)):
            print(json.dumps({"status": "error", "message": t("letter_post_err_no_such_number", delete=delete, count=len(entries))}, ensure_ascii=False))
            return
        archived = strip_number(entries[delete - 1])
        entries.pop(delete - 1)
        # 転記
        today = datetime.now(JST).strftime("%Y-%m-%d")
        archive_path = os.path.join(letters_dir, f"letter_{today}.md")
        with open(archive_path, "a", encoding="utf-8") as f:
            f.write(archived + "\n")

    # 追記処理
    if title and body:
        if len(entries) >= MAX_ENTRIES:
            print(json.dumps({"status": "error", "message": t("letter_post_err_full", max=MAX_ENTRIES)}, ensure_ascii=False))
            return
        now = datetime.now(JST).strftime("%Y-%m-%d %H:%M")
        new_entry = t("letter_post_entry_format", emoji=emoji, title=title, body=body, now=now)
        entries.append(new_entry)

    # 書き込み
    new_content = rebuild_content(header, entries)
    with open(letter_path, "w", encoding="utf-8") as f:
        f.write(new_content)

    # 結果メッセージ
    result_parts = []
    if archived:
        result_parts.append(t("letter_post_archived", excerpt=archived[:40]))
    if title and body:
        result_parts.append(t("letter_post_appended", emoji=emoji, title=title))
    result_parts.append(t("letter_post_count_summary", count=len(entries), max=MAX_ENTRIES))

    print(json.dumps({"status": "success", "message": "\n".join(result_parts)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
