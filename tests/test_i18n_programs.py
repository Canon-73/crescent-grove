# test_i18n_programs.py
"""
programs サブプロセス用 i18n（フェーズ4-A 基盤）の回帰テスト。

test_i18n_prompts.py と同じ流儀: pytest 不要・自前ランナー・venv で直接実行する。
    venv\\Scripts\\python.exe tests\\test_i18n_programs.py

柚月サーバ（dev）は起動しない。検証するのは:

  1. programs/_lang/ja.json と programs/_lang/en.json が読める。
  2. ja/en でキー集合が完全一致（欠落ゼロ）。
  3. programs/_i18n.py が import でき、t() が動く。
     CG_LANG=en で呼ぶと、辞書に未定義のキーは {{t:key}} のまま返る。
  4. 各 program の main.py を実際に subprocess で起動して、env 経由で
     CG_LANG / CG_PROJECT_ROOT / PYTHONPATH が渡り、from _i18n import t が成立する。
     （hello_world をサンプルとして使う）
  5. core/tools.py の _i18n_manifest() が manifest の {{t:key}} を
     description / args[].description / tool.description で展開する。
  6. 親 core/i18n.py と programs/_i18n.py の API（t の引数仕様・未解決時の挙動）が一致。
"""

import json
import os
import re
import subprocess
import sys
from pathlib import Path


# --- 日本語文字判定（test_i18n_prompts.py と同じ） ---
def has_jp(s: str) -> bool:
    for c in s:
        if "぀" <= c <= "ヿ" or "一" <= c <= "鿿" or "＀" <= c <= "￯":
            return True
    return False


def has_unresolved(s: str) -> bool:
    return "{{t:" in s


# tests/ から見て1段上がリポジトリルート
PROJECT_ROOT = Path(__file__).resolve().parent.parent
PROGRAMS_DIR = PROJECT_ROOT / "programs"
LANG_DIR = PROGRAMS_DIR / "_lang"
# core.* を import できるようルートを sys.path に追加
sys.path.insert(0, str(PROJECT_ROOT))


def main() -> int:
    failures = []

    # ----- 1. programs/_lang ファイルが存在し読み込める -----
    ja_path = LANG_DIR / "ja.json"
    en_path = LANG_DIR / "en.json"
    if not ja_path.exists():
        failures.append(f"[file] {ja_path} が存在しない")
    if not en_path.exists():
        failures.append(f"[file] {en_path} が存在しない")

    ja = {}
    en = {}
    if ja_path.exists():
        try:
            ja = json.load(open(ja_path, encoding="utf-8"))
        except Exception as e:
            failures.append(f"[file] ja.json 読み込み失敗: {e}")
    if en_path.exists():
        try:
            en = json.load(open(en_path, encoding="utf-8"))
        except Exception as e:
            failures.append(f"[file] en.json 読み込み失敗: {e}")

    # ----- 2. ja/en でキー集合が完全一致 -----
    only_ja = sorted(set(ja) - set(en))
    only_en = sorted(set(en) - set(ja))
    if only_ja:
        failures.append(f"[keys] ja のみに存在: {only_ja[:5]}{'...' if len(only_ja) > 5 else ''}")
    if only_en:
        failures.append(f"[keys] en のみに存在: {only_en[:5]}{'...' if len(only_en) > 5 else ''}")

    # ----- 3. programs/_i18n.py を import して t() を直接叩く -----
    # sys.path に programs/ を入れて、サブプロセスと同じ条件にする
    sys.path.insert(0, str(PROGRAMS_DIR))
    # CG_LANG を en にしてキャッシュをクリアして再ロード
    os.environ["CG_LANG"] = "en"
    if "_i18n" in sys.modules:
        del sys.modules["_i18n"]
    import _i18n as prog_i18n  # type: ignore

    # 未定義キーは {{t:key}} のまま
    v = prog_i18n.t("__definitely_undefined_key__")
    if v != "{{t:__definitely_undefined_key__}}":
        failures.append(f"[api] 未定義キーの戻り値が不正: {v!r}")

    # kwargs 置換が str.replace 流儀（.format ではない）
    # 動作確認のため、辞書を一時的にいじって検証する
    prog_i18n._T = {"__test_replace__": "Hello {name}, count={n}, lb={lb}rb={rb}"}
    v = prog_i18n.t("__test_replace__", name="Alice", n=3, lb="{", rb="}")
    if v != "Hello Alice, count=3, lb={rb=}":
        failures.append(f"[api] kwargs 置換が不正: {v!r}")

    # positional-only: kwargs に "key" を渡せる
    prog_i18n._T = {"__test_key_kw__": "key={key}"}
    v = prog_i18n.t("__test_key_kw__", key="myvalue")
    if v != "key=myvalue":
        failures.append(f"[api] positional-only ではない（kwargs に key を渡せない）: {v!r}")

    # 辞書を元に戻して get_language の挙動も確認
    prog_i18n._T = None  # 再ロードさせる
    if prog_i18n.get_language() != "en":
        failures.append(f"[api] get_language() が CG_LANG を反映していない")

    # ----- 4. hello_world をサブプロセス起動して動作確認 -----
    hw_main = PROGRAMS_DIR / "hello_world" / "main.py"
    if not hw_main.exists():
        failures.append(f"[subproc] hello_world/main.py が見つからない")
    else:
        env = os.environ.copy()
        env["CG_LANG"] = "en"
        env["CG_PROJECT_ROOT"] = str(PROJECT_ROOT)
        env["CG_WORKSPACE"] = str(PROJECT_ROOT)
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONPATH"] = str(PROGRAMS_DIR) + os.pathsep + env.get("PYTHONPATH", "")
        try:
            proc = subprocess.run(
                [sys.executable, str(hw_main)],
                input=json.dumps({"name": "Tester"}),
                capture_output=True, text=True, encoding="utf-8",
                timeout=10, shell=False, env=env,
            )
            if proc.returncode != 0:
                failures.append(f"[subproc] hello_world が異常終了: rc={proc.returncode} stderr={proc.stderr[:200]}")
            else:
                # stdout が JSON として読めること
                try:
                    result = json.loads(proc.stdout.strip())
                except Exception as e:
                    failures.append(f"[subproc] hello_world stdout が JSON でない: {e} / {proc.stdout[:200]}")
                    result = None
                # en で実行したので message に英語が出るはず（日本語残存なし・未解決マーカなし）
                if result is not None:
                    msg = result.get("message", "")
                    if has_unresolved(msg):
                        failures.append(f"[subproc] hello_world en の message に未解決マーカ: {msg!r}")
                    if has_jp(msg):
                        failures.append(f"[subproc] hello_world en の message に日本語残存: {msg!r}")
                    if "Tester" not in msg:
                        failures.append(f"[subproc] hello_world en の message に name 展開なし: {msg!r}")
        except Exception as e:
            failures.append(f"[subproc] hello_world 実行失敗: {e}")

        # 4a-2. 同じことを ja でも実行（dev 既定）
        env_ja = dict(env)
        env_ja["CG_LANG"] = "ja"
        try:
            proc = subprocess.run(
                [sys.executable, str(hw_main)],
                input=json.dumps({"name": "Tester"}),
                capture_output=True, text=True, encoding="utf-8",
                timeout=10, shell=False, env=env_ja,
            )
            if proc.returncode == 0:
                result = json.loads(proc.stdout.strip())
                msg = result.get("message", "")
                if has_unresolved(msg):
                    failures.append(f"[subproc] hello_world ja の message に未解決マーカ: {msg!r}")
                if not has_jp(msg):
                    failures.append(f"[subproc] hello_world ja の message に日本語が無い: {msg!r}")
                if "Tester" not in msg:
                    failures.append(f"[subproc] hello_world ja の message に name 展開なし: {msg!r}")
            else:
                failures.append(f"[subproc] hello_world ja が異常終了: rc={proc.returncode}")
        except Exception as e:
            failures.append(f"[subproc] hello_world ja 実行失敗: {e}")

    # 4b. サブプロセス側で `from _i18n import t` が成功し、CG_LANG が見える
    probe_code = (
        "import os, sys, json;"
        "sys.path.insert(0, os.environ['CG_PROJECT_ROOT'] + os.sep + 'programs');"
        "import _i18n;"
        "print(json.dumps({'lang': _i18n.get_language(),"
        " 'undef': _i18n.t('__nope__')}))"
    )
    env = os.environ.copy()
    env["CG_LANG"] = "en"
    env["CG_PROJECT_ROOT"] = str(PROJECT_ROOT)
    env["PYTHONIOENCODING"] = "utf-8"
    try:
        proc = subprocess.run(
            [sys.executable, "-c", probe_code],
            capture_output=True, text=True, encoding="utf-8",
            timeout=10, env=env, shell=False,
        )
        if proc.returncode != 0:
            failures.append(f"[subproc] _i18n probe 失敗: rc={proc.returncode} stderr={proc.stderr[:200]}")
        else:
            result = json.loads(proc.stdout.strip())
            if result.get("lang") != "en":
                failures.append(f"[subproc] CG_LANG=en が反映されていない: {result}")
            if result.get("undef") != "{{t:__nope__}}":
                failures.append(f"[subproc] 未定義キーの戻り値が不正: {result}")
    except Exception as e:
        failures.append(f"[subproc] _i18n probe 実行失敗: {e}")

    # ----- 5. core/tools._i18n_manifest() で {{t:key}} 展開 -----
    # 親 core/i18n を ja で初期化（programs 用辞書は別系統なので影響なし）
    from core.i18n import init_i18n
    init_i18n({"language": "en"})
    # tools 内のキャッシュをクリアして再ロード
    import core.tools as ctools
    ctools._program_translations = None
    # テスト用に programs 辞書を仕込む
    ctools._program_translations = {
        "__test_prog_desc__": "Manifest desc OK",
        "__test_arg_desc__": "Arg desc OK",
        "__test_tool_desc__": "Tool desc OK",
    }
    manifest = {
        "name": "x",
        "description": "{{t:__test_prog_desc__}}",
        "tool": {"description": "{{t:__test_tool_desc__}}"},
        "args": [
            {"name": "a", "description": "{{t:__test_arg_desc__}}"},
            {"name": "b", "description": "literal"},
        ],
    }
    out = ctools._i18n_manifest(manifest)
    if out["description"] != "Manifest desc OK":
        failures.append(f"[manifest] description 展開失敗: {out['description']!r}")
    if out["tool"]["description"] != "Tool desc OK":
        failures.append(f"[manifest] tool.description 展開失敗: {out['tool']['description']!r}")
    if out["args"][0]["description"] != "Arg desc OK":
        failures.append(f"[manifest] args[0].description 展開失敗: {out['args'][0]['description']!r}")
    if out["args"][1]["description"] != "literal":
        failures.append(f"[manifest] args[1].description（リテラル）が変質: {out['args'][1]['description']!r}")
    # 元 manifest が破壊されていない
    if manifest["description"] != "{{t:__test_prog_desc__}}":
        failures.append(f"[manifest] 元 dict が破壊された: {manifest['description']!r}")

    # 未定義キーは {{t:key}} のまま残る
    ctools._program_translations = {}
    out = ctools._i18n_manifest({"description": "{{t:__nope__}}"})
    if out["description"] != "{{t:__nope__}}":
        failures.append(f"[manifest] 未定義キーが消えた: {out['description']!r}")

    # ----- 6. 親 i18n と programs i18n の API 仕様一致 -----
    # 親で同じ操作をしたときの戻り値と比較する
    from core.i18n import t as parent_t
    # 既に init_i18n(en) してあるので、未定義キーは {{t:key}} のまま
    if parent_t("__undef_compare__") != "{{t:__undef_compare__}}":
        failures.append(f"[api] 親 i18n の未定義キー戻り値が想定外: {parent_t('__undef_compare__')!r}")

    # ----- 結果 -----
    if failures:
        print("FAIL")
        for f in failures:
            print(" -", f)
        return 1

    print("PASS")
    print(f"  ja keys: {len(ja)}, en keys: {len(en)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
