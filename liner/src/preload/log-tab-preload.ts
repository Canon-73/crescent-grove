import { contextBridge, ipcRenderer, IpcRendererEvent } from 'electron'

// サーバログタブ専用の preload。
// ログ受信（履歴 + 逐次）とテーマ取得/変更通知だけを公開する最小 API。
// タブ操作や検索 API は持たない（攻撃面を最小限に）。

const logTabAPI = {
  // 逐次受信するログ行（main の LogStream が extraTargets として配信）
  onLogLine: (cb: (entry: unknown) => void): (() => void) => {
    const listener = (_e: IpcRendererEvent, entry: unknown): void => cb(entry)
    ipcRenderer.on('log:line', listener)
    return () => ipcRenderer.removeListener('log:line', listener)
  },
  // 起動時に既存のログバッファ（最大 10000 行）を取得する
  getLogBuffer: (): Promise<unknown[]> => ipcRenderer.invoke('log:getBuffer'),

  // テーマ変更通知。payload 形: { theme: string, palette: Record<string, string> }
  onThemeChanged: (cb: (payload: unknown) => void): (() => void) => {
    const listener = (_e: IpcRendererEvent, payload: unknown): void => cb(payload)
    ipcRenderer.on('theme:changed', listener)
    return () => ipcRenderer.removeListener('theme:changed', listener)
  },
  // 現在のテーマを取得（タブ初期化直後の初期適用用）。未取得なら null。
  getTheme: (): Promise<unknown> => ipcRenderer.invoke('theme:current')
}

const i18nAPI = {
  getDict: (): Promise<Record<string, string>> => ipcRenderer.invoke('i18n:dict')
}

try {
  contextBridge.exposeInMainWorld('logTabAPI', logTabAPI)
  contextBridge.exposeInMainWorld('i18nAPI', i18nAPI)
} catch (error) {
  console.error('[log-tab-preload] contextBridge exposure failed:', error)
}
