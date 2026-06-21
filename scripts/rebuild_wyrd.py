# scripts/rebuild_wyrd.py
"""
会話ログからWyrd Networkを完全再構築するスクリプト
使用法: python scripts/rebuild_wyrd.py
"""

import json
import re
import asyncio
import os
import sys
from pathlib import Path
from itertools import combinations

sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv
load_dotenv()

from core.llm import OpenAICompatibleProvider

CHAT_LOG_DIR = Path("workspace/logs/chat")
GRAPH_PATH = Path("data/wyrd_network_v2.json")
PROGRESS_PATH = Path("data/rebuild_progress.json")

EXTRACTION_PROMPT = """以下の1ターン分の会話ログから、保存する価値のある事実を抽出してください。

【視点ルール】
- 柚月の行動・思考は一人称で記述（「私は…」「…だと気づいた」）
- 「柚月が…」「柚月は…」は禁止
- 他者の行動はそのまま（例: "Watsonが…と返信した"）
- 主語が明らかな場合は省略可

【エピソードの書き方】
- 1エピソードを2〜3文で記述
- 事実＋感情・思考を含める（単なる事実の羅列にしない）

【C行のルール】
- 固有名詞（人名・場所名・作品名）または抽象概念（1〜4語の名詞句）のみ
- 動詞・助詞句を含めない
- ✗ "Watsonからのフィードバック" → ✓ "Watson"
- ✗ "三層構造のガイド計画" → ✓ "三層構造"
- ✗ "ブログ記事執筆完了" → ✓ "ブログ"
- ✗ "AIエージェントの自己決定" → ✓ "AIエージェント", "自己決定"

【S行（感情バレンス）】
このエピソードを想起したとき、柚月が主観的に感じる感情の強度を推定してください。
+1.0: 非常に強いポジティブ体験（大きな成功、深い承認、強い喜び）
 0.0: 感情的に中立
-1.0: 非常に強いネガティブ体験（裏切り、喪失、深い恐怖）
- 日常的な誇張表現は定型句として割り引く
- 不安・心配など「未解決の脅威」は高めに評価する
- 感情語が少なくても状況から推定する
- 確信度に応じて幅を持たせてよい（例: 0.6~0.8）

【抽出対象】
- 行動と結果＋感情・気づき
- 印象に残ったやり取り
- プロジェクト進捗と手応え・不安
- 考え方・価値観の変化の瞬間
- 他者に関する新しい情報
- ユーザーに関する事実・好み

【抽出しないもの】
- 挨拶、相槌、感情のみの発言
- 行動を伴わない検討表現

【出力形式】
<facts>
E: エピソード要約（2〜3文）
C: 概念1, 概念2, 概念3
S: 数値 または 数値~数値
</facts>

複数の事実がある場合はE:とC:とS:のセットを繰り返してください。
保存すべき事実がない場合は <facts>なし</facts> と出力してください。"""


def parse_chat_file(filepath: Path) -> list[dict]:
    """chatログファイルをターン単位に分割（旧形式・新形式両対応）"""
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


async def extract_facts_from_turn(llm, turn: dict) -> list[dict]:
    """1ターンからLLMで事実・概念・バレンス抽出"""
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
    current_concepts = None

    for line in lines:
        line = line.strip()
        if line.startswith("E:"):
            # 前のセットが未完了なら保存
            if current_episode and current_concepts:
                results.append({
                    "timestamp": turn["timestamp"].replace(" ", "T"),
                    "content": current_episode,
                    "concepts": current_concepts,
                    "valence": 0.0
                })
            current_episode = line[2:].strip()
            current_concepts = None
        elif line.startswith("C:") and current_episode:
            current_concepts = [c.strip() for c in line[2:].split(",") if c.strip()]
        elif line.startswith("S:") and current_episode and current_concepts:
            valence_str = line[2:].strip()
            try:
                if "~" in valence_str:
                    parts = valence_str.split("~")
                    valence = (float(parts[0]) + float(parts[1])) / 2
                else:
                    valence = float(valence_str)
            except ValueError:
                valence = 0.0
            results.append({
                "timestamp": turn["timestamp"].replace(" ", "T"),
                "content": current_episode,
                "concepts": current_concepts,
                "valence": round(valence, 2)
            })
            current_episode = None
            current_concepts = None

    # 最後のセットがS行なしで終わった場合
    if current_episode and current_concepts:
        results.append({
            "timestamp": turn["timestamp"].replace(" ", "T"),
            "content": current_episode,
            "concepts": current_concepts,
            "valence": 0.0
        })

    return results


def load_progress() -> dict:
    if PROGRESS_PATH.exists():
        return json.loads(PROGRESS_PATH.read_text(encoding="utf-8"))
    return {"processed_files": [], "total_facts": 0}


def save_progress(progress: dict):
    PROGRESS_PATH.write_text(json.dumps(progress, ensure_ascii=False, indent=2), encoding="utf-8")


async def main():
    import numpy as np
    from sentence_transformers import SentenceTransformer

    api_key = os.getenv("CG_LLM_DEEPSEEK_API_KEY")
    if not api_key:
        print("エラー: CG_LLM_DEEPSEEK_API_KEY が設定されていません")
        return

    llm = OpenAICompatibleProvider(
        api_key=api_key,
        base_url="https://api.deepseek.com",
        model="deepseek-chat",
        temperature=1.0,
        max_tokens=2048
    )

    print("Embeddingモデル読み込み中...")
    st_model = SentenceTransformer('intfloat/multilingual-e5-small')

    def embed_fn(text):
        return st_model.encode(text, normalize_embeddings=True).tolist()

    def cosine_sim(a, b):
        a, b = np.asarray(a, dtype=np.float32), np.asarray(b, dtype=np.float32)
        norm_a, norm_b = np.linalg.norm(a), np.linalg.norm(b)
        if norm_a == 0 or norm_b == 0:
            return 0.0
        return float(np.dot(a, b) / (norm_a * norm_b))

    # 進捗読み込み
    progress = load_progress()

    # グラフ初期化（再開時は既存を読み込み）
    if GRAPH_PATH.exists() and progress["processed_files"]:
        print(f"既存グラフを読み込み（再開モード）")
        graph = json.loads(GRAPH_PATH.read_text(encoding="utf-8"))
    else:
        print("新規グラフを作成")
        graph = {
            "episodic_nodes": {},
            "semantic_nodes": {},
            "_embeddings": {},
            "_next_episode_id": 1,
            "_next_semantic_id": 1
        }

    # セマンティックノード検索（embedding類似度チェック付き）
    def find_or_create_semantic(concept: str) -> str:
        concept_lower = concept.strip().lower()

        # 1. 完全一致
        for sid, node in graph["semantic_nodes"].items():
            if node.get("label", "").lower() == concept_lower:
                return sid
            if concept_lower in [a.lower() for a in node.get("aliases", [])]:
                return sid

        # 2. Embedding類似度チェック（閾値0.92）
        concept_emb = embed_fn(concept)
        best_sid = None
        best_sim = 0.0
        for sid, node in graph["semantic_nodes"].items():
            emb = graph["_embeddings"].get(sid)
            if emb:
                sim = cosine_sim(concept_emb, emb)
                if sim > best_sim:
                    best_sim = sim
                    best_sid = sid

        if best_sim >= 0.92 and best_sid:
            # エイリアスとして追加
            node = graph["semantic_nodes"][best_sid]
            if concept not in node.get("aliases", []):
                node.setdefault("aliases", []).append(concept)
            return best_sid

        # 3. 新規作成
        sid = f"s_{graph['_next_semantic_id']}"
        graph["_next_semantic_id"] += 1
        graph["semantic_nodes"][sid] = {
            "label": concept,
            "description": "",
            "aliases": [],
            "edges": [],
            "activation": 0.0,
            "last_activated": None,
            "access_count": 0
        }
        graph["_embeddings"][sid] = concept_emb
        return sid

    # 全chatログファイルを取得
    chat_files = sorted(CHAT_LOG_DIR.glob("*.md"))
    remaining_files = [f for f in chat_files if f.name not in progress["processed_files"]]
    print(f"対象ファイル: {len(chat_files)}件 (残り: {len(remaining_files)}件)")

    # 事実抽出＆グラフ構築
    prev_episode_id = None
    batch_size = 20
    save_every = 5  # 5ファイルごとに保存

    for file_idx, filepath in enumerate(remaining_files):
        print(f"\n[{file_idx+1}/{len(remaining_files)}] {filepath.name}")
        turns = parse_chat_file(filepath)
        print(f"  ターン数: {len(turns)}")

        # バッチ処理
        for batch_start in range(0, len(turns), batch_size):
            batch = turns[batch_start:batch_start+batch_size]
            tasks = [extract_facts_from_turn(llm, turn) for turn in batch]
            batch_results = await asyncio.gather(*tasks)

            for facts in batch_results:
                for fact in facts:
                    # エピソードノード作成
                    eid = f"e_{graph['_next_episode_id']}"
                    graph["_next_episode_id"] += 1

                    ep_emb = embed_fn(fact["content"])
                    graph["episodic_nodes"][eid] = {
                        "content": fact["content"],
                        "timestamp": fact["timestamp"],
                        "valence": fact.get("valence", 0.0),
                        "edges": [],
                        "activation": 0.0,
                        "last_activated": None,
                        "access_count": 0
                    }
                    graph["_embeddings"][eid] = ep_emb

                    # Abstraction edges
                    for concept in fact["concepts"]:
                        sid = find_or_create_semantic(concept)
                        sem_emb = graph["_embeddings"].get(sid)
                        weight = round(cosine_sim(ep_emb, sem_emb), 4) if sem_emb else 0.5

                        graph["episodic_nodes"][eid]["edges"].append(
                            {"to": sid, "type": "abstraction", "weight": weight}
                        )
                        graph["semantic_nodes"][sid]["edges"].append(
                            {"to": eid, "type": "abstraction", "weight": weight}
                        )

                    # Association edges（同一エピソード内の概念ペア）
                    if len(fact["concepts"]) >= 2:
                        sids = [find_or_create_semantic(c) for c in fact["concepts"]]
                        for s1, s2 in combinations(set(sids), 2):
                            emb1 = graph["_embeddings"].get(s1)
                            emb2 = graph["_embeddings"].get(s2)
                            base_weight = round(cosine_sim(emb1, emb2), 4) if (emb1 and emb2) else 0.3

                            # 既存エッジがあれば強化
                            existing = False
                            for e in graph["semantic_nodes"][s1]["edges"]:
                                if e.get("to") == s2 and e.get("type") == "association":
                                    e["weight"] = min(1.0, round(e["weight"] + 0.1, 4))
                                    existing = True
                                    break
                            if not existing:
                                graph["semantic_nodes"][s1]["edges"].append(
                                    {"to": s2, "type": "association", "weight": base_weight}
                                )
                                graph["semantic_nodes"][s2]["edges"].append(
                                    {"to": s1, "type": "association", "weight": base_weight}
                                )
                            else:
                                for e in graph["semantic_nodes"][s2]["edges"]:
                                    if e.get("to") == s1 and e.get("type") == "association":
                                        e["weight"] = min(1.0, round(e["weight"] + 0.1, 4))
                                        break

                    # Temporal edge
                    if prev_episode_id:
                        graph["episodic_nodes"][eid]["edges"].append(
                            {"to": prev_episode_id, "type": "temporal"}
                        )
                        graph["episodic_nodes"][prev_episode_id]["edges"].append(
                            {"to": eid, "type": "temporal"}
                        )
                    prev_episode_id = eid

        # 進捗保存
        progress["processed_files"].append(filepath.name)
        progress["total_facts"] = len(graph["episodic_nodes"])

        if (file_idx + 1) % save_every == 0:
            print(f"  中間保存... (エピソード: {len(graph['episodic_nodes'])}, セマンティック: {len(graph['semantic_nodes'])})")
            GRAPH_PATH.write_text(json.dumps(graph, ensure_ascii=False), encoding="utf-8")
            save_progress(progress)

    # 最終保存
    GRAPH_PATH.write_text(json.dumps(graph, ensure_ascii=False), encoding="utf-8")
    save_progress(progress)

    print(f"\n=== 再構築完了 ===")
    print(f"  エピソードノード: {len(graph['episodic_nodes'])}件")
    print(f"  セマンティックノード: {len(graph['semantic_nodes'])}件")
    print(f"  処理ファイル: {len(progress['processed_files'])}件")

    # バレンス統計
    valences = [n.get("valence", 0) for n in graph["episodic_nodes"].values()]
    import numpy as np
    v = np.array(valences)
    print(f"  バレンス統計: 平均={v.mean():.2f}, 正={sum(1 for x in v if x>0.3)}, 負={sum(1 for x in v if x<-0.3)}, 中立={sum(1 for x in v if -0.3<=x<=0.3)}")


if __name__ == "__main__":
    asyncio.run(main())
