// web/static/chat_history.js
/**
 * チャット履歴のSPA化に向けた共通状態管理モジュール
 * 画面遷移時にsessionStorageを利用して履歴を保持・復元する役割を担います。
 */

class ChatHistoryManager {
    constructor(storageKey = 'yuzuki_chat_history') {
        this.storageKey = storageKey;
        this.history = JSON.parse(localStorage.getItem(this.storageKey) || '[]');
        this.isCancelled = false;
        // バルクモード中は save() を保留する（履歴一括復元時の localStorage 書き込み回数を1回に抑える）
        this.bulkMode = false;
    }

    save() {
        if (this.bulkMode) return;
        if (this.history.length > 500) {
            this.history = this.history.slice(this.history.length - 500);
        }
        localStorage.setItem(this.storageKey, JSON.stringify(this.history));
    }

    setBulkMode(enabled) {
        this.bulkMode = !!enabled;
        if (!enabled) this.save();
    }

    addMessage(role, content, time, isMoonbeat = false) {
        this.history.push({ type: 'message', role, content, time, isMoonbeat });
        this.save();
    }

    addToolLog(content) {
        this.history.push({ type: 'tool', content });
        this.save();
    }

    addSystemLog(content) {
        this.history.push({ type: 'system', content });
        this.save();
    }

    // ダッシュボード等での汎用ログ用
    addDashboardLog(logType, content) {
        this.history.push({ type: 'log', logType, content });
        this.save();
    }

    removeLastAssistant() {
        for (let i = this.history.length - 1; i >= 0; i--) {
            if (this.history[i].type === 'message' && this.history[i].role === 'assistant') {
                this.history.splice(i, 1);
                this.save();
                return;
            }
        }
    }

    /**
     * 保存されている履歴を各画面のUIに復元する
     * @param {Object} handlers 各ログタイプに応じたUI描画関数群
     */
    restore(handlers) {
        this.history.forEach(item => {
            try {
                if (item.type === 'message' && handlers.onMessage) {
                    handlers.onMessage(item.role, item.content, item.time, false, item.isMoonbeat || false);
                } else if (item.type === 'tool' && handlers.onTool) {
                    handlers.onTool(item.content, false);
                } else if (item.type === 'system' && handlers.onSystem) {
                    handlers.onSystem(item.content, false);
                } else if (item.type === 'log' && handlers.onLog) {
                    handlers.onLog(item.logType, item.content, false);
                }
            } catch (e) {
                console.error("履歴の復元中にエラーが発生しました", e, item);
            }
        });
    }

    clear() {
        this.history = [];
        this.save();
    }

    /**
     * 直近のやり取り（最後の user メッセージ以降）を履歴から削除する
     */
    popLastExchange() {
        // 後ろから走査して、最後の user メッセージのインデックスを見つける
        let lastUserIdx = -1;
        for (let i = this.history.length - 1; i >= 0; i--) {
            if (this.history[i].type === 'message' && this.history[i].role === 'user') {
                lastUserIdx = i;
                break;
            }
        }

        if (lastUserIdx !== -1) {
            // 最後の user メッセージを含めてそれ以降を削除
            this.history = this.history.slice(0, lastUserIdx);
            this.save();
        }
    }

    /**
     * 送信中（生成中）の状態を一括制御する
     * @param {boolean} isLoading 
     */
    setLoading(isLoading) {
        const sendBtn = document.getElementById('sendButton');
        const stopBtn = document.getElementById('stopButton');
        const typingInd = document.getElementById('typingIndicator');
        const input = document.getElementById('messageInput');

        if (isLoading) {
            if (sendBtn) sendBtn.style.display = 'none';
            if (stopBtn) stopBtn.classList.add('visible');
            if (typingInd) typingInd.classList.add('visible');
            if (input) input.disabled = true;
        } else {
            if (sendBtn) {
                sendBtn.style.display = 'flex';
                sendBtn.disabled = false;
            }
            if (stopBtn) stopBtn.classList.remove('visible');
            if (typingInd) typingInd.classList.remove('visible');
            if (input) {
                input.disabled = false;
                input.focus();
            }
        }
    }
}

// グローバルにインスタンスを公開
window.chatHistoryManager = new ChatHistoryManager();
