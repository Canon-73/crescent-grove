# vital/cost_tracker.py
"""
CostTracker 抽象基底クラス

役割:
    API使用量を追跡するインターフェースを定義する。
    将来、DeepSeek以外のプロバイダー（OpenAI, Anthropic等）に
    対応する際にファイル追加だけで済むようにする。
"""

from abc import ABC, abstractmethod


class CostTracker(ABC):
    @abstractmethod
    def get_daily_usage_ratio(self) -> float:
        """
        本日のAPI使用量を 0.0〜1.0 の比率で返す。
        0.0 = 未使用、1.0 = 日次予算上限到達
        """
        pass

    def record_usage(self, usage: dict):
        """
        各APIレスポンスの usage フィールドを受け取り記録する。
        usage の例: {"prompt_tokens": 1500, "completion_tokens": 800, "total_tokens": 2300}
        デフォルトでは何もしない（balance API方式では不要なため）。
        """
        pass
