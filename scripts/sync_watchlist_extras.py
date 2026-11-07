"""
常用品种增强数据同步（依赖天勤专业版 tq_dl）

1) 常用主连 1m 拉长到 5 年
2) 主力换月/标的历史 -> cont_roll_history
3) 近月实盘合约近 6 个月 1m
4) 燃油+乙二醇 Tick（默认近 10 天）-> tick_bars

用法:
  python scripts/sync_watchlist_extras.py
  python scripts/sync_watchlist_extras.py --skip-tick
  python scripts/sync_watchlist_extras.py --only rolls
"""

from __future__ import annotations

import argparse
import math
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd
from tqsdk import TqApi, TqAuth

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from config.settings import load_settings
from config.watchlist import (
    NEAR_MONTHS,
    TICK_LOOKBACK_DAYS,
    TICK_RESEARCH_CONTRACTS,
    WATCH_LONG_YEARS,
    WATCH_MAIN_SYMBOLS,
    WATCH_NEAR_CONTRACTS,
)
from core.extra_store import (
    ROLL_TABLE,
    TICK_TABLE,
    connect,
    count_table,
    ensure_extra_tables,
    upsert_rolls,
    upsert_ticks,
)
from core.kline_fetch import fetch_download_bars
from core.kline_store import DURATION_1M, ensure_table, upsert_bars


def _safe_float(v: object) -> float | None:
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _safe_int(v: object) -> int | None:
    f = _safe_float(v)
    return int(f) if f is not None else None


def sync_long_1m(api: TqApi, conn, start: date, end: date, tmp_dir: Path) -> None:
    symbols = WATCH_MAIN_SYMBOLS
    print(f"\n=== 常用主连 1m 加长 {start}~{end} symbols={len(symbols)} ===", flush=True)
    for i, symbol in enumerate(symbols, 1):
        t0 = time.time()
        print(f"[{i}/{len(symbols)}] {symbol} downloading 1m...", flush=True)
        try:
            bars = fetch_download_bars(api, symbol, DURATION_1M, start, end, tmp_dir)
            n = upsert_bars(conn, symbol, DURATION_1M, bars)
            print(
                f"  upserted {n} ({time.time() - t0:.1f}s) "
                f"{bars[0]['datetime_end'] if bars else '-'} ~ "
                f"{bars[-1]['datetime_end'] if bars else '-'}",
                flush=True,
            )
        except Exception as exc:  # noqa: BLE001
            print(f"  FAIL {exc}", flush=True)


def sync_rolls(api: TqApi, conn) -> None:
    symbols = WATCH_MAIN_SYMBOLS
    print(f"\n=== 主力换月历史 symbols={len(symbols)} n=1200 ===", flush=True)
    # 分批，避免一次列太多
    batch = 10
    total = 0
    for i in range(0, len(symbols), batch):
        chunk = symbols[i : i + batch]
        df = api.query_his_cont_quotes(symbol=chunk, n=1200)
        rows: list[dict] = []
        for _, row in df.iterrows():
            trade_date = pd.to_datetime(row["date"]).date()
            for sym in chunk:
                underlying = str(row.get(sym, "") or "").strip()
                if not underlying:
                    continue
                rows.append(
                    {
                        "main_symbol": sym,
                        "trade_date": trade_date,
                        "underlying": underlying,
                    }
                )
        n = upsert_rolls(conn, rows)
        total += n
        print(f"  batch {i // batch + 1}: wrote {n}", flush=True)
    print(f"  rolls total upserted≈{total}, table={count_table(conn, ROLL_TABLE)}", flush=True)


def sync_near_1m(api: TqApi, conn, start: date, end: date, tmp_dir: Path) -> None:
    symbols = WATCH_NEAR_CONTRACTS
    print(f"\n=== 近月合约 1m {start}~{end} symbols={len(symbols)} ===", flush=True)
    for i, symbol in enumerate(symbols, 1):
        t0 = time.time()
        print(f"[{i}/{len(symbols)}] {symbol} downloading 1m...", flush=True)
        try:
            bars = fetch_download_bars(api, symbol, DURATION_1M, start, end, tmp_dir)
            n = upsert_bars(conn, symbol, DURATION_1M, bars)
            print(f"  upserted {n} ({time.time() - t0:.1f}s)", flush=True)
        except Exception as exc:  # noqa: BLE001
            print(f"  FAIL {exc}", flush=True)


def _parse_tick_csv(csv_path: Path, symbol: str) -> list[dict]:
    if not csv_path.exists() or csv_path.stat().st_size == 0:
        return []
    df = pd.read_csv(csv_path)
    if df.empty:
        return []
    prefix = symbol
    cols = {
        "last_price": f"{prefix}.last_price",
        "highest": f"{prefix}.highest",
        "lowest": f"{prefix}.lowest",
        "average": f"{prefix}.average",
        "volume": f"{prefix}.volume",
        "amount": f"{prefix}.amount",
        "open_interest": f"{prefix}.open_interest",
        "bid_price1": f"{prefix}.bid_price1",
        "ask_price1": f"{prefix}.ask_price1",
        "bid_volume1": f"{prefix}.bid_volume1",
        "ask_volume1": f"{prefix}.ask_volume1",
    }
    # 兼容列名截断/缺失
    for key, col in list(cols.items()):
        if col not in df.columns:
            found = [c for c in df.columns if c.endswith("." + key)]
            cols[key] = found[0] if found else None

    rows: list[dict] = []
    dts = pd.to_datetime(df["datetime"])
    for i in range(len(df)):
        last_price = _safe_float(df.iloc[i][cols["last_price"]]) if cols["last_price"] else None
        if last_price is None:
            continue
        rows.append(
            {
                "symbol": symbol,
                "datetime_end": dts.iloc[i].to_pydatetime(),
                "last_price": last_price,
                "highest": _safe_float(df.iloc[i][cols["highest"]]) if cols["highest"] else None,
                "lowest": _safe_float(df.iloc[i][cols["lowest"]]) if cols["lowest"] else None,
                "average": _safe_float(df.iloc[i][cols["average"]]) if cols["average"] else None,
                "volume": (_safe_int(df.iloc[i][cols["volume"]]) or 0)
                if cols["volume"]
                else 0,
                "amount": _safe_float(df.iloc[i][cols["amount"]]) if cols["amount"] else None,
                "open_interest": _safe_int(df.iloc[i][cols["open_interest"]])
                if cols["open_interest"]
                else None,
                "bid_price1": _safe_float(df.iloc[i][cols["bid_price1"]])
                if cols["bid_price1"]
                else None,
                "ask_price1": _safe_float(df.iloc[i][cols["ask_price1"]])
                if cols["ask_price1"]
                else None,
                "bid_volume1": _safe_int(df.iloc[i][cols["bid_volume1"]])
                if cols["bid_volume1"]
                else None,
                "ask_volume1": _safe_int(df.iloc[i][cols["ask_volume1"]])
                if cols["ask_volume1"]
                else None,
            }
        )
    return rows


def sync_ticks(api: TqApi, conn, start: datetime, end: datetime, tmp_dir: Path) -> None:
    from tqsdk.tools import DataDownloader

    symbols = TICK_RESEARCH_CONTRACTS
    print(f"\n=== Tick 研究 {start.date()}~{end.date()} symbols={symbols} ===", flush=True)
    for i, symbol in enumerate(symbols, 1):
        t0 = time.time()
        csv_path = tmp_dir / f"tick_{symbol.replace('.', '_')}.csv"
        print(f"[{i}/{len(symbols)}] {symbol} downloading tick...", flush=True)
        try:
            task = DataDownloader(
                api,
                symbol_list=symbol,
                dur_sec=0,
                start_dt=start,
                end_dt=end,
                csv_file_name=str(csv_path),
            )
            while not task.is_finished():
                api.wait_update()
            rows = _parse_tick_csv(csv_path, symbol)
            n = upsert_ticks(conn, rows)
            print(
                f"  upserted {n} ticks ({time.time() - t0:.1f}s) "
                f"table_rows={count_table(conn, TICK_TABLE, symbol)}",
                flush=True,
            )
        except Exception as exc:  # noqa: BLE001
            print(f"  FAIL {exc}", flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="常用品种增强数据同步")
    parser.add_argument(
        "--only",
        choices=["all", "long1m", "rolls", "near", "tick"],
        default="all",
    )
    parser.add_argument("--skip-tick", action="store_true")
    parser.add_argument("--tick-days", type=int, default=TICK_LOOKBACK_DAYS)
    parser.add_argument("--long-years", type=float, default=WATCH_LONG_YEARS)
    parser.add_argument("--near-months", type=int, default=NEAR_MONTHS)
    args = parser.parse_args()

    settings = load_settings()
    end = date.today()
    long_start = end - timedelta(days=int(365 * args.long_years))
    near_start = end - timedelta(days=int(30 * args.near_months))
    tick_end = datetime.now()
    tick_start = tick_end - timedelta(days=args.tick_days)

    tmp_dir = ROOT / "data" / "kline_dl_tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)

    api = TqApi(auth=TqAuth(settings.auth.user, settings.auth.password))
    conn = connect()
    ensure_table(conn)
    ensure_extra_tables(conn)
    try:
        if not api._auth._has_feature("tq_dl"):
            raise RuntimeError("需要天勤专业版试用/正式版（tq_dl）")

        do_all = args.only == "all"
        if do_all or args.only == "long1m":
            sync_long_1m(api, conn, long_start, end, tmp_dir)
        if do_all or args.only == "rolls":
            sync_rolls(api, conn)
        if do_all or args.only == "near":
            sync_near_1m(api, conn, near_start, end, tmp_dir)
        if (do_all or args.only == "tick") and not args.skip_tick:
            sync_ticks(api, conn, tick_start, tick_end, tmp_dir)

        print("\n=== extras done ===", flush=True)
        print(f"rolls={count_table(conn, ROLL_TABLE)} ticks={count_table(conn, TICK_TABLE)}", flush=True)
    finally:
        conn.close()
        api.close()


if __name__ == "__main__":
    main()
