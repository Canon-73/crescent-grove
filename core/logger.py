# core/logger.py
"""
会話ログ管理モジュール

役割:
    会話のログを2種類保存する。
    
    1. 全ログ（full log）:
       ツール呼び出し・結果を含む全てのやりとり。デバッグ・分析用。
       JSON Lines形式（1行1イベント）で保存。
    
    2. 会話ログ（chat log）:
       ユーザーの発言とエージェントの応答だけを抽出したもの。
       RAG検索や読み返し用。markdown形式で保存。
       「ユーザーの話しかけ → エージェントの回答」を1単位とする。

ファイル名:
    日付ごとにファイルを分ける（JST基準）。
    - full: 2026-02-18_full.jsonl
    - chat: 2026-02-18_chat.md

会話ログ（markdown）のフォーマット:
    検索しやすく、人間が読み返しやすい形式。
    各やりとりにタイムスタンプとセパレータを付与。

    ---
    **[2026-02-18 15:30:42]**
    
    **ご主人様:** おはよう
    
    **Assistant:** ご主人様、おはようございます！今日もご主人様とお話できるのを楽しみにしていました。
    
    ---
"""

import json
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional


# 日本時間
JST = timezone(timedelta(hours=9))


class ConversationLogger:
    """会話ログを管理するクラス"""

    # 不正なディレクトリ指定（空文字・None・"."など）が来た時のフォールバック先
    DEFAULT_FULL_LOG_DIR = "workspace/logs/full"
    DEFAULT_CHAT_LOG_DIR = "workspace/logs/chat"

    def __init__(self, full_log_dir: str, chat_log_dir: str, agent_name: str = "Assistant"):
        """
        Args:
            full_log_dir: 全ログの保存ディレクトリ
            chat_log_dir: 会話ログの保存ディレクトリ
            agent_name: エージェント名（ログに記録する名前）
        """
        self.full_log_dir = Path(self._sanitize_dir(full_log_dir, self.DEFAULT_FULL_LOG_DIR, "full_log_dir"))
        self.chat_log_dir = Path(self._sanitize_dir(chat_log_dir, self.DEFAULT_CHAT_LOG_DIR, "chat_log_dir"))
        self.agent_name = agent_name

        # ディレクトリがなければ作成
        self.full_log_dir.mkdir(parents=True, exist_ok=True)
        self.chat_log_dir.mkdir(parents=True, exist_ok=True)

        # 起動時に書き込みテストを実行（権限不足・パス不正などを早期検出）
        self._startup_write_check()

    def _startup_write_check(self):
        """起動時に各ログディレクトリへ実際に書き込めるかを検証する。
        失敗してもプロセスは止めず、stderrに大きく警告を出す。"""
        import sys
        for label, d in (("full_log_dir", self.full_log_dir), ("chat_log_dir", self.chat_log_dir)):
            try:
                probe = d / ".write_check"
                probe.write_text("ok", encoding="utf-8")
                probe.unlink()
            except Exception as e:
                print(
                    "\n" + "!" * 70 + "\n"
                    f"[ConversationLogger] 重大警告: {label} ({d}) に書き込めません: {e}\n"
                    f"  → このセッションのログは保存されない可能性があります。\n"
                    + "!" * 70 + "\n",
                    file=sys.stderr,
                )

    @staticmethod
    def _sanitize_dir(value: Optional[str], default: str, label: str) -> str:
        """
        ログディレクトリ指定をサニタイズする。
        空文字・None・"."・空白のみ等の不正値が来たら、デフォルトにフォールバックする。
        （設定欠落でログがルート直下に保存される事故を防ぐための最終防衛線）
        """
        if value is None or not str(value).strip() or str(value).strip() in (".", "./"):
            import sys
            print(
                f"[ConversationLogger] 警告: {label}が不正な値({value!r})です。"
                f"デフォルト '{default}' にフォールバックします。",
                file=sys.stderr,
            )
            return default
        return value

    def _now_jst(self) -> datetime:
        """現在の日本時間を返す"""
        return datetime.now(JST)

    def _today_str(self) -> str:
        """今日の日付文字列を返す（YYYY-MM-DD）"""
        return self._now_jst().strftime("%Y-%m-%d")

    def _timestamp_str(self) -> str:
        """現在時刻の文字列を返す（YYYY-MM-DD HH:MM:SS）"""
        return self._now_jst().strftime("%Y-%m-%d %H:%M:%S")

    def log_full_event(self, event_type: str, data: dict):
        """
        全ログにイベントを1行追記する（JSON Lines形式）。

        Args:
            event_type: イベント種別
                "user_message" - ユーザー発言
                "assistant_message" - 柚月の応答
                "tool_call" - ツール呼び出し
                "tool_result" - ツール実行結果
                "error" - エラー
            data: イベントデータ
        """
        filepath = self.full_log_dir / f"{self._today_str()}_full.jsonl"

        entry = {
            "timestamp": self._timestamp_str(),
            "type": event_type,
            **data,
        }
        line = json.dumps(entry, ensure_ascii=False) + "\n"

        try:
            with open(filepath, "a", encoding="utf-8") as f:
                f.write(line)
        except Exception as e:
            # 書き込み失敗時は緊急フォールバック先へ退避し、stderrに警告。
            # 例外は上に伝播させない（ログ失敗で本体処理を止めないため）。
            self._write_emergency(line, error=e, original_path=filepath)

    def _write_emergency(self, line: str, error: Exception, original_path: Path):
        """通常の書き込みが失敗した時の緊急退避。複数のフォールバック先を順に試す。"""
        import sys, tempfile
        candidates = [
            self.full_log_dir / "_emergency.log",
            Path(self.DEFAULT_FULL_LOG_DIR) / "_emergency.log",
            Path(tempfile.gettempdir()) / "agent_log_emergency.log",
        ]
        for fallback in candidates:
            try:
                fallback.parent.mkdir(parents=True, exist_ok=True)
                with open(fallback, "a", encoding="utf-8") as f:
                    f.write(line)
                print(
                    f"[ConversationLogger] 警告: {original_path} への書き込みに失敗 ({error})。"
                    f"{fallback} に退避しました。",
                    file=sys.stderr,
                )
                return
            except Exception:
                continue
        # 全フォールバック失敗：ここまで来ると本当にどこにも書けない
        print(
            f"[ConversationLogger] 致命的: {original_path} への書き込みも全フォールバックも失敗 ({error})。"
            f"このイベントは失われました: {line[:200]}",
            file=sys.stderr,
        )

    async def log_chat_exchange(self, user_message: str, assistant_message: str):
        """
        会話ログに1単位のやりとりを追記する（markdown形式）。
        「ユーザーの話しかけ → エージェントの回答」で1単位。
        さらに、RAGDBが有効な場合はLLMで感情評価を行い、logsコレクションに自動登録する。

        Args:
            user_message: ユーザーの発言
            assistant_message: 柚月の応答
            llm: LLMプロバイダー（感情評価用）
            rag_db: RAGデータベース
        """
        filepath = self.chat_log_dir / f"{self._today_str()}_chat.md"

        # ファイルが新規の場合、ヘッダーを付ける
        is_new = not filepath.exists()

        with open(filepath, "a", encoding="utf-8") as f:
            if is_new:
                f.write(f"# 会話ログ {self._today_str()}\n\n")

            f.write(f"---\n")
            f.write(f"**[{self._timestamp_str()}]**\n\n")
            f.write(f"**user:** {user_message}\n\n")
            f.write(f"**assistant:** {assistant_message}\n\n")
            

    def log_user_message(self, content: str):
        """ユーザー発言を全ログに記録"""
        self.log_full_event("user_message", {"content": content})

    def log_assistant_message(self, content: str):
        """エージェントの応答を全ログに記録"""
        self.log_full_event("assistant_message", {"content": content})

    def log_tool_call(self, tool_name: str, arguments: dict):
        """ツール呼び出しを全ログに記録"""
        self.log_full_event("tool_call", {
            "tool": tool_name,
            "arguments": arguments,
        })

    def log_tool_result(self, tool_name: str, result: str):
        """ツール実行結果を全ログに記録"""
        self.log_full_event("tool_result", {
            "tool": tool_name,
            "result": result,
        })