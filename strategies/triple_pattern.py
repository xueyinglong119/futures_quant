"""三重顶 / 三重底检测。

形态定义（用户 11-18 给的标准图例）：趋势末端三次冲到几乎同一水平、中间两次回落到同一条
水平颈线，**收盘越过颈线才确认形态**——底向上突破、顶向下跌破。

点位（三个顶/底、两个中间回调点）一律按收盘价量，拐点确认继承头肩；区别在触发时点：
三重不在第三脚出，而是等到颈线被收盘突破的那根才出。
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
    """三个相邻、近似等高的顶（底）+ 两个近似水平的中间回调点；收盘越过颈线才出信号。

    硬性条件全 AND：
    ① **三个相邻极值**——P1/P2/P3 必须是连续三个确认拐点，且 P3 是最近一个。中间夹着
    别的拐点（台阶走势）或第三脚后又出新极值，都不是三重形态；
    ② 粗筛 `Dev(P2,P1)`、`Dev(P3,P1)`、`Dev(S2,S1)` ≤ `dev_threshold`（基准取第一个）；
    ③ 形态闸——`max(P)-min(P)` 与 `|S2-S1|` ≤ `triple_head_tolerance_ratio × height`
    （与头肩三重头同一把尺）。只按价格 2.5% 卡会放过台阶/通道（2222→2190→2184、
    8284/8306/8472 那种），等高与颈线水平必须按形态高度算；
    ④ 区间极值——`[P1, 突破根]` 内不能出现比 `min(P)`（顶为 `max(P)`）更低（高）
    超过同一容差的收盘，否则那是被第四脚打掉的形态；
    ⑤ **颈线突破**——底线收盘 > 颈线、顶线收盘 < 颈线，这根才是触发根；
    ⑥ `P1→P3` 根数 ≥ `min_form_bars`、≤ `max_span_bars`；
    ⑦ 量能——底：第三脚之后到突破根均量 > 第三段均量（图例「量增」）；
    顶：三段均量递减（最大量 → 次大量 → 最小量），均量前剔单根天量；
    ⑧ 形态高度三件套——≥ `min_head_neck_points` 价点、≥ `min_head_neck_ratio` 比例、
    三个极值逐个离颈线 ≥ `shoulder_prominence_ratio`×高度。

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
        # 颈线突破才确认：第三脚和颈线必须在突破根之前就成立，所以只用确认拐点，
        # 不挂当根预备极值（那等于把还没站稳的第三脚当成触发点）
        pivot_highs = self._find_pivot_highs(highs)
        pivot_lows = self._find_pivot_lows(lows)
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
        if len(main_pivots) < 3:
            return None
        # 三个「相邻」极值：连续三个确认拐点，且第三个就是最近一个。
        # 中间夹着别的拐点（台阶走势）或第三脚后又出新极值，都不是三重形态。
        (idx1, price1), (idx2, price2), (idx3, price3) = main_pivots[-3:]
        if price1 <= 0 or price2 <= 0 or price3 <= 0:
            return None
        if idx3 >= last_idx or last_idx - idx1 > self.max_span_bars:
            return None
        if abs(price2 - price1) / price1 > self.dev_threshold:
            return None
        if abs(price3 - price1) / price1 > self.dev_threshold:
            return None
        if idx3 - idx1 + 1 < self.min_form_bars:
            return None

        vols = np.asarray(klines["volume"], dtype="float64")
        cand = self._check_legs(
            klines, vols, react_pivots, bottom,
            (idx1, price1), (idx2, price2), (idx3, price3), last_idx,
        )
        if cand is None:
            return None
        if not self._breakout_ok(cand, klines["close"], last_idx, bottom):
            return None
        return self._emit(cand, klines, volume, traded, symbol, bottom, kind)

    def _breakout_ok(
        self, cand: dict[str, float], closes: pd.Series, last_idx: int, bottom: bool
    ) -> bool:
        """当前收盘越过颈线（底向上、顶向下）才确认；越过之前不做判断。"""
        line = self._line_at(
            int(cand["idx_n1"]), float(cand["neck_l"]),
            int(cand["idx_n2"]), float(cand["neck_r"]), last_idx,
        )
        close = float(closes.iloc[last_idx])
        if bottom:
            if close <= line:
                return False
        elif close >= line:
            return False
        # 第三脚之后不能再破三个极值的区间，否则形态已经被第四脚打掉
        tol = self.triple_head_tolerance_ratio * float(cand["height"])
        peak = float(cand["peak"])
        seg = closes.iloc[int(cand["idx3"]) : last_idx + 1]
        if bottom:
            return float(seg.min()) >= peak - tol
        return float(seg.max()) <= peak + tol

    def _check_legs(
        self,
        klines: pd.DataFrame,
        vols: np.ndarray,
        react_pivots: list[tuple[int, float]],
        bottom: bool,
        p1: tuple[int, float],
        p2: tuple[int, float],
        p3: tuple[int, float],
        break_idx: int,
    ) -> dict[str, float] | None:
        """中间两个回调点 + 等高/水平/区间极值三道形态闸 + 高度三件套 + 量能。"""
        idx1, price1 = p1
        idx2, price2 = p2
        idx3, price3 = p3
        # 底的回调点是反弹高（取最高），顶的回调点是回落低（取最低），与头肩同口径
        s1 = self._reaction_neck(react_pivots, idx1, idx2, lowest=not bottom)
        if s1 is None:
            return None
        s2 = self._reaction_neck(react_pivots, idx2, idx3, lowest=not bottom)
        if s2 is None:
            return None
        idx_n1, v1 = s1
        idx_n2, v2 = s2
        if v1 <= 0 or abs(v2 - v1) / v1 > self.dev_threshold:
            return None

        tops = (price1, price2, price3)
        peak = min(tops) if bottom else max(tops)
        neck_mid = (v1 + v2) / 2.0
        height = (neck_mid - peak) if bottom else (peak - neck_mid)
        if height <= 0 or abs(peak) == 0:
            return None
        # 三个极值必须近乎等高、两个回调点必须近乎同水平。
        # 只卡价格的 2.5% 会把台阶/通道（2222→2190→2184）也当成三重形态，这里改用
        # 形态高度的比例——与头肩三重头容差同一把尺。
        tol = self.triple_head_tolerance_ratio * height
        if max(tops) - min(tops) > tol:
            return None
        if abs(v2 - v1) > tol:
            return None
        # 三个极值要是整个区间的最高/最低：区间里还藏着更低的底就不是三重底
        window = klines["close"].iloc[idx1 : idx3 + 1]
        if bottom:
            if float(window.min()) < peak - tol:
                return None
        elif float(window.max()) > peak + tol:
            return None

        # 颈线取突破根处的位置：告警与量度目标都按突破价算
        neck = self._line_at(idx_n1, v1, idx_n2, v2, break_idx)
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
            # 底：第三脚之后到突破根要放量（图例里三重底标的就是「量增」）
            v_break = self._vol_avg(vols, idx3, break_idx)
            if v_break is None or v_break <= v_seg3:
                return None
        elif not (v_seg2 <= v_seg1 and v_seg3 < v_seg2):
            # 顶：三段量递减（最大量 → 次大量 → 最小量）
            return None

        t1 = idx2 - idx1
        t2 = idx3 - idx2
        time_dev = abs(t1 - t2) / max(t1, t2) if max(t1, t2) > 0 else 1.0
        if self.time_filter and time_dev > self.time_dev_threshold:
            return None
        spread = abs(price2 - price1) / price1 + abs(price3 - price1) / price1
        return {
            "idx1": idx1, "price1": price1,
            "idx2": idx2, "price2": price2,
            "idx3": idx3, "price3": price3,
            "idx_n1": idx_n1, "idx_n2": idx_n2,
            "neck": neck, "neck_l": v1, "neck_r": v2, "peak": peak, "height": height,
            "spread": spread, "time_dev": time_dev,
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
        # 量度目标 = 颈线 ± 形态高度；突破入场时它才是止盈/参考目标，
        # 颈线本身就在脚下，不能再当目标
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
        break_time = _format_bar_time(klines.iloc[-1]["datetime"])
        p1 = float(cand["price1"])
        p2 = float(cand["price2"])
        p3 = float(cand["price3"])
        symmetric = "时间对称" if time_dev <= self.time_dev_threshold else "时间不对称"
        tag = "底" if bottom else "顶"

        signal = Signal(
            target_volume=volume if bottom else -volume,
            action="buy" if bottom else "sell",
            reason=(
                f"{KIND_TEXT[kind]}颈线突破 {tag}1 {p1:.0f}({t1}) {tag}2 {p2:.0f}({t2}) "
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
                "break_time": break_time,
                "left_idx": i1,
                "head_idx": i2,
                "right_idx": i3,
            },
        )
        self._tr(i1, i2, i3, f"selected triple-{kind}")
        return self._register_pending(
            signal, symbol, pattern_id, peak_key,
            "long" if bottom else "short", peak, measured, traded,
        )
