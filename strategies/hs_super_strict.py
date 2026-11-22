"""超严格头肩底/顶附加校验（用户 2026-11-21 规格）。

在现有严格档命中（右肩收盘当根）之上再卡：

1. 底：右肩反弹幅度 ≥ 左肩 * 0.75；顶：右肩下跌幅度 ≥ 左肩 * 0.75
2. 底：颈线斜率 ≥ 0；顶：颈线斜率 ≤ 0
3. 右肩 K 线 high/low 到颈线（外推到右肩那根）的缺口 ≤ 头部深度 * 0.25
4. 底：左右肩低点价差 ≤ 头部深度 * 0.3；顶：左右肩高点价差 ≤ 0.3
5. 右肩之后 3 根不能创新高/新低（当根看不到，`pullback_ok` 单独做后验）
6. 头部深度不能太小

`check()` 只算当根可判的 1/2/3/4/6；规则 5 由 `pullback_ok()` 在右肩后第 3 根回填。
价格点沿用检测器的收盘价口径，肩部 K 线的高低点取该根 `high` / `low`。
"""

from __future__ import annotations

import pandas as pd

MIN_REBOUND_RATIO = 0.75
MAX_GAP_RATIO = 0.25
MAX_SHOULDER_DIFF_RATIO = 0.30
PULLBACK_BARS = 3


def _line_at(n1: int, p1: float, n2: int, p2: float, i: int) -> float:
    if n2 == n1:
        return (p1 + p2) / 2.0
    return p1 + (p2 - p1) * (i - n1) / (n2 - n1)


def check(
    *,
    bottom: bool,
    left_price: float,
    left_bar_high: float,
    left_bar_low: float,
    head_price: float,
    right_price: float,
    right_bar_high: float,
    right_bar_low: float,
    neck_l: tuple[int, float],
    neck_r: tuple[int, float],
    right_idx: int,
    min_head_points: float = 10.0,
    min_head_ratio: float = 0.008,
) -> tuple[bool, dict[str, float | str]]:
    """当根可判的五条（1/2/3/4/6）。返回 (是否通过, 指标)。"""
    n1, p1 = neck_l
    n2, p2 = neck_r
    head_depth = (p1 - head_price) if bottom else (head_price - p1)
    info: dict[str, float | str] = {"head_depth": head_depth}
    if head_depth <= 0:
        info["reason"] = "头部深度<=0"
        return False, info
    if head_depth < min_head_points or abs(head_price) == 0:
        info["reason"] = "头部深度过小"
        return False, info
    if head_depth / abs(head_price) < min_head_ratio:
        info["reason"] = "头部深度占比过小"
        return False, info

    if bottom:
        left_reb = left_bar_high - head_price
        right_reb = right_bar_high - head_price
        ratio = right_reb / left_reb if left_reb > 0 else 0.0
    else:
        left_drop = head_price - left_bar_low
        right_drop = head_price - right_bar_low
        ratio = right_drop / left_drop if left_drop > 0 else 0.0
    info["rebound_ratio"] = ratio
    if ratio < MIN_REBOUND_RATIO:
        info["reason"] = f"右肩幅度不足 {ratio:.3f}<{MIN_REBOUND_RATIO}"
        return False, info

    slope = (p2 - p1) / (n2 - n1) if n2 != n1 else 0.0
    info["neck_slope"] = slope
    if (bottom and slope < 0) or (not bottom and slope > 0):
        info["reason"] = f"颈线斜率方向不对 {slope:.3f}"
        return False, info

    neck_at_r = _line_at(n1, p1, n2, p2, right_idx)
    shoulder_edge = right_bar_high if bottom else right_bar_low
    gap_ratio = abs(neck_at_r - shoulder_edge) / head_depth
    info["gap_ratio"] = gap_ratio
    if gap_ratio > MAX_GAP_RATIO:
        info["reason"] = f"右肩离颈太远 {gap_ratio:.3f}>{MAX_GAP_RATIO}"
        return False, info

    left_edge = left_price if bottom else left_bar_high
    right_edge = right_price if bottom else right_bar_high
    diff_ratio = abs(left_edge - right_edge) / head_depth
    info["shoulder_diff_ratio"] = diff_ratio
    if diff_ratio > MAX_SHOULDER_DIFF_RATIO:
        info["reason"] = f"两肩价差过大 {diff_ratio:.3f}>{MAX_SHOULDER_DIFF_RATIO}"
        return False, info

    info["reason"] = "通过"
    return True, info


def pullback_ok(
    klines: pd.DataFrame,
    right_idx: int,
    right_bar_high: float,
    right_bar_low: float,
    *,
    bottom: bool,
    bars: int = PULLBACK_BARS,
) -> bool | None:
    """规则 5：右肩之后 `bars` 根不能创新高（底）/新低（顶）。数据不足返回 None。"""
    end = right_idx + bars
    if end >= len(klines):
        return None
    if bottom:
        return float(klines["high"].iloc[right_idx + 1 : end + 1].max()) <= right_bar_high
    return float(klines["low"].iloc[right_idx + 1 : end + 1].min()) >= right_bar_low