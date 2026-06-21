import { contextBridge, ipcRenderer, IpcRendererEvent } from 'electron'

// Phase 4:
// - splashAPI: Phase 2 から継続
// - linerAPI: Shell UI 用。タブ操作・ログ・bounds 通知

interface ContentBounds {
  x: number
  y: number
  width: number
  height: number
}

interface TabInfo {
  id: string
  url: string
  title: string
  pinned: boolean
}

const splashAPI = {
  onStatus: (cb: (text: string) => void): (() => void) => {
    const listener = (_e: IpcRendererEvent, text: string): void => cb(text)
    ipcRenderer.on('splash:status', listener)
    return () => ipcRenderer.removeListener('splash:status', listener)
  }
}

const linerAPI = {
  // renderer 準備完了通知
  ready: (): void => {
    ipcRenderer.send('renderer:ready')
  },

  // ログ
  onLogLine: (cb: (entry: unknown) => void): (() => void) => {
    const listener = (_e: IpcRendererEvent, entry: unknown): void => cb(entry)
    ipcRenderer.on('log:line', listener)
    return () => ipcRenderer.removeListener('log:line', listener)
  },
  getLogBuffer: (): Promise<unknown[]> => ipcRenderer.invoke('log:getBuffer'),

  // bounds
  setContentBounds: (bounds: ContentBounds): void => {
    ipcRenderer.send('content:setBounds', bounds)
  },

  // タブ
  onTabsChanged: (
    cb: (tabs: TabInfo[], activeId: string | null) => void
  ): (() => void) => {
    const listener = (
      _e: IpcRendererEvent,
      tabs: TabInfo[],
      activeId: string | null
    ): void => cb(tabs, activeId)
    ipcRenderer.on('tabs:changed', listener)
    return () => ipcRenderer.removeListener('tabs:changed', listener)
  },
  activateTab: (tabId: string): void => {
    ipcRenderer.send('tab:activate', tabId)
  },
  closeTab: (tabId: string): void => {
    ipcRenderer.send('tab:close', tabId)
  },
  reloadActiveTab: (): void => {
    ipcRenderer.send('tab:reload')
  },
  hardReloadActiveTab: (): void => {
    ipcRenderer.send('tab:hardReload')
  },
  // タブ一覧の同期取得（起動直後に renderer が現状を取得するため）
  listTabs: (): Promise<{ tabs: TabInfo[]; activeId: string | null }> =>
    ipcRenderer.invoke('tab:list'),

  // WebContentsView がフォーカスを持っている時のショートカットを main 経由で受ける
  onShortcutToggleLog: (cb: () => void): (() => void) => {
    const listener = (): void => cb()
    ipcRenderer.on('shortcut:toggle-log', listener)
    return () => ipcRenderer.removeListener('shortcut:toggle-log', listener)
  },

  // テーマ変更通知。ダッシュボード側で取得したテーマ名と CSS 変数パレットを受け取る。
  // payload 形: { theme: string, palette: Record<string, string> }
  onThemeChanged: (cb: (payload: unknown) => void): (() => void) => {
    const listener = (_e: IpcRendererEvent, payload: unknown): void => cb(payload)
    ipcRenderer.on('theme:changed', listener)
    return () => ipcRenderer.removeListener('theme:changed', listener)
  }
}

interface FindRunOptions {
  forward?: boolean
  matchCase?: boolean
  findNext?: boolean
}

// ページ内検索 API。対象は常にアクティブタブの WebContentsView（main 側で解決）。
const findAPI = {
  run: (text: string, options: FindRunOptions = {}): void => {
    ipcRenderer.send('find:run', { text, ...options })
  },
  next: (forward: boolean): void => {
    ipcRenderer.send('find:next', forward)
  },
  stop: (): void => {
    ipcRenderer.send('find:stop')
  },
  onResult: (cb: (result: unknown) => void): (() => void) => {
    const listener = (_e: IpcRendererEvent, result: unknown): void => cb(result)
    ipcRenderer.on('find:result', listener)
    return () => ipcRenderer.removeListener('find:result', listener)
  },
  onOpen: (cb: () => void): (() => void) => {
    const listener = (): void => cb()
    ipcRenderer.on('find:open', listener)
    return () => ipcRenderer.removeListener('find:open', listener)
  },
  onClosed: (cb: () => void): (() => void) => {
    const listener = (): void => cb()
    ipcRenderer.on('find:closed', listener)
    return () => ipcRenderer.removeListener('find:closed', listener)
  }
}

type ServerStatus = 'stopped' | 'starting' | 'running' | 'crashed'
interface ServerOpResult {
  ok: boolean
  status: ServerStatus
  error?: string
}

// サーバー操作 API（起動 / 停止 / 再起動）。Shell UI のボタンから使う。
const serverAPI = {
  getStatus: (): Promise<ServerStatus> => ipcRenderer.invoke('server:status'),
  start: (): Promise<ServerOpResult> => ipcRenderer.invoke('server:start'),
  stop: (): Promise<ServerOpResult> => ipcRenderer.invoke('server:stop'),
  restart: (): Promise<ServerOpResult> => ipcRenderer.invoke('server:restart'),
  onStatusChanged: (cb: (status: ServerStatus) => void): (() => void) => {
    const listener = (_e: IpcRendererEvent, status: ServerStatus): void => cb(status)
    ipcRenderer.on('server:status-changed', listener)
    return () => ipcRenderer.removeListener('server:status-changed', listener)
  }
}

const i18nAPI = {
  getDict: (): Promise<Record<string, string>> => ipcRenderer.invoke('i18n:dict')
}

try {
  contextBridge.exposeInMainWorld('splashAPI', splashAPI)
  contextBridge.exposeInMainWorld('linerAPI', linerAPI)
  contextBridge.exposeInMainWorld('findAPI', findAPI)
  contextBridge.exposeInMainWorld('serverAPI', serverAPI)
  contextBridge.exposeInMainWorld('i18nAPI', i18nAPI)
} catch (error) {
  console.error('[preload] contextBridge exposure failed:', error)
}
