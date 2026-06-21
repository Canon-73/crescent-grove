// Crescent Liner renderer のエントリポイント
// - TabBar / LogPane / Resizer の初期化
// - ready 通知、ログ受信、bounds 通知
// - タブ操作 IPC、キーボードショートカット

import { TabBar } from './components/TabBar'
import { LogPane } from './components/LogPane'
import { Resizer } from './components/Resizer'
import { FindBar } from './components/FindBar'
import type { ContentBounds, FindResult } from './types'

// === 初期化 ===
const tabBarEl = document.getElementById('tab-bar')!
const contentAreaEl = document.getElementById('content-area')!
const resizerEl = document.getElementById('resizer')!
const logPaneEl = document.getElementById('log-pane')!
const findBarEl = document.getElementById('find-bar')!

const tabBar = new TabBar(tabBarEl)
const logPane = new LogPane()
new Resizer(resizerEl, logPaneEl)
// 検索バー: 開閉で content-area が伸縮 → ResizeObserver が拾うが、
// 念のため明示的にも bounds 更新をスケジュールする。
const findBar = new FindBar(findBarEl, {
  onVisibilityChange: () => scheduleBoundsUpdate()
})

// === タブ操作 ===
tabBar.onTabClick((tabId) => window.linerAPI.activateTab(tabId))
tabBar.onTabClose((tabId) => window.linerAPI.closeTab(tabId))

window.linerAPI.onTabsChanged((tabs, activeId) => {
  tabBar.render(tabs, activeId)
})

// WebContentsView 経由のショートカット委譲を受ける
window.linerAPI.onShortcutToggleLog(() => {
  logPane.toggleCollapse()
  scheduleBoundsUpdate()
})

// === ページ内検索 ===
// メニュー (Ctrl+F) からの開く指示。WebContentsView フォーカス時でも届く。
window.findAPI.onOpen(() => findBar.open())
// タブ切替/クローズ時に main から閉じる指示
window.findAPI.onClosed(() => findBar.close())
// 検索結果（件数・ラップ通知）
window.findAPI.onResult((result) => findBar.setResult(result as FindResult))

// ダッシュボードのテーマ + CSS 変数を Shell UI に伝播。
// テーマ別の配色を Liner 側でハードコードせず、ダッシュボードから受け取った
// CSS 変数（--cg-* として再公開）にそのまま追従する。
window.linerAPI.onThemeChanged((payload) => {
  const root = document.documentElement
  root.setAttribute('data-shell-theme', payload.theme)
  for (const [key, value] of Object.entries(payload.palette)) {
    if (!value) continue
    // camelCase → kebab-case で --cg-* に再マッピング
    // 例: 'bgPrimary' → '--cg-bg-primary'
    const cssVar = '--cg-' + key.replace(/([A-Z])/g, '-$1').toLowerCase()
    root.style.setProperty(cssVar, value)
  }
})

// === ログ受信 ===
// 起動時バッファ（server.py の起動ログ含む）を先に流し込み、以降は逐次受信する。
// LogStream を server 起動より前に接続しているため、ターミナル相当の起動ログも
// このバッファに含まれる。
window.linerAPI.getLogBuffer().then((buf) => {
  logPane.appendMany(buf)
})
window.linerAPI.onLogLine((entry) => {
  logPane.append(entry)
})

// === renderer → main: content-area の bounds を通知 ===
let pendingTimer: number | null = null
let lastBounds: ContentBounds | null = null

function pushBounds(): void {
  const r = contentAreaEl.getBoundingClientRect()
  const bounds: ContentBounds = {
    x: Math.round(r.left),
    y: Math.round(r.top),
    width: Math.round(r.width),
    height: Math.round(r.height)
  }
  if (
    lastBounds &&
    lastBounds.x === bounds.x &&
    lastBounds.y === bounds.y &&
    lastBounds.width === bounds.width &&
    lastBounds.height === bounds.height
  ) {
    return
  }
  lastBounds = bounds
  window.linerAPI.setContentBounds(bounds)
}

function scheduleBoundsUpdate(): void {
  if (pendingTimer != null) return
  pendingTimer = window.setTimeout(() => {
    pendingTimer = null
    pushBounds()
  }, 16)
}

const ro = new ResizeObserver(() => scheduleBoundsUpdate())
ro.observe(contentAreaEl)
window.addEventListener('resize', scheduleBoundsUpdate)

// === キーボードショートカット ===
// 注: WebContentsView がフォーカスを持っている時はここに届かない。
// タブ操作の大半（Ctrl+W / Ctrl+R / Ctrl+Shift+R / Ctrl+Tab / Ctrl+Shift+Tab /
// F12）はアプリメニューの accelerator がアプリ全体で処理するため、ここでは扱わない
// （メニューと二重発火させないため）。
// ここで扱うのはメニューに割り当てていないもの:
//   - Ctrl+L（ログペイン折りたたみ。Shell renderer 固有の UI）
//   - Ctrl+1〜9（番号指定タブ切替）
// なお WebContentsView フォーカス時はこれらが main 側 before-input-event で
// 捕捉される（focus 位置に応じてどちらか一方のみが発火するので競合しない）。
window.addEventListener('keydown', (e) => {
  // Ctrl+L: ログペイン折りたたみ／展開
  if (e.ctrlKey && !e.shiftKey && !e.altKey && (e.key === 'l' || e.key === 'L')) {
    e.preventDefault()
    logPane.toggleCollapse()
    scheduleBoundsUpdate()
    return
  }

  // Ctrl+1〜9: タブ番号指定切替
  if (e.ctrlKey && !e.shiftKey && !e.altKey && /^[1-9]$/.test(e.key)) {
    e.preventDefault()
    const target = tabBar.tabIdAt(parseInt(e.key, 10))
    if (target) window.linerAPI.activateTab(target)
    return
  }
})

// === 初期表示: 現状のタブを取得して描画 ===
window.linerAPI.listTabs().then(({ tabs, activeId }) => {
  if (tabs.length > 0) tabBar.render(tabs, activeId)
})

// === サーバー操作（起動 / 停止 / 再起動） ===
const STATUS_TEXT: Record<string, string> = {
  stopped: '停止中',
  starting: '起動中…',
  running: '稼働中',
  crashed: '異常終了'
}
const confirmMessages = {
  stop: 'サーバーを停止しますか？\n処理中のエージェントの応答が中断される場合があります。',
  restart: 'サーバーを再起動しますか？\n処理中のエージェントの応答が中断される場合があります。'
}
// i18n 辞書取得後にステータスラベルを再描画するためのフック。
// getStatus() が getDict() より先に解決して日本語ラベルが残るのを防ぐ。
let reapplyStatusLabel: () => void = () => {}
{
  const startBtn = document.getElementById('server-start-btn') as HTMLButtonElement
  const stopBtn = document.getElementById('server-stop-btn') as HTMLButtonElement
  const restartBtn = document.getElementById('server-restart-btn') as HTMLButtonElement
  const dot = document.getElementById('server-status-dot')!
  const label = document.getElementById('server-status-label')!
  let lastStatus = 'stopped'

  function applyStatus(status: string): void {
    lastStatus = status
    dot.className = `status-${status}`
    label.textContent = STATUS_TEXT[status] ?? status
    const running = status === 'running'
    const stopped = status === 'stopped'
    const crashed = status === 'crashed'
    const busy = status === 'starting'
    startBtn.disabled = busy || running
    stopBtn.disabled = busy || stopped || crashed
    restartBtn.disabled = busy || stopped
  }

  function setBusy(): void {
    startBtn.disabled = true
    stopBtn.disabled = true
    restartBtn.disabled = true
  }

  startBtn.addEventListener('click', async () => {
    setBusy()
    const r = await window.serverAPI.start()
    applyStatus(r.status)
  })
  stopBtn.addEventListener('click', async () => {
    if (!window.confirm(confirmMessages.stop)) return
    setBusy()
    const r = await window.serverAPI.stop()
    applyStatus(r.status)
  })
  restartBtn.addEventListener('click', async () => {
    if (!window.confirm(confirmMessages.restart)) return
    setBusy()
    const r = await window.serverAPI.restart()
    applyStatus(r.status)
  })

  window.serverAPI.onStatusChanged((status) => applyStatus(status))
  window.serverAPI.getStatus().then((status) => applyStatus(status))
  // i18n 辞書が後から届いた時に、現在の状態でラベルを再描画できるようにする。
  reapplyStatusLabel = () => applyStatus(lastStatus)
}

// === ナビ操作（再読込 / 強制再読込 / 検索） ===
// メニュー (View) と同じ操作を、タブバー上のボタンでワンクリック実行できるようにする。
{
  const reloadBtn = document.getElementById('nav-reload-btn') as HTMLButtonElement
  const hardReloadBtn = document.getElementById('nav-hardreload-btn') as HTMLButtonElement
  const findBtn = document.getElementById('nav-find-btn') as HTMLButtonElement

  reloadBtn.addEventListener('click', () => window.linerAPI.reloadActiveTab())
  hardReloadBtn.addEventListener('click', () => window.linerAPI.hardReloadActiveTab())
  // 検索バーは renderer 側 UI なので直接開く（メニュー Ctrl+F と同じ FindBar を再利用）。
  findBtn.addEventListener('click', () => findBar.open())
}

// === i18n: main プロセスから辞書を取得し DOM に適用 ===
window.i18nAPI.getDict().then((dict) => {
  const t = (key: string) => dict[key] || ''

  // title 属性
  const titles: Record<string, string> = {
    'nav-reload-btn': t('nav_reload_tooltip'),
    'nav-hardreload-btn': t('nav_hard_reload_tooltip'),
    'nav-find-btn': t('nav_find_tooltip'),
    'server-status-dot': t('server_status_tooltip'),
    'server-start-btn': t('server_start_tooltip'),
    'server-stop-btn': t('server_stop_tooltip'),
    'server-restart-btn': t('server_restart_tooltip'),
    'find-prev': t('find_prev_tooltip'),
    'find-next': t('find_next_tooltip'),
    'find-close': t('find_close_tooltip'),
    'log-collapse-btn': t('log_collapse_tooltip'),
    'log-clear-btn': t('log_clear_tooltip'),
  }
  for (const [id, val] of Object.entries(titles)) {
    const el = document.getElementById(id)
    if (el && val) el.title = val
  }

  // ボタンテキスト
  const texts: Record<string, string> = {
    'nav-reload-btn': `⟳ ${t('nav_reload')}`,
    'nav-hardreload-btn': `⟳ ${t('nav_hard_reload')}`,
    'nav-find-btn': `🔍 ${t('nav_find')}`,
    'server-start-btn': `▶ ${t('server_start')}`,
    'server-stop-btn': `■ ${t('server_stop')}`,
    'server-restart-btn': `⟳ ${t('server_restart')}`,
  }
  for (const [id, val] of Object.entries(texts)) {
    const el = document.getElementById(id)
    if (el) el.textContent = val
  }

  // 検索バー
  const findInput = document.getElementById('find-input') as HTMLInputElement | null
  if (findInput && t('find_placeholder')) findInput.placeholder = t('find_placeholder')

  // チェックボックスラベル
  const highlightSpan = document.querySelector('#find-highlight-label span')
  if (highlightSpan && t('find_highlight_all')) highlightSpan.textContent = t('find_highlight_all')
  const caseSpan = document.querySelector('#find-matchcase-label span')
  if (caseSpan && t('find_case_sensitive')) caseSpan.textContent = t('find_case_sensitive')

  // STATUS_TEXT を差し替え、現在の状態でラベルを再描画する
  if (t('status_stopped')) {
    STATUS_TEXT.stopped = t('status_stopped')
    STATUS_TEXT.starting = t('status_starting')
    STATUS_TEXT.running = t('status_running')
    STATUS_TEXT.crashed = t('status_crashed')
    reapplyStatusLabel()
  }

  // confirm メッセージを保持
  if (t('confirm_stop_prompt')) {
    confirmMessages.stop = t('confirm_stop_prompt')
    confirmMessages.restart = t('confirm_restart_prompt')
  }

  // タブの閉じる×ボタンの title（既存タブも再描画される）
  if (t('tab_close')) tabBar.setCloseTitle(t('tab_close'))
})

// === main にレンダラ準備完了を通知 ===
window.linerAPI.ready()
requestAnimationFrame(() => pushBounds())
