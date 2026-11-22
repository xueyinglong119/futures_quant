"""趋势斜率公共口径：三重与头肩共用。

`calc_slope` 是原始线性回归斜率（点/根）；`pre_trend_ok` 把它按窗口均价归一化，
这样 `trend_threshold=0.0002` 在 2200 和 85000 的品种上是同一含义（≈0.02%/根）。
"""

from __future__ import annotations

import numpy as np


def calc_slope(x_arr, y_arr) -> float:
    x = np.asarray(x_arr, dtype="float64")
    y = np.asarray(y_arr, dtype="float64")
    if x.size < 2 or y.size < 2:
        return 0.0
    return float(np.polyfit(x, y, 1)[0])


def pre_trend_ok(
    closes,
    idx: int,
    *,
    bottom: bool,
    window: int = 20,
    threshold: float = 0.0002,
) -> bool:
    """idx 之前 window 根收盘的趋势方向；顶要上升、底要下降，震荡返回 False。"""
    closes = np.asarray(closes, dtype="float64")
    y = closes[max(0, idx - window) : idx]
    if y.size < 2:
        return False
    slope = calc_slope(np.arange(y.size), y)
    mean = float(np.mean(np.abs(y)))
    if mean <= 0:
        return False
    rel = slope / mean
    return rel < -threshold if bottom else rel > threshold