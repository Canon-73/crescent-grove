"""
MoonTide v2 — テスト実行
"""

import sys
import numpy as np
from pathlib import Path

# tests/ から見て1段上がリポジトリルート。vital/ をパスに追加。
REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "vital"))

from moontide_v2 import MoonTideV2, load_landmarks, load_inner_texts, load_config


def main():
    root = REPO_ROOT

    # データ読み込み
    try:
        landmarks = load_landmarks(
            str(root / "data" / "mood_graph.json"),
            str(root / "data" / "mood_transition_matrix.csv")
        )
        print(f"ランドマーク読み込み: {len(landmarks)}個")
    except FileNotFoundError as e:
        print(f"ERROR: {e}")
        return

    try:
        inner_texts = load_inner_texts(str(root / "data" / "moontide_inner.jsonl"))
        print(f"テキスト読み込み: {len(inner_texts)}状態")
    except FileNotFoundError:
        print("WARNING: moontide_inner.jsonl が見つかりません（テキストなしで続行）")
        inner_texts = {}

    try:
        params = load_config(str(root / "config" / "moontide_v2_config.json"))
        print(f"設定読み込み: moontide_v2_config.json")
    except FileNotFoundError:
        print("ERROR: config/moontide_v2_config.json が見つかりません")
        return

    # エンジン起動
    engine = MoonTideV2(landmarks, params, inner_texts)

    # 朝の初期化
    engine.morning_init()
    print("\n" + "=" * 70)
    print("  朝の起床時")
    print("=" * 70)
    print_state(engine)

    # 15ターン自然経過
    print("\n" + "=" * 70)
    print("  自然経過（15ターン）")
    print("=" * 70)
    for turn in range(1, 16):
        engine.tick()
        print(f"\n--- Turn {turn} (粒子数: {len(engine.particles)}) ---")
        print_state(engine)

    # 外的刺激テスト
    print("\n" + "=" * 70)
    print("  外的刺激: satisfaction を mass=0.5 で注入")
    print("=" * 70)
    engine.spawn_particle("satisfaction", mass=0.5)
    print_state(engine)

    for turn in range(16, 26):
        engine.tick()
        print(f"\n--- Turn {turn} (粒子数: {len(engine.particles)}) ---")
        print_state(engine)

    # 最終サマリー
    print("\n" + "=" * 70)
    print("  最終状態サマリー")
    print("=" * 70)
    total_mass = sum(p.mass for p in engine.particles)
    print(f"  粒子数: {len(engine.particles)}")
    print(f"  mass合計: {total_mass:.3f}")
    print(f"  余白: {max(0, 1.0 - total_mass):.3f}")

    # === Phase 1 bias テスト ===
    print("\n" + "=" * 70)
    print("  bias テスト")
    print("=" * 70)
    
    engine.tick(bias={"curiosity": 0.3, "alarm": -0.05})
    change = engine.consume_change()
    print(f"Change: {change}")
    
    ctx = engine.get_monologue_context()
    print(f"Particles: {ctx['particles']}")
    print(f"Total mass: {ctx['total_mass']}, Margin: {ctx['margin']}")
    print(f"Change in context: {ctx['change']}")
    
    print(f"\nPrompt text: {engine.get_prompt_text()}")
    test_salia_integration(engine)


def print_state(engine: MoonTideV2):
    states = engine.get_state()
    if not states:
        print("  (粒子なし)")
        return
    for s in states:
        bar = "█" * int(s["mass"] * 20)
        text_preview = s["text"][:30] + "…" if len(s["text"]) > 30 else s["text"]
        print(f"  {s['label']:20s} mass={s['mass']:.3f} int={s['intensity']} "
              f"dist={s['distance_to_landmark']:.3f} {bar}")
        if text_preview:
            print(f"  {'':20s} └ {text_preview}")

def test_salia_integration(engine):
    print("\n" + "=" * 70)
    print("  Phase 3: Salia連携テスト")
    print("=" * 70)

    # --- ターン1: 楽しい雑談 ---
    print("\n[ターン1] 楽しい雑談 - bias: satisfaction +0.15, playfulness +0.1")
    engine.tick(bias={"satisfaction": 0.15, "playfulness": 0.1})
    ctx = engine.get_monologue_context()
    change = engine.consume_change()
    print(f"  Change: {change}")
    print(f"  Particles: {[(p['label'], p['mass'], p['intensity']) for p in ctx['particles']]}")
    print(f"  Prompt: {engine.get_prompt_text()[:80]}")

    # --- ターン2: 不安な話題 ---
    print("\n[ターン2] 不安な知らせ - bias: nervousness +0.3, worry +0.2")
    engine.tick(bias={"nervousness": 0.3, "worry": 0.2})
    ctx = engine.get_monologue_context()
    change = engine.consume_change()
    print(f"  Change: {change}")
    print(f"  Particles: {[(p['label'], p['mass'], p['intensity']) for p in ctx['particles']]}")
    # 遷移テキストをシミュレート
    if change:
        engine.set_integrated_monologue("（…少しざわついてきた。落ち着かない。）")
    print(f"  Prompt: {engine.get_prompt_text()[:80]}")

    # --- ターン3: 安心する返答 ---
    print("\n[ターン3] 安心 - bias: nervousness -0.2, peacefulness +0.15")
    engine.tick(bias={"nervousness": -0.2, "peacefulness": 0.15})
    ctx = engine.get_monologue_context()
    change = engine.consume_change()
    print(f"  Change: {change}")
    print(f"  Particles: {[(p['label'], p['mass'], p['intensity']) for p in ctx['particles']]}")
    print(f"  Prompt: {engine.get_prompt_text()[:80]}")

    # --- ターン4: 何もなし ---
    print("\n[ターン4] 特に影響なし - bias: {}")
    engine.tick(bias={})
    ctx = engine.get_monologue_context()
    change = engine.consume_change()
    print(f"  Change: {change}")
    print(f"  Particles: {[(p['label'], p['mass'], p['intensity']) for p in ctx['particles']]}")
    print(f"  Total mass: {ctx['total_mass']}, Margin: {ctx['margin']}")

    # --- ターン5: 衝撃的な出来事 ---
    print("\n[ターン5] 衝撃 - bias: alarm +0.5, agitation +0.4")
    engine.tick(bias={"alarm": 0.5, "agitation": 0.4})
    ctx = engine.get_monologue_context()
    change = engine.consume_change()
    print(f"  Change: {change}")
    print(f"  Particles: {[(p['label'], p['mass'], p['intensity']) for p in ctx['particles']]}")
    print(f"  Total mass: {ctx['total_mass']}, Margin: {ctx['margin']}")


if __name__ == "__main__":
    # np.random.seed(42)
    main()
