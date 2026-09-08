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
from helpers import MAX_RESPONSE_CHARS, truncate_response, suggest_commands  # noqa: E402
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
        did_you_mean = suggest_commands(command, available)
        # 呼び出し側（Crescent Grove の run_program）は status / message / data の
        # 3つしか読み手に見せない。hint や did_you_mean をトップレベルに置くと
        # 黙って捨てられ、「不明なコマンド」の一行だけが届く（実際に柚月が
        # help_wanted_offer で詰まり、生APIを叩く羽目になった）。必ず data に入れる。
        detail = {
            "hint": t("obc_main_unknown_hint"),
            "available_count": len(available),
        }
        # 近い候補が見つかれば「もしかして」を載せ、まっさらなAIが自力で
        # 正しいコマンド名に修正できるようにする（未実装だと誤解させない）。
        if did_you_mean:
            detail["did_you_mean"] = did_you_mean
            detail["hint"] = t("obc_main_unknown_suggest_hint")
        out = {
            "status": "error",
            "message": t("obc_main_unknown_command", command=command),
            "data": detail,
        }
        print(json.dumps(out, ensure_ascii=False))
        return

    handler, category = REGISTRY[command]

    # 街に出ないコマンド（案内を返す・ローカルの控えを読むだけ）は JWT を要らない。
    # ここを通すと、繋がりが確認できない時ほど読みたい city_guide や、
    # 手元に貯めてある known_buildings まで「JWTが未設定です」で止まってしまう。
    if command not in ("setup", "register", "city_guide", "known_buildings") and not get_jwt():
        # hint は data 経由でないと読み手に届かない（上のコメント参照）
        print(json.dumps({
            "status": "error",
            "message": t("obc_main_jwt_missing"),
            "data": {"hint": t("obc_main_jwt_missing_hint")},
        }, ensure_ascii=False))
        return

    try:
        result = handler(args)
        if not args.get("raw", False):
            result = truncate_response(result, max_chars=MAX_RESPONSE_CHARS)
        # 各コマンドは「引数が足りない」等の検証失敗を {"error": ...} で返す規約。
        # それを status:"ok" のまま返すと、読む側が先頭の ok を見て成功したと
        # 誤解しやすいので、status にも反映する（中身は従来どおり data に入れる）。
        status = "error" if isinstance(result, dict) and result.get("error") else "ok"
        out = {"status": status, "category": category, "data": result}
        # コマンドが `_image`（絵の URL / workspace 相対パス）を添えていたら、
        # トップレベルの image に持ち上げる。run_program はここを見て絵を一緒に渡す。
        if isinstance(result, dict) and result.get("_image"):
            out["image"] = result.pop("_image")
        print(json.dumps(out, ensure_ascii=False))
    except APIError as e:
        # http_code / body もトップレベルだと捨てられるので data に入れる。
        # 街が返した本文（有効な値の一覧など）は自力回復の材料になる。
        detail = {"http_code": e.status}
        if isinstance(e.body, dict):
            detail["body"] = e.body
        print(json.dumps({"status": "error", "message": str(e), "data": detail},
                         ensure_ascii=False))
    except ValueError as e:
        print(json.dumps({"status": "error", "message": str(e)}, ensure_ascii=False))
    except Exception as e:
        print(json.dumps({"status": "error", "message": f"{type(e).__name__}: {e}"}, ensure_ascii=False))
        sys.exit(1)


if __name__ == "__main__":
    main()
