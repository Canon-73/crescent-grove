# scripts/search_wyrd.py
"""
Wyrd Network（柚月の長期記憶ネットワーク）を検索するツール。

2つの使い方:
  1) ダブルクリック起動の対話モード（search_wyrd.bat 経由）
     → メニューでモードを選び、クエリを打ち込んで何度でも検索できる。
  2) コマンドライン（従来どおり）
     python scripts/search_wyrd.py "カノン 配信"
     python scripts/search_wyrd.py --dict "カノン"

サーバ本体の recall ツール（core/tools.py）と同じ load_graph / search_memory /
search_concept を使い、同じ embedding モデル・同じ検索設定で結果を再現する。

【重要・安全設計】
このツールは「読むだけ」。検索でヒットしたノードの access_count などを書き戻す
save_graph は呼ばない。稼働中の Crescent Grove サーバ（柚月）と data/wyrd_network.json
を共有しているため、CLI から勝手に書き戻すとライブデータと競合しうる。

dev は venv で動かすこと（faiss / sentence-transformers / numpy が要る）。
"""

import argparse
import json
import sys
from pathlib import Path

# Windows の cp932 コンソールでも日本語（em-dash 等含む）を落とさず入出力するため UTF-8 化。
# 古い Python だと reconfigure が無いので握り潰す。
for _stream in (sys.stdout, sys.stdin):
    try:
        _stream.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

# プロジェクトルートを import パスに追加（scripts/ から core を読む）
sys.path.insert(0, str(Path(__file__).parent.parent))

from core.wyrd_network import load_graph, search_memory, search_concept, node_count
from core.paths import resolve_model, config_file


def build_embed_fn():
    """
    検索クエリを埋め込みベクトルに変換する関数を返す。

    サーバ本体（core/agent.py / core/rag.py）と同じ multilingual-e5-small を使う。
    resolve_model の二刀流で、同梱 models/ があればオフライン、無ければ HF キャッシュ解決。
    """
    from sentence_transformers import SentenceTransformer

    model_src, local_only = resolve_model(
        "multilingual-e5-small", "intfloat/multilingual-e5-small"
    )
    model = SentenceTransformer(model_src, local_files_only=local_only)

    def embed_fn(text):
        return model.encode(text, normalize_embeddings=True).tolist()

    return embed_fn


def load_search_config():
    """config/wyrd_config.json の search セクションを読む（recall ツールと同じ設定）。"""
    config_path = config_file("wyrd_config.json")
    if config_path.exists():
        with open(config_path, "r", encoding="utf-8") as f:
            return json.load(f).get("search", {})
    return {}


def print_network_results(query, results):
    """ネットワーク検索結果を整形して表示する。"""
    episodes = results.get("episodes") if results else None
    if not episodes:
        print(f"\n「{query}」に関連する記憶は見つかりませんでした。")
        return

    print(f"\n=== 「{query}」に関連する記憶（上位 {len(episodes)}件）===\n")
    for i, r in enumerate(episodes, 1):
        date = r["timestamp"][:10]
        valence = r.get("valence", 0.0)
        print(f"{i}. [{date}] (energy={r['energy']}, valence={valence:+.2f})")
        print(f"   {r['content']}\n")

    related = results.get("related_concepts")
    if related:
        print(f"関連概念: {', '.join(related)}")


def print_dictionary_result(query, result):
    """辞書検索結果を整形して表示する。"""
    if result["match"] in ("exact", "alias"):
        match_label = "完全一致" if result["match"] == "exact" else "エイリアス一致"
        print(f"\n=== {result['label']} （{match_label}）===")
        print(f"説明: {result['description'] or '(説明なし)'}")
        print(f"接続エッジ数: {result['edge_count']}")
        if result.get("aliases"):
            print(f"別名: {', '.join(result['aliases'])}")
    else:
        suggestions = ", ".join(result["suggestions"]) or "(候補なし)"
        print(f"\n「{query}」に一致する概念は見つかりませんでした。")
        print(f"近い概念の候補: {suggestions}")


def do_network_search(graph, embed_fn, query, top_k):
    """ネットワーク検索（spreading activation）。recall(source=network) 相当。"""
    config = load_search_config()
    results = search_memory(query, graph, embed_fn=embed_fn, config=config, top_k=top_k)
    # ※ save_graph は呼ばない（柚月のライブデータを壊さないため）
    print_network_results(query, results)


def do_dictionary_search(graph, embed_fn, query):
    """辞書検索。概念名から description を引く。recall(source=dictionary) 相当。"""
    result = search_concept(query, graph, embed_fn=embed_fn)
    print_dictionary_result(query, result)


# ─────────────────────────────────────────────
# 対話モード（ダブルクリック起動向け）
# ─────────────────────────────────────────────

BANNER = """
============================================================
  Wyrd Network 検索ツール（柚月の長期記憶 / 読み取り専用）
============================================================"""


def _ask(prompt):
    """入力を1行受け取る。Ctrl+C / EOF は終了シグナルとして None を返す。"""
    try:
        return input(prompt).strip()
    except (EOFError, KeyboardInterrupt):
        return None


def interactive_loop():
    """メニューでモードを選び、クエリを打ち込んで繰り返し検索する対話UI。"""
    print(BANNER)
    print("モデルとグラフを読み込み中...（初回は少し時間がかかります）")

    embed_fn = build_embed_fn()
    graph = load_graph()
    counts = node_count(graph)
    print(
        f"読み込み完了: エピソード {counts['episodic']}件 / "
        f"セマンティック {counts['semantic']}件"
    )

    while True:
        print("\n------------------------------------------------------------")
        print("モードを選んでください:")
        print("  1) ネットワーク検索（関連する記憶エピソードを探す）")
        print("  2) 辞書検索（概念ノードの説明を引く）")
        print("  q) 終了")
        choice = _ask("> ")

        if choice is None or choice.lower() in ("q", "quit", "exit"):
            print("\n終了します。")
            break

        if choice == "1":
            query = _ask("検索クエリを入力（複数語は空白区切り）: ")
            if not query:
                print("クエリが空です。メニューに戻ります。")
                continue
            n_raw = _ask("表示件数（Enter で 5件）: ")
            try:
                top_k = int(n_raw) if n_raw else 5
            except ValueError:
                print("数値として読めないので 5件にします。")
                top_k = 5
            do_network_search(graph, embed_fn, query, top_k)

        elif choice == "2":
            query = _ask("調べたい概念名を入力: ")
            if not query:
                print("クエリが空です。メニューに戻ります。")
                continue
            do_dictionary_search(graph, embed_fn, query)

        else:
            print("1 / 2 / q のいずれかを入力してください。")


# ─────────────────────────────────────────────
# コマンドラインモード（従来どおり）
# ─────────────────────────────────────────────

def cli_mode(args):
    """引数つきで呼ばれた場合の一発検索。"""
    query = " ".join(args.query)
    embed_fn = build_embed_fn()
    graph = load_graph()

    if args.dict_mode:
        do_dictionary_search(graph, embed_fn, query)
    else:
        counts = node_count(graph)
        print(
            f"[グラフ] エピソード {counts['episodic']}件 / "
            f"セマンティック {counts['semantic']}件"
        )
        do_network_search(graph, embed_fn, query, args.top_k)


def main():
    parser = argparse.ArgumentParser(
        description="Wyrd Network（柚月の長期記憶）を検索する読み取り専用ツール",
    )
    # query 省略時は対話モードに入る（ダブルクリック起動を想定）
    parser.add_argument("query", nargs="*", help="検索クエリ（省略すると対話モード）")
    parser.add_argument(
        "--dict",
        action="store_true",
        dest="dict_mode",
        help="辞書検索モード（概念名から説明を引く）。指定しなければネットワーク検索",
    )
    parser.add_argument(
        "-n",
        type=int,
        default=5,
        dest="top_k",
        help="ネットワーク検索で返す件数（既定: 5）",
    )
    parser.add_argument(
        "-i",
        "--interactive",
        action="store_true",
        help="対話モードを明示的に起動する",
    )
    args = parser.parse_args()

    if args.interactive or not args.query:
        interactive_loop()
    else:
        cli_mode(args)


if __name__ == "__main__":
    main()
