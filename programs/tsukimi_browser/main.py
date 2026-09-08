"""tsukimi_browser - テキストブラウザ。ネットを「散歩」するためのサテライト。

stdin から JSON 引数を受け取り、1画面ぶんのテキスト（screen）を stdout へ返す。
読むだけで副作用を持たない。書き込むのは自分の program_data/ だけ。
"""
import json
import os
import sys
import time
from urllib.parse import urlsplit

from _i18n import t

import browser
import session as session_mod
import shot
import startpage

COMMANDS = ["start", "open", "follow", "more", "back", "lucky", "search",
            "look", "bookmark", "bookmarks", "history", "end", "help"]

# 遷移としてカウントし、歩数ハードストップで止めるコマンド
MOVE_COMMANDS = ("open", "follow", "lucky", "search")

RULE = "─" * 40


# --------------------------------------------------------------------------
# 出力
# --------------------------------------------------------------------------
def out(screen, status="ok", image=None, **extra):
    data = {"screen": screen}
    data.update(extra)
    payload = {"status": status, "data": data}
    # image は **トップレベル**に置く。core の _run_program がここを見て、
    # 本文と一緒に絵そのものを柚月へ渡す（規約は programs/README.md §3）。
    if image:
        payload["image"] = image
    if status == "error":
        payload["message"] = extra.get("message") or screen.split("\n")[0]
    print(json.dumps(payload, ensure_ascii=False))
    return 0 if status == "ok" else 1


def plain_screen(body, footer=None):
    parts = [body]
    if footer:
        parts += ["", footer]
    return "\n".join(parts)


# --------------------------------------------------------------------------
# 画面描画
# --------------------------------------------------------------------------
def render(sess, page, note=None):
    """現在ページの1画面を組み立てる。柚月はこれをそのまま読む。"""
    text = page.get("text", "")
    idx = page.get("viewport", 1)
    body, idx, total = browser.viewport(text, idx)
    page["viewport"] = idx

    url = page.get("url")
    host = urlsplit(url).hostname if url else t("tsukimi_virtual_host")
    title = page.get("title") or ""

    header = t("tsukimi_header", host=host or "?", title=title)
    meta = t("tsukimi_meta", cur=idx, total=total,
             moves=sess.moves(), move_max=session_mod.MOVE_SOFT)

    lines = [header, meta]

    budget = sess.budget_state()
    if budget == "soft":
        lines.append(t("tsukimi_budget_soft"))
    elif budget == "hard":
        lines.append(t("tsukimi_budget_hard"))
    if note:
        lines.append(note)
    if page.get("truncated"):
        lines.append(t("tsukimi_truncated"))

    lines += [RULE, body, RULE]

    if idx < total:
        lines.append(t("tsukimi_footer_more"))
    else:
        lines.append(t("tsukimi_footer_end"))

    sess.count_screen()
    return "\n".join(lines)


def error_screen(kind, detail=None, title=None):
    """散歩を止めないためのエラー画面。back / lucky / end のヒントを必ず添える。"""
    key = {
        "scheme": "tsukimi_err_scheme",
        "bad_url": "tsukimi_err_bad_url",
        "dns": "tsukimi_err_dns",
        "private": "tsukimi_err_private",
        "http": "tsukimi_err_http",
        "binary": "tsukimi_err_binary",
        "js_page": "tsukimi_err_js_page",
        "timeout": "tsukimi_err_timeout",
        "network": "tsukimi_err_network",
        "redirect": "tsukimi_err_redirect",
    }.get(kind, "tsukimi_err_network")
    msg = t(key, detail=detail if detail is not None else "", title=title or "")
    return plain_screen(msg, t("tsukimi_footer_recover"))


# --------------------------------------------------------------------------
# ページ遷移の共通処理
# --------------------------------------------------------------------------
def goto(sess, url, note=None, push=True):
    """URL を開いて現在ページにする。失敗時はエラー画面（現在ページは保持）。"""
    from_url = (sess.current() or {}).get("url")
    res = browser.fetch_page(url)
    if not res.get("ok"):
        return error_screen(res.get("kind"), res.get("detail"), res.get("title")), "error"
    page = {
        "url": res["url"], "title": res["title"], "text": res["text"],
        "links": res["links"], "virtual": False, "viewport": 1,
        "truncated": res.get("truncated", False),
    }
    sess.set_current(page, push=push)
    sess.record_visit(res["url"], res["title"], from_url)
    return render(sess, page, note=note), "ok"


def show_virtual(sess, page, note=None):
    sess.set_current(page, push=True)
    return render(sess, page, note=note)


# --------------------------------------------------------------------------
# コマンド
# --------------------------------------------------------------------------
def cmd_start(sess, args):
    seed_text = args.get("text")
    seed_url = args.get("url")

    prelude = None
    if sess.active():
        # 前の散歩が開きっぱなしなら、畳んでから始める
        summary = sess.end()
        prelude = summarize(summary, closing=False)

    sess.start(seed_text=seed_text, seed_url=seed_url)
    page = startpage.build_start_page(sess, seed_text=seed_text, seed_url=seed_url)
    sess.set_current(page, push=False)
    screen = render(sess, page)
    if prelude:
        screen = prelude + "\n\n" + screen
    sess.save()
    return out(screen)


def cmd_open(sess, args):
    url = (args.get("url") or "").strip()
    if not url:
        return out(plain_screen(t("tsukimi_err_need_url"), t("tsukimi_footer_recover")),
                   status="error")
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    sess.count_move()
    screen, st = goto(sess, url)
    sess.save()
    return out(screen, status=st)


def cmd_follow(sess, args):
    n = args.get("n")
    cur = sess.current()
    if not cur:
        return out(plain_screen(t("tsukimi_err_no_page"), t("tsukimi_footer_recover")),
                   status="error")
    if n is None:
        return out(plain_screen(t("tsukimi_err_need_n"), t("tsukimi_footer_recover")),
                   status="error")
    link = sess.link_by_number(n)
    if not link:
        total = len(cur.get("links", []))
        return out(plain_screen(t("tsukimi_err_no_link", n=n, total=total),
                                t("tsukimi_footer_recover")), status="error")
    sess.count_move()
    screen, st = goto(sess, link["url"])
    sess.save()
    return out(screen, status=st)


def cmd_more(sess, args):
    page = sess.current()
    if not page:
        return out(plain_screen(t("tsukimi_err_no_page"), t("tsukimi_footer_recover")),
                   status="error")
    total = browser.page_count(page.get("text", ""))
    if page.get("viewport", 1) >= total:
        screen = render(sess, page, note=t("tsukimi_note_page_end"))
    else:
        page["viewport"] = page.get("viewport", 1) + 1
        screen = render(sess, page)
    sess.save()
    return out(screen)


def cmd_back(sess, args):
    page = sess.back()
    if not page:
        return out(plain_screen(t("tsukimi_err_no_back"), t("tsukimi_footer_recover")),
                   status="error")
    screen = render(sess, page, note=t("tsukimi_note_back"))
    sess.save()
    return out(screen)


LUCKY_TRIES = 3


def cmd_lucky(sess, args):
    """🎲 行き先が開けなければ別の母集団へ振り直す（散歩を止めないため）。"""
    sess.count_move()
    tried, screen, st = [], None, "error"
    for pick in startpage.iter_lucky(sess):
        note = t("tsukimi_note_lucky", pool=t("tsukimi_pool_" + pick["pool"]))
        if tried:
            note = t("tsukimi_note_lucky_retry",
                     pool=t("tsukimi_pool_" + pick["pool"]), tried=len(tried))
        screen, st = goto(sess, pick["url"], note=note)
        if st == "ok":
            break
        tried.append(pick)
        if len(tried) >= LUCKY_TRIES:
            break
    if screen is None:
        screen, st = plain_screen(t("tsukimi_err_lucky"), t("tsukimi_footer_recover")), "error"
    sess.save()
    return out(screen, status=st)


def cmd_search(sess, args):
    query = (args.get("text") or "").strip()
    if not query:
        return out(plain_screen(t("tsukimi_err_need_query"), t("tsukimi_footer_recover")),
                   status="error")
    sess.count_move()
    page = startpage.search_page(query)
    if not page:
        sess.save()
        return out(plain_screen(t("tsukimi_search_unavailable"), t("tsukimi_footer_recover")),
                   status="error")
    screen = show_virtual(sess, page)
    sess.save()
    return out(screen)


def cmd_look(sess, args):
    """いま居るページの見た目を1枚の絵にして見る。

    テキスト抽出では落ちてしまうもの（図・写真・レイアウト・画像の中の文字）を
    確かめるための「目を開ける」コマンド。url を渡せば、そこへ移ってから撮る。
    """
    url = (args.get("url") or "").strip()
    if url:
        if sess.budget_state() == "hard":
            # look は MOVE_COMMANDS に入っていないので、ここで自分で止める
            # （url 付きの look は実質 open + 撮影＝移動なので）
            return out(plain_screen(t("tsukimi_budget_stop"), t("tsukimi_footer_recover")),
                       status="error")
        # 生の URL を Chrome に渡さない。goto → http_get がリダイレクトを毎ホップ
        # 検証するので、撮るのは必ず「検証を通った後の URL」になる。
        if not url.startswith(("http://", "https://")):
            url = "https://" + url
        sess.count_move()
        screen, st = goto(sess, url)
        if st != "ok":
            sess.save()
            return out(screen, status=st)

    page = sess.current()
    if not page or not page.get("url"):
        return out(plain_screen(t("tsukimi_err_no_page"), t("tsukimi_footer_recover")),
                   status="error")
    if page.get("virtual"):
        # スタートページ・検索結果・ブックマーク一覧はこちらが組んだ画面なので撮る意味がない
        return out(render(sess, page, note=t("tsukimi_look_virtual")), status="error")

    screen_n = args.get("n") or 1
    try:
        screen_n = max(1, min(int(screen_n), shot.MAX_SCREENS))
    except (TypeError, ValueError):
        screen_n = 1

    shots_dir = sess.dir / "shots"
    name = f"{time.strftime('%Y%m%d_%H%M%S')}_{screen_n}.png"
    dest = shots_dir / name
    ok, reason = shot.capture(page["url"], dest, screen=screen_n,
                              user_agent=browser.USER_AGENT)
    if not ok:
        sess.save()
        return out(render(sess, page, note=t("tsukimi_look_fail_" + reason)), status="error")
    shot.crop_screen(dest, screen_n)
    shot.prune(shots_dir)

    note = t("tsukimi_look_done", n=screen_n) if screen_n > 1 else t("tsukimi_look_done_top")
    sess.save()   # 画面数は render() の中で数えられる
    # image は workspace からの相対パス。core 側がここから絵を読んで柚月に見せる。
    return out(render(sess, page, note=note),
               image=f"program_data/tsukimi_browser/shots/{name}")


def cmd_bookmark(sess, args):
    page = sess.current()
    if not page or not page.get("url"):
        return out(plain_screen(t("tsukimi_err_no_bookmark_target"), t("tsukimi_footer_recover")),
                   status="error")
    note = (args.get("text") or "").strip()
    sess.add_bookmark(page["url"], page.get("title"), note)
    screen = render(sess, page, note=t("tsukimi_note_bookmarked",
                                       note=note or t("tsukimi_note_no_memo")))
    sess.save()
    return out(screen)


def cmd_bookmarks(sess, args):
    screen = show_virtual(sess, startpage.bookmarks_page(sess))
    sess.save()
    return out(screen)


def cmd_history(sess, args):
    screen = show_virtual(sess, startpage.history_page(sess))
    sess.save()
    return out(screen)


def summarize(summary, closing=True):
    """散歩のまとめ（持ち帰り）。"""
    if not summary:
        return t("tsukimi_end_none")
    lines = [t("tsukimi_end_title")]
    visited = summary.get("visited", [])
    if visited:
        lines.append(t("tsukimi_end_visited", count=len(visited)))
        for v in visited:
            lines.append("  ・{}".format(v.get("title") or v.get("url")))
            lines.append("    {}".format(v.get("url")))
    else:
        lines.append(t("tsukimi_end_no_visit"))

    marked = summary.get("bookmarked", [])
    if marked:
        lines.append("")
        lines.append(t("tsukimi_end_bookmarked"))
        for m in marked:
            note = " ─ {}".format(m["note"]) if m.get("note") else ""
            lines.append("  ・{}{}".format(m.get("title") or m.get("url"), note))

    counters = summary.get("counters", {})
    lines.append("")
    lines.append(t("tsukimi_end_steps",
                   moves=counters.get("moves", 0), screens=counters.get("screens", 0)))
    if closing:
        lines.append("")
        lines.append(t("tsukimi_end_footer"))
    return "\n".join(lines)


def cmd_end(sess, args):
    if not sess.active():
        return out(plain_screen(t("tsukimi_err_no_session"), t("tsukimi_footer_recover")),
                   status="error")
    summary = sess.end()
    return out(summarize(summary))


def cmd_help(sess, args):
    return out(t("tsukimi_help"))


DISPATCH = {
    "start": cmd_start, "open": cmd_open, "follow": cmd_follow, "more": cmd_more,
    "back": cmd_back, "lucky": cmd_lucky, "search": cmd_search,
    "look": cmd_look, "bookmark": cmd_bookmark, "bookmarks": cmd_bookmarks, "history": cmd_history,
    "end": cmd_end, "help": cmd_help,
}


def main():
    try:
        raw = sys.stdin.read().strip()
        args = json.loads(raw) if raw else {}
    except Exception as e:
        print(json.dumps({"status": "error", "message": t("tsukimi_invalid_json", e=e)},
                         ensure_ascii=False))
        return 1

    command = (args.get("command") or "").strip()
    if command not in DISPATCH:
        return out(t("tsukimi_help"), status="error",
                   message=t("tsukimi_err_unknown_command", command=command))

    workspace = os.environ.get("CG_WORKSPACE", ".")
    sess = session_mod.Session(workspace)
    sess.load()

    if command == "help":
        return cmd_help(sess, args)

    # セッションが無ければ散歩を始める（follow/more/back は現在ページが要るので除く）
    if not sess.active():
        # look は「今のページ」を撮るので現在ページが要る。ただし url 付きの look は
        # open と同じで自分で行き先を持っているため、散歩を始めてよい。
        needs_page = ("follow", "more", "back", "bookmark")
        if command in needs_page or (command == "look" and not args.get("url")):
            return out(plain_screen(t("tsukimi_err_no_session"), t("tsukimi_footer_recover")),
                       status="error")
        if command != "start":
            sess.start()

    # 歩数ハードストップ: 遷移は止めるが、読みかけの整理と持ち帰りは常に可能
    if command in MOVE_COMMANDS and sess.active() and sess.budget_state() == "hard":
        return out(plain_screen(t("tsukimi_budget_stop"), t("tsukimi_footer_recover")),
                   status="error")

    try:
        return DISPATCH[command](sess, args)
    except Exception as e:
        return out(plain_screen(t("tsukimi_err_unexpected", e=str(e)[:200]),
                                t("tsukimi_footer_recover")), status="error")


if __name__ == "__main__":
    sys.exit(main())
