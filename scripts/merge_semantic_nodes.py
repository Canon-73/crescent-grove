# scripts/merge_semantic_nodes.py
import sys
import json
import asyncio
import os
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv
load_dotenv()

from core.llm import OpenAICompatibleProvider

PROMPT = """以下の2つの概念ラベルが「同じ概念」として統合すべきかを判定してください。

【判定基準】
- 一方が他方の具体例や一時的な状態に過ぎない場合 → 統合（短い方をマスターに）
- 明確に異なる意味を持つ場合 → 別概念
- 迷う場合 → 別概念

A: {label_a}
B: {label_b}

回答は「統合」または「別概念」の一言のみ。"""


async def main():
    api_key = os.getenv("CG_LLM_DEEPSEEK_API_KEY")
    if not api_key:
        print("エラー: CG_LLM_DEEPSEEK_API_KEY が設定されていません")
        return

    llm = OpenAICompatibleProvider(
        api_key=api_key,
        base_url="https://api.deepseek.com",
        model="deepseek-chat",
        temperature=0.0,
        max_tokens=10
    )

    # グラフ読み込み
    graph_path = Path("data/wyrd_network.json")
    with open(graph_path, "r", encoding="utf-8") as f:
        graph = json.load(f)

    # 部分一致ペアを収集
    labels = {}
    for sid, node in graph["semantic_nodes"].items():
        labels[sid] = node.get("label", "")

    label_to_sid = {}
    for sid, label in labels.items():
        label_to_sid.setdefault(label.lower(), []).append(sid)

    pairs = []
    sids = list(labels.keys())
    for i, sid_a in enumerate(sids):
        a = labels[sid_a]
        for sid_b in sids[i+1:]:
            b = labels[sid_b]
            if len(a) > 3 and len(b) > 3:
                if a in b or b in a:
                    # 短い方をマスター候補に
                    if len(a) <= len(b):
                        pairs.append((sid_a, sid_b, a, b))
                    else:
                        pairs.append((sid_b, sid_a, b, a))

    print(f"部分一致ペア: {len(pairs)}件")

    # LLM で判定
    merge_targets = []  # (master_sid, merge_sid)
    progress_path = Path("data/merge_progress.json")
    if progress_path.exists():
        with open(progress_path, "r", encoding="utf-8") as f:
            done_data = json.load(f)
            done_keys = set(done_data.get("done", []))
            merge_targets = done_data.get("merges", [])
        print(f"既に判定済み: {len(done_keys)}件, 統合対象: {len(merge_targets)}件")
    else:
        done_keys = set()

    remaining = [(a, b, la, lb) for a, b, la, lb in pairs
                 if f"{a}_{b}" not in done_keys]
    print(f"残り判定: {len(remaining)}件")

    batch_size = 30
    save_every = 300

    for i in range(0, len(remaining), batch_size):
        batch = remaining[i:i + batch_size]
        tasks = [_judge(llm, la, lb) for _, _, la, lb in batch]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        for (master, merge, la, lb), result in zip(batch, results):
            key = f"{master}_{merge}"
            done_keys.add(key)
            if isinstance(result, Exception):
                continue
            if "統合" in result:
                merge_targets.append((master, merge))

        if (i // batch_size) % (save_every // batch_size) == 0:
            with open(progress_path, "w", encoding="utf-8") as f:
                json.dump({"done": list(done_keys), "merges": merge_targets}, f, ensure_ascii=False)
            print(f"  判定進捗: {min(i + batch_size, len(remaining))}/{len(remaining)}, 統合候補: {len(merge_targets)}件")

    # 最終保存
    with open(progress_path, "w", encoding="utf-8") as f:
        json.dump({"done": list(done_keys), "merges": merge_targets}, f, ensure_ascii=False)

    print(f"\n判定完了: {len(merge_targets)}件を統合")

    # 統合実行
    merged = 0
    for master_sid, merge_sid in merge_targets:
        if merge_sid not in graph["semantic_nodes"]:
            continue
        if master_sid not in graph["semantic_nodes"]:
            continue

        master_node = graph["semantic_nodes"][master_sid]
        merge_node = graph["semantic_nodes"][merge_sid]

        # マスターの aliases に統合先ラベルを追加
        merge_label = merge_node.get("label", "")
        if merge_label and merge_label not in master_node.get("aliases", []):
            master_node.setdefault("aliases", []).append(merge_label)

        # 統合先のエッジをマスターに付け替え
        for edge in merge_node.get("edges", []):
            target = edge["to"]
            edge_type = edge.get("type", "")
            weight = edge.get("weight", 0.8)

            # マスター → target のエッジがなければ追加
            exists = any(
                e["to"] == target and e.get("type") == edge_type
                for e in master_node.get("edges", [])
            )
            if not exists and target != master_sid:
                master_node.setdefault("edges", []).append(edge)

            # target 側の merge_sid → master_sid に付け替え
            target_node = (graph["episodic_nodes"].get(target)
                          or graph["semantic_nodes"].get(target))
            if target_node:
                for te in target_node.get("edges", []):
                    if te["to"] == merge_sid:
                        te["to"] = master_sid

        # 統合先ノードを削除
        del graph["semantic_nodes"][merge_sid]
        merged += 1

    # 保存
    with open(graph_path, "w", encoding="utf-8") as f:
        json.dump(graph, f, ensure_ascii=False)

    print(f"統合完了: {merged}ノード削除")
    print(f"残りセマンティックノード: {len(graph['semantic_nodes'])}件")


async def _judge(llm, label_a, label_b):
    prompt = PROMPT.format(label_a=label_a, label_b=label_b)
    messages = [{"role": "user", "content": prompt}]
    response = await llm.chat(messages)
    return response.content.strip()


if __name__ == "__main__":
    asyncio.run(main())
