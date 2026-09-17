"""timeline / note / notifications / user_notes: 読む

直叩き（web_request）に対する最大の付加価値がここ。生JSONは長大で読む気が起きないので、
1ノート=数行に要約して返す。全文が要るときは raw: true で切り替えられる。
"""
from _i18n import t
from api import request, resolve_user_id
from helpers import (CommandError, fit_to_budget, next_until_id, parse_bool,
                     parse_limit, summarize_note, summarize_notes,
                     summarize_notification)


TIMELINE_KINDS = {
    "home": "notes/timeline",
    "local": "notes/local-timeline",
    "social": "notes/hybrid-timeline",
    "global": "notes/global-timeline",
}

# note コマンドで一緒に取る前後の件数（深追いはしない。必要なら note を辿れる）
THREAD_LIMIT = 10

# note は本体・祖先・返信の3つを一度に返すので、周囲は本体より控えめな枠に収める
THREAD_SECTION_BUDGET = 4000


def _limit(args, default=10):
    try:
        return parse_limit(args.get("limit"), default=default)
    except ValueError:
        raise CommandError(
            t("msat_bad_limit", value=args.get("limit")),
            hint=t("msat_bad_limit_hint"),
            example={"command": "timeline", "limit": 10},
        )


def _paged(summarized, key, extra=None):
    """一覧系の共通の返し方（大きさを収める＋続き読みカーソル）。

    大きすぎる一覧は末尾から落とす。カーソルは「残した最後の要素」から作るので、
    落とした分は until_id で続きを読めば取りこぼさない。
    """
    kept, dropped = fit_to_budget(summarized)

    out = dict(extra or {})
    out[key] = kept
    out["count"] = len(kept)
    if dropped:
        out["dropped"] = dropped
        out["dropped_note"] = t("msat_dropped_note", n=dropped)
    out["next_until_id"] = next_until_id(kept)
    if out["next_until_id"]:
        out["hint"] = t("msat_hint_next_page")
    return out


def cmd_timeline(args):
    """タイムラインを読む。"""
    kind = str(args.get("kind") or "home").strip()
    if kind not in TIMELINE_KINDS:
        raise CommandError(
            t("msat_bad_kind", value=kind),
            hint=t("msat_bad_kind_hint", allowed=", ".join(TIMELINE_KINDS)),
            example={"command": "timeline", "kind": "home", "limit": 10},
        )

    endpoint = TIMELINE_KINDS[kind]
    body = {"limit": _limit(args)}
    if args.get("until_id"):
        body["untilId"] = str(args["until_id"])

    notes = request(endpoint, body) or []
    if parse_bool(args.get("raw")):
        return {"raw": {endpoint: notes}}

    return _paged(summarize_notes(notes), "notes", {"kind": kind})


def cmd_note(args):
    """ノート1件を、そこへ至る文脈と直接の返信つきで読む。

    notes/conversation が返すのは **そのノートへ至る返信元の連鎖（祖先）** であって、
    そのノートへの返信一覧ではない。返信は notes/replies で別に取る。
    """
    note_id = args.get("note_id")
    if not note_id:
        raise CommandError(
            t("msat_note_need_note_id"),
            hint=t("msat_note_need_note_id_hint"),
            example={"command": "note", "note_id": "9xxxxxxxxx"},
        )
    note_id = str(note_id)

    note = request("notes/show", {"noteId": note_id}) or {}
    ancestors = request("notes/conversation",
                        {"noteId": note_id, "limit": THREAD_LIMIT}) or []
    replies = request("notes/replies",
                      {"noteId": note_id, "limit": THREAD_LIMIT}) or []

    if parse_bool(args.get("raw")):
        return {"raw": {
            "notes/show": note,
            "notes/conversation": ancestors,
            "notes/replies": replies,
        }}

    # 名指しで開いたノートなので CW があっても本文まで見せる。
    # 周囲（祖先・返信）は一覧扱いのままなので CW は伏せる
    kept_anc, drop_anc = fit_to_budget(summarize_notes(ancestors),
                                       THREAD_SECTION_BUDGET)
    kept_rep, drop_rep = fit_to_budget(summarize_notes(replies),
                                       THREAD_SECTION_BUDGET)

    out = {
        "note": summarize_note(note, hide_cw=False),
        "ancestors": kept_anc,
        "ancestors_note": t("msat_note_ancestors_note"),
        "replies": kept_rep,
    }
    if drop_anc or drop_rep:
        out["dropped"] = drop_anc + drop_rep
        out["dropped_note"] = t("msat_dropped_thread", n=drop_anc + drop_rep)
    return out


def cmd_notifications(args):
    """通知を読む。

    Misskey の i/notifications は既定で **取得しただけで全通知を既読にする**
    （markAsRead の default が true）。見るだけのつもりで状態を変えないよう、
    ここでは常に明示送信し、既定を false にしている。
    既読にしたいときだけ mark_as_read: true を指定する。
    """
    mark_as_read = parse_bool(args.get("mark_as_read"), default=False)
    body = {"limit": _limit(args), "markAsRead": mark_as_read}
    if args.get("until_id"):
        body["untilId"] = str(args["until_id"])

    items = request("i/notifications", body) or []
    if parse_bool(args.get("raw")):
        return {"raw": {"i/notifications": items}}

    summarized = [n for n in (summarize_notification(i) for i in items) if n]
    return _paged(summarized, "notifications", {"marked_as_read": mark_as_read})


def cmd_user_notes(args):
    """特定のユーザーの投稿を読む。"""
    user = args.get("user")
    if not user:
        raise CommandError(
            t("msat_need_user"),
            hint=t("msat_need_user_hint"),
            example={"command": "user_notes", "user": "@alice", "limit": 10},
        )

    user_id = resolve_user_id(user)
    if not user_id:
        raise CommandError(
            t("msat_user_not_found", user=user),
            hint=t("msat_user_not_found_hint"),
            example={"command": "user_notes", "user": "@alice@example.com"},
        )

    body = {"userId": user_id, "limit": _limit(args)}
    if args.get("until_id"):
        body["untilId"] = str(args["until_id"])

    notes = request("users/notes", body) or []
    if parse_bool(args.get("raw")):
        return {"raw": {"users/notes": notes}}

    return _paged(summarize_notes(notes), "notes",
                  {"user": str(user), "user_id": user_id})


COMMANDS = {
    "timeline": cmd_timeline,
    "note": cmd_note,
    "notifications": cmd_notifications,
    "user_notes": cmd_user_notes,
}
