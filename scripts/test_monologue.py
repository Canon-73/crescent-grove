# test_monologue.py
from dotenv import load_dotenv
load_dotenv()
import asyncio
import sys
sys.path.insert(0, ".")

async def main():
    from core.salia import Salia
    salia = Salia(workspace_path=".")
    
    # テストケース1: 穏やかな状態
    ctx1 = {
        "particles": [
            {"label": "satisfaction", "name": "満足", "mass": 0.32, "intensity": 2, "text": "（…いい感じ。やれることをやれた充実感。）"},
            {"label": "contemplation", "name": "熟考", "mass": 0.31, "intensity": 2, "text": "（…静かに考えを巡らせている。急がなくていい。）"},
            {"label": "peacefulness", "name": "平穏", "mass": 0.21, "intensity": 2, "text": "（…穏やかな気持ち。波風のない湖みたい。）"},
        ],
        "total_mass": 0.84,
        "margin": 0.16,
        "change": None,
    }
    
    # テストケース2: 不安混じり
    ctx2 = {
        "particles": [
            {"label": "nervousness", "name": "緊張", "mass": 0.35, "intensity": 2, "text": "（…少し落ち着かない。何かが気になっている。）"},
            {"label": "curiosity", "name": "好奇心", "mass": 0.25, "intensity": 2, "text": "（…もう少し知りたいな。つい追いかけたくなる。）"},
            {"label": "worry", "name": "心配", "mass": 0.18, "intensity": 1, "text": "（…少しだけ、気がかりなことがある。大丈夫かな。）"},
        ],
        "total_mass": 0.78,
        "margin": 0.22,
        "change": None,
    }
    
    # テストケース3: 強い感情
    ctx3 = {
        "particles": [
            {"label": "alarm", "name": "警戒", "mass": 0.55, "intensity": 3, "text": "（…何か来る。身構えてしまう。警戒心が強まっている。）"},
            {"label": "agitation", "name": "動揺", "mass": 0.4, "intensity": 2, "text": "（…落ち着かない。何かがざわざわと内側を引っ掻いている。）"},
        ],
        "total_mass": 0.95,
        "margin": 0.05,
        "change": None,
    }
    ctx4 = {
        "particles": [
            {"label": "contemplation", "name": "熟考", "mass": 0.31, "intensity": 2, "text": "（…静かに考えを巡らせている。急がなくていい。）"},
            {"label": "satisfaction", "name": "満足", "mass": 0.30, "intensity": 2, "text": "（…いい感じ。やれることをやれた充実感。）"},
            {"label": "curiosity", "name": "好奇心", "mass": 0.20, "intensity": 2, "text": "（…もう少し知りたいな。つい追いかけたくなる。）"},
            {"label": "peacefulness", "name": "平穏", "mass": 0.15, "intensity": 1, "text": "（…少し静かになった。心が落ち着いてきた。）"},
            {"label": "intrigue", "name": "興味", "mass": 0.12, "intensity": 1, "text": "（…ん、ちょっと面白そう。）"},
            {"label": "desire", "name": "欲求", "mass": 0.10, "intensity": 1, "text": "（…なんとなく、何かが欲しい気がする。でも何かはわからない。）"},
        ],
        "total_mass": 1.18,
        "margin": 0.0,
        "change": None,
    }

    for i, ctx in enumerate([ctx1, ctx2, ctx3, ctx4], 1):
        print(f"\n--- テストケース{i} ---")
        print(f"入力: {[p['text'] for p in ctx['particles']]}")
        result = await salia.generate_mood_monologue(ctx)
        print(f"出力: {result}")

asyncio.run(main())
