"""补 ag2702 单合约 1m K 线（11-07 ~ 11-12），用于验证白银三重顶。"""
from __future__ import annotations

import sys
from datetime import date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from config.settings import load_settings
from core.kline_fetch import fetch_backtest_bars
from core.kline_store import DURATION_1M, connect, ensure_table, upsert_bars

SYMBOL = "SHFE.ag2702"
START = date(2026, 11, 7)
END = date(2026, 11, 12)

settings = load_settings()
print(f"拉取 {SYMBOL} 1m 从 {START} 到 {END}...", flush=True)

bars = fetch_backtest_bars(SYMBOL, DURATION_1M, START, END, settings=settings)
print(f"拿到 {len(bars)} 根 1m", flush=True)

if bars:
    print(f"  范围: {bars[0]['datetime_end']} ~ {bars[-1]['datetime_end']}", flush=True)
    conn = connect()
    ensure_table(conn)
    try:
        n = upsert_bars(conn, SYMBOL, DURATION_1M, bars)
        print(f"写入 {n} 根到 MySQL", flush=True)
    finally:
        conn.close()

    # 验证
    conn = connect()
    try:
        from core.kline_store import load_bars
        raw = load_bars(conn, SYMBOL, DURATION_1M, start=datetime(2026, 11, 7), end=datetime(2026, 11, 13))
        print(f"库内 ag2702 1m: {len(raw)} 根 ({raw[0]['datetime_end'] if raw else '-'} ~ {raw[-1]['datetime_end'] if raw else '-'})", flush=True)
    finally:
        conn.close()
else:
    print("没拿到数据", flush=True)
