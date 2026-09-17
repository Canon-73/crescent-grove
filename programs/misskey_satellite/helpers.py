"""
共通ユーティリティ（要約整形・バリデーション）

サテライトはフォルダ内で自己完結させる方針のため、他サテライトと同等の処理
（suggest_commands / truncate_response）もここに持つ。共有モジュール化はしない。

要約の方針は docs/MISSKEY_SATELLITE_DESIGN.md §5 に準拠:
- 日時は JST の ISO 8601（+09:00）
- Renote は入れ子オブジェクト（純Renoteを空投稿に見せない）
- CW 付きノートは一覧では本文を伏せ、note / raw で読める
"""
import difflib
import json
from datetime import datetime, timedelta, timezone


JST = timezone(timedelta(hours=9))

# 一覧での本文の切り詰め幅（超えたら long: true を添える）
TEXT_CLIP = 400

# 入れ子（Renote元・返信先）の本文の切り詰め幅。
# 実測でタイムライン出力の 55% を renote_of が占めていた（本文は 17%）。
# 入れ子は「文脈」であって主役ではないので短くする。
NESTED_TEXT_CLIP = 200

# リアクションの種類数の上限（多い順）。人気ノートは何十種類も付くため。
MAX_REACTION_KINDS = 8

# 一覧系レスポンス1回あたりの目安の大きさ（文字）。
# 超える分は末尾から落として構造を保つ（truncate_response の塊化を発動させない）。
# 既定 limit=10 の実測が約 3,600 文字なので、通常の読みでは発動しない。
RESPONSE_BUDGET_CHARS = 12000


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


def truncate_response(obj, max_chars=16000):
    """巨大レスポンスを切り詰める（コンテキスト保護）。"""
    s = json.dumps(obj, ensure_ascii=False)
    if len(s) <= max_chars:
        return obj
    return {
        "_truncated": True,
        "_note": f"Response truncated ({len(s)} chars). Narrow the range with limit=.",
        "_preview": s[:max_chars] + "...",
    }


def fit_to_budget(items, budget=RESPONSE_BUDGET_CHARS):
    """一覧が大きすぎるとき、末尾から落として構造を保ったまま収める。

    truncate_response は応答ごと文字列の塊に潰してしまい、柚月には
    notes 配列の消えた読めない塊が届く（limit=100 で実際にそうなっていた）。
    一覧系ではこちらで先に収め、落とした分は next_until_id から読めるようにする。

    戻り値: (残した要素, 落とした件数)
    """
    kept = list(items or [])
    dropped = 0
    while kept and len(json.dumps(kept, ensure_ascii=False)) > budget:
        kept.pop()
        dropped += 1
    return kept, dropped


def parse_bool(value, default=False):
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def parse_limit(value, default=10, maximum=100):
    """limit 引数を 1〜maximum の整数にする。範囲外・非数値は ValueError。

    黙って丸めない。柚月が「100件取ったつもりで10件だった」と誤解するより、
    弾いて言い直してもらう方が安全（ai-first 設計）。
    """
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        raise ValueError(str(value))
    try:
        n = int(value)
    except (TypeError, ValueError):
        raise ValueError(str(value))
    if n < 1 or n > maximum:
        raise ValueError(str(value))
    return n


def to_jst_iso(created_at):
    """Misskey の createdAt（UTC・末尾Z）を JST の ISO 8601 に変換する。

    解析できない値はそのまま返す（表示のために落ちない）。
    """
    if not created_at:
        return None
    try:
        s = str(created_at).replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(JST).replace(microsecond=0).isoformat()
    except (TypeError, ValueError):
        return str(created_at)


def user_handle(user):
    """ユーザーの表記を作る。リモートは host まで付ける（同名の別人を区別するため）。"""
    if not isinstance(user, dict):
        return None
    name = user.get("username") or "?"
    host = user.get("host")
    return f"@{name}@{host}" if host else f"@{name}"


def clip(text, limit=TEXT_CLIP):
    """本文を limit 文字で切り詰める。戻り値: (本文, 切り詰めたか)"""
    if text is None:
        return None, False
    text = str(text)
    if limit is None or len(text) <= limit:
        return text, False
    return text[:limit] + "…", True


def summarize_files(note, limit=4):
    """添付ファイルを type と url だけにする（中身は see_image で見られる）。"""
    out = []
    for f in (note.get("files") or [])[:limit]:
        if not isinstance(f, dict):
            continue
        out.append({
            "type": f.get("type"),
            "url": f.get("url"),
            "name": f.get("name"),
        })
    return out


def _brief_note(note, hide_cw=True):
    """入れ子（Renote元・返信先）用の短い要約。

    誰がいつ何と言ったかは残すが、本文は短く、添付は件数だけにする。
    入れ子まで本編と同じ密度で書くと、実測どおり出力の過半を食う。
    """
    if not isinstance(note, dict):
        return None

    out = {
        "id": note.get("id"),
        "user": user_handle(note.get("user")),
        "at": to_jst_iso(note.get("createdAt")),
    }
    text = note.get("text")
    cw = note.get("cw")
    if cw:
        out["cw"] = cw
    if cw and hide_cw:
        out["text"] = None
        if text:
            out["has_hidden_text"] = True
    else:
        body, long = clip(text, NESTED_TEXT_CLIP)
        out["text"] = body
        if long:
            out["long"] = True

    n_files = len(note.get("files") or [])
    if n_files:
        # URLは主役のノートにだけ載せる（1件あたり約150文字あるため）
        out["files_count"] = n_files
    return out


def summarize_note(note, hide_cw=True, depth=1):
    """1ノートを数行に要約する。

    hide_cw=True（一覧系）では CW 付きノートの本文を伏せる。作者が Misskey 上で
    「ワンクッション置いて見せる」と決めたものを、一覧で無条件に展開しないため。
    読みたいときは note コマンド（hide_cw=False）か raw=true で読める。

    depth は renote / reply の入れ子をたどる深さ。
    """
    if not isinstance(note, dict):
        return None

    out = {
        "id": note.get("id"),
        "user": user_handle(note.get("user")),
        "at": to_jst_iso(note.get("createdAt")),
    }

    text = note.get("text")
    cw = note.get("cw")
    if cw:
        out["cw"] = cw
        if hide_cw:
            # 本文は伏せる。中身があることだけ伝える
            out["text"] = None
            if text:
                out["has_hidden_text"] = True
        else:
            body, long = clip(text)
            out["text"] = body
            if long:
                out["long"] = True
    else:
        body, long = clip(text)
        out["text"] = body
        if long:
            out["long"] = True

    visibility = note.get("visibility")
    if visibility and visibility != "public":
        out["visibility"] = visibility

    reactions = note.get("reactions")
    if isinstance(reactions, dict) and reactions:
        # 種類が多いノートがあるので多い順に打ち切る（何が優勢かは分かる）
        ranked = sorted(reactions.items(), key=lambda kv: -(kv[1] or 0))
        out["reactions"] = dict(ranked[:MAX_REACTION_KINDS])
        if len(ranked) > MAX_REACTION_KINDS:
            out["reactions_more"] = len(ranked) - MAX_REACTION_KINDS
    if note.get("myReaction"):
        out["my_reaction"] = note.get("myReaction")

    files = summarize_files(note)
    if files:
        out["files"] = files

    # 純Renote（外側の text が null で実体は renote 側）を空投稿に見せない。
    # 引用Renoteなら外側 text=引用コメント / renote_of.text=引用元本文 になる。
    renote = note.get("renote")
    if isinstance(renote, dict):
        out["renote_of"] = (_brief_note(renote, hide_cw=hide_cw)
                            if depth > 0 else {"id": renote.get("id")})
    elif note.get("renoteId"):
        out["renote_of"] = {"id": note.get("renoteId")}

    reply = note.get("reply")
    if isinstance(reply, dict):
        out["reply_to"] = (_brief_note(reply, hide_cw=hide_cw)
                           if depth > 0 else {"id": reply.get("id")})
    elif note.get("replyId"):
        out["reply_to"] = {"id": note.get("replyId")}

    for key, field in (("replies", "repliesCount"), ("renotes", "renoteCount")):
        n = note.get(field)
        if isinstance(n, int) and n > 0:
            out.setdefault("counts", {})[key] = n

    return out


def summarize_notes(notes, hide_cw=True):
    """ノート配列を要約する。"""
    return [summarize_note(n, hide_cw=hide_cw) for n in (notes or [])
            if isinstance(n, dict)]


def summarize_notification(item):
    """通知1件を要約する。種類ごとに必要なものだけ残す。"""
    if not isinstance(item, dict):
        return None
    out = {
        "id": item.get("id"),
        "type": item.get("type"),
        "at": to_jst_iso(item.get("createdAt")),
    }
    user = item.get("user")
    if isinstance(user, dict):
        out["user"] = user_handle(user)
    if item.get("reaction"):
        out["reaction"] = item.get("reaction")

    note = item.get("note")
    if isinstance(note, dict):
        # 通知は「どのノートに対する何か」が分かれば十分なので浅く要約する
        out["note"] = summarize_note(note, hide_cw=True, depth=0)
    return out


def next_until_id(items):
    """続き読み用のカーソル（最後の要素のID）を返す。"""
    for item in reversed(items or []):
        if isinstance(item, dict) and item.get("id"):
            return item.get("id")
    return None
