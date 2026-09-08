"""開発者カテゴリ記事の投稿 CLI（人間＝カノン＋Claude 用）。

cg_blog サテライト（柚月の自発投稿）と同じ投稿パイプライン（MD→content/→build→deploy、
pair_id による日英紐付け）を流用しつつ、入口だけ人間向けにした素の Python スクリプト。

cg_blog との違い:
  - サテライトではなく、ターミナルから直接叩く CLI（位置引数でフォルダ指定）
  - MD の置き場は workspace 非依存（柚月の生活空間には触れない）
  - カテゴリは dev / dev_en（表示ラベルは「開発者」/「Developer」）
  - サムネ＆本文画像＆添付ファイルを「記事フォルダ」にまとめて入れておく方式
  - 本文の相対パス参照を自動で uploads/ にコピー＆実URLへ書き換え
  - 画像はコピー時に最大幅 1200px へリサイズ＆再圧縮（軽さ優先・集客=表示速度重視）

使い方:
  # 記事1本＝1フォルダ。フォルダごと渡す
  venv/Scripts/python.exe scripts/post_dev_article.py path/to/my-article/

  # 下書き（本番デプロイしない・ローカルビルドだけ）
  venv/Scripts/python.exe scripts/post_dev_article.py my-article/ --status draft

  # ビルドだけしてデプロイ保留
  venv/Scripts/python.exe scripts/post_dev_article.py my-article/ --skip-deploy

  # カテゴリ slug を上書き（既定 dev。en 側は自動で <slug>_en）
  venv/Scripts/python.exe scripts/post_dev_article.py my-article/ --category devlog

フォルダ構成（例）:
  my-article/
    jp.md            日本語本文（ja.md でも可）
    en.md            英語本文（省略可。後から追加もできる）
    main.jpg         サムネ（frontmatter thumbnail 指定が無ければ main.* を自動採用）
    diagram1.png     本文に ![説明](diagram1.png) で挟む図（何枚でも）
    sample.zip       [DL](sample.zip) で貼る添付ファイル（リサイズ対象外）

本文の書き方（jp.md / en.md 内）:
  MD の 1行目（冒頭空行スキップ後の最初の非空行）を `# タイトル` の H1 で始めること。
  この H1 がそのまま記事タイトルになり、本文からは自動で取り除かれる
  （サイトのテンプレが h1 を出すので、残すと二重タイトルになる）。
  H1 が無い MD はエラーで弾かれる。frontmatter は不要（cg_blog と同じルール）。

  相対パスの画像/添付はそのまま参照すれば良い。投稿時に実URLへ自動変換される。
    ![ベンチ結果](benchmark.png)
    詳しくは [サンプルをDL](sample.zip) を参照。

  例（jp.md）:
    # ベンチマーク結果の見方

    ## 1. 環境
    ...本文...
"""
from __future__ import annotations

import argparse
import datetime
import json
import re
import shutil
import sys
from pathlib import Path

# ── cg_blog の投稿ヘルパを再利用（パイプラインを二重実装しない）────────────
ROOT_DIR = Path(__file__).resolve().parent.parent
CG_BLOG_DIR = ROOT_DIR / "programs" / "cg_blog"
sys.path.insert(0, str(CG_BLOG_DIR))

import _post  # noqa: E402  (cg_blog/_post.py)

CONTENT_DIR = _post.CONTENT_DIR
UPLOADS_DIR = _post.UPLOADS_DIR

# 本文中の参照を拾う正規表現
#   markdown:  ![alt](path) / [text](path)  → 共通の "](path)" 部分を捕捉
#   raw HTML:  src="path" / href="path"
MD_LINK_RE = re.compile(r'(\]\()([^)\s]+)((?:\s+"[^"]*")?\))')
HTML_ATTR_RE = re.compile(r'((?:src|href)\s*=\s*")([^"]+)(")', re.IGNORECASE)

# リサイズ＆再圧縮の対象（それ以外＝zip/pdf 等はそのままコピー）
RESIZE_EXTS = {".jpg", ".jpeg", ".png", ".webp"}
IMAGE_MAX_WIDTH = 1200  # 軽さ優先（プランA）
JA_FILE_CANDIDATES = ("jp.md", "ja.md")
EN_FILE_CANDIDATES = ("en.md",)
THUMB_CANDIDATES = ("main.jpg", "main.jpeg", "main.png", "main.webp")


def _emit(payload: dict) -> int:
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if payload.get("status") == "ok" else 1


def _is_local_ref(path: str) -> bool:
    """本文中の参照が「フォルダ内のローカルアセット」かどうか。
    外部URL・絶対パス・アンカー・mailto・data URI は対象外。"""
    p = path.strip()
    if not p:
        return False
    lower = p.lower()
    if lower.startswith(("http://", "https://", "//", "/", "#", "mailto:", "tel:", "data:")):
        return False
    return True


def _resize_image(src: Path, dest: Path) -> None:
    """画像を最大幅 IMAGE_MAX_WIDTH に収めて再圧縮保存（拡大はしない）。
    Pillow が無ければそのままコピーにフォールバック。"""
    try:
        from PIL import Image
    except ImportError:
        shutil.copy2(src, dest)
        return
    img = Image.open(src)
    if img.width > IMAGE_MAX_WIDTH:
        ratio = IMAGE_MAX_WIDTH / img.width
        img = img.resize((IMAGE_MAX_WIDTH, max(1, round(img.height * ratio))), Image.LANCZOS)
    ext = dest.suffix.lower()
    if ext in (".jpg", ".jpeg"):
        img.convert("RGB").save(dest, "JPEG", quality=85, optimize=True)
    elif ext == ".png":
        img.save(dest, "PNG", optimize=True)
    elif ext == ".webp":
        img.save(dest, "WEBP", quality=85, method=6)
    else:
        img.save(dest)


def _place_asset(src: Path, dest_dir: Path, used_names: dict[str, Path]) -> str:
    """アセットを dest_dir に配置（画像はリサイズ）。ファイル名の衝突は連番回避。
    返り値は配置先のファイル名。"""
    name = src.name
    # 別ソースが同名を既に使っていたら連番でずらす
    if name in used_names and used_names[name] != src:
        stem, suffix = Path(name).stem, Path(name).suffix
        i = 1
        while f"{stem}-{i}{suffix}" in used_names:
            i += 1
        name = f"{stem}-{i}{suffix}"
    used_names[name] = src
    dest = dest_dir / name
    if not dest.exists():
        dest_dir.mkdir(parents=True, exist_ok=True)
        if src.suffix.lower() in RESIZE_EXTS:
            _resize_image(src, dest)
        else:
            shutil.copy2(src, dest)
    return name


def _rewrite_body(body: str, folder: Path, dest_dir: Path, url_base: str,
                  url_map: dict[str, str], used_names: dict[str, Path],
                  copied: list[str]) -> tuple[str, list[str]]:
    """本文中のローカル参照を uploads/ にコピーし、実URLへ書き換える。
    url_map は日英で共有（同じアセットは1度だけコピー）。
    返り値は (書き換え後の本文, 見つからなかった参照のリスト)。"""
    missing: list[str] = []

    def _resolve(rel: str) -> str | None:
        """相対参照を配置→実URLへ。見つからなければ None（本文は据え置き）。"""
        if not _is_local_ref(rel):
            return None
        if rel in url_map:
            return url_map[rel]
        src = (folder / rel).resolve()
        try:
            src.relative_to(folder.resolve())
        except ValueError:
            return None  # フォルダ外（../ など）は安全のため触らない
        if not src.is_file():
            missing.append(rel)
            return None
        name = _place_asset(src, dest_dir, used_names)
        url = f"{url_base}/{name}"
        url_map[rel] = url
        copied.append(f"{rel} -> {url}")
        return url

    def _md_sub(m: re.Match) -> str:
        url = _resolve(m.group(2))
        return m.group(1) + (url if url else m.group(2)) + m.group(3)

    def _html_sub(m: re.Match) -> str:
        url = _resolve(m.group(2))
        return m.group(1) + (url if url else m.group(2)) + m.group(3)

    body = MD_LINK_RE.sub(_md_sub, body)
    body = HTML_ATTR_RE.sub(_html_sub, body)
    return body, missing


def _find_lang_files(folder: Path) -> dict[str, Path]:
    out: dict[str, Path] = {}
    for cand in JA_FILE_CANDIDATES:
        if (folder / cand).is_file():
            out["ja"] = folder / cand
            break
    for cand in EN_FILE_CANDIDATES:
        if (folder / cand).is_file():
            out["en"] = folder / cand
            break
    return out


def _resolve_thumbnail(folder: Path, meta: dict) -> Path | None:
    """サムネ画像を決定。frontmatter thumbnail（相対名）優先、無ければ main.* を自動採用。"""
    fm = (meta.get("thumbnail") or "").strip()
    if fm and _is_local_ref(fm):
        cand = (folder / fm).resolve()
        if cand.is_file():
            return cand
    for cand in THUMB_CANDIDATES:
        if (folder / cand).is_file():
            return folder / cand
    return None


def run(folder: Path, *, base_category: str, status: str, skip_deploy: bool) -> int:
    if not folder.is_dir():
        return _emit({"status": "error", "message": f"フォルダが無い: {folder}"})

    lang_files = _find_lang_files(folder)
    if "ja" not in lang_files:
        return _emit({
            "status": "error",
            "message": f"日本語本文が見つからない（{folder} 直下に jp.md か ja.md を置く）",
            "hint": "最低でも JA 版が必要。EN 版(en.md)は後から追加でも可",
        })

    # ── 1. 各言語をパース（title/body 取得）──
    # タイトルは本文冒頭の H1 (`# タイトル`) 必須。cg_blog と同じルール。
    langs = [lg for lg in ("ja", "en") if lg in lang_files]
    parsed: dict[str, dict] = {}
    for lg in langs:
        meta, body = _post._parse_md_file(lang_files[lg])
        if not body.strip():
            return _emit({"status": "error", "message": f"{lg} 本文が空: {lang_files[lg].name}"})
        title, body_after_h1 = _post._extract_h1_title(body)
        if not title:
            return _emit({
                "status": "error",
                "reason": "no_h1_title",
                "lang": lg,
                "file": str(lang_files[lg].relative_to(folder)) if lang_files[lg].is_relative_to(folder) else str(lang_files[lg]),
                "message": (
                    f"{lg} 本文 {lang_files[lg].name} の冒頭に `# タイトル` が見当たりません。"
                    " MD の最初の非空行を H1 (半角の # + 半角スペース + タイトル文字列) にしてください。"
                    " その行がそのまま記事タイトルになり、本文からは自動で取り除かれます"
                    " (サイトのテンプレが h1 を出すので、残すと二重タイトルになります)。"
                ),
                "example_md": (
                    "# ベンチマーク結果の見方\n"
                    "\n"
                    "## 1. 環境\n"
                    "\n"
                    "計測はローカルの…(本文)\n"
                ),
                "hint": (
                    "H2 (##) や H3 は本文構造用に使ってOK。タイトル判定は最初の H1 だけ。"
                    " frontmatter の title: は使いません(本ツールは MD の H1 一本で判定します・cg_blog と同じ)。"
                ),
            })
        parsed[lg] = {"meta": meta, "body": body_after_h1.strip(), "title": title}

    # ── 2. サムネ決定（JA の frontmatter / main.* から）──
    thumb_src = _resolve_thumbnail(folder, parsed["ja"]["meta"])
    if thumb_src is None:
        return _emit({
            "status": "error",
            "message": "サムネ画像が見つからない",
            "hint": f"{folder.name} 直下に main.jpg/png を置くか、jp.md の frontmatter に thumbnail: 画像名 を書く",
        })

    # ── 3. アセット配置先を決定（pair で1ディレクトリを日英共有）──
    pair_id = _post._make_pair_id()
    ja_id = _post._next_post_id()  # アセットディレクトリ名に使う安定ID（= JA の wp_id）
    now = datetime.datetime.now()
    ym_dir = now.strftime("%Y") + "/" + now.strftime("%m")
    asset_subdir = f"dev-{ja_id}"
    dest_dir = UPLOADS_DIR / now.strftime("%Y") / now.strftime("%m") / asset_subdir
    url_base = f"/static/uploads/{ym_dir}/{asset_subdir}"

    # ── 4. サムネ＋本文アセットをコピー＆本文書き換え（日英で共有）──
    url_map: dict[str, str] = {}
    used_names: dict[str, Path] = {}
    copied: list[str] = []
    thumb_name = _place_asset(thumb_src, dest_dir, used_names)
    thumbnail_url = f"{url_base}/{thumb_name}"

    all_missing: dict[str, list[str]] = {}
    for lg in langs:
        new_body, missing = _rewrite_body(
            parsed[lg]["body"], folder, dest_dir, url_base, url_map, used_names, copied,
        )
        parsed[lg]["body"] = new_body
        if missing:
            all_missing[lg] = missing

    if all_missing:
        return _emit({
            "status": "error",
            "message": "本文が参照しているファイルがフォルダ内に見つからない（投稿中止）",
            "missing": all_missing,
            "hint": "綴り違い or フォルダに入れ忘れ。外部URLや絶対パスはそのまま使えます",
        })

    # ── 5. 各言語を content/{lang}/posts/ に書き出し ──
    written: list[dict] = []
    date_str = now.strftime("%Y-%m-%d %H:%M:%S")
    date_part = now.strftime("%Y-%m-%d")
    for lg in langs:
        p = parsed[lg]
        category = base_category if lg == "ja" else f"{base_category}_en"
        post_id = _post._next_post_id()  # JA を書いた後は EN が +1 される
        body_norm = p["body"] + "\n"
        slug = _post._slugify(p["title"], max_len=60)

        fm = ["---"]
        fm.append('title: "{}"'.format(p["title"].replace('"', '\\"')))
        fm.append(f"date: {date_str}")
        fm.append(f"language: {lg}")
        fm.append("post_type: post")
        fm.append(f"status: {status}")
        fm.append(f"wp_id: {post_id}")
        fm.append(f'slug: "{slug}"')
        fm.append(f"category: {category}")
        fm.append(f'thumbnail: "{thumbnail_url}"')
        fm.append(f"pair_id: {pair_id}")
        fm.append("post_kind: dev")  # 開発者記事フラグ（JSON-LD 著者を運営元名義にする等）
        fm.append(f"content_hash: {_post._content_hash(body_norm)}")
        fm.append("---")

        out_dir = CONTENT_DIR / lg / "posts"
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / f"{date_part}-{_post._slugify(p['title'], max_len=30)}-{post_id}.md"
        out_path.write_text("\n".join(fm) + "\n\n" + body_norm, encoding="utf-8")
        md_path = (str(out_path.relative_to(ROOT_DIR).as_posix())
                   if out_path.is_relative_to(ROOT_DIR) else str(out_path))
        written.append({
            "lang": lg,
            "post_id": post_id,
            "title": p["title"],
            "md_path": md_path,
            "url": f"https://www.crescent-grove.net{'/blog/' if lg == 'ja' else '/en/blog/'}",
        })

    # ── 6. build + deploy ──
    do_deploy = (status == "publish") and not skip_deploy
    ok, msg = _post._trigger_build_and_deploy(do_build=True, do_deploy=do_deploy)
    if not ok:
        return _emit({
            "status": "error",
            "message": f"MD は書けたが後段で失敗: {msg}",
            "posted": written,
            "pair_id": pair_id,
        })

    return _emit({
        "status": "ok",
        "message": "投稿完了: " + ", ".join(f"{w['lang']}={w['title']}" for w in written) + f" ({msg})",
        "posted": written,
        "pair_id": pair_id,
        "thumbnail": thumbnail_url,
        "category": base_category,
        "assets_copied": copied,
        "status_value": status,
        "deployed": do_deploy,
    })


def main() -> int:
    parser = argparse.ArgumentParser(
        description="開発者カテゴリ記事の投稿（記事フォルダをまるごと渡す）",
    )
    parser.add_argument("folder", help="記事フォルダ（jp.md/en.md + 画像/添付を含む）")
    parser.add_argument("--category", default="dev",
                        help="カテゴリ slug（既定 dev。en 側は自動で <slug>_en）")
    parser.add_argument("--status", default="publish", choices=["publish", "draft"],
                        help="publish(既定) / draft（draft は本番デプロイしない）")
    parser.add_argument("--skip-deploy", action="store_true",
                        help="ビルドはするが本番デプロイをしない（ローカル確認用）")
    ns = parser.parse_args()
    return run(
        Path(ns.folder).resolve(),
        base_category=ns.category,
        status=ns.status,
        skip_deploy=ns.skip_deploy,
    )


if __name__ == "__main__":
    sys.exit(main())
