// dashboard.js - 柚月エージェント ダッシュボードUI (v3: テーマ切り替え対応)

// 絵文字の代わりに自作SVGアイコンを描く共通ヘルパー（フォント非依存）。
// JSがボタンのラベルを書き換える箇所で textContent ではなく innerHTML と併用する。
function uiIcon(name) {
    return `<img class="ui-icon" src="/static/images/icons/${name}.svg" alt="">`;
}

// --- 要素の取得 ---
const chatMessages = document.getElementById('chatMessages');
const typingIndicator = document.getElementById('typingIndicator');
const messageInput = document.getElementById('messageInput');
const sendButton = document.getElementById('sendButton');
const statusEl = document.getElementById('status');
const compressButton = document.getElementById('compressButton');
const moonbeatToggle = document.getElementById('moonbeatToggle');
const moonbeatFire = document.getElementById('moonbeatFire');
const memoryLetters = document.getElementById('memoryLetters');
const memoryNotes = document.getElementById('memoryNotes');
const stopButton = document.getElementById('stopButton');
const attachButton = document.getElementById('attachButton');
const imageFileInput = document.getElementById('imageFileInput');
const attachmentPreviewArea = document.getElementById('attachmentPreviewArea');

let lastTokenUsage = null;
let ws = null;

// --- 添付処理（画像・テキストファイル、複数・混在可）---
// 各要素: { kind:'image', name, dataUrl } または { kind:'text', name, content }
let pendingAttachments = [];

// テキストファイルの上限。生サイズ2MBまで読み込み、AIに渡す文字数は10万字で打ち切る
// （長すぎる貼り付けでコンテキストを食い潰さないための保険）。
const TEXT_FILE_MAX_BYTES = 2 * 1024 * 1024;
const TEXT_FILE_MAX_CHARS = 100000;
const IMAGE_FILE_MAX_BYTES = 10 * 1024 * 1024;

attachButton.addEventListener('click', () => imageFileInput.click());

imageFileInput.addEventListener('change', (e) => {
    const files = Array.from(e.target.files || []);
    for (const file of files) {
        const isImage = file.type.startsWith('image/');
        if (isImage) {
            if (file.size > IMAGE_FILE_MAX_BYTES) {
                alert((T.file_image_too_large || '').replace('{name}', file.name));
                continue;
            }
            const reader = new FileReader();
            reader.onload = (ev) => {
                pendingAttachments.push({ kind: 'image', name: file.name, dataUrl: ev.target.result });
                renderAttachmentPreviews();
            };
            reader.readAsDataURL(file);
        } else {
            // 画像以外はテキストとして読む。中身がバイナリかどうかは読み込み後に判定する。
            if (file.size > TEXT_FILE_MAX_BYTES) {
                alert((T.file_text_too_large || '').replace('{name}', file.name));
                continue;
            }
            const reader = new FileReader();
            reader.onload = (ev) => {
                let text = ev.target.result || '';
                // ヌル文字を含むものはバイナリとみなして弾く（テキストとして扱えない）
                if (/\u0000/.test(text)) {
                    alert((T.file_not_text || '').replace('{name}', file.name));
                    return;
                }
                if (text.length > TEXT_FILE_MAX_CHARS) {
                    text = text.slice(0, TEXT_FILE_MAX_CHARS) + '\n' + (T.file_truncated || '...');
                }
                pendingAttachments.push({ kind: 'text', name: file.name, content: text });
                renderAttachmentPreviews();
            };
            reader.readAsText(file);
        }
    }
    imageFileInput.value = ''; // リセットして同じファイルも再選択可能に
});

// 添付プレビューを描画する。画像はサムネ、テキストはファイル名チップで横並び表示。
function renderAttachmentPreviews() {
    attachmentPreviewArea.innerHTML = '';
    if (pendingAttachments.length === 0) {
        attachmentPreviewArea.classList.remove('visible');
        return;
    }
    attachmentPreviewArea.classList.add('visible');
    pendingAttachments.forEach((att, idx) => {
        const item = document.createElement('div');
        item.className = `attachment-item ${att.kind}`;
        if (att.kind === 'image') {
            const img = document.createElement('img');
            img.src = att.dataUrl;
            img.alt = att.name;
            img.title = att.name;
            item.appendChild(img);
        } else {
            const icon = document.createElement('img');
            icon.className = 'ui-icon';
            icon.src = '/static/images/icons/scroll.svg';
            icon.alt = '';
            const label = document.createElement('span');
            label.className = 'attachment-name';
            label.textContent = att.name;
            label.title = att.name;
            item.appendChild(icon);
            item.appendChild(label);
        }
        const remove = document.createElement('button');
        remove.className = 'attachment-remove';
        remove.title = T.attachment_remove || 'Remove';
        remove.innerHTML = '<img class="ui-icon" src="/static/images/icons/x-mark.svg" alt="">';
        remove.addEventListener('click', () => {
            pendingAttachments.splice(idx, 1);
            renderAttachmentPreviews();
        });
        item.appendChild(remove);
        attachmentPreviewArea.appendChild(item);
    });
}

// テーマ切り替えは共通モジュール theme.js（<head> で読込）に一元化。

// --- リサイズ機能（左右 + 上下） ---
function enableResize() {
    const mainLayout = document.getElementById('mainLayout');
    const rightContainer = document.getElementById('rightContainer');

    // 1. 左右リサイズ (id="resizerH")
    const resizerH = document.getElementById('resizerH');
    if (resizerH) {
        resizerH.addEventListener('mousedown', (e) => {
            e.preventDefault();
            document.body.style.cursor = 'col-resize';
            const startX = e.clientX;

            const computed = window.getComputedStyle(mainLayout);
            const cols = computed.gridTemplateColumns.split(' ');
            const leftW = parseFloat(cols[0]);
            const rightW = parseFloat(cols[2]);

            const onMouseMove = (ev) => {
                const delta = ev.clientX - startX;
                const newLeft = leftW + delta;
                const newRight = rightW - delta;

                if (newLeft < 300 || newRight < 300) return;

                mainLayout.style.gridTemplateColumns = `1fr 6px ${newRight}px`;
            };

            const onMouseUp = () => {
                document.removeEventListener('mousemove', onMouseMove);
                document.removeEventListener('mouseup', onMouseUp);
                document.body.style.cursor = '';
                saveLayout();
            };

            document.addEventListener('mousemove', onMouseMove);
            document.addEventListener('mouseup', onMouseUp);
        });
    }

    // 2. 上下リサイズ (id="resizerV")
    const resizerV = document.getElementById('resizerV');
    if (resizerV) {
        resizerV.addEventListener('mousedown', (e) => {
            e.preventDefault();
            document.body.style.cursor = 'row-resize';
            const startY = e.clientY;

            const computed = window.getComputedStyle(rightContainer);
            const rows = computed.gridTemplateRows.split(' ');
            const topH = parseFloat(rows[0]);
            const bottomH = parseFloat(rows[2]);

            const onMouseMove = (ev) => {
                const delta = ev.clientY - startY;
                const newTop = topH + delta;
                const newBottom = bottomH - delta;

                if (newTop < 100 || newBottom < 100) return;

                rightContainer.style.gridTemplateRows = `${newTop}fr 6px ${newBottom}fr`;
            };

            const onMouseUp = () => {
                document.removeEventListener('mousemove', onMouseMove);
                document.removeEventListener('mouseup', onMouseUp);
                document.body.style.cursor = '';
                saveLayout();
            };

            document.addEventListener('mousemove', onMouseMove);
            document.addEventListener('mouseup', onMouseUp);
        });
    }

    loadLayout();
}

function saveLayout() {
    const mainLayout = document.getElementById('mainLayout');
    const rightContainer = document.getElementById('rightContainer');
    const settings = {
        cols: mainLayout.style.gridTemplateColumns,
        rows: rightContainer.style.gridTemplateRows
    };
    localStorage.setItem('yuzuki_dashboard_layout_v2', JSON.stringify(settings));
}

function loadLayout() {
    const saved = localStorage.getItem('yuzuki_dashboard_layout_v2');
    if (saved) {
        try {
            const settings = JSON.parse(saved);
            if (settings.cols) {
                // 過去の固定ピクセル保存データがあれば柔軟なレイアウトに変換
                let cols = settings.cols;
                if (/^\d+(\.\d+)?px\s+6px\s+\d+(\.\d+)?px$/.test(cols)) {
                    const parts = cols.split(' ');
                    cols = `1fr 6px ${parts[2]}`;
                }
                document.getElementById('mainLayout').style.gridTemplateColumns = cols;
            }
            if (settings.rows) {
                let rows = settings.rows;
                if (/^\d+(\.\d+)?px\s+6px\s+\d+(\.\d+)?px$/.test(rows)) {
                    const parts = rows.split(' ');
                    rows = `${parts[0].replace('px', '')}fr 6px ${parts[2].replace('px', '')}fr`;
                }
                document.getElementById('rightContainer').style.gridTemplateRows = rows;
            }
        } catch (e) { console.error(e); }
    }
}

// --- 履歴管理 ---
function restoreHistory() {
    window.chatHistoryManager.restore({
        onMessage: (role, content, time, save, isMoonbeat) => {
            addChatMessage(role, content, time, save, isMoonbeat);
        },
        onLog: (logType, content, save) => {
            addLog(logType, content, save);
        },
        onSystem: (content, save) => {
            if (content === '🌙 [Moonbeat]') {
                const marker = document.createElement('div');
                marker.className = 'chat-block moonbeat-marker';
                marker.innerHTML = '<span class="moonbeat-label">🌙 [Moonbeat]</span>';
                chatMessages.insertBefore(marker, typingIndicator);
            } else {
                addLog('system', content, save);
            }
        },
        onTool: (content, save) => {
            addLog('tool-call', content, save);
        }
    });
    scrollChat();
}

// --- WebSocket接続 ---
function connect() {
    ws = new WebSocket(`ws://${location.host}/ws`);

    ws.onopen = () => {
        statusEl.textContent = T.status_online || 'Online';
        statusEl.classList.add('status-online');
        statusEl.classList.remove('status-offline');
        messageInput.disabled = false;
        sendButton.disabled = false;
        compressButton.disabled = false;
        attachButton.disabled = false;
        window._historyRestored = false;
        // 履歴復元が途中で切れた場合に備えてバッチモードフラグを必ずリセット
        _historyBatchMode = false;
        window.chatHistoryManager.setBulkMode(false);
        messageInput.focus();
        addLog('system', T.ws_connected || 'Connected', false);
    };

    ws.onmessage = (event) => {
        const data = JSON.parse(event.data);
        if (data.type === 'history_start') {
            // サーバーが履歴の真実を再送してくる。古い表示と localStorage を一旦クリア。
            // 受信途中のストリーミング状態も破棄する（吹き出しごと作り直されるため）
            window._activeStream = null;
            window._pendingStreamEl = null;
            window.chatHistoryManager.clear();
            while (chatMessages.firstChild && chatMessages.firstChild !== typingIndicator) {
                chatMessages.removeChild(chatMessages.firstChild);
            }
            window._historyRestored = false;
            // バッチ復元モード開始: scrollChat と localStorage 書き込みを抑制
            _historyBatchMode = true;
            window.chatHistoryManager.setBulkMode(true);
            return;
        }
        if (data.type === 'history_end') {
            // バッチ復元モード解除 → localStorage に1回だけ保存し、1回だけスクロール
            window.chatHistoryManager.setBulkMode(false);
            _historyBatchMode = false;
            window._historyRestored = true;
            scrollChat();
            return;
        }
        if (data.type === 'history_batch') {
            // 履歴をitems配列で一括受信 → 同期的に全件描画。
            // _historyBatchMode 中は addChatMessage/addLog 内の scrollChat が no-op になり、
            // chatHistoryManager の save() も保留される（history_end で flush）。
            const items = data.items || [];
            // 履歴再生中の「現在のターンの時刻」。tool_callログの前に差し込まれる
            // アシスタントのアバターヘッダー（ensureAssistantHeader）へ正しい時刻を渡すために追跡する。
            // （これが無いと空ヘッダーが現在時刻にフォールバックし、ターンの時刻が「今」に化ける）
            let turnTime = '';
            for (const item of items) {
                if (item.kind === 'message') {
                    // 履歴復元: 時刻が無い場合は空文字を渡す（''）。
                    // 空文字なら addChatMessage 内の「現在時刻で補completion」は発動せず空欄表示になる。
                    // （null/undefined のときだけ現在時刻にフォールバックする ＝ ライブ新着メッセージ用）
                    if (item.time) turnTime = item.time;
                    addChatMessage(item.role, item.content, item.time || '', true, false);
                } else if (item.kind === 'intermediate') {
                    if (item.content === '🌙 [Moonbeat]') {
                        const marker = document.createElement('div');
                        marker.className = 'chat-block moonbeat-marker';
                        marker.innerHTML = '<span class="moonbeat-label">🌙 [Moonbeat]</span>';
                        chatMessages.insertBefore(marker, typingIndicator);
                        window.chatHistoryManager.addSystemLog('🌙 [Moonbeat]');
                    } else {
                        addChatMessage('assistant', item.content, null, true, false);
                    }
                } else if (item.kind === 'tool_call') {
                    let detail = '';
                    const a = item.arguments || {};
                    if (a.path) detail = ` → ${a.path}`;
                    else if (a.directory !== undefined) detail = ` → ${a.directory || '/'}`;
                    else if (a.query) detail = ` → "${a.query}"`;
                    else if (a.url) detail = ` → ${a.url}`;
                    else if (a.app_name) detail = ` → ${a.app_name}`;
                    else if (a.source) detail = ` → ${a.source}`;
                    // turnTime を渡し、アバターヘッダーが現在時刻に化けないようにする
                    addLog('tool-call', `🔧 ${item.tool_name}${detail}`, true, turnTime);
                } else if (item.kind === 'marker') {
                    // 上限超過時に先頭へ差し込まれる「これより前は /logs で閲覧できます」マーカー
                    addLog('system', item.content);
                }
            }
            return;
        }
        if (data.type === 'user_message') {
            console.log('user_message received, msg_id:', data.msg_id, '_sentMessageIds:', window._sentMessageIds);
            if (!window._sentMessageIds?.has(data.msg_id)) {
                addChatMessage('user', data.content, null, true, false);
            } else {
                window._sentMessageIds.delete(data.msg_id);
            }
            return;
        }
        // === リアルタイムストリーミング受信 ===
        // stream_delta で吹き出しを伸ばし、直後に来る intermediate / response（正規テキスト）で置換・保存する。
        // _activeStream: 受信中のストリーム。_pendingStreamEl: stream_end 済みで正規テキスト待ちの吹き出し。
        if (data.type === 'stream_begin') {
            window._activeStream = { id: data.stream_id, el: null, text: '' };
            return;
        }
        if (data.type === 'stream_delta') {
            let s = window._activeStream;
            if (!s || s.id !== data.stream_id) {
                // stream_begin を取り逃した場合（ストリーム途中で接続したクライアント等）もここから追随
                s = window._activeStream = { id: data.stream_id, el: null, text: '' };
            }
            s.text += data.content;
            if (!s.el) {
                // 最初の非空deltaで吹き出しを生成。保存しない（正規テキスト確定時に保存）・タイプライター演出なし
                s.el = addChatMessage('assistant', s.text, data.time || null, false, false);
                if (s.el) s.el.classList.add('streaming');
            } else {
                s.el.textContent = s.text;
            }
            // 最下部付近にいるときだけ追従スクロール（過去ログを読んでいる最中は邪魔しない）
            const nearBottom = chatMessages.scrollHeight - chatMessages.scrollTop - chatMessages.clientHeight < 80;
            if (nearBottom) scrollChat();
            return;
        }
        if (data.type === 'stream_reset') {
            // 再生成（簡体字リトライ等）: ここまで流れたテキストを破棄して書き直し
            const s = window._activeStream;
            if (s && s.id === data.stream_id) {
                s.text = '';
                if (s.el) s.el.textContent = '';
            }
            return;
        }
        if (data.type === 'stream_end') {
            const s = window._activeStream;
            if (s && s.id === data.stream_id) {
                if (s.el) {
                    s.el.classList.remove('streaming');
                    window._pendingStreamEl = s.el;
                }
                window._activeStream = null;
            }
            return;
        }
        if (data.type === 'response') {
            if (window.chatHistoryManager.isCancelled) {
                console.log("[DEBUG] キャンセル済みの応答を無視します");
                window.chatHistoryManager.isCancelled = false;
                // 流しかけの吹き出しは表示したまま、ストリーム状態だけ破棄する
                window._activeStream = null;
                window._pendingStreamEl = null;
                return;
            }
            window.chatHistoryManager.setLoading(false);

            // Moonbeatの場合は新しいブロックとして扱う
            if (data.is_moonbeat) {
                window._lastIntermediateText = '';
                window._intermediateEl = null;
                // サーバーがターン時刻を送ってきたらそれを使う（長いターンで「今」になるのを防ぐ）
                addChatMessage('assistant', data.content, data.time || null, true, false, null, false, true);
            } else {
                // 中間テキストと最終応答の重複防止
                const lastIntermediate = window._lastIntermediateText || '';
                window._lastIntermediateText = '';
                if (window._pendingStreamEl) {
                    // ストリーム済み吹き出しを正規テキスト（クリーン済み全文）で確定し、ここで履歴に保存
                    window._pendingStreamEl.textContent = data.content;
                    window.chatHistoryManager.addMessage('assistant', data.content, data.time || nowTimeStr(), false);
                    window._pendingStreamEl = null;
                } else if (lastIntermediate && data.content === lastIntermediate) {
                    window.chatHistoryManager.removeLastAssistant();
                } else {
                    addChatMessage('assistant', data.content, data.time || null, true, false, null, false, true);
                }
                window._intermediateEl = null;
            }

            if (data.token_usage) {
                lastTokenUsage = data.token_usage;
                updateTokenDisplay(data.token_usage);
            }
            refreshMemory();
        }
        else if (data.type === 'intermediate') {
            if (data.content === '🌙 [Moonbeat]') {
                // Moonbeatマーカーはログとして表示
                // Moonbeatマーカーを区切りブロックとして挿入
                const marker = document.createElement('div');
                marker.className = 'chat-block moonbeat-marker';
                marker.innerHTML = '<span class="moonbeat-label">🌙 [Moonbeat]</span>';
                chatMessages.insertBefore(marker, typingIndicator);
                window.chatHistoryManager.addSystemLog('🌙 [Moonbeat]');
                scrollChat();            } else {
                if (window._pendingStreamEl) {
                    // ストリーム済み吹き出しを正規テキストで置換し、ここで初めて履歴に保存する
                    window._pendingStreamEl.textContent = data.content;
                    window.chatHistoryManager.addMessage('assistant', data.content, data.time || nowTimeStr(), false);
                    window._intermediateEl = window._pendingStreamEl;
                    window._pendingStreamEl = null;
                } else {
                    // 通常の中間テキストは吹き出しとして表示
                    window._intermediateEl = addChatMessage('assistant', data.content, data.time || null, true, false, null, false, true);
                }
                window._lastIntermediateText = data.content;
            }
            scrollChat();
        }
        else if (data.type === 'tool_call') {
            let detail = '';
            if (data.arguments) {
                if (data.arguments.path) detail = ` → ${data.arguments.path}`;
                else if (data.arguments.directory !== undefined) detail = ` → ${data.arguments.directory || '/'}`;
                else if (data.arguments.query) detail = ` → "${data.arguments.query}"`;
                else if (data.arguments.url) detail = ` → ${data.arguments.url}`;
                else if (data.arguments.app_name) detail = ` → ${data.arguments.app_name}`;
                else if (data.arguments.source) detail = ` → ${data.arguments.source}`;
            }
            addLog('tool-call', `🔧 ${data.tool_name}${detail}`);

            if (data.result) {
                const resultText = typeof data.result === 'string' ? data.result : JSON.stringify(data.result, null, 2);
                const truncated = resultText.length > 300 ? resultText.substring(0, 300) + '\n... ' + (T.tool_result_truncated || '(truncated)') : resultText;
                addLog('tool-result', truncated);
            }
            if (data.token_usage) {
                lastTokenUsage = data.token_usage;
                updateTokenDisplay(data.token_usage);
            }
        }
        else if (data.type === 'compress_result') {
            compressButton.disabled = false;
            compressButton.innerHTML = uiIcon('box') + (T.compress || 'Compress');

            const notice = document.createElement('div');
            notice.className = 'system-notice';
            const noticeContent = data.success ? `✅ ${data.message}` : `⚠️ ${data.message}`;
            notice.textContent = noticeContent;
            chatMessages.insertBefore(notice, typingIndicator);
            scrollChat();

            addLog('system', data.success
                ? (T.compress_success || 'Compression succeeded') + ': ' + data.message
                : (T.compress_fail || 'Compression failed') + ': ' + data.message);
            if (data.token_usage) {
                lastTokenUsage = data.token_usage;
                updateTokenDisplay(data.token_usage);
            }
        }
        else if (data.type === 'token_update') {
            if (data.token_usage) {
                lastTokenUsage = data.token_usage;
                updateTokenDisplay(data.token_usage);
            }
        }
        else if (data.type === 'error') {
            window.chatHistoryManager.setLoading(false);
            // 流しかけの吹き出しは表示したまま、ストリーム状態だけ破棄する
            if (window._activeStream && window._activeStream.el) {
                window._activeStream.el.classList.remove('streaming');
            }
            window._activeStream = null;
            window._pendingStreamEl = null;
            addChatMessage('assistant', `⚠️ ${data.content}`);
            addLog('error', data.content);
        }
    };

    ws.onclose = () => {
        // 切断時はストリーム状態を破棄（再接続で履歴が真実として再送される）
        if (window._activeStream && window._activeStream.el) {
            window._activeStream.el.classList.remove('streaming');
        }
        window._activeStream = null;
        window._pendingStreamEl = null;
        statusEl.textContent = T.status_reconnecting || 'Offline - Reconnecting...';
        statusEl.classList.add('status-offline');
        statusEl.classList.remove('status-online');
        messageInput.disabled = true;
        sendButton.disabled = true;
        compressButton.disabled = true;
        attachButton.disabled = true;
        addLog('system', T.ws_disconnected || 'Disconnected. Reconnecting...', false);
        setTimeout(connect, 3000);
    };

    ws.onerror = () => {
        ws.close();
    };
}

// --- プロファイル機能 ---
let userProfile = { name: "User", avatar: "" };
let agentProfile = { name: "Assistant", avatar: "" };

async function initProfile() {
    try {
        const res = await fetch('/api/config');
        if (res.ok) {
            const data = await res.json();
            if (data.profile) {
                if (data.profile.user) userProfile = data.profile.user;
                if (data.profile.agent) agentProfile = data.profile.agent;

                // ドキュメント内のエージェント名を動的更新
                document.title = `${agentProfile.name} - ${T.dashboard_title || 'Dashboard'}`;
                const headerNames = document.querySelectorAll('.header-name');
                headerNames.forEach(el => el.textContent = agentProfile.name);

                const typingObj = document.getElementById('typingIndicator');
                if (typingObj) typingObj.textContent = (T.agent_typing_named || '{name} is typing...').replace('{name}', agentProfile.name);
            }
        }
    } catch (e) {
        console.error("Config load error:", e);
    }
}

// --- UI操作関数群 ---
// タイプライター演出: 吹き出しのテキストを1文字ずつ表示する。
// クリックで即座に全文表示。要素がDOMから外れたら停止する。
function typewriterReveal(el, fullText) {
    // 絵文字などサロゲートペアを途中で切らないようコードポイント単位に分割
    const chars = Array.from(fullText);
    const total = chars.length;
    if (total === 0) return;
    const tick = 25;
    // 1文字あたり約25ms、ただし長文でも全体が約3秒で終わるようにまとめ出しする
    const charsPerTick = Math.max(1, Math.ceil(total / (3000 / tick)));
    let shown = 0;
    el.textContent = '';
    el.classList.add('typing-anim');
    const finish = () => {
        clearInterval(timer);
        el.textContent = fullText;
        el.classList.remove('typing-anim');
        el.removeEventListener('click', finish);
    };
    el.addEventListener('click', finish);
    const timer = setInterval(() => {
        if (!el.isConnected) { clearInterval(timer); return; }
        shown = Math.min(total, shown + charsPerTick);
        el.textContent = chars.slice(0, shown).join('');
        // ユーザーが最下部付近にいるときだけ追従スクロール（履歴読み中は邪魔しない）
        const nearBottom = chatMessages.scrollHeight - chatMessages.scrollTop - chatMessages.clientHeight < 80;
        if (nearBottom) scrollChat();
        if (shown >= total) finish();
    }, tick);
}

function addChatMessage(role, content, timeStr = null, save = true, forceNewBlock = false, attachments = null, headerOnly = false, animate = false) {
    // attachments: 添付の配列（{kind:'image',dataUrl,name} / {kind:'text',name,...}）。null/空可。
    const hasAttachments = Array.isArray(attachments) && attachments.length > 0;
    // 空メッセージ（テキストも添付も無い）は描画しない。
    // ツール呼び出しだけの assistant メッセージが「現在時刻つきの空吹き出し」として表示され、
    // その先頭ヘッダーがターンの見かけ上の時刻を「今」で乗っ取ってしまうバグを防ぐ。
    if (!headerOnly && !hasAttachments && (!content || !String(content).trim())) {
        return null;
    }

    const profile = role === 'user' ? userProfile : agentProfile;
    const layout = profile.layout || (role === 'user' ? 'horizontal' : 'vertical');

    // ブロックコンテナ
    const block = document.createElement('div');
    block.className = `chat-block ${role} ${layout}`;

    // アバター要素作成（共通）
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

    // 時刻フォールバックは「null / undefined のときだけ」現在時刻にする（＝ライブ新着用）。
    // 履歴復元で時刻不明のときは空文字 '' が渡るので、ここは発動せず空欄のままにする
    // （過去メッセージを「今」の時刻で描画して“タイムスリップ”表示になるのを防ぐ）。
    if (timeStr === null || timeStr === undefined) {
        const now = new Date();
        timeStr = now.getHours().toString().padStart(2, '0') + ':' + now.getMinutes().toString().padStart(2, '0');
    }

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
        timeNode.textContent = timeStr;

        nameContainer.appendChild(name);
        nameContainer.appendChild(timeNode);
        return nameContainer;
    }

    // メッセージバブル要素作成（共通）
    const msg = document.createElement('div');
    msg.className = `message ${role}`;
    msg.textContent = content;
    // ライブ新着のアシスタント応答だけタイプライター表示（履歴復元・ユーザー発言は即時）
    if (animate && !headerOnly && !hasAttachments && content) {
        typewriterReveal(msg, String(content));
    }

    // 添付があればバブル内に表示（画像はサムネ、テキストはファイル名チップ）
    if (hasAttachments && role === 'user') {
        const wrap = document.createElement('div');
        wrap.className = 'message-attachments';
        attachments.forEach((att) => {
            if (att.kind === 'image') {
                const img = document.createElement('img');
                img.src = att.dataUrl;
                img.alt = att.name || (T.attachment_image || 'Attached image');
                img.className = 'message-attachment-image';
                wrap.appendChild(img);
            } else {
                const chip = document.createElement('span');
                chip.className = 'message-attachment-file';
                chip.title = att.name || '';
                chip.innerHTML = '<img class="ui-icon" src="/static/images/icons/scroll.svg" alt="">';
                const label = document.createElement('span');
                label.textContent = att.name || 'file';
                chip.appendChild(label);
                wrap.appendChild(chip);
            }
        });
        msg.appendChild(wrap);
    }

    if (layout === 'vertical') {
        // === 縦並びレイアウト（画像の下にセリフ） ===
        const header = document.createElement('div');
        header.className = 'chat-header';

        header.appendChild(createAvatar());
        header.appendChild(createName());

        block.appendChild(header);
        if (!headerOnly) block.appendChild(msg);

    } else {
        // === 横並びレイアウト（画像の横にセリフ） ===
        // アバター
        block.appendChild(createAvatar());

        // ボディ（名前＋メッセージ）
        const body = document.createElement('div');
        body.className = 'chat-body';

        body.appendChild(createName());
        if (!headerOnly) body.appendChild(msg);

        block.appendChild(body);
    }
    // 連続する同じroleの場合、アバターとヘッダーを非表示（間のツールログを飛ばす）
    let prevBlock = typingIndicator.previousElementSibling;
    const directPrev = prevBlock;
    while (prevBlock && !prevBlock.classList.contains('chat-block')) {
        prevBlock = prevBlock.previousElementSibling;
    }
    if (!forceNewBlock && prevBlock && prevBlock.classList.contains(role)) {
        const header = block.querySelector('.chat-header');
        if (header) header.style.display = 'none';
        // 詰めるのは吹き出し同士が直接隣り合うときだけ。
        // 間にツールログが挟まる場合はCSS側の間隔調整（.log-entry + .chat-block）に任せる
        if (prevBlock === directPrev) block.style.marginTop = '-8px';
    }
    chatMessages.insertBefore(block, typingIndicator);
    scrollChat();

    if (save && !headerOnly) {
        window.chatHistoryManager.addMessage(role, content, timeStr, forceNewBlock);
    }

    return msg;
}

// 現在時刻の "HH:MM" 文字列（ライブ新着でサーバーから時刻が来なかったときの補完用）
function nowTimeStr() {
    const now = new Date();
    return now.getHours().toString().padStart(2, '0') + ':' + now.getMinutes().toString().padStart(2, '0');
}

// 履歴一括復元中は scrollChat を抑止し、最後に1回だけ呼ぶ（layout thrash 回避）
let _historyBatchMode = false;
function scrollChat() {
    if (_historyBatchMode) return;
    chatMessages.scrollTop = chatMessages.scrollHeight;
}

// ツールログを差し込む前に、そのターンでアシスタントのアバターがまだ出ていなければ
// アバターヘッダーだけのブロックを先に挿入する（アバターより先にツールが並ぶのを防ぐ）
function ensureAssistantHeader(timeStr = null) {
    // typingIndicatorの直前から遡り、最も近い chat-block を探す
    let prev = typingIndicator.previousElementSibling;
    while (prev && !prev.classList.contains('chat-block')) {
        prev = prev.previousElementSibling;
    }
    // 直近のブロックが既にアシスタントの発言なら、アバターは表示済みなので何もしない
    if (prev && prev.classList.contains('assistant')) return;
    // それ以外（Moonbeat区切り・ユーザー発言・先頭）の場合はヘッダーのみのブロックを差し込む。
    // timeStr は履歴再生時にそのターンの時刻を渡す（ライブ時はnull→現在時刻でOK）。
    addChatMessage('assistant', '', timeStr, false, true, null, true);
}

function addLog(type, text, save = true, timeStr = null) {
    if (type === 'tool-call' || type === 'tool-result') {
        ensureAssistantHeader(timeStr);
    }
    const entry = document.createElement('div');
    entry.className = `log-entry ${type}`;
    entry.textContent = text;
    chatMessages.insertBefore(entry, typingIndicator);
    scrollChat();

    if (save) {
        window.chatHistoryManager.addDashboardLog(type, text);
    }
}

function sendMessage() {
    const text = messageInput.value.trim();
    const attachments = pendingAttachments.slice();
    const hasAttachments = attachments.length > 0;
    if ((!text && !hasAttachments) || !ws || ws.readyState !== WebSocket.OPEN) return;
    // 中断時に復元できるよう、送信したテキストを一時保存
    window.lastSentMessage = text;

    // 添付の内訳で、本文が空のときのデフォルト文言を切り替える
    const images = attachments.filter(a => a.kind === 'image');
    const files = attachments.filter(a => a.kind === 'text');
    let displayText = text;
    if (!displayText) {
        if (images.length && files.length) displayText = T.send_files || '(Sending files)';
        else if (images.length) displayText = T.send_images || '(Sending images)';
        else displayText = T.send_files || '(Sending files)';
    }

    const msgId = Math.random().toString(36).slice(2);
    window._sentMessageIds = window._sentMessageIds || new Set();
    window._sentMessageIds.add(msgId);
    addChatMessage('user', displayText, null, true, false, attachments);

    messageInput.value = '';
    messageInput.style.height = 'auto';
    window.chatHistoryManager.setLoading(true);
    scrollChat();

    // 本文が空のときは中立な添付通知だけを置く（指示はAIの判断に任せる）。
    // ファイル名は別途 <attached_images> / <attached_file> でAIに伝わる。
    let content = text;
    if (!content) {
        if (images.length && files.length) content = T.attached_files || '(Files attached)';
        else if (images.length) content = T.attached_images || '(Images attached)';
        else content = T.attached_files || '(Files attached)';
    }

    const payload = {
        type: 'message',
        content: content,
        msg_id: msgId,
    };
    if (images.length) {
        // ファイル名も渡す（image_url には名前が乗らないため、バックエンドが本文に明記する）
        payload.images = images.map(a => ({ name: a.name, url: a.dataUrl }));
    }
    if (files.length) {
        payload.files = files.map(a => ({ name: a.name, content: a.content }));
    }
    ws.send(JSON.stringify(payload));

    // 添付プレビューをクリア
    pendingAttachments = [];
    renderAttachmentPreviews();
}

function cancelResponse() {
    if (!ws || ws.readyState !== WebSocket.OPEN) return;

    console.log("中断をリクエストします...");
    ws.send(JSON.stringify({ type: 'cancel' }));
    window.chatHistoryManager.isCancelled = true;

    // UIを即座に更新
    window.chatHistoryManager.setLoading(false);

    // 直近のメッセージを履歴から消去（UI上）
    window.chatHistoryManager.popLastExchange();

    // チャットエリアをクリアして再描画
    while (chatMessages.firstChild && chatMessages.firstChild !== typingIndicator) {
        chatMessages.removeChild(chatMessages.firstChild);
    }
    restoreHistory();

    // 入力欄にメッセージを復元
    if (window.lastSentMessage) {
        messageInput.value = window.lastSentMessage;
        messageInput.focus();
        messageInput.style.height = 'auto';
        messageInput.style.height = Math.min(messageInput.scrollHeight, 80) + 'px';
    }
}

function updateTokenDisplay(tokenUsage) {
    const used = tokenUsage.used;
    const max = tokenUsage.max;
    const ratio = tokenUsage.ratio;
    const pct = Math.round(ratio * 100);
    document.getElementById('tokenInfo').textContent = `${used.toLocaleString()} / ${max.toLocaleString()} (${pct}%)`;
    const bar = document.getElementById('tokenBar');
    bar.style.width = `${Math.min(ratio * 100, 100)}%`;
    if (ratio >= 0.9) bar.style.background = 'var(--danger)';
    else if (ratio >= 0.7) bar.style.background = 'var(--warning)';
    else bar.style.background = 'var(--tool-color)';
}

function requestCompress() {
    if (!ws || ws.readyState !== WebSocket.OPEN) return;
    compressButton.disabled = true;
    compressButton.innerHTML = uiIcon('box') + (T.compress_running || 'Compressing...');
    const count = parseInt(document.getElementById('compressCount')?.value || '100');
    ws.send(JSON.stringify({ type: 'compress', count: count }));
}

// --- Moonbeatオン/オフ ---
// ボタンの見た目を現在の状態に合わせて更新する
function renderMoonbeatToggle(enabled) {
    if (!moonbeatToggle) return;
    moonbeatToggle.dataset.enabled = enabled ? '1' : '0';
    moonbeatToggle.innerHTML = uiIcon('moon') + (enabled ? 'Moonbeat ON' : 'Moonbeat OFF');
    moonbeatToggle.classList.toggle('moonbeat-on', enabled);
    moonbeatToggle.classList.toggle('moonbeat-off', !enabled);
    moonbeatToggle.disabled = false;
}

// 起動時に現在のMoonbeat設定を読み込んでボタンに反映する
async function loadMoonbeatState() {
    // 手動発火はオン/オフ状態に依存しないため、ここで常に有効化する
    if (moonbeatFire) moonbeatFire.disabled = false;
    if (!moonbeatToggle) return;
    try {
        const res = await fetch('/api/settings/moonbeat');
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const data = await res.json();
        renderMoonbeatToggle(data.enabled !== false);
    } catch (e) {
        // 読み込み失敗時は操作不能のままにする
        moonbeatToggle.innerHTML = uiIcon('moon') + 'Moonbeat −';
        moonbeatToggle.disabled = true;
        console.error('[Moonbeat] 状態の読み込みに失敗しました:', e);
    }
}

// クリックで enabled をトグルし、設定ファイルへ保存する（部分payloadでディープマージ）
async function toggleMoonbeat() {
    if (!moonbeatToggle || moonbeatToggle.disabled) return;
    const next = moonbeatToggle.dataset.enabled !== '1';
    moonbeatToggle.disabled = true;
    try {
        const res = await fetch('/api/settings/moonbeat', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ enabled: next })
        });
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        renderMoonbeatToggle(next);
    } catch (e) {
        console.error('[Moonbeat] 状態の保存に失敗しました:', e);
        moonbeatToggle.disabled = false;
    }
}

// 手動Moonbeat発火。オフ中でも実行可。睡眠中・処理中はサーバー側でスキップされる。
async function fireMoonbeat() {
    if (!moonbeatFire || moonbeatFire.disabled) return;
    const original = moonbeatFire.innerHTML;  // アイコン込みで退避（textContentだとSVGが消える）
    moonbeatFire.disabled = true;
    moonbeatFire.innerHTML = uiIcon('lightning') + (T.moonbeat_firing || 'Firing...');
    // ステータスを一時表示してから元のラベルに戻す（一時表示はテキストのみ）
    const flash = (label, ms = 2500) => {
        moonbeatFire.textContent = label;
        setTimeout(() => {
            moonbeatFire.innerHTML = original;
            moonbeatFire.disabled = false;
        }, ms);
    };
    try {
        const res = await fetch('/api/moonbeat/fire', { method: 'POST' });
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const data = await res.json();
        if (data.status === 'fired') {
            // 発火成功。応答自体はWS経由でチャットに流れてくる
            flash(T.moonbeat_fired || 'Fired');
        } else if (data.status === 'skipped') {
            flash(T.moonbeat_skipped_sleep || 'Sleeping — not fired');
        } else if (data.status === 'busy') {
            flash(T.moonbeat_skipped_busy || 'Busy — not fired');
        } else {
            flash(T.moonbeat_fire_failed || 'Could not fire');
        }
    } catch (e) {
        console.error('[Moonbeat] fire failed:', e);
        flash(T.moonbeat_fire_error || 'Fire failed');
    }
}

// --- workspace フォルダをエクスプローラで開く ---
async function openWorkspaceFolder() {
    const btn = document.getElementById('openWorkspace');
    if (!btn || btn.disabled) return;
    const original = btn.textContent;
    btn.disabled = true;
    // ステータスを一時表示してから元のラベルに戻す
    const flash = (label, ms = 2500) => {
        btn.textContent = label;
        setTimeout(() => {
            btn.textContent = original;
            btn.disabled = false;
        }, ms);
    };
    try {
        const res = await fetch('/api/open-workspace', { method: 'POST' });
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        flash(T.workspace_opened || 'Opened');
    } catch (e) {
        console.error('[Workspace] open failed:', e);
        flash(T.workspace_open_failed || 'Could not open');
    }
}

// --- 記憶パネル更新 ---
async function refreshMemory() {
    try {
        const res = await fetch('/api/memory');
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const data = await res.json();

        // トークン使用状況をAPIから復元（リロード後もWebSocket受信を待たずに表示する）
        if (data.token_usage) {
            lastTokenUsage = data.token_usage;
            updateTokenDisplay(data.token_usage);
        }

        let lettersHtml = '';
        lettersHtml += `<div class="memory-section"><div class="memory-section-title">💌 ${T.memory_letter_title || 'Letter to Self'}</div>`;
        lettersHtml += data.letter ? `<div class="memory-content">${escapeHtml(data.letter)}</div>` : `<div class="memory-content"><span class="empty">${T.memory_file_not_found || 'File not found'}</span></div>`;
        lettersHtml += '</div>';


        memoryLetters.innerHTML = lettersHtml;

        let notesHtml = '';
        if (data.notes && data.notes.length > 0) {
            // 最新の1件（今日の分）のみ表示
            const note = data.notes[0];
            notesHtml += '<div class="memory-section">';
            notesHtml += `<div class="memory-file-name">${escapeHtml(note.name)}</div>`;
            notesHtml += `<div class="memory-content">${escapeHtml(note.content)}</div>`;
            notesHtml += '</div>';
        } else {
            notesHtml += `<div class="memory-content"><span class="empty">${T.memory_not_written || 'Not written yet'}</span></div>`;
        }
        memoryNotes.innerHTML = notesHtml;
        // 今日の活動
        const memoryToday = document.getElementById('memoryToday');
        if (memoryToday) {
            memoryToday.innerHTML = data.today
                ? `<div class="memory-content">${escapeHtml(data.today)}</div>`
                : `<div class="memory-content"><span class="empty">${T.memory_no_records || 'No records yet'}</span></div>`;
        }

        // 好悪
        const memoryPrefs = document.getElementById('memoryPrefs');
        if (memoryPrefs) {
            memoryPrefs.innerHTML = data.preferences
                ? `<div class="memory-content">${escapeHtml(data.preferences)}</div>`
                : `<div class="memory-content"><span class="empty">${T.memory_no_records || 'No records yet'}</span></div>`;
        }

        // 体調
        const memoryVital = document.getElementById('memoryVital');
        if (memoryVital) {
            if (data.vital) {
                const v = data.vital;
                const staminaMax = v.config?.stamina_max ?? 500;
                const energyMax = v.config?.energy_max ?? 50;
                let html = `<div class="memory-content">`;
                // 体調カード: アイコン｜ラベル｜バー｜数値 の横一列
                // 体力＝緑 / エナジー＝青 / メンタル＝黄、各25%未満で赤
                html += `<div class="vital-section-title">${uiIcon('star')}${T.vital_condition || 'Condition'}</div>`;
                html += `<div class="vital-card">`;
                html += renderVitalGauge(T.vital_stamina || 'Stamina', v.stamina, staminaMax, 'stamina', 'muscle');
                html += renderVitalGauge(T.vital_energy || 'Energy', v.energy, energyMax, 'energy', 'lightning');
                html += renderVitalGauge(T.vital_mental || 'Mental', v.mental, 100, 'mental', 'heart-stitch');
                html += `</div>`;

                // 気分: moodsae は活性状態ごとに感情価カラーのバー、moodphase は各軸を中立バー
                if (data.moodsae && Array.isArray(data.moodsae.active) && data.moodsae.active.length) {
                    html += `<div class="vital-section-title">${uiIcon('star')}${T.vital_mood || 'Mood'}</div>`;
                    html += `<div class="vital-card">`;
                    for (const s of data.moodsae.active) {
                        const label = T.html_lang === 'ja'
                            ? (s.name || s.state)
                            : s.state.replace(/\b\w/g, c => c.toUpperCase());
                        html += renderMoodGauge(label, s.state, s.activation);
                    }
                    html += `</div>`;
                } else if (data.moodphase) {
                    const mp = data.moodphase;
                    const axes = [
                        {key: 'h', name: T.mood_hedonic || 'Hedonic'},
                        {key: 's', name: T.mood_social || 'Social'},
                        {key: 't', name: T.mood_tension || 'Tension'},
                        {key: 'a', name: T.mood_absorption || 'Absorption'},
                    ];
                    html += `<div class="vital-section-title">${uiIcon('star')}${T.vital_mood || 'Mood'}</div>`;
                    html += `<div class="vital-card">`;
                    for (const ax of axes) {
                        if (typeof mp[ax.key] === 'number') {
                            // moodphase は 1〜7 スケール。中立フォールバック色（is-mood）で割合表示
                            html += renderVitalGauge(ax.name, mp[ax.key], 7, 'mood');
                        }
                    }
                    html += `</div>`;
                }

                // Sleep / 生活行動
                if (data.life_action) {
                    const la = data.life_action;
                    const actionNames = {
                        sleep: T.action_sleeping || 'Sleeping',
                        nap: T.action_napping || 'Napping',
                        idle: T.action_resting || 'Resting',
                        nothing: T.action_idle || 'Idle',
                    };
                    const name = actionNames[la.action] || la.action;
                    html += `<div class="vital-section-title">${uiIcon('star')}${T.vital_life_action || 'Activity'}</div>`;
                    html += `<div class="vital-card vital-card-text"><div>${name} (${T.vital_until || 'until'} ${la.until || '?'})</div></div>`;
                }

                // 欲求とコンテキストはペアにして、手帳パネルが広い時は横並びにする
                // （幅の判定は dashboard.css のコンテナクエリ .vital-pair が行う）
                let desireHtml = '';
                if (data.desire?.state) {
                    const cfg = data.desire.config || {};
                    desireHtml += `<div class="vital-section-title">${uiIcon('star')}${T.vital_desire || 'Desire'}</div>`;
                    desireHtml += `<div class="vital-card vital-card-text">`;
                    for (const [key, st] of Object.entries(data.desire.state)) {
                        const name = cfg[key]?.display_name || key;
                        const min = cfg[key]?.min ?? -10;
                        const max = cfg[key]?.max ?? 10;
                        desireHtml += `<div>${name}: ${st.value} (${min}〜${max})</div>`;
                    }
                    desireHtml += `</div>`;
                }

                // コンテキスト情報
                let contextHtml = '';
                if (lastTokenUsage) {
                    const tu = lastTokenUsage;
                    const used = tu.used?.toLocaleString() ?? '?';
                    const max = tu.max?.toLocaleString() ?? '?';
                    const pct = Math.round((tu.ratio ?? 0) * 100);
                    const l0 = (tu.layer0 ?? 0).toLocaleString();
                    const l1 = (tu.layer1 ?? 0).toLocaleString();
                    const l2 = (tu.layer2 ?? 0).toLocaleString();
                    const ct = data.context_tokens;
                    const sys = ct ? ct.system?.toLocaleString() : '?';
                    const tools = ct ? ct.tools?.toLocaleString() : '?';
                    // ct（System/Tools内訳）が未取得の間は正確なRawを算出できないため '?' を表示する
                    const rawOnly = ct ? (tu.used - (tu.layer0 ?? 0) - (tu.layer1 ?? 0) - (tu.layer2 ?? 0) - (ct.system ?? 0) - (ct.tools ?? 0)).toLocaleString() : '?';
                    const l0turns = tu.layer0_turns ?? 0;
                    const rawTurns = tu.raw_turns ?? 0;
                    contextHtml += `<div class="vital-section-title">${uiIcon('star')}${T.vital_context || 'Context'}</div>`;
                    contextHtml += `<div class="vital-card vital-card-text">`;
                    contextHtml += `<div>${uiIcon('yuzu')}${T.vital_total || 'Total'}: ${used} / ${max} (${pct}%)</div>`;
                    contextHtml += `<div>${uiIcon('gear')}System: ${sys}</div>`;
                    contextHtml += `<div>${uiIcon('wrench')}Tools: ${tools}</div>`;
                    contextHtml += `<div>${uiIcon('raw')}Raw: ${rawOnly} (${rawTurns} ${T.vital_turns || 'turns'})</div>`;
                    contextHtml += `<div>${uiIcon('layer0')}Layer0: ${l0} (${l0turns} ${T.vital_turns || 'turns'})</div>`;
                    contextHtml += `<div>${uiIcon('layer1')}Layer1: ${l1}</div>`;
                    contextHtml += `<div>${uiIcon('layer2')}Layer2: ${l2}</div>`;
                    contextHtml += `</div>`;
                }

                if (desireHtml || contextHtml) {
                    html += `<div class="vital-pair">`;
                    if (contextHtml) html += `<div class="vital-pair-item">${contextHtml}</div>`;
                    if (desireHtml) html += `<div class="vital-pair-item">${desireHtml}</div>`;
                    html += `</div>`;
                }

                html += `<div class="vital-footnote">${T.vital_last_updated || 'Last updated'}: ${escapeHtml(v.last_updated || (T.vital_unknown || 'Unknown'))}</div>`;
                html += `</div>`;
                memoryVital.innerHTML = html;
            } else {
                memoryVital.innerHTML = `<div class="memory-content"><span class="empty">${T.memory_no_data || 'No data'}</span></div>`;
            }
        }

        // スケジュール
        const memorySchedule = document.getElementById('memorySchedule');
        if (memorySchedule) {
            if (data.schedules && data.schedules.length > 0) {
                let schedHtml = '<div class="memory-content">';
                data.schedules.forEach(s => {
                    // schedule_type ごとに表示すべきフィールドが異なる:
                    //   daily    → time ("HH:MM")
                    //   once     → datetime ("YYYY-MM-DD HH:MM")
                    //   interval → interval_minutes / start_time / end_time
                    const typeLabel = {
                        'daily': T.schedule_type_daily || 'Daily',
                        'once': T.schedule_type_once || 'Once',
                        'interval': T.schedule_type_interval || 'Interval',
                    }[s.schedule_type] || s.schedule_type || '';
                    let when = '';
                    if (s.schedule_type === 'once') {
                        when = s.datetime || '';
                    } else if (s.schedule_type === 'interval') {
                        const every = (T.schedule_interval_every || 'every {n} min').replace('{n}', s.interval_minutes ?? '?');
                        const parts = [every];
                        if (s.start_time && s.end_time) {
                            parts.push((T.schedule_interval_window || '{start}-{end}')
                                .replace('{start}', s.start_time)
                                .replace('{end}', s.end_time));
                        }
                        when = parts.join(' ');
                    } else {
                        when = s.time || '';
                    }
                    const name = escapeHtml(s.name || (T.schedule_unnamed || 'Unnamed'));
                    const typeText = escapeHtml(typeLabel);
                    const whenText = escapeHtml(when);
                    const disabledBadge = s.enabled === false
                        ? ` <span class="schedule-badge-disabled">${escapeHtml(T.schedule_disabled || 'Disabled')}</span>`
                        : '';
                    let item = `<div class="schedule-item">`;
                    item += `<div class="schedule-title">⏰ <strong>${name}</strong>${disabledBadge}</div>`;
                    item += `<div class="schedule-row">${typeText}${whenText ? ' ' + whenText : ''}</div>`;
                    if (s.task_file) {
                        item += `<div class="schedule-row schedule-sub">${escapeHtml(T.schedule_task_file || 'Task file')}: ${escapeHtml(s.task_file)}</div>`;
                    }
                    if (s.last_run) {
                        item += `<div class="schedule-row schedule-sub">${escapeHtml(T.schedule_last_run || 'Last run')}: ${escapeHtml(s.last_run)}</div>`;
                    }
                    item += `</div>`;
                    schedHtml += item;
                });
                schedHtml += '</div>';
                memorySchedule.innerHTML = schedHtml;
            } else {
                memorySchedule.innerHTML = `<div class="memory-content"><span class="empty">${T.schedule_none || 'No schedules'}</span></div>`;
            }
        }


    } catch (e) {
        memoryLetters.innerHTML = `<span class="empty">${T.memory_load_error || 'Load error'}: ${escapeHtml(e.message)}</span>`;
    }
}

function escapeHtml(text) {
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
}

// === バイタル/気分ゲージ ===
// 気分（moodsae）の感情価マッピング。valence値ではなく手動分類で
// ポジ(pink)/中立(yellow)/ネガ(purple)に割り当てる。キーは英語stateラベル。
const MOOD_VALENCE = {
    // ポジティブ（ピンク）16
    peacefulness: 'pos', relaxation: 'pos', friendliness: 'pos', satisfaction: 'pos',
    affection: 'pos', playfulness: 'pos', inspiration: 'pos', ecstasy: 'pos',
    awe: 'pos', curiosity: 'pos', intrigue: 'pos', belief: 'pos',
    exaltation: 'pos', anticipation: 'pos', earnestness: 'pos', imagination: 'pos',
    // 中立（黄）21
    patience: 'neu', consciousness: 'neu', awareness: 'neu', attention: 'neu',
    reason: 'neu', cognition: 'neu', contemplation: 'neu', thought: 'neu',
    transcendence: 'neu', decision: 'neu', seriousness: 'neu', planning: 'neu',
    opinion: 'neu', objectivity: 'neu', sleepiness: 'neu', trance: 'neu',
    'self-consciousness': 'neu', judgment: 'neu', dominance: 'neu', desire: 'neu', lust: 'neu',
    // ネガティブ（紫）23
    disarray: 'neg', subordination: 'neg', insanity: 'neg', drunkenness: 'neg',
    embarrassment: 'neg', disgust: 'neg', stupor: 'neg', distrust: 'neg',
    worry: 'neg', craziness: 'neg', alarm: 'neg', lethargy: 'neg',
    agitation: 'neg', nervousness: 'neg', fatigue: 'neg', laziness: 'neg',
    'self-pity': 'neg', weariness: 'neg', uneasiness: 'neg', exhaustion: 'neg',
    skepticism: 'neg', pity: 'neg', pensiveness: 'neg',
};

// 体力/エナジー/メンタル用ゲージ行（アイコン｜ラベル｜バー｜数値 の横一列）。
// kind: 'stamina'|'energy'|'mental'|'mood'。割合が25%未満なら赤（is-low）。
// icon は icons/ のSVGファイル名（省略時はアイコン無し＝ラベルのみの行）。
function renderVitalGauge(label, value, max, kind, icon) {
    const v = (typeof value === 'number') ? value : 0;
    const ratio = max > 0 ? Math.max(0, Math.min(1, v / max)) : 0;
    const pct = (ratio * 100).toFixed(0);
    const low = ratio < 0.25 ? ' is-low' : '';
    // pre-wrap な .memory-content 直下に裸の空白が残らないよう trim() して返す
    return `
      <div class="vital-row">
        ${icon ? uiIcon(icon) : ''}<span class="vital-row-label">${escapeHtml(label)}</span>
        <div class="vital-gauge-track">
          <div class="vital-gauge-fill is-${kind}${low}" style="width:${pct}%"></div>
        </div>
        <span class="vital-row-value">${value ?? '?'} / ${max}</span>
      </div>`.trim();
}

// 気分（活性状態）ゲージ行。幅は activation(0..1)、色は感情価マッピングで固定。
// 行頭に感情価カラーの play アイコン（pos=pink / neu=yellow / neg=purple）を置く。
const MOOD_PLAY_ICON = { pos: 'play-pink', neu: 'play-yellow', neg: 'play-purple' };
function renderMoodGauge(label, stateId, activation) {
    const cls = MOOD_VALENCE[stateId] || 'neu';
    const a = (typeof activation === 'number') ? activation : 0;
    const pct = (Math.max(0, Math.min(1, a)) * 100).toFixed(0);
    return `
      <div class="vital-row">
        ${uiIcon(MOOD_PLAY_ICON[cls])}<span class="vital-row-label">${escapeHtml(label)}</span>
        <div class="vital-gauge-track">
          <div class="vital-gauge-fill mood-${cls}" style="width:${pct}%"></div>
        </div>
        <span class="vital-row-value">${a.toFixed(2)}</span>
      </div>`.trim();
}

// --- イベントリスナー ---
sendButton.addEventListener('click', sendMessage);
stopButton.addEventListener('click', cancelResponse);
compressButton.addEventListener('click', requestCompress);
moonbeatToggle?.addEventListener('click', toggleMoonbeat);
moonbeatFire?.addEventListener('click', fireMoonbeat);
messageInput.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendMessage(); }
});
messageInput.addEventListener('input', () => {
    messageInput.style.height = 'auto';
    messageInput.style.height = Math.min(messageInput.scrollHeight, 80) + 'px';
});
document.getElementById('memoryRefresh')?.addEventListener('click', refreshMemory);

// --- 起動 ---
initProfile().then(() => {
    enableResize();  // リサイズ有効化
    setupRightCollapse(); // 右パネルの開閉（enableResize後＝loadLayout後に呼ぶ）
    // 履歴はサーバー側 context_state.json を真実として WS 接続時に受け取る。
    // localStorage は中断(キャンセル)時の再描画キャッシュとしてのみ使う。
    connect();       // WebSocket接続
    refreshMemory(); // 記憶読み込み
    loadMoonbeatState(); // Moonbeatトグルの現在状態を反映
});
// タブ切り替え
document.querySelectorAll('.tab-button[data-tab]').forEach(btn => {
    btn.addEventListener('click', () => {
        document.querySelectorAll('.tab-button[data-tab]').forEach(b => b.classList.remove('active'));
        document.querySelectorAll('.tab-content').forEach(c => c.classList.remove('active'));
        btn.classList.add('active');
        document.getElementById(btn.dataset.tab).classList.add('active');
    });
});

// --- 右パネル（記憶コンテナ）の開閉（折りたたみ） ---
// 折りたたみ時はリサイザーと右コンテナを隠し、チャットを全幅にする。
// 展開時の列幅は壊さないよう、折りたたみ前の幅を別キーに退避しておく。
function setupRightCollapse() {
    const mainLayout = document.getElementById('mainLayout');
    const collapseBtn = document.getElementById('memoryCollapse');
    const reopenBtn = document.getElementById('rightReopen');
    if (!mainLayout || !collapseBtn || !reopenBtn) return;

    const STATE_KEY = 'yuzuki_dashboard_right_collapsed';
    const COLS_KEY = 'yuzuki_dashboard_right_lastcols';
    const COLLAPSED_COLS = '1fr 0 0';

    // スマホ（横幅700px以下）では手帳を全画面オーバーレイで出す。
    // CSS側と同じ閾値。開閉状態をPCと共有すると紛らわしいので、
    // スマホでは「最初は必ず畳む＝チャット主役」で開始し、保存もしない。
    const isMobile = () => window.matchMedia('(max-width: 700px)').matches;

    function applyCollapsed(collapsed) {
        if (collapsed) {
            // 直前の（折りたたみでない）列幅を退避してから畳む
            const cur = mainLayout.style.gridTemplateColumns;
            if (cur && cur !== COLLAPSED_COLS) {
                localStorage.setItem(COLS_KEY, cur);
            }
            mainLayout.style.gridTemplateColumns = COLLAPSED_COLS;
            mainLayout.classList.add('right-collapsed');
            document.body.classList.add('right-panel-collapsed');
            collapseBtn.setAttribute('aria-expanded', 'false');
            reopenBtn.setAttribute('aria-hidden', 'false');
        } else {
            // 退避した幅、無ければ初期値に戻す
            const last = localStorage.getItem(COLS_KEY) || '1fr 6px 1fr';
            mainLayout.style.gridTemplateColumns = last;
            mainLayout.classList.remove('right-collapsed');
            document.body.classList.remove('right-panel-collapsed');
            collapseBtn.setAttribute('aria-expanded', 'true');
            reopenBtn.setAttribute('aria-hidden', 'true');
        }
    }

    function setCollapsed(collapsed) {
        applyCollapsed(collapsed);
        // スマホでの開閉はその場限り。PCの保存状態に書き込まない。
        if (!isMobile()) {
            localStorage.setItem(STATE_KEY, collapsed ? '1' : '0');
        }
    }

    collapseBtn.addEventListener('click', () => setCollapsed(true));
    reopenBtn.addEventListener('click', () => setCollapsed(false));

    // 起動時：スマホは必ず畳んでチャットから。PCは前回の開閉状態を復元
    // （loadLayout後なので列幅を上書きできる）。
    applyCollapsed(isMobile() ? true : localStorage.getItem(STATE_KEY) === '1');
}

// --- ヘッダー2段目（折りたたみドロワー）の開閉 ---
(function setupHeaderDrawer() {
    const toggle = document.getElementById('headerDrawerToggle');
    const drawer = document.getElementById('headerDrawer');
    if (!toggle || !drawer) return;

    const STORAGE_KEY = 'yuzuki_header_drawer_open';

    // 開閉状態を反映する（保存はしない）
    function setOpen(open) {
        drawer.classList.toggle('open', open);
        toggle.classList.toggle('open', open);
        toggle.setAttribute('aria-expanded', open ? 'true' : 'false');
    }

    // 起動時：前回の開閉状態を localStorage から復元
    setOpen(localStorage.getItem(STORAGE_KEY) === '1');

    // クリックでトグルし、状態を保存
    toggle.addEventListener('click', () => {
        const open = !drawer.classList.contains('open');
        setOpen(open);
        localStorage.setItem(STORAGE_KEY, open ? '1' : '0');
    });
})();