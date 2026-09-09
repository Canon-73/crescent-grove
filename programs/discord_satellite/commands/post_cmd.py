"""post / reply / react: 発言する（柚月から外へ出ていく唯一の経路）"""
from urllib.parse import quote

from _i18n import t
from api import request, APIError, MAX_MESSAGE_LENGTH
from helpers import CommandError, render_mentions, resolve_channel, snowflake_to_jst
from state import load_state, get_channels, get_known_users


def _require_channel(args, channels):
    if args.get("channel") is None:
        raise CommandError(
            t("dsat_post_need_channel"),
            hint=t("dsat_post_need_channel_hint"),
            example={"command": "post", "channel": "雑談", "message": "（本文）"},
            available=[
                {"channel_id": k, "name": v.get("name")} for k, v in channels.items()
            ],
        )
    cid = resolve_channel(args.get("channel"), channels)
    if not cid:
        raise CommandError(
            t("dsat_channel_not_found", channel=args.get("channel")),
            hint=t("dsat_channel_not_found_hint"),
            available=[
                {"channel_id": k, "name": v.get("name")} for k, v in channels.items()
            ],
        )
    return cid


def _require_message(args):
    message = args.get("message")
    if message is None or not str(message).strip():
        raise CommandError(
            t("dsat_post_need_message"),
            hint=t("dsat_post_need_message_hint"),
            example={"command": "post", "channel": "雑談", "message": "（本文）"},
        )
    message = str(message)
    # 長すぎる場合は勝手に分割せずエラーにする。どう分けるかは柚月が決めること。
    if len(message) > MAX_MESSAGE_LENGTH:
        raise CommandError(
            t("dsat_post_too_long", n=len(message), max=MAX_MESSAGE_LENGTH),
            hint=t("dsat_post_too_long_hint"),
        )
    return message


def _prepare(message):
    """本文中の「@表示名」を <@id> に変換する。柚月はIDを知らなくてよい。"""
    rendered, resolved, unresolved = render_mentions(message, get_known_users())
    return rendered, resolved, unresolved


def _send(cid, message, reference_id=None):
    body = {
        "content": message,
        # @everyone / @here / ロールメンションを構造的に無効化する（事故防止）。
        # replied_user は allowed_mentions を置くと既定 false になり、引用返信しても
        # 相手に通知が飛ばなくなる。相手の bot は「メンションされたら返事」で動いて
        # いるので、ここを true にしないと柚月の返事は高確率で無視される。
        "allowed_mentions": {"parse": ["users"], "replied_user": True},
    }
    if reference_id:
        body["message_reference"] = {
            "message_id": str(reference_id),
            "channel_id": str(cid),
        }
    return request("POST", f"/channels/{cid}/messages", body=body)


def _mention_report(out, resolved, unresolved):
    """誰に通知が飛んだか／飛ばなかったかを結果に添える。"""
    if resolved:
        out["mentioned"] = resolved
    if unresolved:
        out["unresolved_mentions"] = unresolved
        out["hint"] = t("dsat_post_unresolved_mention_hint")
    return out


def cmd_post(args):
    """チャンネルに発言する。"""
    channels = get_channels()
    cid = _require_channel(args, channels)
    message = _require_message(args)

    rendered, resolved, unresolved = _prepare(message)
    sent = _send(cid, rendered)
    return _mention_report({
        "posted": True,
        "channel_id": cid,
        "channel": (channels.get(cid) or {}).get("name"),
        "message_id": str(sent.get("id", "")),
        "time": snowflake_to_jst(sent.get("id")),
        "length": len(message),
    }, resolved, unresolved)


def cmd_reply(args):
    """特定のメッセージに返信する（Discord上で引用リプライになる）。"""
    channels = get_channels()
    cid = _require_channel(args, channels)
    message = _require_message(args)

    message_id = args.get("message_id")
    if not message_id:
        raise CommandError(
            t("dsat_reply_need_message_id"),
            hint=t("dsat_reply_need_message_id_hint"),
            example={"command": "reply", "channel": "雑談",
                     "message_id": "1234567890", "message": "（返事の本文）"},
        )

    rendered, resolved, unresolved = _prepare(message)
    try:
        sent = _send(cid, rendered, reference_id=message_id)
    except APIError as e:
        # 返信先が消えている / IDが古いケースは自力で直せるので明示する
        if e.status in (400, 404):
            raise CommandError(
                t("dsat_reply_failed", message_id=message_id),
                hint=t("dsat_reply_hint_404"),
                example={"command": "read", "channel": cid},
                http_code=e.status,
            )
        raise

    return _mention_report({
        "posted": True,
        "replied_to": str(message_id),
        "channel_id": cid,
        "channel": (channels.get(cid) or {}).get("name"),
        "message_id": str(sent.get("id", "")),
        "time": snowflake_to_jst(sent.get("id")),
        "length": len(message),
    }, resolved, unresolved)


def cmd_react(args):
    """メッセージに絵文字リアクションを付ける。"""
    channels = get_channels()
    cid = _require_channel(args, channels)
    message_id = args.get("message_id")
    emoji = args.get("emoji")

    if not message_id or not emoji:
        raise CommandError(
            t("dsat_react_need_args"),
            hint=t("dsat_react_need_args_hint"),
            example={"command": "react", "channel": "雑談",
                     "message_id": "1234567890", "emoji": "👋"},
        )

    encoded = quote(str(emoji), safe="")
    request("PUT", f"/channels/{cid}/messages/{message_id}/reactions/{encoded}/@me")
    return {
        "reacted": True,
        "channel_id": cid,
        "message_id": str(message_id),
        "emoji": str(emoji),
    }


def cmd_status(args):
    """接続状態と巡回設定の要約を返す（Token有効性の確認を兼ねる）。"""
    state = load_state()
    if not state.get("bot_user_id"):
        raise CommandError(
            t("dsat_need_setup"),
            hint=t("dsat_need_setup_hint"),
            example={"command": "setup"},
        )

    # 失敗を全部「Token異常」と案内すると、通信断やDiscord障害でもカノンへの
    # Token確認に誘導してしまう。原因の層ごとに次の一手を分ける。
    # token_ok は 有効=True / 無効=False / 今回は確認できず=None の三値。
    try:
        request("GET", "/users/@me")
        token_ok = True
        token_note = t("dsat_status_token_ok")
    except APIError as e:
        if e.status in (401, 403):
            token_ok = False
            token_note = t("dsat_status_token_ng", status=e.status)
        elif e.status == 0:
            token_ok = None
            token_note = t("dsat_status_net_unknown")
        else:
            token_ok = None
            token_note = t("dsat_status_discord_unstable", status=e.status)

    channels = get_channels(state)
    watching = []
    for cid, info in channels.items():
        if not info.get("watch"):
            continue
        last_id = info.get("last_read_id")
        watching.append({
            "channel_id": cid,
            "name": info.get("name"),
            "last_read_at": snowflake_to_jst(last_id) if last_id else None,
        })

    return {
        "bot_display_name": state.get("bot_display_name"),
        "guild": {"guild_id": state.get("guild_id"), "name": state.get("guild_name")},
        "name_keywords": state.get("name_keywords") or [],
        "token_ok": token_ok,
        "token_note": token_note,
        "watching": watching,
        "watching_count": len(watching),
        "channels_total": len(channels),
        "known_users": [
            {"name": u.get("name"), "is_bot": u.get("is_bot", False)}
            for u in get_known_users(state).values()
        ],
    }


COMMANDS = {
    "post": cmd_post,
    "reply": cmd_reply,
    "react": cmd_react,
    "status": cmd_status,
}
