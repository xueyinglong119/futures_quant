"""从库内 1 分钟 K 按国内交易时段重算 30 分 / 1 小时。

写入 kline_bars 时使用北京时间结束标注（与盘面一致），例如 10:00 / 11:15 / 14:15 / 15:00。
会先删除该品种对应周期在重建范围内的旧行（含天勤整点口径），再 upsert 新结果。
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any, Iterable, Literal

from pymysql.connections import Connection

from core.kline_store import (
    DURATION_1H,
    DURATION_1M,
    DURATION_30M,
    load_bars,
    upsert_bars,
)
from utils.trading_session import (
    aggregate_session_1h_bars,
    aggregate_session_bars,
)

SessionTf = Literal["30m", "1h"]

TF_DURATION: dict[SessionTf, int] = {
    "30m": DURATION_30M,
    "1h": DURATION_1H,
}


def detect_1m_clock(bars: list[dict[str, Any]]) -> str:
    """粗判 1 分钟库内时间是北京还是 UTC。"""
    if not bars:
        return "beijing"
    session_hits = 0
    utc_session_hits = 0
    for b in bars[-300:]:
        dt = b["datetime_end"]
        hm = dt.hour * 60 + dt.minute
        if 9 * 60 <= hm <= 15 * 60 or 21 * 60 <= hm <= 23 * 60:
            session_hits += 1
        bj = dt + timedelta(hours=8)
        bhm = bj.hour * 60 + bj.minute
        if 9 * 60 <= bhm <= 15 * 60 or 21 * 60 <= bhm <= 23 * 60:
            utc_session_hits += 1
    return "beijing" if session_hits >= utc_session_hits else "utc"


def minutes_as_beijing(bars: list[dict[str, Any]], clock: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for b in bars:
        dt = b["datetime_end"]
        if clock == "utc":
            dt = dt + timedelta(hours=8)
        out.append(
            {
                "datetime": dt,
                "open": b["open"],
                "high": b["high"],
                "low": b["low"],
                "close": b["close"],
                "volume": b["volume"],
                "open_oi": b.get("open_oi") or 0,
            }
        )
    return out


def _parse_iso_dt(raw: str) -> datetime:
    return datetime.fromisoformat(raw)


def session_dicts_to_store_rows(session_bars: list[dict]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for b in session_bars:
        dt_end = _parse_iso_dt(b["datetime_end"])
        rows.append(
            {
                "datetime_end": dt_end,
                "open": float(b["open"]),
                "high": float(b["high"]),
                "low": float(b["low"]),
                "close": float(b["close"]),
                "volume": int(b["volume"]),
                "open_oi": int(b.get("open_oi") or 0),
            }
        )
    return rows


def aggregate_symbol_session(
    minute_bj: list[dict[str, Any]],
    tfs: Iterable[SessionTf] = ("30m", "1h"),
    *,
    include_night: bool = True,
) -> dict[SessionTf, list[dict[str, Any]]]:
    """按自然日分组合成小节 K，返回可入库行。"""
    by_day: dict[date, list[dict[str, Any]]] = {}
    for m in minute_bj:
        by_day.setdefault(m["datetime"].date(), []).append(m)

    result: dict[SessionTf, list[dict[str, Any]]] = {tf: [] for tf in tfs}
    for trade_date in sorted(by_day):
        day_mins = by_day[trade_date]
        if "30m" in result:
            raw = aggregate_session_bars(
                day_mins, trade_date, include_night=include_night
            )
            result["30m"].extend(session_dicts_to_store_rows(raw))
        if "1h" in result:
            raw = aggregate_session_1h_bars(
                day_mins, trade_date, include_night=include_night
            )
            result["1h"].extend(session_dicts_to_store_rows(raw))
    return result


def delete_duration_range(
    conn: Connection,
    symbol: str,
    duration_sec: int,
    start: datetime | None,
    end: datetime | None,
) -> int:
    """删除某品种某周期在时间窗内的旧 K（重建前清理天勤整点口径）。"""
    clauses = ["symbol=%s", "duration_sec=%s"]
    params: list[Any] = [symbol, duration_sec]
    if start is not None:
        clauses.append("datetime_end>=%s")
        params.append(start)
    if end is not None:
        clauses.append("datetime_end<=%s")
        params.append(end)
    sql = f"DELETE FROM kline_bars WHERE {' AND '.join(clauses)}"
    with conn.cursor() as cur:
        cur.execute(sql, params)
        n = cur.rowcount
    conn.commit()
    return int(n)


def rebuild_symbol_session_klines(
    conn: Connection,
    symbol: str,
    *,
    tfs: Iterable[SessionTf] = ("30m", "1h"),
    start: datetime | None = None,
    end: datetime | None = None,
    execute: bool = False,
    include_night: bool = True,
) -> dict[str, int]:
    """从 1 分钟重算并（可选）写入小节 30m/1h。

    execute=False 时只统计将生成的根数，不删不写。
    """
    tf_list = list(tfs)
    raw_1m = load_bars(conn, symbol, DURATION_1M)
    clock = detect_1m_clock(raw_1m)
    minute_bj = minutes_as_beijing(raw_1m, clock)
    if start is not None or end is not None:
        clipped = []
        for m in minute_bj:
            dt = m["datetime"]
            if start is not None and dt < start:
                continue
            if end is not None and dt > end:
                continue
            clipped.append(m)
        minute_bj = clipped

    built = aggregate_symbol_session(
        minute_bj, tf_list, include_night=include_night
    )
    stats: dict[str, int] = {
        "1m_used": len(minute_bj),
        "clock_1m": 1 if clock == "beijing" else 0,
    }
    for tf in tf_list:
        rows = built.get(tf, [])
        stats[f"{tf}_bars"] = len(rows)
        if not execute:
            continue
        dur = TF_DURATION[tf]
        # 清理窗：北京标注与旧 UTC 整点都可能落在 [start-1d, end+1d]
        del_start = (start - timedelta(days=1)) if start else None
        del_end = (end + timedelta(days=1)) if end else None
        deleted = delete_duration_range(conn, symbol, dur, del_start, del_end)
        stats[f"{tf}_deleted"] = deleted
        stats[f"{tf}_upserted"] = upsert_bars(conn, symbol, dur, rows) if rows else 0
    return stats
