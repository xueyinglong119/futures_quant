"""日终补连续主力（KQ.m@）入库。

工作日 15:30 起由 ingest 进程自动调用；也可手工：

    docker compose run --rm --no-deps -T futures-ingest \\
      python -u scripts/sync_cont_mains_daily.py
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.cont_sync import LOOKBACK_CALENDAR_DAYS, run_cont_mains_sync


def main() -> None:
    parser = argparse.ArgumentParser(description="补连续主力 KQ.m@ 最近若干日 K 线")
    parser.add_argument(
        "--lookback-days",
        type=int,
        default=LOOKBACK_CALENDAR_DAYS,
        help=f"回溯日历天数，默认 {LOOKBACK_CALENDAR_DAYS}",
    )
    args = parser.parse_args()
    run_cont_mains_sync(lookback_days=max(1, int(args.lookback_days)))


if __name__ == "__main__":
    main()
