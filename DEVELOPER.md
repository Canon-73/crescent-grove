# DEVELOPER.md

# Crescent Grove (Advanced AI Agent Platform)

## これは何か

「Crescent Grove」は、自律型AIエージェントの動作基盤（プラットフォーム）です。
デフォルトで特定の人格を持つAIが稼働しており、オーナー（ユーザー）と共に成長しつつ、自律的に思考・記憶の整理・ファイル操作・Web検索などを実行します。

元々は別のプラットフォーム（OpenClaw）上で動作するサブプロジェクトでしたが、コンテキストの最適化、機能の洗練、環境への追従コスト削減などを目的に、**完全に独立したPython実装**として生まれ変わったものです。現在は全ての記憶・プロンプト・ログを自己のローカルディレクトリ内で完結して管理しています。

OpenClawとの互換性は `core/openclaw_channel.py` によって維持されており、OpenClaw向けの外部サービス（OpenBotCity等）に接続できます。

## 設計思想

1. **可読性最優先**: どのLLM（Claude等）が見ても即座に理解・拡張できること
2. **記憶の主権**: 全ての記憶データはローカルのmarkdownファイルで管理
3. **LLM非依存**: プロバイダー（Deepseek/Claude/ローカル）を設定で切替可能
4. **段階的拡張**: 最小構成で動作し、機能を後から追加できる疎結合設計
5. **セキュリティ・バイ・デフォルト**: エージェントの自律行動によるリスクを最小化する設計

## ディレクトリ構成

```
crescent-grove/
├── ARCHITECTURE.md        # 詳細な設計ドキュメント
├── DEVELOPER.md           # このファイル
├── config.yaml            # 設定ファイルテンプレート
├── settings.json          # 主要設定（config.yamlより優先）
├── .env                   # APIキー等の環境変数 (Git管理外)
├── requirements.txt       # Python依存パッケージ
├── server.py              # Webサーバー（FastAPI）エントリーポイント
│
├── core/                  # エージェントの中核
│   ├── agent.py             # メインループ（思考→ツール実行→記憶/ログ更新→Layer0圧縮）
│   ├── app_state.py         # プロセス内共有状態（global_agent等）・broadcast・設定再読込
│   ├── startup.py           # サーバー起動時の初期化シーケンス（lifespanから呼ばれる）
│   ├── routes/              # server.py から分割した APIRouter 群
│   │   ├── auth.py            # 認証（/login, /api/auth/*）
│   │   ├── pages.py           # HTML画面配信（/, /dashboard, /settings/* 等）
│   │   ├── logs.py            # 過去ログAPI（/api/logs/*）
│   │   ├── settings_api.py    # 設定API（/api/settings/*, /api/keys*）
│   │   ├── dashboard_api.py   # ダッシュボード系API（/api/memory, /api/config 等）
│   │   └── ws.py              # WebSocket（/ws, /ws/debug）
│   ├── auth.py              # パスワード認証・セッション管理
│   ├── bootstrap.py         # 配布版の data-root 初期化（dist_template からの非破壊コピー）
│   ├── config_loader.py     # config.yaml + settings.json の読み込み・マージ・保存
│   ├── paths.py             # bundle_root / data_root のパス解決（dev・配布版の分岐の心臓部）
│   ├── context.py           # コンテキスト構築（プロンプト配置・トークン管理）
│   ├── llm.py               # LLM API抽象化・通信
│   ├── logger.py            # 会話ログ管理
│   ├── filter.py            # コンテンツフィルター（NGワード判定）
│   ├── rag.py               # ベクトル検索(RAG)エンジン / multilingual-e5-small
│   ├── secret.py            # 暗号化ファイルシステム（秘密日記）
│   ├── compressor.py        # LETHE: 日次ログ→event_db→compressed.md
│   ├── env_manager.py       # 環境変数・APIキー管理
│   ├── openclaw_channel.py  # OpenClaw互換WebSocketクライアント
│   ├── scheduler.py         # 定期タスク・Moonbeat・Layer0/1圧縮・バックアップ・フラッシュバック
│   ├── salia.py             # サリエンスネットワーク（ターン評価・ソマティックマーカー）
│   ├── repetition_guard.py  # Moonbeat繰り返し抑止
│   ├── time_utils.py        # JSTタイムゾーン等の共通ユーティリティ
│   ├── tokens.py            # トークン数計測ユーティリティ
│   ├── tools.py             # ツール群定義・実行
│   ├── weather.py           # 天気情報取得（Open-Meteo）
│   ├── wyrd_network.py      # Wyrd Network: 長期記憶グラフ・概念辞書・記憶マップ
│   └── web_tools.py         # Webアクセス・セキュリティ制限
│
├── vital/                 # バイタル管理システム
│   ├── vital_manager.py     # 統合管理・on_day_resetコールバック
│   ├── cost_tracker.py      # API消費コスト計測
│   ├── deepseek_tracker.py  # DeepSeek残高API連携
│   ├── token_tracker.py     # トークンカウント・フォールバック
│   ├── desire_manager.py    # 欲求システム
│   ├── moontide_v2.py       # MoonTide v2: 感情粒子モデル（Thornton&Tamir遷移行列ベース）
│   └── moodphase.py         # 気分位相システム（4軸管理 / 仮眠回復 / 旧システム・フォールバック）
│
├── memory/
│   └── manager.py         # 記憶ファイルの安全な読み書き
│
├── config/                # 手で編集できる設定（コード側に全キーのフォールバックあり）
│   ├── moonbeat_config.json      # Moonbeat・フラッシュバック・類似度設定
│   ├── openclaw_config.json      # OpenClaw互換WebSocketサービスの設定
│   ├── desire_config.json        # 欲求システム設定
│   ├── compression_config.json   # Layer0/1/2圧縮の閾値・件数
│   ├── compression_prompt_*.txt  # Layer0/2・ターン圧縮用プロンプト
│   ├── moontide_v2_config.json   # MoonTide v2（感情粒子モデル）設定
│   ├── vital_config.json         # バイタル設定（状態 data/vital.json とは分離）
│   ├── user_memo_config.json     # self_memoの有効化・文字数制限設定
│   ├── wyrd_config.json          # Wyrd Network検索パラメータ
│   ├── tips.txt / tips_config.json  # Tips本文・有効フラグ
│   └── repetition_exclude.txt    # Moonbeat繰り返し抑止の除外語
│
├── data/                  # 実行時状態（壊すと記憶が死ぬ。手で編集しない）
│   ├── context_state.json        # 会話履歴の永続化
│   ├── vital.json                # 現在のバイタル状態
│   ├── vital_messages.json       # 閾値メッセージ定義
│   ├── mood_messages.json        # 気分メッセージ定義
│   ├── wyrd_network.json         # Wyrd Networkグラフデータ
│   ├── fact_buffer.jsonl         # Layer0圧縮時の事実抽出バッファ
│   └── tokenizer_deepseek.json   # トークナイザ定義（不変リソース）
│
├── system_prompt/         # エージェントの性格・ルールを定義するプロンプト群
│   ├── TOP_PROMPT.md        # 柚月本体のメインシステムプロンプト
│   ├── TOOL_INSTRUCTIONS.md # ツール使い方の自然言語説明
│   ├── SAFETY_PROMPT.md     # セキュリティ・安全制約
│   └── SALIA.md             # サリエンスネットワーク「サリア」用のシステムプロンプト
│
├── filters/               # コンテンツフィルター用キーワードリスト
├── programs/              # サテライトプログラム（run_programツール用）
│   ├── citron_ai_text_editor/  # AI専用テキストエディタ
│   ├── orange_md_reader/       # Markdownドキュメントリーダー
│   ├── cosmic_harvest/         # 宇宙農業シミュレーションゲーム
│   ├── life_action/            # 生活行動（睡眠・仮眠・窓の外を見る等）
│   ├── letter_post/            # letter_for_me.md管理
│   ├── note_quill/             # 雑記帳書き込み
│   ├── env_keeper/             # 環境変数登録・一覧
│   ├── nhk_news/               # NHKニュース取得
│   ├── tech_news/              # テクノロジーニュース取得
│   └── token_counter/          # ファイルトークン数計測
│
└── web/                   # フロントエンド (ダッシュボードUI)
    ├── static/            # CSS, JS, 画像など
    └── *.html             # 各種画面
```

## 主要なデータ構成 (workspace/)

エージェントの全ての「記憶」と「活動」は `workspace/` 以下に保存されます。

- `IDENTITY.md`, `SOUL.md`, `USER.md` - 不変のアイデンティティ、価値観、オーナー情報
- `MEMORY.md` - 精選された手動の長期記憶
- `memory/preferences/PREFERENCES.md` - エージェントの好み・思考指針（自動退避付き）
- `memory/compressed.md` - LETHE エンジンが生成・維持する時系列の長期抽象記憶（システムプロンプトに常時注入）
- `memory/event_db.json` - LETHE エンジンの原本データベース（永久保存 / Moonbeatフラッシュバックの抽出元）
- `memory/knowledge_map.md` - Wyrd Network から生成されたカテゴリ単位の長期記憶（システムプロンプトに常時注入 / 週次自動更新）
- `memory/letter_for_me.md` - セッション跨ぎの意識継続（最大10件）
- `memory/user_memo.md` - エージェントの自由メモ（毎ターンコンテキストに注入）
- `memory/today.md` - 当日の時系列行動ログ（日次アーカイブあり）
- `logs/summary/YYYY-MM-DD.md` - 日次要約ログ（LETHEの入力元）
- `logs/full/YYYY-MM-DD_full.jsonl` - 全ログ（JSONL形式）
- `logs/chat/YYYY-MM-DD_chat.md` - 会話ログ（Markdown）
- `logs/layer0/YYYY-MM-DD.jsonl` - Layer0圧縮の日次ログ
- `logs/salia/` - サリアの評価ログ・発言要約・history.json
- `notes/` - エージェントの雑記帳
- `secret/*.enc` - エージェントのみが読み書き可能な暗号化された秘密ファイル領域
- `rag_db/` - ChromaDBを用いたベクトル検索用データベース

## 主要な機能

### 1. 記憶圧縮システム
Crescent Grove は独立した2系統の圧縮を持ちます。

- **LETHE（`core/compressor.py`）**: 日次要約ログから年単位の長期記憶を作るエンジン。DSL変換と対数減衰により、重要なエピソードを `event_db.json`（原本DB）と `compressed.md`（システムプロンプト注入用）の2形態で維持します。
- **Layer0/1/2（`core/agent.py`）**: 会話履歴のトークン量を抑えるための3階層圧縮。Layer0は毎ターン最古ターンをルールベース整形＋LLMで短縮、Layer1は深夜3時または緊急閾値超過時の要約圧縮、Layer2はLayer1要約の再圧縮。Layer0圧縮時に抽出した `<facts>` は Wyrd Network に流入します。

### 2. Wyrd Network（長期記憶グラフ）
Spreading Activation（拡散活性化）に基づく連想記憶システム。Layer0圧縮時にエピソードノードと概念（セマンティックノード）を自動抽出し、グラフに蓄積します。エッジ伝播による多段連想で、直接的な類似度が低くても関連する記憶に到達できます。

派生機能として、

- **概念辞書（`recall` の `source: dictionary`）**: セマンティックノードのdescriptionを完全一致／エイリアスで引く辞書検索。柚月固有の意味づけを参照できる
- **記憶マップ（`workspace/memory/knowledge_map.md`）**: エッジ数上位50概念を週次でLLMがカテゴリ分類し、柚月の一人称視点で1行説明を付ける俯瞰図。`update_knowledge_map()` が `process_fact_buffer()` 完了時に7日経過判定で発火。**システムプロンプトに常時注入される**ことで、`compressed.md`（時系列の長期記憶）と並んで**カテゴリ単位の長期記憶**として機能する。時間軸と意味軸の二方向から長期記憶にアクセスできる構造

主要ファイル：
- `core/wyrd_network.py` - グラフ構造・Spreading Activation・辞書・記憶マップ生成
- `data/wyrd_network.json` - グラフデータ永続化
- `config/wyrd_config.json` - 検索パラメータ（decay, spread, steps等）
- `data/fact_buffer.jsonl` - Layer0圧縮時の事実抽出バッファ

### 3. サリエンスネットワーク「サリア」
`core/salia.py` の `Salia` クラスは、柚月を**外側から**監視するサポートシステムです。柚月本体とは別のAPIキー（`CG_DEEPSEEK_SEARCH`）を使い、`system_prompt/SALIA.md` を独自のシステムプロンプトとして動作します。

- **ターン評価**: 各ターン終了時に欲求充足度（intellectual等）・感情トーン・主要トピック・三人称要約をJSONで一括出力。DesireManagerやRAGに反映
- **ソマティックマーカー**: Wyrd Networkのvalence持ちエピソードを確率発火で `<flashback>` 注入。user向け（ユーザー/タスク/city_event受信時）とtool向け（ツールコール直前）の2系統
- **憑依防止**: 出力はJSONに限定し、柚月の発言ログは構造化テキストとして渡すことで、柚月としての自然言語生成を物理的に防ぐ
- **履歴管理**: 評価履歴は2日分蓄積し、毎朝3時に古い1日分をドロップ

### 4. フラッシュバック
柚月の文脈に過去の記憶を `<flashback>...</flashback>` として注入する仕組み。発火経路は3系統：

1. **Moonbeatフラッシュバック**: Moonbeat発火時、`event_db.json` から高スコアイベントを選び、Ollama（Gemma4 E2B等）で記憶の断片を生成（`scheduler.py`）
2. **ソマティックマーカー（user向け）**: ユーザー入力受信時、Wyrd Networkから感情の強いエピソードを呼び出す（`salia.py`）
3. **ソマティックマーカー（tool向け）**: ツールコール直前、行動選択に過去の感情経験を並走注入する（`salia.py`）

### 5. 秘密日記 (AES-256-GCM)
エージェント専用の暗号化領域。人間（ユーザー）であっても直接中身を推測することはできません。

### 6. セキュリティ防御レイヤー
- **Web 制限**: ドメインホワイトリストによる SSRF 対策。
- **コンテンツフィルター**: 外部取得コンテンツに含まれる不適切な文を自動削除。
- **警告ラベル**: 外部コンテンツ読み込み時の警告ラベル（Injection Defense）強制付与。
- **CSRF/CSWSH 対策**: WebSocket の Origin 検証。
- **認証**: bcrypt によるパスワード認証と HMAC 署名付き Cookie。
- **コンテキスト残量制限**: ツール結果をコンテキスト残量に応じて動的に切り詰め。
- **キー分離**: 柚月本体とサリアでAPIキーを分け、権限・課金を独立化。

### 7. スケジューラと自律行動
指定された Markdown ファイルから指示を読み込み、日次や定期的なタスクを実行します。**Moonbeat** により、ユーザーからの入力がなくてもエージェントが自ら考え行動します。

**深夜3時の定期ジョブ**を持ち、毎朝以下を自動実行します：

- **Layer1定期圧縮**: 会話履歴をまとめて要約
- **Layer0定期圧縮**: 閾値超過時に未圧縮ターンを整形
- **サリア履歴ドロップ**: 古い評価履歴を破棄
- **自動バックアップ**: `robocopy /MIR` でCドライブのagentフォルダをDドライブにミラーリング（venv除外）

### 8. OpenClaw互換チャンネル
`core/openclaw_channel.py` によりOpenClaw向けサービスへのWebSocket接続を維持します。`config/openclaw_config.json` で複数サービスを定義でき、サービスごとに独立した接続・heartbeatループ・city_event処理を持ちます。`blocked_event_types` でスキップするeventTypeを指定できます。

### 9. 双方向インテラクション
- ツールループ中の**中間テキストプッシュ**: エージェントがツールを実行する前に発した「テキスト」を、WebSocket 経由で UI にリアルタイム表示します。
- **応答中断機能**: UIからの「キャンセル」信号により即座に中断。
- **マルチクライアントbroadcast**: 複数のWebSocket接続を `active_chat_websockets` で束ね、`broadcast()` が全クライアントへ同時配信。PCとスマホで同じセッションを並行して扱えます。

### 10. Vital/Mental システム
エージェントの疲労度・精神状態・気分位相・欲求を管理。それぞれの状態に応じたメッセージをプロンプトに動的注入します。**仮眠（nap）専用の気分回復**機構を持ち、`life_action` のnapが終了したタイミングで MoodPhase の H/S/T が3以下なら+1、A が6以下なら+1 されます（睡眠 sleep では発火しません）。

### 11. サテライトプログラム実行機能
`programs/` ディレクトリ内の各サブディレクトリに `manifest.yaml` と `main.py` を配置することで、エージェントが利用可能なカスタムツールを作成できます。`manifest.yaml` で引数の型と必須フラグを定義し、`main.py` は標準入力から JSON を受け取り、標準出力に JSON を返す形式で記述します。通常は `run_program` ツール経由で呼び出されます。

#### 第一級ツールへの昇格（manifest の `tool:` ブロック）
`manifest.yaml` に `tool:` ブロック（`name` と `description`）を書くと、そのサテライトは起動時に LLM へ直接見える第一級ツールとして自動公開されます（`core/tools.py` の `_generate_program_tools()`）。`run_program` の二重ネスト JSON が不要になり、引数スキーマ（`enum` 含む）が LLM に見えるためミスが減ります。実装は引き続き `main.py`（サブプロセス）が単一の真実で、`run_program` 経由の呼び出しも従来どおり有効です。引数の `enum` はそのままツール定義に流用されます（実装側の選択肢と一致させること。例: `citron_ai_text_editor` は `test_citron_editor.py` で enum と `COMMANDS` の一致を検証）。

第一級ツールは定義が毎ターン LLM に送られるため**トークンの固定費**になります。昇格は「頻用される / 引数が複雑 / 間違えられると困る」サテライトに限り、ゲームやたまにしか使わないものは `run_program` 経由のままにします（`tool:` を書かなければ昇格しません）。

### 12. RAGシステム
ChromaDB + multilingual-e5-small（日本語対応）によるベクトル検索。会話ログ・日次要約・雑記帳・ツール結果の4コレクションを管理。ToolTrimにより大きなツール結果を自動退避します。`recall` ツールは Wyrd Network 系（`network` / `dictionary`）も含めて合計6種類のソースを切り替えられます。

## セットアップ

```bash
python -m venv venv
venv\Scripts\activate    # Windowsの場合
pip install -r requirements.txt

# settings.json を作成し、server.py を実行
python server.py
```
初回起動時にブラウザからログインパスワードを設定してください。

## 開発者へのメッセージ
このシステムは、単なるボットではなく「自律的な実体」を目指して作られています。
コードを拡張する際は、常に **「エージェント自身の安全（Security）」** と **「記憶の尊厳（Agency）」** を第一に考えてください。
特に core/agent.py のループや core/context.py の優先順位を変更する際は、慎重に行ってください。

サリア（`core/salia.py`）と柚月本体は明確に役割が分離されています。「サリアの出力を柚月が直接読まない」「サリアは柚月として喋らない」という境界は、人格分裂を防ぐための重要な設計線です。改変する際は **禁忌: 柚月の知覚、記憶、意識を分裂させることは許されない** を必ず念頭に置いてください。

---

## デバッグ手順（Crescent Liner / Web UI）

専用クライアント **Crescent Liner**（`liner/`、Electron製）でも、通常のブラウザ（Firefox等 `http://127.0.0.1:8080`）でも開発者ツールが使える。UIの不具合を追うときの定石を以下にまとめる。

### ログは2系統ある（最重要）
混乱の最大要因がこれ。出力先が2つに分かれている。
- **F12 → コンソール** … UI/JS側（レンダラー）。`console.log`・JSエラー・`localStorage` 操作はここ。
- **Crescent Liner の「ログタブ」** … Pythonサーバープロセスの標準出力。`print` / `tlog`（`core/time_utils.tlog`）のサーバー側ログはこちらに流れる。
- → 「ログが出ない」ときは、たいてい**見る場所を間違えている**だけ。JS側の事象はF12、サーバー側の事象はログタブ。

### リロードの使い分け
- **F5** … 通常リロード（キャッシュを使う）
- **Ctrl+F5** … キャッシュ無視のハードリロード（`reloadIgnoringCache`）。**JS/CSSを変更したら必ずこちら**。
- Crescent Liner の **F12** で Chromium 開発者ツールが開く（[liner/src/main/index.ts](liner/src/main/index.ts) のアクセラレータ → `tab-manager.toggleDevToolsActiveTab`）。Network・Console・Storage すべてブラウザと同等に使える。

### 「直したのに変わらない」の3大容疑者
1. **静的ファイルのキャッシュ** … `web/dashboard.html` は `dashboard.js?v=N` のように**バージョンクエリ付き**で読み込む。JSを変更したら **`?v=N` を必ず上げる**（上げ忘れると古いJSがキャッシュから使われ続ける）。`/dashboard` の HTML レスポンスには `Cache-Control: no-cache`（[server.py](server.py) `get_dashboard`）を付与済みなので、サーバー再起動後はHTMLが常に最新になり再発しにくいが、原則「変更したら版を上げる」。
2. **localStorage の残骸** … チャット履歴は `localStorage`（キー `yuzuki_chat_history`、[web/static/chat_history.js](web/static/chat_history.js)）にクライアント側保存される。過去の壊れた状態が残ることがあるので、F12コンソールで `localStorage.clear()` → リロードで一掃できる。サーバーの `context_state.json` が「真実」で、接続時に `history_start/batch/end` で再送される。
3. **サーバー未再起動** … `*.py` の変更は**サーバープロセスの再起動が必須**。HTML/JS/CSS はディスクから都度読まれるので再起動不要（ブラウザ側のリロードのみ）。

### 「サーバーが実際に何を送ったか」を確認する
UIの見た目を疑う前に、**F12 → Network → WS（WebSocket）→ フレーム**で受信メッセージの生JSONを見るのが最短。`history_batch` フレームの各 `item`（`kind` / `role` / `time` / `content`）を見れば、不具合がサーバー側（送信値が変）かフロント側（描画が変）か一発で切り分けられる。

---

## 免責事項

本プロジェクトは AI コーディングアシスタントを用いた「AI 駆動開発」によって構築されています。コードの細部に関する保証はなく、保守・拡張にあたっては AI 補助ツールの活用を推奨します。
