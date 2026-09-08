# search_wyrd.py 使い方

柚月の長期記憶ネットワーク（Wyrd Network）をコマンドラインから検索する**読み取り専用**ツールです。
サーバ本体の `recall` ツールと同じ検索ロジック・同じembeddingモデル・同じ設定を使うので、
柚月がサーバ内でやる検索とほぼ同じ結果が出ます。

## 一番かんたんな使い方（ダブルクリック）

プロジェクト直下の **`search_wyrd.bat` をダブルクリック**するだけ。
venv の Python で対話モードが立ち上がり、メニューが出ます。

```
モードを選んでください:
  1) ネットワーク検索（関連する記憶エピソードを探す）
  2) 辞書検索（概念ノードの説明を引く）
  q) 終了
> 1
検索クエリを入力（複数語は空白区切り）: カノン 配信
表示件数（Enter で 5件）:
```

1 か 2 を選んでクエリを打つと結果が出て、またメニューに戻ります。
`q` で終了。何度でも続けて検索できます（モデルの読み込みは最初の1回だけ）。

## 前提

- **必ず `venv` で実行**すること（faiss / sentence-transformers / numpy が要る）。
  バッチ経由ならこれは自動で満たされます。
- 既定の `python`（3.9）はNG。
- サーバの起動・停止は不要。柚月を止めずにそのまま使えます。

## 検索モードは2つ

柚月が使えるWyrd検索と1:1で対応しています。

| モード | 何を検索する | 柚月の `recall` |
|---|---|---|
| ネットワーク検索（既定） | 関連する記憶エピソード（spreading activation） | `source=network` |
| 辞書検索（`--dict`） | セマンティックノード（概念）の説明 | `source=dictionary` |

## コマンドラインから使う（上級者向け・任意）

対話モードを使わず、引数で一発検索することもできます。

### 1. ネットワーク検索（記憶エピソードを探す）

```bash
venv/Scripts/python.exe scripts/search_wyrd.py "カノン 配信"
```

- 複数語は空白区切りでOK（各語でもアンカー検索して結果を合成します）。
- `-n` で件数を変更（既定5件）:

```bash
venv/Scripts/python.exe scripts/search_wyrd.py "ブログ 執筆" -n 10
```

出力には各エピソードの日付・関連度（energy）・感情値（valence）・本文と、末尾に関連概念が並びます。

### 2. 辞書検索（概念ノードの説明を引く）

```bash
venv/Scripts/python.exe scripts/search_wyrd.py --dict "カノン"
```

- label / 別名（alias）の完全一致 → その概念の説明・接続エッジ数・別名を表示。
- 一致しない場合 → 近い概念を上位5件サジェストするので、その語で引き直してください。

## 安全について（重要）

- このツールは**読むだけ**。検索結果をグラフに書き戻す `save_graph` は呼びません。
  柚月のライブデータ（`data/wyrd_network.json`）と競合させないためです。
- そのため柚月の記憶やアクセス履歴を一切変更しません。安心して何度でも実行できます。

## トラブル時のヒント

- `BertModel LOAD REPORT ... embeddings.position_ids UNEXPECTED` という行が出ますが、
  これはモデルロードの正常な情報メッセージで、検索結果に影響しません。
- `ModuleNotFoundError`（faiss 等）が出たら、`venv` ではなく素の `python` で動かしていないか確認。
- 日本語が文字化けする場合も中身は正しく検索できています（コンソールのエンコーディング表示の問題）。

## 関連ファイル

- 本体スクリプト: `scripts/search_wyrd.py`
- 検索設定: `config/wyrd_config.json`（`search` セクション）
- 検索ロジック: `core/wyrd_network.py`（`search_memory` / `search_concept`）
