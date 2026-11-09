"""用库内 1 分钟按国内交易时段重算 30m/1h/2h/3h/4h，写入 kline_bars。

默认 --dry-run：只统计，不删不写。确认后再加 --execute。

示例:
  python scripts/rebuild_session_klines.py --symbols KQ.m@SHFE.fu
  python scripts/rebuild_session_klines.py --symbols KQ.m@SHFE.fu --execute
  python scripts/rebuild_session_klines.py --all --tfs 2h,3h,4h --execute
  python scripts/rebuild_session_klines.py --all --execute

说明:
  - 1h: 10:00 / 11:15 / 14:15 / 15:00（及夜盘 22:00 / 23:00）
  - 2h: 11:15 / 15:00（夜盘 23:00）
  - 3h: 14:15 / 15:00（夜盘 23:00）
  - 4h: 15:00（夜盘 23:00）
  - 30m: 既有小节规则（含 10:45 等）
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
from core.kline_store import (  # noqa: E402
    DURATION_1M,
    connect,
    ensure_table,
    list_symbols,
)
from core.session_rebuild import (  # noqa: E402
    ALL_SESSION_TFS,
    SessionTf,
    rebuild_symbol_session_klines,
)
from utils.trading_session import (  # noqa: E402
    get_day_1h_schedule_text,
    get_day_2h_schedule_text,
    get_day_3h_schedule_text,
    get_day_4h_schedule_text,
    get_day_30min_schedule_text,
)


def _parse_day(raw: str | None) -> date | None:
    if not raw:
        return None
    return date.fromisoformat(raw)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="从 1m 重建交易时段对齐的 30m/1h/2h/3h/4h"
    )
    parser.add_argument("--symbols", default=None, help="逗号分隔主连")
    parser.add_argument("--all", action="store_true", help="库内全部有 1m 的品种")
    parser.add_argument(
        "--tfs",
        default=",".join(ALL_SESSION_TFS),
        help=f"要重建的周期，默认 {','.join(ALL_SESSION_TFS)}",
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
        help="打印各周期小节切分说明后退出",
    )
    parser.add_argument("--no-night", action="store_true", help="不合成夜盘")
    args = parser.parse_args()

    if args.show_schedule:
        for fn in (
            get_day_30min_schedule_text,
            get_day_1h_schedule_text,
            get_day_2h_schedule_text,
            get_day_3h_schedule_text,
            get_day_4h_schedule_text,
        ):
            print(fn())
            print()
        return

    allowed = set(ALL_SESSION_TFS)
    tfs: list[SessionTf] = []
    for part in args.tfs.split(","):
        key = part.strip().lower()
        if key not in allowed:
            raise SystemExit(f"支持周期: {','.join(ALL_SESSION_TFS)}，收到: {key}")
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
    conn = connect()
    ensure_table(conn)
    if args.symbols:
        symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
    elif args.all:
        # 直接用库里已有 1m 的品种，避免再连天勤拉主连列表
        symbols = list_symbols(conn, DURATION_1M)
    else:
        conn.close()
        raise SystemExit("请指定 --symbols 或 --all")

    mode = "EXECUTE" if args.execute else "DRY-RUN"
    print(
        f"mode={mode} symbols={len(symbols)} tfs={','.join(tfs)} "
        f"range={start_dt} ~ {end_dt}",
        flush=True,
    )
    if not args.execute:
        print("（未加 --execute，只统计不写库）", flush=True)

    t0 = time.time()
    try:
        totals = {tf: 0 for tf in tfs}
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
            for tf in tfs:
                totals[tf] += stats.get(f"{tf}_bars", 0)
            detail = " ".join(
                f"{tf}={stats.get(f'{tf}_bars', 0)}"
                f"/del={stats.get(f'{tf}_deleted', '-')}"
                f"/up={stats.get(f'{tf}_upserted', '-')}"
                for tf in tfs
            )
            print(
                f"[{i}/{len(symbols)}] {symbol} "
                f"1m={stats.get('1m_used', 0)} {detail} "
                f"({time.time() - st:.1f}s)",
                flush=True,
            )
        print(
            f"done mode={mode} "
            + " ".join(f"{tf}_bars={n}" for tf, n in totals.items())
            + f" elapsed={time.time() - t0:.1f}s",
            flush=True,
        )
    finally:
        conn.close()


if __name__ == "__main__":
    main()
