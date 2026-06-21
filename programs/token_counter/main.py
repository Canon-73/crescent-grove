import json
import os
import sys
from pathlib import Path

from _i18n import t


def main():
    args = json.loads(sys.stdin.read())
    file_path = args.get("file")

    if not file_path:
        print(json.dumps({"status": "error", "message": t("token_counter_err_no_file")}, ensure_ascii=False))
        return

    workspace = os.environ.get("CG_WORKSPACE", ".")
    full_path = Path(workspace) / file_path

    # パストラバーサルチェック
    try:
        resolved = full_path.resolve()
        workspace_resolved = Path(workspace).resolve()
        if not str(resolved).startswith(str(workspace_resolved)):
            print(json.dumps({"status": "error", "message": t("token_counter_err_outside_workspace")}, ensure_ascii=False))
            return
    except Exception as e:
        print(json.dumps({"status": "error", "message": t("token_counter_err_resolve_fail", e=e)}, ensure_ascii=False))
        return

    if not resolved.is_file():
        print(json.dumps({"status": "error", "message": t("token_counter_err_not_found", file=file_path)}, ensure_ascii=False))
        return

    try:
        text = resolved.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        print(json.dumps({"status": "error", "message": t("token_counter_err_not_text")}, ensure_ascii=False))
        return
    except Exception as e:
        print(json.dumps({"status": "error", "message": t("token_counter_err_read_fail", e=e)}, ensure_ascii=False))
        return

    try:
        import tiktoken
        enc = tiktoken.get_encoding("cl100k_base")
        token_count = len(enc.encode(text))
    except ImportError:
        print(json.dumps({"status": "error", "message": t("token_counter_err_no_tiktoken")}, ensure_ascii=False))
        return

    lines = text.count("\n") + (1 if text and not text.endswith("\n") else 0)
    char_count = len(text)
    byte_size = resolved.stat().st_size

    result = {
        "status": "success",
        "message": t("token_counter_success", file=file_path, tokens=token_count),
        "data": {
            "file": file_path,
            "tokens": token_count,
            "lines": lines,
            "characters": char_count,
            "bytes": byte_size
        }
    }
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
