"""30 分钟双均线日内策略"""

from __future__ import annotations

import pandas as pd
from tqsdk.tafunc import ma

from strategies.base import Signal

__all__ = ["Signal", "MACrossoverStrategy"]


class MACrossoverStrategy:
    """双均线金叉/死叉，示例策略，可在此替换为你的逻辑"""

    def __init__(self, short_period: int = 5, long_period: int = 20, max_volume: int = 1):
        self.short_period = short_period
        self.long_period = long_period
        self.max_volume = max_volume

    def on_bar(
        self,
        klines: pd.DataFrame,
        max_volume: int | None = None,
        symbol: str = "",
    ) -> Signal | None:
        volume = max_volume if max_volume is not None else self.max_volume
        min_len = self.long_period + 2
        if len(klines) < min_len:
            return None

        short_ma = ma(klines.close, self.short_period)
        long_ma = ma(klines.close, self.long_period)

        # 金叉做多
        if short_ma.iloc[-2] < long_ma.iloc[-2] and short_ma.iloc[-1] > long_ma.iloc[-1]:
            return Signal(
                target_volume=volume,
                action="buy",
                reason=f"MA{self.short_period}上穿MA{self.long_period}",
            )

        # 死叉做空
        if short_ma.iloc[-2] > long_ma.iloc[-2] and short_ma.iloc[-1] < long_ma.iloc[-1]:
            return Signal(
                target_volume=-volume,
                action="sell",
                reason=f"MA{self.short_period}下穿MA{self.long_period}",
            )

        return None
