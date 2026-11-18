"""三重顶 / 三重底检测。

点位（三个顶/底、两个中间回调点）一律按收盘价量，拐点确认与当根触发直接继承头肩，
所以两个通道在同一根 K 线上说的是同一套时刻语义，胜率也能并排读。
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from strategies.base import Signal
from strategies.head_shoulders import HeadShouldersStrategy, _format_bar_time

TOP = "top"
BOTTOM = "bottom"
KIND_TEXT = {TOP: "三重顶", BOTTOM: "三重底"}


class TriplePatternStrategy(HeadShouldersStrategy):
    """三个近似等高的顶（底）+ 两个近似水平的中间回调点，颈线只算量度目标、不是开仓条件。

    硬性条件全 AND：`Dev(P2,P1)`、`Dev(P3,P1)`、`Dev(S2,S1)` 都 ≤ `dev_threshold`
    （基准点位都取第一个，与规格里的 `Abs(P2-P1)/P1` 一致）；P1→P3 总根数 ≥ `min_form_bars`
    且不超过 `max_span_bars`；三段均量递减（顶 2≤1 且 3<2，底把 < 与 ≤ 对调；均量前先按
    `PULSE_MULTIPLE` 剔单根天量）；
    形态高度用头肩同款三件套 —— ≥ `min_head_neck_points` 个价点、≥ `min_head_neck_ratio`
    的价格比例、且三个顶都高出颈线 ≥ `shoulder_prominence_ratio`×高度。最后一条是防横盘箱体
    三次触碰同构命中：没有高度约束，水平箱体一定会被当成三重形态。

    P3 就是当前这根已收盘 K 线（`_with_tail_pivot` 的当根口径），不延后、不补推旧形态。
    时间对称只是软标记：`time_filter=False` 时 `TimeDev` 进 `detail` 与 reason，
    不参与过滤也不改手数。
    """

    def __init__(
        self,
        *args,
        dev_threshold: float = 0.025,
        min_form_bars: int = 10,
        time_dev_threshold: float = 0.5,
        time_filter: bool = False,
        **kwargs,
    ) -> None:
        super().__init__(*args, **kwargs)
        self.dev_threshold = dev_threshold
        self.min_form_bars = min_form_bars
        self.time_dev_threshold = time_dev_threshold
        self.time_filter = time_filter

    def _scan(
        self,
        klines: pd.DataFrame,
        max_volume: int | None,
        symbol: str,
        *,
        strict: bool,
    ) -> Signal | None:
        volume = max_volume if max_volume is not None else self.max_volume
        min_len = self.max_span_bars + self.pivot_order * 2 + 5
        if len(klines) < min_len:
            return None

        highs = self._pivot_series(klines, is_high=True)
        lows = self._pivot_series(klines, is_high=False)
        if self.confirmed_shoulders:
            pivot_highs = self._find_pivot_highs(highs)
            pivot_lows = self._find_pivot_lows(lows)
        else:
            pivot_highs = self._with_tail_pivot(
                self._find_pivot_highs(highs), highs, is_high=True
            )
            pivot_lows = self._with_tail_pivot(
                self._find_pivot_lows(lows), lows, is_high=False
            )
        traded = self._traded_patterns.setdefault(symbol, set())

        top = self._detect(
            klines, pivot_highs, pivot_lows, volume, traded, symbol, bottom=False
        )
        if top is not None:
            return top
        return self._detect(
            klines, pivot_lows, pivot_highs, volume, traded, symbol, bottom=True
        )

    def _detect(
        self,
        klines: pd.DataFrame,
        main_pivots: list[tuple[int, float]],
        react_pivots: list[tuple[int, float]],
        volume: int,
        traded: set[str],
        symbol: str,
        *,
        bottom: bool,
    ) -> Signal | None:
        kind = BOTTOM if bottom else TOP
        last_idx = len(klines) - 1
        # 第三个极值必须是当根，否则这条就是被补推的旧形态
        if not main_pivots or main_pivots[-1][0] != last_idx:
            return None
        idx3, price3 = main_pivots[-1]
        if price3 <= 0:
            return None
        earlier = [
            p for p in main_pivots[:-1] if p[0] >= idx3 - self.max_span_bars and p[1] > 0
        ]
        if len(earlier) < 2:
            return None

        vols = np.asarray(klines["volume"], dtype="float64")
        best: tuple[tuple[float, float, int], dict[str, float]] | None = None
        for a, (idx1, price1) in enumerate(earlier):
            dev3 = abs(price3 - price1) / price1
            if dev3 > self.dev_threshold:
                continue
            if idx3 - idx1 + 1 < self.min_form_bars:
                continue
            for idx2, price2 in earlier[a + 1 :]:
                dev2 = abs(price2 - price1) / price1
                if dev2 > self.dev_threshold:
                    continue
                found = self._check_legs(
                    klines, vols, react_pivots, bottom,
                    (idx1, price1), (idx2, price2), (idx3, price3), dev2, dev3,
                )
                if found is None:
                    continue
                key = (found["spread"], found["time_dev"], -idx1)
                if best is None or key < best[0]:
                    best = (key, found)

        if best is None:
            return None
        cand = best[1]
        return self._emit(cand, klines, volume, traded, symbol, bottom, kind)

    def _check_legs(
        self,
        klines: pd.DataFrame,
        vols: np.ndarray,
        react_pivots: list[tuple[int, float]],
        bottom: bool,
        p1: tuple[int, float],
        p2: tuple[int, float],
        p3: tuple[int, float],
        dev2: float,
        dev3: float,
    ) -> dict[str, float] | None:
        """中间两个回调点 + 颈线 + 高度三件套 + 量能递减，逐条不过就返回 None。"""
        idx1, price1 = p1
        idx2, price2 = p2
        idx3, price3 = p3
        s1 = self._reaction_neck(react_pivots, idx1, idx2, lowest=bottom)
        if s1 is None:
            return None
        s2 = self._reaction_neck(react_pivots, idx2, idx3, lowest=bottom)
        if s2 is None:
            return None
        idx_n1, v1 = s1
        idx_n2, v2 = s2
        if v1 <= 0 or abs(v2 - v1) / v1 > self.dev_threshold:
            return None

        neck = self._line_at(idx_n1, v1, idx_n2, v2, idx3)
        peak = (
            min(price1, price2, price3) if bottom else max(price1, price2, price3)
        )
        height = (neck - peak) if bottom else (peak - neck)
        if height <= 0 or abs(peak) == 0:
            return None
        if height < self.min_head_neck_points:
            return None
        if height / abs(peak) < self.min_head_neck_ratio:
            return None
        prom = self.shoulder_prominence_ratio * height
        for idx, price in (p1, p2, p3):
            line = self._line_at(idx_n1, v1, idx_n2, v2, idx)
            off = (line - price) if bottom else (price - line)
            if off < prom:
                return None

        start = idx1 - self.max_span_bars
        prior = [q[0] for q in react_pivots if q[0] < idx1]
        if prior:
            start = max(start, max(prior))
        v_seg1 = self._vol_avg(vols, start, idx1)
        v_seg2 = self._vol_avg(vols, idx_n1, idx2)
        v_seg3 = self._vol_avg(vols, idx_n2, idx3)
        if v_seg1 is None or v_seg2 is None or v_seg3 is None:
            return None
        if bottom:
            if not (v_seg2 < v_seg1 and v_seg3 <= v_seg2):
                return None
        elif not (v_seg2 <= v_seg1 and v_seg3 < v_seg2):
            return None

        t1 = idx2 - idx1
        t2 = idx3 - idx2
        time_dev = abs(t1 - t2) / max(t1, t2) if max(t1, t2) > 0 else 1.0
        if self.time_filter and time_dev > self.time_dev_threshold:
            return None
        return {
            "idx1": idx1, "price1": price1,
            "idx2": idx2, "price2": price2,
            "idx3": idx3, "price3": price3,
            "idx_n1": idx_n1, "idx_n2": idx_n2,
            "neck": neck, "peak": peak, "height": height,
            "spread": dev2 + dev3, "time_dev": time_dev,
        }

    def _emit(
        self,
        cand: dict[str, float],
        klines: pd.DataFrame,
        volume: int,
        traded: set[str],
        symbol: str,
        bottom: bool,
        kind: str,
    ) -> Signal | None:
        i1 = int(cand["idx1"])
        i2 = int(cand["idx2"])
        i3 = int(cand["idx3"])
        t1 = _format_bar_time(klines.iloc[i1]["datetime"])
        t2 = _format_bar_time(klines.iloc[i2]["datetime"])
        t3 = _format_bar_time(klines.iloc[i3]["datetime"])
        pattern_id = f"triple-{kind}:{t1}:{t2}:{t3}"
        peak_key = f"triple-{kind}-p2:{t2}"
        if pattern_id in traded or peak_key in traded:
            self._tr(i1, i2, i3, "traded")
            return None

        neck = float(cand["neck"])
        height = float(cand["height"])
        peak = float(cand["peak"])
        # 量度目标只作参考，不参与判断，也不进止盈止损（那条仍按 0.8/0.2×高度）
        measured = neck + height if bottom else neck - height
        swing = height
        extreme = (
            self._swing_high(klines.high, i1, i3)
            if bottom
            else self._swing_low(klines.low, i1, i3)
        )
        time_dev = float(cand["time_dev"])
        neck_l_time = _format_bar_time(klines.iloc[int(cand["idx_n1"])]["datetime"])
        neck_r_time = _format_bar_time(klines.iloc[int(cand["idx_n2"])]["datetime"])
        p1 = float(cand["price1"])
        p2 = float(cand["price2"])
        p3 = float(cand["price3"])
        symmetric = "时间对称" if time_dev <= self.time_dev_threshold else "时间不对称"
        tag = "底" if bottom else "顶"

        signal = Signal(
            target_volume=volume if bottom else -volume,
            action="buy" if bottom else "sell",
            reason=(
                f"{KIND_TEXT[kind]}确认 {tag}1 {p1:.0f}({t1}) {tag}2 {p2:.0f}({t2}) "
                f"{tag}3 {p3:.0f}({t3}) 颈{neck:.0f} 量度{measured:.0f} {symmetric}"
            ),
            detail={
                "side": "long" if bottom else "short",
                "strict": False,
                "pattern_id": pattern_id,
                "pattern_kind": KIND_TEXT[kind],
                "left": p1,
                "head": p2,
                "right": p3,
                "neck": neck,
                "extreme": extreme,
                "swing": swing,
                "measured": measured,
                "time_dev": round(time_dev, 3),
                "left_time": t1,
                "head_time": t2,
                "right_time": t3,
                "neck_l_time": neck_l_time,
                "neck_r_time": neck_r_time,
                "left_idx": i1,
                "head_idx": i2,
                "right_idx": i3,
            },
        )
        self._tr(i1, i2, i3, f"selected triple-{kind}")
        return self._register_pending(
            signal, symbol, pattern_id, peak_key,
            "long" if bottom else "short", peak, neck, traded,
        )
