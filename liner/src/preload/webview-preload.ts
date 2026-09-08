import { ipcRenderer } from 'electron'

// WebContentsView (= Crescent Grove のダッシュボード等) 専用の最小 preload。
// 唯一の責務はテーマ + CSS 変数（パレット）の検知と main プロセスへの一方向通知。
// 攻撃面を最小化するため API は公開せず、contextBridge も使わない。
//
// SPEC: Crescent Grove のテーマは
//   - localStorage.yuzuki_theme: 'dark' | 'light' | 'moonlit' | ...
//   - dark の時は属性なし、それ以外は document.body[data-theme="..."]
// 配色は :root スコープで定義された CSS 変数を getComputedStyle で取得する。

// Liner Shell UI が必要とするダッシュボード側 CSS 変数の一覧。
// dashboard.css に実在する変数だけを列挙する（存在しないものは空文字が返る）。
const PALETTE_VARS = [
  '--bg-primary',
  '--bg-secondary',
  '--bg-card',
  '--bg-hover',
  '--text-primary',
  '--text-secondary',
  '--text-muted',
  '--accent',
  '--accent-hover',
  '--border',
  '--warning',
  '--danger'
] as const

// 取得した CSS 変数を camelCase に変換したキーで持つ。
// 例: '--bg-primary' → 'bgPrimary'
type Palette = Record<string, string>

interface ThemePayload {
  theme: string
  palette: Palette
}

let lastSent: string | null = null

function getCurrentTheme(): string {
  const dataTheme = document.body?.getAttribute('data-theme')
  if (dataTheme) return dataTheme
  const htmlTheme = document.documentElement.getAttribute('data-theme')
  if (htmlTheme) return htmlTheme
  return 'dark'
}

function kebabToCamel(name: string): string {
  // '--bg-primary' → 'bgPrimary'
  return name.replace(/^--/, '').replace(/-([a-z])/g, (_m, c: string) => c.toUpperCase())
}

function readPalette(): Palette {
  // テーマ変数は :root スコープで定義されているため、body / documentElement
  // どちらの getComputedStyle でも参照できる。互換性のため body を優先。
  const target = document.body ?? document.documentElement
  const cs = getComputedStyle(target)
  const out: Palette = {}
  for (const v of PALETTE_VARS) {
    const value = cs.getPropertyValue(v).trim()
    if (value) out[kebabToCamel(v)] = value
  }
  return out
}

function buildPayload(): ThemePayload {
  return {
    theme: getCurrentTheme(),
    palette: readPalette()
  }
}

function notifyTheme(): void {
  const payload = buildPayload()
  const serialized = JSON.stringify(payload)
  if (serialized === lastSent) return // 重複通知の抑制
  lastSent = serialized
  ipcRenderer.send('webview:theme-changed', payload)
}

function startObserving(): void {
  const observer = new MutationObserver(() => notifyTheme())
  observer.observe(document.documentElement, {
    attributes: true,
    attributeFilter: ['data-theme', 'class']
  })
  if (document.body) {
    observer.observe(document.body, {
      attributes: true,
      attributeFilter: ['data-theme', 'class']
    })
  }
}

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', () => {
    startObserving()
    notifyTheme()
  })
} else {
  startObserving()
  notifyTheme()
}

// theme.js が DOMContentLoaded で書き換えるため、その後にもう一度送る保険
window.addEventListener('load', () => notifyTheme())

window.addEventListener('storage', (e) => {
  if (e.key === null || e.key === 'yuzuki_theme') notifyTheme()
})
