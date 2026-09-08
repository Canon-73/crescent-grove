# core/routes/
"""
server.py から分割した APIRouter 群。

各モジュールが `router = APIRouter()` を公開し、server.py が include_router で束ねる。
ルートパス・レスポンス形式は分割前の server.py と完全に同一に保つこと。

構成:
    auth.py          認証（/login, /api/auth/*）
    pages.py         HTML画面配信（/, /dashboard, /settings/* 等）
    logs.py          過去ログAPI（/api/logs/*）
    settings_api.py  設定API（/api/settings/*, /api/keys*）
    dashboard_api.py ダッシュボード系API（/api/memory, /api/config 等）
    ws.py            WebSocket（/ws, /ws/debug）
"""
