# core/routes/auth.py
"""認証エンドポイント（ログイン画面・パスワード設定・ログイン・ログアウト）。"""

import time

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

from core.auth import (
    is_password_set, get_password_hash, hash_password, verify_password,
    create_session_token, save_password_hash, get_cookie_name,
)
from core.i18n import t
from core.routes.pages import WEB_DIR, NO_CACHE_HEADERS, _serve_html

router = APIRouter()


# =============================================================================
# ログイン総当たり対策（プロセス内・IP単位のレート制限）
# =============================================================================
# 正しいパスワードなら一発で通るため、通常利用には影響しない。
# 連続失敗が _LOGIN_FREE_ATTEMPTS を超えると、超過回数に比例した待機を課す。
# プロセス内メモリのみ（再起動でリセット）。シンプルさ優先で外部依存を持たない。

_login_failures: dict[str, dict] = {}  # ip -> {"count": int, "blocked_until": float}
_LOGIN_FREE_ATTEMPTS = 5   # ここまでは即時に試せる（人間の打ち間違いを許容）
_LOGIN_BLOCK_BASE = 5      # 超過1回ごとに加算する待機秒数
_LOGIN_BLOCK_MAX = 300     # 1回のロック上限（秒）


def _client_ip(request: Request) -> str:
    """リクエスト元IP。リバースプロキシ経由は X-Forwarded-For 先頭を優先。"""
    fwd = request.headers.get("x-forwarded-for", "")
    if fwd:
        return fwd.split(",")[0].strip()
    return (request.client.host if request.client else "") or "unknown"


def _check_login_block(ip: str) -> None:
    """ロック中なら 429 を返す。"""
    rec = _login_failures.get(ip)
    if not rec:
        return
    remaining = rec.get("blocked_until", 0) - time.time()
    if remaining > 0:
        raise HTTPException(
            status_code=429,
            detail=t("auth_too_many_attempts").replace("{sec}", str(int(remaining) + 1)),
        )


def _record_login_failure(ip: str) -> None:
    """失敗を記録し、規定回数を超えたら待機時間を設定する。"""
    rec = _login_failures.setdefault(ip, {"count": 0, "blocked_until": 0.0})
    rec["count"] += 1
    over = rec["count"] - _LOGIN_FREE_ATTEMPTS
    if over > 0:
        wait = min(_LOGIN_BLOCK_BASE * over, _LOGIN_BLOCK_MAX)
        rec["blocked_until"] = time.time() + wait


def _reset_login_failure(ip: str) -> None:
    """ログイン成功時に失敗カウンタをクリアする。"""
    _login_failures.pop(ip, None)


def _set_session_cookie(response, request: Request, token: str) -> None:
    """
    セッションCookieを発行する共通処理。
    https 経由（または X-Forwarded-Proto: https）のときだけ secure 属性を付け、
    平文httpのローカル運用では従来どおり secure を付けない（壊さない）。
    """
    is_https = (
        request.url.scheme == "https"
        or request.headers.get("x-forwarded-proto", "").lower() == "https"
    )
    response.set_cookie(
        key=get_cookie_name(),
        value=token,
        httponly=True,      # JavaScriptからのアクセスを遮断
        samesite="strict",  # CSRF対策
        secure=is_https,    # https のときだけ平文送信を禁止（盗聴対策）
        max_age=60 * 60 * 24 * 30,  # 30日間有効（ブラウザ/クライアントを閉じても維持）
    )


@router.get("/login")
async def get_login():
    """ログインページのHTMLを返す。AuthMiddlewareの除外リストに含まれる。
    i18n マーカー（{{t:...}}）を置換するため _serve_html を経由する。
    生 read_text で返すとマーカーが未置換のままブラウザに丸出しになり、
    ボタンや見出しが "{{t:login_set_password_button}}" のように表示されて壊れる。"""
    return _serve_html("login.html")


class AuthPasswordReq(BaseModel):
    """パスワード認証リクエストのボディスキーマ。"""
    password: str


# パスワードの最大長（バイト）。巨大な文字列でbcryptを重くするDoSを防ぐための上限。
# 通常のパスワードはこの長さに達しないため実用上の制約にはならない。
_PASSWORD_MAX_BYTES = 1024


def _reject_oversize_password(password: str) -> None:
    """極端に長いパスワード入力を拒否する（DoS対策）。"""
    if len(password.encode("utf-8")) > _PASSWORD_MAX_BYTES:
        raise HTTPException(status_code=400, detail=t("auth_password_too_long"))


@router.get("/api/auth/status")
async def auth_status():
    """パスワードが設定済みかどうかを返す。初回セットアップ判定に使用。"""
    return {"password_set": is_password_set()}


@router.post("/api/auth/setup")
async def auth_setup(req: AuthPasswordReq, request: Request):
    """初回パスワード設定。既に設定済みの場合は400エラーを返す。"""
    if is_password_set():
        raise HTTPException(status_code=400, detail=t("auth_password_already_set"))
    _reject_oversize_password(req.password)
    if len(req.password) < 8:
        raise HTTPException(status_code=400, detail=t("auth_password_too_short"))
    hashed = hash_password(req.password)
    save_password_hash(hashed)
    token = create_session_token()
    response = JSONResponse({"detail": "OK"})
    _set_session_cookie(response, request, token)
    return response


@router.post("/api/auth/login")
async def auth_login(req: AuthPasswordReq, request: Request):
    """パスワードを検証し、正しければセッションCookieを発行する。"""
    ip = _client_ip(request)
    _check_login_block(ip)  # ロック中なら 429 で即時拒否（総当たり対策）
    _reject_oversize_password(req.password)  # 巨大入力によるDoSを防ぐ

    stored = get_password_hash()
    if not stored or not verify_password(req.password, stored):
        _record_login_failure(ip)
        raise HTTPException(status_code=401, detail=t("auth_wrong_password"))

    _reset_login_failure(ip)  # 成功したら失敗カウンタをクリア
    token = create_session_token()
    response = JSONResponse({"detail": "OK"})
    _set_session_cookie(response, request, token)
    return response


@router.post("/api/auth/logout")
async def auth_logout():
    """セッションCookieを削除してログアウトする。"""
    response = JSONResponse({"detail": "OK"})
    response.delete_cookie(key=get_cookie_name())
    return response
