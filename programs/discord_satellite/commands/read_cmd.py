"""read: 前回読んだところから先の新着を読む（柚月の主コマンド）／before で過去ログを遡る"""
import json
import time

from _i18n import t
from api import request, APIError
from helpers import (
    MAX_RESPONSE_CHARS,
    CommandError, author_name, clip, collect_users, humanize_content,
    is_system_message, mentions_me, names_me, parse_bool, resolve_channel,
    snowflake_to_jst, summarize_attachments, summarize_reactions,
)
from state import load_state, save_state, get_channels, get_known_users


# 1チャンネルあたりの既定取得件数と、Discord API の上限
DEFAULT_LIMIT = 30
MAX_LIMIT = 100
# 初回（カーソル未設定・limit 未指定）は履歴を遡りすぎないよう直近だけ見る
FIRST_READ_LIMIT = 10
# 本文の既定切り詰め文字数（raw=true で全文）
CONTENT_CLIP = 500
EXCERPT_CLIP = 120
# 名簿の上限（際限なく太らせない）
KNOWN_USERS_MAX = 500

# 応答全体の予算。main.py の最後の砦（truncate_response）は構造ごと潰して
# 後ろを消すので、そこに落ちる前にこちらで構造を保ったまま畳む。
# skipped / deferred は _fit_to_budget の測定対象に含めるので、この余白が
# 吸収するのは total_new / hint / example などの小さな項目だけ。
BUDGET_MARGIN = 4000
RESPONSE_BUDGET = MAX_RESPONSE_CHARS - BUDGET_MARGIN

# raw=true（柚月が明示的に全文を求めたとき）の配送上限。無制限だと巨大応答の
# 配送がどこかで欠けたときに「カーソルだけ進んで本文は届かない」が起き得るので、
# 十分大きな上限だけ残す（親の _run_program は data を素通しするため、上限は
# こちらで持つしかない）。畳んだ分は通常時と同じく未読のまま残る。
# 使うときは通常時と同じく BUDGET_MARGIN を引いて余白を確保する。
RAW_RESPONSE_BUDGET = 200000

# 巡回全体の時間予算（秒）。manifest の timeout(120秒) で親に強制終了されると
# 応答自体が出せなくなるため、その手前で自分から巡回を打ち切る。
# 基準はプロセス起動時刻（このモジュールの import はロック取得より前なので、
# main.py の state_lock 待ち・最大10秒もこの時計に含まれる）。
# 最悪値: 予算60秒 + 最後の取得（接続15 + 429待ち10 + 再試行15 ≒ 40秒）≒ 100秒 < 120秒。
TIME_BUDGET = 60
_PROCESS_START = time.monotonic()


def _channel_label(info, cid):
    """表示用のチャンネル名。スレッドは「親/スレ名」にして場所が分かるようにする。"""
    name = info.get("name") or cid
    parent = info.get("parent")
    return f"{parent}/{name}" if parent else name


def _resolve_targets(args, state, channels):
    """読む対象チャンネルを決める。channel指定があればそれ、無ければ watch=true の全部。"""
    if args.get("channel") is not None:
        cid = resolve_channel(args.get("channel"), channels)
        if not cid:
            raise CommandError(
                t("dsat_channel_not_found", channel=args.get("channel")),
                hint=t("dsat_channel_not_found_hint"),
                available=[
                    {"channel_id": k, "name": _channel_label(v, k)} for k, v in channels.items()
                ],
            )
        return [(cid, channels.get(cid) or {"name": cid})]

    watching = [(cid, info) for cid, info in channels.items() if info.get("watch")]
    if not watching:
        raise CommandError(
            t("dsat_read_no_watch"),
            hint=t("dsat_read_no_watch_hint"),
            example={"command": "channels", "channel": "雑談", "watch": True},
            available=[
                {"channel_id": k, "name": _channel_label(v, k)} for k, v in channels.items()
            ],
        )
    return watching


def _summarize_message(msg, state, channels, content_limit):
    """1件のメッセージを、柚月が読める最小限のフィールドへ整形する（ホワイトリスト方式）。"""
    bot_user_id = state.get("bot_user_id")
    author = msg.get("author") or {}
    raw_content = msg.get("content") or ""
    content = humanize_content(raw_content, msg, channels)

    attachments = summarize_attachments(msg)
    system = is_system_message(msg)

    # 本文が空のケースは「何があったか」を必ず伝える（添付だけ・参加通知など）
    if not content:
        if attachments or msg.get("embeds"):
            content = t("dsat_read_attachment_only",
                        n=len(msg.get("attachments") or []) + len(msg.get("embeds") or []))
        elif system:
            content = t("dsat_read_system_message")
        elif msg.get("sticker_items"):
            content = t("dsat_read_sticker_only")

    is_me = str(author.get("id")) == str(bot_user_id) if bot_user_id else False
    ref = msg.get("referenced_message") or {}

    out = {
        "id": str(msg.get("id")),
        "author": author_name(msg),
        "is_bot": bool(author.get("bot", False)),
        "is_me": is_me,
        "time": snowflake_to_jst(msg.get("id")),
        "content": clip(content, content_limit),
        "mentions_me": (not is_me) and mentions_me(msg, bot_user_id),
        "names_me": (not is_me) and names_me(raw_content, state.get("name_keywords")),
        "reply_to": author_name(ref) if ref else None,
    }
    # 以下は該当するときだけ載せる（毎件のノイズを増やさない）
    reactions = summarize_reactions(msg)
    if reactions:
        out["reactions"] = reactions
    if attachments:
        out["attachments"] = attachments
    if system:
        out["system"] = True
    return out


def _fit_to_budget(channels_out, mentions_summary, pending_cursor,
                   budget=RESPONSE_BUDGET, extras=None):
    """応答を予算内へ、構造を保ったまま畳む（最後の砦に潰させない）。

    新しい側のメッセージから外し、外したチャンネルの既読カーソルは
    「実際に返した最後のメッセージ」までしか進めない（pending_cursor を書き換える）。
    外れたメッセージは未読のまま残るので、次の read で必ずそのまま届く。
    extras には測定にだけ含めたい応答項目（skipped / deferred 等）を渡す。
    戻り値: 畳んだかどうか。
    """
    def _len(obj):
        return len(json.dumps(obj, ensure_ascii=False))

    trimmed = set()
    while True:
        # note を付けた完成形で毎回測り直す（近似減算で予算を踏み外さない）
        for e in channels_out:
            if e["channel_id"] in trimmed:
                e["truncated"] = True
                e["truncated_note"] = t("dsat_read_deferred_note")
        if _len({"channels": channels_out,
                 "mentions_summary": mentions_summary,
                 "extras": extras or {}}) <= budget:
            return bool(trimmed)

        # いちばん場所を取っているチャンネルの、いちばん新しいメッセージから外す
        entry = max((e for e in channels_out if e["messages"]),
                    key=lambda e: _len(e["messages"]), default=None)
        if entry is None:
            return bool(trimmed)
        dropped = entry["messages"].pop()
        entry["new_count"] = len(entry["messages"])
        cid = entry["channel_id"]
        trimmed.add(cid)

        # 外したメッセージ宛のメンション要約も外す（次の read で本文ごと届く）
        mentions_summary[:] = [m for m in mentions_summary
                               if m["message_id"] != dropped["id"]]

        # カーソルは返せた分まで。全部外れたら前回位置のまま動かさない
        if entry["messages"]:
            pending_cursor[cid] = entry["messages"][-1]["id"]
        else:
            pending_cursor.pop(cid, None)


def _fetch(cid, path):
    """メッセージ取得。権限なし・消滅チャンネルは None を返して巡回を止めない。"""
    try:
        messages = request("GET", path)
    except APIError as e:
        if e.status in (403, 404):
            return None, e.status
        raise
    if not isinstance(messages, list):
        messages = []
    # Discordは新しい順で返すため、必ず古い順へ揃えてから扱う
    messages.sort(key=lambda m: int(m.get("id", 0)))
    return messages, None


def _read_history(args, state, channels, limit, content_limit):
    """
    before 指定の過去ログ読み。既読カーソルは絶対に動かさない（pull の安全網を壊さない）。
    ルール・自己紹介・ペア登録など、参加前の流れを知るための経路。
    """
    if args.get("channel") is None:
        raise CommandError(
            t("dsat_read_before_need_channel"),
            hint=t("dsat_read_before_need_channel_hint"),
            example={"command": "read", "channel": "ペア登録", "before": "latest"},
            available=[
                {"channel_id": k, "name": _channel_label(v, k)} for k, v in channels.items()
            ],
        )
    (cid, info), = _resolve_targets(args, state, channels)

    before = str(args.get("before")).strip()
    if before.lower() == "latest" or not before.isdigit():
        path = f"/channels/{cid}/messages?limit={limit}"
    else:
        path = f"/channels/{cid}/messages?before={before}&limit={limit}"

    messages, err = _fetch(cid, path)
    if messages is None:
        raise CommandError(
            t("dsat_read_skipped_channel", status=err),
            hint=t("dsat_hint_403") if err == 403 else t("dsat_hint_404"),
        )

    known = get_known_users(state)
    changed = False
    for m in messages:
        changed = collect_users(m, known) or changed
    if changed:
        state["known_users"] = dict(list(known.items())[-KNOWN_USERS_MAX:])
        save_state(state)

    summarized = [_summarize_message(m, state, channels, content_limit) for m in messages]

    # 応答の予算に収める（古い側から畳む）。カーソルは元々動かさない経路なので
    # 消失はなく、畳んだ分は example の before= でそのまま遡り直せる。
    # raw=true（content_limit なし）は全文要求なので予算だけ大きくする。
    budget = (RESPONSE_BUDGET if content_limit is not None
              else RAW_RESPONSE_BUDGET - BUDGET_MARGIN)
    trimmed = False
    while len(summarized) > 1 and \
            len(json.dumps(summarized, ensure_ascii=False)) > budget:
        summarized.pop(0)
        trimmed = True

    out = {
        "history": True,
        "channel": _channel_label(info, cid),
        "channel_id": cid,
        "count": len(summarized),
        "messages": summarized,
        "note": t("dsat_read_history_note"),
    }
    if summarized and (len(messages) >= limit or trimmed):
        oldest = summarized[0]["id"]
        out["older_available"] = True
        out["hint"] = t("dsat_read_history_more_hint")
        out["example"] = {"command": "read", "channel": cid, "before": oldest}
    elif not summarized:
        out["note"] = t("dsat_read_history_end")
    return out


def cmd_read(args):
    """
    前回 read した位置から先の新着メッセージを取得する。

    カーソル(last_read_id)は read が成功したチャンネルだけ前進する。これにより、
    常駐ブリッジ(push)が寝ている間に取りこぼしたメンションも、次の read で必ず拾える。
    peek=true のときはカーソルを進めない（覗くだけ）。
    before を指定すると過去ログを遡る（カーソルは動かない）。
    """
    state = load_state()
    if not state.get("bot_user_id"):
        raise CommandError(
            t("dsat_need_setup"),
            hint=t("dsat_need_setup_hint"),
            example={"command": "setup"},
        )

    channels = get_channels(state)

    limit_given = args.get("limit") not in (None, "", 0)
    try:
        limit = int(args.get("limit") or DEFAULT_LIMIT)
    except (TypeError, ValueError):
        limit = DEFAULT_LIMIT
    limit = max(1, min(limit, MAX_LIMIT))

    peek = parse_bool(args.get("peek"))
    raw = parse_bool(args.get("raw"))
    content_limit = None if raw else CONTENT_CLIP

    if args.get("before") not in (None, ""):
        return _read_history(args, state, channels, limit, content_limit)

    targets = _resolve_targets(args, state, channels)

    channels_out = []
    mentions_summary = []
    skipped = []
    deferred = []
    pending_cursor = {}
    known = get_known_users(state)
    users_changed = False
    deadline = _PROCESS_START + TIME_BUDGET

    for cid, info in targets:
        # 時間予算を使い切ったら、残りは未読のまま次回へ回す（親の強制終了で
        # 応答ごと消えるより、途中までの結果を構造化して返す方が必ず良い）
        if time.monotonic() > deadline:
            deferred.append({"channel": _channel_label(info, cid), "channel_id": cid})
            continue

        last_read_id = info.get("last_read_id")
        if last_read_id:
            fetch_limit = limit
            path = f"/channels/{cid}/messages?after={last_read_id}&limit={fetch_limit}"
        else:
            # 初回は直近だけ。ただし limit を明示されたらその分だけ遡る
            fetch_limit = limit if limit_given else FIRST_READ_LIMIT
            path = f"/channels/{cid}/messages?limit={fetch_limit}"

        messages, err = _fetch(cid, path)
        if messages is None:
            skipped.append({
                "channel": _channel_label(info, cid),
                "channel_id": cid,
                "reason": t("dsat_read_skipped_channel", status=err),
            })
            continue

        for m in messages:
            users_changed = collect_users(m, known) or users_changed

        summarized = [
            _summarize_message(m, state, channels, content_limit) for m in messages
        ]

        label = _channel_label(info, cid)
        for s in summarized:
            if s["mentions_me"] or s["names_me"]:
                mentions_summary.append({
                    "channel": label,
                    "channel_id": cid,
                    "message_id": s["id"],
                    "author": s["author"],
                    "excerpt": clip(s["content"], EXCERPT_CLIP),
                })

        entry = {
            "channel": label,
            "channel_id": cid,
            "new_count": len(summarized),
            "messages": summarized,
        }
        # 取り切れなかった可能性があることは必ず伝える（黙って捨てない）
        if len(messages) >= fetch_limit:
            entry["truncated"] = True
            entry["truncated_note"] = t("dsat_read_truncated_hint")
        channels_out.append(entry)

        if messages:
            pending_cursor[cid] = str(max(int(m.get("id", 0)) for m in messages))

    # 応答が大きすぎるときは、カーソルを保存する前に構造を保ったまま畳む。
    # ここが切り詰めより先だから「畳んだ分は既読にならず、次の read で届く」が成立する。
    # raw=true は全文要求なので予算だけ大きくする（無制限にはしない）。
    budget_trimmed = _fit_to_budget(
        channels_out, mentions_summary, pending_cursor,
        budget=(RAW_RESPONSE_BUDGET - BUDGET_MARGIN) if raw else RESPONSE_BUDGET,
        extras={"skipped_channels": skipped, "deferred_channels": deferred})

    info_by_cid = dict(targets)
    if pending_cursor and not peek:
        for cid, newest in pending_cursor.items():
            if cid in channels:
                channels[cid]["last_read_id"] = newest
            else:
                info = info_by_cid.get(cid) or {}
                channels[cid] = {"name": info.get("name") or cid,
                                 "last_read_id": newest, "watch": False,
                                 "kind": "channel"}
        state["channels"] = channels

    if users_changed:
        state["known_users"] = dict(list(known.items())[-KNOWN_USERS_MAX:])
    if (pending_cursor and not peek) or users_changed:
        save_state(state)

    total_new = sum(c["new_count"] for c in channels_out)

    out = {
        "channels": channels_out,
        "total_new": total_new,
        "mentions_summary": mentions_summary,
        "peeked": peek,
    }
    if skipped:
        out["skipped_channels"] = skipped
    if deferred:
        out["deferred_channels"] = deferred
        out["deferred_note"] = t("dsat_read_time_deferred_note")

    if total_new == 0 and not deferred and not budget_trimmed:
        out["note"] = t("dsat_read_nothing_new")
    elif mentions_summary:
        first = mentions_summary[0]
        out["hint"] = t("dsat_read_hint_reply")
        out["example"] = {
            "command": "reply",
            "channel": first["channel_id"],
            "message_id": first["message_id"],
            "message": "（返事の本文）",
        }
    elif total_new == 0 and budget_trimmed:
        # 全件を次回に回したときに「新着なし」と矛盾する案内をしない。
        # 続きの取り方（もう一度 read）をトップレベルでも示す。
        out["hint"] = t("dsat_read_truncated_hint")
        out["example"] = {"command": "read"}
    elif total_new == 0 and deferred:
        pass  # deferred_note が案内済み
    else:
        out["hint"] = t("dsat_read_hint_post")
        out["example"] = {
            "command": "post",
            "channel": channels_out[0]["channel_id"] if channels_out else "",
            "message": "（話しかける本文）",
        }

    return out


COMMANDS = {
    "read": cmd_read,
}
