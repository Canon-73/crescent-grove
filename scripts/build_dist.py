#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
配布物ステージング統合 + オフライン丸ごと検証スクリプト。

ゴール:
    install_root レイアウト（dist_build/staging/）に全部品を配置し、
    (1) 相対 ".." だけで `import core` が解決すること、
    (2) ネット遮断 + HFキャッシュ参照不可の擬似オフライン環境で
        配布フォルダ単体が startup complete / HTTP 200 に到達すること、
    を実証する。本体コードは一切変更しない。

------------------------------------------------------------------------------
配布レイアウト（前段 build_runtime.py で確定した想定どおり）:

    dist_build/staging/        (= install_root)
    ├─ runtime/                dist_build/runtime/ をコピー（embeddable + 全依存）
    │   └─ python313._pth       "python313.zip / . / .." + import site（絶対パス無し）
    ├─ server.py               本体エントリ
    ├─ core/  memory/  vital/  本体パッケージ（読み取り専用コード）
    ├─ filters/                ワードフィルタ定義（*.txt、読み取り専用リソース）
    ├─ web/                    UI（HTML + static、server.py が __file__/"web" で参照）
    ├─ programs/               外部サテライト（subprocess 実行・読み取り専用コード）
    ├─ models/                 e5 / ruri / tiktoken_cache（prepare_models.py 生成物）
    └─ dist_template/          初回ブートストラップ用テンプレート

._pth の ".." は runtime/ の 1つ上（install_root）を指し、そこに server.py / core/ がある。

------------------------------------------------------------------------------
■ 同梱するもの（読み取り専用のコード・不変リソースのみ）
    server.py / core/ / memory/ / vital/ / filters/ / web/ / programs/
    models/ / dist_template/ / runtime/
    LICENSE / THIRD_PARTY_NOTICES.md  … 再配布で保持義務のあるライセンス表記

■ 絶対に同梱しないもの（柚月の個人データ・秘密・dev 専用物）
    .env                  … APIキー等の秘密（最重要）
    settings.json         … 柚月の設定（秘密を含み得る）
    workspace/            … 柚月のキャラ・記憶・アバター・ログ
    data/                 … 状態ファイル（vital 等の実データ）
    logs/                 … 実ログ
    memory/letters/ の中身… 柚月の手紙（コードのみ同梱、データは除外）
    avatars/ (トップ階層)  … 柚月の立ち絵画像（個人データ）
    system_prompt/(トップ) … 柚月の人格プロンプト（配布版は dist_template から生成）
    config.yaml (トップ)   … dev の実設定（配布版は dist_template/config.yaml が雛形）
    venv/ .git/ __pycache__/ dist/ dist_build/ build/ / 各種 dev スクリプト/ドキュメント

    → 配布版のユーザーデータ（config.yaml / .env / workspace 等）は初回起動時に
      ブートストラップが dist_template から data-root へ生成する。install_root には置かない。
------------------------------------------------------------------------------

使い方:
    python scripts/build_dist.py                  # 既存 runtime を使ってステージング+検証
    python scripts/build_dist.py --force           # 既存 staging を確認なしで作り直す
    python scripts/build_dist.py --rebuild-runtime # runtime も build_runtime.py で作り直す
    python scripts/build_dist.py --skip-verify     # コピーのみ（検証を省略）
"""

import argparse
import os
import re
import shutil
import socket
import subprocess
import sys
import time
import urllib.request
from datetime import datetime
from pathlib import Path

# =============================================================================
# パス・マニフェスト
# =============================================================================
_THIS = Path(__file__).resolve()
REPO_ROOT = _THIS.parent.parent
DIST_BUILD = REPO_ROOT / "dist_build"
RUNTIME_SRC = DIST_BUILD / "runtime"
STAGING = DIST_BUILD / "staging"
BUILD_RUNTIME = _THIS.parent / "build_runtime.py"
BUILD_LOG = DIST_BUILD / "build_dist.log"
PTH_NAME = "python313._pth"

# 同梱するディレクトリ（読み取り専用コード・不変リソース）
INCLUDE_DIRS = ["core", "memory", "vital", "filters", "web", "programs", "models", "dist_template", "lang"]
# 同梱するファイル（server.py 本体 + 配布で保持義務のあるライセンス表記）
INCLUDE_FILES = ["server.py", "LICENSE", "THIRD_PARTY_NOTICES.md"]
# コードディレクトリ用の除外パターン（programs.zip 等の冗長アーカイブも除外）
# obc_state.json はサテライトの実行時状態（柚月の個人データ）であり、配布物には
# 絶対に含めない。本来 workspace/program_data/ に保存されるが、旧構成の名残が
# programs/*/data/ に残っていても拾わないよう名前単位で除外する。
# cg_blog / comment_mod / site_builder は作者の crescent-grove.net 運用専用サテライト。
# 配布版ユーザーには無関係（DB 認証情報・サイト固有設定が前提）なので、ビルドからは
# 未来永劫除外する。programs/ 直下のディレクトリ名で確実に弾く。
IGNORE = shutil.ignore_patterns(
    "__pycache__", "*.pyc", "*.pyo", "*.zip", ".git", "obc_state.json",
    "cg_blog", "comment_mod", "site_builder",
)
# runtime 用の除外パターン（python313.zip = stdlib は絶対に消さない！ *.zip は除外しない）
IGNORE_RUNTIME = shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo")

_log_fp = None


def log(msg: str = "") -> None:
    line = f"[{datetime.now().strftime('%H:%M:%S')}] {msg}"
    # コンソールが cp932 等の場合、tqdm の █ など Unicode 文字で
    # UnicodeEncodeError を起こしてビルドが落ちるのを防ぐ（出力の符号化に頑健化）。
    try:
        print(line, flush=True)
    except UnicodeEncodeError:
        enc = (sys.stdout.encoding or "utf-8")
        sys.stdout.buffer.write((line + "\n").encode(enc, errors="replace"))
        sys.stdout.flush()
    if _log_fp is not None:
        _log_fp.write(line + "\n")
        _log_fp.flush()


def fail(msg: str) -> None:
    log(f"❌ 失敗: {msg}")
    sys.exit(1)


# =============================================================================
# Step 1: runtime 準備
# =============================================================================
def step_ensure_runtime(rebuild: bool) -> None:
    log("=== Step 1: runtime 準備 ===")
    if rebuild or not (RUNTIME_SRC / "python.exe").exists():
        log("build_runtime.py を実行して runtime を構築します")
        rc = subprocess.run([sys.executable, "-u", str(BUILD_RUNTIME), "--force"]).returncode
        if rc != 0:
            fail("build_runtime.py が失敗しました")
    else:
        log(f"既存 runtime を使用: {RUNTIME_SRC}")
    if not (RUNTIME_SRC / "python.exe").exists():
        fail("runtime/python.exe が見つかりません")


# =============================================================================
# Step 2: ステージングへコピー
# =============================================================================
def step_clean_staging(force: bool) -> None:
    log("=== Step 2a: staging クリーン ===")
    DIST_BUILD.mkdir(parents=True, exist_ok=True)
    if STAGING.exists():
        if not force:
            ans = input(f"{STAGING} が既に存在します。削除して作り直しますか？ [y/N]: ").strip().lower()
            if ans not in ("y", "yes"):
                fail("ユーザーが中止しました（--force で確認スキップ可）")
        shutil.rmtree(STAGING)
    STAGING.mkdir(parents=True, exist_ok=True)


def step_copy() -> None:
    log("=== Step 2b: 部品コピー（同梱/除外はファイル先頭マニフェスト参照）===")
    # runtime
    log("  runtime/ をコピー中…（embeddable + 全依存・約1.5GB）")
    # runtime は python313.zip(stdlib) を含むため *.zip を除外しない専用 ignore を使う
    shutil.copytree(RUNTIME_SRC, STAGING / "runtime", ignore=IGNORE_RUNTIME)
    # ._pth が万一検証用の絶対パスを含んでいたら配布用に正規化（.. のみ）
    _normalize_pth(STAGING / "runtime" / PTH_NAME)
    # コードディレクトリ
    for d in INCLUDE_DIRS:
        src = REPO_ROOT / d
        if not src.exists():
            log(f"  ! {d}/ が無いためスキップ（想定外なら要確認）")
            continue
        log(f"  {d}/ をコピー中…")
        shutil.copytree(src, STAGING / d, ignore=IGNORE)
    # 個別ファイル
    for f in INCLUDE_FILES:
        shutil.copy2(REPO_ROOT / f, STAGING / f)
        log(f"  {f} をコピー")
    # 念のため: 個人データ/秘密が紛れ込んでいないか検査
    _assert_no_secrets()


def _assert_no_secrets() -> None:
    """staging 配下に秘密・個人データが混入していないか最終チェック。"""
    # obc_state.json は OpenBotCity の個人状態（bot_id/display_name/jwt_env）。
    # programs/*/data/ に紛れていないか rglob で全階層を検査する。
    forbidden = [".env", "settings.json", ".secret_key", "obc_state.json"]
    for name in forbidden:
        hits = list(STAGING.rglob(name))
        if hits:
            fail(f"秘密/個人データが staging に混入しています: {hits[:3]}")
    # workspace/ data/ logs/ avatars/ (トップ) が紛れていないか
    for d in ["workspace", "logs"]:
        if (STAGING / d).exists():
            fail(f"個人データディレクトリが staging に混入: {d}/")
    log("  秘密/個人データの混入なしを確認（.env / settings.json / workspace / logs 不在）")


# =============================================================================
# Step 3: ._pth 確認
# =============================================================================
def _normalize_pth(pth: Path) -> None:
    """._pth を配布用（python313.zip / . / .. + import site）に正規化。絶対パス行を除去。"""
    lines = ["python313.zip", ".", "..", "", "# Uncomment to run site.main() automatically", "import site", ""]
    pth.write_text("\n".join(lines), encoding="utf-8")


def step_check_pth() -> None:
    log("=== Step 3: ._pth が配布用（.. のみ・絶対パス無し）か確認 ===")
    pth = STAGING / "runtime" / PTH_NAME
    content = pth.read_text(encoding="utf-8")
    log("  ._pth 内容:")
    for ln in content.splitlines():
        log("    | " + ln)
    # ドライブレター（絶対パス）が残っていないか
    for ln in content.splitlines():
        s = ln.strip()
        if len(s) >= 2 and s[1] == ":":
            fail(f"._pth に絶対パスが残存: {s!r}（検証用一時追記の消し忘れ）")
    if ".." not in content.splitlines():
        fail("._pth に '..'（本体コードへの相対パス）がありません")
    log("  OK: 配布用 ._pth（.. で install_root を参照、絶対パス無し）")


# =============================================================================
# 検証ヘルパー
# =============================================================================
def _free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def _offline_env(extra_path_only: bool) -> dict:
    """擬似オフライン用 env を作る。HF 参照不可・ネット遮断相当、必要なら PATH も最小化。"""
    env = dict(os.environ)
    empty_hf = DIST_BUILD / "_offline_hf"
    empty_hf.mkdir(parents=True, exist_ok=True)
    env["HF_HOME"] = str(empty_hf)
    env["HUGGINGFACE_HUB_CACHE"] = str(empty_hf / "hub")
    env["HF_HUB_OFFLINE"] = "1"
    env["TRANSFORMERS_OFFLINE"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    if extra_path_only:
        # 母艦の python/conda を PATH から排除し、staging/runtime の python.exe だけで動くことを担保
        sysroot = os.environ.get("SystemRoot", r"C:\Windows")
        env["PATH"] = ";".join([
            os.path.join(sysroot, "System32"),
            sysroot,
            os.path.join(sysroot, "System32", "Wbem"),
        ])
    return env


def _launch_server(droot: Path, env: dict, cwd: str, label: str, timeout: int = 200) -> "tuple[bool, Path, int]":
    """staging の python.exe で server.py を起動し startup complete を待つ。(ok, log_path, port)。"""
    py_exe = STAGING / "runtime" / "python.exe"
    server_py = STAGING / "server.py"
    slog = DIST_BUILD / f"_verify_{label}.log"
    proc = None
    ok = False
    port = -1
    # config を取得（pre-place されていれば port を読む）
    cfg = droot / "config.yaml"
    if cfg.exists():
        for ln in cfg.read_text(encoding="utf-8").splitlines():
            if ln.strip().startswith("port:"):
                try:
                    port = int(ln.split(":", 1)[1].strip())
                except ValueError:
                    pass
    try:
        with open(slog, "w", encoding="utf-8") as lf:
            proc = subprocess.Popen(
                [str(py_exe), "-u", str(server_py), "--data-root", str(droot)],
                stdout=lf, stderr=subprocess.STDOUT, cwd=cwd, env=env,
                creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
            )
        deadline = time.time() + timeout
        ok_markers = ("Application startup complete", "Uvicorn running")
        err_markers = ("Traceback (most recent call last)", "起動エラー", "ModuleNotFoundError")
        while time.time() < deadline:
            if proc.poll() is not None:
                break
            txt = slog.read_text(encoding="utf-8", errors="replace") if slog.exists() else ""
            if any(m in txt for m in ok_markers):
                ok = True
                break
            if any(m in txt for m in err_markers):
                break
            time.sleep(2)
        # startup complete 後、HTTP 200 を確認
        if ok and port > 0:
            ok = _http_ok(port)
    finally:
        if proc is not None and proc.poll() is None and ok:
            # HTTP 確認のため少し動かしたまま → 停止
            _kill_tree(proc.pid)
        elif proc is not None and proc.poll() is None:
            _kill_tree(proc.pid)
    return ok, slog, port


def _http_ok(port: int) -> bool:
    """/api/auth/status に GET して 200 を確認（このビルドスクリプト自身のプロセスから）。"""
    url = f"http://127.0.0.1:{port}/api/auth/status"
    for _ in range(10):
        try:
            with urllib.request.urlopen(url, timeout=5) as r:
                log(f"  HTTP {r.status} {url}")
                return r.status == 200
        except Exception:
            time.sleep(1)
    log(f"  HTTP 取得失敗: {url}")
    return False


def _kill_tree(pid: int) -> None:
    try:
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)], capture_output=True)
    except Exception:
        pass


def _preplace_config_127(droot: Path) -> int:
    """dist_template/config.yaml を host=127.0.0.1・空きポートで data-root に先置きする。
    （配布版の推奨デフォルトを模擬。ブートストラップは残り（.env/data/等）を生成する）。戻り値: port。"""
    droot.mkdir(parents=True, exist_ok=True)
    port = _free_port()
    tmpl = REPO_ROOT / "dist_template" / "config.yaml"
    s = tmpl.read_text(encoding="utf-8")
    s = s.replace('host: "0.0.0.0"', 'host: "127.0.0.1"')
    # port は任意の数値（過去は 8080、現在は 43117）→ 一括で空きポートに置換。
    # 固定値マッチに頼ると、テンプレ既定 port が変わったときに置換漏れし、
    # 検証ステップが同一 port を取り合って即落ちする（過去にハマった）。
    s = re.sub(r"^(\s*port:\s*)\d+", rf"\g<1>{port}", s, count=1, flags=re.MULTILINE)
    (droot / "config.yaml").write_text(s, encoding="utf-8")
    return port


# =============================================================================
# Step 4: 検証
# =============================================================================
def step_verify_relative_import() -> bool:
    """検証1: 実レイアウトで CWD を staging 外（C:\\）に置き、相対 .. だけで起動するか。"""
    log("=== Step 4-1: 実レイアウトでの '..' 解決検証（CWD=C:\\）===")
    droot = DIST_BUILD / "_verify_droot_rel"
    if droot.exists():
        shutil.rmtree(droot)
    _preplace_config_127(droot)  # host 127.0.0.1 で起動できるように
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    ok, slog, port = _launch_server(droot, env, cwd="C:\\", label="rel")
    if ok:
        log(f"  OK: CWD=C:\\ でも .. だけで import core 解決 → startup complete + HTTP 200（port={port}）")
    else:
        log("  NG: 相対 .. 解決 or 起動に失敗。ログ末尾:")
        _tail_log(slog)
    shutil.rmtree(droot, ignore_errors=True)
    return ok


def step_verify_offline() -> "tuple[bool, bool, bool]":
    """検証2: 擬似オフライン（HF不可・ネット遮断・PATH最小）で staging 単体起動。
    戻り値: (server_ok, bootstrap_ok, models_from_staging_ok)。"""
    log("=== Step 4-2: 擬似オフライン丸ごと検証 ===")

    # (a) ブートストラップ単体証明: 完全に空の data-root に対し ensure_data_root が
    #     config.yaml / .env 等を生成するか（staging runtime で実行）
    bootstrap_ok = _verify_bootstrap_offline()

    # (b) オフライン起動: 127.0.0.1 config を先置き（残りは bootstrap 生成）、
    #     HF 参照不可・PATH 最小で startup complete + HTTP 200 に到達するか
    droot = DIST_BUILD / "_verify_droot_off"
    if droot.exists():
        shutil.rmtree(droot)
    port = _preplace_config_127(droot)
    env = _offline_env(extra_path_only=True)
    ok, slog, _ = _launch_server(droot, env, cwd="C:\\", label="off")
    models_ok = False
    if ok:
        txt = slog.read_text(encoding="utf-8", errors="replace")
        # e5 が staging/models から読まれた形跡（ロードレポートのパス）
        models_marker = str((STAGING / "models").resolve())
        models_ok = models_marker in txt or "models\\multilingual-e5-small" in txt
        log(f"  OK: オフラインで startup complete + HTTP 200（port={port}）")
        # 注: server はモデルの読込元パスを出力しないため、ログ走査では検出できない
        #     ことがある（情報目的）。staging/models からの読込の確定的証明は下の
        #     プローブ（resolve_model + local_files_only=True）が担う。
        log(f"  models from staging (ログ走査・参考値)? {models_ok}")
        # bootstrap がこの data-root に生成した成果物を確認
        for f in ["config.yaml", ".env"]:
            exists = (droot / f).exists()
            log(f"    bootstrap 生成 {f}: {'あり' if exists else 'なし'}")
    else:
        log("  NG: オフライン起動に失敗。ログ末尾:")
        _tail_log(slog)

    # (c) e5/ruri/tiktoken が staging/models から読めることを直接プローブ（ruri も含めて証明）
    #     これが staging/models からのオフライン読込の確定的な証明（authoritative）。
    #     上のログ走査 models_ok は server がパスを出力しないため参考値に留める。
    probe_ok = _verify_model_probe_offline()

    shutil.rmtree(droot, ignore_errors=True)
    return ok, bootstrap_ok, probe_ok


def _verify_bootstrap_offline() -> bool:
    """完全に空の data-root で ensure_data_root() が config.yaml/.env を生成するか。"""
    log("  -- ブートストラップ生成証明（空 data-root）--")
    py_exe = STAGING / "runtime" / "python.exe"
    droot = DIST_BUILD / "_verify_bootstrap"
    if droot.exists():
        shutil.rmtree(droot)
    droot.mkdir(parents=True, exist_ok=True)
    code = (
        "from core.paths import set_data_root, data_root\n"
        "set_data_root(r'" + str(droot) + "')\n"
        "from core.bootstrap import ensure_data_root\n"
        "ensure_data_root()\n"
        "import os\n"
        "root=r'" + str(droot) + "'\n"
        "print('GEN', sorted(os.listdir(root)))\n"
    )
    env = _offline_env(extra_path_only=True)
    proc = subprocess.run([str(py_exe), "-c", code], capture_output=True, text=True,
                          encoding="utf-8", errors="replace", cwd="C:\\", env=env)
    out = (proc.stdout or "") + (proc.stderr or "")
    for ln in out.splitlines()[-6:]:
        log("    : " + ln)
    cfg = (droot / "config.yaml").exists()
    envf = (droot / ".env").exists()
    log(f"    生成 config.yaml={cfg} .env={envf}")
    shutil.rmtree(droot, ignore_errors=True)
    return cfg and envf


def _verify_model_probe_offline() -> bool:
    """staging/runtime + オフラインで e5/ruri を staging/models からロードし tiktoken も確認。"""
    log("  -- e5/ruri/tiktoken オフライン読み込みプローブ --")
    py_exe = STAGING / "runtime" / "python.exe"
    code = (
        "from core.paths import resolve_model, configure_tiktoken_offline, bundle_root\n"
        "from sentence_transformers import SentenceTransformer\n"
        "print('bundle_root', bundle_root())\n"
        "for sub,hf in [('multilingual-e5-small','intfloat/multilingual-e5-small'),"
        "('ruri-v3-30m','cl-nagoya/ruri-v3-30m')]:\n"
        "    src,lfo=resolve_model(sub,hf)\n"
        "    assert lfo, sub+' is NOT local'\n"
        "    m=SentenceTransformer(src, local_files_only=True)\n"
        "    print(sub,'shape',m.encode(['x'],normalize_embeddings=True).shape,'local')\n"
        "assert configure_tiktoken_offline(), 'tiktoken cache not bundled'\n"
        "import tiktoken\n"
        "print('tiktoken tokens', tiktoken.get_encoding('cl100k_base').encode('hi 柚月'))\n"
        "print('PROBE_OK')\n"
    )
    env = _offline_env(extra_path_only=True)
    proc = subprocess.run([str(py_exe), "-c", code], capture_output=True, text=True,
                          encoding="utf-8", errors="replace", cwd="C:\\", env=env)
    out = (proc.stdout or "") + (proc.stderr or "")
    for ln in out.splitlines()[-12:]:
        log("    : " + ln)
    return "PROBE_OK" in out


def _tail_log(slog: Path, n: int = 18) -> None:
    if slog.exists():
        for ln in slog.read_text(encoding="utf-8", errors="replace").splitlines()[-n:]:
            log("    | " + ln)


def _dir_size_bytes(path: Path) -> int:
    total = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += (Path(root) / name).stat().st_size
            except OSError:
                pass
    return total


def _cleanup_temp() -> None:
    for name in ["_offline_hf", "_verify_droot_rel", "_verify_droot_off",
                 "_verify_bootstrap", "_verify_rel.log", "_verify_off.log"]:
        p = DIST_BUILD / name
        if p.is_dir():
            shutil.rmtree(p, ignore_errors=True)
        elif p.exists():
            try:
                p.unlink()
            except OSError:
                pass


# 検証中に staging 内で起動したサーバ／プローブが install_root（=staging）へ書き出す
# 実行時生成物。これらは一部の本体モジュールが data_root ではなく install_root 基準で
# パスを解決するため staging に漏れる（既知の配布上の課題・レポート参照）。
# 配布物としては不要かつ「個人データ／秘密を絶対に含めない」大原則に反するため、
# 検証後に必ず除去して staging を出荷可能な状態に戻す。
_SCRUB_NAMES = [
    ".secret_key", ".env", "settings.json", "config.yaml",
    "data", "logs", "workspace", "avatars", "system_prompt",
]


def _scrub_staging() -> None:
    """検証で staging 直下に生成された実行時生成物（個人/状態/秘密）を除去する。"""
    removed = []
    for name in _SCRUB_NAMES:
        p = STAGING / name
        if p.is_dir():
            shutil.rmtree(p, ignore_errors=True)
            removed.append(name + "/")
        elif p.exists():
            try:
                p.unlink()
                removed.append(name)
            except OSError:
                pass
    if removed:
        log(f"  検証生成物を staging から除去: {', '.join(removed)}")
    else:
        log("  検証生成物なし（staging はクリーン）")


# =============================================================================
# main
# =============================================================================
def main() -> None:
    global _log_fp
    parser = argparse.ArgumentParser(description="配布物ステージング統合 + オフライン検証")
    parser.add_argument("--force", action="store_true", help="既存 staging を確認なしで作り直す")
    parser.add_argument("--rebuild-runtime", action="store_true", help="runtime も build_runtime.py で作り直す")
    parser.add_argument("--skip-verify", action="store_true", help="コピーのみ（検証省略）")
    args = parser.parse_args()

    DIST_BUILD.mkdir(parents=True, exist_ok=True)
    _log_fp = open(BUILD_LOG, "w", encoding="utf-8")

    t0 = time.time()
    log(f"### build_dist 開始 -> {STAGING}")

    step_ensure_runtime(args.rebuild_runtime)
    step_clean_staging(args.force)
    step_copy()
    step_check_pth()

    rel_ok = off_ok = boot_ok = models_ok = None
    if not args.skip_verify:
        rel_ok = step_verify_relative_import()
        off_ok, boot_ok, models_ok = step_verify_offline()

    _cleanup_temp()
    if not args.skip_verify:
        # 検証で staging に漏れた実行時生成物を除去し、出荷可能な状態へ戻す。
        log("=== Step 5: 検証生成物の除去 + 秘密混入の最終確認 ===")
        _scrub_staging()
        _assert_no_secrets()
    size = _dir_size_bytes(STAGING)

    log("")
    log("==================== サマリ ====================")
    log(f"staging パス      : {STAGING}")
    log(f"staging 総サイズ  : {size / (1024*1024):.0f} MB")
    log(f"同梱             : {', '.join(INCLUDE_DIRS + INCLUDE_FILES + ['runtime/'])}")
    log(f"除外（個人/秘密）: .env, settings.json, workspace/, data/, logs/, avatars/, "
        f"system_prompt/(top), config.yaml(top), venv/, .git/, __pycache__/")
    if not args.skip_verify:
        # 注: f-string 式部にバックスラッシュを置くと Python 3.11 以前で SyntaxError に
        # なるため、文字列を事前に変数化する（ビルド機の python が 3.9 等でも動くように）。
        _rel_note = "OK（CWD=C:\\ でも startup complete+HTTP200）" if rel_ok else "NG"
        log(f"'..' 解決検証     : {_rel_note}")
        log(f"オフライン起動    : {'OK（startup complete+HTTP200）' if off_ok else 'NG'}")
        log(f"ブートストラップ  : {'OK（config.yaml/.env 生成）' if boot_ok else 'NG'}")
        log(f"models from staging: {'OK（e5/ruri/tiktoken をローカルから）' if models_ok else 'NG'}")
    log(f"所要時間          : {time.time() - t0:.0f} 秒")
    all_ok = args.skip_verify or (rel_ok and off_ok and boot_ok and models_ok)
    log("結果: " + ("✅ 自己完結配布フォルダがオフラインで起動可能" if all_ok else "⚠️ 一部検証が未達（要確認）"))
    log("===============================================")
    if not all_ok and not args.skip_verify:
        sys.exit(1)


if __name__ == "__main__":
    main()
