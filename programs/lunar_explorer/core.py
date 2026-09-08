"""
Lunar Explorer - 共通ロジック

search / fetch / llm / format の共通関数群。
main.py（柚月モード）と server.py（人間モード）から使用。
"""
import os
from dotenv import load_dotenv
load_dotenv()  # .envを読み込む
import json
import sys
import urllib.request
import urllib.parse
import urllib.error
import html
import re

# 設定
SEARXNG_URL = "http://localhost:13254"
LLM_URL = "https://api.deepseek.com/v1/chat/completions"
LLM_MODEL = "deepseek-chat"
MAX_RESULTS_PER_SEARCH = 5
SEARCH_TIMEOUT = 15
FETCH_TIMEOUT = 10
LLM_TIMEOUT = 120


def search(query: str) -> list:
    """SearXNGで検索してresultsを返す"""
    params = urllib.parse.urlencode({
        "q": query,
        "format": "json",
        "safesearch": "0",
    })
    url = f"{SEARXNG_URL}/search?{params}"
    req = urllib.request.Request(url, headers={"User-Agent": "LunarExplorer/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=SEARCH_TIMEOUT) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            results = data.get("results", [])[:MAX_RESULTS_PER_SEARCH]
            return [
                {
                    "title": r.get("title", ""),
                    "url": r.get("url", ""),
                    "content": r.get("content", ""),
                }
                for r in results
            ]
    except Exception as e:
        print(f"[lunar/core] 検索エラー: {e}", file=sys.stderr)
        return []


def fetch_content(url: str, max_chars: int = 2000) -> str:
    """URLのページ本文を取得してテキストを返す"""
    try:
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": "Mozilla/5.0 (compatible; LunarExplorer/1.0)",
                "Accept": "text/html,application/xhtml+xml",
                "Accept-Language": "ja,en",
            }
        )
        with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT) as resp:
            content_type = resp.headers.get("Content-Type", "")
            if "text/html" not in content_type and "text/plain" not in content_type:
                return ""
            raw = resp.read(50000).decode("utf-8", errors="ignore")

            # HTMLタグ除去
            raw = re.sub(r'<script[^>]*>.*?</script>', '', raw, flags=re.DOTALL)
            raw = re.sub(r'<style[^>]*>.*?</style>', '', raw, flags=re.DOTALL)
            raw = re.sub(r'<[^>]+>', ' ', raw)
            raw = html.unescape(raw)
            # 連続空白を圧縮
            raw = re.sub(r'\s+', ' ', raw).strip()
            return raw[:max_chars]
    except Exception as e:
        print(f"[lunar/core] フェッチエラー {url}: {e}", file=sys.stderr)
        return ""


def call_llm(messages: list, max_tokens: int = 1024) -> str:
    api_key = os.environ.get("CG_DEEPSEEK_SEARCH", "")
    payload = json.dumps({
        "model": LLM_MODEL,
        "messages": messages,
        "temperature": 0.3,
        "max_tokens": max_tokens,
    }).encode("utf-8")
    req = urllib.request.Request(
        LLM_URL,
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=LLM_TIMEOUT) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            return data["choices"][0]["message"]["content"].strip()
    except Exception as e:
        print(f"[lunar/core] LLMエラー: {e}", file=sys.stderr)
        return ""


def make_parallel_queries(query: str) -> tuple[str, str]:
    """日本語クエリと英語クエリを生成して返す"""
    # 英語クエリを生成
    messages = [
        {
            "role": "user",
            "content": (
                f"Translate this search query to English. Output only the translated query, nothing else.\n\n{query}"
            ),
        }
    ]
    en_query = call_llm(messages, max_tokens=64) or query
    # 余計な記号を除去
    en_query = en_query.strip('"\'').strip()
    return query, en_query


def format_results_with_content(results: list, fetched: dict) -> str:
    """検索結果＋本文をLLMに渡す形式に整形"""
    if not results:
        return "（検索結果なし）"
    lines = []
    for i, r in enumerate(results, 1):
        content = fetched.get(r["url"]) or r["content"]
        lines.append(f"[{i}] {r['title']}\nURL: {r['url']}\n{content[:1500]}")
    return "\n\n".join(lines)


def format_results_snippet(results: list, max_chars: int = 300) -> str:
    """スニペットのみの軽量整形（柚月モード用）"""
    if not results:
        return "(no results)"
    lines = []
    for i, r in enumerate(results, 1):
        lines.append(f"[{i}] {r['title']}: {r['content'][:max_chars]}")
    return "\n".join(lines)
