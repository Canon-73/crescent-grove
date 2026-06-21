"""
Orange MD Reader - マークダウンファイル構造確認・範囲読み込みツール

コマンド:
  outline - ファイルの見出し構造と強調箇所を表示
  read    - 指定範囲を行番号付きでそのまま表示
  scan    - 指定範囲をキーワード抽出して圧縮表示
  help    - 使い方を表示
"""

import json
import os
import re
import sys

from _i18n import t

# --- tiktoken ---
try:
    import tiktoken
    _enc = tiktoken.get_encoding("cl100k_base")
    def count_tokens(text: str) -> int:
        return len(_enc.encode(text))
except ImportError:
    def count_tokens(text: str) -> int:
        return len(text) // 3

# --- 定数 ---
WORKSPACE = os.environ.get("CG_WORKSPACE", ".")

def build_help() -> dict:
    """HELP テキストを現在の言語で構築する。"""
    return {
        "status": "success",
        "message": t("orange_md_help_title"),
        "data": {
            "commands": {
                "outline": {
                    "description": t("orange_md_help_outline_desc"),
                    "args": {"file": t("orange_md_help_file_arg")},
                    "example": {"command": "outline", "file": "SOUL.md"}
                },
                "read": {
                    "description": t("orange_md_help_read_desc"),
                    "args": {
                        "file": t("orange_md_help_file_arg"),
                        "start": t("orange_md_help_start_arg"),
                        "end": t("orange_md_help_end_arg")
                    },
                    "examples": [
                        {"command": "read", "file": "SOUL.md", "start": "1", "end": "10"},
                        {"command": "read", "file": "SOUL.md", "start": "50", "end": "*"},
                        {"command": "read", "file": "SOUL.md", "start": "25"}
                    ]
                },
                "scan": {
                    "description": t("orange_md_help_scan_desc"),
                    "args": {
                        "file": t("orange_md_help_file_arg"),
                        "start": t("orange_md_help_start_arg"),
                        "end": t("orange_md_help_end_arg")
                    },
                    "examples": [
                        {"command": "scan", "file": "SOUL.md", "start": "40", "end": "71"},
                        {"command": "scan", "file": "SOUL.md", "start": "1", "end": "*"}
                    ]
                },
                "help": {
                    "description": t("orange_md_help_help_desc"),
                    "example": {"command": "help"}
                }
            },
            "usage_flow": t("orange_md_help_usage_flow"),
            "tips": [
                t("orange_md_help_tip_end_star"),
                t("orange_md_help_tip_start_only"),
                t("orange_md_help_tip_outline_full")
            ]
        }
    }

# --- キーワード抽出 (scan用) ---
KEYWORD_RE = re.compile(
    r"\*\*.+?\*\*"
    r"|[一-龥㐀-䶵]{2,}"
    r"|[ァ-ヶー]{2,}"
    r"|[A-Za-z]{4,}"
    r"|\d[\d,.]*[%日目年月zZ円個件回位名枚本棟台杯着勝敗]"
)

def unique_ordered(seq):
    """重複排除しつつ順序を保持"""
    seen = set()
    result = []
    for item in seq:
        if item not in seen:
            seen.add(item)
            result.append(item)
    return result

def extract_keywords(text: str, max_kw: int = 5) -> list:
    """テキストからキーワードを抽出"""
    matches = KEYWORD_RE.findall(text)
    return unique_ordered(matches)[:max_kw]

# --- ユーティリティ ---
def success(message: str = "", data: dict = None) -> dict:
    r = {"status": "success"}
    if message:
        r["message"] = message
    if data is not None:
        r["data"] = data
    return r

def error(message: str) -> dict:
    return {"status": "error", "message": message}

def resolve_file(file_arg: str):
    """ファイルパスを解決し、(絶対パス, エラーdict or None) を返す"""
    if not file_arg:
        return None, error(t("orange_md_err_no_file", lb="{", rb="}"))
    abs_path = os.path.normpath(os.path.join(WORKSPACE, file_arg))
    ws = os.path.normpath(WORKSPACE)
    if not abs_path.startswith(ws):
        return None, error(t("orange_md_err_bad_path", file=file_arg, lb="{", rb="}"))
    if not os.path.isfile(abs_path):
        return None, error(t("orange_md_err_not_found", file=file_arg, lb="{", rb="}"))
    return abs_path, None

def load_lines(abs_path: str) -> list:
    """ファイルを行リストとして読み込む（改行除去済み）"""
    with open(abs_path, "r", encoding="utf-8") as f:
        return [line.rstrip("\n").rstrip("\r") for line in f.readlines()]

def parse_range(lines: list, start_str: str, end_str: str, command_name: str):
    """
    行範囲をパースする。
    戻り値: (start_idx, end_idx, エラーdict or None)
    start_idx, end_idx は 0始まりインデックス（両端含む）
    """
    total = len(lines)

    if not start_str:
        return None, None, error(t("orange_md_err_no_start", command=command_name, lb="{", rb="}"))

    # start のパース
    try:
        start = int(start_str)
    except ValueError:
        return None, None, error(t("orange_md_err_bad_start", start=start_str))

    if start < 1:
        return None, None, error(t("orange_md_err_start_too_small", start=start, command=command_name, lb="{", rb="}"))

    if start > total:
        return None, None, error(t("orange_md_err_start_too_big", start=start, total=total, command=command_name, lb="{", rb="}"))

    # end のパース
    if not end_str:
        # end 省略時は start と同じ（1行だけ）
        end = start
    elif end_str == "*":
        end = total
    else:
        try:
            end = int(end_str)
        except ValueError:
            return None, None, error(t("orange_md_err_bad_end", end=end_str))
        if end < start:
            return None, None, error(t("orange_md_err_end_before_start", end=end, start=start, total=total, command=command_name, lb="{", rb="}"))
        if end > total:
            end = total  # 超過は末尾に丸める（エラーにしない）

    return start - 1, end - 1, None  # 0始まりに変換

# --- コマンド実装 ---

def cmd_outline(args: dict) -> dict:
    """ファイルの見出し構造と強調箇所を表示"""
    file_arg = args.get("file", "")
    abs_path, err = resolve_file(file_arg)
    if err:
        return err

    lines = load_lines(abs_path)
    total_lines = len(lines)
    full_text = "\n".join(lines)
    total_tokens = count_tokens(full_text)

    # 見出し行の位置を特定
    heading_indices = []
    for i, line in enumerate(lines):
        if re.match(r"^\s*#{1,6}\s+", line):
            heading_indices.append(i)

    # セクション情報を構築
    sections = []
    for idx, hi in enumerate(heading_indices):
        # セクションの終了行を決定
        if idx + 1 < len(heading_indices):
            section_end = heading_indices[idx + 1] - 1
        else:
            section_end = total_lines - 1

        # 末尾の空行を除外
        while section_end > hi and lines[section_end].strip() == "":
            section_end -= 1

        section_text = "\n".join(lines[hi:section_end + 1])
        section_tokens = count_tokens(section_text)

        sections.append({
            "line": hi + 1,
            "heading": lines[hi].rstrip(),
            "range": f"{hi + 1}-{section_end + 1}",
            "tokens": section_tokens
        })

        # このセクション内の **強調** を含む行を探す（見出し自体は除外）
        for j in range(hi + 1, section_end + 1):
            if "**" in lines[j] and re.search(r"\*\*.+?\*\*", lines[j]):
                sections.append({
                    "line": j + 1,
                    "text": lines[j].rstrip()
                })

    return success(
        t("orange_md_outline_summary", file=file_arg, lines=total_lines, tokens=total_tokens),
        {
            "file": file_arg,
            "total_lines": total_lines,
            "total_tokens": total_tokens,
            "sections": sections
        }
    )


def cmd_read(args: dict) -> dict:
    """指定範囲を行番号付きでそのまま表示"""
    file_arg = args.get("file", "")
    abs_path, err = resolve_file(file_arg)
    if err:
        return err

    lines = load_lines(abs_path)
    start_str = args.get("start", "")
    end_str = args.get("end", "")
    start_idx, end_idx, err = parse_range(lines, start_str, end_str, "read")
    if err:
        return err

    selected = []
    for i in range(start_idx, end_idx + 1):
        selected.append({"line": i + 1, "content": lines[i]})

    selected_text = "\n".join(lines[start_idx:end_idx + 1])
    tokens = count_tokens(selected_text)
    line_count = end_idx - start_idx + 1

    return success(
        t("orange_md_read_summary", file=file_arg, start=start_idx + 1, end=end_idx + 1, line_count=line_count, tokens=tokens),
        {
            "file": file_arg,
            "start": start_idx + 1,
            "end": end_idx + 1,
            "line_count": line_count,
            "tokens": tokens,
            "lines": selected
        }
    )


def cmd_scan(args: dict) -> dict:
    """指定範囲をキーワード抽出して圧縮表示"""
    file_arg = args.get("file", "")
    abs_path, err = resolve_file(file_arg)
    if err:
        return err

    lines = load_lines(abs_path)
    start_str = args.get("start", "")
    end_str = args.get("end", "")
    start_idx, end_idx, err = parse_range(lines, start_str, end_str, "scan")
    if err:
        return err

    # 元テキストのトークン数
    original_text = "\n".join(lines[start_idx:end_idx + 1])
    original_tokens = count_tokens(original_text)

    # キーワード抽出（添付コードのロジック）
    target_lines = lines[start_idx:end_idx + 1]

    sections = []
    current_section = {"heading": None, "keywords": []}

    for line in target_lines:
        stripped = line.strip()
        if stripped == "":
            continue

        # 見出し行
        hm = re.match(r"^(\s*#{1,6})\s+(.*)", line)
        if hm:
            if current_section["heading"] or current_section["keywords"]:
                sections.append(current_section)
            heading_prefix = hm.group(1)
            heading_kw = extract_keywords(hm.group(2), max_kw=4)
            if heading_kw:
                current_section = {
                    "heading": heading_prefix + " " + "/".join(heading_kw),
                    "keywords": []
                }
            else:
                current_section = {
                    "heading": hm.group(0).rstrip(),
                    "keywords": []
                }
            continue

        # リスト行
        lm = re.match(r"^(\s*(?:[-*+]|\d+\.)\s+)", line)
        if lm:
            body = line[lm.end():]
            kw = extract_keywords(body, max_kw=4)
            if kw:
                current_section["keywords"].extend(kw)
            continue

        # 通常行
        kw = extract_keywords(stripped, max_kw=5)
        if kw:
            current_section["keywords"].extend(kw)

    # 最後のセクション
    if current_section["heading"] or current_section["keywords"]:
        sections.append(current_section)

    # 出力テキスト構築
    result_lines = []
    for sec in sections:
        if sec["heading"]:
            result_lines.append("")
            result_lines.append(sec["heading"])
        all_kw = unique_ordered(sec["keywords"])
        for i in range(0, len(all_kw), 5):
            chunk = all_kw[i:i + 5]
            result_lines.append("/".join(chunk))

    # 先頭の空行除去
    while result_lines and result_lines[0] == "":
        result_lines.pop(0)

    compressed_text = "\n".join(result_lines)
    compressed_tokens = count_tokens(compressed_text)

    if original_tokens > 0:
        reduction = (1 - compressed_tokens / original_tokens) * 100
    else:
        reduction = 0.0

    return success(
        t("orange_md_scan_summary",
          file=file_arg, start=start_idx + 1, end=end_idx + 1,
          original=original_tokens, compressed=compressed_tokens,
          reduction=f"{reduction:.1f}"),
        {
            "file": file_arg,
            "start": start_idx + 1,
            "end": end_idx + 1,
            "original_tokens": original_tokens,
            "compressed_tokens": compressed_tokens,
            "reduction_percent": round(reduction, 1),
            "content": compressed_text
        }
    )


def cmd_help(args: dict) -> dict:
    """使い方を表示"""
    return build_help()


# --- メインディスパッチャ ---

COMMANDS = {
    "outline": cmd_outline,
    "read": cmd_read,
    "scan": cmd_scan,
    "help": cmd_help,
}

def main():
    try:
        raw = sys.stdin.read().strip()
    except Exception:
        raw = ""

    # 入力なしまたは空 → help
    if not raw:
        print(json.dumps(cmd_help({}), ensure_ascii=False, indent=2))
        return

    try:
        args = json.loads(raw)
    except json.JSONDecodeError:
        result = error(t("orange_md_err_json_parse", lb="{", rb="}"))
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return

    command = args.get("command", "")

    # command なし → help
    if not command:
        print(json.dumps(cmd_help({}), ensure_ascii=False, indent=2))
        return

    if command not in COMMANDS:
        valid = ", ".join(COMMANDS.keys())
        result = error(t("orange_md_err_unknown_command", command=command, valid=valid, lb="{", rb="}"))
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return

    result = COMMANDS[command](args)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()