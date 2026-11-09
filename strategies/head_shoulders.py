"""30 分钟 K 线头肩顶 / 头肩底形态策略"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

import pandas as pd

from strategies.base import Signal


def _format_bar_time(dt_val) -> str:
    """将 K 线时间值（datetime / Timestamp / 纳秒整数）格式化为 MM-DD HH:MM。"""
    if isinstance(dt_val, (int, float)) and not isinstance(dt_val, bool):
        try:
            return pd.Timestamp(int(dt_val), unit="ns").strftime("%m-%d %H:%M")
        except (ValueError, OverflowError):
            return str(dt_val)
    if isinstance(dt_val, pd.Timestamp):
        return dt_val.strftime("%m-%d %H:%M")
    if isinstance(dt_val, datetime):
        return dt_val.strftime("%m-%d %H:%M")
    return str(dt_val)


@dataclass
class _ActiveTrade:
    stop: float
    target: float
    head: float
    neck: float


class HeadShouldersStrategy:
    """
    头肩顶 / 头肩底。

    头是区间里的极值。一边可以有两个肩，取其中能和另一边配成形态的那一对。
    颈线连接头两侧拉开的拐点，贴着肩的那根 K 线不算。允许倾斜，倾斜不超过头部高度的一半。
    两肩都要明显离开颈线。右肩拐点确认（再走出 pivot_order 根）即告警/开仓，不必等破颈线。
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
            pivot_highs, pivot_lows, klines, volume, traded, symbol
        )
        if top_signal is not None:
            return top_signal

        return self._detect_head_shoulders_bottom(
            pivot_lows, pivot_highs, klines, volume, traded, symbol
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
        return last_idx >= idx_r + self.pivot_order

    def _level_neck(self, left_anchor: float, right_anchor: float) -> float | None:
        """颈线两端要大致一样高，和形态图里的水平颈线一致。"""
        neck = (left_anchor + right_anchor) / 2
        if neck == 0:
            return None
        if abs(left_anchor - right_anchor) / abs(neck) > self.shoulder_tolerance:
            return None
        return neck

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

    def _line_at(self, i1: int, p1: float, i2: int, p2: float, idx: int) -> float:
        if i2 == i1:
            return (p1 + p2) / 2
        return p1 + (p2 - p1) * ((idx - i1) / (i2 - i1))

    def _shape_ok(
        self,
        price_l: float,
        price_h: float,
        price_r: float,
        idx_l: int,
        idx_h: int,
        idx_r: int,
        neck_l: tuple[int, float],
        neck_r: tuple[int, float],
        *,
        bottom: bool,
    ) -> tuple[float, float] | None:
        """返回 (头部高度, 突破当下的颈线价)。对不上形态图就返回 None。"""
        n1, p1 = neck_l
        n2, p2 = neck_r
        neck_head = self._line_at(n1, p1, n2, p2, idx_h)
        height = (neck_head - price_h) if bottom else (price_h - neck_head)
        if height <= 0 or price_h == 0:
            return None
        if height / abs(price_h) < self.min_head_neck_ratio:
            return None
        if abs(p1 - p2) > 0.5 * height:
            return None
        if bottom:
            if price_l - price_h < 0.25 * height or price_r - price_h < 0.25 * height:
                return None
        elif price_h - price_l < 0.25 * height or price_h - price_r < 0.25 * height:
            return None
        if abs(price_l - price_r) > 0.5 * height:
            return None
        neck_at_l = self._line_at(n1, p1, n2, p2, idx_l)
        neck_at_r = self._line_at(n1, p1, n2, p2, idx_r)
        if bottom:
            if neck_at_l - price_l < 0.4 * height or neck_at_r - price_r < 0.4 * height:
                return None
        elif price_l - neck_at_l < 0.4 * height or price_r - neck_at_r < 0.4 * height:
            return None
        return height, neck_head

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

    def _swing_low(self, lows: pd.Series, idx_l: int, idx_h: int) -> float | None:
        """左肩和头之间的最低价，不含两根拐点本身。"""
        if idx_h <= idx_l + 1:
            return None
        seg = lows.iloc[idx_l + 1 : idx_h]
        if seg.empty:
            return None
        return float(seg.min())

    def _swing_high(self, highs: pd.Series, idx_l: int, idx_h: int) -> float | None:
        """左肩和头之间的最高价，不含两根拐点本身。"""
        if idx_h <= idx_l + 1:
            return None
        seg = highs.iloc[idx_l + 1 : idx_h]
        if seg.empty:
            return None
        return float(seg.max())

    def _detect_head_shoulders_top(
        self,
        pivot_highs: list[tuple[int, float]],
        pivot_lows: list[tuple[int, float]],
        klines: pd.DataFrame,
        volume: int,
        traded: set[str],
        symbol: str,
    ) -> Signal | None:
        found = self._best_break(
            pivot_highs, pivot_lows, klines, traded, bottom=False, kind="top"
        )
        if found is None:
            return None
        idx_l, price_l, idx_h, price_h, idx_r, price_r, neck, pattern_id, neck_l_idx, neck_r_idx = found
        traded.add(pattern_id)
        traded.add(f"top-head:{idx_h}")
        self._register_short(symbol, price_h, neck)
        valley = self._swing_low(klines.low, idx_l, idx_h)
        swing = None if valley is None else price_h - valley

        left_time = _format_bar_time(klines.iloc[idx_l]["datetime"])
        head_time = _format_bar_time(klines.iloc[idx_h]["datetime"])
        right_time = _format_bar_time(klines.iloc[idx_r]["datetime"])
        neck_l_time = _format_bar_time(klines.iloc[neck_l_idx]["datetime"])
        neck_r_time = _format_bar_time(klines.iloc[neck_r_idx]["datetime"])

        return Signal(
            target_volume=-volume,
            action="sell",
            reason=(
                f"头肩顶右肩确认 左{price_l:.0f}({left_time}) 头{price_h:.0f}({head_time}) "
                f"右{price_r:.0f}({right_time}) 颈{neck:.0f}"
            ),
            detail={
                "side": "short",
                "left": price_l,
                "head": price_h,
                "right": price_r,
                "neck": neck,
                "extreme": valley,
                "swing": swing,
                "left_time": left_time,
                "head_time": head_time,
                "right_time": right_time,
                "neck_l_time": neck_l_time,
                "neck_r_time": neck_r_time,
                "left_idx": idx_l,
                "head_idx": idx_h,
                "right_idx": idx_r,
            },
        )

    def _detect_head_shoulders_bottom(
        self,
        pivot_lows: list[tuple[int, float]],
        pivot_highs: list[tuple[int, float]],
        klines: pd.DataFrame,
        volume: int,
        traded: set[str],
        symbol: str,
    ) -> Signal | None:
        found = self._best_break(
            pivot_lows, pivot_highs, klines, traded, bottom=True, kind="bottom"
        )
        if found is None:
            return None
        idx_l, price_l, idx_h, price_h, idx_r, price_r, neck, pattern_id, neck_l_idx, neck_r_idx = found
        traded.add(pattern_id)
        traded.add(f"bottom-head:{idx_h}")
        self._register_long(symbol, price_h, neck)
        peak = self._swing_high(klines.high, idx_l, idx_h)
        swing = None if peak is None else peak - price_h

        left_time = _format_bar_time(klines.iloc[idx_l]["datetime"])
        head_time = _format_bar_time(klines.iloc[idx_h]["datetime"])
        right_time = _format_bar_time(klines.iloc[idx_r]["datetime"])
        neck_l_time = _format_bar_time(klines.iloc[neck_l_idx]["datetime"])
        neck_r_time = _format_bar_time(klines.iloc[neck_r_idx]["datetime"])

        return Signal(
            target_volume=volume,
            action="buy",
            reason=(
                f"头肩底右肩确认 左{price_l:.0f}({left_time}) 头{price_h:.0f}({head_time}) "
                f"右{price_r:.0f}({right_time}) 颈{neck:.0f}"
            ),
            detail={
                "side": "long",
                "left": price_l,
                "head": price_h,
                "right": price_r,
                "neck": neck,
                "extreme": peak,
                "swing": swing,
                "left_time": left_time,
                "head_time": head_time,
                "right_time": right_time,
                "neck_l_time": neck_l_time,
                "neck_r_time": neck_r_time,
                "left_idx": idx_l,
                "head_idx": idx_h,
                "right_idx": idx_r,
            },
        )

    def _reaction_neck(
        self,
        reaction_pivots: list[tuple[int, float]],
        i0: int,
        i1: int,
        *,
        lowest: bool,
    ) -> tuple[int, float] | None:
        """肩和头之间的颈线拐点，必须离开两端至少一根拐点宽度。"""
        gap = self.pivot_order
        inside = [p for p in reaction_pivots if i0 + gap <= p[0] <= i1 - gap]
        if not inside:
            return None
        if lowest:
            return min(inside, key=lambda p: (p[1], p[0]))
        return max(inside, key=lambda p: (p[1], -p[0]))

    def _best_break(
        self,
        pivots: list[tuple[int, float]],
        reaction_pivots: list[tuple[int, float]],
        klines: pd.DataFrame,
        traded: set[str],
        *,
        bottom: bool,
        kind: str,
    ) -> tuple[int, float, int, float, int, float, float, str, int, int] | None:
        """本根刚好确认右肩的形态里，取头部幅度最大的那一个（不必破颈线）。"""
        if len(pivots) < 3 or len(klines) < 2:
            return None
        closes = klines.close
        last_idx = len(closes) - 1
        extreme = klines.low if bottom else klines.high
        best: tuple[float, tuple] | None = None

        for hi, (idx_h, price_h) in enumerate(pivots):
            for li in range(hi):
                idx_l, price_l = pivots[li]
                if idx_h - idx_l < self.pivot_order:
                    continue
                if idx_h - idx_l > self.max_span_bars:
                    continue
                if bottom and price_l <= price_h:
                    continue
                if not bottom and price_l >= price_h:
                    continue
                for ri in range(hi + 1, len(pivots)):
                    idx_r, price_r = pivots[ri]
                    span = idx_r - idx_l
                    if span < self.min_span_bars:
                        continue
                    if span > self.max_span_bars:
                        break
                    if idx_r - idx_h < self.pivot_order:
                        continue
                    # 仅在右肩「刚刚」确认的那一根告警，避免事后补推旧形态
                    if last_idx != idx_r + self.pivot_order:
                        continue
                    if bottom and price_r <= price_h:
                        continue
                    if not bottom and price_r >= price_h:
                        continue
                    pattern_id = f"{kind}:{idx_l}:{idx_h}:{idx_r}"
                    if pattern_id in traded or f"{kind}-head:{idx_h}" in traded:
                        continue
                    window = extreme.iloc[idx_l : idx_r + 1]
                    if bottom and float(window.min()) < price_h:
                        continue
                    if not bottom and float(window.max()) > price_h:
                        continue
                    neck_l = self._reaction_neck(
                        reaction_pivots, idx_l, idx_h, lowest=not bottom
                    )
                    neck_r = self._reaction_neck(
                        reaction_pivots, idx_h, idx_r, lowest=not bottom
                    )
                    if neck_l is None or neck_r is None:
                        continue
                    shaped = self._shape_ok(
                        price_l,
                        price_h,
                        price_r,
                        idx_l,
                        idx_h,
                        idx_r,
                        neck_l,
                        neck_r,
                        bottom=bottom,
                    )
                    if shaped is None:
                        continue
                    height, _neck_head = shaped
                    n1, p1 = neck_l
                    n2, p2 = neck_r
                    neck_now = self._line_at(n1, p1, n2, p2, last_idx)
                    if best is None or height > best[0]:
                        best = (
                            height,
                            (
                                idx_l,
                                price_l,
                                idx_h,
                                price_h,
                                idx_r,
                                price_r,
                                neck_now,
                                pattern_id,
                                n1,
                                n2,
                            ),
                        )

        if best is None:
            return None
        return best[1]

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
                return self._level_neck(min(left_troughs), min(right_troughs))
        return None

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
                return self._level_neck(max(left_peaks), max(right_peaks))
        return None
