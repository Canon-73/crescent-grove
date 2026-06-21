"""
記憶マップ生成スクリプト
エッジ数上位50件のセマンティックノードをカテゴリ分け+1行要約して
workspace/memory/knowledge_map.md に出力する
"""

import asyncio
import json
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pathlib import Path
from core.llm import OpenAICompatibleProvider

GRAPH_PATH = Path("data/wyrd_network.json")
OUTPUT_PATH = Path("workspace/memory/knowledge_map.md")

def _strip_preamble(text: str) -> str:
    """LLM出力の冒頭から、本文見出しより前の前置き・区切り線を取り除く。

    優先順位:
      1. 「## 記憶マップ」が見つかればそこから
      2. なければ最初の Markdown 見出し（# で始まる行）から
      3. どちらも無ければそのまま返す
    """
    lines = text.splitlines()

    # 1. 「## 記憶マップ」見出しを探す
    for i, line in enumerate(lines):
        if line.strip().startswith("## 記憶マップ"):
            return "\n".join(lines[i:]).strip()

    # 2. 最初の Markdown 見出しを探す
    for i, line in enumerate(lines):
        if line.lstrip().startswith("#"):
            return "\n".join(lines[i:]).strip()

    # 3. 見出しが無ければそのまま
    return text.strip()


async def main():
    api_key = os.getenv("DEEPSEEK_API_KEY")
    if not api_key:
        print("エラー: DEEPSEEK_API_KEY が設定されていません")
        return

    llm = OpenAICompatibleProvider(
        api_key=api_key,
        base_url="https://api.deepseek.com",
        model="deepseek-chat",
        temperature=0.3,
        max_tokens=4096
    )

    graph = json.loads(GRAPH_PATH.read_text(encoding="utf-8"))

    # エッジ数上位50件取得
    nodes = []
    for sid, node in graph["semantic_nodes"].items():
        nodes.append({
            "label": node["label"],
            "description": node.get("description", ""),
            "edges": len(node["edges"])
        })
    nodes.sort(key=lambda x: -x["edges"])
    top50 = nodes[:50]

    # LLMに渡す材料を作成
    entries = ""
    for n in top50:
        desc = n["description"][:200] if n["description"] else "(なし)"
        entries += f"- {n['label']} ({n['edges']}edges): {desc}\n"

    prompt = f"""以下は、AIメイド「柚月」の記憶ネットワークにおける重要概念トップ50です。
各概念にはラベル、エッジ数（関連エピソード数）、descriptionの冒頭が含まれています。

{entries}

これらを適切なカテゴリに分類し、各概念に1行（30〜60文字程度）の説明をつけてください。

【ルール】
- カテゴリは5〜8個程度。自然に分かれるように
- 説明は柚月の一人称視点で書くこと（「私の〜」「ご主人様が〜」等）
- 同じカテゴリ内の類似項目はまとめてよい（例: cosmic_harvestの作物は「火星スイカ・トゲレモン等の農作物を栽培中」のように1行に）
- 「ご主人様」は省略（別途USER.mdに詳細あり）
- 前置きや挨拶（「はい、〜」「以下に〜」等）は一切書かず、いきなり「## 記憶マップ」から始めること
- 出力はMarkdown形式で、以下の構造にすること：

## 記憶マップ

### カテゴリ名
- **ラベル**: 説明
- **ラベル**: 説明

### カテゴリ名
...
"""

    print("LLMに送信中...")
    messages = [
        {"role": "system", "content": "あなたは情報整理の専門家です。簡潔で正確な要約を作成してください。"},
        {"role": "user", "content": prompt}
    ]
    response = await llm.chat(messages)
    result = response.content.strip()

    # LLMが「はい、〜作成しました。」のような前置きを付けることがあるため、
    # 本文の見出し（## 記憶マップ、なければ最初の Markdown 見出し）以降だけを残す
    result = _strip_preamble(result)

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(result, encoding="utf-8")

    # トークン概算
    char_count = len(result)
    print(f"出力: {OUTPUT_PATH}")
    print(f"文字数: {char_count}")
    print("done")

if __name__ == "__main__":
    asyncio.run(main())
