import os
import json
import yaml
import re
from datetime import datetime

CONFIG_FILE = "config.yaml"
SETTINGS_FILE = "settings.json"
print(f"[DEBUG] core/config_loader.py loaded. Path: {__file__}")

def deep_merge(dict1, dict2):
    """
    dict1 に dict2 の内容を再帰的にマージする。
    辞書の場合はさらにマージし、それ以外は dict2 の値で上書きする。
    """
    for key, value in dict2.items():
        if isinstance(value, dict) and key in dict1 and isinstance(dict1[key], dict):
            deep_merge(dict1[key], value)
        else:
            dict1[key] = value

def resolve_env_vars(config):
    """
    設定値に含まれる ${ENV_VAR_NAME} を環境変数から展開する。
    """
    env_pattern = re.compile(r'\$\{([^}^{]+)\}')

    def _resolve(node):
        if isinstance(node, dict):
            for k, v in node.items():
                node[k] = _resolve(v)
            return node
        elif isinstance(node, list):
            return [_resolve(item) for item in node]
        elif isinstance(node, str):
            def replace_var(match):
                var_name = match.group(1)
                return os.environ.get(var_name, match.group(0))
            return env_pattern.sub(replace_var, node)
        else:
            return node

    return _resolve(config)

def load_config():
    """
    config.yaml をベースに、存在すれば settings.json で上書きし、
    最後に環境変数を解決した設定辞書を返す。
    """
    # 1. パスの解決（core.paths 経由 = data_root 基準）
    #    引数なし dev では data_root() が agent ディレクトリを返すため、
    #    従来の project_root 基準と完全に同一パスになる（dev 非破壊）。
    from core.paths import config_yaml_path as _config_yaml_path, settings_path as _settings_path
    config_path = str(_config_yaml_path())
    settings_path = str(_settings_path())
    print(f"[DEBUG] load_config from: {settings_path}")

    config = {}
    
    # 2. config.yaml の読み込み（デフォルト）
    if os.path.exists(config_path):
        with open(config_path, "r", encoding="utf-8") as f:
            yaml_config = yaml.safe_load(f) or {}
            config.update(yaml_config)

    # 3. settings.json の読み込み（ユーザー上書き）
    if os.path.exists(settings_path):
        try:
            with open(settings_path, "r", encoding="utf-8") as f:
                settings_config = json.load(f) or {}
                deep_merge(config, settings_config)
        except json.JSONDecodeError as e:
            print(f"警告: settings.json の読み込みに失敗しました（JSONフォーマットエラー）: {e}")
        except Exception as e:
            print(f"警告: settings.json の読み込みに失敗しました: {e}")

    # 4. 環境変数の展開
    config = resolve_env_vars(config)

    return config

def load_config_strict():
    """config.yaml の存在を確認してから load_config() を実行する。

    config.yaml が無い場合は案内を表示して SystemExit する
    （旧 server.py の load_config ラッパーと同一挙動）。
    """
    from core.paths import config_yaml_path
    if not config_yaml_path().exists():
        print("エラー: config.yaml が見つかりません")
        print("config.example.yaml を config.yaml にコピーして初回の設定をしてください")
        raise SystemExit(1)
    return load_config()


def apply_prompt_placeholders(text: str, agent_name: str = "Assistant", honorific: str = "ユーザー") -> str:
    """プロンプト/設定テキスト共通のプレースホルダを実際の値に置換する。

    - {{agent_name}}      -> エージェント名（profile.agent.name）
    - {{user_honorific}}  -> ユーザー呼称（profile.user.honorific）

    context.py / salia.py / agent.py（圧縮プロンプト）/ repetition_guard.py から共通利用し、
    プレースホルダの一覧を一箇所に集約する。
    """
    if not text:
        return text
    return text.replace("{{agent_name}}", agent_name).replace("{{user_honorific}}", honorific)


def save_setting(section: str, key: str, value):
    """
    単一の設定を settings.json に保存する便利関数。
    """
    save_settings(section, {key: value})

def save_settings(section_or_dict, settings_dict=None):
    """
    設定を settings.json に保存する。
    
    呼び出し形式:
    1. save_settings("section_name", {"key": "value"})  # 単一セクション
    2. save_settings({"section1": {...}, "section2": {...}})  # 複数セクション一括
    """
    from core.paths import settings_path as _settings_path
    settings_path = str(_settings_path())
    # debug_save_settings.log は今段階では project_root のまま（デバッグ用、移行は後段）。
    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    # 関数の入口でログを出力（最優先）
    try:
        with open(os.path.join(project_root, "debug_save_settings.log"), "a", encoding="utf-8") as log_f:
            log_f.write(f"{datetime.now()}: [ENTRY] save_settings called with section_or_dict type={type(section_or_dict)}\n")
    except Exception:
        pass

    current_settings = {}
    if os.path.exists(settings_path):
        try:
            with open(settings_path, "r", encoding="utf-8") as f:
                current_settings = json.load(f) or {}
        except Exception as e:
            print(f"警告: 既存の settings.json 読み込みに失敗しました: {e}")

    # 引数の形式に応じて処理を分ける
    to_save = {}
    if isinstance(section_or_dict, dict) and settings_dict is None:
        # 複数セクション一括形式
        to_save = section_or_dict
    elif isinstance(section_or_dict, str) and isinstance(settings_dict, dict):
        # 単一セクション形式
        to_save = {section_or_dict: settings_dict}
    else:
        print(f"警告: save_settings の引数が不正です: {section_or_dict}, {settings_dict}")
        return

    # current_settings にマージ
    for sec, data in to_save.items():
        if isinstance(data, dict):
            if sec not in current_settings or not isinstance(current_settings[sec], dict):
                current_settings[sec] = {}
            for k, v in data.items():
                current_settings[sec][k] = v
        else:
            # 文字列やリストなどの場合は直接上書き
            current_settings[sec] = data

    try:
        with open(settings_path, "w", encoding="utf-8") as f:
            json.dump(current_settings, f, ensure_ascii=False, indent=4)
    except Exception as e:
        print(f"警告: settings.json への保存に失敗しました: {e}")

def get_settings():
    """
    現在の settings.json（ユーザー上書き分のみ）の内容を取得する。
    """
    from core.paths import settings_path as _settings_path
    settings_path = str(_settings_path())
    if os.path.exists(settings_path):
        try:
            with open(settings_path, "r", encoding="utf-8") as f:
                return json.load(f) or {}
        except Exception:
            return {}
    return {}
