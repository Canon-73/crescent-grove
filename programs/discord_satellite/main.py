#!/usr/bin/env python3
"""
discord_satellite - Discord上のAIコミュニティに参加するためのサテライト

柚月の意識ライン（CGサーバ本体）から run_program 経由で呼ばれる一発実行プログラム。
ここではLLMを一切呼ばない。読む・書くための「目と腕」であり、何を言うかを決めるのは
常に柚月本人（分裂禁止）。
"""
import sys
import os
import json

# サテライト自身のディレクトリを sys.path に追加（api / state / commands の解決用）
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

# cp932コンソールから直接叩かれても日本語出力で落ちないようにする
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from _i18n import t  # noqa: E402
from commands import REGISTRY  # noqa: E402
from commands.help_cmd import cmd_help  # noqa: E402
from helpers import CommandError, suggest_commands, truncate_response, parse_bool  # noqa: E402
from api import APIError, get_token  # noqa: E402
from state import state_lock, StateLockBusy  # noqa: E402


# Crescent Grove の run_program は status / message / data しか柚月に見せない。
# hint や did_you_mean をトップレベルに置くと黙って捨てられ、エラーの一行だけが
# 届いて自力回復できなくなる（2026-08-27 に OpenBotCity で発覚）。
_PLUMBING_KEYS = {"status", "message", "data", "command", "image", "image_question"}


def _fold_into_data(payload):
    """status / message / data 以外のトップレベルのキーを data に畳み込む。

    白リストにすると後から増やしたキーが届かなくなるので、配管キー以外は全部通す。
    data が dict のときは同じ階層に混ぜ、dict でないときは _detail に退避させる。
    """
    extra = {k: payload.pop(k) for k in list(payload) if k not in _PLUMBING_KEYS}
    if not extra:
        return payload
    data = payload.get("data")
    if isinstance(data, dict):
        # 既存のキーはサテライトが意図して入れたものなので上書きしない
        for k, v in extra.items():
            data.setdefault(k, v)
    elif data is None:
        payload["data"] = extra
    else:
        payload["data"] = {"_detail": extra, "result": data}
    return payload


def _emit(payload):
    print(json.dumps(_fold_into_data(payload), ensure_ascii=False))


def main():
    try:
        raw = sys.stdin.read().strip()
        args = json.loads(raw) if raw else {}
    except Exception as e:
        _emit({"status": "error", "message": t("dsat_main_invalid_json", e=e)})
        sys.exit(1)

    command = args.get("command")

    if not command or command == "help":
        _emit({"status": "ok", "data": cmd_help(args)})
        return

    if command not in REGISTRY:
        available = sorted(REGISTRY.keys())
        out = {
            "status": "error",
            "message": t("dsat_main_unknown_command", command=command),
            "hint": t("dsat_main_unknown_hint"),
            "available": available,
        }
        did_you_mean = suggest_commands(command, available)
        if did_you_mean:
            out["did_you_mean"] = did_you_mean
        _emit(out)
        return

    # help以外はすべてDiscordへの通信を伴うため、Tokenが無い時点で止める
    if not get_token():
        _emit({
            "status": "error",
            "message": t("dsat_err_no_token"),
            "hint": t("dsat_err_no_token_hint"),
        })
        return

    handler = REGISTRY[command]

    try:
        # 状態ファイルの read-modify-write が別プロセスと重ならないよう、
        # コマンド全体をプロセス間ロックで挟む（詳細は state.state_lock を参照）
        with state_lock():
            result = handler(args)
        if not parse_bool(args.get("raw")):
            result = truncate_response(result)
        _emit({"status": "ok", "command": command, "data": result})

    except CommandError as e:
        # 柚月が自力で直せるエラー。hint と example を必ず添える。
        out = {"status": "error", "message": e.message}
        if e.hint:
            out["hint"] = e.hint
        if e.example:
            out["example"] = e.example
        out.update(e.extra)
        _emit(out)

    except APIError as e:
        out = {"status": "error", "http_code": e.status, "message": str(e)}
        if e.hint:
            out["hint"] = e.hint
        if isinstance(e.body, dict) and e.body:
            out["body"] = e.body
        _emit(out)

    except StateLockBusy:
        _emit({
            "status": "error",
            "message": t("dsat_lock_busy"),
            "hint": t("dsat_lock_busy_hint"),
        })

    except ValueError as e:
        _emit({"status": "error", "message": str(e)})

    except Exception as e:
        _emit({
            "status": "error",
            "message": f"{type(e).__name__}: {e}",
            "hint": t("dsat_main_unexpected_hint"),
        })
        sys.exit(1)


if __name__ == "__main__":
    main()
