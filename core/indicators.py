"""
常用主图指标（基于 OHLCV）

- MA: 简单移动平均
- EXPMA / EMA: 指数移动平均
- BOLL: 布林带
- 神奇九转: TD Sequential 结构计数（买/卖 Setup 1~9）
"""

from __future__ import annotations

from typing import Iterable, Sequence

import numpy as np
import pandas as pd

# 与常见行情软件默认一致
DEFAULT_MA_PERIODS = (5, 10, 20, 30, 60, 120, 250)
DEFAULT_BOLL_PERIOD = 20
DEFAULT_BOLL_STD = 2.0
DEFAULT_EXPMA_PERIODS = (12, 50)


def _as_close_series(df: pd.DataFrame, col: str = "close") -> pd.Series:
    if col not in df.columns:
        raise KeyError(f"缺少列 {col}")
    return pd.to_numeric(df[col], errors="coerce")


def ma(series: pd.Series, period: int) -> pd.Series:
    """简单移动平均 MA"""
    if period < 1:
        raise ValueError("period 必须 >= 1")
    return series.rolling(window=period, min_periods=period).mean()


def expma(series: pd.Series, period: int) -> pd.Series:
    """指数移动平均 EXPMA / EMA（adjust=False，贴近行情软件）"""
    if period < 1:
        raise ValueError("period 必须 >= 1")
    return series.ewm(span=period, adjust=False, min_periods=period).mean()


def boll(
    series: pd.Series,
    period: int = DEFAULT_BOLL_PERIOD,
    num_std: float = DEFAULT_BOLL_STD,
) -> pd.DataFrame:
    """
    布林带 BOLL
    mid = MA(period)
    upper/lower = mid ± num_std * STD(period)
    """
    mid = ma(series, period)
    std = series.rolling(window=period, min_periods=period).std(ddof=0)
    return pd.DataFrame(
        {
            "boll_mid": mid,
            "boll_upper": mid + num_std * std,
            "boll_lower": mid - num_std * std,
        }
    )


def magic_nine(df: pd.DataFrame) -> pd.DataFrame:
    """
    神奇九转（TD Sequential Setup）

    卖 Setup（高九）: 收盘价 > 4 根前收盘，连续计数 1..9
    买 Setup（低九）: 收盘价 < 4 根前收盘，连续计数 1..9

    返回列:
      td_sell: 上升计数，到 9 为卖九转信号位（其余为 0 或 1..8）
      td_buy:  下降计数，到 9 为买九转信号位
      td_sell_nine: bool，是否卖九
      td_buy_nine: bool，是否买九
    """
    close = _as_close_series(df)
    n = len(close)
    sell = np.zeros(n, dtype=int)
    buy = np.zeros(n, dtype=int)

    for i in range(n):
        if i < 4:
            continue
        # 卖结构
        if close.iloc[i] > close.iloc[i - 4]:
            sell[i] = sell[i - 1] + 1 if sell[i - 1] > 0 else 1
            if sell[i] > 9:
                sell[i] = 1
        else:
            sell[i] = 0
        # 买结构
        if close.iloc[i] < close.iloc[i - 4]:
            buy[i] = buy[i - 1] + 1 if buy[i - 1] > 0 else 1
            if buy[i] > 9:
                buy[i] = 1
        else:
            buy[i] = 0

    out = pd.DataFrame(
        {
            "td_sell": sell,
            "td_buy": buy,
            "td_sell_nine": sell == 9,
            "td_buy_nine": buy == 9,
        },
        index=df.index,
    )
    return out


def compute_overlay_indicators(
    df: pd.DataFrame,
    ma_periods: Sequence[int] = DEFAULT_MA_PERIODS,
    expma_periods: Sequence[int] = DEFAULT_EXPMA_PERIODS,
    boll_period: int = DEFAULT_BOLL_PERIOD,
    boll_std: float = DEFAULT_BOLL_STD,
) -> pd.DataFrame:
    """
    在 K 线 DataFrame 上附加主图指标列。
    需要至少包含 close；神奇九转还用不到 high/low，但保留完整 df。
    """
    if df is None or len(df) == 0:
        return pd.DataFrame()

    out = df.copy()
    close = _as_close_series(out)

    for p in ma_periods:
        out[f"ma{p}"] = ma(close, int(p))

    for p in expma_periods:
        out[f"expma{p}"] = expma(close, int(p))

    bb = boll(close, period=boll_period, num_std=boll_std)
    out = pd.concat([out, bb], axis=1)

    nine = magic_nine(out)
    out = pd.concat([out, nine], axis=1)
    return out


def latest_indicator_snapshot(df: pd.DataFrame) -> dict:
    """取最后一根已计算指标的快照，便于 API / 打印。"""
    if df is None or len(df) == 0:
        return {}
    row = df.iloc[-1]
    snap: dict = {}
    if "datetime_end" in df.columns:
        snap["datetime_end"] = str(row["datetime_end"])
    if "close" in df.columns:
        snap["close"] = float(row["close"])

    for col in df.columns:
        if col.startswith("ma") or col.startswith("expma") or col.startswith("boll_"):
            val = row[col]
            snap[col] = None if pd.isna(val) else float(val)

    for col in ("td_sell", "td_buy"):
        if col in df.columns:
            snap[col] = int(row[col])
    for col in ("td_sell_nine", "td_buy_nine"):
        if col in df.columns:
            snap[col] = bool(row[col])
    return snap


def bars_from_records(records: Iterable[dict]) -> pd.DataFrame:
    """把 kline_store.load_bars 结果转成指标计算用 DataFrame。"""
    df = pd.DataFrame(list(records))
    if df.empty:
        return df
    if "datetime_end" in df.columns:
        df = df.sort_values("datetime_end").reset_index(drop=True)
    return df
