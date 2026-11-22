"""30 分钟 K 线头肩顶 / 头肩底形态策略"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

import numpy as np
import pandas as pd
from numpy.lib.stride_tricks import sliding_window_view

from strategies.base import Signal
from strategies.hs_super_strict import check as check_super
from strategies.trend import pre_trend_ok

_NAN = float("nan")


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


def _range_aggs(arr: np.ndarray):
    """稀疏表，O(1) 取区间 [a,b) 的 (最小, 最大)。NaN 按 pandas 的方式忽略，
    空区间返回 (nan, nan)，与 `series.iloc[a:b].min()/.max()` 同值。"""
    n = int(arr.size)
    if n == 0:
        return lambda a, b: (_NAN, _NAN)
    max_tbl = [arr]
    min_tbl = [arr]
    length = 2
    while length <= n:
        half = length >> 1
        span = n - length + 1
        prev_max = max_tbl[-1]
        prev_min = min_tbl[-1]
        max_tbl.append(np.fmax(prev_max[:span], prev_max[half : half + span]))
        min_tbl.append(np.fmin(prev_min[:span], prev_min[half : half + span]))
        length <<= 1
    log2 = [0] * (n + 1)
    for i in range(2, n + 1):
        log2[i] = log2[i >> 1] + 1

    def query(a: int, b: int) -> tuple[float, float]:
        if b > n:
            b = n
        span = b - a
        if span <= 0:
            return (_NAN, _NAN)
        j = log2[span]
        off = b - (1 << j)
        return (
            float(np.fmin(min_tbl[j][a], min_tbl[j][off])),
            float(np.fmax(max_tbl[j][a], max_tbl[j][off])),
        )

    return query


@dataclass
class _ActiveTrade:
    stop: float
    target: float
    head: float
    neck: float


class HeadShouldersStrategy:
    """
    头肩顶 / 头肩底。

    头是区间里的极值；三重底/顶时允许头是近似等高的中间极值（与区间极值差 ≤0.25×头高）。
    一边可以有两个肩，取其中能和另一边配成形态的那一对。
    颈线连接头两侧拉开的拐点，贴着肩的那根 K 线不算。允许倾斜，倾斜不超过头部高度的 1/4。
    形态点位（肩、头、颈线拐点）一律按收盘价量，蜡烛高低点只用于风控摆动。
    两肩都要明显离开颈线（至少 0.1×头高的余量，1m 另要求颈线锚点离肩≥3 根）。
    右肩就是当前这根 K 线（收盘确定）即出告警/开仓，全周期不延迟；confirmed_shoulders=True 才回到
    「肩取两侧确认极值 + 右肩后 pivot_order 根触发」的慢口径。
    strict_shape 质量闸：左右肩价差≤颈线1.5%、两肩到头的 K 线间距比≤2.5×（双重/平台/三重头跳过）；
    头到颈线至少 10 个价格点（且 ≥0.8% 价格），太浅的形态不成立；
    两肩价差最小的候选优先，其次取最早的合格右肩，再次左右腿最均衡的，最后头高。
    肩部主导性：左肩须为其与头之间同向拐点极端；右肩之后若走出更极端的同向拐点，
    该形态在后续 K 线上自然不再成立。
    严格档走 `on_bar_strict`，与常规档同一根触发、同一套候选，只多几道闸：肩差改用
    「|右肩−左肩| ÷ 左肩 ≤ DEV_THRESHOLD」量，另加形态≥8 根与三段均量递减（趋势起点→左肩 /
    左颈→头 / 右颈→右肩，底 2<1 且 3≤2，顶把 < 与 ≤ 对调）。两档各一套 `_traded_*` 记账。
    颈线只用来算目标，不是严格档的开仓条件。
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
        min_head_neck_points: float = 10.0,
        min_head_boost: float = 0.003,
        stop_beyond_head_ratio: float = 0.005,
        take_profit_at_neck: bool = True,
        max_volume: int = 1,
        duration_sec: int = 0,
        strict_shape: bool = True,
        max_shoulder_spread_ratio: float = 0.015,
        max_neck_slope_ratio: float = 0.005,
        dominance_reaction_ratio: float = 3.0,
        shoulder_prominence_ratio: float = 0.2,
        shoulder_neck_bars: int = 3,
        min_shoulder_neck_ratio: float = 0.1,
        triple_head_tolerance_ratio: float = 0.25,
        double_head_tolerance_ratio: float = 0.25,
        flat_head_tolerance_ratio: float = 0.1,
        flat_head_min_bars: int = 4,
        max_shoulder_head_span_ratio: float = 2.5,
        confirmed_shoulders: bool = False,
        pre_trend: bool = False,
        trend_window: int = 20,
        trend_threshold: float = 0.0002,
        strict_signals: bool = False,
        strict_head_boost: bool = False,
        strict_dev_threshold: float = 0.025,
        strict_min_form_bars: int = 8,
        strict_volume_gates: bool = True,
        strict_volume_triple: bool = False,
        strict_pulse_filter: bool = False,
        strict_pulse_multiple: float = 3.0,
    ):
        self.pivot_order = pivot_order
        self.shoulder_tolerance = shoulder_tolerance
        self.min_span_bars = min_span_bars
        self.max_span_bars = max_span_bars
        self.neck_break_ratio = neck_break_ratio
        self.max_right_above_left_ratio = max_right_above_left_ratio
        self.min_head_neck_ratio = min_head_neck_ratio
        self.min_head_boost = min_head_boost
        self.min_head_neck_points = min_head_neck_points
        self.stop_beyond_head_ratio = stop_beyond_head_ratio
        self.take_profit_at_neck = take_profit_at_neck
        self.max_volume = max_volume
        self.duration_sec = duration_sec
        self.strict_shape = strict_shape
        self.max_shoulder_spread_ratio = max_shoulder_spread_ratio
        self.max_neck_slope_ratio = max_neck_slope_ratio
        self.dominance_reaction_ratio = dominance_reaction_ratio
        self.shoulder_prominence_ratio = shoulder_prominence_ratio
        self.shoulder_neck_bars = shoulder_neck_bars
        self.min_shoulder_neck_ratio = min_shoulder_neck_ratio
        self.triple_head_tolerance_ratio = triple_head_tolerance_ratio
        self.double_head_tolerance_ratio = double_head_tolerance_ratio
        self.flat_head_tolerance_ratio = flat_head_tolerance_ratio
        self.flat_head_min_bars = flat_head_min_bars
        self.max_shoulder_head_span_ratio = max_shoulder_head_span_ratio
        self.confirmed_shoulders = confirmed_shoulders
        self.pre_trend = pre_trend
        self.trend_window = trend_window
        self.trend_threshold = trend_threshold
        self.strict_signals = strict_signals
        self.strict_head_boost = strict_head_boost
        self.strict_dev_threshold = strict_dev_threshold
        self.strict_min_form_bars = strict_min_form_bars
        self.strict_volume_gates = strict_volume_gates
        self.strict_volume_triple = strict_volume_triple
        self.strict_pulse_filter = strict_pulse_filter
        self.strict_pulse_multiple = strict_pulse_multiple
        self._traded_patterns: dict[str, set[str]] = {}
        self._traded_strict: dict[str, set[str]] = {}
        self._traded_super: dict[str, set[str]] = {}
        self._active_trades: dict[str, _ActiveTrade] = {}
        self.trace: bool = False
        self.last_trace: list[tuple] = []

    def on_bar(
        self,
        klines: pd.DataFrame,
        max_volume: int | None = None,
        symbol: str = "",
    ) -> Signal | None:
        return self._scan(klines, max_volume, symbol, strict=False)

    def on_bar_strict(
        self,
        klines: pd.DataFrame,
        max_volume: int | None = None,
        symbol: str = "",
    ) -> Signal | None:
        """严格档：与常规档同一根触发、同一套候选，只多几道闸。"""
        if not self.strict_signals:
            return None
        return self._scan(klines, max_volume, symbol, strict=True)

    def on_bar_super_strict(
        self,
        klines: pd.DataFrame,
        max_volume: int | None = None,
        symbol: str = "",
    ) -> Signal | None:
        """超严格档：严格档同一候选，再套用户 11-21 的附加校验（1/2/3/4/6 当根可判）。"""
        if not self.strict_signals:
            return None
        return self._scan(
            klines, max_volume, symbol, strict=True, super_strict=True
        )

    def _scan(
        self,
        klines: pd.DataFrame,
        max_volume: int | None,
        symbol: str,
        *,
        strict: bool,
        super_strict: bool = False,
    ) -> Signal | None:
        volume = max_volume if max_volume is not None else self.max_volume
        min_len = self.max_span_bars + self.pivot_order * 2 + 5
        if len(klines) < min_len:
            return None

        highs = self._pivot_series(klines, is_high=True)
        lows = self._pivot_series(klines, is_high=False)
        if self.confirmed_shoulders:
            # 只认两侧确认的局部极值作肩，右肩取与左肩价位匹配者；触发回到确认那根
            pivot_highs = self._find_pivot_highs(highs)
            pivot_lows = self._find_pivot_lows(lows)
        else:
            pivot_highs = self._with_tail_pivot(self._find_pivot_highs(highs), highs, is_high=True)
            pivot_lows = self._with_tail_pivot(self._find_pivot_lows(lows), lows, is_high=False)
        # 各档各记各的账：同一形态严格档/超严格档也要能独立出一条
        if super_strict:
            traded = self._traded_super.setdefault(symbol, set())
        elif strict:
            traded = self._traded_strict.setdefault(symbol, set())
        else:
            traded = self._traded_patterns.setdefault(symbol, set())

        top_signal = self._detect_head_shoulders_top(
            pivot_highs, pivot_lows, klines, volume, traded, symbol,
            strict=strict, super_strict=super_strict,
        )
        if top_signal is not None:
            return top_signal

        return self._detect_head_shoulders_bottom(
            pivot_lows, pivot_highs, klines, volume, traded, symbol,
            strict=strict, super_strict=super_strict,
        )

    def _pulse_kept(self, seg: np.ndarray) -> np.ndarray:
        """开脉冲过滤时剔掉 >PULSE_MULTIPLE×均值 的单根；无可剔时原样返回。"""
        if not self.strict_pulse_filter or seg.size == 0:
            return seg
        base = float(seg.mean())
        if base <= 0:
            return seg
        kept = seg[seg <= self.strict_pulse_multiple * base]
        return kept if kept.size else seg

    def _vol_avg(self, vols: np.ndarray, a: int, b: int) -> float | None:
        """[a,b] 闭区间均量。开脉冲过滤时先剔掉 >PULSE_MULTIPLE×均值 的单根再求均值。"""
        if b < a:
            return None
        seg = vols[max(0, a) : b + 1]
        if seg.size == 0:
            return None
        return float(self._pulse_kept(seg).mean())

    def _volumes_ok(
        self,
        klines: pd.DataFrame,
        reaction_pivots: list[tuple[int, float]],
        idx_l: int,
        idx_h: int,
        idx_r: int,
        neck_l_idx: int,
        neck_r_idx: int,
        *,
        bottom: bool,
    ) -> bool:
        """三段均量递减：底=头段<左肩段 且 右肩段≤头段；顶把 < 与 ≤ 对调。

        seg1 的「趋势起点」不是形态锚点，取左肩之前最近一个反向拐点，再早也不超过一个形态长度。
        """
        vols = np.asarray(klines["volume"], dtype="float64")
        if vols.size == 0:
            return False
        start = idx_l - self.max_span_bars
        prior = [p[0] for p in reaction_pivots if p[0] < idx_l]
        if prior:
            start = max(start, max(prior))
        v1 = self._vol_avg(vols, start, idx_l)
        v2 = self._vol_avg(vols, neck_l_idx, idx_h)
        v3 = self._vol_avg(vols, neck_r_idx, idx_r)
        if v1 is None or v2 is None or v3 is None:
            return False
        if self.strict_volume_triple:
            # 三重同款「第三腿最弱」：顶 v1>v3 且 v2>v3；底 v1<v3 且 v2<v3
            if bottom:
                return v1 < v3 and v2 < v3
            return v1 > v3 and v2 > v3
        if bottom:
            return v2 < v1 and v3 <= v2
        return v2 <= v1 and v3 < v2

    def _register_pending(
        self,
        signal: Signal,
        symbol: str,
        pattern_id: str,
        head_key: str,
        side: str,
        price_h: float,
        neck: float,
        traded: set[str],
    ) -> Signal | None:
        traded.add(pattern_id)
        traded.add(head_key)
        if side == "short":
            self._register_short(symbol, price_h, neck)
        else:
            self._register_long(symbol, price_h, neck)
        return signal

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

    def _pivot_series(self, klines: pd.DataFrame, *, is_high: bool) -> pd.Series:
        """形态点位（肩、头、颈线拐点）按收盘价量；蜡烛高低点只留给风控摆动。"""
        return klines.close

    def _find_pivot_highs(self, series: pd.Series) -> list[tuple[int, float]]:
        order = self.pivot_order
        n = len(series)
        if n <= 2 * order:
            return []
        arr = np.asarray(series, dtype="float64")
        wmax = np.fmax.reduce(sliding_window_view(arr, order), axis=1)
        pivots: list[tuple[int, float]] = []
        for i in range(order, n - order):
            val = float(arr[i])
            lm = float(wmax[i - order])
            rm = float(wmax[i + 1])
            if val >= lm and val >= rm and (val > lm or val > rm):
                pivots.append((i, val))
        return pivots

    def _find_pivot_lows(self, series: pd.Series) -> list[tuple[int, float]]:
        order = self.pivot_order
        n = len(series)
        if n <= 2 * order:
            return []
        arr = np.asarray(series, dtype="float64")
        wmin = np.fmin.reduce(sliding_window_view(arr, order), axis=1)
        pivots: list[tuple[int, float]] = []
        for i in range(order, n - order):
            val = float(arr[i])
            lm = float(wmin[i - order])
            rm = float(wmin[i + 1])
            if val <= lm and val <= rm and (val < lm or val < rm):
                pivots.append((i, val))
        return pivots

    def _with_tail_pivot(
        self, pivots: list[tuple[int, float]], series: pd.Series, *, is_high: bool
    ) -> list[tuple[int, float]]:
        """当前这根只要盖过前 pivot_order 根就算拐点——右肩当根即出告警，不等 4 根。"""
        order = self.pivot_order
        i = len(series) - 1
        if i < order:
            return pivots
        arr = np.asarray(series, dtype="float64")
        val = float(arr[i])
        seg = arr[i - order : i]
        if is_high:
            if val > float(np.fmax.reduce(seg)):
                pivots.append((i, val))
        else:
            if val < float(np.fmin.reduce(seg)):
                pivots.append((i, val))
        return pivots

    def _shoulders_balanced(self, left: float, right: float, reference: float) -> bool:
        if reference == 0:
            return False
        return abs(left - right) / abs(reference) <= self.shoulder_tolerance

    def _right_shoulder_confirmed(self, idx_r: int, last_idx: int) -> bool:
        if self.confirmed_shoulders:
            return last_idx == idx_r + self.pivot_order
        return idx_r == last_idx

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
        closes: pd.Series | None = None,
        double_head: bool = False,
        strict: bool = False,
    ) -> tuple[float, float] | None:
        """返回 (头部高度, 突破当下的颈线价)。对不上形态图就返回 None。"""
        n1, p1 = neck_l
        n2, p2 = neck_r
        neck_head = self._line_at(n1, p1, n2, p2, idx_h)
        height = (neck_head - price_h) if bottom else (price_h - neck_head)
        if height <= 0 or price_h == 0:
            return None
        if height < self.min_head_neck_points:
            return None
        use_boost = strict and self.strict_head_boost
        if not use_boost and height / abs(price_h) < self.min_head_neck_ratio:
            return None
        if use_boost:
            # 严格档可选：头比颈线的 0.8% 换成「头比肩 ≥0.3%」（用户 11-22 口径）
            if bottom:
                if (price_l - price_h) / abs(price_h) < self.min_head_boost or (
                    price_r - price_h
                ) / abs(price_h) < self.min_head_boost:
                    return None
            else:
                if (price_h - price_l) / abs(price_l) < self.min_head_boost or (
                    price_h - price_r
                ) / abs(price_r) < self.min_head_boost:
                    return None
        # 锚点落在单边急跌/急拉远端时颈线会贴着趋势走，斜率超过 1/4 头高即不成立
        if abs(p1 - p2) > 0.25 * height:
            return None
        prom = self.shoulder_prominence_ratio
        if bottom:
            if price_l - price_h < prom * height or price_r - price_h < prom * height:
                return None
        elif price_h - price_l < prom * height or price_h - price_r < prom * height:
            return None
        if abs(price_l - price_r) > 0.5 * height:
            return None
        neck_at_l = self._line_at(n1, p1, n2, p2, idx_l)
        neck_at_r = self._line_at(n1, p1, n2, p2, idx_r)
        # 肩离颈不卡 0.3×头高，但至少要有 0.1×头高的余量（防止颈线贴穿肩）
        gap = self.shoulder_neck_bars if self.duration_sec == 60 else 1
        if n1 - idx_l < gap or idx_r - n2 < gap:
            return None
        margin = self.min_shoulder_neck_ratio * height
        if bottom:
            if neck_at_l - price_l < margin or neck_at_r - price_r < margin:
                return None
        elif price_l - neck_at_l < margin or price_r - neck_at_r < margin:
            return None
        if self.strict_shape:
            if strict:
                # 严格档把这条换成「两肩价差 ÷ 左肩价位 ≤ DEV_THRESHOLD」，不再按颈线量
                if price_l == 0:
                    return None
                if abs(price_r - price_l) / abs(price_l) > self.strict_dev_threshold:
                    return None
            elif abs(price_l - price_r) > self.max_shoulder_spread_ratio * abs(neck_head):
                return None
            # 两肩到头的 K 线间距比 ≤2.5（2026-11-15 由 1.6 → 4.0 → 2.5 回调）
            # （双重底/顶、平台头天然更宽，跳过此闸）
            d_l = idx_h - idx_l
            d_r = idx_r - idx_h
            if not double_head and max(d_l, d_r) > self.max_shoulder_head_span_ratio * min(d_l, d_r):
                return None
            if closes is not None:
                # 颈线被收盘价穿透说明锚点没取到真正的反弹极值，形态不成立
                tol = 0.1 * height
                span = np.arange(n1, n2 + 1)
                if n2 == n1:
                    lines = np.full(span.size, (p1 + p2) / 2)
                else:
                    lines = p1 + (p2 - p1) * ((span - n1) / (n2 - n1))
                c = np.asarray(closes, dtype="float64")[n1 : n2 + 1]
                bad = c > lines + tol if bottom else c < lines - tol
                if bool(bad.any()):
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
        *,
        strict: bool = False,
        super_strict: bool = False,
    ) -> Signal | None:
        found = self._best_break(
            pivot_highs,
            pivot_lows,
            klines,
            traded,
            bottom=False,
            kind="top",
            strict=strict,
            super_strict=super_strict,
        )
        if found is None:
            return None
        idx_l, price_l, idx_h, price_h, idx_r, price_r, neck, pattern_id, neck_l_idx, neck_r_idx = found
        valley = self._swing_low(klines.low, idx_l, idx_h)
        swing = None if valley is None else price_h - valley

        left_time = _format_bar_time(klines.iloc[idx_l]["datetime"])
        head_time = _format_bar_time(klines.iloc[idx_h]["datetime"])
        right_time = _format_bar_time(klines.iloc[idx_r]["datetime"])
        neck_l_time = _format_bar_time(klines.iloc[neck_l_idx]["datetime"])
        neck_r_time = _format_bar_time(klines.iloc[neck_r_idx]["datetime"])

        signal = Signal(
            target_volume=-volume,
            action="sell",
            reason=(
                f"{'超严格' if super_strict else '严格' if strict else ''}头肩顶右肩确认 左{price_l:.0f}({left_time}) "
                f"头{price_h:.0f}({head_time}) 右{price_r:.0f}({right_time}) 颈{neck:.0f}"
            ),
            detail={
                "side": "short",
                "strict": strict and not super_strict,
                "super_strict": super_strict,
                "pattern_id": pattern_id,
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
        return self._register_pending(
            signal, symbol, pattern_id, f"top-head:{head_time}", "short",
            price_h, neck, traded,
        )

    def _detect_head_shoulders_bottom(
        self,
        pivot_lows: list[tuple[int, float]],
        pivot_highs: list[tuple[int, float]],
        klines: pd.DataFrame,
        volume: int,
        traded: set[str],
        symbol: str,
        *,
        strict: bool = False,
        super_strict: bool = False,
    ) -> Signal | None:
        found = self._best_break(
            pivot_lows,
            pivot_highs,
            klines,
            traded,
            bottom=True,
            kind="bottom",
            strict=strict,
            super_strict=super_strict,
        )
        if found is None:
            return None
        idx_l, price_l, idx_h, price_h, idx_r, price_r, neck, pattern_id, neck_l_idx, neck_r_idx = found
        peak = self._swing_high(klines.high, idx_l, idx_h)
        swing = None if peak is None else peak - price_h

        left_time = _format_bar_time(klines.iloc[idx_l]["datetime"])
        head_time = _format_bar_time(klines.iloc[idx_h]["datetime"])
        right_time = _format_bar_time(klines.iloc[idx_r]["datetime"])
        neck_l_time = _format_bar_time(klines.iloc[neck_l_idx]["datetime"])
        neck_r_time = _format_bar_time(klines.iloc[neck_r_idx]["datetime"])

        signal = Signal(
            target_volume=volume,
            action="buy",
            reason=(
                f"{'超严格' if super_strict else '严格' if strict else ''}头肩底右肩确认 左{price_l:.0f}({left_time}) "
                f"头{price_h:.0f}({head_time}) 右{price_r:.0f}({right_time}) 颈{neck:.0f}"
            ),
            detail={
                "side": "long",
                "strict": strict and not super_strict,
                "super_strict": super_strict,
                "pattern_id": pattern_id,
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
        return self._register_pending(
            signal, symbol, pattern_id, f"bottom-head:{head_time}", "long",
            price_h, neck, traded,
        )

    def _reaction_neck(
        self,
        reaction_pivots: list[tuple[int, float]],
        i0: int,
        i1: int,
        *,
        lowest: bool,
    ) -> tuple[int, float] | None:
        """肩和头之间的颈线拐点，离开两端各至少一根即可。"""
        gap = 1
        inside = [p for p in reaction_pivots if i0 + gap <= p[0] <= i1 - gap]
        if not inside:
            return None
        if lowest:
            return min(inside, key=lambda p: (p[1], p[0]))
        return max(inside, key=lambda p: (p[1], -p[0]))

    def _tr(self, idx_l: int, idx_h: int, idx_r: int, why: str) -> None:
        if self.trace:
            self.last_trace.append((idx_l, idx_h, idx_r, why))

    def _best_break(
        self,
        pivots: list[tuple[int, float]],
        reaction_pivots: list[tuple[int, float]],
        klines: pd.DataFrame,
        traded: set[str],
        *,
        bottom: bool,
        kind: str,
        strict: bool = False,
        super_strict: bool = False,
    ) -> tuple[int, float, int, float, int, float, float, str, int, int] | None:
        """本根刚好确认右肩的形态里，取头部幅度最大的那一个（不必破颈线）。"""
        if len(pivots) < 3 or len(klines) < 2:
            return None
        pts = self._pivot_series(klines, is_high=not bottom)
        last_idx = len(klines) - 1
        pts_arr = np.asarray(pts, dtype="float64")
        agg = _range_aggs(pts_arr)
        dt_col = klines["datetime"].tolist()
        bt_cache: dict[int, str] = {}

        def bar_time(i: int) -> str:
            v = bt_cache.get(i)
            if v is None:
                v = _format_bar_time(dt_col[i])
                bt_cache[i] = v
            return v

        best: tuple[float, tuple] | None = None
        if self.trace:
            self.last_trace = []

        for hi, (idx_h, price_h) in enumerate(pivots):
            head_time = bar_time(idx_h)
            for li in range(hi):
                idx_l, price_l = pivots[li]
                if idx_h - idx_l < self.pivot_order:
                    self._tr(idx_l, idx_h, -1, "order_l")
                    continue
                if idx_h - idx_l > self.max_span_bars:
                    self._tr(idx_l, idx_h, -1, "span_l")
                    continue
                if bottom and price_l <= price_h:
                    self._tr(idx_l, idx_h, -1, "side_l")
                    continue
                if not bottom and price_l >= price_h:
                    self._tr(idx_l, idx_h, -1, "side_l")
                    continue
                if self.pre_trend and not pre_trend_ok(
                    klines["close"].to_numpy(dtype="float64"),
                    idx_l,
                    bottom=bottom,
                    window=self.trend_window,
                    threshold=self.trend_threshold,
                ):
                    self._tr(idx_l, idx_h, -1, "pre_trend")
                    continue
                # 只依赖 (li, hi) 的量在外层算一次，ri 循环里只查表
                neck_l = self._reaction_neck(
                    reaction_pivots, idx_l, idx_h, lowest=not bottom
                )
                between_l: list[tuple[int, float]] = []
                if self.strict_shape:
                    between_l = [
                        p for p in pivots[li + 1 : hi] if idx_h - p[0] > self.pivot_order
                    ]
                    d_l = (price_l - price_h) if bottom else (price_h - price_l)
                between_r: list[tuple[int, float]] = []
                for ri in range(hi + 1, len(pivots)):
                    idx_r, price_r = pivots[ri]
                    if self.strict_shape:
                        if ri > hi + 1:
                            prev = pivots[ri - 1]
                            if prev[0] - idx_h > self.pivot_order:
                                between_r.append(prev)
                        d_r = (price_r - price_h) if bottom else (price_h - price_r)
                        ref = self.dominance_reaction_ratio * min(d_l, d_r)
                        if bottom:
                            dominated = any(
                                p < price_l
                                and agg(idx_l, ip)[1] - price_l >= ref
                                for ip, p in between_l
                            ) or any(
                                p < price_r
                                and agg(ip, idx_r)[1] - price_r >= ref
                                for ip, p in between_r
                            )
                        else:
                            dominated = any(
                                p > price_l
                                and price_l - agg(idx_l, ip)[0] >= ref
                                for ip, p in between_l
                            ) or any(
                                p > price_r
                                and price_r - agg(ip, idx_r)[0] >= ref
                                for ip, p in between_r
                            )
                        if dominated:
                            self._tr(idx_l, idx_h, idx_r, "dominated")
                            continue
                    span = idx_r - idx_l
                    if span < self.min_span_bars:
                        self._tr(idx_l, idx_h, idx_r, "span_min")
                        continue
                    if span > self.max_span_bars:
                        self._tr(idx_l, idx_h, idx_r, "span_max")
                        break
                    if idx_r - idx_h < self.pivot_order:
                        self._tr(idx_l, idx_h, idx_r, "order_r")
                        continue
                    # 只在口径要求的那根上告警（默认=右肩收盘当根），错过就不补推旧形态
                    if not self._right_shoulder_confirmed(idx_r, last_idx):
                        self._tr(idx_l, idx_h, idx_r, "trigger")
                        continue
                    if bottom and price_r <= price_h:
                        self._tr(idx_l, idx_h, idx_r, "side_r")
                        continue
                    if not bottom and price_r >= price_h:
                        self._tr(idx_l, idx_h, idx_r, "side_r")
                        continue
                    # 窗口在滑，相对下标每根都会变，去重必须用 K 线时刻
                    pattern_id = (
                        f"{kind}:{bar_time(idx_l)}:{head_time}:{bar_time(idx_r)}"
                    )
                    if pattern_id in traded or f"{kind}-head:{head_time}" in traded:
                        self._tr(idx_l, idx_h, idx_r, "traded")
                        continue
                    win_lo, win_hi = agg(idx_l, idx_r + 1)
                    neck_r = self._reaction_neck(
                        reaction_pivots, idx_h, idx_r, lowest=not bottom
                    )
                    if neck_l is None or neck_r is None:
                        self._tr(idx_l, idx_h, idx_r, "neck_none")
                        continue
                    # 双重底/顶：两个颈锚点之间还有一根与头价差 ≤0.25×头高的同向极值；
                    # 平台底/顶：头附近（≤0.25×头高）连续 4 根以上收盘也算特殊形态
                    double_head = False
                    neck_head_pre = self._line_at(
                        neck_l[0], neck_l[1], neck_r[0], neck_r[1], idx_h
                    )
                    height_pre = (
                        neck_head_pre - price_h if bottom else price_h - neck_head_pre
                    )
                    if height_pre > 0:
                        tol_d = self.double_head_tolerance_ratio * height_pre
                        span_lo, span_hi = sorted((neck_l[0], neck_r[0]))
                        similar = [
                            p
                            for i, p in pivots
                            if span_lo < i < span_hi and abs(p - price_h) <= tol_d
                        ]
                        flat_tol = self.flat_head_tolerance_ratio * height_pre
                        near_bars = int(
                            (
                                np.abs(pts_arr[span_lo + 1 : span_hi] - price_h)
                                <= flat_tol
                            ).sum()
                        )
                        double_head = (
                            len(similar) >= 2
                            or near_bars >= self.flat_head_min_bars
                        )
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
                        closes=pts,
                        double_head=double_head,
                        strict=strict,
                    )
                    if shaped is None:
                        self._tr(idx_l, idx_h, idx_r, "shape")
                        continue
                    height, _neck_head = shaped
                    # 三重底/顶：头可偏离区间极值至多 0.25×头高（近似等高的三个极值也算成立）
                    tol_h = self.triple_head_tolerance_ratio * height
                    if bottom:
                        if price_h - win_lo > tol_h:
                            self._tr(idx_l, idx_h, idx_r, "window_extreme")
                            continue
                    elif win_hi - price_h > tol_h:
                        self._tr(idx_l, idx_h, idx_r, "window_extreme")
                        continue
                    n1, p1 = neck_l
                    n2, p2 = neck_r
                    if strict:
                        if idx_r - idx_l + 1 < self.strict_min_form_bars:
                            self._tr(idx_l, idx_h, idx_r, "form_bars")
                            continue
                        if self.strict_volume_gates and not self._volumes_ok(
                            klines,
                            reaction_pivots,
                            idx_l,
                            idx_h,
                            idx_r,
                            n1,
                            n2,
                            bottom=bottom,
                        ):
                            self._tr(idx_l, idx_h, idx_r, "volume")
                            continue
                    if super_strict:
                        sup_ok, _sup = check_super(
                            bottom=bottom,
                            left_price=price_l,
                            left_bar_high=float(klines["high"].iloc[idx_l]),
                            left_bar_low=float(klines["low"].iloc[idx_l]),
                            head_price=price_h,
                            right_price=price_r,
                            right_bar_high=float(klines["high"].iloc[idx_r]),
                            right_bar_low=float(klines["low"].iloc[idx_r]),
                            neck_l=(n1, p1),
                            neck_r=(n2, p2),
                            right_idx=idx_r,
                            min_head_points=self.min_head_neck_points,
                            min_head_ratio=self.min_head_neck_ratio,
                        )
                        if not sup_ok:
                            self._tr(idx_l, idx_h, idx_r, "super")
                            continue
                    neck_now = self._line_at(n1, p1, n2, p2, last_idx)
                    spread = abs(price_l - price_r)
                    # 肩形优先：两肩价差最小 → 最早合格右肩 → 左右腿最均衡 → 头高
                    # （三重底/顶时同一对肩下会选出中间那个更均衡的头）
                    # 腿长用别名，别覆盖 strict 块里按价格算的 d_l / d_r
                    leg_l = idx_h - idx_l
                    leg_r = idx_r - idx_h
                    key = (-spread, -idx_r, -abs(leg_l - leg_r), height)
                    if best is None or key > best[0]:
                        self._tr(idx_l, idx_h, idx_r, f"selected h={height:.1f} sp={spread:.1f}")
                        best = (
                            key,
                            spread,
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
        return best[2]

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
