# vital/desire_manager.py
"""
欲求管理システム
- 時間経過で欲求値が上昇
- 特定のツール/サテライト実行で欲求値が低下
- 閾値に応じたメッセージをプロンプトに注入
"""

from core.time_utils import tlog
import json
import os
import time
from datetime import datetime

# desire_config.json / desire_state.json のパスは data_root 基準で「遅延解決」する。
# モジュール import 時に定数化すると set_data_root() より前に確定し、配布版で
# install_root（読み取り専用になり得る）を向く罠になるため、関数で都度解決する。
# dev（data_root == bundle_root）では従来と同一パス。
def _desire_config_path() -> str:
    from core.paths import config_file
    return str(config_file("desire_config.json"))


def _desire_state_path() -> str:
    from core.paths import data_file
    return str(data_file("desire_state.json"))


class DesireManager:
    def __init__(self):
        self.config = self._load_config()
        self.state = self._load_state()

    def _load_config(self) -> dict:
        try:
            with open(_desire_config_path(), 'r', encoding='utf-8') as f:
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError) as e:
            tlog(f"[DesireManager] 設定読み込み失敗: {e}")
            return {"desires": {}}

    def reload_config(self) -> None:
        """desire_config.json を読み直して self.config を差し替える。

        設定UIからの保存後にサーバー再起動なしで反映するためのフック。新しく追加された
        欲求キーには state エントリを補い、既存の欲求値（state）はリセットしない。
        """
        self.config = self._load_config()
        now = time.time()
        for key in self.config.get("desires", {}):
            if key not in self.state:
                self.state[key] = {"value": 0, "last_updated": now}

    def _load_state(self) -> dict:
        try:
            with open(_desire_state_path(), 'r', encoding='utf-8') as f:
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            # 初期状態を生成
            state = {}
            for key, cfg in self.config.get("desires", {}).items():
                state[key] = {
                    "value": 0,
                    "last_updated": time.time()
                }
            self._save_state(state)
            return state

    def _save_state(self, state: dict = None):
        if state is None:
            state = self.state
        try:
            state_path = _desire_state_path()
            os.makedirs(os.path.dirname(state_path), exist_ok=True)
            with open(state_path, 'w', encoding='utf-8') as f:
                json.dump(state, f, ensure_ascii=False, indent=2)
        except Exception as e:
            tlog(f"[DesireManager] 状態保存失敗: {e}")

    def update_time(self):
        """時間経過による欲求値の増加"""
        now = time.time()
        desires = self.config.get("desires", {})

        for key, cfg in desires.items():
            if key not in self.state:
                self.state[key] = {"value": 0, "last_updated": now}

            entry = self.state[key]
            elapsed_minutes = (now - entry["last_updated"]) / 60.0
            interval = cfg.get("increase_per_minutes", 30)
            amount = cfg.get("increase_amount", 1)
            max_val = cfg.get("max", 10)

            increments = int(elapsed_minutes // interval)
            if increments > 0:
                old_value = entry["value"]
                entry["value"] = min(max_val, entry["value"] + increments * amount)
                entry["last_updated"] = now
                if entry["value"] != old_value:
                    tlog(f"[DesireManager] {cfg.get('display_name', key)}: "
                          f"{old_value} → {entry['value']} (+{increments * amount})")

        self._save_state()

    def on_tool_executed(self, tool_name: str, arguments: dict):
        """ツール実行時のトリガー判定と欲求値低下"""
        desires = self.config.get("desires", {})

        for key, cfg in desires.items():
            if key not in self.state:
                continue

            for trigger in cfg.get("triggers", []):
                if trigger.get("tool") != tool_name:
                    continue

                # run_program の場合はサテライト名も照合
                if "program" in trigger:
                    app_name = arguments.get("app_name", "")
                    if app_name != trigger["program"]:
                        continue

                # トリガー一致 → 欲求値を下げる
                reduction = trigger.get("reduction", 1)
                min_val = cfg.get("min", -10)
                old_value = self.state[key]["value"]
                self.state[key]["value"] = max(min_val, old_value - reduction)
                self.state[key]["last_updated"] = time.time()
                tlog(f"[DesireManager] {cfg.get('display_name', key)}: "
                      f"{old_value} → {self.state[key]['value']} "
                      f"(trigger: {tool_name}"
                      f"{'/' + trigger['program'] if 'program' in trigger else ''}"
                      f", -{reduction})")

        self._save_state()

    def get_desire_prompt(self) -> str:
        """現在の欲求に基づくプロンプトメッセージを返す"""
        # desire_config.json の "enabled" が false の場合、欲求メッセージは注入しない。
        # デフォルト true で後方互換（キー未指定でも従来通り有効）。
        if not self.config.get("enabled", True):
            return ""

        desires = self.config.get("desires", {})
        messages = []

        for key, cfg in desires.items():
            if key not in self.state:
                continue

            value = self.state[key]["value"]
            thresholds = cfg.get("thresholds", [])

            # value 以下の最大 threshold を探す
            matched_message = None
            for th in sorted(thresholds, key=lambda t: t["value"]):
                if value >= th["value"]:
                    matched_message = th.get("message")

            if matched_message:
                messages.append(matched_message)

        if not messages:
            return ""

        return "\n".join(messages)

    def get_status(self) -> dict:
        """UI表示用の欲求状態を返す"""
        desires = self.config.get("desires", {})
        result = {}
        for key, cfg in desires.items():
            value = self.state.get(key, {}).get("value", 0)
            result[key] = {
                "display_name": cfg.get("display_name", key),
                "value": value,
                "min": cfg.get("min", -10),
                "max": cfg.get("max", 10)
            }
        return result
