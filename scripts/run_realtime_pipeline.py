"""开盘实时链路入口。

交易时段内：订阅全商品主力 K 线，收盘后写入 MySQL，立刻做头肩告警。
休市时睡眠，不占用行情连接。进程退出后由容器循环拉起。
"""

from __future__ import annotations

import sys
import time
import traceback
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.realtime_pipeline import TZ, RealtimePipeline, in_trading_window  # noqa: E402


def main() -> None:
    alert_log = ROOT / "reports" / "all_mains_hs_alerts.txt"
    while True:
        now = datetime.now(TZ)
        if not in_trading_window(now):
            print(f"[pipeline] 休市 {now:%Y-%m-%d %H:%M:%S}，30 秒后再检查", flush=True)
            time.sleep(30)
            continue
        print(f"[pipeline] 进入交易时段 {now:%Y-%m-%d %H:%M:%S}", flush=True)
        try:
            RealtimePipeline(alert_log=alert_log).run_session()
        except Exception:
            traceback.print_exc()
            print("[pipeline] 异常，15 秒后重连", flush=True)
            time.sleep(15)
        else:
            print("[pipeline] 本时段结束，断开行情", flush=True)


if __name__ == "__main__":
    main()
