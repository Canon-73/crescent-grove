import { spawn, ChildProcess, execFile } from 'child_process'
import { createInterface } from 'readline'
import * as http from 'http'
import * as fs from 'fs'
import { EventEmitter } from 'events'

// Crescent Grove の server.py を venv の python.exe で起動・監視・停止する。
// Phase 2 ではタブもログペインも触らないが、onLog のフックは Phase 3 で使えるよう用意する。

export type ServerStatus = 'stopped' | 'starting' | 'running' | 'crashed'
export type LogStream = 'stdout' | 'stderr'
export type LogCallback = (line: string, stream: LogStream) => void
export type StatusCallback = (status: ServerStatus) => void

export interface ServerProcessOptions {
  pythonPath: string       // venv\Scripts\python.exe（dev）/ runtime\python.exe（配布版）へのフルパス
  serverScript: string     // server.py へのフルパス
  cwd: string              // agent ルート
  port: number             // 8080
  healthCheckPath: string  // '/api/memory'
  startupTimeoutMs: number // 180000（初回起動の重さ対応。ポーリング間隔は500ms維持）
  // data-root のフルパス（配布版のみ指定）。指定時は server.py に
  // --data-root として渡す。未指定（dev）なら従来通り agent ルート基準で動く。
  dataRoot?: string
  // 初回ブートストラップで展開する雛形の言語（"ja" / "en"）。配布版の初回起動時のみ
  // 意味を持つ（2回目以降は data-root に既存があるので server.py 側で実質無視される）。
  // 指定時は server.py に --init-lang として渡す。
  initLang?: string
}

export class ServerProcess extends EventEmitter {
  private opts: ServerProcessOptions
  private child: ChildProcess | null = null
  private _status: ServerStatus = 'stopped'
  // stop() による意図的な終了かどうか。exit イベントが「クラッシュ」か「正常停止」かを
  // 区別するために使う（手動停止を crashed と誤判定しないため）。
  private intentionalStop = false
  private logCallbacks: LogCallback[] = []
  private statusCallbacks: StatusCallback[] = []

  constructor(opts: ServerProcessOptions) {
    super()
    this.opts = opts
  }

  get status(): ServerStatus {
    return this._status
  }

  onLog(cb: LogCallback): void {
    this.logCallbacks.push(cb)
  }

  onStatusChange(cb: StatusCallback): void {
    this.statusCallbacks.push(cb)
  }

  private setStatus(s: ServerStatus): void {
    if (this._status === s) return
    this._status = s
    for (const cb of this.statusCallbacks) {
      try {
        cb(s)
      } catch (e) {
        console.error('[server-process] status callback error:', e)
      }
    }
  }

  private emitLog(line: string, stream: LogStream): void {
    for (const cb of this.logCallbacks) {
      try {
        cb(line, stream)
      } catch (e) {
        console.error('[server-process] log callback error:', e)
      }
    }
  }

  // 既に LISTEN している外部サーバに接続するだけのモード（spawn しない）
  async attachToExisting(): Promise<void> {
    this.setStatus('starting')
    await this.waitHealthy()
    this.setStatus('running')
  }

  async start(): Promise<void> {
    if (this._status === 'running' || this._status === 'starting') {
      return
    }

    // 事前チェック
    if (!fs.existsSync(this.opts.pythonPath)) {
      throw new Error(`Python が見つかりません: ${this.opts.pythonPath}`)
    }
    if (!fs.existsSync(this.opts.serverScript)) {
      throw new Error(`server.py が見つかりません: ${this.opts.serverScript}`)
    }

    this.setStatus('starting')
    // 新規 spawn なので意図的停止フラグをリセットする
    this.intentionalStop = false

    // spawn
    // dataRoot 指定時（配布版）は --data-root を渡し、未指定（dev）なら従来通り
    // server.py のみを渡す（agent ルート基準で動く）。
    const args = [
      this.opts.serverScript,
      ...(this.opts.dataRoot ? ['--data-root', this.opts.dataRoot] : []),
      ...(this.opts.initLang ? ['--init-lang', this.opts.initLang] : [])
    ]
    // PYTHONIOENCODING=utf-8 で日本語ログ対策、PYTHONUNBUFFERED=1 で
    // バッファリングを切ってリアルタイム配信を可能にする
    this.child = spawn(this.opts.pythonPath, args, {
      cwd: this.opts.cwd,
      env: {
        ...process.env,
        PYTHONIOENCODING: 'utf-8',
        PYTHONUNBUFFERED: '1'
      },
      windowsHide: true
    })

    // stdout / stderr を行単位で受け取る
    if (this.child.stdout) {
      const rl = createInterface({ input: this.child.stdout })
      rl.on('line', (line) => this.emitLog(line, 'stdout'))
    }
    if (this.child.stderr) {
      const rl = createInterface({ input: this.child.stderr })
      rl.on('line', (line) => this.emitLog(line, 'stderr'))
    }

    // 早期終了監視: ヘルスチェック前に死んだら crashed
    let exited = false
    this.child.on('exit', (code, signal) => {
      exited = true
      this.emitLog(`[server-process] exited code=${code} signal=${signal}`, 'stderr')
      // 意図的な停止（stop() 経由）は正常停止扱い。そうでない予期せぬ終了のみ crashed。
      if (!this.intentionalStop && (this._status === 'starting' || this._status === 'running')) {
        this.setStatus('crashed')
      } else {
        this.setStatus('stopped')
      }
      this.child = null
    })
    this.child.on('error', (err) => {
      this.emitLog(`[server-process] spawn error: ${err.message}`, 'stderr')
    })

    // ヘルスチェック
    try {
      await this.waitHealthy(() => exited)
      this.setStatus('running')
    } catch (e) {
      // ヘルスチェック失敗 → プロセスをまだ生きているなら殺す
      await this.stop().catch(() => {})
      throw e
    }
  }

  // pollPrematureExit が true を返したら即エラーで打ち切る
  private async waitHealthy(pollPrematureExit?: () => boolean): Promise<void> {
    const deadline = Date.now() + this.opts.startupTimeoutMs
    const url = `http://127.0.0.1:${this.opts.port}${this.opts.healthCheckPath}`

    while (Date.now() < deadline) {
      if (pollPrematureExit && pollPrematureExit()) {
        throw new Error('server.py が起動中に終了しました')
      }
      const ok = await this.probe(url)
      if (ok) return
      await sleep(500)
    }
    throw new Error(`ヘルスチェックがタイムアウトしました (${this.opts.startupTimeoutMs}ms)`)
  }

  private probe(url: string): Promise<boolean> {
    return new Promise((resolve) => {
      const req = http.get(url, { timeout: 2000 }, (res) => {
        // server.py は /api/memory に対し 302 (認証リダイレクト) を返すケースがある。
        // 起動判定としては「HTTP 応答が返ってきた」ことが重要なので 2xx/3xx を成功扱いにする。
        res.resume()
        const code = res.statusCode ?? 0
        resolve(code > 0 && code < 400)
      })
      req.on('error', () => resolve(false))
      req.on('timeout', () => {
        req.destroy()
        resolve(false)
      })
    })
  }

  async stop(): Promise<void> {
    // これ以降に発生する exit は意図的な停止として扱う
    this.intentionalStop = true
    const child = this.child
    if (!child || child.pid == null) {
      this.setStatus('stopped')
      return
    }
    const pid = child.pid

    // Windows ではプロセスツリーを taskkill /F /T で確実に殺す
    await new Promise<void>((resolve) => {
      if (process.platform === 'win32') {
        execFile('taskkill', ['/F', '/T', '/PID', String(pid)], (err) => {
          if (err) {
            this.emitLog(`[server-process] taskkill 失敗: ${err.message}`, 'stderr')
          }
          resolve()
        })
      } else {
        try {
          child.kill('SIGTERM')
          setTimeout(() => {
            if (this.child) child.kill('SIGKILL')
            resolve()
          }, 5000)
        } catch {
          resolve()
        }
      }
    })

    // exit イベントで status=stopped に遷移する。念のため明示。
    this.child = null
    if (this._status !== 'crashed') {
      this.setStatus('stopped')
    }
  }

  async restart(): Promise<void> {
    await this.stop()
    await this.start()
  }
}

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms))
}
