import type { TabInfo } from '../types'

// Phase 4: 動的に複数タブを描画し、クリックでアクティブ化、× で閉じる。

export class TabBar {
  private root: HTMLElement
  private clickHandler: ((tabId: string) => void) | null = null
  private closeHandler: ((tabId: string) => void) | null = null
  private currentTabs: TabInfo[] = []
  private currentActiveId: string | null = null
  // 閉じる×ボタンの title。i18n 辞書取得後に setCloseTitle で差し替える。
  private closeTitle = 'タブを閉じる'

  constructor(root: HTMLElement) {
    this.root = root

    // イベント委譲: タブ全体のクリックを root で受ける
    this.root.addEventListener('click', (e) => this.handleClick(e))
    // 中ボタンクリックで閉じる
    this.root.addEventListener('auxclick', (e) => this.handleAuxClick(e))
  }

  render(tabs: TabInfo[], activeTabId: string | null): void {
    this.currentTabs = tabs
    this.currentActiveId = activeTabId

    // タブ要素のみを差し替える。#tab-bar には #nav-controls / #server-controls
    // など右寄せのコントロール群も同居しているため、innerHTML='' で全消去すると
    // それらまで消えてしまう。既存の .tab だけを除去し、コントロール群の手前に
    // 新しいタブを挿入する。
    const controlsAnchor = this.root.querySelector('#nav-controls, #server-controls')
    this.root.querySelectorAll('.tab').forEach((el) => el.remove())

    for (const tab of tabs) {
      const el = document.createElement('div')
      el.className = 'tab'
      if (tab.id === activeTabId) el.classList.add('tab-active')
      if (tab.pinned) el.classList.add('tab-pinned')
      el.dataset['tabId'] = tab.id
      el.title = `${tab.title}\n${tab.url}`

      const title = document.createElement('span')
      title.className = 'tab-title'
      title.textContent = tab.title
      el.appendChild(title)

      if (!tab.pinned) {
        const close = document.createElement('button')
        close.className = 'tab-close'
        close.textContent = '×'
        close.title = this.closeTitle
        close.dataset['close'] = '1'
        el.appendChild(close)
      }

      // コントロール群があればその手前に、なければ末尾に追加する。
      if (controlsAnchor) this.root.insertBefore(el, controlsAnchor)
      else this.root.appendChild(el)
    }
  }

  // アクティブタブの次/前を返す
  nextTabId(): string | null {
    if (this.currentTabs.length === 0) return null
    const idx = this.currentTabs.findIndex((t) => t.id === this.currentActiveId)
    const next = this.currentTabs[(idx + 1) % this.currentTabs.length]
    return next ? next.id : null
  }

  prevTabId(): string | null {
    if (this.currentTabs.length === 0) return null
    const idx = this.currentTabs.findIndex((t) => t.id === this.currentActiveId)
    const len = this.currentTabs.length
    const prev = this.currentTabs[(idx - 1 + len) % len]
    return prev ? prev.id : null
  }

  // 1-origin の番号でタブ取得（Ctrl+1〜9 用）
  tabIdAt(index1: number): string | null {
    const t = this.currentTabs[index1 - 1]
    return t ? t.id : null
  }

  activeTabId(): string | null {
    return this.currentActiveId
  }

  isPinned(tabId: string): boolean {
    return !!this.currentTabs.find((t) => t.id === tabId && t.pinned)
  }

  // 閉じる×ボタンの title を差し替え、現在のタブを再描画する（i18n 適用用）。
  setCloseTitle(title: string): void {
    if (!title) return
    this.closeTitle = title
    this.render(this.currentTabs, this.currentActiveId)
  }

  onTabClick(cb: (tabId: string) => void): void {
    this.clickHandler = cb
  }

  onTabClose(cb: (tabId: string) => void): void {
    this.closeHandler = cb
  }

  // ============== 内部 ==============

  private handleClick(e: MouseEvent): void {
    const target = e.target as HTMLElement
    if (!target) return

    // × ボタン
    if (target.dataset['close'] === '1') {
      const tabEl = target.closest('.tab') as HTMLElement | null
      const id = tabEl?.dataset['tabId']
      if (id && this.closeHandler) this.closeHandler(id)
      e.stopPropagation()
      return
    }

    // タブ本体クリック
    const tabEl = target.closest('.tab') as HTMLElement | null
    const id = tabEl?.dataset['tabId']
    if (id && this.clickHandler) this.clickHandler(id)
  }

  private handleAuxClick(e: MouseEvent): void {
    // 中ボタン (button === 1) で閉じる
    if (e.button !== 1) return
    const target = e.target as HTMLElement
    const tabEl = target.closest('.tab') as HTMLElement | null
    const id = tabEl?.dataset['tabId']
    if (!id) return
    if (this.isPinned(id)) return
    if (this.closeHandler) this.closeHandler(id)
    e.preventDefault()
  }
}
