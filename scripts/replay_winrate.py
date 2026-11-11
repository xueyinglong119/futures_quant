"""新规则信号胜率回放：按策略自身止盈(0.8×swing)/止损(0.2×swing)模拟逐笔结果。"""

from __future__ import annotations

import argparse
import sys
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from audit_killed_samples import replay_detail
from core.hs_monitor import HS_MONITOR_DURATIONS
from core.kline_store import DURATION_LABELS, connect, load_bars

HORIZON = 120


def _job(j: tuple[str, int, int]) -> list[dict]:
    return _outcome(*j)


def _outcome(symbol: str, tf: int, days: int) -> list[dict]:
    conn = connect()
    try:
        bars = load_bars(conn, symbol, tf, start=datetime.now() - timedelta(days=days))
    finally:
        conn.close()
    if len(bars) < 120:
        return []
    from core.hs_monitor import bars_to_strategy_df

    df = bars_to_strategy_df(bars)
    sigs = replay_detail(symbol, tf, bars, True)
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
    ap.add_argument("--days", type=int, default=15)
    ap.add_argument("--symbols", type=str, required=True)
    ap.add_argument("--workers", type=int, default=8)
    args = ap.parse_args()
    symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
    jobs = [(s, tf, args.days) for tf in HS_MONITOR_DURATIONS for s in symbols]
    rows: list[dict] = []
    with ProcessPoolExecutor(max_workers=args.workers) as ex:
        for chunk in ex.map(_job, jobs, chunksize=2):
            rows.extend(chunk)
    wins = [r for r in rows if r["result"] == "win"]
    losses = [r for r in rows if r["result"] == "loss"]
    timeouts = [r for r in rows if r["result"] == "timeout"]
    decided = len(wins) + len(losses)
    print(f"信号 {len(rows)} 已决 {decided} 胜 {len(wins)} 负 {len(losses)} 超时 {len(timeouts)}")
    if decided:
        print(f"胜率 {len(wins) / decided:.1%}  期望(以 swing 计) {len(wins) * 0.8 / decided - len(losses) * 0.2 / decided:+.3f}")
    by_tf: dict[int, list[dict]] = {}
    for r in rows:
        by_tf.setdefault(r["tf"], []).append(r)
    for tf, rs in sorted(by_tf.items()):
        w = sum(1 for r in rs if r["result"] == "win")
        l = sum(1 for r in rs if r["result"] == "loss")
        t = sum(1 for r in rs if r["result"] == "timeout")
        print(f"  {DURATION_LABELS.get(tf, tf)}: 信号 {len(rs)} 胜 {w} 负 {l} 超时 {t}")


if __name__ == "__main__":
    main()
