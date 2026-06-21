# core/i18n.py
"""多言語化（i18n）モジュール。

config.yaml の language 設定に基づき、起動時に言語辞書を読み込む。
- t(key): Python コードで翻訳文字列を取得
- apply_i18n(html): HTML 内の {{t:key}} マーカーを翻訳文字列に置換
- get_js_injection(): JS 用の翻訳辞書 <script> タグを生成
"""

import json
import re
from pathlib import Path

from core.paths import bundle_root

_translations: dict = {}
_language: str = "ja"
_lang_file: Path | None = None
_lang_mtime: float = 0.0

_MARKER_RE = re.compile(r"\{\{t:([a-zA-Z0-9_.]+)\}\}")


def init_i18n(config: dict) -> None:
    """起動時に呼び出し、言語辞書をロードする。"""
    global _translations, _language, _lang_file, _lang_mtime
    _language = config.get("language", "ja")

    lang_dir = bundle_root() / "lang"
    lang_file = lang_dir / f"{_language}.json"

    if not lang_file.exists():
        print(f"[i18n] 言語ファイルが見つかりません: {lang_file}")
        return

    with open(lang_file, "r", encoding="utf-8") as f:
        _translations = json.load(f)

    _lang_file = lang_file
    _lang_mtime = lang_file.stat().st_mtime

    _check_key_diff(lang_dir)
    print(f"[i18n] language={_language} ({len(_translations)} keys)")


def _maybe_reload() -> None:
    """lang ファイルの mtime が変わっていたら辞書を再ロードする（ホットリロード）。"""
    global _translations, _lang_mtime
    if _lang_file is None:
        return
    try:
        mtime = _lang_file.stat().st_mtime
    except OSError:
        return
    if mtime == _lang_mtime:
        return
    try:
        with open(_lang_file, "r", encoding="utf-8") as f:
            _translations = json.load(f)
        _lang_mtime = mtime
        print(f"[i18n] reloaded {_lang_file.name} ({len(_translations)} keys)")
    except (OSError, json.JSONDecodeError) as e:
        print(f"[i18n] reload failed: {e}")


def _check_key_diff(lang_dir: Path) -> None:
    """全言語ファイル間のキー差分を警告する。"""
    all_keys: dict = {}
    for f in lang_dir.glob("*.json"):
        try:
            with open(f, "r", encoding="utf-8") as fh:
                all_keys[f.stem] = set(json.load(fh).keys())
        except Exception:
            continue

    if len(all_keys) < 2:
        return

    langs = list(all_keys.keys())
    for i, a in enumerate(langs):
        for b in langs[i + 1:]:
            only_a = all_keys[a] - all_keys[b]
            only_b = all_keys[b] - all_keys[a]
            if only_a:
                print(f"[i18n] {a}.json にのみ存在: {', '.join(sorted(only_a))}")
            if only_b:
                print(f"[i18n] {b}.json にのみ存在: {', '.join(sorted(only_b))}")


def t(key: str, /, **kwargs) -> str:
    """翻訳文字列を返す。未定義キーは {{t:key}} のまま返す。

    第1引数は positional-only。これにより kwargs に "key" という名前のプレースホルダ値を
    渡せる（例: `t("tool_err_program_arg_traversal", key=arg_name)`）。
    kwargs を渡すと、値の中の {name} を str.replace で置換する
    （`.format` は使わない＝翻訳値に含まれる JSON 例などの `{}` を壊さないため）。
    kwargs 無しなら従来と完全に同一挙動（後方互換）。
    """
    _maybe_reload()
    s = _translations.get(key, "{{t:" + key + "}}")
    if kwargs:
        for k, v in kwargs.items():
            s = s.replace("{" + k + "}", str(v))
    return s


def apply_i18n(html: str) -> str:
    """HTML 内の {{t:key}} を翻訳文字列に置換する。"""
    _maybe_reload()
    def _replace(m):
        return _translations.get(m.group(1), m.group(0))
    return _MARKER_RE.sub(_replace, html)


def get_translations() -> dict:
    """辞書全体を返す。"""
    _maybe_reload()
    return dict(_translations)


def get_language() -> str:
    """現在の言語コードを返す。"""
    return _language


def get_js_injection() -> str:
    """<head> 末尾に注入する <script> タグを返す。"""
    _maybe_reload()
    data = json.dumps(_translations, ensure_ascii=False)
    return f'<script>window.T={data};window.LANG="{_language}";</script>'
