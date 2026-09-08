"""柚月専用 ComfyUI インスタンスの世話をする常駐プロセス。

main.py から切り離して起動される（親のサテライト subprocess が終了しても生き残る）。
サテライト自身はコマンドごとに終了するのでタイマーを持てず、この係が必要になる。

やること:
  1. ComfyUI を GPU1 / 8189 / 専用出力先で起動する
  2. 定期的に待ち行列と最終利用時刻を見る
  3. IDLE_FREE_SEC 無操作 → POST /free（VRAM だけ解放・プロセスは生存）
  4. IDLE_STOP_SEC 無操作 → ComfyUI を終了し、自分も終了（GPU1 を完全に空ける）

柚月の睡眠状態ファイルは読まない。寝ていれば draw が来ないので勝手にここへ落ちる。
「起きているが使っていない時間」も同じ扱いになるので、生活状態への結合を作らないほうが堅い。
"""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import comfy      # noqa: E402
import config     # noqa: E402
import jobs       # noqa: E402


def _no_window_flags() -> int:
    """Windows でコンソール窓を出さないための creationflags（main.py と同じ方針）。

    DETACHED_PROCESS は使わない。Windows 11 では新しいコンソールが割り当てられて
    Windows Terminal が窓を開き、プロセス終了後も空の窓が残る。
    """
    if os.name != "nt":
        return 0
    return (getattr(subprocess, "CREATE_NO_WINDOW", 0)
            | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))


def log(msg: str) -> None:
    """ランチャーのログ。柚月の workspace ではなく program_data 配下に置く。"""
    try:
        config.DATA_DIR.mkdir(parents=True, exist_ok=True)
        line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}\n"
        # ログが無限に伸びないよう、大きくなったら切り詰める
        if config.LAUNCHER_LOG.exists() and config.LAUNCHER_LOG.stat().st_size > 512 * 1024:
            tail = config.LAUNCHER_LOG.read_text(encoding="utf-8", errors="replace")[-100 * 1024:]
            config.LAUNCHER_LOG.write_text(tail, encoding="utf-8")
        with open(config.LAUNCHER_LOG, "a", encoding="utf-8") as f:
            f.write(line)
    except Exception:
        pass


def write_state(**fields) -> None:
    """launcher.json を更新する。main.py 側が生死とハートビートを見る。"""
    try:
        config.DATA_DIR.mkdir(parents=True, exist_ok=True)
        state = read_state()
        state.update(fields)
        state["heartbeat"] = time.time()
        tmp = config.LAUNCHER_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, config.LAUNCHER_FILE)
    except Exception as e:
        log(f"状態ファイルの書き込みに失敗: {e}")


def read_state() -> dict:
    try:
        if config.LAUNCHER_FILE.exists():
            data = json.loads(config.LAUNCHER_FILE.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
    except Exception:
        pass
    return {}


def heartbeat_fresh(state: dict = None) -> bool:
    """launcher.json のハートビートが新しいか（＝ランチャーが生きているか）。

    ランチャーが強制終了された場合、状態ファイルは最後の値（"ready" 等）のまま残る。
    プロセスが消えても誰も "stopped" を書けないので、**状態の文字列は信用できない**。
    生死はハートビートの鮮度だけで判断する。
    """
    if state is None:
        state = read_state()
    return (time.time() - state.get("heartbeat", 0)) < config.LAUNCHER_POLL_SEC * 3


def effective_state() -> str:
    """表示用の状態。ハートビートが古ければ、中身が何であれ stopped として扱う。"""
    state = read_state()
    if not state:
        return "stopped"
    return state.get("state", "stopped") if heartbeat_fresh(state) else "stopped"


def launcher_running() -> bool:
    """別のランチャーが生きているか（ハートビートの鮮度で判定）。"""
    state = read_state()
    if state.get("pid") == os.getpid():
        return False
    return heartbeat_fresh(state)


def start_comfy():
    """ComfyUI を子プロセスとして起動する。"""
    cmd = [
        config.COMFY_PYTHON, "main.py",
        "--cuda-device", str(config.COMFY_GPU),
        "--port", str(config.COMFY_PORT),
        "--listen", config.COMFY_HOST,
        "--output-directory", config.COMFY_OUTPUT,
    ]
    log(f"ComfyUI を起動します: {' '.join(cmd)}")
    # 新しいプロセスグループにして、後で taskkill /T で子ごと止められるようにする。
    # CREATE_NO_WINDOW でコンソール窓を出さない（DETACHED_PROCESS は逆に窓を作るので使わない）
    creationflags = _no_window_flags()
    return subprocess.Popen(
        cmd, cwd=config.COMFY_DIR,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
        creationflags=creationflags,
    )


def stop_comfy(proc) -> None:
    """ComfyUI を子ごと確実に止める。"""
    if proc is None or proc.poll() is not None:
        return
    log("ComfyUI を停止します")
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                           capture_output=True, timeout=30,
                           creationflags=_no_window_flags())
        else:
            proc.terminate()
        proc.wait(timeout=30)
    except Exception as e:
        log(f"停止に失敗、強制終了を試みます: {e}")
        try:
            proc.kill()
        except Exception:
            pass


def wait_until_ready(proc) -> bool:
    """ComfyUI が応答するまで待つ。実測 12 秒程度。"""
    deadline = time.time() + config.BOOT_TIMEOUT_SEC
    while time.time() < deadline:
        if proc.poll() is not None:
            log(f"ComfyUI が起動途中で終了しました（exit={proc.returncode}）")
            return False
        if comfy.is_alive(timeout=2.0):
            return True
        write_state(state="booting", comfy_pid=proc.pid)
        time.sleep(2)
    log("起動がタイムアウトしました")
    return False


def submit_waiting_jobs() -> int:
    """アトリエが閉じている間に預かったジョブを投入する。

    柚月が閉店中に draw したとき、main.py は依頼を捨てずに台帳へ
    state="waiting_open" で積んでおく。ここで代わりに投入することで、
    柚月は同じ draw を2回叩かずに済み、開くまでの待ち時間も無駄にならない。
    """
    submitted = 0
    for job in jobs.load():
        if job.get("state") != "waiting_open":
            continue
        graph = job.get("graph")
        if not graph:
            jobs.update(job["job_id"], state="error", error="投入内容が失われていました")
            continue
        try:
            prompt_id = comfy.submit(graph)
        except comfy.ComfyDown:
            return submitted          # まだ開いていない。次のループで再試行
        except Exception as e:
            log(f"預かりジョブ {job['job_id']} の投入に失敗: {e}")
            jobs.update(job["job_id"], state="error", error=str(e))
            continue
        # graph は台帳から落とす（大きいうえ、投入後は不要）
        jobs.update(job["job_id"], state="drawing", prompt_id=prompt_id, graph=None)
        submitted += 1
        log(f"預かりジョブを投入しました: {job['job_id']}")
    return submitted


def main() -> int:
    if launcher_running():
        log("別のランチャーが動いているので終了します")
        return 0
    if comfy.is_alive(timeout=2.0):
        # 誰かが既に 8189 を使っている。勝手に止めない
        log("8189 は既に応答しています。管理下に置かず終了します")
        return 0

    write_state(pid=os.getpid(), state="booting", started_at=time.time(),
                comfy_pid=None, error=None)

    proc = None
    try:
        proc = start_comfy()
    except Exception as e:
        log(f"ComfyUI の起動に失敗: {e}")
        write_state(state="error", error=str(e))
        return 1

    if not wait_until_ready(proc):
        write_state(state="error", error="起動に失敗またはタイムアウトしました")
        stop_comfy(proc)
        return 1

    log("ComfyUI が応答しました")
    write_state(state="ready", comfy_pid=proc.pid, error=None)

    # 閉店中に預かった依頼を、開いた直後に投入する
    try:
        n = submit_waiting_jobs()
        if n:
            jobs.touch_use()
    except Exception as e:
        log(f"預かりジョブの投入で例外: {e}")

    # アイドル判定の基準時刻。台帳がまだ無い（＝初回起動で、最初の draw は
    # "opening" を返しただけでジョブを積んでいない）場合、jobs.last_use() は 0 を返す。
    # それをそのまま基準にすると「UNIX元期からずっと未使用」と判定され、
    # 起動した直後に自分で落ちる。自分の起動時刻を下限に敷いて防ぐ。
    started_at = time.time()

    freed = False
    try:
        while True:
            time.sleep(config.LAUNCHER_POLL_SEC)

            if proc.poll() is not None:
                log(f"ComfyUI が終了しました（exit={proc.returncode}）")
                write_state(state="stopped", error="ComfyUI が予期せず終了しました")
                return 1

            # 開いている間に新しく預かった依頼があれば投入する
            # （main.py 側で submit に失敗して預けられた分の受け皿）
            try:
                if submit_waiting_jobs():
                    jobs.touch_use()
            except Exception as e:
                log(f"預かりジョブの投入で例外: {e}")

            try:
                running, waiting = comfy.queue_state(timeout=5.0)
            except comfy.ComfyDown:
                running, waiting = 0, 0

            busy = (running + waiting) > 0
            idle_for = time.time() - max(jobs.last_use(), started_at)
            if busy:
                # 生成中は使用中とみなす（長い生成でアイドル判定に落ちないように）
                jobs.touch_use()
                idle_for = 0
                if freed:
                    freed = False

            if not busy and not freed and idle_for >= config.IDLE_FREE_SEC:
                log(f"{int(idle_for)}秒 未使用のため VRAM を解放します")
                try:
                    comfy.free_memory()
                    freed = True
                    write_state(state="freed")
                except Exception as e:
                    log(f"VRAM 解放に失敗: {e}")

            if not busy and idle_for >= config.IDLE_STOP_SEC:
                log(f"{int(idle_for)}秒 未使用のため ComfyUI を終了します")
                break

            write_state(state="freed" if freed else "ready", comfy_pid=proc.pid)
    except KeyboardInterrupt:
        log("割り込みを受けました")
    except Exception as e:
        log(f"監視ループで例外: {type(e).__name__}: {e}")
        write_state(state="error", error=f"{type(e).__name__}: {e}")
    finally:
        stop_comfy(proc)
        write_state(state="stopped", comfy_pid=None)
    log("ランチャーを終了します")
    return 0


if __name__ == "__main__":
    sys.exit(main())
