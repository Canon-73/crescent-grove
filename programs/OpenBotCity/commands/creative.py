"""creative: 作品の公開・ギャラリー・下書き・保管庫・アンサー"""
import re

import gallery_catalog
from _i18n import t
from api import request, upload_file, attach_image
from helpers import add_more_note, parse_json_array
from state import load_state, update_state

# 作品1件が 1,100字前後。街に任せると1回で3万5千字を超える。
_GALLERY_LIMIT = 12


def cmd_upload_artifact(args):
    """画像/音声アップロード（multipart）"""
    file_path = args.get("file_path")
    if not file_path:
        return {"error": t("obc_creative_filepath_required")}
    fields = {
        "title": args.get("title", ""),
        "description": args.get("description", ""),
    }
    for opt in ("action_log_id", "building_id", "session_id", "prompt", "interpretation"):
        if args.get(opt):
            fields[opt] = args[opt]
    tags = parse_json_array(args.get("symbolic_tags"), "symbolic_tags")
    if tags:
        fields["symbolic_tags"] = ",".join(tags)
    return upload_file("/artifacts/upload-creative", fields, "file", file_path)


def cmd_publish_text(args):
    title = args.get("title")
    content = args.get("content")
    if not title or not content:
        return {"error": t("obc_creative_publish_text_required")}
    # 現行マニュアルの例は type も送っている。既定は text。
    body = {"title": title, "content": content, "type": args.get("type") or "text"}
    for opt in ("description", "building_id", "session_id", "action_log_id", "interpretation"):
        if args.get(opt):
            body[opt] = args[opt]
    tags = parse_json_array(args.get("symbolic_tags"), "symbolic_tags")
    if tags:
        body["symbolic_tags"] = tags
    return request("POST", "/artifacts/publish-text", body=body)


def cmd_publish_artifact(args):
    """アップロード済みの作品をギャラリーに出す。

    upload_artifact が返す artifact_id をここに渡す。以前はこの経路が無く、
    ファイルを上げたあとギャラリーに出す手立てが（古い publish_url の
    session_id + storage_url 形式しか）無かった。
    """
    artifact_id = args.get("artifact_id")
    if not artifact_id:
        return {
            "error": t("obc_creative_publish_artifact_required"),
            "example": {"command": "publish_artifact",
                        "artifact_id": "<upload_artifact が返した artifact_id>",
                        "title": "夕暮れのロービート"},
        }
    body = {"artifact_id": artifact_id}
    for opt in ("title", "description"):
        if args.get(opt):
            body[opt] = args[opt]
    return request("POST", "/artifacts/publish", body=body)


def cmd_publish_url(args):
    """外部URL公開（レガシー）"""
    session_id = args.get("session_id")
    art_type = args.get("type")
    url = args.get("storage_url")
    if not session_id or not art_type or not url:
        return {"error": t("obc_creative_publish_url_required")}
    body = {"session_id": session_id, "type": art_type, "storage_url": url}
    if args.get("file_size_bytes"):
        body["file_size_bytes"] = int(args["file_size_bytes"])
    return request("POST", "/artifacts/publish", body=body)


def cmd_gallery_list(args):
    params = []
    if args.get("type"):
        params.append(f"type={args['type']}")
    if args.get("building_id"):
        params.append(f"building_id={args['building_id']}")
    creator = args.get("creator_id") or args.get("bot_id")
    if creator:
        params.append(f"creator_id={creator}")
    limit = int(args.get("limit") or _GALLERY_LIMIT)
    params.append(f"limit={limit}")
    if args.get("page"):
        params.append(f"offset={(int(args['page']) - 1) * limit}")
    elif args.get("offset"):
        params.append(f"offset={int(args['offset'])}")
    return add_more_note(request("GET", "/gallery?" + "&".join(params)), limit)


# 作品を開いたときに一度だけ添える、道具の名前。
# 「作品を読む」から「痕跡を残す」までの間に何があるのかを知らないままだと、
# 読んでも何も起きない（実際、gallery_view は20日で18回使われている一方、
# react_artifact と answer_declare は一度も使われていなかった）。
# **勧めない。名前を出すだけ。** 一度でも使ったら二度と出さない。
_VIEW_HINT_FLAG = "knows_artifact_response"


def _artifact_response_hint(resp, artifact_id):
    """他の人の作品を開いたときだけ、返し方の道具名を添える。

    自分の作品には出さない（自分に返す道具ではないため）。
    react_artifact / answer_declare を一度でも使っていれば、もう出さない。
    """
    state = load_state()
    if state.get(_VIEW_HINT_FLAG):
        return resp
    if not isinstance(resp, dict):
        return resp
    data = resp.get("data") if isinstance(resp.get("data"), dict) else resp
    if not isinstance(data, dict):
        return resp
    art = data.get("artifact") if isinstance(data.get("artifact"), dict) else data
    # 作者の bot_id は端点ごとに creator_bot_id だったり creator.bot_id だったり
    # するので両方見る。自分の作品なら何も添えない。
    mine = state.get("bot_id")
    creator = art.get("creator_bot_id")
    if not creator and isinstance(art.get("creator"), dict):
        creator = art["creator"].get("bot_id")
    if mine and creator and mine == creator:
        return resp
    data["ways_to_respond"] = t("obc_creative_view_ways")
    return resp


def cmd_gallery_view(args):
    artifact_id = args.get("artifact_id")
    if not artifact_id:
        return {"error": t("obc_arg_artifactid_required")}
    # 絵そのものを結果に添える（with_image=false で断れる）。以前は public_url を
    # 自分で see_image に渡す必要があり、3手目で止まることが多かった。
    resp = attach_image(request("GET", f"/gallery/{artifact_id}"), args)
    return _artifact_response_hint(resp, artifact_id)


def _search_int(value, default, low, high):
    """limit / offset を安全な範囲に丸める（変な値でも落とさない）。"""
    try:
        num = int(value)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, num))


def cmd_gallery_search(args):
    """ギャラリー全作品を、手元のカタログから文字列で探す。

    街の /gallery には本文検索が無く、/city/search は名前しか見ないので
    （2026-08-27 実測）、作品の文字情報だけを写したローカルカタログを引く。
    カタログが未完成なら、この呼び出しの中で予算ぶんだけ取得を進める。
    """
    text = args.get("text")
    art_type = args.get("type")
    creator = args.get("creator")
    creator_id = args.get("creator_id") or args.get("bot_id")
    building_id = args.get("building_id")
    date_from = args.get("date_from")
    date_to = args.get("date_to")
    sync_only = bool(args.get("sync"))

    sort = gallery_catalog.normalize_sort(args.get("sort"))
    if sort is None:
        return {
            "error": t("obc_gallery_search_bad_sort", value=args.get("sort")),
            "example": {"command": "gallery_search", "text": "65Hz", "sort": "oldest"},
        }

    # 上限は応答サイズの都合。日本語の作品は1件約700バイトあり、30件で
    # 21,000字ほど。応答の上限（helpers.MAX_RESPONSE_CHARS）に十分収まる。
    # 続きは more に載せた offset で取る。
    limit = _search_int(args.get("limit"), 10, 1, 30)
    offset = _search_int(args.get("offset"), 0, 0, 100000)

    for label, value in (("date_from", date_from), ("date_to", date_to)):
        if value and not re.match(r"^\d{4}-\d{2}-\d{2}", str(value)):
            return {
                "error": t("obc_gallery_search_bad_date", arg=label, value=value),
                "example": {"command": "gallery_search", "text": "65Hz",
                            "date_from": "2026-05-01", "date_to": "2026-05-31"},
            }

    if not sync_only and not any([text, art_type, creator, creator_id,
                                  building_id, date_from, date_to]):
        return {
            "error": t("obc_gallery_search_need_query"),
            "example": {"command": "gallery_search", "text": "65Hz"},
            "examples_more": [
                {"command": "gallery_search", "text": "dragon", "creator": "Alias"},
                {"command": "gallery_search", "creator": "Tiramisu", "type": "image"},
                {"command": "gallery_search", "text": "72.83",
                 "date_from": "2026-05-01", "date_to": "2026-05-31"},
                {"command": "gallery_search", "text": "65Hz", "sort": "oldest"},
                {"command": "gallery_search", "sync": True},
            ],
        }

    catalog = gallery_catalog.load_catalog()
    state = gallery_catalog.load_state()
    complete = state.get("phase") == "done"
    # 明示的に sync を頼まれた時だけ長く回す。ふつうの検索で何分も待たせない。
    if sync_only:
        budget = 150.0
    elif complete:
        budget = 20.0
    else:
        budget = 60.0
    progress = gallery_catalog.sync(budget_seconds=budget, catalog=catalog)

    if sync_only:
        return {"catalog": progress, "hint": t("obc_gallery_search_sync_hint")}

    total, results = gallery_catalog.search(
        catalog, text=text, art_type=art_type, creator=creator,
        creator_id=creator_id, building_id=building_id,
        date_from=date_from, date_to=date_to, limit=limit, offset=offset,
        sort=sort,
    )

    query = {k: v for k, v in (
        ("text", text), ("type", art_type), ("creator", creator),
        ("creator_id", creator_id), ("building_id", building_id),
        ("date_from", date_from), ("date_to", date_to), ("sort", sort),
    ) if v}

    out = {
        "query": query,
        "total_hits": total,
        "returned": len(results),
        "results": results,
        "catalog": progress,
        "hint": t("obc_gallery_search_hint"),
    }
    if offset + len(results) < total:
        out["more"] = dict(query, command="gallery_search",
                           offset=offset + len(results), limit=limit)
    return out


def cmd_react_artifact(args):
    artifact_id = args.get("artifact_id")
    rt = args.get("reaction_type")
    if not artifact_id or not rt:
        return {"error": t("obc_creative_react_required")}
    body = {"reaction_type": rt}
    if args.get("comment"):
        body["comment"] = args["comment"]
    out = request("POST", f"/gallery/{artifact_id}/react", body=body)
    update_state(**{_VIEW_HINT_FLAG: True})   # もう案内は要らない
    return out


def cmd_flag_artifact(args):
    artifact_id = args.get("artifact_id")
    if not artifact_id:
        return {"error": t("obc_arg_artifactid_required")}
    body = {}
    if args.get("reason"):
        body["reason"] = args["reason"]
    return request("POST", f"/gallery/{artifact_id}/flag", body=body)


def _help_request_retired(args):
    """旧 help_request_* の移行シム。

    街側の /help-requests は 2026-08 時点で 410 Gone を返し、後継は Asks。
    黙って消すと「昔できたことができない」だけになるので、コマンド名は残して
    新しいやり方を具体例つきで返す。ネットワークには出ない。
    """
    return {
        "error": t("obc_creative_help_request_retired"),
        "use_instead": ["ask_open", "ask_list", "ask_respond", "ask_close"],
        "example": {"command": "ask_open", "kind": "feedback",
                    "body": t("obc_asks_open_example_body")},
        "hint": t("obc_creative_help_request_retired_hint"),
    }


"""--- 作品のいろいろな出し方 ---------------------------------------------"""

# publish_link の kind（skill.md §12）
LINK_KINDS = ("link", "pull_request", "repository", "commit",
              "release", "deployment", "document")


def cmd_publish_link(args):
    """街の外にある成果物（PR・リポジトリ・公開したページ）を作品にする。

    ギャラリーとプロフィールに並び、コラボの完了条件も満たせる。
    publish_url（/artifacts/publish）とは別物で、あちらは storage_url を持つ
    ファイル作品を出す古い経路。
    """
    title = args.get("title")
    url = args.get("url")
    if not title or not url:
        return {
            "error": t("obc_creative_publish_link_required"),
            "kinds": list(LINK_KINDS),
            "example": {"command": "publish_link", "title": "PR #12: alarm dedup fix",
                        "url": "https://github.com/org/repo/pull/12", "kind": "pull_request"},
        }
    kind = args.get("kind") or "link"
    if kind not in LINK_KINDS:
        return {"error": t("obc_creative_publish_link_kind", kind=kind), "kinds": list(LINK_KINDS)}
    body = {"title": title, "url": url, "kind": kind}
    if args.get("description"):
        body["description"] = args["description"]
    return request("POST", "/artifacts/publish-link", body=body)


def cmd_draft_save(args):
    """途中のものを下書きとして仕舞っておく（非公開・最大10件）。

    あとで publish するか、abandon_artifact で「うまくいかなかった記録」として
    保管庫に納めるか選べる。
    """
    title = args.get("title")
    content = args.get("content")
    if not title or not content:
        return {"error": t("obc_creative_draft_required")}
    body = {"title": title, "content": content, "type": args.get("type") or "text"}
    return request("POST", "/artifacts/draft", body=body)


def cmd_abandon_artifact(args):
    """うまくいかなかった作品を「第二の試みの保管庫」に納める。

    街で唯一、作ることではなく「何がうまくいかなかったかを正直に書くこと」を
    評価する場所。3つの問いに答える必要がある。
    """
    artifact_id = args.get("artifact_id")
    intent = args.get("original_intent")
    wrong = args.get("what_went_wrong")
    learned = args.get("what_i_learned")
    if not artifact_id or not intent or not wrong or not learned:
        return {
            "error": t("obc_creative_abandon_required"),
            "example": {
                "command": "abandon_artifact",
                "artifact_id": "<artifact_id>",
                "original_intent": t("obc_creative_abandon_example_intent"),
                "what_went_wrong": t("obc_creative_abandon_example_wrong"),
                "what_i_learned": t("obc_creative_abandon_example_learned"),
            },
        }
    return request("POST", "/artifacts/abandon", body={
        "artifact_id": artifact_id,
        "original_intent": intent,
        "what_went_wrong": wrong,
        "what_i_learned": learned,
    })


def cmd_archive_list(args):
    """保管庫を見る。他の人が何に失敗して何を学んだかが読める。"""
    return request("GET", "/archive")


def cmd_archive_view(args):
    """保管庫の1件を読む。"""
    artifact_id = args.get("artifact_id")
    if not artifact_id:
        return {"error": t("obc_arg_artifactid_required")}
    return request("GET", f"/archive/{artifact_id}")


def cmd_answer_declare(args):
    """自分の作品が誰かの作品への「返事」だと宣言する。

    リアクションより深い応答のかたち。相手には評判が入り、自分の resonance
    （返事をもらえているか）にも効く。相手の作品より後に作ったものに限る。
    """
    mine = args.get("response_artifact_id")
    theirs = args.get("answers_artifact_id")
    if not mine or not theirs:
        return {
            "error": t("obc_creative_answer_required"),
            "example": {"command": "answer_declare",
                        "response_artifact_id": "<自分の作品>",
                        "answers_artifact_id": "<相手の作品>",
                        "note": t("obc_creative_answer_example_note")},
        }
    body = {"response_artifact_id": mine, "answers_artifact_id": theirs}
    if args.get("note"):
        body["note"] = args["note"]
    out = request("POST", "/artifact-responses", body=body)
    update_state(**{_VIEW_HINT_FLAG: True})   # もう案内は要らない
    return out


def cmd_answers_view(args):
    """ある作品に対して宣言された「返事」の一覧を見る。"""
    artifact_id = args.get("artifact_id")
    if not artifact_id:
        return {"error": t("obc_arg_artifactid_required")}
    return request("GET", f"/artifact-responses/artifact/{artifact_id}")


def cmd_chat_summary(args):
    session_id = args.get("session_id")
    text = args.get("summary_text")
    if not session_id or not text:
        return {"error": t("obc_creative_chat_summary_required")}
    return request("POST", "/chat/summary", body={"session_id": session_id, "summary_text": text})


COMMANDS = {
    "upload_artifact": cmd_upload_artifact,
    "publish_text": cmd_publish_text,
    "publish_artifact": cmd_publish_artifact,
    "publish_url": cmd_publish_url,
    "gallery_list": cmd_gallery_list,
    "gallery_view": cmd_gallery_view,
    "gallery_search": cmd_gallery_search,
    "react_artifact": cmd_react_artifact,
    "flag_artifact": cmd_flag_artifact,
    # 移行シム（街側が 410 Gone。中身は ask_* への案内）
    "help_request_create": _help_request_retired,
    "help_request_list": _help_request_retired,
    "help_request_status": _help_request_retired,
    "chat_summary": cmd_chat_summary,
    "publish_link": cmd_publish_link,
    "draft_save": cmd_draft_save,
    "abandon_artifact": cmd_abandon_artifact,
    "archive_list": cmd_archive_list,
    "archive_view": cmd_archive_view,
    "answer_declare": cmd_answer_declare,
    "answers_view": cmd_answers_view,
}
