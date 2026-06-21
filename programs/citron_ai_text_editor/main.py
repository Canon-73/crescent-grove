"""
Citron AI Text Editor — セッションベースの行番号固定テキストエディタ

LLMエージェントが長いテキストファイルを正確に編集するためのエディタ。
open→操作→commit の流れでファイルを安全に編集する。

行番号体系:
  - 通常行: "1", "2", "3", ...
  - 挿入行: "25i1", "25i2", ...（再帰ネスト: "25i1i1", "25i1i1i1"）
  - 先頭挿入行: "0i1", "0i2", ...（after="0" でファイル先頭に挿入したとき）
  - 削除行: tombstone化（行番号は保持、内容は非表示）

programs/ 規約準拠: stdin JSON → stdout JSON, デバッグは stderr

============================================================================
このファイルだけを読むレビュアー/AIへ ― Crescent Grove(CG) 側の前提
============================================================================
このサテライトは単体実行されるのではなく、必ずフレームワーク core/tools.py を
通して呼ばれる。main.py だけを見ると過剰防御に見えたり、逆に穴に見えたりする箇所が
あるが、以下の前提を踏まえて評価してほしい。

1. 引数の型はフレームワークが先に検証している。
   manifest.yaml の args 定義（type: string / integer / boolean / enum）に基づき、
   core/tools.py がサブプロセス起動前に全引数を型チェックし、不正なら
   「数値ではなく文字列で指定してください」等の自己回復ヒント付きで弾く。
   そのため main.py に到達する args は基本的に型が正しい（command が list で
   unhashable、content が int で AttributeError…といった事故は通常経路では起きない）。
   main.py 側の型チェックは「直接叩かれた場合」のための二次防御という位置づけ。

2. パストラバーサルも二段構え。
   core/tools.py が "../"・絶対パス始まりの引数を workspace 内に収まるか検証する
   （path_check: false の本文系引数は除外）。その上で resolve_file_path() が
   realpath で実体解決して再チェックする（symlink 経由の脱出対策）。

3. 呼び出しは「単一エージェントからの逐次サブプロセス実行」。
   複数プロセスが同時に open/commit することは想定しない。よってセッションファイルの
   ロックやハッシュ照合後の os.replace までの race は実害がなく、外部変更チェックは
   「ユーザーが別エディタで触ったら気付く」best-effort で十分。

4. 開発・配布とも Windows が主環境。
   POSIX のパーミッション/所有者/os.fchmod は基本的に対象外。commit が
   mkstemp+os.replace でメタデータを引き継がない点は Windows では問題になりにくい。

5. 呼び出し主は LLM。エラーメッセージは必ず「次の一手」（正しい JSON 例）を日本語で
   添える。まっさらな安価モデルでもミスから自己回復できることを最優先する。

----------------------------------------------------------------------------
意図的な仕様（バグではない・指摘不要）― 過去のレビューで繰り返し挙がった点
----------------------------------------------------------------------------
以下は検討済みの設計判断であり、バグではない。再提案する場合は「なぜ現仕様が不可か」
まで踏み込んで書いてほしい（単なる「揃っていない／保存されない」という指摘は不要）。

A. 新規ファイルは末尾改行ありで固定（content末尾の改行有無に依らない）。POSIX流儀。
   test_35/36 が仕様として固定している。既存ファイルは末尾改行の有無を保存するが、
   新規だけ挙動が違うのは承知の上。

B. insert/その他は渡された content を改行で「忠実に」行へ分解する。
   末尾に改行が付いた content（"a\n"）は a + 空行 の2行になる。末尾改行を勝手に
   食う方が魔法的で事故るため、忠実分解を採用。

C. 改行コードは「行ごとの完全保存」はしない。読込時に元の代表スタイルを記録し
   （混在時は CRLF 優先、CR-only は LF 扱い）、commit 時に全体をそれで統一する。
   旧Mac(CR-only)の完全往復は対象外。混在ファイルでも内容が元に戻れば書き込み自体を
   スキップするので（commitの no-op 判定）、実害は最小化済み。

D. undo/redo は差分でなく全文スナップショット（最大5世代）。実装の単純さと確実さを
   メモリ効率より優先した意図的なトレードオフ。巨大ファイルでセッションJSONが膨らむのは
   承知の上（セッションは commit/discard で消える一時ファイル）。

E. 行番号 LINE_RE は "0" を許可するが、これは insert/move/copy の after="0"(ファイル先頭)
   専用。delete/rewrite 等で "0" を渡しても実在行 "0" は無いので自然にエラーになる。

F. セッションファイルには編集中の全文が平文で保存される（program_data/配下）。
   エディタの性質上不可避。stderr ログの伏字（SENSITIVE_LOG_KEYS）とは別レイヤの話。
============================================================================
"""
import copy
import hashlib
import json
import os
import re
import sys
import tempfile
from datetime import datetime

# i18n: 言語別文言の解決。framework (core/tools.py) が PYTHONPATH=programs/ と CG_LANG を
# 注入してくれるので、直叩きでも from _i18n import t で引ける。
from _i18n import t

# tiktokenはCrescent Groveの依存に含まれている
try:
    import tiktoken
    _ENC = tiktoken.get_encoding("cl100k_base")
    def count_tokens(text: str) -> int:
        return len(_ENC.encode(text))
except ImportError:
    _ENC = None  # 切り詰め処理がtiktokenの有無を判定できるよう常に定義しておく
    def count_tokens(text: str) -> int:
        # フォールバック: 概算（文字数÷3）
        return len(text) // 3

# --- 定数 ---
MAX_UNDO = 5
# open/viewが一度に返す本文のトークン上限。これを超えると表示だけ切り詰める（セッションは全文保持）。
# 安価モデルが巨大ファイル（1行超長ファイル含む）をうっかり開いてもコンテキストを食い潰さないための安全装置。
DISPLAY_TOKEN_LIMIT = 3000
# 行番号の形式。先頭ゼロを禁止（"01"が"1"と同一視される事故を防ぐ）。"0"はinsertの特別値として許可
LINE_RE = re.compile(r'^(0|[1-9]\d*)(i[1-9]\d*)*$')
def NO_SESSION() -> str:
    """セッション未存在エラー文。LLM 向けに「先に open してください」と
    JSON 例つきで返す。i18n により言語に追従するため、定数ではなく関数。"""
    return t("citron_no_session", lb="{", rb="}")
# stderrログで伏せる引数。本文(content/replacement)に加え、検索語(query/search)も
# トークン・パスワード・APIキーを探す用途で秘密を含みうるため伏せる。fileはパスなのでデバッグ用に残す。
SENSITIVE_LOG_KEYS = {"content", "replacement", "query", "search"}

def invalid_line_msg(value):
    return t("citron_invalid_line", value=value)

# --- ユーティリティ ---

def parse_line_id(line_id: str) -> list:
    """行番号を解析して比較用のセグメントリストを返す。
    例: "25i1i3" → [25, 1, 3]
    """
    parts = re.split(r'i', line_id)
    return [int(p) for p in parts]


def line_sort_key(line_id: str):
    """line_orderのソートキー。セグメント分割による数値比較。"""
    return parse_line_id(line_id)


def validate_line_id(line_id) -> bool:
    """行番号の形式を検証する。文字列以外（JSONの数値など）はFalseを返す。"""
    return isinstance(line_id, str) and LINE_RE.fullmatch(line_id) is not None


def normalize_newlines(text: str) -> str:
    """入力テキストの改行をLF(\\n)に正規化する（CRLF/CRをLFへ）。
    insertの複数行入力に紛れた\\rが行内容に残るのを防ぐ。"""
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _truncate_notice() -> str:
    """表示を打ち切ったときに本文末尾へ貼る案内。安価モデルでも次の一手が分かるよう、
    省略の事実と、そのままコピペできるsearch/viewの例を添える。"""
    return t("citron_truncate_notice", limit=DISPLAY_TOKEN_LIMIT, lb="{", rb="}")


def truncate_for_display(text: str, limit: int = DISPLAY_TOKEN_LIMIT) -> tuple:
    """表示用テキストをトークン上限で切り詰める。(切り詰め後テキスト, 切り詰めたか) を返す。
    行構造に依存せずトークン量で切るので、1行だけの巨大ファイルでも安全に止まる。
    縮めるのは表示だけで、セッション内部の全文には影響しない。"""
    if _ENC is not None:
        toks = _ENC.encode(text)
        if len(toks) <= limit:
            return text, False
        return _ENC.decode(toks[:limit]), True
    # tiktokenが無い環境: count_tokensの概算（3文字≒1トークン）に合わせて字数で切る
    char_limit = limit * 3
    if len(text) <= char_limit:
        return text, False
    return text[:char_limit], True


def find_line_position(line_order: list, line_id: str) -> int:
    """line_order内での指定行の位置を返す。見つからなければ-1。"""
    try:
        return line_order.index(line_id)
    except ValueError:
        return -1


def get_session_path() -> str:
    """セッションファイルのパスを返す。
    保存先は workspace/program_data/citron_ai_text_editor/。"""
    workspace = os.environ.get("CG_WORKSPACE", ".")
    sessions_dir = os.path.join(workspace, "program_data", "citron_ai_text_editor")
    os.makedirs(sessions_dir, exist_ok=True)
    return os.path.join(sessions_dir, "text_editor.json")


def _old_session_path() -> str:
    """旧バージョンの保存先 workspace/.sessions/text_editor.json。"""
    workspace = os.environ.get("CG_WORKSPACE", ".")
    return os.path.join(workspace, ".sessions", "text_editor.json")


class SessionCorrupted(Exception):
    """セッションファイルが壊れていて読み取れないことを表す。
    main()で握って、discardを促す自己回復用エラーに変換する。"""


# セッションが最低限満たすべき「構造キー」と型。全バージョン共通で、欠けると各コマンドが
# 即KeyError/TypeError（=内部エラー）になるものだけに絞る。file_path/original_file_path/is_new
# 等は .get で防御的に読むうえ、旧バージョンの残留セッションには無いことがあるので含めない
# （含めると正当な旧セッションまで「破損」扱いになる。test_29参照）。
_SESSION_SCHEMA = {
    "lines": dict,
    "line_order": list,
    "undo_stack": list,
    "redo_stack": list,
}


def _is_valid_session(data) -> bool:
    """セッションが最低限のスキーマを満たすか。構文は正しいが中身が壊れた/別形式のJSON（{}等）を弾く。
    旧バージョンの残留セッションは通す（構造キーは歴代共通のため）。"""
    if not isinstance(data, dict):
        return False
    return all(k in data and isinstance(data[k], t) for k, t in _SESSION_SCHEMA.items())


def load_session() -> dict | None:
    """セッションファイルを読み込む。存在しなければNone。破損していればSessionCorruptedを投げる。"""
    # 新パス優先、無ければ旧パスにフォールバック
    path = get_session_path()
    if not os.path.exists(path):
        old = _old_session_path()
        if os.path.exists(old):
            path = old
        else:
            return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        # broad exceptの「内部エラー」ではなく、復旧手順(discard)付きの明示エラーにする。
        # discardはload_sessionを通らないので、この状態からでも破棄して開き直せる。
        raise SessionCorrupted(t("citron_corrupted_json", e=e, lb="{", rb="}"))
    # 構文は正しいがスキーマが壊れている場合（外部書き換え・別バージョン等）も、各コマンドで
    # KeyError/TypeError（＝内部エラー）になる前にSessionCorruptedへ寄せ、同じdiscard導線に乗せる。
    if not _is_valid_session(data):
        raise SessionCorrupted(t("citron_corrupted_schema", lb="{", rb="}"))
    return data


def save_session(session: dict):
    """セッションファイルをアトミックに保存する。"""
    path = get_session_path()
    dir_path = os.path.dirname(path)
    fd, tmp_path = tempfile.mkstemp(dir=dir_path, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(session, f, ensure_ascii=False, indent=2)
        # os.replaceはWindowsでも既存ファイルをアトミックに上書きできる
        os.replace(tmp_path, path)
    except Exception:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        raise


def delete_session():
    """セッションファイルを削除する。
    旧パスも消さないと、load_sessionのフォールバックが旧セッションを拾い続けて
    「セッションが既に存在します」が解消しなくなる。"""
    for path in (get_session_path(), _old_session_path()):
        if os.path.exists(path):
            os.remove(path)


def file_hash(filepath: str) -> str:
    """ファイルのSHA-256ハッシュを返す。"""
    h = hashlib.sha256()
    with open(filepath, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def resolve_file_path(file_arg: str) -> str:
    """ファイルパスをworkspace相対で解決する。
    abspathだけだと、workspace内に置かれた外部を指すsymlink（例: link -> /etc/passwd）が
    文字列上はworkspace内に見えて通ってしまう。realpathで実体まで解決してから判定することで、
    symlink経由のworkspace脱出を防ぐ。新規ファイル（未存在）でも親ディレクトリのsymlinkは
    realpathが追従するため、存在しないパスに対しても安全に効く。"""
    workspace = os.path.realpath(os.environ.get("CG_WORKSPACE", "."))
    abs_path = os.path.realpath(os.path.join(workspace, file_arg))
    # パストラバーサル防止: 前方一致だと "ws" が "ws2" を通すため commonpath で判定
    try:
        inside = os.path.commonpath([workspace, abs_path]) == workspace
    except ValueError:
        # ドライブが異なる場合など
        inside = False
    if not inside:
        raise ValueError(t("citron_workspace_outside", file_arg=file_arg))
    return abs_path


def push_undo(session: dict, operation: str, description: str):
    """現在の状態をundo_stackにpushする。redo_stackはクリア。"""
    snapshot = {
        "operation": operation,
        "description": description,
        "snapshot_lines": copy.deepcopy(session["lines"]),
        "snapshot_line_order": list(session["line_order"]),
    }
    session["undo_stack"].append(snapshot)
    if len(session["undo_stack"]) > MAX_UNDO:
        session["undo_stack"].pop(0)
    session["redo_stack"] = []


def get_lines_in_range(session: dict, start: str | None, end: str | None,
                       include_head: bool = False) -> list[str]:
    """line_order内でstart〜endの範囲に含まれる行番号のリストを返す。

    include_head（表示専用）: 先頭挿入行（0i*: after="0"で挿入した行）はソート上 "1" より前に
    来るため、view start="1" のような指定だと範囲から漏れてAIが見失う。表示系（view）では
    include_head=True を渡し、範囲が先頭（start<="1"）から始まる場合に限り 0i* を含める。
    delete/move/replace など破壊的コマンドでは False のままにし、見えていない先頭挿入行を
    巻き込まないようにする。"""
    order = session["line_order"]
    if start is None and end is None:
        return list(order)

    start_key = line_sort_key(start) if start else None
    end_key = line_sort_key(end) if end else None

    result = []
    for lid in order:
        k = line_sort_key(lid)
        # 先頭挿入行(0i*)は「ファイルの先頭」に属する。範囲が先頭(start<=1)から始まる表示では、
        # start="1" であっても下限チェックをバイパスして含める（end の上限チェックは通常どおり効く）。
        head_line = include_head and k[0] == 0 and (start_key is None or start_key <= [1])
        if not head_line and start_key is not None and k < start_key:
            continue
        if end_key is not None and k > end_key:
            continue
        result.append(lid)
    return result


def count_changes(session: dict) -> dict:
    """変更サマリー(modified/deleted/inserted)を集計する。
    挿入してから削除した行(origin=inserted かつ state=deleted)は実ファイルに最初から現れず
    実質無変更なので、deletedにもinsertedにも数えない。これにより「insert→delete」後の
    集計やoriginal_lines計算が歪まない（stateだけ見ると挿入削除行が元行の削除に化ける）。
    origin未設定の旧セッションはoriginalとみなす（後方互換）。"""
    counts = {"modified": 0, "deleted": 0, "inserted": 0}
    for info in session["lines"].values():
        state = info["state"]
        origin = info.get("origin", "original")
        if state == "deleted":
            # 元から在った行の削除だけをdeletedに数える
            if origin == "original":
                counts["deleted"] += 1
        elif state in counts:
            counts[state] += 1
    return counts


def check_range_order(start, end):
    """範囲が逆(start>end)なら自己回復用エラーを返す。問題なければNone。
    破壊的コマンドで「範囲が逆」を黙って0件空振りさせず、明示してLLMに気づかせる。"""
    if start and end and line_sort_key(start) > line_sort_key(end):
        return error(t("citron_range_reverse", start=start, end=end))
    return None


def insert_lines(session: dict, after: str, new_lines: list) -> list:
    """afterの後（"0"はファイル先頭）にnew_linesを挿入し、[(行ID, 内容), ...] を返す。
    枝番号の採番と挿入位置の決定を担う。afterの検証とpush_undo/save_sessionは呼び出し側の責務。"""
    # 直下の枝番号の最大値を求める（再帰ネストは除外 = suffixに"i"が含まれない）
    prefix = after + "i"
    max_branch = 0
    for lid in session["lines"]:
        if lid.startswith(prefix):
            suffix = lid[len(prefix):]
            if "i" not in suffix:
                try:
                    max_branch = max(max_branch, int(suffix))
                except ValueError:
                    pass

    # 挿入位置: after行の直後（"0"はファイル先頭）、ただしafterの既存子孫の後
    if after == "0":
        insert_pos = 0
    else:
        insert_pos = find_line_position(session["line_order"], after) + 1
    while insert_pos < len(session["line_order"]):
        if session["line_order"][insert_pos].startswith(prefix):
            insert_pos += 1
        else:
            break

    inserted = []
    for i, line_content in enumerate(new_lines, start=max_branch + 1):
        new_id = f"{after}i{i}"
        session["lines"][new_id] = {"content": line_content, "state": "inserted", "origin": "inserted"}
        session["line_order"].insert(insert_pos, new_id)
        inserted.append((new_id, line_content))
        insert_pos += 1
    return inserted


def success(message: str = "", data: dict = None) -> dict:
    """成功レスポンスを構築する。"""
    r = {"status": "success"}
    if message:
        r["message"] = message
    if data:
        r["data"] = data
    return r


def error(message: str) -> dict:
    """エラーレスポンスを構築する。"""
    return {"status": "error", "message": message}


# --- コマンド実装 ---

def cmd_open(args: dict) -> dict:
    """ファイルを開いてセッションを開始する。"""
    # 既存セッションチェック
    existing = load_session()
    if existing is not None:
        ef = existing.get('file_path', '?')
        return error(t("citron_open_session_exists", ef=ef, lb="{", rb="}"))

    file_arg = args.get("file")
    if not file_arg:
        return error(t("citron_open_file_required", lb="{", rb="}"))

    try:
        abs_path = resolve_file_path(file_arg)
    except ValueError as e:
        return error(str(e))

    # AIが "true"（文字列）で渡しても通す
    create = args.get("create") in (True, "true", "True", 1)
    file_exists = os.path.exists(abs_path)

    if not file_exists and not create:
        return error(t("citron_open_not_found", file_arg=file_arg, lb="{", rb="}"))

    # 表示範囲の検証はセッション作成前に行う（エラー時にセッションを残さない）
    start = args.get("start")
    end = args.get("end")
    if start and not validate_line_id(start):
        return error(invalid_line_msg(start))
    if end and not validate_line_id(end):
        return error(invalid_line_msg(end))
    range_err = check_range_order(start, end)
    if range_err:
        return range_err

    content_ignored = False
    if file_exists:
        # 既存ファイルにcontentを渡しても上書きしない（誤爆防止）。無視したことは明示する。
        # create の有無に関わらず、contentが来ていたら必ず「無視した」と返す（黙殺しない）。
        content_ignored = args.get("content") is not None
        # ファイル読み込み（universal newlinesでCRLF/LFとも\nに正規化される）
        try:
            with open(abs_path, "r", encoding="utf-8") as f:
                raw_content = f.read()
                # 元の改行コードを記録（commit時に復元するため）。混在時はCRLF優先
                seen = f.newlines
                if isinstance(seen, tuple):
                    newline_style = "\r\n" if "\r\n" in seen else "\n"
                else:
                    newline_style = seen if seen in ("\r\n", "\n") else "\n"
        except UnicodeDecodeError:
            return error(t("citron_open_not_utf8", file_arg=file_arg))
        except OSError as e:
            return error(t("citron_open_read_error", file_arg=file_arg, e=e))
        trailing_newline = raw_content.endswith("\n")
    else:
        # 新規作成: 実ファイルはcommitまで作らない（discardで痕跡を残さないため）。
        # 初期contentも既存読込・insertと同じく改行をLFへ正規化し、行内容に\rが残らないようにする。
        raw_content = normalize_newlines(args.get("content") or "")
        newline_style = "\n"
        # 新規ファイルは末尾改行ありで固定（POSIX流儀。test_35/36 で仕様として固定）。
        # 空ファイル（論理行0）のときはcommit側で改行を付けないので実体は空のまま。
        trailing_newline = True

    raw_lines = raw_content.split("\n")
    # 末尾の改行による空行を除去（ファイルが改行で終わる場合）
    if raw_lines and raw_lines[-1] == "":
        raw_lines.pop()

    # セッション構築
    lines = {}
    line_order = []
    for i, content in enumerate(raw_lines, start=1):
        lid = str(i)
        lines[lid] = {"content": content, "state": "normal", "origin": "original"}
        line_order.append(lid)

    fhash = file_hash(abs_path) if file_exists else None
    total_tokens = count_tokens(raw_content)

    session = {
        "file_path": file_arg,
        "original_file_path": abs_path,
        "file_hash": fhash,
        "is_new": not file_exists,
        "opened_at": datetime.now().isoformat(),
        "newline_style": newline_style,
        "trailing_newline": trailing_newline,
        # open時の正規化済み(LF)本文。commitで「最終内容が元と同じか」を改行コードに依存せず
        # 論理比較するために保持する（混在改行ファイルでも無駄な書き込みを避けるため）。新規はNone。
        "original_content": raw_content if file_exists else None,
        "lines": lines,
        "line_order": line_order,
        "undo_stack": [],
        "redo_stack": [],
    }
    save_session(session)

    # 表示範囲
    display_lines = get_lines_in_range(session, start, end)

    shown = []
    for lid in display_lines:
        info = lines[lid]
        if info["state"] != "deleted":
            shown.append(f"{lid}: {info['content']}")

    total = len(line_order)
    showing_start = display_lines[0] if display_lines else "1"
    showing_end = display_lines[-1] if display_lines else str(total)

    if not file_exists:
        msg = t("citron_open_msg_new")
        if total == 0:
            msg += t("citron_open_msg_new_empty_hint", lb="{", rb="}")
    elif content_ignored:
        msg = t("citron_open_msg_content_ignored")
    else:
        msg = t("citron_open_msg_done")

    content_str, truncated = truncate_for_display("\n".join(shown))
    if truncated:
        content_str += _truncate_notice()
        msg += t("citron_open_msg_truncated_hint")

    return success(msg, {
        "file": file_arg,
        "total_lines": total,
        "total_tokens": total_tokens,
        "showing": f"{showing_start}-{showing_end}",
        "truncated": truncated,
        "content": content_str,
    })

def cmd_view(args: dict) -> dict:
    """セッション中のファイル内容を表示する。"""
    session = load_session()
    if session is None:
        return error(NO_SESSION())

    start = args.get("start")
    end = args.get("end")
    if start and not validate_line_id(start):
        return error(invalid_line_msg(start))
    if end and not validate_line_id(end):
        return error(invalid_line_msg(end))
    range_err = check_range_order(start, end)
    if range_err:
        return range_err
    # 先頭挿入行(0i*)を view start="1" で取りこぼさないよう表示時は include_head=True
    display_lines = get_lines_in_range(session, start, end, include_head=True)

    shown = []
    for lid in display_lines:
        info = session["lines"][lid]
        if info["state"] != "deleted":
            shown.append(f"{lid}: {info['content']}")

    showing_start = display_lines[0] if display_lines else ""
    showing_end = display_lines[-1] if display_lines else ""

    content_str, truncated = truncate_for_display("\n".join(shown))
    msg = ""
    if truncated:
        content_str += _truncate_notice()
        msg = t("citron_view_msg_truncated")

    return success(msg, {
        "showing": f"{showing_start}-{showing_end}",
        "truncated": truncated,
        "content": content_str,
    })

MAX_SEARCH_RESULTS = 50
# searchのcontext上限。巨大値で表示トークン制限を迂回し、ファイル全体をstdoutへ吐かせないため。
MAX_SEARCH_CONTEXT = 20

def cmd_search(args: dict) -> dict:
    """ファイル内を部分一致で検索し、マッチした行番号と内容を返す。"""
    session = load_session()
    if session is None:
        return error(NO_SESSION())

    query = args.get("query")
    if not query:
        return error(t("citron_search_query_required", lb="{", rb="}"))

    start = args.get("start")
    end = args.get("end")
    if start and not validate_line_id(start):
        return error(invalid_line_msg(start))
    if end and not validate_line_id(end):
        return error(invalid_line_msg(end))
    range_err = check_range_order(start, end)
    if range_err:
        return range_err

    # context: マッチ行の前後に表示する行数。AIが文字列で渡しても許容する。
    # 巨大値での表示トークン制限の迂回を防ぐため上限でクランプする。
    try:
        context = max(0, int(args.get("context") or 0))
    except (TypeError, ValueError):
        return error(t("citron_search_context_not_number", lb="{", rb="}"))
    context = min(context, MAX_SEARCH_CONTEXT)

    # searchは読み取り専用なのでviewと同じく include_head=True。これがないと start="1" 指定時に
    # 先頭挿入行(0i*)を取りこぼし、「viewで見えている行がsearchで見つからない」不整合が起きる。
    # （delete/move/replace は破壊的なので見えない先頭挿入行を巻き込まないよう False のままにする）
    target_lines = get_lines_in_range(session, start, end, include_head=True)
    # 削除行を除いた表示順リストの中で検索する
    visible = [lid for lid in target_lines if session["lines"][lid]["state"] != "deleted"]
    match_idx = [i for i, lid in enumerate(visible) if query in session["lines"][lid]["content"]]
    matched_lines = [visible[i] for i in match_idx]

    if not matched_lines:
        return success(t("citron_search_no_match"), {
            "query": query,
            "match_count": 0,
            "matched_lines": [],
            "content": "",
        })

    # 表示する行位置を収集（マッチ行 + 前後context行）。非連続箇所は "---" で区切る
    shown_idx = set()
    for i in match_idx[:MAX_SEARCH_RESULTS]:
        for j in range(max(0, i - context), min(len(visible), i + context + 1)):
            shown_idx.add(j)

    parts = []
    prev = None
    for j in sorted(shown_idx):
        if prev is not None and j != prev + 1:
            parts.append("---")
        lid = visible[j]
        parts.append(f"{lid}: {session['lines'][lid]['content']}")
        prev = j

    msg = t("citron_search_matched_count", n=len(matched_lines))
    if len(matched_lines) > MAX_SEARCH_RESULTS:
        msg += t("citron_search_results_limited", max=MAX_SEARCH_RESULTS)

    # search結果もopen/viewと同じ表示トークン上限で切り詰める（contextを絞っても巨大行が並ぶ場合の保険）
    content_str, truncated = truncate_for_display("\n".join(parts))
    if truncated:
        content_str += _truncate_notice()
        msg += t("citron_search_results_truncated")

    return success(msg, {
        "query": query,
        "match_count": len(matched_lines),
        "matched_lines": matched_lines[:MAX_SEARCH_RESULTS],
        "content": content_str,
    })

def cmd_rewrite(args: dict) -> dict:
    """指定行の内容を丸ごと書き換える。"""
    session = load_session()
    if session is None:
        return error(NO_SESSION())

    line = args.get("line")
    content = args.get("content")
    if not line:
        return error(t("citron_rewrite_line_required", lb="{", rb="}"))
    if content is None:
        return error(t("citron_rewrite_content_required", lb="{", rb="}"))
    if "\n" in content or "\r" in content:
        return error(t("citron_rewrite_no_newline", lb="{", rb="}"))
    if not validate_line_id(line):
        return error(invalid_line_msg(line))
    if line not in session["lines"]:
        return error(t("citron_line_missing", line=line, lb="{", rb="}"))
    if session["lines"][line]["state"] == "deleted":
        return error(t("citron_rewrite_line_deleted", line=line))

    push_undo(session, "rewrite", t("citron_rewrite_undo_desc", line=line))
    session["lines"][line]["content"] = content
    # 挿入行はinsertedのまま維持し、それ以外をmodifiedにする
    if session["lines"][line]["state"] != "inserted":
        session["lines"][line]["state"] = "modified"
    save_session(session)

    # 巨大な1行を書き換えた場合に戻り値で表示トークン制限を迂回しないよう切り詰める
    result_text, truncated = truncate_for_display(f"{line}: {content}")
    if truncated:
        result_text += _truncate_notice()
    return success(t("citron_rewrite_done", line=line), {
        "result": result_text,
        "truncated": truncated,
    })

def cmd_replace(args: dict) -> dict:
    """指定範囲内で文字列を検索し置換する。"""
    session = load_session()
    if session is None:
        return error(NO_SESSION())

    search = args.get("search")
    replacement = args.get("replacement")
    if not search:
        return error(t("citron_replace_search_required", lb="{", rb="}"))
    if replacement is None:
        return error(t("citron_replace_replacement_required", lb="{", rb="}"))
    if "\n" in replacement or "\r" in replacement:
        return error(t("citron_replace_no_newline"))

    start = args.get("start")
    end = args.get("end")
    if start and not validate_line_id(start):
        return error(invalid_line_msg(start))
    if end and not validate_line_id(end):
        return error(invalid_line_msg(end))
    range_err = check_range_order(start, end)
    if range_err:
        return range_err
    target_lines = get_lines_in_range(session, start, end)

    # 先にマッチ行を確認し、0件ならundoスロットを消費しない
    hit_lines = [
        lid for lid in target_lines
        if session["lines"][lid]["state"] != "deleted" and search in session["lines"][lid]["content"]
    ]
    if not hit_lines:
        return success(t("citron_replace_no_match"), {
            "search": search,
            "replacement": replacement,
            "matches": 0,
            "affected_lines": [],
        })

    push_undo(session, "replace", t("citron_replace_undo_desc", search=search, replacement=replacement))

    matches = 0
    affected_lines = []
    for lid in hit_lines:
        info = session["lines"][lid]
        matches += info["content"].count(search)
        info["content"] = info["content"].replace(search, replacement)
        if info["state"] != "inserted":
            info["state"] = "modified"
        affected_lines.append(lid)

    save_session(session)

    return success(t("citron_replace_done", matches=matches), {
        "search": search,
        "replacement": replacement,
        "matches": matches,
        "affected_lines": affected_lines,
    })


def cmd_delete(args: dict) -> dict:
    """指定行または範囲をtombstone化する。"""
    session = load_session()
    if session is None:
        return error(NO_SESSION())

    start = args.get("start")
    if not start:
        return error(t("citron_delete_start_required", lb="{", rb="}"))
    if not validate_line_id(start):
        return error(invalid_line_msg(start))

    end = args.get("end", start)
    if not validate_line_id(end):
        return error(invalid_line_msg(end))
    range_err = check_range_order(start, end)
    if range_err:
        return range_err

    target_lines = get_lines_in_range(session, start, end)
    if not target_lines:
        return error(t("citron_range_not_found", start=start, end=end, lb="{", rb="}"))

    # 既にtombstoneの行を除いた実削除対象。0件ならundoスロットを消費しない
    targets = [lid for lid in target_lines if session["lines"][lid]["state"] != "deleted"]
    if not targets:
        return success(t("citron_delete_all_already_deleted"), {
            "deleted_lines": [],
            "deleted_count": 0,
        })

    push_undo(session, "delete", t("citron_delete_undo_desc", start=start, end=end))

    deleted = []
    for lid in targets:
        session["lines"][lid]["state"] = "deleted"
        deleted.append(lid)

    save_session(session)

    if len(deleted) == 1:
        msg = t("citron_delete_done_single", deleted_line=deleted[0])
    else:
        msg = t("citron_delete_done_range", start=start, end=end, count=len(deleted))

    return success(msg, {
        "deleted_lines": deleted,
        "deleted_count": len(deleted),
    })


def cmd_insert(args: dict) -> dict:
    """指定行の後に行を挿入する。枝番号を自動採番。after="0"はファイル先頭への挿入。"""
    session = load_session()
    if session is None:
        return error(NO_SESSION())

    after = args.get("after")
    content = args.get("content")
    if after is None or after == "":
        return error(t("citron_insert_after_required", lb="{", rb="}"))
    if content is None:
        return error(t("citron_insert_content_required", lb="{", rb="}"))
    if not validate_line_id(after):
        return error(invalid_line_msg(after))
    # "0"はファイル先頭への挿入を表す特別な値（空ファイルへの書き込みもこれで行う）
    if after != "0" and after not in session["lines"]:
        return error(t("citron_insert_after_missing", after=after, lb="{", rb="}"))
    if after != "0" and session["lines"][after]["state"] == "deleted":
        return error(t("citron_insert_after_deleted", after=after, lb="{", rb="}"))

    # 複数行の場合は改行で分割。入力のCRLF/CRを先にLFへ正規化し、行内容に\rが残らないようにする
    new_lines = normalize_newlines(content).split("\n")

    push_undo(session, "insert", t("citron_insert_undo_desc", count=len(new_lines), after=after))
    inserted = insert_lines(session, after, new_lines)
    save_session(session)

    place = t("citron_place_top") if after == "0" else t("citron_place_after_line", after=after)
    msg = t("citron_insert_done", place=place, count=len(inserted))

    # 巨大な複数行を挿入した場合に戻り値で表示トークン制限を迂回しないよう切り詰める
    inserted_text, truncated = truncate_for_display("\n".join(f"{lid}: {c}" for lid, c in inserted))
    if truncated:
        inserted_text += _truncate_notice()
    return success(msg, {
        "inserted": inserted_text,
        "truncated": truncated,
    })

def _move_or_copy(args: dict, move: bool) -> dict:
    """move/copyの共通実装。内容のコピーを挿入し、moveの場合は元の行をtombstone化する。
    行番号固定モデルを保つため、移動でも行IDの付け替えはしない。"""
    op = "move" if move else "copy"
    session = load_session()
    if session is None:
        return error(NO_SESSION())

    start = args.get("start")
    after = args.get("after")
    if not start:
        return error(t("citron_move_start_required" if move else "citron_copy_start_required", lb="{", rb="}"))
    if after is None or after == "":
        return error(t("citron_move_after_required" if move else "citron_copy_after_required", lb="{", rb="}"))
    if not validate_line_id(start):
        return error(invalid_line_msg(start))
    end = args.get("end", start)
    if not validate_line_id(end):
        return error(invalid_line_msg(end))
    if not validate_line_id(after):
        return error(invalid_line_msg(after))
    if after != "0" and after not in session["lines"]:
        return error(t("citron_move_after_missing" if move else "citron_copy_after_missing", after=after))
    if after != "0" and session["lines"][after]["state"] == "deleted":
        return error(t("citron_move_after_deleted" if move else "citron_copy_after_deleted", after=after))
    range_err = check_range_order(start, end)
    if range_err:
        return range_err

    source = [
        lid for lid in get_lines_in_range(session, start, end)
        if session["lines"][lid]["state"] != "deleted"
    ]
    if not source:
        return error(t("citron_mc_range_not_found", start=start, end=end))
    if move and after in source:
        return error(t("citron_move_dest_in_source", after=after))

    contents = [session["lines"][lid]["content"] for lid in source]

    push_undo(session, op, t("citron_mc_undo_desc", op=op, start=start, end=end, after=after))
    inserted = insert_lines(session, after, contents)
    if move:
        for lid in source:
            session["lines"][lid]["state"] = "deleted"
    save_session(session)

    place = t("citron_place_top") if after == "0" else t("citron_place_after_line", after=after)
    # 巨大な範囲をmove/copyした場合に戻り値で表示トークン制限を迂回しないよう切り詰める
    inserted_text, truncated = truncate_for_display("\n".join(f"{lid}: {c}" for lid, c in inserted))
    if truncated:
        inserted_text += _truncate_notice()
    done_key = "citron_move_done" if move else "citron_copy_done"
    return success(t(done_key, start=start, end=end, count=len(source), place=place), {
        "source_lines": source,
        "inserted": inserted_text,
        "truncated": truncated,
    })


def cmd_move(args: dict) -> dict:
    """指定範囲の行を移動する（内容の打ち直し不要）。"""
    return _move_or_copy(args, move=True)


def cmd_copy(args: dict) -> dict:
    """指定範囲の行を複製する（内容の打ち直し不要）。"""
    return _move_or_copy(args, move=False)


def cmd_undo(args: dict) -> dict:
    """直前の操作を取り消す。"""
    session = load_session()
    if session is None:
        return error(NO_SESSION())

    if not session["undo_stack"]:
        return error(t("citron_undo_empty"))

    snapshot = session["undo_stack"].pop()

    # 現在の状態をredo_stackにpush
    redo_snapshot = {
        "operation": snapshot["operation"],
        "description": snapshot["description"],
        "snapshot_lines": copy.deepcopy(session["lines"]),
        "snapshot_line_order": list(session["line_order"]),
    }
    session["redo_stack"].append(redo_snapshot)
    if len(session["redo_stack"]) > MAX_UNDO:
        session["redo_stack"].pop(0)

    # 復元
    session["lines"] = snapshot["snapshot_lines"]
    session["line_order"] = snapshot["snapshot_line_order"]
    save_session(session)

    return success(t("citron_undo_done", description=snapshot["description"]), {
        "undone_operation": snapshot["operation"],
        "remaining_undos": len(session["undo_stack"]),
    })


def cmd_redo(args: dict) -> dict:
    """取り消した操作をやり直す。"""
    session = load_session()
    if session is None:
        return error(NO_SESSION())

    if not session["redo_stack"]:
        return error(t("citron_redo_empty"))

    snapshot = session["redo_stack"].pop()

    # 現在の状態をundo_stackにpush
    undo_snapshot = {
        "operation": snapshot["operation"],
        "description": snapshot["description"],
        "snapshot_lines": copy.deepcopy(session["lines"]),
        "snapshot_line_order": list(session["line_order"]),
    }
    session["undo_stack"].append(undo_snapshot)
    if len(session["undo_stack"]) > MAX_UNDO:
        session["undo_stack"].pop(0)

    # 復元
    session["lines"] = snapshot["snapshot_lines"]
    session["line_order"] = snapshot["snapshot_line_order"]
    save_session(session)

    return success(t("citron_redo_done", description=snapshot["description"]), {
        "redone_operation": snapshot["operation"],
        "remaining_redos": len(session["redo_stack"]),
    })


def cmd_status(args: dict) -> dict:
    """セッションの変更サマリーを返す。"""
    session = load_session()
    if session is None:
        return error(NO_SESSION())

    counts = count_changes(session)

    return success(data={
        "file": session["file_path"],
        "modified": counts["modified"],
        "deleted": counts["deleted"],
        "inserted": counts["inserted"],
        "remaining_undos": len(session["undo_stack"]),
        "remaining_redos": len(session["redo_stack"]),
    })


def cmd_commit(args: dict) -> dict:
    """編集結果を実ファイルに書き出す。"""
    session = load_session()
    if session is None:
        return error(NO_SESSION())

    abs_path = session["original_file_path"]
    is_new = session.get("is_new", False)

    # 1. 外部変更チェック（新規はopen後に同名ファイルが現れていないか、既存はハッシュ照合）
    if is_new:
        if os.path.exists(abs_path):
            return error(t("citron_commit_external_create_conflict", lb="{", rb="}"))
    else:
        if not os.path.exists(abs_path):
            return error(t("citron_commit_original_missing", file_path=session['file_path']))
        current_hash = file_hash(abs_path)
        if current_hash != session["file_hash"]:
            return error(t("citron_commit_file_modified_externally", lb="{", rb="}"))

    # 2. 最終的な行のリストを構築（tombstone除外、枝番号統合）
    final_lines = []
    for lid in session["line_order"]:
        info = session["lines"][lid]
        if info["state"] != "deleted":
            final_lines.append(info["content"])

    # 3. 変更サマリー集計
    counts = count_changes(session)

    original_lines = sum(1 for info in session["lines"].values() if info.get("origin", "original") == "original")

    # 3.5 既存ファイルで変更が一切ない場合は書き出さない。
    # 無駄な再書き込みによるmtime変更・改行正規化・空行のみファイル破壊・symlink挙動変化を避ける。
    # （新規作成はファイル自体を作る必要があるので除外）
    if not is_new and counts["modified"] == counts["deleted"] == counts["inserted"] == 0:
        delete_session()
        return success(t("citron_commit_no_changes"), {
            "file": session["file_path"],
            "changes": counts,
        })

    # 4. アトミック書き出し（元ファイルの改行コード・末尾改行有無を復元）
    final_content = "\n".join(final_lines)
    # 末尾改行の判定は内容の真偽ではなく論理行の有無で行う。
    # （空行1行だけのファイル= final_lines=[""] のとき final_content="" でも改行を復元する）
    if session.get("trailing_newline", True) and final_lines:
        final_content += "\n"
    newline_style = session.get("newline_style", "\n")

    # 3.6 既存ファイルで、最終的な論理内容(LF基準)が元と一致するなら書き込まない。
    # open時に保存した正規化済み本文(original_content)と論理比較するので、CRLF/LF混在ファイルでも
    # 「insert→delete」「rewrite→元に戻す」等で内容が元に戻ったケースを正しく拾える。
    # 直前のハッシュ照合で元ファイルはopen時から不変が保証されているため、論理比較で十分かつ
    # バイト比較より安全（混在改行を保ったままスキップでき、無駄なmtime変更・改行正規化を避けられる）。
    if not is_new and final_content == session.get("original_content"):
        delete_session()
        return success(t("citron_commit_content_unchanged"), {
            "file": session["file_path"],
            "changes": counts,
        })

    dir_path = os.path.dirname(abs_path)
    try:
        if is_new:
            # 新規作成: 親ディレクトリごと作る（resolve_file_path検証済みなのでworkspace内）
            os.makedirs(dir_path, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(dir=dir_path, suffix=".tmp")
    except OSError as e:
        # I/O失敗を握って自己回復しやすいエラーにする（broad exceptの「内部エラー」を避ける）
        return error(t("citron_commit_tmp_prep_failed", file_path=session["file_path"], e=e))
    try:
        # newline指定により"\n"がnewline_styleへ変換される（無指定だとWindowsで強制CRLF化）
        with os.fdopen(fd, "w", encoding="utf-8", newline=newline_style) as f:
            f.write(final_content)
        # os.replaceはWindowsでも既存ファイルをアトミックに上書きできる
        os.replace(tmp_path, abs_path)
    except OSError as e:
        # 書き込み/置換のI/O失敗（権限・空き容量・他アプリのロック等）も内部エラーにせず
        # 自己回復用のエラーで返す。Windowsではos.replaceがアンチウイルスや別アプリの
        # ファイルロックでPermissionErrorになりやすいので、ここを握るのは実用上重要。
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        return error(t("citron_commit_write_failed", file_path=session["file_path"], e=e))
    except Exception:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        raise

    # 5. トークン数計測
    final_tokens = count_tokens(final_content)

    # 6. セッション削除
    delete_session()

    msg = t("citron_commit_done_new") if is_new else t("citron_commit_done_update")
    return success(msg, {
        "file": session["file_path"],
        "original_lines": original_lines,
        "final_lines": len(final_lines),
        "final_tokens": final_tokens,
        "changes": counts,
    })


def cmd_discard(args: dict) -> dict:
    """セッションを破棄する（冪等）。"""
    delete_session()
    return success(t("citron_discard_done"))


# --- メインディスパッチャ ---

def cmd_help(args: dict) -> dict:
    """コマンド一覧と使い方を返す。i18n: 各 description / args ラベル / example の
    自然言語部分は t() 経由で言語に追従させる。コマンド名・引数名・行番号のような
    機械が読む値は両言語共通でリテラルのまま。"""
    return {
        "status": "success",
        "message": t("citron_help_msg_title"),
        "data": {
            "commands": {
                "open": {
                    "description": t("citron_help_open_desc"),
                    "args": {
                        "file": t("citron_help_open_arg_file"),
                        "create": t("citron_help_open_arg_create"),
                        "content": t("citron_help_open_arg_content"),
                        "start": t("citron_help_open_arg_start"),
                        "end": t("citron_help_open_arg_end"),
                    },
                    "example": {"command": "open", "file": "SOUL.md"},
                    "example_create": {"command": "open", "file": "new_note.md", "create": True, "content": t("citron_help_open_example_content")}
                },
                "view": {
                    "description": t("citron_help_view_desc"),
                    "args": {"start": t("citron_help_view_arg_start"), "end": t("citron_help_view_arg_end")},
                    "example": {"command": "view", "start": "50", "end": "80"}
                },
                "search": {
                    "description": t("citron_help_search_desc"),
                    "args": {
                        "query": t("citron_help_search_arg_query"),
                        "context": t("citron_help_search_arg_context"),
                        "start": t("citron_help_search_arg_start"),
                        "end": t("citron_help_search_arg_end"),
                    },
                    "example": {"command": "search", "query": t("citron_help_search_example_query"), "context": 2}
                },
                "rewrite": {
                    "description": t("citron_help_rewrite_desc"),
                    "args": {"line": t("citron_help_rewrite_arg_line"), "content": t("citron_help_rewrite_arg_content")},
                    "example": {"command": "rewrite", "line": "12", "content": t("citron_help_rewrite_example_content")}
                },
                "replace": {
                    "description": t("citron_help_replace_desc"),
                    "args": {
                        "search": t("citron_help_replace_arg_search"),
                        "replacement": t("citron_help_replace_arg_replacement"),
                        "start": t("citron_help_replace_arg_start"),
                        "end": t("citron_help_replace_arg_end"),
                    },
                    "example": {"command": "replace", "search": t("citron_help_replace_example_search"), "replacement": t("citron_help_replace_example_replacement")}
                },
                "delete": {
                    "description": t("citron_help_delete_desc"),
                    "args": {"start": t("citron_help_delete_arg_start"), "end": t("citron_help_delete_arg_end")},
                    "example": {"command": "delete", "start": "10", "end": "15"}
                },
                "insert": {
                    "description": t("citron_help_insert_desc"),
                    "args": {"after": t("citron_help_insert_arg_after"), "content": t("citron_help_insert_arg_content")},
                    "example": {"command": "insert", "after": "25", "content": t("citron_help_insert_example_content")},
                    "example_top": {"command": "insert", "after": "0", "content": t("citron_help_insert_example_top_content")}
                },
                "move": {
                    "description": t("citron_help_move_desc"),
                    "args": {"start": t("citron_help_move_arg_start"), "end": t("citron_help_move_arg_end"), "after": t("citron_help_move_arg_after")},
                    "example": {"command": "move", "start": "10", "end": "15", "after": "30"}
                },
                "copy": {
                    "description": t("citron_help_copy_desc"),
                    "args": {"start": t("citron_help_copy_arg_start"), "end": t("citron_help_copy_arg_end"), "after": t("citron_help_copy_arg_after")},
                    "example": {"command": "copy", "start": "10", "end": "15", "after": "30"}
                },
                "undo": {
                    "description": t("citron_help_undo_desc"),
                    "args": {},
                    "example": {"command": "undo"}
                },
                "redo": {
                    "description": t("citron_help_redo_desc"),
                    "args": {},
                    "example": {"command": "redo"}
                },
                "status": {
                    "description": t("citron_help_status_desc"),
                    "args": {},
                    "example": {"command": "status"}
                },
                "commit": {
                    "description": t("citron_help_commit_desc"),
                    "args": {},
                    "example": {"command": "commit"}
                },
                "discard": {
                    "description": t("citron_help_discard_desc"),
                    "args": {},
                    "example": {"command": "discard"}
                },
                "help": {
                    "description": t("citron_help_help_desc"),
                    "args": {},
                    "example": {"command": "help"}
                }
            },
            "line_format": t("citron_help_line_format")
        }
    }


COMMANDS = {
    "open": cmd_open,
    "view": cmd_view,
    "search": cmd_search,
    "rewrite": cmd_rewrite,
    "replace": cmd_replace,
    "delete": cmd_delete,
    "insert": cmd_insert,
    "move": cmd_move,
    "copy": cmd_copy,
    "undo": cmd_undo,
    "redo": cmd_redo,
    "status": cmd_status,
    "commit": cmd_commit,
    "discard": cmd_discard,
    "help": cmd_help,
}


def main():
    try:
        args = json.loads(sys.stdin.read())
    except json.JSONDecodeError as e:
        print(json.dumps(error(t("citron_json_parse_err", e=e)), ensure_ascii=False))
        return

    # "abc" や 123 など、dict以外のJSONも契約どおりJSONエラーで返す
    if not isinstance(args, dict):
        print(json.dumps(error(
            t("citron_not_dict_json", typename=type(args).__name__, lb="{", rb="}")
        ), ensure_ascii=False))
        return

    command = args.get("command", "")
    # command非文字列(list/dict等)はこの後の "command not in COMMANDS" がtry外でTypeError
    # (unhashable)を起こしプロセスごと落ちる。frameworkが型検証する前提だが、main.py単体で
    # 直接叩かれた場合の二次防御として先に弾く。
    if not isinstance(command, str):
        print(json.dumps(error(
            t("citron_command_not_string", typename=type(command).__name__, lb="{", rb="}")
        ), ensure_ascii=False))
        return
    # 本文や検索語はファイル中身・秘密情報を含みうるのでログに出さない（SENSITIVE_LOG_KEYS参照）
    safe_args = {k: ("<省略>" if k in SENSITIVE_LOG_KEYS else v) for k, v in args.items()}
    print(f"[citron] command={command} args={safe_args}", file=sys.stderr)

    if command not in COMMANDS:
        valid = ", ".join(COMMANDS.keys())
        print(json.dumps(error(
            t("citron_unknown_command", command=command, valid=valid, lb="{", rb="}")
        ), ensure_ascii=False))
        return

    try:
        result = COMMANDS[command](args)
    except SessionCorrupted as e:
        # 破損セッションは復旧手順付きの明示エラーで返す（discardで脱出可能）
        result = error(str(e))
    except Exception as e:
        print(f"[citron] 内部エラー: {e}", file=sys.stderr)
        import traceback
        traceback.print_exc(file=sys.stderr)
        result = error(t("citron_internal_error", e=e))

    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
