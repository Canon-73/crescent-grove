"""ComfyUI の HTTP API クライアントと、Krea 2 用グラフの組み立て。

標準ライブラリ（urllib）だけで書く。サテライトは torch も diffusers も
ComfyUI の venv も持たない ＝ 依存は「ポートが生きているか」だけ。
"""
import json
import socket
import urllib.error
import urllib.parse
import urllib.request

import config


class ComfyDown(Exception):
    """ComfyUI に繋がらない（起動していない / 起動途中）。"""


class ComfyError(Exception):
    """ComfyUI が応答したが、内容がエラー。"""


def _request(method: str, path: str, body=None, timeout: float = 10.0):
    """ComfyUI に HTTP リクエストを投げ、(status, bytes) を返す。

    繋がらない場合は ComfyDown を投げる（＝起動していない、で扱う）。
    """
    url = config.BASE_URL + path
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        # 応答はあるのでダウンではない。本文をそのまま上に渡す
        return e.code, e.read()
    except (urllib.error.URLError, socket.timeout, ConnectionError, OSError) as e:
        raise ComfyDown(str(e))


def _json_request(method: str, path: str, body=None, timeout: float = 10.0):
    """レスポンス本文を JSON として読む。本文が空なら None を返す。

    注意: POST /free は 200 を返すが本文が空。JSON 解析すると落ちるのでここで吸収する。
    """
    status, raw = _request(method, path, body, timeout)
    if not raw:
        return status, None
    try:
        return status, json.loads(raw)
    except ValueError:
        return status, None


def is_alive(timeout: float = 2.0) -> bool:
    """インスタンスが応答するか。"""
    try:
        status, _ = _request("GET", "/system_stats", timeout=timeout)
        return status == 200
    except ComfyDown:
        return False


def system_stats(timeout: float = 3.0):
    status, data = _json_request("GET", "/system_stats", timeout=timeout)
    return data


def queue_state(timeout: float = 3.0):
    """(実行中の件数, 待機中の件数) を返す。"""
    status, data = _json_request("GET", "/queue", timeout=timeout)
    if not isinstance(data, dict):
        return 0, 0
    return len(data.get("queue_running") or []), len(data.get("queue_pending") or [])


def free_memory(timeout: float = 10.0):
    """VRAM を解放する（プロセスは生かしたまま）。本文は空で返る。"""
    _request("POST", "/free", {"unload_models": True, "free_memory": True}, timeout=timeout)


def submit(graph: dict, client_id: str = "atelier", timeout: float = 15.0) -> str:
    """グラフを投入し prompt_id を返す。実測 0.03 秒で返る（生成完了は待たない）。"""
    status, data = _json_request("POST", "/prompt", {"prompt": graph, "client_id": client_id},
                                 timeout=timeout)
    if not isinstance(data, dict) or not data.get("prompt_id"):
        raise ComfyError(f"投入に失敗しました (HTTP {status}): {json.dumps(data, ensure_ascii=False)[:400]}")
    node_errors = data.get("node_errors")
    if node_errors:
        raise ComfyError(f"グラフが受け付けられませんでした: {json.dumps(node_errors, ensure_ascii=False)[:400]}")
    return data["prompt_id"]


def history(prompt_id: str, timeout: float = 10.0):
    """完了していれば履歴 dict、未完了なら None を返す。"""
    status, data = _json_request("GET", f"/history/{urllib.parse.quote(prompt_id)}",
                                 timeout=timeout)
    if not isinstance(data, dict) or prompt_id not in data:
        return None
    return data[prompt_id]


def interrupt(timeout: float = 5.0):
    """実行中のジョブを中断する。"""
    _request("POST", "/interrupt", {}, timeout=timeout)


def delete_queued(prompt_id: str, timeout: float = 5.0):
    """待機中のジョブを待ち行列から削除する。"""
    _request("POST", "/queue", {"delete": [prompt_id]}, timeout=timeout)


def fetch_image(filename: str, subfolder: str, ftype: str = "output",
                timeout: float = 30.0) -> bytes:
    """生成された画像のバイト列を取得する。"""
    qs = urllib.parse.urlencode({"filename": filename, "subfolder": subfolder or "", "type": ftype})
    status, raw = _request("GET", f"/view?{qs}", timeout=timeout)
    if status != 200 or not raw:
        raise ComfyError(f"画像の取得に失敗しました (HTTP {status})")
    return raw


def parse_outputs(hist: dict):
    """履歴から (状態, 画像リスト, エラー文) を取り出す。

    状態は "success" / "error" / その他（ComfyUI の status_str をそのまま）。
    """
    status = (hist.get("status") or {})
    status_str = status.get("status_str") or "unknown"
    images = []
    for node_out in (hist.get("outputs") or {}).values():
        for img in (node_out.get("images") or []):
            images.append(img)
    error_text = ""
    for m in (status.get("messages") or []):
        # messages は ["execution_error", {...}] 形式のペア
        if isinstance(m, (list, tuple)) and len(m) >= 2 and m[0] in ("execution_error", "execution_interrupted"):
            error_text = json.dumps(m[1], ensure_ascii=False)[:600]
    return status_str, images, error_text


def build_graph(prompt: str, negative: str, width: int, height: int,
                steps: int, cfg: float, seed: int, filename_prefix: str) -> dict:
    """Krea 2 の API 形式グラフを組み立てる。

    Krea2_Base.json（動作実績のあるワークフロー）と同じ構成:
        UNETLoader → KSampler → VAEDecode → SaveImage
        CLIPLoader → CLIPTextEncode ↗
        VAELoader  ───────────────↗

    prompt は自然文でも JSON 領域指定でも、そのまま CLIPTextEncode に渡すだけでよい
    （テキストエンコーダの Qwen3-VL が文字列として解釈する）。別コードパスは不要。
    """
    return {
        "1": {"class_type": "UNETLoader",
              "inputs": {"unet_name": config.UNET_NAME, "weight_dtype": "default"}},
        "2": {"class_type": "CLIPLoader",
              "inputs": {"clip_name": config.CLIP_NAME, "type": config.CLIP_TYPE,
                         "device": "default"}},
        "3": {"class_type": "VAELoader", "inputs": {"vae_name": config.VAE_NAME}},
        "5": {"class_type": "CLIPTextEncode", "inputs": {"text": prompt, "clip": ["2", 0]}},
        "6": {"class_type": "CLIPTextEncode", "inputs": {"text": negative or "", "clip": ["2", 0]}},
        "7": {"class_type": "EmptyLatentImage",
              "inputs": {"width": width, "height": height, "batch_size": 1}},
        "8": {"class_type": "KSampler",
              "inputs": {"seed": seed, "steps": steps, "cfg": cfg,
                         "sampler_name": config.SAMPLER, "scheduler": config.SCHEDULER,
                         "denoise": 1.0,
                         "model": ["1", 0], "positive": ["5", 0], "negative": ["6", 0],
                         "latent_image": ["7", 0]}},
        "9": {"class_type": "VAEDecode", "inputs": {"samples": ["8", 0], "vae": ["3", 0]}},
        "10": {"class_type": "SaveImage",
               "inputs": {"filename_prefix": filename_prefix, "images": ["9", 0]}},
    }
