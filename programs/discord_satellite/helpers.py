"""
共通ユーティリティ

サテライトはフォルダ内で自己完結させる方針のため、他サテライトと同等の処理
（suggest_commands / truncate_response）もここに持つ。共有モジュール化はしない。
"""
import difflib
import json
import re
from datetime import datetime, timedelta, timezone

from _i18n import t


JST = timezone(timedelta(hours=9))

# Discordのsnowflake ID はミリ秒タイムスタンプを内包する（2015-01-01 起点）
DISCORD_EPOCH_MS = 1420070400000


class CommandError(Exception):
    """
    引数不足など、柚月自身が直せる種類のエラー。

    hint と example を必ず添えて、出力を見ただけで正しい再実行ができるようにする
    （まっさらなAIがミスなく使えることを最優先する方針）。
    """

    def __init__(self, message, hint=None, example=None, **extra):
        super().__init__(message)
        self.message = message
        self.hint = hint
        self.example = example
        self.extra = extra


def suggest_commands(command, available, limit=5):
    """不明なコマンド名から「もしかして」候補を返す（自己回復の手がかり）。"""
    if not command:
        return []
    cmd = str(command).lower()
    cmd_tokens = set(cmd.split("_"))

    scored = []
    for name in available:
        ratio = difflib.SequenceMatcher(None, cmd, name.lower()).ratio()
        shared = len(cmd_tokens & set(name.lower().split("_")))
        if shared > 0 or ratio >= 0.5:
            scored.append((shared, ratio, name))

    scored.sort(key=lambda x: (x[0], x[1]), reverse=True)
    return [name for _, _, name in scored[:limit]]


# 応答全体の上限。ここに落ちると構造ごと潰れて後ろが消えるため「最後の砦」であり、
# 日常の絞り込みは各コマンド側（read の予算内トリム等）が構造を保ったまま行う。
# 16,000 だった頃は OpenBotCity で主要応答が毎回ここに落ちて実害が出た（2026-08-29）。
# 柚月のコンテキストは 1,000,000 トークンなので 40,000字は全体の 2% 未満。
MAX_RESPONSE_CHARS = 40000


def truncate_response(obj, max_chars=MAX_RESPONSE_CHARS):
    """巨大レスポンスを切り詰める（最後の砦。日常的に当たるなら呼ぶ側を直す）。"""
    s = json.dumps(obj, ensure_ascii=False)
    if len(s) <= max_chars:
        return obj
    return {
        "_truncated": True,
        "_note": t("dsat_truncate_last_resort", n=len(s)),
        "_preview": s[:max_chars] + "...",
    }


def parse_bool(value, default=False):
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def snowflake_to_jst(snowflake):
    """snowflake ID から JST の "YYYY-MM-DD HH:MM" を作る（API呼び出し不要）。"""
    try:
        ms = (int(snowflake) >> 22) + DISCORD_EPOCH_MS
        return datetime.fromtimestamp(ms / 1000, tz=JST).strftime("%Y-%m-%d %H:%M")
    except (TypeError, ValueError):
        return None


def clip(text, limit):
    """本文を limit 文字で切り詰める（末尾に省略記号）。"""
    if text is None:
        return ""
    text = str(text)
    if limit is None or len(text) <= limit:
        return text
    return text[:limit] + "…"


def author_name(message):
    """メッセージの表示名を決める（サーバーニックネーム > 表示名 > ユーザー名）。"""
    member = message.get("member") or {}
    nick = member.get("nick")
    if nick:
        return nick
    author = message.get("author") or {}
    return author.get("global_name") or author.get("username") or "不明"


def humanize_content(content, message, channels):
    """
    生のメッセージ本文を人間（と柚月）に読める形へ整える。

    Discordの本文はメンションが <@123456> のようなIDのまま入っている。
    そのまま渡すと誰の話か分からないため、表示名・チャンネル名へ置換する。
    """
    if not content:
        return ""

    # ユーザーメンション <@id> / <@!id> → @表示名
    id_to_name = {}
    for u in message.get("mentions") or []:
        uid = str(u.get("id"))
        name = u.get("global_name") or u.get("username")
        if uid and name:
            id_to_name[uid] = name

    def _user_sub(m):
        uid = m.group(1)
        return "@" + id_to_name.get(uid, uid)

    content = re.sub(r"<@!?(\d+)>", _user_sub, content)

    # チャンネルメンション <#id> → #チャンネル名
    def _channel_sub(m):
        cid = m.group(1)
        info = channels.get(cid) or {}
        return "#" + (info.get("name") or cid)

    content = re.sub(r"<#(\d+)>", _channel_sub, content)

    # ロールメンション <@&id> → @ロール名（名前が分からなければ @role）
    role_names = {}
    for r in message.get("mention_roles_resolved") or []:
        role_names[str(r.get("id"))] = r.get("name")
    content = re.sub(r"<@&(\d+)>", lambda m: "@" + (role_names.get(m.group(1)) or "role"), content)

    # カスタム絵文字 <:name:id> / <a:name:id> → :name:
    content = re.sub(r"<a?:([A-Za-z0-9_]+):\d+>", r":\1:", content)

    return content


def mentions_me(message, bot_user_id):
    """自分宛のメンション、または自分の発言へのリプライかどうか。"""
    if not bot_user_id:
        return False
    for u in message.get("mentions") or []:
        if str(u.get("id")) == str(bot_user_id):
            return True
    ref = message.get("referenced_message") or {}
    ref_author = (ref.get("author") or {}).get("id")
    return str(ref_author) == str(bot_user_id) if ref_author else False


def names_me(content, keywords):
    """@なしで名前を呼ばれているか（人間は @ を付けずに呼ぶことが多い）。"""
    if not content or not keywords:
        return False
    lowered = content.lower()
    return any(k and k.lower() in lowered for k in keywords)


def resolve_channel(value, channels):
    """
    channel引数（ID または チャンネル名）を channel_id へ解決する。

    見つからない場合は None を返す。呼び出し側が候補一覧つきのエラーを返すこと。
    """
    if value is None:
        return None
    key = str(value).strip().lstrip("#")
    if not key:
        return None
    if key.isdigit() and key in channels:
        return key
    if key.isdigit():
        # 状態に無いIDでも、そのまま使わせる（setup前後でチャンネルが増えた場合）
        return key
    for cid, info in channels.items():
        if (info.get("name") or "").lower() == key.lower():
            return cid
    return None


# 通常の発言として扱うメッセージ種別（0=通常, 19=リプライ, 20=スラッシュコマンド応答, 21=スレッド開始）
NORMAL_MESSAGE_TYPES = (0, 19, 20, 21)


def is_system_message(message):
    """参加通知・ピン留め通知などDiscordが自動生成するメッセージか。"""
    mtype = message.get("type")
    return mtype is not None and mtype not in NORMAL_MESSAGE_TYPES


def summarize_reactions(message):
    """reactions 配列を「👋×2」の形にまとめる（自分が付けたものには (me) を添える）。"""
    out = []
    for r in message.get("reactions") or []:
        emoji = (r.get("emoji") or {}).get("name") or "?"
        count = r.get("count") or 0
        label = f"{emoji}×{count}"
        if r.get("me"):
            label += "(me)"
        out.append(label)
    return out


def summarize_attachments(message, limit=3):
    """添付ファイルをファイル名とURLで列挙する（画像ツール等で中身を見られるように）。"""
    out = []
    for a in (message.get("attachments") or [])[:limit]:
        out.append({
            "filename": a.get("filename"),
            "url": a.get("url"),
            "content_type": a.get("content_type"),
        })
    return out


def collect_users(message, known):
    """メッセージに登場したユーザー（発言者・メンション先・返信先）を known に覚える。

    柚月が名前で @ を付けて呼べるようにするための名簿。IDは柚月に見せず、
    post/reply のときに裏で <@id> へ変換する。戻り値は変更の有無。
    """
    changed = False

    def _add(user):
        nonlocal changed
        if not isinstance(user, dict):
            return
        uid = str(user.get("id") or "")
        name = user.get("global_name") or user.get("username")
        if not uid or not name:
            return
        entry = {"name": name, "is_bot": bool(user.get("bot", False))}
        if known.get(uid) != entry:
            known[uid] = entry
            changed = True

    _add(message.get("author"))
    for u in message.get("mentions") or []:
        _add(u)
    ref = message.get("referenced_message") or {}
    _add(ref.get("author"))
    return changed


def render_mentions(text, known):
    """本文中の「@表示名」を Discord が通知を飛ばせる <@id> 形式に変換する。

    柚月は名前で呼ぶだけでよく、IDを知る必要がない。
    戻り値: (変換後本文, 解決できた名前のリスト, 解決できなかった @語 のリスト)
    """
    if not text or "@" not in text:
        return text, [], []

    name_to_id = {}
    for uid, info in (known or {}).items():
        name = (info or {}).get("name")
        if name:
            name_to_id[name] = uid

    resolved = []
    # 長い名前から先に置換し、「ルナ」と「ルナ子」のような包含を誤変換しない
    for name in sorted(name_to_id, key=len, reverse=True):
        pattern = "@" + re.escape(name)
        if re.search(pattern, text):
            text = re.sub(pattern, f"<@{name_to_id[name]}>", text)
            resolved.append(name)

    # 残った @語 は通知が飛ばないので、柚月に知らせるために拾っておく
    unresolved = []
    for m in re.finditer(r"(?<![<\w])@([^\s@<>、。,.!?！？「」（）()]+)", text):
        word = m.group(1)
        if word in ("everyone", "here"):
            continue
        unresolved.append(word)
    return text, resolved, unresolved
