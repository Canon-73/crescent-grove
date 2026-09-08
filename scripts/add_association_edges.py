# scripts/add_association_edges.py
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import numpy as np
from itertools import combinations
from core.wyrd_network import load_graph, save_graph, _cosine_similarity

def main():
    graph = load_graph()
    embeddings = graph.get("_embeddings", {})
    
    # エピソードごとに接続先セマンティックノードを収集
    # association edge = 同一エピソードに紐づくセマンティックノード同士
    
    pair_count = {}  # (sid1, sid2) → 共起回数
    
    for eid, node in graph["episodic_nodes"].items():
        # このエピソードに繋がるセマンティックノードを取得
        semantic_neighbors = []
        for edge in node.get("edges", []):
            if edge.get("type") == "abstraction" and edge["to"].startswith("s_"):
                semantic_neighbors.append(edge["to"])
        
        # 全ペアの共起をカウント
        for s1, s2 in combinations(set(semantic_neighbors), 2):
            pair = tuple(sorted([s1, s2]))
            pair_count[pair] = pair_count.get(pair, 0) + 1
    
    # association edge を作成
    added = 0
    for (sid1, sid2), count in pair_count.items():
        # 重み = コサイン類似度 × 共起強化
        emb1 = embeddings.get(sid1)
        emb2 = embeddings.get(sid2)
        
        if emb1 is not None and emb2 is not None:
            sim = _cosine_similarity(emb1, emb2)
            # 共起回数で強化: base_sim + log(count) * 0.05, 最大1.0
            weight = round(min(sim + np.log1p(count) * 0.05, 1.0), 4)
        else:
            weight = round(min(0.3 + np.log1p(count) * 0.05, 1.0), 4)
        
        # 双方向に追加（既存チェック）
        _add_if_not_exists(graph, sid1, sid2, "association", weight)
        _add_if_not_exists(graph, sid2, sid1, "association", weight)
        added += 1
    
    save_graph(graph)
    print(f"完了: {added}ペア ({added * 2}エッジ) 追加")
    print(f"共起回数統計: 最大={max(pair_count.values())}, 平均={np.mean(list(pair_count.values())):.1f}")
    
    # 重み統計
    weights = []
    for nid, node in graph["semantic_nodes"].items():
        for edge in node.get("edges", []):
            if edge.get("type") == "association":
                weights.append(edge["weight"])
    if weights:
        w = np.array(weights)
        print(f"重み統計: 平均={w.mean():.4f}, 中央値={np.median(w):.4f}, 最小={w.min():.4f}, 最大={w.max():.4f}")


def _add_if_not_exists(graph, src, tgt, edge_type, weight):
    node = graph["semantic_nodes"].get(src)
    if not node:
        return
    for edge in node.get("edges", []):
        if edge["to"] == tgt and edge.get("type") == edge_type:
            # 既存なら重みを更新
            edge["weight"] = weight
            return
    node.setdefault("edges", []).append({
        "to": tgt,
        "type": edge_type,
        "weight": weight
    })


if __name__ == "__main__":
    main()
