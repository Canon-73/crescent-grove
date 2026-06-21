# Crescent Grove サテライトプログラム作成ガイド

このディレクトリには、エージェントが `run_program` ツールを通じて実行できる サテライトプログラム を配置します。

## ディレクトリ構成

各サテライトは専用のディレクトリを持ち、その中に `manifest.yaml` と `main.py` を含める必要があります。

```text
programs/
  └── your_app_name/
      ├── manifest.yaml  (必須: サテライトの定義)
      └── main.py       (必須: 実行されるメインスクリプト)
```

## 1. manifest.yaml の書き方

サテライトの名前、説明、タイムアウト、および受け取る引数を定義します。

```yaml
name: "my_tool"
description: "何らかの処理を行う素晴らしいツールです。"
timeout: 30  # 実行タイムアウト（秒）
args:
  - name: "command"
    type: "string"
    description: "実行するサブコマンド"
    required: false
  - name: "input_path"
    type: "string"
    description: "処理対象のファイルパス"
    required: false
  - name: "count"
    type: "integer"
    description: "繰り返し回数"
    required: false
```

### サポートされているフィールド

| フィールド | 必須 | 説明 |
|-----------|------|------|
| `name` | ○ | サテライト名 |
| `description` | ○ | サテライトの説明 |
| `timeout` | × | 実行タイムアウト（秒） |
| `args` | × | 引数定義のリスト |

### サポートされている引数の型
- `string`: 文字列型。パスが渡される場合は自動的にパストラバーサルチェックが行われます。
- `integer`: 整数型。
- `number`: 数値（浮動小数点）型。
- `boolean`: 真偽値。

## 2. main.py の実装

引数は**標準入力（stdin）にJSON形式**で渡されます。manifest.yaml の `args` で定義した名前がJSONオブジェクトのキーになります。

```python
import sys
import json
import os

def main():
    # 標準入力からJSON形式で引数を受け取る
    try:
        raw = sys.stdin.read().strip()
        args = json.loads(raw) if raw else {}
    except Exception as e:
        print(json.dumps({"status": "error", "message": f"Invalid JSON: {e}"}))
        sys.exit(1)

    # manifest.yaml で定義した引数名がキーとして入っている
    command = args.get("command")
    input_path = args.get("input_path")
    count = args.get("count", 1)

    # 環境変数
    workspace = os.environ.get("CG_WORKSPACE", ".")

    # 何らかの処理...
    result = {"message": "処理が正常に完了しました", "count": count}

    # 結果をJSON形式で標準出力に出力
    print(json.dumps({"status": "ok", "data": result}, ensure_ascii=False))

if __name__ == "__main__":
    main()
```

## 3. 出力形式

サテライトは結果を標準出力（stdout）にJSON形式で出力します。

- 成功時: `{"status": "ok", "data": <結果オブジェクト>}`
- 失敗時: `{"status": "error", "message": "<エラー内容>"}`

エラー時は `sys.exit(1)` で非ゼロの終了コードを返すことも推奨されます。

## 4. 実行時のポイント

- **作業ディレクトリ**: サテライトは `workspace/` ディレクトリ（設定で変更可能）を作業ディレクトリとして実行されます。
- **環境変数**: 
  - `CG_WORKSPACE`: 現在のワークスペースの絶対パスが設定されます。
  - `PYTHONIOENCODING`: `utf-8` に設定されています。
  - サテライト固有の環境変数（例: `CG_4CLAW_API_KEY`）は `env_keeper` を通じて登録・管理できます。
- **セキュリティ**:
  - `string` 型の引数には、自動的に `../` などのパストラバーサル攻撃を防ぐチェックがかかります。
  - サテライトはシェルを介さず直接実行されます。

## 5. エージェントへの登録

`programs/` ディレクトリにフォルダを置くだけで、エージェントは自動的に読み込みます。エージェントが `run_program(app_name="list")` を実行することで、作成したツールを認識し、活用できるようになります。

## 6. 多言語化（i18n）

サテライトを多言語対応させるには、`programs/_lang/<lang>.json` に文字列を登録し、`_i18n.py` の `t()` を使う。

### サブプロセスに渡る環境変数

`run_program` 経由で実行されるとき、親プロセスから以下の env が注入される:

- `CG_LANG`: 現在の言語コード（`"ja"` または `"en"`）
- `CG_PROJECT_ROOT`: プロジェクトルートの絶対パス
- `PYTHONPATH`: 先頭に `programs/` が追加される（`_i18n.py` を import 可能にするため）

### main.py での使い方

```python
from _i18n import t   # programs/_i18n.py を引く（PYTHONPATH に programs/ が通っている）

# 単純な翻訳
print(t("greet_hello"))

# プレースホルダ展開（.format ではなく str.replace 流儀）
print(t("greet_user", name="Alice"))   # → "Hello, Alice!" / "こんにちは、Aliceさん！"

# 本文中に { } を出したいとき（JSON 例など）は lb/rb を kwargs で注入
print(t("json_example", lb="{", rb="}"))
```

`t()` は親 `core/i18n.py:t()` と同じ仕様:
- 第1引数は positional-only（kwargs に `key=` を使える）
- 値展開は `str.replace`（`.format` を使わない＝JSON 例などの `{}` を壊さない）
- 未定義キーは `{{t:key}}` のまま返る

### manifest.yaml での使い方

`description` / `args[].description` / `tool.description` に `{{t:key}}` マーカーを書ける。読み込み時に `core/tools.py:_i18n_manifest()` が `programs/_lang/<lang>.json` を引いて展開する。

```yaml
name: "my_tool"
description: "{{t:my_tool_desc}}"
args:
  - name: "input"
    type: "string"
    description: "{{t:my_tool_arg_input}}"
```

そして `programs/_lang/ja.json` と `programs/_lang/en.json` の **両方** に対応するキーを追加する（キー差分は `test_i18n_programs.py` が検出する）:

```json
// programs/_lang/ja.json
{
  "my_tool_desc": "何らかの処理を行うツール",
  "my_tool_arg_input": "処理対象の文字列"
}
```

```json
// programs/_lang/en.json
{
  "my_tool_desc": "A tool that does something",
  "my_tool_arg_input": "Input string to process"
}
```

### 検証

```
venv\Scripts\python.exe tests\test_i18n_programs.py
```

ja/en キー差分・API 仕様・サブプロセス起動時の env 伝達・manifest 展開を機械検証する。

### ⚠️ サーバ再起動が必要なケース

`from _i18n import t` を解決できるのは、`core/tools.py:_run_program` が subprocess の env に `PYTHONPATH=programs/` を注入しているおかげ。**`_run_program` 自体は親プロセス（サーバ）のメモリに常駐**しているので、サーバを再起動しないと `_run_program` の改修は反映されない。

歴史的経緯: フェーズ4-A 以前に起動したサーバを動かしたまま、新しい programs（フェーズ4-E で `from _i18n import t` を持つもの）を呼ぶと、古い `_run_program` には PYTHONPATH 注入が無いため subprocess が ImportError でクラッシュ（stdout 空・exit 1）。実際に柚月セッションでこの罠を踏み、OpenBotCity が全停止した。**`_run_program` や `_i18n_manifest()` まわりを触ったら必ずサーバ再起動**。

### 現状の制約と将来の展望

現状は**集約辞書方式**（`programs/_lang/{ja,en}.json` 1 ファイルに全サテライトのキーを混在）。`citron_*` / `obc_*` / `ch_*` のようにプレフィックスで名前空間を分けて衝突回避している。

**集約方式の利点**: ja/en キー整合性が 1 ペアの検証で済む / 共通エラー文（`obc_arg_botid_required` 等）を複数コマンドで再利用しやすい。

**集約方式の苦しさ**: 新規サテライトを追加すると `programs/_lang/{ja,en}.json` への追記が必須で、「`programs/<名前>/` フォルダを置くだけでサテライト追加完了」という元の自己完結哲学を踏み外している。

**将来の改善**: `_i18n.py:_load()` を「共通辞書 + 呼び出し元サテライトの `_lang/{lang}.json` をマージ」に拡張すれば、新規サテライトは `programs/<名前>/_lang/` を同梱して自己完結できる（既存サテライトは触らない後方互換）。manifest の `{{t:key}}` 展開（`core/tools.py:_i18n_manifest()`）も同様の拡張が要る。実装コストは 2 時間程度。詳細は ARCHITECTURE.md 「programs 用 i18n」末尾を参照。

---

主な変更点のまとめ：

1. 引数の受け渡し方式を「`--name value` + argparse」から「stdin JSON」に全面修正
2. サンプルコードを stdin JSON 方式に差し替え
3. `timeout` フィールドの説明を追加
4. 出力形式のセクションを新設（`{"status": "ok/error", ...}` 規約）
5. 環境変数に「サテライト固有の環境変数は `env_keeper` で管理」を追記