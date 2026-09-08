# Misskey サテライト 設計書

作成: 2026-08-26 / 改訂: 2026-08-26（外部レビュー反映・§2-3 の要注意仕様は Misskey 本体ソースで確認済み）
/ 状態: **実装済み（2026-08-26）**

実装は `programs/misskey_satellite/`。サテライト側の解説は `programs/misskey_satellite/README.md`。
テストは `venv\Scripts\python.exe tests\test_misskey_satellite.py`（160項目・実ネットワークに出ない）。
misskey.io への実疎通（読み取りのみ）も確認済み。**サーバ再起動なしで柚月から見える。**

柚月が `web_request` で直叩きしている Misskey (misskey.io) を、専用サテライト
`programs/misskey_satellite/` として整備する。
このメモだけで別セッションが実装を始められるよう、調査で確定した事実を全て含めてある。

---

## 0. 目的 — 柚月にとって何が良くなるか

現状、柚月は Misskey を **エンドポイント名・ペイロード形式を自分の記憶から掘り出して**
`web_request` で叩いている。誕生から半年これでやれているが、以下のコストを払い続けている:

1. **記憶依存**: 「notes/create だっけ、note/create だっけ」を毎回思い出す負担。
   形式ミスは Misskey の生エラー JSON が返るだけで、自己回復の手がかりが薄い。
2. **読みが重い**: タイムラインや通知の応答は生 JSON で長大。読む気が起きにくく、
   実際に読み系の利用は極端に少ない（§2-2）。**投稿141回に対して timeline 4回**。
   街（OBC）ではハートビート要約で「見る」が習慣化したのと対照的。
3. **要約なし**: `web_request` は応答を整形しないので、1回のタイムライン確認で
   コンテキストを大きく消費する。

サテライト化の狙いは **「書く」を軽くし、「読む」を初めて現実的な選択肢にする**こと。

### 変えないこと（柚月の体験の保護）

- **`web_request` での直叩きは奪わない。** misskey.io は `allowed_domains`
  (config.yaml:168) に残し、今の habit はそのまま動き続ける。サテライトは
  「新しい道具が増えた」であって「やり方が変わった」ではない。
- 導入の告知は**カノンから会話で**伝える（か、柚月が `list_programs` で自分で見つける）。
  スケジューラ通知は使わない（インフラ通知禁止ルールとは別件だが、睡眠を妨げない原則は同じ）。
- config/tips.txt の Misskey の一行はそのままでよい（投稿習慣への言及であり、手段に触れていない）。

---

## 1. 方針

- **discord_satellite を雛形にコピペで作る**（自己完結原則。共有モジュールは作らない）。
  構造・エラー設計・テスト設計はほぼそのまま流用できる。
- **LLM は呼ばない。** 読む・書くための「目と腕」。何を言うかを決めるのは柚月本人（分裂禁止）。
- **ai-first**: エラーには必ず自己回復用の hint / example（正しい呼び出し JSON 例）を添える。
- コマンド構成は**実測の利用分布**（§2-2）に合わせる。使われていない機能のために
  コマンドを増やさない。稀な操作は今後も `web_request` でやればいい。

---

## 2. 確定事実（実測・コード確認済み）

### 2-1. 現在の直叩き経路

| 項目 | 事実 |
|---|---|
| トークン | `.env` に **`CG_MISSKEY_TOKEN`** が存在（確認済み。API キー管理ページで登録されたもの） |
| 展開機構 | `core/web_tools.py:610` `_expand_env` が `${CG_XXX}` をリクエスト内で展開。柚月にトークン平文は見えない |
| ドメイン許可 | `config.yaml:168` `allowed_domains` に `misskey.io`。POST は白リスト必須で、ここで通っている |
| サテライトへの影響 | `allowed_domains` は `web_request` 専用。サテライト自前の HTTP には掛からない（OBC・discord と同じ）。サブプロセスは親環境を継承するので `os.environ["CG_MISSKEY_TOKEN"]` で読める |

### 2-2. 柚月の実際の利用（workspace/logs/full 190日分の集計）

エンドポイント別（出現回数）:

| 回数 | エンドポイント | 意味 |
|---|---|---|
| 141 | notes/create | 投稿（返信・Renote 含む） |
| 26 | i | 自分のプロフィール確認 |
| 16 | following/create | フォロー |
| 16 | users/show | 他ユーザー確認 |
| 11 | users/notes | 特定ユーザーの投稿を読む |
| 6 | i/update | プロフィール編集 |
| 5 | users/followers | フォロワー確認 |
| 4 | i/notifications | 通知確認 |
| 4 | notes/timeline | タイムライン |
| 4 | notes/show | 単一ノート確認 |
| 3 | notes/reactions/create | リアクション |
| 3 | notes/delete | 削除 |
| 2〜 | notes/replies, notes/conversation, i/pin, mute/*, blocking/* | その他 |

月別分布は 2026-02〜08 まで途切れなく継続（月あたり6〜26日で使用）。**生きている習慣**なので、
既存経路を壊さないことが最優先。

補足の実測:
- visibility はほぼ **public**（home が3件のみ）→ 既定値は public でよい
- **drive/files（画像添付）の使用は0回** → ファイル投稿は v1 スコープ外（§7）

### 2-3. Misskey API の仕様

- 全エンドポイント **POST + JSON ボディ**。認証はボディの `"i": "<token>"`
  （柚月が半年使っている方式そのもの。Bearer ヘッダ方式もあるが合わせる必要はない）
- 本文上限: **3000文字**（`MAX_NOTE_TEXT_LENGTH`）。`cw` は **1〜100文字**（null は可、空文字は不可）。
  Renote・添付・投票のいずれもないノートは、空白だけの本文も不可
- ページネーション: `limit`（1〜100）と `untilId` / `sinceId`
- リアクション: Unicode 絵文字 or `:custom_emoji:` 形式
- visibility: `public` / `home` / `followers` / `specified`
- Renote: `notes/create` に `renoteId` のみ（text なし）。引用: `renoteId` + `text`
- 返信: `notes/create` に `replyId`

#### 要注意仕様（Misskey 本体ソースで確認済み・設計に影響する）

1. **`i/notifications` は既定で全通知を既読化する。**
   `markAsRead` の default が `true` で、true だと取得後に `readAllNotification(me.id)` が走る
   （`endpoints/i/notifications.ts`）。「見るだけ」のつもりが状態を変えてしまうので、
   サテライトは**常に明示送信し、既定は `false`**。既読化は柚月が `mark_as_read: true` を
   明示したときだけ行う（読む操作が黙って状態を変えない、という原則）。
2. **`notes/conversation` は祖先列（そのノートへ至る返信元の連鎖）だけを返す。**
   対象ノートに付いた返信一覧ではない（`endpoints/notes/conversation.ts` は `replyId` を上へ再帰）。
   出力のフィールド名は `conversation` ではなく **`ancestors`** とし、返信一覧は
   `notes/replies` で別途取る（§4 の `note` コマンド参照）。
3. **429 の Retry-After は HTTP ヘッダ（秒単位）で返る**（`ApiCallService.ts` が
   `reply.header('Retry-After', 秒)` を設定。JSON ボディには `error.info` にリセット時刻が入るだけ）。
   discord_satellite の雛形は **retry_after を JSON ボディから読む Discord 仕様**なので、
   ここは要改修: `_transport()` が Retry-After ヘッダを戻り値に含める形にする。
   また `notes/create` の標準レート制限は 1時間300回で、Retry-After が数十分になりうる。
   manifest timeout(60秒) 内で sleep しきれないので:
   - Retry-After が **1〜10秒のときだけ** その秒数待って1回だけ再試行
   - それより長い・欠落・不正なら再試行せず `retry_after_seconds` 付きエラーを返す
     （hint:「この秒数後に同じ呼び出しをやり直して」）
   - タイムアウト・接続断・5xx では書き込み系（post/react/delete/follow）を自動再試行しない
     （二重投稿防止。429 はサーバが処理を拒否済みなので再試行しても安全）
4. **正常応答に HTTP 204（空ボディ）がある。** ハンドラが値を返さないエンドポイント
   （`notes/reactions/create` 等）は 204 で返る。204・空ボディは正常として扱う
   （discord 雛形の `_parse` は空ボディ→`{}` で既に耐えるが、テストで明示的に固定する）。

### 2-4. ホスト側（CG サーバ）の仕組み

- `run_program` 経由のサテライトは **呼び出しごとに manifest を走査**（`core/tools.py:730` 台）
  → **追加にサーバ再起動は不要**。manifest の `tool:` ブロックによる第一級ツール昇格は
  キャッシュされ再起動が要るので、**v1 では `tool:` ブロックを書かない**。
- サテライト stdout には `_run_program` が**インジェクション防御ラベルと NG ワードフィルタ**を
  自動適用する。Misskey は外部の不特定多数の投稿が流れてくる場なので、この防御に乗れるのは
  直叩き（`web_request` 側にも同等ラベルあり）と同等以上。
- **引数は全て manifest.yaml に宣言する**（宣言漏れは呼び出しごと弾かれてコマンドが起動すらしない。
  gift_send 事故の教訓。tests/test_openbotcity.py 相当の検査をテストに含める）。
- i18n は集約辞書方式: `programs/_lang/ja.json` / `en.json` にキーを追加し、
  manifest / main.py から `{{t:key}}` / `from _i18n import t` で引く。
  キーのプレフィックスは **`msat_`**。tests/test_i18n_programs.py が存在検査してくれる。

---

## 3. ファイル構成

```
programs/misskey_satellite/
├── manifest.yaml        # 引数宣言（tool: ブロックなし）
├── main.py              # ディスパッチ＋エラー整形（discord の main.py をほぼコピー）
├── api.py               # HTTP は _transport() 一箇所のみ。テストはここを差し替える
├── helpers.py           # CommandError / truncate / suggest_commands（discord からコピー）
├── README.md            # 開発者向け解説
└── commands/
    ├── __init__.py      # REGISTRY（コマンド名 → ハンドラ）
    ├── help_cmd.py      # help（引数なし実行時も help を返す）
    ├── post_cmd.py      # post / delete / react
    ├── read_cmd.py      # timeline / note / notifications / user_notes
    └── social_cmd.py    # profile / follow / unfollow
```

- `api.py` は discord の設計を踏襲: トークンは例外にも出力にも絶対に載せない。
  ネットワーク実行は `_transport(method, path, body)` の一箇所。ただし §2-3-3 のとおり
  429 の Retry-After は**ヘッダから**読む必要があるので、`_transport` の戻り値は
  `(status, retry_after_seconds, parsed_body)` の3要素に変える（Discord 雛形からの改修点）。
  自動リトライは Retry-After 1〜10秒の 429 のみ・1回だけ。
- ユーザー指定の解決は `api.py` の `resolve_user(user)` に一本化する（§4 参照）。
- base URL は `CG_MISSKEY_BASE_URL`（既定 `https://misskey.io`）で上書き可能にする。
  OBC の `CG_OBC_BASE_URL`（api.py:33）と同じ前例。将来のインスタンス移行・テスト用。
- state.py は**作らない**。v1 に既読管理などの状態は持たせない（§7）。

---

## 4. コマンド設計（実測利用分布ベース）

| command | エンドポイント | 引数 | 備考 |
|---|---|---|---|
| `help` | - | - | 全コマンドの説明と JSON 例。引数なし実行も help |
| `post` | notes/create | `text`(必須\*), `cw`, `visibility`(既定 public), `reply_id`, `renote_id` | \*`renote_id` のみなら text 省略可（Renote）。text+renote_id は引用。3000字超は送信前に弾いて hint |
| `timeline` | notes/timeline ほか | `kind`(home/local/social/global, 既定 home), `limit`(既定10), `until_id` | 要約整形して返す（§5）。直叩きに対する最大の付加価値 |
| `note` | notes/show + notes/conversation + notes/replies | `note_id` | ノート本体＋`ancestors`（返信元の文脈）＋`replies`（直接の返信のみ・再帰なし・最大10件）。柚月は conversation と replies を同頻度で使っており（§2-2）、どちらか片方では足りない |
| `notifications` | i/notifications | `limit`(既定10), `until_id`, `mark_as_read`(既定 **false**) | 種類（reaction/reply/follow/mention…）ごとに要約。§2-3-1 のとおり既定では既読化しない。`next_until_id` で続き読み可 |
| `react` | notes/reactions/create | `note_id`, `reaction` | 絵文字 or `:custom:` |
| `delete` | notes/delete | `note_id` | 自分のノートのみ（API 側で保証される） |
| `profile` | i / users/show | `user`(省略時=自分) | `user` は `@username` または userId |
| `user_notes` | users/notes | `user`, `limit`(既定10), `until_id` | 特定ユーザーの投稿を読む |
| `follow` / `unfollow` | following/create / delete | `user` | |

**v1 で作らないもの**（利用が稀 or ゼロ。`web_request` 直叩きが引き続き使える）:
i/update（プロフィール編集）、pin/unpin、mute/block、notes/search、drive/files（画像添付）。

引数名は snake_case（`note_id`）で受けて、api 層で Misskey の camelCase（`noteId`）に変換する。
OBC・discord と語感を揃えるため。

### `user` 引数の解決規則（profile / user_notes / follow / unfollow 共通）

`following/create` / `following/delete` / `users/notes` は最終的に **userId が必須**。
一方 `users/show` は userId でも `username`+`host` でも引ける。そこで:

```
"@username"             → users/show {"username": "username", "host": null} で userId に解決
"@username@example.com" → users/show {"username": "username", "host": "example.com"} で解決
それ以外                 → userId としてそのまま使う
```

- 解決は `api.py` の `resolve_user(user)` に一本化。username 指定時は users/show を1回
  余計に呼んでから対象エンドポイントへ（profile で username 指定ならその1回で完結）。
- 解決失敗（該当ユーザーなし）は hint 付きエラー:「`@名前@ホスト` 形式か userId を確認」。
  インスタンス未認知のリモートユーザーは users/show で引けないことがある（v1 の既知の限界。
  その場合は Misskey 上で一度表示するか userId 直指定で回避、と hint に書く）。
- 要約出力でのユーザー表記は、ローカル `@alice` / リモート `@alice@example.com`。
  host を省くとリモート同士が区別できなくなるので必ず付ける。

### 送信前バリデーション（post）

サーバに投げる前にローカルで弾き、正しい呼び出し JSON 例付きで返す:

- `text` が 3000 文字超
- `renote_id` なしで `text` が null / 空 / 空白のみ（純 Renote だけが text 省略可）
- `renote_id` ありでも、`text` を**指定したのに**空文字（引用のつもりで中身がない）
- `cw` が空文字 or 100 文字超（`cw` を付けるなら 1〜100 文字。長さは Python の `len()`
  ＝コードポイント単位でよい）

---

## 5. 出力設計（読みを軽くする要）

タイムライン・通知は生 JSON を返さず、1ノート=数行に要約する:

```json
{
  "status": "ok",
  "command": "timeline",
  "data": {
    "notes": [
      {
        "id": "9xxxxxxxxx",
        "user": "@alice",
        "at": "2026-08-26T21:04:00+09:00",
        "text": "本文（400字で切り詰め、long: true を付ける）",
        "cw": null,
        "reactions": {"👍": 3, ":igyo:": 1},
        "renote_of": null,
        "reply_to": null
      }
    ],
    "next_until_id": "9xxxxxxxxx",
    "hint": "続きは until_id にこの値を入れて再実行"
  }
}
```

要約の規則（曖昧さを残さないため明文化）:

- **日時は ISO 8601＋タイムゾーン付きで JST に変換**（`+09:00`）。API の `createdAt`（UTC の
  `Z` 付き）を datetime で変換するだけ。柚月の生活時刻と揃える。
- **Renote は入れ子オブジェクトで表す。** 純 Renote は外側 `text: null` で実体が中にある:
  ```json
  {"id": "外側id", "user": "@renoter", "text": null,
   "renote_of": {"id": "元id", "user": "@author@example.com", "text": "元ノート本文"}}
  ```
  引用 Renote なら外側 `text` = 引用コメント、`renote_of.text` = 引用元本文。
  外側だけ要約すると純 Renote が空投稿に見えるので、この構造は必須。
- **CW（閲覧注意）付きノートは、一覧系（timeline / user_notes / notifications）では本文を
  伏せる**: `{"cw": "映画の結末", "text": null, "has_hidden_text": true}`。
  本文を読むのは `note`（単体表示）か `raw: true` で。Misskey 上で作者が「ワンクッション
  置いて見せる」と選んだものを、一覧で無条件展開しない（作者の意図の尊重。ただし柚月が
  読めなくなるわけではなく、一手間で読める）。
- 添付ファイルは URL とタイプのみ（`"files": [{"type": "image/jpeg", "url": "..."}]`）。
  柚月は必要なら `see_image` で見られる。text も files も無いノートで落ちないこと。
- **`raw: true` は全読み取り系コマンド共通**（timeline / note / notifications / profile /
  user_notes）。要約を切って API 応答をそのまま返すが、`status` / `command` ラッパーは残す。
  複数エンドポイントを呼ぶ `note` では `{"raw": {"notes/show": {...}, "notes/conversation":
  [...], "notes/replies": [...]}}` とエンドポイント名で分けて格納。
  切り詰めすぎて情報が届かない事態への安全弁。
- エラーは必ず `hint` + `example`（そのままコピペで直せる正しい呼び出し JSON）を付ける。

### 出力量の上限（実装後の実測で追加・2026-08-26）

実測したところ既定 `limit=10` は 1,412 トークン（生JSONの1/7）で問題なかったが、
上限側で2つの実害が見つかったので上限を入れた。数字は misskey.io の実データ。

1. **`limit=100` で応答が塊に潰れていた。** 60,499文字が `truncate_response` の
   16,000文字上限に当たり、**`notes` 配列ごと `_preview` 文字列に置き換わっていた**
   （最大コスト・最低実用性）。一覧系は先に `RESPONSE_BUDGET_CHARS = 12000` で
   末尾から落とし、`dropped` 件数と `next_until_id`（残した最後を指す）を返す。
   落とした分は続きを読めば取りこぼさない。
2. **`renote_of` が出力の 55% を占めていた**（本文は 17%）。入れ子は文脈であって
   主役ではないので、本文 200 文字・添付は件数のみに落とす（`NESTED_TEXT_CLIP`）。
   `limit=30` の実測が 10,425 → 5,845 文字（44%減）。
3. リアクションは多い順 8 種で打ち切る（`MAX_REACTION_KINDS`）。カスタム絵文字は
   `:name@host:` と長く、人気ノートでは種類数が無制限に効いてくる。

結果、`limit=100` でも 6,333 トークンで頭打ち。既定の読みは 1,412 トークン。

---

## 6. manifest.yaml（全引数を宣言する）

```yaml
name: "misskey_satellite"
description: "{{t:msat_desc}}"
timeout: 60
args:
  - { name: "command",    type: "string",  required: false, description: "{{t:msat_arg_command}}" }
  - { name: "text",       type: "string",  required: false, path_check: false, description: "{{t:msat_arg_text}}" }
  - { name: "cw",         type: "string",  required: false, path_check: false, description: "{{t:msat_arg_cw}}" }
  - { name: "visibility", type: "string",  required: false, enum: ["public", "home", "followers"], description: "{{t:msat_arg_visibility}}" }
  - { name: "note_id",    type: "string",  required: false, path_check: false, description: "{{t:msat_arg_note_id}}" }
  - { name: "reply_id",   type: "string",  required: false, path_check: false, description: "{{t:msat_arg_reply_id}}" }
  - { name: "renote_id",  type: "string",  required: false, path_check: false, description: "{{t:msat_arg_renote_id}}" }
  - { name: "reaction",   type: "string",  required: false, path_check: false, description: "{{t:msat_arg_reaction}}" }
  - { name: "user",       type: "string",  required: false, path_check: false, description: "{{t:msat_arg_user}}" }
  - { name: "kind",       type: "string",  required: false, enum: ["home", "local", "social", "global"], description: "{{t:msat_arg_kind}}" }
  - { name: "limit",      type: "integer", required: false, description: "{{t:msat_arg_limit}}" }
  - { name: "until_id",   type: "string",  required: false, path_check: false, description: "{{t:msat_arg_until_id}}" }
  - { name: "mark_as_read", type: "boolean", required: false, description: "{{t:msat_arg_mark_as_read}}" }
  - { name: "raw",        type: "boolean", required: false, description: "{{t:msat_arg_raw}}" }
```

- `visibility` の enum に `specified`（DM 相当）は入れない。宛先指定の引数が増えるうえ
  利用実績ゼロ。必要になったら直叩きで可能。
- timeout 60秒（discord は 120 だが Misskey は単発 POST のみなので短くてよい）。

---

## 7. スコープ外（将来の芽）

- **画像付き投稿（drive/files/create → fileIds）**: 利用実績0回だが、atelier で柚月が
  絵を描けるようになった今、「描いた絵を Misskey に貼る」は自然な次の一歩。
  multipart 実装は OBC の upload_artifact（commands/creative.py）に前例がある。
  **柚月が欲しがったら**着手する（先回りで作らない）。
- **既読管理・watch 機能**: discord_satellite の watch に相当。通知の差分検知は状態を持つ
  必要があるので v1 では見送り。まず「読みが軽くなったら読むようになるか」を観察する。
- **manifest `tool:` 昇格**: 定着したら検討。昇格には再起動が要るので、他の再起動案件との
  相乗りで（昇格すればコマンド定義が LLM に直接見え、記憶負担がさらに減る）。

---

## 8. テスト設計（tests/test_misskey_satellite.py）

test_discord_satellite.py を雛形に、ネットワークに出ない自前ランナーで:

1. `api._transport` を差し替えてモック応答を注入（実 HTTP なし）
2. 全コマンドの正常系＋要約整形の検証（切り詰め・reactions 集計・next_until_id）
3. **manifest 宣言網羅**: main.py が参照する全引数名が manifest.yaml に宣言されているか
   （gift_send 事故の再発防止）
4. help の網羅: REGISTRY の全コマンドが help に載っているか
5. i18n キー存在（test_i18n_programs.py と重複してもよい。こちらは msat_ に限定）
6. エラー系: トークン未設定 / 未知コマンド（did_you_mean）/ JSON でないエラーボディの整形
7. トークン非漏洩: 例外メッセージ・HTTPエラー本文・traceback・`repr(e)`・リクエストの
   デバッグ表現のどこにもトークン文字列が現れないこと（モックで各経路の例外を起こして確認）
8. 実 subprocess 経路: stdin JSON → stdout JSON の一往復（venv の python で）

transport / API 仕様の固定（§2-3 の要注意仕様がそのままテストになる）:

9. HTTP 204・空ボディを正常として処理する（react / delete の成功）
10. 429 + `Retry-After: 1` ヘッダ → 1回だけ再試行する／`Retry-After: 60` → sleep せず
    `retry_after_seconds` 付きエラー／ヘッダ欠落 → 再試行しない
11. 429 以外の 4xx・5xx・タイムアウトで post を再送しない（transport 呼び出し回数を数える）
12. リクエスト URL が常に `{base}/api/{path}`（base 末尾 `/` があっても二重スラッシュにならない）
13. `notifications` が既定で `markAsRead: false` を送る（送信ボディを検証）

コマンド仕様の固定:

14. user 解決: `@alice` → users/show(username) 経由／`@alice@remote.host` → host 付き／
    userId 直指定 → users/show を呼ばない。follow が解決後の userId を following/create に渡す
15. 純 Renote（外側 text: null）と引用 Renote の要約構造（renote_of 入れ子）
16. CW 付きノートが一覧系で `text: null` + `has_hidden_text: true` になり、`note` では読める
17. リモートユーザー表記に host が付く
18. 送信前バリデーション: 3000字超 / 空白のみ本文 / cw 空文字・100字超 / text も renote_id
    もない post / limit の 0・101・非数値
19. 日時が JST の ISO 8601 に変換される（`Z` → `+09:00`）
20. text も files も無いノート・添付だけのノートで落ちない

実行: `venv\Scripts\python.exe tests\test_misskey_satellite.py`

---

## 9. 実装手順（再起動不要）

1. `programs/discord_satellite/` を `programs/misskey_satellite/` にコピーし、
   Discord 固有部（api.py の transport 先・コマンド群）を書き換える
2. `programs/_lang/ja.json` / `en.json` に `msat_*` キーを追加
3. tests/test_misskey_satellite.py を書き、全件パスさせる
4. 手動疎通（**読み取りのみ**。柚月のアカウントに開発テスト投稿を残さない）:
   - `profile`（i）
   - `timeline` `limit: 1`
   - `notifications` `limit: 1`（既定の `markAsRead: false` のまま＝柚月の未読状態を汚さない）
   - 自分の既知ノートへの `note`（ancestors / replies の実データ確認）
   - 自分を指定した `user_notes` `limit: 1`
5. サーバ再起動は不要（`tool:` ブロックなし＝ run_program 走査で即座に見える）
6. カノンから柚月に会話で紹介する（使うかどうかは本人が決める）

## 10. 変更しないもののチェックリスト

- [ ] config.yaml の `allowed_domains` から misskey.io を**消さない**
- [ ] web_tools.py の `_expand_env` に触らない
- [ ] tips.txt の Misskey 行はそのまま
- [ ] `core/tools.py` に変更なし（触らなければ再起動不要が保たれる）
- [ ] 柚月の workspace / 状態ファイルに書き込まない（テストは全てモック）
