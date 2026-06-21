import { app, BrowserWindow, dialog, ipcMain, Menu, shell } from 'electron'
import type { MenuItemConstructorOptions, WebContents } from 'electron'
import { join, resolve } from 'path'
import * as net from 'net'
import * as http from 'http'
import * as fs from 'fs'
import { ServerProcess } from './server-process'
import type { ServerStatus } from './server-process'
import { TabManager } from './tab-manager'
import { LogStream } from './log-stream'
import { initLinerI18n, lt, getLinerDict, setLinerLang } from './i18n'
import type { ContentBounds } from './tab-manager'

// Phase 3: スプラッシュ → server.py 起動 → Shell UI → ダッシュボード(WebContentsView)埋め込み
//          ＋ ログを Shell UI 下部のログペインに常時表示

// agent ルートの推定
function resolveProjectRoot(): string {
  const appPath = app.getAppPath()
  console.log('[main] app.getAppPath() =', appPath)
  // liner/ の親 = agent ルート
  const projectRoot = resolve(appPath, '..')
  console.log('[main] projectRoot =', projectRoot)
  return projectRoot
}

// i18n（config.yaml / settings.json）を読むためのルートを解決する。
// dev: projectRoot のみ。packaged: Documents の data-root と同梱 agent ルート。
function resolveI18nRoots(): { dataRoot: string | undefined; projectRoot: string } {
  if (!app.isPackaged) {
    return { dataRoot: undefined, projectRoot: resolveProjectRoot() }
  }
  const agentRoot = join(process.resourcesPath, 'agent')
  const dataRoot = join(app.getPath('documents'), 'Crescent Grove')
  return { dataRoot, projectRoot: agentRoot }
}

// 配布既定ポート。dist_template/config.yaml の server.port と一致させること
// （初回起動で config.yaml がまだ無いときのフォールバック値として使う）。
const DEFAULT_PORT = 43117
// dev は従来通り 8080 固定（dev の挙動を変えない）。
const DEV_PORT = 8080

const CONFIG = {
  healthCheckPath: '/api/memory',
  // 初回起動は bootstrap 展開＋トークナイザ/埋め込みモデル重みのロード等で時間がかかり、
  // 60s では超過しうるため 180s に延長（ポーリング間隔 500ms は server-process 側で維持）。
  // dev/packaged 共通でよい（dev は速く成功して即抜けるだけ）。
  startupTimeoutMs: 180000
}

// 起動ポートを解決する。
// - dev (!app.isPackaged): 常に DEV_PORT(8080)。dataRoot は undefined で渡る。
// - packaged: data-root の config.yaml の server.port を読む。初回起動で config.yaml が
//   まだ bootstrap 生成されていない／読めない場合は DEFAULT_PORT にフォールバックする
//   （DEFAULT_PORT は dist_template の既定と一致するので、server 側が初回に生成する
//    config.yaml の port とも一致し、health-check と食い違わない）。
function resolvePort(dataRoot: string | undefined): number {
  if (!dataRoot) return DEV_PORT
  try {
    const cfgPath = join(dataRoot, 'config.yaml')
    const text = fs.readFileSync(cfgPath, 'utf-8')
    // config.yaml の port: は server.port の1箇所のみ（他に port キーは無い）。
    const m = text.match(/^[ \t]*port:[ \t]*(\d+)/m)
    if (m) {
      const p = parseInt(m[1], 10)
      if (p > 0 && p < 65536) return p
    }
  } catch {
    // config.yaml 未生成（初回）／読取失敗 → 既定へフォールバック
  }
  return DEFAULT_PORT
}

let mainWindow: BrowserWindow | null = null
let serverProcess: ServerProcess | null = null
// bootSequence で解決したダッシュボード初期タブURL（port 追従）。
// module レベルの renderer:ready ハンドラから参照するため module スコープに保持する。
let resolvedDashboardUrl = `http://127.0.0.1:${DEFAULT_PORT}/`
let tabManager: TabManager | null = null
let logStream: LogStream | null = null
let isQuitting = false

// 最後に受信したテーマ payload。ログタブを開いた直後に theme:current で渡し、
// 以降のテーマ変更は theme:changed で配信する（Shell renderer と同じ仕組み）。
let lastThemePayload: unknown = null
// テーマ変更を転送するログタブの WebContents 群。タブ破棄時に解除する。
const logTabWebContents = new Set<WebContents>()

// アプリアイコンのパスを解決する。dev 起動時はタスクバー/ウィンドウに
// アイコンを出すために明示指定する（パッケージ版は exe 自体に埋め込まれた
// アイコンが使われるため、build/ が同梱されていなくても問題ない）。
// 存在しない場合は undefined を返し、Electron の既定アイコンに任せる。
function resolveAppIcon(): string | undefined {
  const iconPath = join(app.getAppPath(), 'build', 'icon.ico')
  return fs.existsSync(iconPath) ? iconPath : undefined
}

function createMainWindow(): BrowserWindow {
  const icon = resolveAppIcon()
  const w = new BrowserWindow({
    width: 1400,
    height: 900,
    show: false,
    title: 'Crescent Grove',
    backgroundColor: '#1a1a1a',
    ...(icon ? { icon } : {}),
    webPreferences: {
      preload: join(__dirname, '../preload/index.js'),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true
    }
  })

  w.on('ready-to-show', () => w.show())

  // メインウィンドウ自体は Shell UI を表示するので、外部リンクは OS ブラウザへ
  w.webContents.setWindowOpenHandler(({ url }) => {
    shell.openExternal(url)
    return { action: 'deny' }
  })

  return w
}

function sendSplashStatus(text: string): void {
  if (mainWindow && !mainWindow.isDestroyed()) {
    mainWindow.webContents.send('splash:status', text)
  }
  console.log('[splash]', text)
}

// 0.0.0.0 で listen 試行してポート占有状況を確認
function isPortFree(port: number): Promise<boolean> {
  return new Promise((resolve) => {
    const tester = net.createServer()
    tester.once('error', () => resolve(false))
    tester.once('listening', () => {
      tester.close(() => resolve(true))
    })
    tester.listen(port, '0.0.0.0')
  })
}

// 既存 Crescent Grove を HTTP プローブで検出（2xx/3xx で応答ありとみなす）
function probeExistingServer(port: number, path: string): Promise<boolean> {
  return new Promise((resolve) => {
    const req = http.get(
      { host: '127.0.0.1', port, path, timeout: 2000 },
      (res) => {
        res.resume()
        const code = res.statusCode ?? 0
        resolve(code > 0 && code < 400)
      }
    )
    req.on('error', () => resolve(false))
    req.on('timeout', () => {
      req.destroy()
      resolve(false)
    })
  })
}

async function loadSplash(win: BrowserWindow): Promise<void> {
  const splashPath = join(app.getAppPath(), 'resources', 'splash.html')
  if (!fs.existsSync(splashPath)) {
    throw new Error(`splash.html が見つかりません: ${splashPath}`)
  }
  await win.loadFile(splashPath)
}

// Shell UI (renderer index.html) をロード。dev/prod 切替。
async function loadShell(win: BrowserWindow): Promise<void> {
  const devUrl = process.env['ELECTRON_RENDERER_URL']
  if (devUrl) {
    await win.loadURL(devUrl)
  } else {
    await win.loadFile(join(__dirname, '../renderer/index.html'))
  }
}

async function bootSequence(): Promise<void> {
  // dev / packaged でサーバ起動構成を切り替える。
  // dev (!app.isPackaged): 現状維持。venv python + agent ルートの server.py。
  //   data-root は渡さない（server.py 側で data_root == bundle_root == agent ルート）。
  // packaged (app.isPackaged): 同梱した embeddable runtime + Documents 配下 data-root。
  //   注意: packaged では app.getAppPath() が resources/app.asar を返すため使わず、
  //   必ず process.resourcesPath を基準にする（extraResources で resources/agent/ に
  //   staging 一式が展開されている）。
  let pythonPath: string
  let serverScript: string
  let serverCwd: string
  let dataRoot: string | undefined

  if (!app.isPackaged) {
    const projectRoot = resolveProjectRoot()
    pythonPath = join(projectRoot, 'venv', 'Scripts', 'python.exe')
    serverScript = join(projectRoot, 'server.py')
    serverCwd = projectRoot
    dataRoot = undefined
  } else {
    const agentRoot = join(process.resourcesPath, 'agent')
    pythonPath = join(agentRoot, 'runtime', 'python.exe')
    serverScript = join(agentRoot, 'server.py')
    serverCwd = agentRoot
    // data-root は Documents\Crescent Grove。存在しなくても作らない
    // （本体の core/bootstrap.py が初回起動時に生成する）。
    dataRoot = join(app.getPath('documents'), 'Crescent Grove')
  }

  // ポートは config.yaml（packaged）に追従。dev は 8080 固定。
  // dashboardUrl / INTERNAL_ORIGIN / health-check すべてこの解決済み port から生成する。
  const port = resolvePort(dataRoot)
  const dashboardUrl = `http://127.0.0.1:${port}/`

  console.log('[main] isPackaged =', app.isPackaged)
  console.log('[main] pythonPath =', pythonPath)
  console.log('[main] serverScript =', serverScript)
  console.log('[main] serverCwd =', serverCwd)
  console.log('[main] dataRoot =', dataRoot ?? '(none / agent-root default)')
  console.log('[main] resolved port =', port)

  mainWindow = createMainWindow()
  await loadSplash(mainWindow)

  // 初回起動の言語選択（配布版のみ）。data-root に config.yaml がまだ無い＝初回。
  // 選択結果は server.py の初回ブートストラップに --init-lang として渡し、
  // その言語の雛形（人格テンプレ等）を展開させる。2回目以降は出さない。
  // 言語未確定の場面なので、ダイアログは日英併記の固定文言にする。
  let initLang: string | undefined
  if (dataRoot && !fs.existsSync(join(dataRoot, 'config.yaml'))) {
    const langChoice = await dialog.showMessageBox(mainWindow, {
      type: 'question',
      buttons: ['日本語', 'English'],
      defaultId: 0,
      cancelId: 0,
      title: 'Language / 言語',
      message: '言語を選択してください。\nPlease select your language.',
      detail: 'あとで設定画面から変更できます。\nYou can change this later in Settings.'
    })
    initLang = langChoice.response === 1 ? 'en' : 'ja'
    // 選択結果を Liner 自身の i18n（スプラッシュ／アプリメニュー／コンテキストメニュー／
    // Shell renderer 配信辞書）にも即時反映する。config.yaml がまだ無い初回起動では
    // initLinerI18n() が ja でフォールバックしているため、ここで上書きしないと
    // 「英語選んだのに UI が日本語のまま」になる。アプリメニューは構築済みなので
    // 再ビルドして言語反映する。
    setLinerLang(initLang)
    installAppMenu()
  }

  sendSplashStatus(lt('splash_checking'))
  const existingServer = await probeExistingServer(port, CONFIG.healthCheckPath)
  const portFree = existingServer ? false : await isPortFree(port)

  serverProcess = new ServerProcess({
    pythonPath,
    serverScript,
    cwd: serverCwd,
    port,
    healthCheckPath: CONFIG.healthCheckPath,
    startupTimeoutMs: CONFIG.startupTimeoutMs,
    dataRoot,
    initLang
  })

  // ログを console にも出しておく（dev での観察用）
  serverProcess.onLog((line, stream) => {
    if (stream === 'stderr') {
      console.error('[server.py:err]', line)
    } else {
      console.log('[server.py]', line)
    }
  })

  // サーバー状態の変化を Shell renderer に配信し、操作ボタンの表示・活性を追従させる
  serverProcess.onStatusChange((status) => broadcastServerStatus(status))

  // LogStream は start() より前に接続する。これにより:
  //  1. server.py の起動時ログ（ターミナル相当）が LogStream のバッファに残り、
  //     後でログペイン／ログタブに初期表示できる（attachTo を start 後に置くと、
  //     起動中に流れた行が buffer に入らず取りこぼす）。
  //  2. 起動中の行が 'log:line' で mainWindow に届くので、スプラッシュ画面が
  //     それを購読してリアルタイムに演出表示できる（splash も同じ preload を使うため
  //     window.linerAPI.onLogLine が利用できる）。
  logStream = new LogStream(mainWindow)
  logStream.attachTo(serverProcess)

  if (existingServer) {
    const choice = await dialog.showMessageBox(mainWindow, {
      type: 'question',
      buttons: [lt('dialog_detect_connect'), lt('confirm_cancel')],
      defaultId: 0,
      cancelId: 1,
      title: lt('dialog_detect_title'),
      message: lt('dialog_detect_message').replace('{port}', String(port)),
      detail: lt('dialog_detect_detail')
    })
    if (choice.response === 1) {
      app.quit()
      return
    }
    sendSplashStatus(lt('splash_connect'))
    await serverProcess.attachToExisting()
  } else if (!portFree) {
    throw new Error(lt('error_port_used').replace('{port}', String(port)))
  } else {
    if (!fs.existsSync(pythonPath)) {
      throw new Error(lt('error_python_not_found').replace('{path}', pythonPath))
    }
    if (!fs.existsSync(serverScript)) {
      throw new Error(lt('error_server_script').replace('{path}', serverScript))
    }

    sendSplashStatus(lt('splash_starting'))
    const healthTimer = setTimeout(() => {
      sendSplashStatus(lt('splash_health'))
    }, 1500)
    try {
      await serverProcess.start()
    } finally {
      clearTimeout(healthTimer)
    }
  }

  // TabManager と LogStream を準備（renderer:ready IPC が届く前に生成しておく必要がある。
  // loadURL の resolve より先に renderer の linerAPI.ready() が到達するケースがあるため）
  // ダッシュボードの初期タブURLを module スコープに記録（renderer:ready ハンドラで使う）。
  resolvedDashboardUrl = dashboardUrl
  // TabManager には解決済み port を渡し、同一オリジン判定（INTERNAL_ORIGIN）を追従させる。
  tabManager = new TabManager(mainWindow, port)
  // logStream は上で start() より前に生成・接続済み。

  // Shell UI に遷移
  sendSplashStatus(lt('splash_shell'))
  await loadShell(mainWindow)

  // 以降は renderer:ready を待って初期化を仕上げる
}

function showFatalAndQuit(err: unknown): void {
  const message = err instanceof Error ? err.message : String(err)
  console.error('[main] fatal:', err)
  dialog.showErrorBox(lt('error_startup'), message)
  app.quit()
}

// ====== IPC ハンドラ ======
ipcMain.on('renderer:ready', () => {
  if (!tabManager || !logStream || !mainWindow) {
    // bootSequence の順序が崩れた時のみ届く想定。SPEC.md「実装上の注意」参照。
    console.warn('[main] renderer:ready received before components were ready')
    return
  }
  tabManager.createTab(resolvedDashboardUrl, { pinned: true })
  const buf = logStream.getBuffer()
  if (buf.length > 0) {
    mainWindow.webContents.send('log:buffer', buf)
  }
})

ipcMain.handle('log:getBuffer', () => {
  return logStream ? logStream.getBuffer() : []
})

// i18n 辞書を renderer に渡す
ipcMain.handle('i18n:dict', () => getLinerDict())

// サーバー操作（Shell renderer のボタンから呼ばれる）。確認は renderer 側で行う。
ipcMain.handle('server:status', () => currentServerStatus())
ipcMain.handle('server:start', () => startServer())
ipcMain.handle('server:stop', () => stopServer())
ipcMain.handle('server:restart', () => restartServer())

ipcMain.on('content:setBounds', (_e, bounds: ContentBounds) => {
  if (tabManager) tabManager.setContentBounds(bounds)
})

// タブ操作
ipcMain.on('tab:activate', (_e, tabId: string) => {
  if (tabManager) tabManager.activateTab(tabId)
})
ipcMain.on('tab:close', (_e, tabId: string) => {
  if (tabManager) tabManager.closeTab(tabId)
})
ipcMain.on('tab:reload', () => {
  if (tabManager) tabManager.reloadActiveTab()
})
ipcMain.on('tab:hardReload', () => {
  if (tabManager) tabManager.hardReloadActiveTab()
})
ipcMain.handle('tab:list', () => {
  if (!tabManager) return { tabs: [], activeId: null }
  return { tabs: tabManager.listTabs(), activeId: tabManager.getActiveTabId() }
})

// ページ内検索（対象は常にアクティブタブの WebContentsView）
ipcMain.on(
  'find:run',
  (
    _e,
    payload: { text?: string; forward?: boolean; matchCase?: boolean; findNext?: boolean }
  ) => {
    if (!tabManager || !payload || typeof payload.text !== 'string') return
    tabManager.runFind(payload.text, {
      forward: payload.forward,
      matchCase: payload.matchCase,
      findNext: payload.findNext
    })
  }
)
ipcMain.on('find:next', (_e, forward: boolean) => {
  if (tabManager) tabManager.findNext(!!forward)
})
ipcMain.on('find:stop', () => {
  if (tabManager) tabManager.stopFind()
})

// WebContentsView の preload から届くテーマ変更通知（テーマ名 + CSS 変数パレット）を
// Liner renderer に転送する。MutationObserver の頻発を抑えるため
// シリアライズ比較で重複を抑制。
let lastThemeJson: string | null = null
ipcMain.on('webview:theme-changed', (_e, payload: unknown) => {
  if (!payload || typeof payload !== 'object') return
  let serialized: string
  try {
    serialized = JSON.stringify(payload)
  } catch {
    return
  }
  if (serialized === lastThemeJson) return
  lastThemeJson = serialized
  lastThemePayload = payload
  if (mainWindow && !mainWindow.isDestroyed()) {
    mainWindow.webContents.send('theme:changed', payload)
  }
  // 開いているログタブにもテーマ変更を転送する
  for (const wc of logTabWebContents) {
    if (!wc.isDestroyed()) wc.send('theme:changed', payload)
  }
})

// ログタブ初期化時に現在のテーマを取得するためのハンドラ。
// まだテーマ通知が届いていなければ null（log-tab.ts 側で CSS の :root フォールバックを使う）。
ipcMain.handle('theme:current', () => lastThemePayload)

// アプリケーションメニュー
// - Electron デフォルトの File/Edit/Window/Help を消し、Crescent Grove 専用
//   クライアントとして必要な項目だけを並べる（New Tab 等の汎用機能は持たない）。
// - role: 'reload' は Liner Shell renderer 自体をリロードしてしまうため使わず、
//   すべて click で tabManager 経由のアクティブタブ操作を呼ぶ。
// - ここで定義した accelerator はアプリ全体（Shell renderer / WebContentsView の
//   どちらにフォーカスがあっても）発火する。二重発火を避けるため、これらのキーは
//   tab-manager.ts の before-input-event と renderer の window.keydown では
//   重複処理しない（Ctrl+F5 のハードリロードのみ before-input-event 側に残す）。
function showAboutDialog(): void {
  const parent = mainWindow && !mainWindow.isDestroyed() ? mainWindow : undefined
  const opts = {
    type: 'info' as const,
    title: 'About Crescent Liner',
    message: 'Crescent Liner',
    detail:
      `${lt('about_version')}: ${app.getVersion()}\n` +
      `Electron: ${process.versions.electron}\n\n` +
      lt('about_detail')
  }
  if (parent) {
    dialog.showMessageBox(parent, opts)
  } else {
    dialog.showMessageBox(opts)
  }
}

// サーバログを独立タブで開く（既に開いていればアクティブ化のみ）。
// ログタブは log-tab.html（ローカル）を WebContentsView に読み込み、
// log:line 配信先・テーマ転送先として登録する。重複起動防止は TabManager 側。
function openServerLogTab(): void {
  if (!tabManager || !logStream) return
  const devUrl = process.env['ELECTRON_RENDERER_URL']
  const load = devUrl
    ? { url: `${devUrl}/log-tab.html` }
    : { file: join(__dirname, '../renderer/log-tab.html') }

  const { webContents, created } = tabManager.openOrFocusLogTab(load)
  if (!created) return

  // 新規作成時のみ配信先として登録し、破棄時にクリーンアップする。
  logStream.addTarget(webContents)
  logTabWebContents.add(webContents)
  webContents.once('destroyed', () => {
    if (logStream) logStream.removeTarget(webContents)
    logTabWebContents.delete(webContents)
  })
}

// ====== サーバー操作（起動 / 停止 / 再起動） ======
// メニュー項目・IPC（renderer のボタン）・before-quit から共通で使う。
// 各操作は ServerProcess に委譲し、結果をダイアログ／ステータス配信で通知する。

interface ServerOpResult {
  ok: boolean
  status: ServerStatus
  error?: string
}

function currentServerStatus(): ServerStatus {
  return serverProcess ? serverProcess.status : 'stopped'
}

// サーバー状態を Shell renderer に配信する（ボタンの活性・ラベル更新用）。
function broadcastServerStatus(status: ServerStatus): void {
  if (mainWindow && !mainWindow.isDestroyed()) {
    mainWindow.webContents.send('server:status-changed', status)
  }
}

async function startServer(): Promise<ServerOpResult> {
  if (!serverProcess) {
    return { ok: false, status: 'stopped', error: lt('error_server_not_init') }
  }
  try {
    await serverProcess.start()
    // 起動完了後にタブを再読込してダッシュボード等を再接続させる
    if (tabManager) tabManager.reloadAll()
    return { ok: true, status: serverProcess.status }
  } catch (e) {
    const error = e instanceof Error ? e.message : String(e)
    if (mainWindow && !mainWindow.isDestroyed()) {
      dialog.showErrorBox(lt('error_server_start'), error)
    }
    return { ok: false, status: serverProcess.status, error }
  }
}

async function stopServer(): Promise<ServerOpResult> {
  if (!serverProcess) {
    return { ok: false, status: 'stopped', error: lt('error_server_not_init') }
  }
  try {
    await serverProcess.stop()
    return { ok: true, status: serverProcess.status }
  } catch (e) {
    const error = e instanceof Error ? e.message : String(e)
    if (mainWindow && !mainWindow.isDestroyed()) {
      dialog.showErrorBox(lt('error_server_stop'), error)
    }
    return { ok: false, status: serverProcess.status, error }
  }
}

async function restartServer(): Promise<ServerOpResult> {
  if (!serverProcess) {
    return { ok: false, status: 'stopped', error: lt('error_server_not_init') }
  }
  try {
    await serverProcess.restart()
    if (tabManager) tabManager.reloadAll()
    return { ok: true, status: serverProcess.status }
  } catch (e) {
    const error = e instanceof Error ? e.message : String(e)
    if (mainWindow && !mainWindow.isDestroyed()) {
      dialog.showErrorBox(lt('error_server_restart'), error)
    }
    return { ok: false, status: serverProcess.status, error }
  }
}

// 停止・再起動はエージェントの処理を中断するため、メニュー操作時のみ確認する
// （renderer のボタンは renderer 側で確認してから IPC を呼ぶ）。
function confirmInterrupt(action: string): boolean {
  const parent = mainWindow && !mainWindow.isDestroyed() ? mainWindow : undefined
  const opts = {
    type: 'warning' as const,
    buttons: [action, lt('confirm_cancel')],
    defaultId: 1,
    cancelId: 1,
    title: lt('confirm_title').replace('{action}', action),
    message: lt('confirm_message').replace('{action}', action),
    detail: lt('confirm_detail')
  }
  const idx = parent ? dialog.showMessageBoxSync(parent, opts) : dialog.showMessageBoxSync(opts)
  return idx === 0
}

function installAppMenu(): void {
  const template: MenuItemConstructorOptions[] = [
    {
      label: 'File',
      submenu: [
        {
          label: 'Close Tab',
          accelerator: 'CmdOrCtrl+W',
          click: () => {
            // pinned タブは closeActiveTab → closeTab 内で無視される
            if (tabManager) tabManager.closeActiveTab()
          }
        },
        { type: 'separator' },
        {
          label: 'Quit',
          accelerator: 'CmdOrCtrl+Q',
          click: () => {
            app.quit()
          }
        }
      ]
    },
    {
      label: 'View',
      submenu: [
        {
          label: 'Reload Active Tab',
          accelerator: 'CmdOrCtrl+R',
          click: () => {
            if (tabManager) tabManager.reloadActiveTab()
          }
        },
        {
          label: 'Hard Reload',
          accelerator: 'CmdOrCtrl+Shift+R',
          click: () => {
            if (tabManager) tabManager.hardReloadActiveTab()
          }
        },
        { type: 'separator' },
        {
          label: 'Find in Page',
          accelerator: 'CmdOrCtrl+F',
          click: () => {
            // 検索バーを開く指示を renderer に送る。実際の findInPage は
            // renderer → find:run IPC 経由でアクティブタブに対して実行される。
            if (mainWindow && !mainWindow.isDestroyed()) {
              mainWindow.webContents.send('find:open')
            }
          }
        },
        { type: 'separator' },
        {
          label: 'Open Server Log Tab',
          accelerator: 'CmdOrCtrl+Shift+L',
          click: () => {
            // サーバログを独立タブで開く（重複時はアクティブ化のみ）
            openServerLogTab()
          }
        },
        { type: 'separator' },
        {
          label: 'Toggle DevTools',
          accelerator: 'F12',
          click: () => {
            // Shell renderer ではなくアクティブタブの WebContentsView を対象にする
            if (tabManager) tabManager.toggleDevToolsActiveTab()
          }
        }
      ]
    },
    {
      label: 'Tab',
      submenu: [
        {
          label: 'Next Tab',
          accelerator: 'Ctrl+Tab',
          click: () => {
            if (tabManager) tabManager.nextTab()
          }
        },
        {
          label: 'Previous Tab',
          accelerator: 'Ctrl+Shift+Tab',
          click: () => {
            if (tabManager) tabManager.prevTab()
          }
        }
      ]
    },
    {
      label: lt('menu_server'),
      submenu: [
        {
          label: lt('menu_server_start'),
          click: () => {
            void startServer()
          }
        },
        {
          label: lt('menu_server_stop'),
          click: () => {
            if (confirmInterrupt(lt('action_stop'))) void stopServer()
          }
        },
        {
          label: lt('menu_server_restart'),
          click: () => {
            if (confirmInterrupt(lt('action_restart'))) void restartServer()
          }
        }
      ]
    },
    {
      label: 'Help',
      submenu: [
        {
          label: 'About Crescent Liner',
          click: () => showAboutDialog()
        }
      ]
    }
  ]
  Menu.setApplicationMenu(Menu.buildFromTemplate(template))
}

// ====== ライフサイクル ======
app.whenReady().then(() => {
  // i18n はメニュー構築より前に初期化する
  const { dataRoot, projectRoot } = resolveI18nRoots()
  initLinerI18n(dataRoot, projectRoot)
  installAppMenu()
  bootSequence().catch(showFatalAndQuit)

  app.on('activate', () => {
    if (BrowserWindow.getAllWindows().length === 0) {
      bootSequence().catch(showFatalAndQuit)
    }
  })
})

app.on('before-quit', async (event) => {
  if (isQuitting) return
  const needStop = serverProcess && serverProcess.status !== 'stopped'
  if (!needStop && !tabManager) return
  event.preventDefault()
  isQuitting = true
  try {
    if (tabManager) tabManager.destroyAll()
  } catch (e) {
    console.error('[main] tab destroy error:', e)
  }
  try {
    if (serverProcess) await serverProcess.stop()
  } catch (e) {
    console.error('[main] server stop error:', e)
  }
  app.quit()
})

app.on('window-all-closed', () => {
  if (process.platform !== 'darwin') app.quit()
})
