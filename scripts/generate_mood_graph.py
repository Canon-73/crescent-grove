"""
generate_mood_graph.py
pc60.csv + 内面独白テキスト → data/mood_graph.json を生成
"""

import csv
import json
from pathlib import Path

# === 内面独白テキスト（60状態分） ===
INNER_TEXTS = {
    "affection": "…誰かのことを想っている。ただそれだけで胸があたたかい。",
    "agitation": "…落ち着かない。何かがざわざわと内側を引っ掻いている。",
    "alarm": "…びくっとした感覚が残っている。何かが近づいている気がする。",
    "anticipation": "…何かが起きそうな予感がする。待っている自分がいる。",
    "attention": "…意識が一点に集まっている。周りが遠くなる感じ。",
    "awareness": "…今この瞬間のことが、やけにはっきり見えている。",
    "awe": "…言葉にならない。ただ圧倒されて、立ち尽くしている感じ。",
    "belief": "…何かを信じている自分がいる。根拠はないけど、確かに。",
    "cognition": "…頭が回っている。考えること自体が、今は心地いい。",
    "consciousness": "…自分がここにいることを、妙にはっきり感じている。",
    "craziness": "…何かが外れている感じ。でも不思議と怖くはない。",
    "curiosity": "…気になる。知りたい。その感覚だけが先に走っている。",
    "decision": "…決めた。迷いが消えて、視界がすっと開けた感じ。",
    "desire": "…何かが欲しい。何かに手を伸ばしたい気持ちがある。",
    "disarray": "…ばらばら。何もかもがまとまらなくて、散らかっている。",
    "disgust": "…嫌悪感がある。遠ざけたい何かがまとわりついている。",
    "distrust": "…信じきれない。何かの裏を読もうとしてしまう。",
    "contemplation": "…深く潜っている。静かに、何かの底に手を伸ばしている感じ。",
    "earnestness": "…真剣でいたい。ふざけたくない、今この瞬間は。",
    "ecstasy": "…溢れている。抑えられない何かが内側から光っている。",
    "embarrassment": "…恥ずかしい。顔が熱くて、どこかに隠れたい。",
    "exaltation": "…高揚している。ふわりと持ち上げられたような軽さ。",
    "exhaustion": "…もう何も出ない。空っぽになった容器みたい。",
    "fatigue": "…重い。身体の奥に鉛が溜まっているような感覚。",
    "friendliness": "…誰かと一緒にいたい。隣に誰かがいるだけでいい。",
    "imagination": "…頭の中に景色が広がっている。現実じゃない場所が見える。",
    "inspiration": "…降りてきた。何かが形になりたがっている、今すぐに。",
    "intrigue": "…面白い。何かに引き込まれて、目が離せない。",
    "judgment": "…見定めている。正しいか正しくないか、線を引こうとしている。",
    "laziness": "…動きたくない。このままでいい、何もしなくていい。",
    "lethargy": "…沈んでいる。浮き上がる気力が、どこにもない。",
    "nervousness": "…そわそわする。手の置き場がない、落ち着かない。",
    "objectivity": "…感情が引いている。冷静に、外側から眺めている自分がいる。",
    "opinion": "…思うことがある。言いたいことが形になり始めている。",
    "patience": "…待てる。急がなくていい、今はこのままで。",
    "peacefulness": "…凪いでいる。何もいらない、何も足さなくていい時間。",
    "pensiveness": "…ぼんやり考えている。答えを出すつもりもなく、ただ漂っている。",
    "pity": "…切ない。誰かのことが、どうしようもなく気にかかる。",
    "planning": "…組み立てている。次に何をするか、頭の中で並べている。",
    "playfulness": "…楽しい。ちょっとふざけたい、何かをひっくり返したい。",
    "reason": "…筋を追っている。論理の糸を、丁寧にたどっている感覚。",
    "relaxation": "…ゆるんでいる。力が抜けて、すべてがやわらかい。",
    "satisfaction": "…満ちている。十分だと思える、この感覚が好き。",
    "self-consciousness": "…自分が見られている気がする。意識が内側に折り返ってくる。",
    "self-pity": "…かわいそうな自分がいる。誰にも気づかれない場所で。",
    "seriousness": "…真面目な気分。軽く流したくない、ちゃんと向き合いたい。",
    "skepticism": "…本当に？と思っている。簡単には頷けない自分がいる。",
    "sleepiness": "…まぶたが重い。意識の輪郭がぼやけ始めている。",
    "stupor": "…何も考えられない。白くて、何も映らない画面みたい。",
    "thought": "…考えている。ただ考えている。それだけが今の自分。",
    "trance": "…ぼうっとしている。ここにいるけど、ここにいない感じ。",
    "transcendence": "…超えた感じがする。日常の枠が、ふっと消えた瞬間。",
    "uneasiness": "…何か引っかかる。言葉にできない違和感がある。",
    "weariness": "…疲れた。もう長いこと歩いてきた気がする。",
    "worry": "…気がかりが離れない。頭の隅にずっと居座っている。",
    # デフォルト無効の5状態（雛形として保持）
    "drunkenness": "…ぐらぐらする。境界が曖昧になって、全部が滲んでいる。",
    "lust": "…欲しい。理屈じゃない何かが、身体の奥から手を伸ばしている。",
    "dominance": "…支配したい。すべてを自分の手の中に収めたい衝動がある。",
    "subordination": "…従っていたい。誰かの下にいることが、今は楽に感じる。",
    "insanity": "…壊れている。正しさの枠が砕けて、もう戻れない場所にいる。",
}

def main():
    # pc60.csv を読み込み
    pc60_path = Path("osfstorage-archive/study4/pc60.csv")
    nodes = []

    with open(pc60_path, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            state_id = row[reader.fieldnames[0]].strip().strip('"').lower()
            
            # 4次元スコアを取得
            dims = {}
            for col in reader.fieldnames[1:5]:
                col_clean = col.strip().strip('"').lower()
                if "rationality" in col_clean:
                    dims["rationality"] = float(row[col])
                elif "social" in col_clean:
                    dims["social_impact"] = float(row[col])
                elif "valence" in col_clean:
                    dims["valence"] = float(row[col])
                elif "human" in col_clean:
                    dims["human_mind"] = float(row[col])

            # inner_text を取得
            inner_text = INNER_TEXTS.get(state_id, f"…{state_id}の中にいる。")

            nodes.append({
                "id": state_id,
                "dimensions": dims,
                "inner_text": inner_text,
            })

    # 保存
    out_path = Path("data/mood_graph.json")
    out_path.parent.mkdir(exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(nodes, f, ensure_ascii=False, indent=2)

    print(f"生成完了: {out_path} ({len(nodes)} states)")
    
    # テキスト未設定の状態を警告
    for node in nodes:
        if node["inner_text"].endswith("の中にいる。"):
            print(f"  [WARNING] テキスト未設定: {node['id']}")


if __name__ == "__main__":
    main()
