# misskey_satellite

Misskey（misskey.io）を読み書きするサテライト。柚月が `run_program` 経由で呼ぶ。

設計の全文は `docs/MISSKEY_SATELLITE_DESIGN.md`。ここは実装側の要点だけ。

```
venv\Scripts\python.exe tests\test_misskey_satellite.py
```

（pytest不要・自前ランナー・実ネットワークに出ない。160項目）

---

## 位置づけ

柚月は誕生以来 `web_request` で Misskey API を直叩きしてきた（190日分のログで
notes/create 141回・timeline 4回）。**この経路は今も生きていて、奪っていない。**
misskey.io は `config.yaml` の `allowed_domains` に残してある。

このサテライトは「エンドポイント名を思い出さなくてよく、読むのが軽い」ための
選択肢を増やしただけ。どちらを使うかは柚月が決める。

---

## 構成

| ファイル | 役割 |
|---|---|
| `main.py` | ディスパッチとエラー整形。出力段でトークンを伏せる最後の砦 |
| `api.py` | HTTPは `_transport()` 一箇所のみ。トークン合流・429再試行・ユーザー解決 |
| `helpers.py` | 要約整形（ノート・通知）とバリデーション補助 |
| `commands/read_cmd.py` | timeline / note / notifications / user_notes |
| `commands/post_cmd.py` | post / react / delete |
| `commands/social_cmd.py` | profile / follow / unfollow |
| `commands/help_cmd.py` | help |

状態ファイルは持たない（既読管理をしないため）。柚月の workspace には書き込まない。

---

## Misskey 固有の落とし穴（変更する前に読む）

いずれも Misskey 本体のソースで確認済み。**元に戻すと実害が出る。**

### 1. `i/notifications` は既定で全通知を既読にする

`markAsRead` の default が `true` で、true だと取得後に `readAllNotification()` が走る。
「見るだけ」のつもりで状態が変わってしまうので、**常に明示送信し、既定は `false`**。

```python
body = {"limit": ..., "markAsRead": mark_as_read}  # mark_as_read は既定 False
```

読む操作が黙って状態を変えない、という原則。既読にするのは柚月が
`mark_as_read: true` と言ったときだけ。

### 2. `notes/conversation` は祖先列しか返さない

返すのは「そのノートが返信している元」を上へたどった連鎖であって、
**そのノートへの返信一覧ではない**。出力では `ancestors` と名付け、
返信は `notes/replies` で別に取って `replies` に入れている。
`conversation` という名前で1つにまとめると誤読される。

### 3. 429 の Retry-After は **HTTPヘッダ**（秒）

discord_satellite は retry_after を **JSONボディ**から読む（Discordの仕様）。
ここからコピペすると Misskey ではリトライが効かない。`_transport()` の戻り値を
`(status, retry_after_seconds, body)` の3要素にしてあるのはこのため。

`notes/create` の標準レート制限は1時間300回で、Retry-After が数十分になりうる。
manifest の timeout(60秒) 内に収まらないので:

- Retry-After が **1〜10秒のときだけ**待って1回だけ再試行
- それより長い・欠落・不正なら待たずに `retry_after_seconds` 付きエラー
- タイムアウト・接続断・5xx では **再試行しない**（post の二重投稿を防ぐ）

### 4. 正常応答に HTTP 204（空ボディ）がある

`notes/reactions/create` や `notes/delete` は 204 で返る。
204・空ボディは正常として `None` を返す（JSONデコードエラーにしない）。

### 5. トークンはボディの `"i"` に載る

ヘッダ方式より、うっかり応答やログへ回り込む経路が多い。防御は二重:

- `_transport()` の中だけでボディに合流させる（コマンド層はトークン入りのボディを作らない）
- `main.py` の `_emit()` が出力文字列を検査し、混じっていたら伏せる

---

## 要約の規則

読みを軽くするのがこのサテライトの主目的なので、既定では生JSONを返さない。

- **日時**: `createdAt`(UTC) → JST の ISO 8601（`2026-08-26T21:04:00+09:00`）
- **Renote**: 入れ子の `renote_of` に実体を入れる。純Renoteは外側 `text` が null なので、
  外側だけ要約すると**空投稿に見える**。引用は外側=コメント / `renote_of`=引用元
- **CW（閲覧注意）**: 一覧では本文を伏せて `has_hidden_text: true` を立てる。
  作者がワンクッション置くと決めたものを一覧で無条件に展開しない。
  `note` で名指しで開けば本文まで読める
- **添付**: `type` と `url` だけ（中身は `see_image` で見る）
- **続き読み**: `next_until_id` を返し、`until_id` に渡すと続きが読める
- **`raw: true`**: 全読み取り系で要約を切れる。複数APIを呼ぶ `note` は
  エンドポイント名ごとに格納する

---

## 出力量（読み込みすぎを防ぐ仕掛け）

実測値（misskey.io の実データ・deepseek トークナイザ）:

| 呼び出し | 文字数 | トークン |
|---|---:|---:|
| `timeline limit=10`（既定） | 2,837 | **1,412** |
| `timeline limit=10` を `raw: true` で | 21,818 | 9,486 |
| `timeline limit=20` | 5,760 | 2,977 |
| `timeline limit=100`（上限） | 12,216 | 6,333 |
| `notifications limit=10` | 930 | 443 |

**既定の読みは約1,400トークン**で、生JSON直叩きの **1/7**。柚月の 1M コンテキストに対して
0.15% 程度なので、普通に読む分には効かない。

上限（`limit=100`）でも 6,300 トークンで頭打ちになるよう、3つの仕掛けを入れてある:

1. **一覧の予算制（`RESPONSE_BUDGET_CHARS = 12000`）**
   超える分は末尾から落とし、`dropped` と `next_until_id` を返す。
   これが無いと `truncate_response` が発動し、**応答が構造ごと文字列の塊に潰れて
   `notes` 配列が消える**（実際に limit=100 でそうなっていた。60,499文字 → 塊）。
   カーソルは「残した最後」を指すので、続きを読めば取りこぼさない。
2. **入れ子は短く（`NESTED_TEXT_CLIP = 200`・添付は件数のみ）**
   Renote元・返信先は「文脈」であって主役ではない。
   対策前は `renote_of` がタイムライン出力の **55%** を占めていた（本文は17%）。
   対策後は 19% に下がり、本文（26%）が最大の構成要素になった。
3. **リアクションは多い順に8種まで（`MAX_REACTION_KINDS`）**
   人気ノートは何十種類も付き、カスタム絵文字のキーは `:name@host:` と長い。

`limit=30` の実測は 10,425文字 → **5,845文字**（44%削減）。

**予算を緩めるときは実測してから。** 上の表を再現するスクリプトは
`tests/test_misskey_satellite.py` の「出力量」系テストが構造面を、
実測は手元で `timeline` を叩いて数えるのが早い。

---

## 変更するときの注意

- **引数は必ず `manifest.yaml` に宣言する。** 宣言漏れは `_run_program` が呼び出しごと
  弾き、コマンドが起動すらしない（OpenBotCity の gift_send が実際にこれで死んでいた）。
  テストの「manifest: 引数の宣言漏れ」がコードと manifest を突き合わせて検査する
- 本文・ID・ハンドルなどパスでない文字列引数には `path_check: false` を付ける
  （`..` を含む本文がパストラバーサル検査に引っかかる）
- i18n キーは `msat_` プレフィックスで `programs/_lang/{ja,en}.json` の両方に入れる
  （キー集合が一致しないと `tests/test_i18n_programs.py` が落ちる）
- **`tool:` ブロックは書かない。** 書くと第一級ツールに昇格するが、
  昇格したツール定義はキャッシュされるので**サーバ再起動が必要になる**（柚月の生活が止まる）。
  現状は `run_program` 経由なので、追加・変更が再起動なしで反映される
- 疎通確認は読み取りのみで行う。柚月のアカウントに開発用のテスト投稿を残さない

---

## v1 のスコープ外

利用実績と必要性から外したもの。必要になったら `web_request` の直叩きで今すぐできる。

- 画像付き投稿（`drive/files/create` → `fileIds`）: 190日で利用0回。
  ただし atelier で絵を描けるようになったので、**柚月が欲しがったら**次の一歩
- 既読管理・差分検知（discord_satellite の watch 相当）
- プロフィール編集 / ピン留め / ミュート / ブロック / ノート検索
- `visibility: specified`（DM相当）
