import { BrowserWindow, type WebContents } from 'electron'
import type { ServerProcess } from './server-process'

// server.py からの行を構造化して renderer に流す。
// renderer 側はリングバッファを別途持つが、ここでも最大 10000 行保持して
// 起動が遅れた renderer に getBuffer で渡せるようにする。

export type LogLevel = 'info' | 'debug' | 'warn' | 'error'
export type LogStreamKind = 'stdout' | 'stderr'

export interface LogEntry {
  id: number
  timestamp: string // "HH:MM:SS"
  module: string
  level: LogLevel
  body: string
  raw: string
  stream: LogStreamKind
  receivedAt: number
}

// SPEC 準拠の行パース正規表現
const LINE_RE = /^\[(?<time>\d{2}:\d{2}:\d{2})\]\s+\[(?<module>[^\]]+)\]\s+(?<body>.*)$/

const MAX_BUFFER = 10000

export class LogStream {
  private mainWindow: BrowserWindow
  private buffer: LogEntry[] = []
  private nextId = 1
  // ログタブ等、Shell renderer 以外に log:line を配信する追加ターゲット。
  // サーバログタブを開くと登録され、閉じる/破棄時に removeTarget で外れる。
  private extraTargets = new Set<WebContents>()

  constructor(mainWindow: BrowserWindow) {
    this.mainWindow = mainWindow
  }

  // ログ配信先を追加する（サーバログタブの WebContents 用）。
  addTarget(wc: WebContents): void {
    this.extraTargets.add(wc)
  }

  // ログ配信先を解除する（タブを閉じた/破棄した時）。
  removeTarget(wc: WebContents): void {
    this.extraTargets.delete(wc)
  }

  attachTo(serverProcess: ServerProcess): void {
    serverProcess.onLog((line, stream) => this.handle(line, stream))
  }

  private handle(line: string, stream: LogStreamKind): void {
    const entry = this.parse(line, stream)
    this.push(entry)
    if (this.mainWindow && !this.mainWindow.isDestroyed()) {
      this.mainWindow.webContents.send('log:line', entry)
    }
    // 追加ターゲット（ログタブ）にも配信。破棄済みは念のためスキップ。
    for (const wc of this.extraTargets) {
      if (!wc.isDestroyed()) wc.send('log:line', entry)
    }
  }

  private parse(line: string, stream: LogStreamKind): LogEntry {
    const m = LINE_RE.exec(line)
    let timestamp: string
    let module: string
    let body: string

    if (m && m.groups) {
      timestamp = m.groups['time']!
      module = m.groups['module']!
      body = m.groups['body'] ?? ''
    } else {
      timestamp = nowTime()
      module = 'raw'
      body = line
    }

    return {
      id: this.nextId++,
      timestamp,
      module,
      level: detectLevel(module, body, stream),
      body,
      raw: line,
      stream,
      receivedAt: Date.now()
    }
  }

  private push(entry: LogEntry): void {
    this.buffer.push(entry)
    if (this.buffer.length > MAX_BUFFER) {
      // 古いものから削除（リングバッファ）
      this.buffer.splice(0, this.buffer.length - MAX_BUFFER)
    }
  }

  getBuffer(): LogEntry[] {
    return this.buffer.slice()
  }

  clear(): void {
    this.buffer = []
  }
}

function nowTime(): string {
  const d = new Date()
  const pad = (n: number): string => n.toString().padStart(2, '0')
  return `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`
}

function detectLevel(module: string, body: string, stream: LogStreamKind): LogLevel {
  // 厳密な仕様は SPEC 参照。シンプルなヒューリスティック実装。
  if (/ERROR|Error|Exception|Traceback/.test(body)) return 'error'
  if (/WARN|Warning/.test(body)) return 'warn'
  if (module === 'DEBUG' || /^DEBUG/.test(body) || /^\[DEBUG\]/.test(body)) return 'debug'
  // stderr は uvicorn の INFO も流れてくるため、stderr=error 扱いはしない。
  if (stream === 'stderr' && /ERROR|CRITICAL/.test(body)) return 'error'
  return 'info'
}
