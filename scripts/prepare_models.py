#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
配布ビルド用: 不変リソース（モデル・tiktoken BPE）を agent/models/ に同梱する準備スクリプト。

目的:
    配布版がネット接続なし（オフライン）で embedding/類似度モデルと tiktoken を使えるよう、
    HuggingFace キャッシュ等にあるモデル実体を agent/models/ 配下へコピーする。

二刀流の前提（core/paths.py の resolve_model / configure_tiktoken_offline）:
    - agent/models/<モデル名> が存在すれば、配布版はそこからオフラインで読む。
    - 存在しなければ、母艦 dev は従来通り HF キャッシュ（モデル名解決）から読む。
    したがって本スクリプトは「配布ビルド時にのみ」実行する。母艦 dev の通常運用では実行不要。

注意:
    - models/ は巨大（約600MB）なので git にはコミットしない（.gitignore 済み）。
    - 既に models/<...> が存在する場合は上書きしない（--force で再生成）。

使い方:
    python scripts/prepare_models.py            # 不足分のみ用意
    python scripts/prepare_models.py --force     # 既存を消して作り直す
"""

import argparse
import os
import shutil
import sys
from pathlib import Path

# agent ルートを import パスに追加（core.paths を使う）
_AGENT_ROOT = Path(__file__).resolve().parent.parent
if str(_AGENT_ROOT) not in sys.path:
    sys.path.insert(0, str(_AGENT_ROOT))

from core.paths import models_dir, tiktoken_cache_dir  # noqa: E402


# 同梱したい SentenceTransformer モデル: (models/ 配下のサブディレクトリ名, HF モデルID)
MODELS = [
    ("multilingual-e5-small", "intfloat/multilingual-e5-small"),
    ("ruri-v3-30m", "cl-nagoya/ruri-v3-30m"),
]

# tiktoken cl100k_base の BPE blob ファイル名（URL の sha1）。
# 母艦の TEMP/data-gym-cache に既にダウンロード済みのものを流用する。
TIKTOKEN_BLOBS = [
    "9b5ad71b2ce5302211f9c61530b329a4922fc6a4",
]


def _hf_snapshot_dir(hf_name: str) -> "Path | None":
    """
    HuggingFace キャッシュから当該モデルの最新スナップショット実体ディレクトリを返す。
    見つからなければ None。
    """
    # HF_HOME / HUGGINGFACE_HUB_CACHE を尊重しつつ、デフォルトの ~/.cache/huggingface/hub も探す
    candidates = []
    hub = os.environ.get("HUGGINGFACE_HUB_CACHE")
    if hub:
        candidates.append(Path(hub))
    hf_home = os.environ.get("HF_HOME")
    if hf_home:
        candidates.append(Path(hf_home) / "hub")
    candidates.append(Path.home() / ".cache" / "huggingface" / "hub")

    repo_dir_name = "models--" + hf_name.replace("/", "--")
    for cache_root in candidates:
        snap_root = cache_root / repo_dir_name / "snapshots"
        if not snap_root.is_dir():
            continue
        snaps = [p for p in snap_root.iterdir() if p.is_dir()]
        if not snaps:
            continue
        # 更新時刻が最新のスナップショットを採用
        snaps.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        return snaps[0]
    return None


def _tiktoken_blob_src(blob: str) -> "Path | None":
    """tiktoken の BPE blob を探す（TIKTOKEN_CACHE_DIR / TEMP/data-gym-cache）。"""
    candidates = []
    env_cache = os.environ.get("TIKTOKEN_CACHE_DIR")
    if env_cache:
        candidates.append(Path(env_cache))
    for var in ("TEMP", "TMP"):
        t = os.environ.get(var)
        if t:
            candidates.append(Path(t) / "data-gym-cache")
    for c in candidates:
        p = c / blob
        if p.is_file():
            return p
    return None


def _copy_tree(src: Path, dst: Path) -> int:
    """src ディレクトリ配下を dst へコピー（シンボリックリンクは実体化）。コピーしたファイル数を返す。"""
    count = 0
    for root, _dirs, files in os.walk(src):
        rel = Path(root).relative_to(src)
        out_dir = dst / rel
        out_dir.mkdir(parents=True, exist_ok=True)
        for name in files:
            shutil.copy2(Path(root) / name, out_dir / name)  # copy2 はシンボリックリンクを実体化
            count += 1
    return count


def prepare_models(force: bool) -> None:
    mroot = models_dir()
    mroot.mkdir(parents=True, exist_ok=True)
    print(f"[prepare_models] models_dir = {mroot}")

    # --- SentenceTransformer モデル ---
    for subdir, hf_name in MODELS:
        dst = mroot / subdir
        if dst.exists() and any(dst.iterdir()):
            if force:
                print(f"  - {subdir}: 既存を削除して作り直します（--force）")
                shutil.rmtree(dst)
            else:
                print(f"  - {subdir}: 既に存在するためスキップ（--force で再生成）")
                continue
        snap = _hf_snapshot_dir(hf_name)
        if snap is None:
            print(f"  ! {subdir}: HF キャッシュにスナップショットが見つかりません（{hf_name}）。"
                  f"先に母艦で一度モデルをロードしてキャッシュを作ってください。")
            continue
        n = _copy_tree(snap, dst)
        print(f"  + {subdir}: {n} ファイルをコピー（src={snap}）")

    # --- tiktoken BPE ---
    tdir = tiktoken_cache_dir()
    tdir.mkdir(parents=True, exist_ok=True)
    for blob in TIKTOKEN_BLOBS:
        dst = tdir / blob
        if dst.is_file() and not force:
            print(f"  - tiktoken/{blob[:12]}…: 既に存在（スキップ）")
            continue
        src = _tiktoken_blob_src(blob)
        if src is None:
            print(f"  ! tiktoken/{blob[:12]}…: BPE blob が見つかりません。"
                  f"先に母艦で一度 tiktoken.get_encoding('cl100k_base') を実行してキャッシュを作ってください。")
            continue
        shutil.copy2(src, dst)
        print(f"  + tiktoken/{blob[:12]}…: コピー（src={src}）")

    print("[prepare_models] 完了。配布物に agent/models/ を同梱してください。")


def main():
    parser = argparse.ArgumentParser(description="配布用に不変リソース(モデル/tiktoken)を models/ へ同梱する")
    parser.add_argument("--force", action="store_true", help="既存の models/<...> を消して作り直す")
    args = parser.parse_args()
    prepare_models(force=args.force)


if __name__ == "__main__":
    main()
