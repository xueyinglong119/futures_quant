"""
从 MySQL kline_bars 计算 MA / BOLL / EXPMA / 神奇九转

示例:
  python scripts/calc_indicators_demo.py --symbol KQ.m@DCE.eg --duration 1d
  python scripts/calc_indicators_demo.py --symbol KQ.m@SHFE.fu --duration 30m --tail 15
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.indicators import compute_overlay_indicators, latest_indicator_snapshot
from core.kline_store import LABEL_TO_DURATION, connect, ensure_table, load_bars


def main() -> None:
    parser = argparse.ArgumentParser(description="计算主图指标")
    parser.add_argument("--symbol", default="KQ.m@DCE.eg")
    parser.add_argument(
        "--duration",
        default="1d",
        help="周期标签: 1m/3m/5m/10m/15m/30m/1h/2h/3h/4h/1d/1w/1mon",
    )
    parser.add_argument("--tail", type=int, default=10, help="打印最近 N 根")
    args = parser.parse_args()

    label = args.duration.strip().lower()
    if label not in LABEL_TO_DURATION:
        raise SystemExit(f"未知周期 {label}")
    dur = LABEL_TO_DURATION[label]

    conn = connect()
    ensure_table(conn)
    try:
        bars = load_bars(conn, args.symbol, dur)
    finally:
        conn.close()

    if not bars:
        raise SystemExit(f"库中无数据: {args.symbol} {label}")

    import pandas as pd

    df = pd.DataFrame(bars)
    enriched = compute_overlay_indicators(df)
    snap = latest_indicator_snapshot(enriched)

    print(f"symbol={args.symbol} duration={label} bars={len(enriched)}")
    print("latest:", json.dumps(snap, ensure_ascii=False, indent=2))

    cols = [
        "datetime_end",
        "close",
        "ma5",
        "ma10",
        "ma20",
        "boll_mid",
        "boll_upper",
        "boll_lower",
        "expma12",
        "td_sell",
        "td_buy",
    ]
    cols = [c for c in cols if c in enriched.columns]
    print("\nrecent:")
    print(enriched[cols].tail(args.tail).to_string(index=False))

    nines = enriched[enriched["td_sell_nine"] | enriched["td_buy_nine"]].tail(5)
    if len(nines):
        print("\nrecent magic-nine signals:")
        print(
            nines[["datetime_end", "close", "td_sell", "td_buy", "td_sell_nine", "td_buy_nine"]]
            .to_string(index=False)
        )


if __name__ == "__main__":
    main()
