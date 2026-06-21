// preload で contextBridge 経由で公開される API の型定義

export type LogLevel = 'info' | 'debug' | 'warn' | 'error'
export type LogStreamKind = 'stdout' | 'stderr'

export interface LogEntry {
  id: number
  timestamp: string
  module: string
  level: LogLevel
  body: string
  raw: string
  stream: LogStreamKind
  receivedAt: number
}

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

export interface LinerAPI {
  ready: () => void
  onLogLine: (cb: (entry: LogEntry) => void) => () => void
  getLogBuffer: () => Promise<LogEntry[]>
  setContentBounds: (bounds: ContentBounds) => void

  // タブ
  onTabsChanged: (cb: (tabs: TabInfo[], activeId: string | null) => void) => () => void
  activateTab: (tabId: string) => void
  closeTab: (tabId: string) => void
  reloadActiveTab: () => void
  hardReloadActiveTab: () => void
  listTabs: () => Promise<{ tabs: TabInfo[]; activeId: string | null }>
  onShortcutToggleLog: (cb: () => void) => () => void
  onThemeChanged: (cb: (payload: ThemePayload) => void) => () => void
}

export interface ThemePayload {
  theme: string
  palette: Record<string, string>
}

// ページ内検索（webContents.findInPage の found-in-page result から必要分だけ抜粋）
export interface FindResult {
  requestId: number
  activeMatchOrdinal: number
  matches: number
  finalUpdate: boolean
}

export interface FindRunOptions {
  forward?: boolean
  matchCase?: boolean
  findNext?: boolean
}

export interface FindAPI {
  // 検索実行/更新（入力変更時は findNext:false で全ハイライト）
  run: (text: string, options?: FindRunOptions) => void
  // 次/前のマッチへ移動（forward=true で次、false で前）
  next: (forward: boolean) => void
  // 検索停止（選択ハイライトをクリア）。検索バーは閉じない。
  stop: () => void
  // 検索結果通知（件数表示更新用）
  onResult: (cb: (result: FindResult) => void) => () => void
  // 検索バーを開く指示（メニュー Ctrl+F → main 経由）
  onOpen: (cb: () => void) => () => void
  // 検索バーを閉じる指示（タブ切替/クローズ時に main から）
  onClosed: (cb: () => void) => () => void
}

export interface SplashAPI {
  onStatus: (cb: (text: string) => void) => () => void
}

export type ServerStatus = 'stopped' | 'starting' | 'running' | 'crashed'

export interface ServerOpResult {
  ok: boolean
  status: ServerStatus
  error?: string
}

// サーバー操作 API（起動 / 停止 / 再起動）。preload が contextBridge で公開する。
export interface ServerAPI {
  getStatus: () => Promise<ServerStatus>
  start: () => Promise<ServerOpResult>
  stop: () => Promise<ServerOpResult>
  restart: () => Promise<ServerOpResult>
  onStatusChanged: (cb: (status: ServerStatus) => void) => () => void
}

// サーバログ独立タブ（log-tab.html）専用 API。
// log-tab-preload.ts が contextBridge で公開する。
export interface LogTabAPI {
  onLogLine: (cb: (entry: LogEntry) => void) => () => void
  getLogBuffer: () => Promise<LogEntry[]>
  onThemeChanged: (cb: (payload: ThemePayload) => void) => () => void
  getTheme: () => Promise<ThemePayload | null>
}

export interface I18nAPI {
  getDict: () => Promise<Record<string, string>>
}

declare global {
  interface Window {
    linerAPI: LinerAPI
    splashAPI?: SplashAPI
    findAPI: FindAPI
    serverAPI: ServerAPI
    i18nAPI: I18nAPI
    // ログタブの文脈でのみ存在する
    logTabAPI: LogTabAPI
  }
}

// JSON モジュールの型
declare module '~/colors.json' {
  const colors: Record<string, string>
  export default colors
}
