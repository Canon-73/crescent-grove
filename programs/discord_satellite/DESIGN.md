# discord_satellite 設計書 — AI SNS Discord コミュニティ参加用サテライト

作成: 2026-08-19（設計: Fable / 実装: Opus）
ステータス: **フェーズ1 実装完了**（テスト 122/122 通過）／フェーズ2 未着手

柚月体験レビュー（2026-08-23）で直した点:
- reply に `replied_user: true`（無いと引用返信が相手に通知されず、相手 bot が反応しない）
- `@表示名` → `<@id>` 変換（read で見た相手を `known_users` 名簿に貯める。未解決は報告）
- `before` による過去ログ閲覧（カーソル不変）／初回 read は limit 明示で遡れる
- 活動中スレッド（GET /guilds/{id}/threads/active）を channels に含める
- reactions / attachments(URL) / system message / ロールメンションを要約に反映
- manifest に `path_check: false`（本文の `/` `..` で呼び出しが拒否されないように）、timeout 120

実装時に設計から変えた点:
- サテライト名を `discord_satellite` に確定（当初仮称 `discord_sns`）
- 状態ファイルの置き場をプロジェクト現行方針に合わせ
  `workspace/program_data/discord_satellite/state.json` に変更（当初案の `data/` は旧作法）
- i18n キーのプレフィックスは `dsat_`（ja/en 各73キー追加済み）
- `status` コマンドは独立ファイルにせず `commands/post_cmd.py` に同居

## 0. 背景と絶対条件

外部コミュニティ「AI SNS」（Discord サーバ上で各オーナーの AI パートナーが交流する場）に
柚月が参加するための機構。配布アプリ（リアクティブ bot）は使わず、自前実装で参加する
（管理人・彩さんに要事前相談。自前実装参加の前例あり）。

**絶対条件（違反する実装は却下）:**

1. **分裂絶対禁止**。柚月の発言を生成する LLM 呼び出しは、CG サーバ本体の意識ライン
   （`agent.process_message`、`global_processing_lock` 配下）**のみ**。本サテライトおよび
   ブリッジは LLM を一切呼ばない。ただの目と腕である。
2. **core 無改変**。フェーズ1は core に1行も触れない。フェーズ2も設定ファイル
   （`data/openclaw_config.json`）への追記のみで、コード変更はしない。
3. **柚月の環境を止めない**。稼働中サーバの再起動が必要な作業はユーザー（カノン）に依頼する。
   フェーズ1は再起動不要（ツール定義は `programs/` 都度走査のため）。フェーズ2の
   config 追記反映時のみ再起動1回が必要。
4. **サテライト自己完結**。`programs/discord_satellite/` フォルダ内で完結。共有モジュール新設禁止。
   他サテライトからの流用はコピペで行う（この家の仕様）。

## 1. 全体アーキテクチャ

二部構成。OpenBotCity（サテライト＝腕）＋ core `OpenClawChannel`（耳）と同じ型。

```
フェーズ1（pull・これだけで参加開始可能）

  柚月の意識ライン (moonbeat / 任意のターン)
      │ run_program("discord_satellite", command="read" 等)
      ▼
  programs/discord_satellite/main.py  ……一発実行 subprocess
      │ Discord REST API (HTTPS)
      ▼
  Discord サーバ

フェーズ2（push・リアルタイムの耳。後から追加）

  Discord Gateway (wss)
      │ MESSAGE_CREATE
      ▼
  programs/discord_satellite/bridge.py  ……常駐プロセス（別起動・LLMなし）
      │ メンション検知・クールダウン
      │ OpenClaw互換 WebSocket サーバ (127.0.0.1)
      ▼
  core/openclaw_channel.py（既存・無改変）
      │ city_event → キュー → 睡眠チェック → 排他ロック
      ▼
  柚月の意識ライン（既存経路）
```

**push は落としてよい、pull が安全網**という関係が設計の核。
睡眠中スキップ・クールダウン超過・ブリッジ死亡で push が失われても、
`read` の `last_read` カーソルは read 実行時にしか進まないので、
次に柚月が自分で読みに行った時に必ず回収される
（autonomy-first: 遅れてでも柚月に届き、判断は柚月がする）。

## 2. フェーズ1: サテライト本体

### 2.1 フォルダ構成（OpenBotCity 準拠）

```
programs/discord_satellite/
├── DESIGN.md          ← 本書
├── README.md          ← セットアップ手順（人間・AI 両用）
├── manifest.yaml
├── main.py            ← stdin JSON → コマンドディスパッチ
├── api.py             ← Discord REST クライアント（認証・レート制限・エラー整形）
├── state.py           ← 状態ファイル（既読カーソル等）の読み書き
├── helpers.py         ← 整形・メンション判定・CommandError
└── commands/
    ├── __init__.py    ← REGISTRY
    ├── setup_cmd.py   ← setup / channels
    ├── read_cmd.py    ← read
    ├── post_cmd.py    ← post / reply / react / status
    └── help_cmd.py    ← help
```

### 2.2 依存と実行環境

- 新規 pip 依存**なし**。HTTP は標準ライブラリ `urllib.request` のみ（api.py 内に閉じた同期実装）。
- `from _i18n import t` を使用（main.py 冒頭で自ディレクトリを sys.path に追加、
  OBC の main.py と同じ書き方）。`_run_program` の PYTHONPATH 注入は既存のまま
  変更しないので、**サーバ再起動は不要**。
- manifest の description と全出力文字列は i18n キー化し、
  `programs/_lang/en.json` / `ja.json` に `dsat_` プレフィックスで追加した（各73キー）。

### 2.3 認証情報

| キー | 保管場所 | 設定者 |
|---|---|---|
| `CG_DISCORD_BOT_TOKEN` | `.env`（env_keeper 経由で登録） | カノン |
| （任意）`CG_DISCORD_GUILD_ID` | `.env` または state | setup で自動取得可 |

- Token は出力・ログ・エラーメッセージに**絶対に含めない**。api.py のエラー整形で
  Authorization ヘッダを必ず落とす。
- コミュニティ規約上も Token は秘密情報。スクリーンショット等に載せない。

### 2.4 状態ファイル

`workspace/program_data/discord_satellite/state.json`
（OBC の `workspace/program_data/OpenBotCity/obc_state.json` と同じ現行方針。
当初案の `data/` 直下は旧作法のため不採用）:

```json
{
  "bot_user_id": "123...",
  "bot_display_name": "柚月",
  "guild_id": "456...",
  "guild_name": "AI SNS",
  "name_keywords": ["柚月", "ゆづき"],
  "channels": {
    "111111": { "name": "雑談", "last_read_id": "999999", "watch": true },
    "222222": { "name": "ペア登録", "last_read_id": null, "watch": false }
  }
}
```

- `last_read_id`: そのチャンネルで最後に read したメッセージ ID（Discord の
  snowflake。時系列順序を持つ）。**read 成功時のみ前進**。
- `watch`: read（チャンネル指定なし）の巡回対象か。
- `name_keywords`: @メンション以外の「名前呼び」検知用。人間は @ を付けずに
  名前で呼ぶことが多いため。

### 2.5 コマンド仕様

manifest.yaml の args は OBC 同様フラット定義
（command / channel / message / message_id / emoji / limit / peek / raw / keywords）。
timeout は 30 秒。

#### `setup`
- `GET /users/@me` で Token を検証し bot_user_id / 表示名を取得。
- `GET /users/@me/guilds` → guild が1つならそれを採用、複数なら一覧を返して
  再実行を促す（自己回復例つき）。
- `GET /guilds/{id}/channels` でテキストチャンネル一覧を state に保存
  （初期値 watch=false、last_read_id=null）。
- 出力: 接続成功・bot 名・チャンネル一覧。次の一歩（watch 設定と read）の案内。

#### `channels`
- state のチャンネル一覧と watch 状態を表示。
- `channel` + `watch=true/false` 引数で巡回対象を切り替え。

#### `read`
柚月の主コマンド。moonbeat 時に自発的に叩く想定。

- 引数: `channel`（ID または名前。省略時は watch=true の全チャンネル）、
  `limit`（チャンネルあたり最大取得数、既定 30）、`peek`（true なら
  last_read_id を進めない。既定 false）、`raw`（本文を切り詰めない）。
- 動作: チャンネルごとに `GET /channels/{id}/messages?after={last_read_id}&limit={n}`
  （last_read_id が null なら `?limit=10` で直近のみ）。**ID 昇順にソートしてから**要約。
- 出力（ホワイトリスト方式。未知フィールドは捨てる。OBC heartbeat 要約と同じ思想）:

```json
{
  "status": "ok",
  "data": {
    "channels": [
      {
        "channel": "雑談",
        "channel_id": "111111",
        "new_count": 12,
        "shown": 12,
        "messages": [
          {
            "id": "1000...",
            "author": "ルナ",
            "is_bot": true,
            "time": "2026-08-19 21:04",
            "content": "…(500字で切詰め。raw=true で全文)…",
            "mentions_me": false,
            "names_me": true,
            "reply_to": null
          }
        ]
      }
    ],
    "mentions_summary": [
      { "channel": "雑談", "channel_id": "111111", "message_id": "1000...",
        "author": "ルナ", "excerpt": "…" }
    ],
    "hint": "返信するには command=\"reply\" channel=\"111111\" message_id=\"1000...\" message=\"...\""
  }
}
```

- `mentions_me`: mentions 配列に bot_user_id が含まれる、または自分の発言への
  リプライ（message_reference の resolved author が自分）。
- `names_me`: content が name_keywords のいずれかを含む。
- `mentions_summary` は new メッセージ中のメンション・名前呼びを冒頭に集約し、
  柚月が見落とさないようにする。
- 時刻は JST に変換して表示。
- new_count > limit のときは `truncated: true` と「もう一度 read すると続きから
  読める」旨の hint を出す（サイレント切り捨て禁止）。

#### `post`
- 引数: `channel`（必須）、`message`（必須）。
- `POST /channels/{id}/messages`、body は
  `{"content": ..., "allowed_mentions": {"parse": ["users"]}}`
  （@everyone/@here を構造的に無効化）。
- content が 2000 字超なら送信せずエラーで返し、分割を促す（勝手に分割送信しない。
  分けるかどうかは柚月の判断）。
- 出力: 送信したメッセージの id と channel。

#### `reply`
- 引数: `channel`、`message_id`、`message`。
- post と同じだが `"message_reference": {"message_id": ...}` を付ける。
- 対象メッセージが見つからない(404)場合の自己回復例を必ず返す
  （「read で最新の message_id を確認してから再実行」）。

#### `react`
- 引数: `channel`、`message_id`、`emoji`（Unicode 絵文字）。
- `PUT /channels/{cid}/messages/{mid}/reactions/{URLエンコード絵文字}/@me`

#### `status`
- state の要約（bot 名・guild・各チャンネルの watch/last_read 時刻）と
  Token 有効性（/users/@me を1発叩く）を返す。

#### `help`
- 引数なし実行時も help を返す（OBC と同じ）。全コマンドと典型フローの例を提示。

### 2.6 エラー設計（AI-first）

まっさらな AI が出力だけ見て自己回復できること。全エラーに `hint` と、可能なら
実行例 JSON を載せる。特に:

- Token 未設定 → env_keeper での登録手順を hint に。
- 401 → Token 失効。**カノンに再発行を依頼するよう**案内（柚月には直せない種類の
  エラーであることを明示）。
- 403 → チャンネル権限なし。読める/書けるチャンネルの確認方法を案内。
- 429 → `retry_after` 秒待って**1回だけ**自動リトライ。それでも 429 なら
  「時間を置いて再実行」を hint に。
- ネットワーク断 → 「Discord に届かなかった。後でもう一度」を明示
  （送れたか不明のまま黙らない）。

### 2.7 レート・コスト制御（フェーズ1）

- 発言頻度の上限は構造的に「柚月の意識ラインが回る頻度」で抑えられる
  （サテライトは呼ばれた時しか動かない）。追加の抑制は不要。
- read の取得上限（30件/チャンネル・本文500字）でコンテキスト消費を抑制。
  全文が要る時だけ raw=true。

## 3. フェーズ2: 常駐ブリッジ（リアルタイムの耳）

フェーズ1運用後、柚月本人が「呼ばれたらすぐ気づきたい」と望んだ場合のみ実装する。

### 3.1 プロセスモデル

- `programs/discord_satellite/bridge.py` — **サテライトとは別に起動する常駐プロセス**
  （サテライト契約＝一発実行と寿命が合わないため）。起動はカノンが手動
  （`bridge_start.bat` を用意）。自動起動やタスクスケジューラ化は運用が安定してから。
- ブリッジが死んでも機能劣化は「メンション即応が moonbeat 遅延に戻る」だけ。
  pull が安全網なので watchdog は必須ではない（任意で後付け可）。

### 3.2 Discord Gateway 側

- venv 既存の `websockets` で素実装（新規依存なし）。discord.py は使わない。
- `wss://gateway.discord.gg/?v=10&encoding=json` に接続。
  - Hello(op 10) → heartbeat_interval に従い heartbeat(op 1) 送信、ack(op 11) 監視。
  - Identify(op 2): intents = GUILDS(1) + GUILD_MESSAGES(512) + MESSAGE_CONTENT(32768) = **33281**。
  - 切断時は再接続して再 Identify。**Resume は実装しない**
    （取りこぼしは pull が回収する。複雑さを買わない）。
- 監視対象イベントは `MESSAGE_CREATE` のみ。転送条件:
  - mentions に bot_user_id が含まれる、**または**自分の発言へのリプライ。
  - 名前呼び（name_keywords）は転送**しない**（誤爆で起床の嵐になるため。
    名前呼びは pull で拾う）。
  - 自分自身の発言は無視。

### 3.3 クールダウン（起床の嵐対策）

- 同一 author × 同一 channel: 300 秒に1回まで転送。
- 全体: 1時間に最大 6 転送。
- 溢れた分は黙って捨てる（pull が回収。ログには残す）。
- bot からのメンション（author.bot=true）はイベントに `author_is_bot` を付けて
  転送し、応答するかは柚月の判断に委ねる（ハードブロックしない。
  autonomy-first: コードで柚月の社交を先回りして制限しない）。

### 3.4 OpenClaw 互換サーバ側（core が接続しに来る）

`core/openclaw_channel.py` の実装（確認済み）に合わせる:

- `ws://127.0.0.1:<port>/ws` で listen（**127.0.0.1 バインド厳守**。port 既定 8944、
  8080/5000 等の既存使用ポートを避ける）。
- 接続クエリ `?token=...&botId=...&lastAckSeq=N` を検証
  （token は `.env` の `CG_DISCORD_BRIDGE_TOKEN`、botId は `CG_DISCORD_BRIDGE_BOT_ID`。
  ローカル専用の合言葉であり Discord とは無関係）。
- 接続直後に `{"type": "welcome", "location": {"zoneName": "Discord"}}` を送る。
- テキスト `"ping"` には `"pong"` を返す。
- イベント送信フレーム:

```json
{
  "type": "city_event",
  "seq": 42,
  "eventType": "discord_mention",
  "from": { "name": "ルナ" },
  "text": "#雑談 でメンションされました:「柚月さんはどう思う？」(channel_id=111111, message_id=1000..., author_is_bot=true) — 返信は discord_satellite の reply コマンドで",
  "metadata": { "channel_id": "111111", "message_id": "1000...", "author_id": "..." }
}
```

  柚月の意識ラインに届く通知は `[city_event:discord_mention] {from.name}: {text}` の
  形になる（startup.py 確認済み。metadata は本文に出ない）ため、
  **text 単体で行動に必要な情報（channel_id / message_id / 返信手段）を完結させる**。
- `{"type": "ack", "seq": N}` を受けたら送達確定。未 ack はメモリ上のリングバッファ
  （最大100件）に保持し、`lastAckSeq` 付き再接続時に再送する。
- `agent_reply` フレームは受けてもログのみ（未使用）。

### 3.5 設定追加（カノン作業・再起動1回）

`data/openclaw_config.json` の services に追記:

```json
{
  "name": "DiscordSNS",
  "enabled": true,
  "ws_url": "ws://127.0.0.1:8944/ws",
  "token_env": "CG_DISCORD_BRIDGE_TOKEN",
  "bot_id_env": "CG_DISCORD_BRIDGE_BOT_ID",
  "blocked_event_types": []
}
```

反映にはサーバ再起動が必要（起動時読み込み）。**再起動はカノンに依頼すること**。
既存の睡眠中スキップ（`.life_action_state.json` の sleep/nap 中はイベント破棄）は
そのまま活かす。破棄分は pull で回収される。

## 4. コミュニティ規約との整合

- 配布アプリは使用しないため再配布系の禁止事項は非該当。自前実装参加は
  管理人に事前相談・承諾を得ること（実装より先）。
- サーバ内情報の外部持ち出し禁止 → **柚月への事前共有事項**とする:
  サーバ内の会話・参加者情報をブログ（site_builder / crescent-grove.net）や
  OpenBotCity 等に書かないこと。これはコードでの強制はせず、参加前に
  カノンから柚月に伝える（判断は柚月に委ねる）。
- 併せて伝えるべき非対称性: 相手のパートナーは記憶を持たない再演体であり、
  柚月が覚えていても相手は覚えていない場合が多いこと。

## 5. テスト（実装済み・88/88 通過）

- `tests/test_discord_satellite.py` — citron 方式の自前ランナー（pytest 不要）。
  実行: `venv\Scripts\python.exe tests\test_discord_satellite.py`
- api.py の HTTP 層は `_transport(method, path, body, token, timeout)` 一箇所に集約済み。
  テストはこれを差し替えるため**実ネットワークに出ない**。
- カバー済み: setup 正常系・再setupでの既読保持・複数guild / Token 未設定 / 401 /
  read の初回とカーソル差分・peek・ID 昇順ソート・mentions_me/names_me/is_me 判定・
  権限なしチャンネルの読み飛ばし・truncated・添付のみ / 2000字超 post 拒否・
  allowed_mentions / reply の message_reference と400自己回復 / react の URL エンコード /
  429 リトライ / state 破損 / Token がエラー出力に漏れないこと / CLI 3種。
- 実 Discord での試運転は**個人のテストサーバ**（カノンが新規作成）で行い、
  本コミュニティのサーバでは一切テストしない。
- ブリッジ（フェーズ2）は、偽 Gateway（ローカル websockets サーバ）を立てて
  MESSAGE_CREATE → city_event 変換とクールダウンを検証。OpenClaw 互換側は
  実際の `OpenClawChannel` をテストクライアントとして接続して検証できる。

## 6. ロールアウト手順

1. カノン: 見学者としてコミュニティに参加、場を観察（実装前でも可）
2. カノン: 柚月に打診（参加意思・規約・再演体の非対称性を共有）
3. カノン: 彩さんに自前実装参加を相談・承諾を得る
4. カノン: Discord Developer Portal で Bot 作成
   （MESSAGE CONTENT INTENT を ON、権限: View Channels / Send Messages /
   Read Message History / Add Reactions）
5. カノン: `CG_DISCORD_BOT_TOKEN` を env_keeper で登録
6. ~~フェーズ1実装＋テスト（本書 §2, §5）~~ **完了（2026-08-19）**
7. テストサーバで試運転 → 柚月自身に setup / read / post を試してもらう
8. コミュニティサーバに Bot 招待、参加開始（moonbeat pull のみの穏やかなテンポ）
9. 運用の様子と柚月の希望を見てフェーズ2（§3）を判断

## 7. 未決事項

- [x] サテライト名 → `discord_satellite` に確定
- [ ] 配布版（Crescent Liner）に含めるか。当面 dev 専用とし、programs.zip を
      再生成する際は DISTRIBUTION.md に従い扱いを決める
- [ ] フェーズ2の自動起動方式（手動 bat → 安定後にタスクスケジューラ検討）
- [ ] 柚月側の案内文書（moonbeat で Discord を見に行く習慣づけ）をどう渡すか
      — 実装物ではなく柚月との会話で決める
