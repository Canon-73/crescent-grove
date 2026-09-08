"""profile / follow / unfollow: 自分と相手を見る・つながる"""
from _i18n import t
from api import fetch_user, get_base_url, request, resolve_user_id
from helpers import CommandError, parse_bool, to_jst_iso, user_handle


def _require_user(args, example):
    user = args.get("user")
    if not user:
        raise CommandError(
            t("msat_need_user"),
            hint=t("msat_need_user_hint"),
            example=example,
        )
    user_id = resolve_user_id(user)
    if not user_id:
        raise CommandError(
            t("msat_user_not_found", user=user),
            hint=t("msat_user_not_found_hint"),
            example=example,
        )
    return str(user), user_id


def _summarize_user(u):
    """ユーザー情報を要約する。"""
    if not isinstance(u, dict):
        return None
    out = {
        "handle": user_handle(u),
        "name": u.get("name"),
        "id": u.get("id"),
        "created_at": to_jst_iso(u.get("createdAt")),
        "counts": {
            "notes": u.get("notesCount"),
            "following": u.get("followingCount"),
            "followers": u.get("followersCount"),
        },
    }
    if u.get("description"):
        out["description"] = u.get("description")
    for src, dst in (("isFollowing", "i_follow_them"),
                     ("isFollowed", "they_follow_me"),
                     ("isLocked", "approval_required"),
                     ("isBot", "is_bot")):
        if u.get(src) is not None:
            out[dst] = bool(u.get(src))
    username = u.get("username")
    if username:
        host = u.get("host")
        out["url"] = f"{get_base_url()}/@{username}" + (f"@{host}" if host else "")
    return out


def cmd_profile(args):
    """自分または相手のプロフィールを見る（user 省略時は自分）。"""
    user = args.get("user")
    if user:
        info = fetch_user(user) or {}
        endpoint = "users/show"
    else:
        info = request("i", {}) or {}
        endpoint = "i"

    if parse_bool(args.get("raw")):
        return {"raw": {endpoint: info}}

    out = {"profile": _summarize_user(info)}
    if not user:
        out["is_me"] = True
    return out


def cmd_follow(args):
    """相手をフォローする。"""
    user, user_id = _require_user(
        args, {"command": "follow", "user": "@alice"})
    request("following/create", {"userId": user_id})
    return {"followed": True, "user": user, "user_id": user_id}


def cmd_unfollow(args):
    """フォローを外す。"""
    user, user_id = _require_user(
        args, {"command": "unfollow", "user": "@alice"})
    request("following/delete", {"userId": user_id})
    return {"unfollowed": True, "user": user, "user_id": user_id}


COMMANDS = {
    "profile": cmd_profile,
    "follow": cmd_follow,
    "unfollow": cmd_unfollow,
}
