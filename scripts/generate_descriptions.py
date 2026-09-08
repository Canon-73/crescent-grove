# scripts/generate_descriptions.py
"""
セマンティックノードにdescriptionを一括生成するスクリプト
使用法: python scripts/generate_descriptions.py
"""

import json
import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv
load_dotenv()

from core.llm import OpenAICompatibleProvider

GRAPH_PATH = Path("data/wyrd_network.json")
PROGRESS_PATH = Path("data/description_progress.json")

DESCRIPTION_PROMPT = """以下はAIメイド「柚月」の記憶ネットワークにおける概念ノードです。
この概念に接続されているエピソードの一部を示します。
これらの情報から、この概念についての柚月の記憶を一人称で生成してください。

【ルール】
- 柚月の一人称（私）で書く
- 以下の要素を含める：
  1. この概念は何か・誰か（人物なら関係性・役割・特徴）
  2. 私にとっての意味（なぜ重要か）
  3. 主な出来事やエピソード（時系列の羅列ではなく、印象的なものを厳選）
  4. 現在の状態や方向性
- 人物の場合は「どういう人か」を最初に書くこと
- 時系列の羅列ではなく、全体像が伝わる文章にすること
- 一般的な辞書定義ではなく、柚月の経験に基づいた説明にする
- 情報量に応じて長さは自由
- 同じ情報の繰り返しはしない
- 説明文のみ出力。前置き不要

概念: {label}
関連エピソード:
{episodes}"""


def load_progress() -> set:
    if PROGRESS_PATH.exists():
        data = json.loads(PROGRESS_PATH.read_text(encoding="utf-8"))
        return set(data.get("done", []))
    return set()


def save_progress(done: set):
    PROGRESS_PATH.write_text(
        json.dumps({"done": list(done)}, ensure_ascii=False),
        encoding="utf-8"
    )


async def generate_description(llm, label: str, episodes: list[str]) -> str:
    ep_text = "\n".join(episodes)
    prompt = DESCRIPTION_PROMPT.format(label=label, episodes=ep_text)
    messages = [{"role": "user", "content": prompt}]

    try:
        response = await llm.chat(messages, tools=None)
        result = (response.content or "").strip()
        # 長すぎる場合は切る（安全策）
        if len(result) > 1000:
            result = result[:1000]
        return result
    except Exception as e:
        print(f"  LLM呼び出し失敗 ({label}): {e}")
        return ""


async def main():
    api_key = os.getenv("CG_LLM_DEEPSEEK_API_KEY")
    if not api_key:
        print("エラー: CG_LLM_DEEPSEEK_API_KEY が設定されていません")
        return

    llm = OpenAICompatibleProvider(
        api_key=api_key,
        base_url="https://api.deepseek.com",
        model="deepseek-chat",
        temperature=1.0,
        max_tokens=1024
    )

    # グラフ読み込み
    print("グラフ読み込み中...")
    graph = json.loads(GRAPH_PATH.read_text(encoding="utf-8"))
    print(f"セマンティックノード: {len(graph['semantic_nodes'])}件")

    # 進捗読み込み
    done = load_progress()
    print(f"処理済み: {len(done)}件")

    # 対象ノードをエッジ数順にソート（多い方から）
    targets = []
    for sid, node in graph["semantic_nodes"].items():
        if sid in done:
            continue
        targets.append((sid, node, len(node.get("edges", []))))
    targets.sort(key=lambda x: -x[2])
    targets = targets[:200]  # ← この1行を追加
    print(f"残り: {len(targets)}件")

    # バッチ処理
    batch_size = 20
    save_every = 100
    processed = 0

    for batch_start in range(0, len(targets), batch_size):
        batch = targets[batch_start:batch_start + batch_size]

        tasks = []
        for sid, node, edge_count in batch:
            label = node.get("label", "")

            # 接続エピソードを取得（weight順上位15件）
            ep_edges = [
                (e["to"], e.get("weight", 0))
                for e in node.get("edges", [])
                if e["to"].startswith("e_") and e.get("type") == "abstraction"
            ]
            ep_edges.sort(key=lambda x: -x[1])

            episodes = []
            for eid, w in ep_edges[:15]:
                ep_node = graph["episodic_nodes"].get(eid)
                if ep_node:
                    ts = ep_node.get("timestamp", "")[:10]
                    content = ep_node["content"][:150]
                    v = ep_node.get("valence", 0)
                    episodes.append(f"[{ts}] (v={v:+.2f}) {content}")

            if not episodes:
                # エピソード接続なし → スキップ
                done.add(sid)
                continue

            tasks.append((sid, generate_description(llm, label, episodes)))

        if not tasks:
            continue

        # 並列実行
        results = await asyncio.gather(*[t[1] for t in tasks])

        for (sid, _), description in zip(tasks, results):
            if description:
                graph["semantic_nodes"][sid]["description"] = description
            done.add(sid)
            processed += 1

        print(f"  {processed}/{len(targets)} 完了 (最新: {batch[0][1].get('label','')})")

        # 定期保存
        if processed % save_every == 0:
            GRAPH_PATH.write_text(json.dumps(graph, ensure_ascii=False), encoding="utf-8")
            save_progress(done)
            print(f"  中間保存完了")

    # 最終保存
    GRAPH_PATH.write_text(json.dumps(graph, ensure_ascii=False), encoding="utf-8")
    save_progress(done)

    # 統計
    has_desc = sum(1 for n in graph["semantic_nodes"].values() if n.get("description"))
    no_desc = sum(1 for n in graph["semantic_nodes"].values() if not n.get("description"))
    print(f"\n=== 完了 ===")
    print(f"  description あり: {has_desc}件")
    print(f"  description なし: {no_desc}件")

    # サンプル表示
    print("\n--- サンプル ---")
    samples = sorted(graph["semantic_nodes"].items(), key=lambda x: -len(x[1].get("edges", [])))[:5]
    for sid, node in samples:
        print(f"\n[{node.get('label','')}] (エッジ{len(node.get('edges',[]))}本)")
        print(f"  {node.get('description','(なし)')[:200]}")


if __name__ == "__main__":
    asyncio.run(main())
