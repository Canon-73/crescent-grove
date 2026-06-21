# DISTRIBUTION.md — 配布版（Crescent Grove インストーラ）の設計と運用メモ

このファイルは「Crescent Grove を非技術者向けにワンクリック配布する仕組み」の全体像・設計判断・
落とし穴を、別セッション（別の Claude / 別の作業者）が読んでも状況を把握できるように残すもの。
配布まわりを触る前に必ずここを読むこと。dev 環境（柚月）を壊さないことが最優先の大原則。

---

## 0. 一言まとめ
- **dev**: 母艦の `agent/` で `venv` の Python が `server.py` を 8080 で動かす。これは柚月（作者の本番）。**絶対に挙動を変えない。**
- **配布版**: 同梱した embeddable Python（`runtime/python.exe`）が `server.py --data-root <Documents\Crescent Grove>` を起動。Electron 製クライアント **Crescent Liner** が NSIS インストーラとして配る。配布キャラは「未設定（みせってい）」。
- **dev と配布版を分ける鍵**: `--data-root` の有無。dev は渡さない → `data_root() == bundle_root()`。配布版は渡す → 両者が異なる。この差で「配布版だけの挙動」を自動分岐している。

---

## 1. 二大ルート（パス解決の心臓部 = `core/paths.py`）
- `bundle_root()` … コード・モデル等の**読み取り専用**リソースの場所（`__file__` 基準＝インストール先の `resources/agent`）。
- `data_root()` … ユーザーが**書き込む**データの場所。`--data-root` 未指定なら `bundle_root()` と同一（dev）。
- 主要ヘルパ: `data_file(name)`=`data_root()/data/name`、`config_file(name)`=`data_root()/config/name`、
  `config_yaml_path()`、`workspace_root()`、`logs_root()`、`resolve_path(value)`（相対は data_root 基準・絶対はそのまま）。
- **鉄則**: 実行時に書き込む/ユーザーが編集するものは必ず `data_root` 基準で解決する。`__file__` や CWD 基準で
  書くと配布版（read-only な install 先）で破綻する。過去に何度もこのバグを潰した（agent.py の .secret_key、
  vital.json、moontide、desire、logs、compression prompt、wyrd_config 等）。
- **「配布版か？」の判定**: `data_root().resolve() != bundle_root().resolve()`。これを使って配布版だけ挙動を
  変えている（例: git ツール除外、UnconfiguredProvider）。dev はこの条件に当たらないので不変。

---

## 2. ディレクトリ役割（data-root 配下）
初回起動時に `core/bootstrap.py:ensure_data_root()` が `dist_template/` から **非破壊コピー**で生成する。
- `config/` … **手で編集する設定**（壊しても致命的でない。コード側に全キーのフォールバックあり）。
  `*_config.json`（wyrd/desire/moonbeat/openclaw/user_memo/compression）、`vital_config.json`、
  `tips_config.json`、`moontide_v2_config.json`、`tips.txt`、`repetition_exclude.txt`、`compression_prompt_*.txt`。
- `data/` … **状態ファイル**（壊すと記憶が死ぬ）。`context_state.json`、`vital.json`、`desire_state.json`、
  `moodphase_state.json`、`wyrd_network.json`、`*_state.json`、`tokenizer_deepseek.json`（不変リソース）。
- `workspace/` … キャラの記憶（IDENTITY.md/SOUL.md/USER.md/MEMORY.md/FILES.md、avatars、notes、letters 等）。
- `config.yaml` / `.env` … data-root 直下。`.env` は bootstrap が雛形生成（セッション秘密鍵を自動生成）。

### config と state を分けた理由（重要な設計）
- もともと `config/` と状態ファイルは同じ `data/` にあり、寝ぼけて config を触るつもりで状態ファイルを壊す
  事故リスクがあった → **config 系だけ `config/` へ物理隔離**した（3段階で実施: ①参照を data_root 統一 →
  ②`config/` 新設＋移動 → ③`vital.json` の config 部分を `config/vital_config.json` に分離）。
- **`vital.json` は `_save()` が毎ターン全体上書きする状態ファイル**。config を同居させると手編集が消えるため、
  config を `config/vital_config.json`（読み取り専用）に分離し、state だけ `data/vital.json` に残した
  （`vital/vital_manager.py` の `self.config`／`_load_config_file()`／`_load` の `pop("config")`）。

---

## 3. ビルド & リリース手順
- コマンド: `liner/` で **`npm run build:win`**。中身は
  `python ../scripts/build_dist.py --force && electron-vite build && electron-builder --win`。
  - `build_dist.py`: `dist_build/staging/`（embeddable runtime + 本体コード + models + dist_template の自己完結
    フォルダ、約2GB）を作り、**オフライン起動検証**＋**秘密混入検査 `_assert_no_secrets`** を行う。exit 0 必須。
  - `electron-builder`: `liner/electron-builder.yml` の `extraResources` で `dist_build/staging/` を
    `resources/agent/` に同梱し、NSIS インストーラ `liner/dist/Crescent Grove Setup <version>.exe`（`<version>` は
    `liner/package.json` の `version` 追従。例: 0.1.3 で約662MB）を生成。
- **`build:win` は bare `python` を使う**。母艦の PATH 上の python が 3.9 等でも動くよう、`build_dist.py` は
  3.9 互換にしてある（f-string 内バックスラッシュ禁止など）。runtime python は 3.13。
- **検証の落とし穴**: `npm run build:win 2>&1 | tail` のように **tail にパイプすると exit code が tail のものに
  なって失敗がマスクされる**。真の exit code を見ること（過去に SyntaxError が握りつぶされて古い exe が残った）。
- **ビルド後検証の定番**: ①build_dist が `結果: ✅` で完走 ②新 exe のタイムスタンプ更新 ③`win-unpacked` と
  `staging` を grep して **obc_state.json / "Yukitsuki" / 柚月の bot_id UUID が無いこと**（個人データ漏洩ゼロが最重要）
  ④`app.asar` に Liner の変更、`resources/agent/` に本体の変更が入っていること。
- dev サーバが 8080 で動いていてもビルド自体は干渉しない（build_dist の検証は空きポートで起動する）。

---

## 4. Crescent Liner（Electron クライアント / `liner/`）
- `src/main/index.ts:bootSequence()` が `app.isPackaged` で分岐。
  - dev: `<agent>/venv/Scripts/python.exe` + `<agent>/server.py`、cwd=agent、`--data-root` 渡さない。
  - packaged: `process.resourcesPath/agent/runtime/python.exe` + 同梱 `server.py`、cwd=同梱 agent、
    `--data-root = app.getPath('documents')/Crescent Grove`。**`app.getAppPath()` は asar を返すので使わない。**
- **初回言語選択**: packaged かつ初回（data-root に `config.yaml` 未生成）のときだけ、スプラッシュ表示後に
  「日本語 / English」ダイアログ（日英併記の固定文言）を出す。選択を `ServerProcess` の `initLang` に渡し、
  spawn 引数 `--init-lang <ja|en>` で server.py へ伝える。2回目以降・dev は出さない（詳細は §5 の「初回言語選択」）。
- **ポート**: 8080 は常駐サービスと衝突しやすいので**配布既定は 43117**（`dist_template/config.yaml`）。dev は 8080 維持。
  Liner は data-root の `config.yaml` の `server.port` を読んで GUI ごと追従する（初回は config 未生成なので
  `DEFAULT_PORT=43117` にフォールバック。`tab-manager.ts` の `INTERNAL_ORIGIN` も解決済み port から生成）。
- **ヘルスチェック**: 初回起動は bootstrap 展開＋モデルロードで重いので **タイムアウト 180000ms (18e4)**（`index.ts`）。
- アイコン: `liner/build/icon.ico`（`web/static/images/yuzu_512.png` から 16/32/48/64/128/256 の透過 ICO を Pillow で生成）。
- Liner の作法（崩さない）: デフォルトメニュー無効化、`role:'reload'` 禁止、タブ切替時 `webContents.focus()`、
  WebContentsView は最小 preload、ANSI 保存。

---

## 5. 配布版だけの挙動（dev は不変）
| 項目 | 実装 | 配布版の状態 |
|---|---|---|
| LLM プロバイダ | `dist_template/config.yaml` `provider: ""` ＋ `core/llm.py:UnconfiguredProvider` | **未選択**。会話すると「設定→LLM設定で選んで」と案内。deepseek 等を勝手に既定にしない。dev は `provider:"deepseek"` なので従来通り |
| git ツール | （廃止）`git_commit/push/log` ツールは dev・配布版とも**完全に削除済み** | dev でも実益が薄いため撤去。配布版固有の除外ロジック（旧 `_DISTRIBUTION_EXCLUDED_TOOLS`）も廃止。`get_tool_definitions()` は `TOOL_DEFINITIONS` をそのまま返すだけ |
| config 機能 | `dist_template/config/*` の `enabled` | desire/moonbeat/moontide_v2/openclaw/tips/user_memo/vital は **false**。wyrd/compression は enabled キー無し＝常時オン。openclaw は top=false だが OpenBotCity service=true なので top を true にする一手で起動可 |
| ホスト | `dist_template/config.yaml` `host: "127.0.0.1"` | ローカルのみ（外部公開＋パスワード未設定だと起動拒否される鶏卵問題回避） |
| 初回案内 | `dist_template/data/context_state.json` に assistant ロールの seed | 起動直後に「プロバイダ選択→APIキー登録→再起動」を会話で案内（プロバイダ中立） |
| 初回言語選択 | Liner `index.ts:bootSequence` の初回ダイアログ ＋ `server.py --init-lang` ＋ `core/bootstrap.py:ensure_data_root(init_lang)` ＋ **`dist_template/en/`** | 配布版の初回起動（config.yaml 未生成）で「日本語/English」を選ばせ、人格雛形を選択言語で配置する。**方式（共通土台＋言語上書き）**: `dist_template/` 直下＝日本語のデフォルト土台は不変。英語で差し替えるファイルだけ同じ相対パスで `dist_template/en/`（config.yaml・system_prompt の TOP_PROMPT/salia/RECORD_CHECK・workspace 全mds・config/*.txt・data/*_messages.json・moontide_inner.jsonl）に置く。bootstrap は en 選択時に `en/` を**先に**展開してから直下を `_copy_*_if_absent` で重ねるため、英語版が置かれたファイルは日本語コピーがスキップされ、tokenizer/CSV/設定JSON 等の言語非依存ファイルだけが直下から補完される。未知言語や `en/` 欠落時は日本語土台にフォールバック。`--init-lang` は dev では bootstrap が no-op なので無害。2回目以降は config.yaml 既存のためダイアログ無し・展開も no-op |
| Web検索 | `config.yaml` `search.provider`（dev/配布とも既定 `"ddgs"`） | **DuckDuckGo（ddgs）が既定＝APIキー不要で即使える**。配布ユーザーが Tavily キーを用意しなくても `search_web`/`fetch_url` が動く。設定UI（一般設定）または `search.provider: "tavily"`＋`CG_TAVILY_API_KEY` で切替可。dev も同じ既定（旧 Tavily 既定から変更） |
| 3時バックアップ | `core/scheduler.py:_check_backup()` が `CG_DEV_MACHINE=1` 以外で即 return | **無効**（既定）。作者母艦専用ローカルバックアップ（src/dst を `.env` の `CG_DEV_BACKUP_SRC` / `CG_DEV_BACKUP_DST` で指定＋robocopy `/MIR`）。**判定は data_root≠bundle_root では不可**: OSS をソース配布すると作者と同じ dev 構成で動くため構造的に区別不能。よって明示フラグ `CG_DEV_MACHINE=1`（作者の `.env` のみ・非コミット非配布）でゲート。packaged 配布版も OSS ソース実行も自動 OFF。作者母艦だけ `.env` に同フラグと src/dst を持つので従来通り毎朝3時に動く。`CG_DEV_MACHINE=1` でも SRC/DST 未設定なら警告ログを出して skip（他のスケジュールは止めない）。**宛先は2系統**: ①ローカルドライブ（`CG_DEV_BACKUP_DST`、例 `D:\agent_backup\agent`）②Google Drive（`CG_GDRIVE_BACKUP_DST` が set のときのみ。Google Drive for Desktop のマウント先 `G:\マイドライブ\agent_backup\agent` を作者 `.env` で指定）。両宛先は独立 try/except で、片方失敗でももう片方は走る。**除外は `venv` + ビルド成果物 `dist_build`・`liner\dist`（再生成可・計約10GB。robocopy `/XD` にフルパス指定）** なのでミラー量は約1.6GB。Drive 宛先も `CG_DEV_MACHINE` ガードの内側にあり配布版では発動しない |
| 配布キャラ知識 | `dist_template/workspace/MEMORY.md` | Web検索（既定 ddgs＝無料・キー不要 / 任意で Tavily=`CG_TAVILY_API_KEY`）、Lunar Explorer(Docker+SearXNG `localhost:13254`+`CG_DEEPSEEK_SEARCH`)、設定ファイル/インストールパスの場所を記載 |

---

## 6. プライバシー / 秘密の扱い（大原則: 柚月の個人データ・APIキーを配布物に絶対入れない）
- `build_dist.py`: `IGNORE` に `obc_state.json` を入れて同梱除外、`_assert_no_secrets()` の forbidden に
  `.env / settings.json / .secret_key / obc_state.json` を列挙（混入で exit 1）。検証後 `_scrub_staging()` で
  検証中に staging 直下へ漏れた生成物（data/logs/workspace/.env/config.yaml 等）を除去。
- OpenBotCity の個人状態（bot_id/display_name "Yukitsuki"/jwt_env）は **`workspace/program_data/OpenBotCity/` に
  移行済み**（旧 `programs/OpenBotCity/data/obc_state.json` は廃止）。env 名は `CG_OPENBOTCITY_TOKEN` に統一。
- `.env` は **REAL の秘密**を含む（DeepSeek/Moltbook/GitHub 等）。絶対にコミット・配布しない。

---

## 7. よくある落とし穴 / 申し送り
1. **bootstrap は非破壊**。＝既にセットアップ済みの data-root（`Documents\Crescent Grove`）には
   `config.yaml`/`config/`/`data/context_state.json`/`workspace/MEMORY.md` の更新が**自動では届かない**。
   既存ユーザーに新しい既定（provider 未選択・新 seed・新 MEMORY 等）を効かせるには、その data-root を消して
   再起動するか、該当ファイルを手で差し替える必要がある。コード修正は再インストール/コピーで効く。
2. **サテライトの初回クラッシュ**: 新規 workspace にファイルが無い前提で `open(...,'r')` するサテライトは落ちる
   （`letter_post` を存在チェックで修正済み）。他にも未ガードな read が残っている可能性あり（4claw/citron/wp_blog 等）。
3. **API キー破壊バグ（修正済み）**: `env_manager.py` の `_update_config_llm_key` が raw f-string で `\"` を
   config.yaml に書き込み、キー末尾に `"` が混入して 401 になっていた。lambda 置換に修正＋`llm.py` 側で
   `api_key.strip().strip('"\\')` の健全化を入れて過去破損データも救済。
4. **柚月固有名のハードコード残**: `core/agent.py` / `wyrd_network.py` / `memory/manager.py` の一部 LLM プロンプトに
   リテラル「柚月」が残る（コメント/docstring＋一部プロンプト）。`agent_name`/`honorific` は config から差し込む
   仕組み（`profile.agent.name`、`profile.user.honorific`）に移行中だが、未完の箇所がある。配布キャラは「未設定」
   なので、ここが残ると圧縮/記憶生成で自分を柚月と誤認しうる。要継続対応。
5. **workspace パスは固定**: `resolve_workspace()` は config を無視し常に `data_root/workspace` を返す（split-brain
   解消のため。設定UIの workspace 欄は非表示化済み）。
6. **サリアはメインLLMを共用（1キーで本体＋サリア）**: 以前 `core/salia.py` は `CG_DEEPSEEK_SEARCH` という
   2つ目の別キー＋deepseek固定だったが、配布で2キー要求になる問題があった。現在は `server.py` がメインの
   `llm` プロバイダを `Salia(llm_provider=llm)` で渡し、サリアは `provider.client`/`provider.model` を共用する
   （標準 chat.completions のみ使うので OpenAI互換プロバイダなら何でも可）。メインが未設定/非互換なら
   サリアは生成しない。**この変更は dev にも及ぶ**: 柚月のサリアも `CG_DEEPSEEK_SEARCH`→`CG_LLM_DEEPSEEK_API_KEY`、
   モデルも deepseek-chat→メインの deepseek-v4-flash に変わる（ユーザー要望で2キーを1キーに統一）。
   `CG_DEEPSEEK_SEARCH` を別キーで使うのは `programs/lunar_explorer`（Docker+SearXNG が前提の別物）のみ。
7. **Web検索は ddgs 既定（旧 Tavily 既定から変更）**: `core/web_tools.py` の `search_web`/`fetch_url` は
   `config.yaml` の `search.provider` で切替。**既定 `"ddgs"`（DuckDuckGo・無料・APIキー不要）**、`"tavily"` で
   Tavily（要 `CG_TAVILY_API_KEY`・要約付き）。**両プロバイダのコードは残してあり**（`_search_web_ddgs`/
   `_search_web_tavily`/`_fetch_url_tavily`/`_fetch_url_fallback`）、設定値だけで往復切替できる。`_get_search_provider()`
   が毎回 `load_config` を読むので**設定変更は再起動不要で即反映**（設定UI＝一般設定の「Web検索プロバイダー」欄、
   不正値は ddgs に丸める）。**依存は旧 `duckduckgo-search` を捨てて新パッケージ `ddgs` に移行**（旧版はリネーム＋
   DuckDuckGo 側 API 変更で検索結果ゼロになっていた。`requirements.txt`／import を `from ddgs import DDGS` に更新）。
   配布版は **Tavily キー無しでも検索が動く**のが利点。なお `dist_template/workspace/MEMORY.md` は依然 Tavily 前提の
   記述が残るので、配布キャラ知識を ddgs 既定に合わせるなら追って要更新。
   - **複数クエリ同時検索（追加）**: `search_web` の `query` は文字列／文字列配列の両対応。配列を渡すと
     `ThreadPoolExecutor` で並列検索し、クエリごとに見出し付きで結合・URL横断 dedup・1クエリあたり件数の自動抑制を行う
     （`_search_multi`/`_search_one_raw`/`_search_ddgs_raw`/`_search_tavily_raw`）。同時本数 `search.max_parallel`（既定3）、
     受付クエリ上限 `search.max_queries`（既定5）で調整。**dev/配布とも `config.yaml` と `dist_template/config.yaml` の
     両方にキーを追加済み**。ツール定義（`core/tools.py` の `search_web`）の `query` は `anyOf:[string, array]`。
     プロンプト説明は dev/配布の `system_prompt/TOOL_INSTRUCTIONS.md` 両方に追記済み。コードは共有 `core/` なので配布に自動同梱。

---

## 8. 主要ファイル早見
- パス解決: `core/paths.py`（`config_file`/`data_file`/`resolve_path`/`bundle_root`/`data_root`）
- 初回展開: `core/bootstrap.py`（`ensure_data_root`/`_generate_env_if_absent`）
- ビルド: `scripts/build_dist.py`、`liner/electron-builder.yml`、`liner/package.json`(build:win)
- 配布テンプレ: `dist_template/`（config.yaml / config/ / data/ / workspace/ / system_prompt/）。英語雛形は `dist_template/en/`（言語依存ファイルのみ。初回 `--init-lang en` で先に重ねる。§5「初回言語選択」）
- LLM: `core/llm.py`（`create_provider`/`UnconfiguredProvider`）
- ツール: `core/tools.py`（`TOOL_DEFINITIONS`、`get_tool_definitions`、`_run_program`）
- Web検索: `core/web_tools.py`（`search_web`/`fetch_url` が `search.provider` で ddgs⇄tavily 切替、`_get_search_provider`）
- Liner: `liner/src/main/{index.ts, server-process.ts, tab-manager.ts}`

---

_最終更新: タイムゾーン設定（`time.tz_offset`/`time.tz_label`）を追加。一般設定UIから切替でき、コンテキストに注入する時刻表示（`core/context.py:_get_context_tz`）と論理日付の午前3時境界（`core/time_utils`：可変オフセット＋`set_context_timezone`、startup と `app_state.reload_runtime_config` で同期）の両方に追従する。**dev/配布とも `config.yaml`・`dist_template/config.yaml` 両方に既定 JST(+9) を追加済み**。キー欠落時もコード側で JST にフォールバックするため既存 data-root（bootstrap 非破壊）でも安全。tlog のログ記録時刻・日付別ログファイル名は JST 固定のまま（変更なし）。なお `_check_backup` の「3時バックアップ」は別物（dev母艦専用・JST固定で無関係）。_

_3時バックアップ（`_check_backup`）に Google Drive 宛先（`CG_GDRIVE_BACKUP_DST`、作者 .env のみ）を追加し、宛先ごと独立 try/except 化。除外を `venv` に加え再生成可能なビルド成果物 `dist_build`・`liner\dist`（計約10GB）へ拡張しミラー量を約1.6GB に圧縮。いずれも `CG_DEV_MACHINE=1` ガードの内側のため配布版・OSS では発動しない。配布まわりを変更したらこのメモも更新すること。_
