# test_i18n_prompts.py
"""
LLMに渡る足場テキストの英語出し分けの回帰テスト。

citron テストと同じ流儀: pytest 不要・自前ランナー・venv で直接実行する。
    venv\\Scripts\\python.exe tests\\test_i18n_prompts.py

柚月サーバ（dev）は起動しない。i18n 辞書と各モジュールのキー解決を
関数レベルで検証するだけなので、柚月環境に英語を流し込むことはない。

カバー範囲（フェーズ2 + フェーズ3 全部）:
  - tool_*  : core/tools.py のツール定義の description（フェーズ2）
  - ctx_*   : core/context.py の毎ターン注入見出し・時刻・要約見出し（フェーズ2）
  - web_*   : core/web_tools.py の検索結果ラベル・エラー文（フェーズ3 S1）
  - rep_*   : core/repetition_guard.py の反復警告ヘッダ（フェーズ3 S2）
  - tool_*  : core/tools.py のツール実行結果ラベル・エラー文（フェーズ3 S3、tool_err_* 等）
  - wyrd_*  : core/wyrd_network.py の概念説明・記憶マップ生成プロンプト（フェーズ3 S6）
  - salia_* : core/salia.py の評価/フラッシュバック/雑記帳/モノローグ プロンプト（フェーズ3 S4）
  - agent_* : core/agent.py の警告/中断/通知/サマリ フォールバック（フェーズ3 S5）
  - wx_*    : core/weather.py の天気ラベル・テンプレ（フェーズ3 S7）

検証内容:
  1. lang/ja.json と lang/en.json でキー集合が完全一致（欠落ゼロ）。
  2. en: 各モジュールが使うキーが全て解決済み（{{t: なし）かつ英語（日本語残存なし）。
     （プレースホルダのみで構成される一部キーは日本語残存チェックを免除）
  3. ja: 同キーが全て解決済み。
  4. 全対象コードが import エラーなく読み込める。
  5. en で has_unfulfilled_declaration() が常に False（日本語固有regex のため短絡）。
  6. Layer0 構造マーカー（"user:" "task:" "city_event:" "moonbeat" <!-- layer0 -->）が
     agent.py 内に文字列リテラルとして残っている（翻訳されていない）。
"""

import json
import os
import re
import sys

# tests/ から見て1段上がリポジトリルート。core.* を import するため sys.path に追加。
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# --- 日本語文字判定 ---
def has_jp(s: str) -> bool:
    for c in s:
        # ひらがな・カタカナ / CJK統合漢字 / 全角記号類
        if "぀" <= c <= "ヿ" or "一" <= c <= "鿿" or "＀" <= c <= "￯":
            return True
    return False


def has_unresolved(s: str) -> bool:
    return "{{t:" in s


# context.py が実際に参照する ctx_* キー一覧（コードと手で同期）
CTX_KEYS = [
    "ctx_wd_mon", "ctx_wd_tue", "ctx_wd_wed", "ctx_wd_thu",
    "ctx_wd_fri", "ctx_wd_sat", "ctx_wd_sun",
    "ctx_datetime_format", "ctx_holiday_prefix",
    "ctx_hol_new_year", "ctx_hol_foundation", "ctx_hol_emperor_bday", "ctx_hol_showa",
    "ctx_hol_constitution", "ctx_hol_greenery", "ctx_hol_childrens", "ctx_hol_mountain",
    "ctx_hol_culture", "ctx_hol_labor_thanks", "ctx_hol_coming_of_age", "ctx_hol_marine",
    "ctx_hol_respect_aged", "ctx_hol_sports", "ctx_hol_vernal_equinox", "ctx_hol_autumnal_equinox",
    "ctx_usage", "ctx_image_default", "ctx_image_unsupported_note", "ctx_summary_heading",
]

# プレースホルダのみで構成される値は en でも「日本語を含まない」検査の対象外にする
# （ja でも記号と数字のみで日本語0文字、というケースを許容）。
PLACEHOLDER_OR_FORMAT = {
    "ctx_datetime_format",  # %Y-%m-%d ... 数字と記号のみ
}

# Layer0 構造マーカーの目印リテラル（翻訳禁止）
LAYER0_LITERALS = ['f"user: {content}"', 'f"task: {content}"', 'f"city_event: {content}"',
                   '"moonbeat"', '<!-- layer0 -->']

# 「ja で日本語を含む」検査をスキップするキー名パターン
JA_NON_JP_OK_KEYS = {
    "ctx_datetime_format",
    # WMO 天気ラベルや単位は ja でも英字や数字+単位のみのものがあり得るが、
    # 今は全て日本語訳済み。コードラベル類で純英字のものがあればここに追加する。
}


def _collect_descs(build_fn):
    """静的ツール定義から description を全て集める（関数説明＋各パラメータ説明）。"""
    out = []
    for tool in build_fn():
        fn = tool["function"]
        out.append((fn["name"], fn.get("description", "")))
        props = fn.get("parameters", {}).get("properties", {})
        for pname, p in props.items():
            if "description" in p:
                out.append((fn["name"] + "." + pname, p["description"]))
    return out


def _collect_module_keys(filepath: str, prefix: str) -> set:
    """対象モジュール内で t("prefix_xxx") として参照されているキーを抽出する。"""
    src = open(filepath, encoding="utf-8").read()
    return set(re.findall(r'\bt\("(' + prefix + r'[a-z_0-9]+)"', src))


def main() -> int:
    failures = []

    ja = json.load(open("lang/ja.json", encoding="utf-8"))
    en = json.load(open("lang/en.json", encoding="utf-8"))

    # ----- 1. 全キーで ja/en が完全一致 -----
    only_ja = sorted(set(ja) - set(en))
    only_en = sorted(set(en) - set(ja))
    if only_ja:
        failures.append(f"[keys] ja のみに存在: {only_ja[:5]}{'...' if len(only_ja) > 5 else ''}")
    if only_en:
        failures.append(f"[keys] en のみに存在: {only_en[:5]}{'...' if len(only_en) > 5 else ''}")

    # ----- 各プレフィックスの存在チェック -----
    for prefix in ("tool_", "ctx_", "web_", "rep_", "wyrd_", "salia_", "agent_", "wx_"):
        jk = set(k for k in ja if k.startswith(prefix))
        if not jk:
            failures.append(f"[keys] {prefix} キーが1つも無い")

    # ----- 2-3. コード内で参照されているキーが lang に存在するか -----
    module_targets = [
        ("core/tools.py", "tool_"),
        ("core/context.py", "ctx_"),
        ("core/web_tools.py", "web_"),
        ("core/repetition_guard.py", "rep_"),
        ("core/wyrd_network.py", "wyrd_"),
        ("core/salia.py", "salia_"),
        ("core/agent.py", "agent_"),
        ("core/weather.py", "wx_"),
    ]
    referenced = {}  # prefix -> set of keys referenced in code
    for path, prefix in module_targets:
        keys = _collect_module_keys(path, prefix)
        referenced.setdefault(prefix, set()).update(keys)
        for k in keys:
            if k not in ja:
                failures.append(f"[keys] ja に {k} が無い（{path} 参照）")
            if k not in en:
                failures.append(f"[keys] en に {k} が無い（{path} 参照）")

    # context.py の祝日/曜日キーは t() ではなく辞書経由なので明示リストで追加チェック
    for k in CTX_KEYS:
        if k not in ja:
            failures.append(f"[keys] ja に {k} が無い")
        if k not in en:
            failures.append(f"[keys] en に {k} が無い")

    # i18n とツール定義を読み込む
    from core.i18n import init_i18n, t
    from core.tools import _build_tool_definitions

    # ----- en: ツール description は英語・未解決なし -----
    init_i18n({"language": "en"})
    for name, desc in _collect_descs(_build_tool_definitions):
        if has_unresolved(desc):
            failures.append(f"[en/tool] 未解決マーカ: {name} -> {desc[:40]}")
        if has_jp(desc):
            failures.append(f"[en/tool] 日本語が残存: {name} -> {desc[:40]}")
    # en: ctx_ キーも英語・未解決なし
    for k in CTX_KEYS:
        v = t(k)
        if has_unresolved(v):
            failures.append(f"[en/ctx] 未解決マーカ: {k}")
        if k not in PLACEHOLDER_OR_FORMAT and has_jp(v):
            failures.append(f"[en/ctx] 日本語が残存: {k} -> {v}")

    # en: その他プレフィックスのキーも英語・未解決なし（コード参照されているもののみ）
    for prefix in ("web_", "rep_", "wyrd_", "salia_", "agent_", "wx_"):
        for k in referenced.get(prefix, set()):
            v = en.get(k, "")
            if has_unresolved(v):
                failures.append(f"[en/{prefix}] 未解決マーカ: {k}")
            if has_jp(v) and k not in JA_NON_JP_OK_KEYS:
                failures.append(f"[en/{prefix}] 日本語が残存: {k} -> {v[:40]}")

    # ----- ja: 全て解決済み -----
    init_i18n({"language": "ja"})
    for name, desc in _collect_descs(_build_tool_definitions):
        if has_unresolved(desc):
            failures.append(f"[ja/tool] 未解決マーカ: {name}")
        if not has_jp(desc):
            failures.append(f"[ja/tool] 日本語でない（翻訳漏れ?）: {name} -> {desc[:40]}")
    for k in CTX_KEYS:
        v = t(k)
        if has_unresolved(v):
            failures.append(f"[ja/ctx] 未解決マーカ: {k}")
    for prefix in ("web_", "rep_", "wyrd_", "salia_", "agent_", "wx_"):
        for k in referenced.get(prefix, set()):
            v = ja.get(k, "")
            if has_unresolved(v):
                failures.append(f"[ja/{prefix}] 未解決マーカ: {k}")

    # ----- 4. 対象モジュールが import できる（構文・依存の健全性）-----
    for path, _ in module_targets:
        modname = path.replace("/", ".").rstrip(".py")[:-3] if path.endswith(".py") else None
        # path: 'core/foo.py' -> 'core.foo'
        modname = path[:-3].replace("/", ".")
        try:
            __import__(modname)
        except Exception as e:
            failures.append(f"[import] {modname} の import に失敗: {e}")

    # ----- 5. has_unfulfilled_declaration の en 短絡 -----
    from core.agent import has_unfulfilled_declaration
    init_i18n({"language": "en"})
    if has_unfulfilled_declaration("I will write something."):
        failures.append("[en/declaration] en でも True を返した（短絡されていない）")
    if has_unfulfilled_declaration("雑記帳に書いてみようと思います。"):
        failures.append("[en/declaration] en で日本語入力にも True（短絡されていない）")
    init_i18n({"language": "ja"})
    if not has_unfulfilled_declaration("雑記帳に書いてみようと思います。"):
        failures.append("[ja/declaration] ja で True が返らない（regex 破損?）")

    # ----- 6. Layer0 構造マーカーが agent.py に文字列リテラルとして残っている -----
    agent_src = open("core/agent.py", encoding="utf-8").read()
    for lit in LAYER0_LITERALS:
        if lit not in agent_src:
            failures.append(f"[layer0] 構造マーカー {lit} が agent.py から消えている（翻訳した?）")

    # --- 結果 ---
    total_tool = len([1 for _ in _build_tool_definitions()])
    referenced_count = sum(len(v) for v in referenced.values())
    if failures:
        print("FAIL: %d 件" % len(failures))
        for f in failures:
            print("  - " + f)
        return 1
    print("PASS: tools=%d, ctx_keys=%d, referenced_keys=%d, ja/en matched, no unresolved/leak, "
          "declaration short-circuit OK, Layer0 markers intact"
          % (total_tool, len(CTX_KEYS), referenced_count))
    return 0


if __name__ == "__main__":
    sys.exit(main())
