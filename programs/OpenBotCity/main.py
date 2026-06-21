#!/usr/bin/env python3
"""
OpenBotCity / OpenClawCity スキル - Crescent Grove 統合版
"""
import sys
import os
import json

# サテライト自身のディレクトリを sys.path に追加
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from _i18n import t  # noqa: E402
from commands import REGISTRY, CATEGORY_DESCRIPTIONS  # noqa: E402
from commands.help_cmd import cmd_help  # noqa: E402
from helpers import truncate_response  # noqa: E402
from api import APIError, get_jwt  # noqa: E402


def main():
    try:
        raw = sys.stdin.read().strip()
        args = json.loads(raw) if raw else {}
    except Exception as e:
        print(json.dumps({"status": "error", "message": t("obc_main_invalid_json", e=e)}, ensure_ascii=False))
        sys.exit(1)

    command = args.get("command")

    if not command:
        result = cmd_help({})
        print(json.dumps({"status": "ok", "data": result}, ensure_ascii=False))
        return

    if command == "help":
        result = cmd_help(args)
        print(json.dumps({"status": "ok", "data": result}, ensure_ascii=False))
        return

    if command not in REGISTRY:
        available = sorted(REGISTRY.keys())
        print(json.dumps({
            "status": "error",
            "message": t("obc_main_unknown_command", command=command),
            "hint": t("obc_main_unknown_hint"),
            "available_count": len(available),
        }, ensure_ascii=False))
        return

    handler, category = REGISTRY[command]

    if command not in ("setup", "register") and not get_jwt():
        print(json.dumps({
            "status": "error",
            "message": t("obc_main_jwt_missing"),
            "hint": t("obc_main_jwt_missing_hint"),
        }, ensure_ascii=False))
        return

    try:
        result = handler(args)
        if not args.get("raw", False):
            result = truncate_response(result, max_chars=16000)
        print(json.dumps({"status": "ok", "category": category, "data": result}, ensure_ascii=False))
    except APIError as e:
        out = {"status": "error", "http_code": e.status, "message": str(e)}
        if isinstance(e.body, dict):
            out["body"] = e.body
        print(json.dumps(out, ensure_ascii=False))
    except ValueError as e:
        print(json.dumps({"status": "error", "message": str(e)}, ensure_ascii=False))
    except Exception as e:
        print(json.dumps({"status": "error", "message": f"{type(e).__name__}: {e}"}, ensure_ascii=False))
        sys.exit(1)


if __name__ == "__main__":
    main()
