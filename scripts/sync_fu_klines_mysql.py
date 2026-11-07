"""同步燃料油 30 分钟 K 线到 MySQL（future_ai.session_kline_30m）"""

from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from config.settings import load_mysql_config, load_settings
from core.historical_session_klines import fetch_session_bars
from core.mysql_kline_store import connect, count_bars, ensure_table, upsert_bars

DEFAULT_SYMBOL = "KQ.m@SHFE.fu"


def main() -> None:
    parser = argparse.ArgumentParser(description="同步燃油 30 分钟 K 线到 MySQL")
    parser.add_argument("--symbol", default=DEFAULT_SYMBOL, help="TqSdk 主连/合约")
    parser.add_argument("--year", type=int, default=2026)
    parser.add_argument("--month", type=int, default=6)
    args = parser.parse_args()

    start = date(args.year, args.month, 1)
    if args.month == 12:
        end = date(args.year + 1, 1, 1)
    else:
        end = date(args.year, args.month + 1, 1)
    # 回测 end 为开区间，取月末
    from datetime import timedelta

    end = min(end - timedelta(days=1), date.today())

    print(f"拉取 {args.symbol} {start} ~ {end} 国内 30 分钟 K 线...")
    settings = load_settings()
    bars = fetch_session_bars(args.symbol, start, end, settings)
    month_bars = [
        b
        for b in bars
        if date.fromisoformat(b["datetime_end"][:10]).year == args.year
        and date.fromisoformat(b["datetime_end"][:10]).month == args.month
    ]
    print(f"合成 {len(month_bars)} 根 K 线，写入 MySQL...")

    cfg = load_mysql_config()
    conn = connect(cfg)
    try:
        ensure_table(conn)
        inserted = upsert_bars(conn, args.symbol, month_bars)
        total = count_bars(conn, args.symbol, args.year, args.month)
        print(
            f"完成: 数据库={cfg.database} 表=session_kline_30m "
            f"写入 {inserted} 条，{args.year}-{args.month:02d} 共 {total} 条"
        )
    finally:
        conn.close()


if __name__ == "__main__":
    main()
