# programs/_i18n.py
"""programs サブプロセス用の軽量 i18n ヘルパー。

親プロセス（core/i18n.py）と同じ t(key, /, **kw) API を提供する。
- 言語コードは env の CG_LANG（既定 "ja"）から取得
- 辞書は programs/_lang/<lang>.json から遅延ロード
- 値展開は str.replace（.format を使わない＝JSON 例や {} を壊さない）
- 未定義キーは {{t:key}} のまま返す（親と同じ挙動）

各 program の main.py から:
    from _i18n import t
    print(t("hello_world_greet", name="Alice"))

PYTHONPATH に programs/ が通っている前提（core/tools.py の _run_program が注入）。
"""
import json
import os
from pathlib import Path

_T: "dict | None" = None
_LANG: str = "ja"


def _load() -> dict:
    """辞書を遅延ロードする。env_LANG が無ければ ja、ファイルが無ければ空辞書。"""
    global _T, _LANG
    if _T is not None:
        return _T

    _LANG = os.environ.get("CG_LANG", "ja")

    # programs/_lang/<lang>.json を探す。本ファイルの隣の _lang/ を基準にする
    # （CG_PROJECT_ROOT 経由でも到達できるが、_i18n.py が programs/ 直下にある
    #  前提なのでローカル基準のほうが堅い）
    base = Path(__file__).resolve().parent / "_lang"
    lang_file = base / f"{_LANG}.json"

    if not lang_file.exists():
        # ja にフォールバック（en.json が未整備でも壊れない）
        lang_file = base / "ja.json"

    if lang_file.exists():
        try:
            with open(lang_file, "r", encoding="utf-8") as f:
                _T = json.load(f)
        except Exception:
            _T = {}
    else:
        _T = {}

    return _T


def t(key: str, /, **kwargs) -> str:
    """翻訳文字列を返す。親 core/i18n.t() と同じ規約。

    第1引数は positional-only（kwargs に "key" を渡せるように）。
    値の中の {name} は str.replace で置換（.format ではない）。
    リテラル `{` / `}` を出したい場合は lb="{" / rb="}" を kwargs に注入する流儀。
    """
    s = _load().get(key, "{{t:" + key + "}}")
    if kwargs:
        for k, v in kwargs.items():
            s = s.replace("{" + k + "}", str(v))
    return s


def get_language() -> str:
    """現在の言語コードを返す。"""
    _load()
    return _LANG
