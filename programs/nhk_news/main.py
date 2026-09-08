"""
nhk_news v3 - NHK World English ニュース取得サテライト

モード1（見出し一覧）: 引数なし or countのみ → sitemap-news.xml から最新見出しを返す
モード2（本文取得）: url指定 → 記事ページから本文テキストを抽出して返す
"""
import json
import sys
import re
import xml.etree.ElementTree as ET
from urllib.request import urlopen, Request
from urllib.error import URLError, HTTPError
from html.parser import HTMLParser

from _i18n import t

SITEMAP_URL = "https://www3.nhk.or.jp/nhkworld/sitemap-news.xml"
USER_AGENT = "CrescentGrove-NHKNews/3.0"
DEFAULT_COUNT = 15
ALLOWED_DOMAIN = "www3.nhk.or.jp"

NS = {
    "sm": "http://www.sitemaps.org/schemas/sitemap/0.9",
    "news": "http://www.google.com/schemas/sitemap-news/0.9",
}


# ---------------------------------------------------------------------------
#  共通: HTTP取得
# ---------------------------------------------------------------------------

def fetch(url: str) -> bytes:
    req = Request(url, headers={"User-Agent": USER_AGENT})
    with urlopen(req, timeout=20) as resp:
        return resp.read()


# ---------------------------------------------------------------------------
#  モード1: 見出し一覧
# ---------------------------------------------------------------------------

def get_headlines(count: int) -> dict:
    xml_bytes = fetch(SITEMAP_URL)
    root = ET.fromstring(xml_bytes)
    items = []

    for url_elem in root.findall("sm:url", NS):
        loc = url_elem.findtext("sm:loc", "", NS).strip()
        news_elem = url_elem.find("news:news", NS)
        if news_elem is None:
            continue

        title = news_elem.findtext("news:title", "", NS).strip()
        pub_date = news_elem.findtext("news:publication_date", "", NS).strip()

        if not title:
            continue

        items.append({
            "title": title,
            "url": loc,
            "date": pub_date,
        })

    items.sort(key=lambda x: x["date"], reverse=True)
    items = items[:count]

    return {
        "status": "success",
        "message": t("nhk_news_headlines_ok", count=len(items)),
        "data": {"articles": items}
    }


# ---------------------------------------------------------------------------
#  モード2: 本文抽出
# ---------------------------------------------------------------------------

class ArticleExtractor(HTMLParser):
    """NHK World 記事ページから本文テキストを抽出する簡易パーサー"""

    # 本文が含まれるタグ/class のヒント
    # NHK World の記事は <article> 内、または class に "body" "content" "detail" を含む要素に本文がある
    CONTENT_TAGS = {"article", "main"}
    SKIP_TAGS = {"script", "style", "nav", "header", "footer", "aside", "figure", "figcaption", "button", "form"}

    def __init__(self):
        super().__init__()
        self.in_content = False
        self.in_skip = 0
        self.depth = 0
        self.content_depth = 0
        self.texts = []
        self.title = ""
        self.in_title = False

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()

        if tag == "title":
            self.in_title = True
            return

        if tag in self.SKIP_TAGS:
            self.in_skip += 1
            return

        if tag in self.CONTENT_TAGS:
            self.in_content = True
            self.content_depth = self.depth

        if self.in_content:
            self.depth += 1
            # 段落やヘッダーの前に改行を入れる
            if tag in ("p", "h1", "h2", "h3", "h4", "h5", "h6", "br", "li"):
                self.texts.append("\n")

    def handle_endtag(self, tag):
        tag = tag.lower()

        if tag == "title":
            self.in_title = False
            return

        if tag in self.SKIP_TAGS:
            self.in_skip = max(0, self.in_skip - 1)
            return

        if self.in_content:
            self.depth -= 1
            if tag in self.CONTENT_TAGS and self.depth <= self.content_depth:
                self.in_content = False

    def handle_data(self, data):
        if self.in_title and not self.title:
            self.title = data.strip()

        if self.in_content and self.in_skip == 0:
            text = data.strip()
            if text:
                self.texts.append(text)

    def get_article(self) -> tuple[str, str]:
        body = "\n".join(self.texts)
        # 連続する空行を整理
        body = re.sub(r"\n{3,}", "\n\n", body)
        return self.title, body.strip()


def get_article(url: str) -> dict:
    # ドメイン制限
    from urllib.parse import urlparse
    parsed = urlparse(url)
    if parsed.hostname != ALLOWED_DOMAIN:
        return {
            "status": "error",
            "message": t("nhk_news_err_domain", hostname=parsed.hostname, allowed=ALLOWED_DOMAIN),
            "data": {}
        }

    html_bytes = fetch(url)
    html_str = html_bytes.decode("utf-8", errors="replace")

    parser = ArticleExtractor()
    parser.feed(html_str)
    title, body = parser.get_article()

    if not body:
        return {
            "status": "error",
            "message": t("nhk_news_err_no_body"),
            "data": {"url": url}
        }

    return {
        "status": "success",
        "message": t("nhk_news_article_ok", title=title),
        "data": {
            "url": url,
            "title": title,
            "body": body,
        }
    }


# ---------------------------------------------------------------------------
#  メイン
# ---------------------------------------------------------------------------

def main():
    args = json.loads(sys.stdin.read())
    url = args.get("url", "")
    count = args.get("count", DEFAULT_COUNT)

    try:
        if url:
            print(f"[nhk_news] Fetching article: {url}", file=sys.stderr)
            result = get_article(url)
        else:
            print(f"[nhk_news] Fetching headlines (count={count})", file=sys.stderr)
            result = get_headlines(count)
    except (URLError, HTTPError) as e:
        result = {
            "status": "error",
            "message": t("nhk_news_err_http", e=e),
            "data": {}
        }
    except ET.ParseError as e:
        result = {
            "status": "error",
            "message": t("nhk_news_err_xml_parse", e=e),
            "data": {}
        }
    except Exception as e:
        result = {
            "status": "error",
            "message": t("nhk_news_err_unexpected", e=e),
            "data": {}
        }

    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
