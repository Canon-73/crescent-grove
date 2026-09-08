// settings_nav.js
// 設定ページ共通の縦サイドバーナビゲーション。
//
// 各設定ページは <div id="settingsNav"></div> を1つ置き、末尾でこのスクリプトを
// 読み込むだけでよい。ここで全設定ページのリンクを一元管理するため、タブを追加・
// 変更する際はこのファイルだけを編集すればよい（旧来は全HTMLにベタ書きで重複していた）。

(function () {
    "use strict";

    // 設定ページの定義。group ごとに見出しを付けて縦に並べる。
    const NAV_GROUPS = [
        {
            title: T.nav_group_language || "Language / 言語",
            items: [
                { href: "/settings/language", icon: "🌐", label: T.nav_language || "Language / 言語", restartKey: "cg_language_dirty" },
            ],
        },
        {
            title: T.nav_group_model || "Model",
            items: [
                { href: "/settings/llm", icon: "🧠", label: T.nav_llm || "LLM" },
            ],
        },
        {
            title: T.nav_group_env || "Environment",
            items: [
                { href: "/settings/security", icon: "🛡️", label: T.nav_security || "Security" },
                { href: "/settings/system-prompts", icon: "📜", label: T.nav_system_prompts || "System Prompts" },
                { href: "/settings/general", icon: "⚙️", label: T.nav_general || "General" },
                { href: "/settings/api-keys", icon: "🔑", label: T.nav_api_keys || "API Keys" },
            ],
        },
        {
            title: T.nav_group_external || "External",
            items: [
                { href: "/settings/openclaw", icon: "📡", label: "OpenClaw", restartKey: "cg_openclaw_dirty" },
            ],
        },
        {
            title: T.nav_group_behavior || "Behavior",
            items: [
                { href: "/settings/moonbeat", icon: "🌙", label: "Moonbeat" },
                { href: "/settings/mood", icon: "🎭", label: T.nav_mood || "Mood", restartKey: "cg_mood_dirty" },
                { href: "/settings/vital", icon: "💗", label: T.nav_vital || "Vital" },
                { href: "/settings/desire", icon: "🎯", label: T.nav_desire || "Desire" },
                { href: "/settings/compression", icon: "🗜️", label: T.nav_compression || "Compression" },
                { href: "/settings/self-memo", icon: "📝", label: "self_memo" },
                { href: "/settings/tips", icon: "💡", label: "Tips" },
            ],
        },
    ];

    function escapeHtml(s) {
        return String(s).replace(/[&<>"']/g, function (c) {
            return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
        });
    }

    function render() {
        const mount = document.getElementById("settingsNav");
        if (!mount) return;

        // 現在パス（末尾スラッシュを正規化）でアクティブ判定する。
        const current = location.pathname.replace(/\/+$/, "") || "/";

        let html = '<aside class="settings-sidebar"><div class="settings-sidebar-title">' + escapeHtml(T.settings_heading || 'Settings') + '</div>';
        for (const group of NAV_GROUPS) {
            html += '<div class="settings-nav-group">';
            html += '<div class="settings-nav-group-title">' + escapeHtml(group.title) + "</div>";
            for (const item of group.items) {
                const active = item.href.replace(/\/+$/, "") === current ? " active" : "";
                // 再起動待ちフラグが立っていればバッジを付ける。
                let badge = "";
                if (item.restartKey) {
                    try {
                        if (localStorage.getItem(item.restartKey) === "1") {
                            badge = '<span class="settings-side-badge" title="' + escapeHtml(T.nav_badge_restart || 'Saved — restart required') + '">⚠️</span>';
                        }
                    } catch (e) { /* localStorage 不可環境は無視 */ }
                }
                html +=
                    '<a class="settings-side-link' + active + '" href="' + escapeHtml(item.href) + '">' +
                    '<span class="settings-side-icon">' + item.icon + "</span>" +
                    '<span class="settings-side-label">' + escapeHtml(item.label) + "</span>" +
                    badge +
                    "</a>";
            }
            html += "</div>";
        }
        html += "</aside>";

        mount.innerHTML = html;
        document.body.classList.add("has-settings-sidebar");
    }

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", render);
    } else {
        render();
    }

    // --- 保存ボタンの即時フィードバック ---
    // 設定フォームの保存は非同期で、完了通知（トースト）が出るまで一瞬間がある。
    // その間ボタンが無反応に見えて不安なので、押した瞬間にボタンを
    // 「保存中…」へ切り替えて反応を返す。各ページの保存処理には手を入れず、
    // 全設定ページ共通でここだけで面倒を見る。
    function flashSaving(btn) {
        if (!btn || btn.dataset.saving === "1") return;
        const original = btn.innerHTML;
        btn.dataset.saving = "1";
        btn.disabled = true;
        btn.classList.add("is-saving");
        btn.innerHTML = T.nav_saving || "Saving…";
        // 完了フックを汎用に取れないため、短時間で元に戻す。
        // 実際の成否はトースト通知側が伝える。
        setTimeout(function () {
            btn.innerHTML = original;
            btn.disabled = false;
            btn.classList.remove("is-saving");
            delete btn.dataset.saving;
        }, 1200);
    }

    // submit 時に送信ボタン（submitter）へフィードバックを付ける。
    document.addEventListener("submit", function (ev) {
        const form = ev.target;
        if (!form || form.tagName !== "FORM") return;
        const btn =
            ev.submitter ||
            form.querySelector('button[type="submit"], input[type="submit"]');
        flashSaving(btn);
    }, true);

    // 他ページからバッジ状態を更新できるよう、簡易ヘルパーを公開する。
    window.settingsNav = {
        setRestartPending: function (key, pending) {
            try {
                if (pending) localStorage.setItem(key, "1");
                else localStorage.removeItem(key);
            } catch (e) { /* 無視 */ }
            render();
        },
    };
})();
