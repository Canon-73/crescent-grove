"""atelier サテライトの設定値。

環境変数で上書きできる（既定値はカノンの dev マシン向け）。
ここに出してあるのは「機械ごとに変わる値」だけで、柚月が触る必要はない。
"""
import os
from pathlib import Path

# --- ComfyUI 実行環境 ---
# 柚月専用インスタンス。カノンの ComfyUI（GPU0 / 8188 / D:\AI\output）とは
# カード・ポート・出力先を全て分けてあるので、どちらを再起動しても相手に影響しない。
COMFY_DIR = os.environ.get("CG_ATELIER_COMFY_DIR", r"D:\AI\ComfyUI")
COMFY_PYTHON = os.environ.get(
    "CG_ATELIER_COMFY_PYTHON", r"D:\AI\ComfyUI\.venv\Scripts\python.exe")
COMFY_HOST = os.environ.get("CG_ATELIER_HOST", "127.0.0.1")
COMFY_PORT = int(os.environ.get("CG_ATELIER_PORT", "8189"))
COMFY_GPU = os.environ.get("CG_ATELIER_GPU", "1")
COMFY_OUTPUT = os.environ.get("CG_ATELIER_OUTPUT", r"D:\AI\output_yuzuki")

BASE_URL = f"http://{COMFY_HOST}:{COMFY_PORT}"

# --- モデル一式（D:\AI\models。Krea2_Base.json と同じ組み合わせ）---
UNET_NAME = os.environ.get("CG_ATELIER_UNET", "krea2_turbo_fp8_scaled.safetensors")
CLIP_NAME = os.environ.get("CG_ATELIER_CLIP", "qwen3vl_4b_fp8_scaled.safetensors")
VAE_NAME = os.environ.get("CG_ATELIER_VAE", "qwen_image_vae.safetensors")
# CLIPLoader の type。Krea 2 は "krea2"
CLIP_TYPE = "krea2"

# --- 生成の既定値（Turbo 蒸留モデルの推奨設定）---
# steps は 8〜12、cfg は 1.0。Turbo は蒸留済みなので steps を増やしても良くならず、
# cfg を上げると彩度が飛ぶ（3.0 以上で破綻）。
DEFAULT_WIDTH = 1024
DEFAULT_HEIGHT = 1024
DEFAULT_STEPS = 8
DEFAULT_CFG = 1.0
SAMPLER = "euler"
SCHEDULER = "simple"
MAX_SIDE = 2048          # これ以上は生成時間が跳ねる（2048² で約 123 秒）
MIN_SIDE = 256

# --- アイドル管理（ランチャーが使う。秒）---
# FREE: VRAM だけ解放（プロセスは生存）。次の draw で再ロード（ページキャッシュから約 33 秒）
# STOP: ComfyUI ごと終了して GPU1 を完全に空ける
IDLE_FREE_SEC = int(os.environ.get("CG_ATELIER_IDLE_FREE_SEC", str(20 * 60)))
IDLE_STOP_SEC = int(os.environ.get("CG_ATELIER_IDLE_STOP_SEC", str(50 * 60)))
LAUNCHER_POLL_SEC = int(os.environ.get("CG_ATELIER_POLL_SEC", "30"))
# 起動してから応答を待つ上限（実測 12 秒）。これを過ぎたら起動失敗として扱う
BOOT_TIMEOUT_SEC = int(os.environ.get("CG_ATELIER_BOOT_TIMEOUT_SEC", "180"))

# --- workspace 内のパス ---
# 生成物は workspace 配下でなければ see_image / upload_artifact が受け付けない。
WORKSPACE = Path(os.environ.get("CG_WORKSPACE", ".")).resolve()
GENERATED_DIR = WORKSPACE / "generated"                       # 柚月の作品置き場（JPEG）
DATA_DIR = WORKSPACE / "program_data" / "atelier"             # 台帳・ランチャー状態
JOBS_FILE = DATA_DIR / "jobs.json"
LAUNCHER_FILE = DATA_DIR / "launcher.json"
LAUNCHER_LOG = DATA_DIR / "launcher.log"

# --- draw が完成を待つときの上限（秒）---
# core の _run_program が別スレッドで動くようになった（2026-08-22）ので、ここで待っても
# サーバは止まらない。ただし manifest の timeout に達すると subprocess ごと殺され、
# 柚月には job_id すら返らなくなるので、必ずその手前で自分から切り上げる。
WAIT_MAX_SEC = float(os.environ.get("CG_ATELIER_WAIT_MAX_SEC", "120"))
WAIT_POLL_SEC = float(os.environ.get("CG_ATELIER_WAIT_POLL_SEC", "2"))

# 台帳に残すジョブ件数の上限（古いものから捨てる）
MAX_JOBS = 200
# 生成物の JPEG 品質。q90 で PNG の約 1/7 の転送量、認識精度は同等（2026-08-21 実測）
JPEG_QUALITY = 90
