import os
import re
from pathlib import Path
from typing import List, Dict, Any

from core.paths import env_path, config_yaml_path

# パスは core.paths 経由（data_root 基準）で「使用時」に解決する。
# 引数なし dev では data_root() が agent ディレクトリを返すため従来と同一パスになる。
# ※ モジュール定数として固定せず遅延解決にすることで、--data-root の反映余地を残す。
#   ただし load_env() は server.py の import 時（set_data_root より前）に呼ばれる点に注意（順序問題）。

class EnvManager:
    """環境変数およびAPIキーの管理クラス"""
    
    @staticmethod
    def load_env():
        """
        .envファイルを読み込み、os.environにセットする
        python-dotenvに依存せず標準機能で実装
        """
        ENV_FILE_PATH = env_path()
        if not ENV_FILE_PATH.exists():
            return

        with open(ENV_FILE_PATH, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                # 最初の '=' で分割
                if "=" in line:
                    key, val = line.split("=", 1)
                    key = key.strip()
                    val = val.strip()
                    # クォーテーションを一重剥がす
                    if (val.startswith('"') and val.endswith('"')) or (val.startswith("'") and val.endswith("'")):
                        val = val[1:-1]
                    os.environ[key] = val

    @staticmethod
    def get_all_keys() -> List[Dict[str, str]]:
        """CG_で始まる環境変数のリストを返す"""
        keys = []
        for key in os.environ:
            if key.startswith("CG_"):
                # 種別の判定
                key_type = "llm" if key.startswith("CG_LLM_") else "other"
                keys.append({"name": key, "type": key_type})
        return keys

    @staticmethod
    def set_key(name: str, value: str, is_llm: bool = False) -> bool:
        """
        環境変数をセットし、.envファイルに永続化する
        is_llmがTrueの場合はconfig.yamlも更新する
        """
        if not name.startswith("CG_"):
            return False
            
        # os.environにセット
        os.environ[name] = value
        
        # .envの更新（既存キーの置換または追記）
        EnvManager._update_env_file(name, value)
        
        # LLMキーの場合はconfig.yamlを置換
        if is_llm:
            EnvManager._update_config_llm_key(name)
            
        return True

    @staticmethod
    def delete_key(name: str) -> bool:
        """指定された環境変数を削除する"""
        if not name.startswith("CG_"):
            return False
            
        if name in os.environ:
            os.environ.pop(name, None)

        ENV_FILE_PATH = env_path()
        if not ENV_FILE_PATH.exists():
            return True

        # .envファイルから行を削除
        lines = []
        with open(ENV_FILE_PATH, "r", encoding="utf-8") as f:
            lines = f.readlines()

        with open(ENV_FILE_PATH, "w", encoding="utf-8") as f:
            for line in lines:
                striped = line.strip()
                if striped.startswith(f"{name}="):
                    continue  # この行をスキップ（削除）
                f.write(line)
                
        return True

    @staticmethod
    def _update_env_file(name: str, value: str):
        """内部用: .env ファイルの指定キーを更新または追記する"""
        ENV_FILE_PATH = env_path()
        lines = []
        if ENV_FILE_PATH.exists():
            with open(ENV_FILE_PATH, "r", encoding="utf-8") as f:
                lines = f.readlines()

        updated = False
        with open(ENV_FILE_PATH, "w", encoding="utf-8") as f:
            for line in lines:
                striped = line.strip()
                if striped.startswith(f"{name}="):
                    f.write(f"{name}={value}\n")
                    updated = True
                else:
                    f.write(line)
            
            if not updated:
                # ファイルが空でなく、末尾に改行がない場合は改行を追加
                if lines and not lines[-1].endswith("\n"):
                    f.write("\n")
                f.write(f"{name}={value}\n")

    @staticmethod
    def _update_config_llm_key(env_name: str):
        """
        内部用: config.yamlのコメントを保持したまま、
        llm.api_keyの値を指定した環境変数参照に書き換える
        """
        CONFIG_FILE_PATH = config_yaml_path()
        if not CONFIG_FILE_PATH.exists():
            return

        with open(CONFIG_FILE_PATH, "r", encoding="utf-8") as f:
            content = f.read()

        # re.sub で api_key: "XXXX" などの行を置換する
        # 正規表現: 行頭の空白 + api_key: + 空白 + 任意の文字列
        # ただし、コメント行（# api_key: ...）は考慮しないように ^\s* を使う
        # MULTILINEモードを使用
        
        # api_key の行を 'api_key: "${env_name}"' に置換する。
        # 置換文字列に raw f-string を使うと \" がバックスラッシュ+クォートとして
        # 残り config.yaml を壊すため（過去バグ）、lambda で組み立てる。
        # lambda 内 f-string では \g<1> は使えないので m.group(1) を使う。
        # ${{...}} で ${...} を生成、ダブルクォートはそのまま。
        new_content = re.sub(
            r'^(\s+api_key:\s*).*$',
            lambda m: f'{m.group(1)}"${{{env_name}}}"',
            content,
            flags=re.MULTILINE
        )
        
        with open(CONFIG_FILE_PATH, "w", encoding="utf-8") as f:
            f.write(new_content)
