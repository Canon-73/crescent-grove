#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
crescent-grove.net 過去ブログ一括投入スクリプト（単発・ルールベース）

使い方（MINGW64 / venv有効化した状態で）:
    pip install markdown requests
    python import_blog.py            # ← まず「下書きで1本だけ」テスト投入
    python import_blog.py --publish-all   # ← 30本すべて公開で本番投入

対象: workspace/blog/ の *.md のうち、_en.md と README.md を除いたもの（日本語版）
日付: ファイル名先頭の YYYY-MM-DD をJSTの執筆日として使用（内部でUTCに変換しdate_gmtへ）
タイトル: 本文先頭の "# 見出し" 行。本文からはその行を除く（署名は残す）
"""

import os
import re
import sys
import glob
import base64
import json
from datetime import datetime, timezone, timedelta

import requests
import markdown

# ─────────────────────────────────────────────
# 設定
# ─────────────────────────────────────────────
# scripts/ から見て1段上がリポジトリルート
AGENT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENV_PATH = os.path.join(AGENT_DIR, ".env")
BLOG_DIR = os.path.join(AGENT_DIR, "workspace", "blog")
API_BASE = "https://www.crescent-grove.net/wp-json/wp/v2"
JST = timezone(timedelta(hours=9))


# ─────────────────────────────────────────────
# .env を素朴に読む（KEY=VALUE 形式）
# ─────────────────────────────────────────────
def load_env(path):
    env = {}
    if not os.path.exists(path):
        return env
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k = k.strip()
            v = v.strip().strip('"').strip("'")
            env[k] = v
    return env


# ─────────────────────────────────────────────
# 認証ヘッダー
# ─────────────────────────────────────────────
def make_auth(env):
    user = env.get("CG_WP_USERNAME", "") or os.environ.get("CG_WP_USERNAME", "")
    pw = env.get("CG_WP_APP_PASSWORD", "") or os.environ.get("CG_WP_APP_PASSWORD", "")
    pw = pw.replace(" ", "")
    if not user or not pw:
        print("[エラー] .env に CG_WP_USERNAME / CG_WP_APP_PASSWORD が見つかりません。")
        sys.exit(1)
    token = base64.b64encode(f"{user}:{pw}".encode("utf-8")).decode("ascii")
    return {"Authorization": f"Basic {token}"}


# ─────────────────────────────────────────────
# 対象ファイルを集める（日本語版のみ）
# ─────────────────────────────────────────────
def collect_files():
    all_md = sorted(glob.glob(os.path.join(BLOG_DIR, "*.md")))
    targets = []
    for p in all_md:
        name = os.path.basename(p)
        if name.lower() == "readme.md":
            continue
        if name.endswith("_en.md"):
            continue
        # 先頭が YYYY-MM-DD で始まるものだけ
        if not re.match(r"^\d{4}-\d{2}-\d{2}_", name):
            continue
        targets.append(p)
    return targets


# ─────────────────────────────────────────────
# ファイル名から日付、本文からタイトルを抽出
# ─────────────────────────────────────────────
def parse_file(path):
    name = os.path.basename(path)
    date_str = name[:10]  # YYYY-MM-DD

    with open(path, "r", encoding="utf-8") as f:
        text = f.read()

    lines = text.split("\n")
    title = None
    body_lines = lines[:]
    # 最初の "# 見出し" をタイトルに（その行は本文から除く）
    for i, line in enumerate(lines):
        m = re.match(r"^#\s+(.*)$", line.strip())
        if m:
            title = m.group(1).strip()
            body_lines = lines[:i] + lines[i + 1:]
            break
    if not title:
        title = name[11:].rsplit(".", 1)[0]  # 見出しが無ければファイル名から

    body_md = "\n".join(body_lines).strip()
    body_html = markdown.markdown(body_md, extensions=["extra", "nl2br"])
    return date_str, title, body_html


# ─────────────────────────────────────────────
# 日付（JSTの日付のみ）→ date_gmt 用 UTC 文字列
# 日付だけなのでJST正午に置いてからUTCへ（前後日へのズレ防止）
# ─────────────────────────────────────────────
def jst_date_to_gmt(date_str):
    dt = datetime.strptime(date_str, "%Y-%m-%d").replace(hour=12, tzinfo=JST)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


# ─────────────────────────────────────────────
# 1本投稿
# ─────────────────────────────────────────────
def post_one(headers, title, body_html, date_str, status):
    payload = {
        "title": title,
        "content": body_html,
        "status": status,
        "date_gmt": jst_date_to_gmt(date_str),
    }
    r = requests.post(API_BASE + "/posts", headers=headers, json=payload, timeout=30)
    if r.status_code in (200, 201):
        data = r.json()
        return True, data.get("id"), data.get("link")
    else:
        return False, r.status_code, r.text[:300]


# ─────────────────────────────────────────────
# メイン
# ─────────────────────────────────────────────
def main():
    publish_all = "--publish-all" in sys.argv

    env = load_env(ENV_PATH)
    headers = make_auth(env)
    headers["Content-Type"] = "application/json"

    files = collect_files()
    if not files:
        print(f"[エラー] 対象ファイルが見つかりません: {BLOG_DIR}")
        sys.exit(1)

    print(f"対象ファイル数: {len(files)} 本（日本語版のみ）")

    if not publish_all:
        # テストモード: 最初の1本だけ下書きで投入
        path = files[0]
        date_str, title, body_html = parse_file(path)
        print("\n=== テストモード（下書き1本のみ）===")
        print(f"ファイル : {os.path.basename(path)}")
        print(f"日付     : {date_str}")
        print(f"タイトル : {title}")
        ok, a, b = post_one(headers, title, body_html, date_str, "draft")
        if ok:
            print(f"[成功] 下書き作成 id={a}  {b}")
            print("\n管理画面で下書きを確認してください。")
            print("問題なければ:  python import_blog.py --publish-all")
        else:
            print(f"[失敗] http={a}  {b}")
        return

    # 本番モード: 全部公開で投入
    print("\n=== 本番モード（全件 publish）===")
    ok_count, ng_count = 0, 0
    for i, path in enumerate(files, 1):
        date_str, title, body_html = parse_file(path)
        ok, a, b = post_one(headers, title, body_html, date_str, "publish")
        if ok:
            ok_count += 1
            print(f"[{i:2d}/{len(files)}] OK  {date_str}  {title}  (id={a})")
        else:
            ng_count += 1
            print(f"[{i:2d}/{len(files)}] NG  {date_str}  {title}  http={a}  {b}")

    print(f"\n完了: 成功 {ok_count} / 失敗 {ng_count}")
    if ng_count:
        print("失敗分は上のNG行を確認してください。")


if __name__ == "__main__":
    main()
