import type { FindResult } from '../types'

// Firefox 風のページ内検索バー。
// - 検索コマンドは window.findAPI 経由で main → アクティブタブの WebContentsView へ。
// - 結果（件数/末尾ラップ）は main から find:result で届き、setResult で表示更新。
// - 表示/非表示の切替時に onVisibilityChange を呼び、呼び出し側が
//   bounds を再計算して WebContentsView を 36px 縮める/戻す。

interface FindBarOptions {
  // 検索バーの表示状態が変わった時（開閉）に呼ばれる。bounds 再計算のトリガ。
  onVisibilityChange?: () => void
}

const NOTICE_DURATION_MS = 2000

export class FindBar {
  private barEl: HTMLElement
  private inputEl: HTMLInputElement
  private countEl: HTMLElement
  private prevBtn: HTMLButtonElement
  private nextBtn: HTMLButtonElement
  private matchCaseCb: HTMLInputElement
  private noticeEl: HTMLElement
  private closeBtn: HTMLButtonElement

  private opened = false
  // 末尾→先頭（または先頭→末尾）のラップ検知用。
  // next/prev 押下時に方向を記録し、結果の ordinal 変化でラップを判定する。
  private lastOrdinal = 0
  private lastDirection: 'forward' | 'backward' | null = null
  private noticeTimer: number | null = null
  private onVisibilityChange?: () => void

  constructor(barEl: HTMLElement, options: FindBarOptions = {}) {
    this.barEl = barEl
    this.onVisibilityChange = options.onVisibilityChange

    this.inputEl = mustEl<HTMLInputElement>('#find-input')
    this.countEl = mustEl('#find-count')
    this.prevBtn = mustEl<HTMLButtonElement>('#find-prev')
    this.nextBtn = mustEl<HTMLButtonElement>('#find-next')
    this.matchCaseCb = mustEl<HTMLInputElement>('#find-match-case')
    this.noticeEl = mustEl('#find-notice')
    this.closeBtn = mustEl<HTMLButtonElement>('#find-close')

    // 入力変更ごとにリアルタイム検索（全マッチをハイライト）
    this.inputEl.addEventListener('input', () => {
      this.lastDirection = null
      this.lastOrdinal = 0
      this.clearNotice()
      window.findAPI.run(this.inputEl.value, {
        findNext: false,
        matchCase: this.matchCaseCb.checked
      })
      if (!this.inputEl.value) this.countEl.textContent = ''
    })

    // 入力欄でのキー操作
    this.inputEl.addEventListener('keydown', (e) => {
      if (e.key === 'Enter') {
        e.preventDefault()
        this.go(!e.shiftKey)
        return
      }
      if (e.key === 'Escape') {
        e.preventDefault()
        this.requestClose()
        return
      }
      // Ctrl+G / Ctrl+Shift+G（Firefox 互換）。入力欄フォーカス時の経路。
      if (e.ctrlKey && (e.key === 'g' || e.key === 'G')) {
        e.preventDefault()
        this.go(!e.shiftKey)
        return
      }
    })

    this.prevBtn.addEventListener('click', () => this.go(false))
    this.nextBtn.addEventListener('click', () => this.go(true))
    this.closeBtn.addEventListener('click', () => this.requestClose())

    // 「大文字小文字を区別」切替時は現在の検索語で再検索
    this.matchCaseCb.addEventListener('change', () => {
      if (this.inputEl.value) {
        this.lastDirection = null
        window.findAPI.run(this.inputEl.value, {
          findNext: false,
          matchCase: this.matchCaseCb.checked
        })
      }
    })
  }

  isOpen(): boolean {
    return this.opened
  }

  // 検索バーを開く。既に開いている場合は入力欄を再フォーカス＋全選択する。
  open(): void {
    const wasOpen = this.opened
    this.opened = true
    this.barEl.classList.add('open')
    this.inputEl.focus()
    this.inputEl.select()
    if (!wasOpen) {
      // 既存の入力が残っている場合は開き直しで再ハイライト
      if (this.inputEl.value) {
        window.findAPI.run(this.inputEl.value, {
          findNext: false,
          matchCase: this.matchCaseCb.checked
        })
      }
      this.onVisibilityChange?.()
    }
  }

  // 検索バーを閉じる（DOM 上の非表示のみ。検索停止は main 側で行われる/行う）。
  close(): void {
    if (!this.opened) return
    this.opened = false
    this.barEl.classList.remove('open')
    this.clearNotice()
    this.onVisibilityChange?.()
  }

  // 検索結果を受けて件数表示とラップ通知を更新する。
  setResult(result: FindResult): void {
    if (result.matches > 0) {
      this.countEl.textContent = `${result.activeMatchOrdinal} / ${result.matches}`
    } else {
      this.countEl.textContent = this.inputEl.value ? '0 / 0' : ''
    }

    // ラップ（末尾→先頭 / 先頭→末尾）の検知は finalUpdate のタイミングで行う
    if (result.finalUpdate && this.lastDirection && result.matches > 1) {
      if (
        this.lastDirection === 'forward' &&
        this.lastOrdinal === result.matches &&
        result.activeMatchOrdinal === 1
      ) {
        this.showNotice('先頭から再検索しました')
      } else if (
        this.lastDirection === 'backward' &&
        this.lastOrdinal === 1 &&
        result.activeMatchOrdinal === result.matches
      ) {
        this.showNotice('末尾から再検索しました')
      }
    }
    if (result.finalUpdate) this.lastOrdinal = result.activeMatchOrdinal
  }

  // ============== 内部 ==============

  // 次/前のマッチへ移動
  private go(forward: boolean): void {
    if (!this.inputEl.value) return
    this.lastDirection = forward ? 'forward' : 'backward'
    window.findAPI.next(forward)
  }

  // バーを閉じる要求（× / Esc）。main 側の検索も停止する。
  private requestClose(): void {
    window.findAPI.stop()
    this.close()
  }

  private showNotice(text: string): void {
    this.noticeEl.textContent = text
    this.noticeEl.classList.add('visible')
    if (this.noticeTimer != null) window.clearTimeout(this.noticeTimer)
    this.noticeTimer = window.setTimeout(() => {
      this.clearNotice()
    }, NOTICE_DURATION_MS)
  }

  private clearNotice(): void {
    if (this.noticeTimer != null) {
      window.clearTimeout(this.noticeTimer)
      this.noticeTimer = null
    }
    this.noticeEl.classList.remove('visible')
    this.noticeEl.textContent = ''
  }
}

function mustEl<T extends HTMLElement = HTMLElement>(sel: string): T {
  const el = document.querySelector<T>(sel)
  if (!el) throw new Error(`element not found: ${sel}`)
  return el
}
