"""头肩校验规则历史回归：同一段历史，strict_shape 开/关各回放一遍对比告警数。"""

from __future__ import annotations

import argparse
import sys
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.hs_monitor import TF_CONFIGS, bars_to_strategy_df, make_strategy
from core.hs_monitor import HS_MONITOR_DURATIONS
from core.kline_store import DURATION_LABELS, TABLE_NAME, connect, load_bars

FOCUS = [
    ("KQ.m@DCE.j", 300),
    ("KQ.m@CZCE.AP", 300),
    ("KQ.m@DCE.bb", 7200),
]


def _pivot_at(series, j: int, order: int) -> bool:
    if j - order < 0 or j + order >= len(series):
        return False
    seg = series.iloc[j - order : j + order + 1]
    v = series.iloc[j]
    return v == seg.min() or v == seg.max()


def replay(symbol: str, duration_sec: int, bars: list[dict], strict: bool):
    strat = make_strategy(duration_sec)
    strat.strict_shape = strict
    df_all = bars_to_strategy_df(bars)
    window = TF_CONFIGS[duration_sec].lookback_bars if duration_sec in TF_CONFIGS else 400
    min_len = strat.max_span_bars + strat.pivot_order * 2 + 5
    signals: list[tuple[str, str]] = []
    for i in range(min_len - 1, len(df_all)):
        # 触发根必须是拐点，否则 on_bar 必然空转
        j = i - strat.pivot_order
        if not _pivot_at(df_all.low, j, strat.pivot_order) and not _pivot_at(
            df_all.high, j, strat.pivot_order
        ):
            continue
        win = df_all.iloc[max(0, i - window) : i + 1]
        sig = strat.on_bar(win, symbol=symbol)
        if sig is not None:
            signals.append((str(df_all.iloc[i]["datetime"]), sig.reason))
    return signals


def _job(job: tuple[str, int, int]) -> tuple[int, str, int, int]:
    symbol, tf, days = job
    conn = connect()
    try:
        bars = load_bars(conn, symbol, tf, start=datetime.now() - timedelta(days=days))
    finally:
        conn.close()
    if len(bars) < 120:
        return tf, symbol, 0, 0
    return tf, symbol, len(replay(symbol, tf, bars, False)), len(replay(symbol, tf, bars, True))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--tfs", type=str, default="", help="逗号分隔周期秒，缺省全部监控周期")
    ap.add_argument("--symbols", type=str, default="", help="逗号分隔品种，缺省全主力")
    args = ap.parse_args()

    tfs = (
        [int(x) for x in args.tfs.split(",") if x.strip()]
        if args.tfs
        else list(HS_MONITOR_DURATIONS)
    )
    cutoff = datetime.now() - timedelta(days=args.days)
    conn = connect()
    if args.symbols:
        symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
    else:
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT DISTINCT symbol FROM {TABLE_NAME} "
                "WHERE duration_sec=60 AND datetime_end>=%s AND symbol LIKE 'KQ.m@%%'",
                (cutoff,),
            )
            symbols = [r[0] for r in cur.fetchall()]
    print(f"品种 {len(symbols)}，回溯 {args.days} 天，周期 {tfs}", flush=True)

    jobs = [(s, tf, args.days) for tf in tfs for s in symbols]
    agg: dict[int, list[int]] = {}
    killed: dict[int, list[tuple[str, int]]] = {}
    done = 0
    t_start = datetime.now()
    with ProcessPoolExecutor(max_workers=args.workers) as ex:
        for tf, symbol, n_loose, n_strict in ex.map(_job, jobs, chunksize=2):
            done += 1
            if done % 16 == 0:
                print(
                    f"进度 {done}/{len(jobs)} 已用 {(datetime.now() - t_start).total_seconds():.0f}s",
                    flush=True,
                )
            a = agg.setdefault(tf, [0, 0])
            a[0] += n_loose
            a[1] += n_strict
            if n_loose > n_strict:
                killed.setdefault(tf, []).append((symbol, n_loose - n_strict))
    for tf in tfs:
        loose_n, strict_n = agg.get(tf, [0, 0])
        label = DURATION_LABELS.get(tf, str(tf))
        print(f"{label}: 改前 {loose_n} -> 改后 {strict_n}，被滤品种 {len(killed.get(tf, []))} 个", flush=True)
        for symbol, n in killed.get(tf, [])[:12]:
            print(f"    - {symbol} 滤掉 {n}")

    print("\n== 定向三例（改前/改后信号） ==", flush=True)
    for symbol, tf in FOCUS:
        bars = load_bars(conn, symbol, tf, start=cutoff)
        for strict in (False, True):
            sigs = replay(symbol, tf, bars, strict)
            tag = "改前" if not strict else "改后"
            print(f"{symbol} {DURATION_LABELS[tf]} {tag} {len(sigs)} 条", flush=True)
            for when, reason in sigs[-4:]:
                print(f"    {when} {reason}")
    conn.close()


if __name__ == "__main__":
    main()
