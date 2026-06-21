# core/tools.py
"""
ツール定義モジュール

役割:
    LLMが呼び出せるツール（function calling）を定義する。
    各ツールは memory/manager.py を通じてファイル操作を行う。

ツール一覧:
    - read_file: ファイル読み込み
    - write_file: 新規ファイル作成（安全装置付き）
    - edit_file: 既存ファイルへの追記
    - replace_file: 既存ファイルの内容置換
    - list_files: ディレクトリ一覧

拡張方法:
    1. 実行関数を定義する
    2. TOOL_DEFINITIONS に JSON Schema を追加する
    3. execute_tool() に分岐を追加する
"""

from core.time_utils import tlog
import os
import re
import sys
import json
import subprocess
from pathlib import Path
from core.paths import data_file, config_file
from datetime import datetime

from memory.manager import MemoryManager
from core.web_tools import search_web, fetch_url, web_request, _add_security_labels
from core.filter import get_filter
from core.i18n import t, get_language
import random
import yaml

# プロジェクトルート（config.yamlがある階層）
_PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)


# --- programs/ 用 i18n（manifest の {{t:key}} を展開する） ---
# programs/_lang/<lang>.json を遅延ロードし、manifest の description / args[].description に
# 含まれる {{t:key}} を翻訳する。サブプロセス側（programs/_i18n.py）と辞書を共有することで、
# manifest（LLM が見るツール定義）と main.py の出力（同じく LLM が見る）を同じキーで揃えられる。
_program_translations: "dict | None" = None
_PROGRAM_MARKER_RE = re.compile(r"\{\{t:([a-zA-Z0-9_.]+)\}\}")


def _load_program_translations() -> dict:
    """programs/_lang/<lang>.json をロードしてキャッシュする。"""
    global _program_translations
    if _program_translations is not None:
        return _program_translations
    lang = get_language()
    base = Path(_PROJECT_ROOT) / "programs" / "_lang"
    lang_file = base / f"{lang}.json"
    if not lang_file.exists():
        lang_file = base / "ja.json"  # 雛形が未整備でも壊れない
    if lang_file.exists():
        try:
            with open(lang_file, "r", encoding="utf-8") as f:
                _program_translations = json.load(f)
        except Exception:
            _program_translations = {}
    else:
        _program_translations = {}
    return _program_translations


def _apply_program_i18n(text):
    """文字列内の {{t:key}} を programs/_lang から引いて置換する。
    text が文字列でなければそのまま返す（manifest の description 欠落対策）。"""
    if not isinstance(text, str):
        return text
    table = _load_program_translations()
    def _replace(m):
        return table.get(m.group(1), m.group(0))
    return _PROGRAM_MARKER_RE.sub(_replace, text)


def _i18n_manifest(manifest: dict) -> dict:
    """manifest の description / args[].description / tool.description を {{t:key}} 展開する。
    元の dict を破壊せず、コピーを返す。"""
    if not isinstance(manifest, dict):
        return manifest
    m = dict(manifest)
    if "description" in m:
        m["description"] = _apply_program_i18n(m["description"])
    if isinstance(m.get("tool"), dict):
        tm = dict(m["tool"])
        if "description" in tm:
            tm["description"] = _apply_program_i18n(tm["description"])
        m["tool"] = tm
    if isinstance(m.get("args"), list):
        new_args = []
        for a in m["args"]:
            if isinstance(a, dict):
                a = dict(a)
                if "description" in a:
                    a["description"] = _apply_program_i18n(a["description"])
            new_args.append(a)
        m["args"] = new_args
    return m


# --- LLMに渡すツール定義（OpenAI function calling 形式） ---

def _build_tool_definitions() -> list:
    """LLM に渡すツール定義（OpenAI function calling 形式）を、現在の言語設定で
    description を解決して構築する。

    description は i18n キー（tool_*）から `t()` で引く。定数ではなく関数にしているのは、
    モジュール import 時（init_i18n より前）に description が確定してしまうのを避けるため。
    呼び出し元の get_tool_definitions() は毎ターン呼ばれ、その時点では init_i18n 済み。
    """
    return [
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": t("tool_read_file_desc"),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": t("tool_read_file_param_path")
                    }
                },
                "required": ["path"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": t("tool_write_file_desc"),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": t("tool_write_file_param_path")
                    },
                    "content": {
                        "type": "string",
                        "description": t("tool_write_file_param_content")
                    }
                },
                "required": ["path", "content"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "edit_file",
            "description": t("tool_edit_file_desc"),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": t("tool_edit_file_param_path")
                    },
                    "content": {
                        "type": "string",
                        "description": t("tool_edit_file_param_content")
                    }
                },
                "required": ["path", "content"]
            }
        }
    },
    # === DISABLED 2026-06-19: replace_file 廃止検証 ===
    # AI がファイルを壊す事故が多いため、ツール一覧から外す。
    # 戻す場合はこのブロックのコメントを外すだけでよい。
    # memory.replace_file() メソッド自体は内部利用（PREFERENCES.md 自動アーカイブ）で残してある。
    # {
    #     "type": "function",
    #     "function": {
    #         "name": "replace_file",
    #         "description": t("tool_replace_file_desc"),
    #         "parameters": {
    #             "type": "object",
    #             "properties": {
    #                 "path": {
    #                     "type": "string",
    #                     "description": t("tool_replace_file_param_path")
    #                 },
    #                 "content": {
    #                     "type": "string",
    #                     "description": t("tool_replace_file_param_content")
    #                 }
    #             },
    #             "required": ["path", "content"]
    #         }
    #     }
    # },
    # === END DISABLED ===
    {
        "type": "function",
        "function": {
            "name": "list_files",
            "description": t("tool_list_files_desc"),
            "parameters": {
                "type": "object",
                "properties": {
                    "directory": {
                        "type": "string",
                        "description": t("tool_list_files_param_directory")
                    }
                },
                "required": []
            }
        }
    },

    {
        "type": "function",
        "function": {
            "name": "move_file",
            "description": t("tool_move_file_desc"),
            "parameters": {
                "type": "object",
                "properties": {
                    "source": {
                        "type": "string",
                        "description": t("tool_move_file_param_source")
                    },
                    "destination": {
                        "type": "string",
                        "description": t("tool_move_file_param_destination")
                    }
                },
                "required": ["source", "destination"]
            }
        }
    },

    {
        "type": "function",
        "function": {
            "name": "schedule_task",
            "description": t("tool_schedule_task_desc"),
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": t("tool_schedule_task_param_name")
                    },
                    "schedule_type": {
                        "type": "string",
                        "enum": ["daily", "once", "interval"],
                        "description": t("tool_schedule_task_param_schedule_type")
                    },
                    "time": {
                        "type": "string",
                        "description": t("tool_schedule_task_param_time")
                    },
                    "task_file": {
                        "type": "string",
                        "description": t("tool_schedule_task_param_task_file")
                    },
                    "interval_minutes": {
                        "type": "integer",
                        "description": t("tool_schedule_task_param_interval_minutes")
                    },
                    "start_time": {
                        "type": "string",
                        "description": t("tool_schedule_task_param_start_time")
                    },
                    "end_time": {
                        "type": "string",
                        "description": t("tool_schedule_task_param_end_time")
                    }
                },
                "required": ["name", "schedule_type", "task_file"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "list_schedules",
            "description": t("tool_list_schedules_desc"),
            "parameters": {
                "type": "object",
                "properties": {},
                "required": []
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "delete_schedule",
            "description": t("tool_delete_schedule_desc"),
            "parameters": {
                "type": "object",
                "properties": {
                    "schedule_id": {
                        "type": "string",
                        "description": t("tool_delete_schedule_param_schedule_id")
                    }
                },
                "required": ["schedule_id"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "search_web",
            "description": t("tool_search_web_desc"),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "anyOf": [
                            {"type": "string"},
                            {"type": "array", "items": {"type": "string"}}
                        ],
                        "description": t("tool_search_web_param_query")
                    },
                    "max_results": {
                        "type": "integer",
                        "description": t("tool_search_web_param_max_results")
                    },
                    "region": {
                        "type": "string",
                        "description": t("tool_search_web_param_region")
                    },
                    "timelimit": {
                        "type": "string",
                        "description": t("tool_search_web_param_timelimit")
                    }
                },
                "required": ["query"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "fetch_url",
            "description": t("tool_fetch_url_desc"),
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": t("tool_fetch_url_param_url")
                    }
                },
                "required": ["url"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "web_request",
            "description": t("tool_web_request_desc"),
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": t("tool_web_request_param_url")
                    },
                    "method": {
                        "type": "string",
                        "description": t("tool_web_request_param_method")
                    },
                    "headers": {
                        "type": "object",
                        "description": t("tool_web_request_param_headers")
                    },
                    "json_data": {
                        "type": "object",
                        "description": t("tool_web_request_param_json_data")
                    }
                },
                "required": ["url"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "recall",
            "description": t("tool_recall_desc"),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": t("tool_recall_param_query")
                    },
                    "source": {
                        "type": "string",
                        "enum": ["experience", "summary", "thoughts", "tools", "network", "dictionary"],
                        "description": t("tool_recall_param_source")
                    },
                    "n_results": {
                        "type": "integer",
                        "description": t("tool_recall_param_n_results")
                    }
                },
                "required": ["query"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "read_secret",
            "description": t("tool_read_secret_desc"),
            "parameters": {
                "type": "object",
                "properties": {
                    "filename": {
                        "type": "string",
                        "description": t("tool_read_secret_param_filename")
                    }
                },
                "required": ["filename"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "write_secret",
            "description": t("tool_write_secret_desc"),
            "parameters": {
                "type": "object",
                "properties": {
                    "filename": {
                        "type": "string",
                        "description": t("tool_write_secret_param_filename")
                    },
                    "content": {
                        "type": "string",
                        "description": t("tool_write_secret_param_content")
                    }
                },
                "required": ["filename", "content"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "edit_secret",
            "description": t("tool_edit_secret_desc"),
            "parameters": {
                "type": "object",
                "properties": {
                    "filename": {
                        "type": "string",
                        "description": t("tool_edit_secret_param_filename")
                    },
                    "append_content": {
                        "type": "string",
                        "description": t("tool_edit_secret_param_append_content")
                    }
                },
                "required": ["filename", "append_content"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "list_secrets",
            "description": t("tool_list_secrets_desc"),
            "parameters": {
                "type": "object",
                "properties": {},
                "required": []
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "roll_dice",
            "description": t("tool_roll_dice_desc"),
            "parameters": {
                "type": "object",
                "properties": {
                    "sides": {
                        "type": "integer",
                        "description": t("tool_roll_dice_param_sides")
                    }
                },
                "required": []
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "coin_flip",
            "description": t("tool_coin_flip_desc"),
            "parameters": {
                "type": "object",
                "properties": {}
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "run_program",
            "description": t("tool_run_program_desc"),
            "parameters": {
                "type": "object",
                "properties": {
                    "app_name": {
                        "type": "string",
                        "description": t("tool_run_program_param_app_name")
                    },
                    "args": {
                        "type": "object",
                        "description": t("tool_run_program_param_args")
                    }
                },
                "required": ["app_name"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "see_image",
            "description": t("tool_see_image_desc"),
            "parameters": {
                "type": "object",
                "properties": {
                    "source": {
                        "type": "string",
                        "description": t("tool_see_image_param_source")
                    },
                    "question": {
                        "type": "string",
                        "description": t("tool_see_image_param_question")
                    }
                },
                "required": ["source"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "reload_prompts",
            "description": t("tool_reload_prompts_desc"),
            "parameters": {
                "type": "object",
                "properties": {},
                "required": []
            }
        }
    },
    ]

# manifest の型表現 → JSON Schema の型名
_MANIFEST_TO_JSONSCHEMA_TYPE = {
    "string": "string",
    "integer": "integer",
    "number": "number",
    "boolean": "boolean",
    "object": "object",
    "array": "array",
}

# 生成した第一級ツールのキャッシュ（起動時に1回だけ構築）
_program_tools_cache = None   # list[dict]: 生成したツール定義
_program_tool_map = None      # dict[str, str]: ツール名 → programs/ のディレクトリ名


def _generate_program_tools():
    """programs/ を走査し、manifest に tool: ブロックを持つサテライトを
    第一級ツール定義（JSON Schema）へ変換する。

    昇格はオプトイン。tool: ブロックが無いサテライトは従来どおり run_program 経由のみ。
    実行は通常のサテライトと同じく _run_program（サブプロセス）を通るので、
    定義だけが LLM に直接見えるようになる。

    戻り値: (tool_definitions: list, name_to_program: dict)
    """
    tools = []
    name_to_program = {}

    prog_settings = _load_settings_programs()
    if not prog_settings.get('enabled', True):
        # サテライト実行が無効なら、実行できないツールを LLM に見せない
        return tools, name_to_program

    prog_dir = os.path.join(_PROJECT_ROOT, prog_settings.get('directory', 'programs'))
    if not os.path.isdir(prog_dir):
        return tools, name_to_program

    # 静的ツール定義と名前が衝突したら静的を優先する
    # （ループ変数 td: import した i18n の t() と名前衝突させないため）
    static_names = {td["function"]["name"] for td in _build_tool_definitions()}

    for entry in sorted(os.listdir(prog_dir)):
        manifest_path = os.path.join(prog_dir, entry, 'manifest.yaml')
        if not os.path.isfile(manifest_path):
            continue
        try:
            with open(manifest_path, 'r', encoding='utf-8') as f:
                manifest = yaml.safe_load(f) or {}
            manifest = _i18n_manifest(manifest)
            tool_meta = manifest.get('tool')
            if not tool_meta:
                continue  # 昇格対象でない（run_program 経由のまま）

            tool_name = tool_meta.get('name') or manifest.get('name', entry)
            if tool_name in static_names or tool_name in name_to_program:
                tlog(f"[tool-gen] ツール名衝突のため昇格をスキップ: {tool_name}（{entry}）")
                continue

            properties = {}
            required = []
            for adef in manifest.get('args', []):
                aname = adef.get('name')
                if not aname:
                    continue
                jtype = _MANIFEST_TO_JSONSCHEMA_TYPE.get(adef.get('type', 'string'), 'string')
                prop = {"type": jtype, "description": adef.get('description', '')}
                if 'enum' in adef:
                    prop['enum'] = adef['enum']
                properties[aname] = prop
                if adef.get('required', False):
                    required.append(aname)

            tools.append({
                "type": "function",
                "function": {
                    "name": tool_name,
                    "description": tool_meta.get('description', manifest.get('description', '')),
                    "parameters": {
                        "type": "object",
                        "properties": properties,
                        "required": required,
                    },
                },
            })
            name_to_program[tool_name] = entry
        except Exception as e:
            # 壊れた manifest はそのサテライトだけスキップし、全体は落とさない
            tlog(f"[tool-gen] {entry}: manifest 読み込み失敗のため昇格をスキップ（{e}）")
            continue

    return tools, name_to_program


def _ensure_program_tools():
    """生成ツールを遅延構築してキャッシュする。(tools, name_to_program) を返す。"""
    global _program_tools_cache, _program_tool_map
    if _program_tools_cache is None:
        _program_tools_cache, _program_tool_map = _generate_program_tools()
    return _program_tools_cache, _program_tool_map


def get_tool_definitions() -> list:
    """LLM に提示するツール定義を返す（静的定義 + manifest から生成した第一級ツール）。"""
    generated, _ = _ensure_program_tools()
    return _build_tool_definitions() + generated


import random

def roll_dice(sides: int = 6) -> str:
    """サイコロを振る"""
    result = random.randint(1, sides)
    return f"コロコロ... 🎲 {result} が出ました！ (1d{sides})"

def coin_flip() -> str:
    """コイントスを行う"""
    result = random.choice(["表", "裏"])
    return f"コイントスの結果: {result}"


def _load_settings_programs() -> dict:
    """settings.json から programs セクションを読み込む"""
    settings_path = os.path.join(_PROJECT_ROOT, 'settings.json')
    try:
        with open(settings_path, 'r', encoding='utf-8') as f:
            settings = json.load(f)
        return settings.get('programs', {})
    except Exception:
        return {}


def _list_programs() -> str:
    """programs/ ディレクトリを走査し、利用可能なサテライト一覧を返す"""
    prog_settings = _load_settings_programs()
    if not prog_settings.get('enabled', True):
        return t("tool_err_programs_disabled")

    prog_dir = os.path.join(_PROJECT_ROOT, prog_settings.get('directory', 'programs'))
    if not os.path.isdir(prog_dir):
        return t("tool_err_programs_dir_missing")

    programs = []
    for entry in sorted(os.listdir(prog_dir)):
        entry_path = os.path.join(prog_dir, entry)
        if not os.path.isdir(entry_path):
            continue
        manifest_path = os.path.join(entry_path, 'manifest.yaml')
        if not os.path.isfile(manifest_path):
            continue
        try:
            with open(manifest_path, 'r', encoding='utf-8') as f:
                manifest = yaml.safe_load(f)
            manifest = _i18n_manifest(manifest)
            name = manifest.get('name', entry)
            desc_default = t("tool_program_no_desc")
            desc = manifest.get('description', desc_default)
            # list 段階では1行のみ表示する。説明が複数行（YAMLの | や >）の場合は
            # 最初の1行だけに切り詰め、長すぎる場合は省略記号を付ける。
            # 詳細は各サテライトを引数なしで実行してヘルプを参照する想定。
            first_line = desc.strip().splitlines()[0].strip() if desc.strip() else desc_default
            max_len = 80
            if len(first_line) > max_len:
                first_line = first_line[:max_len].rstrip() + '…'
            programs.append(f"📦 {name}: {first_line}")
        except Exception as e:
            programs.append(t("tool_program_manifest_load_warn", entry=entry, e=e))

    if not programs:
        return t("tool_program_none")
    header = t("tool_program_list_header")
    return header + "\n" + "\n".join(programs)


def _run_program(app_name: str, args: dict, workspace_path: str) -> str:
    """
    外部Pythonサテライトを安全に実行する。

    1. manifest.yaml を読み込む
    2. 引数をバリデーション（型チェック、必須チェック、未定義引数拒否）
    3. 全 string 型引数にパストラバーサルチェック
    4. subprocess で実行（shell=False, sys.executable）
    5. stdout を JSON パース、stderr はログ
    6. インジェクション防御ラベルとNGワードフィルターを適用
    """
    prog_settings = _load_settings_programs()
    if not prog_settings.get('enabled', True):
        return t("tool_err_programs_disabled")

    prog_dir = os.path.join(_PROJECT_ROOT, prog_settings.get('directory', 'programs'))
    app_dir = os.path.join(prog_dir, app_name)

    # アプリ名にパス区切りが含まれていないか検証
    if os.sep in app_name or '/' in app_name or '..' in app_name:
        return t("tool_err_program_invalid_name", app_name=app_name)

    # manifest.yaml の読み込み
    manifest_path = os.path.join(app_dir, 'manifest.yaml')
    if not os.path.isfile(manifest_path):
        # 利用可能なサテライト一覧を取得
        available = []
        if os.path.isdir(prog_dir):
            for entry in sorted(os.listdir(prog_dir)):
                if os.path.isfile(os.path.join(prog_dir, entry, 'manifest.yaml')):
                    available.append(entry)
        available_str = ", ".join(available) if available else t("tool_program_available_none")
        return t("tool_err_program_not_found", app_name=app_name, available=available_str)
    try:
        with open(manifest_path, 'r', encoding='utf-8') as f:
            manifest = yaml.safe_load(f)
        manifest = _i18n_manifest(manifest)
    except Exception as e:
        return t("tool_err_program_manifest_load", e=e)

    # main.py の存在確認
    main_py = os.path.join(app_dir, 'main.py')
    if not os.path.isfile(main_py):
        return t("tool_err_program_main_missing", app_name=app_name)

    # --- 引数バリデーション ---
    defined_args = {a['name']: a for a in manifest.get('args', [])}
    args = args or {}

    # 未定義の引数を拒否
    for key in args:
        if key not in defined_args:
            valid_args = ", ".join(defined_args.keys())
            suggestion = ""
            for valid_key in defined_args:
                if valid_key in key or key in valid_key:
                    suggestion = t("tool_err_program_arg_suggestion", valid_key=valid_key)
                    break
            args_example = ', '.join(f'"{k}": ...' for k in defined_args)
            return t("tool_err_program_arg_undefined",
                     key=key, valid_args=valid_args, suggestion=suggestion,
                     app_name=app_name, args_example=args_example, lb="{", rb="}")

    # 必須引数チェック
    for name, adef in defined_args.items():
        if adef.get('required', False) and name not in args:
            req_label = t("tool_arg_required")
            opt_label = t("tool_arg_optional")
            args_help = "\n".join(
                f"  - {a['name']} ({a.get('type','string')}, {req_label if a.get('required') else opt_label}): {a.get('description','')}"
                for a in manifest.get('args', [])
            )
            return t("tool_err_program_arg_missing", name=name, app_name=app_name, args_help=args_help)

    # 型チェック
    type_map = {'string': str, 'integer': int, 'number': (int, float), 'boolean': bool}
    for key, value in args.items():
        expected_type_name = defined_args[key].get('type', 'string')
        expected_type = type_map.get(expected_type_name)
        if expected_type and not isinstance(value, expected_type):
            hint = ""
            if expected_type_name == "string" and isinstance(value, int):
                hint = t("tool_err_program_type_hint_string", value=value)
            return t("tool_err_program_type_mismatch",
                     key=key, expected=expected_type_name,
                     actual=type(value).__name__, hint=hint)

    # 全 string 型引数にパストラバーサルチェック
    workspace_resolved = str(Path(workspace_path).resolve())
    for key, value in args.items():
        # manifestで path_check: false が宣言された引数（本文テキスト等、パスではないもの）は対象外
        if defined_args[key].get('path_check', True) is False:
            continue
        if isinstance(value, str) and ('..' in value or value.startswith('/') or value.startswith('\\')):
            # パス的な文字列の場合、workspace 内に収まるか検証
            test_path = str((Path(workspace_path) / value).resolve())
            if not test_path.startswith(workspace_resolved):
                return t("tool_err_program_arg_traversal", key=key)

    # --- subprocess 実行 ---
    default_timeout = prog_settings.get('default_timeout_seconds', 30)
    timeout = manifest.get('timeout', default_timeout)

    env = os.environ.copy()
    env['CG_WORKSPACE'] = str(Path(workspace_path).resolve())
    env['PYTHONIOENCODING'] = 'utf-8'
    # i18n: 言語コードと programs/ ルート（_i18n.py の import 用）をサブプロセスに引き渡す
    env['CG_LANG'] = get_language()
    env['CG_PROJECT_ROOT'] = _PROJECT_ROOT
    # programs/ を PYTHONPATH に通すことで、各 main.py から `from _i18n import t` が引ける
    env['PYTHONPATH'] = prog_dir + (os.pathsep + env['PYTHONPATH'] if env.get('PYTHONPATH') else '')

    try:
        proc = subprocess.run(
            [sys.executable, main_py],
            input=json.dumps(args, ensure_ascii=False),
            capture_output=True,
            text=True,
            encoding='utf-8',
            timeout=timeout,
            cwd=workspace_path,
            env=env,
            shell=False
        )
    except subprocess.TimeoutExpired:
        return t("tool_err_program_timeout", app_name=app_name, timeout=timeout)
    except Exception as e:
        return t("tool_err_program_run_fail", e=e)

    # stderr をログに記録
    if proc.stderr:
        tlog(f"[run_program:{app_name}] stderr: {proc.stderr[:500]}")

    # stdout を JSON パース
    stdout = proc.stdout.strip()
    if not stdout:
        return t("tool_err_program_no_output", app_name=app_name, exit_code=proc.returncode)

    try:
        result = json.loads(stdout)
    except json.JSONDecodeError as e:
        return t("tool_err_program_bad_json", e=e, head=stdout[:200])

    # env_keeper が成功した場合、親プロセスの os.environ に .env を即時反映
    # （サブプロセス側で更新した環境変数を、同一ターン内の後続ツール呼び出しで使えるようにする）
    if app_name == "env_keeper" and result.get('status') in ('success', 'ok'):
        try:
            from core.env_manager import EnvManager
            EnvManager.load_env()
        except Exception as e:
            tlog(f"[run_program:env_keeper] 親プロセスへの .env 反映に失敗: {e}")

    # 結果を文字列に整形
    status = result.get('status', 'unknown')
    message = result.get('message', '')
    data = result.get('data')
    output_parts = [f"[{app_name}] status: {status}"]
    if message:
        output_parts.append(message)
    if data:
        output_parts.append(json.dumps(data, ensure_ascii=False, indent=2))
    output_text = "\n".join(output_parts)

    # NGワードフィルター適用
    content_filter = get_filter()
    if content_filter:
        filtered = content_filter.apply(output_text)
        if filtered != output_text:
            tlog(f"[run_program:{app_name}] フィルターにより一部の内容が除去されました")
            output_text = filtered

    # インジェクション防御ラベルを付与
    output_text = _add_security_labels(output_text)

    return output_text

import httpx
import base64
import mimetypes
import asyncio


async def handle_see_image(source: str, question: str = None, workspace_path: str = "", llm=None) -> str:
    """画像を取得してbase64データを返す。LLMのメインコンテキストで直接認識させる。"""
    try:
        tlog(f"[see_image] {source}")
        if source.startswith("http://") or source.startswith("https://"):
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.get(source)
                response.raise_for_status()
                image_bytes = response.content
                content_type = response.headers.get("content-type", "image/jpeg")
                mime_type = content_type.split(";")[0].strip()
        else:
            file_path = Path(workspace_path) / source
            if not file_path.exists():
                return t("tool_err_image_file_not_found", source=source)
            if not file_path.resolve().is_relative_to(Path(workspace_path).resolve()):
                return t("tool_err_image_outside_workspace")
            image_bytes = file_path.read_bytes()
            mime_type = mimetypes.guess_type(str(file_path))[0] or "image/jpeg"

        if len(image_bytes) > 10 * 1024 * 1024:
            return t("tool_err_image_too_large")

        supported_mimes = {"image/png", "image/jpeg", "image/webp", "image/heic", "image/heif"}
        if mime_type not in supported_mimes:
            return t("tool_err_image_unsupported_mime", mime_type=mime_type)

        b64 = base64.b64encode(image_bytes).decode("utf-8")
        data_url = f"data:{mime_type};base64,{b64}"

        import json
        return json.dumps({"__see_image__": True, "image_url": data_url, "question": question or t("tool_image_default_question")})

    except httpx.HTTPStatusError as e:
        return t("tool_err_image_fetch_fail", status=e.response.status_code)
    except httpx.TimeoutException:
        return t("tool_err_image_timeout")
    except Exception as e:
        return t("tool_err_image_recognize_fail", e=str(e))


async def execute_tool(memory: MemoryManager, tool_name: str, arguments: dict,
                 scheduler=None, rag_db=None, secret_manager=None, llm=None) -> str:
    """
    ツールを実行し、結果を文字列で返す。
    
    Args:
        memory: 記憶管理インスタンス
        tool_name: ツール名
        arguments: ツール引数
        scheduler: スケジューラインスタンス
        rag_db: RAGデータベースインスタンス
        secret_manager: 秘密日記管理インスタンス
    
    Returns:
        実行結果の文字列（成功メッセージまたはエラーメッセージ）
    """
    try:
        if tool_name == "read_file":
            content = memory.read_file(arguments["path"])
            if content is None:
                return t("tool_err_file_not_found_path", path=arguments['path'])
            return content
        
        elif tool_name == "write_file":
            return memory.write_file(arguments["path"], arguments["content"])
        
        elif tool_name == "edit_file":
            result = memory.edit_file(arguments["path"], arguments["content"])
            # PREFERENCES.md の自動退避処理
            if arguments["path"].replace("\\", "/").endswith("memory/preferences/PREFERENCES.md"):
                _handle_preferences_archiving(memory)
            return result
        
        # === DISABLED 2026-06-19: replace_file 廃止検証 ===
        # 上のツール定義ブロックと同時に戻すこと。
        # elif tool_name == "replace_file":
        #     return memory.replace_file(arguments["path"], arguments["content"])
        # === END DISABLED ===

        elif tool_name == "move_file":
            import shutil
            src = memory._resolve_path(arguments["source"])
            dst = memory._resolve_path(arguments["destination"])
            if not src.exists():
                return t("tool_err_move_source_missing", source=arguments['source'])
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src), str(dst))
            return t("tool_ok_moved", source=arguments['source'], destination=arguments['destination'])

        elif tool_name == "list_files":
            directory = arguments.get("directory", "")
            files = memory.list_files(directory)
            if not files:
                return t("tool_err_files_not_found")
            return "\n".join(files)

        elif tool_name == "read_secret":
            if secret_manager is None:
                return t("tool_err_secret_disabled")
            return secret_manager.read_secret(arguments["filename"])

        elif tool_name == "write_secret":
            if secret_manager is None:
                return t("tool_err_secret_disabled")
            return secret_manager.write_secret(arguments["filename"], arguments["content"])

        elif tool_name == "edit_secret":
            if secret_manager is None:
                return t("tool_err_secret_disabled")
            return secret_manager.edit_secret(arguments["filename"], arguments["append_content"])

        elif tool_name == "list_secrets":
            if secret_manager is None:
                return t("tool_err_secret_disabled")
            return secret_manager.list_secrets()

        elif tool_name == "schedule_task":
            if scheduler is None:
                return t("tool_err_scheduler_not_init")
            result = scheduler.add_schedule(
                name=arguments["name"],
                schedule_type=arguments["schedule_type"],
                time_str=arguments.get("time", ""),
                task_file=arguments["task_file"],
                interval_minutes=arguments.get("interval_minutes"),
                start_time=arguments.get("start_time"),
                end_time=arguments.get("end_time")
            )
            type_str = result["schedule_type"]
            if type_str == "interval":
                return t("tool_ok_schedule_interval", id=result['id'],
                         minutes=result.get('interval_minutes'),
                         start=result.get('start_time'), end=result.get('end_time'))
            return t("tool_ok_schedule_other", id=result['id'], type_str=type_str,
                     when=result.get('time') or result.get('datetime'))

        elif tool_name == "list_schedules":
            if scheduler is None:
                return t("tool_err_scheduler_not_init")
            schedules = scheduler.list_schedules()
            if not schedules:
                return t("tool_no_schedules")
            lines = []
            for s in schedules:
                status = t("tool_schedule_enabled") if s.get("enabled", True) else t("tool_schedule_disabled")
                time_info = s.get("time") or s.get("datetime") or t("tool_schedule_unknown_time")
                last = s.get("last_run") or t("tool_schedule_never_run")
                lines.append(t("tool_schedule_line",
                               id=s['id'], name=s['name'], type_str=s['schedule_type'],
                               time_info=time_info, status=status,
                               last=last, task_file=s['task_file']))
            return "\n".join(lines)

        elif tool_name == "delete_schedule":
            if scheduler is None:
                return t("tool_err_scheduler_not_init")
            if scheduler.remove_schedule(arguments["schedule_id"]):
                return t("tool_ok_schedule_deleted", id=arguments['schedule_id'])
            return t("tool_err_schedule_not_found", id=arguments['schedule_id'])

        elif tool_name == "search_web":
            return search_web(
                arguments["query"],
                max_results=arguments.get("max_results", 5),
                region=arguments.get("region", "jp-jp"),
                timelimit=arguments.get("timelimit")
            )

        elif tool_name == "fetch_url":
            return fetch_url(arguments["url"])

        elif tool_name == "web_request":
            return web_request(
                url=arguments["url"],
                method=arguments.get("method", "GET"),
                headers=arguments.get("headers"),
                json_data=arguments.get("json_data")
            )

        elif tool_name == "recall":
            source = arguments.get("source", "summary")
            query = arguments["query"]
            n = arguments.get("n_results", 5)

            # ネットワーク検索
            if source == "network":
                from core.wyrd_network import load_graph, search_memory, save_graph
                import json as _json

                if rag_db is None or not hasattr(rag_db, '_ef'):
                    return t("tool_err_recall_network_no_rag")

                def _embed(text):
                    return rag_db._ef.embed_query([text])[0]

                graph = load_graph()
                config_path = config_file("wyrd_config.json")
                search_config = {}
                if config_path.exists():
                    with open(config_path, "r", encoding="utf-8") as f:
                        search_config = _json.load(f).get("search", {})

                results = search_memory(query, graph, embed_fn=_embed, config=search_config, top_k=n)
                save_graph(graph)

                if not results or not results.get("episodes"):
                    return t("tool_recall_network_empty")

                lines = []
                for r in results["episodes"]:
                    v = r.get("valence", 0)
                    lines.append(t("tool_recall_network_line",
                                   date=r['timestamp'][:10], content=r['content'],
                                   energy=r['energy'], valence=f"{v:+.2f}"))

                if results.get("related_concepts"):
                    lines.append("\n" + t("tool_recall_related_concepts",
                                          concepts=', '.join(results['related_concepts'])))

                return t("tool_recall_network_header") + "\n" + "\n".join(lines)

            elif source == "dictionary":
                from core.wyrd_network import load_graph, search_concept

                if rag_db is None or not hasattr(rag_db, '_ef'):
                    return t("tool_err_recall_dict_no_rag")

                def _embed(text):
                    return rag_db._ef.embed_query([text])[0]

                graph = load_graph()
                result = search_concept(query, graph, embed_fn=_embed)

                if result["match"] in ("exact", "alias"):
                    return t("tool_recall_concept",
                             label=result['label'], description=result['description'],
                             edges=result['edge_count'])
                else:
                    suggestions = ", ".join(result["suggestions"])
                    return t("tool_recall_concept_not_found", query=query, suggestions=suggestions)

            # 既存のChromaDB検索
            if rag_db is None:
                return t("tool_err_recall_no_rag")

            collections = {
                "experience": ("logs", t("tool_recall_label_experience")),
                "summary": ("daily_memories", t("tool_recall_label_summary")),
                "thoughts": ("notes", t("tool_recall_label_thoughts")),
                "tools": ("tool_results", t("tool_recall_label_tools")),
            }

            if source in collections:
                targets = {source: collections[source]}
            else:
                return t("tool_err_recall_unknown_source", source=source)

            all_results = []
            unknown = t("tool_recall_unknown")
            section_all = t("tool_recall_section_all")
            for key, (col_name, label) in targets.items():
                results = rag_db.search(col_name, query, n)
                for r in results:
                    meta = r.get('metadata', {})
                    doc = r.get('document', '')
                    if key == "experience":
                        date = meta.get('date', unknown)
                        emotion = meta.get('emotion', unknown)
                        all_results.append(t("tool_recall_experience_line",
                                             label=label, date=date, emotion=emotion, doc=doc))
                    else:
                        section = meta.get('section', section_all)
                        all_results.append(t("tool_recall_other_line",
                                             label=label, section=section, doc=doc))

            if not all_results:
                return t("tool_recall_no_results")
            return "\n\n---\n\n".join(all_results)


        elif tool_name == "roll_dice":
            sides = arguments.get("sides", 6)
            try:
                sides = int(sides)
            except ValueError:
                sides = 6
            if sides < 1:
                return t("tool_err_dice_sides")
            return roll_dice(sides)

        elif tool_name == "coin_flip":
            return coin_flip()

        elif tool_name == "run_program":
            app_name = arguments.get("app_name", "")
            if app_name == "list":
                return _list_programs()
            return _run_program(app_name, arguments.get("args", {}), str(memory.workspace))

        elif tool_name in _ensure_program_tools()[1]:
            # manifest の tool: ブロックから昇格した第一級ツール。
            # 実装は programs/ 側が単一の真実で、run_program 経由の呼び出しも従来どおり有効。
            program_name = _ensure_program_tools()[1][tool_name]
            return _run_program(program_name, arguments, str(memory.workspace))

        elif tool_name == "see_image":
            return await handle_see_image(
                source=arguments["source"],
                question=arguments.get("question"),
                workspace_path=str(memory.workspace),
                llm=llm
            )
        elif tool_name == "reload_prompts":
            return "__reload_prompts__"
      
        else:
            return t("tool_err_unknown_tool", name=tool_name)

    except (FileExistsError, FileNotFoundError, PermissionError) as e:
        # 安全装置によるエラーはそのままLLMに伝える
        return t("tool_err_generic", e=str(e))
    except Exception as e:
        return t("tool_err_unexpected_kind", kind=type(e).__name__, e=str(e))

def _handle_preferences_archiving(memory: MemoryManager):
    """
    PREFERENCES.md の項目数が20を超えたセクションをアーカイブに退避させる。
    """
    # MemoryManagerはワークスペース内を想定
    rel_path = "memory/preferences/PREFERENCES.md"
    
    content = memory.read_file(rel_path)
    if not content:
        return

    lines = content.splitlines()
    archived_items = [] # (index, (section_name, line_text))
    
    current_section = "未分類"
    section_items = [] # 現在のセクションの項目行のインデックスリスト
    
    for i, line in enumerate(lines):
        clean_line = line.strip()
        if clean_line.startswith("## "):
            # 新しいセクションに入ったので、前のセクションを精査
            _process_section_archiving(lines, section_items, archived_items)
            section_items = []
            current_section = clean_line.replace("## ", "").strip()
        
        # 項目行（- で始まり、テンプレート _ は除外）
        if clean_line.startswith("- ") and not clean_line.startswith("- _"):
            section_items.append(i)
            
    # 最後のセクションを精査
    _process_section_archiving(lines, section_items, archived_items)
    
    if not archived_items:
        return # 退避対象なし

    # 1. アーカイブへの追記
    now = datetime.now()
    month_str = now.strftime("%Y%m")
    archive_path = f"memory/preferences/archive_{month_str}.md"
    date_label = now.strftime("%Y-%m")
    
    # セクションごとにグループ化してアーカイブ文字列を作成
    from collections import defaultdict
    grouped = defaultdict(list)
    for idx, (sec_name, item_text) in archived_items:
        grouped[sec_name].append(item_text)
    
    archive_content = ""
    for sec, items in grouped.items():
        archive_content += f"\n## {sec}（退避: {date_label}）\n"
        for item in items:
            archive_content += f"{item}\n"
            
    try:
        existing_archive = memory.read_file(archive_path)
        if existing_archive:
            memory.edit_file(archive_path, archive_content)
        else:
            header = f"# PREFERENCES アーカイブ ({date_label})\n"
            memory.write_file(archive_path, header + archive_content)
    except Exception as e:
        print(f"警告: アーカイブの保存に失敗しました: {e}")

    # 2. PREFERENCES.md の更新
    archived_indices = {idx for idx, _ in archived_items}
    # 完全に一致する行を削除するため、インデックスベースでフィルタリング
    final_lines = [line for i, line in enumerate(lines) if i not in archived_indices]

    memory.replace_file(rel_path, "\n".join(final_lines))

def _process_section_archiving(lines, section_items, archived_items):
    """セクション内の項目が20個を超えていたら、古いものを退避リストに加える"""
    if len(section_items) > 20:
        over = len(section_items) - 20
        # どのセクションか特定するために、最初の項目の前の ## 行を探す
        sec_name = "未分類"
        for j in range(section_items[0], -1, -1):
            if lines[j].strip().startswith("## "):
                sec_name = lines[j].strip().replace("## ", "").strip()
                break
        
        # 古い（上から）超過分を退避リストへ
        for idx in section_items[-over:]:
            archived_items.append((idx, (sec_name, lines[idx])))
