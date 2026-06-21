"""
Citron AI Text Editor テスト
基本操作 + エラーメッセージ + バグ修正の回帰テスト
（pytest不要・python tests/test_citron_editor.py で直接実行）
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile

# テスト用のワークスペースを一時ディレクトリに作成
TEST_DIR = tempfile.mkdtemp(prefix="citron_test_")
os.environ["CG_WORKSPACE"] = TEST_DIR

# main.py を直接インポート (tests/ から見て1段上がリポジトリルート)
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MAIN_PY = os.path.join(REPO_ROOT, "programs", "citron_ai_text_editor", "main.py")
# programs/ を通すことで main.py 内の `from _i18n import t` を解決
PROGRAMS_DIR = os.path.dirname(os.path.dirname(MAIN_PY))
sys.path.insert(0, PROGRAMS_DIR)
sys.path.insert(0, os.path.dirname(MAIN_PY))
import main as citron

# subprocess 起動時にも i18n が引けるよう、共通の env を組み立てる
_SUBPROC_ENV_BASE = {
    "CG_WORKSPACE": TEST_DIR,
    "PYTHONIOENCODING": "utf-8",
    # CG_LANG はテストの前提（ja の文言で assert している）に合わせ明示
    "CG_LANG": "ja",
    # programs/_i18n.py を import 可能にする
    "PYTHONPATH": PROGRAMS_DIR + (os.pathsep + os.environ.get("PYTHONPATH", "") if os.environ.get("PYTHONPATH") else ""),
}

passed = 0
failed = 0


def setup():
    """テスト前にセッション（新旧パスとも）をクリーンアップ"""
    for d in (os.path.join(TEST_DIR, "program_data"), os.path.join(TEST_DIR, ".sessions")):
        if os.path.exists(d):
            shutil.rmtree(d)


def create_test_file(name="test.md", content="line1\nline2\nline3\nline4\nline5\n", binary=False):
    """テスト用ファイルを作成"""
    path = os.path.join(TEST_DIR, name)
    if binary:
        with open(path, "wb") as f:
            f.write(content)
    else:
        with open(path, "w", encoding="utf-8", newline="") as f:
            f.write(content)
    return path


def read_bytes(name="test.md"):
    with open(os.path.join(TEST_DIR, name), "rb") as f:
        return f.read()


def run_test(name, fn):
    global passed, failed
    setup()
    try:
        fn()
        print(f"[OK] {name}")
        passed += 1
    except AssertionError as e:
        print(f"[NG] {name}: {e}")
        failed += 1
    except Exception as e:
        print(f"[NG] {name}: 例外 {e}")
        failed += 1

# --- 基本テスト群 ---

def test_01_open_no_file_arg():
    """open: file引数なしで例示あり"""
    r = citron.cmd_open({})
    assert r["status"] == "error"
    assert "例:" in r["message"], f"例が含まれていない: {r['message']}"
    assert '"command": "open"' in r["message"]

def test_02_open_file_not_found():
    """open: 存在しないファイルで例示あり"""
    r = citron.cmd_open({"file": "nonexistent.md"})
    assert r["status"] == "error"
    assert "workspace" in r["message"]

def test_03_open_success():
    """open: 正常なファイルopen"""
    create_test_file()
    r = citron.cmd_open({"file": "test.md"})
    assert r["status"] == "success"
    assert r["data"]["total_lines"] == 5

def test_04_open_duplicate_session():
    """open: 二重openでファイル名とcommit/discard例示あり"""
    create_test_file()
    r = citron.cmd_open({"file": "test.md"})
    assert r["status"] == "success"
    r = citron.cmd_open({"file": "test.md"})
    assert r["status"] == "error"
    assert "test.md" in r["message"]
    assert "commit" in r["message"]
    assert "discard" in r["message"]

def test_05_view_no_session():
    """view: セッションなしでNO_SESSIONメッセージ"""
    r = citron.cmd_view({})
    assert r["status"] == "error"
    assert "open" in r["message"]
    assert "例:" in r["message"]

def test_06_view_success():
    """view: 正常表示（範囲指定）"""
    create_test_file()
    citron.cmd_open({"file": "test.md"})
    r = citron.cmd_view({"start": "1", "end": "3"})
    assert r["status"] == "success"
    shown = r["data"]["content"].split("\n")
    assert len(shown) == 3, f"3行のはずが: {shown}"
    assert shown[0] == "1: line1"

def test_07_rewrite_no_line():
    """rewrite: line引数なしで例示あり"""
    create_test_file()
    citron.cmd_open({"file": "test.md"})
    r = citron.cmd_rewrite({})
    assert r["status"] == "error"
    assert "例:" in r["message"]
    assert '"command": "rewrite"' in r["message"]

def test_08_rewrite_no_content():
    """rewrite: content引数なしで例示あり"""
    create_test_file()
    citron.cmd_open({"file": "test.md"})
    r = citron.cmd_rewrite({"line": "1"})
    assert r["status"] == "error"
    assert "例:" in r["message"]

def test_09_rewrite_invalid_line():
    """rewrite: 不正な行番号で行番号フォーマット例示あり"""
    create_test_file()
    citron.cmd_open({"file": "test.md"})
    r = citron.cmd_rewrite({"line": "abc", "content": "x"})
    assert r["status"] == "error"
    assert "通常行" in r["message"]
    assert "挿入行" in r["message"]

def test_10_rewrite_nonexistent_line():
    """rewrite: 存在しない行番号でview誘導あり"""
    create_test_file()
    citron.cmd_open({"file": "test.md"})
    r = citron.cmd_rewrite({"line": "999", "content": "x"})
    assert r["status"] == "error"
    assert "view" in r["message"]

def test_11_rewrite_success():
    """rewrite: 正常な書き換え"""
    create_test_file()
    citron.cmd_open({"file": "test.md"})
    r = citron.cmd_rewrite({"line": "1", "content": "updated"})
    assert r["status"] == "success"
    assert r["data"]["result"] == "1: updated"

def test_12_delete_no_start():
    """delete: start引数なしで例示あり"""
    create_test_file()
    citron.cmd_open({"file": "test.md"})
    r = citron.cmd_delete({})
    assert r["status"] == "error"
    assert "例" in r["message"]

def test_13_delete_success():
    """delete: 正常な削除"""
    create_test_file()
    citron.cmd_open({"file": "test.md"})
    r = citron.cmd_delete({"start": "2"})
    assert r["status"] == "success"
    assert "2" in r["data"]["deleted_lines"]

def test_14_insert_no_after():
    """insert: after引数なしで例示あり"""
    create_test_file()
    citron.cmd_open({"file": "test.md"})
    r = citron.cmd_insert({})
    assert r["status"] == "error"
    assert "例:" in r["message"]

def test_15_insert_success():
    """insert: 正常な挿入"""
    create_test_file()
    citron.cmd_open({"file": "test.md"})
    r = citron.cmd_insert({"after": "3", "content": "new line"})
    assert r["status"] == "success"
    assert r["data"]["inserted"] == "3i1: new line"

def test_16_undo_empty():
    """undo: スタック空で「取り消せる操作がありません」"""
    create_test_file()
    citron.cmd_open({"file": "test.md"})
    r = citron.cmd_undo({})
    assert r["status"] == "error"
    assert "取り消せる" in r["message"]

def test_17_redo_empty():
    """redo: スタック空で「やり直せる操作がありません」+ redo履歴クリア情報"""
    create_test_file()
    citron.cmd_open({"file": "test.md"})
    r = citron.cmd_redo({})
    assert r["status"] == "error"
    assert "やり直せる" in r["message"]
    assert "クリア" in r["message"]

def test_18_commit_success():
    """commit: 正常なコミット"""
    create_test_file()
    citron.cmd_open({"file": "test.md"})
    citron.cmd_rewrite({"line": "1", "content": "modified"})
    r = citron.cmd_commit({})
    assert r["status"] == "success"
    with open(os.path.join(TEST_DIR, "test.md"), "r", encoding="utf-8") as f:
        content = f.read()
    assert "modified" in content

def test_19_help_command():
    """help: コマンド一覧が返る"""
    r = citron.cmd_help({})
    assert r["status"] == "success"
    assert "commands" in r["data"]
    assert "open" in r["data"]["commands"]
    assert "help" in r["data"]["commands"]
    assert "line_format" in r["data"]

def test_20_replace_no_search():
    """replace: search引数なしで例示あり"""
    create_test_file()
    citron.cmd_open({"file": "test.md"})
    r = citron.cmd_replace({})
    assert r["status"] == "error"
    assert "例:" in r["message"]
    assert '"command": "replace"' in r["message"]

def test_21_status_no_session():
    """status: セッションなしでNO_SESSIONメッセージ"""
    r = citron.cmd_status({})
    assert r["status"] == "error"
    assert "open" in r["message"]

def test_22_rewrite_deleted_line():
    """rewrite: 削除済み行にundoまたはinsert誘導あり"""
    create_test_file()
    citron.cmd_open({"file": "test.md"})
    citron.cmd_delete({"start": "1"})
    r = citron.cmd_rewrite({"line": "1", "content": "x"})
    assert r["status"] == "error"
    assert "undo" in r["message"]
    assert "insert" in r["message"]

def test_23_discard_idempotent():
    """discard: セッションなしでもエラーにならない"""
    r = citron.cmd_discard({})
    assert r["status"] == "success"

# --- バグ修正の回帰テスト ---

def test_24_commit_preserves_crlf():
    """commit: CRLFファイルはCRLFのまま"""
    create_test_file(content=b"line1\r\nline2\r\nline3\r\n", binary=True)
    citron.cmd_open({"file": "test.md"})
    citron.cmd_rewrite({"line": "2", "content": "updated"})
    r = citron.cmd_commit({})
    assert r["status"] == "success"
    data = read_bytes()
    assert data == b"line1\r\nupdated\r\nline3\r\n", f"CRLFが保持されていない: {data}"

def test_25_commit_preserves_lf():
    """commit: LFファイルはLFのまま（CRLF化しない）"""
    create_test_file(content=b"line1\nline2\nline3\n", binary=True)
    citron.cmd_open({"file": "test.md"})
    citron.cmd_rewrite({"line": "2", "content": "updated"})
    r = citron.cmd_commit({})
    assert r["status"] == "success"
    data = read_bytes()
    assert b"\r" not in data, f"LFファイルにCRが混入: {data}"
    assert data == b"line1\nupdated\nline3\n"

def test_26_commit_preserves_no_trailing_newline():
    """commit: 末尾改行なしのファイルに改行を追加しない"""
    create_test_file(content=b"line1\nline2", binary=True)
    citron.cmd_open({"file": "test.md"})
    citron.cmd_rewrite({"line": "1", "content": "updated"})
    r = citron.cmd_commit({})
    assert r["status"] == "success"
    data = read_bytes()
    assert data == b"updated\nline2", f"末尾改行が変化: {data}"

def test_27_traversal_dotdot_blocked():
    """open: ../ によるworkspace外アクセスを拒否"""
    outside = os.path.join(os.path.dirname(TEST_DIR), "citron_evil.txt")
    with open(outside, "w", encoding="utf-8") as f:
        f.write("secret\n")
    try:
        r = citron.cmd_open({"file": "../citron_evil.txt"})
        assert r["status"] == "error", f"workspace外が開けてしまった: {r}"
        assert "ワークスペース外" in r["message"]
    finally:
        os.remove(outside)

def test_28_traversal_sibling_prefix_blocked():
    """open: 前方一致で通っていた兄弟ディレクトリ（ws→ws2）を拒否"""
    sibling = TEST_DIR + "2"
    os.makedirs(sibling, exist_ok=True)
    with open(os.path.join(sibling, "secret.txt"), "w", encoding="utf-8") as f:
        f.write("secret\n")
    try:
        rel = "../" + os.path.basename(sibling) + "/secret.txt"
        r = citron.cmd_open({"file": rel})
        assert r["status"] == "error", f"兄弟ディレクトリが開けてしまった: {r}"
    finally:
        shutil.rmtree(sibling, ignore_errors=True)

def test_29_discard_removes_old_path_session():
    """discard: 旧パス(.sessions/)のセッションも削除しゾンビ化しない"""
    create_test_file()
    # 旧パスにセッションを置く（旧バージョンからの残留を再現）
    old_dir = os.path.join(TEST_DIR, ".sessions")
    os.makedirs(old_dir, exist_ok=True)
    with open(os.path.join(old_dir, "text_editor.json"), "w", encoding="utf-8") as f:
        json.dump({"file_path": "old.md", "lines": {}, "line_order": [],
                   "undo_stack": [], "redo_stack": []}, f)
    r = citron.cmd_open({"file": "test.md"})
    assert r["status"] == "error"  # 旧セッションが見える
    r = citron.cmd_discard({})
    assert r["status"] == "success"
    r = citron.cmd_open({"file": "test.md"})
    assert r["status"] == "success", f"discard後もセッションが残留: {r}"

def test_30_replace_no_match_keeps_undo():
    """replace: 0件マッチはundoスロットを消費せずredoも保持"""
    create_test_file()
    citron.cmd_open({"file": "test.md"})
    citron.cmd_rewrite({"line": "1", "content": "updated"})
    citron.cmd_undo({})  # redoが1つできる
    r = citron.cmd_replace({"search": "存在しない文字列", "replacement": "x"})
    assert r["status"] == "success"
    assert r["data"]["matches"] == 0
    s = citron.cmd_status({})
    assert s["data"]["remaining_redos"] == 1, f"0件replaceがredo履歴を消した: {s['data']}"
    assert s["data"]["remaining_undos"] == 0, f"0件replaceがundoを積んだ: {s['data']}"

def test_31_delete_already_deleted_keeps_undo():
    """delete: 全行削除済みの再deleteはundoスロットを消費しない"""
    create_test_file()
    citron.cmd_open({"file": "test.md"})
    citron.cmd_delete({"start": "2"})
    r = citron.cmd_delete({"start": "2"})
    assert r["status"] == "success"
    assert r["data"]["deleted_count"] == 0
    s = citron.cmd_status({})
    assert s["data"]["remaining_undos"] == 1, f"no-op deleteがundoを積んだ: {s['data']}"

def test_32_open_invalid_range_no_session():
    """open: start/endが不正な場合はエラーでセッションを作らない"""
    create_test_file()
    r = citron.cmd_open({"file": "test.md", "start": "abc"})
    assert r["status"] == "error"
    assert "行番号" in r["message"]
    s = citron.cmd_status({})
    assert s["status"] == "error", "不正openでセッションが残った"

def test_34_open_not_found_suggests_create():
    """open: ファイル不存在のエラーがcreateの使い方を案内する"""
    r = citron.cmd_open({"file": "newfile.md"})
    assert r["status"] == "error"
    assert '"create": true' in r["message"], f"create案内がない: {r['message']}"

def test_35_create_new_file_with_content():
    """open+create: 初期内容つき新規作成 → commitで実ファイル生成"""
    r = citron.cmd_open({"file": "created.md", "create": True, "content": "line1\nline2"})
    assert r["status"] == "success"
    assert r["data"]["total_lines"] == 2
    assert "commit" in r["message"]  # 実ファイルはまだ無いことを案内
    assert not os.path.exists(os.path.join(TEST_DIR, "created.md")), "commit前にファイルが作られた"
    r = citron.cmd_commit({})
    assert r["status"] == "success"
    assert read_bytes("created.md") == b"line1\nline2\n"

def test_36_create_empty_then_insert_at_top():
    """open+create(内容なし) → after='0'で書き始め → commit"""
    r = citron.cmd_open({"file": "empty.md", "create": True})
    assert r["status"] == "success"
    assert r["data"]["total_lines"] == 0
    assert '"after": "0"' in r["message"], f"先頭挿入の案内がない: {r['message']}"
    r = citron.cmd_insert({"after": "0", "content": "first\nsecond"})
    assert r["status"] == "success"
    r = citron.cmd_commit({})
    assert r["status"] == "success"
    assert read_bytes("empty.md") == b"first\nsecond\n"

def test_37_create_existing_file_opens_normally():
    """open+create: 既存ファイルなら普通に開き、contentは適用せず明示する"""
    create_test_file()
    r = citron.cmd_open({"file": "test.md", "create": True, "content": "上書きされてはいけない"})
    assert r["status"] == "success"
    assert r["data"]["total_lines"] == 5
    assert "適用されていません" in r["message"], f"content無視の明示がない: {r['message']}"
    citron.cmd_discard({})
    assert b"line1" in read_bytes("test.md"), "既存ファイルが壊れた"

def test_38_create_discard_leaves_nothing():
    """open+create → discard: 実ファイルを作らず痕跡ゼロ"""
    citron.cmd_open({"file": "ghost.md", "create": True, "content": "x"})
    r = citron.cmd_discard({})
    assert r["status"] == "success"
    assert not os.path.exists(os.path.join(TEST_DIR, "ghost.md")), "discardしたのにファイルが残った"

def test_39_create_traversal_blocked():
    """open+create: workspace外への新規作成も拒否"""
    r = citron.cmd_open({"file": "../evil_new.txt", "create": True, "content": "x"})
    assert r["status"] == "error"
    assert "ワークスペース外" in r["message"]

def test_40_create_conflict_external_file():
    """commit: open(create)後に外部で同名ファイルが作られたら上書きせず中止"""
    citron.cmd_open({"file": "race.md", "create": True, "content": "session側"})
    create_test_file("race.md", content="外部で作られた\n")
    r = citron.cmd_commit({})
    assert r["status"] == "error", "外部作成ファイルを上書きしてしまう"
    assert read_bytes("race.md") == "外部で作られた\n".encode("utf-8")
    citron.cmd_discard({})

def test_41_insert_top_existing_file():
    """insert: after='0'で既存ファイルの先頭に挿入できる"""
    create_test_file()
    citron.cmd_open({"file": "test.md"})
    r = citron.cmd_insert({"after": "0", "content": "header"})
    assert r["status"] == "success"
    assert "先頭" in r["message"]
    # 2回目の先頭挿入は既存の0i行の後（= line1の前）に入る
    r = citron.cmd_insert({"after": "0", "content": "header2"})
    assert r["status"] == "success"
    citron.cmd_commit({})
    assert read_bytes() == b"header\nheader2\nline1\nline2\nline3\nline4\nline5\n"

def test_42_insert_nonexistent_line_suggests_zero():
    """insert: 存在しない行のエラーがafter='0'を案内する"""
    create_test_file()
    citron.cmd_open({"file": "test.md"})
    r = citron.cmd_insert({"after": "999", "content": "x"})
    assert r["status"] == "error"
    assert '"after": "0"' in r["message"]

def test_43_create_accepts_string_true():
    """open+create: 文字列の "true" でも新規作成できる（AIの型ミス許容）"""
    r = citron.cmd_open({"file": "strbool.md", "create": "true", "content": "x"})
    assert r["status"] == "success"
    citron.cmd_discard({})

def test_44_search_basic():
    """search: 部分一致でマッチ行番号と内容を返す"""
    create_test_file()
    citron.cmd_open({"file": "test.md"})
    r = citron.cmd_search({"query": "line3"})
    assert r["status"] == "success"
    assert r["data"]["match_count"] == 1
    assert r["data"]["matched_lines"] == ["3"]
    assert r["data"]["content"] == "3: line3"

def test_45_search_no_query():
    """search: query引数なしで例示あり"""
    create_test_file()
    citron.cmd_open({"file": "test.md"})
    r = citron.cmd_search({})
    assert r["status"] == "error"
    assert '"command": "search"' in r["message"]

def test_46_search_no_match():
    """search: マッチなしでもsuccessでcount 0（エラーにしない）"""
    create_test_file()
    citron.cmd_open({"file": "test.md"})
    r = citron.cmd_search({"query": "存在しない文字列"})
    assert r["status"] == "success"
    assert r["data"]["match_count"] == 0

def test_47_search_context():
    """search: contextでマッチ行の前後を表示、離れたマッチは---区切り"""
    create_test_file(content="a\nMATCH\nb\nc\nd\nMATCH\ne\n")
    citron.cmd_open({"file": "test.md"})
    r = citron.cmd_search({"query": "MATCH", "context": 1})
    assert r["status"] == "success"
    assert r["data"]["match_count"] == 2
    expected = "1: a\n2: MATCH\n3: b\n---\n5: d\n6: MATCH\n7: e"
    assert r["data"]["content"] == expected, f"context表示が不正: {r['data']['content']!r}"

def test_48_search_skips_deleted_includes_inserted():
    """search: 削除行は対象外、挿入行は対象"""
    create_test_file()
    citron.cmd_open({"file": "test.md"})
    citron.cmd_delete({"start": "2"})  # "line2" を削除
    citron.cmd_insert({"after": "3", "content": "line2 inserted"})
    r = citron.cmd_search({"query": "line2"})
    assert r["data"]["matched_lines"] == ["3i1"], f"削除行が混入or挿入行が漏れ: {r['data']}"

def test_49_open_non_utf8_friendly_error():
    """open: UTF-8以外のファイルで親切なエラー（生の例外を出さない）"""
    create_test_file("sjis.txt", content="日本語テキスト\n".encode("cp932"), binary=True)
    r = citron.cmd_open({"file": "sjis.txt"})
    assert r["status"] == "error"
    assert "UTF-8" in r["message"], f"UTF-8の説明がない: {r['message']}"
    assert "codec" not in r["message"], "生の例外メッセージが露出している"
    # セッションが残っていないこと
    assert citron.cmd_status({})["status"] == "error"

def test_50_move_basic():
    """move: 範囲を内容の打ち直しなしで移動できる"""
    create_test_file()
    citron.cmd_open({"file": "test.md"})
    r = citron.cmd_move({"start": "2", "end": "3", "after": "5"})
    assert r["status"] == "success"
    citron.cmd_commit({})
    assert read_bytes() == b"line1\nline4\nline5\nline2\nline3\n"

def test_51_move_to_top():
    """move: after='0'でファイル先頭へ移動"""
    create_test_file()
    citron.cmd_open({"file": "test.md"})
    r = citron.cmd_move({"start": "4", "end": "5", "after": "0"})
    assert r["status"] == "success"
    assert "先頭" in r["message"]
    citron.cmd_commit({})
    assert read_bytes() == b"line4\nline5\nline1\nline2\nline3\n"

def test_52_move_into_own_range_blocked():
    """move: 移動先が移動範囲内ならエラー"""
    create_test_file()
    citron.cmd_open({"file": "test.md"})
    r = citron.cmd_move({"start": "2", "end": "4", "after": "3"})
    assert r["status"] == "error"
    assert "範囲内" in r["message"]
    citron.cmd_discard({})

def test_53_copy_basic():
    """copy: 元の行を残したまま複製できる"""
    create_test_file()
    citron.cmd_open({"file": "test.md"})
    r = citron.cmd_copy({"start": "1", "after": "5"})
    assert r["status"] == "success"
    citron.cmd_commit({})
    assert read_bytes() == b"line1\nline2\nline3\nline4\nline5\nline1\n"

def test_54_move_undo():
    """move: undoで移動前の状態に戻る"""
    create_test_file()
    citron.cmd_open({"file": "test.md"})
    citron.cmd_move({"start": "2", "end": "3", "after": "5"})
    r = citron.cmd_undo({})
    assert r["status"] == "success"
    citron.cmd_commit({})
    assert read_bytes() == b"line1\nline2\nline3\nline4\nline5\n"

def test_55_manifest_enum_matches_commands():
    """manifestのcommand enumが実装のCOMMANDSと完全一致すること。
    ズレると、正しいコマンドがツール側で「無効な値」として弾かれる事故になる。"""
    import yaml
    manifest_path = os.path.join(os.path.dirname(MAIN_PY), "manifest.yaml")
    with open(manifest_path, encoding="utf-8") as f:
        manifest = yaml.safe_load(f)
    cmd_arg = next(a for a in manifest["args"] if a["name"] == "command")
    manifest_enum = set(cmd_arg["enum"])
    impl_commands = set(citron.COMMANDS.keys())
    assert manifest_enum == impl_commands, (
        f"manifest enum と COMMANDS が不一致。"
        f"\n  manifestのみ: {manifest_enum - impl_commands}"
        f"\n  実装のみ: {impl_commands - manifest_enum}"
    )

def test_33_main_non_dict_json():
    """main: dict以外のJSON入力でもJSONエラーを返す（stdout契約）"""
    p = subprocess.run(
        [sys.executable, MAIN_PY],
        input='"abc"', capture_output=True, text=True, encoding="utf-8",
        # 子プロセスのstdoutがcp932にならないようUTF-8を強制
        env={**os.environ, **_SUBPROC_ENV_BASE},
    )
    out = json.loads(p.stdout)
    assert out["status"] == "error"
    assert "JSONオブジェクト" in out["message"]

def test_56_insert_then_delete_no_phantom_diff():
    """insert→delete（実質無変更）: changes集計が歪まず、commitも書き込まない"""
    create_test_file(content="a\nb\n")
    citron.cmd_open({"file": "test.md"})
    citron.cmd_insert({"after": "1", "content": "X"})
    citron.cmd_delete({"start": "1i1"})  # たった今挿入した行を削除
    r = citron.cmd_status({})
    assert r["data"]["inserted"] == 0, f"insertedが残っている: {r['data']}"
    assert r["data"]["deleted"] == 0, f"挿入削除行がdeletedに化けた: {r['data']}"
    r = citron.cmd_commit({})
    assert r["status"] == "success"
    assert read_bytes("test.md") == b"a\nb\n", "実質無変更なのにファイルが変わった"

def test_57_reverse_range_explicit_error():
    """start>end の破壊的コマンドは「範囲指定が逆」を明示する"""
    create_test_file()
    citron.cmd_open({"file": "test.md"})
    cases = [
        (citron.cmd_delete, {"start": "5", "end": "2"}),
        (citron.cmd_replace, {"search": "line", "replacement": "x", "start": "5", "end": "2"}),
        (citron.cmd_move, {"start": "5", "end": "2", "after": "1"}),
    ]
    for fn, a in cases:
        r = fn(a)
        assert r["status"] == "error" and "範囲指定が逆" in r["message"], f"{fn.__name__}: {r}"
    citron.cmd_discard({})

def test_58_corrupted_session_friendly_error():
    """破損セッション: 内部エラーでなくdiscard誘導の明示エラー（stdout契約）"""
    sdir = os.path.join(TEST_DIR, "program_data", "citron_ai_text_editor")
    os.makedirs(sdir, exist_ok=True)
    with open(os.path.join(sdir, "text_editor.json"), "w", encoding="utf-8") as f:
        f.write("{ broken json")
    p = subprocess.run(
        [sys.executable, MAIN_PY],
        input=json.dumps({"command": "view"}), capture_output=True, text=True, encoding="utf-8",
        env={**os.environ, **_SUBPROC_ENV_BASE},
    )
    out = json.loads(p.stdout)
    assert out["status"] == "error"
    assert "壊れて" in out["message"] and "discard" in out["message"], out

def test_59_mixed_newline_noop_preserved():
    """混在改行: 内容が元に戻ればcommitは書き込まず混在改行を保持する"""
    with open(os.path.join(TEST_DIR, "mix.md"), "wb") as f:
        f.write(b"a\r\nb\nc\r\n")
    citron.cmd_open({"file": "mix.md"})
    citron.cmd_insert({"after": "1", "content": "T"})
    citron.cmd_delete({"start": "1i1"})
    r = citron.cmd_commit({})
    assert r["status"] == "success"
    assert read_bytes("mix.md") == b"a\r\nb\nc\r\n", f"混在改行が壊れた: {read_bytes('mix.md')!r}"

def test_60_mutation_response_truncated():
    """insert等の戻り値も表示トークン上限で切り詰める（巨大編集でのコンテキスト保護）"""
    create_test_file(content="x\n")
    citron.cmd_open({"file": "test.md"})
    big = "\n".join("L%d_%s" % (i, "z" * 50) for i in range(2000))
    r = citron.cmd_insert({"after": "1", "content": big})
    assert r["data"]["truncated"] is True, "巨大insertが切り詰められていない"
    assert citron.count_tokens(r["data"]["inserted"]) < citron.DISPLAY_TOKEN_LIMIT + 200, "切り詰め後も大きすぎる"
    citron.cmd_discard({})

def test_61_display_reverse_range_error():
    """view/search/open でも start>end は明示エラー"""
    create_test_file()
    citron.cmd_open({"file": "test.md"})
    r = citron.cmd_view({"start": "5", "end": "2"})
    assert r["status"] == "error" and "範囲指定が逆" in r["message"], r
    r = citron.cmd_search({"query": "line", "start": "5", "end": "2"})
    assert r["status"] == "error" and "範囲指定が逆" in r["message"], r
    citron.cmd_discard({})
    # open の表示範囲逆転はセッションを残さない
    r = citron.cmd_open({"file": "test.md", "start": "5", "end": "2"})
    assert r["status"] == "error" and "範囲指定が逆" in r["message"], r
    assert citron.load_session() is None, "逆範囲openでセッションが残った"

def test_62_schema_invalid_session_friendly_error():
    """構文は正しいがスキーマ不正なセッション({})も discard 誘導の明示エラー（stdout契約）"""
    sdir = os.path.join(TEST_DIR, "program_data", "citron_ai_text_editor")
    os.makedirs(sdir, exist_ok=True)
    with open(os.path.join(sdir, "text_editor.json"), "w", encoding="utf-8") as f:
        f.write("{}")
    p = subprocess.run(
        [sys.executable, MAIN_PY],
        input=json.dumps({"command": "view"}), capture_output=True, text=True, encoding="utf-8",
        env={**os.environ, **_SUBPROC_ENV_BASE},
    )
    out = json.loads(p.stdout)
    assert out["status"] == "error"
    assert "形式が不正" in out["message"] and "discard" in out["message"], out

def test_63_commit_write_failure_friendly_error():
    """commitの書き込み/置換失敗(os.replace)は内部エラーでなく自己回復用エラー"""
    create_test_file(content="x\n")
    citron.cmd_open({"file": "test.md"})
    citron.cmd_rewrite({"line": "1", "content": "y"})
    orig = citron.os.replace
    citron.os.replace = lambda *a, **k: (_ for _ in ()).throw(PermissionError("locked (simulated)"))
    try:
        r = citron.cmd_commit({})
    finally:
        citron.os.replace = orig
    assert r["status"] == "error" and "書き込めません" in r["message"] and "内部エラー" not in r["message"], r
    citron.cmd_discard({})

def test_64_search_finds_top_inserted_line_with_start():
    """search: start="1" 指定でも先頭挿入行(0i*)を取りこぼさない（viewと整合）。
    回帰: 以前は cmd_search が include_head=False で、view start="1" では見えるのに
    search start="1" では見つからない不整合があった。"""
    create_test_file(content="alpha\nbeta\ngamma\n")
    citron.cmd_open({"file": "test.md"})
    citron.cmd_insert({"after": "0", "content": "HEADLINE-zzz"})  # 先頭挿入 → 0i1
    # start無し: 元々ヒットしていた
    r_all = citron.cmd_search({"query": "HEADLINE-zzz"})
    assert r_all["data"]["matched_lines"] == ["0i1"], r_all
    # start="1": viewでは0i1が見えるので、searchでも見つかるべき
    r_start = citron.cmd_search({"query": "HEADLINE-zzz", "start": "1"})
    assert r_start["data"]["match_count"] == 1, f"先頭挿入行をsearchが取りこぼした: {r_start['data']}"
    assert r_start["data"]["matched_lines"] == ["0i1"], r_start
    citron.cmd_discard({})

# --- 実行 ---
tests = [
    ("test_01 open: file引数なし", test_01_open_no_file_arg),
    ("test_02 open: ファイル不存在", test_02_open_file_not_found),
    ("test_03 open: 正常", test_03_open_success),
    ("test_04 open: 二重セッション", test_04_open_duplicate_session),
    ("test_05 view: セッションなし", test_05_view_no_session),
    ("test_06 view: 正常表示", test_06_view_success),
    ("test_07 rewrite: line引数なし", test_07_rewrite_no_line),
    ("test_08 rewrite: content引数なし", test_08_rewrite_no_content),
    ("test_09 rewrite: 不正行番号", test_09_rewrite_invalid_line),
    ("test_10 rewrite: 存在しない行", test_10_rewrite_nonexistent_line),
    ("test_11 rewrite: 正常", test_11_rewrite_success),
    ("test_12 delete: start引数なし", test_12_delete_no_start),
    ("test_13 delete: 正常", test_13_delete_success),
    ("test_14 insert: after引数なし", test_14_insert_no_after),
    ("test_15 insert: 正常", test_15_insert_success),
    ("test_16 undo: スタック空", test_16_undo_empty),
    ("test_17 redo: スタック空", test_17_redo_empty),
    ("test_18 commit: 正常", test_18_commit_success),
    ("test_19 help: コマンド一覧", test_19_help_command),
    ("test_20 replace: search引数なし", test_20_replace_no_search),
    ("test_21 status: セッションなし", test_21_status_no_session),
    ("test_22 rewrite: 削除済み行", test_22_rewrite_deleted_line),
    ("test_23 discard: 冪等", test_23_discard_idempotent),
    ("test_24 commit: CRLF保持", test_24_commit_preserves_crlf),
    ("test_25 commit: LF保持", test_25_commit_preserves_lf),
    ("test_26 commit: 末尾改行なし保持", test_26_commit_preserves_no_trailing_newline),
    ("test_27 open: ../トラバーサル拒否", test_27_traversal_dotdot_blocked),
    ("test_28 open: 兄弟ディレクトリ拒否", test_28_traversal_sibling_prefix_blocked),
    ("test_29 discard: 旧パスセッション掃除", test_29_discard_removes_old_path_session),
    ("test_30 replace: 0件でundo不消費", test_30_replace_no_match_keeps_undo),
    ("test_31 delete: no-opでundo不消費", test_31_delete_already_deleted_keeps_undo),
    ("test_32 open: 不正範囲でセッション残さず", test_32_open_invalid_range_no_session),
    ("test_33 main: dict以外のJSON入力", test_33_main_non_dict_json),
    ("test_34 open: 不存在エラーでcreate案内", test_34_open_not_found_suggests_create),
    ("test_35 create: 初期内容つき新規作成", test_35_create_new_file_with_content),
    ("test_36 create: 空から先頭挿入で作成", test_36_create_empty_then_insert_at_top),
    ("test_37 create: 既存ファイルは上書きしない", test_37_create_existing_file_opens_normally),
    ("test_38 create: discardで痕跡ゼロ", test_38_create_discard_leaves_nothing),
    ("test_39 create: トラバーサル拒否", test_39_create_traversal_blocked),
    ("test_40 create: 外部作成と競合時は中止", test_40_create_conflict_external_file),
    ("test_41 insert: after='0'で先頭挿入", test_41_insert_top_existing_file),
    ("test_42 insert: 不存在行エラーで'0'案内", test_42_insert_nonexistent_line_suggests_zero),
    ("test_43 create: 文字列'true'を許容", test_43_create_accepts_string_true),
    ("test_44 search: 基本マッチ", test_44_search_basic),
    ("test_45 search: query引数なし", test_45_search_no_query),
    ("test_46 search: マッチなしはsuccess", test_46_search_no_match),
    ("test_47 search: context表示と区切り", test_47_search_context),
    ("test_48 search: 削除除外・挿入対象", test_48_search_skips_deleted_includes_inserted),
    ("test_49 open: 非UTF-8で親切エラー", test_49_open_non_utf8_friendly_error),
    ("test_50 move: 基本移動", test_50_move_basic),
    ("test_51 move: 先頭へ移動", test_51_move_to_top),
    ("test_52 move: 範囲内への移動拒否", test_52_move_into_own_range_blocked),
    ("test_53 copy: 基本複製", test_53_copy_basic),
    ("test_54 move: undoで復元", test_54_move_undo),
    ("test_55 manifest enum == COMMANDS", test_55_manifest_enum_matches_commands),
    ("test_56 insert→delete: 集計が歪まない", test_56_insert_then_delete_no_phantom_diff),
    ("test_57 範囲逆転: 明示エラー", test_57_reverse_range_explicit_error),
    ("test_58 破損セッション: discard誘導", test_58_corrupted_session_friendly_error),
    ("test_59 混在改行: no-opで保持", test_59_mixed_newline_noop_preserved),
    ("test_60 mutation戻り値: 切り詰め", test_60_mutation_response_truncated),
    ("test_61 表示系: 範囲逆転エラー", test_61_display_reverse_range_error),
    ("test_62 スキーマ不正セッション: discard誘導", test_62_schema_invalid_session_friendly_error),
    ("test_63 commit書き込み失敗: 親切エラー", test_63_commit_write_failure_friendly_error),
    ("test_64 search: start指定でも先頭挿入行を拾う", test_64_search_finds_top_inserted_line_with_start),
]

print(f"\n=== Citron AI Text Editor テスト ({len(tests)}件) ===\n")
for name, fn in tests:
    run_test(name, fn)

print(f"\n=== 結果: {passed}/{passed+failed} 成功 ===")

# クリーンアップ
shutil.rmtree(TEST_DIR, ignore_errors=True)

if failed > 0:
    sys.exit(1)
