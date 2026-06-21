import { Menu, clipboard, type BrowserWindow, type WebContents } from 'electron'
import type { MenuItemConstructorOptions } from 'electron'
import { lt } from './i18n'

// WebContentsView（各タブの中身）向けの右クリックコンテキストメニュー。
// Shell renderer 本体ではなく、ダッシュボード等を表示する WebContentsView に対して付与する。
//
// 設計方針:
//   - Electron 標準の 'context-menu' イベントを使い、params に応じて項目を動的構築する。
//   - 編集系（Undo/Redo/Cut/Copy/Paste/SelectAll）は editFlags で有効/無効を切り替える。
//   - accelerator は「見た目の併記」のみ。実際のショートカット動作は WebContentsView の
//     標準処理に任せるため、ここでは登録しない（registerAccelerator: false）。
//   - 既存の before-input-event / アプリメニュー accelerator / findInPage 等には一切干渉しない
//     （このイベントは右クリック時のみ発火し、何も奪わない追加機能）。

// 指定 WebContents に右クリックメニューを有効化する。
// mainWindow は popup の親（Shell の BrowserWindow）として渡す。
export function installContextMenu(wc: WebContents, mainWindow: BrowserWindow): void {
  wc.on('context-menu', (_event, params) => {
    const flags = params.editFlags
    const template: MenuItemConstructorOptions[] = [
      {
        label: lt('ctx_undo'),
        accelerator: 'CmdOrCtrl+Z',
        enabled: flags.canUndo,
        registerAccelerator: false,
        click: () => wc.undo()
      },
      {
        label: lt('ctx_redo'),
        accelerator: 'CmdOrCtrl+Y',
        enabled: flags.canRedo,
        registerAccelerator: false,
        click: () => wc.redo()
      },
      { type: 'separator' },
      {
        label: lt('ctx_cut'),
        accelerator: 'CmdOrCtrl+X',
        enabled: flags.canCut,
        registerAccelerator: false,
        click: () => wc.cut()
      },
      {
        label: lt('ctx_copy'),
        accelerator: 'CmdOrCtrl+C',
        enabled: flags.canCopy,
        registerAccelerator: false,
        click: () => wc.copy()
      },
      {
        label: lt('ctx_paste'),
        accelerator: 'CmdOrCtrl+V',
        enabled: flags.canPaste,
        registerAccelerator: false,
        click: () => wc.paste()
      },
      {
        label: lt('ctx_select_all'),
        accelerator: 'CmdOrCtrl+A',
        enabled: flags.canSelectAll,
        registerAccelerator: false,
        click: () => wc.selectAll()
      }
    ]

    // リンク上で右クリックした場合は「リンクのアドレスをコピー」を追加
    if (params.linkURL) {
      template.push(
        { type: 'separator' },
        {
          label: lt('ctx_copy_link'),
          click: () => clipboard.writeText(params.linkURL)
        }
      )
    }

    // 画像上で右クリックした場合は画像系の項目を追加
    if (params.hasImageContents && params.mediaType === 'image') {
      template.push(
        { type: 'separator' },
        {
          label: lt('ctx_copy_image'),
          click: () => wc.copyImageAt(params.x, params.y)
        },
        {
          label: lt('ctx_save_image'),
          // 保存先選択は Electron 標準のダウンロード UI に任せる
          click: () => wc.downloadURL(params.srcURL)
        }
      )
    }

    const menu = Menu.buildFromTemplate(template)
    menu.popup({ window: mainWindow })
  })
}
