import { defineConfig, externalizeDepsPlugin } from 'electron-vite'
import { resolve } from 'path'

// Crescent Liner の electron-vite 設定
// main / preload / renderer の3エントリ構成
export default defineConfig({
  main: {
    plugins: [externalizeDepsPlugin()],
    build: {
      outDir: 'out/main'
    }
  },
  preload: {
    plugins: [externalizeDepsPlugin()],
    build: {
      outDir: 'out/preload',
      rollupOptions: {
        input: {
          index: resolve(__dirname, 'src/preload/index.ts'),
          'webview-preload': resolve(__dirname, 'src/preload/webview-preload.ts'),
          'log-tab-preload': resolve(__dirname, 'src/preload/log-tab-preload.ts')
        }
      }
    }
  },
  renderer: {
    root: resolve(__dirname, 'src/renderer'),
    // ~/colors.json で liner/ ルート配下のファイルを参照できるようにする
    resolve: {
      alias: {
        '~': resolve(__dirname)
      }
    },
    build: {
      outDir: 'out/renderer',
      rollupOptions: {
        input: {
          index: resolve(__dirname, 'src/renderer/index.html'),
          'log-tab': resolve(__dirname, 'src/renderer/log-tab.html')
        }
      }
    }
  }
})
