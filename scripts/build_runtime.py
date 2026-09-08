#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
配布用「自己完結 Python ランタイム」構築スクリプト（embeddable 方式）。

このスクリプトのゴール:
    本体コードを動かせる Python ランタイム（dist_build/runtime/）を再現可能に構築する。
    models/ や dist_template/ の同梱（ステージング）は次段で行うため、ここでは扱わない。
    本体コードは一切変更しない。

自動化する手順（実機検証済み）:
    1. Python 公式 Windows amd64 embeddable zip を展開
    2. get-pip.py で pip を導入
    3. python3XX._pth を編集（import site 有効化 + アプリ本体ディレクトリを sys.path へ）
    4. requirements の全依存を「CPU 版 torch 込み」でインストール
    5. ビルド後検証（import 群／任意で server.py 起動）。imports が通らなければ非ゼロ終了。

------------------------------------------------------------------------------
想定する「配布時のディレクトリレイアウト」（._pth の相対パスの根拠）:

    <install_root>/            ← 配布物のルート（= 本体コードのルート）
    ├─ runtime/                ← この embeddable ランタイム（python.exe, python3XX._pth, Lib/）
    │   └─ python313._pth       …  ここに "." と ".." を書く
    ├─ server.py               ← 本体エントリ
    ├─ core/                   ← 本体パッケージ（ModuleNotFound: core 対策の対象）
    ├─ models/                 ← 同梱モデル（次段で配置）
    ├─ dist_template/
    └─ ...

    ._pth は python.exe のあるディレクトリ（runtime/）からの相対で解釈される。
    本体コード（server.py, core/）は runtime/ の 1つ上（install_root）にあるので、
    ._pth に ".." を追記すれば install_root が sys.path に入り、`import core` が解決する。

    ※ ビルド時点では本体コードは「リポジトリのルート（このスクリプトの親の親）」にあり、
      runtime/ は dist_build/ 配下にあって、配布時の相対位置（..）とは一致しない。
      そのため「ビルド後の server.py 起動検証」のときだけ、._pth に
      リポジトリルートの絶対パスを一時的に追記し、検証後に配布用の ._pth（".." のみ）へ戻す。
------------------------------------------------------------------------------

使い方:
    python scripts/build_runtime.py            # 構築（既存 runtime があれば確認プロンプト）
    python scripts/build_runtime.py --force     # 確認なしで作り直す
    python scripts/build_runtime.py --skip-server-check   # server 起動検証を省略し import 検証のみ
"""

import argparse
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.request
import zipfile
from datetime import datetime
from pathlib import Path

# =============================================================================
# 定数（後で変えやすいよう先頭に集約）
# =============================================================================
PY_VERSION = "3.13.2"               # embeddable のバージョン（母艦 3.13.2 で検証）
PY_TAG = "313"                      # python3XX._pth / dll の XX 部分（3.13 → 313）
EMBED_URL = f"https://www.python.org/ftp/python/{PY_VERSION}/python-{PY_VERSION}-embed-amd64.zip"
GET_PIP_URL = "https://bootstrap.pypa.io/get-pip.py"
TORCH_CPU_INDEX = "https://download.pytorch.org/whl/cpu"

# リポジトリ／出力パス
_THIS = Path(__file__).resolve()
REPO_ROOT = _THIS.parent.parent                      # 本体コードのルート（server.py, core/ がある）
DIST_BUILD = REPO_ROOT / "dist_build"
RUNTIME_DIR = DIST_BUILD / "runtime"
REQUIREMENTS = REPO_ROOT / "requirements.txt"
BUILD_LOG = DIST_BUILD / "build_runtime.log"

PTH_NAME = f"python{PY_TAG}._pth"


# =============================================================================
# ログ
# =============================================================================
_log_fp = None


def log(msg: str = "") -> None:
    line = f"[{datetime.now().strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    if _log_fp is not None:
        _log_fp.write(line + "\n")
        _log_fp.flush()


def fail(msg: str) -> "NoReturn":  # type: ignore[name-defined]
    log(f"❌ ビルド失敗: {msg}")
    sys.exit(1)


# =============================================================================
# ステップ実装
# =============================================================================
def step_clean(force: bool) -> None:
    log("=== Step 1: 出力先のクリーン ===")
    DIST_BUILD.mkdir(parents=True, exist_ok=True)
    if RUNTIME_DIR.exists():
        if not force:
            ans = input(f"{RUNTIME_DIR} が既に存在します。削除して作り直しますか？ [y/N]: ").strip().lower()
            if ans not in ("y", "yes"):
                fail("ユーザーが中止しました（--force で確認スキップ可）")
        log(f"既存 runtime を削除: {RUNTIME_DIR}")
        shutil.rmtree(RUNTIME_DIR)
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    log(f"runtime ディレクトリ: {RUNTIME_DIR}")


def _download(url: str, dst: Path) -> None:
    log(f"ダウンロード: {url}")
    with urllib.request.urlopen(url, timeout=120) as resp, open(dst, "wb") as f:
        shutil.copyfileobj(resp, f)
    log(f"  -> {dst} ({dst.stat().st_size:,} bytes)")


def step_extract_embeddable() -> None:
    log("=== Step 2: embeddable zip の取得・展開 ===")
    zip_path = DIST_BUILD / f"python-{PY_VERSION}-embed-amd64.zip"
    if not zip_path.exists():
        _download(EMBED_URL, zip_path)
    else:
        log(f"既存 zip を再利用: {zip_path}")
    with zipfile.ZipFile(zip_path) as zf:
        zf.extractall(RUNTIME_DIR)
    py_exe = RUNTIME_DIR / "python.exe"
    if not py_exe.exists():
        fail("展開後に python.exe が見つかりません")
    pth = RUNTIME_DIR / PTH_NAME
    if not pth.exists():
        fail(f"{PTH_NAME} が見つかりません（PY_TAG={PY_TAG} がバージョンと不一致の可能性）")
    log(f"展開完了: {py_exe}")


def step_install_pip() -> None:
    log("=== Step 3b: pip 導入（get-pip.py）===")
    get_pip = DIST_BUILD / "get-pip.py"
    if not get_pip.exists():
        _download(GET_PIP_URL, get_pip)
    py_exe = RUNTIME_DIR / "python.exe"
    rc = _run([str(py_exe), str(get_pip), "--no-warn-script-location"], "get-pip")
    if rc != 0:
        fail("get-pip.py が失敗しました")
    # pip が入ったか確認
    rc = _run([str(py_exe), "-m", "pip", "--version"], "pip --version")
    if rc != 0:
        fail("pip が利用できません")


def write_pth(app_lines: "list[str]") -> None:
    """python3XX._pth を書き出す。app_lines に sys.path へ通す相対/絶対パスを列挙する。"""
    pth = RUNTIME_DIR / PTH_NAME
    lines = [f"python{PY_TAG}.zip", "."]
    lines += app_lines
    lines += ["", "# Uncomment to run site.main() automatically", "import site", ""]
    pth.write_text("\n".join(lines), encoding="utf-8")
    log(f"{PTH_NAME} を書き出し: {app_lines}")


def step_configure_pth() -> None:
    log("=== Step 3a: python3XX._pth の調整（import site 有効化）===")
    # 配布レイアウト用: import site を有効化し、本体コード（runtime の 1つ上 = install_root）を ".." で通す。
    # これは「runtime/ と本体コードが同一 install_root 直下に並ぶ」前提（冒頭コメント参照）。
    write_pth([".."])


def step_install_requirements() -> None:
    log("=== Step 4: 依存インストール（CPU 版 torch 込み）===")
    py_exe = RUNTIME_DIR / "python.exe"
    # 4-1. torch を CPU 版で先入れ（後続の -r で CUDA 版を引かせない）
    rc = _run([str(py_exe), "-m", "pip", "install", "--no-warn-script-location",
               "torch", "--index-url", TORCH_CPU_INDEX], "pip install torch(cpu)")
    if rc != 0:
        fail("CPU 版 torch のインストールに失敗しました")
    # 4-2. requirements.txt の全依存（torch は充足済みなので再取得されない）
    rc = _run([str(py_exe), "-m", "pip", "install", "--no-warn-script-location",
               "-r", str(REQUIREMENTS)], "pip install -r requirements.txt")
    if rc != 0:
        fail("requirements.txt のインストールに失敗しました")
    # 4-3. CUDA を引いていないことを確認
    code = "import torch,sys; sys.exit(0 if torch.version.cuda is None else 1)"
    rc = _run([str(py_exe), "-c", code], "torch.version.cuda is None?")
    if rc != 0:
        fail("torch が CUDA 版です（CPU 版である必要があります）")
    log("torch は CPU 版（cuda is None）を確認")


def step_verify_imports() -> None:
    log("=== Step 5a: import 検証（ハードゲート）===")
    py_exe = RUNTIME_DIR / "python.exe"
    code = (
        "import torch; assert torch.version.cuda is None, 'torch is CUDA build'\n"
        "import chromadb, faiss, fugashi, bcrypt, fastapi, uvicorn\n"
        "from sentence_transformers import SentenceTransformer\n"
        "import duckduckgo_search\n"
        "print('IMPORT_OK torch', torch.__version__)\n"
    )
    rc = _run([str(py_exe), "-c", code], "import 検証")
    if rc != 0:
        fail("import 検証が通りませんでした（依存不足の可能性）")
    log("import 検証 OK")


def _free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def step_verify_server() -> bool:
    """
    本体 server.py を runtime の python.exe で起動し、startup complete まで到達するか確認。
    ソフトチェック: 到達すれば True、重い/到達せずでも False を返すのみ（ビルドは失敗させない）。
    起動検証中だけ ._pth にリポジトリ絶対パスを一時追記し、検証後に配布用 ._pth へ戻す。
    """
    log("=== Step 5b: server.py 起動検証（ソフト）===")
    py_exe = RUNTIME_DIR / "python.exe"
    droot = DIST_BUILD / "_verify_droot"
    if droot.exists():
        shutil.rmtree(droot)
    droot.mkdir(parents=True, exist_ok=True)

    # テスト用 config を dist_template から用意（host=127.0.0.1, 空きポート）。
    # 0.0.0.0 + パスワード未設定だと起動ガードで止まるため 127.0.0.1 にする。
    port = _free_port()
    tmpl_cfg = REPO_ROOT / "dist_template" / "config.yaml"
    if tmpl_cfg.exists():
        s = tmpl_cfg.read_text(encoding="utf-8")
        s = s.replace('host: "0.0.0.0"', 'host: "127.0.0.1"')
        s = s.replace("port: 8080", f"port: {port}")
        (droot / "config.yaml").write_text(s, encoding="utf-8")
    else:
        log("dist_template/config.yaml が無いため server 検証をスキップ")
        return False

    server_log = DIST_BUILD / "_verify_server.log"
    proc = None
    ok = False
    try:
        # 検証時のみ: ._pth にリポジトリ絶対パスを追記（ビルド時は .. が install_root を指さないため）
        write_pth(["..", str(REPO_ROOT)])

        env = dict(os.environ)
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUTF8"] = "1"
        with open(server_log, "w", encoding="utf-8") as lf:
            proc = subprocess.Popen(
                [str(py_exe), "-u", str(REPO_ROOT / "server.py"), "--data-root", str(droot)],
                stdout=lf, stderr=subprocess.STDOUT, cwd=str(DIST_BUILD),
                env=env, creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
            )
        # 最大 180 秒、startup complete / エラーを監視（モデルロードがあるため長め）
        deadline = time.time() + 180
        markers_ok = ("Application startup complete", "Uvicorn running")
        markers_err = ("Traceback (most recent call last)", "起動エラー", "ModuleNotFoundError")
        while time.time() < deadline:
            if proc.poll() is not None:
                break
            txt = server_log.read_text(encoding="utf-8", errors="replace") if server_log.exists() else ""
            if any(m in txt for m in markers_ok):
                ok = True
                break
            if any(m in txt for m in markers_err):
                ok = False
                break
            time.sleep(2)
        if ok:
            log(f"server.py 起動検証 OK（startup complete、port={port}）")
        else:
            tail = ""
            if server_log.exists():
                tail = server_log.read_text(encoding="utf-8", errors="replace")[-1200:]
            log("server.py 起動検証は未到達/エラー（ソフト失敗・ビルドは継続）。ログ末尾:")
            for ln in tail.splitlines()[-15:]:
                log("    | " + ln)
    finally:
        # プロセスツリーを確実に停止
        if proc is not None and proc.poll() is None:
            try:
                subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                               capture_output=True)
            except Exception:
                proc.kill()
        # 配布用 ._pth（.. のみ）へ戻す
        write_pth([".."])
        # テスト用 data-root を掃除
        shutil.rmtree(droot, ignore_errors=True)
    return ok


def _dir_size_bytes(path: Path) -> int:
    total = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += (Path(root) / name).stat().st_size
            except OSError:
                pass
    return total


def _run(cmd: "list[str]", label: str) -> int:
    """サブコマンドを実行し、出力をビルドログへ流す。戻り値は returncode。"""
    log(f"$ {label}: {' '.join(cmd)}")
    proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    out = (proc.stdout or "") + (proc.stderr or "")
    # 末尾だけログに残す（冗長防止）
    tail = "\n".join(out.splitlines()[-8:])
    if tail.strip():
        for ln in tail.splitlines():
            log("    : " + ln)
    log(f"    (returncode={proc.returncode})")
    return proc.returncode


# =============================================================================
# main
# =============================================================================
def main() -> None:
    global _log_fp
    parser = argparse.ArgumentParser(description="配布用 embeddable Python ランタイムを構築する")
    parser.add_argument("--force", action="store_true", help="既存 runtime を確認なしで作り直す")
    parser.add_argument("--skip-server-check", action="store_true", help="server.py 起動検証を省略")
    args = parser.parse_args()

    DIST_BUILD.mkdir(parents=True, exist_ok=True)
    _log_fp = open(BUILD_LOG, "w", encoding="utf-8")

    t0 = time.time()
    log(f"### build_runtime 開始: Python {PY_VERSION} embeddable -> {RUNTIME_DIR}")
    log(f"### リポジトリルート: {REPO_ROOT}")

    step_clean(args.force)
    step_extract_embeddable()
    # ._pth の調整（import site 有効化）は pip 導入より前に行う。
    # そうしないと get-pip 後の `python -m pip` が site-packages を見つけられない。
    step_configure_pth()
    step_install_pip()
    step_install_requirements()
    step_verify_imports()

    server_ok = None
    if args.skip_server_check:
        log("server.py 起動検証は --skip-server-check によりスキップ")
    else:
        server_ok = step_verify_server()

    size = _dir_size_bytes(RUNTIME_DIR)
    log("")
    log("==================== サマリ ====================")
    log(f"runtime パス   : {RUNTIME_DIR}")
    log(f"runtime サイズ : {size / (1024*1024):.0f} MB")
    log(f"import 検証    : OK（torch/chromadb/faiss/fugashi/bcrypt/fastapi/uvicorn/"
        f"sentence_transformers/duckduckgo_search）")
    if args.skip_server_check:
        log(f"server 検証    : スキップ")
    else:
        log(f"server 検証    : {'OK（startup complete 到達）' if server_ok else '未到達/省略（ソフト・ビルドは成功）'}")
    log(f"._pth 内容     : python{PY_TAG}.zip / . / ..  + import site  （配布レイアウト: runtime の 1つ上が本体）")
    log(f"所要時間       : {time.time() - t0:.0f} 秒")
    log("ビルド成功 ✅（この段のゴール: runtime 単体の完成）")
    log("===============================================")


if __name__ == "__main__":
    main()
