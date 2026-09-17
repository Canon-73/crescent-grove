# test_atelier.py
"""atelier（画像生成）サテライトの回帰テスト。

test_openbotcity.py / test_citron_editor.py と同じ流儀:
pytest 不要・自前ランナー・venv で直接実行する。

    venv\\Scripts\\python.exe tests\\test_atelier.py

**柚月サーバ（dev）は起動しない。ComfyUI も起動しない。ネットワークにも出ない。**
comfy モジュールの HTTP 呼び出しは全てモックに差し替える。
柚月の workspace を壊さないよう、CG_WORKSPACE を一時ディレクトリに差し替えてから import する。

検証項目:
  1. 全モジュールが import でき、REGISTRY にコマンド名の重複が無い
  2. コードが読む引数が全て manifest.yaml に宣言されている
     （未宣言だと _run_program が「未定義の引数」で弾き、コマンドが起動すらしない）
  3. prompt / negative_prompt に path_check: false が付いている
     （本文に "../" や "/" が入ると _run_program が呼び出しごと拒否する）
  4. コードが使う t() キーが ja.json / en.json の両方に存在する
  5. 引数なしで全コマンドを呼んでも例外を投げず dict を返し、エラーには回復のヒントが付く
  6. draw → status → pick_up の一巡が通り、workspace 配下に JPEG が置かれる
  7. ComfyUI が落ちている時に「待たずに」返る（＝イベントループを塞がない）
  8. 台帳がディスクに残り、job_id を忘れても status で拾い直せる
  9. 生成物が workspace の外に出ない（see_image / upload_artifact が受け付けなくなる）
 10. 実際の subprocess 経路（_run_program と同じ env）で main.py が動く
"""

import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

PROJECT_ROOT = Path(__file__).resolve().parent.parent
PROGRAMS_DIR = PROJECT_ROOT / "programs"
AT_DIR = PROGRAMS_DIR / "atelier"
LANG_DIR = PROGRAMS_DIR / "_lang"

# --- 柚月の実データを絶対に触らないための隔離 -------------------------------
# config.py は import 時に CG_WORKSPACE を読んでパスを確定するため、
# import より前に一時ディレクトリへ向けておく必要がある。
_TMP_WS = tempfile.mkdtemp(prefix="atelier_test_ws_")
os.environ["CG_WORKSPACE"] = _TMP_WS
os.environ.setdefault("CG_LANG", "ja")
# 実在しないポートに向けて、取りこぼしがあっても本物へ飛ばないようにする
os.environ["CG_ATELIER_PORT"] = "59189"

sys.path.insert(0, str(PROGRAMS_DIR))   # _i18n
sys.path.insert(0, str(AT_DIR))         # config / comfy / jobs / main

import comfy      # noqa: E402
import config     # noqa: E402
import jobs       # noqa: E402
import launcher   # noqa: E402
import main as at_main  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(("  OK  " if cond else "  NG  ") + name + ("" if cond else f"\n        -> {extra}"))


def section(title):
    print(f"\n=== {title} ===")


# --------------------------------------------------------------------------
# ComfyUI のモック
# --------------------------------------------------------------------------
def _png_bytes():
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (64, 64), (30, 60, 120)).save(buf, "PNG")
    return buf.getvalue()


class FakeComfy:
    """comfy モジュールを差し替えるモック。HTTP は一切発生しない。"""

    def __init__(self):
        self.alive = True
        self.submitted = []
        self.finish = True          # True なら投入直後に完成扱い
        self.status_str = "success"
        self.interrupted = False
        self.deleted = []
        self.freed = False

    def is_alive(self, timeout=2.0):
        return self.alive

    def queue_state(self, timeout=3.0):
        if not self.alive:
            raise comfy.ComfyDown("down")
        return (0, 0)

    def submit(self, graph, client_id="atelier", timeout=15.0):
        if not self.alive:
            raise comfy.ComfyDown("down")
        pid = f"pid-{len(self.submitted)}"
        self.submitted.append((pid, graph))
        return pid

    def history(self, prompt_id, timeout=10.0):
        if not self.alive:
            raise comfy.ComfyDown("down")
        if not self.finish:
            return None
        return {"__fake__": prompt_id}

    def parse_outputs(self, hist):
        if self.status_str != "success":
            return self.status_str, [], "boom"
        return "success", [{"filename": "x_00001_.png", "subfolder": "atelier", "type": "output"}], ""

    def fetch_image(self, filename, subfolder, ftype="output", timeout=30.0):
        if not self.alive:
            raise comfy.ComfyDown("down")
        return _png_bytes()

    def interrupt(self, timeout=5.0):
        self.interrupted = True

    def delete_queued(self, prompt_id, timeout=5.0):
        self.deleted.append(prompt_id)

    def free_memory(self, timeout=10.0):
        self.freed = True

    def build_graph(self, *a, **kw):
        return comfy.build_graph(*a, **kw)

    ComfyDown = comfy.ComfyDown
    ComfyError = comfy.ComfyError


fake = FakeComfy()
at_main.comfy = fake
# draw は既定で描き上がりを待つ。テストでは実時間を待たせない
config.WAIT_POLL_SEC = 0.02
config.WAIT_MAX_SEC = 0.5
# ランチャーを実際に起動しない（GPU も掴まない）
_spawned = []
at_main.ensure_open = lambda: (fake.alive, {"state": "starting"}) if fake.alive else (
    _spawned.append(1) or (False, {"state": "starting"}))
# GPU 問い合わせも止める（nvidia-smi を叩かない）
at_main.gpu_free_mb = lambda: 20000


def reset_state():
    for p in (config.JOBS_FILE, config.LAUNCHER_FILE):
        if p.exists():
            p.unlink()
    if config.GENERATED_DIR.exists():
        shutil.rmtree(config.GENERATED_DIR)
    fake.alive = True
    fake.finish = True
    fake.status_str = "success"
    _spawned.clear()


# --------------------------------------------------------------------------
section("1. 構造")
# --------------------------------------------------------------------------
import yaml  # noqa: E402

manifest = yaml.safe_load((AT_DIR / "manifest.yaml").read_text(encoding="utf-8"))
check("manifest の name が atelier", manifest.get("name") == "atelier", manifest.get("name"))
check("REGISTRY にコマンド重複が無い",
      len(at_main.REGISTRY) == len(set(at_main.REGISTRY)), sorted(at_main.REGISTRY))
check("必要なコマンドが揃っている",
      set(at_main.REGISTRY) == {"draw", "status", "pick_up", "cancel", "health",
                                "help", "manual"},
      sorted(at_main.REGISTRY))
check("timeout が宣言されている（既定30秒だと余裕が無い）",
      isinstance(manifest.get("timeout"), int) and manifest["timeout"] >= 30, manifest.get("timeout"))

# --------------------------------------------------------------------------
section("2. manifest の引数宣言（宣言漏れはコマンドを起動不能にする）")
# --------------------------------------------------------------------------
declared = {a["name"] for a in manifest.get("args", [])}
src = "\n".join((AT_DIR / f).read_text(encoding="utf-8")
                for f in ("main.py", "comfy.py", "jobs.py"))
used = set(re.findall(r'args\.get\(\s*["\']([a-zA-Z0-9_]+)["\']', src))
missing = sorted(used - declared)
check("コードが読む引数が全て manifest に宣言されている", not missing, f"未宣言: {missing}")

pc = {a["name"]: a.get("path_check", True) for a in manifest.get("args", [])}
check("prompt に path_check: false", pc.get("prompt") is False, pc.get("prompt"))
check("negative_prompt に path_check: false",
      pc.get("negative_prompt") is False, pc.get("negative_prompt"))

# --------------------------------------------------------------------------
section("3. i18n")
# --------------------------------------------------------------------------
all_src = "\n".join((AT_DIR / f).read_text(encoding="utf-8")
                    for f in ("main.py", "comfy.py", "jobs.py", "launcher.py", "config.py"))
keys = set(re.findall(r'(?<![\w.])t\(\s*["\']([a-zA-Z0-9_]+)["\']', all_src))
keys |= set(re.findall(r'\{\{t:([a-zA-Z0-9_]+)\}\}',
                       (AT_DIR / "manifest.yaml").read_text(encoding="utf-8")))
ja = json.loads((LANG_DIR / "ja.json").read_text(encoding="utf-8"))
en = json.loads((LANG_DIR / "en.json").read_text(encoding="utf-8"))
check("使用中の t() キーが ja.json に全てある", not (keys - ja.keys()), sorted(keys - ja.keys()))
check("使用中の t() キーが en.json に全てある", not (keys - en.keys()), sorted(keys - en.keys()))
check("ja/en のキー数が一致", len(ja) == len(en), f"{len(ja)} vs {len(en)}")
check("ja/en のキー集合が一致", ja.keys() == en.keys(),
      sorted(set(ja.keys()) ^ set(en.keys()))[:10])
unresolved = [k for k in keys if "{{t:" in ja.get(k, "")]
check("未解決マーカーが残っていない", not unresolved, unresolved)

# --------------------------------------------------------------------------
section("4. 引数なしで全コマンドを叩いても壊れない")
# --------------------------------------------------------------------------
reset_state()
for name, fn in sorted(at_main.REGISTRY.items()):
    try:
        r = fn({})
        ok = isinstance(r, dict)
        detail = type(r).__name__
    except Exception as e:
        ok, r, detail = False, {}, f"{type(e).__name__}: {e}"
    check(f"{name}() が dict を返す", ok, detail)
    if ok and r.get("error"):
        check(f"{name}() のエラーに回復のヒントが付く",
              bool(r.get("hint")) and ("example" in r or "pending_jobs" in r or "ready_jobs" in r),
              json.dumps(r, ensure_ascii=False)[:200])

# --------------------------------------------------------------------------
section("5. draw → status → pick_up の一巡")
# --------------------------------------------------------------------------
reset_state()
# 既定は「描き上がるまで待って、そのまま絵を返す」＝ツール1回で完結する
one = at_main.cmd_draw({"prompt": "a quiet night street, flat illustration, no text"})
check("draw 1回で絵の置き場所まで返る（ターンを終える問題を作らない）",
      one.get("state") == "done" and one.get("path"), one)
check("draw の戻り値に seed が入る（同じ絵を再現できる）",
      isinstance(one.get("seed"), int), one.get("seed"))
check("draw の戻り値が see_image に渡せる形",
      str(one.get("path", "")).startswith("generated/"), one.get("path"))

# nowait 指定なら従来どおり job_id だけ返す（大きいサイズ用）
reset_state()
d = at_main.cmd_draw({"prompt": "a quiet night street, flat illustration, no text",
                      "nowait": True})
check("nowait なら待たずに job_id を返す", d.get("state") == "drawing" and d.get("job_id"), d)
check("nowait でも seed を返す", isinstance(d.get("seed"), int), d.get("seed"))
check("nowait は次にやることを教える", bool(d.get("next")), d)
job_id = d.get("job_id")

st = at_main.cmd_status({"job_id": job_id})
check("status が done を返す", st.get("state") == "done", st)
check("status が pick_up を案内する", "pick_up" in (st.get("next") or ""), st.get("next"))

pu = at_main.cmd_pick_up({"job_id": job_id})
check("pick_up が成功する", pu.get("state") == "done" and pu.get("path"), pu)
rel = pu.get("path", "")
check("返るパスが workspace 相対（see_image がそのまま受け取れる）",
      rel.startswith("generated/") and not os.path.isabs(rel), rel)
saved = config.GENERATED_DIR / Path(rel).name
check("実ファイルが workspace 配下にある", saved.exists(), str(saved))
check("JPEG で保存されている", saved.suffix == ".jpg" and saved.read_bytes()[:2] == b"\xff\xd8",
      saved.suffix)
check("生成物が workspace の外に出ていない",
      str(saved.resolve()).startswith(str(config.WORKSPACE.resolve())), str(saved.resolve()))
check("pick_up が prompt と seed を返す（そのまま投稿に使える）",
      pu.get("prompt") and isinstance(pu.get("seed"), int), pu)

pend = at_main.cmd_status({})
check("回収済みは未回収一覧から消える",
      not any(j.get("job_id") == job_id for j in pend.get("pending", [])), pend)

# --------------------------------------------------------------------------
section("6. アトリエが閉じている時に待たずに返る")
# --------------------------------------------------------------------------
reset_state()
fake.alive = False
d2 = at_main.cmd_draw({"prompt": "test"})
check("閉じていても待たずに返る（生成完了を待たない）", d2.get("state") == "waiting_open", d2)
check("閉じていても job_id が返る（同じ依頼を2回させない）", bool(d2.get("job_id")), d2)
check("開けにいく処理が呼ばれた", bool(_spawned), _spawned)
held = jobs.get(d2["job_id"])
check("依頼が台帳に預けられる", held and held.get("state") == "waiting_open", held)
check("投入内容（graph）が保存される", bool(held and held.get("graph")), bool(held.get("graph")))
check("未投入なので prompt_id は無い", held.get("prompt_id") is None, held.get("prompt_id"))
st2 = at_main.cmd_status({"job_id": d2["job_id"]})
check("status が開店待ちを報告する", st2.get("state") == "waiting_open", st2)
pu3 = at_main.cmd_pick_up({"job_id": d2["job_id"]})
check("開店待ちを pick_up してもエラーにせず案内を返す",
      pu3.get("state") == "waiting_open" and pu3.get("next"), pu3)
cn = at_main.cmd_cancel({"job_id": d2["job_id"]})
check("未投入ジョブも cancel できる（prompt_id が None でも落ちない）",
      cn.get("state") == "cancelled", cn)
h = at_main.cmd_health({})
check("health が closed を報告", h.get("open") is False, h)
fake.alive = True

# ランチャーが開いた瞬間に預かり分を投入する
reset_state()
fake.alive = False
d5 = at_main.cmd_draw({"prompt": "held piece"})
fake.alive = True
import launcher as _lc
_lc.comfy = fake            # ランチャー側も同じモックに向ける
n = _lc.submit_waiting_jobs()
check("開いたら預かり分が投入される", n == 1, n)
after = jobs.get(d5["job_id"])
check("投入後は drawing になり prompt_id が付く",
      after.get("state") == "drawing" and after.get("prompt_id"), after)
check("投入後は graph を台帳から落とす（肥大防止）", not after.get("graph"), after.get("graph"))
st5 = at_main.cmd_status({"job_id": d5["job_id"]})
check("預かり分もそのまま完成まで追える", st5.get("state") == "done", st5)

# ランチャー状態の鮮度判定
reset_state()
_lc.write_state(pid=99999, state="ready", comfy_pid=1)
check("ハートビートが新しければ ready と報告", _lc.effective_state() == "ready",
      _lc.effective_state())
stale = _lc.read_state()
stale["heartbeat"] = 0      # 強制終了で取り残された状態を再現
config.LAUNCHER_FILE.write_text(json.dumps(stale), encoding="utf-8")
check("ハートビートが古ければ stopped と報告（強制終了で ready のまま残る問題）",
      _lc.effective_state() == "stopped", _lc.effective_state())

# --------------------------------------------------------------------------
section("7. 台帳がディスクに残る（job_id を忘れても拾える）")
# --------------------------------------------------------------------------
reset_state()
fake.finish = False          # 描き終わらない状態にする
d3 = at_main.cmd_draw({"prompt": "pending piece"})
jid = d3["job_id"]
check("台帳ファイルが作られる", config.JOBS_FILE.exists(), str(config.JOBS_FILE))
ledger = json.loads(config.JOBS_FILE.read_text(encoding="utf-8"))
check("台帳に prompt と seed が保存される",
      ledger["jobs"][-1].get("prompt") and ledger["jobs"][-1].get("seed") is not None, ledger)
lst = at_main.cmd_status({})
check("引数なし status で未回収が拾える",
      any(j.get("job_id") == jid for j in lst.get("pending", [])), lst)
pu2 = at_main.cmd_pick_up({"job_id": jid})
check("未完成を pick_up したら drawing と案内が返る（エラーにしない）",
      pu2.get("state") == "drawing" and pu2.get("next"), pu2)

# --------------------------------------------------------------------------
section("8. 失敗時の扱い")
# --------------------------------------------------------------------------
reset_state()
fake.status_str = "error"
d4 = at_main.cmd_draw({"prompt": "will fail"})
st4 = at_main.cmd_status({"job_id": d4["job_id"]})
check("失敗が error として返る", st4.get("state") == "error", st4)
check("失敗に次の行動が示される", bool(st4.get("next")), st4)
fake.status_str = "success"

reset_state()
bad = at_main.cmd_draw({})
check("prompt 無しはエラー＋呼び出し例", bad.get("error") and bad.get("example"), bad)
check("呼び出し例がそのまま実行できる形",
      bad["example"].get("command") == "draw" and bad["example"].get("prompt"), bad.get("example"))
nf = at_main.cmd_status({"job_id": "no-such-id"})
check("存在しない job_id はエラー＋一覧の案内", nf.get("error") and nf.get("hint"), nf)

# --------------------------------------------------------------------------
section("9. グラフ組み立て")
# --------------------------------------------------------------------------
g = comfy.build_graph("hello", "", 1024, 1024, 12, 1.0, 42, "atelier/x")
check("Krea2 のモデル一式が乗る",
      g["1"]["inputs"]["unet_name"] == config.UNET_NAME
      and g["2"]["inputs"]["clip_name"] == config.CLIP_NAME
      and g["3"]["inputs"]["vae_name"] == config.VAE_NAME, g["1"])
check("CLIPLoader の type が krea2", g["2"]["inputs"]["type"] == "krea2", g["2"])
check("prompt がそのまま CLIPTextEncode に渡る（JSON領域指定も同じ経路）",
      g["5"]["inputs"]["text"] == "hello", g["5"])
layout = json.dumps({"scene": "x", "regions": [{"bbox": [0, 0, 1, 1], "description": "y"}]})
g2 = comfy.build_graph(layout, "", 512, 512, 8, 1.0, 1, "p")
check("JSON 領域指定も文字列として素通しされる", g2["5"]["inputs"]["text"] == layout, g2["5"])
check("Turbo の推奨設定（euler / simple）", g["8"]["inputs"]["sampler_name"] == "euler"
      and g["8"]["inputs"]["scheduler"] == "simple", g["8"]["inputs"])

# 値の丸め
reset_state()
big = at_main.cmd_draw({"prompt": "x", "width": 99999, "height": 1, "steps": 999, "cfg": 99})
job = jobs.get(big["job_id"])
check("サイズが上下限に丸められる",
      job["width"] == config.MAX_SIDE and job["height"] == config.MIN_SIDE,
      (job["width"], job["height"]))
check("cfg が 3.0 以下に丸められる（超えると彩度が飛ぶ）", job["cfg"] <= 3.0, job["cfg"])

# --------------------------------------------------------------------------
section("10. 実際の subprocess 経路（_run_program と同じ env）")
# --------------------------------------------------------------------------
env = os.environ.copy()
env["CG_WORKSPACE"] = _TMP_WS
env["CG_LANG"] = "ja"
env["CG_PROJECT_ROOT"] = str(PROJECT_ROOT)
env["PYTHONIOENCODING"] = "utf-8"
env["PYTHONPATH"] = str(PROGRAMS_DIR) + os.pathsep + env.get("PYTHONPATH", "")
env["CG_ATELIER_PORT"] = "59189"   # 誰も listen していないポート

for payload, label in (({"command": "help"}, "help"),
                       ({"command": "health"}, "health"),
                       ({}, "引数なし")):
    p = subprocess.run([sys.executable, str(AT_DIR / "main.py")],
                       input=json.dumps(payload), capture_output=True, text=True,
                       encoding="utf-8", cwd=_TMP_WS, env=env, timeout=60)
    ok = p.returncode == 0 and p.stdout.strip()
    check(f"subprocess: {label} が JSON を返す", bool(ok),
          f"rc={p.returncode} stderr={p.stderr[:300]}")
    if ok:
        try:
            out = json.loads(p.stdout)
            check(f"subprocess: {label} の JSON が読める", "status" in out,
                  p.stdout[:200])
        except Exception as e:
            check(f"subprocess: {label} の JSON が読める", False, f"{e}: {p.stdout[:200]}")

# help の中身がモデルの事実を伝えているか
hp = at_main.cmd_help({})
check("help に画風の注意がある",
      any("写真" in s or "photograph" in s for s in hp["about_this_model"]), hp["about_this_model"])
check("help に文字化けの注意と抑止句がある",
      any("no text" in s for s in hp["about_this_model"]), hp["about_this_model"])
check("help に JSON 領域指定の実例がある",
      "regions" in hp["layout_example"] and "bbox" in hp["layout_example"]["regions"][0],
      hp.get("layout_example"))
check("help の example がそのまま draw に渡せる",
      hp["example"].get("command") == "draw" and hp["example"].get("prompt"), hp.get("example"))
check("help が manual の存在を伝える",
      "manual" in hp["commands"] and hp.get("read_more"), hp.get("read_more"))

# --------------------------------------------------------------------------
section("11. 手引き（manual）")
# --------------------------------------------------------------------------
man_dir = AT_DIR / "manual"
check("ja / en の手引きが両方ある",
      (man_dir / "ja.md").exists() and (man_dir / "en.md").exists(),
      sorted(p.name for p in man_dir.glob("*.md")) if man_dir.exists() else "manual/ が無い")

mres = at_main.cmd_manual({})
body = mres.get("manual", "")
check("manual コマンドが本文を返す", len(body) > 1000, len(body))
check("manual が現在の言語（ja）を返す", "アトリエの手引き" in body, body[:60])

for lang in ("ja", "en"):
    md = (man_dir / f"{lang}.md").read_text(encoding="utf-8")
    check(f"{lang}: 画風の軸が説明されている",
          ("媒体" in md or "Medium" in md) and ("光" in md or "Light" in md), lang)
    check(f"{lang}: 文字の抑止句が実際に使える形で載っている",
          "no text, no signs, no lettering" in md, lang)
    check(f"{lang}: JSON 領域指定の bbox 例がある", '"bbox"' in md and "regions" in md, lang)
    check(f"{lang}: seed の使い分けが書かれている",
          "seed" in md and ("再現" in md or "reproducible" in md.lower()), lang)
    check(f"{lang}: negative_prompt が cfg 1.0 で効かないことが書かれている",
          "negative_prompt" in md and ("1.0" in md), lang)
    # 作例集にしない方針の担保: 定型呪文を勧めていないこと
    check(f"{lang}: 審美スコアラー向けの定型呪文を載せていない",
          "masterpiece" not in md.lower() and "best quality" not in md.lower(), lang)

# 見出し構成が ja / en で揃っているか（片方だけ節が欠けるのを防ぐ）
def heads(md):
    return [l.split(" ", 1)[0] for l in md.splitlines() if l.startswith("## ")]
check("ja / en の節数が一致",
      len(heads((man_dir / "ja.md").read_text(encoding="utf-8")))
      == len(heads((man_dir / "en.md").read_text(encoding="utf-8"))),
      (len(heads((man_dir / "ja.md").read_text(encoding="utf-8"))),
       len(heads((man_dir / "en.md").read_text(encoding="utf-8")))))

# subprocess 経路でも読めるか（PYTHONPATH 注入下でファイルを引けること）
p = subprocess.run([sys.executable, str(AT_DIR / "main.py")],
                   input=json.dumps({"command": "manual"}), capture_output=True, text=True,
                   encoding="utf-8", cwd=_TMP_WS, env=env, timeout=60)
ok = p.returncode == 0 and p.stdout.strip()
check("subprocess: manual が読める", bool(ok), f"rc={p.returncode} stderr={p.stderr[:300]}")
if ok:
    out = json.loads(p.stdout)
    check("subprocess: manual の本文が返る",
          len(out.get("data", {}).get("manual", "")) > 1000,
          len(out.get("data", {}).get("manual", "")))

# --------------------------------------------------------------------------
section("12. ランチャーのアイドル判定")
# --------------------------------------------------------------------------
# 実際に踏んだバグ: 台帳がまだ無いと jobs.last_use() が 0 を返し、
# 「UNIX元期からずっと未使用」と判定されて起動直後に自分で落ちる。
# 初回の draw は "opening" を返すだけでジョブを積まないため、初回利用で必ず起きる。
reset_state()
check("台帳が無いとき last_use は 0", jobs.last_use() == 0, jobs.last_use())

import time as _time
_now = _time.time()
# 修正後の式: 自分の起動時刻を下限に敷く
idle_fixed = _now - max(jobs.last_use(), _now)
idle_broken = _now - max(jobs.last_use(), 0)
check("台帳が無くても起動直後は「未使用時間ゼロ」と判定される",
      idle_fixed < 1, idle_fixed)
check("（旧実装なら即停止する条件だったことの確認）",
      idle_broken > config.IDLE_STOP_SEC, idle_broken)

launcher_src = (AT_DIR / "launcher.py").read_text(encoding="utf-8")
check("launcher が起動時刻を下限に使っている",
      "max(jobs.last_use(), started_at)" in launcher_src,
      "started_at を下限にしていない")
check("launcher が起動時刻を記録している",
      "started_at = time.time()" in launcher_src, "started_at が無い")

# --------------------------------------------------------------------------
section("13. 黒いコンソール窓を出さない")
# --------------------------------------------------------------------------
# 実際に踏んだ問題: DETACHED_PROCESS は「親のコンソールを継承しない」フラグだが、
# Windows 11 では新しいコンソールが割り当てられて Windows Terminal が窓を開き、
# プロセス終了後も空の窓が残る。柚月が自律的に描くたびにデスクトップへ窓が湧く。
main_src = (AT_DIR / "main.py").read_text(encoding="utf-8")
for name, src in (("main.py", main_src), ("launcher.py", launcher_src)):
    check(f"{name}: DETACHED_PROCESS を使っていない",
          "DETACHED_PROCESS" not in src.replace("DETACHED_PROCESS は", "")
          .replace("DETACHED_PROCESS を使ってはいけない", "")
          .replace("DETACHED_PROCESS は使わない", ""),
          "DETACHED_PROCESS が残っている")
    check(f"{name}: CREATE_NO_WINDOW を使っている", "CREATE_NO_WINDOW" in src, name)

# 外部コマンドを起動する箇所すべてに creationflags が付いているか
import re as _re2
for name, src in (("main.py", main_src), ("launcher.py", launcher_src)):
    calls = _re2.findall(r'subprocess\.(?:run|Popen)\((.*?)\n\s*\)', src, _re2.DOTALL)
    bad = [c[:60] for c in calls if "creationflags" not in c]
    check(f"{name}: 全ての外部コマンド起動に creationflags が付いている", not bad, bad)

if os.name == "nt":
    check("常駐用に pythonw.exe を選ぶ（コンソールを持たないバイナリ）",
          at_main._background_python().endswith("pythonw.exe"),
          at_main._background_python())
    flags = at_main._no_window_flags()
    check("CREATE_NO_WINDOW が立っている",
          bool(flags & subprocess.CREATE_NO_WINDOW), flags)
    check("DETACHED_PROCESS が立っていない",
          not (flags & getattr(subprocess, "DETACHED_PROCESS", 0)), flags)

# --------------------------------------------------------------------------
# 後片付け
# --------------------------------------------------------------------------
shutil.rmtree(_TMP_WS, ignore_errors=True)

print(f"\n{'=' * 60}")
print(f"通過 {len(PASS)} / 失敗 {len(FAIL)}")
if FAIL:
    for f in FAIL:
        print("  NG:", f)
print("=" * 60)
sys.exit(1 if FAIL else 0)
