# dist_template/data/

配布テンプレートの `data/` 配置場所（プレースホルダ）。

初回起動時、ここに置かれたファイル・ディレクトリは data-root の `data/` に
**コピー先に存在しないものだけ** が展開される（既存ファイルは絶対に上書きしない）。

この段階では中身は空のプレースホルダ。配布キャラ用の不変データ
（tokenizer_deepseek.json, mood_graph.json, mood_transition_matrix.csv,
moontide_inner.jsonl 等）は後続の段階でここに配置する。
