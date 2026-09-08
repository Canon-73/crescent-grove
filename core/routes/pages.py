# core/routes/pages.py
"""ページ配信エンドポイント（各種HTML画面）。"""

import re
import time

from fastapi import APIRouter
from fastapi.responses import HTMLResponse, RedirectResponse

from core.paths import bundle_root
from core.i18n import apply_i18n, get_js_injection

router = APIRouter()

# HTML/静的ファイルはコード同梱リソース（bundle_root/web）。
# 旧 server.py の Path(__file__).parent / "web" と同一パスを指す。
WEB_DIR = bundle_root() / "web"

# HTMLは常に最新を返す。古いHTML/JSキャッシュを掴ませないことで、
# <title> や ?v= の更新を確実に反映させる（専ブラのタブ名もこれで更新される）。
NO_CACHE_HEADERS = {"Cache-Control": "no-cache, no-store, must-revalidate", "Pragma": "no-cache"}

# 起動時に確定するバージョン文字列。JS/CSSの ?v= を自動で差し替え、
# 再起動するだけでブラウザキャッシュを確実に破棄させる。
_ASSET_VERSION = str(int(time.time()))
_RE_ASSET_VER = re.compile(r'\?v=\d+')


def _serve_html(filename: str) -> HTMLResponse:
    """web/ 配下のHTMLをキャッシュ無効ヘッダ付きで返す共通処理。"""
    html_path = WEB_DIR / filename
    html = html_path.read_text(encoding="utf-8")
    html = apply_i18n(html)
    html = html.replace(
        '<meta charset="UTF-8">',
        '<meta charset="UTF-8">\n' + get_js_injection(),
        1,
    )
    html = _RE_ASSET_VER.sub(f'?v={_ASSET_VERSION}', html)
    return HTMLResponse(html, headers=NO_CACHE_HEADERS)


@router.get("/")
async def get_index():
    """ルートアクセスは Dashboard に集約する。
    認証済みなら /dashboard にリダイレクト（未認証時は AuthMiddleware が /login に流す）。
    旧 index.html (シンプルUI) は使用しない。
    """
    return RedirectResponse(url="/dashboard", status_code=302)


@router.get("/settings/language")
async def get_settings_language():
    """言語設定画面のHTMLを返す。"""
    return _serve_html("settings_language.html")


@router.get("/settings/api-keys")
async def get_settings_api_keys():
    """APIキー管理画面のHTMLを返す。"""
    return _serve_html("settings_api_keys.html")


@router.get("/settings/security")
async def get_settings_security():
    """セキュリティ設定画面（ドメインホワイトリスト管理）のHTMLを返す。"""
    return _serve_html("settings_security.html")


@router.get("/settings/llm")
async def get_settings_llm():
    """LLM設定画面（プロバイダー・モデル・パラメータ管理）のHTMLを返す。"""
    return _serve_html("settings_llm.html")


@router.get("/settings/general")
async def get_settings_general():
    """一般設定画面（プロファイル・ワークスペース・プロンプト管理）のHTMLを返す。"""
    return _serve_html("settings_general.html")


@router.get("/settings/system-prompts")
async def get_settings_system_prompts():
    """システムプロンプト編集画面（固定スロットの本文編集）のHTMLを返す。"""
    return _serve_html("settings_system_prompts.html")


@router.get("/settings/moonbeat")
async def get_settings_moonbeat():
    """Moonbeat（月動）設定画面のHTMLを返す。"""
    return _serve_html("settings_moonbeat.html")


@router.get("/settings/mood")
async def get_settings_mood():
    """気分（moontide_v2_config.json）設定画面のHTMLを返す。"""
    return _serve_html("settings_mood.html")


@router.get("/settings/self-memo")
async def get_settings_self_memo():
    """self_memo（自由メモ）設定画面のHTMLを返す。"""
    return _serve_html("settings_self_memo.html")


@router.get("/settings/vital")
async def get_settings_vital():
    """バイタル（Stamina/Energy）設定画面のHTMLを返す。"""
    return _serve_html("settings_vital.html")


@router.get("/settings/compression")
async def get_settings_compression():
    """記憶圧縮（Layer0/1/2）設定画面のHTMLを返す。"""
    return _serve_html("settings_compression.html")


@router.get("/settings/tips")
async def get_settings_tips():
    """Tips（ヒント断片）設定画面のHTMLを返す。"""
    return _serve_html("settings_tips.html")


@router.get("/settings/desire")
async def get_settings_desire():
    """欲求（desire_config.json）設定画面のHTMLを返す。"""
    return _serve_html("settings_desire.html")


@router.get("/settings/openclaw")
async def get_settings_openclaw():
    """OpenClaw（openclaw_config.json）設定画面のHTMLを返す。"""
    return _serve_html("settings_openclaw.html")


@router.get("/dashboard")
async def get_dashboard():
    """ダッシュボード画面（記憶・バイタル・スケジュールの3カラム表示）のHTMLを返す。"""
    return _serve_html("dashboard.html")


@router.get("/manual")
async def get_manual():
    """マニュアルページのHTMLを返す。"""
    return _serve_html("manual.html")


@router.get("/debug/context")
async def get_debug_context_page():
    """コンテキストデバッグページのHTMLを返す。LLMに送信されるメッセージ構成を確認できる。"""
    return _serve_html("debug_context.html")


@router.get("/logs")
async def get_log_viewer_page():
    """過去ログビューワーのHTMLを返す。workspace/logs/full/*.jsonl をチャットUI形式で閲覧する。"""
    return _serve_html("log_viewer.html")
