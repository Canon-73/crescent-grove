"""部分拡大（see_image の region / grid / source="last"）のテスト。pytest 不要。

実行: venv\\Scripts\\python.exe tests\\test_image_zoom.py

検証するもの:
  1. 領域指定のパース（名前・格子・矩形・異常系のエラー文）
  2. 切り出しが「縮小の前」に効いていること＝実際に細部が増えること
  3. 隣のマスと重なる余白（境界で文字が割れないように）
  4. マス目の重ね描き
  5. source="last" の解決
  6. handle_see_image を通した往復（封筒に region が残る）
  7. 受け取った画像の保存（原本のまま・出所が付く）
サーバにも柚月の workspace にも触らない（一時ディレクトリを workspace にする）。
"""
import asyncio
import base64
import io
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.stdout.reconfigure(encoding="utf-8")

from PIL import Image, ImageDraw  # noqa: E402

from core.i18n import init_i18n  # noqa: E402
init_i18n({"language": "ja"})

from core import image_norm as inorm  # noqa: E402
from core.tools import handle_see_image  # noqa: E402

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


_WS = tempfile.mkdtemp(prefix="cg_zoom_ws_")


def decode(data_url):
    return Image.open(io.BytesIO(base64.b64decode(data_url.split(",", 1)[1])))


print("[1] 領域指定のパース")
rect, err = inorm.parse_region_spec("full")
check("full は画像全体", rect == (0.0, 0.0, 1.0, 1.0) and err is None, str(rect))
rect, _ = inorm.parse_region_spec("center")
check("center は中央寄り", rect[0] > 0.1 and rect[2] < 0.9 and rect[0] < 0.25, str(rect))
rect, _ = inorm.parse_region_spec("top_right")
check("top_right は右上", rect[0] > 0.3 and rect[1] == 0.0 and rect[2] == 1.0, str(rect))
rect, _ = inorm.parse_region_spec("TOP-RIGHT")
check("大文字・ハイフンも同じ", rect == inorm.parse_region_spec("top_right")[0])
rect, _ = inorm.parse_region_spec("3x3:5")
check("3x3:5 は中央のマス", 0.25 < rect[0] < 0.34 and 0.66 < rect[2] < 0.75, str(rect))
rect, _ = inorm.parse_region_spec("3x3:1")
check("3x3:1 は左上", rect[0] == 0.0 and rect[1] == 0.0, str(rect))
rect, _ = inorm.parse_region_spec("3x3:9")
check("3x3:9 は右下", rect[2] == 1.0 and rect[3] == 1.0, str(rect))
rect, _ = inorm.parse_region_spec("0.2,0.1,0.5,0.4")
check("矩形は x,y,w,h をそのまま", rect == (0.2, 0.1, 0.7, 0.5), str(rect))
check("空指定は None", inorm.parse_region_spec(None) == (None, None)
      and inorm.parse_region_spec("") == (None, None))

print("[1b] 異常系は理由が返る（柚月が直せる文言か）")
for bad, must in (("3x3:99", "1"), ("9x9:1", "8"), ("0.2,0.1", "x,y"),
                  ("a,b,c,d", "0"), ("naka", "center"), ("3x3:abc", "3x3:5")):
    rect, err = inorm.parse_region_spec(bad)
    check(f'"{bad}" が拒否され理由が付く', rect is None and err and must in err, str(err)[:70])
rect, err = inorm.parse_region_spec(123)
check("文字列以外も拒否", rect is None and err)

print("[2] 切り出しは縮小の前＝細部が増える")
# 800x800 に収まらない大きな画像に、細い線を等間隔で描く。
# 全体表示では潰れて数えられないが、1/9 に寄れば数えられる。
big = Image.new("RGB", (2400, 2400), (255, 255, 255))
d = ImageDraw.Draw(big)
for x in range(0, 2400, 6):          # 6px 間隔 → 縮小(1/3)すると潰れる
    d.line([(x, 800), (x, 1600)], fill=(0, 0, 0), width=2)
buf = io.BytesIO()
big.save(buf, "PNG")
big_bytes = buf.getvalue()


def variance(im):
    """明暗のばらつき。線が潰れるほど小さくなる。"""
    g = im.convert("L")
    px = list(g.crop((g.width // 3, g.height // 2 - 20, g.width * 2 // 3, g.height // 2 + 20)).getdata())
    mean = sum(px) / len(px)
    return sum((p - mean) ** 2 for p in px) / len(px)


# 全体側は rect="full" を渡して必ず縮小版を作らせる。無指定だと「JPEG にすると
# 太る線画は元の PNG のまま返す」規則が効いて 2400x2400 のまま返り、DeepSeek 側で
# 縮小される前の姿になってしまう。柚月が実際に見るのは縮小後なので、そこで比べる。
full_rect, _ = inorm.parse_region_spec("full")
whole, _ = inorm.normalize_image_bytes(big_bytes, "image/png", rect=full_rect)
cell_rect, _ = inorm.parse_region_spec("3x3:5")
zoomed, _ = inorm.normalize_image_bytes(big_bytes, "image/png", rect=cell_rect)
im_whole, im_zoom = Image.open(io.BytesIO(whole)), Image.open(io.BytesIO(zoomed))
check("全体は 800x800 相当に縮む", im_whole.size[0] * im_whole.size[1] <= 640000, str(im_whole.size))
check("縮小前の PNG はそのまま返る（バイト数が増えないので）",
      inorm.normalize_image_bytes(big_bytes, "image/png")[1] == "image/png")
check("拡大側も 800x800 相当に収まる", im_zoom.size[0] * im_zoom.size[1] <= 640000, str(im_zoom.size))
check("拡大した方が線を判別できる（分散が大きい）",
      variance(im_zoom) > variance(im_whole) * 1.5,
      f"whole={variance(im_whole):.0f} zoom={variance(im_zoom):.0f}")

print("[2b] 元の解像度が上限（小さい画像を寄っても増えない）")
small = Image.new("RGB", (200, 200), (10, 10, 10))
b = io.BytesIO(); small.save(b, "PNG")
out, _ = inorm.normalize_image_bytes(b.getvalue(), "image/png", rect=cell_rect)
sz = Image.open(io.BytesIO(out)).size
check("200x200 の 1/9 は拡大されない", max(sz) <= 200, str(sz))

print("[3] 隣のマスと少し重なる（境界で割れないように）")
tl, _ = inorm.parse_region_spec("top_left")
br, _ = inorm.parse_region_spec("bottom_right")
check("四分割の右端が 0.5 を超える", tl[2] > 0.5, str(tl))
check("四分割の左端が 0.5 を下回る", br[0] < 0.5, str(br))
c1, _ = inorm.parse_region_spec("3x3:1")
c2, _ = inorm.parse_region_spec("3x3:2")
check("隣り合うマスが重なる", c1[2] > c2[0], f"{c1[2]:.3f} > {c2[0]:.3f}")
rect_exact, _ = inorm.parse_region_spec("0.0,0.0,0.5,0.5")
check("明示的な矩形には余白を足さない", rect_exact == (0.0, 0.0, 0.5, 0.5), str(rect_exact))

print("[4] マス目の重ね描き")
g, err = inorm.parse_grid_spec(True)
check("true は 3x3", g == (3, 3) and err is None)
check('"4x4" も通る', inorm.parse_grid_spec("4x4")[0] == (4, 4))
check("false は無指定", inorm.parse_grid_spec(False) == (None, None))
check("範囲外は拒否", inorm.parse_grid_spec(99)[0] is None and inorm.parse_grid_spec(99)[1])
plain = Image.new("RGB", (600, 600), (128, 128, 128))
b = io.BytesIO(); plain.save(b, "PNG")
gridded, _ = inorm.normalize_image_bytes(b.getvalue(), "image/png", grid=(3, 3))
gim = Image.open(io.BytesIO(gridded)).convert("RGB")
colors = {c for _, c in gim.getcolors(maxcolors=100000)}
check("単色だった画像に線と文字が乗る", len(colors) > 10, f"{len(colors)} 色")

print("[5] source=\"last\" の解決")
inorm._RECENT_SOURCES.clear()
src, err = inorm.resolve_source("last")
check("何も見ていなければ理由を返す", src is None and err and "last" in err, str(err)[:60])
inorm.remember_source("generated/a.jpg")
inorm.remember_source("https://example.com/b.png")
check('"last" は直近', inorm.resolve_source("last")[0] == "https://example.com/b.png")
check('"last-1" は1つ前', inorm.resolve_source("last-1")[0] == "generated/a.jpg")
inorm.remember_source("generated/a.jpg")   # 見直しても増えない
check("同じ出所は重複せず繰り上がる", inorm.resolve_source("last")[0] == "generated/a.jpg"
      and len(inorm.recent_sources()) == 2, str(inorm.recent_sources()))
check("遡りすぎは理由を返す", inorm.resolve_source("last-9")[0] is None)
check("通常のパスは素通し", inorm.resolve_source("images/x.png")[0] == "images/x.png")
check("data URL は覚えない", (inorm.remember_source("data:image/png;base64,AA"),
                              len(inorm.recent_sources()))[1] == 2)

print("[6] handle_see_image を通した往復")
os.makedirs(os.path.join(_WS, "generated"), exist_ok=True)
big.save(os.path.join(_WS, "generated", "big.png"), "PNG")
Image.new("RGB", (300, 200), (90, 90, 90)).save(os.path.join(_WS, "generated", "small.png"), "PNG")
inorm._RECENT_SOURCES.clear()


def call(**kw):
    return asyncio.run(handle_see_image(workspace_path=_WS, **kw))


import json  # noqa: E402

res = call(source="generated/big.png")
env = json.loads(res)
check("封筒が返る", env.get("__see_image__") is True)
check("region 無しなら封筒に region は無い", "region" not in env)
res = call(source="last", region="3x3:5", question="線の数")
env = json.loads(res)
check('source="last" で同じ画像を開き直せる', env["source"] == "generated/big.png", env.get("source"))
check("封筒に region が残る", env["region"] == "3x3:5")
check("question はそのまま", env["question"] == "線の数")
check("拡大版が入っている", decode(env["image_url"]).size[0] * decode(env["image_url"]).size[1] <= 640000)
res = call(source="generated/big.png")
env = json.loads(res)
check("大きく縮めた画像には「寄れる」ことを添える",
      "region" in env["question"] and "2400x2400" in env["question"], env["question"][:80])
res = call(source="generated/small.png")
env = json.loads(res)
check("もともと小さい画像には添えない", "region" not in env["question"], env["question"][:60])

res = call(source="last", grid=True)
env = json.loads(res)
check("grid のときは番号の使い方を添える", "3x3" in env["question"], env["question"][:60])
res = call(source="generated/big.png", region="nazo")
check("不正な region はエラー文（封筒にしない）", "__see_image__" not in res and "region" in res, res[:60])
res = call(source="generated/nope.png")
check("存在しないファイルは従来どおりエラー", "__see_image__" not in res)

print("[7] 受け取った画像の保存")
orig = Image.new("RGB", (1500, 1000), (200, 30, 30))
b = io.BytesIO(); orig.save(b, "PNG")
data_url = "data:image/png;base64," + base64.b64encode(b.getvalue()).decode()
rel = inorm.save_received_image(data_url, _WS, 1)
check("received/ 配下に保存される", rel and rel.startswith("received/") and rel.endswith(".png"), str(rel))
saved_path = os.path.join(_WS, rel)
check("原本のまま（縮小しない）", Image.open(saved_path).size == (1500, 1000),
      str(Image.open(saved_path).size))
rel2 = inorm.save_received_image(data_url, _WS, 1)
check("同じ秒でも上書きしない", rel2 != rel and os.path.exists(os.path.join(_WS, rel2)))
check("data URL でなければ None", inorm.save_received_image("https://x/y.png", _WS) is None)
check("壊れていても落ちない", inorm.save_received_image("data:image/png;base64,@@@", _WS) is None)
# 保存した原本は see_image でそのまま開けて、拡大もできる
env = json.loads(call(source=rel, region="top_left"))
check("保存した原本を see_image で開ける", env.get("__see_image__") is True and env["source"] == rel)

print(f"\n{PASSED} passed, {len(FAILED)} failed")
if FAILED:
    print("failed:", FAILED)
    sys.exit(1)
