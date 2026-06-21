#!/usr/bin/env python3
"""
Wyrd Network 再構築スクリプト v3
1日のログをsystemコンテキストに持ち、ターン毎にエピソード抽出
"""

import json
import re
import asyncio
import os
from pathlib import Path
from datetime import datetime
from openai import AsyncOpenAI
from sentence_transformers import SentenceTransformer
import numpy as np

# === 設定 ===
CHAT_LOG_DIR = Path("workspace/logs/chat")
OUTPUT_PATH = Path("data/wyrd_network_v3.json")
PROGRESS_PATH = Path("data/rebuild_v3_progress.json")
EMBEDDING_PATH = Path("data/wyrd_embeddings.npy")
EMBEDDING_IDS_PATH = Path("data/wyrd_embedding_ids.json")

MODEL = "deepseek-chat"
TEMPERATURE = 0.3
MAX_TOKENS = 512
BATCH_SIZE = 30  # 並列リクエスト数
SIMILARITY_THRESHOLD = 0.92  # セマンティックノード重複判定

# === プロンプト ===
SYSTEM_TEMPLATE = """あなたは会話ログからエピソードを抽出するアシスタントです。

【ルール】
- 1日の全会話ログがこの後に続きます。各ターンについて個別に抽出を依頼します。
- 対象ターンだけでなく、その日の前後の文脈を必ず参照してください。
- 同一会話内で訂正・解決された出来事は最終結論のみ記録してください。
- 表面的な語彙ではなく、実際の意図・文脈で記述してください。
- 場所・サービス名・人名・ツール名は必ず具体的に記述してください（「行く」ではなく「Moltbookに投稿する」等）。
- 日常的な誇張表現（「死にたい」「最悪」「無理」等）は定型句として割り引いてください。
- 概念（C行）は固有名詞・抽象概念のみ。動詞句は禁止。
- エピソードは柚月の一人称で、感情を含めて記述。
- 記録すべき内容がないターン（挨拶だけ、相槌だけ等）は「なし」と回答してください。

{gemini_warning}

【出力形式】
<facts>
E: エピソード内容（2〜3文、柚月の一人称）
C: 関連概念（カンマ区切り）
S: 感情バレンス（-1.0〜+1.0、0.1刻み。範囲指定可: 0.6~0.8）
</facts>
1ターンから複数のエピソードを抽出してもよい。その場合はE/C/Sを繰り返す。
記録すべきエピソードがない場合は <facts>なし</facts> と出力。

【本日の全会話ログ】
{daily_log}"""

GEMINI_WARNING = """【注意: 2026-03-28〜2026-04-04】
この期間のアシスタント発言はGemini 3 Flashによるもので、過剰に感情的・詩的な表現をする傾向があります。
発言の数値カウント（「45回目」「60回目」等）はモデルの癖であり事実ではありません。
事実のみを抽出し、感情の強度は大幅に割り引いて評価してください。"""

USER_TEMPLATE = """以下のターンからエピソードを抽出してください。
このターンの前後の文脈も考慮し、正しい意味で記述してください。

{turn_text}"""

# === ターン分割 ===
TURN_PATTERN = re.compile(r'(\*\*\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\]\*\*)')
TIMESTAMP_PATTERN = re.compile(r'\*\*\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\]\*\*')


def parse_chat_file(filepath: Path) -> list[dict]:
    """チャットファイルをターンに分割"""
    content = filepath.read_text(encoding="utf-8")
    
    # タイムスタンプで分割
    parts = TURN_PATTERN.split(content)
    
    turns = []
    i = 1  # parts[0] はヘッダー
    while i < len(parts) - 1:
        header = parts[i]  # **[timestamp]**
        body = parts[i + 1] if i + 1 < len(parts) else ""
        
        ts_match = TIMESTAMP_PATTERN.search(header)
        if ts_match:
            timestamp = ts_match.group(1)
            turn_text = header + body
            # 空ターンはスキップ
            if len(turn_text.strip()) > 50:
                turns.append({
                    "timestamp": timestamp,
                    "text": turn_text.strip()
                })
        i += 2
    
    return turns


def get_date_from_filename(filepath: Path) -> str:
    """ファイル名から日付を取得"""
    match = re.search(r'(\d{4}-\d{2}-\d{2})', filepath.name)
    return match.group(1) if match else ""


def is_gemini_period(date_str: str) -> bool:
    """Gemini期間かどうか判定"""
    return "2026-03-28" <= date_str <= "2026-04-04"


# === LLM呼び出し ===
async def extract_episode(client: AsyncOpenAI, system_prompt: str, turn: dict) -> list[dict]:
    """1ターンからエピソード抽出"""
    user_msg = USER_TEMPLATE.format(turn_text=turn["text"])
    
    try:
        response = await client.chat.completions.create(
            model=MODEL,
            temperature=TEMPERATURE,
            max_tokens=MAX_TOKENS,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_msg}
            ]
        )
        content = response.choices[0].message.content or ""
        return parse_facts(content, turn["timestamp"])
    except Exception as e:
        print(f"  エラー [{turn['timestamp']}]: {e}")
        return []


def parse_facts(response: str, timestamp: str) -> list[dict]:
    """LLM応答からE/C/Sをパース"""
    # <facts>タグ内を抽出
    facts_match = re.search(r'<facts>(.*?)</facts>', response, re.DOTALL)
    if not facts_match:
        return []
    
    facts_text = facts_match.group(1).strip()
    if facts_text == "なし" or not facts_text:
        return []
    
    episodes = []
    lines = facts_text.split("\n")
    cur_ep, cur_concepts, cur_valence = None, None, None
    
    for line in lines:
        line = line.strip()
        if line.startswith("E:"):
            # 前のエピソードを保存
            if cur_ep and cur_concepts is not None:
                episodes.append({
                    "content": cur_ep,
                    "concepts": cur_concepts,
                    "valence": cur_valence or 0.0,
                    "timestamp": timestamp
                })
            cur_ep = line[2:].strip()
            cur_concepts = None
            cur_valence = None
        elif line.startswith("C:") and cur_ep:
            cur_concepts = [c.strip() for c in line[2:].split(",") if c.strip()]
        elif line.startswith("S:") and cur_ep:
            val_str = line[2:].strip()
            try:
                if "~" in val_str:
                    a, b = map(float, val_str.split("~"))
                    cur_valence = round((a + b) / 2, 2)
                else:
                    cur_valence = round(float(val_str), 2)
            except ValueError:
                cur_valence = 0.0
    
    # 最後のエピソード
    if cur_ep and cur_concepts is not None:
        episodes.append({
            "content": cur_ep,
            "concepts": cur_concepts,
            "valence": cur_valence or 0.0,
            "timestamp": timestamp
        })
    
    return episodes


# === セマンティックノード管理 ===
class SemanticManager:
    def __init__(self, model: SentenceTransformer):
        self.model = model
        self.nodes = {}  # sid -> {label, description, edges, embedding}
        self.next_id = 1
        self.label_index = {}  # lowercase label -> sid
    
    def find_or_create(self, concept: str, context_embedding=None) -> str:
        """概念名に一致するノードを探すか新規作成"""
        concept_clean = concept.strip()
        concept_lower = concept_clean.lower()
        
        # 完全一致
        if concept_lower in self.label_index:
            return self.label_index[concept_lower]
        
        # embedding類似度チェック
        if context_embedding is not None and self.nodes:
            concept_emb = self.model.encode(concept_clean, normalize_embeddings=True)
            best_sim = 0.0
            best_sid = None
            for sid, node in self.nodes.items():
                if node.get("embedding") is not None:
                    sim = float(np.dot(concept_emb, node["embedding"]))
                    if sim > best_sim:
                        best_sim = sim
                        best_sid = sid
            if best_sim >= SIMILARITY_THRESHOLD and best_sid:
                return best_sid
        
        # 新規作成
        sid = f"s_{self.next_id}"
        self.next_id += 1
        embedding = self.model.encode(concept_clean, normalize_embeddings=True)
        self.nodes[sid] = {
            "label": concept_clean,
            "description": "",
            "edges": [],
            "embedding": embedding
        }
        self.label_index[concept_lower] = sid
        return sid
    
    def to_dict(self) -> dict:
        """保存用にembeddingを除外"""
        return {
            sid: {k: v for k, v in node.items() if k != "embedding"}
            for sid, node in self.nodes.items()
        }
    
    def get_embeddings(self) -> dict:
        """全embeddingを返す"""
        return {
            sid: node["embedding"]
            for sid, node in self.nodes.items()
            if node.get("embedding") is not None
        }


# === 進捗管理 ===
def load_progress() -> dict:
    if PROGRESS_PATH.exists():
        with open(PROGRESS_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"completed_files": [], "episode_count": 0, "semantic_count": 0}


def save_progress(progress: dict):
    with open(PROGRESS_PATH, "w", encoding="utf-8") as f:
        json.dump(progress, f, ensure_ascii=False, indent=2)


# === メイン処理 ===
async def main():
    # API設定
    from dotenv import load_dotenv
    load_dotenv()
    api_key = os.getenv("CG_LLM_DEEPSEEK_API_KEY")
    if not api_key:
        print("エラー: CG_LLM_DEEPSEEK_API_KEY が設定されていません")
        return
    
    client = AsyncOpenAI(api_key=api_key, base_url="https://api.deepseek.com")
    
    # embedding モデル
    print("embedding モデル読み込み中...")
    emb_model = SentenceTransformer("intfloat/multilingual-e5-small")
    
    # セマンティックマネージャー
    sem_mgr = SemanticManager(emb_model)
    
    # 進捗読み込み
    progress = load_progress()
    completed = set(progress["completed_files"])
    
    # グラフ初期化（途中再開の場合は既存データ読み込み）
    if OUTPUT_PATH.exists() and completed:
        with open(OUTPUT_PATH, "r", encoding="utf-8") as f:
            graph = json.load(f)
        # SemanticManagerを復元
        for sid, node in graph.get("semantic_nodes", {}).items():
            sem_mgr.nodes[sid] = node
            sem_mgr.nodes[sid]["embedding"] = emb_model.encode(
                node["label"], normalize_embeddings=True
            )
            sem_mgr.label_index[node["label"].lower()] = sid
            num = int(sid.split("_")[1])
            if num >= sem_mgr.next_id:
                sem_mgr.next_id = num + 1
    else:
        graph = {"episodic_nodes": {}, "semantic_nodes": {}}
    
    episode_id_counter = len(graph["episodic_nodes"]) + 1
    prev_episode_id = None
    
    # ファイル一覧（日付順）
    chat_files = sorted(CHAT_LOG_DIR.glob("*_chat.md"))
    print(f"対象ファイル: {len(chat_files)}件 (完了済み: {len(completed)}件)")
    
    for file_idx, filepath in enumerate(chat_files):
        if filepath.name in completed:
            continue
        
        date_str = get_date_from_filename(filepath)
        print(f"\n[{file_idx+1}/{len(chat_files)}] {filepath.name} ({date_str})")
        
        # ターン分割
        turns = parse_chat_file(filepath)
        if not turns:
            print("  ターンなし、スキップ")
            completed.add(filepath.name)
            continue
        
        print(f"  ターン数: {len(turns)}")
        
        # system prompt 構築
        daily_log = filepath.read_text(encoding="utf-8")
        gemini_warning = GEMINI_WARNING if is_gemini_period(date_str) else ""
        system_prompt = SYSTEM_TEMPLATE.format(
            gemini_warning=gemini_warning,
            daily_log=daily_log
        )
        
        # バッチ処理
        all_episodes = []
        for batch_start in range(0, len(turns), BATCH_SIZE):
            batch = turns[batch_start:batch_start + BATCH_SIZE]
            tasks = [extract_episode(client, system_prompt, t) for t in batch]
            results = await asyncio.gather(*tasks)
            for eps in results:
                all_episodes.extend(eps)
        
        # エピソードノード作成
        for ep in all_episodes:
            eid = f"e_{episode_id_counter}"
            episode_id_counter += 1
            
            # embedding生成
            ep_embedding = emb_model.encode(ep["content"], normalize_embeddings=True)
            
            # エピソードノード
            graph["episodic_nodes"][eid] = {
                "content": ep["content"],
                "timestamp": ep["timestamp"],
                "valence": ep["valence"],
                "edges": [],
                "activation": 0.0,
                "last_activated": None,
                "access_count": 0
            }
            
            # 概念リンク
            for concept in ep["concepts"]:
                sid = sem_mgr.find_or_create(concept, context_embedding=ep_embedding)
                # cosine similarity
                sem_emb = sem_mgr.nodes[sid]["embedding"]
                weight = float(np.dot(ep_embedding, sem_emb))
                
                # abstraction edge (双方向)
                graph["episodic_nodes"][eid]["edges"].append({
                    "target": sid, "type": "abstraction", "weight": round(weight, 4)
                })
                if "edges" not in sem_mgr.nodes[sid]:
                    sem_mgr.nodes[sid]["edges"] = []
                sem_mgr.nodes[sid]["edges"].append({
                    "target": eid, "type": "abstraction", "weight": round(weight, 4)
                })
            
            # temporal edge
            if prev_episode_id:
                graph["episodic_nodes"][eid]["edges"].append({
                    "target": prev_episode_id, "type": "temporal"
                })
                graph["episodic_nodes"][prev_episode_id]["edges"].append({
                    "target": eid, "type": "temporal"
                })
            prev_episode_id = eid
        
        print(f"  抽出: {len(all_episodes)}件")
        
        # 進捗保存
        completed.add(filepath.name)
        progress["completed_files"] = list(completed)
        progress["episode_count"] = len(graph["episodic_nodes"])
        progress["semantic_count"] = len(sem_mgr.nodes)
        
        # 5ファイルごとに中間保存
        if (file_idx + 1) % 5 == 0:
            graph["semantic_nodes"] = sem_mgr.to_dict()
            with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
                json.dump(graph, f, ensure_ascii=False)
            save_progress(progress)
            print(f"  [中間保存] ep={len(graph['episodic_nodes'])}, sem={len(sem_mgr.nodes)}")
    
    # === 最終保存 ===
    graph["semantic_nodes"] = sem_mgr.to_dict()
    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(graph, f, ensure_ascii=False)
    
    # embedding保存
    all_embeddings = {}
    # episodic embeddings
    for eid in graph["episodic_nodes"]:
        content = graph["episodic_nodes"][eid]["content"]
        all_embeddings[eid] = emb_model.encode(content, normalize_embeddings=True)
    # semantic embeddings
    all_embeddings.update(sem_mgr.get_embeddings())
    
    ids = sorted(all_embeddings.keys())
    matrix = np.array([all_embeddings[nid].tolist() if hasattr(all_embeddings[nid], 'tolist') 
                       else all_embeddings[nid] for nid in ids], dtype=np.float32)
    np.save(str(EMBEDDING_PATH), matrix)
    with open(EMBEDDING_IDS_PATH, "w", encoding="utf-8") as f:
        json.dump(ids, f, ensure_ascii=False)
    
    save_progress(progress)
    
    # === 統計 ===
    episodes = graph["episodic_nodes"]
    valences = [n.get("valence", 0) for n in episodes.values()]
    pos = sum(1 for v in valences if v > 0.3)
    neg = sum(1 for v in valences if v < -0.3)
    neu = len(valences) - pos - neg
    avg_v = sum(valences) / len(valences) if valences else 0
    
    print(f"\n=== 再構築完了 ===")
    print(f"エピソードノード: {len(episodes)}件")
    print(f"セマンティックノード: {len(sem_mgr.nodes)}件")
    print(f"embedding: {matrix.shape}")
    print(f"処理ファイル: {len(completed)}件")
    print(f"バレンス統計: 平均={avg_v:.2f}, 正={pos}, 負={neg}, 中立={neu}")


if __name__ == "__main__":
    asyncio.run(main())
