"""ページの見た目を1枚の絵にする（look コマンドの中身）。

テキストブラウザの散歩はそのままに、**気になったときだけ目を開ける**ための機能。
図・グラフ・写真・レイアウトに意味があるページ、本文抽出が空になるページで効く。

描画には**このPCに既に入っている Chrome（無ければ Edge）**をヘッドレスで使う。
Playwright を入れると Chromium が別に 300MB 増えるが、`--headless=new --screenshot`
なら追加の依存なしで同じことができる（実測 1.4 秒）。

安全について:
- 撮る URL は必ず `browser.http_get` を通った後のもの。あれはリダイレクトを自前で
  追い、**毎ホップ `check_url` し直す**ので、localhost や私有 IP へ誘導されない。
  この経路を迂回して生の URL を Chrome に渡さないこと（そこが唯一の守り）。
- 保険として `--host-resolver-rules` で localhost 名を潰し、プロファイルは毎回
  使い捨ての一時ディレクトリにする（カノンの Chrome の Cookie・履歴・拡張に触れない）。
- ダウンロード・音・拡張・バックグラウンド通信は全て切る。
"""

import os
import shutil
import subprocess
import tempfile
from pathlib import Path

# 見開き1画面の大きさ。1280x900 は「PC で普通に見たときの1画面」に近く、
# 縮小後（954x670 前後）でも日本語の本文が読める。
VIEWPORT_W = 1280
VIEWPORT_H = 900
# 何画面ぶんまで下に伸ばせるか（look の n）。伸ばすほど1画面あたりは小さく写る。
MAX_SCREENS = 8
# Chrome を待つ上限（秒）。manifest の timeout(60) より十分手前で切り上げる。
CAPTURE_TIMEOUT = 25
# 溜め込まない。これより古い絵から消す。
KEEP_SHOTS = 20

_CANDIDATES = (
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
)


def find_browser():
    """描画に使えるブラウザの実行ファイルを探す。無ければ None。"""
    for name in ("chrome", "msedge", "chromium", "google-chrome"):
        found = shutil.which(name)
        if found:
            return found
    for path in _CANDIDATES:
        if os.path.exists(path):
            return path
    return None


def _flags(user_data_dir, width, height, dest, user_agent):
    """ヘッドレス撮影のフラグ一式。

    `--headless=new` が要る。旧 --headless は chrome://newtab を開こうとしたり
    GCM 登録で固まったりして、実際に 2 分待っても返ってこなかった。
    """
    return [
        "--headless=new",
        "--disable-gpu",
        "--no-first-run",
        "--no-default-browser-check",
        "--no-sandbox",
        # 余計な通信・常駐を止める（固まる原因になる）
        "--disable-background-networking",
        "--disable-sync",
        "--disable-extensions",
        "--disable-component-update",
        "--disable-client-side-phishing-detection",
        "--disable-default-apps",
        "--mute-audio",
        # カノンの Chrome に触れないよう、毎回使い捨てのプロファイル
        f"--user-data-dir={user_data_dir}",
        # 保険: 名前で localhost に行けないようにする（IP 直書きは http_get 側で弾く）
        "--host-resolver-rules=MAP localhost ~NOTFOUND,MAP *.localhost ~NOTFOUND",
        f"--user-agent={user_agent}",
        f"--window-size={width},{height}",
        "--hide-scrollbars",
        f"--screenshot={dest}",
        # JS のタイマーを早送りして「描き終わり」を待つ。待ちっぱなしを防ぐ上限でもある。
        "--virtual-time-budget=8000",
    ]


def capture(url, dest_path, screen=1, user_agent="", browser_path=None):
    """url を1枚の絵にして dest_path（.png）へ書く。

    screen: 1 なら最初の1画面。2 以上なら窓を縦に伸ばして下のほうを写す。
    戻り値: (ok: bool, reason: str|None)
      reason は "no_browser" / "timeout" / "failed" / "empty" のいずれか。
    """
    exe = browser_path or find_browser()
    if not exe:
        return False, "no_browser"

    screen = max(1, min(int(screen or 1), MAX_SCREENS))
    height = VIEWPORT_H * screen

    dest = Path(dest_path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        dest.unlink()

    user_data_dir = tempfile.mkdtemp(prefix="tsukimi_shot_")
    try:
        cmd = [exe] + _flags(user_data_dir, VIEWPORT_W, height, str(dest), user_agent) + [url]
        try:
            subprocess.run(cmd, capture_output=True, timeout=CAPTURE_TIMEOUT,
                           check=False)
        except subprocess.TimeoutExpired:
            return False, "timeout"
        if not dest.exists():
            return False, "failed"
        if dest.stat().st_size == 0:
            dest.unlink()
            return False, "empty"
        return True, None
    finally:
        shutil.rmtree(user_data_dir, ignore_errors=True)


def crop_screen(path, screen):
    """縦に伸ばして撮った絵から、screen 番目の1画面ぶんだけを切り出して上書きする。

    Chrome の CLI には「途中までスクロールして撮る」機能が無いので、窓を縦に伸ばして
    撮ってから下端を切り出す。Pillow が無ければ何もしない（伸びたまま渡る）。
    """
    if screen <= 1:
        return True
    try:
        from PIL import Image
    except Exception:
        return False
    try:
        with Image.open(path) as im:
            w, h = im.size
            top = min(VIEWPORT_H * (screen - 1), max(0, h - VIEWPORT_H))
            im.crop((0, top, w, min(h, top + VIEWPORT_H))).save(path)
        return True
    except Exception:
        return False


def prune(shots_dir, keep=KEEP_SHOTS):
    """古い絵を片付ける（撮りっぱなしで workspace を太らせない）。"""
    try:
        files = sorted(Path(shots_dir).glob("*.png"), key=lambda p: p.stat().st_mtime)
        for old in files[:-keep]:
            old.unlink(missing_ok=True)
    except Exception:
        pass
