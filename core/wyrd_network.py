"""
Wyrd Network - Spreading Activation による長期記憶ネットワーク
"""

import json
import time
import os
import faiss
from typing import Optional
import math
from pathlib import Path
from datetime import datetime
import numpy as np
from itertools import combinations
from core.i18n import t
DATA_DIR = Path(os.path.dirname(__file__)).parent / "data"
GRAPH_PATH = str(DATA_DIR / "wyrd_network.json")

def _empty_graph() -> dict:
    return {
        "episodic_nodes": {},
        "semantic_nodes": {},
        "meta": {
            "next_episodic_id": 1,
            "next_semantic_id": 1,
            "created_at": time.time(),
            "last_updated": time.time()
        }
    }


def load_graph(path=GRAPH_PATH):
    p = Path(path)
    if p.exists():
        with open(p, "r", encoding="utf-8") as f:
            graph = json.load(f)
    else:
        graph = {"episodic_nodes": {}, "semantic_nodes": {}}

    # embedding読み込み
    emb_path = DATA_DIR / "wyrd_embeddings.npy"
    ids_path = DATA_DIR / "wyrd_embedding_ids.json"
    if emb_path.exists() and ids_path.exists():
        matrix = np.load(str(emb_path))
        with open(ids_path, "r", encoding="utf-8") as f:
            ids = json.load(f)
        graph["_embeddings"] = {nid: matrix[i] for i, nid in enumerate(ids)}
    else:
        graph["_embeddings"] = {}

    return graph


def save_graph(graph, path=GRAPH_PATH):
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)

    embeddings = graph.pop("_embeddings", {})

    save_data = {k: v for k, v in graph.items() if not k.startswith("_")}
    with open(p, "w", encoding="utf-8") as f:
        json.dump(save_data, f, ensure_ascii=False)

    if embeddings:
        ids = sorted(embeddings.keys())
        matrix = np.array([embeddings[i].tolist() if hasattr(embeddings[i], 'tolist') else embeddings[i] for i in ids], dtype=np.float32)
        np.save(str(DATA_DIR / "wyrd_embeddings.npy"), matrix)
        with open(DATA_DIR / "wyrd_embedding_ids.json", "w", encoding="utf-8") as f:
            json.dump(ids, f, ensure_ascii=False)

    graph["_embeddings"] = embeddings

def _find_or_create_semantic_node(
    graph: dict,
    concept: str,
    context_embedding: Optional[list] = None
) -> str:
    """
    概念名に一致するセマンティックノードを探す。
    見つからなければ新規作成する。

    マッチング順序:
    1. label の完全一致（大文字小文字無視）
    2. aliases の完全一致（大文字小文字無視）
    3. 見つからなければ新規作成
    """
    concept_lower = concept.strip().lower()

    for s_id, s_node in graph["semantic_nodes"].items():
        # label一致
        if s_node["label"].strip().lower() == concept_lower:
            return s_id
        # aliases一致
        for alias in s_node.get("aliases", []):
            if alias.strip().lower() == concept_lower:
                return s_id

    # 見つからなければ新規作成
    return add_semantic_node(graph, label=concept.strip())


def get_all_semantic_labels(graph: dict) -> list[dict]:
    """
    全セマンティックノードのlabelとdescriptionを返す。
    記憶マップのシステムプロンプト注入用。
    """
    result = []
    for s_id, s_node in graph["semantic_nodes"].items():
        result.append({
            "id": s_id,
            "label": s_node["label"],
            "description": s_node["description"]
        })
    return result


def get_node(graph: dict, node_id: str) -> Optional[dict]:
    """IDからノードを取得する。エピソード・セマンティック両方を探す。"""
    if node_id in graph["episodic_nodes"]:
        return graph["episodic_nodes"][node_id]
    if node_id in graph["semantic_nodes"]:
        return graph["semantic_nodes"][node_id]
    return None


def get_all_nodes(graph: dict) -> dict:
    """全ノードを統合した辞書を返す。"""
    all_nodes = {}
    all_nodes.update(graph["episodic_nodes"])
    all_nodes.update(graph["semantic_nodes"])
    return all_nodes


def add_edge(graph: dict, from_id: str, to_id: str, edge_type: str, weight: float = 0.5) -> bool:
    """
    任意の2ノード間にエッジを追加する。
    既に同じ to/type のエッジが存在する場合は weight を更新する。

    Returns: True if added, False if updated
    """
    from_node = get_node(graph, from_id)
    if from_node is None:
        return False

    # 既存エッジのチェック
    for edge in from_node["edges"]:
        if edge["to"] == to_id and edge["type"] == edge_type:
            edge["weight"] = weight
            return False

    from_node["edges"].append({
        "to": to_id,
        "type": edge_type,
        "weight": weight
    })
    return True


def node_count(graph: dict) -> dict:
    """ノード数を返す。"""
    return {
        "episodic": len(graph["episodic_nodes"]),
        "semantic": len(graph["semantic_nodes"]),
        "total": len(graph["episodic_nodes"]) + len(graph["semantic_nodes"])
    }

def process_fact_buffer(graph, buffer_path=None, embed_fn=None, agent_name="Assistant"):
    """fact_bufferを読み込み、グラフにエピソード・セマンティックノード・エッジを追加する

    agent_name: 同期版は説明文生成プロンプトに到達しないため未使用だが、
                非同期版とシグネチャを揃え呼び出し元から確実に渡せるよう受け取る。
    """

    if embed_fn is None:
        raise ValueError("embed_fn is required")

    # 未指定なら data_root 基準で解決する（書き込み側 agent._append_to_fact_buffer と一致させる）。
    if buffer_path is None:
        from core.paths import data_file
        buffer_path = str(data_file("fact_buffer.jsonl"))
    buffer = Path(buffer_path)
    if not buffer.exists() or buffer.stat().st_size == 0:
        return 0
    
    entries = []
    with open(buffer, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                entries.append(json.loads(line))
    
    if not entries:
        return 0
    
    prev_episode_id = get_latest_episode_id(graph)
    processed = 0
    
    for entry in entries:
        # entry: {"timestamp": "...", "content": "...", "concepts": [...], "source_turn_id": ...}
        
        # 重複防止: 同一source_turn_idが既に存在すればスキップ
        if entry.get("source_turn_id") and _episode_exists(graph, entry["source_turn_id"]):
            continue
        
        embedding = embed_fn(entry["content"])
        
        episode_id = add_episodic_node(
            graph,
            content=entry["content"],
            timestamp=entry["timestamp"],
            source_turn_id=entry.get("source_turn_id"),
            embedding=embedding,
            concepts=entry.get("concepts", []),
            embed_fn=embed_fn,
            valence=entry.get("valence", 0.0)
        )
        
        # temporal edge: weightなし、検索時にリアルタイム計算
        if prev_episode_id:
            add_edge(graph, prev_episode_id, episode_id, "temporal")
            add_edge(graph, episode_id, prev_episode_id, "temporal")
        
        prev_episode_id = episode_id
        processed += 1
    
    # 保存成功後にのみバッファクリア
    save_graph(graph)
    with open(buffer, "w", encoding="utf-8") as f:
        pass
    
    return processed


def _episode_exists(graph, source_turn_id):
    """同一source_turn_idのエピソードが既に存在するか"""
    for node in graph["episodic_nodes"].values():
        if node.get("source_turn_id") == source_turn_id:
            return True
    return False


def add_episodic_node(graph, content, timestamp, source_turn_id=None,
                      embedding=None, concepts=None, embed_fn=None, valence=0.0):
    eid = f"e_{len(graph['episodic_nodes']) + 1:04d}"
    graph["episodic_nodes"][eid] = {
        "content": content,
        "timestamp": timestamp,
        "valence": valence,  # 追加
        "source_turn_id": source_turn_id,
        "edges": [],
        "activation": 0.0,
        "last_activated": None,
        "access_count": 0
    }
    if embedding is not None:
        graph.setdefault("_embeddings", {})[eid] = embedding

    sids = []
    for concept in (concepts or []):
        sid = _find_or_create_semantic_node(graph, concept, context_embedding=embed_fn(concept) if embed_fn else None)
        sids.append(sid)
        # abstraction edge（コサイン類似度）
        emb_e = graph.get("_embeddings", {}).get(eid)
        emb_s = graph.get("_embeddings", {}).get(sid)
        if emb_e is not None and emb_s is not None:
            weight = round(_cosine_similarity(emb_e, emb_s), 4)
        else:
            weight = 0.5
        add_edge(graph, eid, sid, "abstraction", weight=weight)
        add_edge(graph, sid, eid, "abstraction", weight=weight)

    # association edge（概念間）
    for s1, s2 in combinations(sids, 2):
        emb1 = graph.get("_embeddings", {}).get(s1)
        emb2 = graph.get("_embeddings", {}).get(s2)
        if emb1 is not None and emb2 is not None:
            w = round(_cosine_similarity(emb1, emb2), 4)
        else:
            w = 0.3
        _add_or_strengthen_association(graph, s1, s2, w)

    return eid

def _add_or_strengthen_association(graph, sid1, sid2, base_weight):
    """既存なら重みを強化、なければ新規作成"""
    for direction in [(sid1, sid2), (sid2, sid1)]:
        src, tgt = direction
        node = graph["semantic_nodes"].get(src)
        if not node:
            continue
        found = False
        for edge in node.get("edges", []):
            if edge["to"] == tgt and edge.get("type") == "association":
                edge["weight"] = round(min(edge["weight"] + 0.1, 1.0), 4)
                found = True
                break
        if not found:
            node.setdefault("edges", []).append({
                "to": tgt,
                "type": "association",
                "weight": base_weight
            })

def add_semantic_node(graph, label, description="", embedding=None, aliases=None):
    """セマンティックノード作成"""
    
    sid = f"s_{len(graph['semantic_nodes']) + 1:04d}"
    
    graph["semantic_nodes"][sid] = {
        "label": label,
        "description": description,
        "aliases": aliases or [],
        "edges": [],
        "activation": 0.0,
        "last_activated": None,
        "access_count": 0
    }
    
    # embeddingは_embeddingsに格納
    if embedding is not None:
        if "_embeddings" not in graph:
            graph["_embeddings"] = {}
        graph["_embeddings"][sid] = embedding
    
    return sid


def get_latest_episode_id(graph):
    """最新のエピソードノードIDを返す"""
    if not graph["episodic_nodes"]:
        return None
    
    latest_id = None
    latest_ts = ""
    for eid, node in graph["episodic_nodes"].items():
        if node["timestamp"] > latest_ts:
            latest_ts = node["timestamp"]
            latest_id = eid
    
    return latest_id


def search_memory(query: str, graph: dict, embed_fn, config: dict = None, top_k: int = 10):
    if config is None:
        config = {}
    query_embedding = embed_fn(query)
    decay = config.get("decay", 0.5)
    spread = config.get("spread", 0.8)
    steps = config.get("steps", 3)
    temporal_weight_mode = config.get("temporal_weight_mode", "decay")
    temporal_rho = config.get("temporal_rho", 0.01)
    temporal_fixed_weight = config.get("temporal_fixed_weight", 0.9)

    # アンカー選択（メインクエリ）
    activations = _select_anchors(graph, query_embedding, query, config)

    # サブクエリ展開（2トークン以上の場合、各トークンでもアンカー検索して union）
    tokens = query.strip().split()
    if len(tokens) >= 2:
        for token in tokens:
            if len(token) >= 2:
                sub_emb = embed_fn(token)
                sub_anchors = _select_anchors(graph, sub_emb, token, config)
                for nid, score in sub_anchors.items():
                    if nid not in activations or score > activations[nid]:
                        activations[nid] = score

    if not activations:
        return {"episodes": [], "related_concepts": []}

    for _ in range(steps):
        activations = _propagate(graph, activations, decay, spread,
                                 temporal_weight_mode, temporal_rho, temporal_fixed_weight)
        activations = _inhibit(activations, config)

    activations = _sigmoid_activation(activations, config)

    results = _format_results(graph, activations, query_embedding, top_k)
    _update_access(graph, results["episodes"])
    return results


def _sigmoid_activation(activations, config):
    """シグモイド正規化: θを現在のエネルギー分布から動的決定"""
    if not activations:
        return activations
    gamma = config.get("sigmoid_gamma", 5.0)
    
    # θ = 非ゼロノードの中央値（動的）
    nonzero = [e for e in activations.values() if e > 0]
    if not nonzero:
        return activations
    theta = np.median(nonzero)
    
    return {
        nid: 1.0 / (1.0 + math.exp(-gamma * (energy - theta)))
        for nid, energy in activations.items()
    }


def _inhibit(activations, config):
    """Lateral Inhibition: top-Mノードが他を抑制"""
    if not activations:
        return activations
    beta = config.get("inhibit_beta", 0.1)
    top_m = config.get("inhibit_top_m", 7)
    
    sorted_nodes = sorted(activations.items(), key=lambda x: -x[1])
    top_set = sorted_nodes[:top_m]
    
    result = {}
    for nid, energy in activations.items():
        suppression = 0.0
        for top_nid, top_energy in top_set:
            if top_energy > energy:
                suppression += beta * (top_energy - energy)
        result[nid] = max(0.0, energy - suppression)
    
    # top-Mノード自身はそのまま保持
    for top_nid, top_energy in top_set:
        result[top_nid] = top_energy
    
    return result



def build_index(graph):
    """FAISSインデックスを構築（起動時に1回）"""
    embeddings = graph.get("_embeddings", {})
    ids = list(embeddings.keys())
    if not ids:
        return None
    matrix = np.array(
        [embeddings[nid].tolist() if hasattr(embeddings[nid], 'tolist') else embeddings[nid] for nid in ids],
        dtype=np.float32
    )
    faiss.normalize_L2(matrix)
    index = faiss.IndexFlatIP(384)
    index.add(matrix)
    graph["_faiss_index"] = index
    graph["_faiss_ids"] = ids
    return index


def _select_anchors(graph, query_embedding, query_text, config):
    """FAISSで高速にアンカーを選択"""
    top_k = config.get("anchor_top_k", 50)

    # FAISSインデックスがあれば使う
    index = graph.get("_faiss_index")
    ids = graph.get("_faiss_ids")

    if index is None:
        index = build_index(graph)
        ids = graph.get("_faiss_ids")
        if index is None:
            return {}

    query_vec = np.array([query_embedding], dtype=np.float32)
    faiss.normalize_L2(query_vec)
    scores, indices = index.search(query_vec, top_k)

    activations = {}
    for score, idx in zip(scores[0], indices[0]):
        if idx < 0:
            continue
        nid = ids[idx]
        activations[nid] = float(score)

    return activations

def _propagate(graph, activations, decay, spread,
               temporal_weight_mode, temporal_rho, temporal_fixed_weight):
    """エネルギー伝播（1ステップ）"""
    new_activations = {}
    
    # 既存エネルギーの減衰
    for nid, energy in activations.items():
        new_activations[nid] = (1 - decay) * energy
    
    # エッジに沿ってエネルギーを伝播
    for nid, energy in activations.items():
        node = (graph["episodic_nodes"].get(nid) or
                graph["semantic_nodes"].get(nid))
        
        if node is None:
            continue
        
        edges = node.get("edges", [])
        fan = len(edges)
        if fan == 0:
            continue
        
        for edge in edges:
            to_id = edge["to"]
            
            # temporal edgeはリアルタイム計算
            if edge["type"] == "temporal":
                weight = _calc_temporal_weight(
                    graph, nid, to_id,
                    temporal_weight_mode, temporal_rho, temporal_fixed_weight
                )
            else:
                weight = edge.get("weight", 0.5)
            
            flow = spread * weight * energy / fan
            new_activations[to_id] = new_activations.get(to_id, 0) + flow
    
    return new_activations


def _calc_temporal_weight(graph, from_id, to_id, mode, rho, fixed_weight):
    """temporal edgeの重みをリアルタイム計算"""
    if mode == "fixed":
        return fixed_weight
    
    from_node = (graph["episodic_nodes"].get(from_id) or
                 graph["semantic_nodes"].get(from_id))
    to_node = (graph["episodic_nodes"].get(to_id) or
               graph["semantic_nodes"].get(to_id))
    
    if not from_node or not to_node:
        return fixed_weight
    
    ts_from = from_node.get("timestamp", "")
    ts_to = to_node.get("timestamp", "")
    
    if not ts_from or not ts_to:
        return fixed_weight
    
    try:
        dt_from = datetime.fromisoformat(ts_from)
        dt_to = datetime.fromisoformat(ts_to)
        delta_hours = abs((dt_from - dt_to).total_seconds()) / 3600
        
        if mode == "decay":
            return math.exp(-rho * delta_hours)
        elif mode == "linear":
            max_hours = 720  # 30日
            return max(0.0, 1.0 - delta_hours / max_hours)
        else:
            return fixed_weight
    except (ValueError, TypeError):
        return fixed_weight


def _format_results(graph, activations, query_embedding, top_k):
    """Triple Hybrid Scoring でエピソードノードをランキング"""
    episode_activations = {nid: e for nid, e in activations.items() if nid.startswith('e_')}
    if not episode_activations:
        return []
    
    # config から重みを取得（wyrd_config.json の search 内）
    # wyrd_config.json は config/ へ隔離（data_root 基準で解決）。DATA_DIR は bundle 基準で、
    # packaged では data_root に展開された設定を見ないため使わない。他の DATA_DIR 用途
    # （wyrd_network.json 等）はこの段階では変更しない。
    from core.paths import config_file
    config_path = config_file("wyrd_config.json")
    hybrid_config = {}
    if config_path.exists():
        with open(config_path, "r", encoding="utf-8") as f:
            hybrid_config = json.load(f).get("search", {})
    
    lambda1 = hybrid_config.get("lambda_similarity", 0.0)
    lambda2 = hybrid_config.get("lambda_activation", 1.0)
    lambda3 = hybrid_config.get("lambda_structure", 0.0)
    
    embeddings = graph.get("_embeddings", {})
    
    # 構造スコア（エッジ数）の最大値を事前計算
    max_edges = 1
    if lambda3 > 0:
        max_edges = max(
            len(graph["episodic_nodes"][nid].get("edges", []))
            for nid in episode_activations
        ) or 1
    
    scored = []
    for nid, energy in episode_activations.items():
        # λ1: コサイン類似度
        if lambda1 > 0 and query_embedding is not None:
            emb = embeddings.get(nid)
            sim = _cosine_similarity(emb, query_embedding) if emb is not None else 0.0
        else:
            sim = 0.0
        
        # λ2: 活性化エネルギー（そのまま）
        act = energy
        
        # λ3: 構造スコア（正規化エッジ数）
        if lambda3 > 0:
            node = graph["episodic_nodes"].get(nid, {})
            struct = len(node.get("edges", [])) / max_edges
        else:
            struct = 0.0
        
        score = lambda1 * sim + lambda2 * act + lambda3 * struct
        scored.append((nid, score, energy))
    
    scored.sort(key=lambda x: -x[1])

    episodes = []
    for nid, score, energy in scored[:top_k]:
        node = graph["episodic_nodes"].get(nid)
        if not node:
            continue
        episodes.append({
            "id": nid,
            "energy": round(score, 4),
            "type": "episodic",
            "content": node["content"],
            "timestamp": node.get("timestamp", ""),
            "valence": node.get("valence", 0.0)
        })

    # 関連セマンティック top5
    semantic_activations = {nid: e for nid, e in activations.items() if nid.startswith('s_')}
    sorted_semantics = sorted(semantic_activations.items(), key=lambda x: -x[1])[:5]
    related_concepts = []
    for nid, energy in sorted_semantics:
        node = graph["semantic_nodes"].get(nid)
        if node:
            related_concepts.append(node.get("label", ""))

    return {
        "episodes": episodes,
        "related_concepts": related_concepts
    }

  
def _update_access(graph, results):
    """検索でヒットしたノードのアクセス情報を更新"""
    now = datetime.now().isoformat()
    
    for r in results:
        nid = r["id"]
        node = (graph["episodic_nodes"].get(nid) or
                graph["semantic_nodes"].get(nid))
        
        if node:
            node["activation"] = r["energy"]
            node["last_activated"] = now
            node["access_count"] = node.get("access_count", 0) + 1


def _cosine_similarity(vec_a, vec_b):
    """コサイン類似度"""
    a = np.asarray(vec_a, dtype=np.float32)
    b = np.asarray(vec_b, dtype=np.float32)
    
    dot = np.dot(a, b)
    norm_a = np.linalg.norm(a)
    norm_b = np.linalg.norm(b)
    
    if norm_a == 0 or norm_b == 0:
        return 0.0
    
    return float(dot / (norm_a * norm_b))


def _keyword_match_score(query, text):
    """部分文字列マッチによるキーワードスコア"""
    if not query or not text:
        return 0.0
    
    text_lower = text.lower()
    query_lower = query.lower()
    
    # 空白で分割できる部分はそのまま使い、残りは2文字gramに分解
    terms = query_lower.split()
    if not terms or (len(terms) == 1 and len(terms[0]) > 3):
        # 日本語等スペース無しの場合、2文字gramに分解
        terms = [query_lower[i:i+2] for i in range(len(query_lower) - 1)]
    
    if not terms:
        return 0.0
    
    matches = sum(1 for term in terms if term in text_lower)
    return matches / len(terms)


# core/wyrd_network.py に追加する関数
async def _generate_node_description(graph, sid, llm_fn, agent_name="Assistant"):
    """セマンティックノードのdescriptionをLLMで生成"""
    node = graph["semantic_nodes"].get(sid)
    if not node:
        return ""
    
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
            episodes.append(f"[{ts}] {content}")
    
    if not episodes:
        return ""
    
    prompt = t("wyrd_concept_prompt", agent_name=agent_name, label=label) + "\n".join(episodes)
    
    try:
        result = await llm_fn(prompt)
        return result.strip()[:1000] if result else ""
    except Exception:
        return ""


async def process_fact_buffer_async(graph, buffer_path=None, embed_fn=None, llm_fn=None, agent_name="Assistant"):
    """fact_bufferを読み込み、グラフにノード追加 + 新規セマンティックのdescription生成"""

    if embed_fn is None:
        raise ValueError("embed_fn is required")

    # 未指定なら data_root 基準で解決する（書き込み側 agent._append_to_fact_buffer と一致させる）。
    if buffer_path is None:
        from core.paths import data_file
        buffer_path = str(data_file("fact_buffer.jsonl"))
    buffer = Path(buffer_path)
    if not buffer.exists() or buffer.stat().st_size == 0:
        return 0
    
    entries = []
    with open(buffer, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                entries.append(json.loads(line))
    
    if not entries:
        return 0
    
    # description未設定のノードを記録（追加前の状態）
    existing_no_desc = {sid for sid, node in graph["semantic_nodes"].items() 
                        if not node.get("description")}
    
    prev_episode_id = get_latest_episode_id(graph)
    processed = 0
    
    for entry in entries:
        if entry.get("source_turn_id") and _episode_exists(graph, entry["source_turn_id"]):
            continue
        
        embedding = embed_fn(entry["content"])
        
        episode_id = add_episodic_node(
            graph,
            content=entry["content"],
            timestamp=entry["timestamp"],
            source_turn_id=entry.get("source_turn_id"),
            embedding=embedding,
            concepts=entry.get("concepts", []),
            embed_fn=embed_fn,
            valence=entry.get("valence", 0.0)
        )
        
        if prev_episode_id:
            add_edge(graph, prev_episode_id, episode_id, "temporal")
            add_edge(graph, episode_id, prev_episode_id, "temporal")
        
        prev_episode_id = episode_id
        processed += 1
    
    # 新規セマンティックノードのdescription生成
    if llm_fn:
        new_no_desc = {sid for sid, node in graph["semantic_nodes"].items() 
                       if not node.get("description")} - existing_no_desc
        for sid in new_no_desc:
            desc = await _generate_node_description(graph, sid, llm_fn, agent_name=agent_name)
            if desc:
                graph["semantic_nodes"][sid]["description"] = desc
    
    save_graph(graph)
    # 記憶マップの週次更新
    if llm_fn:
        updated = await update_knowledge_map(graph, llm_fn, agent_name=agent_name)
        if updated:
            print("記憶マップを更新しました")
    with open(buffer, "w", encoding="utf-8") as f:
        pass
    
    return processed


def search_concept(query, graph, embed_fn, threshold=0.85):
    """セマンティックノードを検索し、descriptionを返す辞書機能"""
    query_lower = query.strip().lower()

    # 1. 完全一致
    for sid, node in graph["semantic_nodes"].items():
        if node.get("label", "").lower() == query_lower:
            return {
                "match": "exact",
                "label": node.get("label", ""),
                "description": node.get("description", ""),
                "edge_count": len(node.get("edges", [])),
                "aliases": node.get("aliases", [])
            }
        if query_lower in [a.lower() for a in node.get("aliases", [])]:
            return {
                "match": "alias",
                "label": node.get("label", ""),
                "description": node.get("description", ""),
                "edge_count": len(node.get("edges", [])),
                "aliases": node.get("aliases", [])
            }

    # 2. embedding類似度で候補提示
    query_emb = embed_fn(query)
    similarities = []
    for sid, node in graph["semantic_nodes"].items():
        emb = graph.get("_embeddings", {}).get(sid)
        if emb is not None:
            sim = _cosine_similarity(query_emb, emb)
            similarities.append((node.get("label", ""), sim))

    similarities.sort(key=lambda x: -x[1])
    suggestions = [label for label, sim in similarities[:5]]

    return {
        "match": "none",
        "suggestions": suggestions
    }

async def update_knowledge_map(graph, llm_fn, output_path="workspace/memory/knowledge_map.md", min_days=7, agent_name="Assistant"):
    """記憶マップを週次で再生成する"""
    import time
    output = Path(output_path)
    if output.exists():
        age_days = (time.time() - output.stat().st_mtime) / 86400
        if age_days < min_days:
            return False

    nodes = []
    for sid, node in graph["semantic_nodes"].items():
        nodes.append({
            "label": node["label"],
            "description": node.get("description", ""),
            "edges": len(node["edges"])
        })
    nodes.sort(key=lambda x: -x["edges"])
    top50 = nodes[:50]

    entries = ""
    for n in top50:
        desc = n["description"][:200] if n["description"] else "(なし)"
        entries += f"- {n['label']} ({n['edges']}edges): {desc}\n"

    prompt_text = t("wyrd_memory_map_prompt", agent_name=agent_name, entries=entries)

    result = await llm_fn(prompt_text)
    result = result.strip()

    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(result, encoding="utf-8")
    return True
