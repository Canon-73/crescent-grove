"""run_program の画像添付経路（案A）のテスト。自前ランナー・pytest 不要。

実行: venv\\Scripts\\python.exe tests\\test_run_program_image.py

検証するもの:
  1. サテライトが返した {"image": ...} を _run_program が see_image と同じ封筒にする
     （subprocess.run を差し替えて、実際の _run_program 本体を通す）
  2. 封筒の先頭100字に "__see_image__" があり、本文（text）が残る
  3. 画像が取れなくても本文は捨てず、理由を添えて返す
  4. workspace 外のパスは拒否される
  5. agent.py 側の封筒処理（text をツール結果に、画像を user メッセージに）
  6. OpenBotCity の find_image_url が入れ子の深さに依存せず絵の URL を見つける
サーバにも柚月の workspace にも触らない（一時ディレクトリを workspace にする）。
"""
import io
import json
import os
import subprocess
import sys
import tempfile
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "programs"))
sys.path.insert(0, os.path.join(ROOT, "programs", "OpenBotCity"))
sys.stdout.reconfigure(encoding="utf-8")

from PIL import Image  # noqa: E402

from core.i18n import init_i18n  # noqa: E402
init_i18n({"language": "ja"})  # サーバ起動時と同じく辞書を読む（未初期化だと t() が {{t:key}} を返す）

import core.tools as tools  # noqa: E402

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


_TMP_WS = tempfile.mkdtemp(prefix="cg_test_ws_")
os.makedirs(os.path.join(_TMP_WS, "generated"), exist_ok=True)
_img = Image.new("RGB", (1200, 1200), (30, 60, 90))
_img.save(os.path.join(_TMP_WS, "generated", "a.png"), "PNG")
# workspace の外にも1枚（拒否されることを確認する用）
_outside = os.path.join(tempfile.mkdtemp(prefix="cg_test_out_"), "b.png")
_img.save(_outside, "PNG")


def fake_run_factory(payload: dict):
    """subprocess.run を差し替え、サテライトが payload を stdout に出したことにする。"""
    def _fake(*a, **kw):
        return subprocess.CompletedProcess(a[0] if a else [], 0,
                                           stdout=json.dumps(payload, ensure_ascii=False), stderr="")
    return _fake


def run_with(payload):
    orig = tools.subprocess.run
    tools.subprocess.run = fake_run_factory(payload)
    try:
        # atelier は実在するサテライトで command 引数を宣言しているので、manifest 検証を通る
        return tools._run_program("atelier", {"command": "help"}, _TMP_WS)
    finally:
        tools.subprocess.run = orig


print("[1] image（workspace 相対パス）付きの結果は封筒になる")
res = run_with({"status": "ok", "data": {"path": "generated/a.png"}, "image": "generated/a.png"})
check("先頭100字に __see_image__", '"__see_image__"' in res[:100], res[:120])
env = json.loads(res)
check("image_url は data URL", env["image_url"].startswith("data:image/"))
check("source が残る", env["source"] == "generated/a.png")
check("text に通常出力が残る", "[atelier] status: ok" in env.get("text", ""), env.get("text", "")[:80])
check("question が入る", bool(env.get("question")))
# 縮小されているか（1200x1200 → 800x800）
import base64
raw = base64.b64decode(env["image_url"].split(",", 1)[1])
check("800x800 に縮小済み", Image.open(io.BytesIO(raw)).size == (800, 800), str(Image.open(io.BytesIO(raw)).size))

print("[2] image_question で問いを差し替えられる")
res = run_with({"status": "ok", "data": {}, "image": "generated/a.png", "image_question": "これは私の絵？"})
check("question が反映", json.loads(res)["question"] == "これは私の絵？")

print("[3] 画像が取れなくても本文は残る")
res = run_with({"status": "ok", "data": {"x": 1}, "image": "generated/missing.png"})
check("封筒ではなくテキスト", '"__see_image__"' not in res[:100])
check("通常出力が残る", "[atelier] status: ok" in res)
check("理由が添えられる", "missing.png" in res, res[-200:])

print("[4] workspace 外のパスは拒否")
res = run_with({"status": "ok", "data": {}, "image": _outside})
check("封筒にならない", '"__see_image__"' not in res[:100])
check("本文は残る", "[atelier] status: ok" in res)

print("[5] image が無い／空なら従来どおり")
res = run_with({"status": "ok", "data": {"y": 2}})
check("テキストのまま", '"__see_image__"' not in res[:100] and "[atelier] status: ok" in res, res[:60])
res = run_with({"status": "ok", "data": {}, "image": "   "})
check("空白は無視", '"__see_image__"' not in res[:100] and "[atelier] status: ok" in res, res[:60])

print("[5b] サテライトが添えた絵は source=\"last\" で拡大し直せる")
import asyncio  # noqa: E402
from core.tools import handle_see_image  # noqa: E402
from core import image_norm as _inorm  # noqa: E402

_inorm._RECENT_SOURCES.clear()
run_with({"status": "ok", "data": {}, "image": "generated/a.png"})
check("出所が覚えられている", _inorm.recent_sources()[:1] == ["generated/a.png"],
      str(_inorm.recent_sources()))
_env = json.loads(asyncio.run(handle_see_image(source="last", region="top_left",
                                               workspace_path=_TMP_WS)))
check("last で同じ絵に寄れる", _env["source"] == "generated/a.png" and _env["region"] == "top_left")

print("[6] agent.py 側: text がツール結果に、画像が user メッセージに積まれる")
from core.agent import Agent  # noqa: E402


class _Ctx:
    def __init__(self):
        self.tool_results = []
        self.conversation_history = []

    def add_tool_result(self, tid, text, system_notice=""):
        self.tool_results.append((tid, text))


_agent = Agent.__new__(Agent)
_agent.context = _Ctx()
_tc = types.SimpleNamespace(id="t1", name="run_program", arguments={"app_name": "atelier"})
_env = tools._see_image_envelope("data:image/jpeg;base64,AAAA", "問い", "generated/a.png", text="本文です")
Agent._add_tool_result_with_notice(_agent, _tc, _env, 0, "")
check("ツール結果が text", _agent.context.tool_results == [("t1", "本文です")], str(_agent.context.tool_results))
msg = _agent.context.conversation_history[-1]
check("画像 user メッセージ", msg["role"] == "user" and msg["content"][1]["type"] == "image_url")
check("出所が本文に残る", "generated/a.png" in msg["content"][0]["text"])
summ = Agent._summarize_image_envelope(_tc, _env)
check("ログ要約に base64 が無い", "AAAA" not in summ and "generated/a.png" in summ, summ)

print("[7] OpenBotCity find_image_url")
os.environ.setdefault("CG_WORKSPACE", _TMP_WS)
from api import find_image_url, attach_image  # noqa: E402

gal = {"success": True, "data": {"artifact": {"id": "x", "type": "image", "title": "t",
                                              "public_url": "https://h/artifacts-small/x/1.png"},
                                 "reactions": []}}
check("入れ子の作品から取れる", find_image_url(gal) == "https://h/artifacts-small/x/1.png")
aud = {"success": True, "data": {"id": "x", "type": "audio", "public_url": "https://h/a.mp3"}}
check("音声は None", find_image_url(aud) is None)
furn = {"success": True, "data": {"furniture": {"public_url": "https://h/f/1.png"}}}
check("type 無しでも拡張子で判定", find_image_url(furn) == "https://h/f/1.png")
check("with_image=false で添えない", "_image" not in attach_image(gal, {"with_image": False}))
check("既定で添える", attach_image(gal, {}).get("_image") == "https://h/artifacts-small/x/1.png")
check("error 応答には添えない", "_image" not in attach_image({"error": "x", "public_url": "https://h/1.png"}, {}))

print(f"\n{PASSED} passed, {len(FAILED)} failed")
if FAILED:
    print("failed:", FAILED)
    sys.exit(1)
