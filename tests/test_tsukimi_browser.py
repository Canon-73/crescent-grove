"""
tsukimi_browser テスト
抽出 / ビューポート / リンク番号 / URL安全 / 歩数予算 / 状態 / 仮想ページ

ネットワークには一切出ない（fixture HTML と偽 requests を使う）。
pytest 不要・直接実行する:
    venv\\Scripts\\python.exe tests\\test_tsukimi_browser.py
"""
import json
import os
import shutil
import sys
import tempfile

TEST_DIR = tempfile.mkdtemp(prefix="tsukimi_test_")
os.environ["CG_WORKSPACE"] = TEST_DIR
os.environ.setdefault("CG_LANG", "ja")

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROG_DIR = os.path.join(REPO_ROOT, "programs", "tsukimi_browser")
PROGRAMS_DIR = os.path.dirname(PROG_DIR)
sys.path.insert(0, PROGRAMS_DIR)   # _i18n を引く
sys.path.insert(0, PROG_DIR)       # browser / session / startpage

import browser
import session as session_mod
import shot
import startpage

passed = 0
failed = 0


def check(name, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1
        print("  PASS: %s" % name)
    else:
        failed += 1
        print("  FAIL: %s %s" % (name, detail))


def fresh_session():
    """毎テスト新しい workspace を使う（本番 workspace には絶対触れない）。"""
    d = tempfile.mkdtemp(prefix="tsukimi_ws_", dir=TEST_DIR)
    return session_mod.Session(d)


# --------------------------------------------------------------------------
FIXTURE = """<!doctype html>
<html><head><title>テストページ</title></head>
<body>
<script>var x = "消えるべきJS";</script>
<style>.a { color: red }</style>
<nav><a href="/nav">ナビは捨てる</a></nav>
<main>
<h1>見出し</h1>
<p>これは<a href="/foo">最初のリンク</a>を含む段落です。</p>
<p>2つ目の段落。<a href="https://example.org/bar">外部リンク</a>と
<a href="/foo">同じリンクの再掲</a>と<a href="mailto:a@b.c">メール</a>と
<a href="#section">ページ内</a>。</p>
<ul><li>箇条書き1</li><li>箇条書き2</li></ul>
</main>
<footer><a href="/footer">フッタも捨てる</a></footer>
</body></html>"""


def test_extract():
    print("\n[抽出: ゴミ排除とリンク番号]")
    title, text, links = browser.extract(FIXTURE, "https://example.com/page")

    check("titleを取る", title == "テストページ", repr(title))
    check("scriptの中身が消える", "消えるべきJS" not in text)
    check("styleの中身が消える", "color: red" not in text)
    check("navが消える", "ナビは捨てる" not in text)
    check("footerが消える", "フッタも捨てる" not in text)
    check("本文が残る", "これは" in text and "箇条書き1" in text)

    urls = [l["url"] for l in links]
    check("相対URLが絶対化される", "https://example.com/foo" in urls, urls)
    check("外部リンクを拾う", "https://example.org/bar" in urls, urls)
    check("mailtoを除外", not any("mailto" in u for u in urls), urls)
    check("同一ページ内アンカーを除外", not any(u.endswith("#section") for u in urls), urls)
    check("重複URLは1つに集約", len([u for u in urls if u.endswith("/foo")]) == 1, urls)
    check("番号は1始まりの連番", [l["n"] for l in links] == list(range(1, len(links) + 1)))
    check("本文に[n]が埋まる", "[1]最初のリンク" in text, text[:200])
    check("重複リンクは同じ番号", text.count("[1]") == 2, text)

    # 段落が縦に細切れにならない（リンクごとに改行しない）
    para = [ln for ln in text.split("\n") if ln.startswith("これは")]
    check("インラインリンクで行が割れない",
          para and "最初のリンク" in para[0] and "段落です。" in para[0],
          para[:1])


def test_noise_blocks():
    print("\n[UI部品の除去]")
    html = """<html><head><title>記事</title></head><body><main>
    <div class="social-share"><a href="/tw">ツイート</a>Share</div>
    <ul class="breadcrumb"><li><a href="/top">トップ</a></li></ul>
    <div id="sidebar-related"><a href="/other">関連記事</a></div>
    <p>ここが本当に読みたい本文である。これは残らなければならない。</p>
    <div class="advert"><a href="/ad">広告</a></div>
    <p>本文の続き。</p>
    </main></body></html>"""
    title, text, links = browser.extract(html, "https://example.com/a")
    check("共有ボタンが消える", "Share" not in text and "ツイート" not in text, text)
    check("パンくずが消える", "トップ" not in text, text)
    check("関連記事ブロックが消える", "関連記事" not in text, text)
    check("広告が消える", "広告" not in text, text)
    check("本文は残る", "本当に読みたい本文" in text and "本文の続き" in text, text)
    check("消したブロックのリンクも番号に含まれない",
          all("/tw" not in l["url"] and "/ad" not in l["url"] for l in links), links)

    # 無関係な語（add / header など）を誤爆しない
    html2 = ("<html><body><main><div class='address'><p>%s</p></div>"
             "<div id='adding-notes'><p>%s</p></div></main></body></html>"
             % ("住所の説明。" * 10, "追記の説明。" * 10))
    _, text2, _ = browser.extract(html2, "https://example.com/b")
    check("addressやaddingを誤爆しない",
          "住所の説明" in text2 and "追記の説明" in text2, text2)


def test_paginate():
    print("\n[ビューポート]")
    text = "\n".join("段落%d %s" % (i, "あ" * 100) for i in range(30))
    pages = browser.paginate(text, size=500)
    check("複数画面に割れる", len(pages) > 1, len(pages))
    check("各画面がsize+1行以内", all(len(p) <= 700 for p in pages),
          [len(p) for p in pages])
    check("内容が失われない", "".join(pages).replace("\n", "") == text.replace("\n", ""))

    body, idx, total = browser.viewport(text, 2, size=500)
    check("画面番号が返る", idx == 2 and total == len(pages), (idx, total))
    body, idx, total = browser.viewport(text, 999, size=500)
    check("範囲外は末尾に丸める", idx == total, (idx, total))

    long_line = "あ" * 1200
    pages = browser.paginate(long_line, size=500)
    check("1行が長すぎる場合も分割する", len(pages) == 3, len(pages))


def test_url_safety():
    print("\n[URL安全チェック]")
    def resolver_public(host):
        return ["93.184.216.34"]
    def resolver_local(host):
        return ["127.0.0.1"]
    def resolver_private(host):
        return ["192.168.1.5"]
    def resolver_fail(host):
        raise OSError("no such host")

    ok, reason = browser.check_url("https://example.com/", resolver=resolver_public)
    check("公開サイトは通る", ok, reason)

    ok, reason = browser.check_url("http://localhost:13254/search", resolver=resolver_local)
    check("loopbackを拒否", not ok and reason == "private", reason)

    ok, reason = browser.check_url("http://nas.local/", resolver=resolver_private)
    check("privateアドレスを拒否", not ok and reason == "private", reason)

    ok, reason = browser.check_url("file:///C:/secret.txt", resolver=resolver_public)
    check("file:スキームを拒否", not ok and reason == "scheme", reason)

    ok, reason = browser.check_url("ftp://example.com/", resolver=resolver_public)
    check("ftp:スキームを拒否", not ok and reason == "scheme", reason)

    ok, reason = browser.check_url("https://nope.invalid/", resolver=resolver_fail)
    check("解決できない住所を拒否", not ok and reason == "dns", reason)

    ok, reason = browser.check_url("", resolver=resolver_public)
    check("空URLを拒否", not ok, reason)


def test_session_state():
    print("\n[セッション状態]")
    sess = fresh_session()
    sess.start(seed_text="きっかけ")
    page = {"url": "https://example.com/a", "title": "A", "text": "本文A",
            "links": [{"n": 1, "url": "https://example.com/b", "label": "B"}],
            "virtual": False, "viewport": 1}
    sess.set_current(page, push=False)
    sess.record_visit(page["url"], page["title"])
    sess.save()

    reloaded = session_mod.Session(str(sess.dir.parent.parent))
    reloaded.load()
    check("session.jsonがroundtripする",
          reloaded.current() and reloaded.current()["url"] == "https://example.com/a",
          reloaded.current())
    check("リンク番号解決", reloaded.link_by_number(1)["url"] == "https://example.com/b")
    check("存在しない番号はNone", reloaded.link_by_number(9) is None)

    # back
    page_b = dict(page, url="https://example.com/b", title="B")
    reloaded.set_current(page_b, push=True)
    back = reloaded.back()
    check("backで前のページへ戻る", back and back["url"] == "https://example.com/a", back)
    check("これ以上戻れないとNone", reloaded.back() is None)

    # ブックマーク重複
    reloaded.add_bookmark("https://example.com/a", "A", "メモ1")
    reloaded.add_bookmark("https://example.com/a", "A", "メモ2")
    marks = reloaded.bookmarks()
    check("同一URLは重複しない", len(marks) == 1, marks)
    check("noteが上書きされる", marks[0]["note"] == "メモ2", marks)

    # 履歴
    hist = reloaded.history()
    check("履歴が残る", any(h["url"] == "https://example.com/a" for h in hist), hist)

    # end
    summary = reloaded.end()
    check("endでまとめが返る", summary and summary["visited"], summary)
    check("endでセッションが閉じる", not reloaded.active())
    check("session.jsonが消える", not reloaded.session_path.exists())
    check("前回の続きが残る", reloaded.last_page() is not None)


def test_budget():
    print("\n[歩数予算]")
    sess = fresh_session()
    sess.start()
    check("初期はok", sess.budget_state() == "ok")

    for _ in range(session_mod.MOVE_SOFT):
        sess.count_move()
    check("ソフト上限でsoft", sess.budget_state() == "soft", sess.moves())

    for _ in range(session_mod.MOVE_HARD - session_mod.MOVE_SOFT):
        sess.count_move()
    check("ハード上限でhard", sess.budget_state() == "hard", sess.moves())

    sess2 = fresh_session()
    sess2.start()
    for _ in range(session_mod.SCREEN_SOFT):
        sess2.count_screen()
    check("画面数でもsoftになる", sess2.budget_state() == "soft", sess2.screens())


def test_virtual_pages():
    print("\n[仮想ページ]")
    sess = fresh_session()
    sess.start()
    sess.add_bookmark("https://example.com/x", "エックス", "好きなページ")
    sess.record_visit("https://example.com/y", "ワイ")

    page = startpage.bookmarks_page(sess)
    check("棚ページにリンクが載る",
          any(l["url"] == "https://example.com/x" for l in page["links"]), page["links"])
    check("棚ページにメモが出る", "好きなページ" in page["text"], page["text"])

    page = startpage.history_page(sess)
    check("履歴ページにリンクが載る",
          any(l["url"] == "https://example.com/y" for l in page["links"]), page["links"])

    # follow できる形になっているか（仮想ページも実ページと同じ扱い）
    sess.set_current(page, push=False)
    check("仮想ページのリンクをfollowできる",
          sess.link_by_number(1)["url"] == "https://example.com/y")

    empty = fresh_session()
    empty.start()
    page = startpage.bookmarks_page(empty)
    check("空の棚でも壊れない", page["links"] == [] and page["text"], page)


def test_startpage_and_lucky():
    print("\n[スタートページと lucky]")

    class FakeResp:
        status_code = 200
        headers = {"Content-Type": "application/rss+xml; charset=utf-8"}
        def __init__(self, body): self._body = body.encode("utf-8")
        def iter_content(self, n): yield self._body
        def close(self): pass

    RSS = """<?xml version="1.0"?><rss><channel>
    <item><title>ニュース1</title><link>https://news.example.com/1</link></item>
    <item><title>ニュース2</title><link>https://news.example.com/2</link></item>
    </channel></rss>"""

    class FakeRequests:
        def get(self, url, **kw): return FakeResp(RSS)

    # http_get は check_url を通るので、公開IPを返すリゾルバを一時的に差す
    orig_resolver = browser._default_resolver
    browser._default_resolver = lambda host: ["93.184.216.34"]
    try:
        items = startpage.fetch_feed("https://news.example.com/rss", _requests=FakeRequests())
        check("RSSから見出しを取る", len(items) == 2 and items[0]["title"] == "ニュース1", items)

        sess = fresh_session()
        sess.start()
        sess.add_bookmark("https://example.com/x", "エックス", "メモ")
        page = startpage.build_start_page(sess, seed_text="きっかけの話題",
                                          _requests=FakeRequests())
        check("スタートページにきっかけが載る", "きっかけの話題" in page["text"])
        check("スタートページにニュースが載る", "ニュース1" in page["text"])
        check("スタートページに棚が載る", "エックス" in page["text"])
        check("スタートページのリンクが番号付き",
              page["links"] and page["links"][0]["n"] == 1, page["links"][:2])
        check("スタートページは仮想ページ", page["virtual"] and page["url"] is None)

        # lucky: 母集団ごとに行き先が決まる
        class FixedRandom:
            def __init__(self, pool): self.pool = pool
            def shuffle(self, seq):
                seq.sort(key=lambda p: 0 if p == self.pool else 1)
            def choice(self, seq): return seq[0]

        pick = startpage.pick_lucky(sess, _requests=FakeRequests(),
                                    _random=FixedRandom("wikipedia"))
        check("luckyでWikipediaへ飛べる",
              pick and "wikipedia.org" in pick["url"] and pick["pool"] == "wikipedia", pick)

        pick = startpage.pick_lucky(sess, _requests=FakeRequests(),
                                    _random=FixedRandom("spots"))
        check("luckyで名簿から選べる", pick and pick["pool"] == "spots", pick)

        pick = startpage.pick_lucky(sess, _requests=FakeRequests(),
                                    _random=FixedRandom("rss"))
        check("luckyでRSS記事へ飛べる",
              pick and pick["pool"] == "rss" and "news.example.com" in pick["url"], pick)

        pick = startpage.pick_lucky(sess, _requests=FakeRequests(),
                                    _random=FixedRandom("memory"))
        check("luckyで履歴/棚を再訪できる", pick and pick["pool"] == "memory", pick)

        # 今日の散歩で見たばかりのページには飛ばない（発見が無いので）
        sess2 = fresh_session()
        sess2.start()
        sess2.add_bookmark("https://example.com/old", "むかし見た", "")
        sess2.record_visit("https://example.com/old", "むかし見た")
        pick = startpage.pick_lucky(sess2, _requests=FakeRequests(),
                                    _random=FixedRandom("memory"))
        check("さっき見たページはlucky対象から外れる",
              pick is None or pick["url"] != "https://example.com/old", pick)

        # 再挑戦の候補が複数出る（1つ目が403でも次を試せる）
        picks = list(startpage.iter_lucky(sess, _requests=FakeRequests(),
                                          _random=FixedRandom("wikipedia")))
        check("luckyは複数候補を出せる", len(picks) >= 2, len(picks))
        check("最初の候補は指定した母集団", picks[0]["pool"] == "wikipedia", picks[0])
    finally:
        browser._default_resolver = orig_resolver


def test_fetch_guards():
    print("\n[取得ガード]")
    orig_resolver = browser._default_resolver
    browser._default_resolver = lambda host: ["93.184.216.34"]

    class Resp:
        def __init__(self, status=200, ctype="text/html", body=b"<html><body>x</body></html>"):
            self.status_code = status
            self.headers = {"Content-Type": ctype}
            self._body = body
        def iter_content(self, n):
            for i in range(0, len(self._body), n):
                yield self._body[i:i + n]
        def close(self): pass

    try:
        class BinReq:
            def get(self, url, **kw): return Resp(ctype="application/pdf")
        res = browser.http_get("https://example.com/a.pdf", _requests=BinReq())
        check("PDFなど非テキストを弾く", not res["ok"] and res["kind"] == "binary", res)

        class ErrReq:
            def get(self, url, **kw): return Resp(status=403)
        res = browser.http_get("https://example.com/", _requests=ErrReq())
        check("403を拾う", not res["ok"] and res["kind"] == "http" and res["detail"] == 403, res)

        class BigReq:
            def get(self, url, **kw):
                return Resp(body=b"a" * (browser.MAX_BYTES + 5000))
        res = browser.http_get("https://example.com/", _requests=BigReq())
        check("巨大ページは打ち切る", res["ok"] and res["truncated"], res.get("truncated"))

        class EmptyReq:
            def get(self, url, **kw):
                return Resp(body=b"<html><body><div id='app'></div></body></html>")
        res = browser.fetch_page("https://example.com/", _requests=EmptyReq())
        check("空ページをjs_page扱いにする", not res["ok"] and res["kind"] == "js_page", res)

        class SpaReq:
            def get(self, url, **kw):
                shell = ("<html><head><title>SPA</title>"
                         "<script>%s</script></head>"
                         "<body><div id='root'>読み込み中</div></body></html>"
                         % ("var bundle=1;" * 3000))
                return Resp(body=shell.encode("utf-8"))
        res = browser.fetch_page("https://example.com/spa", _requests=SpaReq())
        check("SPAの殻をjs_page扱いにする", not res["ok"] and res["kind"] == "js_page", res)

        class ShortReq:
            def get(self, url, **kw):
                return Resp(body="<html><head><title>短い</title></head>"
                                 "<body><p>短いけれど、これはこれで立派なページ。</p></body></html>"
                                 .encode("utf-8"))
        res = browser.fetch_page("https://example.com/short", _requests=ShortReq())
        check("短いだけのページは表示する（誤ってjs_page扱いしない）",
              res["ok"] and "立派なページ" in res["text"], res)

        class OkReq:
            def get(self, url, **kw):
                return Resp(body=FIXTURE.encode("utf-8"))
        res = browser.fetch_page("https://example.com/page", _requests=OkReq())
        check("正常ページを取れる", res["ok"] and res["title"] == "テストページ", res.get("kind"))
        check("取得結果にリンクが入る", res["ok"] and len(res["links"]) >= 2)

        # cp932 のページも読める
        class SjisReq:
            def get(self, url, **kw):
                html = "<html><head><title>日本語</title></head><body><p>%s</p></body></html>" % ("テスト本文" * 40)
                return Resp(ctype="text/html; charset=Shift_JIS",
                            body=html.encode("cp932"))
        res = browser.fetch_page("https://example.com/sjis", _requests=SjisReq())
        check("cp932ページを読める", res["ok"] and res["title"] == "日本語", res.get("kind"))
    finally:
        browser._default_resolver = orig_resolver


def test_manifest_matches_commands():
    print("\n[manifestとmain.pyの整合]")
    import yaml
    with open(os.path.join(PROG_DIR, "manifest.yaml"), encoding="utf-8") as f:
        manifest = yaml.safe_load(f)
    enum = None
    for a in manifest.get("args", []):
        if a["name"] == "command":
            enum = a.get("enum")
    sys.path.insert(0, PROG_DIR)
    import main as tsukimi_main
    check("manifestのenumとCOMMANDSが一致",
          set(enum or []) == set(tsukimi_main.COMMANDS),
          (set(enum or []) ^ set(tsukimi_main.COMMANDS)))
    check("DISPATCHが全コマンドを持つ",
          set(tsukimi_main.DISPATCH) == set(tsukimi_main.COMMANDS),
          set(tsukimi_main.DISPATCH) ^ set(tsukimi_main.COMMANDS))
    check("url/textにpath_check:falseが付く",
          all(a.get("path_check") is False
              for a in manifest["args"] if a["name"] in ("url", "text")))


def test_look_shot():
    """look（ページの見た目を1枚の絵にする）。Chrome は起動せず、呼び出し方だけ検証する。"""
    print("\n[look / スクリーンショット]")
    from PIL import Image
    import subprocess as _sp

    # --- フラグの中身（うっかり外すと固まる・カノンの Chrome を汚す）---
    flags = shot._flags("C:/tmp/ud", 1280, 900, "C:/tmp/a.png", "UA/1.0")
    check("headless=new を使う（旧 --headless は固まる）", "--headless=new" in flags)
    check("使い捨てプロファイル", any(f.startswith("--user-data-dir=C:/tmp/ud") for f in flags))
    check("バックグラウンド通信を切る", "--disable-background-networking" in flags)
    check("localhost を名前で潰す",
          any("MAP localhost ~NOTFOUND" in f for f in flags))
    check("UA を引き継ぐ", "--user-agent=UA/1.0" in flags)
    check("窓の大きさとファイル名", "--window-size=1280,900" in flags
          and "--screenshot=C:/tmp/a.png" in flags)
    check("待ちっぱなしにならない上限がある",
          any(f.startswith("--virtual-time-budget=") for f in flags))
    check("撮影の待ち時間が manifest の timeout より短い", shot.CAPTURE_TIMEOUT < 60)

    # --- ブラウザが無い環境でも壊れない ---
    ok, reason = shot.capture("https://example.com", os.path.join(TEST_DIR, "x.png"),
                              browser_path=None if shot.find_browser() else "")
    if not shot.find_browser():
        check("ブラウザが無ければ no_browser", (ok, reason) == (False, "no_browser"))
    else:
        check("ブラウザが見つかる", bool(shot.find_browser()))

    # --- Chrome を呼ばずに capture の組み立てを見る ---
    called = {}

    def fake_run(cmd, **kw):
        called["cmd"] = cmd
        target = next(c.split("=", 1)[1] for c in cmd if c.startswith("--screenshot="))
        Image.new("RGB", (1280, 900 * 3), (10, 20, 30)).save(target)
        return _sp.CompletedProcess(cmd, 0, stdout=b"", stderr=b"")

    orig_run = shot.subprocess.run
    dest = os.path.join(TEST_DIR, "shot_n3.png")
    try:
        shot.subprocess.run = fake_run
        ok, reason = shot.capture("https://example.com/a", dest, screen=3,
                                  user_agent="UA/1.0", browser_path="chrome.exe")
    finally:
        shot.subprocess.run = orig_run
    check("撮影が成功する", ok and reason is None, reason)
    check("URL が最後の引数", called["cmd"][-1] == "https://example.com/a")
    check("n に応じて窓を縦に伸ばす",
          "--window-size=1280,2700" in called["cmd"], called["cmd"])
    with Image.open(dest) as im:
        check("切り出し前は縦長", im.size == (1280, 2700), str(im.size))
    shot.crop_screen(dest, 3)
    with Image.open(dest) as im:
        check("crop_screen で1画面ぶんになる", im.size == (1280, 900), str(im.size))

    # n の上限を超えても壊れない
    called.clear()
    try:
        shot.subprocess.run = fake_run
        shot.capture("https://example.com/a", os.path.join(TEST_DIR, "shot_big.png"),
                     screen=99, browser_path="chrome.exe")
    finally:
        shot.subprocess.run = orig_run
    check("n は MAX_SCREENS で頭打ち",
          f"--window-size=1280,{shot.VIEWPORT_H * shot.MAX_SCREENS}" in called["cmd"])

    # --- 撮り溜めを片付ける ---
    import time as _time
    shots_dir = os.path.join(TEST_DIR, "prune")
    os.makedirs(shots_dir, exist_ok=True)
    for i in range(shot.KEEP_SHOTS + 5):
        fp = os.path.join(shots_dir, f"{i:03d}.png")
        Image.new("RGB", (4, 4)).save(fp)
        os.utime(fp, (_time.time() + i, _time.time() + i))
    shot.prune(shots_dir)
    left = os.listdir(shots_dir)
    check("古い絵は片付けられる", len(left) == shot.KEEP_SHOTS, len(left))
    check("残るのは新しいほう", "024.png" in left and "000.png" not in left, sorted(left)[:3])


def test_look_command():
    """main.cmd_look の分岐（ネットワークにも Chrome にも出ない）。"""
    print("\n[look コマンド]")
    import main as tsukimi_main

    import contextlib
    import io as _io

    def run_look(sess, args):
        """cmd_look を呼び、stdout に出た JSON を読む。

        画面には 🎲 などの絵文字が乗るので、cp932 コンソールへ直接 print させると
        そこで落ちる（本番の _run_program は PYTHONIOENCODING=utf-8 を注入している）。
        捕まえてしまえばテストは端末の文字コードに依存しない。
        """
        buf = _io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = tsukimi_main.cmd_look(sess, args)
        return rc, json.loads(buf.getvalue())

    sess = fresh_session()
    sess.start()

    # 仮想ページ（玄関）は撮らない
    rc, payload = run_look(sess, {})
    check("仮想ページでは撮らずに理由を返す", rc == 1)
    check("仮想ページでは絵を添えない", "image" not in payload, payload.keys())

    # 実ページを現在ページにしてから撮る
    sess.set_current({"url": "https://example.com/x", "title": "T", "text": "本文",
                      "links": [], "virtual": False, "viewport": 1})
    calls = {}

    def fake_capture(url, dest, screen=1, user_agent="", browser_path=None):
        calls["url"], calls["screen"] = url, screen
        from PIL import Image
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        Image.new("RGB", (20, 20)).save(dest)
        return True, None

    orig = tsukimi_main.shot.capture
    try:
        tsukimi_main.shot.capture = fake_capture
        rc, payload = run_look(sess, {"n": 2})
    finally:
        tsukimi_main.shot.capture = orig
    check("実ページなら撮る", rc == 0)
    check("現在ページの URL を撮る", calls.get("url") == "https://example.com/x", calls)
    check("n が渡る", calls.get("screen") == 2, calls)
    # ここが本体。image が **トップレベル**に無いと core が絵として拾わない
    check("image がトップレベルに乗る", "image" in payload, payload.keys())
    check("image は workspace 相対パス",
          payload.get("image", "").startswith("program_data/tsukimi_browser/shots/")
          and payload["image"].endswith(".png"), payload.get("image"))
    check("本文（screen）も一緒に返る", "screen" in payload.get("data", {}))

    # 失敗しても散歩は止めない（テキストは読めるまま）
    def fail_capture(*a, **kw):
        return False, "no_browser"

    try:
        tsukimi_main.shot.capture = fail_capture
        rc, payload = run_look(sess, {})
    finally:
        tsukimi_main.shot.capture = orig
    check("撮れなくてもエラー画面で理由を返す", rc == 1)
    check("撮れなければ image を添えない", "image" not in payload, payload.keys())
    check("撮れなくてもページ本文は読めるまま",
          "本文" in payload["data"]["screen"], payload["data"]["screen"][:120])

    # 歩数を使い切っていたら url 付き look で移動しない
    for _ in range(session_mod.MOVE_HARD + 1):
        sess.count_move()
    rc, payload = run_look(sess, {"url": "https://example.com/y"})
    check("ハードストップ中は url 付き look を止める",
          sess.budget_state() == "hard" and rc == 1 and "image" not in payload)


def test_i18n_keys_present():
    print("\n[i18nキーの存在]")
    from _i18n import t
    keys = ["tsukimi_desc", "tsukimi_tool_desc", "tsukimi_help", "tsukimi_header",
            "tsukimi_footer_more", "tsukimi_err_private", "tsukimi_end_title",
            "tsukimi_start_title", "tsukimi_pool_wikipedia",
            "tsukimi_look_done_top", "tsukimi_look_done", "tsukimi_look_virtual",
            "tsukimi_look_fail_no_browser", "tsukimi_look_fail_timeout",
            "tsukimi_look_fail_failed", "tsukimi_look_fail_empty"]
    missing = [k for k in keys if t(k).startswith("{{t:")]
    check("主要キーがja辞書にある", not missing, missing)
    check("プレースホルダが展開される",
          "example.com" in t("tsukimi_header", host="example.com", title="T"))


if __name__ == "__main__":
    try:
        test_extract()
        test_noise_blocks()
        test_paginate()
        test_url_safety()
        test_session_state()
        test_budget()
        test_virtual_pages()
        test_startpage_and_lucky()
        test_fetch_guards()
        test_manifest_matches_commands()
        test_look_shot()
        test_look_command()
        test_i18n_keys_present()
    finally:
        shutil.rmtree(TEST_DIR, ignore_errors=True)

    print("\n%d passed, %d failed" % (passed, failed))
    sys.exit(1 if failed else 0)
