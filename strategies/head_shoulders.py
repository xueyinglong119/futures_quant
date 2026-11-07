"""30 分钟 K 线头肩顶 / 头肩底形态策略"""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from strategies.base import Signal


@dataclass
class _ActiveTrade:
    stop: float
    target: float
    head: float
    neck: float


class HeadShouldersStrategy:
    """
    30 分钟 K 线头肩顶 / 头肩底：右肩确认开仓，头部外止损，颈线止盈。
    """

    def __init__(
        self,
        pivot_order: int = 4,
        shoulder_tolerance: float = 0.03,
        min_span_bars: int = 10,
        max_span_bars: int = 60,
        neck_break_ratio: float = 0.001,
        max_right_above_left_ratio: float = 0.015,
        min_head_neck_ratio: float = 0.008,
        stop_beyond_head_ratio: float = 0.005,
        take_profit_at_neck: bool = True,
        max_volume: int = 1,
    ):
        self.pivot_order = pivot_order
        self.shoulder_tolerance = shoulder_tolerance
        self.min_span_bars = min_span_bars
        self.max_span_bars = max_span_bars
        self.neck_break_ratio = neck_break_ratio
        self.max_right_above_left_ratio = max_right_above_left_ratio
        self.min_head_neck_ratio = min_head_neck_ratio
        self.stop_beyond_head_ratio = stop_beyond_head_ratio
        self.take_profit_at_neck = take_profit_at_neck
        self.max_volume = max_volume
        self._traded_patterns: dict[str, set[str]] = {}
        self._active_trades: dict[str, _ActiveTrade] = {}

    def on_bar(
        self,
        klines: pd.DataFrame,
        max_volume: int | None = None,
        symbol: str = "",
    ) -> Signal | None:
        volume = max_volume if max_volume is not None else self.max_volume
        min_len = self.max_span_bars + self.pivot_order * 2 + 5
        if len(klines) < min_len:
            return None

        pivot_highs = self._find_pivot_highs(klines.high)
        pivot_lows = self._find_pivot_lows(klines.low)
        traded = self._traded_patterns.setdefault(symbol, set())

        top_signal = self._detect_head_shoulders_top(
            pivot_highs, pivot_lows, klines.close, volume, traded, symbol
        )
        if top_signal is not None:
            return top_signal

        return self._detect_head_shoulders_bottom(
            pivot_lows, pivot_highs, klines.close, volume, traded, symbol
        )

    def check_exit(
        self, klines: pd.DataFrame, symbol: str, position: int
    ) -> Signal | None:
        if position == 0:
            self._active_trades.pop(symbol, None)
            return None

        meta = self._active_trades.get(symbol)
        if meta is None:
            return None

        high = float(klines.high.iloc[-1])
        low = float(klines.low.iloc[-1])

        if position > 0:
            if low <= meta.stop:
                self._active_trades.pop(symbol, None)
                return Signal(0, "stop_loss", f"多头止损 头{meta.head:.0f} 颈{meta.neck:.0f}")
            if self.take_profit_at_neck and high >= meta.target:
                self._active_trades.pop(symbol, None)
                return Signal(0, "take_profit", f"多头止盈颈线 {meta.neck:.0f}")
        elif position < 0:
            if high >= meta.stop:
                self._active_trades.pop(symbol, None)
                return Signal(0, "stop_loss", f"空头止损 头{meta.head:.0f} 颈{meta.neck:.0f}")
            if self.take_profit_at_neck and low <= meta.target:
                self._active_trades.pop(symbol, None)
                return Signal(0, "take_profit", f"空头止盈颈线 {meta.neck:.0f}")

        return None

    def _find_pivot_highs(self, series: pd.Series) -> list[tuple[int, float]]:
        order = self.pivot_order
        pivots: list[tuple[int, float]] = []
        for i in range(order, len(series) - order):
            val = float(series.iloc[i])
            left = series.iloc[i - order : i]
            right = series.iloc[i + 1 : i + order + 1]
            if val >= left.max() and val >= right.max():
                if val > left.max() or val > right.max():
                    pivots.append((i, val))
        return pivots

    def _find_pivot_lows(self, series: pd.Series) -> list[tuple[int, float]]:
        order = self.pivot_order
        pivots: list[tuple[int, float]] = []
        for i in range(order, len(series) - order):
            val = float(series.iloc[i])
            left = series.iloc[i - order : i]
            right = series.iloc[i + 1 : i + order + 1]
            if val <= left.min() and val <= right.min():
                if val < left.min() or val < right.min():
                    pivots.append((i, val))
        return pivots

    def _shoulders_balanced(self, left: float, right: float, reference: float) -> bool:
        if reference == 0:
            return False
        return abs(left - right) / abs(reference) <= self.shoulder_tolerance

    def _right_shoulder_confirmed(self, idx_r: int, last_idx: int) -> bool:
        return last_idx == idx_r + self.pivot_order

    def _right_shoulder_not_too_high(
        self, price_l: float, price_r: float, price_h: float
    ) -> bool:
        if price_r <= price_l:
            return True
        if price_h == 0:
            return False
        return (price_r - price_l) / abs(price_h) <= self.max_right_above_left_ratio

    def _head_neck_valid_top(self, price_h: float, neck: float) -> bool:
        if price_h == 0:
            return False
        return (price_h - neck) / price_h >= self.min_head_neck_ratio

    def _head_neck_valid_bottom(self, price_h: float, neck: float) -> bool:
        if price_h == 0:
            return False
        return (neck - price_h) / abs(price_h) >= self.min_head_neck_ratio

    def _register_short(self, symbol: str, price_h: float, neck: float) -> None:
        self._active_trades[symbol] = _ActiveTrade(
            stop=price_h * (1 + self.stop_beyond_head_ratio),
            target=neck,
            head=price_h,
            neck=neck,
        )

    def _register_long(self, symbol: str, price_h: float, neck: float) -> None:
        self._active_trades[symbol] = _ActiveTrade(
            stop=price_h * (1 - self.stop_beyond_head_ratio),
            target=neck,
            head=price_h,
            neck=neck,
        )

    def _detect_head_shoulders_top(
        self,
        pivot_highs: list[tuple[int, float]],
        pivot_lows: list[tuple[int, float]],
        closes: pd.Series,
        volume: int,
        traded: set[str],
        symbol: str,
    ) -> Signal | None:
        if len(pivot_highs) < 3:
            return None

        last_idx = len(closes) - 1

        for i in range(len(pivot_highs) - 2):
            idx_l, price_l = pivot_highs[i]
            idx_h, price_h = pivot_highs[i + 1]
            idx_r, price_r = pivot_highs[i + 2]

            span = idx_r - idx_l
            if span < self.min_span_bars or span > self.max_span_bars:
                continue
            if not (price_h > price_l and price_h > price_r):
                continue
            if not self._shoulders_balanced(price_l, price_r, price_h):
                continue
            if not self._right_shoulder_not_too_high(price_l, price_r, price_h):
                continue

            neck = self._neckline_for_top(pivot_lows, idx_l, idx_h, idx_r, closes)
            if neck is None or not self._head_neck_valid_top(price_h, neck):
                continue

            pattern_id = f"top:{idx_l}:{idx_h}:{idx_r}"
            if pattern_id in traded:
                continue

            if self._right_shoulder_confirmed(idx_r, last_idx):
                traded.add(pattern_id)
                self._register_short(symbol, price_h, neck)
                return Signal(
                    target_volume=-volume,
                    action="sell",
                    reason=(
                        f"头肩顶右肩 左{price_l:.0f} 头{price_h:.0f} "
                        f"右{price_r:.0f} 颈{neck:.0f}"
                    ),
                )

        return None

    def _detect_head_shoulders_bottom(
        self,
        pivot_lows: list[tuple[int, float]],
        pivot_highs: list[tuple[int, float]],
        closes: pd.Series,
        volume: int,
        traded: set[str],
        symbol: str,
    ) -> Signal | None:
        if len(pivot_lows) < 3:
            return None

        last_idx = len(closes) - 1

        for i in range(len(pivot_lows) - 2):
            idx_l, price_l = pivot_lows[i]
            idx_h, price_h = pivot_lows[i + 1]
            idx_r, price_r = pivot_lows[i + 2]

            span = idx_r - idx_l
            if span < self.min_span_bars or span > self.max_span_bars:
                continue
            if not (price_h < price_l and price_h < price_r):
                continue
            if not self._shoulders_balanced(price_l, price_r, price_h):
                continue
            if not self._right_shoulder_not_too_high(price_l, price_r, price_h):
                continue

            neck = self._neckline_for_bottom(pivot_highs, idx_l, idx_h, idx_r, closes)
            if neck is None or not self._head_neck_valid_bottom(price_h, neck):
                continue

            pattern_id = f"bottom:{idx_l}:{idx_h}:{idx_r}"
            if pattern_id in traded:
                continue

            if self._right_shoulder_confirmed(idx_r, last_idx):
                traded.add(pattern_id)
                self._register_long(symbol, price_h, neck)
                return Signal(
                    target_volume=volume,
                    action="buy",
                    reason=(
                        f"头肩底右肩 左{price_l:.0f} 头{price_h:.0f} "
                        f"右{price_r:.0f} 颈{neck:.0f}"
                    ),
                )

        return None

    def _neckline_for_top(
        self,
        pivot_lows: list[tuple[int, float]],
        idx_l: int,
        idx_h: int,
        idx_r: int,
        closes: pd.Series,
    ) -> float | None:
        lows_in_range = [p for p in pivot_lows if idx_l < p[0] < idx_r]
        if len(lows_in_range) >= 2:
            left_troughs = [p[1] for p in lows_in_range if p[0] < idx_h]
            right_troughs = [p[1] for p in lows_in_range if p[0] > idx_h]
            if left_troughs and right_troughs:
                return (min(left_troughs) + min(right_troughs)) / 2

        seg = closes.iloc[idx_l : idx_r + 1]
        if len(seg) < 3:
            return None
        mid = len(seg) // 2
        left_min = float(seg.iloc[:mid].min())
        right_min = float(seg.iloc[mid:].min())
        return (left_min + right_min) / 2

    def _neckline_for_bottom(
        self,
        pivot_highs: list[tuple[int, float]],
        idx_l: int,
        idx_h: int,
        idx_r: int,
        closes: pd.Series,
    ) -> float | None:
        highs_in_range = [p for p in pivot_highs if idx_l < p[0] < idx_r]
        if len(highs_in_range) >= 2:
            left_peaks = [p[1] for p in highs_in_range if p[0] < idx_h]
            right_peaks = [p[1] for p in highs_in_range if p[0] > idx_h]
            if left_peaks and right_peaks:
                return (max(left_peaks) + max(right_peaks)) / 2

        seg = closes.iloc[idx_l : idx_r + 1]
        if len(seg) < 3:
            return None
        mid = len(seg) // 2
        left_max = float(seg.iloc[:mid].max())
        right_max = float(seg.iloc[mid:].max())
        return (left_max + right_max) / 2
