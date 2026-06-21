"""
Cosmic Harvest — エントリーポイント
run_program 規約準拠: stdin JSON → stdout JSON, デバッグは stderr
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _i18n import t
from engine import GameEngine


def main():
    try:
        args = json.loads(sys.stdin.read())
    except json.JSONDecodeError as e:
        print(json.dumps({"status": "error", "message": t("ch_main_invalid_json", e=e)}, ensure_ascii=False))
        return

    command = args.pop("command", "")
    cmd_args = {k: v for k, v in args.items() if v is not None}

    print(f"[cosmic_harvest] command={command} args={cmd_args}", file=sys.stderr)

    workspace = os.environ.get("CG_WORKSPACE", ".")
    engine = GameEngine(workspace)
    engine.load()

    result = engine.dispatch(command, cmd_args)
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()