"""行情入库服务入口（独立进程）。

交易时段：订阅主力 K 线 → 收盘写入 MySQL → 会话周期合成。
不做头肩告警（由 run_monitor_service 负责）。
时段正常结束后做入库校验。休市睡眠，进程退出由容器拉起。
"""

from __future__ import annotations

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
    while True:
        now = datetime.now(TZ)
        if not in_trading_window(now):
            print(f"[ingest] 休市 {now:%Y-%m-%d %H:%M:%S}，30 秒后再检查", flush=True)
            time.sleep(30)
            continue
        print(f"[ingest] 进入交易时段 {now:%Y-%m-%d %H:%M:%S}", flush=True)
        try:
            RealtimePipeline(enable_monitor=False).run_session()
        except Exception:
            traceback.print_exc()
            print("[ingest] 异常，15 秒后重连", flush=True)
            time.sleep(15)
        else:
            print("[ingest] 本时段结束，断开行情", flush=True)
            print("[ingest] 开始收盘入库校验", flush=True)
            _run_close_audit()


if __name__ == "__main__":
    main()
