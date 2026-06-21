import { BrowserWindow, WebContentsView, shell, type WebContents } from 'electron'
import { join } from 'path'
import { installContextMenu } from './context-menu'

// Phase 4: タブシステム本格版
// - 複数タブの生成・切替・閉じる
// - 重複タブ防止（URL 正規化）
// - 内部リンク hijack（will-navigate / setWindowOpenHandler）
// - タイトル変更通知

export interface ContentBounds {
  x: number
  y: number
  width: number
  height: number
}

export interface TabInfo {
  id: string
  url: string
  title: string
  pinned: boolean
}

interface TabRecord {
  id: string
  view: WebContentsView
  url: string
  title: string
  pinned: boolean
  // 'log' はローカル HTML を表示するサーバログタブ。重複起動防止のため種別を持つ。
  kind?: 'log'
}

// ダッシュボード Origin（内部判定に使う）は固定値ではなく、解決済みの port から
// インスタンスごとに生成する（config.yaml の port 変更に追従。誤判定で同一オリジンの
// タブが外部ブラウザに飛ぶ事故を防ぐ）。→ TabManager.internalOrigin / isInternalUrl()

// ダッシュボードのヘッダーから新規タブで開きたいパス（完全一致）。
// startsWith にすると /debug 配下のサーバリダイレクトも拾ってしまうので、
// あくまでヘッダーリンク本体のパスのみを列挙する。
const NEW_TAB_PATHS = new Set<string>(['/debug/context', '/settings/llm', '/logs', '/manual'])

// 「ダッシュボード」と呼べる位置にあると判定するパス。
// 認証フローや /login → /dashboard リダイレクトを誤ってヒットさせないため、
// 厳密にこの 2 つだけにする。
const DASHBOARD_PATHS = new Set<string>(['/', '/dashboard'])

// Ctrl キーの押下状態をモジュールスコープで管理する。
// タブ切替時に before-input-event リスナの紐付けが新しい view に切り替わる際、
// `input.control` は最初のイベントで正しく取れないことがあるため、
// Control キー自体の keyDown/keyUp を追跡してこのフラグで代替する。
let ctrlPressed = false

// 非アクティブタブを画面外に追いやるための bounds
const OFFSCREEN: ContentBounds = { x: -10000, y: -10000, width: 0, height: 0 }

export class TabManager {
  private mainWindow: BrowserWindow
  // ダッシュボードの内部オリジン（http://127.0.0.1:<port>）。同一オリジン判定に使う。
  private internalOrigin: string
  // 生成順を保ちたいので配列で保持し、id → index は線形検索（数十タブ以下を想定）
  private tabs: TabRecord[] = []
  private activeTabId: string | null = null
  private currentBounds: ContentBounds = { x: 0, y: 0, width: 0, height: 0 }
  private nextId = 1

  // ページ内検索の状態。検索対象は常にアクティブタブの WebContentsView。
  // 検索バーは Shell 側に1つだけ存在し、状態（検索語/大文字小文字）はここで保持して
  // Ctrl+G などの「次へ」要求に再利用する。
  private findActive = false
  private findText = ''
  private findMatchCase = false

  constructor(mainWindow: BrowserWindow, internalPort: number) {
    this.mainWindow = mainWindow
    // 解決済み port から内部オリジンを生成（config.yaml の port に追従）。
    this.internalOrigin = `http://127.0.0.1:${internalPort}`
    // ウィンドウがフォーカスを失った時に Ctrl 状態をリセット。
    // ウィンドウ外で Ctrl を離されると keyUp が届かず、押しっぱなし扱いに
    // なってしまうのを防ぐ保険。
    mainWindow.on('blur', () => {
      ctrlPressed = false
    })
  }

  // URL がダッシュボードと同一オリジンか判定する（解決済み port に追従）。
  private isInternalUrl(url: string): boolean {
    try {
      return new URL(url).origin === this.internalOrigin
    } catch {
      return false
    }
  }

  // ======================= 公開 API =======================

  createTab(url: string, options: { pinned?: boolean; activate?: boolean } = {}): string {
    // 重複タブ防止: 既存があればアクティブ化して返す
    const existing = this.findTabByUrl(url)
    if (existing) {
      if (options.activate !== false) this.activateTab(existing.id)
      return existing.id
    }

    const id = `tab-${this.nextId++}`
    const view = new WebContentsView({
      webPreferences: {
        // テーマ検知のための最小 preload。一方向 ipcRenderer.send のみで
        // API は公開しない（contextBridge も使わない）。攻撃面は最小限。
        preload: join(__dirname, '../preload/webview-preload.js'),
        contextIsolation: true,
        nodeIntegration: false,
        sandbox: true
      }
    })

    this.mainWindow.contentView.addChildView(view)
    // 新規タブはまず非表示（アクティブ化時に bounds を入れる）
    view.setBounds(OFFSCREEN)

    this.attachWebContentsHandlers(view, id)

    view.webContents.loadURL(url).catch((err) => {
      console.error('[TabManager] loadURL rejected', { id, url, err })
    })

    const record: TabRecord = {
      id,
      view,
      url,
      title: this.titleFromUrl(url),
      pinned: options.pinned ?? false
    }
    this.tabs.push(record)

    // activate オプションのデフォルトは true
    if (options.activate !== false) {
      this.activateTab(id)
    } else {
      // 非アクティブで作る場合でも、最初の 1 個ならアクティブにする
      if (this.activeTabId === null) this.activateTab(id)
      else this.notifyChange()
    }
    return id
  }

  activateTab(tabId: string): void {
    const tab = this.findById(tabId)
    if (!tab) return
    if (this.activeTabId === tabId) {
      tab.view.setBounds(this.currentBounds)
      return
    }

    // タブ切替時は実行中の検索を停止し検索バーを閉じる（Chrome/Firefox と同じ挙動）。
    // 旧アクティブタブのハイライトをクリアしてから切り替える。
    if (this.findActive && this.activeTabId) {
      const old = this.findById(this.activeTabId)
      if (old) old.view.webContents.stopFindInPage('clearSelection')
      this.findActive = false
      this.findText = ''
      if (!this.mainWindow.isDestroyed()) this.mainWindow.webContents.send('find:closed')
    }

    // 旧アクティブを画面外へ
    if (this.activeTabId) {
      const old = this.findById(this.activeTabId)
      if (old) old.view.setBounds(OFFSCREEN)
    }

    this.activeTabId = tabId
    tab.view.setBounds(this.currentBounds)
    // 新 view に明示的にフォーカスを移す。これをしないと keyboard input が
    // 一切届かず、Ctrl+Tab 連続押しが効かない（Phase 4 後の調査で判明）。
    tab.view.webContents.focus()
    this.notifyChange()
  }

  closeTab(tabId: string): void {
    const idx = this.tabs.findIndex((t) => t.id === tabId)
    if (idx < 0) return
    const tab = this.tabs[idx]!
    if (tab.pinned) return // pinned は閉じれない

    // アクティブを閉じる場合は隣のタブをアクティブ化する
    const wasActive = this.activeTabId === tabId

    // アクティブタブを検索中に閉じる場合は検索状態をリセットし、検索バーを閉じる。
    // view はこの後破棄されるので stopFindInPage は不要。
    if (wasActive && this.findActive) {
      this.findActive = false
      this.findText = ''
      if (!this.mainWindow.isDestroyed()) this.mainWindow.webContents.send('find:closed')
    }
    let nextActive: string | null = this.activeTabId
    if (wasActive) {
      const next = this.tabs[idx + 1] ?? this.tabs[idx - 1] ?? null
      nextActive = next ? next.id : null
    }

    try {
      this.mainWindow.contentView.removeChildView(tab.view)
    } catch (e) {
      console.error('[TabManager] removeChildView error', e)
    }
    try {
      // WebContents を確実に解放
      tab.view.webContents.close()
    } catch (e) {
      console.error('[TabManager] webContents.close error', e)
    }

    this.tabs.splice(idx, 1)

    if (wasActive) {
      this.activeTabId = null
      if (nextActive) this.activateTab(nextActive)
      else this.notifyChange()
    } else {
      this.notifyChange()
    }
  }

  listTabs(): TabInfo[] {
    return this.tabs.map((t) => ({
      id: t.id,
      url: t.url,
      title: t.title,
      pinned: t.pinned
    }))
  }

  getActiveTabId(): string | null {
    return this.activeTabId
  }

  reloadActiveTab(): void {
    if (!this.activeTabId) return
    const tab = this.findById(this.activeTabId)
    if (!tab) return
    tab.view.webContents.reload()
  }

  // 全タブの WebContentsView をリロードする。サーバー再起動／起動の直後に
  // ダッシュボード等を再接続させるために使う。
  reloadAll(): void {
    for (const tab of this.tabs) {
      try {
        tab.view.webContents.reload()
      } catch {
        // 破棄済み等は無視
      }
    }
  }

  // ハードリロード（キャッシュ無視）。リロード対象は必ずアクティブタブの
  // WebContentsView であり、Liner Shell の renderer ではない（role:'reload' を
  // 使わないのと同じ理由）。pinned (Dashboard) タブでも動作する。
  hardReloadActiveTab(): void {
    if (!this.activeTabId) return
    const tab = this.findById(this.activeTabId)
    if (!tab) return
    tab.view.webContents.reloadIgnoringCache()
  }

  // アクティブタブの DevTools を開閉する。対象は必ずアクティブタブの
  // WebContentsView であり、Liner Shell の renderer ではない。
  toggleDevToolsActiveTab(): void {
    if (!this.activeTabId) return
    const tab = this.findById(this.activeTabId)
    if (!tab) return
    tab.view.webContents.toggleDevTools()
  }

  // アプリメニュー (Tab > Next/Previous) から呼ぶ公開ラッパ。
  // 実体はモジュール内の cycleTab。
  nextTab(): void {
    this.cycleTab(1)
  }
  prevTab(): void {
    this.cycleTab(-1)
  }

  // アプリメニュー (File > Close Tab) から呼ぶ公開ラッパ。
  // アクティブタブを閉じる（pinned は closeTab 内で無視される）。
  closeActiveTab(): void {
    if (!this.activeTabId) return
    this.closeTab(this.activeTabId)
  }

  // サーバログタブを開く。既に存在すればアクティブ化のみ行う（重複起動防止）。
  // ローカル HTML（log-tab.html）を読み込むため、通常タブの loadURL とは別経路。
  // 戻り値の created が true の時だけ、呼び出し側で log:line 配信先登録などを行う。
  openOrFocusLogTab(load: { url?: string; file?: string }): {
    id: string
    webContents: WebContents
    created: boolean
  } {
    // 既存のログタブがあればアクティブ化して返す
    const existing = this.tabs.find((t) => t.kind === 'log')
    if (existing) {
      this.activateTab(existing.id)
      return { id: existing.id, webContents: existing.view.webContents, created: false }
    }

    const id = `tab-${this.nextId++}`
    const view = new WebContentsView({
      webPreferences: {
        // ログタブ専用 preload（logTabAPI を公開）。
        preload: join(__dirname, '../preload/log-tab-preload.js'),
        contextIsolation: true,
        nodeIntegration: false,
        sandbox: true
      }
    })

    this.mainWindow.contentView.addChildView(view)
    view.setBounds(OFFSCREEN)

    // ローカル HTML タブはタイトル固定・内部リンク hijack 不要なので、
    // 通常タブ用の attachWebContentsHandlers は使わず最小限のハンドラのみ。
    const wc = view.webContents
    // ログタブでもコピー/すべて選択ができるよう右クリックメニューを有効化。
    installContextMenu(wc, this.mainWindow)
    wc.on('did-fail-load', (_e, code, desc, validatedURL) => {
      console.error('[TabManager] log tab did-fail-load', { id, code, desc, validatedURL })
    })
    wc.on('render-process-gone', (_e, details) => {
      console.error('[TabManager] log tab render-process-gone', { id, details })
    })
    // ログタブ内のキー操作も Shell と揃える（Ctrl+L / Ctrl+1..9 / F5）。
    this.attachLogTabInput(view, id)

    if (load.url) {
      wc.loadURL(load.url).catch((err) => {
        console.error('[TabManager] log tab loadURL rejected', { id, err })
      })
    } else if (load.file) {
      wc.loadFile(load.file).catch((err) => {
        console.error('[TabManager] log tab loadFile rejected', { id, err })
      })
    }

    const record: TabRecord = {
      id,
      view,
      url: load.url ?? load.file ?? '',
      title: 'Server Log',
      pinned: false,
      kind: 'log'
    }
    this.tabs.push(record)
    this.activateTab(id)
    return { id, webContents: wc, created: true }
  }

  // ======================= ページ内検索 =======================

  // 検索実行/更新。入力変更時は findNext:false で全マッチをハイライトし、
  // 先頭マッチを選択する（Firefox の「ハイライトすべて」相当）。
  runFind(text: string, opts: { forward?: boolean; matchCase?: boolean; findNext?: boolean } = {}): void {
    const tab = this.getActiveTab()
    if (!tab) return
    this.findActive = true
    this.findText = text
    if (typeof opts.matchCase === 'boolean') this.findMatchCase = opts.matchCase
    if (!text) {
      // 入力が空になったら選択を消す（バーは開いたまま）
      tab.view.webContents.stopFindInPage('clearSelection')
      return
    }
    tab.view.webContents.findInPage(text, {
      forward: opts.forward ?? true,
      findNext: opts.findNext ?? false,
      matchCase: this.findMatchCase
    })
  }

  // 次/前のマッチへ移動（forward=true で次）。保持中の検索語・大文字小文字設定を再利用。
  findNext(forward: boolean): void {
    if (!this.findActive || !this.findText) return
    const tab = this.getActiveTab()
    if (!tab) return
    tab.view.webContents.findInPage(this.findText, {
      forward,
      findNext: true,
      matchCase: this.findMatchCase
    })
  }

  // 検索停止（選択ハイライトをクリア）。検索バーは閉じない（× や Esc は renderer 側で閉じる）。
  stopFind(): void {
    const tab = this.getActiveTab()
    if (tab) tab.view.webContents.stopFindInPage('clearSelection')
    this.findActive = false
    this.findText = ''
  }

  // 検索を停止し、Shell の検索バーも閉じるよう通知する（タブ切替/クローズ時）。
  private closeFind(): void {
    this.stopFind()
    if (!this.mainWindow.isDestroyed()) {
      this.mainWindow.webContents.send('find:closed')
    }
  }

  private getActiveTab(): TabRecord | undefined {
    return this.activeTabId ? this.findById(this.activeTabId) : undefined
  }

  setContentBounds(bounds: ContentBounds): void {
    this.currentBounds = bounds
    if (!this.activeTabId) return
    const tab = this.findById(this.activeTabId)
    if (tab) tab.view.setBounds(bounds)
  }

  destroyAll(): void {
    for (const tab of this.tabs) {
      try {
        this.mainWindow.contentView.removeChildView(tab.view)
      } catch {
        // ignore
      }
      try {
        tab.view.webContents.close()
      } catch {
        // ignore
      }
    }
    this.tabs = []
    this.activeTabId = null
  }

  // ======================= 内部 =======================

  private findById(id: string): TabRecord | undefined {
    return this.tabs.find((t) => t.id === id)
  }

  private findTabByUrl(url: string): TabRecord | undefined {
    const norm = normalizeUrl(url)
    return this.tabs.find((t) => normalizeUrl(t.url) === norm)
  }

  private notifyChange(): void {
    if (this.mainWindow.isDestroyed()) return
    this.mainWindow.webContents.send('tabs:changed', this.listTabs(), this.activeTabId)
  }

  private titleFromUrl(url: string): string {
    try {
      const u = new URL(url)
      const path = u.pathname
      if (path === '/') return 'Dashboard'
      if (path.startsWith('/debug')) return 'Debug'
      if (path.startsWith('/settings')) return 'Settings'
      if (path.startsWith('/logs')) return 'Logs'
      if (path.startsWith('/manual')) return 'Manual'
      return path
    } catch {
      return url
    }
  }

  // WebContentsView ごとに必要なハンドラを登録する
  private attachWebContentsHandlers(view: WebContentsView, id: string): void {
    const wc = view.webContents

    // 右クリックコンテキストメニュー（コピー/貼り付け等）を有効化。
    // pinned Dashboard を含むすべての通常タブの WebContentsView が対象。
    // popup の親は Shell の BrowserWindow。
    installContextMenu(wc, this.mainWindow)

    // タイトル変更 → TabInfo 更新 + notifyChange
    wc.on('page-title-updated', (_e, title) => {
      const tab = this.findById(id)
      if (!tab) return
      if (title && title.trim().length > 0) {
        tab.title = title
        this.notifyChange()
      }
    })

    // 現在 URL の追跡（will-navigate / SPA 遷移どちらでも反映）
    const updateUrl = (): void => {
      const tab = this.findById(id)
      if (!tab) return
      const newUrl = wc.getURL()
      if (newUrl && newUrl !== tab.url) {
        tab.url = newUrl
        this.notifyChange()
      }
    }
    wc.on('did-navigate', updateUrl)
    wc.on('did-navigate-in-page', updateUrl)

    // ページ内検索の結果。findInPage はアクティブタブだけが呼ぶので、
    // アクティブタブ以外からのイベントは無視して renderer の表示を汚さない。
    wc.on('found-in-page', (_e, result) => {
      if (id !== this.activeTabId) return
      if (this.mainWindow.isDestroyed()) return
      this.mainWindow.webContents.send('find:result', {
        requestId: result.requestId,
        activeMatchOrdinal: result.activeMatchOrdinal,
        matches: result.matches,
        finalUpdate: result.finalUpdate
      })
    })

    // 異常系
    wc.on('did-fail-load', (_e, code, desc, validatedURL) => {
      console.error('[TabManager] did-fail-load', { id, code, desc, validatedURL })
    })
    wc.on('render-process-gone', (_e, details) => {
      console.error('[TabManager] render-process-gone', { id, details })
    })

    // target="_blank" や window.open は setWindowOpenHandler で捕捉
    wc.setWindowOpenHandler(({ url }) => {
      if (this.isInternalUrl(url)) {
        this.createTab(url, { activate: true })
      } else {
        shell.openExternal(url)
      }
      return { action: 'deny' }
    })

    // 通常 <a href="..."> 遷移は will-navigate で捕捉
    wc.on('will-navigate', (event, targetUrl) => {
      const currentUrl = wc.getURL()
      // 外部リンクは OS ブラウザに振る
      if (!this.isInternalUrl(targetUrl)) {
        event.preventDefault()
        shell.openExternal(targetUrl)
        return
      }
      // 新規タブ化の対象になる遷移か厳密に判定。
      // ヒットしない遷移は同一タブ内で通常通り進めさせる（サーバリダイレクト等）。
      if (this.shouldOpenInNewTab(id, currentUrl, targetUrl)) {
        event.preventDefault()
        this.createTab(targetUrl, { activate: true })
      }
    })

    // WebContentsView にフォーカスがある時、キーイベントは renderer の
    // window.keydown には届かない（別 webContents のため）。
    //
    // ただしアプリメニューの accelerator はフォーカス位置に関係なくアプリ全体で
    // 発火するため、メニューが拾うキー（Ctrl+W / Ctrl+R / Ctrl+Shift+R / F12 /
    // Ctrl+Tab / Ctrl+Shift+Tab / Ctrl+Q）はここで処理すると二重発火する。
    // よって、ここで扱うのはメニュー accelerator に割り当てていないものだけ:
    //   - F5 / Ctrl+F5（リロード／ハードリロード。accelerator 割当が困難なため）
    //   - Ctrl+1..9（番号指定タブ切替）
    //   - Ctrl+L（ログペイン折りたたみ。renderer 側のロジックへ IPC 委譲）
    wc.on('before-input-event', (event, input) => {
      // Ctrl 自体の押下/解放を追跡してモジュールスコープのフラグを更新する。
      // タブ切替直後の新 view 側 input.control が false 扱いになる問題への対策。
      if (input.key === 'Control') {
        if (input.type === 'keyDown') ctrlPressed = true
        else if (input.type === 'keyUp') ctrlPressed = false
        return
      }

      if (input.type !== 'keyDown') return
      if (input.alt || input.meta) return

      // F5 / Ctrl+F5: リロード。F5 は Ctrl を伴わないため isCtrl 早期 return より
      // 前に処理する必要がある。F5=通常リロード、Ctrl+F5=キャッシュ無視。
      // いずれもアクティブタブ（pinned 含む）の WebContentsView が対象。
      if (input.key === 'F5') {
        event.preventDefault()
        if (input.control || ctrlPressed) this.hardReloadActiveTab()
        else this.reloadActiveTab()
        return
      }

      // Esc: 検索バーが開いている時のみ閉じる（WebContentsView にフォーカスがある
      // 状態での保険。Ctrl を伴わないため isCtrl 早期 return より前に処理する）。
      // 検索中でなければページ側の Esc 挙動を妨げない。
      if (input.key === 'Escape') {
        if (this.findActive) {
          event.preventDefault()
          this.closeFind()
        }
        return
      }

      // input.control が false でも、グローバルに Ctrl が押下中ならそれを採用
      const isCtrl = input.control || ctrlPressed
      if (!isCtrl) return

      const key = input.key
      const lower = key.length === 1 ? key.toLowerCase() : key

      // Ctrl+G / Ctrl+Shift+G: 次/前のマッチ（Firefox 互換）。検索中のみ作用。
      // 検索入力欄にフォーカスがある時は renderer 側で処理されるため、ここは
      // WebContentsView にフォーカスがある場合の経路。
      if (lower === 'g') {
        if (this.findActive) {
          event.preventDefault()
          this.findNext(!input.shift)
        }
        return
      }

      // Ctrl+1..9: 番号指定タブ切替
      if (!input.shift && /^[1-9]$/.test(key)) {
        event.preventDefault()
        const idx = parseInt(key, 10) - 1
        const target = this.tabs[idx]
        if (target) this.activateTab(target.id)
        return
      }

      // Ctrl+L: ログペイン折りたたみは renderer 側のハンドラなので IPC で依頼
      if (!input.shift && lower === 'l') {
        event.preventDefault()
        this.mainWindow.webContents.send('shortcut:toggle-log')
        return
      }
    })
  }

  // ログタブ（ローカル HTML）の before-input-event。
  // 通常タブの attachWebContentsHandlers 内ハンドラと同じ方針で、
  // メニュー accelerator に割り当てていないキーのみを扱う（二重発火防止）。
  //   - Ctrl+1..9（番号指定タブ切替）
  //   - Ctrl+L（ログペイン折りたたみ。Shell renderer へ IPC 委譲）
  // F5/Ctrl+F5（リロード）はログタブでは無意味なので扱わない。
  private attachLogTabInput(view: WebContentsView, _id: string): void {
    const wc = view.webContents
    wc.on('before-input-event', (event, input) => {
      if (input.key === 'Control') {
        if (input.type === 'keyDown') ctrlPressed = true
        else if (input.type === 'keyUp') ctrlPressed = false
        return
      }
      if (input.type !== 'keyDown') return
      if (input.alt || input.meta) return

      const isCtrl = input.control || ctrlPressed
      if (!isCtrl) return

      const key = input.key
      const lower = key.length === 1 ? key.toLowerCase() : key

      if (!input.shift && /^[1-9]$/.test(key)) {
        event.preventDefault()
        const idx = parseInt(key, 10) - 1
        const target = this.tabs[idx]
        if (target) this.activateTab(target.id)
        return
      }

      if (!input.shift && lower === 'l') {
        event.preventDefault()
        this.mainWindow.webContents.send('shortcut:toggle-log')
        return
      }
    })
  }

  // Ctrl+Tab / Ctrl+Shift+Tab のロジック
  private cycleTab(direction: 1 | -1): void {
    if (this.tabs.length === 0) return
    const idx = this.tabs.findIndex((t) => t.id === this.activeTabId)
    if (idx < 0) return
    const len = this.tabs.length
    const nextIdx = (idx + direction + len) % len
    const next = this.tabs[nextIdx]
    if (next) this.activateTab(next.id)
  }

  private findTabIdByView(view: WebContentsView): string | undefined {
    return this.tabs.find((t) => t.view === view)?.id
  }

  // will-navigate での新規タブ化判定。条件を厳しめにして、
  // サーバリダイレクトや SPA 内遷移を誤って拾わないようにする。
  private shouldOpenInNewTab(fromTabId: string, fromUrl: string, toUrl: string): boolean {
    const tab = this.findById(fromTabId)
    if (!tab || !tab.pinned) return false // pinned (Dashboard) からの遷移のみ対象

    let fromPath: string
    let toPath: string
    try {
      fromPath = new URL(fromUrl).pathname
      toPath = new URL(toUrl).pathname
    } catch {
      return false
    }

    // 同じパスはリロード扱い
    if (fromPath === toPath) return false

    // 「ダッシュボード位置からヘッダーリンクへ」の遷移のみ新規タブ化
    if (!DASHBOARD_PATHS.has(fromPath)) return false
    if (!NEW_TAB_PATHS.has(toPath)) return false
    return true
  }
}

// ============== ヘルパー ==============

function normalizeUrl(url: string): string {
  try {
    const u = new URL(url)
    return `${u.origin}${u.pathname}${u.search}`
  } catch {
    return url
  }
}


