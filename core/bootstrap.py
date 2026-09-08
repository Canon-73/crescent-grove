# core/bootstrap.py
"""
初回起動ブートストラップ機構。

役割:
    data-root に必要物（config.yaml / system_prompt / data / workspace / .env）が
    揃っていない初回起動時に、内包テンプレート（dist_template/）から data-root へ
    不足分だけを展開する。

最重要の安全原則（絶対厳守）:
    既存ファイルを絶対に上書きしない。
    コピーは「コピー先に存在しないものだけ」を対象とする。
    二度目以降の起動や、ユーザーが育てたデータがある状態で、
    内包テンプレートが既存を潰すことが決してあってはならない。

dev 無害性:
    引数なし起動では data_root == bundle_root == agent のため、
    本関数は何もせずに即座に return する（完全 no-op）。

注意:
    本関数は server.py で set_data_root() の直後・EnvManager.load_env() の直前に
    呼ばれる。.env 雛形が load_env より前に用意される必要があるため、順序は厳守。
"""

import os
import secrets
import shutil
from pathlib import Path

from core.paths import (
    bundle_root,
    data_root,
    dist_template_root,
    env_path,
)


def _log(msg: str) -> None:
    """ブートストラップのログ出力（生成時のみ info 相当で出す）。"""
    print(f"[Bootstrap] {msg}")


def _copy_file_if_absent(src: Path, dst: Path, created: list) -> None:
    """
    単一ファイルを、コピー先に存在しない場合のみコピーする。
    既存ファイルには一切触れない。
    """
    if not src.is_file():
        return
    if dst.exists():
        return
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)
    created.append(dst)


def _copy_tree_if_absent(src_dir: Path, dst_dir: Path, created: list) -> None:
    """
    ディレクトリを再帰的にマージコピーする。
    コピー先に存在しないファイルだけをコピーし、既存ファイルは絶対に上書きしない。
    """
    if not src_dir.is_dir():
        return
    for src_path in src_dir.rglob("*"):
        rel = src_path.relative_to(src_dir)
        dst_path = dst_dir / rel
        if src_path.is_dir():
            # 空ディレクトリも含めて作成（存在しても mkdir は no-op）
            dst_path.mkdir(parents=True, exist_ok=True)
        elif src_path.is_file():
            _copy_file_if_absent(src_path, dst_path, created)


def _generate_env_if_absent(env_file: Path, created: list) -> None:
    """
    .env が存在しない場合のみ雛形を生成する。既存 .env には絶対に触れない。

    雛形内容:
        - APIキー等の環境変数名（空欄、ユーザーが後で設定する）
        - 自動生成したセッション秘密鍵（auth.py と同じ secrets.token_hex(32) 方式）
        - パスワードハッシュはコメントのみ（初回ログイン設定時に auth.py が書き込む）
    """
    if env_file.exists():
        return
    session_secret = secrets.token_hex(32)
    lines = [
        "# Crescent Grove 環境変数ファイル（初回起動時に自動生成）",
        "# APIキー・各種トークンを設定してください。",
        "",
        "# LLM APIキー: 特定の会社を既定にしていません。",
        "# 「設定」→「LLM設定」でプロバイダを選び、「APIキー管理」でキーを登録すると、",
        "# 対応する CG_LLM_<プロバイダ名>_API_KEY がここに自動で追記されます。",
        "# 例: CG_LLM_DEEPSEEK_API_KEY=  /  CG_LLM_OPENAI_API_KEY=  /  CG_LLM_CLAUDE_API_KEY=",
        "",
        "# セッション署名用の秘密鍵（自動生成済み・通常は変更不要）",
        f"CG_AUTH_SESSION_SECRET={session_secret}",
        "",
        "# パスワードハッシュ（初回ログイン設定時に自動で書き込まれる）",
        "# CG_AUTH_PASSWORD_HASH=",
        "",
    ]
    env_file.parent.mkdir(parents=True, exist_ok=True)
    env_file.write_text("\n".join(lines), encoding="utf-8")
    created.append(env_file)


def _copy_template_set(template: Path, droot: Path, created: list) -> None:
    """
    1つのテンプレート土台（dist_template/ 直下 or dist_template/<lang>/）から
    config.yaml ＋ 各ディレクトリを、既存を温存しつつ展開する共通処理。
    """
    # 1) config.yaml（単一ファイル）
    _copy_file_if_absent(template / "config.yaml", droot / "config.yaml", created)

    # 2) ディレクトリ群（再帰マージ・既存ファイルは温存）
    _copy_tree_if_absent(template / "system_prompt", droot / "system_prompt", created)
    _copy_tree_if_absent(template / "data", droot / "data", created)
    # config/ は手で編集する設定ファイル群（破損致命の状態ファイルは data/ に残す）。
    _copy_tree_if_absent(template / "config", droot / "config", created)
    _copy_tree_if_absent(template / "workspace", droot / "workspace", created)


def ensure_data_root(init_lang: str = "ja") -> None:
    """
    data-root に不足している必要物を内包テンプレートから展開する。

    - dev（data_root == bundle_root）では完全 no-op。
    - 既存ファイルは絶対に上書きしない（不足分だけコピー）。
    - .env が無ければ雛形を生成する（load_env より前に呼ばれる前提）。

    言語別雛形:
        init_lang が "ja" 以外（例: "en"）かつ dist_template/<init_lang>/ が存在する場合、
        その言語版を「先に」展開してから直下（＝日本語の共通土台）を重ねる。
        _copy_*_if_absent は既存を上書きしないため、言語版で置かれたファイルは
        日本語コピーがスキップされ、言語非依存ファイル（tokenizer/CSV/設定JSON 等）
        だけが直下から補完される。未知言語や <init_lang>/ 欠落時は日本語のみで立ち上がる。
    """
    droot = data_root().resolve()
    broot = bundle_root().resolve()

    # dev 無害性: 引数なし起動（data_root == bundle_root）では何もしない。
    if droot == broot:
        return

    template = dist_template_root()
    created: list = []

    # data-root 自体を用意（無ければ作る）
    droot.mkdir(parents=True, exist_ok=True)

    # 言語版を先に重ねる（en など）。直下=日本語の土台はフォールバックとして常に最後に適用。
    if init_lang and init_lang != "ja":
        lang_template = template / init_lang
        if lang_template.is_dir():
            _copy_template_set(lang_template, droot, created)

    # 共通土台（dist_template/ 直下）。言語版で置かれたファイルはスキップされる。
    _copy_template_set(template, droot, created)

    # 3) .env 雛形（load_env より前に存在させる必要がある）
    _generate_env_if_absent(env_path(), created)

    # 生成したものだけ info 相当で報告。スキップは黙る。
    if created:
        _log(f"data-root へ {len(created)} 件を展開しました: {droot}")
        for path in created:
            try:
                rel = path.relative_to(droot)
            except ValueError:
                rel = path
            _log(f"  + {rel}")
