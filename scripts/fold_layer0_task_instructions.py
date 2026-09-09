# -*- coding: utf-8 -*-
"""
Layer0 済みの会話履歴にある、スケジュールタスクの指示書本文の複製を畳む（オフライン用）。

背景: 毎日のタスク通知は同じ指示書ファイルの全文を毎回貼るため、Layer0 済み履歴の
user 側に同一文章が何十部も並んでいた。新しい Layer0（agent._fold_repeated_task_instruction）は
以後のターンを畳むが、既に Layer0 済みの部はそのまま残る。このスクリプトはそれを同じ規則で
一度だけ畳む。規則は agent.py のものをそのまま呼ぶので、判定が食い違うことはない。

  - 履歴の後ろ側に一字一句同じ本文が残っている部だけ畳む（最新の1部は必ず全文で残る）
  - 書き換えるのは user メッセージの本文だけ。メッセージ数・ターン境界・assistant は触らない

使い方（**サーバ停止中に実行すること**。稼働中はメモリ側が正で、ファイルを直しても次の保存で戻る）:

  venv\\Scripts\\python.exe scripts\\fold_layer0_task_instructions.py            # ドライラン（既定・読むだけ）
  venv\\Scripts\\python.exe scripts\\fold_layer0_task_instructions.py --apply    # 控えを取ってから書き換え

--apply は 8080 番が応答する（＝サーバ稼働中）と拒否する。控えは
data/context_state.json.before_fold_<日時> に置く。戻すときは控えを元の名前に戻すだけ。
"""
import argparse
import json
import os
import re
import shutil
import socket
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.agent import Agent            # noqa: E402
from core.tokens import count_text_tokens  # noqa: E402

STATE_PATH = ROOT / "data" / "context_state.json"
SERVER_PORT = 8080


def _text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(p.get("text", "") for p in content if isinstance(p, dict) and p.get("type") == "text")
    return str(content)


def _server_alive() -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", SERVER_PORT)) == 0


class _Ctx:
    """Agent._fold_repeated_task_instruction が触る部分だけの代役。"""

    def __init__(self, history):
        self.conversation_history = history

    @staticmethod
    def _get_text_from_content(content):
        return _text(content)


def _set_text(msg: dict, new_text: str):
    """content が文字列でもパーツ配列でも、テキスト部分だけ差し替える。"""
    content = msg.get("content", "")
    if isinstance(content, list):
        replaced = False
        for p in content:
            if isinstance(p, dict) and p.get("type") == "text" and not replaced:
                p["text"] = new_text
                replaced = True
            elif isinstance(p, dict) and p.get("type") == "text":
                p["text"] = ""
        if not replaced:
            content.append({"type": "text", "text": new_text})
    else:
        msg["content"] = new_text


def main() -> int:
    # cp932 コンソールで日本語が化けないように
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true", help="実際に書き換える（既定はドライラン）")
    ap.add_argument("--state", default=str(STATE_PATH), help="対象の context_state.json")
    args = ap.parse_args()

    state_path = Path(args.state)
    if not state_path.exists():
        print(f"対象が見つかりません: {state_path}")
        return 1
    if args.apply and _server_alive():
        print(f"{SERVER_PORT} 番が応答しています（サーバ稼働中）。停止してから --apply してください。")
        return 2

    state = json.loads(state_path.read_text(encoding="utf-8"))
    history = state.get("conversation_history", [])
    agent = Agent.__new__(Agent)
    agent.context = _Ctx(history)

    # Layer0 済み user メッセージの "task: ..." 部分だけを対象にする（生ターンは Layer0 が自分で畳む）
    task_line = re.compile(r"^task: ", re.MULTILINE)
    folded, kept, saved, skipped_list = 0, 0, 0, 0
    kept_heads = []
    for idx, msg in enumerate(history):
        if msg.get("role") != "user":
            continue
        text = _text(msg.get("content", ""))
        if "<!-- layer0 -->" not in text:
            continue
        # パーツ配列（画像付き等）はテキスト境界を崩さないよう触らない。Layer0 済みは本来 str
        if not isinstance(msg.get("content", ""), str):
            if "\ntask: " in text:
                skipped_list += 1
            continue
        m = task_line.search(text)
        if not m:
            continue
        # "task: " から末尾のマーカー直前までが task 本文
        marker_pos = text.rfind("<!-- layer0 -->")
        task_body = text[m.end():marker_pos].rstrip("\n")
        new_body = agent._fold_repeated_task_instruction(task_body, idx)
        if new_body == task_body:
            if "以下の指示書に従って" in task_body:
                kept += 1
                name = re.search(r"タスク名: (.+)", task_body)
                when = re.search(r"実行時刻: (\S+)", task_body)
                kept_heads.append(f"{when.group(1) if when else '?'} {name.group(1) if name else '?'}")
            continue
        new_text = text[:m.end()] + new_body + "\n" + text[marker_pos:]
        saved += count_text_tokens(text) - count_text_tokens(new_text)
        folded += 1
        if args.apply:
            _set_text(msg, new_text)

    print(f"畳む: {folded} 部 / 全文で残す: {kept} 部 / 削減: {saved:,} tok"
          + (f" / パーツ配列のため見送り: {skipped_list} 部" if skipped_list else ""))
    for h in kept_heads:
        print(f"  残す: {h}")

    if not args.apply:
        print("（ドライラン。--apply で書き換え）")
        return 0
    if folded == 0:
        print("書き換える部がないので何もしません。")
        return 0

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup = state_path.with_name(state_path.name + f".before_fold_{stamp}")
    shutil.copy2(state_path, backup)
    tmp = state_path.with_suffix(".json.tmp")
    # save_state と同じ書式（ensure_ascii=False・インデント無し）で書く
    tmp.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, state_path)
    print(f"書き換えました。控え: {backup}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
