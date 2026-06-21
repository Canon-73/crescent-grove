# core/auth.py
"""
シンプルパスワード認証モジュール

役割:
    サーバーへのアクセスをパスワード認証で保護する。
    パスワードはbcryptでハッシュ化して.envに保存する。
    セッションはHMAC署名付きトークンをセッションCookieで管理する。

Cookie仕様:
    - httpOnly=True  : JavaScriptからの読み取りを完全に遮断
    - SameSite="strict" : CSRF攻撃を防止
    - secure=False   : ローカル(http)環境を考慮（https環境ではTrue推奨）
    - max_age=30日   : 永続Cookie。ブラウザ/クライアントを閉じても維持される
"""

import hmac
import hashlib
import os
import secrets
from pathlib import Path

try:
    import bcrypt
    _BCRYPT_AVAILABLE = True
except ImportError:
    _BCRYPT_AVAILABLE = False

# 定数
# .env のパスは core.paths 経由（data_root 基準）で「使用時」に解決する。
# 引数なし dev では従来の project_root 基準と同一パスになる（dev 非破壊）。
from core.paths import env_path
_ENV_PASSWORD_HASH_KEY = "CG_AUTH_PASSWORD_HASH"
_ENV_SESSION_SECRET_KEY = "CG_AUTH_SESSION_SECRET"
_COOKIE_NAME = "cg_session"


# --- パスワード管理 ---

def is_password_set() -> bool:
    """パスワードが設定済みかどうかを返す"""
    return bool(get_password_hash())


def get_password_hash() -> str:
    """環境変数からパスワードハッシュを取得する"""
    return os.environ.get(_ENV_PASSWORD_HASH_KEY, "")


def hash_password(password: str) -> str:
    """パスワードをbcryptでハッシュ化する"""
    if not _BCRYPT_AVAILABLE:
        raise RuntimeError("bcryptがインストールされていません。pip install bcrypt を実行してください。")
    pw_bytes = password.encode("utf-8")
    hashed = bcrypt.hashpw(pw_bytes, bcrypt.gensalt())
    return hashed.decode("utf-8")


def verify_password(password: str, hashed: str) -> bool:
    """パスワードをハッシュと照合する"""
    if not _BCRYPT_AVAILABLE:
        return False
    try:
        return bcrypt.checkpw(password.encode("utf-8"), hashed.encode("utf-8"))
    except Exception:
        return False


def save_password_hash(hashed: str) -> None:
    """パスワードハッシュを.envに保存し、os.environにも反映する"""
    _update_env_file(_ENV_PASSWORD_HASH_KEY, hashed)
    os.environ[_ENV_PASSWORD_HASH_KEY] = hashed


# --- セッション管理 ---

def _get_or_create_session_secret() -> str:
    """
    セッション署名用の秘密鍵を取得する。
    .envに存在しない場合は自動生成して保存する。
    """
    secret = os.environ.get(_ENV_SESSION_SECRET_KEY, "")
    if not secret:
        secret = secrets.token_hex(32)
        _update_env_file(_ENV_SESSION_SECRET_KEY, secret)
        os.environ[_ENV_SESSION_SECRET_KEY] = secret
    return secret


def create_session_token() -> str:
    """
    HMAC-SHA256で署名したセッショントークンを生成する。
    フォーマット: {random_data}.{signature}
    """
    secret = _get_or_create_session_secret()
    random_data = secrets.token_hex(32)
    sig = hmac.new(secret.encode(), random_data.encode(), hashlib.sha256).hexdigest()
    return f"{random_data}.{sig}"


def verify_session_token(token: str) -> bool:
    """セッショントークンの署名を検証する"""
    if not token:
        return False
    try:
        parts = token.split(".", 1)
        if len(parts) != 2:
            return False
        random_data, sig = parts
        secret = _get_or_create_session_secret()
        expected_sig = hmac.new(secret.encode(), random_data.encode(), hashlib.sha256).hexdigest()
        return hmac.compare_digest(sig, expected_sig)
    except Exception:
        return False


def get_cookie_name() -> str:
    """Cookieの名前を返す"""
    return _COOKIE_NAME


# --- ユーティリティ ---

def _update_env_file(key: str, value: str) -> None:
    """内部用: .envファイルのキーを更新または追記する"""
    _ENV_FILE = env_path()
    lines = []
    if _ENV_FILE.exists():
        with open(_ENV_FILE, "r", encoding="utf-8") as f:
            lines = f.readlines()

    updated = False
    with open(_ENV_FILE, "w", encoding="utf-8") as f:
        for line in lines:
            if line.strip().startswith(f"{key}="):
                f.write(f"{key}={value}\n")
                updated = True
            else:
                f.write(line)
        if not updated:
            if lines and not lines[-1].endswith("\n"):
                f.write("\n")
            f.write(f"{key}={value}\n")
