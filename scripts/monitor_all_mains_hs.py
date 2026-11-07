"""
全商品主力期货 · 多周期头肩形态监控告警

周期: 1m / 5m / 30m / 1h / 2h / 1d
数据源: MySQL kline_bars（历史已入库；盘中需配合增量同步才实时）

用法:
  # 单次扫描全部商品主力
  python scripts/monitor_all_mains_hs.py --once

  # 持续轮询（默认 60 秒一轮）
  python scripts/monitor_all_mains_hs.py --interval 60

  # 只盯常用自选
  python scripts/monitor_all_mains_hs.py --watchlist

  # 指定周期
  python scripts/monitor_all_mains_hs.py --durations 1m,5m,30m,1h,2h,1d
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from config.watchlist import WATCH_MAIN_SYMBOLS
from core.cont_symbols import list_commodity_mains
from core.hs_monitor import HS_MONITOR_DURATIONS, HsMonitorEngine
from core.kline_store import DURATION_LABELS, LABEL_TO_DURATION, count_bars, connect, ensure_table


def _parse_durations(raw: str | None) -> list[int]:
    if not raw:
        return list(HS_MONITOR_DURATIONS)
    out: list[int] = []
    for part in raw.split(","):
        key = part.strip().lower()
        if key not in LABEL_TO_DURATION:
            raise SystemExit(f"未知周期 {key}")
        dur = LABEL_TO_DURATION[key]
        if dur not in HS_MONITOR_DURATIONS:
            raise SystemExit(f"监控暂不支持 {key}，可选 1m/5m/30m/1h/2h/1d")
        out.append(dur)
    return out


def _resolve_symbols(args: argparse.Namespace) -> list[str]:
    if args.symbols:
        return [s.strip() for s in args.symbols.split(",") if s.strip()]
    if args.watchlist:
        return list(WATCH_MAIN_SYMBOLS)
    return list_commodity_mains()


def run_scan(
    engine: HsMonitorEngine,
    symbols: list[str],
    durations: list[int],
    *,
    alert_log: Path,
    push: bool,
    toast: bool,
    force: bool,
    emit: bool = True,
) -> tuple[int, int]:
    """返回 (检查数, 新告警数)。"""
    checked = 0
    alerts = 0
    conn = connect()
    ensure_table(conn)
    try:
        for symbol in symbols:
            for dur in durations:
                checked += 1
                n = engine.check_symbol_tf(
                    symbol,
                    dur,
                    alert_log=alert_log,
                    push=push,
                    toast=toast,
                    force=force,
                    emit=emit,
                    conn=conn,
                )
                alerts += n
    finally:
        conn.close()
    return checked, alerts


def main() -> None:
    parser = argparse.ArgumentParser(description="全商品主力多周期头肩监控")
    parser.add_argument("--once", action="store_true", help="只扫一轮")
    parser.add_argument("--interval", type=int, default=60, help="轮询间隔秒")
    parser.add_argument("--watchlist", action="store_true", help="只用 config/watchlist 常用品种")
    parser.add_argument("--symbols", default=None, help="逗号分隔主连")
    parser.add_argument(
        "--durations",
        default="1m,5m,30m,1h,2h,1d",
        help="监控周期",
    )
    parser.add_argument("--no-push", action="store_true")
    parser.add_argument("--toast", action="store_true", help="Windows 气泡（服务器可关）")
    args = parser.parse_args()

    durations = _parse_durations(args.durations)
    symbols = _resolve_symbols(args)
    alert_log = ROOT / "reports" / "all_mains_hs_alerts.txt"
    status_path = ROOT / "reports" / "all_mains_hs_status.txt"

    conn = connect()
    ensure_table(conn)
    try:
        ready = []
        for dur in durations:
            n = count_bars(conn, duration_sec=dur)
            ready.append(f"{DURATION_LABELS[dur]}={n}")
    finally:
        conn.close()

    print(
        f"监控启动 symbols={len(symbols)} "
        f"durations={[DURATION_LABELS[d] for d in durations]}",
        flush=True,
    )
    print(f"库内条数: {', '.join(ready)}", flush=True)
    print(f"告警日志: {alert_log}", flush=True)
    if count_missing_hint(symbols, durations):
        print(
            "提示: 部分周期/品种尚未入库，对应组合本轮会跳过；"
            "历史同步完成后自动生效。盘中实时需增量写入 MySQL。",
            flush=True,
        )

    engine = HsMonitorEngine()
    round_no = 0
    while True:
        round_no += 1
        t0 = time.time()
        # 第一轮只预热（标记末根K线），不把历史形态当新告警刷屏
        warmup = round_no == 1
        checked, alerts = run_scan(
            engine,
            symbols,
            durations,
            alert_log=alert_log,
            push=not args.no_push,
            toast=args.toast,
            force=warmup,
            emit=not warmup,
        )
        elapsed = time.time() - t0
        msg = (
            f"[{datetime.now().strftime('%H:%M:%S')}] round={round_no} "
            f"checked={checked} new_alerts={alerts} cost={elapsed:.1f}s"
        )
        print(msg, flush=True)
        status_path.parent.mkdir(parents=True, exist_ok=True)
        status_path.write_text(msg + "\n", encoding="utf-8")

        if args.once:
            break
        time.sleep(max(5, args.interval))


def count_missing_hint(symbols: list[str], durations: list[int]) -> bool:
    conn = connect()
    try:
        for sym in symbols[:3]:
            for dur in durations:
                if count_bars(conn, symbol=sym, duration_sec=dur) == 0:
                    return True
    finally:
        conn.close()
    return False


if __name__ == "__main__":
    main()
