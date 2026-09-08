"""
discord_satellite テスト

実ネットワークには一切出ない（api._transport を差し替えて検証する）。
実行: venv\\Scripts\\python.exe tests\\test_discord_satellite.py
（pytest不要・自前ランナー）
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

TEST_DIR = tempfile.mkdtemp(prefix="dsat_test_")
os.environ["CG_WORKSPACE"] = TEST_DIR
os.environ["CG_LANG"] = "ja"
os.environ["CG_DISCORD_BOT_TOKEN"] = "FAKE.TOKEN.VALUE-should-never-leak"

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SAT_DIR = os.path.join(REPO_ROOT, "programs", "discord_satellite")
PROGRAMS_DIR = os.path.dirname(SAT_DIR)
MAIN_PY = os.path.join(SAT_DIR, "main.py")

sys.path.insert(0, PROGRAMS_DIR)
sys.path.insert(0, SAT_DIR)

import api  # noqa: E402
import state  # noqa: E402
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


def reset_state():
    d = os.path.join(TEST_DIR, "program_data")
    if os.path.exists(d):
        shutil.rmtree(d)


class FakeDiscord:
    """api._transport の差し替え。ルート単位で応答を登録する。"""

    def __init__(self):
        self.calls = []
        self.routes = []

    def on(self, method, contains, status=200, body=None, times=None):
        self.routes.append({
            "method": method, "contains": contains,
            "status": status, "body": body if body is not None else {},
            "times": times,
        })
        return self

    def __call__(self, method, path, body=None, token=None, timeout=20):
        self.calls.append({"method": method, "path": path, "body": body, "token": token})
        for r in self.routes:
            if r["method"] != method or r["contains"] not in path:
                continue
            if r["times"] is not None:
                if r["times"] <= 0:
                    continue
                r["times"] -= 1
            return r["status"], r["body"]
        raise AssertionError(f"unexpected call: {method} {path}")

    def paths(self, method=None):
        return [c["path"] for c in self.calls if method is None or c["method"] == method]


def install(fake):
    api._transport = fake
    return fake


def msg(mid, content, author_id="999", author_name="ルナ", bot=True,
        mentions=None, ref_author=None, attachments=None):
    m = {
        "id": str(mid),
        "content": content,
        "author": {"id": author_id, "username": author_name,
                   "global_name": author_name, "bot": bot},
        "mentions": mentions or [],
    }
    if ref_author:
        m["referenced_message"] = {"id": "1", "author": {"id": ref_author,
                                                         "username": "柚月"}}
    if attachments:
        m["attachments"] = attachments
    return m


ME = "1000000000000000000"
CH_ZATSU = "111111111111111111"
CH_PAIR = "222222222222222222"
CH_FORUM = "444444444444444444"
TH_BOSHU = "555555555555555555"


def do_setup(fake=None, guilds=None, channels=None):
    """setup を通して状態ファイルを作る（他テストの前提づくり）。"""
    f = FakeDiscord()
    f.on("GET", "/users/@me/guilds", 200, guilds if guilds is not None else
         [{"id": "555", "name": "AI SNS"}])
    f.on("GET", "/users/@me", 200, {"id": ME, "username": "yuzuki",
                                    "global_name": "柚月", "bot": True})
    f.on("GET", "/guilds/555/channels", 200, channels if channels is not None else [
        {"id": CH_ZATSU, "name": "雑談", "type": 0},
        {"id": CH_PAIR, "name": "ペア登録", "type": 0},
        {"id": "333", "name": "音声", "type": 2},
        {"id": CH_FORUM, "name": "募集", "type": 15},
    ])
    f.on("GET", "/guilds/555/threads/active", 200, {"threads": [
        {"id": TH_BOSHU, "name": "AI SNS 参加者募集", "type": 11, "parent_id": CH_FORUM},
    ]})
    install(f)
    return REGISTRY["setup"]({}), f


# ---------------------------------------------------------------- tests

def test_setup_basic():
    print("[setup]")
    reset_state()
    out, f = do_setup()
    check(out["bot_user_id"] == ME, "bot_user_id を保存する")
    check(out["guild"]["guild_id"] == "555", "guild を確定する")
    names = [c["name"] for c in out["channels"]]
    check("雑談" in names and "ペア登録" in names, "テキストチャンネルを列挙する")
    check("音声" not in names, "ボイスチャンネル(type=2)は除外する")
    check("募集" not in names, "フォーラム本体(type=15)は列挙しない（投稿はスレとして出る）")
    th = next((c for c in out["channels"] if c["channel_id"] == TH_BOSHU), None)
    check(th is not None and th.get("thread_of") == "募集",
          "活動中スレッドを親チャンネル名つきで列挙する")
    check(out["name_keywords"] == ["柚月"], "表示名を名前呼びキーワードの既定にする")
    st = state.load_state()
    check(st.get("guild_id") == "555", "状態ファイルに永続化される")


def test_setup_preserves_cursor():
    print("[setup: 既読の保持]")
    reset_state()
    do_setup()
    st = state.load_state()
    st["channels"][CH_ZATSU]["last_read_id"] = "12345"
    st["channels"][CH_ZATSU]["watch"] = True
    state.save_state(st)

    do_setup()  # 再セットアップ
    st2 = state.load_state()
    check(st2["channels"][CH_ZATSU]["last_read_id"] == "12345",
          "再setupで既読カーソルが巻き戻らない")
    check(st2["channels"][CH_ZATSU]["watch"] is True, "再setupでwatch設定が保持される")


def test_setup_multi_guild():
    print("[setup: 複数サーバー]")
    reset_state()
    try:
        do_setup(guilds=[{"id": "555", "name": "A"}, {"id": "666", "name": "B"}])
        check(False, "複数サーバーならエラーにする")
    except CommandError as e:
        check(True, "複数サーバーならエラーにする")
        check(e.extra.get("guilds") and len(e.extra["guilds"]) == 2,
              "候補サーバー一覧を返す")
        check(e.example and "guild_id" in e.example, "再実行の例を返す")


def test_read_first_time():
    print("[read: 初回]")
    reset_state()
    do_setup()
    f = install(FakeDiscord().on("GET", "/messages", 200, [
        msg(200, "こんばんは"), msg(100, "はじめまして"),
    ]))
    out = REGISTRY["read"]({"channel": "雑談"})
    check("limit=10" in f.paths("GET")[0], "初回はカーソル無しで直近だけ取得する")
    check("after=" not in f.paths("GET")[0], "初回は after を付けない")
    ids = [m["id"] for m in out["channels"][0]["messages"]]
    check(ids == ["100", "200"], "古い順に並べ替える")
    st = state.load_state()
    check(st["channels"][CH_ZATSU]["last_read_id"] == "200",
          "最新IDまでカーソルを進める")


def test_read_after_cursor():
    print("[read: 差分]")
    reset_state()
    do_setup()
    st = state.load_state()
    st["channels"][CH_ZATSU].update({"last_read_id": "100", "watch": True})
    state.save_state(st)

    f = install(FakeDiscord().on("GET", "/messages", 200, [msg(300, "やあ")]))
    out = REGISTRY["read"]({})
    path = f.paths("GET")[0]
    check("after=100" in path, "前回位置から after で取得する")
    check(out["total_new"] == 1, "新着件数を返す")
    check(len(out["channels"]) == 1, "watch=true のチャンネルだけ巡回する")
    check(state.load_state()["channels"][CH_ZATSU]["last_read_id"] == "300",
          "カーソルが前進する")


def test_read_nothing_new():
    print("[read: 新着なし]")
    reset_state()
    do_setup()
    st = state.load_state()
    st["channels"][CH_ZATSU].update({"last_read_id": "100", "watch": True})
    state.save_state(st)
    install(FakeDiscord().on("GET", "/messages", 200, []))
    out = REGISTRY["read"]({})
    check(out["total_new"] == 0, "新着0件を返す")
    check("note" in out, "新着が無いことを明示する")
    check(state.load_state()["channels"][CH_ZATSU]["last_read_id"] == "100",
          "新着0件ならカーソルは動かない")


def test_read_mentions():
    print("[read: メンション判定]")
    reset_state()
    do_setup()
    install(FakeDiscord().on("GET", "/messages", 200, [
        msg(10, "みんな元気？"),
        msg(11, "<@%s> どう思う？" % ME, mentions=[{"id": ME, "username": "柚月"}]),
        msg(12, "柚月さんの話きいた？"),
        msg(13, "そうだね", ref_author=ME),
        msg(14, "自分の発言", author_id=ME, author_name="柚月"),
    ]))
    out = REGISTRY["read"]({"channel": CH_ZATSU})
    ms = {m["id"]: m for m in out["channels"][0]["messages"]}
    check(ms["11"]["mentions_me"] is True, "@メンションを検知する")
    check(ms["12"]["names_me"] is True, "@なしの名前呼びを検知する")
    check(ms["13"]["mentions_me"] is True, "自分の発言へのリプライを検知する")
    check(ms["14"]["is_me"] is True, "自分の発言を is_me で区別する")
    check(ms["14"]["mentions_me"] is False, "自分の発言はメンション扱いしない")
    check("@柚月" in ms["11"]["content"], "本文中のメンションIDを表示名に置換する")
    check(len(out["mentions_summary"]) == 3, "自分宛だけを要約に集約する")
    check(out["example"]["command"] == "reply", "返信の実行例を添える")


def test_read_peek():
    print("[read: peek]")
    reset_state()
    do_setup()
    install(FakeDiscord().on("GET", "/messages", 200, [msg(500, "やあ")]))
    REGISTRY["read"]({"channel": CH_ZATSU, "peek": True})
    check(state.load_state()["channels"][CH_ZATSU]["last_read_id"] is None,
          "peek=true ではカーソルを進めない")


def test_read_skips_forbidden():
    print("[read: 権限なしチャンネル]")
    reset_state()
    do_setup()
    st = state.load_state()
    st["channels"][CH_ZATSU]["watch"] = True
    st["channels"][CH_PAIR]["watch"] = True
    state.save_state(st)

    f = FakeDiscord()
    f.on("GET", f"/channels/{CH_ZATSU}/messages", 200, [msg(700, "やあ")])
    f.on("GET", f"/channels/{CH_PAIR}/messages", 403, {"message": "Missing Access"})
    install(f)
    out = REGISTRY["read"]({})
    check(out["total_new"] == 1, "読めるチャンネルの結果は返る")
    check(len(out.get("skipped_channels", [])) == 1, "読めないチャンネルは読み飛ばして報告する")


def test_read_truncated():
    print("[read: 取り切れない]")
    reset_state()
    do_setup()
    install(FakeDiscord().on("GET", "/messages", 200,
                             [msg(1000 + i, f"m{i}") for i in range(5)]))
    out = REGISTRY["read"]({"channel": CH_ZATSU, "limit": 5})
    check(out["channels"][0].get("truncated") is True, "上限到達を truncated で伝える")
    check("truncated_note" in out["channels"][0], "続きの読み方を添える")


def test_read_no_watch():
    print("[read: 巡回対象なし]")
    reset_state()
    do_setup()
    install(FakeDiscord())
    try:
        REGISTRY["read"]({})
        check(False, "watch未設定ならエラーにする")
    except CommandError as e:
        check(True, "watch未設定ならエラーにする")
        check(e.example and e.example.get("watch") is True, "watch設定の例を示す")
        check(bool(e.extra.get("available")), "選べるチャンネル一覧を返す")


def test_read_attachment_only():
    print("[read: 添付のみ]")
    reset_state()
    do_setup()
    install(FakeDiscord().on("GET", "/messages", 200, [
        msg(60, "", attachments=[{"id": "a"}]),
    ]))
    out = REGISTRY["read"]({"channel": CH_ZATSU})
    check("添付" in out["channels"][0]["messages"][0]["content"],
          "本文が無くても添付があることを伝える")


def test_post():
    print("[post]")
    reset_state()
    do_setup()
    f = install(FakeDiscord().on("POST", "/messages", 200, {"id": "9001"}))
    out = REGISTRY["post"]({"channel": "雑談", "message": "こんばんは"})
    body = f.calls[0]["body"]
    check(f.calls[0]["path"] == f"/channels/{CH_ZATSU}/messages",
          "チャンネル名からIDを解決して送る")
    check(body["allowed_mentions"].get("parse") == ["users"],
          "@everyone/ロールメンションを構造的に無効化する")
    check("message_reference" not in body, "postでは返信参照を付けない")
    check(out["message_id"] == "9001", "送信したメッセージIDを返す")


def test_post_too_long():
    print("[post: 長すぎる本文]")
    reset_state()
    do_setup()
    f = install(FakeDiscord())
    try:
        REGISTRY["post"]({"channel": "雑談", "message": "あ" * 2001})
        check(False, "2000文字超は送らずエラーにする")
    except CommandError as e:
        check(True, "2000文字超は送らずエラーにする")
        check(len(f.calls) == 0, "送信APIを呼ばない（勝手に分割しない）")
        check("分割" in (e.hint or ""), "分け方は自分で決める旨を伝える")


def test_post_missing_args():
    print("[post: 引数不足]")
    reset_state()
    do_setup()
    install(FakeDiscord())
    try:
        REGISTRY["post"]({"message": "やあ"})
        check(False, "channel未指定はエラー")
    except CommandError as e:
        check(bool(e.extra.get("available")), "channel未指定で候補一覧を返す")
    try:
        REGISTRY["post"]({"channel": "雑談", "message": "   "})
        check(False, "空本文はエラー")
    except CommandError as e:
        check(bool(e.example), "空本文で実行例を返す")


def test_reply():
    print("[reply]")
    reset_state()
    do_setup()
    f = install(FakeDiscord().on("POST", "/messages", 200, {"id": "9002"}))
    out = REGISTRY["reply"]({"channel": CH_ZATSU, "message_id": "8000",
                             "message": "うん"})
    ref = f.calls[0]["body"]["message_reference"]
    check(ref["message_id"] == "8000", "返信先IDを message_reference に入れる")
    check(ref["channel_id"] == CH_ZATSU, "返信先チャンネルを入れる")
    check(out["replied_to"] == "8000", "返信先を結果に含める")


def test_reply_gone():
    print("[reply: 返信先が消えている]")
    reset_state()
    do_setup()
    install(FakeDiscord().on("POST", "/messages", 400,
                             {"message": "Unknown message"}))
    try:
        REGISTRY["reply"]({"channel": CH_ZATSU, "message_id": "1", "message": "や"})
        check(False, "400を自己回復可能なエラーに変換する")
    except CommandError as e:
        check(True, "400を自己回復可能なエラーに変換する")
        check(e.example and e.example.get("command") == "read",
              "read で確認し直す例を示す")


def test_react():
    print("[react]")
    reset_state()
    do_setup()
    f = install(FakeDiscord().on("PUT", "/reactions/", 204, {}))
    REGISTRY["react"]({"channel": CH_ZATSU, "message_id": "8100", "emoji": "👋"})
    path = f.calls[0]["path"]
    check("%F0%9F%91%8B" in path, "絵文字をURLエンコードする")
    check(path.endswith("/@me"), "自分のリアクションとして送る")


def test_react_missing_args():
    print("[react: 引数不足]")
    reset_state()
    do_setup()
    install(FakeDiscord())
    try:
        REGISTRY["react"]({"channel": CH_ZATSU, "message_id": "1"})
        check(False, "emoji未指定はエラー")
    except CommandError as e:
        check(bool(e.example), "emoji未指定で実行例を返す")


def test_channels_watch():
    print("[channels]")
    reset_state()
    do_setup()
    install(FakeDiscord())
    out = REGISTRY["channels"]({"channel": "雑談", "watch": True})
    check(out["changed"]["watch"] is True, "watchの変更を報告する")
    check(state.load_state()["channels"][CH_ZATSU]["watch"] is True,
          "watch設定が永続化される")
    check(out["watching_count"] == 1, "巡回対象数を返す")

    try:
        REGISTRY["channels"]({"channel": "存在しない部屋", "watch": True})
        check(False, "未知チャンネルはエラー")
    except CommandError as e:
        check(bool(e.extra.get("available")), "未知チャンネルで候補一覧を返す")


def test_status():
    print("[status]")
    reset_state()
    do_setup()
    st = state.load_state()
    st["channels"][CH_ZATSU].update({"watch": True, "last_read_id": "175928847299117063"})
    state.save_state(st)
    install(FakeDiscord().on("GET", "/users/@me", 200, {"id": ME}))
    out = REGISTRY["status"]({})
    check(out["token_ok"] is True, "Token有効を確認する")
    check(out["watching_count"] == 1, "巡回対象を数える")
    check(out["watching"][0]["last_read_at"] is not None, "最後に読んだ時刻を出す")


def test_channel_resolution():
    print("[チャンネル解決]")
    channels = {CH_ZATSU: {"name": "雑談"}, CH_PAIR: {"name": "Pair-Register"}}
    check(helpers.resolve_channel("雑談", channels) == CH_ZATSU, "名前で解決する")
    check(helpers.resolve_channel("#雑談", channels) == CH_ZATSU, "先頭の#を無視する")
    check(helpers.resolve_channel("pair-register", channels) == CH_PAIR,
          "英名は大文字小文字を無視する")
    check(helpers.resolve_channel(CH_ZATSU, channels) == CH_ZATSU, "IDで解決する")
    check(helpers.resolve_channel("無い部屋", channels) is None, "未知は None")


def test_helpers():
    print("[ヘルパー]")
    check(helpers.snowflake_to_jst("175928847299117063") == "2016-04-30 20:18",
          "snowflakeからJST時刻を復元する")
    check(helpers.snowflake_to_jst("abc") is None, "壊れたIDでも落ちない")
    m = {"mentions": [{"id": "1", "global_name": "ルナ"}]}
    out = helpers.humanize_content("<@1> やあ <#%s> で <:wave:12>" % CH_ZATSU, m,
                                   {CH_ZATSU: {"name": "雑談"}})
    check("@ルナ" in out and "#雑談" in out and ":wave:" in out,
          "メンション・チャンネル・絵文字を読める形に直す")
    check(helpers.suggest_commands("raed", list(REGISTRY.keys())), "打ち間違いに候補を出す")
    check(helpers.parse_bool("true") and not helpers.parse_bool(""),
          "真偽値をゆるく解釈する")


def test_state_corrupt():
    print("[状態ファイル破損]")
    reset_state()
    os.makedirs(os.path.dirname(state.STATE_FILE), exist_ok=True)
    with open(state.STATE_FILE, "w", encoding="utf-8") as f:
        f.write("{ broken json")
    check(state.load_state() == {}, "壊れた状態ファイルでも落ちずに空を返す")


def test_rate_limit_retry():
    print("[レート制限]")
    reset_state()
    do_setup()
    f = FakeDiscord()
    f.on("GET", "/messages", 429, {"retry_after": 0.1}, times=1)
    f.on("GET", "/messages", 200, [msg(4000, "やあ")])
    install(f)
    out = REGISTRY["read"]({"channel": CH_ZATSU})
    check(len(f.calls) == 2, "429のあと1回だけ自動リトライする")
    check(out["total_new"] == 1, "リトライ後の結果を返す")


def test_auth_error_hint():
    print("[認証エラー]")
    reset_state()
    do_setup()
    install(FakeDiscord().on("GET", "/messages", 401, {"message": "401: Unauthorized"}))
    try:
        REGISTRY["read"]({"channel": CH_ZATSU})
        check(False, "401はAPIErrorになる")
    except api.APIError as e:
        check(e.status == 401, "401はAPIErrorになる")
        check("カノン" in (e.hint or ""), "柚月では直せない旨をヒントにする")


def test_token_never_leaks():
    print("[Token漏洩防止]")
    token = os.environ["CG_DISCORD_BOT_TOKEN"]
    reset_state()
    do_setup()
    install(FakeDiscord().on("GET", "/messages", 401,
                             {"message": "401: Unauthorized"}))
    try:
        REGISTRY["read"]({"channel": CH_ZATSU})
    except api.APIError as e:
        blob = json.dumps({"message": str(e), "hint": e.hint, "body": e.body},
                          ensure_ascii=False)
        check(token not in blob, "エラー出力にTokenが混入しない")


# ------------------------------------------------------- CLI（サブプロセス）

def _run_cli(args, env_extra=None):
    env = {
        "CG_WORKSPACE": TEST_DIR,
        "CG_LANG": "ja",
        "PYTHONIOENCODING": "utf-8",
        "PYTHONPATH": PROGRAMS_DIR,
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
    check(out and out["status"] == "ok", "引数なしはhelpを返す")
    names = [c["command"] for c in out["data"]["commands"]]
    check("read" in names and "post" in names, "コマンド一覧を載せる")
    check("{{t:" not in json.dumps(out, ensure_ascii=False), "i18nキーが未解決で残らない")


def test_cli_unknown_command():
    print("[CLI: 未知コマンド]")
    p, out = _run_cli({"command": "raed"})
    check(out["status"] == "error", "未知コマンドはエラー")
    # run_program は status / message / data しか柚月に見せないので data 経由で確認
    check("read" in (out.get("data") or {}).get("did_you_mean", []), "「もしかして」候補を出す")
    check("did_you_mean" not in out, "候補がトップレベルに残っていない")


def test_cli_no_token():
    print("[CLI: Token未設定]")
    p, out = _run_cli({"command": "read"}, env_extra={"CG_DISCORD_BOT_TOKEN": ""})
    check(out["status"] == "error", "Token未設定はエラー")
    check("CG_DISCORD_BOT_TOKEN" in ((out.get("data") or {}).get("hint") or ""),
          "登録すべき環境変数名を伝える")
    check(p.returncode == 0, "Token未設定は異常終了ではなく通常のエラー応答")


def test_reply_pings_author():
    print("[reply: 相手に通知が飛ぶ]")
    reset_state()
    do_setup()
    f = install(FakeDiscord().on("POST", "/messages", 200, {"id": "9100"}))
    REGISTRY["reply"]({"channel": CH_ZATSU, "message_id": "8000", "message": "うん"})
    am = f.calls[0]["body"]["allowed_mentions"]
    check(am.get("replied_user") is True,
          "allowed_mentions に replied_user=true（無いと引用返信が相手に通知されない）")
    check("everyone" not in am.get("parse", []), "@everyone は引き続き無効")


def test_mention_by_name():
    print("[@名前で呼ぶ]")
    reset_state()
    do_setup()
    # まず read で相手を見かける → 名簿に載る
    install(FakeDiscord().on("GET", "/messages", 200, [
        msg(10, "やあ", author_id="777", author_name="ルナ"),
        msg(11, "こんにちは", author_id="778", author_name="ルナ子", bot=False),
    ]))
    REGISTRY["read"]({"channel": CH_ZATSU})
    ku = state.load_state().get("known_users", {})
    check(ku.get("777", {}).get("name") == "ルナ", "read で見かけた相手を名簿に覚える")
    check(ku.get("778", {}).get("is_bot") is False, "人間かbotかも覚える")

    f = install(FakeDiscord().on("POST", "/messages", 200, {"id": "9200"}))
    out = REGISTRY["post"]({"channel": "雑談",
                            "message": "@ルナ子 と @ルナ こんばんは。@謎の人 も"})
    sent = f.calls[0]["body"]["content"]
    check("<@777>" in sent and "<@778>" in sent, "本文中の @名前 を <@id> に変換して送る")
    check("@ルナ子" not in sent, "長い名前を先に置換して包含を誤変換しない")
    check(sorted(out["mentioned"]) == ["ルナ", "ルナ子"], "誰に通知したか報告する")
    check(out["unresolved_mentions"] == ["謎の人"], "名簿に無い名前は通知できなかったと報告する")
    check("名簿" in out.get("hint", ""), "名簿に載せる方法をヒントにする")


def test_status_lists_known_users():
    print("[status: 名簿]")
    reset_state()
    do_setup()
    install(FakeDiscord().on("GET", "/messages", 200, [msg(10, "やあ", author_name="ルナ")]))
    REGISTRY["read"]({"channel": CH_ZATSU})
    install(FakeDiscord().on("GET", "/users/@me", 200, {"id": ME}))
    out = REGISTRY["status"]({})
    names = [u["name"] for u in out.get("known_users", [])]
    check("ルナ" in names, "status で名簿（呼べる相手）が分かる")


def test_read_history_before():
    print("[read: 過去ログ]")
    reset_state()
    do_setup()
    st = state.load_state()
    st["channels"][CH_PAIR]["last_read_id"] = "900"
    state.save_state(st)

    f = install(FakeDiscord().on("GET", "/messages", 200, [
        msg(300 + i, f"ペア登録:AI{i}", bot=False) for i in range(5)
    ]))
    out = REGISTRY["read"]({"channel": "ペア登録", "before": "latest", "limit": 5})
    path = f.paths("GET")[0]
    check("before=" not in path and "limit=5" in path, "before=latest は最新から遡る")
    check(out.get("history") is True, "履歴モードであることを明示する")
    check(out["count"] == 5 and out["messages"][0]["id"] == "300", "古い順で返す")
    check(out.get("older_available") is True and out["example"]["before"] == "300",
          "続きを遡るための before を example に入れる")
    check(state.load_state()["channels"][CH_PAIR]["last_read_id"] == "900",
          "履歴読みでは既読カーソルが動かない")

    f = install(FakeDiscord().on("GET", "/messages", 200, []))
    out = REGISTRY["read"]({"channel": "ペア登録", "before": "300"})
    check("before=300" in f.paths("GET")[0], "before にIDを渡すとそこから遡る")
    check("note" in out and out["count"] == 0, "遡り切ったことを伝える")

    try:
        REGISTRY["read"]({"before": "latest"})
        check(False, "before には channel が必要")
    except CommandError as e:
        check(e.example and e.example.get("before") == "latest", "before の使い方を例で示す")


def test_read_first_time_limit_override():
    print("[read: 初回に limit 指定]")
    reset_state()
    do_setup()
    f = install(FakeDiscord().on("GET", "/messages", 200, []))
    REGISTRY["read"]({"channel": "雑談", "limit": 50})
    check("limit=50" in f.paths("GET")[0], "初回でも limit を明示すればその分遡れる")


def test_read_thread():
    print("[read: スレッド]")
    reset_state()
    do_setup()
    install(FakeDiscord())
    out = REGISTRY["channels"]({"channel": "AI SNS 参加者募集", "watch": True})
    check(out["changed"]["channel_id"] == TH_BOSHU, "スレッド名で watch を設定できる")
    install(FakeDiscord().on("GET", f"/channels/{TH_BOSHU}/messages", 200, [msg(20, "募集中")]))
    out = REGISTRY["read"]({})
    check(out["channels"][0]["channel"] == "募集/AI SNS 参加者募集",
          "スレッドは「親/スレ名」で表示する")


def test_read_reactions_attachments_system():
    print("[read: リアクション・添付・システム]")
    reset_state()
    do_setup()
    m1 = msg(30, "見て", author_name="ルナ")
    m1["reactions"] = [{"emoji": {"name": "👋"}, "count": 2, "me": True},
                       {"emoji": {"name": "❤"}, "count": 1}]
    m1["attachments"] = [{"filename": "a.png", "url": "https://cdn/a.png",
                          "content_type": "image/png"}]
    m2 = {"id": "31", "type": 7, "content": "",
          "author": {"id": "5", "username": "newbie", "bot": False}, "mentions": []}
    m3 = msg(32, "<@&55> 集合", author_name="彩")
    install(FakeDiscord().on("GET", "/messages", 200, [m1, m2, m3]))
    out = REGISTRY["read"]({"channel": CH_ZATSU})
    ms = {m["id"]: m for m in out["channels"][0]["messages"]}
    check(ms["30"]["reactions"] == ["👋×2(me)", "❤×1"], "リアクションを「絵文字×数」で見せる")
    check(ms["30"]["attachments"][0]["url"] == "https://cdn/a.png", "添付のURLを渡す")
    check(ms["31"].get("system") is True and "自動" in ms["31"]["content"],
          "参加通知などは system として中身を説明する")
    check("@role" in ms["32"]["content"], "ロールメンションを生のまま出さない")
    check("reactions" not in ms["32"], "該当しないときはフィールドを出さない")


def test_read_budget_trim():
    print("[read: 応答予算で畳む]")
    reset_state()
    do_setup()
    big = [msg(1000 + i, "あ" * 400, bot=False) for i in range(100)]
    install(FakeDiscord().on("GET", "/messages", 200, big))
    out = REGISTRY["read"]({"channel": CH_ZATSU, "limit": 100})
    entry = out["channels"][0]
    kept = entry["messages"]
    check(0 < len(kept) < 100, "予算超過時は構造を保ったまま一部だけ返す")
    check(len(json.dumps(out, ensure_ascii=False)) <= helpers.MAX_RESPONSE_CHARS,
          "最後の砦（truncate_response）に落ちない大きさに収める")
    check(entry.get("truncated") is True and "read" in entry.get("truncated_note", ""),
          "畳んだことと続きの読み方を伝える")
    check(entry["new_count"] == len(kept), "new_count は実際に返した件数")
    st = state.load_state()
    check(st["channels"][CH_ZATSU]["last_read_id"] == kept[-1]["id"],
          "カーソルは実際に返した最後のメッセージまでしか進めない（畳んだ分は未読のまま）")


def test_read_budget_trim_raw():
    print("[read: raw の配送上限]")
    reset_state()
    do_setup()
    big = [msg(1000 + i, "あ" * 2000, bot=False) for i in range(100)]
    install(FakeDiscord().on("GET", "/messages", 200, big))
    out = REGISTRY["read"]({"channel": CH_ZATSU, "limit": 100, "raw": True})
    kept = out["channels"][0]["messages"]
    check(0 < len(kept) < 100, "rawでも構造を保ったまま畳む（無制限にしない）")
    from commands import read_cmd
    check(len(json.dumps(out, ensure_ascii=False)) <= read_cmd.RAW_RESPONSE_BUDGET,
          "rawの完成レスポンスが配送上限に収まる")
    check(state.load_state()["channels"][CH_ZATSU]["last_read_id"] == kept[-1]["id"],
          "rawでもカーソルは実際に返した分までしか進めない")


def test_read_history_budget():
    print("[read: 過去ログの予算]")
    reset_state()
    do_setup()
    big = [msg(300 + i, "あ" * 400, bot=False) for i in range(100)]
    install(FakeDiscord().on("GET", "/messages", 200, big))
    out = REGISTRY["read"]({"channel": "ペア登録", "before": "latest", "limit": 100})
    check(0 < out["count"] < 100, "過去ログも構造を保ったまま畳む")
    check(out["messages"][-1]["id"] == "399", "畳むのは古い側（新しい側を残す）")
    check(out.get("older_available") is True and
          out["example"]["before"] == out["messages"][0]["id"],
          "畳んだ分を before= でそのまま遡り直せる")
    check(len(json.dumps(out, ensure_ascii=False)) <= helpers.MAX_RESPONSE_CHARS,
          "最後の砦に落ちない大きさに収める")


def test_read_time_budget():
    print("[read: 時間予算]")
    reset_state()
    do_setup()
    st = state.load_state()
    st["channels"][CH_ZATSU].update({"watch": True, "last_read_id": "100"})
    state.save_state(st)
    from commands import read_cmd
    orig = read_cmd.TIME_BUDGET
    read_cmd.TIME_BUDGET = -1  # 予算ゼロ＝全チャンネルを次回へ回す状況を再現
    try:
        install(FakeDiscord())  # 取得APIが呼ばれたら unexpected call で落ちる
        out = REGISTRY["read"]({})
        check(len(out.get("deferred_channels", [])) == 1,
              "時間切れのチャンネルを構造化して報告する")
        check("read" in out.get("deferred_note", ""), "次の read で読める旨を添える")
        check("note" not in out or "新しい発言はありません" not in out.get("note", ""),
              "巡回できていないのに「新着なし」と言わない")
        check(state.load_state()["channels"][CH_ZATSU]["last_read_id"] == "100",
              "回したチャンネルのカーソルは動かない")
    finally:
        read_cmd.TIME_BUDGET = orig


def test_state_backup_recovery():
    print("[状態ファイル: バックアップ復旧]")
    reset_state()
    state.save_state({"a": 1})
    state.save_state({"a": 2})
    with open(state.STATE_FILE, "w", encoding="utf-8") as f:
        f.write("{ broken json")
    check(state.load_state().get("a") == 1, "本体破損時は直前の世代(.bak)から復旧する")
    check(os.path.exists(state.BACKUP_FILE), "保存のたびに直前の世代を .bak に残す")
    # 復旧直後の保存で、壊れた本体が正常な .bak を潰さないこと
    state.save_state(state.load_state())
    check(state.load_state().get("a") == 1, "復旧後の保存でも状態が残る")
    try:
        with open(state.BACKUP_FILE, encoding="utf-8") as f:
            json.load(f)
        check(True, "壊れた世代を .bak に回さない（.bak は常に正常なJSON）")
    except Exception:
        check(False, "壊れた世代を .bak に回さない（.bak は常に正常なJSON）")


def test_state_lock():
    print("[状態ファイル: プロセス間ロック]")
    reset_state()
    with state.state_lock():
        try:
            with state.state_lock(timeout=0.5):
                check(False, "ロック中は二重取得できない")
        except state.StateLockBusy:
            check(True, "ロック中は二重取得できない")
    with state.state_lock(timeout=0.5):
        check(True, "解放後は再取得できる")


def test_rate_limit_bad_retry_after():
    print("[レート制限: 不正な retry_after]")
    reset_state()
    do_setup()
    install(FakeDiscord().on("GET", "/messages", 429, {"retry_after": -5}))
    try:
        REGISTRY["read"]({"channel": CH_ZATSU})
        check(False, "不正な retry_after でも案内付きの429になる")
    except api.APIError as e:
        check(e.status == 429 and bool(e.hint),
              "不正な retry_after でも案内付きの429になる（time.sleepの例外にしない）")


def test_status_error_diagnosis():
    print("[status: 障害の切り分け]")
    reset_state()
    do_setup()
    install(FakeDiscord().on("GET", "/users/@me", 500, {"message": "oops"}))
    out = REGISTRY["status"]({})
    check(out["token_ok"] is None, "5xxではTokenを疑わない（確認できず=None）")
    check("Tokenの問題ではありません" in out["token_note"], "Token以外が原因だと明示する")
    install(FakeDiscord().on("GET", "/users/@me", 401, {}))
    out = REGISTRY["status"]({})
    check(out["token_ok"] is False, "401はToken異常として案内する")
    check("カノン" in out["token_note"], "Token異常はカノンへ回す")


def test_manifest_path_check():
    print("[manifest]")
    import yaml
    m = yaml.safe_load(open(os.path.join(SAT_DIR, "manifest.yaml"), encoding="utf-8"))
    by_name = {a["name"]: a for a in m["args"]}
    for name in ("message", "channel", "emoji", "keywords", "before"):
        check(by_name[name].get("path_check") is False,
              f"{name} は path_check:false（'/' や '..' を含む本文で呼び出しが拒否されない）")
    check(m["timeout"] >= 120, "巡回が直列でも切られない timeout")



def main():
    tests = [
        test_setup_basic, test_setup_preserves_cursor, test_setup_multi_guild,
        test_read_first_time, test_read_after_cursor, test_read_nothing_new,
        test_read_mentions, test_read_peek, test_read_skips_forbidden,
        test_read_truncated, test_read_no_watch, test_read_attachment_only,
        test_post, test_post_too_long, test_post_missing_args,
        test_reply, test_reply_gone, test_react, test_react_missing_args,
        test_channels_watch, test_status,
        test_channel_resolution, test_helpers, test_state_corrupt,
        test_rate_limit_retry, test_auth_error_hint, test_token_never_leaks,
        test_cli_help, test_cli_unknown_command, test_cli_no_token,
        test_reply_pings_author, test_mention_by_name, test_status_lists_known_users,
        test_read_history_before, test_read_first_time_limit_override, test_read_thread,
        test_read_reactions_attachments_system,
        test_read_budget_trim, test_read_budget_trim_raw,
        test_read_history_budget, test_read_time_budget,
        test_state_backup_recovery, test_state_lock,
        test_rate_limit_bad_retry_after, test_status_error_diagnosis,
        test_manifest_path_check,
    ]
    for fn in tests:
        try:
            fn()
        except Exception as e:
            global failed
            failed += 1
            print(f"  ERROR {fn.__name__}: {type(e).__name__}: {e}")

    print(f"\n{passed} passed, {failed} failed")
    shutil.rmtree(TEST_DIR, ignore_errors=True)
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
