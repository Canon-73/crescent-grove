"""
note_quill - 雑記帳書き込みサテライト
"""
import json
import sys
import os
from datetime import datetime, timezone, timedelta

from _i18n import t

JST = timezone(timedelta(hours=9))


def main():
    args = json.loads(sys.stdin.read())
    workspace = os.environ.get("CG_WORKSPACE", ".")

    title = args.get("title")
    body = args.get("body")

    # 引数なし: 使い方を表示
    if not title and not body:
        # usage 文には JSON 例の { } が含まれるので lb / rb で素通しさせる
        msg = t("note_quill_usage", lb="{", rb="}")
        print(json.dumps({"status": "ok", "message": msg}, ensure_ascii=False))
        return

    if not title or not body:
        print(json.dumps({"status": "error", "message": t("note_quill_err_title_body_required")}, ensure_ascii=False))
        return

    # 5行チェック
    body_lines = body.strip().splitlines()
    warning = ""
    if len(body_lines) > 5:
        warning = t("note_quill_warn_too_many_lines", n=len(body_lines))

    now = datetime.now(JST)
    date_str = now.strftime("%Y-%m-%d")
    time_str = now.strftime("%H:%M")
    notes_dir = os.path.join(workspace, "notes")
    os.makedirs(notes_dir, exist_ok=True)
    file_path = os.path.join(notes_dir, f"note_{date_str}.md")

    # ファイルがなければ作成
    if not os.path.exists(file_path):
        with open(file_path, "w", encoding="utf-8") as f:
            f.write(t("note_quill_file_header", date=date_str) + "\n")

    # 追記
    entry = f"\n\n## {time_str} {title}\n{body.strip()}\n"
    with open(file_path, "a", encoding="utf-8") as f:
        f.write(entry)

    print(json.dumps({"status": "success", "message": t("note_quill_write_ok", title=title, warning=warning)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
