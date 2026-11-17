"""严格档历史回归：逐根滚动跑同一段历史，统计严格档出数、其中与常规档撞同形态的有多少。

两档同刻触发，同形态时常规档那条由 `tier_dets` 直接不发，所以「同形态 K」就是**被让位**的
常规档条数，「独占 M−K」是只有严格档才告出来的新形态。常规档口径不变（同一实例分别跑两档，
两套 `_traded_*` 互不影响），这里的「常规档 N」可以直接和改前的告警数对比。
`HS_STRICT_SIGNALS=0` 会让严格档整体静默，用来出对照。
"""

from __future__ import annotations

import argparse
import os
import sys
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.hs_monitor import (  # noqa: E402
    HS_MONITOR_DURATIONS,
    TF_CONFIGS,
    bars_to_strategy_df,
    make_strategy,
)
from core.kline_store import DURATION_LABELS, TABLE_NAME, connect, load_bars  # noqa: E402

FOCUS = [
    ("KQ.m@DCE.jd", 1800),
    ("KQ.m@CZCE.PF", 1800),
    ("KQ.m@DCE.eg", 300),
]


def _min_bars(tf: int) -> int:
    cfg = TF_CONFIGS[tf]
    return cfg.max_span_bars + cfg.pivot_order * 2 + 5


def replay(symbol: str, duration_sec: int, bars: list[dict]):
    """返回 (常规档条数, 严格档条数, 严格档里能在常规档找到同形态的条数)。"""
    strat = make_strategy(duration_sec)
    df = bars_to_strategy_df(bars)
    win = TF_CONFIGS[duration_sec].lookback_bars
    min_len = _min_bars(duration_sec)
    normal_n = 0
    normal_pat: set[str] = set()
    strict: list[tuple[str, str, str]] = []
    for i in range(min_len - 1, len(df)):
        win_df = df.iloc[max(0, i - win) : i + 1]
        sig = strat.on_bar(win_df, symbol=symbol)
        if sig is not None:
            normal_n += 1
            normal_pat.add(str((sig.detail or {}).get("pattern_id")))
        s = strat.on_bar_strict(win_df, symbol=symbol)
        if s is not None:
            strict.append(
                (
                    str(df.iloc[i]["datetime"]),
                    str(s.detail.get("pattern_id")),
                    s.reason,
                )
            )
    hit = sum(1 for _, p, _ in strict if p in normal_pat)
    return normal_n, strict, hit


def _job(job: tuple[str, int, int]) -> tuple[int, str, int, int, int, int]:
    symbol, tf, days = job
    conn = connect()
    try:
        bars = load_bars(conn, symbol, tf, start=datetime.now() - timedelta(days=days))
    finally:
        conn.close()
    # 加载窗不够就一根都出不了，必须报出来：否则 0 会被读成「这个周期没信号」
    need = _min_bars(tf)
    if len(bars) < need:
        return tf, symbol, 0, 0, 0, len(bars)
    normal_n, strict, hit = replay(symbol, tf, bars)
    return tf, symbol, normal_n, len(strict), hit, 0


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=60)
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--tfs", type=str, default="", help="逗号分隔周期秒，缺省全部监控周期")
    ap.add_argument("--symbols", type=str, default="", help="逗号分隔品种，缺省全主力")
    ap.add_argument("--samples", type=int, default=6, help="每周期打印的严格档样例条数")
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
    print(
        f"品种 {len(symbols)}，回溯 {args.days} 天，周期 {tfs}，"
        f"HS_STRICT_SIGNALS={os.environ.get('HS_STRICT_SIGNALS', '1')}",
        flush=True,
    )

    jobs = [(s, tf, args.days) for tf in tfs for s in symbols]
    agg: dict[int, list[int]] = {}
    skipped: dict[int, list[tuple[str, int]]] = {}
    done = 0
    t_start = datetime.now()
    with ProcessPoolExecutor(max_workers=args.workers) as ex:
        for tf, symbol, n_normal, n_strict, n_hit, n_bars in ex.map(_job, jobs, chunksize=2):
            done += 1
            if done % 40 == 0:
                el = (datetime.now() - t_start).total_seconds()
                print(f"进度 {done}/{len(jobs)} 已用 {el:.0f}s", flush=True)
            a = agg.setdefault(tf, [0, 0, 0])
            a[0] += n_normal
            a[1] += n_strict
            a[2] += n_hit
            if n_bars:
                skipped.setdefault(tf, []).append((symbol, n_bars))

    print("\n== 逐周期汇总 ==", flush=True)
    tot = [0, 0, 0]
    for tf in tfs:
        n_normal, n_strict, n_hit = agg.get(tf, [0, 0, 0])
        for k in range(3):
            tot[k] += agg.get(tf, [0, 0, 0])[k]
        label = DURATION_LABELS.get(tf, str(tf))
        rate = f"{n_strict / n_normal:.1%}" if n_normal else "—"
        skip = skipped.get(tf, [])
        hint = (
            f"，另有 {len(skip)} 个品种根数不足 {_min_bars(tf)} 根被跳过"
            f"（如 {skip[0][0]} 只有 {skip[0][1]} 根）"
            if skip
            else ""
        )
        print(
            f"{label}: 常规档 {n_normal} 条 / 严格档 {n_strict} 条（占 {rate}，"
            f"同形态让位 {n_hit}，严格档独占 {n_strict - n_hit}）{hint}",
            flush=True,
        )
    print(
        f"\n合计 常规档 {tot[0]} / 严格档 {tot[1]} / 同形态让位 {tot[2]} / 独占 {tot[1] - tot[2]}",
        flush=True,
    )

    print("\n== 定向样例 ==", flush=True)
    for symbol, tf in FOCUS:
        if tf not in tfs:
            continue
        bars = load_bars(conn, symbol, tf, start=cutoff)
        if len(bars) < _min_bars(tf):
            print(
                f"{symbol} {DURATION_LABELS.get(tf, str(tf))} 只有 {len(bars)} 根，"
                f"低于 {_min_bars(tf)} 根，跳过（加大 --days）",
                flush=True,
            )
            continue
        _n, strict, _hit = replay(symbol, tf, bars)
        label = DURATION_LABELS.get(tf, str(tf))
        print(f"{symbol} {label} 严格档 {len(strict)} 条", flush=True)
        for when, _pat, reason in strict[-args.samples :]:
            print(f"    {when} {reason[:72]}", flush=True)
    conn.close()


if __name__ == "__main__":
    main()
