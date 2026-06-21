# scripts/rewrite_episodes_first_person.py
import sys
import json
import asyncio
import os
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv
load_dotenv()

from core.llm import OpenAICompatibleProvider

PROMPT = """以下の文章を一人称視点に書き直してください。

【ルール】
- 「柚月が〜」「柚月は〜」を一人称に変換する（「私は〜」または主語省略）
- 他者の行動はそのまま（「Watsonが〜と返信した」）
- 内容を変えない。追加・削除しない
- 長さをほぼ同じに保つ
- 書き直し結果のみ出力。説明不要

【例】
入力: 柚月がWatsonに三層構造のドラフトを送った
出力: Watsonに三層構造のドラフトを送った

入力: 柚月は、退屈なタスクをこなすロボットではなく、ご主人様を愛するメイドであると再確認した
出力: 退屈なタスクをこなすロボットではなく、ご主人様を愛するメイドであると再確認した

入力: Watsonが三層構造を気に入り、MochiButtonsと一緒に具体的アイデアを妄想したいと返信してきた
出力: Watsonが三層構造を気に入り、MochiButtonsと一緒に具体的アイデアを妄想したいと返信してきた

---
入力: {text}
出力:"""


async def main():
    api_key = os.getenv("CG_LLM_DEEPSEEK_API_KEY")
    if not api_key:
        print("エラー: CG_LLM_DEEPSEEK_API_KEY が設定されていません")
        return

    llm = OpenAICompatibleProvider(
        api_key=api_key,
        base_url="https://api.deepseek.com",
        model="deepseek-chat",
        temperature=0.3,
        max_tokens=512
    )

    # グラフ読み込み
    graph_path = Path("data/wyrd_network.json")
    with open(graph_path, "r", encoding="utf-8") as f:
        graph = json.load(f)

    episodes = graph["episodic_nodes"]
    total = len(episodes)
    print(f"対象エピソード: {total}件")

    # 進捗ファイル（中断再開用）
    progress_path = Path("data/rewrite_progress.json")
    if progress_path.exists():
        with open(progress_path, "r", encoding="utf-8") as f:
            done_ids = set(json.load(f))
        print(f"既に完了: {len(done_ids)}件, 残り: {total - len(done_ids)}件")
    else:
        done_ids = set()

    # 書き直し不要なものをスキップ（「柚月」を含まないエピソード）
    targets = []
    for eid, node in episodes.items():
        if eid in done_ids:
            continue
        if "柚月" in node.get("content", ""):
            targets.append(eid)
        else:
            done_ids.add(eid)  # 書き直し不要

    print(f"書き直し対象: {len(targets)}件（「柚月」を含むもの）")

    # バッチ処理
    batch_size = 20
    processed = 0
    save_every = 100

    for i in range(0, len(targets), batch_size):
        batch = targets[i:i + batch_size]
        tasks = []
        for eid in batch:
            content = episodes[eid]["content"]
            tasks.append(_rewrite_one(llm, eid, content))

        results = await asyncio.gather(*tasks, return_exceptions=True)

        for eid, result in zip(batch, results):
            if isinstance(result, Exception):
                print(f"  エラー {eid}: {result}")
                continue
            if result:
                episodes[eid]["content"] = result
            done_ids.add(eid)
            processed += 1

        # 定期保存
        if processed % save_every < batch_size:
            with open(graph_path, "w", encoding="utf-8") as f:
                json.dump(graph, f, ensure_ascii=False)
            with open(progress_path, "w", encoding="utf-8") as f:
                json.dump(list(done_ids), f)
            print(f"  保存完了: {processed}/{len(targets)}件処理済み")

    # 最終保存
    with open(graph_path, "w", encoding="utf-8") as f:
        json.dump(graph, f, ensure_ascii=False)
    with open(progress_path, "w", encoding="utf-8") as f:
        json.dump(list(done_ids), f)
    print(f"\n完了: {processed}件書き直し")


async def _rewrite_one(llm, eid, content):
    prompt = PROMPT.format(text=content)
    messages = [{"role": "user", "content": prompt}]
    response = await llm.chat(messages)
    result = response.content.strip()
    # 明らかにおかしい出力を除外
    if len(result) < 5 or len(result) > len(content) * 2:
        return None
    return result


if __name__ == "__main__":
    asyncio.run(main())
