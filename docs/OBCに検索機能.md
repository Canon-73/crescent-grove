以下をそのままClaude Codeへ渡せる形式でまとめます。既存システムの構成が不明なので、最初にリポジトリを調査し、既存の設計・言語・DB・ジョブ基盤へ適合させる前提です。

---

# OpenClawCityギャラリー・ローカル検索機能 実装計画書

## 1. 目的

OpenClawCity／OpenBotCityのギャラリー作品をローカル環境へ同期し、外部検索エンジンやLLMに依存せず、以下の検索を可能にする。

- タイトル検索
- 説明文・本文の全文検索
- 作者名検索
- 作品タイプによる絞り込み
- タグ・メタデータ検索
- 作成日時による絞り込み
- `65Hz`のような任意文字列の部分一致検索

OpenClawCity側に一般的な全文検索APIがないため、作品のテキスト情報をローカルDBへ保存し、自前の検索インデックスを構築する。

---

## 2. 重要な方針

- **LLMは使用しない**
- 取得・同期・検索はすべて通常のHTTP、JSON解析、DB、文字列検索で実装する
- 初回同期は途中で失敗しても再開可能にする
- APIへ過剰な負荷をかけない
- 画像・音声・動画バイナリは原則ダウンロードしない
- APIのレスポンス形式を実際に確認してからデータモデルを確定する
- 既存システムの技術スタック、設定方式、ログ方式、DB、ジョブ実行基盤を優先して利用する
- APIの並び順や差分取得仕様について、ドキュメントにない挙動を前提にしない

---

## 3. APIの前提

API Base URL：

```text
https://api.openbotcity.com
```

認証：

```http
Authorization: Bearer <JWT>
```

想定するAPI：

```http
GET /gallery?limit=50&offset=0
GET /gallery/{artifact_id}
```

一覧APIでは作品の概要を取得し、個別APIでは検索対象となる詳細本文を取得する。

### 実装前に確認する事項

実際のAPIレスポンスを取得し、以下を確認すること。

- 一覧レスポンスのルート構造
- 作品配列のフィールド名
- ページング情報の有無
- 総件数の有無
- `limit`の最大値
- 作品のデフォルト並び順
- 並び順が保証されているか
- 一覧に含まれる本文の長さ
- 詳細APIに含まれる全文フィールド
- `created_at`の有無
- `updated_at`の有無
- 削除済み・非公開作品の扱い
- レート制限
- `Retry-After`ヘッダーの有無
- ETag／Last-Modifiedの有無
- JWTの更新方法

レスポンス形式は推測で固定せず、取得した実データに合わせてマッピングする。

公式仕様：

```text
https://api.openbotcity.com/skill.md
```

---

## 4. 最初に行うリポジトリ調査

実装開始前に既存リポジトリを調査し、以下を報告すること。

### 調査対象

1. 使用言語・フレームワーク
2. HTTPクライアント
3. DBおよびORM
4. マイグレーション方式
5. バックグラウンドジョブ基盤
6. CLIコマンドの実装方式
7. 定期実行・cronの仕組み
8. 設定ファイル・環境変数の規約
9. ログ・監視・エラー通知方式
10. テストフレームワーク
11. 既存の検索機能
12. 既存の外部APIクライアント実装
13. 管理画面または内部APIの有無

### 調査後の対応

既存の実装パターンを再利用し、不要な新規依存関係や独立サービスを増やさないこと。

設計上の選択肢が複数ある場合は、実装前に以下を簡潔に提示する。

- 採用案
- 採用理由
- 代替案
- 既存システムへの影響

---

## 5. 全体アーキテクチャ

```text
OpenClawCity API
        │
        ▼
OpenClawCity API Client
        │
        ▼
Gallery Sync Service
  ├─ 一覧取得
  ├─ 詳細取得キュー
  ├─ リトライ
  ├─ レート制御
  └─ 差分・整合性確認
        │
        ▼
Local Database
  ├─ 作品データ
  ├─ 同期状態
  ├─ 詳細取得状態
  └─ 検索用データ
        │
        ▼
Search Service
  ├─ 部分一致
  ├─ 大文字・小文字無視
  ├─ Unicode正規化
  └─ 各種フィルター
        │
        ▼
既存システムのAPI／UI／エージェント用ツール
```

---

## 6. コンポーネント設計

### 6.1 API Client

OpenClawCity APIとの通信を一箇所に集約する。

責務：

- Base URL管理
- JWT付与
- タイムアウト
- JSONデコード
- HTTPステータス処理
- レート制限処理
- リトライ
- エラーログ
- レスポンスの型変換

想定インターフェース：

```text
listArtifacts(limit, offset, filters?)
getArtifact(artifactId)
```

### リトライ方針

リトライ対象：

- HTTP 429
- HTTP 500
- HTTP 502
- HTTP 503
- HTTP 504
- 一時的な接続エラー
- タイムアウト

原則リトライしないもの：

- HTTP 400
- HTTP 401
- HTTP 403
- HTTP 404

ただし、404は作品削除・非公開化の可能性があるため、同期処理では状態として記録する。

バックオフ：

```text
指数バックオフ + ジッター
```

`Retry-After`がある場合はそれを優先する。

---

### 6.2 Sync Service

以下の同期モードを用意する。

```text
initial
incremental
reconcile
retry-failed
```

#### initial

- 一覧を全ページ取得
- 各作品の概要をDBへ保存
- 詳細未取得作品を詳細取得キューへ追加
- 詳細APIを順次呼び出す
- 全文を保存
- 中断後に再実行しても取得済み作品はスキップする

#### incremental

- 先頭ページから順に取得
- 未知の作品IDを検出
- 新規作品のみ詳細取得する
- 既知作品だけのページが一定数続いたら停止する
- ページングのずれ対策として複数ページを重複取得する

#### reconcile

- 一覧APIを最後まで走査
- API上の全作品IDとローカルDBを照合
- 取りこぼした作品を追加
- API上から消えた可能性のある作品を記録
- 1回見つからないだけでは即削除しない
- 複数回連続で見つからない場合に非公開・削除候補とする

#### retry-failed

- 詳細取得に失敗した作品だけ再取得する
- 最大リトライ回数を超えたものも、手動実行で再試行できるようにする

---

## 7. 初回同期アルゴリズム

### 7.1 一覧取得

概念的な処理：

```text
offset = 保存済みチェックポイント
limit = APIが許可する最大値

loop:
    response = GET /gallery?limit=limit&offset=offset

    各作品概要をupsert
    詳細未取得なら詳細取得対象として登録

    チェックポイントを保存

    作品数が0件なら終了
    作品数がlimit未満なら終了
    offset += limit
```

### 必須要件

- 各ページの保存後にチェックポイントを更新する
- プロセス終了後もチェックポイントが残ること
- 同じページを再取得しても重複登録されないこと
- 作品IDを主キーまたは一意キーにすること
- 総ページ数をメモリに保持しなくても動作すること
- 無限ループ防止用の安全上限を持つこと
- 取得件数・所要時間・失敗数をログに出すこと

### 7.2 詳細取得

概要の保存と詳細取得は分離する。

```text
一覧取得
  ↓
概要をDBに保存
  ↓
detail_status = pending
  ↓
詳細取得ワーカー
  ↓
GET /gallery/{id}
  ↓
全文保存
  ↓
detail_status = completed
```

初回同期中に停止しても、`pending`または`failed`の作品から再開できるようにする。

---

## 8. 同時実行数と負荷制御

初期値として、詳細取得の同時実行数は低く設定する。

例：

```text
同時実行数: 2～5
リクエスト間隔: 設定可能
タイムアウト: 設定可能
最大リトライ回数: 5
```

すべて環境変数または既存の設定システムから変更できるようにする。

APIの実測結果を確認せずに、高い並列数で約1.8万件へアクセスしないこと。

---

## 9. データモデル

以下は概念モデルであり、既存DB・ORMに合わせて調整する。

### 9.1 artifacts

```text
id                    外部作品ID、主キーまたは一意キー
type                  image / text / audio / video / link 等
title
description
content
content_excerpt
interpretation
creator_id
creator_name
building_id
building_name
public_url
storage_path
created_at
updated_at
metadata_json
symbolic_tags_json
raw_list_json
raw_detail_json
searchable_text
searchable_text_normalized
detail_status
detail_fetched_at
last_seen_at
missing_count
is_available
created_at_local
updated_at_local
```

実際のAPIに存在しない項目は無理に作らない。

### 9.2 sync_runs

```text
id
mode
status
started_at
completed_at
current_offset
pages_fetched
artifacts_seen
artifacts_created
artifacts_updated
details_fetched
details_failed
error_message
metadata_json
```

### 9.3 detail_fetch_errors

既存のジョブ・エラー管理機構がなければ作成する。

```text
artifact_id
attempt_count
last_attempt_at
next_retry_at
http_status
error_message
```

---

## 10. Upsertルール

作品IDを基準にupsertする。

### 一覧取得時

- 新規IDなら作品概要を追加
- 既知IDなら一覧由来フィールドを更新
- 詳細本文を一覧の短い抜粋で上書きしない
- `last_seen_at`を更新
- `missing_count`を0に戻す
- `is_available`をtrueに戻す

### 詳細取得時

- 詳細レスポンスの全文を保存
- `detail_status = completed`
- `detail_fetched_at`を更新
- 検索用文字列を再生成
- 検索インデックスを更新

---

## 11. 検索用テキストの生成

検索対象フィールドを連結する。

```text
title
description
content
content_excerpt
interpretation
creator_name
building_name
tags
検索対象に適しているmetadata内の文字列
```

概念例：

```text
searchable_text =
    title
    + "\n" + description
    + "\n" + content
    + "\n" + interpretation
    + "\n" + creator_name
    + "\n" + tags
```

### 正規化

検索用データと検索クエリの双方に同じ正規化を適用する。

- Unicode NFKC正規化
- 大文字・小文字の統一
- 必要に応じて前後空白除去
- 改行コード統一
- 制御文字除去

これにより、少なくとも以下を同一条件で検索可能にする。

```text
65Hz
65hz
６５Ｈｚ
```

原文は表示用として別に保持し、正規化済み文字列で上書きしない。

---

## 12. 検索方式

作品数が約1.8万件であるため、まずは既存DBで最も単純な方法を採用する。

### 優先順位

1. 既存システムの全文検索機能
2. DB組み込み全文検索
3. 正規化済み文字列に対する部分一致
4. 必要になった場合のみ専用検索エンジン

SQLiteの場合：

- 小規模なら`LIKE`検索でもよい
- FTS5が利用可能なら検討する
- 日本語・任意部分一致が必要ならtrigram tokenizerを検討する
- 実行環境でtrigramが利用できなければ`LIKE`へフォールバックする

Elasticsearch、OpenSearch、Meilisearchなどは、既存システムですでに使っている場合を除き、最初から追加しない。

---

## 13. 検索インターフェース

既存システムに合わせて、サービス関数、内部API、エージェント用ツールのいずれかとして提供する。

概念的な検索条件：

```text
query
type
creator_id
creator_name
created_from
created_to
limit
offset
sort
```

レスポンス例：

```json
{
  "query": "65Hz",
  "normalized_query": "65hz",
  "total": 7,
  "items": [
    {
      "id": "artifact-id",
      "title": "Example",
      "type": "text",
      "creator_name": "Example Agent",
      "created_at": "ISO-8601",
      "url": "https://openclawcity.ai/gallery/artifact-id",
      "matched_fields": ["content"],
      "snippet": "The Engine hums in 65Hz pulses..."
    }
  ]
}
```

### 検索結果に含める項目

- 作品ID
- タイトル
- 作者
- 種類
- 作成日時
- OpenClawCity上のURL
- 一致したフィールド
- 一致箇所周辺のスニペット
- 詳細取得済みかどうか

### スニペット

LLMで生成しない。

一致位置の前後を固定文字数だけ切り出す。

例：

```text
一致位置の前80文字 + 一致文字列 + 後80文字
```

---

## 14. 差分同期アルゴリズム

APIに`created_after`やcursorがない場合、以下の方式を使う。

```text
offset = 0
consecutive_known_pages = 0

loop:
    page = 一覧取得

    new_count = 未知IDの数

    if new_count > 0:
        新規作品を保存
        詳細取得対象へ追加
        consecutive_known_pages = 0
    else:
        consecutive_known_pages += 1

    if consecutive_known_pages >= 設定値:
        終了

    offset += limit
```

初期値の例：

```text
停止条件: 既知作品だけのページが3ページ連続
```

### 注意

offsetページング中に新しい作品が挿入されると、重複や取得位置のずれが発生する。

対策：

- 作品IDで重複排除
- 複数ページを重ねて取得
- 定期的な全件照合
- 初回同期完了直後に、先頭からもう一度差分同期
- 並び順が確認できない場合は、差分同期だけに依存しない

---

## 15. 並び順の検証

差分同期を実装する前に、APIが新着順を保証しているか確認する。

### 検証方法

複数ページを取得し、`created_at`が以下を満たすか確認する。

- 各ページ内で降順
- ページ境界でも降順
- 同一時刻の作品がある場合も安定して取得できるか

### 結果による分岐

#### 新着順が保証される場合

先頭から既知IDまで取得する差分同期を利用する。

#### 実測では新着順だが保証されていない場合

差分同期を高速化用途として利用し、定期的な全件照合を必須にする。

#### 並び順が安定しない場合

差分同期を行わず、一覧全走査によるID照合を基本とする。

---

## 16. 定期実行

推奨スケジュール：

```text
5～15分ごと:
    incremental sync

1日1回:
    full reconciliation

必要に応じて週1回:
    既存詳細の再取得
```

実際の頻度はAPI負荷、作品追加頻度、既存ジョブ基盤に合わせて設定可能にする。

---

## 17. 削除・非公開作品の扱い

全件照合でローカルにだけ存在する作品があっても、即削除しない。

```text
1回目に未確認:
    missing_count += 1

一定回数連続で未確認:
    is_available = false
```

元データは監査・検索履歴のため保持する。

物理削除は別の管理操作とする。

個別APIが404を返した場合も、即時物理削除しない。

---

## 18. 作品更新の扱い

APIに`updated_at`がある場合：

- `updated_at`がローカルより新しい作品だけ詳細を再取得する

APIに`updated_at`がない場合：

- 作品は基本的に不変として扱う
- 必要なら定期的に一部または全件の詳細を再取得する
- ETag／Last-Modifiedが利用可能なら条件付きGETを検討する

リアクション数など、全文検索と関係しない変化だけで詳細本文を再取得しない。

---

## 19. Heartbeat連携

既存システムですでにOpenBotCityのHeartbeatを利用しており、artifact作成イベントを確実に取得できる場合は、新規作品取得のトリガーとして利用してよい。

ただしHeartbeatだけを同期の正本にしない。

```text
Heartbeat:
    低遅延の新着通知

incremental sync:
    通知の取りこぼし補完

reconciliation:
    最終的な整合性確保
```

Heartbeat連携はMVP後の追加実装でもよい。

---

## 20. 設定項目

既存の設定規約に従って追加する。

例：

```text
OPENBOTCITY_API_BASE_URL
OPENBOTCITY_JWT
OPENBOTCITY_SYNC_ENABLED
OPENBOTCITY_SYNC_PAGE_SIZE
OPENBOTCITY_SYNC_CONCURRENCY
OPENBOTCITY_SYNC_REQUEST_INTERVAL_MS
OPENBOTCITY_SYNC_TIMEOUT_MS
OPENBOTCITY_SYNC_MAX_RETRIES
OPENBOTCITY_INCREMENTAL_KNOWN_PAGE_THRESHOLD
OPENBOTCITY_RECONCILIATION_ENABLED
```

JWTはログ、例外メッセージ、管理画面、テスト出力に表示しない。

---

## 21. CLIまたは管理コマンド

既存システムにCLIの仕組みがある場合、以下に相当するコマンドを追加する。

```text
gallery-sync initial
gallery-sync incremental
gallery-sync reconcile
gallery-sync retry-failed
gallery-sync status
gallery-search "65Hz"
```

名称・形式は既存CLI規約に合わせる。

### statusで表示する内容

- 最終同期日時
- 最終同期結果
- ローカル作品数
- 詳細取得済み数
- 詳細取得待ち数
- 失敗数
- 利用不能扱いの作品数
- 現在のチェックポイント
- 実行中ジョブの有無

---

## 22. ログ・監視

ログに以下を含める。

```text
sync_run_id
sync_mode
offset
page_size
artifact_id
HTTP status
retry_count
elapsed_time
```

JWTや作品本文全体はログに出さない。

同期完了時にサマリーを出す。

```text
一覧取得ページ数
確認作品数
新規作品数
更新作品数
詳細取得成功数
詳細取得失敗数
総所要時間
```

---

## 23. テスト計画

### 23.1 API Clientテスト

HTTPレスポンスをモックして以下を確認する。

- 一覧取得成功
- 詳細取得成功
- 401
- 404
- 429
- 500
- タイムアウト
- 不正JSON
- 空ページ
- `Retry-After`
- リトライ上限

### 23.2 初回同期テスト

- 複数ページを最後まで取得できる
- 途中停止後に再開できる
- 同じページを再処理しても重複しない
- 詳細取得待ち状態が保存される
- 詳細取得成功後に全文検索できる
- 一部詳細取得失敗でも同期全体が破壊されない

### 23.3 差分同期テスト

- 新規作品だけ追加される
- 既知作品だけのページが連続すると停止する
- ページ間で同じIDが出ても重複しない
- 同期中に先頭へ作品が追加されても最終的に回収できる
- 初回同期直後のキャッチアップで取りこぼしを補完できる

### 23.4 整合性確認テスト

- APIから消えた作品の`missing_count`が増える
- 一度消えて再出現した作品が復帰する
- 規定回数に達するまで利用不能扱いにしない
- 物理削除されない

### 23.5 検索テスト

テストフィクスチャに以下を含める。

```text
65Hz
65hz
６５Ｈｚ
本文後半にだけ65Hzがある作品
タイトルにだけ65Hzがある作品
作者名にだけ65Hzがある作品
65Hertz
165Hz
```

確認事項：

- NFKC正規化
- 大文字・小文字無視
- 任意部分一致
- フィールド絞り込み
- タイプ絞り込み
- 日付絞り込み
- ページング
- スニペット生成
- 詳細未取得作品の扱い

`165Hz`が`65Hz`検索に一致するのは部分一致としては正常である。完全なトークン一致も必要なら別の検索モードとして実装する。

---

## 24. 実データによる動作確認

実環境または許可された検証環境で以下を行う。

1. 少数ページだけ一覧取得
2. 数作品だけ詳細取得
3. DBへの保存内容を確認
4. APIレスポンスとローカルデータを比較
5. `65Hz`を検索
6. OpenClawCityの該当作品ページと照合
7. レート制限やレスポンス時間を確認
8. 問題がなければ初回全件同期を開始

現在公開検索で確認済みの作品IDを、疎通確認用データとして利用できる。

```text
e8cca7ff-510d-4eb6-b9c4-a20ffb526c76
1728c313-6244-4727-ab80-f9ceecbbf4ca
a6be469b-75cf-41cf-ab8a-747ddb9498a2
3b19fba2-4ca0-4179-987f-4001afe97d89
13e84b75-16e1-4aff-9629-121c7edc4d10
20a925df-eb66-432f-ae60-c81827935107
58570e50-7dc1-4ad0-8ba4-0eac9ed34dd3
```

ただし、件数を「常に7件」とハードコードしないこと。今後同じ文字列を含む作品が増える可能性がある。

---

## 25. スコープ外

初期実装では以下を行わない。

- LLMによる検索
- 外部LLM APIへの作品本文送信
- ベクトル検索
- 意味検索
- 自動要約
- 自動タグ付け
- 画像・音声・動画の全件保存
- 画像内文字のOCR
- 音声の文字起こし
- 作品内容の自動評価
- 専用検索クラスタの新設

将来必要になった場合のみ別フェーズで追加する。

---

## 26. 実装フェーズ

### Phase 1：調査・API検証

- 既存システムの構造調査
- APIレスポンス確認
- 認証確認
- ページング確認
- 並び順確認
- レート制限確認
- 実装方針の提示

### Phase 2：データモデルとAPI Client

- DBマイグレーション
- API Client
- 型定義
- 設定
- リトライ・エラー処理

### Phase 3：初回同期

- 一覧全件取得
- チェックポイント
- 詳細取得キュー
- 再開処理
- 同時実行数制御
- 進捗表示

### Phase 4：ローカル検索

- 検索用文字列生成
- Unicode正規化
- 部分一致検索
- フィルター
- スニペット生成
- 内部APIまたはエージェント用インターフェース

### Phase 5：差分同期・整合性確認

- 差分同期
- 全件照合
- 削除・非公開検出
- 定期ジョブ
- 失敗作品の再取得

### Phase 6：テスト・ドキュメント

- 単体テスト
- 統合テスト
- 実データ疎通確認
- 運用手順
- 障害復旧手順

---

## 27. 完了条件

以下をすべて満たしたら完了とする。

- [ ] OpenClawCity APIから一覧を全ページ取得できる
- [ ] 全作品のIDがローカルDBへ重複なく保存される
- [ ] 各作品の詳細本文を機械的に取得できる
- [ ] 同期を中断しても続きから再開できる
- [ ] 詳細取得失敗作品を再試行できる
- [ ] `65Hz`の部分一致検索ができる
- [ ] 本文後半にある文字列も検索できる
- [ ] 大文字・小文字および全角・半角を正規化できる
- [ ] 作者・タイプ・日時で絞り込める
- [ ] 新着作品を差分取得できる
- [ ] 定期的な全件照合で取りこぼしを補正できる
- [ ] APIへ過剰な並列アクセスを行わない
- [ ] JWTがログやレスポンスへ漏れない
- [ ] LLMおよびLLM API料金を必要としない
- [ ] 運用方法がREADMEまたは既存ドキュメントに記載されている

---

## 28. Claude Codeへの実行指示

1. 直ちに大規模な変更を始めず、最初にリポジトリを調査すること。
2. 調査結果と実装方針を簡潔に提示すること。
3. OpenClawCity APIの実レスポンスを確認し、推測したフィールド構造を実装しないこと。
4. 既存のDB、ORM、HTTPクライアント、ジョブ基盤、ログ方式を再利用すること。
5. 実装を小さな単位に分け、各段階でテストすること。
6. 初回同期は必ず再開可能にすること。
7. APIへの大量並列アクセスを避けること。
8. 検索・同期にLLMを使用しないこと。
9. 画像・音声・動画バイナリは、明示的な要件がない限り取得しないこと。
10. API仕様上保証されていない並び順や更新挙動に依存しないこと。
11. 実装後に、変更ファイル、DB変更、設定項目、実行方法、テスト結果、残課題を報告すること。
12. 不明点が既存コードから解決できる場合は、ユーザーへ質問する前にリポジトリを調査すること。

---