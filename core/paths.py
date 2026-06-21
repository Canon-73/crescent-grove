# core/paths.py
"""
パス解決の一元化モジュール（配布対応の土台）。

役割:
    内包リソース（bundle）とユーザーデータ（data_root）の2つのルートを区別し、
    各種ファイル・ディレクトリのパスをここで解決できるようにする。

    - bundle_root(): PyInstallerで固められた内包リソースのルート。
                     開発実行時は agent ディレクトリ。
    - data_root():   ユーザーが読み書きするデータのルート。
                     コマンドライン引数 --data-root 等で差し替え可能。
                     未設定なら bundle_root() と同じ（＝従来のdev運用と同じ挙動）。

注意:
    この段階では既存コードのパス解決は一切置き換えていない。
    本モジュールは「値を一元的に保持・提供できる」ことを目的とした土台であり、
    実際のファイル入出力箇所の移行は後続の段階で行う。
"""

import sys
from pathlib import Path
from typing import Optional

# ユーザーデータのルート。set_data_root() で設定される。
# None のままなら bundle_root() にフォールバックする（従来挙動）。
_data_root: Optional[Path] = None


def bundle_root() -> Path:
    """
    内包リソースのルートを返す。

    PyInstallerで固められている場合（sys.frozen == True）は展開先 sys._MEIPASS、
    そうでなければこのファイルの2階層上（＝ agent ディレクトリ）を返す。
    """
    if getattr(sys, "frozen", False):
        # PyInstaller のワンファイル/ワンフォルダ展開先
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            return Path(meipass)
    return Path(__file__).resolve().parent.parent


def set_data_root(path: "str | None") -> None:
    """
    ユーザーデータのルートを設定する。
    None や空文字の場合は未設定のまま（従来挙動を維持）。
    """
    global _data_root
    if path is None:
        return
    if isinstance(path, str) and not path.strip():
        return
    _data_root = Path(path)


def data_root() -> Path:
    """
    ユーザーデータのルートを返す。
    set_data_root() で設定されていればその値、未設定なら bundle_root() を返す。
    """
    if _data_root is not None:
        return _data_root
    return bundle_root()


# =============================================================================
# 各種パス取得ヘルパー
# いずれも現時点では data_root() 基準で解決する。
# （tokenizer 等の不変リソースは将来 bundle_root() 基準にする可能性があるが、今は data_root 基準）
# =============================================================================

def settings_path() -> Path:
    """settings.json のパス。"""
    return data_root() / "settings.json"


def config_yaml_path() -> Path:
    """config.yaml のパス。"""
    return data_root() / "config.yaml"


def env_path() -> Path:
    """.env のパス。"""
    return data_root() / ".env"


def workspace_root() -> Path:
    """ワークスペース（記憶ファイル群）のルート。"""
    return data_root() / "workspace"


def data_dir() -> Path:
    """data/ ディレクトリ（状態ファイル・設定JSON等）のパス。"""
    return data_root() / "data"


def data_file(name: str) -> Path:
    """data/ 配下のファイルパスを data_root 基準で返す（例: data_file("vital.json")）。"""
    return data_dir() / name


def config_dir() -> Path:
    """config/ ディレクトリ（手で編集する設定ファイル群）のパス。

    破損すると致命的な状態ファイル（context_state.json 等は data/ に残る）から物理的に
    隔離するための置き場。data_root 基準なので dev でも packaged でも同じ流儀で解決する。
    """
    return data_root() / "config"


def config_file(name: str) -> Path:
    """config/ 配下のファイルパスを data_root 基準で返す（例: config_file("wyrd_config.json")）。"""
    return config_dir() / name


def resolve_path(value, default=None) -> Path:
    """
    設定値（相対/絶対パス文字列）を data_root 基準で解決する汎用ヘルパー。

    - 絶対パスならそのまま Path 化して返す。
    - 相対パスなら data_root() 基準で解決する。
    - value が None/空文字なら default を同様に解決する（default も None/空なら data_root() を返す）。

    引数なし dev では data_root() == agent ディレクトリのため、
    相対パス（例 "workspace"）は従来の CWD(=agent) 基準と同一パスに解決される。
    """
    s = value if (isinstance(value, str) and value.strip()) else default
    if not (isinstance(s, str) and s.strip()):
        return data_root()
    p = Path(s)
    if p.is_absolute():
        return p
    return data_root() / p


def resolve_workspace(config: dict = None) -> Path:
    """
    ワークスペースのパスを返す。常に data_root/workspace に固定する。

    以前は config["workspace"]["path"]（絶対パス可）に追従していたが、記憶本体は
    これに追従する一方で vital_manager の today.md アーカイブや logs は data_root/workspace
    固定だったため、ユーザーが path を変更すると保存先が分裂する split-brain があった。
    これを解消するため config.workspace.path は無視し、常に workspace_root() を返す。

    引数 config は呼び出し側（7箇所）互換のため受け取るが未使用。
    dev（data_root == bundle_root, 既定 path="workspace"）では従来と同一パス
    （agent/workspace）を返すため挙動は不変。
    """
    return workspace_root()


def logs_root() -> Path:
    """logs/ ディレクトリのパス。"""
    return data_root() / "logs"


def system_prompt_dir() -> Path:
    """system_prompt/ ディレクトリのパス。"""
    return data_root() / "system_prompt"


def dist_template_root() -> Path:
    """
    初回起動時に data_root へ展開する内包テンプレートのルート。

    必ず bundle_root() 基準で解決する（内包リソースであり、ユーザーデータではない）。
    開発実行時は agent/dist_template/、PyInstaller配布時は展開先内の dist_template/ を指す。
    """
    return bundle_root() / "dist_template"


# =============================================================================
# 不変リソース（モデル等）のオフライン同梱
#
# embedding/類似度モデルや tiktoken の BPE は「ユーザーデータ」ではなく
# 「内包リソース」なので、必ず bundle_root() 基準で解決する。
#
# 二刀流の方針:
#   - 配布版: bundle 側の models/ にモデル実体を同梱し、そこからオフラインで読む。
#   - 母艦 dev: models/ を置かない運用なら、従来通り HF キャッシュ（モデル名解決）から読む。
# =============================================================================

def models_dir() -> Path:
    """
    同梱モデル（不変リソース）のルートディレクトリ。

    bundle_root() / "models" を指す。配布版ではここに
    multilingual-e5-small/ や ruri-v3-30m/ のスナップショット実体を置く。
    母艦 dev では存在しない想定（git 管理外）。
    """
    return bundle_root() / "models"


def tiktoken_cache_dir() -> Path:
    """
    tiktoken の BPE blob を同梱するディレクトリ。

    bundle_root() / "models" / "tiktoken_cache" を指す。
    存在する場合のみ TIKTOKEN_CACHE_DIR に設定してオフライン動作させる（呼び出し側で制御）。
    """
    return models_dir() / "tiktoken_cache"


def resolve_model(local_subdir: str, hf_name: str) -> "tuple[str, bool]":
    """
    SentenceTransformer のロード対象を二刀流で解決する。

    同梱 models/ 配下に <local_subdir> の実体（中身あり）があれば、そのローカルパスと
    local_files_only=True を返す（＝配布版・オフライン）。
    無ければ HF モデル名 <hf_name> と False を返す（＝母艦 dev・従来の HF キャッシュ解決）。

    返り値: (model_name_or_path, local_files_only)
    """
    local = models_dir() / local_subdir
    try:
        has_content = local.is_dir() and any(local.iterdir())
    except OSError:
        has_content = False
    if has_content:
        return (str(local), True)
    return (hf_name, False)


def configure_tiktoken_offline() -> bool:
    """
    同梱 tiktoken_cache が存在すれば TIKTOKEN_CACHE_DIR に設定し、True を返す。

    母艦 dev（同梱なし）では何もせず False を返すため、既存の TEMP キャッシュ動作が温存される。
    tiktoken の get_encoding 呼び出しより前に実行すること。
    """
    import os
    cache = tiktoken_cache_dir()
    try:
        has_content = cache.is_dir() and any(cache.iterdir())
    except OSError:
        has_content = False
    if has_content:
        os.environ["TIKTOKEN_CACHE_DIR"] = str(cache)
        return True
    return False
