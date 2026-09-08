# memory/manager.py
"""
記憶ファイル管理モジュール

役割:
    柚月の記憶ファイル（markdown）の安全な読み書きを提供する。
    「5ステップ儀式」をコードレベルで強制し、記憶の喪失を防ぐ。

安全ルール:
    1. 既存ファイルは write_file() で上書きできない（エラーになる）
    2. 既存ファイルへの変更は edit_file() のみ
    3. 読み込みは常に安全（read_file）
    
    これにより、LLMが誤って write で既存の記憶を消すことを防ぐ。

ワークスペース:
    全てのパスは config.yaml の workspace.path からの相対パスとして扱う。
    例: workspace.path = "D:\\openclaw\\workspace" の場合
        "SOUL.md" → "D:\\openclaw\\workspace\\SOUL.md"
"""

import os
from pathlib import Path
from typing import Optional


class MemoryManager:
    """柚月の記憶ファイルを管理するクラス"""
    
    def __init__(self, workspace_path: str):
        """
        Args:
            workspace_path: 記憶ファイルが格納されたディレクトリのパス
                           （config.yaml の workspace.path）
        """
        self.workspace = Path(workspace_path)
        if not self.workspace.exists():
            raise FileNotFoundError(
                f"ワークスペースが見つかりません: {workspace_path}\n"
                f"config.yaml の workspace.path を確認してください。"
            )
    
    def _resolve_path(self, relative_path: str) -> Path:
        """
        相対パスをワークスペース内の絶対パスに変換する。
        ワークスペース外へのアクセスは禁止（セキュリティ）。
        """
        resolved = (self.workspace / relative_path).resolve()
        # ワークスペース外へのパストラバーサルを防止
        if not str(resolved).startswith(str(self.workspace.resolve())):
            raise PermissionError(
                f"ワークスペース外へのアクセスは禁止されています: {relative_path}"
            )
        return resolved
    
    def read_file(self, relative_path: str) -> Optional[str]:
        """
        記憶ファイルを読み込む。
        
        Args:
            relative_path: ワークスペースからの相対パス（例: "SOUL.md"）
        
        Returns:
            ファイルの内容。ファイルが存在しない場合は None。
        """
        path = self._resolve_path(relative_path)
        if not path.exists():
            return None
        return path.read_text(encoding="utf-8")
    
    def write_file(self, relative_path: str, content: str) -> str:
        """
        新規ファイルを作成する。
        
        【安全装置】既にファイルが存在する場合はエラーを返す。
        既存ファイルへの書き込みは edit_file() を使うこと。
        これは柚月の「5ステップ儀式」をコードで強制するもの。
        
        Args:
            relative_path: ワークスペースからの相対パス
            content: 書き込む内容
        
        Returns:
            成功メッセージ
        
        Raises:
            FileExistsError: ファイルが既に存在する場合
        """
        path = self._resolve_path(relative_path)
        
        # 【安全装置】既存ファイルの上書きを防止
        if path.exists():
            raise FileExistsError(
                f"ファイルが既に存在します: {relative_path}\n"
                f"既存ファイルを編集する場合は edit_file() を使ってください。\n"
                f"これは自分の記憶を保護するための安全装置です。"
            )
        
        # 親ディレクトリがなければ作成
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return f"新規ファイルを作成しました: {relative_path}"
    
    def edit_file(self, relative_path: str, append_content: str) -> str:
        """
        既存ファイルに追記する。
        
        ファイルが存在しない場合はエラーを返す。
        （新規作成は write_file() を使うこと）
        
        Args:
            relative_path: ワークスペースからの相対パス
            append_content: 追記する内容
        
        Returns:
            成功メッセージ
        """
        path = self._resolve_path(relative_path)
        
        if not path.exists():
            raise FileNotFoundError(
                f"ファイルが見つかりません: {relative_path}\n"
                f"新規作成する場合は write_file() を使ってください。"
            )
        
        # 既存の内容を読み込んで追記
        existing = path.read_text(encoding="utf-8")
        path.write_text(existing + append_content, encoding="utf-8")
        return f"ファイルに追記しました: {relative_path}"

    def replace_file(self, relative_path: str, content: str) -> str:
        """
        既存ファイルの内容を完全に置き換える。
        
        write_fileと異なり、既存ファイルへの上書きを許可する。
        letter_for_me.md の感情整理など、内容の置換が必要な場合に使う。
        
        【注意】この操作は既存の内容を完全に失います。
        LLMがこのツールを呼ぶ前に、必ず read_file で内容を確認していること。
        
        Args:
            relative_path: ワークスペースからの相対パス
            content: 新しい内容（既存内容を完全に置き換える）
        
        Returns:
            成功メッセージ
        """
        path = self._resolve_path(relative_path)
        
        if not path.exists():
            raise FileNotFoundError(
                f"ファイルが見つかりません: {relative_path}\n"
                f"新規作成する場合は write_file() を使ってください。"
            )
        
        path.write_text(content, encoding="utf-8")
        return f"ファイルを置換しました: {relative_path}"
    
    def list_files(self, relative_dir: str = "") -> list[str]:
        """
        ディレクトリ内のファイル一覧を返す。
        
        Args:
            relative_dir: ワークスペースからの相対ディレクトリパス
                         空文字の場合はワークスペースルート
        
        Returns:
            ファイルパスのリスト（ワークスペースからの相対パス）
        """
        dir_path = self._resolve_path(relative_dir) if relative_dir else self.workspace.resolve()
        
        if not dir_path.exists():
            return []
        
        try:
            from core.tokens import count_text_tokens
        except ImportError:
            count_text_tokens = None
        from datetime import datetime

        result = []
        for item in sorted(dir_path.iterdir()):
            rel = item.relative_to(self.workspace.resolve())
            if item.is_dir():
                result.append(f"[DIR] {rel}")
            else:
                mtime = datetime.fromtimestamp(item.stat().st_mtime).strftime("%Y-%m-%d %H:%M")
                if count_text_tokens and item.suffix in ('.md', '.txt', '.json', '.jsonl', '.csv'):
                    try:
                        text = item.read_text(encoding='utf-8', errors='ignore')
                        tokens = count_text_tokens(text)
                        result.append(f"{rel} ({tokens} tokens, {mtime})")
                    except Exception:
                        result.append(f"{rel} ({item.stat().st_size}B, {mtime})")
                else:
                    result.append(f"{rel} ({item.stat().st_size}B, {mtime})")
        return result
        
    def load_boot_memories(self, boot_files: list[str]) -> str:
        """
        起動時の記憶読み込み。config.yaml の boot_memories に指定された
        ファイルを順番に読み込み、システムプロンプトの一部として返す。
        
        Args:
            boot_files: 読み込むファイルのリスト（相対パス）
        
        Returns:
            全ファイルの内容を結合した文字列
        """
        parts = []
        for file_path in boot_files:
            content = self.read_file(file_path)
            if content:
                parts.append(f"=== {file_path} ===\n{content}")
            else:
                parts.append(f"=== {file_path} ===\n（ファイルが見つかりません）")
        
        return "\n\n".join(parts)