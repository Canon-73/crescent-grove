// ログペインの上端境界線をドラッグしてペイン高さを変更するコンポーネント。
// 高さ変更後、main プロセスに WebContentsView の bounds 再計算を IPC で依頼する。

const MIN_HEIGHT = 0
const COLLAPSED_HEIGHT = 28 // header だけ見える高さ

export class Resizer {
  private resizerEl: HTMLElement
  private logPaneEl: HTMLElement
  private dragging = false
  private startY = 0
  private startHeight = 0

  constructor(resizerEl: HTMLElement, logPaneEl: HTMLElement) {
    this.resizerEl = resizerEl
    this.logPaneEl = logPaneEl

    this.resizerEl.addEventListener('mousedown', (e) => this.onMouseDown(e))
    window.addEventListener('mousemove', (e) => this.onMouseMove(e))
    window.addEventListener('mouseup', () => this.onMouseUp())
  }

  private onMouseDown(e: MouseEvent): void {
    if (this.logPaneEl.classList.contains('collapsed')) return
    this.dragging = true
    this.startY = e.clientY
    this.startHeight = this.logPaneEl.offsetHeight
    this.resizerEl.classList.add('dragging')
    document.body.style.cursor = 'row-resize'
    e.preventDefault()
  }

  private onMouseMove(e: MouseEvent): void {
    if (!this.dragging) return
    const delta = e.clientY - this.startY
    let h = this.startHeight - delta
    // 画面高の 70% を上限、最小は collapse 寸前まで
    const maxH = Math.floor(window.innerHeight * 0.7)
    if (h < COLLAPSED_HEIGHT + 20) h = COLLAPSED_HEIGHT + 20
    if (h > maxH) h = maxH
    if (h < MIN_HEIGHT) h = MIN_HEIGHT
    this.logPaneEl.style.height = `${h}px`
  }

  private onMouseUp(): void {
    if (!this.dragging) return
    this.dragging = false
    this.resizerEl.classList.remove('dragging')
    document.body.style.cursor = ''
  }
}
