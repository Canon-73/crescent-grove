# scripts/backfill_wyrd.py
"""
過去の会話ログからWyrd Networkを一括構築するスクリプト
使用法: python scripts/backfill_wyrd.py
"""

import json
import re
import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv
load_dotenv()

from core.wyrd_network import load_graph, save_graph, add_episodic_node, add_edge
from core.llm import OpenAICompatibleProvider

CHAT_LOG_DIR = Path("workspace/logs/chat")
GRAPH_PATH = Path("data/wyrd_network.json")


def parse_chat_file(filepath: Path) -> list[dict]:
    """1つのchatログファイルをターン単位に分割"""
    content = filepath.read_text(encoding="utf-8")
    turns = []

    blocks = content.split("\n---\n")

    for block in blocks:
        block = block.strip()
        if not block:
            continue

        ts_match = re.search(r'\*\*\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\]\*\*', block)
        if not ts_match:
            continue

        timestamp = ts_match.group(1)
        turns.append({
            "timestamp": timestamp,
            "text": block
        })

    return turns


EXTRACTION_PROMPT = """以下の1ターン分の会話ログから、保存する価値のある事実を抽出してください。

抽出対象:
- 柚月の行動・結果（何をした、何を作った、何が起きた）
- 気づき・学び・考えの変化
- 他者に関する新しい情報
- プロジェクトの進捗
- ユーザーに関する事実・好み

抽出除外:
- 挨拶・相槌・感情表現のみの発言
- 行動を伴わない検討段階の独り言

出力形式:
<facts>
E: エピソード要約（1行）
C: 概念1, 概念2, 概念3
</facts>

複数の事実がある場合はE:とC:のペアを繰り返してください。
保存すべき事実がない場合は <facts>なし</facts> と出力してください。"""


async def extract_facts_from_turn(llm, turn: dict) -> list[dict]:
    """1ターンからLLMで事実抽出"""
    messages = [
        {"role": "system", "content": EXTRACTION_PROMPT},
        {"role": "user", "content": turn["text"]}
    ]

    try:
        response = await llm.chat(messages, tools=None)
        raw = (response.content or "").strip()
    except Exception as e:
        print(f"  LLM呼び出し失敗: {e}")
        return []

    facts_match = re.search(r'<facts>(.*?)</facts>', raw, re.DOTALL)
    if not facts_match:
        return []

    facts_content = facts_match.group(1).strip()
    if facts_content == "なし":
        return []

    results = []
    lines = facts_content.split("\n")
    current_episode = None

    for line in lines:
        line = line.strip()
        if line.startswith("E:"):
            current_episode = line[2:].strip()
        elif line.startswith("C:") and current_episode:
            concepts = [c.strip() for c in line[2:].split(",") if c.strip()]
            results.append({
                "timestamp": turn["timestamp"].replace(" ", "T"),
                "content": current_episode,
                "concepts": concepts
            })
            current_episode = None

    return results


async def main():
    from core.llm import LLMProvider

    api_key = os.getenv("CG_LLM_DEEPSEEK_API_KEY")
    if not api_key:
        print("エラー: CG_LLM_DEEPSEEK_API_KEY が .env に設定されていません")
        return

    llm = OpenAICompatibleProvider(
        api_key=api_key,
        base_url="https://api.deepseek.com",
        model="deepseek-chat",
        temperature=0.3,
        max_tokens=1024
    )

    # 全chatログファイルを取得
    chat_files = sorted(CHAT_LOG_DIR.glob("*.md"))
    print(f"対象ファイル: {len(chat_files)}件")

    # 事実抽出
    all_facts = []
    total_turns = 0

    for i, filepath in enumerate(chat_files):
        print(f"\n[{i+1}/{len(chat_files)}] {filepath.name}")
        turns = parse_chat_file(filepath)
        print(f"  ターン数: {len(turns)}")

        for j, turn in enumerate(turns):
            facts = await extract_facts_from_turn(llm, turn)
            if facts:
                all_facts.extend(facts)
            total_turns += 1

            # 100ターンごとに進捗表示
            if total_turns % 100 == 0:
                print(f"  進捗: {total_turns}ターン処理済み, {len(all_facts)}件抽出")

    print(f"\n抽出完了: {total_turns}ターンから{len(all_facts)}件の事実")

    # グラフ構築
    print("\nグラフ構築開始...")

    from sentence_transformers import SentenceTransformer
    model = SentenceTransformer('intfloat/multilingual-e5-small')

    def embed_fn(text):
        return model.encode(text, normalize_embeddings=True).tolist()

    graph = load_graph(str(GRAPH_PATH))

    prev_episode_id = None
    for fact in all_facts:
        embedding = embed_fn(fact["content"])
        episode_id = add_episodic_node(
            graph,
            content=fact["content"],
            timestamp=fact["timestamp"],
            embedding=embedding,
            concepts=fact.get("concepts", []),
            embed_fn=embed_fn
        )

        if prev_episode_id:
            add_edge(graph, prev_episode_id, episode_id, "temporal")
            add_edge(graph, episode_id, prev_episode_id, "temporal")

        prev_episode_id = episode_id

    save_graph(graph, str(GRAPH_PATH))
    print(f"\nグラフ保存完了:")
    print(f"  エピソードノード: {len(graph['episodic_nodes'])}件")
    print(f"  セマンティックノード: {len(graph['semantic_nodes'])}件")


if __name__ == "__main__":
    asyncio.run(main())
