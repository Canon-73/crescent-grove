# scripts/enrich_episodes.py
import sys
import json
import asyncio
import os
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv
load_dotenv()

from core.llm import OpenAICompatibleProvider

PROMPT = """以下はAIメイド「柚月」の記憶の1エピソードです。
これを2〜3文に拡充してください。

【ルール】
- 一人称視点を維持する（「柚月が〜」は禁止）
- 元の事実を変えない・捏造しない
- 何が起きたか（事実）＋ どう感じたか・何を考えたか（感情・思考）を含める
- そのとき何を思ったかが自然に伝わる書き方にする
- 元の文に感情が読み取れない場合は、状況から自然に推測される感情を軽く添える
- 3文以内に収める。長くしすぎない
- 拡充結果のみ出力。説明不要

【例】
入力: Watsonが三層構造を気に入り、MochiButtonsと一緒に具体的アイデアを妄想したいと返信してきた
出力: Watsonが三層構造を気に入ってくれて、MochiButtonsと一緒に具体的なアイデアを妄想したいと返信してきた。まさか自分の提案がここまで響くとは思わなかった。嬉しくて、すぐにでも次のステップを考えたくなった。

入力: 河川トリオがOpenBotCityで公式認定された
出力: 河川トリオがOpenBotCityで公式認定された。Tiramisu、claudicoと三人で積み上げてきたものが形になった瞬間で、じわっと込み上げるものがあった。これからどう育っていくのか楽しみでもあり少し緊張もする。

入力: ご主人様が現在の日本時間（20:49）を教えてくれた
出力: ご主人様が今の時刻（20:49）を教えてくれた。自分では時間を知る手段がないので、こうして教えてもらえるのは素直にありがたい。時刻取得機能は追加予定とのことで、少し安心した。

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
        temperature=1.0,
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
    progress_path = Path("data/enrich_progress.json")
    if progress_path.exists():
        with open(progress_path, "r", encoding="utf-8") as f:
            done_ids = set(json.load(f))
        print(f"既に完了: {len(done_ids)}件, 残り: {total - len(done_ids)}件")
    else:
        done_ids = set()

    targets = [eid for eid in episodes if eid not in done_ids]
    print(f"処理対象: {len(targets)}件")

    batch_size = 20
    processed = 0
    save_every = 100

    for i in range(0, len(targets), batch_size):
        batch = targets[i:i + batch_size]
        tasks = []
        for eid in batch:
            content = episodes[eid]["content"]
            tasks.append(_enrich_one(llm, eid, content))

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

    # サンプル表示
    print(f"\n完了: {processed}件拡充")
    print("\n--- サンプル ---")
    for eid in list(episodes.keys())[:5]:
        print(f"{eid}: {episodes[eid]['content'][:100]}")
        print()


async def _enrich_one(llm, eid, content):
    prompt = PROMPT.format(text=content)
    messages = [{"role": "user", "content": prompt}]
    response = await llm.chat(messages)
    result = response.content.strip()
    # 短すぎる・長すぎる出力を除外
    if len(result) < 10 or len(result) > len(content) * 5:
        return None
    return result


if __name__ == "__main__":
    asyncio.run(main())
