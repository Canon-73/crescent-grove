#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
wp_blog スキル - Crescent Grove 配布版
WordPressブログへの多言語投稿（Polylang対応）

基本方針:
  日英同時投稿がデフォルト。create_post に files: {ja: "...", en: "..."} を渡す。
  片方だけ（例外ケース）は files の片方だけ渡す。
  後付け紐付けは translation_of 引数で既存記事IDを渡す。

接続情報は env_keeper 管理の環境変数から取得:
  CG_WP_USERNAME / CG_WP_APP_PASSWORD / CG_WP_BASE_URL

Polylang 連携:
  WordPress 側にカスタムプラグイン polylang-api-lang.php が必要。
  REST 投稿時に payload の "lang" を読んで pll_set_post_language() を呼ぶ仕組み。

設定は workspace/program_files/wp_blog/config.json。
投稿済み記録は workspace/program_files/wp_blog/posted_log.json（二重投稿防止）。
"""
import sys
import os
import re
import json
import base64
import random
import urllib.request
import urllib.error
from datetime import datetime, timezone, timedelta

from _i18n import t

JST = timezone(timedelta(hours=9))
PROGRAM_NAME = "wp_blog"
SUPPORTED_LANGS = ("ja", "en")


# ═══════════════════════════════════════════════════════════
# パス解決（workspace基準）
# ═══════════════════════════════════════════════════════════

def workspace_dir():
    return os.environ.get("CG_WORKSPACE", ".")


def files_dir():
    """サテライト固有の設定置き場"""
    return os.path.join(workspace_dir(), "program_files", PROGRAM_NAME)


def config_path():
    return os.path.join(files_dir(), "config.json")


def posted_log_path():
    return os.path.join(files_dir(), "posted_log.json")


def safe_resolve(rel_path):
    """workspace基準の相対パスを解決。絶対パスや workspace 外を弾く。"""
    if not rel_path or not isinstance(rel_path, str):
        return None, t("wp_blog_err_path_empty")
    if os.path.isabs(rel_path):
        return None, t("wp_blog_err_path_absolute")
    base = os.path.abspath(workspace_dir())
    full = os.path.abspath(os.path.join(base, rel_path))
    if full != base and not full.startswith(base + os.sep):
        return None, t("wp_blog_err_path_escape")
    return full, None


# ═══════════════════════════════════════════════════════════
# 設定（初回はひな形を自動生成）
# ═══════════════════════════════════════════════════════════

CONFIG_TEMPLATE = {
    "default_category_id": {},
    "featured_image": {
        "enabled": False,
        "media_ids": {}
    }
}


def ensure_setup():
    """フォルダとconfigひな形を用意。返り値: (config_dict, created_now)"""
    created = False
    os.makedirs(files_dir(), exist_ok=True)
    if not os.path.exists(config_path()):
        with open(config_path(), "w", encoding="utf-8") as f:
            json.dump(CONFIG_TEMPLATE, f, ensure_ascii=False, indent=2)
        created = True
    return load_config(), created


def load_config():
    if not os.path.exists(config_path()):
        return json.loads(json.dumps(CONFIG_TEMPLATE))
    try:
        with open(config_path(), "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return json.loads(json.dumps(CONFIG_TEMPLATE))


def save_config(config):
    os.makedirs(files_dir(), exist_ok=True)
    with open(config_path(), "w", encoding="utf-8") as f:
        json.dump(config, f, ensure_ascii=False, indent=2)


def get_category_for_lang(config, lang):
    """default_category_id を言語別に取り出す。後方互換: int なら両言語共通とみなす。"""
    val = config.get("default_category_id")
    if val is None:
        return None
    if isinstance(val, dict):
        cat = val.get(lang)
        return cat if cat else None
    # 旧形式の単一int
    try:
        return int(val)
    except (TypeError, ValueError):
        return None


def get_media_ids_for_lang(config, lang):
    """featured_image.media_ids を言語別に取り出す。後方互換: list なら両言語共通。"""
    fi = config.get("featured_image") or {}
    ids = fi.get("media_ids")
    if ids is None:
        return []
    if isinstance(ids, dict):
        v = ids.get(lang, [])
        return list(v) if isinstance(v, list) else []
    if isinstance(ids, list):
        return list(ids)
    return []


# ═══════════════════════════════════════════════════════════
# 接続情報・認証
# ═══════════════════════════════════════════════════════════

def get_base_url():
    return os.environ.get("CG_WP_BASE_URL", "").strip().rstrip("/")


def get_credentials():
    user = os.environ.get("CG_WP_USERNAME", "")
    pw = os.environ.get("CG_WP_APP_PASSWORD", "").replace(" ", "")
    return user, pw


def auth_header():
    user, pw = get_credentials()
    if not user or not pw:
        return None
    token = base64.b64encode(f"{user}:{pw}".encode("utf-8")).decode("ascii")
    return f"Basic {token}"


# ═══════════════════════════════════════════════════════════
# APIリクエスト（常にdictを返す。例外を外に漏らさない）
# ═══════════════════════════════════════════════════════════

def api_request(method, path, body=None, need_auth=True):
    base = get_base_url()
    if not base:
        return {"error": t("wp_blog_err_no_base_url")}
    url = base + path
    if not url.lower().startswith("https://"):
        return {"error": t("wp_blog_err_non_https"), "url": url}

    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if need_auth:
        ah = auth_header()
        if ah is None:
            return {"error": t("wp_blog_err_no_credentials")}
        headers["Authorization"] = ah

    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as res:
            raw = res.read().decode()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        try:
            err_body = json.loads(e.read().decode())
        except Exception:
            err_body = e.reason
        return {"error": err_body, "http_code": e.code}
    except Exception as e:
        return {"error": str(e)}


# ═══════════════════════════════════════════════════════════
# Markdown → HTML（ライブラリ無しでも落ちない）
# ═══════════════════════════════════════════════════════════

def to_html(text, fmt="markdown"):
    if fmt == "html":
        return text, None
    try:
        import markdown
        return markdown.markdown(text, extensions=["extra", "nl2br"]), None
    except ImportError:
        warning = t("wp_blog_warn_no_markdown")
        blocks = [b.strip() for b in text.split("\n\n") if b.strip()]
        html = "\n".join("<p>" + b.replace("\n", "<br>\n") + "</p>" for b in blocks)
        return html, warning


# ═══════════════════════════════════════════════════════════
# .md パース: 先頭の # 見出しをタイトル、それ以外を本文
# ═══════════════════════════════════════════════════════════

def parse_md(full_path):
    """戻り値: (title, body_md) または (None, None)（タイトル無し）"""
    with open(full_path, "r", encoding="utf-8") as f:
        text = f.read()
    lines = text.split("\n")
    title = None
    body_start = None
    for i, line in enumerate(lines):
        stripped = line.strip()
        if not stripped:
            continue
        m = re.match(r"^#\s+(.+)$", stripped)
        if m:
            title = m.group(1).strip()
            body_start = i + 1
            break
        # 先頭の非空行が # でない → タイトルなし扱い
        return None, None
    if title is None:
        return None, None
    body = "\n".join(lines[body_start:]).strip()
    return title, body


# ═══════════════════════════════════════════════════════════
# 日付・ID補助
# ═══════════════════════════════════════════════════════════

def jst_to_gmt_string(date_str):
    date_str = date_str.strip()
    dt = None
    for f in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(date_str, f)
            break
        except ValueError:
            dt = None
    if dt is None:
        return None, t("wp_blog_err_date_format", date=date_str)
    if len(date_str) <= 10:
        dt = dt.replace(hour=12)
    return dt.replace(tzinfo=JST).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S"), None


def parse_id_list(value):
    if value is None:
        return None
    if isinstance(value, int):
        return [value]
    if isinstance(value, list):
        out = []
        for x in value:
            try:
                out.append(int(x))
            except (TypeError, ValueError):
                return None
        return out or None
    ids = [int(p.strip()) for p in str(value).split(",") if p.strip().isdigit()]
    return ids or None


# ═══════════════════════════════════════════════════════════
# posted_log: 二重投稿防止
# ═══════════════════════════════════════════════════════════

def load_posted_log():
    p = posted_log_path()
    if not os.path.exists(p):
        return {}
    try:
        with open(p, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_posted_log(log):
    os.makedirs(files_dir(), exist_ok=True)
    with open(posted_log_path(), "w", encoding="utf-8") as f:
        json.dump(log, f, ensure_ascii=False, indent=2)


# ═══════════════════════════════════════════════════════════
# コマンド: test_connection
# ═══════════════════════════════════════════════════════════

def cmd_test_connection(args, config):
    result = api_request("GET", "/users/me?context=edit")
    if "error" in result:
        return {"ok": False,
                "message": t("wp_blog_test_fail"),
                "error": result.get("error"), "http_code": result.get("http_code")}
    return {"ok": True, "message": t("wp_blog_test_ok"),
            "logged_in_as": result.get("name"), "user_id": result.get("id")}


# ═══════════════════════════════════════════════════════════
# コマンド: create_post （日英同時 or 片方のみ）
# ═══════════════════════════════════════════════════════════

def _no_posted_envelope():
    return {"ja": False, "en": False}


def cmd_create_post(args, config):
    files = args.get("files")
    if not isinstance(files, dict):
        return {
            "error": "files_required",
            "message": t("wp_blog_err_files_must_be_dict"),
            "posted": _no_posted_envelope(),
            "hint": t("wp_blog_hint_files"),
            "example": {"command": "create_post",
                        "files": {"ja": "blog/2024-03-15_spring.md",
                                  "en": "blog/2024-03-15_spring_en.md"}},
        }

    langs = [k for k in SUPPORTED_LANGS if files.get(k)]
    if not langs:
        return {
            "error": "no_files",
            "message": t("wp_blog_err_no_files"),
            "posted": _no_posted_envelope(),
            "hint": t("wp_blog_hint_files"),
            "example": {"command": "create_post",
                        "files": {"ja": "blog/2024-03-15_spring.md",
                                  "en": "blog/2024-03-15_spring_en.md"}},
        }

    status = (args.get("status") or "draft").lower()
    if status not in ("draft", "publish", "pending", "private", "future"):
        return {"error": "bad_status", "message": t("wp_blog_err_bad_status"),
                "posted": _no_posted_envelope()}

    date_gmt = None
    if args.get("date"):
        date_gmt, err = jst_to_gmt_string(args["date"])
        if err:
            return {"error": "bad_date", "message": err, "posted": _no_posted_envelope()}

    translation_of = args.get("translation_of")
    if translation_of is not None:
        try:
            translation_of = int(translation_of)
        except (TypeError, ValueError):
            return {"error": "bad_translation_of",
                    "message": t("wp_blog_err_bad_translation_of"),
                    "posted": _no_posted_envelope()}

    force = bool(args.get("force"))
    posted_log = load_posted_log()

    # ── バリデーション: 全 lang を先にチェック。1つでも失敗したら投稿しない ──
    parsed = {}
    for lang in langs:
        rel = files[lang]
        if not isinstance(rel, str) or not rel.strip():
            return {"error": "bad_path",
                    "message": t("wp_blog_err_path_empty"),
                    "lang": lang, "posted": _no_posted_envelope()}
        full, err = safe_resolve(rel)
        if err:
            return {"error": "bad_path", "message": err, "lang": lang, "file": rel,
                    "posted": _no_posted_envelope()}
        if not os.path.exists(full):
            return {"error": "file_not_found",
                    "message": t("wp_blog_err_file_not_found", file=rel),
                    "lang": lang, "file": rel,
                    "posted": _no_posted_envelope(),
                    "hint": t("wp_blog_hint_workspace_relative")}
        # 二重投稿チェック
        if not force and rel in posted_log:
            entry = posted_log[rel]
            return {
                "error": "already_posted",
                "message": t("wp_blog_err_already_posted", file=rel, id=entry.get("id")),
                "lang": lang, "file": rel,
                "posted_id": entry.get("id"),
                "posted_at": entry.get("posted_at"),
                "posted": _no_posted_envelope(),
                "hint": t("wp_blog_hint_force_repost"),
            }
        try:
            title, body = parse_md(full)
        except Exception as e:
            return {"error": "read_failed",
                    "message": t("wp_blog_err_read_failed", file=rel, error=str(e)),
                    "lang": lang, "file": rel, "posted": _no_posted_envelope()}
        if title is None:
            return {
                "error": "title_missing",
                "message": t("wp_blog_err_title_missing", file=rel),
                "file": rel, "lang": lang,
                "posted": _no_posted_envelope(),
                "hint": t("wp_blog_hint_title_missing"),
                "example": "# 春の話\n\n本文…",
            }
        parsed[lang] = {"rel": rel, "full": full, "title": title, "body": body}

    # ── 引数の共通オプション ──
    cats_arg = parse_id_list(args.get("categories"))
    tags_arg = parse_id_list(args.get("tags"))
    excerpt = args.get("excerpt")
    slug = args.get("slug")
    want_featured = args.get("featured")

    fmt = (args.get("format") or "markdown").lower()
    if fmt not in ("markdown", "html"):
        return {"error": "bad_format", "message": t("wp_blog_err_bad_format"),
                "posted": _no_posted_envelope()}

    # ── 投稿フェーズ ──
    posted = {"ja": False, "en": False}
    results = {}
    any_warning = None

    for lang in langs:
        info = parsed[lang]
        html, warning = to_html(info["body"], fmt)
        if warning and not any_warning:
            any_warning = warning

        payload = {
            "title": info["title"],
            "content": html,
            "status": status,
            "lang": lang,  # ← polylang-api-lang.php が読む
        }
        if date_gmt:
            payload["date_gmt"] = date_gmt

        if cats_arg:
            payload["categories"] = cats_arg
        else:
            cat = get_category_for_lang(config, lang)
            if cat:
                payload["categories"] = [cat]

        if tags_arg:
            payload["tags"] = tags_arg
        if excerpt:
            payload["excerpt"] = excerpt
        if slug:
            payload["slug"] = slug

        # アイキャッチ: 引数 > config.enabled
        wf = want_featured if want_featured is not None else bool(
            (config.get("featured_image") or {}).get("enabled"))
        featured_note = None
        if wf:
            ids = get_media_ids_for_lang(config, lang)
            if ids:
                payload["featured_media"] = random.choice(ids)
            else:
                featured_note = t("wp_blog_featured_empty_note_lang", lang=lang)

        r = api_request("POST", "/posts", payload)
        if "error" in r:
            results[lang] = {
                "posted": False,
                "file": info["rel"],
                "title": info["title"],
                "error": r.get("error"),
                "http_code": r.get("http_code"),
            }
        else:
            posted[lang] = True
            results[lang] = {
                "posted": True,
                "id": r.get("id"),
                "link": r.get("link"),
                "status": r.get("status"),
                "date": r.get("date"),
                "title": info["title"],
                "file": info["rel"],
                "lang": lang,
            }
            if featured_note:
                results[lang]["featured_note"] = featured_note
            posted_log[info["rel"]] = {
                "id": r.get("id"),
                "posted_at": datetime.now(JST).isoformat(timespec="seconds"),
                "lang": lang,
                "title": info["title"],
            }

    save_posted_log(posted_log)

    # ── 翻訳ペア紐付け ──
    pairing = None
    if posted["ja"] and posted["en"]:
        ja_id = results["ja"]["id"]
        en_id = results["en"]["id"]
        pair_payload = {"translations": {"ja": ja_id, "en": en_id}}
        link_err = None
        for pid in (ja_id, en_id):
            r = api_request("POST", f"/posts/{pid}", pair_payload)
            if "error" in r:
                link_err = r.get("error")
                break
        if link_err:
            pairing = {"linked": False,
                       "message": t("wp_blog_pairing_failed", error=str(link_err)),
                       "ja_id": ja_id, "en_id": en_id}
        else:
            pairing = {"linked": True,
                       "message": t("wp_blog_paired_ok", ja_id=ja_id, en_id=en_id),
                       "ja_id": ja_id, "en_id": en_id}
    elif translation_of and (posted["ja"] or posted["en"]):
        new_lang = "ja" if posted["ja"] else "en"
        other_lang = "en" if new_lang == "ja" else "ja"
        new_id = results[new_lang]["id"]
        pair_payload = {"translations": {new_lang: new_id, other_lang: translation_of}}
        link_err = None
        for pid in (new_id, translation_of):
            r = api_request("POST", f"/posts/{pid}", pair_payload)
            if "error" in r:
                link_err = r.get("error")
                break
        if link_err:
            pairing = {"linked": False,
                       "message": t("wp_blog_pairing_failed", error=str(link_err)),
                       "new_id": new_id, "existing_id": translation_of}
        else:
            pairing = {"linked": True,
                       "message": t("wp_blog_paired_with_existing",
                                    new_id=new_id, existing_id=translation_of),
                       "new_id": new_id, "existing_id": translation_of}

    # ── 最終メッセージ ──
    out = {"posted": posted}
    for lang in SUPPORTED_LANGS:
        if lang in results:
            out[lang] = results[lang]
    if pairing:
        out["pairing"] = pairing
    if any_warning:
        out["warning"] = any_warning

    both_requested = "ja" in langs and "en" in langs
    if both_requested:
        if posted["ja"] and posted["en"]:
            out["message"] = t("wp_blog_msg_both_ok", status=status)
        elif posted["ja"] or posted["en"]:
            success_lang = "ja" if posted["ja"] else "en"
            failed_lang = "en" if success_lang == "ja" else "ja"
            failed_id = results[success_lang]["id"]
            out["message"] = t("wp_blog_msg_one_ok_one_failed",
                               success_lang=success_lang, failed_lang=failed_lang,
                               success_id=failed_id)
            out["recovery_hint"] = t("wp_blog_hint_recover_pair",
                                     failed_lang=failed_lang,
                                     existing_id=failed_id)
        else:
            out["message"] = t("wp_blog_msg_both_failed")
    else:
        only_lang = langs[0]
        if posted[only_lang]:
            out["message"] = t("wp_blog_msg_single_ok",
                               lang=only_lang, status=status)
        else:
            out["message"] = t("wp_blog_msg_single_failed", lang=only_lang)

    return out


# ═══════════════════════════════════════════════════════════
# コマンド: list_posts / get_post / list_categories / list_tags
# ═══════════════════════════════════════════════════════════

def cmd_list_posts(args, config):
    per_page = min(int(args.get("per_page", 20)), 100)
    path = (f"/posts?per_page={per_page}&context=edit"
            f"&status=publish,draft,pending,private,future&orderby=date&order=desc")
    result = api_request("GET", path)
    if isinstance(result, dict) and "error" in result:
        return {"error": result.get("error"), "http_code": result.get("http_code")}
    posts = []
    for p in result:
        posts.append({
            "id": p.get("id"),
            "title": (p.get("title") or {}).get("rendered", ""),
            "status": p.get("status"),
            "date": p.get("date"),
            "link": p.get("link"),
            "lang": p.get("lang"),
            "translations": p.get("translations"),
        })
    return {"total": len(posts), "posts": posts}


def cmd_get_post(args, config):
    post_id = args.get("post_id")
    if not post_id:
        return {"error": t("wp_blog_err_post_id_required")}
    result = api_request("GET", f"/posts/{post_id}?context=edit")
    if "error" in result:
        return {"error": result.get("error"), "http_code": result.get("http_code")}
    return {"id": result.get("id"),
            "title": (result.get("title") or {}).get("rendered", ""),
            "status": result.get("status"), "date": result.get("date"),
            "link": result.get("link"),
            "lang": result.get("lang"),
            "translations": result.get("translations"),
            "content_html": (result.get("content") or {}).get("rendered", "")}


def cmd_list_categories(args, config):
    result = api_request("GET", "/categories?per_page=100", need_auth=False)
    if isinstance(result, dict) and "error" in result:
        return {"error": result.get("error"), "http_code": result.get("http_code")}
    return {"categories": [{"id": c.get("id"), "name": c.get("name"),
                            "count": c.get("count"),
                            "lang": c.get("lang")} for c in result]}


def cmd_list_tags(args, config):
    result = api_request("GET", "/tags?per_page=100", need_auth=False)
    if isinstance(result, dict) and "error" in result:
        return {"error": result.get("error"), "http_code": result.get("http_code")}
    return {"tags": [{"id": tg.get("id"), "name": tg.get("name"),
                      "count": tg.get("count"),
                      "lang": tg.get("lang")} for tg in result]}


# ═══════════════════════════════════════════════════════════
# コマンド: show_config / set_default_category / set_featured
# ═══════════════════════════════════════════════════════════

def cmd_show_config(args, config):
    return {"config_path": config_path(), "config": config,
            "posted_log_path": posted_log_path(),
            "posted_count": len(load_posted_log())}


def _normalize_lang(args):
    lang = args.get("lang")
    if lang not in SUPPORTED_LANGS:
        return None, {"error": "bad_lang",
                      "message": t("wp_blog_err_bad_lang"),
                      "hint": t("wp_blog_hint_lang_values")}
    return lang, None


def cmd_set_default_category(args, config):
    lang, err = _normalize_lang(args)
    if err:
        return err
    cur = config.get("default_category_id")
    # 後方互換: int → dict
    if cur is None:
        new = {}
    elif isinstance(cur, dict):
        new = dict(cur)
    else:
        try:
            shared = int(cur)
            new = {l: shared for l in SUPPORTED_LANGS}
        except (TypeError, ValueError):
            new = {}

    cat = args.get("category_id")
    if cat is None or cat == "":
        new.pop(lang, None)
    else:
        try:
            new[lang] = int(cat)
        except (TypeError, ValueError):
            return {"error": "bad_category_id",
                    "message": t("wp_blog_err_bad_category_id")}
    config["default_category_id"] = new
    save_config(config)
    return {"ok": True, "default_category_id": new,
            "message": t("wp_blog_msg_default_category_set",
                         lang=lang, value=str(new.get(lang)))}


def cmd_set_featured(args, config):
    lang, err = _normalize_lang(args)
    if err:
        return err
    media_ids_raw = args.get("media_ids")
    if media_ids_raw is None:
        return {"error": "media_ids_required",
                "message": t("wp_blog_err_media_ids_required"),
                "hint": t("wp_blog_hint_media_ids_form")}
    if isinstance(media_ids_raw, list):
        try:
            ids = [int(x) for x in media_ids_raw]
        except (TypeError, ValueError):
            return {"error": "bad_media_ids",
                    "message": t("wp_blog_err_bad_media_ids")}
    elif isinstance(media_ids_raw, (str, int)):
        ids = parse_id_list(media_ids_raw) or []
    else:
        return {"error": "bad_media_ids",
                "message": t("wp_blog_err_bad_media_ids")}

    fi = config.get("featured_image") or {"enabled": False, "media_ids": {}}
    cur_ids = fi.get("media_ids")
    if cur_ids is None:
        new_ids = {}
    elif isinstance(cur_ids, dict):
        new_ids = dict(cur_ids)
    elif isinstance(cur_ids, list):
        # 後方互換: 旧list → 両言語共通
        new_ids = {l: list(cur_ids) for l in SUPPORTED_LANGS}
    else:
        new_ids = {}
    new_ids[lang] = ids
    fi["media_ids"] = new_ids
    if "enabled" in args and args.get("enabled") is not None:
        fi["enabled"] = bool(args["enabled"])
    config["featured_image"] = fi
    save_config(config)
    return {"ok": True, "featured_image": fi,
            "message": t("wp_blog_msg_featured_set",
                         lang=lang, count=len(ids),
                         enabled=str(fi.get("enabled")))}


# ═══════════════════════════════════════════════════════════
# ディスパッチ
# ═══════════════════════════════════════════════════════════

COMMANDS = {
    "test_connection": cmd_test_connection,
    "create_post": cmd_create_post,
    "list_posts": cmd_list_posts,
    "get_post": cmd_get_post,
    "list_categories": cmd_list_categories,
    "list_tags": cmd_list_tags,
    "show_config": cmd_show_config,
    "set_default_category": cmd_set_default_category,
    "set_featured": cmd_set_featured,
}


def _help_dict():
    return {
        "test_connection": t("wp_blog_help_test_connection"),
        "create_post": t("wp_blog_help_create_post"),
        "list_posts": t("wp_blog_help_list_posts"),
        "get_post": t("wp_blog_help_get_post"),
        "list_categories": t("wp_blog_help_list_categories"),
        "list_tags": t("wp_blog_help_list_tags"),
        "show_config": t("wp_blog_help_show_config"),
        "set_default_category": t("wp_blog_help_set_default_category"),
        "set_featured": t("wp_blog_help_set_featured"),
    }


def main():
    try:
        raw = sys.stdin.read().strip()
        args = json.loads(raw) if raw else {}
    except Exception as e:
        print(json.dumps({"status": "error", "message": f"Invalid JSON: {e}"}))
        sys.exit(1)

    config, created = ensure_setup()
    command = args.get("command")

    if not command:
        user, pw = get_credentials()
        out = {
            "message": t("wp_blog_help_title"),
            "commands": _help_dict(),
            "credentials_configured": bool(user and pw),
            "base_url_configured": bool(get_base_url()),
            "config_path": config_path(),
            "default_category_id": config.get("default_category_id"),
            "featured_enabled": bool((config.get("featured_image") or {}).get("enabled")),
            "featured_media_ids": (config.get("featured_image") or {}).get("media_ids"),
            "posted_log_path": posted_log_path(),
        }
        if created:
            out["setup_note"] = t("wp_blog_setup_note")
        print(json.dumps({"status": "ok", "data": out}, ensure_ascii=False))
        return

    if command not in COMMANDS:
        print(json.dumps({"status": "error",
                          "message": t("wp_blog_err_unknown_command", command=command),
                          "available": list(COMMANDS.keys())}, ensure_ascii=False))
        return

    try:
        result = COMMANDS[command](args, config)
        print(json.dumps({"status": "ok", "data": result}, ensure_ascii=False))
    except Exception as e:
        print(json.dumps({"status": "error", "message": str(e)}))
        sys.exit(1)


if __name__ == "__main__":
    main()
