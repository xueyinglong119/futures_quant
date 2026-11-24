"""行情入库服务入口（独立进程）。

交易时段：订阅**持仓主力月约** K 线 → 收盘写入 MySQL → 会话周期合成。
不做头肩告警（由 run_monitor_service 负责）。
时段正常结束后做入库校验。休市睡眠，进程退出由容器拉起。

工作日 15:30～20:50：补写连续主力 KQ.m@（长序列对照，每天一次）。
"""

from __future__ import annotations

import sys
import time
import traceback
from datetime import date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.cont_sync import run_cont_mains_sync, should_run_cont_sync  # noqa: E402
from core.kline_audit import run_post_close_audit  # noqa: E402
from core.realtime_pipeline import TZ, RealtimePipeline, in_trading_window  # noqa: E402

_last_cont_sync_day: date | None = None


def _run_close_audit() -> None:
    try:
        run_post_close_audit(fix=True, report_dir=ROOT / "reports")
    except Exception:
        traceback.print_exc()
        print("[audit] 收盘校验异常，已跳过", flush=True)


def _maybe_sync_cont_mains() -> None:
    """日盘收完后补 KQ.m@；失败不阻断主循环。"""
    global _last_cont_sync_day
    now = datetime.now(TZ)
    if not should_run_cont_sync(now, last_done=_last_cont_sync_day):
        return
    print(
        f"[ingest] {now:%Y-%m-%d %H:%M:%S} 开始日终补连续主力 KQ.m@",
        flush=True,
    )
    try:
        run_cont_mains_sync()
        _last_cont_sync_day = now.date()
    except Exception:
        traceback.print_exc()
        print("[ingest] 连续主力补库异常，已跳过（明天/下次窗口再试）", flush=True)


def main() -> None:
    while True:
        now = datetime.now(TZ)
        if not in_trading_window(now):
            _maybe_sync_cont_mains()
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
            # 下午时段结束后若已过 15:30，立刻补 CONT（不必干等到下一次 30s 轮询）
            _maybe_sync_cont_mains()


if __name__ == "__main__":
    main()
