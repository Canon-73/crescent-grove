"""tsukimi_browser - 散歩セッションと永続データ。

サテライトは呼び出しごとに新プロセスなので、全コマンドが
「state ロード → 処理 → state 保存」の形になる。

書き込み先は workspace/program_data/tsukimi_browser/ のみ。
柚月の日記・記憶・その他のデータには一切触れない。
"""
import json
import os
from datetime import datetime, timezone, timedelta
from pathlib import Path

JST = timezone(timedelta(hours=9))

# 歩数予算（ソフト → その2倍でハードストップ）
MOVE_SOFT = 15      # 遷移（open / follow / lucky / search）
MOVE_HARD = MOVE_SOFT * 2
SCREEN_SOFT = 40    # 画面読み（more 込み）
SCREEN_HARD = SCREEN_SOFT * 2

HISTORY_TAIL = 200   # history コマンドで読む末尾行数


def now_iso():
    return datetime.now(JST).isoformat(timespec="seconds")


def _atomic_write(path: Path, text: str):
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


class Session:
    def __init__(self, workspace):
        self.dir = Path(workspace) / "program_data" / "tsukimi_browser"
        self.session_path = self.dir / "session.json"
        self.history_path = self.dir / "history.jsonl"
        self.bookmarks_path = self.dir / "bookmarks.json"
        self.last_path = self.dir / "last.json"
        self.state = None

    # ---- 基本 I/O -------------------------------------------------------
    def _ensure_dir(self):
        self.dir.mkdir(parents=True, exist_ok=True)

    def load(self):
        """セッションを読む。無ければ None のまま。"""
        if self.session_path.exists():
            try:
                self.state = json.loads(self.session_path.read_text(encoding="utf-8"))
            except Exception:
                self.state = None
        return self.state

    def save(self):
        if self.state is None:
            return
        self._ensure_dir()
        _atomic_write(self.session_path, json.dumps(self.state, ensure_ascii=False))

    def active(self):
        return self.state is not None

    # ---- セッション開始 / 終了 -------------------------------------------
    def start(self, seed_text=None, seed_url=None):
        """新しい散歩を始める。既存セッションがあれば呼び出し側で end してから。"""
        self.state = {
            "started_at": now_iso(),
            "seed": {"text": seed_text, "url": seed_url},
            "stack": [],
            "current": None,
            "counters": {"moves": 0, "screens": 0},
            "visited": [],
            "bookmarked": [],
        }
        return self.state

    def end(self):
        """散歩を締める。まとめ dict を返し、セッションを閉じる。"""
        if self.state is None:
            return None
        cur = self.state.get("current")
        if cur and cur.get("url"):
            self._save_last(cur)
        summary = {
            "started_at": self.state.get("started_at"),
            "ended_at": now_iso(),
            "visited": self.state.get("visited", []),
            "bookmarked": self.state.get("bookmarked", []),
            "counters": dict(self.state.get("counters", {})),
        }
        self.state = None
        try:
            if self.session_path.exists():
                self.session_path.unlink()
        except OSError:
            pass
        return summary

    def _save_last(self, page):
        self._ensure_dir()
        try:
            _atomic_write(self.last_path, json.dumps(
                {"url": page.get("url"), "title": page.get("title"), "ts": now_iso()},
                ensure_ascii=False))
        except OSError:
            pass

    def last_page(self):
        if not self.last_path.exists():
            return None
        try:
            return json.loads(self.last_path.read_text(encoding="utf-8"))
        except Exception:
            return None

    # ---- ページ遷移 ------------------------------------------------------
    def set_current(self, page, push=True):
        """現在ページを差し替える。push=True なら直前ページを戻るスタックへ積む。"""
        if push and self.state.get("current"):
            prev = self.state["current"]
            self.state.setdefault("stack", []).append({
                "url": prev.get("url"), "title": prev.get("title"),
                "virtual": prev.get("virtual", False),
                "text": prev.get("text", ""), "links": prev.get("links", []),
                "viewport": prev.get("viewport", 1),
            })
            # スタックが無限に伸びないよう頭を捨てる
            if len(self.state["stack"]) > 30:
                self.state["stack"] = self.state["stack"][-30:]
        self.state["current"] = page

    def back(self):
        """1つ前のページへ戻る。戻れなければ None。"""
        stack = self.state.get("stack") or []
        if not stack:
            return None
        page = stack.pop()
        self.state["stack"] = stack
        self.state["current"] = page
        return page

    def current(self):
        return (self.state or {}).get("current")

    def link_by_number(self, n):
        cur = self.current()
        if not cur:
            return None
        for link in cur.get("links", []):
            if int(link.get("n", -1)) == int(n):
                return link
        return None

    # ---- カウンタ / 予算 -------------------------------------------------
    def count_move(self):
        self.state["counters"]["moves"] = self.state["counters"].get("moves", 0) + 1

    def count_screen(self):
        self.state["counters"]["screens"] = self.state["counters"].get("screens", 0) + 1

    def moves(self):
        return (self.state or {}).get("counters", {}).get("moves", 0)

    def screens(self):
        return (self.state or {}).get("counters", {}).get("screens", 0)

    def budget_state(self):
        """"ok" / "soft"（そろそろ帰ろう）/ "hard"（遷移禁止）を返す。"""
        if self.moves() >= MOVE_HARD or self.screens() >= SCREEN_HARD:
            return "hard"
        if self.moves() >= MOVE_SOFT or self.screens() >= SCREEN_SOFT:
            return "soft"
        return "ok"

    # ---- 履歴 ------------------------------------------------------------
    def record_visit(self, url, title, from_url=None):
        """永続履歴に1行追記し、セッションの訪問一覧にも足す。"""
        if not url:
            return  # 仮想ページは永続履歴に残さない
        self._ensure_dir()
        rec = {"ts": now_iso(), "url": url, "title": title, "from": from_url}
        try:
            with open(self.history_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except OSError:
            pass
        visited = self.state.setdefault("visited", [])
        if not any(v.get("url") == url for v in visited):
            visited.append({"url": url, "title": title, "ts": rec["ts"]})

    def history(self, limit=30):
        """新しい順の訪問履歴（URL 重複は最新のみ）。"""
        if not self.history_path.exists():
            return []
        try:
            lines = self.history_path.read_text(encoding="utf-8").splitlines()[-HISTORY_TAIL:]
        except OSError:
            return []
        out, seen = [], set()
        for line in reversed(lines):
            try:
                rec = json.loads(line)
            except Exception:
                continue
            url = rec.get("url")
            if not url or url in seen:
                continue
            seen.add(url)
            out.append(rec)
            if len(out) >= limit:
                break
        return out

    # ---- ブックマーク ----------------------------------------------------
    def bookmarks(self):
        if not self.bookmarks_path.exists():
            return []
        try:
            data = json.loads(self.bookmarks_path.read_text(encoding="utf-8"))
            return data if isinstance(data, list) else []
        except Exception:
            return []

    def add_bookmark(self, url, title, note=None):
        """同じ URL は note を上書きする（重複を増やさない）。"""
        items = self.bookmarks()
        for item in items:
            if item.get("url") == url:
                item["title"] = title or item.get("title")
                if note:
                    item["note"] = note
                item["added"] = now_iso()
                break
        else:
            items.append({"url": url, "title": title, "note": note or "",
                          "added": now_iso()})
        self._ensure_dir()
        _atomic_write(self.bookmarks_path, json.dumps(items, ensure_ascii=False, indent=1))
        if self.state is not None:
            marked = self.state.setdefault("bookmarked", [])
            if not any(m.get("url") == url for m in marked):
                marked.append({"url": url, "title": title, "note": note or ""})
        return items
