// サーバログ独立タブのエントリポイント。
// - LogView を #log-tab 内の要素で初期化
// - 起動時に getLogBuffer() で履歴を流し込み、以降は onLogLine で逐次追記
// - テーマは getTheme() で初期適用し、onThemeChanged で追従
//
// 下部ログペイン（LogPane）とは独立した LogView インスタンスなので、
// フィルタ・モジュール選択・スクロール位置はこのタブ専用に保持される。

import { LogView } from './components/LogView'
import type { LogEntry } from './types'

const view = new LogView({
  body: mustEl('#log-body'),
  filterInput: mustEl('#log-filter') as HTMLInputElement,
  moduleFilters: mustEl('#log-module-filters'),
  clearBtn: mustEl('#log-clear-btn') as HTMLButtonElement
})

// ダッシュボードのテーマ + CSS 変数を反映（main.ts と同じマッピング）。
function applyTheme(payload: unknown): void {
  if (!payload || typeof payload !== 'object') return
  const p = payload as { theme?: string; palette?: Record<string, string> }
  const root = document.documentElement
  if (p.theme) root.setAttribute('data-shell-theme', p.theme)
  if (p.palette) {
    for (const [key, value] of Object.entries(p.palette)) {
      if (!value) continue
      // camelCase → kebab-case で --cg-* に再マッピング（例: 'bgPrimary' → '--cg-bg-primary'）
      const cssVar = '--cg-' + key.replace(/([A-Z])/g, '-$1').toLowerCase()
      root.style.setProperty(cssVar, value)
    }
  }
}

// 初期テーマ適用（未取得なら CSS の :root フォールバックがそのまま使われる）
window.logTabAPI.getTheme().then((payload) => applyTheme(payload))
window.logTabAPI.onThemeChanged((payload) => applyTheme(payload))

// i18n: クリアボタンの title を翻訳
window.i18nAPI.getDict().then((dict) => {
  if (dict.log_clear_tooltip) {
    const clearBtn = document.getElementById('log-clear-btn')
    if (clearBtn) clearBtn.title = dict.log_clear_tooltip
  }
})

// 履歴流し込み → 逐次受信
window.logTabAPI.getLogBuffer().then((buf) => {
  view.appendMany(buf as LogEntry[])
})
window.logTabAPI.onLogLine((entry) => {
  view.append(entry as LogEntry)
})

function mustEl(sel: string): HTMLElement {
  const el = document.querySelector<HTMLElement>(sel)
  if (!el) throw new Error(`element not found: ${sel}`)
  return el
}
