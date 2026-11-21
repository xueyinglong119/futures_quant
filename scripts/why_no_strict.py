"""单根「为什么没进严格档」诊断：把最后一根 K 上每个候选三元组过不了的闸连同数值打出来。

与 `why_no_alert.py`（旧口径）互补：这里直接用线上 `make_strategy`，先跑常规档确认形态成立，
再跑 `on_bar_strict` 并把 `_shape_ok` / `_volumes_ok` 的输入数值全部打印，判「差多少」而不是
只看一个闸名。命中的形态缺哪一项，一眼能对到规则表。

服务器上（镜像里没现成脚本时先部署一次再 exec，免开新容器）：

  docker compose exec -T futures-monitor python -u scripts/why_no_strict.py \\
    --symbol KQ.m@INE.sc --tf 3600 --at "2026-11-20 23:00"
"""

from __future__ import annotations

import argparse
import sys
from datetime import timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import numpy as np
import pandas as pd

from core.hs_monitor import TF_CONFIGS, bars_to_strategy_df, make_strategy
from core.kline_store import DURATION_LABELS, connect, load_bars


def explain_shape(s, price_l, price_h, price_r, idx_l, idx_h, idx_r, neck_l, neck_r,
                  *, bottom, closes, double_head, strict):
    """镜像 `_shape_ok` 当前的每一道闸，返回 (不过的闸, 关键数值)。"""
    n1, p1 = neck_l
    n2, p2 = neck_r
    neck_head = s._line_at(n1, p1, n2, p2, idx_h)
    height = (neck_head - price_h) if bottom else (price_h - neck_head)
    fails: list[str] = []
    if height <= 0 or price_h == 0:
        return [f"头高{height:.2f}<=0"], ""
    if height < s.min_head_neck_points:
        fails.append(f"头高{height:.2f}<{s.min_head_neck_points}点")
    if height / abs(price_h) < s.min_head_neck_ratio:
        fails.append(f"头颈比{height / abs(price_h):.3%}<{s.min_head_neck_ratio:.2%}")
    if abs(p1 - p2) > 0.25 * height:
        fails.append(f"颈斜{abs(p1 - p2):.2f}>0.25h={0.25 * height:.2f}")
    prom = s.shoulder_prominence_ratio
    if bottom:
        if price_l - price_h < prom * height or price_r - price_h < prom * height:
            fails.append(
                f"肩突出L={price_l - price_h:.2f} R={price_r - price_h:.2f}"
                f"<{prom}h={prom * height:.2f}"
            )
    elif price_h - price_l < prom * height or price_h - price_r < prom * height:
        fails.append(
            f"肩突出L={price_h - price_l:.2f} R={price_h - price_r:.2f}"
            f"<{prom}h={prom * height:.2f}"
        )
    if abs(price_l - price_r) > 0.5 * height:
        fails.append(f"肩价差{abs(price_l - price_r):.2f}>0.5h={0.5 * height:.2f}")
    neck_at_l = s._line_at(n1, p1, n2, p2, idx_l)
    neck_at_r = s._line_at(n1, p1, n2, p2, idx_r)
    gap = s.shoulder_neck_bars if s.duration_sec == 60 else 1
    if n1 - idx_l < gap or idx_r - n2 < gap:
        fails.append(f"颈锚贴肩 n1-左={n1 - idx_l} 右-n2={idx_r - n2} 需>={gap}")
    margin = s.min_shoulder_neck_ratio * height
    if bottom:
        if neck_at_l - price_l < margin or neck_at_r - price_r < margin:
            fails.append(
                f"肩离颈L={neck_at_l - price_l:.2f} R={neck_at_r - price_r:.2f}"
                f"<{margin:.2f}"
            )
    elif price_l - neck_at_l < margin or price_r - neck_at_r < margin:
        fails.append(
            f"肩离颈L={price_l - neck_at_l:.2f} R={price_r - neck_at_r:.2f}"
            f"<{margin:.2f}"
        )
    if s.strict_shape:
        if strict:
            dev = abs(price_r - price_l) / abs(price_l) if price_l else float("inf")
            if dev > s.strict_dev_threshold:
                fails.append(f"肩差{dev:.3%}>{s.strict_dev_threshold:.1%}(严格)")
        elif abs(price_l - price_r) > s.max_shoulder_spread_ratio * abs(neck_head):
            fails.append(
                f"肩差{abs(price_l - price_r):.2f}>"
                f"{s.max_shoulder_spread_ratio:.1%}颈={s.max_shoulder_spread_ratio * abs(neck_head):.2f}"
            )
        d_l = idx_h - idx_l
        d_r = idx_r - idx_h
        if not double_head and max(d_l, d_r) > s.max_shoulder_head_span_ratio * min(d_l, d_r):
            fails.append(
                f"腿长比{max(d_l, d_r) / min(d_l, d_r):.2f}>"
                f"{s.max_shoulder_head_span_ratio}（{d_l}/{d_r}）"
            )
        if closes is not None:
            tol = 0.1 * height
            span = np.arange(n1, n2 + 1)
            if n2 == n1:
                lines = np.full(span.size, (p1 + p2) / 2)
            else:
                lines = p1 + (p2 - p1) * ((span - n1) / (n2 - n1))
            c = np.asarray(closes, dtype="float64")[n1 : n2 + 1]
            excess = float((c - lines).max()) if bottom else float((lines - c).max())
            if excess > tol:
                fails.append(f"穿颈{excess:.2f}>{tol:.2f}(0.1h)")
    note = (
        f"头高{height:.2f} 颈@{neck_head:.1f} 颈左{neck_at_l:.1f} 颈右{neck_at_r:.1f} "
        f"肩差{abs(price_r - price_l) / abs(price_l) if price_l else 0:.3%} "
        f"根数{idx_r - idx_l + 1} 腿{idx_h - idx_l}/{idx_r - idx_h}"
    )
    return fails, note


def instrument(strat, df_all):
    """包装 `_shape_ok` 与 `_volumes_ok`，把每个候选的实际数值打出来。"""
    orig_shape = strat._shape_ok
    orig_vol = strat._volumes_ok

    def shape(price_l, price_h, price_r, idx_l, idx_h, idx_r, neck_l, neck_r,
              *, bottom, closes=None, double_head=False, strict=False):
        res = orig_shape(price_l, price_h, price_r, idx_l, idx_h, idx_r, neck_l, neck_r,
                         bottom=bottom, closes=closes, double_head=double_head,
                         strict=strict)
        if res is None:
            fails, note = explain_shape(
                strat, price_l, price_h, price_r, idx_l, idx_h, idx_r, neck_l, neck_r,
                bottom=bottom, closes=closes, double_head=double_head, strict=strict,
            )
            side = "底" if bottom else "顶"
            print(
                f"  [形态闸·{side}] 左#{idx_l} 头#{idx_h} 右#{idx_r} "
                f"L{price_l:.1f} H{price_h:.1f} R{price_r:.1f} => "
                f"{'; '.join(fails) or '未镜像的闸'} ｜ {note}",
                flush=True,
            )
        return res

    def volumes(klines, reaction_pivots, idx_l, idx_h, idx_r, neck_l_idx, neck_r_idx, *, bottom):
        ok = orig_vol(klines, reaction_pivots, idx_l, idx_h, idx_r, neck_l_idx, neck_r_idx,
                      bottom=bottom)
        vols = np.asarray(klines["volume"], dtype="float64")
        start = idx_l - strat.max_span_bars
        prior = [p[0] for p in reaction_pivots if p[0] < idx_l]
        if prior:
            start = max(start, max(prior))
        v1 = strat._vol_avg(vols, start, idx_l)
        v2 = strat._vol_avg(vols, neck_l_idx, idx_h)
        v3 = strat._vol_avg(vols, neck_r_idx, idx_r)
        need = "v2<v1 且 v3<=v2" if bottom else "v2<=v1 且 v3<v2"
        print(
            f"  [量能闸·{'底' if bottom else '顶'}] 左#{idx_l} 头#{idx_h} 右#{idx_r} "
            f"段1[#{start}..#{idx_l}]{v1:.0f} 段2[#{neck_l_idx}..#{idx_h}]{v2:.0f} "
            f"段3[#{neck_r_idx}..#{idx_r}]{v3:.0f} 需{need} => "
            f"{'通过' if ok else '不过'}",
            flush=True,
        )
        return ok

    strat._shape_ok = shape
    strat._volumes_ok = volumes
    return df_all


def print_trace(df_all, trace):
    counts: dict[str, int] = {}
    for row in trace:
        k = row[3].split()[0]
        counts[k] = counts.get(k, 0) + 1
    print(f"  拒绝统计: {counts or '无候选'}", flush=True)
    for idx_l, idx_h, idx_r, why in trace:
        if why.startswith("selected"):
            continue
        def dt(i):
            return str(df_all.iloc[i]["datetime"]) if 0 <= i < len(df_all) else "?"

        print(f"    {dt(idx_l)} / {dt(idx_h)} / {dt(idx_r)} => {why}", flush=True)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default="KQ.m@INE.sc")
    ap.add_argument("--tf", type=int, default=3600)
    ap.add_argument("--at", default="2026-11-20 23:00")
    ap.add_argument("--pad-days", type=int, default=40)
    ap.add_argument("--max-trace", type=int, default=40)
    args = ap.parse_args()

    at = pd.Timestamp(args.at)
    conn = connect()
    try:
        bars = load_bars(
            conn,
            args.symbol,
            args.tf,
            start=(at - timedelta(days=args.pad_days)).to_pydatetime(),
            end=(at + timedelta(days=1)).to_pydatetime(),
        )
    finally:
        conn.close()
    df = bars_to_strategy_df(bars)
    if df.empty:
        print(f"无 {args.symbol} {args.tf} 数据", flush=True)
        return
    cfg = TF_CONFIGS.get(args.tf, TF_CONFIGS[300])
    times = pd.to_datetime(df["datetime"])
    idx0 = int((times - at).abs().argmin())
    df = df.iloc[: idx0 + 1].reset_index(drop=True)
    # 与线上 detect_one 同口径：只喂最后 lookback_bars 根
    if len(df) > cfg.lookback_bars:
        df = df.iloc[-cfg.lookback_bars :].reset_index(drop=True)
    min_len = cfg.max_span_bars + cfg.pivot_order * 2 + 5
    label = DURATION_LABELS.get(args.tf, str(args.tf))
    print(
        f"== {args.symbol} {label} 目标根#{len(df) - 1} {df.iloc[-1]['datetime']} "
        f"收{df.iloc[-1]['close']:.1f} 量{df.iloc[-1]['volume']}"
        f"（实取 {len(df)} 根，至少需 {min_len} 根）==",
        flush=True,
    )
    if len(df) < min_len:
        print(f"根数不足 {min_len}，_scan 直接返回，加大 --pad-days", flush=True)
        return

    normal = make_strategy(args.tf)
    normal.trace = True
    normal.last_trace = []
    sig = normal.on_bar(df, symbol=args.symbol)
    print(f"\n[常规档] {'成立: ' + sig.reason if sig is not None else '无信号'}", flush=True)
    print_trace(df, normal.last_trace[-args.max_trace :])

    strict = make_strategy(args.tf)
    strict.strict_signals = True
    strict.trace = True
    strict.last_trace = []
    instrument(strict, df)
    print("\n[严格档] 逐候选实算：", flush=True)
    s_sig = strict.on_bar_strict(df, symbol=args.symbol)
    if s_sig is not None:
        print(f"  => 严格档成立: {s_sig.reason}", flush=True)
    else:
        print("  => 严格档无信号", flush=True)
        print_trace(df, strict.last_trace[-args.max_trace :])
    print(f"\n阈值: 肩差≤{strict.strict_dev_threshold:.1%} 形态根数≥{strict.strict_min_form_bars} "
          f"量能闸={strict.strict_volume_gates} 腿长比≤{strict.max_shoulder_head_span_ratio} "
          f"肩突出≥{strict.shoulder_prominence_ratio}h 头高≥{strict.min_head_neck_points}点", flush=True)


if __name__ == "__main__":
    main()
