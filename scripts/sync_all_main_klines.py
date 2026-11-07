"""
同步全主力主连多周期 K 线到 MySQL（future_ai.kline_bars）

周期对齐常见软件:
  日/周/月/季/年 + 1/3/5/10/15/30分 + 1/2/3/4小时

用法示例:
  python scripts/sync_all_main_klines.py --mode live
  python scripts/sync_all_main_klines.py --mode download
  python scripts/sync_all_main_klines.py --mode download --durations 3m,10m,15m,3h,4h
  python scripts/sync_all_main_klines.py --mode aggregate-htf
  python scripts/sync_all_main_klines.py --mode summary
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tqsdk import TqApi, TqAuth

from config.settings import load_settings
from core.cont_symbols import list_main_continuous
from core.kline_fetch import (
    LIVE_MAX_LENGTH,
    aggregate_from_lower,
    aggregate_monthly_from_daily,
    aggregate_quarterly_from_daily,
    aggregate_yearly_from_daily,
    fetch_backtest_bars,
    fetch_download_bars,
    fetch_live_bars,
    month_ranges,
)
from core.kline_store import (
    DOWNLOAD_GAP_DURATIONS,
    DURATION_1D,
    DURATION_1H,
    DURATION_1M,
    DURATION_1MON,
    DURATION_1Q,
    DURATION_1W,
    DURATION_1Y,
    DURATION_2H,
    DURATION_3H,
    DURATION_4H,
    DURATION_5M,
    DURATION_30M,
    DURATION_LABELS,
    LABEL_TO_DURATION,
    connect,
    count_bars,
    ensure_table,
    list_symbols,
    load_bars,
    upsert_bars,
)

# 直播可覆盖的中长周期
LIVE_DURATIONS: list[tuple[int, int]] = [
    (DURATION_30M, 8000),
    (DURATION_1H, 5000),
    (DURATION_2H, 4000),
    (DURATION_3H, 3000),
    (DURATION_4H, 2500),
    (DURATION_1D, 800),
    (DURATION_1W, 150),
]


def _parse_durations(raw: str | None, default: list[int]) -> list[int]:
    if not raw:
        return list(default)
    out: list[int] = []
    for part in raw.split(","):
        key = part.strip().lower()
        if not key:
            continue
        if key not in LABEL_TO_DURATION:
            raise SystemExit(f"未知周期: {key}，可选: {', '.join(DURATION_LABELS.values())}")
        out.append(LABEL_TO_DURATION[key])
    return out


def sync_live(
    symbols: list[str],
    start: date,
    end: date,
    settings,
) -> None:
    start_dt = datetime.combine(start, datetime.min.time())
    end_dt = datetime.combine(end, datetime.max.time())
    api = TqApi(auth=TqAuth(settings.auth.user, settings.auth.password))
    conn = connect()
    ensure_table(conn)
    try:
        total_symbols = len(symbols)
        for idx, symbol in enumerate(symbols, 1):
            print(f"\n[{idx}/{total_symbols}] LIVE {symbol}", flush=True)
            t0 = time.time()
            daily_bars: list[dict] = []
            for dur, length in LIVE_DURATIONS:
                label = DURATION_LABELS[dur]
                try:
                    bars = fetch_live_bars(
                        api,
                        symbol,
                        dur,
                        start=start_dt,
                        end=end_dt,
                        data_length=length,
                    )
                    n = upsert_bars(conn, symbol, dur, bars)
                    print(
                        f"  {label}: {n} bars range="
                        f"{bars[0]['datetime_end'] if bars else '-'} ~ "
                        f"{bars[-1]['datetime_end'] if bars else '-'}",
                        flush=True,
                    )
                    if dur == DURATION_1D:
                        daily_bars = bars
                except Exception as exc:  # noqa: BLE001
                    print(f"  {label}: FAIL {exc}", flush=True)

            if daily_bars:
                for dur, agg, name in (
                    (DURATION_1MON, aggregate_monthly_from_daily, "1mon"),
                    (DURATION_1Q, aggregate_quarterly_from_daily, "1q"),
                    (DURATION_1Y, aggregate_yearly_from_daily, "1y"),
                ):
                    bars = agg(daily_bars)
                    n = upsert_bars(conn, symbol, dur, bars)
                    print(f"  {name}: {n} bars (from daily)", flush=True)
            print(f"  done in {time.time() - t0:.1f}s", flush=True)
    finally:
        conn.close()
        api.close()


def sync_download(
    symbols: list[str],
    start: date,
    end: date,
    settings,
    durations: list[int] | None = None,
) -> None:
    """专业版 DataDownloader 拉取指定周期。"""
    durs = durations or [DURATION_1M, DURATION_5M]
    tmp_dir = ROOT / "data" / "kline_dl_tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)

    api = TqApi(auth=TqAuth(settings.auth.user, settings.auth.password))
    conn = connect()
    ensure_table(conn)
    try:
        if not api._auth._has_feature("tq_dl"):
            raise RuntimeError("账号无 tq_dl，请先开通天勤专业版试用")

        total = len(symbols)
        for idx, symbol in enumerate(symbols, 1):
            print(f"\n[{idx}/{total}] DOWNLOAD {symbol}", flush=True)
            t0 = time.time()
            for dur in durs:
                label = DURATION_LABELS.get(dur, str(dur))
                try:
                    t1 = time.time()
                    print(f"  {label}: downloading...", flush=True)
                    bars = fetch_download_bars(
                        api, symbol, dur, start, end, tmp_dir=tmp_dir
                    )
                    print(
                        f"  {label}: got {len(bars)} bars "
                        f"({time.time() - t1:.1f}s), writing MySQL...",
                        flush=True,
                    )
                    n = upsert_bars(conn, symbol, dur, bars)
                    print(
                        f"  {label}: upserted {n} "
                        f"range={bars[0]['datetime_end'] if bars else '-'} ~ "
                        f"{bars[-1]['datetime_end'] if bars else '-'}",
                        flush=True,
                    )
                except Exception as exc:  # noqa: BLE001
                    print(f"  {label}: FAIL {exc}", flush=True)
            print(f"  done in {time.time() - t0:.1f}s", flush=True)
    finally:
        conn.close()
        api.close()


def sync_aggregate_htf(symbols: list[str] | None = None) -> None:
    """从已入库日线聚合月/季/年。"""
    conn = connect()
    ensure_table(conn)
    try:
        syms = symbols or list_symbols(conn, DURATION_1D)
        print(f"\n=== aggregate mon/q/y from daily symbols={len(syms)} ===", flush=True)
        for i, symbol in enumerate(syms, 1):
            daily = load_bars(conn, symbol, DURATION_1D)
            if not daily:
                print(f"[{i}/{len(syms)}] {symbol}: no daily, skip", flush=True)
                continue
            n_m = upsert_bars(conn, symbol, DURATION_1MON, aggregate_monthly_from_daily(daily))
            n_q = upsert_bars(conn, symbol, DURATION_1Q, aggregate_quarterly_from_daily(daily))
            n_y = upsert_bars(conn, symbol, DURATION_1Y, aggregate_yearly_from_daily(daily))
            print(f"[{i}/{len(syms)}] {symbol}: mon={n_m} q={n_q} y={n_y}", flush=True)
    finally:
        conn.close()


def sync_backfill_1m(
    symbols: list[str],
    start: date,
    end: date,
    settings,
    also_5m: bool = True,
) -> None:
    """按月回测补 1 分钟，可选聚合成 5 分钟。"""
    conn = connect()
    ensure_table(conn)
    months = month_ranges(start, end)
    try:
        total = len(symbols) * len(months)
        done = 0
        for si, symbol in enumerate(symbols, 1):
            print(f"\n[{si}/{len(symbols)}] BACKFILL 1m {symbol} months={len(months)}")
            for month_start, month_end in months:
                done += 1
                t0 = time.time()
                try:
                    bars = fetch_backtest_bars(
                        symbol,
                        DURATION_1M,
                        month_start,
                        month_end,
                        settings=settings,
                        data_length=LIVE_MAX_LENGTH,
                    )
                    n = upsert_bars(conn, symbol, DURATION_1M, bars)
                    msg = (
                        f"  {month_start}~{month_end}: 1m={n} "
                        f"({time.time() - t0:.1f}s) [{done}/{total}]"
                    )
                    if also_5m and bars:
                        bars_5m = aggregate_from_lower(bars, DURATION_5M)
                        n5 = upsert_bars(conn, symbol, DURATION_5M, bars_5m)
                        msg += f" 5m={n5}"
                    print(msg)
                except Exception as exc:  # noqa: BLE001
                    print(
                        f"  {month_start}~{month_end}: FAIL {exc} [{done}/{total}]"
                    )
    finally:
        conn.close()


def print_summary() -> None:
    conn = connect()
    ensure_table(conn)
    try:
        print("\n=== DB summary (kline_bars) ===")
        for dur, label in DURATION_LABELS.items():
            n = count_bars(conn, duration_sec=dur)
            print(f"  {label:>4}: {n}")
        print(f"  TOTAL: {count_bars(conn)}")
    finally:
        conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="同步全主力主连多周期K线")
    parser.add_argument(
        "--mode",
        choices=[
            "live",
            "download",
            "download-gap",
            "aggregate-htf",
            "backfill-1m",
            "all",
            "summary",
        ],
        default="live",
        help=(
            "live=中长周期; download=专业版下载; download-gap=补3/10/15分与3/4小时; "
            "aggregate-htf=日线聚月季年"
        ),
    )
    parser.add_argument("--years", type=float, default=2.0, help="回溯年数，默认 2")
    parser.add_argument("--symbols", default=None, help="逗号分隔主连，默认全部 CONT")
    parser.add_argument(
        "--durations",
        default=None,
        help="下载周期，逗号分隔，如 1m,3m,5m,10m,15m,30m,1h,2h,3h,4h",
    )
    parser.add_argument("--no-5m-agg", action="store_true", help="backfill 时不聚合 5m")
    args = parser.parse_args()

    if args.mode == "summary":
        print_summary()
        return

    if args.mode == "aggregate-htf":
        symbols = (
            [s.strip() for s in args.symbols.split(",") if s.strip()]
            if args.symbols
            else None
        )
        sync_aggregate_htf(symbols)
        print_summary()
        return

    end = date.today()
    start = end - timedelta(days=int(365 * args.years))
    settings = load_settings()

    if args.symbols:
        symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
    else:
        api = TqApi(auth=TqAuth(settings.auth.user, settings.auth.password))
        try:
            symbols = list_main_continuous(api)
        finally:
            api.close()

    print(
        f"mode={args.mode} symbols={len(symbols)} range={start} ~ {end}",
        flush=True,
    )
    print(
        "symbols sample:",
        ", ".join(symbols[:5]),
        ("..." if len(symbols) > 5 else ""),
        flush=True,
    )

    if args.mode in ("live", "all"):
        sync_live(symbols, start, end, settings)
    if args.mode == "download":
        durs = _parse_durations(args.durations, [DURATION_1M, DURATION_5M])
        print("durations:", ", ".join(DURATION_LABELS[d] for d in durs), flush=True)
        sync_download(symbols, start, end, settings, durations=durs)
    if args.mode == "download-gap":
        durs = _parse_durations(args.durations, DOWNLOAD_GAP_DURATIONS)
        print("gap durations:", ", ".join(DURATION_LABELS[d] for d in durs), flush=True)
        sync_download(symbols, start, end, settings, durations=durs)
        sync_aggregate_htf(symbols)
    if args.mode == "all":
        sync_download(
            symbols,
            start,
            end,
            settings,
            durations=_parse_durations(
                args.durations,
                [DURATION_1M, DURATION_3M, DURATION_5M, DURATION_10M, DURATION_15M],
            ),
        )
        sync_aggregate_htf(symbols)
    if args.mode == "backfill-1m":
        sync_backfill_1m(
            symbols,
            start,
            end,
            settings,
            also_5m=not args.no_5m_agg,
        )

    print_summary()


if __name__ == "__main__":
    main()
