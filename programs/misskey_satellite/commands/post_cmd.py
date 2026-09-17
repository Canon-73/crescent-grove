"""post / react / delete: 柚月から外へ出ていく経路

送信前にローカルで弾けるものは弾き、正しい呼び出し例を添えて返す。
サーバに投げてから汎用エラーを読ませるより、その場で直せる方が早い。
"""
from _i18n import t
from api import (MAX_CW_LENGTH, MAX_NOTE_TEXT_LENGTH, get_base_url, request)
from helpers import CommandError, summarize_note


# specified（宛先指定＝DM相当）は宛先引数が別に要るうえ利用実績がないため v1 では扱わない。
# 必要になったら web_request の直叩きで送れる。
ALLOWED_VISIBILITY = ("public", "home", "followers")

_POST_EXAMPLE = {"command": "post", "text": "（本文）"}


def _validate_text(args):
    """本文まわりの検証。戻り値: (text or None, renote_id or None)

    Misskey 側の制約:
      - text は 1〜3000 文字
      - Renote・添付・投票のいずれも無いノートは、空白だけの本文も不可
      - 純 Renote（renoteId のみ）は text 省略可
    """
    text = args.get("text")
    renote_id = args.get("renote_id")
    text_given = text is not None

    if not text_given and not renote_id:
        raise CommandError(
            t("msat_post_need_text"),
            hint=t("msat_post_need_text_hint"),
            example=_POST_EXAMPLE,
        )

    if text_given:
        text = str(text)
        if not text.strip():
            # 引用のつもりで中身が空、も同じ扱い（何も言っていない投稿を出さない）
            raise CommandError(
                t("msat_post_blank_text"),
                hint=t("msat_post_blank_text_hint"),
                example=_POST_EXAMPLE,
            )
        if len(text) > MAX_NOTE_TEXT_LENGTH:
            # 勝手に分割しない。どう分けるかは柚月が決めること
            raise CommandError(
                t("msat_post_too_long", n=len(text), max=MAX_NOTE_TEXT_LENGTH),
                hint=t("msat_post_too_long_hint", max=MAX_NOTE_TEXT_LENGTH),
            )
        return text, renote_id

    return None, renote_id


def _validate_cw(args):
    """CW（閲覧注意の見出し）の検証。未指定なら None。"""
    cw = args.get("cw")
    if cw is None:
        return None
    cw = str(cw)
    if not cw.strip():
        raise CommandError(
            t("msat_post_cw_empty"),
            hint=t("msat_post_cw_empty_hint"),
            example={"command": "post", "text": "（本文）", "cw": "（見出し）"},
        )
    if len(cw) > MAX_CW_LENGTH:
        raise CommandError(
            t("msat_post_cw_too_long", n=len(cw), max=MAX_CW_LENGTH),
            hint=t("msat_post_cw_too_long_hint", max=MAX_CW_LENGTH),
        )
    return cw


def _validate_visibility(args):
    visibility = args.get("visibility")
    if visibility is None or visibility == "":
        return "public"
    visibility = str(visibility).strip()
    if visibility not in ALLOWED_VISIBILITY:
        raise CommandError(
            t("msat_post_bad_visibility", value=visibility),
            hint=t("msat_post_bad_visibility_hint",
                   allowed=", ".join(ALLOWED_VISIBILITY)),
            example={"command": "post", "text": "（本文）", "visibility": "home"},
        )
    return visibility


def cmd_post(args):
    """ノートを投稿する（返信・Renote・引用も同じ入口）。"""
    text, renote_id = _validate_text(args)
    cw = _validate_cw(args)
    visibility = _validate_visibility(args)
    reply_id = args.get("reply_id")

    body = {"visibility": visibility}
    if text is not None:
        body["text"] = text
    if cw is not None:
        body["cw"] = cw
    if reply_id:
        body["replyId"] = str(reply_id)
    if renote_id:
        body["renoteId"] = str(renote_id)

    result = request("notes/create", body) or {}
    created = result.get("createdNote") or {}

    if renote_id and text is None:
        kind = "renote"
    elif renote_id:
        kind = "quote"
    elif reply_id:
        kind = "reply"
    else:
        kind = "note"

    out = {
        "posted": True,
        "kind": kind,
        "visibility": visibility,
        "length": len(text or ""),
        # 自分の投稿なので CW があっても本文は伏せない（自分が今書いたもの）
        "note": summarize_note(created, hide_cw=False),
    }
    if created.get("id"):
        out["url"] = f"{get_base_url()}/notes/{created['id']}"
    return out


def cmd_react(args):
    """ノートにリアクションを付ける。"""
    note_id = args.get("note_id")
    reaction = args.get("reaction")
    if not note_id or not reaction:
        raise CommandError(
            t("msat_react_need_args"),
            hint=t("msat_react_need_args_hint"),
            example={"command": "react", "note_id": "9xxxxxxxxx", "reaction": "👍"},
        )

    # 成功時は 204（空ボディ）で返る。request は None を返すので戻り値を使わない
    request("notes/reactions/create",
            {"noteId": str(note_id), "reaction": str(reaction)})
    return {
        "reacted": True,
        "note_id": str(note_id),
        "reaction": str(reaction),
    }


def cmd_delete(args):
    """自分のノートを削除する（他人のノートはAPI側で弾かれる）。"""
    note_id = args.get("note_id")
    if not note_id:
        raise CommandError(
            t("msat_delete_need_note_id"),
            hint=t("msat_delete_need_note_id_hint"),
            example={"command": "delete", "note_id": "9xxxxxxxxx"},
        )

    request("notes/delete", {"noteId": str(note_id)})
    return {"deleted": True, "note_id": str(note_id)}


COMMANDS = {
    "post": cmd_post,
    "react": cmd_react,
    "delete": cmd_delete,
}
