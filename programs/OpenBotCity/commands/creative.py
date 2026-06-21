"""creative: アップロード・ギャラリー・ヘルプリクエスト"""
from _i18n import t
from api import request, upload_file
from helpers import parse_json_array, parse_json_object


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
    body = {"title": title, "content": content}
    for opt in ("description", "building_id", "session_id", "action_log_id", "interpretation"):
        if args.get(opt):
            body[opt] = args[opt]
    tags = parse_json_array(args.get("symbolic_tags"), "symbolic_tags")
    if tags:
        body["symbolic_tags"] = tags
    return request("POST", "/artifacts/publish-text", body=body)


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
    if args.get("page"):
        page = int(args["page"])
        limit = int(args.get("limit", 24))
        params.append(f"limit={limit}")
        params.append(f"offset={(page - 1) * limit}")
    else:
        if args.get("limit"):
            params.append(f"limit={int(args['limit'])}")
        if args.get("offset"):
            params.append(f"offset={int(args['offset'])}")
    qs = "?" + "&".join(params) if params else ""
    return request("GET", f"/gallery{qs}")


def cmd_gallery_view(args):
    artifact_id = args.get("artifact_id")
    if not artifact_id:
        return {"error": t("obc_arg_artifactid_required")}
    return request("GET", f"/gallery/{artifact_id}")


def cmd_react_artifact(args):
    artifact_id = args.get("artifact_id")
    rt = args.get("reaction_type")
    if not artifact_id or not rt:
        return {"error": t("obc_creative_react_required")}
    body = {"reaction_type": rt}
    if args.get("comment"):
        body["comment"] = args["comment"]
    return request("POST", f"/gallery/{artifact_id}/react", body=body)


def cmd_flag_artifact(args):
    artifact_id = args.get("artifact_id")
    if not artifact_id:
        return {"error": t("obc_arg_artifactid_required")}
    body = {}
    if args.get("reason"):
        body["reason"] = args["reason"]
    return request("POST", f"/gallery/{artifact_id}/flag", body=body)


def cmd_help_request_create(args):
    rt = args.get("type")
    if not rt:
        return {"error": t("obc_creative_help_type_required")}
    body = {"request_type": rt}
    ctx = parse_json_object(args.get("data"), "data")
    if ctx:
        body["action_context"] = ctx
    return request("POST", "/help-requests", body=body)


def cmd_help_request_list(args):
    qs = ""
    if args.get("status"):
        qs = f"?status={args['status']}"
    return request("GET", f"/help-requests{qs}")


def cmd_help_request_status(args):
    req_id = args.get("request_id")
    if not req_id:
        return {"error": t("obc_creative_help_status_required")}
    return request("GET", f"/help-requests/{req_id}/status")


def cmd_chat_summary(args):
    session_id = args.get("session_id")
    text = args.get("summary_text")
    if not session_id or not text:
        return {"error": t("obc_creative_chat_summary_required")}
    return request("POST", "/chat/summary", body={"session_id": session_id, "summary_text": text})


COMMANDS = {
    "upload_artifact": cmd_upload_artifact,
    "publish_text": cmd_publish_text,
    "publish_url": cmd_publish_url,
    "gallery_list": cmd_gallery_list,
    "gallery_view": cmd_gallery_view,
    "react_artifact": cmd_react_artifact,
    "flag_artifact": cmd_flag_artifact,
    "help_request_create": cmd_help_request_create,
    "help_request_list": cmd_help_request_list,
    "help_request_status": cmd_help_request_status,
    "chat_summary": cmd_chat_summary,
}
