#!/usr/bin/env python3
"""
cg_bridge.py - Crescent Grove programs/ ブリッジ

Crescent Groveのrun_program規約に従うサテライトを、
コマンドラインから実行できるようにするブリッジスクリプト。

使い方:
  python cg_bridge.py list
  python cg_bridge.py astronoka '{"action": "status"}'
  python cg_bridge.py astronoka '{"action": "plant", "vegetable": "シマイモ"}'

Crescent Groveと同じセキュリティチェックを行う:
  - アプリ名のパストラバーサル防止
  - manifest.yamlによる引数バリデーション（型、必須、未定義拒否）
  - string型引数のパストラバーサルチェック
  - shell=False でのsubprocess実行
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import yaml


# このスクリプト自体がプロジェクトルートに置かれる想定
PROGRAMS_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = PROGRAMS_DIR.parent
WORKSPACE_DIR = PROJECT_ROOT / "workspace"


def load_manifest(app_name: str) -> dict:
    manifest_path = PROGRAMS_DIR / app_name / "manifest.yaml"
    with open(manifest_path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def list_programs():
    if not PROGRAMS_DIR.is_dir():
        print("利用可能なサテライトはありません")
        return

    found = False
    for entry in sorted(PROGRAMS_DIR.iterdir()):
        if not entry.is_dir():
            continue
        manifest_path = entry / "manifest.yaml"
        if not manifest_path.is_file():
            continue
        try:
            manifest = load_manifest(entry.name)
            name = manifest.get("name", entry.name)
            desc = manifest.get("description", "説明なし")
            args_def = manifest.get("args", [])
            print(f"\n  {name}: {desc}")
            if args_def:
                for a in args_def:
                    req = "必須" if a.get("required", False) else "任意"
                    print(f"    - {a['name']} ({a.get('type', 'string')}, {req}): {a.get('description', '')}")
            else:
                print("    （引数なし）")
            found = True
        except Exception as e:
            print(f"  {entry.name}: manifest.yaml の読み込みに失敗 ({e})")

    if not found:
        print("利用可能なサテライトはありません")


def validate_and_run(app_name: str, args: dict):
    # アプリ名検証
    if os.sep in app_name or "/" in app_name or ".." in app_name:
        print(f"エラー: 不正なサテライト名です: {app_name}", file=sys.stderr)
        sys.exit(1)

    app_dir = PROGRAMS_DIR / app_name
    manifest_path = app_dir / "manifest.yaml"
    main_py = app_dir / "main.py"

    if not manifest_path.is_file():
        print(f"エラー: サテライトが見つかりません: {app_name}", file=sys.stderr)
        sys.exit(1)

    if not main_py.is_file():
        print(f"エラー: {app_name}/main.py が見つかりません", file=sys.stderr)
        sys.exit(1)

    # manifest読み込み
    try:
        manifest = load_manifest(app_name)
    except Exception as e:
        print(f"エラー: manifest.yaml の読み込みに失敗: {e}", file=sys.stderr)
        sys.exit(1)

    defined_args = {a["name"]: a for a in manifest.get("args", [])}

    # 未定義の引数を拒否
    for key in args:
        if key not in defined_args:
            print(f"エラー: 未定義の引数です: '{key}'", file=sys.stderr)
            sys.exit(1)

    # 必須引数チェック
    for name, adef in defined_args.items():
        if adef.get("required", False) and name not in args:
            print(f"エラー: 必須引数が不足しています: '{name}'", file=sys.stderr)
            sys.exit(1)

    # 型チェック
    type_map = {"string": str, "integer": int, "number": (int, float), "boolean": bool}
    for key, value in args.items():
        expected_type_name = defined_args[key].get("type", "string")
        expected_type = type_map.get(expected_type_name)
        if expected_type and not isinstance(value, expected_type):
            print(f"エラー: 引数 '{key}' の型が不正です。期待: {expected_type_name}", file=sys.stderr)
            sys.exit(1)

    # string型引数のパストラバーサルチェック
    workspace_resolved = str(WORKSPACE_DIR.resolve())
    for key, value in args.items():
        if isinstance(value, str) and (".." in value or value.startswith("/") or value.startswith("\\")):
            test_path = str((WORKSPACE_DIR / value).resolve())
            if not test_path.startswith(workspace_resolved):
                print(f"エラー: 引数 '{key}' にワークスペース外のパスが含まれています", file=sys.stderr)
                sys.exit(1)

    # 実行
    timeout = manifest.get("timeout", 30)

    env = os.environ.copy()
    env["CG_WORKSPACE"] = workspace_resolved
    env["PYTHONIOENCODING"] = "utf-8"

    try:
        proc = subprocess.run(
            [sys.executable, str(main_py)],
            input=json.dumps(args, ensure_ascii=False),
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=timeout,
            cwd=str(WORKSPACE_DIR),
            env=env,
            shell=False,
        )
    except subprocess.TimeoutExpired:
        print(f"エラー: タイムアウト（{timeout}秒）", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"エラー: 実行に失敗: {e}", file=sys.stderr)
        sys.exit(1)

    if proc.stderr:
        print(proc.stderr, file=sys.stderr)

    stdout = proc.stdout.strip()
    if not stdout:
        print(f"エラー: サテライトが何も出力しませんでした (exit code: {proc.returncode})", file=sys.stderr)
        sys.exit(1)

    # JSON整形して出力
    try:
        result = json.loads(stdout)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except json.JSONDecodeError:
        print(stdout)


def main():
    if len(sys.argv) < 2:
        print("使い方:")
        print(f"  python {sys.argv[0]} list")
        print(f'  python {sys.argv[0]} <app_name> \'{{\"arg\": \"value\"}}\'')
        sys.exit(1)

    app_name = sys.argv[1]

    if app_name == "list":
        list_programs()
        return

    # 引数のパース
    if len(sys.argv) >= 3:
        try:
            args = json.loads(sys.argv[2])
        except json.JSONDecodeError:
            print(f"エラー: 引数のJSONが不正です: {sys.argv[2]}", file=sys.stderr)
            sys.exit(1)
    else:
        args = {}

    validate_and_run(app_name, args)


if __name__ == "__main__":
    main()
