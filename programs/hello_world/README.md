修正後の仕様テキスト全文です：

---

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

---

主な変更点のまとめ：

1. 引数の受け渡し方式を「`--name value` + argparse」から「stdin JSON」に全面修正
2. サンプルコードを stdin JSON 方式に差し替え
3. `timeout` フィールドの説明を追加
4. 出力形式のセクションを新設（`{"status": "ok/error", ...}` 規約）
5. 環境変数に「サテライト固有の環境変数は `env_keeper` で管理」を追記