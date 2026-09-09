import os
from pathlib import Path
from cryptography.fernet import Fernet
from typing import List

class SecretManager:
    """エージェント専用の暗号化秘密ファイルシステムを管理するクラス"""

    def __init__(self, workspace_path: str, key_path: str):
        self.workspace_path = Path(workspace_path)
        self.secret_dir = self.workspace_path / "secret"
        self.key_path = Path(key_path)

        # secretディレクトリの確保
        self.secret_dir.mkdir(parents=True, exist_ok=True)

        # 暗号化鍵の読み込みまたは生成
        self.fernet = self._initialize_key()

    def _initialize_key(self) -> Fernet:
        """鍵ファイルから読み込むか、なければ新規生成する"""
        if self.key_path.exists():
            key = self.key_path.read_bytes()
        else:
            key = Fernet.generate_key()
            self.key_path.write_bytes(key)
        return Fernet(key)

    def encrypt(self, plaintext: str) -> bytes:
        """平文を暗号化する"""
        return self.fernet.encrypt(plaintext.encode("utf-8"))

    def decrypt(self, encrypted_data: bytes) -> str:
        """暗号化データを復号して平文を返す"""
        return self.fernet.decrypt(encrypted_data).decode("utf-8")

    def _get_file_path(self, filename: str) -> Path:
        """指定されたファイル名の拡張子を .enc にして安全なパスを返す"""
        # セキュリティ目的：不要なディレクトリトラバーサルを防ぐ
        safe_name = Path(filename).name
        if not safe_name.endswith(".enc"):
            safe_name += ".enc"
        return self.secret_dir / safe_name

    def read_secret(self, filename: str) -> str:
        """暗号化ファイルを読み込んで復号する"""
        file_path = self._get_file_path(filename)
        if not file_path.exists():
            return f"Error: 秘密ファイル '{filename}' は存在しません。"

        try:
            encrypted_data = file_path.read_bytes()
            plaintext = self.decrypt(encrypted_data)
            return plaintext
        except Exception as e:
            return f"Error: 秘密ファイルの読み込みまたは復号に失敗しました: {e}"

    def write_secret(self, filename: str, content: str) -> str:
        """内容を暗号化して新規ファイルに書き込む"""
        file_path = self._get_file_path(filename)
        if file_path.exists():
            return f"Error: 秘密ファイル '{filename}' は既に存在します。"

        try:
            encrypted_data = self.encrypt(content)
            file_path.write_bytes(encrypted_data)
            return f"Success: 秘密ファイル '{filename}' を作成し、安全に保存しました。"
        except Exception as e:
            return f"Error: 秘密ファイルの作成に失敗しました: {e}"

    def edit_secret(self, filename: str, append_content: str) -> str:
        """既存の暗号化ファイルを読み込み、内容を追記して再暗号化・保存する"""
        file_path = self._get_file_path(filename)
        if not file_path.exists():
            return f"Error: 秘密ファイル '{filename}' は存在しません。"

        try:
            # 既存内容の読み込み
            existing_encrypted = file_path.read_bytes()
            existing_plaintext = self.decrypt(existing_encrypted)

            # 追記して再暗号化
            new_plaintext = existing_plaintext + append_content
            new_encrypted = self.encrypt(new_plaintext)

            # 上書き保存
            file_path.write_bytes(new_encrypted)
            return f"Success: 秘密ファイル '{filename}' に正常に追記し、保存しました。"
        except Exception as e:
            return f"Error: 秘密ファイルの編集に失敗しました: {e}"

    def list_secrets(self) -> str:
        """secretディレクトリ内のファイル一覧を返す"""
        try:
            secrets = [str(f.name)[:-4] if str(f.name).endswith('.enc') else str(f.name) 
                       for f in self.secret_dir.glob("*.enc") if f.is_file()]
            
            if not secrets:
                return "秘密ファイルはまだありません。"
            
            return "登録されている秘密ファイル一覧:\n- " + "\n- ".join(secrets)
        except Exception as e:
            return f"Error: 一覧の取得に失敗しました: {e}"
