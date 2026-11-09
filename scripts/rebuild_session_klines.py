"""用库内 1 分钟按国内交易时段重算 30 分 / 1 小时，写入 kline_bars。

默认 --dry-run：只统计，不删不写。确认后再加 --execute。

示例:
  python scripts/rebuild_session_klines.py --symbols KQ.m@SHFE.fu
  python scripts/rebuild_session_klines.py --symbols KQ.m@SHFE.fu --execute
  python scripts/rebuild_session_klines.py --all --tfs 30m,1h --day 2026-11-09
  python scripts/rebuild_session_klines.py --all --execute

说明:
  - 1h 标注: 10:00 / 11:15 / 14:15 / 15:00（及夜盘 22:00 / 23:00）
  - 30m 标注: 既有小节规则（含 10:45 等）
  - 写入时间为北京结束标注；执行时会先删除该品种该周期旧行再覆盖
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import date, datetime, time as dtime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from config.settings import load_settings  # noqa: E402
from core.cont_symbols import list_commodity_mains  # noqa: E402
from core.kline_store import connect, ensure_table  # noqa: E402
from core.session_rebuild import SessionTf, rebuild_symbol_session_klines  # noqa: E402
from utils.trading_session import (  # noqa: E402
    get_day_1h_schedule_text,
    get_day_30min_schedule_text,
)


def _parse_day(raw: str | None) -> date | None:
    if not raw:
        return None
    return date.fromisoformat(raw)


def main() -> None:
    parser = argparse.ArgumentParser(description="从 1m 重建交易时段对齐的 30m/1h")
    parser.add_argument("--symbols", default=None, help="逗号分隔主连")
    parser.add_argument("--all", action="store_true", help="全部商品主力主连")
    parser.add_argument(
        "--tfs",
        default="30m,1h",
        help="要重建的周期，默认 30m,1h",
    )
    parser.add_argument("--day", default=None, help="只重建某一自然日 YYYY-MM-DD")
    parser.add_argument("--start", default=None, help="北京时间起始 YYYY-MM-DD")
    parser.add_argument("--end", default=None, help="北京时间结束 YYYY-MM-DD")
    parser.add_argument(
        "--execute",
        action="store_true",
        help="真正删旧写新；默认 dry-run 只统计",
    )
    parser.add_argument(
        "--show-schedule",
        action="store_true",
        help="打印 30m/1h 小节切分说明后退出",
    )
    parser.add_argument("--no-night", action="store_true", help="不合成夜盘")
    args = parser.parse_args()

    if args.show_schedule:
        print(get_day_30min_schedule_text())
        print()
        print(get_day_1h_schedule_text())
        return

    tfs: list[SessionTf] = []
    for part in args.tfs.split(","):
        key = part.strip().lower()
        if key not in ("30m", "1h"):
            raise SystemExit(f"暂只支持 30m/1h，收到: {key}")
        tfs.append(key)  # type: ignore[arg-type]

    start_dt = None
    end_dt = None
    day = _parse_day(args.day)
    if day is not None:
        start_dt = datetime.combine(day, dtime(0, 0))
        end_dt = datetime.combine(day, dtime(23, 59, 59))
    else:
        if args.start:
            start_dt = datetime.combine(date.fromisoformat(args.start), dtime(0, 0))
        if args.end:
            end_dt = datetime.combine(date.fromisoformat(args.end), dtime(23, 59, 59))

    load_settings()  # 确保 .env 已加载
    if args.symbols:
        symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
    elif args.all:
        symbols = list_commodity_mains()
    else:
        raise SystemExit("请指定 --symbols 或 --all")

    mode = "EXECUTE" if args.execute else "DRY-RUN"
    print(
        f"mode={mode} symbols={len(symbols)} tfs={','.join(tfs)} "
        f"range={start_dt} ~ {end_dt}",
        flush=True,
    )
    if not args.execute:
        print("（未加 --execute，只统计不写库）", flush=True)

    conn = connect()
    ensure_table(conn)
    t0 = time.time()
    try:
        total_30 = total_1h = 0
        for i, symbol in enumerate(symbols, 1):
            st = time.time()
            stats = rebuild_symbol_session_klines(
                conn,
                symbol,
                tfs=tfs,
                start=start_dt,
                end=end_dt,
                execute=args.execute,
                include_night=not args.no_night,
            )
            total_30 += stats.get("30m_bars", 0)
            total_1h += stats.get("1h_bars", 0)
            print(
                f"[{i}/{len(symbols)}] {symbol} "
                f"1m={stats.get('1m_used', 0)} "
                f"30m={stats.get('30m_bars', 0)} "
                f"1h={stats.get('1h_bars', 0)} "
                f"del30={stats.get('30m_deleted', '-')} "
                f"del1h={stats.get('1h_deleted', '-')} "
                f"up30={stats.get('30m_upserted', '-')} "
                f"up1h={stats.get('1h_upserted', '-')} "
                f"({time.time() - st:.1f}s)",
                flush=True,
            )
        print(
            f"done mode={mode} 30m_bars={total_30} 1h_bars={total_1h} "
            f"elapsed={time.time() - t0:.1f}s",
            flush=True,
        )
    finally:
        conn.close()


if __name__ == "__main__":
    main()
