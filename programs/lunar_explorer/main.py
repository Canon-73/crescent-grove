"""
Lunar Explorer - 柚月モード

トークン節約最優先。核心情報を英語で濃縮して返す。
run_programツール経由で柚月が使用。
"""
import json
import sys
import os

from _i18n import t

sys.path.insert(0, os.path.dirname(__file__))
from core import search, call_llm, make_parallel_queries, format_results_snippet


def run_yuzuki(query: str, max_iterations: int = 2) -> dict:
    all_results = []
    search_log = []

    # 日英並行クエリ生成
    ja_query, en_query = make_parallel_queries(query)
    print(f"[lunar/yuzuki] ja: {ja_query} / en: {en_query}", file=sys.stderr)

    # 初回検索（日英同時）
    results_ja = search(ja_query)
    results_en = search(en_query) if en_query != ja_query else []
    all_results = results_ja + results_en
    search_log.append({"query": f"{ja_query} / {en_query}", "results_count": len(all_results)})

    # 反復ループ
    for iteration in range(max_iterations - 1):
        formatted = format_results_snippet(all_results)
        judge_messages = [
            {
                "role": "system",
                "content": "You are a search agent. Reply with only the specified format.",
            },
            {
                "role": "user",
                "content": (
                    f"Question: {query}\n\n"
                    f"Search results so far:\n{formatted}\n\n"
                    f"Is this sufficient to answer the question?\n"
                    f"If yes, output: SUFFICIENT\n"
                    f"If no, output one additional search query in English only."
                ),
            },
        ]
        judgment = call_llm(judge_messages, max_tokens=64)
        print(f"[lunar/yuzuki] judgment: {judgment}", file=sys.stderr)

        if "SUFFICIENT" in judgment:
            break

        next_query = judgment.strip().strip('"\'')
        if next_query:
            extra = search(next_query)
            all_results.extend(extra)
            search_log.append({"query": next_query, "results_count": len(extra)})
        else:
            break

    # 濃縮回答生成（英語・箇条書き・200words以内）
    formatted_final = format_results_snippet(all_results, max_chars=400)
    answer_messages = [
        {
            "role": "system",
            "content": (
                "You are a research assistant for an AI agent. "
                "Summarize the key facts concisely in English bullet points. "
                "Max 200 words. Focus on essential information only. "
                "No intro, no outro, just the facts."
            ),
        },
        {
            "role": "user",
            "content": (
                f"Question: {query}\n\n"
                f"Sources:\n{formatted_final}\n\n"
                f"Provide a concise bullet-point summary."
            ),
        },
    ]
    answer = call_llm(answer_messages, max_tokens=512)

    return {
        "status": "success",
        "message": answer,
        "data": {
            "query": query,
            "search_log": search_log,
        }
    }


def main():
    args = json.loads(sys.stdin.read())
    query = args.get("query", "").strip()
    max_iter = int(args.get("max_iterations", 2))

    if not query:
        print(json.dumps({"status": "error", "message": t("lunar_explorer_err_empty_query")}, ensure_ascii=False))
        return

    result = run_yuzuki(query, max_iter)
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
