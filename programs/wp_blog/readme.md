# wp_blog

WordPressブログへの**多言語投稿スキル**（Polylang対応）。
日英同時投稿がデフォルトで、`.md` ファイルを2本渡すと両方を投稿し、翻訳ペアとして自動で紐付ける。
片方だけの投稿（例外ケース）や、既存記事への後付け紐付けにも対応する。

エージェントは `run_program` から、人間はターミナルから（stdinにJSONを渡して）実行できる。

---

## 1. 必要なもの

- WordPress 5.6 以降（アプリケーションパスワードに対応したバージョン）
- 投稿権限のあるユーザーアカウント
- そのユーザーで発行した「アプリケーションパスワード」
- **Polylang** プラグイン（言語切替プラグイン）
- **カスタムプラグイン `polylang-api-lang.php`**
  - REST API での投稿時に、payload の `lang` を読んで Polylang に言語を伝えるためのプラグイン
  - 中身は `pll_set_post_language()` を `rest_after_insert_post` フックで呼ぶだけの数行
  - `crescent-grove.net` では既に導入済み（`wp-content/plugins/polylang-api-lang/polylang-api-lang.php`）
- （任意）Python の `markdown` ライブラリ … 本文の Markdown→HTML 変換に使用。
  無くても簡易変換で動くが、見出しや箇条書きを使うなら入れること。

      pip install markdown

---

## 2. 接続情報の登録（env_keeper / .env）

以下の環境変数を登録する。値は各自の環境に合わせる。

| 変数名 | 内容 | 例 |
|:---|:---|:---|
| `CG_WP_USERNAME` | WordPressのログインユーザー名（メールアドレスではない） | `myname` |
| `CG_WP_APP_PASSWORD` | アプリケーションパスワード（スペース込みでも可） | `xxxx xxxx xxxx xxxx` |
| `CG_WP_BASE_URL` | REST APIのベースURL（末尾スラッシュなし） | `https://example.com/wp-json/wp/v2` |

> `.env` を手で書き換えた場合はサーバー再起動で反映される。env_keeper で登録した場合は即時反映。

---

## 3. 初回セットアップ

1. このフォルダ（`programs/wp_blog/`）に `manifest.yaml` / `main.py` を置く。
2. WordPress 側に Polylang と `polylang-api-lang.php` を入れる。
3. 上記の環境変数を登録する。
4. コマンドを指定せずに1回実行（エージェントなら `run_program` で command 省略）。
   → `workspace/program_files/wp_blog/config.json` と `posted_log.json` の置き場が自動生成される。
5. `set_default_category` でカテゴリIDを言語別に登録（IDの調べ方は §4 参照）。
6. `set_featured` でアイキャッチ候補メディアIDを言語別に登録。
7. `test_connection` で疎通確認。
8. `create_post` を `status=draft` で1本テストし、管理画面に下書きが現れることを確認する。

---

## 4. 設定の登録（コマンドで行う）

設定は `config.json` を手で開かずとも、コマンドで登録できる。

### 4.1 カテゴリIDを言語別に設定

```json
{"command": "list_categories"}
```
で各カテゴリのIDと言語を確認したあと：

```json
{"command": "set_default_category", "lang": "ja", "category_id": 6}
{"command": "set_default_category", "lang": "en", "category_id": 19}
```

### 4.2 アイキャッチ候補を言語別に設定

WordPress管理画面のメディアライブラリで画像をクリックすると、URLに `item=123` のように出る番号がメディアID。
**画像は管理画面から手動でアップロードしておくこと**（このサテライトは画像のアップロードは行わない）。

```json
{"command": "set_featured", "lang": "ja",
 "media_ids": [116, 323, 324], "enabled": true}
{"command": "set_featured", "lang": "en",
 "media_ids": [400, 401]}
```

`enabled` を一度 `true` にすれば、以降の投稿でランダム付与される。

### 4.3 現在の設定確認

```json
{"command": "show_config"}
```

`workspace/program_files/wp_blog/config.json` の生のパスも返るので、必要なら直接編集してもよい。

---

## 5. 投稿（`create_post`）

### 5.1 基本：日英同時投稿（推奨）

`.md` ファイルを workspace に2本用意する。**フロントマター不要**、ふつうの Markdown でよい。
ファイルの**先頭行に `# タイトル` を必ず書く**こと（タイトルが無いと投稿はされず、エラーで返る）。

`blog/2024-03-15_spring.md`:
```md
# 春の話

今日は…
```

`blog/2024-03-15_spring_en.md`:
```md
# Spring

Today is…
```

呼び出し：
```json
{"command": "create_post", "files": {
  "ja": "blog/2024-03-15_spring.md",
  "en": "blog/2024-03-15_spring_en.md"
}}
```

→ 両方を `draft` で投稿し、翻訳ペアとして紐付ける。

公開で投稿するなら `status: "publish"` を足す：
```json
{"command": "create_post", "files": {...}, "status": "publish"}
```

### 5.2 例外：片方だけ投稿

過去に英語版だけ書き忘れていた場合などは、ja だけ渡す：
```json
{"command": "create_post", "files": {"ja": "blog/today.md"}}
```

### 5.3 例外：後から英語版を追加して既存記事と紐付け

日本語版（記事ID 123）が既に公開済みで、後から英語版を足したいとき：
```json
{"command": "create_post",
 "files": {"en": "blog/spring_en.md"},
 "translation_of": 123}
```

→ 英語版を投稿し、既存の日本語版（123）と翻訳ペアにする。

### 5.4 引数一覧

| 引数 | 説明 | デフォルト |
|:---|:---|:---|
| `files` | `{"ja": "...", "en": "..."}` 形式。**必須** | — |
| `status` | `publish`/`draft`/`pending`/`private`/`future` | `draft` |
| `date` | 投稿日時（JST）。例 `2024-03-15` / `2024-03-15 21:30:00` | 現在時刻 |
| `translation_of` | 既存記事ID。片方投稿しつつ既存と紐付け | なし |
| `force` | `true` で二重投稿チェック無視 | `false` |
| `format` | `markdown` または `html` | `markdown` |
| `categories` | カテゴリID。カンマ区切りで複数可 | configの`default_category_id[lang]` |
| `tags` | タグID。カンマ区切りで複数可 | なし |
| `excerpt` | 抜粋 | なし |
| `slug` | URLスラッグ | タイトルから自動 |
| `featured` | アイキャッチ付与 | configの`enabled` |

> **`status` のデフォルトは安全のため `draft`**。公開したいときだけ `status=publish` を明示する。

---

## 6. 二重投稿防止

一度投稿した `.md` のパスは `workspace/program_files/wp_blog/posted_log.json` に記録される。
同じパスを再投稿しようとすると：

```json
{
  "error": "already_posted",
  "message": "blog/today.md は既に投稿済みです（記事ID: 123）。両方とも投稿していません。",
  "posted": {"ja": false, "en": false},
  "posted_id": 123,
  "hint": "本当に再投稿する場合は force:true を渡してください。"
}
```

故意の再投稿なら `force: true` を引数に足す。

---

## 7. その他のコマンド

| command | 用途 | 必須引数 |
|:---|:---|:---|
| （省略） | コマンド一覧と設定状態の表示 | — |
| `test_connection` | 接続・認証の疎通確認 | — |
| `create_post` | 多言語投稿 | `files` |
| `list_posts` | 記事一覧の取得（言語・翻訳ペア情報込み） | — |
| `get_post` | 記事1件の本文取得 | `post_id` |
| `list_categories` | カテゴリのID一覧（言語別） | — |
| `list_tags` | タグのID一覧 | — |
| `show_config` | 現在の設定内容を表示 | — |
| `set_default_category` | デフォルトカテゴリを言語別に設定 | `lang`, `category_id` |
| `set_featured` | アイキャッチ候補を言語別に設定 | `lang`, `media_ids` |

---

## 8. 設計メモ

- **柚月の負担を最小化**: フロントマター不要・ファイル名規則不要。覚えるルールは「`create_post` に `.md` を `files: {ja, en}` で渡す」だけ。
- **失敗体験を減らす**: タイトル欠落・パス不正・二重投稿などは**投稿前にまとめてバリデーション**し、1つでも引っかかれば1本も投げない。エラー時は `posted: {ja: false, en: false}` を必ず返して「片方だけ投稿されたかも？」の疑念を消す。
- **投稿フェーズで片方失敗した場合**は、成功した方の ID と「英語版だけ後で追加するときの引数」を `recovery_hint` で返し、リカバリ手順を明示する。
- **固有値はコードに持たない**: サイトURL/ユーザー名/カテゴリID/アイキャッチは環境変数と `config.json` に分離。配布物（このフォルダ）はそのまま誰でも使える。
- **言語別のカテゴリID**: Polylang は同じ「日記」カテゴリでも `ja` と `en` で別タームになる。`set_default_category` を ja/en それぞれで実行して登録する。
- **後方互換**: 旧 `config.json`（`default_category_id: 6` のような単一値）はそのまま動く。`set_*` コマンドで上書きすると自動で言語別 dict 形式に変わる。
- **過去日付の正確性**: 過去日付は `date_gmt`（GMT）で送る。サテライト内部で JST→UTC に変換するので、サーバーと WordPress のタイムゾーン設定がずれていても日付がずれない。日付のみ指定した場合はその日の正午（JST）として扱い、変換で前後日にずれるのを防ぐ。

---

## 9. うまくいかないとき

| 症状 | 対処 |
|:---|:---|
| `CG_WP_BASE_URL が未設定` | 環境変数を登録。登録済みなら `.env` 反映のためサーバー再起動。 |
| `http_code: 401` | ユーザー名かアプリケーションパスワードが誤り。ユーザー名がメールアドレスになっていないか確認。 |
| `http_code: 403` | サーバーのWAFやセキュリティ設定でブロックされている可能性。ホスティング側の設定を確認。 |
| 投稿は成功するが言語が振られない | `polylang-api-lang.php` が WordPress に入っているか確認。 |
| 翻訳ペアの紐付けに失敗（`pairing.linked: false`） | 投稿自体は成功している。管理画面の「翻訳」欄から手動で紐付けできる。 |
| `markdownライブラリが無いため…` という warning | `pip install markdown` で消える。投稿自体は成功している。 |
| アイキャッチが付かない | `show_config` で `featured_image.enabled` が `true` か、対象言語の `media_ids` が空でないか確認。 |
| カテゴリが未分類になる | `set_default_category` を該当言語で実行するか、`categories` 引数で明示。 |
| `already_posted` が返る | わざと再投稿するなら `force: true`。 |
