"""core/image_norm.py のテスト（自前ランナー・pytest 不要）。

実行: venv\\Scripts\\python.exe tests\\test_image_norm.py

サーバにも柚月の workspace にも触らない。画像はすべてメモリ上で作る。
"""
import base64
import io
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8")

from PIL import Image  # noqa: E402

from core.image_norm import normalize_image_bytes, normalize_data_url  # noqa: E402

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


import random
random.seed(1)


def make(mode, size, fmt, noise=True, **kw):
    """テスト画像。noise=True は写真相当（乱数）、False は単色（よく圧縮される）。"""
    if noise:
        n = len(mode)
        im = Image.frombytes(mode, size, bytes(random.getrandbits(8) for _ in range(size[0] * size[1] * n)))
        if "alpha" in kw:
            im.putalpha(kw.pop("alpha"))
    else:
        im = Image.new(mode, size, kw.pop("color", (200, 80, 30) if mode in ("RGB", "RGBA") else 128))
    buf = io.BytesIO()
    im.save(buf, fmt, **kw)
    return buf.getvalue()


def open_bytes(b):
    return Image.open(io.BytesIO(b))


print("[1] 大きい PNG は JPEG になり、総ピクセルが上限以下へ縮む")
src = make("RGB", (2048, 2048), "PNG")
out, mime = normalize_image_bytes(src, "image/png", max_pixels=640000)
im = open_bytes(out)
check("mime は image/jpeg", mime == "image/jpeg", mime)
check("フォーマットは JPEG", im.format == "JPEG", im.format)
check("総ピクセルが 64万以下", im.size[0] * im.size[1] <= 640000, str(im.size))
check("正方形は 800x800", im.size == (800, 800), str(im.size))
check("バイト数が減る", len(out) < len(src), f"{len(src)} -> {len(out)}")

print("[2] アスペクト比が保たれる（16:9）")
src = make("RGB", (3840, 2160), "PNG")
out, _ = normalize_image_bytes(src, "image/png", max_pixels=640000)
im = open_bytes(out)
ratio = im.size[0] / im.size[1]
check("比率 ≈ 16:9", abs(ratio - 16 / 9) < 0.02, str(im.size))
check("総ピクセルが上限以下", im.size[0] * im.size[1] <= 640000, str(im.size))

print("[3] 小さい画像は拡大しない")
src = make("RGB", (300, 200), "PNG")
out, mime = normalize_image_bytes(src, "image/png", max_pixels=640000)
im = open_bytes(out)
check("寸法そのまま", im.size == (300, 200), str(im.size))
check("大きくならない", len(out) <= len(src), f"{len(src)} -> {len(out)}")

print("[3b] よく圧縮された PNG は JPEG にすると太るので元のまま")
# 2値ノイズ（線画・文字に近い）。PNG は得意で JPEG は苦手な画像。
_bw = Image.frombytes("1", (1024, 1024), bytes(random.getrandbits(8) for _ in range(1024 * 1024 // 8))).convert("RGB")
_buf = io.BytesIO()
_bw.save(_buf, "PNG", optimize=True)
src = _buf.getvalue()
out, mime = normalize_image_bytes(src, "image/png", max_pixels=640000)
check("元の PNG が返る", out is src and mime == "image/png", f"{len(src)} -> {len(out)} {mime}")

print("[4] 透過 PNG は白で潰される")
src = make("RGB", (100, 100), "PNG", alpha=0)  # 全面透明なノイズ
out, _ = normalize_image_bytes(src, "image/png")
im = open_bytes(out).convert("RGB")
check("左上が白", im.getpixel((0, 0)) == (255, 255, 255), str(im.getpixel((0, 0))))

print("[5] GIF / WebP も JPEG になる")
for fmt, mime_in in (("GIF", "image/gif"), ("WEBP", "image/webp")):
    src = make("RGB", (1200, 1200), fmt)
    out, mime = normalize_image_bytes(src, mime_in, max_pixels=640000)
    im = open_bytes(out)
    check(f"{fmt} -> JPEG", mime == "image/jpeg" and im.format == "JPEG")
    check(f"{fmt} 縮小", im.size == (800, 800), str(im.size))

print("[6] 小さい JPEG はそのまま（再圧縮で太らせない）")
src = make("RGB", (400, 400), "JPEG", quality=60)
out, mime = normalize_image_bytes(src, "image/jpeg")
check("バイト列が同一オブジェクト", out is src)

print("[7] 壊れた画像・未対応 MIME は素通し")
junk = b"not an image at all"
out, mime = normalize_image_bytes(junk, "image/png")
check("壊れた PNG は元のまま", out is junk and mime == "image/png")
out, mime = normalize_image_bytes(junk, "image/svg+xml")
check("svg は触らない", out is junk and mime == "image/svg+xml")

print("[8] data URL の正規化")
src = make("RGB", (1600, 1600), "PNG")
url = "data:image/png;base64," + base64.b64encode(src).decode()
new = normalize_data_url(url)
check("先頭が data:image/jpeg", new.startswith("data:image/jpeg;base64,"), new[:40])
decoded = base64.b64decode(new.split(",", 1)[1])
check("中身は 800x800 JPEG", open_bytes(decoded).size == (800, 800))
check("非 data URL は素通し", normalize_data_url("https://example.com/a.png") == "https://example.com/a.png")
check("壊れた data URL は素通し", normalize_data_url("data:image/png;base64,@@@") == "data:image/png;base64,@@@")
check("None は素通し", normalize_data_url(None) is None)

print("[8b] 1辺が 4096px を超える画像はバイト数が増えても縮小版になる")
src = make("RGB", (5000, 40), "PNG", noise=False, optimize=True)  # 細長い単色 PNG（数百バイト）
out, mime = normalize_image_bytes(src, "image/png")
check("JPEG 縮小版が返る", mime == "image/jpeg" and max(open_bytes(out).size) <= 4096, f"{mime} {open_bytes(out).size}")

print("[9] EXIF 回転が反映される")
im = Image.new("RGB", (400, 200), (10, 20, 30))
exif = Image.Exif()
exif[0x0112] = 6  # Rotate 90 CW
buf = io.BytesIO()
im.save(buf, "JPEG", exif=exif.tobytes(), quality=95)
out, _ = normalize_image_bytes(buf.getvalue(), "image/jpeg")
check("縦長になる", open_bytes(out).size == (200, 400), str(open_bytes(out).size))

print(f"\n{PASSED} passed, {len(FAILED)} failed")
if FAILED:
    print("failed:", FAILED)
    sys.exit(1)
