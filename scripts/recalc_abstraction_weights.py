# scripts/recalc_abstraction_weights.py
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import numpy as np
from core.wyrd_network import load_graph, save_graph

def cosine_sim(a, b):
    a, b = np.asarray(a, dtype=np.float32), np.asarray(b, dtype=np.float32)
    norm_a, norm_b = np.linalg.norm(a), np.linalg.norm(b)
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return float(np.dot(a, b) / (norm_a * norm_b))

def main():
    graph = load_graph()
    embeddings = graph.get("_embeddings", {})
    
    updated = 0
    skipped = 0
    
    # episodic nodes のエッジを走査
    for nid, node in graph["episodic_nodes"].items():
        for edge in node.get("edges", []):
            if edge.get("type") != "abstraction":
                continue
            target = edge["to"]
            emb_src = embeddings.get(nid)
            emb_tgt = embeddings.get(target)
            if emb_src is None or emb_tgt is None:
                skipped += 1
                continue
            edge["weight"] = round(cosine_sim(emb_src, emb_tgt), 4)
            updated += 1
    
    # semantic nodes のエッジも走査（逆方向）
    for nid, node in graph["semantic_nodes"].items():
        for edge in node.get("edges", []):
            if edge.get("type") != "abstraction":
                continue
            target = edge["to"]
            emb_src = embeddings.get(nid)
            emb_tgt = embeddings.get(target)
            if emb_src is None or emb_tgt is None:
                skipped += 1
                continue
            edge["weight"] = round(cosine_sim(emb_src, emb_tgt), 4)
            updated += 1
    
    save_graph(graph)
    print(f"完了: {updated}件更新, {skipped}件スキップ(embedding無し)")
    
    # 統計表示
    weights = []
    for nid, node in graph["episodic_nodes"].items():
        for edge in node.get("edges", []):
            if edge.get("type") == "abstraction":
                weights.append(edge["weight"])
    
    if weights:
        w = np.array(weights)
        print(f"重み統計: 平均={w.mean():.4f}, 中央値={np.median(w):.4f}, 最小={w.min():.4f}, 最大={w.max():.4f}")

if __name__ == "__main__":
    main()
