import colors from '~/colors.json'
import type { LogEntry } from '../types'

// ログ表示の再利用可能コア。
// 「コンテナ（本体・フィルタ入力・モジュールフィルタ・任意のクリアボタン）を受け取り、
//  受信した LogEntry を append し、フィルタ・色付け・オートスクロール・リングバッファ管理を行う」
// という責務を担う。下部ログペイン（LogPane）と独立ログタブ（log-tab）の両方から使う。
//
// 注: ここには「折りたたみ（collapse）」は含めない。折りたたみはペイン固有 UI なので
//     LogPane 側が担当する。LogView の挙動は従来の LogPane 内部処理と完全に同一。

const MAX_LINES = 10000
const AUTO_SCROLL_THRESHOLD_PX = 50

export interface LogViewElements {
  body: HTMLElement
  filterInput: HTMLInputElement
  moduleFilters: HTMLElement
  // クリアボタンは任意（ログタブには置くが、無くても動く）
  clearBtn?: HTMLButtonElement
}

export class LogView {
  private bodyEl: HTMLElement
  private filterInput: HTMLInputElement
  private moduleFiltersEl: HTMLElement
  private clearBtn?: HTMLButtonElement
  // 全モジュールを一括ON/OFFするトグルボタン
  private toggleAllBtn: HTMLButtonElement

  private lines: { entry: LogEntry; el: HTMLElement }[] = []
  private knownModules = new Set<string>()
  // モジュール名 → 表示するか
  private moduleEnabled = new Map<string, boolean>()
  private filterText = ''

  constructor(els: LogViewElements) {
    this.bodyEl = els.body
    this.filterInput = els.filterInput
    this.moduleFiltersEl = els.moduleFilters
    this.clearBtn = els.clearBtn

    this.filterInput.addEventListener('input', () => {
      this.filterText = this.filterInput.value.toLowerCase()
      this.applyFilters()
    })
    this.clearBtn?.addEventListener('click', () => this.clear())

    // 全モジュールの表示を一括でON/OFFするボタン。
    // モジュールフィルタ群はスクロールするので、その外（ヘッダー直下、
    // フィルタ群の直前）に置いて常に見えるようにする。
    this.toggleAllBtn = document.createElement('button')
    this.toggleAllBtn.id = 'log-toggle-all-btn'
    this.toggleAllBtn.type = 'button'
    this.toggleAllBtn.title = '全モジュールの表示を一括ON/OFF'
    this.toggleAllBtn.textContent = '全OFF'
    this.toggleAllBtn.addEventListener('click', () => this.toggleAllModules())
    const host = this.moduleFiltersEl.parentElement ?? this.moduleFiltersEl
    host.insertBefore(this.toggleAllBtn, this.moduleFiltersEl)
  }

  appendMany(entries: LogEntry[]): void {
    for (const e of entries) this.append(e, /*skipScroll*/ true)
    this.scrollToBottomIfNeeded(true)
  }

  append(entry: LogEntry, skipScroll = false): void {
    const shouldAutoScroll = !skipScroll && this.isNearBottom()
    const el = this.renderLine(entry)
    this.bodyEl.appendChild(el)
    this.lines.push({ entry, el })

    // 新規モジュール検出 → フィルタ UI に追加
    if (!this.knownModules.has(entry.module)) {
      this.knownModules.add(entry.module)
      this.moduleEnabled.set(entry.module, true)
      this.addModuleFilter(entry.module)
    }

    // フィルタ適用（個別判定の方が高速）
    if (!this.shouldShow(entry)) {
      el.classList.add('hidden')
    }

    // リングバッファ超過分を DOM ごと削除
    while (this.lines.length > MAX_LINES) {
      const head = this.lines.shift()
      if (head) head.el.remove()
    }

    if (shouldAutoScroll) this.scrollToBottomIfNeeded(true)
  }

  clear(): void {
    this.lines = []
    this.bodyEl.innerHTML = ''
  }

  // ====================== 内部 ======================

  private renderLine(entry: LogEntry): HTMLElement {
    const div = document.createElement('div')
    div.className = `log-line level-${entry.level} stream-${entry.stream}`

    const time = document.createElement('span')
    time.className = 'log-time'
    time.textContent = `[${entry.timestamp}]`

    const mod = document.createElement('span')
    mod.className = 'log-module'
    mod.textContent = `[${entry.module}]`
    mod.style.color = moduleColor(entry.module)

    const body = document.createElement('span')
    body.className = 'log-body'
    body.textContent = entry.body || entry.raw

    div.appendChild(time)
    div.appendChild(mod)
    div.appendChild(body)
    return div
  }

  private addModuleFilter(module: string): void {
    const label = document.createElement('label')
    label.title = module
    const cb = document.createElement('input')
    cb.type = 'checkbox'
    cb.checked = true
    cb.dataset['module'] = module
    cb.addEventListener('change', () => {
      this.moduleEnabled.set(module, cb.checked)
      this.updateToggleAllLabel()
      this.applyFilters()
    })
    const name = document.createElement('span')
    name.textContent = module
    name.style.color = moduleColor(module)
    label.appendChild(cb)
    label.appendChild(name)
    this.moduleFiltersEl.appendChild(label)
    // 新規モジュールは既定でONなので、ボタン表記（全ON/全OFF）を更新
    this.updateToggleAllLabel()
  }

  // 全モジュールの一括ON/OFF。ひとつでもONなら全OFF、すべてOFFなら全ONにする。
  private toggleAllModules(): void {
    const anyOn = [...this.moduleEnabled.values()].some((v) => v)
    const next = !anyOn
    for (const m of this.knownModules) {
      this.moduleEnabled.set(m, next)
    }
    // チェックボックスのUIも同期する
    this.moduleFiltersEl
      .querySelectorAll<HTMLInputElement>('input[type="checkbox"]')
      .forEach((cb) => {
        cb.checked = next
      })
    this.updateToggleAllLabel()
    this.applyFilters()
  }

  // ボタンの表記を「次に押したときの動作」に合わせる。
  private updateToggleAllLabel(): void {
    const anyOn = [...this.moduleEnabled.values()].some((v) => v)
    this.toggleAllBtn.textContent = anyOn ? '全OFF' : '全ON'
  }

  private shouldShow(entry: LogEntry): boolean {
    const modOn = this.moduleEnabled.get(entry.module) ?? true
    if (!modOn) return false
    if (this.filterText.length === 0) return true
    // 部分一致（大文字小文字無視）
    const hay = `${entry.module} ${entry.body} ${entry.raw}`.toLowerCase()
    return hay.includes(this.filterText)
  }

  private applyFilters(): void {
    const nearBottom = this.isNearBottom()
    for (const { entry, el } of this.lines) {
      const show = this.shouldShow(entry)
      el.classList.toggle('hidden', !show)
    }
    if (nearBottom) this.scrollToBottomIfNeeded(true)
  }

  private isNearBottom(): boolean {
    const remaining = this.bodyEl.scrollHeight - (this.bodyEl.scrollTop + this.bodyEl.clientHeight)
    return remaining <= AUTO_SCROLL_THRESHOLD_PX
  }

  private scrollToBottomIfNeeded(force = false): void {
    if (force || this.isNearBottom()) {
      this.bodyEl.scrollTop = this.bodyEl.scrollHeight
    }
  }
}

// モジュール名 → 色。colors.json になければハッシュから HSL 自動生成。
function moduleColor(module: string): string {
  const known = (colors as Record<string, string>)[module]
  if (known) return known
  return hashColor(module)
}

function hashColor(s: string): string {
  let h = 0
  for (const c of s) h = (h * 31 + c.charCodeAt(0)) | 0
  return `hsl(${Math.abs(h) % 360}, 60%, 70%)`
}
