from datetime import datetime, timezone, timedelta, date as date_type
import os
from pathlib import Path

JST = timezone(timedelta(hours=9))

# 論理日付（午前3時境界）に使う可変タイムゾーンのオフセット（時間単位）。既定は JST(+9)。
# 一般設定 time.tz_offset に追従させるため、起動時（startup_event）と設定保存時
# （app_state.reload_runtime_config）に set_context_timezone() で更新される。
# コンテキストに注入する時刻表示（context.py）とこの論理日付が同じタイムゾーンで動く。
# ※ tlog() のログ記録時刻・日付別ログファイル名は従来どおり JST 固定（運用ログの一貫性のため）。
_context_tz_offset_hours = 9.0


def set_context_timezone(offset_hours):
    """論理日付（午前3時境界）に使うタイムゾーンのオフセット（時間単位）を設定する。

    一般設定 time.tz_offset に追従させるため、起動時と設定保存時に呼ばれる。
    不正値・範囲外（UTC-12〜+14外）は JST(+9) に丸める。
    """
    global _context_tz_offset_hours
    try:
        off = float(offset_hours)
    except (TypeError, ValueError):
        off = 9.0
    _context_tz_offset_hours = max(-12.0, min(14.0, off))


def _logical_tz() -> timezone:
    """論理日付の計算に使う現在のタイムゾーンを返す（set_context_timezone で可変）。"""
    return timezone(timedelta(hours=_context_tz_offset_hours))


def _console_log_dir() -> Path:
    """コンソールログの出力先（logs/console）を data_root 基準で遅延解決する。

    モジュール import 時に定数で固定すると set_data_root() より前にパスが確定し、
    配布版で install_root（読み取り専用になり得る）を向いてしまう罠になる。
    そのため呼び出しごとに data_root() から解決する。
    dev（data_root == bundle_root）では従来と同一パス（agent/logs/console）。
    """
    from core.paths import logs_root
    return logs_root() / "console"


def tlog(msg: str):
    """タイムスタンプ付きコンソール出力とファイル書き出し。"""
    now = datetime.now(JST)
    line = f"[{now.strftime('%H:%M:%S')}] {msg}"
    print(line)
    try:
        log_dir = _console_log_dir()
        log_dir.mkdir(parents=True, exist_ok=True)
        date_str = now.strftime("%Y-%m-%d")
        log_path = log_dir / f"{date_str}.log"
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass

def get_logical_date() -> str:
    """午前3時境界の論理日付を文字列で返す。3時前なら前日。

    タイムゾーンは一般設定（time.tz_offset）に追従する（既定 JST）。
    コンテキストに注入する時刻表示と同じタイムゾーンで「1日の区切り」が動く。
    """
    now = datetime.now(_logical_tz())
    if now.hour < 3:
        now = now - timedelta(days=1)
    return now.strftime("%Y-%m-%d")

def get_logical_date_obj() -> date_type:
    """午前3時境界の論理日付をdateオブジェクトで返す。

    タイムゾーンは一般設定（time.tz_offset）に追従する（既定 JST）。
    """
    now = datetime.now(_logical_tz())
    if now.hour < 3:
        now = now - timedelta(days=1)
    return now.date()
