# server.py
"""
Crescent Grove Webサーバー

役割:
    FastAPI + WebSocket でブラウザベースのチャットUIを提供する。
    エントリーポイント（このファイルを実行してエージェントを起動する）。

使い方:
    python server.py
    → ブラウザで http://localhost:8080 を開く

通信方式:
    WebSocketで双方向通信を行う。
    ストリーミングは現時点では行わない（agent.pyのコメント参照）。
    LLMの応答を完全に受け取ってから一括で返す。

主な責務:
    - アプリケーション生成（FastAPI / 認証ミドルウェア / 静的ファイル配信）
    - 起動順序の制御（--data-root反映 → bootstrap → .env読込 → ルーター登録）
    - エントリーポイント（uvicorn起動・外部バインド時のセキュリティチェック）

エンドポイントの実装は core/routes/ に分割されている:
    - core/routes/auth.py          認証（/login, /api/auth/*）
    - core/routes/pages.py         HTML画面配信（/, /dashboard, /settings/* 等）
    - core/routes/logs.py          過去ログAPI（/api/logs/*）
    - core/routes/settings_api.py  設定API（/api/settings/*, /api/keys*）
    - core/routes/dashboard_api.py ダッシュボード系API（/api/memory, /api/config 等）
    - core/routes/ws.py            WebSocket（/ws, /ws/debug）
起動時の初期化処理は core/startup.py、プロセス内共有状態は core/app_state.py にある。
"""

import uvicorn
from pathlib import Path
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware

from core.auth import is_password_set, verify_session_token, get_cookie_name

# --data-root を load_env より前に反映する（順序是正）。
# argparse のフルパースは import 副作用回避のため使わず、sys.argv を軽量走査して
# --data-root の値だけを拾う。これにより load_env 時点で data_root が確定し、
# .env の読み書きが一貫する。dev（引数なし）では set_data_root が no-op で従来同一。
from core.paths import set_data_root as _set_data_root


def _early_data_root_from_argv():
    """sys.argv から --data-root の値だけを最小実装で取り出す（--data-root V / --data-root=V 両対応）。"""
    import sys as _sys
    argv = _sys.argv[1:]
    for i, a in enumerate(argv):
        if a == "--data-root":
            if i + 1 < len(argv):
                return argv[i + 1]
        elif a.startswith("--data-root="):
            return a.split("=", 1)[1]
    return None


def _early_init_lang_from_argv():
    """sys.argv から --init-lang の値だけを取り出す（--init-lang V / --init-lang=V）。
    初回ブートストラップで「どの言語の雛形を展開するか」を決めるためだけに使う。
    未指定は "ja"（2回目以降は data-root に既存があるので、この値は実質無視される）。"""
    import sys as _sys
    argv = _sys.argv[1:]
    for i, a in enumerate(argv):
        if a == "--init-lang":
            if i + 1 < len(argv):
                return argv[i + 1]
        elif a.startswith("--init-lang="):
            return a.split("=", 1)[1]
    return "ja"


_set_data_root(_early_data_root_from_argv())

# 初回起動ブートストラップ: data-root に必要物が無ければ内包テンプレートから展開する。
# .env 雛形を load_env より前に用意する必要があるため、set_data_root 直後・load_env 直前で呼ぶ。
# dev（data_root == bundle_root）では完全 no-op。既存ファイルは絶対に上書きしない。
# --init-lang で初回に展開する雛形の言語（en など）を選べる（dev では no-op なので無害）。
from core.bootstrap import ensure_data_root as _ensure_data_root
_ensure_data_root(_early_init_lang_from_argv())

# tiktoken のオフライン化: 同梱 models/tiktoken_cache があれば TIKTOKEN_CACHE_DIR を設定する。
# tiktoken の get_encoding 呼び出しより前に行う必要がある（母艦 dev では同梱なし＝no-op）。
from core.paths import configure_tiktoken_offline as _configure_tiktoken_offline
if _configure_tiktoken_offline():
    print("[Offline] 同梱 tiktoken_cache を使用します（TIKTOKEN_CACHE_DIR 設定済み）")

# .envファイルから環境変数を読み込み、プロセスに反映する
from core.env_manager import EnvManager
EnvManager.load_env()

# 設定読み込み（config.yaml 必須チェック付き）
from core.config_loader import load_config_strict as load_config


# =============================================================================
# FastAPIアプリケーション定義
# =============================================================================

@asynccontextmanager
async def lifespan(app: FastAPI):
    """FastAPIのライフスパンイベント。起動時にスケジューラ等を初期化する。"""
    from core.startup import startup_event
    await startup_event()
    yield
    # シャットダウン時の処理が必要であればここに記述

app = FastAPI(title="Crescent Grove", lifespan=lifespan)


# =============================================================================
# 認証ミドルウェア
# =============================================================================

# ミドルウェアによる認証チェックを免除するパスの集合
_AUTH_EXCLUDE_PATHS = {"/login", "/api/auth/status", "/api/auth/setup", "/api/auth/login"}

class AuthMiddleware(BaseHTTPMiddleware):
    """全HTTPリクエストに対してセッションCookieを検証し、未認証なら/loginへリダイレクトする。"""
    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        # 静的ファイルと除外パスは認証チェックなしで素通りさせる
        if path.startswith("/static/") or path in _AUTH_EXCLUDE_PATHS:
            return await call_next(request)
        # パスワード未設定時は初回セットアップ画面へ誘導
        if not is_password_set():
            return RedirectResponse(url="/login?setup=1", status_code=302)
        # セッションCookieを検証し、無効ならログイン画面へリダイレクト
        token = request.cookies.get(get_cookie_name(), "")
        if not verify_session_token(token):
            return RedirectResponse(url="/login", status_code=302)
        return await call_next(request)

app.add_middleware(AuthMiddleware)

# 静的ファイル配信（CSS、JS、画像など）
static_dir = Path(__file__).parent / "web" / "static"
static_dir.mkdir(parents=True, exist_ok=True)
app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")


# =============================================================================
# ルーター登録（エンドポイント実装は core/routes/ に分割）
# =============================================================================

from core.routes import auth as routes_auth
from core.routes import pages as routes_pages
from core.routes import logs as routes_logs
from core.routes import settings_api as routes_settings_api
from core.routes import dashboard_api as routes_dashboard_api
from core.routes import ws as routes_ws

app.include_router(routes_auth.router)
app.include_router(routes_pages.router)
app.include_router(routes_logs.router)
app.include_router(routes_dashboard_api.router)
app.include_router(routes_settings_api.router)
app.include_router(routes_ws.router)


# =============================================================================
# エントリーポイント
# =============================================================================

if __name__ == "__main__":
    # --- コマンドライン引数の処理 ---
    # --data-root: ユーザーデータのルートを差し替える（配布対応の土台）。
    # 指定が無ければ何もしない（従来挙動）。実際のパス解決移行は後続段階で行う。
    import argparse
    from core.paths import set_data_root

    parser = argparse.ArgumentParser(description="Crescent Grove server")
    parser.add_argument("--data-root", dest="data_root", default=None,
                        help="ユーザーデータ（settings.json/workspace/data/logs等）のルートディレクトリ")
    # --init-lang は import 時に _early_init_lang_from_argv() で先取りして
    # ensure_data_root() に渡している。argparse でも受理しないと
    # 「unrecognized arguments」で SystemExit するため、ここでも定義する（値は未使用）。
    parser.add_argument("--init-lang", dest="init_lang", default=None,
                        help="初回ブートストラップで展開する雛形の言語（ja/en）。2回目以降は無視。")
    args = parser.parse_args()
    set_data_root(args.data_root)

    config = load_config()
    host = config.get("server", {}).get("host", "127.0.0.1")
    port = config.get("server", {}).get("port", 8080)

    # セキュリティチェック:
    # 127.0.0.1以外にバインドする場合、パスワード設定が必須（外部からのアクセスを保護するため）
    if host != "127.0.0.1" and not is_password_set():
        print("\n起動エラー: 外部バインドが有効な状態でパスワードが設定されていません。")
        print(f"  host={host!r} はクライアントアクセスが可能なアドレスです。")
        print("対処法: ブラウザで http://127.0.0.1:8080 にアクセスし、パスワードを先に設定してください。")
        print("  または config.yaml の server.host を '127.0.0.1' に戻してから起動してください。")
        raise SystemExit(1)

    # 起動バナーのエージェント名は config から解決する（配布版で柚月固有名を出さない）。
    # dev では profile.agent.name == "柚月" のため従来と同一出力になる。
    banner_agent_name = config.get("profile", {}).get("agent", {}).get("name", "Assistant")
    print(f"{banner_agent_name}エージェントを起動します: http://{host}:{port}")
    uvicorn.run(app, host=host, port=port)
