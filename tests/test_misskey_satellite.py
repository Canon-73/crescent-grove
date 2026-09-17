"""
misskey_satellite テスト

実ネットワークには一切出ない（api._transport を差し替えて検証する）。
柚月の workspace / 状態ファイルにも触らない（このサテライトは状態を持たない）。

実行: venv\\Scripts\\python.exe tests\\test_misskey_satellite.py
（pytest不要・自前ランナー）

検査の重心は docs/MISSKEY_SATELLITE_DESIGN.md §8:
- Misskey 固有の落とし穴（通知の暗黙既読化 / Retry-After はヘッダ / 204空ボディ）
- manifest への引数宣言漏れ（宣言し忘れるとコマンドが起動すらしない）
- トークンが出力のどこにも出ないこと
"""
import json
import os
import re
import subprocess
import sys
import tempfile

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

TEST_DIR = tempfile.mkdtemp(prefix="msat_test_")
os.environ["CG_WORKSPACE"] = TEST_DIR
os.environ["CG_LANG"] = "ja"
FAKE_TOKEN = "FAKE-MISSKEY-TOKEN-should-never-leak"
os.environ["CG_MISSKEY_TOKEN"] = FAKE_TOKEN
os.environ.pop("CG_MISSKEY_BASE_URL", None)

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SAT_DIR = os.path.join(REPO_ROOT, "programs", "misskey_satellite")
PROGRAMS_DIR = os.path.dirname(SAT_DIR)
MAIN_PY = os.path.join(SAT_DIR, "main.py")

sys.path.insert(0, PROGRAMS_DIR)
sys.path.insert(0, SAT_DIR)

import api  # noqa: E402
import helpers  # noqa: E402
from helpers import CommandError  # noqa: E402
from commands import REGISTRY  # noqa: E402

passed = 0
failed = 0


def check(cond, name, detail=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  ok   {name}")
    else:
        failed += 1
        print(f"  FAIL {name} {detail}")


class FakeMisskey:
    """api._transport の差し替え。エンドポイント単位で応答を登録する。

    _transport の戻り値は (status, retry_after_seconds, body) の3要素。
    """

    def __init__(self):
        self.calls = []
        self.routes = []

    def on(self, path, status=200, body=None, retry_after=None, times=None):
        self.routes.append({
            "path": path, "status": status, "body": body,
            "retry_after": retry_after, "times": times,
        })
        return self

    def raises(self, exc):
        self._raise = exc
        return self

    def __call__(self, path, body=None, token=None, timeout=15):
        self.calls.append({"path": path, "body": body, "token": token})
        if getattr(self, "_raise", None):
            raise self._raise
        for r in self.routes:
            if r["path"] != path:
                continue
            if r["times"] is not None:
                if r["times"] <= 0:
                    continue
                r["times"] -= 1
            return r["status"], r["retry_after"], r["body"]
        raise AssertionError(f"unexpected call: {path}")

    def paths(self):
        return [c["path"] for c in self.calls]

    def body_of(self, path):
        for c in self.calls:
            if c["path"] == path:
                return c["body"]
        return None


_sleeps = []


def install(fake):
    api._transport = fake
    _sleeps.clear()
    api.time.sleep = lambda s: _sleeps.append(s)
    return fake


def note(nid="9aaa", text="こんばんは", user="yuzuki", host=None,
         created="2026-08-26T12:04:00.000Z", **extra):
    n = {
        "id": nid,
        "text": text,
        "createdAt": created,
        "user": {"id": "u1", "username": user, "host": host},
    }
    n.update(extra)
    return n


# ---------------------------------------------------------------- 基本の疎通

def test_help():
    print("[help: 網羅]")
    out = REGISTRY["help"]({})
    listed = {c["command"] for c in out["commands"]}
    missing = set(REGISTRY) - listed - {"help"}
    check(not missing, "REGISTRY の全コマンドが help に載っている", f"漏れ: {missing}")
    check(all("example" in c for c in out["commands"]),
          "全コマンドに実行例が付いている")
    check("{{t:" not in json.dumps(out, ensure_ascii=False),
          "help に未解決の i18n キーが残らない")


def test_timeline_kinds():
    print("[timeline: 種類ごとのエンドポイント]")
    expect = {
        "home": "notes/timeline",
        "local": "notes/local-timeline",
        "social": "notes/hybrid-timeline",
        "global": "notes/global-timeline",
    }
    for kind, endpoint in expect.items():
        f = install(FakeMisskey().on(endpoint, 200, [note()]))
        REGISTRY["timeline"]({"kind": kind})
        check(f.paths() == [endpoint], f"kind={kind} は {endpoint} を呼ぶ", f.paths())

    install(FakeMisskey().on("notes/timeline", 200, []))
    out = REGISTRY["timeline"]({})
    check(out["kind"] == "home", "kind 省略時は home")

    try:
        REGISTRY["timeline"]({"kind": "nowhere"})
        check(False, "未知の kind はエラー")
    except CommandError as e:
        check("home" in (e.hint or ""), "未知の kind は使える値を hint に出す")


def test_timeline_summary_and_paging():
    print("[timeline: 要約と続き読み]")
    notes = [
        note("9a", "みじかい", reactions={"👍": 3, ":igyo:": 1}, myReaction="👍"),
        note("9b", "あ" * 500),
    ]
    install(FakeMisskey().on("notes/timeline", 200, notes))
    out = REGISTRY["timeline"]({"limit": 2})

    first, second = out["notes"]
    check(first["user"] == "@yuzuki", "ローカルユーザーは @名前")
    check(first["reactions"] == {"👍": 3, ":igyo:": 1}, "リアクションを集計して渡す")
    check(first["my_reaction"] == "👍", "自分が付けたリアクションが分かる")
    check(second.get("long") is True, "長い本文は long: true で知らせる")
    check(len(second["text"]) < 500, "長い本文は切り詰める")
    check(out["next_until_id"] == "9b", "最後の要素のIDが続き読みカーソルになる")
    check("until_id" in (out.get("hint") or ""), "続きの読み方を hint で伝える")

    f = install(FakeMisskey().on("notes/timeline", 200, []))
    out = REGISTRY["timeline"]({"until_id": "9b"})
    check(f.body_of("notes/timeline")["untilId"] == "9b", "until_id を untilId で送る")
    check(out["next_until_id"] is None, "空の結果ではカーソルを出さない")


def test_remote_user_handle():
    print("[要約: リモートユーザー表記]")
    install(FakeMisskey().on("notes/timeline", 200,
                             [note(user="alice", host="example.com")]))
    out = REGISTRY["timeline"]({})
    check(out["notes"][0]["user"] == "@alice@example.com",
          "リモートユーザーは host まで付ける（同名の別人を区別するため）")


def test_jst_conversion():
    print("[要約: 日時のJST変換]")
    install(FakeMisskey().on("notes/timeline", 200,
                             [note(created="2026-08-26T12:04:00.000Z")]))
    out = REGISTRY["timeline"]({})
    check(out["notes"][0]["at"] == "2026-08-26T21:04:00+09:00",
          "UTCのcreatedAtをJSTのISO 8601に直す", out["notes"][0]["at"])
    check(helpers.to_jst_iso("こわれた値") == "こわれた値",
          "解析できない日時はそのまま返す（表示のために落ちない）")


def test_renote_summary():
    print("[要約: Renote と引用]")
    original = note("9orig", "元ノート本文", user="alice", host="example.com")

    install(FakeMisskey().on("notes/timeline", 200,
                             [note("9re", None, renote=original)]))
    out = REGISTRY["timeline"]({})["notes"][0]
    check(out["text"] is None and out["renote_of"]["text"] == "元ノート本文",
          "純Renoteは中身を renote_of に入れる（空投稿に見せない）")
    check(out["renote_of"]["user"] == "@alice@example.com",
          "Renote元の投稿者が分かる")

    install(FakeMisskey().on("notes/timeline", 200,
                             [note("9q", "これは面白い", renote=original)]))
    out = REGISTRY["timeline"]({})["notes"][0]
    check(out["text"] == "これは面白い" and out["renote_of"]["text"] == "元ノート本文",
          "引用は外側=コメント / renote_of=引用元")


def test_cw_hidden_in_list_shown_in_note():
    print("[要約: CW（閲覧注意）]")
    cw_note = note("9cw", "犯人は執事", cw="ミステリの結末")
    install(FakeMisskey().on("notes/timeline", 200, [cw_note]))
    listed = REGISTRY["timeline"]({})["notes"][0]
    check(listed["text"] is None and listed["has_hidden_text"] is True,
          "一覧ではCW本文を伏せる（作者がワンクッション置いた意図を尊重）")
    check(listed["cw"] == "ミステリの結末", "CWの見出しは見せる")

    install(FakeMisskey()
            .on("notes/show", 200, cw_note)
            .on("notes/conversation", 200, [])
            .on("notes/replies", 200, []))
    opened = REGISTRY["note"]({"note_id": "9cw"})["note"]
    check(opened["text"] == "犯人は執事",
          "名指しで開いたノートは本文まで読める（読めなくなるわけではない）")


def test_note_thread():
    print("[note: 祖先と返信]")
    f = install(FakeMisskey()
                .on("notes/show", 200, note("9x", "本題"))
                .on("notes/conversation", 200, [note("9p", "その前の話")])
                .on("notes/replies", 200, [note("9r", "なるほど")]))
    out = REGISTRY["note"]({"note_id": "9x"})
    check(sorted(f.paths()) == ["notes/conversation", "notes/replies", "notes/show"],
          "本体・祖先・返信の3つをまとめて取る")
    check(out["ancestors"][0]["text"] == "その前の話",
          "ancestors は返信元の連鎖（conversation の実体）")
    check(out["replies"][0]["text"] == "なるほど", "replies はこのノートへの返信")
    check("返信ではありません" in out["ancestors_note"],
          "ancestors が返信一覧ではないことを明示する")

    try:
        REGISTRY["note"]({})
        check(False, "note_id なしはエラー")
    except CommandError as e:
        check(e.example.get("note_id"), "note_id なしは正しい呼び出し例を返す")


def test_note_without_text_or_files():
    print("[要約: 本文も添付も無いノート]")
    install(FakeMisskey().on("notes/timeline", 200, [
        {"id": "9e", "user": {"username": "alice"}, "createdAt": None},
        note("9f", None, files=[{"type": "image/jpeg", "url": "https://x/i.jpg",
                                "name": "i.jpg"}]),
    ]))
    out = REGISTRY["timeline"]({})
    check(out["count"] == 2, "欠損だらけのノートでも落ちない")
    check(out["notes"][1]["files"][0]["type"] == "image/jpeg",
          "添付は type と url だけ渡す（中身は see_image で見る）")


# ---------------------------------------------------------------- 通知

def test_response_budget():
    print("[出力量: 一覧が大きいとき]")
    heavy = [note(f"9n{i:03d}", "あ" * 500) for i in range(100)]
    install(FakeMisskey().on("notes/timeline", 200, heavy))
    out = REGISTRY["timeline"]({"limit": 100})
    rendered = json.dumps(out, ensure_ascii=False)

    check(isinstance(out.get("notes"), list) and out["notes"],
          "大量でも notes 配列が構造のまま残る（塊に潰れない）")
    check("_truncated" not in rendered,
          "main.py の非常用切り詰め（構造ごと文字列化）を発動させない")
    check(out.get("dropped", 0) > 0, "省いた件数を伝える")
    check(out["count"] == len(out["notes"]), "count は実際に返した件数と一致する")
    check(out["count"] + out["dropped"] == 100, "省いた件数と合わせて要求数になる")
    check(len(rendered) <= helpers.RESPONSE_BUDGET_CHARS + 1000,
          f"1回の出力が予算内に収まる（{len(rendered)}文字）")
    check(out["next_until_id"] == out["notes"][-1]["id"],
          "カーソルは「残した最後」を指す（省いた分を読み飛ばさない）")
    check("{{t:" not in rendered, "省略メッセージの i18n キーが解決されている")

    # 通常の読み方では発動しない
    install(FakeMisskey().on("notes/timeline", 200,
                             [note(f"9m{i}", "ふつうの長さの投稿です") for i in range(10)]))
    out = REGISTRY["timeline"]({})
    check("dropped" not in out, "既定 limit=10 の通常利用では省略が起きない")


def test_nested_note_is_brief():
    print("[出力量: 入れ子は短く]")
    fat = note("9orig", "い" * 900,
               files=[{"type": "image/png", "url": "https://x/" + "u" * 120,
                       "name": "a.png"} for _ in range(3)],
               reactions={"👍": 5}, repliesCount=4)
    install(FakeMisskey().on("notes/timeline", 200, [note("9re", None, renote=fat)]))
    inner = REGISTRY["timeline"]({})["notes"][0]["renote_of"]

    check(len(inner["text"]) <= helpers.NESTED_TEXT_CLIP + 1,
          f"入れ子の本文は{helpers.NESTED_TEXT_CLIP}文字に抑える", len(inner["text"]))
    check("files" not in inner and inner.get("files_count") == 3,
          "入れ子の添付はURLを載せず件数だけにする（1件約150文字あるため）")
    check("reactions" not in inner and "counts" not in inner,
          "入れ子にリアクションや件数は載せない（主役ではない）")
    check(inner["user"] and inner["at"] and inner["id"],
          "誰がいつ何と言ったかは残す")

    # 主役のノートは従来どおりURLまで載る（see_image で見られる必要がある）
    install(FakeMisskey().on("notes/timeline", 200, [fat]))
    main_note = REGISTRY["timeline"]({})["notes"][0]
    check(main_note["files"][0]["url"].startswith("https://"),
          "主役のノートの添付はURLを載せる")


def test_reactions_capped():
    print("[出力量: リアクションの種類数]")
    many = {f":emoji_{i}@remote.example:": (30 - i) for i in range(12)}
    install(FakeMisskey().on("notes/timeline", 200, [note("9r", "人気", reactions=many)]))
    out = REGISTRY["timeline"]({})["notes"][0]

    check(len(out["reactions"]) == helpers.MAX_REACTION_KINDS,
          f"リアクションは{helpers.MAX_REACTION_KINDS}種で打ち切る")
    check(out["reactions_more"] == 12 - helpers.MAX_REACTION_KINDS,
          "打ち切った種類数を伝える")
    check(max(out["reactions"].values()) == 30,
          "多い順に残す（何が優勢かは分かる）")

    install(FakeMisskey().on("notes/timeline", 200,
                             [note("9s", "ふつう", reactions={"👍": 2})]))
    out = REGISTRY["timeline"]({})["notes"][0]
    check("reactions_more" not in out, "少ないときは余計なキーを足さない")


def test_thread_budget():
    print("[出力量: note のスレッド]")
    heavy = [note(f"9a{i}", "う" * 400) for i in range(10)]
    install(FakeMisskey()
            .on("notes/show", 200, note("9x", "本題"))
            .on("notes/conversation", 200, heavy)
            .on("notes/replies", 200, heavy))
    out = REGISTRY["note"]({"note_id": "9x"})
    rendered = json.dumps(out, ensure_ascii=False)
    check(out["note"]["text"] == "本題", "本体は必ず残る")
    check(len(rendered) < 16000, f"スレッド全体が塊化の閾値未満に収まる（{len(rendered)}文字）")
    if out.get("dropped"):
        check(bool(out.get("dropped_note")), "省いたときは理由と代替手段を伝える")
    else:
        check(True, "予算内なら省略しない")


def test_notifications_does_not_mark_read():
    print("[notifications: 暗黙の既読化をしない]")
    items = [{"id": "n1", "type": "reaction", "reaction": "👍",
              "createdAt": "2026-08-26T12:00:00.000Z",
              "user": {"username": "alice", "host": "example.com"},
              "note": note("9a", "対象ノート")}]
    f = install(FakeMisskey().on("i/notifications", 200, items))
    out = REGISTRY["notifications"]({})
    sent = f.body_of("i/notifications")
    check(sent["markAsRead"] is False,
          "既定で markAsRead: false を明示送信する（Misskey側の既定は true）")
    check(out["marked_as_read"] is False, "既読にしていないことを結果で伝える")
    check(out["notifications"][0]["user"] == "@alice@example.com", "誰からかが分かる")
    check(out["notifications"][0]["reaction"] == "👍", "何のリアクションかが分かる")
    check(out["next_until_id"] == "n1", "通知も続き読みできる")

    f = install(FakeMisskey().on("i/notifications", 200, items))
    out = REGISTRY["notifications"]({"mark_as_read": True})
    check(f.body_of("i/notifications")["markAsRead"] is True,
          "明示されたときだけ既読にする")
    check(out["marked_as_read"] is True, "既読にしたことを結果で伝える")


# ---------------------------------------------------------------- ユーザー解決

def test_user_resolution():
    print("[user: 解決規則]")
    f = install(FakeMisskey().on("users/show", 200, {"id": "U1", "username": "alice"})
                .on("users/notes", 200, []))
    REGISTRY["user_notes"]({"user": "@alice"})
    check(f.body_of("users/show") == {"username": "alice", "host": None},
          "@名前 はローカル（host=None）として解決する")
    check(f.body_of("users/notes")["userId"] == "U1",
          "解決した userId で users/notes を呼ぶ")

    f = install(FakeMisskey().on("users/show", 200, {"id": "U2"})
                .on("users/notes", 200, []))
    REGISTRY["user_notes"]({"user": "@bob@remote.example"})
    check(f.body_of("users/show") == {"username": "bob", "host": "remote.example"},
          "@名前@ホスト はリモートとして解決する")

    f = install(FakeMisskey().on("users/notes", 200, []))
    REGISTRY["user_notes"]({"user": "9userid"})
    check("users/show" not in f.paths(),
          "userId 直指定では users/show を呼ばない（余計な往復をしない）")
    check(f.body_of("users/notes")["userId"] == "9userid", "IDをそのまま使う")

    f = install(FakeMisskey().on("users/show", 200, {"id": "U3"})
                .on("following/create", 204, None))
    out = REGISTRY["follow"]({"user": "@carol"})
    check(f.body_of("following/create") == {"userId": "U3"},
          "follow は解決後の userId を following/create に渡す")
    check(out["followed"] is True, "フォロー成功を返す（204でも落ちない）")

    f = install(FakeMisskey().on("users/show", 200, {"id": "U3"})
                .on("following/delete", 204, None))
    check(REGISTRY["unfollow"]({"user": "@carol"})["unfollowed"] is True,
          "unfollow も同じ経路")

    install(FakeMisskey().on("users/show", 200, {}))
    try:
        REGISTRY["user_notes"]({"user": "@nobody"})
        check(False, "解決できないユーザーはエラー")
    except CommandError as e:
        check("@名前@ホスト" in (e.hint or ""),
              "解決失敗時はリモート指定の書き方を hint に出す")

    try:
        REGISTRY["follow"]({})
        check(False, "user なしはエラー")
    except CommandError as e:
        check(e.example.get("user"), "user なしは正しい呼び出し例を返す")


def test_profile():
    print("[profile]")
    me = {"id": "me", "username": "yuzuki", "name": "柚月",
          "notesCount": 141, "followersCount": 5, "followingCount": 7,
          "createdAt": "2026-02-06T00:00:00.000Z"}
    f = install(FakeMisskey().on("i", 200, me))
    out = REGISTRY["profile"]({})
    check(f.paths() == ["i"], "user 省略時は自分（i）を見る")
    check(out["is_me"] is True and out["profile"]["handle"] == "@yuzuki",
          "自分のプロフィールだと分かる")
    check(out["profile"]["counts"]["notes"] == 141, "投稿数などの数え上げを渡す")
    check(out["profile"]["url"].endswith("/@yuzuki"), "プロフィールのURLを添える")

    f = install(FakeMisskey().on("users/show", 200,
                                 {"id": "U9", "username": "alice",
                                  "host": "example.com", "isFollowing": True}))
    out = REGISTRY["profile"]({"user": "@alice@example.com"})
    check(out["profile"]["i_follow_them"] is True, "フォロー関係が分かる")
    check(out["profile"]["url"].endswith("/@alice@example.com"),
          "リモートユーザーのURLにも host を付ける")


# ---------------------------------------------------------------- 投稿

def test_post_kinds():
    print("[post: 投稿・返信・Renote・引用]")
    created = {"createdNote": note("9new", "書いた")}

    f = install(FakeMisskey().on("notes/create", 200, created))
    out = REGISTRY["post"]({"text": "書いた"})
    check(f.body_of("notes/create") == {"visibility": "public", "text": "書いた"},
          "既定は public、余計なキーを送らない", f.body_of("notes/create"))
    check(out["kind"] == "note" and out["posted"] is True, "通常の投稿")
    check(out["url"].endswith("/notes/9new"), "投稿のURLを返す")

    f = install(FakeMisskey().on("notes/create", 200, created))
    REGISTRY["post"]({"text": "返事", "reply_id": "9x"})
    check(f.body_of("notes/create")["replyId"] == "9x", "reply_id は replyId で送る")

    f = install(FakeMisskey().on("notes/create", 200, created))
    out = REGISTRY["post"]({"renote_id": "9x"})
    body = f.body_of("notes/create")
    check(body["renoteId"] == "9x" and "text" not in body,
          "純Renoteは text キー自体を送らない")
    check(out["kind"] == "renote", "Renoteだと分かる")

    f = install(FakeMisskey().on("notes/create", 200, created))
    out = REGISTRY["post"]({"renote_id": "9x", "text": "これは良い"})
    check(f.body_of("notes/create")["text"] == "これは良い", "引用は text も送る")
    check(out["kind"] == "quote", "引用だと分かる")

    f = install(FakeMisskey().on("notes/create", 200, created))
    REGISTRY["post"]({"text": "内緒", "visibility": "followers", "cw": "小声"})
    body = f.body_of("notes/create")
    check(body["visibility"] == "followers" and body["cw"] == "小声",
          "visibility と cw を送る")


def test_post_validation():
    print("[post: 送信前バリデーション]")
    install(FakeMisskey())  # 通信に到達したら unexpected call で落ちる

    cases = [
        ({}, "本文もrenote_idも無い"),
        ({"text": "   "}, "空白だけの本文"),
        ({"text": ""}, "空文字の本文"),
        ({"text": "あ" * 3001}, "3000字超"),
        ({"renote_id": "9x", "text": ""}, "引用のつもりで中身が空"),
        ({"text": "ok", "cw": ""}, "cwが空文字"),
        ({"text": "ok", "cw": "あ" * 101}, "cwが100字超"),
        ({"text": "ok", "visibility": "specified"}, "使えないvisibility"),
    ]
    for args, label in cases:
        try:
            REGISTRY["post"](args)
            check(False, f"{label} は送信前に弾く")
        except CommandError as e:
            check(bool(e.hint), f"{label} は hint 付きで弾く")

    ok = install(FakeMisskey().on("notes/create", 200,
                                  {"createdNote": note("9n", "あ" * 3000)}))
    REGISTRY["post"]({"text": "あ" * 3000})
    check(len(ok.calls) == 1, "ちょうど3000字は通す")


def test_react_and_delete():
    print("[react / delete: 204 空ボディ]")
    f = install(FakeMisskey().on("notes/reactions/create", 204, None))
    out = REGISTRY["react"]({"note_id": "9a", "reaction": "👍"})
    check(out["reacted"] is True, "204（空ボディ）でも成功として扱う")
    check(f.body_of("notes/reactions/create") == {"noteId": "9a", "reaction": "👍"},
          "noteId と reaction を送る")

    f = install(FakeMisskey().on("notes/delete", 204, None))
    check(REGISTRY["delete"]({"note_id": "9a"})["deleted"] is True,
          "delete も204で成功")

    install(FakeMisskey())
    for args, label in (({"note_id": "9a"}, "reaction なし"),
                        ({"reaction": "👍"}, "note_id なし")):
        try:
            REGISTRY["react"](args)
            check(False, f"react: {label} はエラー")
        except CommandError as e:
            check(bool(e.example), f"react: {label} は実行例付きで返す")

    try:
        REGISTRY["delete"]({})
        check(False, "delete: note_id なしはエラー")
    except CommandError as e:
        check(bool(e.hint), "delete: note_id なしは hint 付き")


def test_limit_validation():
    print("[limit: 範囲チェック]")
    install(FakeMisskey())
    for bad in (0, 101, "ten", -1):
        try:
            REGISTRY["timeline"]({"limit": bad})
            check(False, f"limit={bad} は弾く")
        except CommandError:
            check(True, f"limit={bad} は弾く")

    f = install(FakeMisskey().on("notes/timeline", 200, []))
    REGISTRY["timeline"]({"limit": 100})
    check(f.body_of("notes/timeline")["limit"] == 100, "limit=100 は通す")

    f = install(FakeMisskey().on("notes/timeline", 200, []))
    REGISTRY["timeline"]({})
    check(f.body_of("notes/timeline")["limit"] == 10, "limit 省略時は10")


def test_raw_passthrough():
    print("[raw: 生応答]")
    raw_notes = [note("9a", "なま")]
    install(FakeMisskey().on("notes/timeline", 200, raw_notes))
    out = REGISTRY["timeline"]({"raw": True})
    check(out["raw"]["notes/timeline"] == raw_notes, "timeline の生応答をそのまま返す")

    install(FakeMisskey()
            .on("notes/show", 200, note("9x"))
            .on("notes/conversation", 200, [])
            .on("notes/replies", 200, []))
    out = REGISTRY["note"]({"note_id": "9x", "raw": True})
    check(set(out["raw"]) == {"notes/show", "notes/conversation", "notes/replies"},
          "複数APIを呼ぶ note はエンドポイント名ごとに格納する")

    install(FakeMisskey().on("i/notifications", 200, []))
    check("raw" in REGISTRY["notifications"]({"raw": True}), "notifications も raw 対応")
    install(FakeMisskey().on("i", 200, {"id": "me"}))
    check("raw" in REGISTRY["profile"]({"raw": True}), "profile も raw 対応")


# ---------------------------------------------------------------- transport

def test_url_building():
    print("[transport: URLの組み立て]")
    check(api.build_url("notes/create") == "https://misskey.io/api/notes/create",
          "既定は {base}/api/{path}")
    os.environ["CG_MISSKEY_BASE_URL"] = "https://example.social/"
    check(api.build_url("notes/create") == "https://example.social/api/notes/create",
          "base 末尾の / があっても二重スラッシュにならない")
    os.environ["CG_MISSKEY_BASE_URL"] = "https://example.social"
    check(api.build_url("/notes/create") == "https://example.social/api/notes/create",
          "path 先頭の / があっても壊れない")
    os.environ.pop("CG_MISSKEY_BASE_URL", None)


def test_rate_limit_retry():
    print("[transport: 429 の再試行]")
    f = install(FakeMisskey()
                .on("notes/timeline", 429, {"error": {"code": "RATE_LIMIT_EXCEEDED"}},
                    retry_after=1, times=1)
                .on("notes/timeline", 200, []))
    REGISTRY["timeline"]({})
    check(len(f.calls) == 2, "Retry-After が短ければ1回だけ再試行する")
    check(_sleeps and _sleeps[0] < 2, "Retry-After の秒数だけ待つ", _sleeps)

    f = install(FakeMisskey().on("notes/timeline", 429, {"error": {}}, retry_after=1842))
    try:
        REGISTRY["timeline"]({})
        check(False, "長い Retry-After はエラーにする")
    except api.APIError as e:
        check(len(f.calls) == 1, "長い Retry-After では再試行しない（timeoutに殺される）")
        check(not _sleeps, "長い Retry-After では待たない")
        check(e.retry_after == 1842, "いつ再実行すればよいかを retry_after で伝える")
        check("1842" in (e.hint or ""), "待ち秒数を hint に出す")

    f = install(FakeMisskey().on("notes/timeline", 429, {"error": {}}))
    try:
        REGISTRY["timeline"]({})
        check(False, "Retry-After 欠落もエラー")
    except api.APIError as e:
        check(len(f.calls) == 1, "Retry-After が無ければ再試行しない")
        check(e.retry_after is None and bool(e.hint), "待ち時間不明でも hint は出す")


def test_no_retry_on_other_failures():
    print("[transport: 書き込みを再送しない]")
    created = {"createdNote": note()}
    for status, label in ((500, "5xx"), (400, "4xx"), (403, "403")):
        f = install(FakeMisskey().on("notes/create", status, {"error": {"code": "E"}}))
        try:
            REGISTRY["post"]({"text": "だいじな一言"})
        except api.APIError:
            pass
        check(len(f.calls) == 1, f"{label} では post を再送しない（二重投稿を防ぐ）")

    f = install(FakeMisskey())
    f.raises(api.APIError(0, {"error": "timeout"}))
    try:
        REGISTRY["post"]({"text": "だいじな一言"})
    except api.APIError:
        pass
    check(len(f.calls) == 1, "タイムアウトでも post を再送しない")


def test_error_body_shapes():
    print("[transport: エラーボディの整形]")
    install(FakeMisskey().on("notes/timeline", 404,
                             {"error": {"code": "NO_SUCH_NOTE", "message": "見つからない"}}))
    try:
        REGISTRY["timeline"]({})
        check(False, "404 はエラー")
    except api.APIError as e:
        check("NO_SUCH_NOTE" in str(e), "Misskey のエラーコードを出す")
        check(bool(e.hint), "404 には次の一手のヒントを付ける")

    install(FakeMisskey().on("notes/timeline", 502, {"message": "<html>Bad Gateway"}))
    try:
        REGISTRY["timeline"]({})
        check(False, "502 はエラー")
    except api.APIError as e:
        check("502" in str(e), "JSONでないエラーボディでも整形して返す")

    check(api._parse_body("", "application/json") is None, "空ボディは None")
    check(api._parse_body(None) is None, "本文なしは None")
    check(api._parse_body("not json", "text/html")["message"] == "not json",
          "JSONでない本文は文字列として扱う")


def test_no_token():
    print("[transport: トークン未設定]")
    os.environ["CG_MISSKEY_TOKEN"] = ""
    try:
        api.request("i", {})
        check(False, "トークン未設定はエラー")
    except api.APIError as e:
        check("CG_MISSKEY_TOKEN" in (e.hint or ""),
              "登録すべき環境変数名を伝える")
    os.environ["CG_MISSKEY_TOKEN"] = FAKE_TOKEN


def test_token_never_leaks():
    print("[トークン非漏洩]")
    # 1. リクエストのボディにトークンが合流するのは _transport の中だけ。
    #    コマンド層が組むボディにトークンは入らない（ログや例外に載る面を最小化する）
    f = install(FakeMisskey().on("notes/create", 200, {"createdNote": note()}))
    REGISTRY["post"]({"text": "やあ"})
    bodies = json.dumps([c["body"] for c in f.calls], ensure_ascii=False)
    check(FAKE_TOKEN not in bodies and '"i"' not in bodies,
          "コマンド層はトークン入りのボディを組み立てない", bodies)

    # 2. 各種エラー経路の文字列表現に出ない
    leaked = []
    for status, body in ((401, {"error": {"code": "AUTH", "message": FAKE_TOKEN}}),
                         (500, {"error": {}}),
                         (429, {"error": {}})):
        install(FakeMisskey().on("i", status, body))
        try:
            api.request("i", {"i": FAKE_TOKEN})
        except api.APIError as e:
            for rendered in (str(e), repr(e), json.dumps(e.body, ensure_ascii=False)):
                if FAKE_TOKEN in rendered:
                    leaked.append((status, rendered[:80]))
    check(any(leaked), "サーバが返した本文にトークンが混じる経路は実在する（要遮断）")

    # 3. main.py の出力段でトークンを伏せる（最後の砦）
    p, out = _run_cli({"command": "profile"},
                      env_extra={"CG_MISSKEY_BASE_URL": "http://127.0.0.1:9"})
    check(FAKE_TOKEN not in (p.stdout or ""), "実行結果にトークンが出ない")
    check(FAKE_TOKEN not in (p.stderr or ""), "stderr にもトークンが出ない")


# ---------------------------------------------------------------- 宣言と辞書

def _source_files():
    files = [MAIN_PY]
    for root, _dirs, names in os.walk(SAT_DIR):
        if "__pycache__" in root:
            continue
        for n in names:
            if n.endswith(".py"):
                path = os.path.join(root, n)
                if path not in files:
                    files.append(path)
    return files


def test_manifest_declares_every_arg():
    print("[manifest: 引数の宣言漏れ]")
    import yaml
    with open(os.path.join(SAT_DIR, "manifest.yaml"), encoding="utf-8") as f:
        manifest = yaml.safe_load(f)
    declared = {a["name"] for a in manifest.get("args", [])}

    used = set()
    for path in _source_files():
        src = open(path, encoding="utf-8").read()
        used |= set(re.findall(r'args\.get\(\s*"([a-z_]+)"', src))
        used |= set(re.findall(r'args\[\s*"([a-z_]+)"\s*\]', src))

    missing = used - declared
    check(not missing,
          "コードが読む引数はすべて manifest に宣言されている"
          "（宣言漏れは _run_program が呼び出しごと弾く）", f"未宣言: {sorted(missing)}")

    unused = declared - used
    check(not unused, "manifest に使われていない引数が無い", f"未使用: {sorted(unused)}")
    check(manifest.get("timeout", 0) >= 40,
          "429の待ち(最大10秒)＋通信2回が収まる timeout になっている")
    check("tool" not in manifest,
          "v1 は第一級ツール昇格をしない（昇格するとサーバ再起動が必要になる）")


def test_i18n_keys_exist():
    print("[i18n: キーの存在]")
    lang_dir = os.path.join(PROGRAMS_DIR, "_lang")
    ja = json.load(open(os.path.join(lang_dir, "ja.json"), encoding="utf-8"))
    en = json.load(open(os.path.join(lang_dir, "en.json"), encoding="utf-8"))

    used = set()
    for path in _source_files():
        src = open(path, encoding="utf-8").read()
        used |= set(re.findall(r't\(\s*"(msat_[a-z0-9_]+)"', src))
    with open(os.path.join(SAT_DIR, "manifest.yaml"), encoding="utf-8") as f:
        used |= set(re.findall(r"\{\{t:(msat_[a-z0-9_]+)\}\}", f.read()))

    check(len(used) > 50, f"msat_ キーを十分に使っている（{len(used)}個）")
    check(not (used - set(ja)), "使っている msat_ キーが ja.json に揃っている",
          f"欠落: {sorted(used - set(ja))[:5]}")
    check(not (used - set(en)), "使っている msat_ キーが en.json に揃っている",
          f"欠落: {sorted(used - set(en))[:5]}")

    ja_msat = {k for k in ja if k.startswith("msat_")}
    check(ja_msat == {k for k in en if k.startswith("msat_")},
          "ja と en で msat_ キー集合が一致する")
    check(not (ja_msat - used), "辞書に使われていない msat_ キーが無い",
          f"未使用: {sorted(ja_msat - used)[:5]}")


# ---------------------------------------------------------------- 実 subprocess

def _run_cli(args, env_extra=None):
    env = {
        "CG_WORKSPACE": TEST_DIR,
        "CG_LANG": "ja",
        "CG_MISSKEY_TOKEN": FAKE_TOKEN,
        "CG_PROJECT_ROOT": REPO_ROOT,
        "PYTHONPATH": PROGRAMS_DIR,
        "PYTHONIOENCODING": "utf-8",
        "SYSTEMROOT": os.environ.get("SYSTEMROOT", ""),
        "PATH": os.environ.get("PATH", ""),
    }
    if env_extra:
        env.update(env_extra)
    p = subprocess.run([sys.executable, MAIN_PY], input=json.dumps(args),
                       capture_output=True, text=True, encoding="utf-8", env=env)
    return p, (json.loads(p.stdout) if p.stdout.strip() else None)


def test_cli_help():
    print("[CLI: help]")
    p, out = _run_cli({})
    check(p.returncode == 0, "引数なしで正常終了する")
    check(out and out["status"] == "ok", "引数なしは help を返す")
    names = [c["command"] for c in out["data"]["commands"]]
    check("post" in names and "timeline" in names, "コマンド一覧を載せる")
    check("{{t:" not in json.dumps(out, ensure_ascii=False),
          "i18nキーが未解決で残らない（subprocess経路でも辞書が引けている）")


def test_cli_unknown_command():
    print("[CLI: 未知コマンド]")
    p, out = _run_cli({"command": "psot"})
    check(out["status"] == "error", "未知コマンドはエラー")
    # run_program は status / message / data しか柚月に見せないので data 経由で確認
    check("post" in (out.get("data") or {}).get("did_you_mean", []), "「もしかして」候補を出す")
    check("did_you_mean" not in out, "候補がトップレベルに残っていない")
    check(p.returncode == 0, "未知コマンドは異常終了ではなく通常のエラー応答")


def test_cli_no_token():
    print("[CLI: トークン未設定]")
    p, out = _run_cli({"command": "timeline"}, env_extra={"CG_MISSKEY_TOKEN": ""})
    check(out["status"] == "error", "トークン未設定はエラー")
    check("CG_MISSKEY_TOKEN" in ((out.get("data") or {}).get("hint") or ""),
          "登録すべき環境変数名を伝える")
    check(p.returncode == 0, "トークン未設定は異常終了ではなく通常のエラー応答")


def test_cli_validation_reaches_user():
    print("[CLI: バリデーションが柚月まで届く]")
    p, out = _run_cli({"command": "post"})
    check(out["status"] == "error", "本文なしの post はエラー")
    detail = out.get("data") or {}
    check(bool(detail.get("hint")) and bool(detail.get("example")),
          "hint と example が付いて返る（自己回復できる）")
    check(p.returncode == 0, "自分で直せるエラーで異常終了しない")


def main():
    print("=== misskey_satellite テスト ===\n")
    for fn in (
        test_help,
        test_timeline_kinds,
        test_timeline_summary_and_paging,
        test_remote_user_handle,
        test_jst_conversion,
        test_renote_summary,
        test_cw_hidden_in_list_shown_in_note,
        test_note_thread,
        test_note_without_text_or_files,
        test_response_budget,
        test_nested_note_is_brief,
        test_reactions_capped,
        test_thread_budget,
        test_notifications_does_not_mark_read,
        test_user_resolution,
        test_profile,
        test_post_kinds,
        test_post_validation,
        test_react_and_delete,
        test_limit_validation,
        test_raw_passthrough,
        test_url_building,
        test_rate_limit_retry,
        test_no_retry_on_other_failures,
        test_error_body_shapes,
        test_no_token,
        test_token_never_leaks,
        test_manifest_declares_every_arg,
        test_i18n_keys_exist,
        test_cli_help,
        test_cli_unknown_command,
        test_cli_no_token,
        test_cli_validation_reaches_user,
    ):
        fn()

    print(f"\n=== {passed} passed, {failed} failed ===")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
