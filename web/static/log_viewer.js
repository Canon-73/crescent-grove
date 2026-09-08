// web/static/log_viewer.js
// 過去ログビューワー: workspace/logs/full/*.jsonl をチャットUI風に表示する読み取り専用ビュー。

const chatMessages = document.getElementById('chatMessages');
const dateSelect = document.getElementById('dateSelect');
const modeSelect = document.getElementById('modeSelect');
const loadStatus = document.getElementById('loadStatus');
const searchBox = document.getElementById('searchBox');
const searchInput = document.getElementById('searchInput');
const searchBtn = document.getElementById('searchBtn');

let userProfile = { name: 'User', avatar: '' };
let agentProfile = { name: 'Assistant', avatar: '' };

// 表示モード: 'dashboard'（ダッシュボードと同じ表示）/ 'detail'（全ログ）。localStorage に保存。
let viewMode = localStorage.getItem('yuzuki_logviewer_mode') || 'dashboard';
// 直近に読み込んだエントリ。モード切替時に再フェッチせず再描画するため保持する。
let currentEntries = [];

function escapeHtml(text) {
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
}

async function loadProfile() {
    try {
        const res = await fetch('/api/config');
        if (res.ok) {
            const data = await res.json();
            if (data.profile) {
                if (data.profile.user) userProfile = data.profile.user;
                if (data.profile.agent) agentProfile = data.profile.agent;
                document.title = (T.logviewer_title || '{name} Log Viewer').replace('{name}', agentProfile.name);
            }
        }
    } catch (e) {
        console.warn('プロファイル読み込み失敗:', e);
    }
}

function clearChat() {
    chatMessages.innerHTML = '';
}

// ダッシュボードの addChatMessage を簡略化したもの（読み取り専用、保存しない、画像なし）。
// headerOnly=true のときはアバター＋名前ヘッダーのみを描画し、発言バブルは出さない
// （ツール行の前にアシスタントのアバターを先出しするために使う）。
function renderMessage(role, content, timeStr, headerOnly = false) {
    const profile = role === 'user' ? userProfile : agentProfile;
    const layout = profile.layout || (role === 'user' ? 'horizontal' : 'vertical');

    const block = document.createElement('div');
    block.className = `chat-block ${role} ${layout}`;
    // 検索結果からのジャンプ用に時刻・ロールを埋め込む
    if (timeStr) block.dataset.time = timeStr;
    block.dataset.role = role;  // 'user' / 'assistant'

    const createAvatar = () => {
        const avatar = document.createElement('div');
        avatar.className = 'avatar';
        if (profile.size) {
            avatar.style.width = profile.size;
            avatar.style.height = profile.size;
        }
        if (profile.avatar) {
            const img = document.createElement('img');
            img.src = profile.avatar;
            img.onerror = () => { img.style.display = 'none'; avatar.textContent = profile.name[0]; };
            avatar.appendChild(img);
        } else {
            avatar.textContent = profile.name ? profile.name[0] : (role === 'user' ? 'U' : 'A');
        }
        return avatar;
    };

    const createName = () => {
        const nameContainer = document.createElement('div');
        nameContainer.style.display = 'flex';
        nameContainer.style.alignItems = 'baseline';
        nameContainer.style.gap = '8px';

        const name = document.createElement('span');
        name.className = 'sender-name';
        name.textContent = profile.name;

        const timeNode = document.createElement('span');
        timeNode.className = 'message-time';
        timeNode.style.fontSize = '11px';
        timeNode.style.color = 'var(--text-muted, #999)';
        timeNode.textContent = timeStr || '';

        nameContainer.appendChild(name);
        nameContainer.appendChild(timeNode);
        return nameContainer;
    };

    const msg = document.createElement('div');
    msg.className = `message ${role}`;
    msg.textContent = content;

    if (layout === 'vertical') {
        const header = document.createElement('div');
        header.className = 'chat-header';
        header.appendChild(createAvatar());
        header.appendChild(createName());
        block.appendChild(header);
        if (!headerOnly) block.appendChild(msg);
    } else {
        block.appendChild(createAvatar());
        const body = document.createElement('div');
        body.className = 'chat-body';
        body.appendChild(createName());
        if (!headerOnly) body.appendChild(msg);
        block.appendChild(body);
    }

    // ダッシュボードと同じく、直前のブロックが同じ話者なら
    // アバター＋名前ヘッダーを隠して1ターン内の連続発言をまとめる。
    // （moonbeatマーカー等の非発言ブロックは role クラスを持たないため畳まれない）
    let prevBlock = chatMessages.lastElementChild;
    const directPrev = prevBlock;
    while (prevBlock && !prevBlock.classList.contains('chat-block')) {
        prevBlock = prevBlock.previousElementSibling;
    }
    if (prevBlock && prevBlock.classList.contains(role)) {
        const header = block.querySelector('.chat-header');
        if (header) header.style.display = 'none';
        // 詰めるのは吹き出し同士が直接隣り合うときだけ（ダッシュボードと同じ理由）
        if (prevBlock === directPrev) block.style.marginTop = '-8px';
    }

    chatMessages.appendChild(block);
}

function renderLog(type, text, timeStr, role) {
    const entry = document.createElement('div');
    entry.className = `log-entry ${type}`;
    entry.textContent = text;
    // 検索結果からのジャンプ用に時刻・ロールを埋め込む
    if (timeStr) entry.dataset.time = timeStr;
    if (role) entry.dataset.role = role;  // 'tool_call' / 'tool'
    chatMessages.appendChild(entry);
}

// ツール行を差し込む前に、直近の chat-block がアシスタントでなければ
// アバター＋名前のみのヘッダーを先に挿入する（ダッシュボードの ensureAssistantHeader と同等）。
// これにより「ターン冒頭が無言ツール」のときも🔧行がアバターより先に出ない。
function ensureAssistantHeader(timeStr) {
    let prev = chatMessages.lastElementChild;
    while (prev && !prev.classList.contains('chat-block')) {
        prev = prev.previousElementSibling;
    }
    if (prev && prev.classList.contains('assistant')) return;
    renderMessage('assistant', '', timeStr || '', true);
}

function toolDetail(args) {
    if (!args || typeof args !== 'object') return '';
    if (args.path) return ` → ${args.path}`;
    if (args.directory !== undefined) return ` → ${args.directory || '/'}`;
    if (args.query) return ` → "${args.query}"`;
    if (args.url) return ` → ${args.url}`;
    if (args.app_name) return ` → ${args.app_name}`;
    if (args.source) return ` → ${args.source}`;
    return '';
}

// ダッシュボードと同じ「🌙 [Moonbeat]」風の中央寄せピル型マーカーを描画する。
function renderMarker(text) {
    const marker = document.createElement('div');
    marker.className = 'chat-block moonbeat-marker';
    const label = document.createElement('span');
    label.className = 'moonbeat-label';
    label.textContent = text;
    marker.appendChild(label);
    chatMessages.appendChild(marker);
}

// ユーザー発言の末尾に付与される文脈断片（flashback/note_fragment/tips）を取り除く。
// ダッシュボードでは <user_message> の中身だけを表示するのに合わせ、実発言部分のみ残す。
function stripContextFragments(content) {
    return String(content || '')
        .replace(/\n*\s*<(flashback|note_fragment|tips)\b[\s\S]*$/i, '')
        .trim();
}

// full ログの user_message content を分類する。
// ダッシュボードと同じく、Moonbeat・スケジュール・OpenClaw はマーカーに、
// システム注入（[SYSTEM] / 【システム…】）は非表示にし、それ以外を実ユーザー発言とする。
function classifyUserContent(content) {
    const raw = String(content || '');
    const c = raw.replace(/^\s+/, '');
    // 末尾の文脈断片を除いた本体（旧フォーマット判定に使う）
    const head = stripContextFragments(raw);
    // Moonbeat検出は文面フォーマットが時期で変化しているため複数形式に対応する:
    //   現行 : "[Moonbeat] …" プレフィックス
    //   中期 : "自由時間です。"（moonbeat_messages.json 未ロード時のフォールバック文字列）
    //   最初期: "［今は柚月の自由時間です。ご主人様は不在です…］" 形式のディレクティブ
    //          （"ご主人様は不在" はユーザーが打つことはまずないため誤検出しない）
    if (c.startsWith('[Moonbeat]') || head === '自由時間です。' || c.includes('ご主人様は不在')) {
        return { kind: 'marker', text: '🌙 [Moonbeat]' };
    }
    if (c.startsWith('【スケジュールタスク自動実行】')) {
        const m = c.match(/タスク名:\s*([^\n]+)/);
        const name = m ? m[1].trim() : '?';
        return { kind: 'marker', text: `📅 [${name}] ${T.log_schedule_exec || 'Schedule'}` };
    }
    const cm = c.match(/\[city_event:([^\]]+)\]\s*([^:\n]+?):/);
    if (cm) {
        return { kind: 'marker', text: `🌐 [OpenClaw] ${cm[1].trim()} from ${cm[2].trim()}` };
    }
    if (c.startsWith('[SYSTEM]') || c.startsWith('【システム')) {
        return { kind: 'hidden' };
    }
    return { kind: 'user', text: stripContextFragments(content) };
}

// 詳細モード: full ログの全エントリ（ツール・使用量含む）をそのまま描画する。
function renderEntriesDetail(entries) {
    for (const e of entries) {
        try {
            if (e.type === 'message') {
                renderMessage(e.role, e.content || '', e.time || '');
            } else if (e.type === 'tool_call') {
                ensureAssistantHeader(e.time || '');
                // 詳細モードでは引数の全文を JSON で表示する（🔧tool({...全引数...})）
                let argStr = '';
                try { argStr = e.arguments ? JSON.stringify(e.arguments) : ''; } catch (_) { argStr = ''; }
                renderLog('tool-call', `🔧${e.tool || '?'}(${argStr})`, e.time || '', 'tool_call');
            } else if (e.type === 'tool_result') {
                ensureAssistantHeader(e.time || '');
                const txt = e.content || '';
                const truncated = txt.length > 300 ? txt.substring(0, 300) + '\n... ' + (T.tool_result_truncated || '(truncated)') : txt;
                renderLog('tool-result', truncated, e.time || '', 'tool');
            } else if (e.type === 'intermediate') {
                if (e.content === '🌙 [Moonbeat]') {
                    renderMarker('🌙 [Moonbeat]');
                } else {
                    renderMessage('assistant', e.content || '', e.time || '');
                }
            } else {
                renderLog('system', e.content || '');
            }
        } catch (err) {
            console.error('エントリ描画エラー:', err, e);
        }
    }
}

// ダッシュボード表示モード: ダッシュボードのチャット欄と同じ見え方に揃える。
// ・llm_usage 等の生ログ（type:'raw'）を非表示
// ・tool_call / tool_result（ツールロール）を非表示
// ・Moonbeat / スケジューラー / OpenClaw はマーカー表示（ユーザー発言にしない）
function renderEntriesDashboard(entries) {
    let shown = 0;
    for (const e of entries) {
        try {
            if (e.type === 'message' && e.role === 'user') {
                const r = classifyUserContent(e.content || '');
                if (r.kind === 'marker') {
                    renderMarker(r.text);
                    shown++;
                } else if (r.kind === 'user' && r.text) {
                    renderMessage('user', r.text, e.time || '');
                    shown++;
                }
                // kind === 'hidden'（システム注入）は描画しない
            } else if (e.type === 'message' && e.role === 'assistant') {
                if ((e.content || '').trim()) {
                    renderMessage('assistant', e.content, e.time || '');
                    shown++;
                }
            } else if (e.type === 'intermediate') {
                if (e.content === '🌙 [Moonbeat]') {
                    renderMarker('🌙 [Moonbeat]');
                    shown++;
                } else if ((e.content || '').trim()) {
                    renderMessage('assistant', e.content, e.time || '');
                    shown++;
                }
            } else if (e.type === 'tool_call') {
                // ダッシュボード履歴同様に「🔧 ツール名 → 引数」の要約行は表示する。
                // 先にアシスタントのアバターヘッダーを保証し、アバターより先にツール行が出ないようにする。
                ensureAssistantHeader(e.time || '');
                renderLog('tool-call', `🔧 ${e.tool || '?'}${toolDetail(e.arguments)}`, e.time || '', 'tool_call');
                shown++;
            }
            // tool_result（ツールロール本体）/ raw(llm_usage) はダッシュボード履歴同様に非表示
        } catch (err) {
            console.error('エントリ描画エラー:', err, e);
        }
    }
    if (shown === 0) {
        chatMessages.innerHTML = `<div class="empty-state">${T.log_no_conversations || 'No conversations to display (tool/usage logs only)'}</div>`;
    }
}

function renderEntries(entries) {
    clearChat();
    if (!entries || entries.length === 0) {
        chatMessages.innerHTML = `<div class="empty-state">${T.log_empty_day || 'No logs for this day'}</div>`;
        return;
    }
    if (viewMode === 'dashboard') {
        renderEntriesDashboard(entries);
    } else {
        renderEntriesDetail(entries);
    }
}

// 描画済みのログから指定時刻・ロールの要素を探してスクロール＆ハイライトする。
// 検索結果クリック時のみ呼ばれる。見つからなければ上端のまま（無理はしない）。
function scrollToLogTime(time, role) {
    if (!time) return;
    const esc = (s) => (window.CSS && CSS.escape) ? CSS.escape(s) : s;
    // まず時刻＋ロールが一致する要素、無ければ時刻だけ一致する要素を狙う。
    // （ダッシュボード表示ではツール結果が非表示なので役割一致が無い場合がある）
    let target = role
        ? chatMessages.querySelector(`[data-time="${esc(time)}"][data-role="${esc(role)}"]`)
        : null;
    if (!target) target = chatMessages.querySelector(`[data-time="${esc(time)}"]`);
    if (!target) return;
    target.scrollIntoView({ behavior: 'smooth', block: 'center' });
    target.classList.add('search-highlight');
    setTimeout(() => target.classList.remove('search-highlight'), 2000);
}

async function loadDate(date, target = null) {
    if (!date) {
        clearChat();
        chatMessages.innerHTML = `<div class="empty-state">${T.log_select_date || 'Select a date above or use the search box to search all logs'}</div>`;
        return;
    }
    loadStatus.textContent = T.log_loading || 'Loading...';
    try {
        const res = await fetch(`/api/logs/${date}`);
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const data = await res.json();
        if (data.error) {
            chatMessages.innerHTML = `<div class="empty-state">${T.log_error || 'Error'}: ${escapeHtml(data.error)}</div>`;
            loadStatus.textContent = '';
            return;
        }
        currentEntries = data.entries || [];
        renderEntries(currentEntries);
        loadStatus.textContent = `${currentEntries.length} ${T.log_entries || 'entries'}`;
        if (target && target.time) {
            // 検索結果からのジャンプ: 該当時刻までスクロール
            scrollToLogTime(target.time, target.role);
        } else {
            // 履歴のように上から下へ時系列なので、上端にスクロール
            chatMessages.parentElement.scrollTop = 0;
        }
    } catch (e) {
        chatMessages.innerHTML = `<div class="empty-state">${T.log_load_failed || 'Load failed'}: ${escapeHtml(e.message)}</div>`;
        loadStatus.textContent = '';
    }
}

// 検索ボックスは日付未選択のときだけ表示する。
function updateSearchVisibility() {
    if (!searchBox) return;
    searchBox.style.display = dateSelect.value ? 'none' : 'inline-flex';
}

// 検索ワードがどこに出てきたかの4ロール区分。バッジ表示に使う。
const ROLE_META = {
    user:      { label: T.role_user || 'User',           cls: 'role-user' },
    assistant: { label: T.role_assistant || 'Assistant',  cls: 'role-assistant' },
    tool_call: { label: T.role_tool_call || 'Tool Call',  cls: 'role-tool_call' },
    tool:      { label: T.role_tool || 'Tool',            cls: 'role-tool' },
};

// 検索結果を「年月日ごとの時刻一覧」として描画する。本文は表示しない。
// 各時刻にはどのロールで出てきたかのバッジを付ける。
// 時刻チップをクリックするとその日付のログへジャンプする。
function renderSearchResults(query, results) {
    clearChat();
    if (!results || results.length === 0) {
        chatMessages.innerHTML =
            `<div class="empty-state">${(T.search_no_results || 'No logs found containing "{query}"').replace('{query}', escapeHtml(query))}</div>`;
        return;
    }

    const summary = document.createElement('div');
    summary.className = 'search-summary';
    summary.textContent = (T.search_results_count || 'Logs containing "{query}": {count}').replace('{query}', query).replace('{count}', results.length);
    chatMessages.appendChild(summary);

    // 連続する同一日付をグループにまとめる（結果は新しい日付が先頭の順）
    let currentDate = null;
    let group = null;
    let timesWrap = null;
    for (const r of results) {
        if (r.date !== currentDate) {
            currentDate = r.date;
            group = document.createElement('div');
            group.className = 'search-group';

            const dateLabel = document.createElement('div');
            dateLabel.className = 'search-group-date';
            // "2026-06-03" → "2026年6月3日"
            const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(r.date || '');
            dateLabel.textContent = m
                ? (T.date_format || '{y}/{m}/{d}').replace('{y}', m[1]).replace('{m}', parseInt(m[2], 10)).replace('{d}', parseInt(m[3], 10))
                : (r.date || (T.date_unknown || '(Unknown date)'));
            group.appendChild(dateLabel);

            timesWrap = document.createElement('div');
            timesWrap.className = 'search-times';
            group.appendChild(timesWrap);
            chatMessages.appendChild(group);
        }
        const chip = document.createElement('span');
        chip.className = 'search-time';

        const timeLabel = document.createElement('span');
        timeLabel.textContent = r.time || '--:--';
        chip.appendChild(timeLabel);

        const meta = ROLE_META[r.role];
        if (meta) {
            const badge = document.createElement('span');
            badge.className = `role-badge ${meta.cls}`;
            badge.textContent = meta.label;
            chip.appendChild(badge);
        }

        chip.title = (T.log_open_at || 'Open log at {date} {time}').replace('{date}', r.date).replace('{time}', r.time);
        chip.addEventListener('click', () => {
            dateSelect.value = r.date;
            updateSearchVisibility();
            loadDate(r.date, { time: r.time, role: r.role });
        });
        timesWrap.appendChild(chip);
    }
    chatMessages.parentElement.scrollTop = 0;
}

async function runSearch() {
    const query = (searchInput.value || '').trim();
    if (!query) return;
    loadStatus.textContent = T.log_searching || 'Searching...';
    try {
        const res = await fetch(`/api/logs/search?q=${encodeURIComponent(query)}`);
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const data = await res.json();
        renderSearchResults(data.query || query, data.results || []);
        loadStatus.textContent = data.truncated
            ? `${data.count} ${T.log_entries || 'entries'} (${T.log_limit_reached || 'limit reached'})`
            : `${data.count} ${T.log_entries || 'entries'}`;
    } catch (e) {
        chatMessages.innerHTML =
            `<div class="empty-state">${T.search_failed || 'Search failed'}: ${escapeHtml(e.message)}</div>`;
        loadStatus.textContent = '';
    }
}

// テーマ切り替えは共通モジュール theme.js（<head> で読込）に一元化。

async function init() {
    await loadProfile();
    try {
        const res = await fetch('/api/logs/dates');
        const data = await res.json();
        const dates = data.dates || [];
        for (const d of dates) {
            const opt = document.createElement('option');
            opt.value = d;
            opt.textContent = d;
            dateSelect.appendChild(opt);
        }
        if (dates.length === 0) {
            loadStatus.textContent = T.log_none || 'No logs';
        }
    } catch (e) {
        loadStatus.textContent = T.log_dates_load_failed || 'Failed to load dates';
    }

    dateSelect.addEventListener('change', () => {
        updateSearchVisibility();
        loadDate(dateSelect.value);
    });

    // 検索: ボタンクリックと Enter キーで実行
    if (searchBtn) searchBtn.addEventListener('click', runSearch);
    if (searchInput) {
        searchInput.addEventListener('keydown', (e) => {
            if (e.key === 'Enter') { e.preventDefault(); runSearch(); }
        });
    }
    updateSearchVisibility();

    // 表示モードの初期反映と切替（再フェッチせず保持中のエントリを再描画）
    if (modeSelect) {
        modeSelect.value = viewMode;
        modeSelect.addEventListener('change', () => {
            viewMode = modeSelect.value;
            localStorage.setItem('yuzuki_logviewer_mode', viewMode);
            if (dateSelect.value) renderEntries(currentEntries);
        });
    }

    // URL ハッシュで日付指定（例: /logs#2026-05-17）
    const hashDate = (location.hash || '').replace(/^#/, '');
    if (hashDate && /^\d{4}-\d{2}-\d{2}$/.test(hashDate)) {
        dateSelect.value = hashDate;
        loadDate(hashDate);
    }
}

init();
