"""监控告警服务入口（独立进程）。

只读 MySQL 水位发现新收盘 K，跑头肩告警与模拟盘。
不订天勤 K 线；改策略重启本服务即可，不影响 ingest 订阅。
进程退出后由容器循环拉起。
"""

from __future__ import annotations

import sys
import time
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.db_monitor import DbHsMonitor  # noqa: E402


def main() -> None:
    alert_log = ROOT / "reports" / "all_mains_hs_alerts.txt"
    while True:
        try:
            DbHsMonitor(alert_log=alert_log).run_forever()
        except Exception:
            traceback.print_exc()
            print("[monitor] 异常退出，15 秒后重启", flush=True)
            time.sleep(15)


if __name__ == "__main__":
    main()
