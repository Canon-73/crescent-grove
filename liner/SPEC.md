# Crescent Liner - 仕様書

Crescent Grove 専用クライアントアプリ。Electron + Vite + TypeScript 製。
ダッシュボード（既存 WebUI）の表示、server.py のライフサイクル管理、
サーバログの色付き常時表示、設定/Debug/過去ログ等のタブ切り替えを提供する。

## 目的と非目的

### 目的
- Crescent Grove (`server.py`) の起動・停止・監視をワンクリックで完結させる
- ダッシュボード WebUI を専用クライアントとして表示する
- server.py の stdout/stderr を構造化・色付けして常時表示する
- Debug / Settings / Logs / Manual 等を Chrome 風タブで切り替える
- Windows 向けに配布可能なインストーラを生成する

### 非目的
- 汎用ブラウザ機能（任意 URL 入力、ブックマーク、履歴等）は提供しない
- ターミナル機能（PTY、コマンド入力）は提供しない。ログは read-only
- HTML/CSS/JS レンダリングエンジンの自作はしない（Chromium を使う）

## 技術スタック

| 項目 | 採用 |
|:---|:---|
| ランタイム | Electron (最新 LTS) |
| 言語 | TypeScript |
| ビルド | electron-vite |
| パッケージング | electron-builder |
| UI | Vanilla TS + 自前 CSS（React は導入しない、軽量重視） |
| OS | Windows 10/11 を最優先。macOS/Linux は将来対応 |

Prism Liner Terminal の構成を踏襲する。

## ディレクトリ構成

```
liner/
├── package.json
├── electron.vite.config.ts
├── electron-builder.yml
├── tsconfig.json / tsconfig.node.json / tsconfig.web.json
├── icon.ico / icon.png
├── liner_config.json          # ユーザー設定（初回起動時に生成）
├── colors.json                # ログモジュール名→色 のマッピング
│
├── src/
│   ├── main/
│   │   ├── index.ts           # app起動・BrowserWindow生成
│   │   ├── server-process.ts  # server.py の spawn/kill/監視/health-check
│   │   ├── log-stream.ts      # stdout/stderr のパースとIPC配信
│   │   ├── tab-manager.ts     # WebContentsView の生成・破棄・切替
│   │   ├── ipc.ts             # IPCハンドラ集約
│   │   ├── single-instance.ts # 二重起動防止
│   │   ├── menu.ts            # アプリメニュー
│   │   └── config.ts          # liner_config.json 読み書き
│   │
│   ├── preload/
│   │   └── index.ts           # contextBridge で window.liner を公開
│   │
│   └── renderer/
│       ├── index.html
│       ├── main.ts
│       ├── components/
│       │   ├── TabBar.ts
│       │   ├── LogPane.ts
│       │   └── StatusBar.ts
│       └── styles/
│           └── liner.css
│
├── resources/
│   └── splash.html            # サーバ起動待ち画面
│
└── SPEC.md (このファイル)
```

## 起動シーケンス

1. Liner プロセス起動
2. `single-instance.ts` で二重起動チェック。既存があれば前面化して終了
3. `liner_config.json` を読み込み（無ければデフォルト生成）
4. `BrowserWindow` 生成、`resources/splash.html` を表示
5. `127.0.0.1:8080` の LISTEN を確認
   - 既に LISTEN 中 → ダイアログ「既存サーバに接続しますか？/新規起動しますか？」
   - 未 LISTEN → `server-process.ts` が `python server.py` を spawn
6. ヘルスチェック実行（最大 60 秒）。以下のいずれかで成功とみなす:
   - HTTP プローブ: `GET /api/memory` のレスポンスが 2xx または 3xx
     （未認証時の 302 リダイレクトも応答可能と判定）
   - HTTP プローブを優先し、これが失敗した時のみ net 層の
     ポートチェック（`0.0.0.0` で listen 試行）にフォールバック
   - 理由: Windows では `0.0.0.0` バインドと `127.0.0.1` バインドが
     別 listen 扱いになり、net 層のチェックだけでは既存サーバを
     検出しきれないため
7. ダッシュボードタブを生成し、`http://127.0.0.1:8080/` をロード
8. ログペインを表示開始

### 実装上の注意: IPC ハンドラの登録順序

renderer の `main.ts` はトップレベル実行で `linerAPI.ready()` を
同期的に呼ぶため、main プロセス側で `'renderer:ready'` IPC ハンドラが
依存するコンポーネント（TabManager, LogStream 等）は、
**BrowserWindow が renderer をロードする前に必ず生成しておくこと**。

`mainWindow.loadURL(shellURL)` の Promise は「load 完了」で resolve するが、
renderer のスクリプトはその resolve よりも前に IPC を発火しうるため、
`await loadShell()` の後にコンポーネントを生成すると race condition となる
（Phase 3 実装時に発生済み）。

### 実装上の注意: Electron デフォルトメニューの無効化

Electron はデフォルトで Ctrl+W = Close Window, Ctrl+R = Reload Window
などのメニューアクセラレータを持つ。これらは renderer の keydown より
**先に**発火するため、自前ショートカット実装が機能しなくなる。

特に Ctrl+W は最後のウィンドウを閉じると app.quit() を発火させるため、
タブ閉じる用途で使うとアプリ全体が終了してしまう。

対策: `Menu.setApplicationMenu()` で最小メニューに差し替える。
View メニューに DevTools と Reload を独自定義するが、
Reload は **アクティブタブのみ** をリロードする click ハンドラに
オーバーライドすること（デフォルトの role: 'reload' は
Liner の renderer 自体をリロードしてしまうため使用禁止）。

### 実装上の注意: WebContentsView は別 webContents

タブの中身として埋め込む WebContentsView は、Liner の renderer とは
**別の webContents** として動作する。そのため、WebContentsView に
フォーカスがある時のキーイベントは Liner renderer の
`window.addEventListener('keydown')` には届かない。

対策: 各 WebContentsView に対して `webContents.on('before-input-event')`
を設定し、main プロセス側で Ctrl+W / Ctrl+Tab / Ctrl+1〜9 等の
タブ操作ショートカットを処理する。
renderer 専用のショートカット（Ctrl+L 等のログペイン操作）は、
main 側から IPC（`shortcut:toggle-log` 等）で renderer に委譲する。

さらに、タブ切替時には新 view に対して `webContents.focus()` を
**明示的に呼ぶこと**。これをしないと新 view にフォーカスが渡らず、
切替直後の Ctrl+Tab 連打等の keyboard input が一切受け取れない
（Phase 4 後の調査で判明）。

#### 例外: テーマ検知用の最小 preload

「WebContentsView に preload は付けない」が原則だが、
ダッシュボードのテーマ変更を Shell UI に伝播するためだけに
最小限の preload (`src/preload/webview-preload.ts`) を例外的に付与する。
この preload は:

- `ipcRenderer.send('webview:theme-changed', theme)` で **一方向通知のみ** 行う
- `contextBridge.exposeInMainWorld` も使わず、ページから利用可能な API は公開しない
- MutationObserver で `document.body[data-theme]` を観察し変化を検出

攻撃面はゼロ送信のみのため最小限。WebUI 側からは preload の存在自体を
（API として）利用できない。

### 実装上の注意: Shell UI のテーマ連動

ダッシュボード（WebContentsView）と Shell UI（Liner renderer）は
独立した webContents であり、CSS や localStorage を直接共有できない。
Liner 側でテーマ別の配色をハードコードしないため、ダッシュボードの
CSS 変数をそのまま吸い上げて Shell UI に反映する方式を採用する。

**新テーマ追加時、Crescent Grove 側で CSS 変数を定義すれば
Liner は無修正で追従する**（タブバー等の見た目が自動で揃う）。

#### 伝播フロー

1. WebContentsView 側 preload (`webview-preload.ts`) が
   - `body[data-theme]` 属性変化を MutationObserver で検知
   - `getComputedStyle(document.body)` でダッシュボードの CSS 変数を読み取り
   - `ipcRenderer.send('webview:theme-changed', { theme, palette })` で main に送信
2. main プロセスが受信し（JSON シリアライズ比較で重複抑制）
   `mainWindow.webContents.send('theme:changed', payload)` で renderer に転送
3. Liner renderer は
   - `document.documentElement.setAttribute('data-shell-theme', theme)` でテーマ名を反映
   - `palette` の各キー（camelCase）を kebab-case に戻し
     `--cg-bg-primary`, `--cg-accent` 等として `document.documentElement.style.setProperty()` で公開
4. CSS は `var(--cg-bg-primary)` 等を参照し、フォールバック値は `:root` の
   デフォルト（dark 基調）で持つ。テーマ別セレクタは持たない

#### 取得対象の CSS 変数

`webview-preload.ts` の `PALETTE_VARS` で列挙:

- `--bg-primary`, `--bg-secondary`, `--bg-card`, `--bg-hover`
- `--text-primary`, `--text-secondary`, `--text-muted`
- `--accent`, `--accent-hover`
- `--border`

ダッシュボード側で変数定義が変わった場合、または Liner 側で追加で
参照したい変数が出てきた場合は、この配列を更新すること。
存在しない変数を `getPropertyValue` した場合は空文字が返り無害。

Crescent Grove の現状テーマ: `dark`（属性なし）/ `light` / `moonlit`。
localStorage キーは `yuzuki_theme`。

#### 例外: テーマ非連動の領域

ログペインは「コンソール風の read-only 表示」のため
テーマ非連動の dark 固定 + monospace フォントを維持する。
タブ閉じる × ボタンの hover 色も危険色（赤）固定。

## 終了シーケンス

1. `before-quit` イベントで `server-process.stop()` を呼ぶ
2. SIGTERM 相当（Windows では `taskkill /pid /t`）を送信
3. 5 秒待っても生きていれば SIGKILL 相当（`taskkill /F /T`）
4. 子プロセス終了確認後に `app.quit()`

## レイアウト

```
┌──────────────────────────────────────────────────────┐
│ [≡] [Dashboard] [Debug] [Settings] [Logs] [+]   [—□×] │ ← タブバー (高さ32px)
├──────────────────────────────────────────────────────┤
│                                                      │
│         WebContentsView (active tab)                 │
│                                                      │
├═══════════════════ ⇕ resizer ═══════════════════════━┤
│ [▼ Server Log] [Filter: ____] [☐Salia ☑LLM ...] [🗑] │ ← ログペインヘッダ
│ [15:46:42] [Salia]    欲求更新 intellectual: 9 → 6   │
│ [16:17:00] [Moonbeat] パルス送信: 自由時間です。      │
│ [16:17:13] [LLM]      tokens: in=508294 out=121      │
│                                                      │
└──────────────────────────────────────────────────────┘
```

- タブバー: 固定高さ。ダッシュボードタブは閉じれない（pinned）
- WebContentsView: 残り領域の上側
- リサイザ: ドラッグでログペイン高さ変更。`Ctrl+L` で折りたたみ／展開
- ログペイン: デフォルト高さ 240px。最小 0（折りたたみ）、最大 70% 画面高

## タブ仕様

| タブ | 初期URL | 閉じれる | 備考 |
|:---|:---|:---|:---|
| Dashboard | `http://127.0.0.1:8080/` | × | 固定 |
| Debug | `http://127.0.0.1:8080/debug/context` | ○ | リンククリックで生成 |
| Settings | `http://127.0.0.1:8080/settings/llm` | ○ | リンククリックで生成 |
| Logs | `http://127.0.0.1:8080/logs` | ○ | 過去ログビューア |
| Manual | `http://127.0.0.1:8080/manual` | ○ | |

タブ生成のルール:
- WebContentsView の `setWindowOpenHandler` で `window.open` を捕捉
- WebContentsView の `will-navigate` で同一オリジン遷移を捕捉
- 同一 URL が既に開いていればフォーカスのみ（重複生成しない）
- 外部オリジン（127.0.0.1:8080 以外）は OS デフォルトブラウザで開く

## ログストリーム仕様

### 入力
`server-process` の stdout / stderr を行単位で受信する。

### パース規則
正規表現で以下の形式を解釈する:

```
^\[(?<time>\d{2}:\d{2}:\d{2})\]\s+\[(?<module>[^\]]+)\]\s+(?<body>.*)$
```

マッチしない行は `module="raw"` として扱う。

### LogEntry 型
```typescript
interface LogEntry {
  id: number              // 連番
  timestamp: string       // "HH:MM:SS"
  module: string          // "Salia" / "Moonbeat" / "LLM" / ...
  level: 'info' | 'debug' | 'warn' | 'error'  // 推定
  body: string
  raw: string             // 元の行
  stream: 'stdout' | 'stderr'
  receivedAt: number      // Date.now()
}
```

### モジュール一覧と色（colors.json）
スクリーンショットから判明している分:

| module | 色（ダーク背景前提） |
|:---|:---|
| Salia | `#c792ea` (薄紫) |
| Moonbeat | `#f0c674` (黄) |
| Flashback | `#ff9b5e` (オレンジ) |
| LLM | `#82aaff` (シアン) |
| MoonTide | `#a3be8c` (緑) |
| DesireManager | `#e57373` (赤) |
| DEBUG | `#7a7a7a` (グレー) |
| INFO | `#8fbcbb` |
| raw | `#cccccc` |

未知のモジュールはハッシュから色を自動生成する。

### バッファ
renderer 側で最大 10000 行のリングバッファ。超過時は古い行から破棄。

### フィルタ
- 全文検索（部分一致、リアルタイム）
- モジュール名チェックボックスでの絞り込み
- レベルフィルタ（warn/error のみ表示など）

### オートスクロール
ユーザーが最下部から 50px 以内にスクロール位置がある時のみ自動追従。
それ以外では新規行が来ても位置を維持する。

## IPC API（window.liner）

preload で contextBridge 経由で公開:

```typescript
interface LinerAPI {
  server: {
    status(): Promise<'running' | 'starting' | 'stopped' | 'crashed'>
    restart(): Promise<void>
    onStatusChange(cb: (status: string) => void): () => void  // unsubscribe を返す
  }
  log: {
    onLine(cb: (entry: LogEntry) => void): () => void
    getBuffer(): Promise<LogEntry[]>
    clear(): Promise<void>
  }
  tab: {
    open(url: string, title?: string): Promise<string>  // tabId を返す
    close(tabId: string): Promise<void>
    activate(tabId: string): Promise<void>
    list(): Promise<TabInfo[]>
    onChange(cb: (tabs: TabInfo[]) => void): () => void
  }
  app: {
    version(): Promise<string>
    openExternal(url: string): Promise<void>
    showItemInFolder(path: string): Promise<void>
  }
  config: {
    get<K extends keyof LinerConfig>(key: K): Promise<LinerConfig[K]>
    set<K extends keyof LinerConfig>(key: K, value: LinerConfig[K]): Promise<void>
  }
}
```

Phase 1 では `window.liner` は **Liner の renderer 専用** とする。
WebUI 側（ダッシュボード）には公開しない（セキュリティ境界の明確化）。

## liner_config.json

```json
{
  "server": {
    "command": "python",
    "args": ["server.py"],
    "cwd": "..",                  
    "port": 8080,
    "healthCheckPath": "/api/memory",
    "startupTimeoutMs": 60000,
    "autoRestartOnCrash": true
  },
  "ui": {
    "logPaneHeight": 240,
    "logPaneCollapsed": false,
    "theme": "dark",
    "fontFamily": "Consolas, 'BIZ UDGothic', monospace",
    "fontSize": 12
  },
  "tabs": {
    "rememberOpen": true,
    "openOnStartup": ["http://127.0.0.1:8080/"]
  },
  "window": {
    "width": 1400,
    "height": 900,
    "maximized": false
  }
}
```

設置場所: `%APPDATA%\CrescentLiner\liner_config.json`
（`liner/liner_config.json` ではなく Electron の `app.getPath('userData')` 配下）

## キーボードショートカット

| キー | 動作 |
|:---|:---|
| `Ctrl+L` | ログペイン折りたたみ／展開 |
| `Ctrl+F` | ログペイン内検索フォーカス |
| `Ctrl+T` | 新規タブ（ダッシュボード複製） |
| `Ctrl+W` | 現在タブを閉じる（Dashboard は無視） |
| `Ctrl+Tab` / `Ctrl+Shift+Tab` | タブ切り替え |
| `Ctrl+1` ～ `Ctrl+9` | タブ番号指定切り替え |
| `Ctrl+R` | 現在タブのリロード |
| `Ctrl+Shift+R` | サーバ再起動 |
| `F12` | DevTools |

## セキュリティ

- `nodeIntegration: false`, `contextIsolation: true`, `sandbox: true`
- 外部オリジンへの遷移は `setWindowOpenHandler` で `shell.openExternal` に振る
- WebContentsView の `webPreferences.preload` は付けない（WebUI 側は無防備な信頼ゾーン）
- CSP は WebUI 側（server.py）の責務

## エラーハンドリング

| 状況 | 挙動 |
|:---|:---|
| Python が見つからない | ダイアログ「Python が必要です」+ インストールガイドへのリンク |
| port 8080 が既に使用中 | ダイアログ「既存サーバに接続/別ポートで起動/キャンセル」（ただし HTTP プローブで Crescent Grove と判定できた場合はダイアログを出さず、自動で既存サーバに接続する設計も可。Phase 2 では選択式ダイアログを採用） |
| ヘルスチェック 60 秒タイムアウト | ダイアログ「起動失敗。ログを確認」+ ログペインを強制展開 |
| server.py がクラッシュ | バナー表示「サーバが停止しました [再起動]」、autoRestart 有効時は自動再起動 |
| WebContentsView の crash | 該当タブにエラー画面、リロードボタン |

## 配布

### Phase 1（個人開発）
electron-builder で Windows NSIS インストーラと portable zip を生成。
ユーザーは別途 Python 3.11+ と Crescent Grove の依存をインストール必要。

### Phase 2（将来）
PyInstaller で server.py を `server.exe` 化し、`liner/resources/server/` に同梱。
Python レス配布を実現する。

### electron-builder.yml の方針
```yaml
appId: dev.crescentgrove.liner
productName: Crescent Liner
directories:
  output: dist
files:
  - out/**/*
  - icon.ico
win:
  target:
    - nsis
    - zip
  icon: icon.ico
nsis:
  oneClick: false
  allowToChangeInstallationDirectory: true
  shortcutName: Crescent Liner
```

## 開発フェーズ

1. **Phase 1**: 空 Electron アプリ起動（`npm run dev` で空ウィンドウ）
2. **Phase 2**: server.py 起動連携 + スプラッシュ + ヘルスチェック
3. **Phase 3**: ログペイン（パース、色付け、フィルタ、オートスクロール）
4. **Phase 4**: タブシステム（WebContentsView、リンク捕捉、永続化）
5. **Phase 5**: 配布準備（electron-builder、アイコン、インストーラ）

各フェーズ完了時にコミット。Phase 間で動作確認すること。

## 既知の制約 / 将来対応

（現時点で未解決の制約はなし）
