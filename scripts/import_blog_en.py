#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
crescent-grove.net 英語版ブログ一括投入スクリプト（単発・ルールベース）

import_blog.py の英語版。日本語版は import_blog.py で投稿済み。
こちらは workspace/blog/ の *_en.md（英語版）を、Polylang で「英語記事」として投稿する。

英語として投稿する仕組み:
    さくらの WP に入っているカスタムプラグイン
    /crescent-grove/wp-content/plugins/polylang-api-lang/polylang-api-lang.php
    が、REST API の投稿時に lang パラメータを読んで pll_set_post_language() を呼ぶ。
    なので投稿ペイロードに "lang": "en" を足すだけで英語記事になる。
    （日本語版との翻訳ペア紐付け = translations は今回は送らない。後で手動で行う）

使い方（MINGW64 / venv有効化した状態で）:
    pip install markdown requests
    python import_blog_en.py            # ← まず「下書きで1本だけ」テスト投入
    python import_blog_en.py --publish-all   # ← 全件 公開で本番投入

対象: workspace/blog/ の *_en.md（英語版）のみ。README.md と日本語版は除外。
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

# Windows コンソール(cp932)だと em ダッシュ等で print が落ちるので UTF-8 に固定
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

# ─────────────────────────────────────────────
# 設定
# ─────────────────────────────────────────────
# scripts/ から見て1段上がリポジトリルート
AGENT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENV_PATH = os.path.join(AGENT_DIR, ".env")
BLOG_DIR = os.path.join(AGENT_DIR, "workspace", "blog")
API_BASE = "https://www.crescent-grove.net/wp-json/wp/v2"
JST = timezone(timedelta(hours=9))
LANG = "en"  # Polylang で割り当てる言語コード
CATEGORY_ID = 19  # 英語カテゴリ "Yuzuki's Diary" のタームID（Polylang上で英語側の別ID）


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
# 対象ファイルを集める（英語版 *_en.md のみ）
# ─────────────────────────────────────────────
def collect_files():
    all_md = sorted(glob.glob(os.path.join(BLOG_DIR, "*.md")))
    targets = []
    for p in all_md:
        name = os.path.basename(p)
        if name.lower() == "readme.md":
            continue
        # 英語版だけを対象にする
        if not name.endswith("_en.md"):
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
# 1本投稿（lang=en 付き）
# ─────────────────────────────────────────────
def post_one(headers, title, body_html, date_str, status):
    payload = {
        "title": title,
        "content": body_html,
        "status": status,
        "date_gmt": jst_date_to_gmt(date_str),
        "lang": LANG,  # ← カスタムプラグインがこれを読んで英語記事にする
        "categories": [CATEGORY_ID],  # ← 英語カテゴリ "Yuzuki's Diary"
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
def parse_skip(argv):
    """--skip N で先頭 N 本を飛ばす（途中再開用）。"""
    for i, a in enumerate(argv):
        if a == "--skip" and i + 1 < len(argv):
            try:
                return int(argv[i + 1])
            except ValueError:
                return 0
        if a.startswith("--skip="):
            try:
                return int(a.split("=", 1)[1])
            except ValueError:
                return 0
    return 0


def main():
    publish_all = "--publish-all" in sys.argv
    skip = parse_skip(sys.argv)

    env = load_env(ENV_PATH)
    headers = make_auth(env)
    headers["Content-Type"] = "application/json"

    files = collect_files()
    if not files:
        print(f"[エラー] 対象ファイルが見つかりません: {BLOG_DIR}")
        sys.exit(1)

    print(f"対象ファイル数: {len(files)} 本（英語版 *_en.md のみ）")

    if not publish_all:
        # テストモード: 最初の1本だけ下書きで投入
        path = files[0]
        date_str, title, body_html = parse_file(path)
        print("\n=== テストモード（下書き1本のみ・lang=en）===")
        print(f"ファイル : {os.path.basename(path)}")
        print(f"日付     : {date_str}")
        print(f"タイトル : {title}")
        ok, a, b = post_one(headers, title, body_html, date_str, "draft")
        if ok:
            print(f"[成功] 下書き作成 id={a}  {b}")
            print("\n管理画面で下書きを確認してください（言語が English になっているか）。")
            print("問題なければ:  python import_blog_en.py --publish-all")
        else:
            print(f"[失敗] http={a}  {b}")
        return

    # 本番モード: 全部公開で投入
    print("\n=== 本番モード（全件 publish・lang=en）===")
    if skip:
        print(f"※ --skip {skip}: 先頭 {skip} 本は投稿済みとして飛ばす（途中再開）")
    ok_count, ng_count, skip_count = 0, 0, 0
    for i, path in enumerate(files, 1):
        if i <= skip:
            skip_count += 1
            print(f"[{i:2d}/{len(files)}] -- skip  {os.path.basename(path)}")
            continue
        date_str, title, body_html = parse_file(path)
        ok, a, b = post_one(headers, title, body_html, date_str, "publish")
        if ok:
            ok_count += 1
            print(f"[{i:2d}/{len(files)}] OK  {date_str}  {title}  (id={a})")
        else:
            ng_count += 1
            print(f"[{i:2d}/{len(files)}] NG  {date_str}  {title}  http={a}  {b}")

    print(f"\n完了: 成功 {ok_count} / 失敗 {ng_count} / スキップ {skip_count}")
    if ng_count:
        print("失敗分は上のNG行を確認してください。")


if __name__ == "__main__":
    main()
