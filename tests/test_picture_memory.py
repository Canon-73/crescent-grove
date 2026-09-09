"""絵の記憶（見た絵 + そのとき思ったこと → フラッシュバック）のテスト。pytest 不要。

実行: venv\\Scripts\\python.exe tests\\test_picture_memory.py

検証するもの:
  1. 索引の記録（workspace 内はパスのまま／外からの絵は写す）
  2. 重みは「自分から見返した回数」。フラッシュバックで見せた分は数えない
  3. そのとき思ったことが絵に結びつく
  4. 消えた絵・今日見た絵は出さない。壊れた索引でも落ちない
  5. 3つの入口すべてが記録される（see_image / サテライト / チャット）
  6. 雑記帳と同じレールに乗る（サリアが文章と一緒に選ぶ）
  7. 絵が少ないうちはほとんど浮かばない条件
  8. サリアが絵を選んだら絵そのものが届く
サーバにも柚月の workspace にも触らない（一時ディレクトリを workspace にする）。
"""
import asyncio
import base64
import io
import json
import os
import subprocess
import sys
import tempfile
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.stdout.reconfigure(encoding="utf-8")

from PIL import Image  # noqa: E402

from core.i18n import init_i18n  # noqa: E402
init_i18n({"language": "ja"})

from core import picture_memory as pm  # noqa: E402
import core.tools as tools  # noqa: E402
from core.scheduler import Scheduler  # noqa: E402

PASSED = 0
FAILED = []


def check(name, cond, detail=""):
    global PASSED
    if cond:
        PASSED += 1
        print(f"  ok   {name}")
    else:
        FAILED.append(name)
        print(f"  FAIL {name} {detail}")


def new_ws():
    ws = tempfile.mkdtemp(prefix="cg_pic_ws_")
    os.makedirs(os.path.join(ws, "generated"), exist_ok=True)
    return ws


def png_bytes(size=(400, 300), color=(20, 90, 160)):
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, "PNG")
    return buf.getvalue()


def age_all(ws, days=3):
    """年齢ゲート（今日見た絵は出さない）を通すため、見た日を過去にずらす。"""
    from datetime import date, timedelta
    d = pm.load(ws)
    past = (date.fromisoformat(pm.get_logical_date()) - timedelta(days=days)).isoformat()
    for p in d["pictures"]:
        p["last_seen"] = past
        p["first_seen"] = past
    pm._save(ws, d)


print("[1] 索引の記録と絵の写し取り")
WS = new_ws()
open(os.path.join(WS, "generated", "a.png"), "wb").write(png_bytes())

pm.record_view(WS, "generated/a.png", image_bytes=png_bytes(), mime_type="image/png")
idx = pm.load(WS)
check("1件記録される", len(idx["pictures"]) == 1)
e = idx["pictures"][0]
check("workspace 内はパスをそのまま使う", e["path"] == "generated/a.png", e)
check("初回の views は 1", e["views"] == 1, e)
check("索引が workspace/memory 配下にできる",
      os.path.exists(os.path.join(WS, "memory", "pictures.json")))

ext_bytes = png_bytes((500, 500), (200, 40, 40))
pm.record_view(WS, "https://example.com/x.png", image_bytes=ext_bytes, mime_type="image/png")
e2 = pm.load(WS)["pictures"][1]
check("外の絵は memory/pictures/ へ写す", e2["path"].startswith("memory/pictures/"), e2)
copied = os.path.join(WS, e2["path"])
check("写した実体がある", os.path.exists(copied))
check("原本のまま（縮小しない）", Image.open(copied).size == (500, 500), Image.open(copied).size)

pm.record_view(WS, "https://example.com/x.png", image_bytes=ext_bytes, mime_type="image/png")
check("同じ絵を見返してもファイルは増えない",
      len(os.listdir(os.path.join(WS, "memory", "pictures"))) == 1)
check("見返すと views が増える", pm.load(WS)["pictures"][1]["views"] == 2)

print("[2] 重みは「自分から見返した回数」")
pm.record_view(WS, "https://example.com/x.png", deliberate=False,
               image_bytes=ext_bytes, mime_type="image/png")
check("フラッシュバックで見せた分は数えない", pm.load(WS)["pictures"][1]["views"] == 2)

WS2 = new_ws()
for name, n in (("generated/many.png", 10), ("generated/once.png", 1)):
    open(os.path.join(WS2, name), "wb").write(png_bytes())
    for _ in range(n):
        pm.record_view(WS2, name, image_bytes=png_bytes(), mime_type="image/png")
picks = [pm.pick(WS2, min_age_days=0)["source"] for _ in range(300)]
many = picks.count("generated/many.png")
check("見返した絵の方がよく浮かぶ", many > 200, f"{many}/300")
check("少ない方も浮かぶ（消えはしない）", many < 300, f"{many}/300")
check("除外した絵は選ばれない",
      pm.pick(WS2, exclude=["generated/many.png"], min_age_days=0)["source"]
      == "generated/once.png")

print("[3] そのとき思ったことが結びつく")
pm.record_thought(WS, ["generated/a.png"], "  この絵、思ったより静かだ。  ")
e = pm.load(WS)["pictures"][0]
check("思ったことが残る", e["thoughts"][-1]["text"] == "この絵、思ったより静かだ。", e["thoughts"])
check("日付が付く", bool(e["thoughts"][-1]["date"]))
pm.record_thought(WS, ["generated/a.png"], "この絵、思ったより静かだ。")
check("同じ言葉を二重登録しない", len(pm.load(WS)["pictures"][0]["thoughts"]) == 1)
for i in range(pm.MAX_THOUGHTS + 3):
    pm.record_thought(WS, ["generated/a.png"], f"{i}回目の感想")
check("thought は上限で頭打ち",
      len(pm.load(WS)["pictures"][0]["thoughts"]) == pm.MAX_THOUGHTS)
pm.record_thought(WS, ["generated/a.png"], "あ" * 900)
check("長い感想は切り詰める",
      len(pm.load(WS)["pictures"][0]["thoughts"][-1]["text"]) <= pm.MAX_THOUGHT_CHARS + 1)
pm.record_thought(WS, ["generated/nope.png"], "無い絵への感想")
check("知らない絵には何も起きない", len(pm.load(WS)["pictures"]) == 2)

print("[4] 出さない条件と、壊れていても落ちないこと")
check("絵が無ければ None", pm.pick(new_ws()) is None)
check("今日見たばかりの絵は出さない（思い出ではない）", pm.pick(WS) is None)
check("min_age_days=0 なら出る", pm.pick(WS, min_age_days=0) is not None)
WS3 = new_ws()
os.makedirs(os.path.join(WS3, "memory"), exist_ok=True)
open(os.path.join(WS3, "memory", "pictures.json"), "w", encoding="utf-8").write("{壊れた")
check("壊れた索引は空として読む", pm.load(WS3) == {"pictures": []})
pm.record_view(WS3, "generated/b.png")     # 実体が無い＝写せない
check("実体が無くても記録はできる", len(pm.load(WS3)["pictures"]) == 1)
check("実体の無い絵は選ばれない", pm.pick(WS3, min_age_days=0) is None)
pm.record_view(WS, "data:image/png;base64,AAAA")
check("data URL は記録しない", len(pm.load(WS)["pictures"]) == 2)

print("[5] 3つの入口すべてが記録される")
WS4 = new_ws()
img = png_bytes((300, 300), (10, 200, 10))
open(os.path.join(WS4, "generated", "s.png"), "wb").write(img)

asyncio.run(tools.handle_see_image(source="generated/s.png", workspace_path=WS4))
srcs = [p["source"] for p in pm.load(WS4)["pictures"]]
check("see_image で見た絵が記録される", "generated/s.png" in srcs, srcs)
check("context に see_image が入る", pm.load(WS4)["pictures"][0]["context"] == "see_image")

orig_run = tools.subprocess.run
tools.subprocess.run = lambda *a, **k: subprocess.CompletedProcess(
    a[0] if a else [], 0,
    stdout=json.dumps({"status": "ok", "data": {}, "image": "generated/s.png"}), stderr="")
try:
    tools._run_program("atelier", {"command": "help"}, WS4)
finally:
    tools.subprocess.run = orig_run
check("サテライト経由でも同じ絵として数える",
      len(pm.load(WS4)["pictures"]) == 1 and pm.load(WS4)["pictures"][0]["views"] == 2,
      pm.load(WS4)["pictures"])

from core.context import ContextBuilder  # noqa: E402

ctx = ContextBuilder.__new__(ContextBuilder)
ctx.memory = types.SimpleNamespace(workspace=WS4)
data_url = "data:image/png;base64," + base64.b64encode(img).decode()
ContextBuilder._collect_image_entries(ctx, data_url, None)
srcs = [p["source"] for p in pm.load(WS4)["pictures"]]
check("チャットの絵が received/ に保存され記録される",
      any(s.startswith("received/") for s in srcs), srcs)
check("agent が拾えるよう出所を置いていく",
      ctx._last_saved_image_sources and ctx._last_saved_image_sources[0].startswith("received/"),
      getattr(ctx, "_last_saved_image_sources", None))

before = len(os.listdir(os.path.join(WS4, "received")))
ContextBuilder._collect_image_entries(ctx, None, [
    {"name": "generated/s.png", "url": data_url, "source": "generated/s.png",
     "context": "flashback", "from_flashback": True}])
after = len(os.listdir(os.path.join(WS4, "received")))
check("浮かんだ絵は received/ に保存し直さない", before == after, (before, after))
check("浮かんだ絵は回数に数えない", pm.load(WS4)["pictures"][0]["views"] == 2)

print("[6] 選ばれた日付に紐づく（絵は自前の確率を持たない）")
age_all(WS2, days=100)                 # 100日前に見たことにする
pm.record_thought(WS2, ["generated/many.png"], "自分の姿。何度見ても落ち着かない")
age_all(WS2, days=100)
DAY = pm.load(WS2)["pictures"][0]["first_seen"]

sched = Scheduler.__new__(Scheduler)
sched.memory = types.SimpleNamespace(workspace=WS2)
sched._recent_picture_sources = []
FB = {"picture_candidates": 1, "picture_no_repeat": 5, "picture_weight_bytes": 400}

by_date = pm.by_date(WS2)
check("絵が日付ごとに束ねられる", DAY in by_date and len(by_date[DAY]) >= 1, list(by_date))
cands = sched._collect_picture_candidates(FB, [DAY], by_date)
check("その日の絵が候補になる", len(cands) == 1, cands)
check("ラベルに日付が入る", DAY in cands[0]["source"], cands[0]["source"])
check("中身はそのときの言葉", "落ち着かない" in cands[0]["content"], cands[0])
check("絵のパスを持ち回る", cands[0]["_picture"].endswith(".png"), cands[0])

check("別の日付なら候補ゼロ（＝絵は勝手に出てこない）",
      sched._collect_picture_candidates(FB, ["2026-01-01"], by_date) == [])
check("日付が空なら候補ゼロ", sched._collect_picture_candidates(FB, [], by_date) == [])
check("同じ日に何枚あっても per_day 枚まで",
      len(sched._collect_picture_candidates(dict(FB, picture_candidates=1), [DAY], by_date)) == 1)
check("picture_candidates=0 で完全に止められる",
      sched._collect_picture_candidates(dict(FB, picture_candidates=0), [DAY], by_date) == [])
check("直近に出した絵は日付マップから外れる",
      DAY not in pm.by_date(WS2, exclude=["generated/many.png"])
      or all(p["source"] != "generated/many.png"
             for p in pm.by_date(WS2, exclude=["generated/many.png"]).get(DAY, [])))

print("[7] 言葉の無い絵と、消えた絵は候補にしない")
WS6 = new_ws()
open(os.path.join(WS6, "generated", "silent.png"), "wb").write(png_bytes())
pm.record_view(WS6, "generated/silent.png", image_bytes=png_bytes(), mime_type="image/png")
age_all(WS6, days=100)
DAY6 = pm.load(WS6)["pictures"][0]["first_seen"]
check("言葉の無い絵は日付マップに入らない（サリアが判断できないため）", pm.by_date(WS6) == {})
pm.record_thought(WS6, ["generated/silent.png"], "言葉がついた")
check("言葉がつけば入る", DAY6 in pm.by_date(WS6))
os.remove(os.path.join(WS6, "generated", "silent.png"))
check("実体が消えた絵は入らない", pm.by_date(WS6) == {})

print("[8] 雑記帳が無い日の絵も候補に上がる（今回の補正）")


class FakeSalia:
    """絵の候補があればそれを選ぶ（pick_picture=False なら文章の方を選ぶ）偽サリア。"""

    def __init__(self, pick_picture=True):
        self.pick_picture = pick_picture
        self.seen = None

    async def select_note_fragment(self, candidates):
        self.seen = candidates
        for i, c in enumerate(candidates):
            if bool(c.get("_picture")) == self.pick_picture:
                return i
        return None


CFG = {"flashback": FB}

# WS2 には雑記帳が1件も無い。それでも絵の日付だけで候補が立つ
sched7 = Scheduler.__new__(Scheduler)
sched7.memory = types.SimpleNamespace(workspace=WS2)
sched7._recent_picture_sources = []
salia = FakeSalia(pick_picture=True)
sched7.agent = types.SimpleNamespace(salia=salia)
sched7.get_active_agent = None
check("雑記帳ディレクトリが無いことを確認", not os.path.isdir(os.path.join(WS2, "notes")))
text, path = asyncio.run(sched7._generate_note_fragment(CFG))
check("雑記帳が1件も無くても絵が浮かぶ", "<picture_memory>" in text, text[:80])
check("絵のパスが返る", path and path.endswith(".png"), path)
check("いつ見た絵かと、そのときの言葉が入る",
      "に見た絵" in text and "落ち着かない" in text, text)
check("出した絵は直近リストに積まれる",
      sched7._recent_picture_sources == ["generated/many.png"], sched7._recent_picture_sources)

# 同じ日に雑記帳もあれば、断片と絵が並んで候補になる
notes_dir = os.path.join(WS2, "notes")
os.makedirs(notes_dir, exist_ok=True)
with open(os.path.join(notes_dir, f"note_{DAY}.md"), "w", encoding="utf-8") as f:
    f.write("## その日の雑記帳\n" + "この日のことを書いた。" * 20 + "\n")
sched8 = Scheduler.__new__(Scheduler)
sched8.memory = types.SimpleNamespace(workspace=WS2)
sched8._recent_picture_sources = []
salia8 = FakeSalia(pick_picture=True)
sched8.agent = types.SimpleNamespace(salia=salia8)
sched8.get_active_agent = None
asyncio.run(sched8._generate_note_fragment(CFG))
check("同じ日の雑記帳の断片と並んで候補になる",
      salia8.seen and len(salia8.seen) == 2
      and any(c.get("_picture") for c in salia8.seen)
      and any(not c.get("_picture") for c in salia8.seen), salia8.seen)

sched9 = Scheduler.__new__(Scheduler)
sched9.memory = types.SimpleNamespace(workspace=WS2)
sched9._recent_picture_sources = []
sched9.agent = types.SimpleNamespace(salia=FakeSalia(pick_picture=False))
sched9.get_active_agent = None
text, path = asyncio.run(sched9._generate_note_fragment(CFG))
check("サリアが文章を選べば絵は付かない",
      "<note_fragment>" in text and path is None, (text[:40], path))

sched10 = Scheduler.__new__(Scheduler)
sched10.memory = types.SimpleNamespace(workspace=WS2)
sched10._recent_picture_sources = []
sched10.agent = types.SimpleNamespace(salia=FakeSalia(pick_picture=True))
sched10.get_active_agent = None
sched10.rag_db = None
out = asyncio.run(sched10._generate_flashback(
    {"flashback": dict(FB, enabled=True, event_db_probability=0.0,
                       note_fragment_probability=1.0)}, None))
check("フラッシュバック全体が (テキスト, 絵) を返す",
      isinstance(out, tuple) and "<picture_memory>" in out[0] and out[1], str(out)[:60])

sched11 = Scheduler.__new__(Scheduler)
sched11.memory = types.SimpleNamespace(workspace=WS2)
sched11._recent_picture_sources = []
sched11.agent = None
sched11.get_active_agent = None
check("サリアが使えなければ静かに諦める",
      asyncio.run(sched11._generate_note_fragment(CFG)) == ("", None))

sched12 = Scheduler.__new__(Scheduler)
sched12.memory = types.SimpleNamespace(workspace=new_ws())
sched12._recent_picture_sources = []
sched12.agent = types.SimpleNamespace(salia=FakeSalia())
sched12.get_active_agent = None
check("雑記帳も絵も無ければ静かに諦める",
      asyncio.run(sched12._generate_note_fragment(CFG)) == ("", None))

print(f"\n{PASSED} passed, {len(FAILED)} failed")
if FAILED:
    print("failed:", FAILED)
    sys.exit(1)
