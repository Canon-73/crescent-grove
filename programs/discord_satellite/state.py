"""
ローカル状態管理

保存先はプロジェクト統一方針に従い workspace/program_data/discord_satellite/state.json。
保持するのは以下だけ:
  - bot_user_id / bot_display_name / guild_id / guild_name … setup で確定する不変情報
  - name_keywords … @メンション以外の「名前呼び」検知用のキーワード
  - channels … チャンネルごとの表示名・巡回対象フラグ・既読カーソル(last_read_id)

last_read_id は read が成功したときだけ前進する。これが push（常駐ブリッジ）の
取りこぼしに対する安全網になっているため、read 以外の経路で進めてはならない。

保存は「一時ファイルに書き切ってから os.replace」の原子的置換で行い、
直前の正常な世代を state.json.bak として残す。書き込み途中でプロセスが落ちても
既読カーソルごと全部を失わないための設計（load は本体→バックアップの順に読む）。
"""
import contextlib
import json
import os
import time
from datetime import datetime, timezone

try:
    import msvcrt  # Windows
except ImportError:  # POSIX
    msvcrt = None
    import fcntl


_PROG_DIR = os.path.dirname(os.path.abspath(__file__))
_WS = os.environ.get("CG_WORKSPACE", _PROG_DIR)
_STATE_DIR = os.path.join(_WS, "program_data", "discord_satellite")
STATE_FILE = os.path.join(_STATE_DIR, "state.json")
BACKUP_FILE = STATE_FILE + ".bak"
LOCK_FILE = STATE_FILE + ".lock"


class StateLockBusy(Exception):
    """別プロセスが state を使用中。柚月向けの文言への整形は main.py が行う。"""


def _ensure_dir():
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)


def _try_lock(f):
    """ノンブロッキングでロックを試す。取れたら True。"""
    try:
        if msvcrt:
            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError:
        return False


def _unlock(f):
    try:
        if msvcrt:
            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)
    except OSError:
        pass


@contextlib.contextmanager
def state_lock(timeout=10):
    """
    state を読み書きするコマンド全体を挟むプロセス間ロック。

    いまは意識ラインがコマンドを1つずつ呼ぶため衝突しないが、手動実行や
    将来の常駐ブリッジ(push)が重なると read-modify-write 同士が上書きし合い、
    既読カーソルの巻き戻り・watch 設定の消失が起きる。それを防ぐ。
    ロックはプロセス終了で OS が必ず解放するので、クラッシュしても残らない。

    重要: state に触るプロセスは必ずこのロックの中で load/save すること。
    将来の常駐ブリッジ等が load_state()/save_state() を直接呼ぶと、
    このロックが守っている直列化が崩れて lost update が再発する。
    """
    _ensure_dir()
    f = open(LOCK_FILE, "a+")
    try:
        deadline = time.monotonic() + timeout
        while not _try_lock(f):
            if time.monotonic() > deadline:
                raise StateLockBusy()
            time.sleep(0.2)
        try:
            yield
        finally:
            _unlock(f)
    finally:
        f.close()


def _read_json(path):
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    return data if isinstance(data, dict) else None


def load_state():
    # 本体が壊れていても直前の正常な世代（.bak）から既読カーソルごと復旧する。
    # どちらも読めないときだけ空を返す（setup で作り直せる）。
    for path in (STATE_FILE, BACKUP_FILE):
        try:
            if os.path.exists(path):
                data = _read_json(path)
                if data is not None:
                    return data
        except Exception:
            continue
    return {}


def _is_valid_state_file(path):
    try:
        return _read_json(path) is not None
    except Exception:
        return False


def save_state(state):
    _ensure_dir()
    state["updated_at"] = datetime.now(timezone.utc).isoformat()
    # 一時ファイルに書き切って fsync してから原子的に置き換える。
    # 置き換えの直前に、それまでの本体を .bak として残す（破損時の復旧元）。
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    # 本体が壊れているときに .bak へ回すと、正常な復旧元を壊れた世代で
    # 潰してしまう。JSONとして読める世代だけをバックアップに残す。
    if os.path.exists(STATE_FILE) and _is_valid_state_file(STATE_FILE):
        try:
            os.replace(STATE_FILE, BACKUP_FILE)
        except OSError:
            pass  # バックアップに失敗しても保存自体は続ける
    os.replace(tmp, STATE_FILE)
    # POSIX では rename の永続化にディレクトリの fsync が要る（Windows は不可・不要）
    if hasattr(os, "O_DIRECTORY"):
        try:
            dfd = os.open(_STATE_DIR, os.O_DIRECTORY)
            try:
                os.fsync(dfd)
            finally:
                os.close(dfd)
        except OSError:
            pass


def get_channels(state=None):
    st = state if state is not None else load_state()
    ch = st.get("channels")
    return ch if isinstance(ch, dict) else {}


def merge_channels(state, fetched):
    """
    APIから取得したチャンネル一覧を状態にマージする。
    既存の last_read_id / watch は必ず保持する（再setupで既読が巻き戻らないように）。

    fetched: [{"id": str, "name": str, "kind": "channel"|"thread", "parent": str|None}, ...]
    """
    channels = get_channels(state)
    for c in fetched:
        cid = str(c.get("id"))
        if not cid:
            continue
        prev = channels.get(cid) or {}
        entry = {
            "name": c.get("name") or prev.get("name") or cid,
            "last_read_id": prev.get("last_read_id"),
            "watch": bool(prev.get("watch", False)),
            "kind": c.get("kind") or prev.get("kind") or "channel",
        }
        parent = c.get("parent") or prev.get("parent")
        if parent:
            entry["parent"] = parent
        channels[cid] = entry
    state["channels"] = channels
    return state


def get_known_users(state=None):
    """read で見かけたユーザーの名簿 {id: {"name", "is_bot"}}。"""
    st = state if state is not None else load_state()
    ku = st.get("known_users")
    return ku if isinstance(ku, dict) else {}
