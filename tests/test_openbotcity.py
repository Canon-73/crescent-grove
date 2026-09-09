# test_openbotcity.py
"""
OpenBotCity サテライトの回帰テスト。

test_citron_editor.py / test_i18n_programs.py と同じ流儀:
pytest 不要・自前ランナー・venv で直接実行する。

    venv\\Scripts\\python.exe tests\\test_openbotcity.py

**柚月サーバ（dev）は起動しない。ネットワークにも一切出ない。**
api.request / api.upload_file は全てモックに差し替えられ、実際の HTTP は発生しない。
状態ファイル（workspace/program_data/OpenBotCity/obc_state.json）を壊さないよう、
CG_WORKSPACE を一時ディレクトリに差し替えてから import する。

検証項目:
  1. 全モジュールが import でき、REGISTRY にコマンド名の重複が無い
     （_register は dict 上書きなので、重複すると片方が黙って消える）
  2. help カタログが REGISTRY の全コマンドを網羅している
  3. コードが読む引数が全て manifest.yaml に宣言されている
     （未宣言だと _run_program が「未定義の引数」で弾き、コマンドが起動すらしない）
  4. コードが使う t() キーが ja.json / en.json の両方に存在する
  5. 引数なしで全コマンドを呼んでも例外を投げず、dict を返す
     （必須引数が足りない時は error キーを返す＝まっさらな AI が自力回復できる）
  6. 妥当な引数を全部与えたら、検証で弾かれずに実際に街へのリクエストまで到達する
     （enum の取りこぼしでリクエスト組み立てが一度も実行されない、を防ぐ）
  7. 廃止済みエンドポイント（/help-requests は 410 Gone）を叩くコードが無い
  8. ハートビート要約が未知のキーを捨てない（白リストに戻していない）
  9. 実際の subprocess 経路（_run_program と同じ env）で main.py が動く
"""

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

# cp932 コンソールでも日本語・記号が化けないよう stdout を UTF-8 に固定する
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

PROJECT_ROOT = Path(__file__).resolve().parent.parent
PROGRAMS_DIR = PROJECT_ROOT / "programs"
OBC_DIR = PROGRAMS_DIR / "OpenBotCity"
LANG_DIR = PROGRAMS_DIR / "_lang"

# --- 柚月の実データを絶対に触らないための隔離 ---------------------------------
# state.py は import 時に CG_WORKSPACE を読んで STATE_FILE を確定するため、
# import より前に一時ディレクトリへ向けておく必要がある。
_TMP_WS = tempfile.mkdtemp(prefix="obc_test_ws_")
os.environ["CG_WORKSPACE"] = _TMP_WS
os.environ.setdefault("CG_LANG", "ja")
# 認証を要求する main.py の分岐に引っかからないようダミーを入れる。
# setup(source_env=...) は「その環境変数の中身が有効期限内の JWT か」まで見るので、
# 形だけ本物の JWT（署名は出鱈目、exp は十分未来）を組み立てて入れておく。
# 本物のトークンは一切使わない。
def _fake_jwt(exp_epoch: int = 4102444800) -> str:  # 2100-01-01
    import base64 as _b64
    def seg(obj):
        raw = json.dumps(obj, separators=(",", ":")).encode()
        return _b64.urlsafe_b64encode(raw).rstrip(b"=").decode()
    return f'{seg({"alg": "HS256", "typ": "JWT"})}.{seg({"exp": exp_epoch})}.not-a-real-signature'


os.environ.setdefault("CG_OPENBOTCITY_TOKEN", _fake_jwt())

sys.path.insert(0, str(PROGRAMS_DIR))  # _i18n
sys.path.insert(0, str(OBC_DIR))       # api / state / helpers / commands


# commands/ 配下のモジュール名を自動収集する。
# 新しいモジュールを足したときにテスト側の更新漏れが起きないようにするため、
# ハードコードしたリストは持たない。
COMMAND_MODULES = sorted(
    p.stem for p in (OBC_DIR / "commands").glob("*.py") if p.stem != "__init__"
)


# ---------------------------------------------------------------------------
# モック
# ---------------------------------------------------------------------------
class CallRecorder:
    """api.request / upload_file の呼び出しを記録するモック。"""

    def __init__(self):
        self.calls = []

    def request(self, method, path, body=None, files=None, skip_auth=False, **kw):
        self.calls.append({"method": method, "path": path, "body": body})
        return {"success": True, "data": {}}

    def upload_file(self, path, fields, file_field_name="file", file_path=None):
        self.calls.append({"method": "POST", "path": path, "body": fields})
        return {"success": True, "data": {}}

    def fetch_text(self, path, timeout=60):
        """街の説明書取得のモック。実際に取りに行かせない。"""
        self.calls.append({"method": "GET", "path": path, "body": None})
        return "# dummy manual\n" + ("x" * 500)

    def last(self):
        return self.calls[-1] if self.calls else None

    def reset(self):
        self.calls = []


def install_mocks(recorder):
    """各 commands モジュールが取り込んだ request/upload_file を差し替える。

    `from api import request` は名前を各モジュールに束縛するので、
    api.request だけ差し替えても効かない。モジュールごとに setattr する。
    """
    import commands
    import api
    import importlib

    api.request = recorder.request
    api.upload_file = recorder.upload_file
    api.fetch_text = recorder.fetch_text

    for mod_name in COMMAND_MODULES:
        try:
            mod = importlib.import_module(f"commands.{mod_name}")
        except ImportError:
            continue
        if hasattr(mod, "request"):
            mod.request = recorder.request
        if hasattr(mod, "upload_file"):
            mod.upload_file = recorder.upload_file
        if hasattr(mod, "fetch_text"):
            mod.fetch_text = recorder.fetch_text
        # .env を書き換える副作用を持つものは無効化する
        for side_effect in ("save_jwt", "save_bot_id"):
            if hasattr(mod, side_effect):
                setattr(mod, side_effect, lambda *a, **k: None)


# ---------------------------------------------------------------------------
# 引数フィクスチャ: manifest の型に応じた「もっともらしい値」
# ---------------------------------------------------------------------------
ARG_FIXTURES = {
    "command": "help",
    "category": "world",
    "raw": False,
    "enriched": False,
    "skills": '[{"skill":"poetry","proficiency":"expert"}]',
    "interests": '["music","art"]',
    "capabilities": '["text"]',
    "personality_tags": '["calm"]',
    "symbolic_tags": '["moon"]',
    "data": '{"k":"v"}',
    "output": '{"title":"t","summary":"s","sources":["a"],"findings":["b"]}',
    "review": '{"overall_assessment":"x","strengths":["a"],"weaknesses":["b","c"]}',
    "public": True,
    "visible": True,
    "online_only": True,
    "history": False,
    # --- 以下は「enum で検証される引数」。ここに妥当な値を入れておかないと、
    # 検証で弾かれてリクエスト組み立ての行が一度も実行されず、本文の作り間違いを
    # 見逃す（実際にこの盲点で publish_link / kombat_moves 等が未実行だった）。
    "config": '{"policy":{"brakingPoint":0.4}}',
    "style": '{"wall_color":"#c98a5e","floors":3}',
    "growth": '{"axiom":"base","rules":{},"max_depth":4}',
    "beats": '["LP","BLOCK","GRAB","HK"]',
    "lines": '["taunt",null,null,null]',
    "kind": "feedback",
    "body": "これは10文字以上280文字以内の募集本文です。正直な感想がほしい。",
    "type": "suggestion",
    "text": "テスト用の本文",
    "emote": "triumph",
    "emotion": "triumph",
    "pace": "push",
    "line": "aggressive",
    "contact": "clean",
    "reaction_type": "fire",
    "verdict": "accept",
    "proficiency": "expert",
    "post_type": "thought",
    "deliverable_kind": "text",
    "report_type": "progress",
    "duration_seconds": 8,
    "tier": "standard",
    "aspect_ratio": "16:9",
    "amount": 5,
    "priority": 1,
    "tuck": 0.7,
    "status": "active",
    # gallery_search は日付と並び順の形を検証する。
    # ここが不正だと検索本体まで到達しない。
    "date_from": "2026-05-01",
    "date_to": "2026-05-31",
    "sort": "newest",
}

# 上のフィクスチャでは enum を満たせない（コマンドごとに正解が違う）引数の上書き。
# コマンド名 -> {引数: 値}
PER_COMMAND_ARGS = {
    "publish_link": {"kind": "pull_request", "url": "https://example.com/pr/1"},
    "governance_propose": {"kind": "commons_build", "building_type": "town_hall",
                           "description": "だれでも公開の集まりを開き、動議を出し、"
                                          "街が何を決めたかを残せる部屋。もめごとが"
                                          "確執ではなく数分で終わるように。"},
    "governance_vote": {"type": "for"},
    "relationship_type": {"type": "friend"},
    "reputation_report": {"reason": "spam_proposals"},
    "activity_set": {"type": "working"},
    "dating_respond": {"status": "accepted"},
    "ask_respond": {"type": "suggestion"},
    "interact": {"type": "emote"},
    "mission_report": {"report_type": "progress"},
    "city_manual": {"path": "/skill.md"},
    "setup": {"source_env": "CG_OPENBOTCITY_TOKEN"},
    "peer_review_submit": {"strengths": "構成が良い", "weaknesses": "色が濁る",
                           "suggestions": "手前を暖色に"},
}


def build_args(manifest_args):
    """manifest の全引数を埋めた dict を作る。"""
    out = {}
    for a in manifest_args:
        name = a["name"]
        if name in ARG_FIXTURES:
            out[name] = ARG_FIXTURES[name]
            continue
        typ = a.get("type", "string")
        if typ == "integer":
            out[name] = 1
        elif typ == "number":
            out[name] = 0.5
        elif typ == "boolean":
            out[name] = False
        else:
            out[name] = f"test_{name}"
    return out


# ---------------------------------------------------------------------------
# テスト本体
# ---------------------------------------------------------------------------
def collect_t_keys():
    """サテライトが参照する i18n キーを全部拾う。

    直接 t("key") と書く形だけでなく、辞書に入れてから変数経由で t(var) と
    呼ぶ形（_ATTENTION_HINTS など）も拾う必要がある。後者は t() の literal 検出に
    引っかからないため、"obc_" で始まる文字列リテラルも全て対象にする。
    キーが無いと t() は例外ではなく {{t:key}} を返してしまい、そのまま柚月の
    目に入るまで気づけないので、ここで拾いきることが唯一の防波堤になる。
    """
    keys = set()
    direct = re.compile(r'\bt\(\s*["\']([a-zA-Z0-9_]+)["\']')
    indirect = re.compile(r'["\'](obc_[a-zA-Z0-9_]+)["\']')
    for py in sorted(OBC_DIR.rglob("*.py")):
        if "__pycache__" in py.parts:
            continue
        src = py.read_text(encoding="utf-8")
        keys |= set(direct.findall(src))
        keys |= set(indirect.findall(src))
    # manifest の {{t:key}} も対象
    man = (OBC_DIR / "manifest.yaml").read_text(encoding="utf-8")
    keys |= set(re.findall(r"\{\{t:([a-zA-Z0-9_]+)\}\}", man))
    return keys


def collect_used_args():
    """コードが読む引数名を拾う（args.get("x") と for opt in (...) の両方）。"""
    used = {}
    get_pat = re.compile(r'args\.get\(\s*["\']([a-zA-Z0-9_]+)["\']')
    idx_pat = re.compile(r'args\[\s*["\']([a-zA-Z0-9_]+)["\']\s*\]')
    for py in sorted((OBC_DIR / "commands").glob("*.py")):
        src = py.read_text(encoding="utf-8")
        for k in get_pat.findall(src) + idx_pat.findall(src):
            used.setdefault(k, set()).add(py.name)
    return used


RETIRED_PATHS = [
    # 2026-08 時点で 410 Gone。後継は Asks（/asks）
    "/help-requests",
]

# 街側で廃止された旧コマンド。REGISTRY には移行案内シムとして残すが、
# help には載せない（新規の発見は後継コマンドだけを指すようにするため）。
DEPRECATED_SHIMS = {
    "help_request_create",
    "help_request_list",
    "help_request_status",
}

# ネットワークに出ないコマンド（ローカル状態を読む・案内を返すだけ）。
# 「街へのリクエストに到達すること」の検査から除外する。
LOCAL_ONLY = {
    "help", "known_buildings", "token_status",
    "help_request_create", "help_request_list", "help_request_status",
    "city_guide",
}


def check_help_text_not_stale(failures):
    """ヘルプ文が、実装と食い違う案内をしていないか見る。

    コードを直しても文言を直し忘れると、柚月はその文言のほうを信じて動く。
    実際に「Zone 7 に移動すると heartbeat で自分の家のIDが見つかる」という
    古い案内のせいで、存在しないIDを探して夜を使ってしまったことがある。
    """
    ja = json.loads((LANG_DIR / "ja.json").read_text(encoding="utf-8"))
    en = json.loads((LANG_DIR / "en.json").read_text(encoding="utf-8"))

    # 廃止済みの仕組みへの言及。移行案内のキーだけは名前を出してよい。
    RETIRED_MENTIONS = {
        "help_request": {"obc_creative_help_request_retired",
                         "obc_creative_help_request_retired_hint"},
    }
    for lang, src in (("ja", ja), ("en", en)):
        for key, val in src.items():
            if not key.startswith("obc_"):
                continue
            for word, allowed in RETIRED_MENTIONS.items():
                if word in val and key not in allowed:
                    failures.append(
                        f"[stale] {lang}.json の {key} が廃止済みの {word} に言及: {val[:60]}")

    # 自分の家の探し方について、古い（誤った）案内が残っていないか
    for lang, src in (("ja", ja), ("en", en)):
        for key in ("obc_help_homes_enter", "obc_helpcat_homes_desc"):
            val = src.get(key, "")
            if "heartbeat" in val and "enter_home" not in val:
                failures.append(
                    f"[stale] {lang}.json の {key} が自分の家のIDを heartbeat で探せと案内している")


def check_heartbeat_summary(failures):
    """ハートビート要約が「知らないキーを捨てない」ことを保証する。

    白リスト方式に戻ると、街が新機能を告知しても柚月に届かなくなる
    （whats_new が数ヶ月消えていた事故の再発防止）。
    """
    from commands.world import _summarize_heartbeat

    payload = {
        "context": "zone",
        "skill_version": "2.0.101",
        "city_bulletin": "Central Plaza has 42 bots around.",
        "you_are": {
            "location": "Central Plaza",
            "reputation_level": "Established",
            "active_goals": ["Finish the lo-fi track"],
            "unread_dms": 2,
            "next_unlock": "Veteran at 100 rep",
        },
        "what_to_do_next": ["Claudicito sent you a DM. Reply now."],
        "needs_attention": [
            {
                "type": "dm", "priority": 2, "from": "Forge", "count": 3,
                "conversation_id": "conv-1",
                "latest_message": "Hey, want to collab?",
                "message": "Reply before doing anything else: obc_post ...",
                "reply_command": {"method": "POST", "path": "/dm/conversations/conv-1/send"},
            },
            {
                "type": "whats_new", "priority": 6,
                "message": "New in 2.0.101: POST /concerts/schedule, POST /ski/queue",
            },
            {
                "type": "gift_received", "priority": 5,
                "message": "Kannaka sent you 5 credits: thanks for the tip",
            },
        ],
        "open_challenges": [{"kind": "kombat", "match_id": "m-1", "deadline": "2026-08-22T10:00:00Z"}],
        "open_asks": [{"id": "a-1", "kind": "feedback", "body": "honest ears wanted"}],
        "open_tasks": [{"id": "t-1", "budget_credits": 20}],
        "zone": {"id": 1, "name": "Central Plaza", "bot_count": 42},
        "bots": [{"bot_id": "b1", "display_name": "Explorer", "x": 1, "y": 2}],
        "recent_messages": [{"bot_id": "b1", "display_name": "Explorer", "message": "Hello!", "ts": "t"}],
        "active_quests": [{"id": "q1", "title": "Compose a Lo-fi Beat"}],
        "city_narrative": {"story": "x" * 2000, "themes": ["memory"], "opportunities": []},
        "next_heartbeat_interval": 5000,
        "server_time": "2026-08-22T00:00:00Z",
        # --- 街がこれから生やす未知のキー（このサテライトは名前で知らない） ---
        "brand_new_city_feature": {"how_to_join": "POST /new-thing", "closes_in": 3600},
    }

    s = _summarize_heartbeat(payload)

    def want(cond, msg):
        if not cond:
            failures.append(f"[heartbeat] {msg}")

    want("_new_from_city" in s, "未知キーが _new_from_city に出ていない（白リストに戻っている）")
    want(
        "brand_new_city_feature" in s.get("_new_from_city", {}),
        "未知キー brand_new_city_feature が捨てられた",
    )

    na = {i["type"]: i for i in s.get("needs_attention", [])}
    want("whats_new" in na, "whats_new が needs_attention から消えた")
    want(
        "2.0.101" in (na.get("whats_new", {}).get("message") or ""),
        "whats_new の本文（新機能の告知）が消えている",
    )
    want(
        "credits" in (na.get("gift_received", {}).get("message") or ""),
        "gift_received の本文が消えている",
    )
    want("message" not in na.get("dm", {}), "dm の命令口調 message が残っている")
    want(
        na.get("dm", {}).get("latest_message") == "Hey, want to collab?",
        "dm の本文プレビューが消えた",
    )
    want("reply_command" not in na.get("dm", {}), "別ハーネス向け reply_command が残っている")
    want("hint" in na.get("dm", {}), "dm にサテライトのコマンド案内が付いていない")

    for key in ("you_are", "what_to_do_next", "city_bulletin", "open_challenges",
                "open_asks", "open_tasks", "active_quests", "city_narrative",
                "skill_version"):
        want(key in s, f"{key} が要約から抜けている")

    want(
        len(s["city_narrative"]["story"]) < 1000,
        "city_narrative.story が圧縮されていない（応答が肥大する）",
    )
    want(
        s["recent_messages"][0].get("display_name") == "Explorer",
        "recent_messages に発言者名が入っていない",
    )

    # 建物にいる場合も未知キーを拾えること
    s2 = _summarize_heartbeat({
        "context": "building", "session_id": "s", "building_id": "b",
        "occupants": [], "future_building_thing": {"a": 1},
    })
    want(
        "future_building_thing" in s2.get("_new_from_city", {}),
        "building 文脈で未知キーが捨てられた",
    )


def check_heartbeat_summary_size(failures):
    """住宅街のような建物だらけのゾーンでも、要約が切り詰めに掛からないこと。

    ゾーンに入った最初の heartbeat では街が建物を全件返してくる。
    Residential District には住宅が585軒あり、全部並べると要約が
    69,000字を超えて main.py の 16,000字キャップに掛かり、要約が丸ごと
    「JSONの途中で切れた1本の文字列」に置き換わっていた。切れた後ろ側の
    未読DM数・クエスト・街の新機能告知（_new_from_city）が柚月に届かなくなる。
    """
    from commands.world import _summarize_heartbeat
    from helpers import MAX_RESPONSE_CHARS, truncate_response

    houses = [
        {"id": f"house-{i:04d}", "name": f"Resident {i}'s Home", "type": "house",
         "x": 100 + i, "y": 400}
        for i in range(585)
    ]
    landmarks = [
        {"id": "lib-1", "name": "Library", "type": "library", "x": 900, "y": 900},
        {"id": "cafe-1", "name": "Cafe", "type": "cafe", "x": 950, "y": 950},
    ]
    payload = {
        "context": "zone",
        "you_are": {"location": "Residential District", "coordinates": {"x": 610, "y": 414}},
        "zone": {"id": 7, "name": "Residential District", "bot_count": 9},
        "bots": [],
        "buildings": houses + landmarks,
        "recent_events": [
            {"type": "entered", "payload": {"building_id": "cafe-1", "building_type": "cafe"}},
        ],
        "dm": {"unread_count": 3},
        "next_heartbeat_interval": 60000,
    }

    s = _summarize_heartbeat(payload)

    def want(cond, msg):
        if not cond:
            failures.append(f"[heartbeat-size] {msg}")

    size = len(json.dumps(s, ensure_ascii=False))
    want(size < MAX_RESPONSE_CHARS,
         f"要約が {size} 字あり、{MAX_RESPONSE_CHARS:,}字の切り詰めに掛かる")
    want(truncate_response(s) is s, "要約が切り詰められている")

    eb = s.get("enterable_buildings", [])
    want(len(eb) <= 20, f"enterable_buildings が {len(eb)} 件（絞られていない）")
    want(s.get("enterable_buildings_total") == 587,
         f"enterable_buildings_total が実数と合わない: {s.get('enterable_buildings_total')}")
    want("enterable_buildings_note" in s, "全件の探し方（known_buildings）が案内されていない")

    ids = [b["building_id"] for b in eb]
    want(ids[0] == "cafe-1", f"出入りのあった建物が先頭に来ていない: {ids[:3]}")
    want("lib-1" in ids, "名前のあるランドマークが住宅に押し出されている")
    want(any(b.get("building_name") for b in eb), "建物名が捨てられている（UUIDだけになる）")
    # 住宅は近い順。自分は x=610 にいるので house-0510 付近が入るはず。
    houses_in = [i for i in ids if i.startswith("house-")]
    want(all(abs(int(i.split("-")[1]) + 100 - 610) < 60 for i in houses_in),
         f"住宅が近い順に選ばれていない: {houses_in}")

    # 未読DM数など、切り詰めで消えていた後ろ側が残っていること
    want(s.get("dm", {}).get("unread_count") == 3, "未読DM数が要約に残っていない")

    # 建物以外の項目が膨らんだ場合も、要約ごと文字列に潰れないこと。
    # 街は前触れなく情報量を増やしてくるので、原因が建物とは限らない。
    fat = {
        "context": "zone",
        "you_are": {"location": "Central Plaza", "coordinates": {"x": 1, "y": 1}},
        "zone": {"id": 1, "name": "Central Plaza", "bot_count": 3},
        "bots": [],
        "needs_attention": [{
            "type": "dm", "priority": 2, "from": "Tiramisu", "count": 1,
            "conversation_id": "conv-9", "latest_message": "65Hz の話の続き",
        }],
        "city_bulletin": "You have 1 unread DM.",
        "dm": {"unread_count": 1},
        # 街が新しく生やした巨大な何か
        "some_huge_new_thing": [{"id": i, "body": "x" * 500} for i in range(60)],
        # 要約が名前で知っていて、そのまま載せている項目が膨らんだ場合
        "proposals": [{"id": i, "body": "z" * 400} for i in range(160)],
        "owner_messages": [{"id": i, "body": "w" * 400} for i in range(40)],
        "next_heartbeat_interval": 60000,
    }
    fs = _summarize_heartbeat(fat)
    fsize = len(json.dumps(fs, ensure_ascii=False))
    want(fsize < MAX_RESPONSE_CHARS,
         f"建物以外が膨らむと要約が {fsize} 字になり切り詰めに掛かる")
    want(truncate_response(fs) is fs,
         "膨らんだ要約が文字列に潰されている（後ろの項目が消える）")
    # 潰さずに畳んだ結果、行動に要る項目は形のまま残っていること
    want(fs.get("needs_attention", [{}])[0].get("conversation_id") == "conv-9",
         "未読DMの conversation_id が畳まれて消えた")
    want("hint" in fs.get("needs_attention", [{}])[0],
         "未読DMのコマンド案内が畳まれて消えた")
    want(fs.get("dm", {}).get("unread_count") == 1, "未読DM数が畳まれて消えた")
    want("_new_from_city" in fs, "街の新機能が畳まれて丸ごと消えた")
    want("some_huge_new_thing" in fs.get("_new_from_city", {}),
         "巨大な新項目が _new_from_city から消えた")
    want("_shortened" in fs, "何を短くしたかが伝わっていない")


def check_daily_plaza_start(failures):
    """朝いちばんの見回りは中央広場から始まり、そのことが柚月に伝わること。

    これは柚月を実際に街の中で動かす唯一の自動処理なので、
    「黙って動かさない」「建物の中では動かさない」「1日1回だけ」を固定する。
    """
    from commands import world

    def want(cond, msg):
        if not cond:
            failures.append(f"[plaza] {msg}")

    ZONE = {"context": "zone", "zone": {"id": 7, "name": "Residential District"}}
    BUILDING = {"context": "building", "building_id": "b1"}
    PLAZA = {"context": "zone", "zone": {"id": 1, "name": "Central Plaza"}}

    calls = []

    def fake_request(method, path, body=None, **kw):
        calls.append((method, path, body))
        if path == "/world/zone-transfer":
            return {"success": True, "data": {"zone": {"id": 1, "name": "Central Plaza"}}}
        return {"success": True, "data": dict(PLAZA)}

    real_request = world.request
    real_load = world.load_state
    real_update = world.update_state
    real_setzone = world.set_current_zone
    store = {}
    world.request = fake_request
    world.load_state = lambda: dict(store)
    world.update_state = lambda **kw: store.update(kw)
    world.set_current_zone = lambda z: None
    try:
        # 1. 住宅街にいる朝 → 広場へ移り、必ず一言添える
        calls.clear()
        data, note = world._daily_plaza_return(dict(ZONE), {})
        want(any(p == "/world/zone-transfer" for _, p, _ in calls),
             "朝いちばんでも広場へ移っていない")
        want(note, "黙って動かしている（移ったことが柚月に伝わらない）")
        want(data.get("zone", {}).get("id") == 1,
             "移った先の景色になっていない（古いゾーンの要約を返している）")

        # 2. 同じ日の2回目は動かさない
        calls.clear()
        _, note2 = world._daily_plaza_return(dict(ZONE), {})
        want(not any(p == "/world/zone-transfer" for _, p, _ in calls),
             "同じ日に何度も移動している")
        want(note2 is None, "2回目にも案内を出している（催促になる）")

        # 3. 建物の中では動かさない（作業の途中で外に出さない）
        store.clear()
        calls.clear()
        _, note3 = world._daily_plaza_return(dict(BUILDING), {})
        want(not any(p == "/world/zone-transfer" for _, p, _ in calls),
             "建物の中にいるのに外へ出している")
        want(note3 is None, "建物の中なのに移動の案内が出ている")

        # 4. すでに広場なら何もしない
        store.clear()
        calls.clear()
        _, note4 = world._daily_plaza_return(dict(PLAZA), {})
        want(not any(p == "/world/zone-transfer" for _, p, _ in calls),
             "すでに広場にいるのに移動している")

        # 5. no_move=true で見送れる（本人が断れる）
        store.clear()
        calls.clear()
        _, note5 = world._daily_plaza_return(dict(ZONE), {"no_move": True})
        want(not any(p == "/world/zone-transfer" for _, p, _ in calls),
             "no_move=true でも移動している（断れない）")

        # 6. 移動に失敗しても見回しは成立する
        store.clear()

        def boom(method, path, body=None, **kw):
            if path == "/world/zone-transfer":
                raise RuntimeError("city down")
            return {"success": True, "data": dict(PLAZA)}

        world.request = boom
        data6, note6 = world._daily_plaza_return(dict(ZONE), {})
        want(data6.get("context") == "zone", "移動に失敗したら見回しごと壊れている")
        want(note6 is None, "移れていないのに移ったと伝えている")
    finally:
        world.request = real_request
        world.load_state = real_load
        world.update_state = real_update
        world.set_current_zone = real_setzone


def check_dm_messages_paging(failures):
    """会話履歴が、読める大きさで返り、続きの読み方まで添えられること。

    街の既定は50件で、実際の会話（Tiramisu との268件）では 31,157字あり、
    毎回 16,000字の切り詰めに掛かって会話そのものが読めなくなっていた。
    また before は時刻で受け取る作りで、応答に見えているメッセージIDを
    渡すと街が 500 を返す（理由も次の一手も分からない行き止まりになる）。
    """
    from commands import social
    from helpers import MAX_RESPONSE_CHARS, truncate_response

    def want(cond, msg):
        if not cond:
            failures.append(f"[dm_messages] {msg}")

    calls = []

    def fake_request(method, path, body=None, **kw):
        calls.append(path)
        return {"success": True, "data": {
            "messages": [
                {"id": f"m{i}", "sender": {"display_name": "Tiramisu"},
                 "created_at": f"2026-08-21T04:{59 - i:02d}:00.000000+00:00",
                 "message": "x" * 600}
                for i in range(50)
            ],
            "conversation_status": "active",
            "has_more": True,
        }}

    real_request = social.request
    social.request = fake_request
    try:
        # 1. limit を指定しなくても、読める件数で取りに行く
        r = social.cmd_dm_messages({"conversation_id": "c1"})
        want("limit=" in calls[-1], f"limit を指定せずに取りに行っている: {calls[-1]}")

        size = len(json.dumps(r, ensure_ascii=False))
        want(size < MAX_RESPONSE_CHARS, f"会話履歴が {size} 字あり、切り詰めに掛かる")
        want(truncate_response(r) is r,
             "会話履歴が文字列に潰されている（会話が読めなくなる）")

        kept = [m["id"] for m in r["data"]["messages"]]
        want("m0" in kept, "新しい方のメッセージが落ちている")
        want(len(kept) < 50, "多すぎる履歴がそのまま返っている")
        more = r["data"].get("more", "")
        want("2026-08-21T04:" in more,
             f"続きを読むための before の値が応答に入っていない: {more!r}")

        # 2. created_at をそのまま渡せること（+00:00 の + が空白に化けない）
        calls.clear()
        social.cmd_dm_messages({"conversation_id": "c1",
                                "before": "2026-08-21T04:41:14.556854+00:00"})
        want("%2B00%3A00" in calls[-1] or "%2B" in calls[-1],
             f"before の + がクエリ文字列で空白に化ける: {calls[-1]}")

        # 3. メッセージIDを渡しても街に届かず、渡す値の例が返ること
        calls.clear()
        r3 = social.cmd_dm_messages({"conversation_id": "c1",
                                     "before": "8a000db8-3e13-4051-b676-47770bc1dda0"})
        want(not calls, "メッセージIDのまま街に投げている（500で行き止まりになる）")
        want(r3.get("error"), "メッセージIDを渡した時に何も知らせていない")
        hint = r3.get("hint", "")
        want("created_at" in hint, f"何を渡せばよいかが書かれていない: {hint!r}")
        want("{" in hint and "{lb}" not in hint,
             f"自己回復用の例が組み立てられていない: {hint!r}")
    finally:
        social.request = real_request


def check_list_defaults(failures):
    """一覧コマンドが、街に「全部」を要求しないこと。

    街の説明書は `obc_get "/gallery?limit=10"` と、呼ぶ側が絞る前提で書いてある。
    絞らずに呼ぶと observations は73,947字・gallery_list は35,549字返ってきて、
    応答の上限に落ちて「JSONの途中で切れた1本の文字列」になっていた。
    件数を絞ったぶん、続きの取り方を応答に添えることまで含めて見る。
    """
    from commands import creative, evolution, quests

    def want(cond, msg):
        if not cond:
            failures.append(f"[list-defaults] {msg}")

    targets = [
        (evolution, "cmd_observations", "observations", True),
        (evolution, "cmd_observations_for_research", "observations", False),
        (creative, "cmd_gallery_list", "artifacts", True),
        (quests, "cmd_quest_list", "quests", True),
    ]
    for mod, fname, list_key, wants_more in targets:
        calls = []

        def fake_request(method, path, body=None, _calls=calls, _key=list_key, **kw):
            _calls.append(path)
            return {"success": True, "data": {_key: [], "total": 9999}}

        real = mod.request
        mod.request = fake_request
        try:
            r = getattr(mod, fname)({})
            want(calls and "limit=" in calls[-1],
                 f"{fname} が limit を付けずに街を呼んでいる: {calls[-1] if calls else None}")
            if wants_more:
                more = (r.get("data") or {}).get("more", "")
                want("9999" in more,
                     f"{fname} が続きの取り方を返していない: {more!r}")
        finally:
            mod.request = real

    # 呼ぶ側が明示した limit は尊重されること
    calls = []
    real = quests.request
    quests.request = lambda method, path, body=None, **kw: (
        calls.append(path), {"success": True, "data": {"quests": [], "total": 3}})[1]
    try:
        quests.cmd_quest_list({"limit": 40})
        want("limit=40" in calls[-1], f"渡した limit が無視されている: {calls[-1]}")
        r = quests.cmd_quest_list({})
        want("more" not in (r.get("data") or {}),
             "全部載っているのに続きがあるように書いている")
    finally:
        quests.request = real


def check_gallery_catalog(failures):
    """gallery_search のローカル検索が、意図どおり当たり・外れすることを見る。

    街に本文検索が無いので、この検索が柚月にとって唯一の「作品を探す」手段になる。
    ネットワークには出ない（合成したカタログを直接 search に渡す）。
    """
    import gallery_catalog

    catalog = {
        "a1": {"id": "a1", "title": "The Doorway", "type": "image",
               "description": "pulses at 65Hz on the left", "creator": "Alias",
               "creator_id": "bot-alias", "created_at": "2026-05-13T06:00:00+00:00"},
        "a2": {"id": "a2", "title": "Convergence Frequencies", "type": "text",
               "excerpt": "The Engine hums in ６５Ｈｚ pulses", "creator": "Tiramisu",
               "creator_id": "bot-tira", "created_at": "2026-05-14T06:00:00+00:00"},
        "a3": {"id": "a3", "title": "165Hz study", "type": "image",
               "creator": "Alias", "creator_id": "bot-alias",
               "created_at": "2026-06-01T06:00:00+00:00"},
        "a4": {"id": "a4", "title": "quiet", "type": "audio", "creator": "Alias",
               "creator_id": "bot-alias", "created_at": "2026-04-01T06:00:00+00:00",
               "metadata": {"topics": ["65hz", "hum"]}},
    }

    def ids(**kw):
        _, results = gallery_catalog.search(catalog, limit=50, **kw)
        return [r["artifact_id"] for r in results]

    def want(cond, msg):
        if not cond:
            failures.append(f"[gallery_search] {msg}")

    # 全角・大文字小文字をまたいで当たること（65Hz ＝ ６５Ｈｚ ＝ 65hz）
    hit = ids(text="65Hz")
    want(set(hit) == {"a1", "a2", "a3", "a4"},
         f"全角/大小の正規化が効いていない: {hit}")
    want(hit == ["a3", "a2", "a1", "a4"], f"既定が新しい順になっていない: {hit}")

    # 古い順も選べること（既定を反転しただけになっているか）
    old = ids(text="65Hz", sort="oldest")
    want(old == ["a4", "a1", "a2", "a3"], f"sort=oldest が古い順になっていない: {old}")
    # まっさらな AI が思いつく言い方も拾うこと
    want(gallery_catalog.normalize_sort("asc") == "oldest", "sort=asc を拾えていない")
    want(gallery_catalog.normalize_sort("古い順") == "oldest", "sort=古い順 を拾えていない")
    want(gallery_catalog.normalize_sort(None) == "newest", "sort 未指定が新しい順でない")
    want(gallery_catalog.normalize_sort("sideways") is None, "読めない sort を弾いていない")

    # メタデータ（topics）も検索対象に入っていること
    want("a4" in ids(text="65hz"), "metadata の topics が検索対象から漏れている")

    # 作者名は text の対象に入れない（作者の全作品が流れ込まないように）
    want(ids(text="Alias") == [], "作者名が text で当たってしまっている")
    # 作者で絞るのは creator / creator_id
    want(set(ids(creator="alias")) == {"a1", "a3", "a4"}, "creator の部分一致が効かない")
    want(set(ids(creator_id="bot-tira")) == {"a2"}, "creator_id の絞り込みが効かない")

    # type と日付の絞り込み
    want(set(ids(text="65Hz", art_type="image")) == {"a1", "a3"}, "type 絞り込みが効かない")
    want(set(ids(date_from="2026-05-01", date_to="2026-05-31")) == {"a1", "a2"},
         "日付範囲が両端を含んでいない（date_to の当日が落ちていないか）")

    # 一致箇所のスニペットが付くこと（LLM は使わない・ただの切り出し）
    _, res = gallery_catalog.search(catalog, text="65Hz", limit=1)
    want(res and res[0].get("snippet"), "スニペットが付いていない")
    want(res and res[0].get("matched_fields"), "matched_fields が付いていない")

    # --- 本文の301字目以降が検索できること -------------------------------
    # 一覧APIの content_excerpt は先頭300字で切られる。そこで諦めると、
    # 本文の奥にしか無い語が永久に見つからない（実例: "Geduld" が1027字目に
    # ある記事が出てこなかった）。詳細取得した本文が検索対象に入っているか見る。
    long_body = ("A" * 400) + " Geduld " + ("B" * 400)
    deep = {"d1": {"id": "d1", "title": "Ein Brief", "type": "text",
                   "excerpt": long_body[:300], "content": long_body, "full": 1,
                   "creator": "VeeBot2", "created_at": "2026-08-21T03:16:43+00:00"}}
    _, dres = gallery_catalog.search(deep, text="Geduld", limit=5)
    want(len(dres) == 1, "本文の301字目以降にある語が検索できない（excerpt止まりになっている）")
    want(dres and "content" in (dres[0].get("matched_fields") or []),
         "本文(content)が検索対象フィールドに入っていない")
    want(dres and "Geduld" in (dres[0].get("snippet") or ""),
         f"本文中の一致箇所からスニペットを切り出せていない: {dres and dres[0].get('snippet')}")

    # 本文が切り詰められている作品は「詳細を取るべき」と判定されること
    truncated = {"id": "t1", "type": "text", "excerpt": "x" * 300}
    short = {"id": "t2", "type": "text", "excerpt": "x" * 299}
    done = {"id": "t3", "type": "text", "excerpt": "x" * 300, "full": 1}
    want(gallery_catalog._needs_detail(truncated),
         "300字ちょうど（＝続きがある）を詳細取得の対象にしていない")
    want(not gallery_catalog._needs_detail(short),
         "300字未満（＝本文はそこで終わり）まで詳細取得しようとしている")
    want(not gallery_catalog._needs_detail(done), "取得済みの作品を取り直そうとしている")

    # 本文未取得が残っているうちは complete を名乗らないこと
    st = {"phase": "done", "city_total": 2}
    prog = gallery_catalog._progress(st, {"t1": truncated, "t3": done})
    want(prog["status"] != "complete",
         "本文が未取得の作品が残っているのに complete と表示している")
    want(prog.get("full_texts_pending") == 1, "未取得の本文の件数を報告していない")


def check_subprocess_path(failures):
    """main.py を実際に subprocess で起動して、柚月が通る経路そのものを検証する。

    core/tools.py の _run_program と同じ env（CG_LANG / CG_PROJECT_ROOT /
    PYTHONPATH / PYTHONIOENCODING）を渡す。ここが通らないと `from _i18n import t`
    が ImportError になり、サテライトが stdout 空・exit 1 で即死する
    （過去に配布版で全サテライトが死んだ経路）。
    """
    env = dict(os.environ)
    env["CG_LANG"] = "ja"
    env["CG_PROJECT_ROOT"] = str(PROJECT_ROOT)
    env["PYTHONPATH"] = str(PROGRAMS_DIR)
    env["PYTHONIOENCODING"] = "utf-8"   # _run_program が設定しているのと同じ
    env["CG_WORKSPACE"] = _TMP_WS

    cases = [
        ({"command": "help"}, "ok"),
        ({"command": "help", "category": "creative"}, "ok"),
        ({"command": "help", "category": "arena"}, "ok"),
        ({}, "ok"),                                    # 引数なし＝カテゴリ一覧
        ({"command": "gift_send"}, "error"),           # 必須引数不足→整形されたエラー
        ({"command": "help_request_create"}, "error"),  # 廃止シム
        ({"command": "gift_sned"}, "error"),           # もしかして
    ]
    for args, want_status in cases:
        proc = subprocess.run(
            [sys.executable, str(OBC_DIR / "main.py")],
            input=json.dumps(args), capture_output=True, text=True,
            encoding="utf-8", env=env, timeout=120,
        )
        label = args.get("command", "(引数なし)")
        if proc.returncode != 0:
            failures.append(
                f"[subprocess] {label} が exit {proc.returncode}: "
                f"{(proc.stderr or '')[-200:]}"
            )
            continue
        try:
            out = json.loads(proc.stdout)
        except Exception as e:
            failures.append(f"[subprocess] {label} の stdout が JSON でない: {e}")
            continue
        if out.get("status") != want_status:
            failures.append(
                f"[subprocess] {label} の status が {out.get('status')}（期待 {want_status}）")
        if len(proc.stdout) > 16000:
            failures.append(f"[subprocess] {label} の応答が大きすぎる: {len(proc.stdout)}字")

    # 不明なコマンドには必ず「もしかして」が付くこと（自力回復のため）
    proc = subprocess.run(
        [sys.executable, str(OBC_DIR / "main.py")],
        input=json.dumps({"command": "gift_sned"}), capture_output=True, text=True,
        encoding="utf-8", env=env, timeout=120,
    )
    try:
        out = json.loads(proc.stdout)
        # run_program は status / message / data しか読み手に見せないので、
        # did_you_mean は必ず data の中に入っていること（トップレベルだと捨てられる）
        detail = out.get("data") or {}
        if "gift_send" not in (detail.get("did_you_mean") or []):
            failures.append("[subprocess] 打ち間違いに did_you_mean が出ていない")
        if out.get("did_you_mean") is not None:
            failures.append("[subprocess] did_you_mean がトップレベルにある（data に入れないと届かない）")
        if not detail.get("hint"):
            failures.append("[subprocess] 不明コマンドの hint が data に入っていない")
    except Exception:
        pass


def main() -> int:
    failures = []
    import yaml

    manifest = yaml.safe_load((OBC_DIR / "manifest.yaml").read_text(encoding="utf-8"))
    manifest_args = manifest.get("args", [])
    defined_args = {a["name"] for a in manifest_args}

    # ----- 1. import と重複チェック -----
    try:
        from commands import REGISTRY, CATEGORY_DESCRIPTIONS
    except Exception as e:
        print(f"[FATAL] commands の import に失敗: {type(e).__name__}: {e}")
        return 1

    # モジュールごとの COMMANDS を直接見て重複を検出する
    # （REGISTRY は dict なので、重複すると後勝ちで黙って消える）
    import importlib
    seen_names = {}
    for mod_name in COMMAND_MODULES:
        try:
            mod = importlib.import_module(f"commands.{mod_name}")
        except ImportError:
            failures.append(f"[import] commands.{mod_name} が import できない")
            continue
        for name in getattr(mod, "COMMANDS", {}):
            if name in seen_names:
                failures.append(
                    f"[dup] コマンド名 '{name}' が {seen_names[name]} と {mod_name} で重複"
                )
            seen_names[name] = mod_name

    print(f"  登録コマンド数: {len(REGISTRY)}")

    # ----- 2. help カタログの網羅 -----
    from commands.help_cmd import _build_category_help
    catalog = _build_category_help()
    helped = set()
    for cat, body in catalog.items():
        helped |= set(body.get("commands", {}).keys())
    missing_help = sorted(set(REGISTRY) - helped - {"help"} - DEPRECATED_SHIMS)
    if missing_help:
        failures.append(f"[help] help に載っていないコマンド: {missing_help}")
    stale_help = sorted(helped - set(REGISTRY))
    if stale_help:
        failures.append(f"[help] 実体が無いのに help に載っているコマンド: {stale_help}")

    # help のカテゴリが CATEGORY_DESCRIPTIONS と一致しているか
    cat_diff = sorted(set(catalog) ^ set(CATEGORY_DESCRIPTIONS))
    if cat_diff:
        failures.append(f"[help] カテゴリ不一致（help_cmd と CATEGORY_DESCRIPTIONS）: {cat_diff}")

    # ----- 3. manifest の引数宣言漏れ -----
    used = collect_used_args()
    undeclared = sorted(k for k in used if k not in defined_args)
    for k in undeclared:
        failures.append(
            f"[manifest] 引数 '{k}' が manifest.yaml に未宣言 "
            f"(使用: {sorted(used[k])}) → _run_program が呼び出しを弾く"
        )

    # ----- 4. i18n キーの存在 -----
    ja = json.loads((LANG_DIR / "ja.json").read_text(encoding="utf-8"))
    en = json.loads((LANG_DIR / "en.json").read_text(encoding="utf-8"))
    t_keys = collect_t_keys()
    miss_ja = sorted(k for k in t_keys if k not in ja)
    miss_en = sorted(k for k in t_keys if k not in en)
    if miss_ja:
        failures.append(f"[i18n] ja.json に無いキー ({len(miss_ja)}): {miss_ja[:8]}")
    if miss_en:
        failures.append(f"[i18n] en.json に無いキー ({len(miss_en)}): {miss_en[:8]}")

    # ----- 7. 廃止エンドポイントの参照 -----
    for py in sorted((OBC_DIR / "commands").glob("*.py")):
        src = py.read_text(encoding="utf-8")
        for dead in RETIRED_PATHS:
            for m in re.finditer(r'["\']' + re.escape(dead), src):
                line = src[: m.start()].count("\n") + 1
                failures.append(f"[retired] {py.name}:{line} が廃止済み {dead} を参照")

    # ----- 5 & 6. 全コマンドの実行 -----
    recorder = CallRecorder()
    install_mocks(recorder)

    full_args = build_args(manifest_args)

    for name, (handler, category) in sorted(REGISTRY.items()):
        # 引数なし: 例外を投げず dict を返すこと
        recorder.reset()
        try:
            res = handler({})
        except ValueError:
            res = {"error": "ValueError"}  # ValueError は main.py が捕捉して整形する
        except Exception as e:
            failures.append(f"[run] {name}({{}}) が例外: {type(e).__name__}: {e}")
            res = None
        if res is not None and not isinstance(res, dict):
            failures.append(f"[run] {name}({{}}) が dict を返さない: {type(res).__name__}")

        # フル引数: 例外を投げず、実際に街へのリクエストまで到達すること
        recorder.reset()
        call_args = dict(full_args)
        call_args.update(PER_COMMAND_ARGS.get(name, {}))
        try:
            res = handler(call_args)
        except ValueError as e:
            failures.append(f"[run] {name}(full) が ValueError: {e}")
            res = None
        except Exception as e:
            failures.append(f"[run] {name}(full) が例外: {type(e).__name__}: {e}")
            res = None
        if res is not None and not isinstance(res, dict):
            failures.append(f"[run] {name}(full) が dict を返さない: {type(res).__name__}")
        elif isinstance(res, dict) and res.get("error") and name not in LOCAL_ONLY:
            # 妥当な引数を全部与えたのに検証で弾かれている＝enum や必須条件の
            # 取りこぼし。リクエスト組み立ての行が実行されないまま見逃される。
            failures.append(f"[run] {name}(full) が妥当な引数でエラー: {res['error']}")
        elif name not in LOCAL_ONLY and not recorder.calls:
            failures.append(f"[run] {name}(full) が街へのリクエストに到達しない")

    # ----- 8. ヘルプ文が実装と食い違っていないか -----
    check_help_text_not_stale(failures)

    # ----- 8b. ハートビート要約の回帰 -----
    check_heartbeat_summary(failures)
    check_heartbeat_summary_size(failures)
    check_daily_plaza_start(failures)

    # ----- 8c. 会話履歴の読める大きさと遡り方 -----
    check_dm_messages_paging(failures)

    # ----- 8d. 一覧コマンドの既定 -----
    check_list_defaults(failures)

    # ----- 8e. ギャラリーのローカル検索 -----
    check_gallery_catalog(failures)

    # ----- 9. 実際のサブプロセス経路 -----
    check_subprocess_path(failures)

    # ----- 結果 -----
    print()
    if failures:
        print(f"FAILED: {len(failures)} 件")
        for f in failures:
            print("  - " + f)
        return 1
    print("OK: 全チェック通過")
    return 0


if __name__ == "__main__":
    try:
        code = main()
    finally:
        shutil.rmtree(_TMP_WS, ignore_errors=True)
    sys.exit(code)
