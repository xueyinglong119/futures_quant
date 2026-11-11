"""兼容入口：默认走「只入库」模式。

告警请用 scripts/run_monitor_service.py（compose 服务 futures-monitor）。
一体模式：加 --with-monitor。
"""

from __future__ import annotations

import argparse
import sys
import time
import traceback
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.kline_audit import run_post_close_audit  # noqa: E402
from core.realtime_pipeline import TZ, RealtimePipeline, in_trading_window  # noqa: E402


def _run_close_audit() -> None:
    try:
        run_post_close_audit(fix=True, report_dir=ROOT / "reports")
    except Exception:
        traceback.print_exc()
        print("[audit] 收盘校验异常，已跳过", flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="实时入库（可选同进程告警）")
    parser.add_argument(
        "--with-monitor",
        action="store_true",
        help="同进程跑头肩告警（默认关闭，由独立 monitor 服务负责）",
    )
    args = parser.parse_args()
    alert_log = ROOT / "reports" / "all_mains_hs_alerts.txt"
    while True:
        now = datetime.now(TZ)
        if not in_trading_window(now):
            print(f"[pipeline] 休市 {now:%Y-%m-%d %H:%M:%S}，30 秒后再检查", flush=True)
            time.sleep(30)
            continue
        print(f"[pipeline] 进入交易时段 {now:%Y-%m-%d %H:%M:%S}", flush=True)
        try:
            RealtimePipeline(
                alert_log=alert_log if args.with_monitor else None,
                enable_monitor=args.with_monitor,
            ).run_session()
        except Exception:
            traceback.print_exc()
            print("[pipeline] 异常，15 秒后重连", flush=True)
            time.sleep(15)
        else:
            print("[pipeline] 本时段结束，断开行情", flush=True)
            print("[pipeline] 开始收盘入库校验", flush=True)
            _run_close_audit()


if __name__ == "__main__":
    main()
