import { LogView } from './LogView'
import type { LogEntry } from '../types'

// 下部ログペイン。
// 表示の中核（append/フィルタ/色付け/オートスクロール/リングバッファ）は
// LogView に委譲し、このクラスはペイン固有 UI（折りたたみ）だけを担う。
// 外部から見た挙動（append / appendMany / clear / toggleCollapse）は従来と完全同一。

export class LogPane {
  private view: LogView
  private collapseBtn: HTMLButtonElement
  private paneEl: HTMLElement

  constructor() {
    this.paneEl = mustEl('#log-pane')
    this.collapseBtn = mustEl('#log-collapse-btn') as HTMLButtonElement

    this.view = new LogView({
      body: mustEl('#log-body'),
      filterInput: mustEl('#log-filter') as HTMLInputElement,
      moduleFilters: mustEl('#log-module-filters'),
      clearBtn: mustEl('#log-clear-btn') as HTMLButtonElement
    })

    this.collapseBtn.addEventListener('click', () => this.toggleCollapse())
  }

  appendMany(entries: LogEntry[]): void {
    this.view.appendMany(entries)
  }

  append(entry: LogEntry): void {
    this.view.append(entry)
  }

  clear(): void {
    this.view.clear()
  }

  toggleCollapse(): void {
    const collapsed = this.paneEl.classList.toggle('collapsed')
    this.collapseBtn.textContent = collapsed ? '▶' : '▼'
  }
}

function mustEl(sel: string): HTMLElement {
  const el = document.querySelector<HTMLElement>(sel)
  if (!el) throw new Error(`element not found: ${sel}`)
  return el
}
