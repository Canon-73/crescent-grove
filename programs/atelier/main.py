#!/usr/bin/env python3
"""atelier — 柚月のアトリエ（画像生成サテライト）。

柚月から見た流れ:
    draw   … 描き始める（すぐ job_id が返る。描き上がりは待たない）
    status … 進み具合を見る（引数なしなら未回収のもの全部）
    pick_up… 描き上がったものを受け取る（workspace に JPEG で置かれる）
    cancel … やめる
    health … アトリエが開いているか

実際の描画は専用の ComfyUI インスタンス（GPU1 / 8189）が行う。
このサテライトは HTTP でつなぐだけで、torch もモデルも持たない。

**同期で待たない**のは、_run_program が同期 subprocess で CG のイベントループを
塞ぐため。1枚 28 秒かかるので、待つとサーバ全体（scheduler / Moonbeat / WebSocket）が
その間止まる。全コマンドを 1 秒未満で返す設計にしてある。
"""
import json
import os
import random
import subprocess
import sys
import time
from pathlib import Path

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from _i18n import t   # noqa: E402
import comfy          # noqa: E402
import config         # noqa: E402
import jobs           # noqa: E402
import launcher       # noqa: E402


# --- 柚月が自力で直せるようにするための呼び出し例（ai-first-tool-design）---
DRAW_EXAMPLE = {
    "command": "draw",
    "prompt": "A quiet night street in the rain, flat illustration, limited palette. "
              "The image contains no text, no signs, no lettering.",
}


def err(message: str, hint: str = "", example=None, **extra) -> dict:
    """エラーは必ず「次に何をすればいいか」と一緒に返す。"""
    out = {"error": message}
    if hint:
        out["hint"] = hint
    if example is not None:
        out["example"] = example
    out.update(extra)
    return out


def _no_window_flags() -> int:
    """Windows で黒いコンソール窓を出さないための creationflags。

    **DETACHED_PROCESS を使ってはいけない。** 「親のコンソールを継承しない」フラグだが、
    Windows 11 では代わりに新しいコンソールが割り当てられ、既定のターミナル（Windows Terminal）が
    窓を開く。しかもプロセス終了後も空の窓が残る。柚月が自律的に絵を描くたびに
    ご主人様のデスクトップへ窓が湧くことになる（夜中でも）。

    CREATE_NO_WINDOW はコンソール窓の作成そのものを抑止する。親が終了してもランチャーは
    生き残る（Windows では親の終了で子は殺されない。ジョブオブジェクトも使っていない）。
    """
    if os.name != "nt":
        return 0
    return (getattr(subprocess, "CREATE_NO_WINDOW", 0)
            | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))


def _background_python() -> str:
    """常駐プロセス用の Python。pythonw.exe があればそちらを使う。

    pythonw.exe は GUI サブシステムのバイナリで、そもそもコンソールを持たない。
    CREATE_NO_WINDOW との二重の保険。
    """
    exe = sys.executable
    if os.name == "nt":
        cand = os.path.join(os.path.dirname(exe), "pythonw.exe")
        if os.path.exists(cand):
            return cand
    return exe


def gpu_free_mb():
    """GPU1 の空き VRAM(MiB)。取れなければ None。"""
    try:
        r = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.total,memory.used",
             "--format=csv,noheader,nounits", "-i", str(config.COMFY_GPU)],
            capture_output=True, text=True, timeout=5,
            creationflags=_no_window_flags(),   # 一瞬でも窓を光らせない
        )
        if r.returncode != 0 or not r.stdout.strip():
            return None
        total, used = [int(x.strip()) for x in r.stdout.strip().splitlines()[0].split(",")]
        return total - used
    except Exception:
        return None


def ensure_open():
    """アトリエが開いていなければ開けにいく。

    戻り値 (開いているか, 付随情報)。開いていなければランチャーを起動して False を返す
    （起動を待たない＝ここで固まらない）。
    """
    if comfy.is_alive(timeout=2.0):
        return True, {}

    state = launcher.read_state()
    if launcher.launcher_running():
        # 既に誰かが開けにいっている
        return False, {"state": state.get("state", "booting")}

    try:
        subprocess.Popen(
            [_background_python(), os.path.join(_HERE, "launcher.py")],
            cwd=_HERE,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
            creationflags=_no_window_flags(), close_fds=True,
        )
        return False, {"state": "starting"}
    except Exception as e:
        return False, {"state": "error", "detail": str(e)}


def _estimate_seconds(width: int, height: int, steps: int) -> int:
    """生成時間の目安（秒）。ウォーム時の実測 1024²/12steps=27秒 を画素数とステップ数で線形に伸縮。

    実測との一致: 1024²/8steps→18秒（式18秒）、768²/8steps→11秒（式10秒）。
    """
    return max(5, int(27 * (width * height) / (1024 * 1024) * (steps / 12.0)))


def _wait_budget(estimate: int) -> float:
    """完成を待つ上限（秒）。manifest の timeout より確実に手前で諦める。

    manifest の timeout に達すると _run_program が subprocess ごと殺し、柚月には
    「タイムアウトしました」しか返らない（job_id も失われる）。そうなる前に自分で切り上げて、
    job_id と「あとで status を見て」を返すほうが、柚月が続きを追える。
    """
    return min(config.WAIT_MAX_SEC, max(20.0, estimate * 2.0 + 25))


def _wait_for_job(job_id: str, timeout: float, with_image=None):
    """ジョブの完成を待つ。完成したら pick_up 済みの結果を返す。

    時間内に終わらなければ None を返し、呼び出し側は「描いている」と伝える。
    ここで待てるのは core の _run_program が別スレッドで動くようになったため
    （以前はここで待つとサーバ全体が止まった）。
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        time.sleep(config.WAIT_POLL_SEC)
        job = jobs.get(job_id)
        if not job:
            return None
        job = _refresh(job)
        state = job.get("state")
        if state == "done":
            return cmd_pick_up({"job_id": job_id, "with_image": with_image})
        if state in ("error", "cancelled"):
            return _view(job)
    return None


def _clamp(value, lo, hi, default):
    try:
        v = int(value)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, v))


def cmd_draw(args) -> dict:
    prompt = (args.get("prompt") or "").strip()
    if not prompt:
        return err(t("at_err_prompt_required"), t("at_hint_prompt"), DRAW_EXAMPLE)

    width = _clamp(args.get("width", config.DEFAULT_WIDTH),
                   config.MIN_SIDE, config.MAX_SIDE, config.DEFAULT_WIDTH)
    height = _clamp(args.get("height", config.DEFAULT_HEIGHT),
                    config.MIN_SIDE, config.MAX_SIDE, config.DEFAULT_HEIGHT)
    steps = _clamp(args.get("steps", config.DEFAULT_STEPS), 4, 30, config.DEFAULT_STEPS)
    try:
        cfg = float(args.get("cfg", config.DEFAULT_CFG))
    except (TypeError, ValueError):
        cfg = config.DEFAULT_CFG
    cfg = max(1.0, min(3.0, cfg))
    seed = args.get("seed")
    try:
        seed = int(seed) if seed is not None else random.randint(0, 2 ** 32 - 1)
    except (TypeError, ValueError):
        seed = random.randint(0, 2 ** 32 - 1)

    job_id = jobs.new_id()
    graph = comfy.build_graph(prompt, args.get("negative_prompt") or "",
                             width, height, steps, cfg, seed,
                             filename_prefix=f"atelier/{job_id}")
    job = {
        "job_id": job_id,
        "prompt_id": None,
        "prompt": prompt,
        "negative_prompt": args.get("negative_prompt") or "",
        "width": width, "height": height, "steps": steps, "cfg": cfg, "seed": seed,
        "state": "drawing",
        "created_at": time.time(),
        "picked_up": False,
    }

    ok, info = ensure_open()
    if not ok:
        # アトリエが閉じている（＝しばらく描いていない）。ここで待つとサーバが固まるので待たない。
        # ただし依頼を捨てて「もう一度 draw して」と返すのは、柚月に同じことを2回させるうえ、
        # 開くまでの時間が丸ごと無駄になる。**依頼を台帳に預けておき、ランチャーが開けた瞬間に
        # 代わりに投入させる。** 柚月から見れば draw は常に job_id を返し、あとは status を
        # 見ればいいだけになる。
        job["state"] = "waiting_open"
        job["graph"] = graph
        jobs.add(job)
        # 開くまで実測 10〜15 秒＋初回はモデル読み込みが乗る（コールド合計で約 40 秒）
        est = _estimate_seconds(width, height, steps) + 22
        if not args.get("nowait"):
            waited = _wait_for_job(job_id, timeout=_wait_budget(est), with_image=args.get("with_image"))
            if waited is not None:
                return waited
        return {
            "state": "waiting_open",
            "job_id": job_id,
            "seed": seed,
            "size": f"{width}x{height}",
            "steps": steps,
            "estimate_seconds": est,
            "message": t("at_opening_queued", job_id=job_id),
            "next": t("at_drawing_next", job_id=job_id),
            "launcher_state": info.get("state", "starting"),
        }

    try:
        prompt_id = comfy.submit(graph)
    except comfy.ComfyDown:
        # 直前まで開いていたのに落ちた場合も、捨てずに預ける
        job["state"] = "waiting_open"
        job["graph"] = graph
        jobs.add(job)
        return {
            "state": "waiting_open",
            "job_id": job_id,
            "seed": seed,
            "message": t("at_opening_queued", job_id=job_id),
            "next": t("at_drawing_next", job_id=job_id),
        }
    except comfy.ComfyError as e:
        return err(t("at_err_submit", e=str(e)), t("at_hint_submit"), DRAW_EXAMPLE)

    running, waiting = comfy.queue_state()
    job["prompt_id"] = prompt_id
    jobs.add(job)

    est = _estimate_seconds(width, height, steps)

    # 既定は「描き上がるまで待って、そのまま絵を返す」。
    # core 側の _run_program が別スレッドで動くようになった（2026-08-22）ので、
    # ここで待ってもサーバは止まらない。柚月から見るとツール1回で絵が受け取れる。
    # 待たせたくない場合（大きいサイズ等）は nowait: true を渡す。
    if not args.get("nowait"):
        waited = _wait_for_job(job_id, timeout=_wait_budget(est), with_image=args.get("with_image"))
        if waited is not None:
            return waited

    out = {
        "state": "drawing",
        "job_id": job_id,
        "seed": seed,
        "size": f"{width}x{height}",
        "steps": steps,
        "queue_ahead": max(0, running + waiting - 1),
        "estimate_seconds": max(5, est),
        "message": t("at_drawing", job_id=job_id),
        "next": t("at_drawing_next", job_id=job_id),
    }
    free_mb = gpu_free_mb()
    if free_mb is not None and free_mb < 4000:
        # カノンが GPU を使っている可能性。追い出さず、事実だけ伝えて判断を委ねる
        out["warning"] = t("at_gpu_busy", free=free_mb)
    return out


def _refresh(job: dict) -> dict:
    """ComfyUI に問い合わせてジョブの状態を更新する。"""
    if job.get("state") in ("done", "error", "cancelled"):
        return job
    if job.get("state") == "waiting_open" or not job.get("prompt_id"):
        # まだ投入されていない（アトリエが開くのを待っている）。
        # 投入はランチャーが開いた瞬間に行うので、ここでは何もしない。
        return job
    try:
        hist = comfy.history(job["prompt_id"])
    except comfy.ComfyDown:
        return job
    if hist is None:
        return job

    status_str, images, error_text = comfy.parse_outputs(hist)
    if status_str == "success" and images:
        jobs.update(job["job_id"], state="done", images=images)
        job = dict(job, state="done", images=images)
    elif status_str == "success":
        jobs.update(job["job_id"], state="error", error=t("at_err_no_image"))
        job = dict(job, state="error", error=t("at_err_no_image"))
    else:
        msg = error_text or status_str
        jobs.update(job["job_id"], state="error", error=msg)
        job = dict(job, state="error", error=msg)
    return job


def _view(job: dict) -> dict:
    """柚月に見せる形に整える（画像の内部情報は出さない）。"""
    out = {
        "job_id": job.get("job_id"),
        "state": job.get("state"),
        "prompt": job.get("prompt"),
        "seed": job.get("seed"),
        "size": f"{job.get('width')}x{job.get('height')}",
        "picked_up": bool(job.get("picked_up")),
    }
    if job.get("state") == "waiting_open":
        out["message"] = t("at_waiting_open")
        out["next"] = t("at_drawing_next", job_id=job.get("job_id"))
    if job.get("state") == "done" and not job.get("picked_up"):
        out["next"] = t("at_done_next", job_id=job.get("job_id"))
    if job.get("state") == "error":
        out["error"] = job.get("error", "")
        out["next"] = t("at_error_next")
    if job.get("path"):
        out["path"] = job["path"]
    return out


def cmd_status(args) -> dict:
    job_id = args.get("job_id")
    if job_id:
        job = jobs.get(job_id)
        if not job:
            known = [j["job_id"] for j in jobs.pending()]
            return err(t("at_err_job_not_found", job_id=job_id),
                       t("at_hint_job_not_found"),
                       {"command": "status"}, pending_jobs=known)
        return _view(_refresh(job))

    # 引数なし: 未回収のものを全部返す。job_id を覚えていなくても拾い直せる
    pend = jobs.pending()
    if not pend:
        return {"pending": [], "message": t("at_no_pending"), "hint": t("at_no_pending_hint")}
    return {"pending": [_view(_refresh(j)) for j in pend]}


def cmd_pick_up(args) -> dict:
    job_id = args.get("job_id")
    if not job_id:
        # 引数なしなら、受け取れるものを教える
        ready = [j["job_id"] for j in (_refresh(j) for j in jobs.pending())
                 if j.get("state") == "done"]
        if not ready:
            return err(t("at_err_nothing_ready"), t("at_hint_nothing_ready"),
                       {"command": "status"})
        return err(t("at_err_jobid_required"), t("at_hint_jobid_required"),
                   {"command": "pick_up", "job_id": ready[0]}, ready_jobs=ready)

    job = jobs.get(job_id)
    if not job:
        return err(t("at_err_job_not_found", job_id=job_id), t("at_hint_job_not_found"),
                   {"command": "status"})

    job = _refresh(job)
    if job.get("state") in ("drawing", "waiting_open"):
        msg = t("at_waiting_open") if job["state"] == "waiting_open" else t("at_still_drawing")
        return {"state": job["state"], "job_id": job_id, "message": msg,
                "next": t("at_drawing_next", job_id=job_id)}
    if job.get("state") != "done":
        return err(t("at_err_not_done", state=job.get("state", "?")),
                   t("at_error_next"), {"command": "draw", "prompt": job.get("prompt", "")},
                   detail=job.get("error", ""))

    images = job.get("images") or []
    if not images:
        return err(t("at_err_no_image"), t("at_error_next"),
                   {"command": "draw", "prompt": job.get("prompt", "")})

    img = images[0]
    try:
        raw = comfy.fetch_image(img.get("filename", ""), img.get("subfolder", ""),
                               img.get("type", "output"))
    except comfy.ComfyDown:
        return err(t("at_err_closed_pickup"), t("at_hint_closed_pickup"),
                   {"command": "health"})
    except comfy.ComfyError as e:
        return err(str(e), t("at_error_next"), {"command": "status", "job_id": job_id})

    # workspace 配下に JPEG で置く。
    # ここが workspace の外だと see_image も upload_artifact も受け付けない。
    # PNG のままだと 1 枚 1.3MB で履歴（本文 48MiB 上限）をすぐ圧迫するので JPEG q90 にする。
    config.GENERATED_DIR.mkdir(parents=True, exist_ok=True)
    name = f"{job_id}_{job.get('seed')}.jpg"
    dest = config.GENERATED_DIR / name
    try:
        from PIL import Image
        import io
        im = Image.open(io.BytesIO(raw))
        if im.mode not in ("RGB", "L"):
            im = im.convert("RGB")
        im.save(dest, "JPEG", quality=config.JPEG_QUALITY)
    except Exception as e:
        return err(t("at_err_convert", e=str(e)), t("at_error_next"),
                   {"command": "status", "job_id": job_id})

    rel = f"generated/{name}"
    jobs.update(job_id, picked_up=True, path=rel)
    return {
        "state": "done",
        "job_id": job_id,
        "path": rel,
        "prompt": job.get("prompt"),
        "seed": job.get("seed"),
        "size": f"{job.get('width')}x{job.get('height')}",
        "message": t("at_picked_up", path=rel),
        "next": t("at_picked_up_next", path=rel),
        # 受け取った絵をそのまま見せる（run_program が see_image と同じ経路で渡す）。
        # with_image=false で絵なし（パスだけ受け取る）。
        "_image": None if args.get("with_image") is False else rel,
    }


def cmd_cancel(args) -> dict:
    job_id = args.get("job_id")
    if not job_id:
        pend = [j["job_id"] for j in jobs.pending()]
        return err(t("at_err_jobid_required"), t("at_hint_jobid_required"),
                   {"command": "cancel", "job_id": pend[0] if pend else "20260822_213000"},
                   pending_jobs=pend)
    job = jobs.get(job_id)
    if not job:
        return err(t("at_err_job_not_found", job_id=job_id), t("at_hint_job_not_found"),
                   {"command": "status"})
    # まだ投入されていない（アトリエが開くのを待っている）ジョブは、
    # 台帳の状態を変えるだけでよい。prompt_id が None なので街へは問い合わせない。
    if job.get("prompt_id"):
        try:
            comfy.delete_queued(job["prompt_id"])
            running, _ = comfy.queue_state()
            if running:
                comfy.interrupt()
        except comfy.ComfyDown:
            pass
    # graph も落とす（預かったまま残すと台帳が膨らむ）
    jobs.update(job_id, state="cancelled", graph=None)
    return {"state": "cancelled", "job_id": job_id, "message": t("at_cancelled", job_id=job_id)}


def cmd_health(args) -> dict:
    alive = comfy.is_alive(timeout=3.0)
    out = {
        "open": alive,
        # 状態の文字列ではなくハートビートの鮮度で判断する。ランチャーが強制終了されると
        # launcher.json は "ready" のまま残り、実際は止まっているのに開いていると報告してしまう。
        "launcher_state": launcher.effective_state(),
        "message": t("at_health_open") if alive else t("at_health_closed"),
    }
    if alive:
        running, waiting = comfy.queue_state()
        out["drawing_now"] = running
        out["waiting"] = waiting
    free_mb = gpu_free_mb()
    if free_mb is not None:
        out["gpu_free_mb"] = free_mb
    pend = jobs.pending()
    if pend:
        out["pending_jobs"] = [j["job_id"] for j in pend]
    return out


def cmd_manual(args) -> dict:
    """プロンプトの書き方の手引きを返す。

    長文なので集約辞書（programs/_lang）には入れず、サテライト内に言語別の
    マークダウンとして持つ（フォルダ内で自己完結する）。
    中身は「モデルの操作説明」であって作例集ではない。推薦する画風を並べると
    それが事実上の既定絵柄になるので、指定できる軸と、指定しなかったときに
    何が起きるかだけを書いてある。
    """
    from _i18n import get_language
    lang = get_language()
    path = Path(_HERE) / "manual" / f"{lang}.md"
    if not path.exists():
        path = Path(_HERE) / "manual" / "ja.md"
    if not path.exists():
        return err(t("at_err_manual_missing"), t("at_error_next"), {"command": "help"})
    return {"manual": path.read_text(encoding="utf-8")}


def cmd_help(args) -> dict:
    return {
        "about": t("at_help_about"),
        "commands": {
            "draw": t("at_help_draw"),
            "status": t("at_help_status"),
            "pick_up": t("at_help_pick_up"),
            "cancel": t("at_help_cancel"),
            "health": t("at_help_health"),
            "manual": t("at_help_manual"),
        },
        "how_it_works": t("at_help_flow"),
        "about_this_model": [
            t("at_help_model_style"),
            t("at_help_model_text"),
            t("at_help_model_sparse"),
            t("at_help_model_layout"),
        ],
        # 画風の指定軸・位置指定・seed の使い分け・伝わり方まで書いた手引き
        "read_more": t("at_help_read_more"),
        "layout_example": {
            "scene": "night street in the rain, wet asphalt reflecting light",
            "style": "flat illustration, limited palette, no text",
            "regions": [
                {"bbox": [0.0, 0.1, 0.35, 0.9], "description": "a large tree with dark leaves"},
                {"bbox": [0.55, 0.0, 1.0, 0.8],
                 "description": "a shop facade, windows glowing warm orange"},
            ],
        },
        "example": DRAW_EXAMPLE,
        "defaults": {
            "size": f"{config.DEFAULT_WIDTH}x{config.DEFAULT_HEIGHT}",
            "steps": config.DEFAULT_STEPS,
            "cfg": config.DEFAULT_CFG,
            "max_side": config.MAX_SIDE,
        },
    }


REGISTRY = {
    "draw": cmd_draw,
    "status": cmd_status,
    "pick_up": cmd_pick_up,
    "cancel": cmd_cancel,
    "health": cmd_health,
    "help": cmd_help,
    "manual": cmd_manual,
}


def main():
    try:
        raw = sys.stdin.read().strip()
        args = json.loads(raw) if raw else {}
    except Exception as e:
        print(json.dumps({"status": "error", "message": t("at_err_invalid_json", e=e)},
                         ensure_ascii=False))
        sys.exit(1)

    command = args.get("command") or "help"
    handler = REGISTRY.get(command)
    if handler is None:
        print(json.dumps({
            "status": "error",
            "message": t("at_err_unknown_command", command=command),
            "hint": t("at_err_unknown_hint"),
            "available": sorted(REGISTRY.keys()),
            "example": DRAW_EXAMPLE,
        }, ensure_ascii=False))
        return

    try:
        result = handler(args)
        status = "error" if isinstance(result, dict) and result.get("error") else "ok"
        out = {"status": status, "data": result}
        # `_image`（workspace 相対パス）があればトップレベルの image に持ち上げる。
        # run_program はここを見て、結果と一緒に絵そのものを柚月に渡す。
        if isinstance(result, dict) and "_image" in result:
            img = result.pop("_image")
            if img:
                out["image"] = img
        print(json.dumps(out, ensure_ascii=False))
    except Exception as e:
        print(json.dumps({
            "status": "error",
            "message": f"{type(e).__name__}: {e}",
            "hint": t("at_err_unexpected_hint"),
            "example": {"command": "health"},
        }, ensure_ascii=False))
        sys.exit(1)


if __name__ == "__main__":
    main()
