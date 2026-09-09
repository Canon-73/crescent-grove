"""ジョブ台帳。workspace/program_data/atelier/jobs.json に持つ。

サテライトはコマンドごとに終了する subprocess なので、状態はメモリではなくディスクに置く。
柚月の会話履歴（RAW 2日 / Layer0 1ヶ月半）より台帳のほうが確実に残るため、
job_id を忘れても status（引数なし）で未回収のものを拾い直せる。
"""
import json
import os
import tempfile
import time
from pathlib import Path

import config


def _now() -> float:
    return time.time()


def _load_raw() -> dict:
    if not config.JOBS_FILE.exists():
        return {"jobs": [], "last_use": 0}
    try:
        with open(config.JOBS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return {"jobs": [], "last_use": 0}
        data.setdefault("jobs", [])
        data.setdefault("last_use", 0)
        return data
    except Exception:
        # 壊れていても柚月の作業を止めない。空台帳として続行する
        return {"jobs": [], "last_use": 0}


def _save_raw(data: dict) -> None:
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    # アトミック書き込み（tmp → replace）。書きかけを読ませない
    fd, tmp = tempfile.mkstemp(dir=str(config.DATA_DIR), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, config.JOBS_FILE)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def load() -> list:
    return _load_raw()["jobs"]


def touch_use() -> None:
    """最終利用時刻を更新する。ランチャーがアイドル判定に使う。"""
    data = _load_raw()
    data["last_use"] = _now()
    _save_raw(data)


def last_use() -> float:
    return _load_raw().get("last_use", 0)


def add(job: dict) -> None:
    data = _load_raw()
    data["jobs"].append(job)
    # 古いものから捨てて台帳を有界に保つ（画像本体は workspace/generated に残る）
    if len(data["jobs"]) > config.MAX_JOBS:
        data["jobs"] = data["jobs"][-config.MAX_JOBS:]
    data["last_use"] = _now()
    _save_raw(data)


def get(job_id: str):
    for j in load():
        if j.get("job_id") == job_id:
            return j
    return None


def update(job_id: str, **fields) -> bool:
    data = _load_raw()
    hit = False
    for j in data["jobs"]:
        if j.get("job_id") == job_id:
            j.update(fields)
            hit = True
            break
    if hit:
        data["last_use"] = _now()
        _save_raw(data)
    return hit


def pending() -> list:
    """未回収のジョブ（picked_up でなく cancelled でもないもの）を古い順に返す。"""
    return [j for j in load()
            if not j.get("picked_up") and j.get("state") not in ("cancelled",)]


def new_id() -> str:
    """短くて読める job_id。日時ベースなので柚月が見て順序が分かる。"""
    return time.strftime("%Y%m%d_%H%M%S", time.localtime())
