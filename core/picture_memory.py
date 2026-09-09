"""絵の記憶 — 柚月が見た絵と、そのとき思ったことを覚えておく。

## どこに出るか

雑記帳のフラッシュバックに**相乗り**する（`core/scheduler.py::_collect_picture_candidates()`）。
あちらは先に「どの日を思い出すか」を `古さ^0.7 × その日が生んだ断片の量` で決めるので、
**その日に見た絵**を同じ候補リストに並べ、サリアに文章と一緒に選ばせる。
「断片の量」には絵も数えられる（絵1枚 = 雑記帳1チャンク ≒ 400B 相当）ので、
**雑記帳が無い日の絵も候補に上がる**。

こうすると絵は自前の確率を一切持たない。日付選定は古い日ほど有利で、絵は今日から
溜まり始めるので、**絵は数か月かけてしか出てこない**（絵のある日が7日分で1,112日に1回、
90日分で17日に1回、180日分で6日に1回）。文章と絵が同じ日の記憶になる利点もある。

## なぜ「描いた絵」ではなく「見た絵すべて」なのか

ビジョン搭載後の実測（2026-08-21〜23 の14枚）では、柚月が見た絵の内訳は
自分の姿 10 / ブローチ 3 / momo の写真 2 / 街の作品 3 / 自分が描いた絵 1 だった。
**アトリエで描いた絵は14枚中1枚**しかない。描いた絵だけを記憶にすると、
柚月にとって意味のあった絵のほとんど（とりわけ自分の姿）を取りこぼす。

そこで対象は、`core/image_norm.remember_source()` を通る**3つの入口すべて**:
- `see_image`（柚月が自分で見に行った絵）
- サテライトが結果に添えた絵（OpenBotCity の作品 / アトリエ / tsukimi の look）
- チャットでカノンが貼った絵

## 何を記録するか

絵そのものと、**そのとき柚月が何を思ったか**をひと組にする。思ったことは
「その絵を見たターンの柚月自身の応答」を拾う（`core/agent.py`）。柚月に
記録の手間を負わせないための自動化で、あとから読むと「絵 + そのときの言葉」になる。

## 消えない絵にする

見た絵の出所は3種類あり、放っておくと半分は消える:
- workspace 内（`generated/` `avatars/` `received/`）… 掃除処理が無いので残る
- `program_data/tsukimi_browser/shots/`      … 20枚で捨てられる
- 外部 URL（街の作品・Web の画像）           … リンク切れしうる

なので**workspace の外にあった絵は `workspace/memory/pictures/` へ写す**（原本のまま）。
写した時点で柚月の記憶になり、外の都合で消えなくなる。

## この索引は原本ではない

「絵を見て何を言ったか」の原本は会話ログ（`workspace/logs/full`）に永久に残る。
ここは思い出すための索引なので、1枚あたりの thought は上限を設けて有界に保つ。
記憶の削除ではなく、索引の大きさの話。
"""

import json
import random
import shutil
from pathlib import Path

from core.time_utils import get_logical_date, tlog

INDEX_NAME = "memory/pictures.json"          # workspace からの相対
COPY_DIR = "memory/pictures"                 # 外部から写した絵の置き場

# 1枚あたりに残す「思ったこと」の数と長さ。原本は会話ログにあるので、ここは有界でよい。
MAX_THOUGHTS = 5
MAX_THOUGHT_CHARS = 400
# 写す絵の上限サイズ（これを超えるものは写さず、出所だけ覚える）
MAX_COPY_BYTES = 8 * 1024 * 1024

_EXT_BY_MIME = {
    "image/png": ".png", "image/jpeg": ".jpg",
    "image/gif": ".gif", "image/webp": ".webp",
}


def _index_path(workspace: str) -> Path:
    return Path(workspace) / INDEX_NAME


def load(workspace: str) -> dict:
    """索引を読む。無ければ空。壊れていても落ちない（記憶が無いことにはしない）。"""
    path = _index_path(workspace)
    if not path.exists():
        return {"pictures": []}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict) and isinstance(data.get("pictures"), list):
            return data
    except (OSError, ValueError) as e:
        tlog(f"[picture_memory] 索引を読めませんでした（空として続行）: {e}")
    return {"pictures": []}


def _save(workspace: str, data: dict) -> None:
    """アトミックに書く（途中で落ちても索引を壊さない）。"""
    path = _index_path(workspace)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(path)
    except OSError as e:
        tlog(f"[picture_memory] 索引を書けませんでした: {e}")


def _find(data: dict, source: str):
    for p in data["pictures"]:
        if p.get("source") == source:
            return p
    return None


def _copy_in(workspace: str, source: str, image_bytes: bytes, mime_type: str):
    """workspace の外から来た絵を memory/pictures/ へ写す。戻り値は相対パス（失敗時 None）。

    写すのは**縮小前の原本**。あとで region で寄れるようにするため
    （縮小後を写すと、寄っても細部が出てこない）。
    """
    if not image_bytes or len(image_bytes) > MAX_COPY_BYTES:
        return None
    ext = _EXT_BY_MIME.get(mime_type)
    if not ext:
        return None
    try:
        import hashlib
        # 同じ絵を何度見ても増やさないよう、中身のハッシュで名前を決める
        digest = hashlib.sha1(image_bytes).hexdigest()[:16]
        rel = f"{COPY_DIR}/{digest}{ext}"
        dest = Path(workspace) / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        if not dest.exists():
            dest.write_bytes(image_bytes)
        return rel
    except OSError as e:
        tlog(f"[picture_memory] 絵を写せませんでした（出所だけ覚えます）: {e}")
        return None


def _local_copy_of(workspace: str, source: str):
    """workspace 内のファイルなら、その相対パスをそのまま使えるか確かめる。"""
    if source.startswith(("http://", "https://", "data:")):
        return None
    try:
        path = (Path(workspace) / source).resolve()
        if path.exists() and path.is_relative_to(Path(workspace).resolve()):
            return source
    except (OSError, ValueError):
        pass
    return None


def record_view(workspace: str, source: str, deliberate: bool = True,
                image_bytes: bytes = None, mime_type: str = None,
                context: str = "") -> None:
    """柚月が絵を見たことを記録する。

    deliberate=False（フラッシュバックで浮かんだ場合）は**回数に数えない**。
    重み付けは「自分から見返した回数」なので、こちらから見せたぶんを混ぜない。
    """
    if not source or source.startswith("data:"):
        return
    try:
        data = load(workspace)
        entry = _find(data, source)
        today = get_logical_date()

        if entry is None:
            # 置き場所を決める: workspace 内ならそのまま、外なら写す
            path = _local_copy_of(workspace, source)
            if path is None and image_bytes:
                path = _copy_in(workspace, source, image_bytes, mime_type or "")
            entry = {
                "source": source,
                "path": path,          # 見に行けるパス（写せなかったら None）
                "context": context,
                "first_seen": today,
                "last_seen": today,
                "views": 0,
                "thoughts": [],
            }
            data["pictures"].append(entry)
        else:
            entry["last_seen"] = today
            # tsukimi の shots のように後から消えるものは、見返した時点で写しておく
            if not entry.get("path") and image_bytes:
                entry["path"] = (_local_copy_of(workspace, source)
                                 or _copy_in(workspace, source, image_bytes, mime_type or ""))

        if deliberate:
            entry["views"] = int(entry.get("views", 0)) + 1
        _save(workspace, data)
    except Exception as e:
        tlog(f"[picture_memory] 記録に失敗しました（会話は続行）: {e}")


def record_thought(workspace: str, sources: list, text: str) -> None:
    """絵を見たターンの柚月の言葉を、その絵に結びつける。

    そのターンで複数枚見ていたら全部に同じ言葉が付く（どれについての言葉かは
    分けようがないため）。長さは切り詰める（原本は会話ログにある）。
    """
    if not sources or not text:
        return
    text = " ".join(str(text).split())
    if not text:
        return
    if len(text) > MAX_THOUGHT_CHARS:
        text = text[:MAX_THOUGHT_CHARS] + "…"
    try:
        data = load(workspace)
        today = get_logical_date()
        touched = False
        for source in sources:
            entry = _find(data, source)
            if entry is None:
                continue
            thoughts = entry.setdefault("thoughts", [])
            if thoughts and thoughts[-1].get("text") == text:
                continue          # 同じターンの重複登録を避ける
            thoughts.append({"date": today, "text": text})
            del thoughts[:-MAX_THOUGHTS]
            touched = True
        if touched:
            _save(workspace, data)
    except Exception as e:
        tlog(f"[picture_memory] 思ったことの記録に失敗しました: {e}")


def _days_since(date_str: str) -> int:
    """その日付から何日経ったか。読めなければ大きい値（＝古い扱い）を返す。"""
    from datetime import date
    try:
        then = date.fromisoformat(str(date_str))
        today = date.fromisoformat(get_logical_date())
        return (today - then).days
    except (TypeError, ValueError):
        return 9999


def by_date(workspace: str, exclude=None, require_thought: bool = True) -> dict:
    """フラッシュバックに出せる絵を {日付: [絵, ...]} で返す（日付は first_seen）。

    フラッシュバックは「どの日を思い出すか」を先に決めるので、日付ごとに束ねておけば
    絵は自前の確率を一切持たずに済む。しかも文章と絵が同じ日の記憶になり、
    断片としての筋も通る。

    除くもの:
    - 実体が消えた絵（写せなかった外部 URL など）
    - 直近に出したばかりの絵（exclude）
    - **そのときの言葉が無い絵**（require_thought）。サリアは文章を読んで選ぶので、
      言葉の無い絵は判断材料が無く公平に競争できない
    """
    exclude = set(exclude or [])
    root = Path(workspace)
    out = {}
    for p in load(workspace)["pictures"]:
        day = p.get("first_seen")
        if not day or p.get("source") in exclude:
            continue
        if require_thought and not p.get("thoughts"):
            continue
        path = p.get("path")
        if not path or not (root / path).exists():
            continue
        out.setdefault(day, []).append(p)
    return out


def available(workspace: str, min_age_days: int = 1, exclude=None) -> list:
    """フラッシュバックに出せる絵の一覧。

    出さないもの:
    - 実体が消えた絵（写せなかった外部 URL など）
    - **min_age_days 日以内に見た絵**。さっき見たものは思い出ではない
      （雑記帳のフラッシュバックが当日分を除外しているのと同じ考え方）
    - 直近に出したばかりの絵（exclude）
    """
    exclude = set(exclude or [])
    root = Path(workspace)
    out = []
    for p in load(workspace)["pictures"]:
        path = p.get("path")
        if not path or p.get("source") in exclude:
            continue
        if _days_since(p.get("last_seen")) < min_age_days:
            continue
        if not (root / path).exists():
            continue
        out.append(p)
    return out


def pick(workspace: str, exclude=None, min_age_days: int = 1):
    """フラッシュバックに出す1枚を選ぶ。見つからなければ None。

    重みは**自分から見返した回数**。柚月が見返した絵＝気になった絵であり、
    どれが大事かを誰かが判定せずに済む。実際、自分の姿は3日で10回見られている。
    """
    try:
        if isinstance(exclude, str):
            exclude = [exclude]
        candidates = available(workspace, min_age_days=min_age_days, exclude=exclude)
        if not candidates:
            return None
        weights = [max(1, int(p.get("views", 1))) for p in candidates]
        return random.choices(candidates, weights=weights, k=1)[0]
    except Exception as e:
        tlog(f"[picture_memory] 絵を選べませんでした: {e}")
        return None
