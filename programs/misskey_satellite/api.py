"""
Misskey APIクライアント

- アクセストークンは環境変数 CG_MISSKEY_TOKEN から読む（.env / APIキー管理ページで登録）
- Misskey はトークンを**ボディの "i"** に入れる方式。トークンは例外にも出力にも絶対に載せない
  （_transport の中だけでボディに合流させ、request 側はトークンを持ったボディを作らない）
- 全エンドポイントが POST + JSON。URL は必ず {base}/api/{path}
- 429 の Retry-After は **HTTPヘッダ**（秒）に載る。Discord のようにボディには入らない
- HTTP 204（空ボディ）が正常応答としてありうる（notes/reactions/create 等）
- HTTPを実際に行うのは _transport() 一箇所のみ。テストはここを差し替える
"""
import json
import os
import socket
import time
import urllib.error
import urllib.request

from _i18n import t


DEFAULT_BASE = "https://misskey.io"
TOKEN_ENV = "CG_MISSKEY_TOKEN"
BASE_ENV = "CG_MISSKEY_BASE_URL"

# ノート本文の上限（Misskey の MAX_NOTE_TEXT_LENGTH）
MAX_NOTE_TEXT_LENGTH = 3000
# CW（閲覧注意）の見出しは 1〜100 文字。null は可、空文字は不可
MAX_CW_LENGTH = 100

# 429 のとき、この秒数以下なら待って1回だけ再試行する。
# これを超える待ちは manifest の timeout(60秒) 内に収まらず親プロセスに殺されるため、
# 待たずに retry_after_seconds 付きのエラーを返して柚月に判断を委ねる。
RETRY_MAX_WAIT = 10


class APIError(Exception):
    """Misskey APIのエラー。status=0 はネットワーク層の失敗を表す。"""

    def __init__(self, status, body, hint=None, retry_after=None):
        self.status = status
        self.body = body
        self.hint = hint
        self.retry_after = retry_after
        super().__init__(f"HTTP {status}" + (f": {_error_text(body)}" if body else ""))


def _error_text(body):
    """Misskey のエラーボディから人間（と柚月）に読める一文を作る。

    Misskey の形: {"error": {"message": ..., "code": ..., "id": ..., "kind": ...}}
    """
    if not isinstance(body, dict):
        return str(body)[:200] if body else ""
    err = body.get("error")
    if isinstance(err, dict):
        code = err.get("code")
        msg = err.get("message")
        if code and msg:
            return f"{code}: {msg}"
        return str(code or msg or err)[:200]
    return str(body.get("message") or body.get("error") or "")[:200]


def get_token():
    """アクセストークンを環境変数から読む（未設定なら空文字）。"""
    return os.environ.get(TOKEN_ENV, "").strip()


def get_base_url():
    """接続先インスタンスのベースURL。末尾の / は落とす。"""
    return (os.environ.get(BASE_ENV) or DEFAULT_BASE).strip().rstrip("/")


def build_url(path):
    """{base}/api/{path} を作る。base 末尾や path 先頭の / が重なっても壊れない。"""
    return get_base_url() + "/api/" + str(path).lstrip("/")


def _parse_body(raw, content_type=""):
    """レスポンス本文を解析する。

    204 や空ボディは正常（None を返す）。JSON でなければ切り詰めた文字列として扱う。
    """
    if raw is None or not str(raw).strip():
        return None
    if "json" in (content_type or "").lower():
        try:
            return json.loads(raw)
        except Exception:
            return {"message": str(raw)[:500]}
    try:
        return json.loads(raw)
    except Exception:
        return {"message": str(raw)[:500]}


def _retry_after_of(headers):
    """Retry-After ヘッダ（秒）を int で返す。無い・不正なら None。"""
    if not headers:
        return None
    try:
        value = headers.get("Retry-After")
    except AttributeError:
        return None
    if value is None:
        return None
    try:
        return max(0, int(float(str(value).strip())))
    except (TypeError, ValueError):
        return None


def _transport(path, body=None, token=None, timeout=15):
    """
    HTTPリクエストを実際に行う唯一の場所。

    戻り値: (status_code, retry_after_seconds, parsed_body)
      - parsed_body は 204／空ボディのとき None
      - retry_after_seconds はヘッダが無ければ None
    HTTPエラー(4xx/5xx)も戻り値として返す。ネットワーク層の失敗のみ APIError(0) を送出する。
    テストからはこの関数を差し替えることで、実ネットワークに出ずに検証できる。

    トークンはこの関数の中だけでボディに合流させる（呼び出し側にトークン入りの
    ボディを持たせない＝ログや例外に載る経路を最小化する）。
    """
    payload = dict(body or {})
    if token:
        payload["i"] = token

    data = json.dumps(payload).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": "CrescentGrove-MisskeySatellite/1.0",
    }
    req = urllib.request.Request(build_url(path), data=data, headers=headers, method="POST")

    try:
        with urllib.request.urlopen(req, timeout=timeout) as res:
            raw = res.read().decode("utf-8", errors="replace")
            ctype = res.headers.get("Content-Type", "") if res.headers else ""
            return res.status, _retry_after_of(res.headers), _parse_body(raw, ctype)
    except urllib.error.HTTPError as e:
        raw = ""
        try:
            raw = e.read().decode("utf-8", errors="replace")
        except Exception:
            pass
        ctype = e.headers.get("Content-Type", "") if e.headers else ""
        return e.code, _retry_after_of(e.headers), _parse_body(raw, ctype)
    except urllib.error.URLError as e:
        # タイムアウトは URLError.reason に包まれて来る経路がある
        if isinstance(e.reason, (socket.timeout, TimeoutError)):
            raise APIError(0, {"error": t("msat_err_timeout", sec=timeout)},
                           hint=t("msat_err_network_hint"))
        raise APIError(0, {"error": t("msat_err_network", reason=e.reason)},
                       hint=t("msat_err_network_hint"))
    except (socket.timeout, TimeoutError):
        raise APIError(0, {"error": t("msat_err_timeout", sec=timeout)},
                       hint=t("msat_err_network_hint"))


def _hint_for(status):
    """HTTPステータスから、柚月が次の一手を選べるヒントを返す。"""
    return {
        400: t("msat_hint_400"),
        401: t("msat_hint_401"),
        403: t("msat_hint_403"),
        404: t("msat_hint_404"),
    }.get(status, t("msat_hint_5xx") if status >= 500 else None)


def request(path, body=None, timeout=15, _retry=0):
    """
    Misskey APIを呼ぶ。成功時はパース済みボディ（204・空ボディなら None）を返す。

    トークン未設定・HTTPエラーは APIError を送出する。呼び出し側は捕まえずに
    main.py まで投げてよい（main.py が自己回復ヒント付きJSONに整形する）。

    自動再試行は **429 かつ Retry-After が 1〜10秒のときだけ1回**。
    タイムアウト・接続断・5xx では再試行しない（post の二重投稿を防ぐため。
    429 はサーバが処理を拒否済みなので、待って投げ直しても重複しない）。
    """
    token = get_token()
    if not token:
        raise APIError(0, {"error": t("msat_err_no_token")}, hint=t("msat_err_no_token_hint"))

    status, retry_after, data = _transport(path, body=body, token=token, timeout=timeout)

    if 200 <= status < 300:
        return data

    if status == 429:
        if _retry == 0 and retry_after is not None and 0 < retry_after <= RETRY_MAX_WAIT:
            time.sleep(retry_after + 0.2)
            return request(path, body=body, timeout=timeout, _retry=1)
        # 待ちが長い／ヘッダが無い場合は待たずに返し、いつ再実行すればよいかを伝える
        raise APIError(
            429, data,
            hint=(t("msat_hint_429_wait", sec=retry_after) if retry_after
                  else t("msat_hint_429_unknown")),
            retry_after=retry_after,
        )

    raise APIError(status, data, hint=_hint_for(status))


# --- ユーザー指定の解決 -------------------------------------------------
# following/create・following/delete・users/notes はいずれも userId が必須。
# 一方 users/show は userId でも username+host でも引ける。
# 柚月が「@名前」で呼べるように、ここで一本化して解決する。

def parse_user_ref(user):
    """user 引数を users/show 用のボディへ変換する。

      "@alice"             → {"username": "alice", "host": None}   （ローカル）
      "@alice@example.com" → {"username": "alice", "host": "example.com"}（リモート）
      それ以外              → {"userId": "..."}
    """
    s = str(user or "").strip()
    if not s.startswith("@"):
        return {"userId": s}
    parts = s[1:].split("@")
    username = parts[0]
    host = parts[1] if len(parts) > 1 and parts[1] else None
    return {"username": username, "host": host}


def fetch_user(user):
    """user 引数からユーザー情報（users/show の応答）を取る。"""
    return request("users/show", parse_user_ref(user))


def resolve_user_id(user):
    """user 引数を userId へ解決する。

    userId 直指定のときは users/show を呼ばない（余計な往復をしない）。
    """
    s = str(user or "").strip()
    if not s.startswith("@"):
        return s
    info = fetch_user(s) or {}
    return info.get("id")
