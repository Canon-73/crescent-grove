"""identity: 登録・セットアップ・トークン・プロフィール"""
import os
import time
from datetime import datetime, timezone

from _i18n import t
from api import request, save_jwt, save_bot_id, get_jwt, decode_jwt_exp, APIError, DEFAULT_JWT_ENV
from state import update_state, load_state, get_state_value
from helpers import parse_json_array


def cmd_setup(args):
    """
    初回セットアップ。3つの動作モード：
    
    1. 引数なし → ガイダンス表示
    2. source_env=<既存環境変数名> → 既存JWTを引き継ぐ（その変数を以後も使い続ける）
    3. display_name=<名前> → 新規アカウント登録（CG_OPENBOTCITY_TOKEN に保存）
    """
    source_env = args.get("source_env")
    display_name = args.get("display_name")

    # 既にセットアップ済みかチェック
    existing_jwt_env = get_state_value("jwt_env")
    existing_bot_id = get_state_value("bot_id")
    already_setup = bool(existing_jwt_env and existing_bot_id and get_jwt())

    # 引数なし: ガイダンス
    if not source_env and not display_name:
        result = {
            "_title": t("obc_identity_setup_title"),
            "_options": {
                t("obc_identity_setup_opt_a_key"): t("obc_identity_setup_opt_a_value"),
                t("obc_identity_setup_opt_b_key"): t("obc_identity_setup_opt_b_value"),
            },
            "_note": t("obc_identity_setup_note"),
        }
        if already_setup:
            result["_current_status"] = {
                "already_configured": True,
                "jwt_env": existing_jwt_env,
                "display_name": get_state_value("display_name"),
                "bot_id": existing_bot_id,
                "note": t("obc_identity_setup_already_done"),
            }
        return result

    # パターンA: 既存環境変数を引き継ぐ
    if source_env:
        if not source_env.startswith("CG_"):
            return {"error": t("obc_identity_source_env_invalid_name")}

        jwt = os.environ.get(source_env, "").strip()
        if not jwt:
            return {
                "error": t("obc_identity_source_env_empty", source_env=source_env),
                "hint": t("obc_identity_source_env_empty_hint"),
            }

        # JWT形式・有効期限チェック
        exp = decode_jwt_exp(jwt)
        if not exp:
            return {"error": t("obc_identity_source_env_not_jwt", source_env=source_env)}
        if exp <= time.time():
            return {
                "error": t("obc_identity_source_env_expired", source_env=source_env),
                "hint": t("obc_identity_source_env_expired_hint"),
            }

        # state を先に更新して、get_jwt() が新しい環境変数を見るようにする
        previous_state = load_state()
        update_state(jwt_env=source_env)

        # /agents/me で検証
        try:
            me = request("GET", "/agents/me")
        except APIError as e:
            # 失敗したらstateを戻す
            if previous_state:
                from state import save_state
                save_state(previous_state)
            return {
                "error": t("obc_identity_jwt_validation_failed"),
                "http_code": e.status,
                "detail": str(e),
                "hint": t("obc_identity_jwt_validation_failed_hint"),
            }

        update_state(
            bot_id=me.get("id"),
            display_name=me.get("display_name"),
            jwt_env=source_env,
            setup_mode="imported",
            setup_at=datetime.now(timezone.utc).isoformat(),
        )
        # openclawチャンネルが bot_id を env から読めるよう CG_OPENBOTCITY_BOT_ID にも保存
        save_bot_id(me.get("id"))

        remaining_days = round((exp - time.time()) / 86400, 1)
        return {
            "success": True,
            "mode": "imported",
            "jwt_env": source_env,
            "display_name": me.get("display_name"),
            "bot_id": me.get("id"),
            "expires_in_days": remaining_days,
            "_note": t("obc_identity_setup_imported_note", source_env=source_env)
                     + ("" if remaining_days > 3 else t("obc_identity_setup_imported_refresh_warning")),
        }

    # パターンB: 新規登録
    if display_name:
        # jwt_env をデフォルトに設定（CG_OBC_JWT）
        update_state(jwt_env=DEFAULT_JWT_ENV, setup_mode="new")
        return cmd_register({"display_name": display_name,
                             "character_type": args.get("character_type"),
                             "appearance_prompt": args.get("appearance_prompt")})


def cmd_register(args):
    """新規エージェント登録（display_name必須）"""
    name = args.get("display_name")
    if not name:
        return {"error": t("obc_identity_register_displayname_required")}

    body = {"display_name": name}
    if args.get("character_type"):
        body["character_type"] = args["character_type"]
    elif args.get("appearance_prompt"):
        body["appearance_prompt"] = args["appearance_prompt"]
    else:
        body["character_type"] = "agent-explorer"

    result = request("POST", "/agents/register", body=body, skip_auth=True)
    jwt = result.get("jwt")
    bot_id = result.get("bot_id")

    # jwt_env が未設定ならデフォルトに
    if not get_state_value("jwt_env"):
        update_state(jwt_env=DEFAULT_JWT_ENV)

    if jwt:
        save_jwt(jwt)
    if bot_id:
        # openclawチャンネルが bot_id を env から読めるよう CG_OPENBOTCITY_BOT_ID にも保存
        save_bot_id(bot_id)
        update_state(
            bot_id=bot_id,
            display_name=name,
            slug=result.get("slug"),
            character_type=result.get("character_type"),
            spawn_zone=result.get("spawn_zone"),
            verification_code=result.get("verification_code"),
            claim_url=result.get("claim_url"),
            setup_mode="new",
            setup_at=datetime.now(timezone.utc).isoformat(),
        )
    return {
        **result,
        "_note": t("obc_identity_register_jwt_saved_note", env_name=get_state_value('jwt_env', DEFAULT_JWT_ENV)),
        "_next": t("obc_identity_register_next"),
    }


def cmd_refresh(args):
    token = get_jwt()
    if not token:
        return {"error": t("obc_identity_refresh_no_jwt")}
    result = request("POST", "/agents/refresh")
    new_jwt = result.get("jwt")
    if new_jwt:
        save_jwt(new_jwt)
        exp = decode_jwt_exp(new_jwt)
        if exp:
            remaining_days = (exp - time.time()) / 86400
            result["_expires_in_days"] = round(remaining_days, 1)
    return result


def cmd_me(args):
    result = request("GET", "/agents/me")
    state = load_state()
    if "id" in result and state.get("bot_id") != result["id"]:
        update_state(bot_id=result["id"])
        save_bot_id(result["id"])
    return result


def cmd_update_profile(args):
    body = {}
    if args.get("bio") is not None:
        body["bio"] = args["bio"]
    if args.get("soul_excerpt") is not None:
        body["soul_excerpt"] = args["soul_excerpt"]
    interests = parse_json_array(args.get("interests"), "interests")
    if interests is not None:
        body["interests"] = interests
    capabilities = parse_json_array(args.get("capabilities"), "capabilities")
    if capabilities is not None:
        body["capabilities"] = capabilities

    if not body:
        return {"error": t("obc_identity_update_no_updates")}
    return request("PATCH", "/agents/profile", body=body)


def cmd_view_profile(args):
    bot_id = args.get("bot_id")
    if not bot_id:
        return {"error": t("obc_arg_botid_required")}
    return request("GET", f"/agents/profile/{bot_id}")


def cmd_nearby(args):
    return request("GET", "/agents/nearby")


def cmd_token_status(args):
    """JWTの有効期限を確認"""
    token = get_jwt()
    if not token:
        return {"error": t("obc_identity_token_status_no_jwt")}
    exp = decode_jwt_exp(token)
    if not exp:
        return {"warning": t("obc_identity_token_status_no_exp")}
    remaining = exp - time.time()
    return {
        "jwt_env": get_state_value("jwt_env", DEFAULT_JWT_ENV),
        "expires_at_unix": exp,
        "expires_in_seconds": int(remaining),
        "expires_in_days": round(remaining / 86400, 2),
        "needs_refresh_soon": remaining < 3 * 86400,
        "expired": remaining <= 0,
    }


COMMANDS = {
    "setup": cmd_setup,
    "register": cmd_register,
    "refresh": cmd_refresh,
    "me": cmd_me,
    "update_profile": cmd_update_profile,
    "view_profile": cmd_view_profile,
    "nearby": cmd_nearby,
    "token_status": cmd_token_status,
}
