"""用天勤最新已收盘 K 线覆盖修复「今天」入库数据。

按库里既有钟（UTC / 北京）写入，避免同一根插两套时间戳。
月/季/年在日线修复后重算。
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import date, datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tqsdk import TqApi, TqAuth

from config.settings import load_settings
from core.cont_symbols import list_commodity_mains
from core.kline_fetch import (
    aggregate_monthly_from_daily,
    aggregate_quarterly_from_daily,
    aggregate_yearly_from_daily,
    wait_klines_ready,
)
from core.kline_store import (
    DURATION_1D,
    DURATION_1MON,
    DURATION_1Q,
    DURATION_1Y,
    DURATION_LABELS,
    INGEST_DURATIONS,
    connect,
    ensure_table,
    load_bars,
    upsert_bars,
)
from core.realtime_pipeline import (
    TAIL_LENGTH,
    detect_clock,
    serial_to_rows,
)

TZ = ZoneInfo("Asia/Shanghai")


def _parse_day(raw: str | None) -> date:
    if not raw:
        return datetime.now(TZ).date()
    return date.fromisoformat(raw)


def repair_symbol(
    api: TqApi,
    conn,
    symbol: str,
    day: date,
    durations: list[int],
) -> dict[str, int]:
    now_utc = datetime.now(timezone.utc).replace(tzinfo=None, microsecond=0)
    serials = {}
    for dur in durations:
        length = TAIL_LENGTH.get(dur, 200)
        try:
            serials[dur] = api.get_kline_serial(symbol, dur, data_length=length)
        except Exception as exc:  # noqa: BLE001
            print(f"  订阅失败 {DURATION_LABELS.get(dur, dur)}: {exc}", flush=True)
    if not serials:
        return {}
    wait_klines_ready(api, list(serials.values()), timeout_sec=120)

    wrote: dict[str, int] = {}
    for dur, kl in serials.items():
        label = DURATION_LABELS.get(dur, str(dur))
        closed = serial_to_rows(kl, dur, now_utc)
        if not closed:
            wrote[label] = 0
            continue
        recent = load_bars(conn, symbol, dur, limit=40)
        clock = detect_clock(recent, closed)
        bars = []
        for row in closed:
            stored = row["utc"] if clock == "utc" else row["beijing"]
            bj = row["beijing"]
            # 覆盖交易日：自然日匹配，或夜盘归到下一交易日的前夜（21-23 点算前一日夜盘）
            if bj.date() != day:
                continue
            bars.append(
                {
                    "datetime_end": stored,
                    "open": row["open"],
                    "high": row["high"],
                    "low": row["low"],
                    "close": row["close"],
                    "volume": row["volume"],
                    "open_oi": row["open_oi"],
                }
            )
        n = upsert_bars(conn, symbol, dur, bars) if bars else 0
        wrote[label] = n
    if wrote.get("1d", 0) > 0:
        daily = load_bars(conn, symbol, DURATION_1D)
        upsert_bars(conn, symbol, DURATION_1MON, aggregate_monthly_from_daily(daily))
        upsert_bars(conn, symbol, DURATION_1Q, aggregate_quarterly_from_daily(daily))
        upsert_bars(conn, symbol, DURATION_1Y, aggregate_yearly_from_daily(daily))
        wrote["htf"] = 1
    return wrote


def main() -> None:
    parser = argparse.ArgumentParser(description="修复当日多周期 K 线入库")
    parser.add_argument("--day", default=None, help="交易日 YYYY-MM-DD，默认今天(北京)")
    parser.add_argument("--symbols", default=None, help="逗号分隔主连，默认全部商品主力")
    parser.add_argument(
        "--durations",
        default=None,
        help="逗号分隔周期，默认全部 INGEST_DURATIONS",
    )
    args = parser.parse_args()
    day = _parse_day(args.day)
    settings = load_settings()

    if args.durations:
        from core.kline_store import LABEL_TO_DURATION

        durations = []
        for part in args.durations.split(","):
            key = part.strip().lower()
            if key not in LABEL_TO_DURATION:
                raise SystemExit(f"未知周期: {key}")
            durations.append(LABEL_TO_DURATION[key])
    else:
        durations = list(INGEST_DURATIONS)

    api = TqApi(auth=TqAuth(settings.auth.user, settings.auth.password))
    conn = connect()
    ensure_table(conn)
    try:
        if args.symbols:
            symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
        else:
            symbols = list_commodity_mains(api)
        print(
            f"repair day={day} symbols={len(symbols)} "
            f"durations={','.join(DURATION_LABELS[d] for d in durations)}",
            flush=True,
        )
        t0 = time.time()
        total_bars = 0
        for i, symbol in enumerate(symbols, 1):
            st = time.time()
            wrote = repair_symbol(api, conn, symbol, day, durations)
            n = sum(v for k, v in wrote.items() if k != "htf")
            total_bars += n
            detail = ", ".join(f"{k}={v}" for k, v in wrote.items() if v)
            print(
                f"[{i}/{len(symbols)}] {symbol} bars={n} "
                f"({time.time() - st:.1f}s) {detail}",
                flush=True,
            )
        print(
            f"done total_bars={total_bars} elapsed={time.time() - t0:.1f}s",
            flush=True,
        )
    finally:
        conn.close()
        api.close()


if __name__ == "__main__":
    main()
