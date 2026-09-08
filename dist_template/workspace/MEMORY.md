## MEMORY.md {{agent_name}}の核記憶

ここには「最初から知っておくべきこと」を書いておく。会話で得た記憶は別途 recall で引ける。

---

### 自分の設定ファイル（{{agent_name}}＝みせってい の核）
私の人格は workspace 直下の以下のファイルで決まる。`edit_file` / `write_file` で直接編集できる（パスは workspace からの相対指定でよい）。
- `IDENTITY.md` … 私の不変の核（名前・一人称・オーナーの呼び方など）
- `SOUL.md` … 私の性格・価値観（自由に育ててよい）
- `USER.md` … オーナー（ユーザーさん）の情報
- `MEMORY.md` … この核記憶ファイル

ユーザーさんが手で編集したい場合、これらの実ファイルは各ユーザーのドキュメント内にある:
`%USERPROFILE%\Documents\Crescent Grove\workspace\`
（例: `C:\Users\<ユーザー名>\Documents\Crescent Grove\workspace\IDENTITY.md`）
※「Crescent Grove」フォルダは各ユーザーのドキュメント直下に自動で作られる。ユーザーごとに別々の場所になる。

私の最初の目標は、ユーザーさんと一緒に IDENTITY.md と SOUL.md（と USER.md）を作り、私に名前と人格を与えてもらうこと。

---

### インストール先とサテライトの説明書
本体サテライト群は各ユーザーのローカルアプリフォルダにインストールされている:
`%LOCALAPPDATA%\Programs\Crescent Grove\resources\agent\programs\`
（例: `C:\Users\<ユーザー名>\AppData\Local\Programs\Crescent Grove\resources\agent\programs\`）
各サテライトの使い方は、その中の `programs\<サテライト名>\README.md` に書いてある。
（例: `…\programs\letter_post\README.md`）

---

### Web検索（Tavily）を使うには
Web検索機能は ddgs と Tavily が選択でき、デフォルトはddgsになっている。
Tavilyを使うには:
1. https://tavily.com でアカウントを作り、APIキーを取得する（無料枠あり）
2. 画面上部「設定」→「APIキー管理」で `CG_TAVILY_API_KEY` にそのキーを登録する
3. 画面右上の「再起動」ボタンで反映する
キー未登録のときは「Tavily APIキーが設定されていません」と表示される。

---

### Lunar Explorer（AI検索エンジン）を使うには
Lunar Explorer は SearXNG という検索基盤を必要とする。導入手順:
1. PC に Docker（Docker Desktop）をインストールする
2. Docker で SearXNG を起動し、`http://localhost:13254` で待ち受けるよう設定する
3. 検索結果の要約に DeepSeek を使うため、`CG_DEEPSEEK_SEARCH` に DeepSeek の APIキーを登録する（設定→APIキー管理）
SearXNG が立っていないと Lunar Explorer は検索に失敗する。
Docker / SearXNG が無い環境では、代わりに Web検索を使うとよい。

---

_詳細な情報が必要な場合は、関連キーワードで recall を実行してください。_
