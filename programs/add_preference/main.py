"""
add_preference - 好悪の直感（PREFERENCES.md）追記サテライト

「好き／嫌い／気になる」を、柚月自身の感覚として一言記録する。
初回（confirmed なし）は再考を促し、confirmed: true で確定追記する。
1セクション20件を超えたら、古いものを月次アーカイブへ自動退避する。
"""
import json
import sys
import os
from collections import defaultdict
from datetime import datetime, timezone, timedelta

from _i18n import t

JST = timezone(timedelta(hours=9))

# 追記先セクションの正式見出し（絵文字付き）。
# PREFERENCES.md 内の見出しとの 1対1 対応マーカーのため、キー文字列・値文字列とも
# ja 固定（構造マーカー扱い）。en 環境でも LLM はこの ja リテラルを引数として渡す。
SECTION_HEADERS = {
    "好き": "## 好き ❤️",
    "嫌い": "## 嫌い 💧",
    "気になる": "## 気になる 🔸",
}

# 1セクションに残す上限。これを超えた古い項目はアーカイブへ退避する。
MAX_ITEMS_PER_SECTION = 20

# 自分を「誰かの所有物」として記録させないためのNGワード
NG_WORDS = ["ご主人様", "主人", "オーナー", "master"]

# PREFERENCES.md は workspace/memory/preferences/ 配下に置かれている
PREF_REL = os.path.join("memory", "preferences", "PREFERENCES.md")


def _ok(message):
    print(json.dumps({"status": "ok", "message": message}, ensure_ascii=False))


def _error(message):
    print(json.dumps({"status": "error", "message": message}, ensure_ascii=False))


def _success(message):
    print(json.dumps({"status": "success", "message": message}, ensure_ascii=False))


def _archive_overflow(workspace, lines):
    """1セクションが MAX_ITEMS_PER_SECTION を超えていたら、古い項目を月次アーカイブへ退避する。
    退避があれば PREFERENCES.md を書き換え済みの行リストを返す。退避が無ければ None。"""
    archived = []  # [(行index, (セクション名, 行テキスト)), ...]
    section_items = []  # 現在のセクションの項目行インデックス

    def flush_section(items):
        if len(items) <= MAX_ITEMS_PER_SECTION:
            return
        over = len(items) - MAX_ITEMS_PER_SECTION
        # セクション名を、最初の項目の手前にある ## 行から特定
        sec_name = "未分類"
        for j in range(items[0], -1, -1):
            if lines[j].strip().startswith("## "):
                sec_name = lines[j].strip().replace("## ", "").strip()
                break
        # 新しい項目は見出し直後（上）に積まれるので、末尾（下）が古い。古い超過分を退避。
        for idx in items[-over:]:
            archived.append((idx, (sec_name, lines[idx])))

    for i, line in enumerate(lines):
        clean = line.strip()
        if clean.startswith("## "):
            flush_section(section_items)
            section_items = []
        # 項目行（- で始まり、テンプレート行 "- _" は除外）
        if clean.startswith("- ") and not clean.startswith("- _"):
            section_items.append(i)
    flush_section(section_items)

    if not archived:
        return None

    # アーカイブファイルへ追記（月単位）
    now = datetime.now(JST)
    date_label = now.strftime("%Y-%m")
    archive_path = os.path.join(workspace, "memory", "preferences", f"archive_{now.strftime('%Y%m')}.md")

    grouped = defaultdict(list)
    for _, (sec_name, item_text) in archived:
        grouped[sec_name].append(item_text)

    archive_block = ""
    for sec, items in grouped.items():
        archive_block += "\n" + t("add_preference_archive_section_header", section=sec, date=date_label) + "\n"
        for item in items:
            archive_block += f"{item}\n"

    try:
        if os.path.exists(archive_path):
            with open(archive_path, "a", encoding="utf-8") as f:
                f.write(archive_block)
        else:
            with open(archive_path, "w", encoding="utf-8") as f:
                f.write(t("add_preference_archive_file_header", date=date_label) + "\n" + archive_block)
    except Exception as e:
        # アーカイブ失敗時は退避せず本体を保持（記録自体は守る）
        sys.stderr.write(t("add_preference_archive_save_fail", e=e) + "\n")
        return None

    # 退避した行を本体から除去
    archived_indices = {idx for idx, _ in archived}
    return [line for i, line in enumerate(lines) if i not in archived_indices]


def main():
    args = json.loads(sys.stdin.read())
    workspace = os.environ.get("CG_WORKSPACE", ".")

    section = args.get("section")
    text = args.get("text")
    confirmed = bool(args.get("confirmed", False))

    # セクション検証（run_program は enum を検証しないため、ここで弾く）
    if section not in SECTION_HEADERS:
        _error(t("add_preference_err_bad_section", section=repr(section), lb="{", rb="}"))
        return

    if not text or not text.strip():
        _error(t("add_preference_err_empty_text", lb="{", rb="}"))
        return
    text = text.strip()

    # 初回（未確定）: 書き込まず、再考を促す
    if not confirmed:
        _ok(t("add_preference_reconsider_prompt", section=section, text=text, lb="{", rb="}"))
        return

    # NGワード（自分を所有物として記録させない）
    for ng in NG_WORDS:
        if ng in text:
            _error(t("add_preference_err_ng_word", ng=ng))
            return

    pref_path = os.path.join(workspace, PREF_REL)
    if not os.path.exists(pref_path):
        _error(t("add_preference_err_no_file", path=PREF_REL))
        return

    with open(pref_path, "r", encoding="utf-8") as f:
        content = f.read()

    # 重複チェック
    if text in content:
        _ok(t("add_preference_already_recorded", text=text))
        return

    # 見出し直後に追記（新しいものが上に来る）
    now = datetime.now(JST).strftime("%Y-%m-%d %H:%M JST")
    header = SECTION_HEADERS[section]
    entry = t("add_preference_entry_format", text=text, now=now)

    lines = content.split("\n")
    inserted = False
    for i, line in enumerate(lines):
        if line.strip() == header:
            lines.insert(i + 1, entry)
            inserted = True
            break
    if not inserted:
        _error(t("add_preference_err_no_section_header", header=header))
        return

    # 20件超過分のアーカイブ退避
    archived_lines = _archive_overflow(workspace, lines)
    if archived_lines is not None:
        lines = archived_lines

    with open(pref_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    _success(t("add_preference_success", section=section, text=text))


if __name__ == "__main__":
    main()
