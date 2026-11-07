"""查询当日 30 分钟 K 线

用法:
  python main_kline.py MA609
  python main_kline.py CZCE.MA609 --date 2026-06-13
  python main_kline.py fu2605 --json
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.market_data import get_today_klines


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="查询合约当日 30 分钟 K 线")
    parser.add_argument("symbol", help="合约号，如 MA609、CZCE.MA609、KQ.m@CZCE.MA")
    parser.add_argument(
        "--date",
        default=None,
        help="日期 YYYY-MM-DD，默认今天",
    )
    parser.add_argument(
        "--minutes",
        type=int,
        default=30,
        help="K 线周期（分钟），默认 30",
    )
    parser.add_argument("--json", action="store_true", help="以 JSON 输出")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    trade_date = date.fromisoformat(args.date) if args.date else None
    result = get_today_klines(
        args.symbol,
        trade_date=trade_date,
        duration_seconds=args.minutes * 60,
    )

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return

    print(f"合约: {result['symbol']}")
    print(f"日期: {result['trade_date']}  周期: {result['duration_minutes']} 分钟")
    print(f"K 线数量: {result['count']}")
    print("-" * 72)
    print(f"{'时间':<20} {'开':>8} {'高':>8} {'低':>8} {'收':>8} {'量':>8} {'持仓':>8}")
    for bar in result["klines"]:
        print(
            f"{bar['datetime']:<20} "
            f"{bar['open']:>8.0f} {bar['high']:>8.0f} {bar['low']:>8.0f} "
            f"{bar['close']:>8.0f} {bar['volume']:>8} {bar['open_oi']:>8}"
        )


if __name__ == "__main__":
    main()
