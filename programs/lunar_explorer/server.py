"""
Lunar Explorer - 人間モードAPIサーバー

本文取得・深掘り調査・対話型チャット対応。
"""
import json
import sys
import os
import re
from http.server import HTTPServer, BaseHTTPRequestHandler

sys.path.insert(0, os.path.dirname(__file__))
from core import (
    search, fetch_content, call_llm,
    make_parallel_queries, format_results_with_content
)

PORT = 8765
MAX_FETCH_URLS = 3


def run_human(query: str, max_iterations: int = 5, history: list = None) -> dict:
    history = history or []
    all_results = []
    search_log = []

    ja_query, en_query = make_parallel_queries(query)
    print(f"[lunar/human] ja: {ja_query} / en: {en_query}", file=sys.stderr)

    results_ja = search(ja_query)
    results_en = search(en_query) if en_query != ja_query else []
    merged = results_ja + results_en
    all_results.extend(merged)
    search_log.append({"query": f"{ja_query} / {en_query}", "results_count": len(merged)})

    for iteration in range(max_iterations - 1):
        fetched = {}
        for r in all_results[:MAX_FETCH_URLS]:
            content = fetch_content(r["url"], max_chars=2000)
            if content:
                fetched[r["url"]] = content

        formatted = format_results_with_content(all_results, fetched)
        judge_messages = [
            {
                "role": "system",
                "content": (
                    "You are a search agent. Be strict - only output SUFFICIENT if the information "
                    "comprehensively answers the question with specific details and evidence. "
                    "If there is any uncertainty or missing detail, output one additional search query in English only."
                ),
            },
            {
                "role": "user",
                "content": (
                    f"Question: {query}\n\n"
                    f"Searches completed: {len(search_log)}/{max_iterations}\n"
                    f"Queries used: {[s['query'] for s in search_log]}\n\n"
                    f"Information collected:\n{formatted[:2000]}\n\n"
                    f"Output SUFFICIENT if you can give a reasonably good answer. "
                    f"If critical information is still missing, output one more search query (text only). "
                    f"After {max_iterations-1} searches, always output SUFFICIENT."
                ),
            },
        ]
        judgment = call_llm(judge_messages, max_tokens=128)
        print(f"[lunar/human] judgment {iteration+1}: {judgment}", file=sys.stderr)

        if "SUFFICIENT" in judgment:
            break

        next_query = judgment.strip()
        # リスト形式 ["query"] を除去
        next_query = re.sub(r'^\[[\'""]?|[\'""]?\]$', '', next_query).strip().strip('"\'')
        if next_query and next_query != "SUFFICIENT":
            extra = search(next_query)
            all_results.extend(extra)
            search_log.append({"query": next_query, "results_count": len(extra)})
        else:
            break

    fetched_final = {}
    for r in all_results[:MAX_FETCH_URLS]:
        content = fetch_content(r["url"], max_chars=2000)
        if content:
            fetched_final[r["url"]] = content

    formatted_final = format_results_with_content(all_results[:12], fetched_final)

    system_msg = {
        "role": "system",
        "content": (
            "あなたは優秀なリサーチアシスタントです。"
            "収集した情報を統合し、質問に対する詳細で読みやすい回答を日本語で作成してください。"
            "Markdownで整形し、情報源のURLを適宜引用してください。"
            "対話の文脈を踏まえて回答してください。"
        ),
    }
    messages = [system_msg] + history + [
        {
            "role": "user",
            "content": (
                f"質問: {query}\n\n"
                f"収集した情報:\n{formatted_final}\n\n"
                f"上記の情報を統合して、詳細な回答を作成してください。"
            ),
        }
    ]
    answer = call_llm(messages, max_tokens=2048)

    return {
        "status": "success",
        "query": query,
        "search_log": search_log,
        "answer": answer,
    }


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def send_cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_cors()
        self.end_headers()

    def do_POST(self):
        if self.path != "/search":
            self.send_response(404)
            self.end_headers()
            return

        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        try:
            data = json.loads(body.decode("utf-8"))
            query = data.get("query", "").strip()
            max_iter = int(data.get("max_iterations", 5))
            history = data.get("history", [])

            if not query:
                raise ValueError("queryが空です")

            result = run_human(query, max_iter, history)
            resp_body = json.dumps(result, ensure_ascii=False).encode("utf-8")

            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_cors()
            self.end_headers()
            self.wfile.write(resp_body)

        except Exception as e:
            err = json.dumps({"status": "error", "message": str(e)}, ensure_ascii=False).encode("utf-8")
            self.send_response(500)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_cors()
            self.end_headers()
            self.wfile.write(err)


if __name__ == "__main__":
    print(f"[Lunar Explorer] サーバー起動: http://localhost:{PORT}", file=sys.stderr)
    server = HTTPServer(("localhost", PORT), Handler)
    server.serve_forever()
