// Liner 用インライン i18n。config.yaml の language を読んで切り替える。

import * as fs from 'fs'
import { join } from 'path'

type Dict = Record<string, string>

const ja: Dict = {
  // メニュー
  menu_server: 'サーバー',
  menu_server_start: 'サーバーを起動',
  menu_server_stop: 'サーバーを停止',
  menu_server_restart: 'サーバーを再起動',
  // ダイアログ
  confirm_cancel: 'キャンセル',
  confirm_title: 'サーバーを{action}',
  confirm_message: 'サーバーを{action}しますか？',
  confirm_detail: '処理中のエージェントの応答が中断される場合があります。',
  error_server_start: 'サーバー起動エラー',
  error_server_stop: 'サーバー停止エラー',
  error_server_restart: 'サーバー再起動エラー',
  error_startup: 'Crescent Liner 起動エラー',
  about_version: 'バージョン',
  about_detail: 'Crescent Grove 専用クライアント',
  dialog_detect_title: '既存のサーバを検出しました',
  dialog_detect_message: 'port {port} で Crescent Grove が既に動作しています。',
  dialog_detect_detail: '既存のサーバに接続しますか？',
  dialog_detect_connect: '既存サーバに接続',
  splash_checking: '既存サーバを確認中...',
  splash_starting: 'server.py を起動中...',
  splash_health: 'ヘルスチェック中...',
  splash_connect: '既存サーバへ接続中...',
  splash_shell: 'Shell UI を初期化中...',
  error_port_used: 'port {port} が別のアプリケーションによって使用されています。\n該当アプリを終了してから再度起動してください。',
  error_python_not_found: 'Python (venv) が見つかりません:\n{path}\n\nagent ルートに venv を作成してください。',
  error_server_script: 'server.py が見つかりません:\n{path}',
  error_server_not_init: 'サーバー管理が初期化されていません',
  action_stop: '停止',
  action_restart: '再起動',
  // コンテキストメニュー
  ctx_undo: '元に戻す',
  ctx_redo: 'やり直し',
  ctx_cut: '切り取り',
  ctx_copy: 'コピー',
  ctx_paste: '貼り付け',
  ctx_select_all: 'すべて選択',
  ctx_copy_link: 'リンクのアドレスをコピー',
  ctx_copy_image: '画像をコピー',
  ctx_save_image: '画像を名前を付けて保存...',
  // Shell UI (renderer)
  tab_close: 'タブを閉じる',
  nav_reload: '再読込',
  nav_hard_reload: '強制再読込',
  nav_find: '検索',
  nav_reload_tooltip: '再読込 (Ctrl+R)',
  nav_hard_reload_tooltip: '強制再読込 (Ctrl+Shift+R)',
  nav_find_tooltip: 'ページ内検索 (Ctrl+F)',
  server_status_tooltip: 'サーバー状態',
  server_start: '起動',
  server_stop: '停止',
  server_restart: '再起動',
  server_start_tooltip: 'サーバーを起動',
  server_stop_tooltip: 'サーバーを停止',
  server_restart_tooltip: 'サーバーを再起動',
  find_placeholder: 'ページを検索',
  find_prev_tooltip: '前へ (Shift+Enter)',
  find_next_tooltip: '次へ (Enter)',
  find_highlight_all: 'ハイライトすべて',
  find_case_sensitive: '大文字小文字を区別',
  find_close_tooltip: '閉じる (Esc)',
  log_collapse_tooltip: '折りたたみ／展開 (Ctrl+L)',
  log_clear_tooltip: 'クリア',
  status_stopped: '停止中',
  status_starting: '起動中…',
  status_running: '稼働中',
  status_crashed: '異常終了',
  confirm_stop_prompt: 'サーバーを停止しますか？\n処理中のエージェントの応答が中断される場合があります。',
  confirm_restart_prompt: 'サーバーを再起動しますか？\n処理中のエージェントの応答が中断される場合があります。',
}

const en: Dict = {
  menu_server: 'Server',
  menu_server_start: 'Start Server',
  menu_server_stop: 'Stop Server',
  menu_server_restart: 'Restart Server',
  confirm_cancel: 'Cancel',
  confirm_title: '{action} Server',
  confirm_message: '{action} the server?',
  confirm_detail: 'Running agent responses may be interrupted.',
  error_server_start: 'Server Start Error',
  error_server_stop: 'Server Stop Error',
  error_server_restart: 'Server Restart Error',
  error_startup: 'Crescent Liner Startup Error',
  about_version: 'Version',
  about_detail: 'Client for Crescent Grove',
  dialog_detect_title: 'Existing server detected',
  dialog_detect_message: 'Crescent Grove is already running on port {port}.',
  dialog_detect_detail: 'Connect to the existing server?',
  dialog_detect_connect: 'Connect to existing',
  splash_checking: 'Checking for existing server...',
  splash_starting: 'Starting server.py...',
  splash_health: 'Health check...',
  splash_connect: 'Connecting to existing server...',
  splash_shell: 'Initializing Shell UI...',
  error_port_used: 'Port {port} is in use by another application.\nPlease close that application and try again.',
  error_python_not_found: 'Python (venv) not found:\n{path}\n\nPlease create a venv in the agent root.',
  error_server_script: 'server.py not found:\n{path}',
  error_server_not_init: 'Server management not initialized',
  action_stop: 'Stop',
  action_restart: 'Restart',
  ctx_undo: 'Undo',
  ctx_redo: 'Redo',
  ctx_cut: 'Cut',
  ctx_copy: 'Copy',
  ctx_paste: 'Paste',
  ctx_select_all: 'Select All',
  ctx_copy_link: 'Copy Link Address',
  ctx_copy_image: 'Copy Image',
  ctx_save_image: 'Save Image As...',
  tab_close: 'Close Tab',
  nav_reload: 'Reload',
  nav_hard_reload: 'Hard Reload',
  nav_find: 'Find',
  nav_reload_tooltip: 'Reload (Ctrl+R)',
  nav_hard_reload_tooltip: 'Hard Reload (Ctrl+Shift+R)',
  nav_find_tooltip: 'Find in Page (Ctrl+F)',
  server_status_tooltip: 'Server Status',
  server_start: 'Start',
  server_stop: 'Stop',
  server_restart: 'Restart',
  server_start_tooltip: 'Start Server',
  server_stop_tooltip: 'Stop Server',
  server_restart_tooltip: 'Restart Server',
  find_placeholder: 'Find in page',
  find_prev_tooltip: 'Previous (Shift+Enter)',
  find_next_tooltip: 'Next (Enter)',
  find_highlight_all: 'Highlight All',
  find_case_sensitive: 'Match Case',
  find_close_tooltip: 'Close (Esc)',
  log_collapse_tooltip: 'Collapse / Expand (Ctrl+L)',
  log_clear_tooltip: 'Clear',
  status_stopped: 'Stopped',
  status_starting: 'Starting...',
  status_running: 'Running',
  status_crashed: 'Crashed',
  confirm_stop_prompt: 'Stop the server?\nRunning agent responses may be interrupted.',
  confirm_restart_prompt: 'Restart the server?\nRunning agent responses may be interrupted.',
}

const dicts: Record<string, Dict> = { ja, en }
let current: Dict = ja

// config.yaml（ベース）→ settings.json（上書き）の順で言語コードだけを解決する。
// サーバー本体と同じく settings.json が config.yaml を上書きする。
export function resolveLinerLang(dataRoot: string | undefined, projectRoot: string): string {
  let lang = 'ja'
  const roots = dataRoot ? [dataRoot, projectRoot] : [projectRoot]

  // 1) config.yaml の language をベース値として読む。
  for (const root of roots) {
    try {
      const text = fs.readFileSync(join(root, 'config.yaml'), 'utf-8')
      const m = text.match(/^[ \t]*language:[ \t]*["']?(\w+)["']?/m)
      if (m) { lang = m[1]; break }
    } catch { /* 未生成 */ }
  }

  // 2) settings.json の language で上書きする。
  for (const root of roots) {
    try {
      const text = fs.readFileSync(join(root, 'settings.json'), 'utf-8')
      const obj = JSON.parse(text) as { language?: unknown }
      if (typeof obj.language === 'string' && obj.language) {
        lang = obj.language
        break
      }
    } catch { /* 未生成／パース失敗は無視 */ }
  }

  return lang
}

export function initLinerI18n(dataRoot: string | undefined, projectRoot: string): void {
  const lang = resolveLinerLang(dataRoot, projectRoot)
  current = dicts[lang] || ja
  console.log('[i18n] Liner language =', lang)
}

// 初回起動の言語選択ダイアログ結果を即座に反映するための上書き経路。
// config.yaml がまだ無い初回起動では initLinerI18n が ja でフォールバックするため、
// ダイアログで選んだ言語をスプラッシュ／メニュー／コンテキストメニューに即時適用する。
export function setLinerLang(lang: string): void {
  current = dicts[lang] || ja
  console.log('[i18n] Liner language switched to =', lang)
}

export function lt(key: string): string {
  return current[key] || ja[key] || key
}

export function getLinerDict(): Dict {
  return { ...current }
}
