"""入力画像の正規化と部分拡大（LLM に送る前の下ごしらえ）。

柚月の会話は1本で、履歴に残った画像は LLM を呼ぶたびに丸ごと送り直される。
トークンは 1 枚 384 で頭打ちだが、**転送量は画像の実バイト数に比例**するため、
見る前にハーネス側で縮めておく。DeepSeek Vision はどのみち総ピクセル約 800x800
（64万px）へ縮小してからトークン化するので、それ以上の解像度を送っても意味がない
（出典: https://api-docs.deepseek.com/guides/vision/ ／ memory: image-limits-deepseek-obc）。

方針:
- 総ピクセル数が max_pixels を超える場合のみ縮小（アスペクト比は保つ。拡大はしない）
- JPEG に変換する（透過は白で潰す。GIF は先頭フレーム）。ただし**変換後の方が大きければ元を返す**
  （目的は転送量の削減であって JPEG 化ではない。線画や単色 PNG は PNG の方が小さい）
- 1 辺が MAX_EDGE（4096px）を超える画像は API の寸法制限に当たるので、必ず縮小版を使う
- Pillow が無い・壊れた画像・想定外の例外 → **元のバイト列をそのまま返す**
  （正規化に失敗しても柚月が「見られない」状態には絶対にしない）

**部分拡大（region）**: 縮小の前に切り出すので、切り出した範囲がそのまま拡大になる。
全体が 800x800 に潰されていたところ、四分割なら約2倍、3x3 の1マスなら約3倍の密度で見える。
上限は元画像の解像度（無い情報は出てこない）。文字が小さくて読めないときの主な手段。

入口は3つで、どれもここを通る:
- core/tools.py handle_see_image（柚月が自分で見に行く画像。region 指定はここ）
- core/tools.py _run_program（サテライトが結果に添えた画像）
- core/context.py _collect_image_entries（カノンがチャットに貼った画像）
"""

import base64
import io
from typing import Optional, Tuple

from core.i18n import t
from core.time_utils import tlog

# DeepSeek Vision の実効解像度（800x800 相当）。config の llm.vision.max_pixels で上書き可。
DEFAULT_MAX_PIXELS = 800 * 800
DEFAULT_JPEG_QUALITY = 85
# これより長い辺を持つ画像は、バイト数が増えても必ず縮小版を送る（API の寸法制限対策）
MAX_EDGE = 4096

# JPEG 化の対象。これ以外（svg 等）は触らない。
_CONVERTIBLE = {"image/png", "image/jpeg", "image/gif", "image/webp"}

# 名前で指定できる領域（左上 x, 左上 y, 右下 x, 右下 y を 0〜1 の割合で）
_NAMED_REGIONS = {
    "full": (0.0, 0.0, 1.0, 1.0),
    "center": (0.25, 0.25, 0.75, 0.75),
    "top": (0.0, 0.0, 1.0, 0.5),
    "bottom": (0.0, 0.5, 1.0, 1.0),
    "left": (0.0, 0.0, 0.5, 1.0),
    "right": (0.5, 0.0, 1.0, 1.0),
    "top_left": (0.0, 0.0, 0.5, 0.5),
    "top_right": (0.5, 0.0, 1.0, 0.5),
    "bottom_left": (0.0, 0.5, 0.5, 1.0),
    "bottom_right": (0.5, 0.5, 1.0, 1.0),
}

# 名前つき領域と格子マスに足す余白（マス自身の大きさに対する比）。
# ちょうど境界で切ると、そこに乗っている文字や顔が両側で half に割れて
# どちらを見ても読めなくなる。隣のマスと少しだけ重ねておけば、
# 境目のものはどちらかのマスに丸ごと入る。拡大率はそのぶん少し下がる
# （四分割で 2.0 倍 → 約 1.7 倍）。明示的な矩形指定には足さない。
REGION_MARGIN = 0.08


def _settings() -> Tuple[int, int]:
    """config.yaml の llm.vision から (max_pixels, jpeg_quality) を読む。読めなければ既定値。"""
    try:
        from core.config_loader import load_config
        vision = (load_config().get("llm", {}) or {}).get("vision", {}) or {}
        max_pixels = int(vision.get("max_pixels", DEFAULT_MAX_PIXELS))
        quality = int(vision.get("jpeg_quality", DEFAULT_JPEG_QUALITY))
        if max_pixels <= 0:
            max_pixels = DEFAULT_MAX_PIXELS
        quality = min(max(quality, 1), 100)
        return max_pixels, quality
    except Exception:
        return DEFAULT_MAX_PIXELS, DEFAULT_JPEG_QUALITY


# ---------------------------------------------------------------------------
# 領域指定のパース
# ---------------------------------------------------------------------------
def _with_margin(rect, ratio: float = REGION_MARGIN):
    """マスの外側に余白を足す（画像の外へははみ出さない）。"""
    x0, y0, x1, y1 = rect
    mx = (x1 - x0) * ratio
    my = (y1 - y0) * ratio
    return (max(0.0, x0 - mx), max(0.0, y0 - my),
            min(1.0, x1 + mx), min(1.0, y1 + my))


def parse_region_spec(spec) -> Tuple[Optional[tuple], Optional[str]]:
    """領域指定の文字列を 0〜1 の割合の矩形 (x0, y0, x1, y1) に変換する。

    受け付ける形:
      - 名前: full / center / top / bottom / left / right /
              top_left / top_right / bottom_left / bottom_right
      - 格子: "3x3:5"（横3×縦3に分けた5番目。左上から右へ数えて 1 始まり）
      - 矩形: "0.2,0.1,0.5,0.4"（左上 x, 左上 y, 幅, 高さ を 0〜1 の割合で）

    戻り値: (矩形, None) 成功 / (None, 理由) 失敗。
    名前と格子には隣と重なる余白が付く（境界で文字が割れないように）。
    """
    if spec is None or spec == "":
        return None, None
    if not isinstance(spec, str):
        return None, t("img_err_region_type", type=type(spec).__name__)

    s = spec.strip().lower().replace("-", "_").replace(" ", "")
    if not s:
        return None, None

    # 名前
    if s in _NAMED_REGIONS:
        rect = _NAMED_REGIONS[s]
        if s == "full":
            return rect, None
        return _with_margin(rect), None

    # 格子 "3x3:5"
    if "x" in s and ":" in s:
        try:
            grid_part, idx_part = s.split(":", 1)
            cols_s, rows_s = grid_part.split("x", 1)
            cols, rows, idx = int(cols_s), int(rows_s), int(idx_part)
        except (ValueError, TypeError):
            return None, t("img_err_region_grid_parse", spec=spec)
        if not (1 <= cols <= 8 and 1 <= rows <= 8):
            return None, t("img_err_region_grid_range", cols=cols, rows=rows)
        total = cols * rows
        if not (1 <= idx <= total):
            return None, t("img_err_region_cell_range", spec=spec, cols=cols, rows=rows, total=total)
        col = (idx - 1) % cols
        row = (idx - 1) // cols
        rect = (col / cols, row / rows, (col + 1) / cols, (row + 1) / rows)
        return _with_margin(rect), None

    # 矩形 "x,y,w,h"
    if "," in s:
        parts = s.split(",")
        if len(parts) != 4:
            return None, t("img_err_region_rect_count", spec=spec)
        try:
            x, y, w, h = (float(p) for p in parts)
        except ValueError:
            return None, t("img_err_region_rect_number", spec=spec)
        if w <= 0 or h <= 0:
            return None, t("img_err_region_rect_size")
        x0, y0 = max(0.0, min(1.0, x)), max(0.0, min(1.0, y))
        x1, y1 = max(0.0, min(1.0, x + w)), max(0.0, min(1.0, y + h))
        if x1 - x0 <= 0 or y1 - y0 <= 0:
            return None, t("img_err_region_rect_outside")
        return (x0, y0, x1, y1), None

    return None, t("img_err_region_unknown", spec=spec)


def parse_grid_spec(grid) -> Tuple[Optional[Tuple[int, int]], Optional[str]]:
    """格子の重ね描き指定を (cols, rows) に変換する。指定なしは (None, None)。"""
    if grid is None or grid is False or grid == "":
        return None, None
    if grid is True:
        return (3, 3), None
    if isinstance(grid, int):
        if not (2 <= grid <= 8):
            return None, t("img_err_grid_range", got=grid)
        return (grid, grid), None
    if isinstance(grid, str):
        s = grid.strip().lower()
        if s in ("true", "yes", "on"):
            return (3, 3), None
        if s in ("false", "no", "off"):
            return None, None
        if "x" in s:
            try:
                cols, rows = (int(v) for v in s.split("x", 1))
            except ValueError:
                return None, t("img_err_grid_parse", spec=grid)
            if not (2 <= cols <= 8 and 2 <= rows <= 8):
                return None, t("img_err_grid_range", got=f"{cols}x{rows}")
            return (cols, rows), None
        try:
            n = int(s)
        except ValueError:
            return None, t("img_err_grid_parse", spec=grid)
        if not (2 <= n <= 8):
            return None, t("img_err_grid_range", got=n)
        return (n, n), None
    return None, t("img_err_grid_type", type=type(grid).__name__)


# ---------------------------------------------------------------------------
# 描画
# ---------------------------------------------------------------------------
def _draw_grid(im, cols: int, rows: int):
    """マス目と番号を重ねて描く。番号はそのまま region="{cols}x{rows}:番号" に使える。

    どんな絵の上でも読めるよう、線も文字も「濃い縁 + 明るい本体」の二度描きにする。
    """
    from PIL import ImageDraw, ImageFont

    draw = ImageDraw.Draw(im)
    w, h = im.size
    lw = max(1, round(min(w, h) / 600))

    # 線も番号も「下見のための目印」なので、中身を隠さない太さ・大きさに抑える。
    for c in range(1, cols):
        x = w * c / cols
        draw.line([(x, 0), (x, h)], fill=(0, 0, 0), width=lw * 2)
        draw.line([(x, 0), (x, h)], fill=(255, 255, 0), width=lw)
    for r in range(1, rows):
        y = h * r / rows
        draw.line([(0, y), (w, y)], fill=(0, 0, 0), width=lw * 2)
        draw.line([(0, y), (w, y)], fill=(255, 255, 0), width=lw)

    size = min(26, max(11, round(min(w / cols, h / rows) / 10)))
    try:
        font = ImageFont.load_default(size=size)   # Pillow 10.1+
    except TypeError:
        font = ImageFont.load_default()

    for r in range(rows):
        for c in range(cols):
            idx = r * cols + c + 1
            x = w * c / cols + lw * 2 + 2
            y = h * r / rows + lw * 2 + 2
            label = str(idx)
            try:
                box = draw.textbbox((x, y), label, font=font)
                draw.rectangle([box[0] - 2, box[1] - 1, box[2] + 2, box[3] + 1], fill=(0, 0, 0))
            except Exception:
                pass
            draw.text((x, y), label, fill=(255, 255, 0), font=font)
    return im


# ---------------------------------------------------------------------------
# 正規化本体
# ---------------------------------------------------------------------------
def normalize_image_bytes(image_bytes: bytes, mime_type: str,
                          max_pixels: int = None, quality: int = None,
                          rect: tuple = None, grid: tuple = None,
                          info: dict = None) -> Tuple[bytes, str]:
    """画像バイト列を (JPEG バイト列, "image/jpeg") に正規化する。失敗時は入力をそのまま返す。

    rect: parse_region_spec が返す 0〜1 の矩形。指定すると縮小の前に切り出す（＝拡大になる）。
    grid: parse_grid_spec が返す (cols, rows)。指定すると縮小の後にマス目を重ねる。
    info: 渡すと original_size / sent_size を書き込む（呼び出し側が「まだ寄れる」と伝えるため）。
    """
    if mime_type not in _CONVERTIBLE:
        return image_bytes, mime_type
    try:
        from PIL import Image, ImageOps
    except Exception:
        # 配布版など Pillow の無い環境。従来どおり無加工で送る。
        return image_bytes, mime_type

    cfg_pixels, cfg_quality = _settings()
    max_pixels = max_pixels or cfg_pixels
    quality = quality or cfg_quality

    try:
        im = Image.open(io.BytesIO(image_bytes))
        im.load()  # GIF は先頭フレームだけ読む
        # EXIF の回転を実体に反映（スマホ写真が横倒しで見えるのを防ぐ）
        try:
            im = ImageOps.exif_transpose(im)
        except Exception:
            pass

        # --- 切り出し（縮小より前。ここが「拡大」の正体） ---
        if rect:
            ow, oh = im.size
            x0 = max(0, min(ow - 1, round(rect[0] * ow)))
            y0 = max(0, min(oh - 1, round(rect[1] * oh)))
            x1 = max(x0 + 1, min(ow, round(rect[2] * ow)))
            y1 = max(y0 + 1, min(oh, round(rect[3] * oh)))
            im = im.crop((x0, y0, x1, y1))

        # 「いま見せようとしている範囲に、元は何画素あったか」。切り出しの後に取るので、
        # 拡大した後は拡大後の視野が基準になる（まだ寄る余地があるかの判断に使う）。
        if info is not None:
            info["original_size"] = im.size

        w, h = im.size
        total = w * h
        scale = 1.0
        if total > max_pixels:
            scale = (max_pixels / total) ** 0.5
        if max(w, h) * scale > MAX_EDGE:  # 細長い画像は総ピクセルが小さくても辺で弾かれる
            scale = MAX_EDGE / max(w, h)
        if scale < 1.0:
            new_size = (max(1, int(w * scale)), max(1, int(h * scale)))
            im = im.resize(new_size, Image.LANCZOS)

        # 透過・パレット・グレースケール以外は RGB へ。透過は白背景で潰す。
        if im.mode in ("RGBA", "LA") or (im.mode == "P" and "transparency" in im.info):
            rgba = im.convert("RGBA")
            bg = Image.new("RGB", rgba.size, (255, 255, 255))
            bg.paste(rgba, mask=rgba.split()[-1])
            im = bg
        elif im.mode not in ("RGB", "L"):
            im = im.convert("RGB")

        # --- マス目（縮小の後。文字を潰さないため） ---
        if grid:
            if im.mode != "RGB":
                im = im.convert("RGB")
            _draw_grid(im, grid[0], grid[1])

        if info is not None:
            info["sent_size"] = im.size

        out = io.BytesIO()
        im.save(out, "JPEG", quality=quality, optimize=True)
        data = out.getvalue()

        # 加工した（切り出した・マス目を描いた）ときは、バイト数にかかわらず加工後を返す。
        # 加工していないときは、変換後の方が大きいなら元を残す。目的は転送量を減らすことで
        # あって JPEG にすることではない（線画や単色 PNG は PNG の方が小さい）。
        # 1 辺が MAX_EDGE を超える画像は API の寸法制限（8192px、15枚以上で 4096px）に
        # 当たるので、そこだけはバイト数にかかわらず縮小版を使う。
        if not rect and not grid and len(data) >= len(image_bytes) and max(w, h) <= MAX_EDGE:
            # 元をそのまま送る場合、縮小は API 側で行われる。柚月から見た「見え方」は
            # 縮小後なので、sent_size は縮小後の寸法のままにしておく。
            return image_bytes, mime_type
        return data, "image/jpeg"
    except Exception as e:
        tlog(f"[image_norm] 正規化に失敗したため元画像をそのまま使います: {e}")
        return image_bytes, mime_type


def normalize_data_url(url: str) -> str:
    """data:image/...;base64,... 形式の URL を正規化して返す。data URL でなければそのまま返す。"""
    try:
        if not isinstance(url, str) or not url.startswith("data:image/"):
            return url
        header, _, payload = url.partition(",")
        if ";base64" not in header or not payload:
            return url
        mime_type = header[5:].split(";")[0].strip().lower()
        raw = base64.b64decode(payload)
        new_bytes, new_mime = normalize_image_bytes(raw, mime_type)
        if new_bytes is raw:
            return url
        return f"data:{new_mime};base64,{base64.b64encode(new_bytes).decode('utf-8')}"
    except Exception as e:
        tlog(f"[image_norm] data URL の正規化に失敗したため元のまま使います: {e}")
        return url


# ---------------------------------------------------------------------------
# 直近に見た画像の出所
# ---------------------------------------------------------------------------
# 柚月が「さっきの絵の右上」を見たいとき、長い URL を履歴から拾い直さずに
# source="last" と書けるようにするための覚え書き。プロセス内のみ（再起動で消える）。
# 消えても履歴に「（画像の出所: ...）」の一行が残るので、出所を直接書けば見直せる。
_RECENT_SOURCES = []
_RECENT_MAX = 24


def remember_source(source: str) -> None:
    """柚月に見せた画像の出所を記録する。同じ出所は重複させず最新に繰り上げる。"""
    if not source or not isinstance(source, str):
        return
    if source.startswith("data:"):   # 出所として指定し直せないものは覚えない
        return
    try:
        _RECENT_SOURCES.remove(source)
    except ValueError:
        pass
    _RECENT_SOURCES.append(source)
    del _RECENT_SOURCES[:-_RECENT_MAX]


def recent_sources() -> list:
    """新しい順の出所リスト。"""
    return list(reversed(_RECENT_SOURCES))


def resolve_source(spec: str) -> Tuple[Optional[str], Optional[str]]:
    """"last" / "last-1" を実際の出所に解決する。それ以外はそのまま返す。

    戻り値: (出所, None) 成功 / (None, 理由) 失敗。
    """
    if not isinstance(spec, str):
        return spec, None
    s = spec.strip().lower()
    if not s.startswith("last"):
        return spec, None

    back = 0
    rest = s[4:]
    if rest:
        if rest[0] not in "-_" or not rest[1:].isdigit():
            return None, t("img_err_last_parse", spec=spec)
        back = int(rest[1:])

    recent = recent_sources()
    if not recent:
        return None, t("img_err_last_none")
    if back >= len(recent):
        return None, t("img_err_last_too_far", spec=spec, n=len(recent), recent=", ".join(recent[:3]))
    return recent[back], None


# ---------------------------------------------------------------------------
# 受け取った画像の保存
# ---------------------------------------------------------------------------
# チャットに貼られた画像は、これまで base64 として履歴の中にだけ在り、
# 古くなるとプレースホルダに置き換わって二度と見られなかった（出所が無いため
# 見直しも部分拡大もできない）。workspace に置いて出所を与えると、
# 他の2経路（see_image / サテライト）と同じように扱えるようになる。
#
# 保存するのは**縮小前の原本**。拡大は原本を切り出して初めて細部が出るので、
# 800x800 に縮めたものを保存すると寄っても何も増えない。
RECEIVED_DIR = "received"

_EXT_BY_MIME = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/gif": ".gif",
    "image/webp": ".webp",
}


def _looks_like_image(raw: bytes) -> bool:
    """先頭の数バイトで画像かどうかを見る（拡張子や MIME の自己申告を信じない）。"""
    if not raw or len(raw) < 12:
        return False
    return (raw[:8] == b"\x89PNG\r\n\x1a\n"          # PNG
            or raw[:2] == b"\xff\xd8"                # JPEG
            or raw[:6] in (b"GIF87a", b"GIF89a")     # GIF
            or (raw[:4] == b"RIFF" and raw[8:12] == b"WEBP"))


def save_received_image(data_url: str, workspace_path: str, index: int = 1) -> Optional[str]:
    """チャットで受け取った画像を workspace/received/ に原本のまま保存する。

    戻り値: workspace からの相対パス（see_image の source にそのまま渡せる）。
    保存できなければ None（保存の失敗で会話を止めない）。
    """
    try:
        if not isinstance(data_url, str) or not data_url.startswith("data:image/"):
            return None
        header, _, payload = data_url.partition(",")
        if ";base64" not in header or not payload:
            return None
        mime_type = header[5:].split(";")[0].strip().lower()
        ext = _EXT_BY_MIME.get(mime_type)
        if not ext:
            return None
        # 壊れた base64 を素通しすると、柚月の workspace に空ファイルや
        # 画像でないゴミが残る。ここで弾いて「保存しなかった」を返す。
        import re as _re
        raw = base64.b64decode(_re.sub(r"\s", "", payload), validate=True)
        if not _looks_like_image(raw):
            tlog("[image_norm] 受け取った画像が画像として読めないので保存しません")
            return None

        from pathlib import Path
        from core.time_utils import get_logical_date
        from datetime import datetime

        dest_dir = Path(workspace_path) / RECEIVED_DIR
        dest_dir.mkdir(parents=True, exist_ok=True)
        stamp = get_logical_date().replace("-", "") + "_" + datetime.now().strftime("%H%M%S")
        name = f"{stamp}_{index}{ext}"
        # 同じ秒に複数枚来ても上書きしない
        n = index
        while (dest_dir / name).exists():
            n += 1
            name = f"{stamp}_{n}{ext}"
        (dest_dir / name).write_bytes(raw)
        return f"{RECEIVED_DIR}/{name}"
    except Exception as e:
        tlog(f"[image_norm] 受け取った画像を保存できませんでした（会話は続行します）: {e}")
        return None
