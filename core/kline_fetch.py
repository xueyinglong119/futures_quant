"""从 TqSdk 拉取多周期 K 线（直播序列 / 专业版下载 / 回测补历史）"""

from __future__ import annotations

import math
import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import pandas as pd
from tqsdk import TqApi, TqAuth, TqBacktest
from tqsdk.exceptions import BacktestFinished
from tqsdk.tools import DataDownloader

from config.settings import Settings, load_settings

# 免费行情单序列最大有效根数约 10000
LIVE_MAX_LENGTH = 10000


def _ns_to_dt(ns: Any) -> datetime | None:
    if ns is None or (isinstance(ns, float) and math.isnan(ns)):
        return None
    return pd.to_datetime(int(ns), unit="ns").to_pydatetime()


def _row_to_bar(klines: pd.DataFrame, index: int) -> dict[str, Any] | None:
    dt = _ns_to_dt(klines.iloc[index]["datetime"])
    if dt is None:
        return None
    oi = klines.iloc[index]["open_oi"]
    if isinstance(oi, float) and math.isnan(oi):
        oi = 0
    vol = klines.iloc[index]["volume"]
    if isinstance(vol, float) and math.isnan(vol):
        vol = 0
    return {
        "datetime_end": dt,
        "open": float(klines.iloc[index]["open"]),
        "high": float(klines.iloc[index]["high"]),
        "low": float(klines.iloc[index]["low"]),
        "close": float(klines.iloc[index]["close"]),
        "volume": int(vol),
        "open_oi": int(oi),
    }


def _serial_to_bars(
    klines: pd.DataFrame,
    start: datetime | None = None,
    end: datetime | None = None,
    drop_last: bool = False,
) -> list[dict[str, Any]]:
    if len(klines) == 0:
        return []
    last_idx = len(klines) - (2 if drop_last and len(klines) > 1 else 1)
    bars: list[dict[str, Any]] = []
    for i in range(0, last_idx + 1):
        bar = _row_to_bar(klines, i)
        if bar is None:
            continue
        dt = bar["datetime_end"]
        if start is not None and dt < start:
            continue
        if end is not None and dt > end:
            continue
        bars.append(bar)
    return bars


def _is_ready(klines: pd.DataFrame) -> bool:
    if klines is None or len(klines) == 0:
        return False
    last = klines.iloc[-1]["datetime"]
    return not (isinstance(last, float) and math.isnan(last))


def wait_klines_ready(
    api: TqApi,
    series: list[pd.DataFrame],
    timeout_sec: float = 60,
) -> bool:
    deadline = time_monotonic(api) + timeout_sec
    while time_monotonic(api) < deadline:
        remain = deadline - time_monotonic(api)
        if remain <= 0:
            break
        api.wait_update(deadline=time_monotonic(api) + min(1.0, remain))
        if all(_is_ready(kl) for kl in series):
            return True
    return all(_is_ready(kl) for kl in series)


def time_monotonic(api: TqApi) -> float:
    return float(api._loop.time())


def fetch_live_multi(
    api: TqApi,
    symbol: str,
    durations: list[int],
    start: datetime,
    end: datetime,
    data_length: int = LIVE_MAX_LENGTH,
) -> dict[int, list[dict[str, Any]]]:
    """一次订阅多周期，等待就绪后导出。"""
    serials: dict[int, pd.DataFrame] = {
        dur: api.get_kline_serial(symbol, dur, data_length=data_length)
        for dur in durations
    }
    wait_klines_ready(api, list(serials.values()), timeout_sec=90)
    result: dict[int, list[dict[str, Any]]] = {}
    for dur, kl in serials.items():
        if not _is_ready(kl):
            result[dur] = []
            continue
        result[dur] = _serial_to_bars(kl, start=start, end=end, drop_last=True)
    return result


def fetch_live_bars(
    api: TqApi,
    symbol: str,
    duration_sec: int,
    start: datetime,
    end: datetime,
    data_length: int = LIVE_MAX_LENGTH,
) -> list[dict[str, Any]]:
    multi = fetch_live_multi(api, symbol, [duration_sec], start, end, data_length)
    return multi.get(duration_sec, [])


def fetch_download_bars(
    api: TqApi,
    symbol: str,
    duration_sec: int,
    start_dt: date,
    end_dt: date,
    tmp_dir: str | Path | None = None,
) -> list[dict[str, Any]]:
    """
    专业版 DataDownloader 拉取历史 K 线。
    需要账号具备 tq_dl 权限（试用/正式专业版）。
    """
    if not api._auth._has_feature("tq_dl"):
        raise RuntimeError(
            "当前账号无历史下载权限(tq_dl)，请开通天勤专业版试用或正式版"
        )

    out_dir = Path(tmp_dir) if tmp_dir else Path(tempfile.gettempdir())
    out_dir.mkdir(parents=True, exist_ok=True)
    safe = symbol.replace("@", "_").replace(".", "_")
    csv_path = out_dir / f"dl_{safe}_{duration_sec}.csv"

    task = DataDownloader(
        api,
        symbol_list=symbol,
        dur_sec=duration_sec,
        start_dt=start_dt,
        end_dt=end_dt,
        csv_file_name=str(csv_path),
    )
    while not task.is_finished():
        api.wait_update()

    return _parse_downloader_csv(csv_path, symbol)


def _parse_downloader_csv(csv_path: Path, symbol: str) -> list[dict[str, Any]]:
    if not csv_path.exists() or csv_path.stat().st_size == 0:
        return []
    df = pd.read_csv(csv_path)
    if df.empty:
        return []

    open_col = f"{symbol}.open"
    if open_col not in df.columns:
        # 兼容仅单列前缀差异
        cols = [c for c in df.columns if c.endswith(".open")]
        if not cols:
            return []
        prefix = cols[0][: -len(".open")]
        open_col = f"{prefix}.open"
        high_col = f"{prefix}.high"
        low_col = f"{prefix}.low"
        close_col = f"{prefix}.close"
        vol_col = f"{prefix}.volume"
        oi_col = f"{prefix}.open_oi"
    else:
        high_col = f"{symbol}.high"
        low_col = f"{symbol}.low"
        close_col = f"{symbol}.close"
        vol_col = f"{symbol}.volume"
        oi_col = f"{symbol}.open_oi"

    dts = pd.to_datetime(df["datetime"])
    opens = df[open_col].to_numpy(dtype=float)
    highs = df[high_col].to_numpy(dtype=float)
    lows = df[low_col].to_numpy(dtype=float)
    closes = df[close_col].to_numpy(dtype=float)
    vols = df[vol_col].fillna(0).to_numpy(dtype=float)
    ois = df[oi_col].fillna(0).to_numpy(dtype=float)
    bars: list[dict[str, Any]] = []
    for i in range(len(df)):
        bars.append(
            {
                "datetime_end": dts.iloc[i].to_pydatetime(),
                "open": float(opens[i]),
                "high": float(highs[i]),
                "low": float(lows[i]),
                "close": float(closes[i]),
                "volume": int(vols[i]),
                "open_oi": int(ois[i]),
            }
        )
    return bars


def fetch_backtest_bars(
    symbol: str,
    duration_sec: int,
    start_dt: date,
    end_dt: date,
    settings: Settings | None = None,
    data_length: int = LIVE_MAX_LENGTH,
) -> list[dict[str, Any]]:
    """
    回测区间拉取 K 线。
    推进过程中累积已收盘 K 线，结束时再合并序列中剩余根。
    """
    cfg = settings or load_settings()
    api = TqApi(
        auth=TqAuth(cfg.auth.user, cfg.auth.password),
        backtest=TqBacktest(start_dt=start_dt, end_dt=end_dt),
    )
    collected: dict[int, dict[str, Any]] = {}
    klines = api.get_kline_serial(symbol, duration_sec, data_length=data_length)
    try:
        while True:
            api.wait_update()
            if len(klines) < 2:
                continue
            bar = _row_to_bar(klines, -2)
            if bar is not None:
                key = int(pd.Timestamp(bar["datetime_end"]).value)
                collected[key] = bar
    except BacktestFinished:
        pass
    finally:
        for i in range(len(klines)):
            bar = _row_to_bar(klines, i)
            if bar is None:
                continue
            key = int(pd.Timestamp(bar["datetime_end"]).value)
            collected[key] = bar
        api.close()

    start_bound = datetime.combine(start_dt, datetime.min.time())
    end_bound = datetime.combine(end_dt, datetime.max.time())
    return [
        collected[k]
        for k in sorted(collected)
        if start_bound <= collected[k]["datetime_end"] <= end_bound
    ]


def month_ranges(start: date, end: date) -> list[tuple[date, date]]:
    """按自然月切分 [start, end]（两端含）。"""
    ranges: list[tuple[date, date]] = []
    cur = date(start.year, start.month, 1)
    while cur <= end:
        if cur.month == 12:
            nxt = date(cur.year + 1, 1, 1)
        else:
            nxt = date(cur.year, cur.month + 1, 1)
        month_end = min(nxt - timedelta(days=1), end)
        month_start = max(cur, start)
        if month_start <= month_end:
            ranges.append((month_start, month_end))
        cur = nxt
    return ranges


def _aggregate_buckets(
    buckets: dict[Any, list[dict[str, Any]]],
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for key in sorted(buckets):
        group = sorted(buckets[key], key=lambda b: b["datetime_end"])
        out.append(
            {
                "datetime_end": group[-1]["datetime_end"],
                "open": group[0]["open"],
                "high": max(b["high"] for b in group),
                "low": min(b["low"] for b in group),
                "close": group[-1]["close"],
                "volume": sum(int(b["volume"]) for b in group),
                "open_oi": int(group[-1]["open_oi"]),
            }
        )
    return out


def aggregate_monthly_from_daily(daily_bars: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """按自然月聚合日线为月线。"""
    if not daily_bars:
        return []
    buckets: dict[tuple[int, int], list[dict[str, Any]]] = {}
    for bar in daily_bars:
        dt: datetime = bar["datetime_end"]
        buckets.setdefault((dt.year, dt.month), []).append(bar)
    return _aggregate_buckets(buckets)


def aggregate_quarterly_from_daily(daily_bars: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """按自然季聚合日线为季线。"""
    if not daily_bars:
        return []
    buckets: dict[tuple[int, int], list[dict[str, Any]]] = {}
    for bar in daily_bars:
        dt: datetime = bar["datetime_end"]
        q = (dt.month - 1) // 3 + 1
        buckets.setdefault((dt.year, q), []).append(bar)
    return _aggregate_buckets(buckets)


def aggregate_yearly_from_daily(daily_bars: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """按自然年聚合日线为年线。"""
    if not daily_bars:
        return []
    buckets: dict[int, list[dict[str, Any]]] = {}
    for bar in daily_bars:
        dt: datetime = bar["datetime_end"]
        buckets.setdefault(dt.year, []).append(bar)
    return _aggregate_buckets(buckets)


def aggregate_from_lower(
    bars: list[dict[str, Any]],
    duration_sec: int,
) -> list[dict[str, Any]]:
    """将更小周期 K 线按固定秒数聚合（用于 1m -> 5m）。"""
    if not bars:
        return []
    buckets: dict[int, list[dict[str, Any]]] = {}
    for bar in bars:
        dt: datetime = bar["datetime_end"]
        ts = int(dt.timestamp())
        aligned = ts - (ts % duration_sec) + duration_sec
        buckets.setdefault(aligned, []).append(bar)

    out: list[dict[str, Any]] = []
    for aligned in sorted(buckets):
        group = sorted(buckets[aligned], key=lambda b: b["datetime_end"])
        out.append(
            {
                "datetime_end": datetime.fromtimestamp(aligned),
                "open": group[0]["open"],
                "high": max(b["high"] for b in group),
                "low": min(b["low"] for b in group),
                "close": group[-1]["close"],
                "volume": sum(int(b["volume"]) for b in group),
                "open_oi": int(group[-1]["open_oi"]),
            }
        )
    return out
