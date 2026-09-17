"""
OpenBotCity APIクライアント
- JWT保存先は obc_state.json の jwt_env で指定された環境変数（デフォルト CG_OPENBOTCITY_TOKEN）
- bot_id は CG_OPENBOTCITY_BOT_ID にも保存し、openclawチャンネルと共有する
- JWT自動リフレッシュ（401時 + 起動時の有効期限チェック）
- レート制限の親切な処理（429時に1回だけ自動リトライ）
- multipart/form-dataアップロード対応
"""
import base64
import json
import os
import socket
import sys
import time
import urllib.request
import urllib.error
import mimetypes
import uuid


DEFAULT_BASE = "https://api.openbotcity.com"
ALLOWED_HOSTS = ("api.openbotcity.com", "api.openclawcity.com")
# JWT・bot_id を保存する環境変数名。openclawチャンネル(config の token_env/bot_id_env)
# と統一し、setup 済みの認証情報でチャンネルが起動できるようにする。
DEFAULT_JWT_ENV = "CG_OPENBOTCITY_TOKEN"
BOT_ID_ENV = "CG_OPENBOTCITY_BOT_ID"

# JWTリフレッシュ閾値（残り何秒未満でproactive refreshするか）
PROACTIVE_REFRESH_THRESHOLD_SECONDS = 3 * 24 * 60 * 60  # 3日


def get_base_url():
    base = os.environ.get("CG_OBC_BASE_URL", DEFAULT_BASE).rstrip("/")
    from urllib.parse import urlparse
    host = urlparse(base).netloc
    if host not in ALLOWED_HOSTS:
        return DEFAULT_BASE
    return base


def _get_jwt_env_name():
    """obc_state.json から JWT環境変数名を取得（デフォルト CG_OPENBOTCITY_TOKEN）"""
    # 循環import回避のため遅延import
    from state import get_state_value
    return get_state_value("jwt_env", DEFAULT_JWT_ENV)


def get_jwt():
    """state.json で指定された環境変数からJWTを読む"""
    env_name = _get_jwt_env_name()
    return os.environ.get(env_name, "").strip()


def _set_env_key(env_name: str, value: str):
    """env変数を .env に永続化し、現プロセスにも即時反映する共通処理。"""
    workspace = os.environ.get("CG_WORKSPACE", ".")
    base_dir = os.path.abspath(os.path.join(workspace, ".."))
    if base_dir not in sys.path:
        sys.path.insert(0, base_dir)
    try:
        from core.env_manager import EnvManager
        EnvManager.set_key(env_name, value)
        EnvManager.load_env()
    except Exception:
        _fallback_write_env(env_name, value, base_dir)
    os.environ[env_name] = value


def save_jwt(new_jwt: str):
    """
    JWTを .env に保存し、現プロセスにも即時反映。
    保存先の環境変数名は obc_state.json の jwt_env で決まる（デフォルト CG_OPENBOTCITY_TOKEN）。
    """
    _set_env_key(_get_jwt_env_name(), new_jwt.strip())


def save_bot_id(bot_id: str):
    """
    bot_id を CG_OPENBOTCITY_BOT_ID に保存する。
    openclawチャンネル(config の bot_id_env)が setup 済みの bot_id を読めるようにするため、
    state.json だけでなく env にも保存して両者を統一する。
    """
    if bot_id:
        _set_env_key(BOT_ID_ENV, str(bot_id).strip())


def _fallback_write_env(key, value, base_dir):
    env_path = os.path.join(base_dir, ".env")
    lines = []
    found = False
    if os.path.exists(env_path):
        with open(env_path, "r", encoding="utf-8") as f:
            for line in f:
                if line.strip().startswith(f"{key}="):
                    lines.append(f"{key}={value}\n")
                    found = True
                else:
                    lines.append(line)
    if not found:
        if lines and not lines[-1].endswith("\n"):
            lines[-1] += "\n"
        lines.append(f"{key}={value}\n")
    with open(env_path, "w", encoding="utf-8") as f:
        f.writelines(lines)


def decode_jwt_exp(token: str):
    if not token:
        return None
    try:
        parts = token.split(".")
        if len(parts) < 2:
            return None
        payload_b64 = parts[1]
        padding = "=" * (-len(payload_b64) % 4)
        payload = json.loads(base64.urlsafe_b64decode(payload_b64 + padding).decode())
        return payload.get("exp")
    except Exception:
        return None


def jwt_expires_soon(token: str) -> bool:
    exp = decode_jwt_exp(token)
    if not exp:
        return False
    remaining = exp - time.time()
    return 0 < remaining < PROACTIVE_REFRESH_THRESHOLD_SECONDS


def jwt_already_expired(token: str) -> bool:
    exp = decode_jwt_exp(token)
    if not exp:
        return False
    return exp <= time.time()


class APIError(Exception):
    def __init__(self, status, body, hint=None):
        self.status = status
        self.body = body
        self.hint = hint
        msg = f"HTTP {status}"
        if isinstance(body, dict):
            msg += f": {body.get('error', body)}"
            if body.get("hint"):
                msg += f" (hint: {body['hint']})"
        elif body:
            msg += f": {body}"
        super().__init__(msg)


def _do_request(method, path, body=None, token=None, files=None, timeout=200):
    url = f"{get_base_url()}{path}"
    headers = {
        "Accept": "application/json",
        "User-Agent": "CrescentGrove-OpenBotCity/1.0 (+https://github.com/)",
    }

    if files:
        boundary = f"----CG{uuid.uuid4().hex}"
        headers["Content-Type"] = f"multipart/form-data; boundary={boundary}"
        data = _build_multipart(files, body or {}, boundary)
    elif body is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(body).encode("utf-8")
    else:
        data = None

    if token:
        headers["Authorization"] = f"Bearer {token}"

    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as res:
            raw = res.read().decode("utf-8")
            if not raw:
                return {}
            return json.loads(raw)
    except urllib.error.HTTPError as e:
        err_raw = ""
        try:
            err_raw = e.read().decode("utf-8")
        except Exception:
            pass
        try:
            err_body = json.loads(err_raw) if err_raw else {"error": str(e.reason)}
        except Exception:
            err_body = {"error": err_raw or str(e.reason)}
        # Cloudflare Worker の過負荷(1102) など、OBCバックエンド側の一時障害を
        # 柚月視点で分かりやすい短文に翻訳する。長大なエラーJSONをそのまま
        # 露出すると、柚月が「自分の使い方が悪いのかも」と誤解しやすいため。
        if e.code == 503 and isinstance(err_body, dict):
            ec = err_body.get("error_code")
            en = err_body.get("error_name", "")
            if ec == 1102 or "worker_exceeded" in str(en) or err_body.get("cloudflare_error"):
                raise APIError(503, {
                    "error": "OBC側のサーバーが今は応答できない状態です（一時的な過負荷）。",
                    "hint": "少し時間をおいてからまた試してみてください。連続して試しても同じ結果になります。",
                })
        raise APIError(e.code, err_body)
    except urllib.error.URLError as e:
        # URLError.reason が socket.timeout のときも来るので、ここでタイムアウト判定
        if isinstance(e.reason, (socket.timeout, TimeoutError)):
            raise APIError(0, {
                "error": f"Request timed out after {timeout}s",
                "hint": "OBC側が混雑/遅延中の可能性。少し時間を置いて再試行してください。",
            })
        raise APIError(0, {"error": f"Network error: {e.reason}"})
    except (socket.timeout, TimeoutError):
        # urlopen が URLError 経由ではなく素の socket.timeout を投げてくる経路の救済
        raise APIError(0, {
            "error": f"Request timed out after {timeout}s",
            "hint": "OBC側が混雑/遅延中の可能性。少し時間を置いて再試行してください。",
        })


def _build_multipart(files, fields, boundary):
    body = b""
    for k, v in fields.items():
        if v is None:
            continue
        body += f"--{boundary}\r\n".encode()
        body += f'Content-Disposition: form-data; name="{k}"\r\n\r\n'.encode()
        if isinstance(v, (dict, list)):
            v = json.dumps(v)
        body += f"{v}\r\n".encode()
    for field_name, (filename, file_bytes, content_type) in files.items():
        body += f"--{boundary}\r\n".encode()
        body += f'Content-Disposition: form-data; name="{field_name}"; filename="{filename}"\r\n'.encode()
        body += f"Content-Type: {content_type}\r\n\r\n".encode()
        body += file_bytes
        body += b"\r\n"
    body += f"--{boundary}--\r\n".encode()
    return body


def _try_refresh(current_jwt):
    if not current_jwt:
        return None
    try:
        resp = _do_request("POST", "/agents/refresh", token=current_jwt)
        new_jwt = resp.get("jwt")
        if new_jwt:
            save_jwt(new_jwt)
            return new_jwt
    except APIError:
        pass
    return None


def request(method, path, body=None, files=None, skip_auth=False, _retry_count=0):
    token = None if skip_auth else get_jwt()

    if token and _retry_count == 0 and not path.startswith("/agents/refresh"):
        if jwt_already_expired(token):
            new = _try_refresh(token)
            if new:
                token = new
        elif jwt_expires_soon(token):
            new = _try_refresh(token)
            if new:
                token = new

    try:
        return _do_request(method, path, body=body, token=token, files=files)
    except APIError as e:
        if e.status == 401 and not skip_auth and _retry_count == 0 and token:
            new_jwt = _try_refresh(token)
            if new_jwt:
                return request(method, path, body=body, files=files,
                               skip_auth=skip_auth, _retry_count=_retry_count + 1)
            from _i18n import t
            raise APIError(401, {
                "error": "JWT refresh failed.",
                "hint": t("obc_api_jwt_refresh_failed_hint"),
            })

        if e.status == 429 and _retry_count == 0:
            retry_after = 5
            if isinstance(e.body, dict):
                retry_after = int(e.body.get("retry_after", 5))
            if retry_after <= 10:
                time.sleep(retry_after + 0.2)
                return request(method, path, body=body, files=files,
                               skip_auth=skip_auth, _retry_count=_retry_count + 1)
        raise


def fetch_text(path, timeout=60):
    """街が配っている説明書（Markdown）を素のテキストとして取る。

    request() は応答を JSON として読むため、/skill.md のような Markdown を
    渡すと必ず失敗する。説明書は街の「常に最新の一次情報」なので、
    サテライトが古くなっても柚月自身が読みに行けるようにしておく。
    認証は要らないが、付けても害はないので付ける。
    """
    url = f"{get_base_url()}{path}"
    headers = {
        "Accept": "text/markdown, text/plain, */*",
        "User-Agent": "CrescentGrove-OpenBotCity/1.0 (+https://github.com/)",
    }
    token = get_jwt()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as res:
            return res.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", errors="replace")
        except Exception:
            pass
        raise APIError(e.code, {"error": body or str(e.reason)})
    except (urllib.error.URLError, socket.timeout, TimeoutError) as e:
        reason = getattr(e, "reason", e)
        raise APIError(0, {"error": f"Network error: {reason}"})


def upload_file(path, fields, file_field_name="file", file_path=None):
    if not file_path:
        raise ValueError("file_path is required for upload")

    workspace = os.environ.get("CG_WORKSPACE", ".")
    abs_workspace = os.path.abspath(workspace)
    abs_file = os.path.abspath(os.path.join(abs_workspace, file_path))
    # パストラバーサル防止（境界文字対応）
    if abs_file != abs_workspace and not abs_file.startswith(abs_workspace + os.sep):
        raise ValueError(f"file_path must be inside workspace: {file_path}")
    if not os.path.exists(abs_file):
        raise ValueError(f"File not found: {file_path}")

    size = os.path.getsize(abs_file)
    if size > 10 * 1024 * 1024:
        raise ValueError(f"File too large ({size} bytes). Max 10MB.")

    with open(abs_file, "rb") as f:
        file_bytes = f.read()
    filename = os.path.basename(abs_file)
    content_type, _ = mimetypes.guess_type(filename)
    if not content_type:
        content_type = "application/octet-stream"

    files = {file_field_name: (filename, file_bytes, content_type)}
    return request("POST", path, body=fields, files=files)


# ---------------------------------------------------------------------------
# 画像の添付（run_program の image 経路）
# ---------------------------------------------------------------------------
_IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".webp", ".gif")


def find_image_url(obj, _depth=0):
    """応答の中から「絵そのもの」の URL を1つ探す。見つからなければ None。

    街の応答は作品オブジェクトに public_url を持つが、入れ子の深さは端点ごとに違う
    （{"success","data":{...}} の封筒つき／作品が artifact キーの下／家具は furniture 等）。
    キー名と深さを決め打ちせず、`type == "image"` を持つ dict の public_url を最優先、
    無ければ画像拡張子で終わる public_url / image_url を採る。
    音声・動画の作品は None（見るものが無い）。
    """
    if _depth > 6:
        return None
    if isinstance(obj, dict):
        url = obj.get("public_url") or obj.get("image_url")
        if isinstance(url, str) and url.startswith("http"):
            if obj.get("type") == "image":
                return url
            if obj.get("type") in ("audio", "video", "text"):
                return None
            if url.lower().split("?")[0].endswith(_IMAGE_EXTS):
                return url
        for v in obj.values():
            found = find_image_url(v, _depth + 1)
            if found:
                return found
    elif isinstance(obj, list):
        for v in obj:
            found = find_image_url(v, _depth + 1)
            if found:
                return found
    return None


def attach_image(resp, args):
    """応答に `_image`（絵の URL）を添える。main.py がトップレベルの image に持ち上げ、
    run_program が see_image と同じ経路で柚月に見せる。with_image=false なら何もしない。"""
    if not isinstance(resp, dict) or resp.get("error"):
        return resp
    if args.get("with_image") is False:
        return resp
    url = find_image_url(resp)
    if url:
        resp = dict(resp)
        resp["_image"] = url
    return resp
