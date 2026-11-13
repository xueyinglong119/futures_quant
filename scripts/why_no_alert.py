"""单品种头肩「为什么没告警」诊断：逐根回放 + 逐关打印被刷原因。

默认用实盘旧口径（scripts/hs_live_old.py，等同线上监控加载的版本）：
高低点拐点 / 肩离颈>=0.4h / 颈斜<=0.5h / 右肩后等 pivot_order 根触发 / 外侧极值主导性。
在服务器上跑（容器内有行情库），例如：

  docker compose run --rm --no-deps futures-backtest \
    python -u scripts/why_no_alert.py --symbol KQ.m@CZCE.FG --tf 300 --around "2026-11-12 11:30"

输出：目标根附近的拐点列表、每根的 trace 拒绝统计、每个被形态闸刷掉的候选三元组的
肩/头/颈数值与具体不过的闸。--also-new 再跑一遍仓库新版做对照。
"""

from __future__ import annotations

import argparse
import sys
from datetime import timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import pandas as pd

import hs_live_old
from core.hs_monitor import TF_CONFIGS, bars_to_strategy_df, make_strategy
from core.kline_store import DURATION_LABELS, connect, load_bars


def explain_shape(strat, price_l, price_h, price_r, idx_l, idx_h, idx_r,
                  neck_l, neck_r, *, bottom):
    """镜像旧版 _shape_ok 的 8 道闸，返回 (头高, 颈@头, 颈@左, 颈@右, 不过的闸列表)。"""
    n1, p1 = neck_l
    n2, p2 = neck_r
    neck_head = strat._line_at(n1, p1, n2, p2, idx_h)
    height = (neck_head - price_h) if bottom else (price_h - neck_head)
    neck_at_l = strat._line_at(n1, p1, n2, p2, idx_l)
    neck_at_r = strat._line_at(n1, p1, n2, p2, idx_r)
    fails = []
    if height <= 0 or price_h == 0:
        fails.append(f"头高{height:.2f}<=0")
        return height, neck_head, neck_at_l, neck_at_r, fails
    if height / abs(price_h) < strat.min_head_neck_ratio:
        fails.append(f"头颈比{height / abs(price_h):.4f}<{strat.min_head_neck_ratio}")
    if abs(p1 - p2) > 0.5 * height:
        fails.append(f"颈斜{abs(p1 - p2):.2f}>0.5h={0.5 * height:.2f}")
    if bottom:
        if price_l - price_h < 0.25 * height or price_r - price_h < 0.25 * height:
            fails.append(f"肩突出<0.25h L={price_l - price_h:.2f} R={price_r - price_h:.2f}")
    else:
        if price_h - price_l < 0.25 * height or price_h - price_r < 0.25 * height:
            fails.append(f"肩突出<0.25h L={price_h - price_l:.2f} R={price_h - price_r:.2f} 需>={0.25 * height:.2f}")
    if abs(price_l - price_r) > 0.5 * height:
        fails.append(f"肩价差{abs(price_l - price_r):.2f}>0.5h={0.5 * height:.2f}")
    if bottom:
        if neck_at_l - price_l < 0.4 * height or neck_at_r - price_r < 0.4 * height:
            fails.append(f"肩离颈<0.4h L={neck_at_l - price_l:.2f} R={neck_at_r - price_r:.2f} 需>={0.4 * height:.2f}")
    else:
        dl, dr = price_l - neck_at_l, price_r - neck_at_r
        if dl < 0.4 * height or dr < 0.4 * height:
            fails.append(f"肩离颈<0.4h L={dl:.2f} R={dr:.2f} 需>={0.4 * height:.2f}")
    if strat.strict_shape:
        if abs(price_l - price_r) > strat.max_shoulder_spread_ratio * abs(neck_head):
            fails.append(f"肩价差{abs(price_l - price_r):.2f}>1.5%颈={strat.max_shoulder_spread_ratio * abs(neck_head):.2f}")
        if abs(p1 - p2) > strat.max_neck_slope_ratio * abs(neck_head):
            fails.append(f"颈斜{abs(p1 - p2):.2f}>0.5%颈={strat.max_neck_slope_ratio * abs(neck_head):.2f}")
    return height, neck_head, neck_at_l, neck_at_r, fails


def install_shape_trace(strat, tag):
    orig = strat._shape_ok

    def traced(price_l, price_h, price_r, idx_l, idx_h, idx_r, neck_l, neck_r, *, bottom):
        res = orig(price_l, price_h, price_r, idx_l, idx_h, idx_r, neck_l, neck_r, bottom=bottom)
        if res is None:
            h, nh, nl, nr, fails = explain_shape(
                strat, price_l, price_h, price_r, idx_l, idx_h, idx_r, neck_l, neck_r, bottom=bottom
            )
            side = "底" if bottom else "顶"
            print(
                f"      [{tag}·形态拒·{side}] 左{price_l:.1f} 头{price_h:.1f} 右{price_r:.1f} "
                f"颈左{nl:.1f} 颈右{nr:.1f} 颈@头{nh:.1f} 头高{h:.2f} => {'; '.join(fails) or '?'}",
                flush=True,
            )
        return res

    strat._shape_ok = traced


def dump_pivots(strat, df, i, window):
    win = df.iloc[max(0, i - window): i + 1].reset_index(drop=True)
    ph = strat._find_pivot_highs(win.high)
    pl = strat._find_pivot_lows(win.low)
    print(f"    拐点(高) 末12: " + ", ".join(
        f"{win.iloc[j]['datetime']}={v:.1f}" for j, v in ph[-12:]), flush=True)
    print(f"    拐点(低) 末12: " + ", ".join(
        f"{win.iloc[j]['datetime']}={v:.1f}" for j, v in pl[-12:]), flush=True)


def run(symbol, tf, df, idx0, before, after, strat, tag, window):
    min_len = strat.max_span_bars + strat.pivot_order * 2 + 5
    lo = max(min_len - 1, idx0 - before)
    hi = min(len(df), idx0 + after + 1)
    print(f"\n== {tag} 回放 {symbol} {DURATION_LABELS.get(tf, tf)} 根#{lo}..{hi - 1} ==", flush=True)
    dump_pivots(strat, df, idx0, window)
    any_signal = False
    reason_total: dict[str, int] = {}
    for i in range(lo, hi):
        win = df.iloc[max(0, i - window): i + 1].reset_index(drop=True)
        strat._traded_patterns.clear()
        strat._active_trades.clear()
        strat.trace = True
        strat.last_trace = []
        sig = strat.on_bar(win, symbol=f"{symbol}|{tf}")
        counts: dict[str, int] = {}
        for _l, _h, _r, why in strat.last_trace:
            k = why.split()[0]
            counts[k] = counts.get(k, 0) + 1
            reason_total[k] = reason_total.get(k, 0) + 1
        bar = df.iloc[i]
        mark = " <== 目标根" if i == idx0 else ""
        sigtxt = f"告警! {sig.reason}" if sig is not None else "无"
        if sig is not None:
            any_signal = True
        print(f"  {bar['datetime']} 收{bar['close']:.1f}{mark} -> {sigtxt} | trace {counts}", flush=True)
    print(f"  小结: {'有告警' if any_signal else '全程无告警'} | 拒绝累计 {reason_total}", flush=True)
    return any_signal


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default="KQ.m@CZCE.FG")
    ap.add_argument("--tf", type=int, default=300)
    ap.add_argument("--around", default="2026-11-12 11:30")
    ap.add_argument("--pad-days", type=int, default=4)
    ap.add_argument("--before", type=int, default=12)
    ap.add_argument("--after", type=int, default=24)
    ap.add_argument("--also-new", action="store_true")
    args = ap.parse_args()

    around = pd.Timestamp(args.around)
    conn = connect()
    try:
        bars = load_bars(
            conn, args.symbol, args.tf,
            start=(around - timedelta(days=args.pad_days)).to_pydatetime(),
            end=(around + timedelta(days=1)).to_pydatetime(),
        )
    finally:
        conn.close()
    df = bars_to_strategy_df(bars)
    if df.empty:
        print(f"无 {args.symbol} {args.tf} 数据（{around} 前后 {args.pad_days} 天）", flush=True)
        return
    times = pd.to_datetime(df["datetime"])
    idx0 = int((times - around).abs().argmin())
    print(f"取到 {len(df)} 根，目标 {around} 落在根#{idx0} {df.iloc[idx0]['datetime']}", flush=True)

    cfg = TF_CONFIGS.get(args.tf, TF_CONFIGS[300])
    window = cfg.lookback_bars
    old = hs_live_old.HeadShouldersStrategy(
        pivot_order=cfg.pivot_order, shoulder_tolerance=0.03,
        min_span_bars=cfg.min_span_bars, max_span_bars=cfg.max_span_bars,
        max_right_above_left_ratio=0.015, min_head_neck_ratio=0.008,
        max_volume=1, duration_sec=args.tf,
    )
    install_shape_trace(old, "旧")
    run(args.symbol, args.tf, df, idx0, args.before, args.after, old, "旧口径(实盘)", window)

    if args.also_new:
        new = make_strategy(args.tf)
        run(args.symbol, args.tf, df, idx0, args.before, args.after, new, "新口径(仓库)", window)


if __name__ == "__main__":
    main()