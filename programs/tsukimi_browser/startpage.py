"""tsukimi_browser - 仮想ページ（スタートページ / 検索結果 / 棚 / 履歴）と lucky 抽選。

仮想ページとは「HTML を取らずに生成するページ」。実ページと同じく
リンク番号 + follow で扱えるので、柚月から見れば区別なく歩ける。
"""
import random
import xml.etree.ElementTree as ET
from pathlib import Path

import yaml

import browser
from _i18n import t, get_language

# SearXNG（ローカル）。check_url は private を弾くので、ここだけ専用経路で叩く。
SEARXNG_URL = "http://localhost:13254"

DATA_DIR = Path(__file__).resolve().parent / "data"

LUCKY_POOLS = ["wikipedia", "spots", "rss", "memory"]

WIKIPEDIA_RANDOM = {
    "ja": "https://ja.wikipedia.org/wiki/特別:おまかせ表示",
    "en": "https://en.wikipedia.org/wiki/Special:Random",
}


def make_page(title, text, links, url=None):
    """仮想ページ dict を作る。"""
    return {
        "url": url, "title": title, "text": text, "links": links,
        "virtual": True, "viewport": 1,
    }


def load_spots():
    """お出かけ先リストと RSS フィード一覧を読む。壊れていても散歩を止めない。"""
    path = DATA_DIR / "spots.yaml"
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception:
        return {"feeds": [], "spots": []}
    return {
        "feeds": [f for f in (data.get("feeds") or []) if f.get("url")],
        "spots": [s for s in (data.get("spots") or []) if s.get("url")],
    }


# --------------------------------------------------------------------------
# RSS
# --------------------------------------------------------------------------
def fetch_feed(url, limit=8, _requests=None):
    """RSS/Atom から [{"title","link"}] を取る。失敗時は空リスト（fail-soft）。"""
    kwargs = {"_requests": _requests} if _requests is not None else {}
    res = browser.http_get(url, **kwargs)
    if not res.get("ok"):
        return []
    try:
        root = ET.fromstring(res["body"].encode("utf-8", "replace"))
    except Exception:
        return []
    items = []
    for item in root.iter():
        tag = item.tag.split("}")[-1]
        if tag not in ("item", "entry"):
            continue
        title, link = None, None
        for child in item:
            ctag = child.tag.split("}")[-1]
            if ctag == "title" and child.text:
                title = child.text.strip()
            elif ctag == "link":
                link = (child.text or "").strip() or child.get("href")
        if title and link:
            items.append({"title": title, "link": link})
        if len(items) >= limit:
            break
    return items


# --------------------------------------------------------------------------
# スタートページ
# --------------------------------------------------------------------------
def build_start_page(session, seed_text=None, seed_url=None, _requests=None):
    """今日の玄関。白紙のアドレスバー問題を避けるための入口。"""
    lines, links = [], []

    def add_link(url, label):
        n = len(links) + 1
        links.append({"n": n, "url": url, "label": label[:120]})
        return n

    lines.append(t("tsukimi_start_title"))
    lines.append("")

    # きっかけ（他エージェント / ニュース由来）は最上段
    if seed_text or seed_url:
        lines.append(t("tsukimi_start_seed"))
        if seed_text:
            lines.append("  " + seed_text)
        if seed_url:
            n = add_link(seed_url, seed_text or seed_url)
            lines.append("  [{}] {}".format(n, seed_url))
        lines.append("")

    # ニュース見出し（取得失敗はセクションごと省略）
    spots = load_spots()
    feeds = list(spots["feeds"])
    random.shuffle(feeds)
    news = []
    for feed in feeds[:2]:
        for item in fetch_feed(feed["url"], limit=5, _requests=_requests):
            news.append((feed.get("name", ""), item))
        if len(news) >= 5:
            break
    if news:
        lines.append(t("tsukimi_start_news"))
        for name, item in news[:5]:
            n = add_link(item["link"], item["title"])
            lines.append("  [{}] {}{}".format(n, item["title"], " ({})".format(name) if name else ""))
        lines.append("")

    # ブックマーク棚（最近3件 + ランダム2件）
    marks = session.bookmarks()
    if marks:
        recent = list(reversed(marks))[:3]
        rest = [m for m in marks if m not in recent]
        picks = recent + random.sample(rest, min(2, len(rest)))
        lines.append(t("tsukimi_start_bookmarks"))
        for m in picks:
            n = add_link(m["url"], m.get("title") or m["url"])
            note = " ─ {}".format(m["note"]) if m.get("note") else ""
            lines.append("  [{}] {}{}".format(n, m.get("title") or m["url"], note))
        lines.append("")

    # 前回の続き
    last = session.last_page()
    if last and last.get("url"):
        lines.append(t("tsukimi_start_last"))
        n = add_link(last["url"], last.get("title") or last["url"])
        lines.append("  [{}] {}".format(n, last.get("title") or last["url"]))
        lines.append("")

    lines.append(t("tsukimi_start_lucky"))
    lines.append("")
    lines.append(t("tsukimi_start_footer"))

    return make_page(t("tsukimi_start_page_title"), "\n".join(lines), links)


# --------------------------------------------------------------------------
# lucky
# --------------------------------------------------------------------------
def iter_lucky(session, _requests=None, _random=random):
    """🎲 母集団をシャッフルして順に行き先候補を出す。

    呼び出し側は最初の候補が 403 などで開けなければ次を試せる。
    どのサイトが落ちていても散歩が止まらないようにするための形。
    """
    pools = list(LUCKY_POOLS)
    _random.shuffle(pools)
    spots = load_spots()

    for pool in pools:
        if pool == "wikipedia":
            lang = get_language()
            yield {"url": WIKIPEDIA_RANDOM.get(lang, WIKIPEDIA_RANDOM["ja"]), "pool": pool}

        elif pool == "spots" and spots["spots"]:
            spot = _random.choice(spots["spots"])
            yield {"url": spot["url"], "pool": pool}

        elif pool == "rss" and spots["feeds"]:
            feed = _random.choice(spots["feeds"])
            items = fetch_feed(feed["url"], limit=20, _requests=_requests)
            if items:
                yield {"url": _random.choice(items)["link"], "pool": pool}

        elif pool == "memory":
            # 履歴とブックマークからの再訪。「そういえば」の再現。
            # 今日の散歩で既に見たページは除く（さっき見たところに戻されても発見が無い）
            seen = {v.get("url") for v in (session.state or {}).get("visited", [])}
            cur = (session.current() or {}).get("url")
            if cur:
                seen.add(cur)
            candidates = [h["url"] for h in session.history(limit=50)
                          if h.get("url") and h["url"] not in seen]
            candidates += [b["url"] for b in session.bookmarks()
                           if b.get("url") and b["url"] not in seen]
            if candidates:
                yield {"url": _random.choice(candidates), "pool": pool}


def pick_lucky(session, _requests=None, _random=random):
    """🎲 行き先を1つ返す。見つからなければ None。"""
    for pick in iter_lucky(session, _requests=_requests, _random=_random):
        return pick
    return None


# --------------------------------------------------------------------------
# 検索（SearXNG）
# --------------------------------------------------------------------------
def search_page(query, _requests=None):
    """SearXNG で検索し、結果を仮想ページにする。使えなければ None（fail-soft）。"""
    import json as _json
    from urllib.parse import urlencode

    req = _requests or browser.requests
    url = "{}/search?{}".format(
        SEARXNG_URL, urlencode({"q": query, "format": "json", "language": "ja"}))
    try:
        # SearXNG はローカル固定 URL なので check_url を通さない専用経路
        resp = req.get(url, timeout=browser.TIMEOUT,
                       headers={"User-Agent": browser.USER_AGENT})
        if resp.status_code >= 400:
            return None
        data = _json.loads(resp.text)
    except Exception:
        return None

    results = (data.get("results") or [])[:10]
    if not results:
        return None

    lines, links = [t("tsukimi_search_title", query=query), ""], []
    for r in results:
        href = r.get("url")
        title = (r.get("title") or href or "").strip()
        if not href:
            continue
        n = len(links) + 1
        links.append({"n": n, "url": href, "label": title[:120]})
        lines.append("[{}] {}".format(n, title))
        content = (r.get("content") or "").strip()
        if content:
            lines.append("    " + content[:160])
        lines.append("")

    if not links:
        return None
    return make_page(t("tsukimi_search_page_title", query=query), "\n".join(lines), links)


# --------------------------------------------------------------------------
# ブックマーク棚 / 履歴
# --------------------------------------------------------------------------
def bookmarks_page(session):
    marks = list(reversed(session.bookmarks()))
    if not marks:
        return make_page(t("tsukimi_bookmarks_page_title"), t("tsukimi_bookmarks_empty"), [])
    lines, links = [t("tsukimi_bookmarks_page_title"), ""], []
    for m in marks:
        n = len(links) + 1
        links.append({"n": n, "url": m["url"], "label": m.get("title") or m["url"]})
        note = " ─ {}".format(m["note"]) if m.get("note") else ""
        lines.append("[{}] {}{}".format(n, m.get("title") or m["url"], note))
    return make_page(t("tsukimi_bookmarks_page_title"), "\n".join(lines), links)


def history_page(session, limit=30):
    recs = session.history(limit=limit)
    if not recs:
        return make_page(t("tsukimi_history_page_title"), t("tsukimi_history_empty"), [])
    lines, links = [t("tsukimi_history_page_title"), ""], []
    for r in recs:
        n = len(links) + 1
        links.append({"n": n, "url": r["url"], "label": r.get("title") or r["url"]})
        ts = (r.get("ts") or "")[:16].replace("T", " ")
        lines.append("[{}] {}  ({})".format(n, r.get("title") or r["url"], ts))
    return make_page(t("tsukimi_history_page_title"), "\n".join(lines), links)
