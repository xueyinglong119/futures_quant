"""行情数据接口"""

from __future__ import annotations

import math
import time as time_module
from datetime import date, datetime, timedelta
from typing import Any, Literal
from zoneinfo import ZoneInfo

import pandas as pd
from tqsdk import TqApi, TqAuth

from config.settings import Settings, load_settings
from utils.symbols import normalize_contract_symbol
from utils.trading_session import aggregate_session_bars, get_day_30min_schedule_text

DEFAULT_KLINE_SECONDS = 30 * 60
ONE_MINUTE_SECONDS = 60
TZ_SHANGHAI = ZoneInfo("Asia/Shanghai")


def get_today_klines(
    symbol: str,
    *,
    trade_date: date | None = None,
    duration_seconds: int = DEFAULT_KLINE_SECONDS,
    settings: Settings | None = None,
    wait_timeout: float = 30.0,
    time_label: Literal["start", "end"] = "end",
    session_aligned: bool = True,
) -> dict[str, Any]:
    """
    获取指定合约在某一自然日的 K 线（默认当天、30 分钟、按国内小节休市规则合成）。

    30 分钟日盘时间点（time_label=end）：
    9:30, 10:00, 10:45, 11:15, 13:45, 14:15, 14:45, 15:00
    11:15 后午休；13:30 开盘首根结束标注为 13:45。
    """
    cfg = settings or load_settings()
    tq_symbol = normalize_contract_symbol(symbol)
    target_date = trade_date or date.today()
    use_session = session_aligned and duration_seconds == DEFAULT_KLINE_SECONDS

    api = TqApi(auth=TqAuth(cfg.auth.user, cfg.auth.password))
    try:
        fetch_seconds = ONE_MINUTE_SECONDS if use_session else duration_seconds
        klines = api.get_kline_serial(tq_symbol, fetch_seconds, data_length=2000)
        deadline = time_module.time() + wait_timeout
        while time_module.time() < deadline:
            api.wait_update(deadline=deadline)
            if _klines_ready(klines):
                break
        else:
            raise TimeoutError(f"等待 {tq_symbol} K 线超时（{wait_timeout}s）")

        if use_session:
            minute_bars = _parse_minute_bars(klines, target_date)
            bars = aggregate_session_bars(
                minute_bars, target_date, time_label=time_label
            )
            time_note = get_day_30min_schedule_text()
        else:
            bars = _filter_klines_by_date(
                klines, target_date, duration_seconds, time_label
            )
            time_note = (
                f"非 30 分钟或未启用 session_aligned，按 TqSdk {duration_seconds // 60} "
                "分钟周期原样返回。"
            )

        return {
            "symbol": tq_symbol,
            "trade_date": target_date.isoformat(),
            "duration_minutes": duration_seconds // 60,
            "time_label": time_label,
            "session_aligned": use_session,
            "time_note": time_note,
            "count": len(bars),
            "klines": bars,
        }
    finally:
        api.close()


def get_today_klines_df(
    symbol: str,
    *,
    trade_date: date | None = None,
    duration_seconds: int = DEFAULT_KLINE_SECONDS,
    settings: Settings | None = None,
    time_label: Literal["start", "end"] = "end",
) -> pd.DataFrame:
    """同上，返回 pandas DataFrame。"""
    result = get_today_klines(
        symbol,
        trade_date=trade_date,
        duration_seconds=duration_seconds,
        settings=settings,
        time_label=time_label,
    )
    if not result["klines"]:
        return pd.DataFrame(
            columns=[
                "datetime",
                "datetime_start",
                "datetime_end",
                "open",
                "high",
                "low",
                "close",
                "volume",
                "open_oi",
            ]
        )
    df = pd.DataFrame(result["klines"])
    df["datetime"] = pd.to_datetime(df["datetime"])
    return df


def _klines_ready(klines: pd.DataFrame) -> bool:
    if klines is None or len(klines) == 0:
        return False
    last_dt = klines.iloc[-1]["datetime"]
    return not (last_dt is None or (isinstance(last_dt, float) and math.isnan(last_dt)))


def _parse_minute_bars(klines: pd.DataFrame, target: date) -> list[dict]:
    """解析 TqSdk 1 分钟 K 线，保留目标日及前一自然日（夜盘）以便过滤。"""
    rows: list[dict] = []
    for i in range(len(klines)):
        dt_ns = klines.iloc[i]["datetime"]
        if dt_ns is None or (isinstance(dt_ns, float) and math.isnan(dt_ns)):
            continue
        dt = datetime.fromtimestamp(dt_ns / 1e9, tz=TZ_SHANGHAI).replace(tzinfo=None)
        if dt.date() not in (target, target - timedelta(days=1)):
            continue
        rows.append(
            {
                "datetime": dt,
                "open": float(klines.iloc[i]["open"]),
                "high": float(klines.iloc[i]["high"]),
                "low": float(klines.iloc[i]["low"]),
                "close": float(klines.iloc[i]["close"]),
                "volume": int(klines.iloc[i]["volume"]),
                "open_oi": int(klines.iloc[i]["open_oi"]),
            }
        )
    return rows


def _filter_klines_by_date(
    klines: pd.DataFrame,
    target: date,
    duration_seconds: int,
    time_label: Literal["start", "end"] = "start",
) -> list[dict]:
    """TqSdk 原生周期过滤（未按小节休市重算）。"""
    rows: list[dict] = []
    duration = timedelta(seconds=duration_seconds)
    for i in range(len(klines)):
        dt_ns = klines.iloc[i]["datetime"]
        if dt_ns is None or (isinstance(dt_ns, float) and math.isnan(dt_ns)):
            continue
        dt_start = datetime.fromtimestamp(dt_ns / 1e9, tz=TZ_SHANGHAI).replace(tzinfo=None)
        if dt_start.date() != target:
            continue
        dt_end = dt_start + duration
        label_dt = dt_end if time_label == "end" else dt_start
        rows.append(
            {
                "datetime": label_dt.isoformat(timespec="seconds"),
                "datetime_start": dt_start.isoformat(timespec="seconds"),
                "datetime_end": dt_end.isoformat(timespec="seconds"),
                "open": float(klines.iloc[i]["open"]),
                "high": float(klines.iloc[i]["high"]),
                "low": float(klines.iloc[i]["low"]),
                "close": float(klines.iloc[i]["close"]),
                "volume": int(klines.iloc[i]["volume"]),
                "open_oi": int(klines.iloc[i]["open_oi"]),
            }
        )
    return rows
