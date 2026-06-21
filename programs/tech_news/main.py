# programs/tech_news/main.py
import sys
import json
import urllib.request
import xml.etree.ElementTree as ET
from html.parser import HTMLParser
from html import unescape
from datetime import datetime

from _i18n import t

RSS_SOURCES = {
    "techcrunch": {
        "url": "https://techcrunch.com/category/artificial-intelligence/feed/",
        "name": "TechCrunch AI"
    },
    "arstechnica": {
        "url": "https://feeds.arstechnica.com/arstechnica/technology-lab",
        "name": "Ars Technica Tech"
    }
}

USER_AGENT = "CrescentGrove-TechNews/1.0"
ALLOWED_DOMAINS = ["techcrunch.com", "arstechnica.com"]


# ── HTML → plain text ──
class TextExtractor(HTMLParser):
    def __init__(self):
        super().__init__()
        self._text = []

    def handle_data(self, data):
        self._text.append(data)

    def get_text(self):
        return " ".join("".join(self._text).split())


def html_to_text(html_str):
    ext = TextExtractor()
    ext.feed(unescape(html_str))
    return ext.get_text()


# ── Article body extractor ──
class ArticleExtractor(HTMLParser):
    SKIP_TAGS = {"script", "style", "nav", "header", "footer", "aside", "noscript", "svg", "form"}

    def __init__(self):
        super().__init__()
        self._in_article = False
        self._skip_depth = 0
        self._text = []
        self._title = ""
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        if tag == "title":
            self._in_title = True
        if tag in ("article", "main"):
            self._in_article = True
        if tag in self.SKIP_TAGS:
            self._skip_depth += 1

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False
        if tag in ("article", "main"):
            self._in_article = False
        if tag in self.SKIP_TAGS:
            self._skip_depth = max(0, self._skip_depth - 1)

    def handle_data(self, data):
        if self._in_title and not self._title:
            self._title = data.strip()
        if self._in_article and self._skip_depth == 0:
            stripped = data.strip()
            if stripped:
                self._text.append(stripped)

    def get_result(self):
        return self._title, "\n".join(self._text)


def fetch_url(url):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=20) as resp:
        return resp.read().decode("utf-8", errors="replace")


# ── Mode 1: Headlines from RSS ──
def get_headlines(source_key, count):
    src = RSS_SOURCES.get(source_key)
    if not src:
        return {"status": "error", "message": t("tech_news_err_unknown_source", source=source_key)}

    try:
        xml_text = fetch_url(src["url"])
    except Exception as e:
        return {"status": "error", "message": t("tech_news_err_fetch_rss", e=e)}

    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as e:
        return {"status": "error", "message": t("tech_news_err_xml_parse", e=e)}

    articles = []
    for item in root.iter("item"):
        title_el = item.find("title")
        link_el = item.find("link")
        pub_el = item.find("pubDate")
        desc_el = item.find("description")

        title = title_el.text.strip() if title_el is not None and title_el.text else ""
        link = link_el.text.strip() if link_el is not None and link_el.text else ""
        pub = pub_el.text.strip() if pub_el is not None and pub_el.text else ""
        desc = html_to_text(desc_el.text) if desc_el is not None and desc_el.text else ""

        # Truncate description to keep tokens low
        if len(desc) > 200:
            desc = desc[:200] + "..."

        articles.append({"title": title, "description": desc, "date": pub, "url": link})

        if len(articles) >= count:
            break

    return {
        "status": "success",
        "message": t("tech_news_headlines_ok", source=src["name"], count=len(articles)),
        "data": {"source": src["name"], "articles": articles}
    }


# ── Mode 2: Article body ──
def get_article(url):
    domain_ok = any(d in url for d in ALLOWED_DOMAINS)
    if not domain_ok:
        return {"status": "error", "message": t("tech_news_err_domain_not_allowed", allowed=", ".join(ALLOWED_DOMAINS))}

    try:
        html = fetch_url(url)
    except Exception as e:
        return {"status": "error", "message": t("tech_news_err_fetch_article", e=e)}

    parser = ArticleExtractor()
    parser.feed(html)
    title, body = parser.get_result()

    if not body:
        return {"status": "error", "message": t("tech_news_err_no_body")}

    return {
        "status": "success",
        "message": t("tech_news_article_ok", chars=len(body)),
        "data": {"title": title, "body": body}
    }


# ── Main ──
def main():
    raw = sys.stdin.read().strip()
    if not raw:
        raw = "{}"
    try:
        args = json.loads(raw)
    except json.JSONDecodeError:
        args = {}

    url = args.get("url")
    if url:
        result = get_article(url)
    else:
        source = args.get("source", "techcrunch").lower()
        count = args.get("count", 8)
        result = get_headlines(source, count)

    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
