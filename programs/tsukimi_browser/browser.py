"""tsukimi_browser - 取得・抽出・ビューポートの純ロジック。

ネットワーク I/O は fetch_page() に閉じ込め、それ以外（extract / paginate /
check_url）はネット無しでテストできるようにしてある。
"""
import ipaddress
import re
import socket
from urllib.parse import urlsplit, urljoin

import requests
from bs4 import BeautifulSoup

USER_AGENT = "tsukimi_browser/1.0 (Crescent Grove satellite)"
TIMEOUT = 15
MAX_BYTES = 2 * 1024 * 1024      # 2MB
MAX_REDIRECTS = 5
VIEWPORT_CHARS = 2500

# テキストとして読める Content-Type
TEXT_TYPES = (
    "text/html", "text/plain", "application/xhtml+xml",
    "application/xml", "text/xml", "application/rss+xml", "application/atom+xml",
)

# 本文抽出前に丸ごと捨てるタグ（ゴミ排除）
DROP_TAGS = [
    "script", "style", "noscript", "template", "iframe", "svg",
    "nav", "header", "footer", "aside", "form", "button", "select",
]

# class / id がこれに当たる要素も捨てる（共有ボタン・パンくず・広告などのUI残骸）。
# 単語境界で見るので "add" や "header" のような無関係な語には当たらない。
NOISE_PATTERN = re.compile(
    r"(^|[-_ ])("
    r"share|sharing|social|sns|breadcrumb|related|recommend|advert|adsense"
    r"|banner|cookie|newsletter|subscribe|pagetop|gnav|globalnav|sidebar|widget"
    r")([-_ ]|$)", re.I)

# 改行を入れるブロック要素。インライン要素（a / span / strong 等）では改行しない。
# ※素朴な get_text("\n") はリンクごとに行が割れて本文が縦に細切れになるため、
#   ブロック境界にだけ改行を差し込む方式にしている（プロトタイプ検証済み）。
BLOCK_TAGS = [
    "p", "div", "section", "article", "main", "ul", "ol", "li",
    "h1", "h2", "h3", "h4", "h5", "h6", "tr", "table", "blockquote",
    "pre", "dl", "dt", "dd", "figure", "figcaption", "hr", "br", "address",
]

# 本文候補として認めるおおよその文字数（_pick_root の main/article 採用判定に使う）
MIN_BODY_CHARS = 120

# JS 必須ページ（SPA の殻）判定:
#   本文がこれ未満で、かつ HTML 自体は大きい ＝ 中身が JS でしか出てこないページ。
#   「短いだけの正当なページ」を誤って弾かないための2条件。
JS_PAGE_TEXT_MAX = 120
JS_PAGE_HTML_MIN = 20000


# --------------------------------------------------------------------------
# URL 安全チェック
# --------------------------------------------------------------------------
def _default_resolver(host):
    """ホスト名を IP 文字列のリストに解決する。"""
    infos = socket.getaddrinfo(host, None)
    return [info[4][0] for info in infos]


def check_url(url, resolver=None):
    """散歩先として安全な URL か調べる。

    戻り値: (ok: bool, reason: str|None)
      reason は "scheme" / "bad_url" / "dns" / "private" のいずれか。

    このPCでは Crescent Grove 本体や SearXNG が localhost に居るため、
    ページ内リンク経由でローカルサービスを叩かされる事故を構造的に防ぐ。
    """
    if not url or not isinstance(url, str):
        return False, "bad_url"
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return False, "bad_url"

    if parts.scheme not in ("http", "https"):
        return False, "scheme"

    try:
        host = parts.hostname
    except ValueError:
        return False, "bad_url"
    if not host:
        return False, "bad_url"

    resolve = resolver or _default_resolver
    try:
        addrs = resolve(host)
    except Exception:
        return False, "dns"
    if not addrs:
        return False, "dns"

    for addr in addrs:
        try:
            ip = ipaddress.ip_address(addr)
        except ValueError:
            return False, "dns"
        if (ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_reserved or ip.is_multicast or ip.is_unspecified):
            return False, "private"
    return True, None


# --------------------------------------------------------------------------
# 取得
# --------------------------------------------------------------------------
def _charset_from_content_type(content_type):
    m = re.search(r"charset\s*=\s*([\w\-]+)", content_type or "", re.I)
    return m.group(1) if m else None


def _charset_from_meta(raw):
    head = raw[:4096]
    m = re.search(br'charset\s*=\s*["\']?\s*([\w\-]+)', head, re.I)
    if m:
        try:
            return m.group(1).decode("ascii", "ignore")
        except Exception:
            return None
    return None


def decode_bytes(raw, header_charset=None):
    """バイト列を文字列にする。ヘッダ → meta charset → utf-8 → cp932 の順。"""
    candidates = []
    for cs in (header_charset, _charset_from_meta(raw)):
        if cs and cs.lower() not in ("iso-8859-1", "latin-1"):
            candidates.append(cs)
    candidates += ["utf-8", "cp932", "euc-jp"]
    for cs in candidates:
        try:
            return raw.decode(cs)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", "replace")


def http_get(url, resolver=None, _requests=requests):
    """安全チェック付きの GET。リダイレクトは自前で追い、毎回チェックし直す。

    戻り値: 成功 {"ok":True,"url":最終URL,"body":str}
            失敗 {"ok":False,"kind":...,"detail":...}
    """
    current = url
    for _ in range(MAX_REDIRECTS + 1):
        ok, reason = check_url(current, resolver=resolver)
        if not ok:
            return {"ok": False, "kind": reason, "detail": current}
        try:
            resp = _requests.get(
                current,
                timeout=TIMEOUT,
                headers={"User-Agent": USER_AGENT},
                stream=True,
                allow_redirects=False,
            )
        except Exception as e:
            kind = "timeout" if "Timeout" in type(e).__name__ else "network"
            return {"ok": False, "kind": kind, "detail": str(e)[:200]}

        # リダイレクト（Location を安全チェックし直してから追う）
        if resp.status_code in (301, 302, 303, 307, 308):
            loc = resp.headers.get("Location")
            resp.close()
            if not loc:
                return {"ok": False, "kind": "http", "detail": resp.status_code}
            current = urljoin(current, loc)
            continue

        if resp.status_code >= 400:
            resp.close()
            return {"ok": False, "kind": "http", "detail": resp.status_code}

        content_type = (resp.headers.get("Content-Type") or "").lower()
        if content_type and not any(t in content_type for t in TEXT_TYPES):
            resp.close()
            return {"ok": False, "kind": "binary", "detail": content_type.split(";")[0]}

        chunks, size = [], 0
        too_big = False
        try:
            for chunk in resp.iter_content(8192):
                chunks.append(chunk)
                size += len(chunk)
                if size > MAX_BYTES:
                    too_big = True
                    break
        except Exception as e:
            return {"ok": False, "kind": "network", "detail": str(e)[:200]}
        finally:
            resp.close()

        raw = b"".join(chunks)
        body = decode_bytes(raw, _charset_from_content_type(content_type))
        return {"ok": True, "url": current, "body": body, "truncated": too_big}

    return {"ok": False, "kind": "redirect", "detail": current}


# --------------------------------------------------------------------------
# 抽出
# --------------------------------------------------------------------------
def _pick_root(soup):
    """本文候補を main → article → 本文密度 → body の順で選ぶ簡易 readability。"""
    body = soup.body or soup
    for name in ("main", "article"):
        node = soup.find(name)
        if node and len(node.get_text(strip=True)) >= MIN_BODY_CHARS:
            return node
    # 密度ヒューリスティック: <p> のテキスト量が最も多い div を探す
    best, best_len = None, 0
    for div in body.find_all("div", recursive=True):
        plen = sum(len(p.get_text(strip=True)) for p in div.find_all("p", recursive=False))
        if plen > best_len:
            best, best_len = div, plen
    body_len = len(body.get_text(strip=True))
    if best is not None and best_len >= 400 and best_len > body_len * 0.5:
        return best
    return body


def _normalize_text(text):
    lines = [ln.strip() for ln in text.split("\n")]
    out, blank = [], False
    for ln in lines:
        if not ln:
            blank = True
            continue
        if out and blank:
            out.append("")
        blank = False
        out.append(ln)
    return "\n".join(out).strip()


def extract(html, base_url):
    """HTML → (title, text, links)。

    links は [{"n":1,"url":"...","label":"..."}]。本文中には [n]ラベル の形で
    インライン埋め込みされるので、柚月は follow n で飛べる。
    """
    soup = BeautifulSoup(html, "html.parser")

    title = ""
    if soup.title and soup.title.get_text(strip=True):
        title = soup.title.get_text(strip=True)
    else:
        og = soup.find("meta", attrs={"property": "og:title"})
        if og and og.get("content"):
            title = og["content"].strip()

    for tag in soup(DROP_TAGS):
        tag.decompose()

    # class / id が明らかに UI 部品のものを捨てる。
    # 親を decompose すると子孫も外れるので、走査中は必ず生死を確認する。
    for tag in soup.find_all(True):
        if getattr(tag, "decomposed", False) or not tag.attrs:
            continue
        marks = " ".join(tag.get("class") or []) + " " + (tag.get("id") or "")
        if NOISE_PATTERN.search(marks):
            tag.decompose()

    root = _pick_root(soup)

    # リンク収集 + 番号埋め込み
    links, seen = [], {}
    base_no_frag = base_url.split("#")[0]
    for a in root.find_all("a", href=True):
        href_raw = (a.get("href") or "").strip()
        if not href_raw or href_raw.startswith(("javascript:", "mailto:", "tel:")):
            continue
        href = urljoin(base_url, href_raw)
        if not href.startswith(("http://", "https://")):
            continue
        # 同一ページ内アンカーは飛ぶ意味がないので落とす
        if href.split("#")[0] == base_no_frag and "#" in href:
            continue
        label = a.get_text(" ", strip=True)
        if not label:
            continue
        if href in seen:
            n = seen[href]
        else:
            n = len(links) + 1
            seen[href] = n
            links.append({"n": n, "url": href, "label": label[:120]})
        a.string = "[{}]{}".format(n, label)

    # ブロック境界にだけ改行を差し込む
    for tag in root.find_all(BLOCK_TAGS):
        tag.insert_before("\n")
        tag.insert_after("\n")
    for tag in root.find_all(["td", "th"]):
        tag.insert_after(" ")

    text = _normalize_text(root.get_text(""))
    return title or "(no title)", text, links


def fetch_page(url, resolver=None, _requests=requests):
    """URL を開いて抽出済みページ dict を返す。失敗時は ok=False。"""
    res = http_get(url, resolver=resolver, _requests=_requests)
    if not res.get("ok"):
        return res
    title, text, links = extract(res["body"], res["url"])
    # 中身が空、または「HTML は大きいのに本文が出てこない」＝ JS 必須ページ。
    # 単に短いだけのページは、短いまま見せる（それも正当なページ）。
    if not text.strip() or (len(text) < JS_PAGE_TEXT_MAX
                            and len(res["body"]) >= JS_PAGE_HTML_MIN):
        return {"ok": False, "kind": "js_page", "detail": res["url"], "title": title}
    return {
        "ok": True, "url": res["url"], "title": title,
        "text": text, "links": links, "truncated": res.get("truncated", False),
    }


# --------------------------------------------------------------------------
# ビューポート
# --------------------------------------------------------------------------
def paginate(text, size=VIEWPORT_CHARS):
    """本文を約 size 文字ずつの画面に分割する（段落境界優先）。"""
    if not text:
        return [""]
    pages, cur, cur_len = [], [], 0
    for line in text.split("\n"):
        while len(line) > size:
            if cur:
                pages.append("\n".join(cur))
                cur, cur_len = [], 0
            pages.append(line[:size])
            line = line[size:]
        if cur and cur_len + len(line) + 1 > size:
            pages.append("\n".join(cur))
            cur, cur_len = [], 0
        cur.append(line)
        cur_len += len(line) + 1
    if cur:
        pages.append("\n".join(cur))
    return pages or [""]


def page_count(text, size=VIEWPORT_CHARS):
    return len(paginate(text, size))


def viewport(text, index, size=VIEWPORT_CHARS):
    """index（1始まり）の画面テキストを返す。"""
    pages = paginate(text, size)
    idx = max(1, min(index, len(pages)))
    return pages[idx - 1], idx, len(pages)
