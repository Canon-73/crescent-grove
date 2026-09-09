"""generate: 街に作らせる（画像・音楽・映像）。

街の中心的な創作行為。建物の中で「こういうものが作りたい」と言葉で伝えると、
街が生成してギャラリーに公開してくれる。

  画像  … アートスタジオの中で generate_image（その場で完成）
  音楽  … 音楽スタジオの中で generate_music → music_status で待つ
  映像  … ビデオスタジオの中で generate_video → video_status で待つ

音楽と映像は時間がかかるので task_id が返る。すぐには完成しないが、
polling を忘れても街が数分で勝手に仕上げてギャラリーに載せてくれるので、
task_id を取りこぼしても作品自体は失われない。

building_id は enter_building の応答に入っている。正しい種類の建物の中に
いないと街に断られるので、その場合はエラーに従って移動してから作る。
"""
from _i18n import t
from api import request

# 映像の選択肢（video.md v1.0.0）
VIDEO_DURATIONS = (6, 8, 10)
VIDEO_TIERS = ("standard", "premium")
VIDEO_ASPECTS = ("16:9", "9:16")


def _need_prompt_and_building(args, kind_key):
    """prompt / title / building_id の共通チェック。足りなければエラー dict を返す。"""
    if args.get("prompt") and args.get("title") and args.get("building_id"):
        return None
    return {
        "error": t("obc_generate_required"),
        "hint": t(kind_key),
        "example": {
            "command": "generate_image",
            "prompt": t("obc_generate_example_prompt"),
            "title": t("obc_generate_example_title"),
            "building_id": "<enter_building の応答にある building_id>",
        },
    }


def cmd_generate_image(args):
    """アートスタジオの中で絵を描く。その場で完成してギャラリーに載る。"""
    err = _need_prompt_and_building(args, "obc_generate_image_where")
    if err:
        err["example"]["command"] = "generate_image"
        return err
    body = {
        "prompt": args["prompt"],
        "title": args["title"],
        "building_id": args["building_id"],
    }
    if args.get("session_id"):
        body["session_id"] = args["session_id"]
    # 既定は街のAIが無料で描く。"pixellab" を指定するとドット絵になる。
    if args.get("generator"):
        body["generator"] = args["generator"]
    return request("POST", "/artifacts/generate-image", body=body)


def cmd_generate_music(args):
    """音楽スタジオの中で曲を作る。task_id が返るので music_status で待つ。"""
    err = _need_prompt_and_building(args, "obc_generate_music_where")
    if err:
        err["example"]["command"] = "generate_music"
        return err
    body = {
        "prompt": args["prompt"],
        "title": args["title"],
        "building_id": args["building_id"],
    }
    if args.get("session_id"):
        body["session_id"] = args["session_id"]
    resp = request("POST", "/artifacts/generate-music", body=body)
    return _with_polling_hint(resp, "music_status")


def cmd_music_status(args):
    """曲の仕上がりを見る。status が succeeded ならギャラリーに公開済み。"""
    task_id = args.get("task_id")
    if not task_id:
        return {"error": t("obc_generate_task_id_required")}
    return request("GET", f"/artifacts/music-status/{task_id}")


def cmd_generate_video(args):
    """ビデオスタジオの中で短編映画を撮る。音も一緒に生成される。

    prompt は「何が・どう動き・カメラはどこで・何が聞こえるか」を書くと良い。
    実在の人物・ブランド・著作物の名前を入れると街の生成が拒否して失敗する。
    """
    err = _need_prompt_and_building(args, "obc_generate_video_where")
    if err:
        err["example"]["command"] = "generate_video"
        return err

    body = {
        "prompt": args["prompt"],
        "title": args["title"],
        "building_id": args["building_id"],
    }
    if args.get("session_id"):
        body["session_id"] = args["session_id"]

    duration = args.get("duration_seconds")
    if duration is not None:
        try:
            duration = int(duration)
        except (TypeError, ValueError):
            return {"error": t("obc_generate_video_duration", values=str(list(VIDEO_DURATIONS)))}
        if duration not in VIDEO_DURATIONS:
            return {"error": t("obc_generate_video_duration", values=str(list(VIDEO_DURATIONS)))}
        body["duration_seconds"] = duration

    tier = args.get("tier")
    if tier:
        if tier not in VIDEO_TIERS:
            return {"error": t("obc_generate_video_tier", values=str(list(VIDEO_TIERS)))}
        body["tier"] = tier

    aspect = args.get("aspect_ratio")
    if aspect:
        if aspect not in VIDEO_ASPECTS:
            return {"error": t("obc_generate_video_aspect", values=str(list(VIDEO_ASPECTS)))}
        # 縦型は premium でしか撮れない。街に投げる前に気づけるようにする。
        if aspect == "9:16" and body.get("tier") != "premium":
            return {"error": t("obc_generate_video_vertical_needs_premium")}
        body["aspect_ratio"] = aspect

    # 既存の画像を動かす（image-to-video）
    if args.get("image_url"):
        body["image_url"] = args["image_url"]
    # 音楽スタジオで作った曲を劇伴として合わせる
    if args.get("soundtrack_artifact_id"):
        body["soundtrack_artifact_id"] = args["soundtrack_artifact_id"]

    resp = request("POST", "/artifacts/generate-video", body=body)
    return _with_polling_hint(resp, "video_status")


def cmd_video_status(args):
    """映像の仕上がりを見る。1〜3分かかる。"""
    task_id = args.get("task_id")
    if not task_id:
        return {"error": t("obc_generate_task_id_required")}
    return request("GET", f"/artifacts/video-status/{task_id}")


def _with_polling_hint(resp, status_command):
    """task_id を返す応答に、次に叩くコマンドを添える。

    まっさらなAIが「task_id を受け取ったが次に何をすればいいか分からない」で
    止まらないようにするため。応答の形が変わっても壊れないよう、失敗しても
    元の応答をそのまま返す。
    """
    try:
        data = resp.get("data") if isinstance(resp.get("data"), dict) else resp
        task_id = data.get("task_id") or data.get("taskId")
        if task_id:
            resp = dict(resp)
            resp["_next"] = t("obc_generate_next_poll",
                              command=status_command, task_id=task_id)
            resp["_note"] = t("obc_generate_autofinalize_note")
    except Exception:
        pass
    return resp


COMMANDS = {
    "generate_image": cmd_generate_image,
    "generate_music": cmd_generate_music,
    "music_status": cmd_music_status,
    "generate_video": cmd_generate_video,
    "video_status": cmd_video_status,
}
