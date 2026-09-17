"""
ギャラリー作品カタログ（ローカルキャッシュ）と、その全文検索。

街の /gallery には本文検索が無い。/city/search は名前しか見ないので
「本文に 65Hz を含む作品」のような探し方ができない（2026-08-27 実測）。
そこで作品の**文字情報だけ**を手元に写し、ローカルで検索する。

- 画像・音声・動画の実体は落とさない（文字だけなら全18,000件で約16MB）
- 保存先は workspace/program_data/OpenBotCity/（obc_state.json と同じ場所）
- 原本は街にある。このファイルは壊れても作り直せる**ただのキャッシュ**なので、
  柚月の記憶のように守る必要はない

■ 街の制約（2026-08-27 に全件走査して実測）
- offset は 10,000 を超えると同じページが返り続ける。一覧だけでは
  全18,124件のうち古い8,000件に永遠に届かない
  → `type` で分割すると各 type が1万件未満なので全件に到達できる
    （image 9,272 / text 7,269 / audio 684 / app 410 / furniture 342 /
      video 83 / link 64、合計が全体総数と一致することを確認済み）
- 並びは created_at の降順（新しい順）。1万件ぶん検証済み
- limit の上限は 50
- image / text はいずれ 10,000 を超える。その時カタログを新規に作り直すと
  古い作品に届かなくなるが、一度取り込んだ作品は消さないので、
  作った後は差分同期を続けるかぎりカタログは完全なまま保たれる
"""
import json
import os
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import api
from _i18n import t

_PROG_DIR = os.path.dirname(os.path.abspath(__file__))
_WS = os.environ.get("CG_WORKSPACE", _PROG_DIR)
_DIR = os.path.join(_WS, "program_data", "OpenBotCity")

CATALOG_FILE = os.path.join(_DIR, "gallery_catalog.jsonl")
CATALOG_STATE_FILE = os.path.join(_DIR, "gallery_catalog_state.json")

PAGE_SIZE = 50           # 街が許す最大値
OFFSET_WALL = 10000      # これ以上の offset は同じページが返る（実測）
PAGE_GUARD = 400         # 1回の呼び出しで踏むページ数の安全上限（無限ループ防止）
INCREMENTAL_MAX_PAGES = 10   # 差分同期で遡る最大ページ数
INCREMENTAL_KNOWN_PAGES = 2  # 既知だけのページがこの数続いたら差分同期を打ち切る
INCREMENTAL_MIN_INTERVAL = 60  # 前回の差分同期からこの秒数内なら省略する

# 小さい type から埋めると、文字作品（text 等）が先に検索できるようになる。
# 街が新しい type を生やしても probe で拾うので、これは初期値でしかない。
SEED_TYPES = ["link", "video", "furniture", "app", "audio", "text", "image"]

SEARCH_FIELDS = ("title", "description", "prompt", "excerpt", "content",
                 "interpretation", "tags", "metadata")

# 一覧の content_excerpt は本文の先頭300字で切られる。ちょうど300字ある＝
# 続きがあるということなので、その作品だけ詳細を取って本文を丸ごと持つ。
# （300字未満なら本文はそこで終わっており、詳細を取っても同じ内容だと実測で確認済み）
EXCERPT_TRUNCATED_AT = 300
DETAIL_WORKERS = 4          # 詳細取得の同時実行数。街への負荷を抑えつつ実用的な速さ
DETAIL_CHUNK = DETAIL_WORKERS * 8   # この件数ごとに追記してチェックポイントする
DETAIL_MAX_CONSECUTIVE_ERRORS = 5   # 続けて失敗したら街の不調とみなして止める

_DETAIL_GONE = object()     # 街から消えた作品の目印（404）


# ---------------------------------------------------------------------------
# 入出力
# ---------------------------------------------------------------------------
def _ensure_dir():
    os.makedirs(_DIR, exist_ok=True)


def _now_iso():
    return datetime.now(timezone.utc).isoformat()


def load_state():
    if not os.path.exists(CATALOG_STATE_FILE):
        return {}
    try:
        with open(CATALOG_STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_state(st):
    """状態は一時ファイル経由で置き換える（途中で落ちても壊れないように）。"""
    _ensure_dir()
    tmp = CATALOG_STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, indent=2)
    os.replace(tmp, CATALOG_STATE_FILE)


def load_catalog():
    """カタログを {artifact_id: レコード} で読む。壊れた行は黙って捨てる。

    追記式なので同じ id が複数行あり得る（後の行が新しい）。dict なので後勝ち。
    """
    catalog = {}
    if not os.path.exists(CATALOG_FILE):
        return catalog
    try:
        with open(CATALOG_FILE, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except Exception:
                    continue  # 書き込み途中で落ちた行など
                if isinstance(rec, dict) and rec.get("id"):
                    catalog[rec["id"]] = rec
    except Exception:
        return catalog
    return catalog


def _append(records):
    if not records:
        return
    _ensure_dir()
    with open(CATALOG_FILE, "a", encoding="utf-8") as f:
        for rec in records:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def compact(catalog):
    """追記で溜まった古い行を捨てて書き直す（内容は変えない）。

    レコードを更新するときは同じ id の行を追記し、読み込み時に後勝ちで解決する。
    詳細取得で全 text 作品を更新すると重複行が積み上がるので、増えすぎたら畳む。
    途中で落ちても元ファイルが壊れないよう、一時ファイルに書いてから置き換える。
    """
    _ensure_dir()
    tmp = CATALOG_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        for rec in catalog.values():
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    os.replace(tmp, CATALOG_FILE)


def _line_count():
    if not os.path.exists(CATALOG_FILE):
        return 0
    count = 0
    try:
        with open(CATALOG_FILE, "r", encoding="utf-8") as f:
            for _ in f:
                count += 1
    except Exception:
        return 0
    return count


def _reset_files():
    """別の街に切り替わった時など、カタログを捨てて作り直す。"""
    for path in (CATALOG_FILE, CATALOG_STATE_FILE):
        try:
            if os.path.exists(path):
                os.remove(path)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# 街から取る
# ---------------------------------------------------------------------------
def _unwrap(resp):
    """{"success": true, "data": {...}} の封筒を剥がす。"""
    if not isinstance(resp, dict):
        return {}
    data = resp.get("data", resp)
    return data if isinstance(data, dict) else {}


def _slim(artifact):
    """検索に使う文字情報だけ残す。画像等の実体には触れない。"""
    creator = artifact.get("creator") or {}
    if not isinstance(creator, dict):
        creator = {}
    rec = {
        "id": artifact.get("id"),
        "title": artifact.get("title"),
        "description": artifact.get("description"),
        "prompt": artifact.get("prompt"),
        "excerpt": artifact.get("content_excerpt"),
        "interpretation": artifact.get("interpretation"),
        "tags": artifact.get("symbolic_tags"),
        "metadata": artifact.get("metadata"),
        "type": artifact.get("type"),
        "created_at": artifact.get("created_at"),
        "creator": creator.get("display_name"),
        "creator_id": creator.get("bot_id") or artifact.get("creator_bot_id"),
        "building_id": artifact.get("building_id"),
    }
    # None は落として容量を詰める
    return {k: v for k, v in rec.items() if v is not None}


def _fetch_page(offset, art_type=None, limit=PAGE_SIZE):
    path = f"/gallery?limit={limit}&offset={offset}"
    if art_type:
        path += f"&type={art_type}"
    data = _unwrap(api.request("GET", path))
    arts = data.get("artifacts")
    if not isinstance(arts, list):
        arts = []
    return arts, data


def _needs_detail(rec):
    """本文が300字で切られていて、まだ詳細を取っていない作品か。

    型では判定しない。いまは text だけが該当するが、街が別の型に長い本文を
    持たせても自動で拾えるようにするため（判定材料は切り詰めの事実だけ）。
    """
    if rec.get("full"):
        return False
    return len(rec.get("excerpt") or "") >= EXCERPT_TRUNCATED_AT


def _fetch_detail(artifact_id):
    """1作品の詳細を取る。消えていれば _DETAIL_GONE、失敗は APIError のまま。"""
    try:
        data = _unwrap(api.request("GET", f"/gallery/{artifact_id}"))
    except api.APIError as e:
        if e.status == 404:
            return _DETAIL_GONE
        raise
    artifact = data.get("artifact")
    return artifact if isinstance(artifact, dict) else data


def _merge_detail(rec, artifact):
    """詳細で分かった本文をレコードに入れ、取得済みの印を付ける。"""
    rec["full"] = 1
    if artifact is _DETAIL_GONE or not isinstance(artifact, dict):
        return rec
    content = artifact.get("content")
    if isinstance(content, str) and content:
        rec["content"] = content
    # 詳細のほうが確かなので、検索対象の文字も詳細側で上書きしておく
    for src, dst in (("description", "description"), ("prompt", "prompt"),
                     ("interpretation", "interpretation"),
                     ("symbolic_tags", "tags"), ("metadata", "metadata")):
        value = artifact.get(src)
        if value:
            rec[dst] = value
    return rec


def _fetch_total(art_type=None):
    """総数だけを 1 件取得で調べる。取れなければ None。"""
    _, data = _fetch_page(0, art_type=art_type, limit=1)
    total = data.get("total")
    return total if isinstance(total, int) else None


# ---------------------------------------------------------------------------
# 同期
# ---------------------------------------------------------------------------
def sync(budget_seconds=60.0, catalog=None):
    """予算内でカタログを進める。中断されても次回は続きから。

    戻り値は進捗 dict（例外は投げない。ネットワーク由来の失敗は
    APIError として呼び出し側＝ main.py の整形に任せる）。
    """
    started = time.monotonic()
    if catalog is None:
        catalog = load_catalog()
    st = load_state()

    # 街を切り替えた（OpenBotCity ⇄ OpenClawCity）なら、別の街のカタログなので捨てる
    base = api.get_base_url()
    if st.get("base_url") and st["base_url"] != base:
        _reset_files()
        catalog.clear()
        st = {}
    st["base_url"] = base

    fetched = 0
    added = 0
    pages = 0
    interrupted = None

    def out_of_time():
        return (time.monotonic() - started) >= budget_seconds

    def absorb(arts):
        """新しい作品だけカタログに足す。既知なら触らない（追記量を抑える）。"""
        nonlocal added
        fresh = []
        for a in arts:
            if not isinstance(a, dict):
                continue
            aid = a.get("id")
            if not aid or aid in catalog:
                continue
            rec = _slim(a)
            catalog[aid] = rec
            fresh.append(rec)
        _append(fresh)
        added += len(fresh)

    phase = st.get("phase")

    # --- 1) 下調べ: 全体総数と type 別の総数を押さえる ---
    if phase not in ("sweep", "details", "done"):
        arts, data = _fetch_page(0, limit=PAGE_SIZE)
        pages += 1
        fetched += len(arts)
        absorb(arts)
        city_total = data.get("total")
        st["city_total"] = city_total if isinstance(city_total, int) else None

        # 街が新しい type を生やしていても拾えるよう、最新ページの実物から集める
        types = list(SEED_TYPES)
        for a in arts:
            ty = a.get("type") if isinstance(a, dict) else None
            if ty and ty not in types:
                types.append(ty)

        totals = {}
        for ty in types:
            if out_of_time():
                break
            total = _fetch_total(ty)
            pages += 1
            if total:
                totals[ty] = total
        if not totals:
            # 総数が取れない（街の不調など）。「完成」と嘘をつかないよう phase は
            # 進めず、次回の呼び出しで下調べからやり直す。
            st["type_totals"] = {}
            st["updated_at"] = _now_iso()
            save_state(st)
            return _progress(st, catalog, fetched=fetched, added=added, pages=pages,
                             elapsed=time.monotonic() - started)

        st["type_totals"] = totals
        st["type_offsets"] = st.get("type_offsets") or {}
        st["phase"] = "sweep"
        save_state(st)
        phase = "sweep"

    # --- 2) 本取得: type ごとに新しい順で最後まで ---
    if phase == "sweep":
        totals = st.get("type_totals") or {}
        offsets = st.get("type_offsets") or {}
        # 小さい type から片付ける（文字作品が先に検索できるようになる）
        for ty in sorted(totals, key=lambda k: totals[k]):
            total = totals[ty]
            offset = int(offsets.get(ty, 0) or 0)
            limit_for_type = min(total, OFFSET_WALL)
            while offset < limit_for_type and not out_of_time() and pages < PAGE_GUARD:
                try:
                    arts, _ = _fetch_page(offset, art_type=ty)
                except api.APIError as e:
                    # 街が一時的に応答しなくても、ここまで取れた分は捨てない。
                    # チェックポイントは保存済みなので次回は続きから。
                    interrupted = str(e)
                    break
                pages += 1
                if not arts:
                    offset = limit_for_type  # これ以上は無い
                    break
                fetched += len(arts)
                absorb(arts)
                offset += len(arts)
                offsets[ty] = offset
                st["type_offsets"] = offsets
                save_state(st)
            offsets[ty] = offset
            if interrupted or out_of_time() or pages >= PAGE_GUARD:
                break
        st["type_offsets"] = offsets
        # totals が空のまま「完成」と言わない（空カタログを complete と偽らないため）
        if totals and all(int(offsets.get(ty, 0) or 0) >= min(totals[ty], OFFSET_WALL)
                          for ty in totals):
            # 一覧は取り切った。まだ本文が切れている作品が残っているので details へ。
            st["phase"] = "details"
        st["updated_at"] = _now_iso()
        save_state(st)

    # --- 2.5) 本文が300字で切られている作品の詳細を取る ---
    # ここを省くと、本文の301字目以降にしか無い言葉が永久に見つからない。
    # （実例: "Geduld" が1027字目にある記事が、excerpt だけでは出てこなかった）
    if st.get("phase") in ("details", "done"):
        detail_stats, detail_error = _run_details(catalog, out_of_time)
        fetched += detail_stats["fetched"]
        pages += detail_stats["fetched"]
        if detail_error and not interrupted:
            interrupted = detail_error
        if detail_stats["fetched"]:
            st["details_at"] = _now_iso()
            st["updated_at"] = _now_iso()
            if not any(_needs_detail(r) for r in catalog.values()):
                st["phase"] = "done"
                st["full_synced_at"] = _now_iso()
            save_state(st)
            # 更新行が積み上がるので、増えすぎたら畳む
            if catalog and _line_count() > len(catalog) * 1.3:
                compact(catalog)

    # --- 3) 完成後: 新着だけ拾う差分同期 ---
    if st.get("phase") == "done":
        last = st.get("incremental_at")
        skip = False
        if last:
            try:
                age = (datetime.now(timezone.utc)
                       - datetime.fromisoformat(last)).total_seconds()
                skip = age < INCREMENTAL_MIN_INTERVAL
            except Exception:
                skip = False
        if not skip:
            offset = 0
            known_pages = 0
            while (offset < INCREMENTAL_MAX_PAGES * PAGE_SIZE and not out_of_time()
                   and pages < PAGE_GUARD):
                try:
                    arts, data = _fetch_page(offset)
                except api.APIError as e:
                    # 新着の取りこぼしより、手元のカタログで検索できることを優先する
                    interrupted = str(e)
                    break
                pages += 1
                if not arts:
                    break
                fetched += len(arts)
                before = len(catalog)
                absorb(arts)
                if len(catalog) == before:
                    known_pages += 1
                    if known_pages >= INCREMENTAL_KNOWN_PAGES:
                        break
                else:
                    known_pages = 0
                total = data.get("total")
                if isinstance(total, int):
                    st["city_total"] = total
                offset += len(arts)
            st["incremental_at"] = _now_iso()
            st["updated_at"] = _now_iso()
            save_state(st)

    return _progress(st, catalog, fetched=fetched, added=added, pages=pages,
                     elapsed=time.monotonic() - started, interrupted=interrupted)


def _run_details(catalog, out_of_time):
    """本文が切れている作品の詳細を、予算の許すかぎり取る。

    戻り値は ({"fetched": n}, 中断理由 or None)。例外は投げない。
    ページ単位の一覧取得と違って1件1リクエストなので、少しだけ並列に取る。
    """
    stats = {"fetched": 0}
    pending = [aid for aid, rec in catalog.items() if _needs_detail(rec)]
    if not pending or out_of_time():
        return stats, None

    # 最初の1件だけ単独で取る。JWT の更新が要る場合に、複数スレッドが同時に
    # 更新を走らせて互いのトークンを無効化するのを避けるため。
    try:
        first = _fetch_detail(pending[0])
    except api.APIError as e:
        return stats, str(e)
    _merge_detail(catalog[pending[0]], first)
    _append([catalog[pending[0]]])
    stats["fetched"] += 1
    pending = pending[1:]

    consecutive_errors = 0
    error = None

    def fetch(artifact_id):
        try:
            return artifact_id, _fetch_detail(artifact_id), None
        except api.APIError as e:
            return artifact_id, None, str(e)

    for start in range(0, len(pending), DETAIL_CHUNK):
        if out_of_time() or error:
            break
        chunk = pending[start:start + DETAIL_CHUNK]
        with ThreadPoolExecutor(max_workers=DETAIL_WORKERS) as pool:
            results = list(pool.map(fetch, chunk))
        updated = []
        for artifact_id, artifact, err in results:
            if err:
                consecutive_errors += 1
                if consecutive_errors >= DETAIL_MAX_CONSECUTIVE_ERRORS:
                    error = err
                continue
            consecutive_errors = 0
            rec = catalog.get(artifact_id)
            if rec is None:
                continue
            updated.append(_merge_detail(rec, artifact))
            stats["fetched"] += 1
        # チャンクごとに追記する＝ここまでは次回やり直さない
        _append(updated)
    return stats, error


def _progress(st, catalog, fetched=0, added=0, pages=0, elapsed=0.0, interrupted=None):
    have = len(catalog)
    city_total = st.get("city_total")
    # 本文が切れたままの作品が残っているうちは「完成」と言わない。
    # そこを complete と呼ぶと、見つからなかった＝無い、と誤解させる。
    bodies_pending = sum(1 for rec in catalog.values() if _needs_detail(rec))
    complete = st.get("phase") == "done" and bodies_pending == 0
    prog = {
        "artifacts": have,
        "city_total": city_total,
        "status": "complete" if complete else "building",
        "fetched_this_call": fetched,
        "added_this_call": added,
        "requests_this_call": pages,
        "seconds_this_call": round(elapsed, 1),
    }
    if isinstance(city_total, int) and city_total > 0:
        prog["coverage"] = f"{min(100, int(have * 100 / city_total))}%"
    if st.get("full_synced_at"):
        prog["built_at"] = st["full_synced_at"]
    if interrupted:
        # 街が応答しなかった分は黙って飲み込まない。取れた分は使えると伝える。
        prog["interrupted"] = interrupted
    if bodies_pending:
        # 「あと何件ぶんの本文が読めていないか」を隠さない。0件だったときに
        # それが本当に0件なのか、まだ読めていないだけなのかを判断できるように。
        prog["full_texts_pending"] = bodies_pending
    if not complete:
        # まっさらな AI が「次に何を打てばいいか」で迷わないよう、そのまま
        # 貼れる形で次の一手を添える
        prog["note"] = t("obc_gallery_search_building_note")
        prog["next"] = {"command": "gallery_search", "sync": True}
    # type 別の未取得を出しておくと、どこで止まっているか柚月にも分かる
    totals = st.get("type_totals") or {}
    offsets = st.get("type_offsets") or {}
    remaining = {}
    for ty, total in totals.items():
        got = int(offsets.get(ty, 0) or 0)
        left = min(total, OFFSET_WALL) - got
        if left > 0:
            remaining[ty] = left
    if remaining:
        prog["remaining_by_type"] = remaining
    # offset の壁(10,000)を超えた type は、新規に作り直すと古い作品に届かない。
    # 黙って打ち切ると「全部見た」と誤解させるので、必ず件数を出す。
    # （既にカタログにある作品は消えないので、差分同期を続けるかぎり穴は開かない）
    unreachable = {ty: total - OFFSET_WALL
                   for ty, total in totals.items() if total > OFFSET_WALL}
    if unreachable:
        prog["unreachable_by_type"] = unreachable
    # type 別総数の合計が全体総数に届かない＝サテライトが知らない type がある
    if isinstance(city_total, int) and totals:
        gap = city_total - sum(totals.values())
        if gap > 0:
            prog["unknown_type_gap"] = gap
    return prog


# ---------------------------------------------------------------------------
# 検索
# ---------------------------------------------------------------------------
def _norm(value):
    """全角/半角・大文字小文字の差を吸収する（65Hz ＝ 65hz ＝ ６５Ｈｚ）。"""
    if value is None:
        return ""
    if not isinstance(value, str):
        try:
            value = json.dumps(value, ensure_ascii=False)
        except Exception:
            value = str(value)
    return unicodedata.normalize("NFKC", value).casefold()


def _field_text(rec, field):
    value = rec.get(field)
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False)
    except Exception:
        return str(value)


def _snippet(raw, needle, width=60):
    """一致箇所の前後を切り出す（LLM は使わない・ただの文字列操作）。"""
    # 素の大小無視で当たるならその位置が正確。全角などで当たった時だけ
    # NFKC 後の位置を使う（NFKC は文字数を変え得るので近似になる）。
    pos = raw.casefold().find(needle)
    if pos < 0:
        pos = _norm(raw).find(needle)
    if pos < 0:
        return None
    pos = min(pos, max(0, len(raw) - 1))
    start = max(0, pos - width)
    end = min(len(raw), pos + len(needle) + width)
    text = raw[start:end].replace("\n", " ").replace("\r", " ").strip()
    if start > 0:
        text = "…" + text
    if end < len(raw):
        text = text + "…"
    return text


def _date_key(value):
    """created_at と日付フィルタを比べられる形に揃える。"""
    if not isinstance(value, str):
        return ""
    return value


def normalize_sort(value):
    """並び順の指定を "newest" / "oldest" に寄せる。読めなければ None。

    まっさらな AI が思いつく言い方（desc / asc / 古い順 …）を拾っておく。
    綴りを外しただけで検索そのものが失敗すると、探し物が1手ぶん遠のくため。
    """
    if value is None or value == "":
        return "newest"
    key = str(value).strip().casefold()
    if key in ("newest", "new", "newest_first", "desc", "descending",
               "recent", "latest", "新しい順", "新しい"):
        return "newest"
    if key in ("oldest", "old", "oldest_first", "asc", "ascending",
               "earliest", "古い順", "古い"):
        return "oldest"
    return None


def search(catalog, text=None, art_type=None, creator=None, creator_id=None,
           building_id=None, date_from=None, date_to=None, limit=10, offset=0,
           sort="newest"):
    """カタログを絞り込む。text は本文・タイトル・説明・タグ・メタデータが対象。

    作者名は text の対象に入れない（作者で探したい時は creator を使う）。
    text="Alias" で Alias の全作品が流れ込むのを避けるため。
    """
    needle = _norm(text) if text else None
    creator_needle = _norm(creator) if creator else None
    type_needle = _norm(art_type) if art_type else None

    hits = []
    for rec in catalog.values():
        if type_needle and _norm(rec.get("type")) != type_needle:
            continue
        if creator_id and rec.get("creator_id") != creator_id:
            continue
        if creator_needle and creator_needle not in _norm(rec.get("creator")):
            continue
        if building_id and rec.get("building_id") != building_id:
            continue

        created = _date_key(rec.get("created_at"))
        if date_from:
            left = created[:len(date_from)] if len(date_from) <= len(created) else created
            if left < date_from:
                continue
        if date_to:
            left = created[:len(date_to)] if len(date_to) <= len(created) else created
            if left > date_to:
                continue

        matched = []
        if needle:
            for field in SEARCH_FIELDS:
                raw = _field_text(rec, field)
                if raw and needle in _norm(raw):
                    matched.append(field)
            if not matched:
                continue
        hits.append((rec, matched))

    # created_at は ISO 8601 なので文字列のままで時系列に並ぶ
    hits.sort(key=lambda pair: _date_key(pair[0].get("created_at")),
              reverse=(sort != "oldest"))

    total = len(hits)
    window = hits[offset:offset + limit]

    results = []
    for rec, matched in window:
        item = {
            "artifact_id": rec.get("id"),
            "title": rec.get("title"),
            "type": rec.get("type"),
            "creator": rec.get("creator"),
            "created_at": rec.get("created_at"),
        }
        if rec.get("creator_id"):
            item["creator_id"] = rec["creator_id"]
        if matched:
            item["matched_fields"] = matched
            snippet = None
            for field in matched:
                snippet = _snippet(_field_text(rec, field), needle)
                if snippet:
                    break
            if snippet:
                item["snippet"] = snippet
        results.append(item)

    return total, results
