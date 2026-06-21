#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
publish_oss.py — Crescent Grove 開発リポジトリ (CG_Yuzuki) から
公開リポジトリ (crescent-grove) へ「最新スナップショット1コミット」を force-push する。

# 設計
- pattern 1 (snapshot upsert): 公開リポジトリには「現時点のスナップショット1コミット」だけが
  載る。履歴は CG_Yuzuki 側にだけ残り、crescent-grove は常に「最新の窓口」として
  上書きされる。配布は GitHub Releases (.exe) 経由で、ソース clone 想定ユーザーがいないため、
  履歴を毎回リセットしても実害なし。
- 追跡ファイル (git ls-files) のみを対象 → .gitignore で守られている秘密ファイル
  (.env / settings.json / workspace/secret/ 等) は自動で除外される。
- EXCLUDED_PATHS で「追跡されているが公開はしたくない内部ドキュメント類」を追加除外。
- push 前に必ずシークレットパターンスキャンを実行。1件でも検出したら abort。
- デフォルトは dry-run。--push を明示しない限り何も送信しない。

# 使い方
    venv\\Scripts\\python.exe scripts\\publish_oss.py            # dry-run (何が公開されるか確認)
    venv\\Scripts\\python.exe scripts\\publish_oss.py --push     # 実際に force-push

# 動作
1. git ls-files で追跡ファイル一覧を取得
2. EXCLUDED_PATHS で内部ドキュメントを除外
3. シークレットスキャン (検出時は abort)
4. (--push 時のみ) temp dir に対象ファイルだけコピー → git init → 1コミット → crescent-grove に force-push
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path

# Windows cp932 コンソールで em-dash (U+2014) 等の Unicode を print すると
# UnicodeEncodeError で落ちるため、stdout/stderr を UTF-8 に切り替える。
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
        sys.stderr.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except (AttributeError, OSError):
        pass

REPO_ROOT = Path(__file__).resolve().parent.parent
PUBLIC_REMOTE = "https://github.com/Canon-73/crescent-grove.git"
PUBLIC_BRANCH = "main"

# 追跡されているが公開リポジトリには含めないパス (リポジトリ相対)。
# 完全一致 or 前方一致 (末尾 / でディレクトリ全体) でマッチする。
EXCLUDED_PATHS: list[str] = [
    # AI コーディングアシスタント (Antigravity) への内部指示書群。
    # 開発時の思考整理用で公開する性質のものではない。
    "Antigravityへの依頼書/",

    # programs/hello_world 配下に紛れ込んだ Antigravity 生成の実装計画書類。
    # サンプルプログラム本体 (README.md / main.py / manifest.yaml) は公開する。
    "programs/hello_world/walkthrough_run_program.md",
    "programs/hello_world/implementation_plan_run_program.md",
    "programs/hello_world/プログラム実行機能.md",

    # 走り書きのメモ群 (MoonTide 実装メモ・キャラ設定の下書き等)。
    # 整形されていない作業メモなので公開対象外。
    "readme/",

    # キャラクター画像 (柚月の立ち絵・テスト画像・ロゴ等)。
    # dev 個人のキャラ設定そのものなので公開対象外。
    # 公開リポジトリでは avatars/ 参照箇所が画像欠落になるが許容。
    "avatars/",

    # ルート直下の system_prompt/ には作者の dev キャラ (柚月) がベタ書きされている。
    # 配布版 .exe は dist_template/system_prompt/ をブートストラップで展開するため
    # こちらを使わないので、ルート直下版を除外しても配布側に影響なし。
    # OSS clone した人は `python server.py --data-root=./mydata` で起動すると
    # bootstrap が dist_template から中立テンプレを展開するので、そちらを使う。
    "system_prompt/",
]

# シークレットパターン (1件でも検出したら push を abort)。
SECRET_PATTERNS: list[tuple[str, str]] = [
    (r"sk-[a-zA-Z0-9]{20,}", "OpenAI/DeepSeek 風 API キー"),
    (r"ghp_[a-zA-Z0-9]{20,}", "GitHub Personal Access Token"),
    (r"gho_[a-zA-Z0-9]{20,}", "GitHub OAuth Token"),
    (r"github_pat_[a-zA-Z0-9_]{20,}", "GitHub Fine-grained PAT"),
    (r"AKIA[0-9A-Z]{16}", "AWS Access Key ID"),
    (r"xox[baprs]-[a-zA-Z0-9-]{20,}", "Slack Token"),
    (r"AIza[0-9A-Za-z_-]{30,}", "Google API Key"),
]


def _run(cmd: list[str], cwd: Path | None = None, check: bool = True) -> subprocess.CompletedProcess:
    """サブプロセス実行 (進捗を逐次表示)。"""
    print(f"  $ {' '.join(cmd)}" + (f"  (cwd={cwd})" if cwd else ""))
    return subprocess.run(
        cmd, cwd=str(cwd) if cwd else None, check=check,
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )


def list_tracked_files() -> list[str]:
    """git ls-files で追跡ファイル一覧を取得。

    core.quotepath=false で日本語ファイル名をエスケープせずそのまま受け取る
    (Antigravityへの依頼書/ や プログラム実行機能.md を正しく扱うため)。"""
    res = _run(
        ["git", "-c", "core.quotepath=false", "ls-files"],
        cwd=REPO_ROOT,
    )
    return [line for line in res.stdout.splitlines() if line.strip()]


def filter_excluded(files: list[str]) -> tuple[list[str], list[str]]:
    """EXCLUDED_PATHS で除外されるものとそれ以外を分ける。"""
    included: list[str] = []
    excluded: list[str] = []
    for f in files:
        if _is_excluded(f):
            excluded.append(f)
        else:
            included.append(f)
    return included, excluded


def _is_excluded(path: str) -> bool:
    """EXCLUDED_PATHS のいずれかに前方一致 (ディレクトリ) or 完全一致 (ファイル) するか。"""
    for p in EXCLUDED_PATHS:
        if p.endswith("/"):
            if path.startswith(p):
                return True
        else:
            if path == p:
                return True
    return False


def scan_secrets(files: list[str]) -> list[str]:
    """公開対象ファイルにシークレットパターンが含まれてないか確認。

    バイナリは UnicodeDecodeError で握り潰す (画像・モデル等は対象外)。"""
    findings: list[str] = []
    for f in files:
        path = REPO_ROOT / f
        try:
            content = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for pattern, desc in SECRET_PATTERNS:
            for m in re.finditer(pattern, content):
                snippet = m.group()[:24]
                findings.append(f"  {f}: {desc} ({snippet}...)")
    return findings


def copy_files_to_snapshot(files: list[str], dst_root: Path) -> None:
    """選別済みファイルをディレクトリ構造を保ったまま dst_root にコピー。"""
    for f in files:
        src = REPO_ROOT / f
        dst = dst_root / f
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)


def build_snapshot_commit(snapshot_root: Path, message: str) -> None:
    """snapshot_root で git init → 1コミットを作る。"""
    _run(["git", "init", "-b", PUBLIC_BRANCH], cwd=snapshot_root)
    # 公開スナップショットには CRLF 揺らぎを持ち込まないよう、Windows でも LF を維持。
    _run(["git", "config", "core.autocrlf", "false"], cwd=snapshot_root)
    # 作者の dev 環境の user.email/name はそのまま使う (CG_Yuzuki と同じ identity)。
    _run(["git", "add", "-A"], cwd=snapshot_root)
    _run(["git", "commit", "-m", message], cwd=snapshot_root)


def push_to_public(snapshot_root: Path) -> None:
    """crescent-grove に force-push (main を上書き)。"""
    _run(["git", "remote", "add", "origin", PUBLIC_REMOTE], cwd=snapshot_root)
    _run(["git", "push", "--force", "origin", f"HEAD:{PUBLIC_BRANCH}"], cwd=snapshot_root)


def default_commit_message() -> str:
    """公開コミットメッセージのデフォルト。CG_Yuzuki の HEAD short hash と日付を埋め込む。"""
    short = _run(["git", "rev-parse", "--short", "HEAD"], cwd=REPO_ROOT).stdout.strip()
    today = datetime.now().strftime("%Y-%m-%d")
    return f"Crescent Grove snapshot {today} (dev {short})"


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Crescent Grove OSS 公開スナップショットを crescent-grove に force-push する。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--push", action="store_true",
                        help="実際に crescent-grove へ force-push する。指定しない場合は dry-run。")
    parser.add_argument("--message", default=None,
                        help="公開コミットメッセージ。省略時は 'Crescent Grove snapshot YYYY-MM-DD (dev <hash>)' を自動生成。")
    parser.add_argument("--yes", action="store_true",
                        help="--push 時の最終確認プロンプトをスキップする (CI 用)。")
    args = parser.parse_args()

    print("=" * 70)
    print(f"Crescent Grove publish_oss — repo: {REPO_ROOT}")
    print(f"Target: {PUBLIC_REMOTE} (branch: {PUBLIC_BRANCH})")
    print("=" * 70)

    # 1. 追跡ファイル一覧
    print("\n[1/4] 追跡ファイル一覧を取得")
    tracked = list_tracked_files()
    print(f"  追跡ファイル: {len(tracked)} 件")

    # 2. 除外フィルタ
    print("\n[2/4] 内部ドキュメントを除外")
    included, excluded = filter_excluded(tracked)
    if excluded:
        print(f"  除外: {len(excluded)} 件")
        for f in excluded:
            print(f"    - {f}")
    else:
        print("  除外対象なし (EXCLUDED_PATHS に該当するファイルがありません)")
    print(f"  → 公開対象: {len(included)} 件")

    # 3. シークレットスキャン
    print("\n[3/4] シークレットスキャン")
    findings = scan_secrets(included)
    if findings:
        print("  !! シークレット候補を検出しました。push を中止します:")
        for f in findings:
            print(f)
        return 1
    print("  OK (検出なし)")

    # dry-run はここで終了
    if not args.push:
        print("\n[dry-run] --push を指定すると実際に crescent-grove へ force-push します。")
        return 0

    # 4. snapshot 構築 → push
    print("\n[4/4] 公開スナップショットを構築 → force-push")
    msg = args.message or default_commit_message()
    print(f"  コミットメッセージ: {msg}")

    if not args.yes:
        ans = input(f"\n  >>> {PUBLIC_REMOTE} の {PUBLIC_BRANCH} を上書きします。続行しますか? [y/N]: ").strip().lower()
        if ans != "y":
            print("  中断しました。")
            return 1

    with tempfile.TemporaryDirectory(prefix="cg_publish_") as td:
        snapshot_root = Path(td)
        print(f"\n  ステージング先: {snapshot_root}")
        copy_files_to_snapshot(included, snapshot_root)
        build_snapshot_commit(snapshot_root, msg)
        push_to_public(snapshot_root)

    print("\n✓ 公開完了: https://github.com/Canon-73/crescent-grove")
    return 0


if __name__ == "__main__":
    sys.exit(main())
