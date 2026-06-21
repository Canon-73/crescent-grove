# vital/token_tracker.py
"""
トークン累積カウントによるコスト追跡（フォールバック実装）

役割:
    balance API が使えない場合のフォールバック。
    各APIレスポンスの total_tokens を加算し、
    日次上限に対する消費率を算出する。
"""

from vital.cost_tracker import CostTracker


class TokenTracker(CostTracker):
    def __init__(self, daily_token_limit: int = 500000):
        self.daily_token_limit = daily_token_limit
        self.daily_total_tokens: int = 0

    def record_usage(self, usage: dict):
        """APIレスポンスの usage を受け取り、total_tokens を加算する"""
        tokens = usage.get("total_tokens", 0)
        self.daily_total_tokens += tokens

    def get_daily_usage_ratio(self) -> float:
        """日次トークン上限に対する消費率を返す（0.0〜1.0）"""
        if self.daily_token_limit <= 0:
            return 0.0
        ratio = self.daily_total_tokens / self.daily_token_limit
        return min(ratio, 1.0)  # 1.0 にクランプ

    def reset(self):
        """日次リセット"""
        self.daily_total_tokens = 0
