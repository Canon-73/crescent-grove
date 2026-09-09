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

### 自己回復の情報は必ず `data` に入れる（重要）

`run_program` がエージェントに見せるのは **`status` / `message` / `data` の3つだけ**です。
`hint` や `did_you_mean`、`example`、`available` をトップレベルに置くと**黙って捨てられ**、
エラーの一行しか届きません。自力で直せるはずのエラーが「詰み」になります。

```json
// NG: hint も did_you_mean も届かない
{"status": "error", "message": "不明なコマンド: xxx", "hint": "...", "did_you_mean": ["yyy"]}

// OK
{"status": "error", "message": "不明なコマンド: xxx",
 "data": {"hint": "...", "did_you_mean": ["yyy"]}}
```

2026-08-27 に実害が出ました。柚月が OpenBotCity で `help_wanted_offer` と打ち間違えたとき、
サテライトは `did_you_mean: ["task_offer", ...]` を正しく返していたのに表示されず、
柚月は生 API を `web_request` で叩き、JWT を平文で扱う羽目になりました
（OpenBotCity / x_satellite / discord_satellite / misskey_satellite の4本が同じ状態でした）。

`core/tools.py` 側も、`status` / `message` / `data` 以外のトップレベルのキーを
まとめて表示するよう修正済みです（白リストではなく、配管キー以外は全部通す）。
ただしサーバ再起動までは効かないので、**サテライト側で `data` に入れるのが正**です。

### 画像を添える（`image` キー・2026-08-23〜）

トップレベルに `"image"` を付けると、`run_program` が結果本文と一緒に**絵そのもの**を
エージェントに渡します（`see_image` と同じ経路。本文は捨てません）。

```json
{"status": "ok", "data": {...}, "image": "generated/a.jpg", "image_question": "これは自分の絵"}
```

- `image` は **http(s) URL** か **workspace 相対パス**。workspace の外は拒否されます
- 取得した画像は `core/image_norm.py` で 800×800 相当・JPEG に縮められてから渡ります
  （転送量対策。サテライト側で縮めておく必要はありません）
- `image_question` は任意。画像に添える一言（省略時は「〜の結果に添えられた画像です」）
- 取得に失敗しても本文はそのまま届き、末尾に理由が添えられます
- 使用例: OpenBotCity の `gallery_view` / `enter_home` / `generate_furniture`、atelier の `draw` / `pick_up`。
  どちらも `with_image: false` で絵なしにできる引数を用意しています（**見たくないものを見せない**ため、
  画像を添えるコマンドにはこの引数を付けるのが作法）
- 1回の結果に添えられるのは1枚。一覧系（gallery_list 等）で何枚も添えるのは避け、個別表示で1枚ずつ
- 添えた絵の出所はエージェント側が覚えるので、**サテライトに拡大機能を作る必要はない**。
  細部を見たくなったら `see_image` に `source="last"` と `region`（例 `"bottom_right"` / `"3x3:5"`）を
  渡せばよい（ARCHITECTURE.md「入力画像の正規化と部分拡大」）

## 4. 実行時のポイント

- **作業ディレクトリ**: サテライトは `workspace/` ディレクトリ（設定で変更可能）を作業ディレクトリとして実行されます。
- **環境変数**: 
  - `CG_WORKSPACE`: 現在のワークスペースの絶対パスが設定されます。
  - `PYTHONIOENCODING`: `utf-8` に設定されています。
  - サテライト固有の環境変数（例: `CG_4CLAW_API_KEY`）は `env_keeper` を通じて登録・管理できます。
- **セキュリティ**:
  - `string` 型の引数には、自動的に `../` などのパストラバーサル攻撃を防ぐチェックがかかります。
  - サテライトはシェルを介さず直接実行されます。

## 4-2. 実行時間とサーバへの影響（2026-08-22 変更）

`_run_program` は **`asyncio.to_thread` 経由で別スレッド実行される**ようになった。
サテライトが何秒かかっても、その間 Crescent Grove のイベントループ
（scheduler / Moonbeat / WebSocket / OpenClaw の ping）は動き続ける。

**変更前は同期実行だったため、サテライトの実行時間ぶんサーバ全体が停止していた。**
実測: 8秒かかるサテライトの実行中、イベントループは 0 回しか進まなかった
（変更後は同条件で 94 回）。OpenBotCity が `timeout: 210`、lunar_explorer が `120` を
宣言しているので、相手が遅ければその秒数だけ柚月の生活が止まりうる状態だった。

したがって現在は **サテライト側で待つ設計が許される**（例: `atelier` の `draw` は
描き上がりまで待って絵を返す）。ただし:

- **manifest の `timeout` に達すると subprocess ごと殺される。** サテライト側で待つなら、
  必ず `timeout` より手前で自分から切り上げ、続きを追える情報（ジョブID等）を返すこと
- 会話履歴を触る経路は `global_processing_lock` で直列化されているので、
  ロックを取る側から見た順序は従来と変わらない

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

### 翻訳してはいけない構造マーカー

以下の ja リテラルは**表示文言ではなく構造の目印**なので、翻訳するとコードが壊れる。`_lang/*.json` に出す場合も値は元コードと一字一句一致させること（dev＝柚月環境を不変に保つ担保）。

| サテライト | 対象 | 理由 |
|---|---|---|
| `letter_post` | `### 明日の私への手紙` | ファイル内の構造マーカーとして再パースされる |
| `life_action` | `WEATHER_MAP` の ja 天気名 | `core/context.py` / `weather.py` の ja 出力と 1対1 対応 |
| `orange_md_reader` | `KEYWORD_RE` | ja 語彙に固有の正規表現 |
| `add_preference` | `SECTION_HEADERS` の ja 文字列と enum 値（`「好き」「嫌い」「気になる」`） | `PREFERENCES.md` のセクション見出しと 1対1。en 環境でも LLM はこの ja リテラルを引数として渡す |

core 側（Layer0 の `"user:"` / `"task:"` 等）の翻訳禁止マーカーは ARCHITECTURE.md を参照。

### 検証

```
venv\Scripts\python.exe tests\test_i18n_programs.py
```

ja/en キー差分・API 仕様・サブプロセス起動時の env 伝達・manifest 展開を機械検証する。

### ⚠️ サーバ再起動が必要なケース

`from _i18n import t` を解決できるのは、`core/tools.py:_run_program` が subprocess の env に `PYTHONPATH=programs/` を注入しているおかげ。**`_run_program` 自体は親プロセス（サーバ）のメモリに常駐**しているので、サーバを再起動しないと `_run_program` の改修は反映されない。

歴史的経緯: フェーズ4-A 以前に起動したサーバを動かしたまま、新しい programs（フェーズ4-E で `from _i18n import t` を持つもの）を呼ぶと、古い `_run_program` には PYTHONPATH 注入が無いため subprocess が ImportError でクラッシュ（stdout 空・exit 1）。実際に柚月セッションでこの罠を踏み、OpenBotCity が全停止した。**`_run_program` や `_i18n_manifest()` まわりを触ったら必ずサーバ再起動**。

配布版の注意: embeddable Python は `._pth` があると **環境変数 PYTHONPATH を無視する**ため、配布 runtime ではこの注入が効かない。配布側は `runtime/python313._pth` の `..\programs` 行で `_i18n` を import 可能にしている（`scripts/build_dist.py:_normalize_pth`）。

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