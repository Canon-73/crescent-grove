"""setup / channels: 初期設定とチャンネル巡回対象の管理"""
from _i18n import t
from api import request, APIError
from helpers import CommandError, parse_bool, resolve_channel, snowflake_to_jst
from state import load_state, save_state, get_channels, merge_channels


# テキストとして読めるチャンネル種別（0=通常テキスト, 5=アナウンス）
TEXT_CHANNEL_TYPES = (0, 5)


# スレッド種別（10=アナウンススレ, 11=公開スレ, 12=非公開スレ）。フォーラム(15)の投稿もスレとして現れる
THREAD_TYPES = (10, 11, 12)


def _fetch_text_channels(guild_id):
    """テキストチャンネルに加え、活動中のスレッドも読める場所として列挙する。

    募集告知やフォーラム投稿はスレッドの中で進むため、親チャンネルしか見えないと
    会話の本体を見落とす。
    """
    channels = request("GET", f"/guilds/{guild_id}/channels")
    name_by_id = {str(c.get("id")): c.get("name") for c in channels}
    out = [
        {"id": str(c.get("id")), "name": c.get("name"), "kind": "channel"}
        for c in channels
        if c.get("type") in TEXT_CHANNEL_TYPES
    ]
    try:
        active = request("GET", f"/guilds/{guild_id}/threads/active")
        threads = active.get("threads", []) if isinstance(active, dict) else []
    except APIError:
        # スレッド一覧が取れなくてもチャンネルだけで続行する
        threads = []
    for th in threads:
        if th.get("type") not in THREAD_TYPES:
            continue
        out.append({
            "id": str(th.get("id")),
            "name": th.get("name"),
            "kind": "thread",
            "parent": name_by_id.get(str(th.get("parent_id"))),
        })
    return out


def cmd_setup(args):
    """
    Token検証 → guild確定 → チャンネル一覧取得 まで行い、状態ファイルを作る。

    既存の last_read_id / watch は保持されるので、再実行しても既読は巻き戻らない。
    """
    state = load_state()

    me = request("GET", "/users/@me")
    bot_user_id = str(me.get("id") or "")
    bot_name = me.get("global_name") or me.get("username") or ""

    # --- 参加サーバーの確定 ---
    guild_id = str(args.get("guild_id") or "").strip() or state.get("guild_id")
    guilds = request("GET", "/users/@me/guilds")
    guild_map = {str(g.get("id")): g.get("name") for g in guilds}

    if not guilds:
        raise CommandError(
            t("dsat_setup_no_guild"),
            hint=t("dsat_setup_no_guild_hint"),
        )

    if not guild_id:
        if len(guilds) == 1:
            guild_id = str(guilds[0].get("id"))
        else:
            raise CommandError(
                t("dsat_setup_multi_guild"),
                hint=t("dsat_setup_multi_guild_hint"),
                example={"command": "setup", "guild_id": str(guilds[0].get("id"))},
                guilds=[{"guild_id": k, "name": v} for k, v in guild_map.items()],
            )

    if guild_id not in guild_map:
        raise CommandError(
            t("dsat_setup_guild_not_found", guild_id=guild_id),
            hint=t("dsat_setup_guild_not_found_hint"),
            guilds=[{"guild_id": k, "name": v} for k, v in guild_map.items()],
        )

    # --- 名前呼び検知用キーワード ---
    keywords = args.get("keywords")
    if keywords:
        name_keywords = [k.strip() for k in str(keywords).split(",") if k.strip()]
    else:
        name_keywords = state.get("name_keywords") or ([bot_name] if bot_name else [])

    state.update({
        "bot_user_id": bot_user_id,
        "bot_display_name": bot_name,
        "guild_id": guild_id,
        "guild_name": guild_map.get(guild_id),
        "name_keywords": name_keywords,
    })

    fetched = _fetch_text_channels(guild_id)
    merge_channels(state, fetched)
    save_state(state)

    channels = get_channels(state)
    return {
        "bot_display_name": bot_name,
        "bot_user_id": bot_user_id,
        "guild": {"guild_id": guild_id, "name": guild_map.get(guild_id)},
        "name_keywords": name_keywords,
        "channels": [
            {"channel_id": cid, "name": info.get("name"), "watch": info.get("watch", False),
             **({"thread_of": info.get("parent")} if info.get("kind") == "thread" else {})}
            for cid, info in channels.items()
        ],
        "note": t("dsat_setup_done_note"),
        "next_step": t("dsat_setup_next"),
        "example": {"command": "channels", "channel": "雑談", "watch": True},
    }


def cmd_channels(args):
    """
    チャンネル一覧の表示と、巡回対象(watch)の切り替え。

    watch=true にしたチャンネルが、read をチャンネル指定なしで実行したときの対象になる。
    refresh=true で Discord から一覧を取り直す（新しいチャンネルが増えたとき用）。
    """
    state = load_state()
    if not state.get("guild_id"):
        raise CommandError(
            t("dsat_need_setup"),
            hint=t("dsat_need_setup_hint"),
            example={"command": "setup"},
        )

    if parse_bool(args.get("refresh")):
        merge_channels(state, _fetch_text_channels(state["guild_id"]))
        save_state(state)

    channels = get_channels(state)
    changed = None

    if args.get("channel") is not None and args.get("watch") is not None:
        cid = resolve_channel(args.get("channel"), channels)
        if not cid or cid not in channels:
            raise CommandError(
                t("dsat_channel_not_found", channel=args.get("channel")),
                hint=t("dsat_channel_not_found_hint"),
                available=[
                    {"channel_id": k, "name": v.get("name")} for k, v in channels.items()
                ],
            )
        channels[cid]["watch"] = parse_bool(args.get("watch"))
        state["channels"] = channels
        save_state(state)
        changed = {
            "channel_id": cid,
            "name": channels[cid].get("name"),
            "watch": channels[cid]["watch"],
        }

    listed = []
    for cid, info in channels.items():
        last_id = info.get("last_read_id")
        row = {
            "channel_id": cid,
            "name": info.get("name"),
            "watch": bool(info.get("watch", False)),
            "last_read_at": snowflake_to_jst(last_id) if last_id else None,
        }
        if info.get("kind") == "thread":
            row["thread_of"] = info.get("parent")
        listed.append(row)

    out = {
        "guild": {"guild_id": state.get("guild_id"), "name": state.get("guild_name")},
        "channels": listed,
        "watching_count": sum(1 for c in listed if c["watch"]),
        "hint": t("dsat_channels_hint"),
        "example": {"command": "channels", "channel": "雑談", "watch": True},
    }
    if changed:
        out["changed"] = changed
    return out


COMMANDS = {
    "setup": cmd_setup,
    "channels": cmd_channels,
}
