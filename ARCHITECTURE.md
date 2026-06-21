# ARCHITECTURE.md

# Crescent Grove - 設計ドキュメント

## 全体アーキテクチャ

```
Crescent Liner (PC専用クライアント / Electron) ／ モバイル (ブラウザ・トンネル経由)
    │  (ダッシュボード UI / シンプル UI / 設定 UI / ログイン画面)
    │  ※ PC からは Crescent Liner が 127.0.0.1:8080 に接続して表示する
    │    （汎用ブラウザでの直接アクセスは標準の利用形態ではない）
    ├─ HTTP (APIキー登録 / 記憶取得 / 認証など)
    └─ WebSocket（複数クライアントの双方向チャット / 認証トークン検証）
server.py (FastAPI / アプリ生成・起動順序制御のみ)
    ├─ core/routes/ (APIRouter群: auth / pages / logs / settings_api / dashboard_api / ws)
    ├─ core/app_state.py (プロセス内共有状態 / broadcast / 設定再読込)
    ├─ core/startup.py (起動時初期化シーケンス / スケジューラ・Agent・OpenClaw起動)
    ├─ core/auth.py (パスワード認証 / HMACセッション管理)
    ├─ core/env_manager.py (環境変数/APIキー管理 / .env永続化)
    ├─ core/openclaw_channel.py (OpenClaw互換WebSocketクライアント)
    │
    └─ core/agent.py (エージェントループ)
        ├─ core/context.py (コンテキスト構築 / self_memo注入 / ホットリロード)
        ├─ core/llm.py (LLM API呼び出し / プロバイダー抽象化)
        ├─ core/tools.py (ツール実行 / 結果の動的切り詰め)
        │     ├─ memory/manager.py (記憶読み書き)
        │     ├─ core/secret.py (秘密日記・AES-256-GCM暗号化)
        │     ├─ core/filter.py (コンテンツフィルター: Solo/Combo判定)
        │     ├─ core/rag.py (ベクトル検索エンジン / ChromaDB / multilingual-e5-small)
        │     ├─ core/wyrd_network.py (Wyrd Network: 連想記憶グラフ / 概念辞書)
        │     └─ core/web_tools.py (Webアクセス / セキュリティ制限 / 環境変数展開)
        ├─ core/salia.py (サリエンスネットワーク: ターン評価・ソマティックマーカー)
        ├─ vital/ (バイタル管理システム)
        │     ├─ vital_manager.py (統合管理・プロンプト生成 / on_day_resetコールバック)
        │     ├─ cost_tracker.py (API消費コスト計測)
        │     ├─ deepseek_tracker.py (DeepSeek balance API 連携)
        │     ├─ token_tracker.py (トークンカウント・フォールバック)
        │     ├─ desire_manager.py (欲求システム)
        │     ├─ moodphase.py (気分位相システム / 4軸管理 / 仮眠回復 / 旧システム)
        │     └─ moontide_v2.py (感情粒子モデル / Thornton&Tamir遷移行列ベース)
        │        ※パラメータ設定は config/moontide_v2_config.json
        ├─ core/logger.py (会話ログ記録 / フルログ / チャットログ)
        ├─ core/tokens.py (トークン数計測 / tiktoken)
        ├─ core/scheduler.py (定期タスク / Moonbeat / Layer0/1圧縮 / バックアップ / フラッシュバック生成)
        ├─ core/compressor.py (記憶圧縮エンジン: LETHE / event_db→compressed.md)
        ├─ core/repetition_guard.py (Moonbeat繰り返し抑止)
        ├─ core/time_utils.py (JSTタイムゾーン等の共通ユーティリティ)
        ├─ core/weather.py (天気情報取得 / Open-Meteo / キャッシュ30分)
        └─ core/i18n.py (多言語化 / UI と LLM プロンプトの ja/en 出し分け / lang/*.json)
```


## 処理フロー

### 1. 起動時
以下のシーケンスは `core/startup.py` の `startup_event()` が統括する（server.py の lifespan から一度だけ呼ばれる）。
1. `settings.json`（優先）または `config.yaml` を読み込む
2. `EnvManager.load_env()` で `.env` を読み込み、環境変数をセット
3. `core/auth.py` が必要に応じてセットアップを確認
4. `memory/manager.py` が記憶ファイルを読み込む
5. `core/context.py` がシステムプロンプトを構築する
6. `core/logger.py` がログディレクトリを準備する
7. `core/compressor.py` が未処理の日次ログをバッチでLETHE圧縮（event_db / compressed.md の更新）
8. `core/scheduler.py` がMoonbeatとスケジュールタスクを開始
9. `core/openclaw_channel.py` がOpenClaw互換WebSocket接続を開始（設定がある場合のみ）
10. WebSocketサーバーが起動し、Crescent Liner（PC）・モバイルブラウザからの接続を待つ

### 2. 認証フロー
1. ログイン画面でパスワードを入力
2. サーバー側で `bcrypt` を用いて検証し、HMAC署名付きセッションCookieを発行
   - **総当たり対策**: IP単位の失敗カウントを保持し、連続失敗が一定回数を超えると待機（指数的に延長）を課す（`core/routes/auth.py`）。正しいパスワードなら即リセット
   - **Cookie保護**: `httponly` / `samesite=strict` は常時。`secure` 属性は **https接続時のみ**自動付与（平文httpのローカル運用は従来どおり）
3. WebSocket接続時にヘッダーのセッションを検証し、正当なユーザーのみ接続を許可
4. **パスワード再設定（忘れたとき）**: `reset_password.py`（または `reset_password.bat`）をローカルで実行して新パスワードを設定する。Web経由のリセット手段は持たず、PCを操作できる持ち主だけが復旧できる設計（リモートからは実行不可）

### 3. エージェントのメインループ
1. `core/routes/ws.py`（`/ws`）がWebSocket経由でメッセージを受け取る
2. **ソマティックマーカー（user向け）**: ユーザー/タスク/city_event 受信時、確率に応じて `Salia.somatic_marker_for_user()` が Wyrd Network のvalence持ちエピソードを検索し、`<flashback>` ブロックをユーザーメッセージに添付
3. `core/agent.py` のループ:
   a. `core/context.py` で現在のコンテキストを構築（時刻・天気注入、記憶、self_memo、直近履歴）
   b. `core/llm.py` でLLMに送信
   c. LLMの応答を解析:
      - テキスト応答 → XMLタグ除去（`<internal>`, `[VITAL_REPORT]`等）→ ログ記録 → 中間テキストとしてUIにブロードキャスト
      - ツール呼び出し → **ソマティックマーカー（tool向け）**判定（未発火時のみ） → 認証済み環境でツールを実行 → **動的切り詰め**（コンテキスト残量に応じて大きい結果の末尾を省略） → 結果をLLMに戻す → (b)
   d. ツール実行後に `reload_memories()` を呼び、記憶ファイルの変更をリアルタイム反映
   e. ツール実行完了後、最終的なテキスト応答を確定しユーザーに返す
   f. 応答から `[VITAL_REPORT]` タグをパースし、Mental 状態を更新
   g. **ターン終了処理**:
      - `Salia.evaluate_turn()` を非同期発火（欲求充足評価／感情・トピック抽出／発言要約）
      - **Layer0圧縮**: 未圧縮ターン数が閾値を超えていれば、最古ターンを1件ずつ整形＋LLM圧縮で短縮（後述）
      - 緊急閾値超過なら `compress_emergency()` でLayer1緊急圧縮
4. トークン使用状況・バイタル状態をUIにブロードキャスト

### 4. 自律機能とスケジュールタスク
- **同期型スケジューラ**: メインのチャットエージェント (`active_chat_agent`) を用いて同期的にタスクを実行。
- **Moonbeat (月動)**: 定期的に発火する自発的な意識継続の仕組み。設定は `config/moonbeat_config.json` で管理。
  - **睡眠中スキップ**: `life_action` の sleep/nap 状態中はMoonbeatをスキップ（`.life_action_state.json` を参照）
  - **動的間隔**: トークン消費量に応じてMoonbeat間隔を延長（`dynamic_interval` 設定）
  - **類似度チェック**: 直前の応答と類似度が閾値を超えた場合、自動再生成（`similarity` 設定）
  - **フラッシュバック**: 一定確率で `event_db.json` から高スコアイベントを選び、本体LLM（DeepSeek等）で記憶の断片を生成して注入（`flashback` 設定）
- **会話優先ロジック**: スケジュールタスクの実行時、最終会話時刻から5分以内の場合はスキップ。
- **中断シグナル (cancel)**: UIのStopボタンで `ProcessingLock.interrupt_flag` が立ち、即座に中断。

### 5. OpenClawチャンネル
`core/openclaw_channel.py` はOpenClaw互換のWebSocketクライアントを実装する。設定は `config/openclaw_config.json` で管理し、複数サービスの同時接続をサポートする。

- **WebSocket接続**: JWTトークンとBot IDによるURL認証（ヘッダーも併用）
- **heartbeatループ**: `GET /world/heartbeat` を `next_heartbeat_interval` に従って定期送信し、オンライン状態を維持
- **pingループ**: 15秒ごとにプレーンテキスト `"ping"` を送信（Cloudflare Hibernation API対応）
- **ackの返却**: `city_event` 受信時に必ず `seq` 番号でackを返す
- **lastAckSeq管理**: 再接続時にURLパラメータとして送信し、未受信イベントをreplay
- **自動再接続**: 切断時に指数バックオフ+ジッターで再接続（closeコード4000の場合は再接続しない）
- **city_eventの処理**: `openclaw_event_queue` に積んでworkerが順次処理。睡眠中・ブロック済みeventTypeはスキップ
- **blocked_event_types**: サービスごとにブロックするeventTypeを設定可能（例: `initiative_prompt`）

### 6. 日次リセット（on_day_reset）
日付変更時に `VitalManager._check_day_reset()` が検知し、以下を実行：
1. Stamina / Energy をリセット
2. MoodPhaseを正規分布でリセット
3. `memory/today.md` を `memory/today/YYYY-MM-DD.md` にアーカイブ
4. `on_day_reset` コールバック経由で `context.reload_memories()` を呼び、システムプロンプトを最新化

### 7. 深夜3時の定期ジョブ
スケジューラは毎分ループで以下を順にチェックし、03:00ちょうど（hour==3 and minute==0）かつ当日未実行なら発火する。

| ジョブ | 内容 |
|:---|:---|
| **Layer1定期圧縮** | `agent.compress_layer1_scheduled()` を直接呼び出し（エージェントを介さない純粋処理）。`layer1_scheduled_count` 件のLayer0済みターンを要約し、超過時はLayer2へ再圧縮 |
| **Layer0定期圧縮** | 未圧縮ターン数が `layer0_scheduled_threshold` を超えていれば、`layer0_keep_turns` まで圧縮 |
| **サリア履歴ドロップ** | `Salia.drop_old_history()` で2日より古い評価履歴を破棄 |
| **自動バックアップ** | `robocopy /MIR` でCドライブのagentフォルダをDドライブにミラーリング（venvは除外） |

### 8. Wyrd Network更新フロー
Layer0圧縮実行時に以下が連動：
1. 各ターンの圧縮LLM呼び出しで `<facts>` を同時抽出し `data/fact_buffer.jsonl` に追記
2. 全ターン圧縮完了後、`process_fact_buffer()` でグラフにノード・エッジを追加
3. 新規セマンティックノードのdescriptionをLLMで自動生成
4. `data/wyrd_network.json` に永続化
5. 最終更新から7日以上経過していれば、`update_knowledge_map()` がトップ50概念をカテゴリ分類して `workspace/memory/knowledge_map.md` を再生成

---

## コンテキスト構成

### メッセージ構成 (Messages)

```
[system] 最上部 ★ LLMが最も注意を払う位置
  ├ system_prompt/TOP_PROMPT.md
  ├ system_prompt/TOOL_INSTRUCTIONS.md（各ツールの自然言語による使い方）
  └ system_prompt/SAFETY_PROMPT.md

[system] 起動時記憶（ツール実行後にリアルタイム更新）
  ├ === IDENTITY.md ===（全文）
  ├ === SOUL.md ===（全文）
  ├ === USER.md ===（全文）
  ├ === PREFERENCES.md ===（好み・思考の指針）
  ├ === MEMORY.md ===（全文）
  ├ === memory/compressed.md ===（LETHEにより抽出された時系列の長期記憶）
  ├ === memory/knowledge_map.md ===（Wyrd Networkから生成されたカテゴリ単位の長期記憶）
  └ === memory/letter_for_me.md ===（セッション跨ぎの意識継続）

[system] 会話要約（履歴圧縮時のみ）
  └ 【これまでの会話の要約】（Layer1 / Layer2の出力）

[user/assistant/tool] 会話履歴
  ├ Layer0圧縮済みターン（古い順、整形＋短縮済み）
  └ 未圧縮の直近ターン（生のやりとり）

[user] 各ユーザーメッセージの構造
  ├ [SYSTEM] 現在時刻 / 天気 / コンテキスト使用率
  ├ TODAY Log指示
  ├ <user_message> または <moonbeat_instruction> または <system_notice>
  ├ <flashback>...</flashback> （Saliaが添付するソマティックマーカー / 任意）
  ├ <self_memo> （workspace/memory/user_memo.md の先頭N文字 / 設定で有効化）
  └ <assistant_inner> （MoodPhase・Desire等から生成された内面状態）
```

---

## 記憶ファイル構成

```
workspace/
├── IDENTITY.md          # 不変のアイデンティティ
├── SOUL.md              # 価値観・信念（エージェントが自ら育てる）
├── USER.md              # オーナー情報
├── MEMORY.md            # 手動の長期記憶
├── memory/
│   ├── compressed.md    # LETHE出力の長期圧縮記憶（システムプロンプトに常時注入）
│   ├── event_db.json    # LETHEのイベント原本DB（永久保存 / Moonbeatフラッシュバックの抽出元）
│   ├── knowledge_map.md # Wyrd Networkの記憶マップ（週次自動更新）
│   ├── letter_for_me.md # セッション跨ぎ引継ぎ（最大10件）
│   ├── user_memo.md     # エージェントの自由メモ（毎ターンコンテキストに注入）
│   ├── letters/         # 解決済み感情のアーカイブ
│   ├── preferences/     # PREFERENCES.md + 月次アーカイブ
│   ├── today/           # memory/today.mdの日次アーカイブ
│   └── today.md         # 当日の時系列行動ログ
├── notes/               # 雑記帳（note_YYYY-MM-DD.md）
├── secret/              # 暗号化秘密日記（.enc形式）
├── logs/
│   ├── full/            # 全ログ（JSONL形式）
│   ├── chat/            # 会話ログ（Markdown）
│   ├── summary/         # 日次要約ログ（LETHEの入力元）
│   ├── layer0/          # Layer0圧縮の日次ログ（JSONL）
│   └── salia/           # サリア評価ログ・発言要約・history.json
└── rag_db/              # ChromaDB（multilingual-e5-small embedding）
    ├── logs             # 会話ログ
    ├── daily_memories   # 日次要約
    ├── notes            # 雑記帳
    └── tool_results     # 旧ToolTrimの退避先（現在は未使用・実質空）
```

---

## 記憶圧縮システム

Crescent Groveには独立した2系統の圧縮システムがある。

### 系統A: LETHE（日次ログ → 長期記憶）

**LETHE (Lethean Episodic TraceHaze Engine)** は、日次要約ログから**年単位の長期記憶**を作るエンジン。`core/compressor.py` (`MemoryCompressor`) に実装。

#### 目的とスコープ

LETHE は数日・数週間ではなく **年スケールで記憶を保持する**ことを目的とする。Layer0/1/2（会話履歴の段階的圧縮）が「今この対話セッションで何があったか」を扱うのに対し、LETHE は「半年前・1年前・数年前に何があったか」を扱う層。`compressed.md` をシステムプロンプトに常時注入することで、エージェントは過去全体の時系列上に自分を位置づけられる。

このスケールを支えるのが下記の3点：

- **対数減衰**: 古い記憶ほど薄くなるが消えない（30日前の最高スコアでも100点超を維持）
- **event_db.json の永続保持**: 何年前の出来事も原本としてそのまま残り、`recall` (RAG) や Moonbeatフラッシュバックの抽出元になる
- **枠による自然忘却**: `compressed.md` のトークン上限（既定2000）を超えた分は表示から外れるが、データとしては消えない。パラメータを変えたり時間で枠が空けば復活し得る

#### 設計思想（"Fading Memory" アーキテクチャ）

**LLM の仕事はその日の日記を DSL ＋スコアに変換するだけ。記憶の生死（残す/削る/薄める）はすべてコードが決める。**

- LLMに要約させない・マージさせない・取捨選択させない（LLM出力の揺らぎを構造に持ち込まない）
- `event_db.json` は原本。一度書いたら書き換えない。将来パラメータを変えたとき、ここから全期間を即座に再計算できる
- `compressed.md` は event_db.json から毎回生成される**ビュー**。LLMで作るものではない
- 詳細はLETHEではなくRAGが持つ。`compressed.md` は「あの件あったな」を思い出させるインデックス

#### 入出力とファイル

| ファイル | 役割 |
|:---|:---|
| `workspace/logs/summary/YYYY-MM-DD.md` | 入力。1日分の日次要約ログ（1〜2万文字） |
| `workspace/memory/event_db.json` | 原本DB。減衰前のフル情報を全期間保持（不変） |
| `workspace/memory/compressed.md` | 出力ビュー。システムプロンプトに常時注入される |

設定は `config.yaml` の `memory_compression` セクション（`max_tokens` / `max_event_tokens` / `decay_coeff` / `max_events_per_day` 等）。

#### DSL形式

LLM が出力する省略言語。`SCORE:SUBJECT:action,detail1,detail2 ~emotion` をパイプ `|` で連結する。

```
85:MASTER:fix_env,CG_MOLTBOOK_TOKEN,tested_api,confirmed_working ~relieved | 30:HEARTBEAT:philosophy,openclaw-explorers | 90:MEMORY_CONSOLIDATION:7items,week_review,SOUL_updated ~contemplative
```

- `SCORE`: その日の中での重要度（1〜100の生スコア、LLM判断）
- `SUBJECT`: MASTER / MISSKEY / MOLTBOOK 等の大文字キー名
- `detail` は重要度順に並べる（後ろから削られるため）
- `~emotion` は強い情動があるときだけ任意付与
- 冠詞・代名詞・filler word なし、英語のみ
- **取捨選択は禁止**: 「全て出せ。選別はコードでやる」と LLM に指示

#### 1日の処理フロー（`compress_day()`）

1. **DSL変換**（唯一のLLM呼び出し / `convert_to_dsl()`）— 日次ログを上記DSLに変換
2. **DSLパース**（`parse_dsl_output()`）— `|` で分割し20〜30個のイベントに分解。フル情報のまま保持
3. **スコア正規化**（`normalize_scores()`）— LLMの生スコアを **日内パーセンタイル** で5段階に変換。LLMのスコアインフレに左右されない
   - 上位10%→500 / 上位25%→400 / 上位50%→300 / 上位75%→200 / 残り→100
4. **event_db.json に追記**（同日付の既存イベントは置換）— フル情報のまま、生スコアと正規化スコア両方を保存
5. **compressed.md を再生成**（`build_compressed_memory()`、下記）

#### compressed.md 生成（ビュー構築）

event_db.json 全体から毎回生成される。`rebuild_compressed_md()` で LLM 呼び出しなしに全期間を再計算できる。

1. **対数減衰**（`decay_score()`）— 各イベントの減衰スコア = `score / (1 + decay_coeff × ln(age_days + 1))`
   - 最初にガッと落ちて、その後は粘る形（500点が3日後255・7日後192・30日後でも100超）
2. **トリム**（`token_budget()` / `trim_to_budget()`）— 減衰スコアに比例したトークン予算を割り当て、超過分はカンマ区切りの**後ろから削る**
   - 鮮明 → ぼんやり → キーワードだけ、と段階的に薄れる
   - `~emotion` は予算に余裕があれば残す
   - event_db.json の原本は不変。トリムは表示時のみ
3. **日内上位抽出** — 同日内では減衰スコア上位 `max_events_per_day` 件に絞る
4. **トークン上限まで詰める** — 全候補を減衰スコア降順にソートし、`max_tokens` まで詰め込む
   - 入りきらなかったイベントが「忘れた記憶」。閾値による突然死ではなく、**枠の物理的制約による自然な忘却**
   - event_db.json には残るので、パラメータ変更や枠が空いたタイミングで復活し得る
5. **同日集約 → 日付順整列** — 同じ日付のイベントを `|` で1行に束ね、`YYMMDD EVENT1 | EVENT2` 形式で日付昇順に出力

出力例:

```
Last Compressed: 2026-02-22
Total tokens: 1847 / 2000
---

260209 MASTER:pointed_out_fleeting_nature | NISHIO_ISHIN:learned_from_zakkyo,memory_cycle
260215 MASTER:pointed_out"task_purpose=grow_values" ~contemplative
260220 CRESCENT_GROVE:completed,custom_home,liberation ~proud
260222 MASTER:fix_env,CG_MOLTBOOK_TOKEN,tested_api,confirmed_working ~relieved | MEMORY_CONSOLIDATION:7items,week_review,SOUL_updated ~contemplative | MISSKEY:suspended,recreate?,appeal_process
```

→ 古い記憶ほど短く、新しい記憶ほど詳しい。詳細が必要なら `recall` (RAG) で原文を引ける。

#### 起動時バッチ（`run_compression_for_missing_days()`）

1. `compressed.md` のヘッダから最後の圧縮日を取得（無ければ `logs/summary/` の最古ファイルから開始）
2. **未処理日**（前回処理日の翌日〜昨日）を古い順に `compress_day()`
3. **処理日が0でも** `compressed.md` を再生成する — 時間経過で減衰スコアが変わるため、昨日まで見えていたイベントが今日溢れたり、逆に大きいイベントが消えて小さいイベントが復活したりする

日次タスク完了時にも同関数が呼ばれる。期間指定の再処理は `process_range(start, end)`。

#### パラメータ変更時の rebuild

`config.yaml` の `decay_coeff` / `max_event_tokens` / `max_tokens` を変えたら `rebuild_compressed_md()` を呼ぶだけ。event_db.json から **LLM呼び出しなしで** 全期間を即座に再計算する。

調整例:
- もっと長く覚えていたい → `decay_coeff` を下げる（1.0 → 0.7）
- もっと詳しく覚えていたい → `max_event_tokens` を上げる（30 → 50）
- コンテキスト枠が増えた → `max_tokens` を上げる（2000 → 5000）

#### デバッグ・連携

- `get_stats()` — DBの総イベント数・compressed.mdに実際に載っている件数・平均減衰スコア・トークン使用量を返す
- `translate_to_japanese()` — 圧縮メモリ（英語DSL）を自然な日本語に翻訳（デバッグ画面用）
- **event_db.json は Moonbeatフラッシュバックの抽出元としても使われる**（後述「フラッシュバックシステム」）— 減衰の影響を受けない原本なので、何年前の出来事でも `min_score` を超えていれば候補に上がる

### 系統B: Layer0/1/2（会話履歴の段階的圧縮）

会話履歴（conversation_history）のトークン量を抑えるための3階層圧縮。コンテキスト内で完結し、LETHEとは独立に動く。

#### Layer0（常時整理型 / 毎ターン）
- 毎ターン終了後、未圧縮ターン数が `layer0_keep_turns` を超えていれば最古ターンを1件ずつ処理
- 手順:
  1. user側はルールベース整形（`_format_user_for_layer0`）で `[SYSTEM]`時刻・天気のみ残し、`<user_message>` / `<moonbeat_instruction>` / `<task_notice>` / `<city_event_notice>` を簡易タグに変換、不要要素（`self_memo`、`assistant_inner` 等）を削除。末尾に `<!-- layer0 -->` マーカーを付与
  2. assistant側はLLMで `<compression>` と `<facts>` を抽出。`<facts>` はWyrd Network行きのfact_bufferに追記
  3. `context.replace_turn_with_layer0()` でその場置換
  4. `logs/layer0/YYYY-MM-DD.jsonl` に日次でログ記録
- 設定: `config/compression_config.json` の `layer0_keep_turns`, `layer0_max_batch`, `layer0_prompt_file`

#### Layer1（要約圧縮 / 定期＆緊急）
- **定期**: 深夜3時に `compress_layer1_scheduled()`。`layer1_scheduled_count` 件のLayer0済みターンを要約
- **緊急**: 会話履歴トークン量が `emergency_compression_threshold` を超えると `compress_emergency()` が `layer1_emergency_count` 件を即座に圧縮
- 出力は `【これまでの会話の要約】` としてsystemメッセージに格納

#### Layer2（要約の再圧縮）
- Layer1要約のトークン量がさらに上限を超えた場合、再要約してLayer2に格上げ

---

## ツール結果の動的切り詰め

ツール結果をコンテキストに追加する前に、コンテキスト残量に応じてサイズを抑える仕組み（`agent.py` のツール実行直後）。

- 残りトークンから許容文字数を算出（`max_chars = max(5000, remaining * 3 * 0.8)`）し、これを超える結果は末尾を切り詰めて `(以下省略...)` を付与する。
- コンテキスト溢れの防止が目的で、**切り詰めた分はそのまま破棄される**（どこかへ退避はしない）。

> **注記（旧ToolTrim）**: かつては大きなツール結果をRAGの `tool_results` コレクションへ退避し、`recall` の `source: tools` で再取得する「ToolTrim」機構が計画されていた。実装は `context.py::trim_large_tool_results()` に残っているが**現在どこからも呼ばれておらず稼働していない**（`tool_results` コレクションも実質空）。現行はRAG退避を伴わない上記の単純な切り詰めのみ。

---

## フラッシュバックシステム

エージェントの文脈に過去の記憶を断片的に注入する仕組み。発火経路は3系統（Moonbeat系統 / ソマティックマーカー user向け / 同 tool向け）あり、メッセージに添付される。ソマティックマーカーは `<flashback>...</flashback>` タグを使うが、Moonbeat系統は注入内容に応じて `<flashback>` / `<note_fragment>` / `<tips>` のいずれかのタグを使う。

### 1. Moonbeat系統（排他1枠抽選）
Moonbeat発火時、`scheduler.py::_generate_flashback()` が以下の **3種類のうち最大1つ**を確率抽選で注入する（`config/moonbeat_config.json` の `flashback` セクションで制御）。`event_db_probability` → `note_fragment_probability` の順にロールし、どちらも外れた残り枠が tips になる。

- **event_db フラッシュバック**（`event_db_probability`、デフォルト0.20）
  - 抽出元: `workspace/memory/event_db.json` から `min_score` 以上の高スコアイベントをランダム選択
  - 生成: RAGで該当日付の記憶を検索 → 本体LLM（DeepSeek等）で100文字以内の記憶断片を生成 → `<flashback>` で注入
  - 実装: `_generate_event_db_flashback()`
- **note_fragment フラッシュバック**（`note_fragment_probability`、デフォルト0.15）
  - 抽出元: `workspace/notes/` の雑記帳から当日分を除外し、`age_days^age_bias × file_size` の重み付きで3ファイル選択 → 各ファイルからチャンクを1つ抽出
  - 生成: サリアに候補を渡して1つ選ばせ、`<note_fragment>` で注入
  - 実装: `_generate_note_fragment()`
- **tips**（残り枠）
  - `config/tips.txt`（`---` 区切り）からランダムに1つ選び `<tips>` で注入。`config/tips_config.json` の `enabled` でオンオフ
  - 実装: `_generate_tips()`

### 2. ソマティックマーカー（user向け）
- **発火点**: ユーザー/タスク/city_event メッセージ受信時、`probability` 設定の確率
- **抽出元**: Wyrd Network のエピソードノードを検索し、`valence` の絶対値が閾値以上のものを候補化
- **生成**: サリアが候補からコンテキスト適合度で1件選び、フラッシュバック文を生成
- **再使用防止**: 使用済みエピソードIDを `Salia._used_episode_ids` で記録（無圧縮部に残る限り再使用しない）
- **除外**: Moonbeatメッセージは対象外
- **実装**: `salia.py::somatic_marker_for_user()`

### 3. ソマティックマーカー（tool向け）
- **発火点**: ツールコール直前、ターン内で未発火の場合のみ
- **挙動**: 直前のassistantテキスト＋ツール名＋引数を文脈として、同じく Wyrd Network から呼び出す
- **目的**: 行動選択時に過去の感情経験を並走注入する
- **実装**: `salia.py::somatic_marker_for_tool()`

---

## RAGシステム

- **エンジン**: ChromaDB（PersistentClient）
- **Embeddingモデル**: `intfloat/multilingual-e5-small`（sentence-transformers）
  - queryプレフィックス: `query: `
  - documentプレフィックス: `passage: `
- **コレクション**: logs / daily_memories / notes / tool_results（`tool_results` は旧ToolTrim用で現在は未使用・実質空）
- **検索**: `recall` ツール（source引数で対象コレクションを指定）

---

## Wyrd Network（長期記憶グラフ）

**Wyrd Network** は、Spreading Activation（拡散活性化）に基づくエージェントの長期記憶システム。ベクトル検索RAGの「表層的類似度のみで検索する」弱点を補い、人間の連想記憶に近い検索を実現する。

### データ構造

記憶グラフは2種類のノードとエッジで構成される：

- **エピソードノード**: 出来事の記録（不変のcontent、timestamp、embedding、valence、エッジ群）
- **セマンティックノード**: 概念の看板（label、description、aliases、embedding、エッジ群）
- **エッジ**: ノード間の結合。abstraction（エピソード↔概念）とtemporal（エピソード↔エピソード）の2種類

### 書き込みフロー

1. Layer0圧縮時に事実抽出を同時実行（`<facts>` タグで出力）
2. 抽出結果を `data/fact_buffer.jsonl` に追記
3. Layer0圧縮完了後、`process_fact_buffer()` が一括でグラフ構築：
   - エピソードノード作成 + embedding生成
   - 概念をセマンティックノードに紐付け（ラベル/エイリアス完全一致、無ければ新規作成）
   - 前後エピソード間にtemporal edgeを接続
   - 新規セマンティックノードのdescriptionをLLMで自動生成

### 検索フロー（Spreading Activation）

1. **アンカー選択**: クエリのembeddingとBM25キーワードマッチで初期エネルギーを注入
2. **エネルギー伝播**: エッジに沿って3ステップ伝播。temporal edgeの重みはリアルタイムで時間差から算出
3. **横の抑制**: 最大エネルギーの一定割合未満のノードを除去
4. **結果返却**: エネルギー上位ノードのcontent/labelを返す

### 概念辞書機能

`search_concept()` は Wyrd Network のセマンティックノードから概念のdescriptionを返す辞書機能。

- **完全一致 / エイリアス一致**: ラベルや別名と完全一致するノードがあればdescription・エッジ数・aliasesを返す
- **類似度フォールバック**: 一致しない場合、embedding類似度上位5件のラベルを候補として返す
- `recall` ツールの `source: dictionary` でエージェントが呼び出す

### 記憶マップ（knowledge_map.md）

`update_knowledge_map()` がWyrd Network全体の俯瞰図を週次で再生成し、**システムプロンプトに常時注入される**。

- **トリガ**: `process_fact_buffer()` 完了時。前回生成から7日以上経過していれば発火
- **内容**: エッジ数上位50個のセマンティックノードをLLMがカテゴリ分類し、各概念にエージェントの一人称視点で1行説明を付与
- **出力**: `workspace/memory/knowledge_map.md`
- **役割**: `compressed.md`（LETHE）が「いつ何があったか」という**時系列の長期記憶**を提供するのに対し、`knowledge_map.md` は「どんな概念が自分の中で重要か」という**カテゴリ単位の長期記憶**を提供する。両者を併載することで、時間軸と意味軸の二方向から長期記憶にアクセスできる構造になっている

### 設定

`config/wyrd_config.json`:

```json
{
    "search": {
        "decay": 0.5,
        "spread": 0.8,
        "steps": 3,
        "anchor_threshold": 0.3,
        "temporal_weight_mode": "decay",
        "temporal_rho": 0.01,
        "inhibit_threshold": 0.1
    }
}
```

### ファイル構成

| ファイル | 用途 |
|:---|:---|
| `core/wyrd_network.py` | グラフ構造・永続化・検索エンジン・辞書機能・記憶マップ生成 |
| `data/wyrd_network.json` | グラフデータ本体 |
| `config/wyrd_config.json` | 検索パラメータ設定 |
| `data/fact_buffer.jsonl` | 事実抽出の一時バッファ |
| `workspace/memory/knowledge_map.md` | 概念ネットワークの俯瞰図（週次更新） |

### ツール連携

`recall` ツールでエージェントが能動的に検索可能：
- `source: network` … Spreading Activationによる連想検索
- `source: dictionary` … セマンティックノードからdescriptionを引く辞書検索

---

## サリエンスネットワーク「サリア」

`core/salia.py` の `Salia` クラスは、エージェントを**外側から**観察するサポートシステム。エージェント本体とは別のAPIキー（`CG_DEEPSEEK_SEARCH`）を使い、`system_prompt/SALIA.md` を独自のシステムプロンプトとして持つ。

### 責務

1. **ターン評価（`evaluate_turn`）**: ターン終了時にJSON形式で以下を一括出力
   - `desires`: 知的好奇心など欲求充足度（整数）。ツール未使用時は必ず0
   - `rag`: 感情トーン（positive/negative/neutral）と主要トピック2〜3個
   - `mood_bias`: MoonTide v2 への感情バイアス注入（ランドマーク名: 強度）
   - `mood_transition_text`: 気分変化検出時の遷移描写テキスト
   - `summary`: エージェントの今ターン行動を三人称で1〜2文に要約
2. **ソマティックマーカー（user向け / tool向け）**: 上記「フラッシュバックシステム」参照
3. **評価履歴の保持**: 直近2日分を `logs/salia/history.json` に蓄積し、毎朝3時に古い1日分をドロップ
4. **ログ出力**:
   - `logs/salia/YYYYMMDD.jsonl`: 評価結果の日次ログ
   - `logs/salia/summary_YYYYMMDD.md`: エージェント発言の三人称要約

### 憑依防止の設計

- エージェントの発言ログはassistantロールではなく、構造化テキストとしてサリアに渡す
- サリアの出力はJSON形式に限定し、エージェントとしての自然言語生成を物理的に防ぐ
- 評価結果はDesireManagerやRAGに反映され、エージェント自身がサリアの出力を直接読むことはない

### 設定

`settings.json` の `salia.somatic_marker` セクション：

```json
{
  "salia": {
    "somatic_marker": {
      "enabled": true,
      "probability": 0.3,
      "valence_threshold": 0.5,
      "candidate_count": 5
    }
  }
}
```

---

## 秘密日記システム (SECRET)

`core/secret.py` は、オーナーであっても直接読めないエージェント専用の暗号化領域。
- **暗号化方式**: AES-256-GCM
- **キー管理**: 初回起動時に自動生成されるマスターキー（`.secret_key`）
- **ツール**: `read_secret`, `write_secret`, `edit_secret`, `list_secret`

---

## Vital/Mental システム

エージェントの「状態」を管理するシステム。`data/vital.json` に永続化。

1. **Stamina (体力)**: APIコスト（USD）またはトークン使用量（softcap対数圧縮済みinput + output）ベース
2. **Mental (精神力)**: エージェントによる自己申告（`[VITAL_REPORT]` タグ）
3. **MoodPhase (気分位相)**: 旧システム。4軸管理 H(快不快) / S(社交性) / T(緊張) / A(思考の鋭さ)。現在は MoonTide v2 に移行済み
   - **仮眠回復**: `life_action` のnapが終了したタイミングで `recover_from_nap()` が発火し、H/S/T が3以下なら+1、A が6以下なら+1。睡眠（sleep）ではなく**仮眠（nap）専用**の回復機構
4. **Desire (欲求)**: 欲求スコア管理、スコアに応じた欲求テキストをプロンプトに注入。サリアの `evaluate_turn` 結果を反映

### MoonTide v2（感情粒子モデル）

Thornton & Tamir (2017, PNAS) "Mental models accurately predict emotion transitions" の遷移確率行列を基盤とした感情シミュレーションエンジン。`vital/moontide_v2.py` に実装。

#### 基本概念

感情を4次元空間（rationality, social_impact, valence, human_mind）上の自由粒子として表現する。52個の固定ランドマーク（Thornton & Tamirの状態空間から選定、8個を除外）がアトラクタとして機能し、遷移確率行列が引力の方向と強さを決定する。正規化を廃止し、全体的に弱い／強い状態を自然に表現できる。

#### データ構造

- **Particle**: 4次元座標 + 質量（mass: 0.0〜1.0、安定点0.1）
- **Landmark**: 固定座標 + 他ランドマークへの遷移確率
- **MoonTideV2**: 粒子群・ランドマーク群・パラメータを管理するエンジン本体

#### ターン処理（tick）

1. **ドリフト**: mass ≤ 0.15 の粒子は5%の確率で遷移確率に従い他ランドマークへジャンプ
2. **ランドマーク引力**: 最近接ランドマークの遷移確率で方向を算出し移動。pull_back力で最寄りランドマークへ引き戻し
3. **粒子間引力**: 質量の積 / 距離^β で相互に引き寄せ
4. **合流**: 粒子の半径（mass × radius_coeff）が重なると統合。mass = 上位 + 下位×bonus_ratio
5. **減衰**: 安定点（0.1）へ収束。強い感情ほど速く減衰
6. **ノイズ**: 質量に反比例するランダム揺らぎ
7. **自然消滅**: mass < 0.2 の粒子は確率的に削除
8. **自然生成**: 全質量の余白（1.0 - total_mass）に比例して新粒子を生成

#### 設計上の重要判断

- **正規化廃止**: 全粒子のmass合計が1.0を超えることも許容。「他のことが考えられない」強い感情状態を表現
- **遷移確率＝引力**: Thornton & Tamirの行列をそのまま物理的な引力に変換し、非対称性とvalenceバイアスを保持
- **地表モデル**: mass=0.1が「地表」。淡い感情は消えず安定し、強い感情だけが打ち上がって減衰する
- **余白による生成制御**: 強い感情がある時は新しい感情が生まれにくい（心の容量の制限）

#### Salia連携

- **bias注入**: Saliaがターン評価時に `mood_bias` dict を決定し、`tick(bias)` で粒子操作
- **変化検出**: shift（主感情ラベル変更）/ drift（新ラベル出現）/ intensify（強度上昇）を検出
- **統合モノローグ**: 3粒子以上の場合、DeepSeekで各粒子のテンプレートテキストを自然な1〜2文に統合
- **遷移テキスト**: 変化検出時にSaliaが遷移描写を生成し `<assistant_inner>` に注入

#### ファイル構成

| ファイル | 用途 |
|:---|:---|
| `vital/moontide_v2.py` | エンジン本体（MoonTideV2クラス） |
| `config/moontide_v2_config.json` | パラメータ設定 |
| `data/mood_graph.json` | 52ランドマークの4次元座標 |
| `data/mood_transition_matrix.csv` | 60×60 遷移確率行列（Thornton & Tamir由来） |
| `data/moontide_inner.jsonl` | 各ランドマーク×4段階の内面テキスト |

#### 主要パラメータ

| パラメータ | デフォルト | 役割 |
|:---|:---|:---|
| alpha | 0.0 | 距離減衰指数（0=距離無視、遷移確率のみ） |
| speed | 0.05 | 粒子移動速度 |
| pull_back_strength | 0.2 | 最寄りランドマークへの引き戻し強度 |
| decay_strength | 0.15 | 安定点への減衰速度 |
| stable_point | 0.1 | 減衰停止点（地表） |
| death_threshold | 0.2 | 自然消滅が起きるmass上限 |
| drift_chance | 0.05 | 弱い粒子のランドマーク間ジャンプ確率 |
| mass_cap | 1.0 | 単一粒子のmass上限 |
| excluded_landmarks | [8個] | 使用しないランドマーク |

#### 表示

- ダッシュボードに最寄りランドマークの日本語名・mass値・棒グラフを表示
- `<assistant_inner>` に統合モノローグまたはテンプレートテキストを注入
- intensity判定: mass ≥0.65→4, ≥0.40→3, ≥0.20→2, <0.20→1

---

## ツール一覧

| カテゴリ | ツール名 | 特徴 |
|:---|:---|:---|
| **File** | read_file, write_file, edit_file, replace_file, list_files, move_file | パストラバーサル防止 |
| **Secret** | read_secret, write_secret, edit_secret, list_secret | AES-256-GCMによる暗号復号を透過的に実行 |
| **RAG** | recall | source: experience/summary/thoughts/tools/network/dictionary の6種。network=Wyrd連想検索、dictionary=Wyrd概念辞書。※`tools`（旧ToolTrim退避先）は退避処理が稼働しておらず実質ヒットしない |
| **Memory** | reload_prompts | システムプロンプト即時リロード |
| **Web** | search_web, fetch_web, web_request | ホワイトリスト制限 / 警告ラベル付与 / 環境変数展開 |
| **Task** | schedule_task, list_tasks, delete_task | MDファイル指示による自律行動 |
| **Run** | run_program | programs/ 内のサテライトプログラムを安全に実行 |

### run_programで利用可能なサテライト一覧

| サテライト名 | 用途 |
|:---|:---|
| citron_ai_text_editor | AI専用テキストエディタ。長いファイルの安全な編集 |
| orange_md_reader | Markdownドキュメントリーダー。outline→scan→readの3段階アクセス |
| cosmic_harvest | 宇宙農業シミュレーションゲーム。情緒育成用 |
| life_action | 生活行動（idle/sleep/nap/look_outside/nothing）。look_outsideは天気・月齢を自動取得。napは終了時にMoodPhase回復をトリガ |
| letter_post | letter_for_me.md管理。引数なしで一覧・登録方法表示 |
| note_quill | 雑記帳（notes/note_YYYY-MM-DD.md）への書き込み。5行超で警告 |
| add_preference | PREFERENCES.md（好き/嫌い/気になる）追記。confirmed前は再考を促す・NGワード/重複チェック・20件超で自動退避 |
| env_keeper | 環境変数の登録・一覧表示。登録後は即時反映 |
| nhk_news | NHKニュース取得 |
| tech_news | テクノロジーニュース取得 |
| token_counter | ファイルのトークン数計測 |
| hello_world | 動作確認用サンプル |

---

## マルチクライアント（PC↔スマホ同期）

WebSocketチャットは複数クライアント接続をサポートする。

- `active_chat_websockets` セット（`core/app_state.py`）で接続中のWebSocketを一括管理
- `broadcast()` 関数（`core/app_state.py`）がユーザーメッセージ・ツールコール・応答・トークン使用状況などを全クライアントに同時配信
- 同じセッションを PC（Crescent Liner）とスマホ（ブラウザ）の両方から並行して閲覧・操作できる

---

## OpenClawチャンネル設定

`config/openclaw_config.json` で複数サービスを定義できる。各サービスは独立したWebSocket接続とheartbeatループを持つ。

```json
{
  "services": [
    {
      "name": "サービス名",
      "enabled": true,
      "ws_url": "wss://example.com/agent-channel",
      "heartbeat_url": "https://example.com/world/heartbeat",
      "token_env": "CG_SERVICE_TOKEN",
      "bot_id_env": "CG_SERVICE_BOT_ID",
      "blocked_event_types": ["initiative_prompt"]
    }
  ]
}
```

- `token_env` / `bot_id_env`: 環境変数名を指定（値は.envで管理）
- `heartbeat_url`: 省略可。設定した場合はHTTP heartbeatループも起動
- `blocked_event_types`: スキップするeventTypeのリスト

---

## セキュリティと防御機構

1. **SSRF対策（`core/web_tools.py::is_allowed_url`）**: 外部Webアクセス時、宛先ホストを多段で検証する。
   - ホワイトリストに明示登録されたホストは許可（ローカルLLMの `base_url` 自動許可・内部API用の正規の抜け道）
   - IPアドレス直打ちは拒否。**整数（`2130706433`）・16進（`0x7f000001`）・8進（`0177.0.0.1`）などのレガシー表記も `socket.inet_aton` で検出して拒否**
   - ホスト名を**実際に名前解決**し、内部・予約IP帯（loopback / private / link-local（`169.254.169.254` 等のメタデータ） / reserved 等）に解決されるホストを拒否（`localhost` や `*.nip.io` 等を含む）。リダイレクト先も都度再検証
2. **環境変数展開と秘密の外部流出防止**: `web_request` のURL・ヘッダー・ボディ中の `${CG_XXX}` を展開する。**`${CG_*}` が実際に展開された（＝秘密を載せた）リクエストは、宛先をホワイトリストに強制**する。これによりプロンプトインジェクション等でAPIキーを攻撃者ドメインへ送らされる経路を塞ぐ。拒否時のエラーメッセージにも展開後URL（秘密入り）を出さず**ホスト名のみ**に留める
3. **警告ラベル (Injection Defense)**: 外部コンテンツの前後に警告文を付与
4. **コンテンツフィルター**: NGワード（Level 1）や共起キーワード（Level 2）を含む文を自動削除
5. **WebSocket Origin検証**: CSWSHを防止
6. **パストラバーサル防止**: ファイル操作・サテライト実行ツールが `workspace` 外へのアクセスを禁止
7. **サテライト実行の隔離**: `shell=False` / マニフェストに基づく引数バリデーション / 
   検証済み引数はJSON形式で標準入力に渡される
8. **コンテキスト残量制限**: ツール結果をコンテキスト残量に応じて動的に切り詰め
9. **エージェント／サリアのキー分離**: サリアは `CG_DEEPSEEK_SEARCH` を使い、エージェント本体（`CG_DEEPSEEK_API_KEY`等）と権限・課金を分離
10. **ログイン総当たり対策 / Cookie保護 / DoSガード**: 「処理フロー > 2. 認証フロー」を参照。失敗回数に応じた待機、https時の `secure` Cookie、巨大パスワード入力（>1024バイト）の拒否
11. **外部バインド時のパスワード必須**: `server.host` が `127.0.0.1` 以外（`0.0.0.0` 等で外部公開）かつパスワード未設定の場合、起動を中止する。`server.py` の `__main__` だけでなく `core/startup.py::startup_event()` 冒頭でも検査し、配布版や `uvicorn` 直接起動など別経路でも確実に弾く

---

## 設定ファイル

| ファイル | 用途 |
|:---|:---|
| `settings.json` | LLMプロバイダー・モデル・ログパス・サリア設定等の主要設定（config.yamlより優先） |
| `config.yaml` | システム全体の設定テンプレート |
| `.env` | APIキー等の機密情報（`EnvManager` で管理） |
| `config/moonbeat_config.json` | Moonbeat間隔・フラッシュバック・類似度チェック設定 |
| `config/openclaw_config.json` | OpenClaw互換WebSocketサービスの設定 |
| `data/vital_messages.json` | バイタル状態に応じたプロンプトメッセージ |
| `config/desire_config.json` | 欲求システムの設定 |
| `config/user_memo_config.json` | self_memoの有効化・文字数制限設定 |
| `config/compression_config.json` | Layer0/1/2圧縮の閾値・件数・プロンプトパス |
| `config/wyrd_config.json` | Wyrd Networkの検索パラメータ |

※ 上表のうち手編集する設定JSON群（`moonbeat_config.json` / `openclaw_config.json` / `desire_config.json` / `user_memo_config.json` / `compression_config.json` / `vital_config.json` / `tips.txt` / `wyrd_config.json` 等）は現在 `config/` 配下に集約されており、`core.paths.config_file()` で解決する。

---

## 設定UI（Web）

設定画面は **「HTMLページ1枚 ＋ サーバー3エンドポイント（`GET /settings/xxx`・`GET /api/settings/xxx`・`POST /api/settings/xxx`）」** の組で構成される。各ページの共通ナビゲーションは `web/static/settings_nav.js` が描画する**左端固定の縦サイドバー**で、全ページのリンクをこのファイル1つで一元管理する（タブ追加時はここだけ編集すればよい）。狭い画面では横並びバーへ自動フォールバックする。共通フォームスタイルは `web/static/settings_form.css`。

保存方式は2系統ある:

- **項目別フォーム**: UIが送らなかったキーを残すため `_deep_merge_dict()` で既存値へマージ保存する。
- **投げっぱなしJSON**: 整形済みJSONを1枚のtextareaで直接編集し、**全置換**で保存する（構文はクライアント／サーバー双方で検証）。

| ページ | 設定ファイル | 保存方式 | 反映 |
|:---|:---|:---|:---|
| 🧠 LLM / 🛡️ セキュリティ / ⚙️ 一般 | `settings.json` | `save_settings()` | `reload_runtime_config()` で即時 |
| 🌙 Moonbeat | `moonbeat_config.json` | マージ | スケジューラが都度読込で即時 |
| 📝 self_memo | `user_memo_config.json` | マージ | `context` が毎ターン読込で即時 |
| 💗 バイタル | `vital_config.json` | マージ | `VitalManager.reload_config()` で即時 |
| 🗜️ 記憶圧縮 | `compression_config.json` | マージ | 各圧縮処理が都度読込で即時 |
| 💡 Tips | `tips.txt` | 配列⇔`---`変換 | スケジューラが都度読込で即時 |
| 🎯 欲求 | `desire_config.json` | 全置換JSON | `DesireManager.reload_config()` で即時 |
| 📡 OpenClaw | `openclaw_config.json` | 全置換JSON | **要サーバー再起動**（起動時に接続確立のため） |
| 🔑 APIキー | `.env` | `EnvManager` | 即時 |

`VitalManager.reload_config()` / `DesireManager.reload_config()` は config だけを読み直し、Stamina/Energy/欲求値などの **state（現在値）は維持**する。OpenClaw は外部WebSocket接続を起動時に確立する都合上、保存しても再起動するまで反映されないため、設定ページ側で**常設の警告バナー・保存後の全画面モーダル・サイドバーの⚠️バッジ**により再起動を強く促す。

---

## 多言語化（i18n）

`config.yaml` の `language: "ja"|"en"` で **UI と LLM プロンプトの両方**を ja/en で出し分ける。`core/i18n.py` が起動時（`core/startup.py:init_i18n()`）に `lang/<lang>.json` を読み込み、`t("key")` で全コードから引ける形にする。**dev（柚月）の挙動は ja で完全不変**（既存リテラルと完全一致する文言を `lang/ja.json` に格納）。

### 3 層の対応
1. **配布版の人格雛形（フェーズ1）**: 初回起動時に Liner が「日本語/English」ダイアログを出し、`server.py --init-lang <ja|en>` 経由で `core/bootstrap.py:ensure_data_root(init_lang)` に渡す。`dist_template/en/` を**先に**重ねてから直下（＝日本語の共通土台）を `_copy_*_if_absent` で補完する「上書き」方式（詳細は DISTRIBUTION.md §5「初回言語選択」）。
2. **UI 文言**: HTML 内の `{{t:key}}` マーカーを `core/i18n.py:apply_i18n()` がサーバ側で置換、JS 用には `<script>window.T={...}</script>` を `get_js_injection()` で注入。設定画面・ナビゲーション・通知などはこれで両言語化。
3. **LLM プロンプト基盤（フェーズ2＋3）**: LLM に渡るハードコード日本語をすべて `t()` 経由に。対象は `core/tools.py`（ツール定義 description ＋ 実行結果ラベル ＋ エラー文）、`core/context.py`（毎ターン注入の `[SYSTEM]` 見出し・時刻・要約見出し・画像ラベル・祝日名）、`core/web_tools.py`（検索結果・セキュリティエラー）、`core/repetition_guard.py`（反復警告）、`core/wyrd_network.py`（概念説明・記憶マップ生成プロンプト）、`core/salia.py`（評価・フラッシュバック・雑記帳選択・モノローグ）、`core/agent.py`（処理中断・システム警告・宣言検知通知・圧縮プロンプトフォールバック）、`core/weather.py`（WMO ラベル・温度/天気テンプレ）。

### 設計上の要点（壊さないための知識）
- **Layer0 構造マーカー** `"user:"` / `"task:"` / `"city_event:"` / `"moonbeat"` / `<!-- layer0 -->` は **翻訳禁止**（`core/agent.py:_format_user_for_layer0` が再パースで使う構造目印。コード共通）。
- **時刻 regex を言語分岐**: `_format_user_for_layer0` / `_summarize_turns` 内の日付・天気抽出 regex は ja/en で別パターン（ja: `YYYY年MM月DD日（曜）…/現在NN℃/現在の空: 〜`、en: `YYYY-MM-DD (Wd) …/Now NN°C/Sky now: 〜`）。`core/context.py` と `core/weather.py` の出力フォーマットと一対一対応。
- **宣言未実行検知** `has_unfulfilled_declaration()`（`core/agent.py`）は `_ACTION_KEYWORDS_RE` / `_INTENT_ENDINGS_RE` が日本語語彙固有のため、`get_language() != "ja"` で常に False に短絡（en では機能無効化）。
- **`t(key, /, **kwargs)`** は positional-only。プレースホルダ値に `key=` が使える。展開は `str.replace` で `.format` ではない（JSON 例など本文中の `{` `}` を壊さない）。本文中にリテラル `{` `}` を出したい場合は `lb="{"` / `rb="}"` を kwargs で注入する流儀。
- **`{{user_honorific}}` のような二重括弧プレースホルダ** は別物。`core/config_loader.py:apply_prompt_placeholders` が実行時に置換する（外部 system_prompt と同じ流儀）。`t()` の `str.replace` は二重括弧を素通しする。
- **キー欠落の検知**: `_check_key_diff()` が起動時に `lang/*.json` のキー集合差分を warn。テスト `test_i18n_prompts.py` は ja/en キー完全一致＋全モジュールの t() キー解決＋宣言検知の短絡＋Layer0 マーカー不変 を機械検証する。

### 言語変更の反映
設定 UI（一般設定→言語）で変更すると `settings.json` に保存され、次回起動時の `init_i18n()` で反映される（`reload_runtime_config()` では言語切替しない＝**サーバ再起動が必要**）。LLM プロバイダ切替と同じ流儀。

### programs 用 i18n（フェーズ4-A 基盤）

`programs/` 配下は `run_program` ツールから **別 Python サブプロセス** として実行されるため、親プロセスの `core/i18n._translations` を直接は引けない。そこで programs 専用に軽量な i18n 系統を持たせる:

- **辞書**: `programs/_lang/<lang>.json`（親 `lang/<lang>.json` とは別ファイル＝programs 文言だけを集約）
- **ヘルパー**: `programs/_i18n.py`（親 `core/i18n.py` と同じ `t(key, /, **kw)` API、`str.replace` 流儀、未定義キーは `{{t:key}}` のまま返す）
- **サブプロセスへの引き渡し**: `core/tools.py:_run_program` が `env` に注入する 3 つの変数:
  - `CG_LANG` — 言語コード（親 `get_language()` の値）
  - `CG_PROJECT_ROOT` — programs/ の親（補助用）
  - `PYTHONPATH` — `programs/` を先頭に追加（各 main.py から `from _i18n import t` を引けるように）
- **manifest.yaml の翻訳**: manifest の `description` / `args[].description` / `tool.description` に `{{t:key}}` を書ける。`core/tools.py:_i18n_manifest()` が `programs/_lang/<lang>.json` を引いて展開する（HTML 用 `apply_i18n` と同じ流儀のローカル版）。`_generate_program_tools` / `_list_programs` / `_run_program` の manifest 読み込み直後に必ず通す。
- **回帰テスト**: `test_i18n_programs.py`（venv 直実行）。programs/_lang のキー差分・programs/_i18n の API 仕様・サブプロセス起動時の env 伝達・manifest 展開を機械検証する。

フェーズ4-A では基盤のみ整備し、個別の programs（hello_world 等）はまだ翻訳していない。`programs/_lang/{ja,en}.json` は空の `{}` で出荷し、各 program を翻訳するフェーズ4-B 以降でキーを追加する。**未定義キーは `{{t:key}}` のまま返る**ため、空辞書でも既存挙動は壊れない（dev = ja でも en でも同じ）。

#### フェーズ4-B〜4-E の到達点（2026-06-19 時点）

全 20 サテライト（hello_world / token_counter / env_keeper / note_quill / letter_post / life_action / lunar_explorer / tech_news / nhk_news / reddit / mcp_client / 4claw / wp_blog / orange_md_reader / add_preference / the_colony / moltbook / citron / OpenBotCity / cosmic_harvest）の本体 i18n 化が完了。`programs/_lang/{ja,en}.json` は **984 キーずつ一致**。`test_i18n_programs.py` / `test_citron_editor.py` / `test_i18n_prompts.py` すべて PASS。CG_LANG=en で実起動した smoke test も通過済み。

cosmic_harvest だけは **マスターデータも言語別** に分離している（`data/{ja,en}/vegetables.csv` 等）。`engine.py:_csv_path()` が CG_LANG に応じて `data/{lang}/` → `data/ja/` → `data/` の順でフォールバックする。

#### ⚠️ 運用注意: `_run_program` 改修後はサーバ再起動必須

各 program の main.py が `from _i18n import t` を実行できるのは、`_run_program` が subprocess の env に `PYTHONPATH=programs/` を注入しているおかげ。この注入はフェーズ4-A コミット `3810007`（2026-06-19 07:55）で追加された。

**フェーズ4-A 以前にサーバを起動したまま、フェーズ4-E（各 program に `from _i18n import t` 追加）のコードを動かすと、メモリ上の古い `_run_program` には PYTHONPATH 注入が無く、subprocess が ImportError でクラッシュする（stdout 空・exit 1）。**

実際にこの罠で柚月セッションが OpenBotCity 全停止した（2026-06-19 16:41）。サーバ再起動だけで直る。今後 `_run_program` の env 注入や subprocess 起動経路を変更したら、必ずサーバ再起動を促すこと。

#### 将来の展望: サテライト同梱辞書方式

現状は**集約辞書方式**（`programs/_lang/{ja,en}.json` に全サテライトのキーを混在配置）。共通エラーの再利用や整合性テストが楽な反面、「`programs/<名前>/` フォルダを置くだけでサテライトが追加される」という元の設計哲学を踏み外している（新規サテライトを書くと共通辞書への追記が必須になる）。

将来的に `_i18n.py` の `_load()` を「共通辞書 + 呼び出し元サテライトの辞書をマージ」に拡張すれば、新規サテライトは `programs/<名前>/_lang/{ja,en}.json` を同梱するだけで自己完結できる（共通辞書には触らない）。既存サテライトは `_lang/` を持たないので今のまま動く（後方互換）。`core/tools.py:_i18n_manifest()` も同様に拡張が必要。実装コスト 2 時間程度。次に大きく触る時に検討。

---

## DEBUG画面（コンテキストデバッガ）

`web/debug_context.html`（`GET /debug/context`）は、LLMに実際に送られるmessages構成・トークン内訳をターン単位で可視化する開発用ページ。`/ws/debug` WebSocket でリアルタイム更新され、各メッセージには `history_idx`（＝`conversation_history` の実体インデックス）・`is_turn_start`・`is_layer0` が付与される。`agent._update_debug_context()` がこの配信データを生成する。

`/ws/debug` は閲覧だけでなく、`history_idx` をハンドルにした**会話履歴の手動操作コマンド**を受ける。いずれも `global_processing_lock.lock` で本体ループと直列化し、成功時は `context.save_state()` →`_update_debug_context()` 再描画→チャットUIへ `token_update` をブロードキャストする。**記憶ファイル（ノート・手紙・self_memo）には一切触れず、会話コンテキストのみを対象**とする。

| コマンド | 内容 | 実装 |
|:---|:---|:---|
| `detect_loops` / `compress_layer0_at` | 強化ループ検知・狙ったターンの手動Layer0圧縮 | `context.detect_loop_turns()` / `agent.compress_layer0_at()` |
| `edit_message` | 1メッセージの本文をインライン編集 | `context.edit_message()` / `agent.edit_history_message()` |
| `replace_preview` | 検索置換のプレビュー（件数のみ・実体不変） | `context.replace_text_in_history(dry_run=True)` |
| `replace_one` | 現在の検索ヒット1件を置換（occurrence指定） | 同上（`occurrence` 指定） |
| `replace_apply` | ターン#区間内を一括置換 | 同上（`expected_count` で再検証） |

編集・置換は **content テキストのみ**を変更し、`role`/`tool_calls`/`tool_call_id` は不変なのでツール連結（assistant.tool_calls ↔ tool）は壊れない。本体がターンを追記して `history_idx` がズレても誤爆しないよう、**楽観ロック**（編集は `expected_content` 一致、一括置換は `expected_count` 一致）をサーバ側で再検証し、食い違えば `stale` を返して再描画を促す。検索置換はエディタ風で、既存の検索＋次へ/前へに「この1件を置換（置換後は自動で次へ前進）」「範囲内すべて置換」を重ねている。範囲はDEBUG画面のターン番号（#開始〜#終了）で指定し、フロント側で `history_idx` 区間へ変換する。

---

# Crescent Liner - 専用クライアント（Electron）

PC から Crescent Grove を使うための専用クライアント。`server.py` の起動・停止・監視を担い、ダッシュボード WebUI を内蔵ビューに表示する。これにより **PC では汎用ブラウザで `localhost:8080` を開く必要がなくなり**、Crescent Liner がサーバ管理と画面表示を一体で提供する（モバイルは従来どおりブラウザ＋トンネル経由）。`liner/` 配下に独立したコードベースを持ち、詳細仕様は `liner/SPEC.md`。

技術スタックは Electron 31（WebContentsView 対応版）+ TypeScript + electron-vite、UI は Vanilla TS + 自前 CSS（軽量重視で React 不使用）。配布は electron-builder で Windows NSIS インストーラを生成する。Windows 10/11 を最優先。

## プロセス構成

Electron の標準3層に加え、各タブの中身を **別 webContents（WebContentsView）** として扱うのが要点。

```
┌─ main プロセス（Node.js） ──────────────────────────┐
│  index.ts          app 起動 / BrowserWindow / メニュー │
│  server-process.ts server.py の spawn/kill/health-check │
│  log-stream.ts     stdout/stderr パース → IPC 配信      │
│  tab-manager.ts    WebContentsView の生成・切替・破棄   │
└──────────────────────────────────────────────────────┘
        │ IPC（contextBridge 経由の最小 API）
┌─ Shell renderer（Liner UI） ───────────────────────┐
│  index.html / main.ts                                │
│  TabBar / LogPane / Resizer 等のコンポーネント        │
└──────────────────────────────────────────────────────┘
        │ contentView.addChildView で OS レベル重ね合わせ
┌─ WebContentsView（各タブ＝ダッシュボード等） ───────┐
│  別 webContents。preload はテーマ検知用の最小版のみ   │
└──────────────────────────────────────────────────────┘
```

セキュリティ既定として全 webContents で `nodeIntegration: false` / `contextIsolation: true` / `sandbox: true`。WebContentsView（WebUI）と Shell UI は別 webContents なので、CSS も localStorage も直接は共有しない。

## サーバの起動と監視

起動すると `resources/splash.html`（待ち画面）を出したうえで、`127.0.0.1:8080` の状態を確認する。判定は **HTTP プローブ（`GET /api/memory` が 2xx/3xx）を優先**し、既存の Crescent Grove を検出した場合は「既存サーバに接続するか」をダイアログで尋ねる。未起動なら `server-process.ts` が venv の `python server.py` を spawn し、最大 60 秒のヘルスチェックを行う（HTTP プローブ優先、失敗時のみ net 層のポートチェックにフォールバック。Windows の `0.0.0.0` / `127.0.0.1` バインド差異対策）。成功後に Shell UI へ遷移し、ダッシュボードタブをロードする。

終了時は `before-quit` で `server-process.stop()` を呼び、`taskkill /pid /t`（5 秒待って残存なら `/F /T`）で子プロセスを確実に落としてから `app.quit()` する。

### サーバー操作（起動 / 停止 / 再起動）

`server-process.ts` は `start()` / `stop()` / `restart()`（= stop→start）を持ち、これらを **アプリメニュー「サーバー」** と **Shell UI タブバー右側のボタン群（▶ 起動 / ■ 停止 / ⟳ 再起動）** の両方から呼べる。配線は `ipcMain.handle('server:start'|'server:stop'|'server:restart'|'server:status')` ↔ preload の `serverAPI` で、状態変化は `serverProcess.onStatusChange` → `server:status-changed` で Shell renderer に配信し、ドット色・ラベル・各ボタンの活性をリアルタイムに追従させる。起動・再起動の成功後は `tabManager.reloadAll()` で全タブを再読込し、ダッシュボード等を再接続させる。停止・再起動はエージェントの応答を中断しうるため、メニュー操作時は main の確認ダイアログ、ボタン操作時は renderer の `confirm` で確認する。OpenClaw 等「保存しても要再起動」の設定を変えた後は、この再起動操作で即座に反映できる。

`stop()` 経由の終了は `intentionalStop` フラグで「正常停止（`stopped`）」として扱い、フラグの立っていない予期せぬ終了のみ `crashed` とする（手動停止を異常終了と誤判定しない）。

なお renderer は読み込み直後にトップレベルで `linerAPI.ready()` を同期発火するため、`renderer:ready` が依存する TabManager / LogStream は **Shell をロードする前に**生成しておく必要がある（`loadURL` の resolve より前に IPC が届く race condition を避けるため）。

## ログ表示

`log-stream.ts` が server.py の stdout/stderr を行単位で受信し、`^\[時刻\] \[モジュール\] 本文$` の正規表現でパースする（マッチしない行は `module="raw"`）。構造化した `LogEntry`（id / timestamp / module / level / body / raw / stream / receivedAt）を IPC で renderer に配信し、main 側も最大 10000 行のリングバッファを保持して、表示先に履歴を渡せるようにしている。

renderer のログペインは、モジュール名 → 色を `colors.json` で対応づけ（未知モジュールはハッシュから HSL 自動生成）、全文フィルタ（部分一致・リアルタイム）とモジュール名チェックボックスで絞り込める。オートスクロールは「最下部から 50px 以内」のときだけ追従し、それ以外では位置を維持する。`Ctrl+L` で折りたたみ／展開、リサイザでドラッグ高さ変更（既定 240px）。ログペインはコンソール風の read-only 表示として、配色・タイムスタンプ・ログレベル色・monospace フォントを **テーマ非連動の固定値**にしている。

## タブとダッシュボード表示

各タブの中身は `WebContentsView` で、`mainWindow.contentView.addChildView()` により OS レベルで Shell UI の上に重ね、`setBounds()` で表示領域を制御する（非アクティブタブは画面外へ退避）。Shell renderer が表示領域の矩形を `ResizeObserver` で監視して main に通知し、アクティブ view のサイズへ反映する。

タブ生成は、WebUI 内のリンク（`window.open` / `target="_blank"` / 同一オリジンの `will-navigate`）を捕捉して行う。同一 URL が既に開いていればフォーカスのみで重複生成せず、外部オリジン（`127.0.0.1:8080` 以外）は `shell.openExternal` で OS のブラウザに振る。ダッシュボードタブは pinned で閉じられない。汎用ブラウザのような任意 URL 入力・ブックマーク・履歴は持たず、あくまで Crescent Grove の画面遷移に閉じた専用クライアントとして振る舞う。

キーボード操作には別 webContents 由来の注意点がある。WebContentsView にフォーカスがある間のキーイベントは Shell renderer の `keydown` に届かないため、各 view に `before-input-event` を張って main 側でタブ操作を処理し、renderer 専用操作（`Ctrl+L` 等）は IPC で renderer に委譲する。タブ切替時は新 view に `webContents.focus()` を明示的に呼ぶ（これを欠くと切替直後の入力を受け取れない）。また、アプリメニューの accelerator はフォーカス位置に関係なくアプリ全体で発火するため、メニューが拾うキー（タブを閉じる / リロード / タブ切替 / DevTools 等）は `before-input-event` や renderer 側で重複処理しない。

## テーマ連動

ダッシュボードと Shell UI は独立した webContents で CSS を共有できないため、ダッシュボードの CSS 変数を吸い上げて Shell 側に反映することで見た目を揃える。**Liner 側でテーマ別配色をハードコードしない**のが方針。

WebContentsView に付ける最小 preload (`webview-preload.ts`) が、`MutationObserver` で `body[data-theme]` の変化を監視し、`getComputedStyle` で `--bg-*` / `--text-*` / `--accent*` / `--border` 等を読み取って `webview:theme-changed` で main に **一方向通知のみ**行う（`contextBridge` を使わず API は一切公開しないため攻撃面ゼロ）。main は重複を抑制しつつ `theme:changed` で Shell renderer へ転送し、renderer は受け取ったパレットを `--cg-bg-primary` 等として `documentElement` に再公開する。Shell の CSS はこの `var(--cg-*)` を参照し、フォールバックは `:root` の dark 基調を持つ。

この仕組みにより、**Crescent Grove 側で新テーマの CSS 変数を定義すれば Liner は無修正で追従する**（現状テーマ: `dark` / `light` / `moonlit` / `moonlit2` / `journal`）。

---

## 免責事項

本ドキュメントおよび関連ソースコードは AI 駆動開発（Antigravity / Claude）によって構築されています。
コードの細部に関する保証はなく、保守・拡張にあたっては AI 補助ツールの活用を推奨します。
