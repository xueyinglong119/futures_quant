"""三重顶 / 三重底检测。

形态定义（用户 11-18 给的标准图例）：趋势末端三次冲到几乎同一水平、中间两次回落到同一条
水平颈线。

点位（三个顶/底、两个中间回调点）一律按收盘价量，拐点确认继承头肩。触发时点与头肩当前的
尾拐点口径一致：**第三脚那根收盘确认即出**，不等颈线被突破、不补推旧形态（11-17 铁律）。
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
    """三个相邻、近似等高的顶（底）+ 两个近似水平的中间回调点，第三脚当根出信号。

    硬性条件全 AND：
    ① **三个相邻极值**——P1/P2/P3 必须是连续三个拐点，且 P3 是当根。中间夹着别的
    拐点（台阶走势）或第三脚后又出新极值，都不是三重形态；
    ② 粗筛 `Dev(P2,P1)`、`Dev(P3,P1)`、`Dev(S2,S1)` ≤ `dev_threshold`（基准取第一个）；
    ③ 形态闸——`max(P)-min(P)` 与 `|S2-S1|` ≤ `triple_head_tolerance_ratio × height`
    （与头肩三重头同一把尺）。只按价格 2.5% 卡会放过台阶/通道（2222→2190→2184、
    8284/8306/8472 那种），等高与颈线水平必须按形态高度算；
    ④ 区间极值——`[P1, P3]` 内不能出现比 `min(P)`（顶为 `max(P)`）更低（高）超过
    同一容差的收盘，否则那是被第四脚打掉的形态；
    ⑤ `P1→P3` 根数 ≥ `min_form_bars`、≤ `max_span_bars`；
    ⑥ 量能——第三极值那条腿最弱：顶缩量（`V1>V3` 且 `V2>V3`）、底放量（`V1<V3` 且
    `V2<V3`），三种口径满足其一即过：段内同向根均量（均量前剔单根天量）、三个极值
    当根单量、或「当根单量逐次排 + 当根 MACD 柱逐次下降」；
    ⑦ 形态高度三件套——≥ `min_head_neck_points` 价点、≥ `min_head_neck_ratio` 比例、
    三个极值逐个离颈线 ≥ `shoulder_prominence_ratio`×高度；
    ⑧ 趋势方向（`trend_filter`，默认关，回测 A/B 用）——「趋势末端」的另一半：顶必须
    从上涨里来（P1 收盘高于等长前一段），底必须从下跌里来。没有这一闸时，暴跌后的
    横盘箱体里那组等高的**反弹高点**会被当成三重顶，实际上它是底部的颈线。

    时间对称只是软标记：`time_filter=False` 时 `TimeDev` 进 `detail` 与 reason，
    不参与过滤也不改手数。
    """

    def __init__(
        self,
        *args,
        dev_threshold: float = 0.01,
        min_form_bars: int = 10,
        time_dev_threshold: float = 0.5,
        time_filter: bool = False,
        trend_filter: bool = False,
        macd_fast: int = 12,
        macd_slow: int = 26,
        macd_signal: int = 9,
        macd_warmup_bars: int = 250,
        **kwargs,
    ) -> None:
        super().__init__(*args, **kwargs)
        self.dev_threshold = dev_threshold
        self.min_form_bars = min_form_bars
        self.time_dev_threshold = time_dev_threshold
        self.time_filter = time_filter
        self.trend_filter = trend_filter
        self.macd_fast = macd_fast
        self.macd_slow = macd_slow
        self.macd_signal = macd_signal
        self.macd_warmup_bars = macd_warmup_bars

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
        if len(main_pivots) < 3:
            return None
        # 三个「相邻」极值：连续三个拐点。中间夹着别的拐点（台阶走势）就不是三重形态。
        (idx1, price1), (idx2, price2), (idx3, price3) = main_pivots[-3:]
        if price1 <= 0 or price2 <= 0 or price3 <= 0:
            return None
        if last_idx - idx1 > self.max_span_bars:
            return None
        if abs(price2 - price1) / price1 > self.dev_threshold:
            return None
        if abs(price3 - price1) / price1 > self.dev_threshold:
            return None
        if idx3 - idx1 + 1 < self.min_form_bars:
            return None
        if self.trend_filter and not self._trend_ok(klines, idx1, idx3, bottom):
            return None

        vols = np.asarray(klines["volume"], dtype="float64")
        cand = self._check_legs(
            klines, vols, react_pivots, bottom,
            (idx1, price1), (idx2, price2), (idx3, price3),
        )
        if cand is None:
            return None
        return self._emit(cand, klines, volume, traded, symbol, bottom, kind)

    def _trend_ok(
        self, klines: pd.DataFrame, idx1: int, idx3: int, bottom: bool
    ) -> bool:
        """趋势末端：顶从上涨里来、底从下跌里来。

        拿进入形态前**同等长度**那一段的收盘作参照（P1 往前 `P3−P1` 根），不引入新参数。
        数据不够就看不了趋势，放行由后面的几何闸决定。
        """
        span = idx3 - idx1
        prior_idx = idx1 - span
        if span <= 0 or prior_idx < 0:
            return True
        closes = klines["close"]
        prior = float(closes.iloc[prior_idx])
        cur = float(closes.iloc[idx1])
        return cur < prior if bottom else cur > prior

    def _check_legs(
        self,
        klines: pd.DataFrame,
        vols: np.ndarray,
        react_pivots: list[tuple[int, float]],
        bottom: bool,
        p1: tuple[int, float],
        p2: tuple[int, float],
        p3: tuple[int, float],
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

        # 颈线取第三脚处的位置
        neck = self._line_at(idx_n1, v1, idx_n2, v2, idx3)
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
        if not self._volume_ok(
            klines, vols, ((start, idx1), (idx_n1, idx2), (idx_n2, idx3)), bottom
        ):
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

    def _volume_ok(
        self,
        klines: pd.DataFrame,
        vols: np.ndarray,
        legs: tuple[tuple[int, int], ...],
        bottom: bool,
    ) -> bool:
        """量能闸：第三个极值那条腿必须最弱，三种口径满足其一即放行。

        顶要第三段缩量（V1>V3 且 V2>V3），底要第三段放量（V1<V3 且 V2<V3）。
        口径1 段内同向根均量。顶只数上涨根、底只数下跌根——砸出颈线的那几根反向
        放量不算这一波的推动量，混进段均量会把「第三段确实在缩」误判成放量。
        口径2 三个极值当根单量，同上。
        口径3 三个极值当根单量逐次排（顶 V1>V2>V3、底 V1<V2<V3），
        且三个极值当根的 MACD 柱逐次下降。
        口径3 的量条件蕴含口径2（逐次排必然第三根最小），这是用户 11-20 明确要
        保留的原样规格，别当冗余删掉。
        """
        at_bar = tuple(float(vols[b]) for _, b in legs)
        means = self._same_dir_means(vols, klines, legs, bottom=bottom)
        if means is not None and self._last_leg_smallest(means, bottom):
            return True
        if self._last_leg_smallest(at_bar, bottom):
            return True
        return self._momentum_fades(klines, at_bar, legs, bottom)

    def _same_dir_means(
        self,
        vols: np.ndarray,
        klines: pd.DataFrame,
        legs: tuple[tuple[int, int], ...],
        *,
        bottom: bool,
    ) -> tuple[float, float, float] | None:
        """每段只取同向那几根的均量；某段一根同向都没有，这条口径判不了。"""
        closes = klines["close"].to_numpy(dtype="float64")
        out: list[float] = []
        for a, b in legs:
            sel = [
                k
                for k in range(max(1, a) + 1, b + 1)
                if (closes[k] < closes[k - 1] if bottom else closes[k] > closes[k - 1])
            ]
            if not sel:
                return None
            out.append(float(self._pulse_kept(vols[sel]).mean()))
        return (out[0], out[1], out[2])

    def _momentum_fades(
        self,
        klines: pd.DataFrame,
        at_bar: tuple[float, float, float],
        legs: tuple[tuple[int, int], ...],
        bottom: bool,
    ) -> bool:
        """口径3：当根单量逐次排 + 三个极值当根的 MACD 柱逐次下降。"""
        v1, v2, v3 = at_bar
        if not ((v1 < v2 < v3) if bottom else (v1 > v2 > v3)):
            return False
        m = self._macd_at(klines, legs)
        return bool(m[0] > m[1] > m[2])

    def _macd_at(
        self, klines: pd.DataFrame, legs: tuple[tuple[int, int], ...]
    ) -> tuple[float, float, float]:
        """三个极值当根的通达信 MACD 柱：`2×(DIF-DEA)`，DIF=EMA(f)-EMA(s)，DEA=EMA(DIF,g)。

        只从第一个极值往前多取 `macd_warmup_bars` 根算：逐根回放里全量重算是 O(n²)，
        而 EMA 递归经过 250 根后初值权重已衰减到 1e-8，与全序列取值实质相同。
        """
        closes = klines["close"].to_numpy(dtype="float64")
        lo = max(0, legs[0][1] - self.macd_warmup_bars)
        s = pd.Series(closes[lo : legs[-1][1] + 1])
        dif = s.ewm(span=self.macd_fast, adjust=False).mean() - s.ewm(
            span=self.macd_slow, adjust=False
        ).mean()
        hist = 2.0 * (dif - dif.ewm(span=self.macd_signal, adjust=False).mean()).to_numpy()
        return tuple(float(hist[b - lo]) for _, b in legs)

    @staticmethod
    def _last_leg_smallest(vol: tuple[float, float, float], bottom: bool) -> bool:
        """第三段量最小：顶要缩、底要放，前两段的相对大小不作要求。"""
        return (vol[0] < vol[2] and vol[1] < vol[2]) if bottom else (
            vol[0] > vol[2] and vol[1] > vol[2]
        )

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
