# vital/deepseek_tracker.py
"""
DeepSeek Balance API を使ったコスト追跡

役割:
    DeepSeekのbalance APIで残高を取得し、
    日次予算に対する消費率を算出する。
    APIが失敗した場合は例外を送出し、
    VitalManagerがTokenTrackerへフォールバックする。
"""

import requests
from vital.cost_tracker import CostTracker


class DeepSeekTracker(CostTracker):
    def __init__(self, api_key: str, daily_budget_usd: float = 0.50,
                 daily_start_balance: float = None):
        self.api_key = api_key
        self.daily_budget_usd = daily_budget_usd
        self.daily_start_balance = daily_start_balance

        # 起動時に初期残高を取得（daily_start_balanceが未設定の場合）
        if self.daily_start_balance is None:
            self.daily_start_balance = self._fetch_balance()

    def _fetch_balance(self) -> float:
        """DeepSeek balance API を呼び出して現在の残高（USD）を返す"""
        try:
            resp = requests.get(
                "https://api.deepseek.com/user/balance",
                headers={"Authorization": f"Bearer {self.api_key}"},
                timeout=10
            )
            resp.raise_for_status()
            data = resp.json()

            # balance_infos から USD の total_balance を取得
            for info in data.get("balance_infos", []):
                if info.get("currency", "").upper() == "USD":
                    return float(info.get("total_balance", 0))

            # USD が見つからない場合は最初のエントリを使用
            infos = data.get("balance_infos", [])
            if infos:
                return float(infos[0].get("total_balance", 0))

            raise RuntimeError("balance_infos が空です")
        except requests.RequestException as e:
            raise RuntimeError(f"DeepSeek balance API 失敗: {e}") from e

    def get_daily_usage_ratio(self) -> float:
        """日次予算に対する消費率を返す（0.0〜1.0）"""
        current_balance = self._fetch_balance()
        spent = self.daily_start_balance - current_balance
        ratio = spent / self.daily_budget_usd if self.daily_budget_usd > 0 else 0.0
        return min(max(ratio, 0.0), 1.0)  # 0〜1 にクランプ

    def record_usage(self, usage: dict):
        """balance API方式では不要（何もしない）"""
        pass
