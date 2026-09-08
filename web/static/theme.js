// 共通テーマ管理モジュール（dark / light / moonlit / moonlit2 / journal の5テーマ）
// 全ページ共通。<head> で読み込むことで、本文描画前にテーマを確定させる。
// テーマボタンをクリックすると選択メニューが開き、一覧から直接選べる。
// moonlit（クリーム手帳）/ moonlit2（月夜の森）/ journal（スクラップブック手帳）は
// それぞれコンセプトの異なる正式テーマとして共存させる。
(function () {
    // 表示順・アイコン（icons ディレクトリの SVG ファイル名）・表示名を一元管理
    const THEMES = [
        { id: 'dark', icon: 'moon', label: T.theme_dark || 'Dark' },
        { id: 'light', icon: 'sun', label: T.theme_light || 'Light' },
        { id: 'moonlit', icon: 'moon-star', label: 'Moonlit Grove' },
        { id: 'moonlit2', icon: 'moon-sparkle', label: 'Moonlit Grove Ⅱ' },
        { id: 'journal', icon: 'notebook', label: 'Grove Journal' },
    ];
    const STORAGE_KEY = 'yuzuki_theme';

    function themeOf(id) {
        return THEMES.find((t) => t.id === id);
    }

    // 未知・旧値は dark にフォールバック
    function normalize(theme) {
        return themeOf(theme) ? theme : 'dark';
    }

    // data-theme は <html>（documentElement）に付与する。
    // moonlit の背景グラデや星屑は `[data-theme="moonlit"] body` を前提にしており、
    // body 自身ではなく body の祖先（=html）に属性が必要なため。
    function applyToRoot(theme) {
        const root = document.documentElement;
        if (theme === 'dark') {
            root.removeAttribute('data-theme');
        } else {
            root.setAttribute('data-theme', theme);
        }
    }

    // ボタンの見た目（アイコン・ツールチップ）を反映
    function updateButton(theme) {
        const btn = document.getElementById('themeToggle');
        if (!btn) return;
        const t = themeOf(theme) || THEMES[0];
        btn.innerHTML = `<img class="ui-icon ui-icon-only" src="/static/images/icons/${t.icon}.svg" alt="">`;
        btn.title = (T.theme_tooltip || '').replace('{name}', t.label) || t.label;
    }

    // テーマを確定・保存・反映する
    function applyTheme(theme) {
        const t = normalize(theme);
        applyToRoot(t);
        updateButton(t);
        updateMenuActive(t);
        localStorage.setItem(STORAGE_KEY, t);
        return t;
    }

    // ===== テーマ選択メニュー =====
    let menuEl = null; // 開いている間だけ存在

    // メニュー用スタイル（全ページ共通にするため JS から注入する）
    function injectStyles() {
        if (document.getElementById('themeMenuStyle')) return;
        const style = document.createElement('style');
        style.id = 'themeMenuStyle';
        style.textContent = `
.theme-menu {
    position: fixed;
    z-index: 10000;
    min-width: 190px;
    padding: 6px;
    background: var(--panel-bg, #1e2a45);
    border: 1px solid var(--border-color, #2a3550);
    border-radius: 10px;
    box-shadow: 0 6px 24px var(--shadow-color, rgba(0, 0, 0, 0.4));
    font-family: var(--font-ui, sans-serif);
}
.theme-menu-item {
    display: flex;
    align-items: center;
    gap: 0.55em;
    width: 100%;
    padding: 7px 10px;
    border: none;
    background: none;
    border-radius: 7px;
    color: var(--text-primary, #e0e0e0);
    font-size: 13px;
    line-height: 1.4;
    cursor: pointer;
    text-align: left;
}
.theme-menu-item:hover {
    background: var(--bg-hover, #2a3550);
}
.theme-menu-item.active {
    background: var(--accent-dim, rgba(124, 77, 255, 0.15));
    color: var(--accent, #7c4dff);
    font-weight: 600;
}
.theme-menu-item.active::after {
    content: "✓";
    margin-left: auto;
    padding-left: 0.6em;
}
.theme-menu-item .ui-icon {
    margin-right: 0;
}
`;
        document.head.appendChild(style);
    }

    // 開いているメニューの選択中ハイライトを更新
    function updateMenuActive(theme) {
        if (!menuEl) return;
        menuEl.querySelectorAll('.theme-menu-item').forEach((item) => {
            item.classList.toggle('active', item.dataset.theme === theme);
        });
    }

    function closeMenu() {
        if (!menuEl) return;
        menuEl.remove();
        menuEl = null;
        document.removeEventListener('mousedown', onOutsidePress, true);
        document.removeEventListener('keydown', onMenuKeydown, true);
    }

    function onOutsidePress(e) {
        const btn = document.getElementById('themeToggle');
        if (menuEl && !menuEl.contains(e.target) && e.target !== btn && !(btn && btn.contains(e.target))) {
            closeMenu();
        }
    }

    function onMenuKeydown(e) {
        if (e.key === 'Escape') closeMenu();
    }

    function openMenu(btn) {
        injectStyles();
        const current = normalize(localStorage.getItem(STORAGE_KEY));

        menuEl = document.createElement('div');
        menuEl.className = 'theme-menu';
        menuEl.setAttribute('role', 'menu');
        for (const t of THEMES) {
            const item = document.createElement('button');
            item.type = 'button';
            item.className = 'theme-menu-item' + (t.id === current ? ' active' : '');
            item.dataset.theme = t.id;
            item.setAttribute('role', 'menuitem');
            item.innerHTML = `<img class="ui-icon" src="/static/images/icons/${t.icon}.svg" alt="">${t.label}`;
            item.addEventListener('click', () => {
                applyTheme(t.id);
                closeMenu();
            });
            menuEl.appendChild(item);
        }
        document.body.appendChild(menuEl);

        // ボタンの直下・右端揃えで表示（画面外にはみ出す場合は左端揃えへ）
        const rect = btn.getBoundingClientRect();
        menuEl.style.top = `${rect.bottom + 6}px`;
        const right = window.innerWidth - rect.right;
        if (right + menuEl.offsetWidth > window.innerWidth) {
            menuEl.style.left = `${Math.max(8, rect.left)}px`;
        } else {
            menuEl.style.right = `${right}px`;
        }

        document.addEventListener('mousedown', onOutsidePress, true);
        document.addEventListener('keydown', onMenuKeydown, true);
    }

    // 1) 描画前の即時復元（head 実行時点で documentElement は存在する）
    const initial = normalize(localStorage.getItem(STORAGE_KEY));
    applyToRoot(initial);

    // 2) DOM 構築後にボタンへアイコン反映＋クリック束縛
    function initButton() {
        updateButton(normalize(localStorage.getItem(STORAGE_KEY)));
        const btn = document.getElementById('themeToggle');
        if (!btn) return; // トグルが無いページ（debug 等）は復元のみ
        btn.setAttribute('aria-haspopup', 'menu');
        btn.addEventListener('click', () => {
            if (menuEl) {
                closeMenu();
            } else {
                openMenu(btn);
            }
        });
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', initButton);
    } else {
        initButton();
    }

    // 他スクリプトからの利用用に最小限を公開
    window.YuzukiTheme = {
        apply: applyTheme,
        current: () => normalize(localStorage.getItem(STORAGE_KEY))
    };
})();
