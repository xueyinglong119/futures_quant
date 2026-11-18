"""新规则信号胜率回放：按策略自身止盈(0.8×swing)/止损(0.2×swing)模拟逐笔结果。

两档（常规档 / 严格档）在同一次取数上各跑一遍，逐周期并排出胜率——严格档不是常规档的子集，
只有同窗口同品种对比才看得出它新增那批形态的胜率。平衡胜率 = 0.2 / (0.8 + 0.2) = 20%。
"""

from __future__ import annotations

import argparse
import sys
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from audit_killed_samples import replay_detail  # noqa: E402
from core.hs_monitor import (  # noqa: E402
    HS_MONITOR_DURATIONS,
    TF_CONFIGS,
    bars_to_strategy_df,
)
from core.kline_store import (  # noqa: E402
    DURATION_LABELS,
    TABLE_NAME,
    connect,
    load_bars,
)

HORIZON = 120
TIERS = ("常规档", "严格档")
BALANCE = 0.2 / (0.8 + 0.2)


def _job(j: tuple[str, int, int]) -> tuple[list[dict], int, str, int]:
    symbol, tf, days = j
    rows, n_bars = _outcome(symbol, tf, days)
    return rows, n_bars, symbol, tf


def _min_bars(tf: int) -> int:
    cfg = TF_CONFIGS[tf]
    return cfg.max_span_bars + cfg.pivot_order * 2 + 5


def _outcome(symbol: str, tf: int, days: int) -> tuple[list[dict], int]:
    """返回 (逐笔结果, 根数不足的标记)。标记 = 加载到的根数，正常跑完是 0。"""
    conn = connect()
    try:
        bars = load_bars(conn, symbol, tf, start=datetime.now() - timedelta(days=days))
    finally:
        conn.close()
    if len(bars) < _min_bars(tf):
        return [], len(bars)
    df = bars_to_strategy_df(bars)
    rows: list[dict] = []
    for tier, strict_tier in zip(TIERS, (False, True)):
        sigs = replay_detail(symbol, tf, bars, True, strict_tier=strict_tier)
        for r in simulate_outcomes(symbol, tf, df, sigs):
            r["tier"] = tier
            rows.append(r)
    return rows, 0


def summarize(rs: list[dict]) -> str:
    if not rs:
        return "无信号"
    w = sum(1 for r in rs if r["result"] == "win")
    l = sum(1 for r in rs if r["result"] == "loss")
    t = sum(1 for r in rs if r["result"] == "timeout")
    ns = sum(1 for r in rs if r["result"] == "no_swing")
    decided = w + l
    rate = f"{w / decided:.1%}" if decided else "—"
    exp = f"{(w * 0.8 - l * 0.2) / decided:+.3f}" if decided else "—"
    return (
        f"信号 {len(rs)} 已决 {decided} 胜 {w} 负 {l} 超时 {t} 无swing {ns}"
        f" → 胜率 {rate} 期望 {exp}×swing"
    )


def simulate_outcomes(symbol: str, tf: int, df, sigs: list[dict]) -> list[dict]:
    when_to_i = {str(df.iloc[i]["datetime"]): i for i in range(len(df))}
    out = []
    for s in sigs:
        i = when_to_i.get(s["when"])
        if i is None:
            continue
        d = s["detail"]
        swing = d.get("swing")
        entry = float(df.iloc[i]["close"])
        if not swing or swing <= 0 or entry <= 0:
            out.append({"symbol": symbol, "tf": tf, "when": s["when"], "result": "no_swing"})
            continue
        side = d.get("side")
        tp_d, sl_d = swing * 0.8, swing * 0.2
        if side == "short":
            tp, sl = entry - tp_d, entry + sl_d
        else:
            tp, sl = entry + tp_d, entry - sl_d
        result = "timeout"
        for j in range(i + 1, min(i + 1 + HORIZON, len(df))):
            b = df.iloc[j]
            o, h, l = float(b["open"]), float(b["high"]), float(b["low"])
            if side == "short":
                hit_sl = o >= sl if o >= sl else h >= sl
                hit_tp = o <= tp if o <= tp else l <= tp
                if o >= sl:
                    result = "loss"
                    break
                if o <= tp:
                    result = "win"
                    break
                if h >= sl:
                    result = "loss"
                    break
                if l <= tp:
                    result = "win"
                    break
            else:
                if o <= sl:
                    result = "loss"
                    break
                if o >= tp:
                    result = "win"
                    break
                if l <= sl:
                    result = "loss"
                    break
                if h >= tp:
                    result = "win"
                    break
        out.append({"symbol": symbol, "tf": tf, "when": s["when"], "result": result})
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=60)
    ap.add_argument("--symbols", type=str, default="", help="逗号分隔品种，缺省全主力")
    ap.add_argument("--tfs", type=str, default="", help="逗号分隔周期秒，缺省全部监控周期")
    ap.add_argument("--workers", type=int, default=8)
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
    conn.close()
    print(
        f"品种 {len(symbols)}，回溯 {args.days} 天，周期 {tfs}，两档各跑一遍"
        f"（平衡胜率 {BALANCE:.1%}）",
        flush=True,
    )

    jobs = [(s, tf, args.days) for tf in tfs for s in symbols]
    rows: list[dict] = []
    skipped: dict[int, list[tuple[str, int]]] = {}
    done = 0
    t_start = datetime.now()
    with ProcessPoolExecutor(max_workers=args.workers) as ex:
        for chunk, n_bars, symbol, tf in ex.map(_job, jobs, chunksize=2):
            done += 1
            if done % 40 == 0:
                el = (datetime.now() - t_start).total_seconds()
                print(f"进度 {done}/{len(jobs)} 已用 {el:.0f}s", flush=True)
            rows.extend(chunk)
            if n_bars:
                skipped.setdefault(tf, []).append((symbol, n_bars))

    print(f"\n== 逐周期并排（平衡胜率 {BALANCE:.1%}，低于它即负期望）==", flush=True)
    for tf in tfs:
        label = DURATION_LABELS.get(tf, str(tf))
        for tier in TIERS:
            rs = [r for r in rows if r["tf"] == tf and r["tier"] == tier]
            print(f"  {label} {tier}: {summarize(rs)}", flush=True)
        got = skipped.get(tf, [])
        if got:
            print(
                f"  {label}: 另有 {len(got)} 个品种根数不足 {_min_bars(tf)} 根被跳过"
                f"（如 {got[0][0]} 只有 {got[0][1]} 根）",
                flush=True,
            )

    print("\n== 合计 ==", flush=True)
    for tier in TIERS:
        print(f"  {tier}: {summarize([r for r in rows if r['tier'] == tier])}", flush=True)


if __name__ == "__main__":
    main()
