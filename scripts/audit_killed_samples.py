"""图表审计：把被质量闸滤掉的信号渲染成 K 线图，供目视判断杀得对不对。"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.hs_chart import render_hs_chart
from core.hs_monitor import TF_CONFIGS, bars_to_strategy_df, make_strategy
from core.kline_store import connect, load_bars


def replay_detail(
    symbol: str,
    duration_sec: int,
    bars: list[dict],
    strict: bool,
    *,
    pivot_order: int | None = None,
    prominence: float | None = None,
    strict_tier: bool = False,
    factory=make_strategy,
):
    """逐根滚动回放信号。`strict` 是 `strict_shape` 那套质量闸，
    `strict_tier=True` 改走严格档入口 `on_bar_strict`（两档同刻触发，闸更多）。
    `factory` 换形态家族，比如三重顶底传 `make_triple_strategy`：它只走 `on_bar`。

    严格档这里强制打开 `strict_signals`：那是生产侧的总开关（`HS_STRICT_SIGNALS`），
    回测不该被它静默掉。
    """
    strat = factory(duration_sec)
    strat.strict_shape = strict
    if strict_tier:
        strat.strict_signals = True
    if pivot_order is not None:
        # 只用于回测对比：缩短拐点左右确认根数
        strat.pivot_order = pivot_order
    if prominence is not None:
        # 只用于回测对比：覆盖肩突出门槛（实盘 0.2×头高）
        strat.shoulder_prominence_ratio = prominence
    df_all = bars_to_strategy_df(bars)
    window = TF_CONFIGS[duration_sec].lookback_bars if duration_sec in TF_CONFIGS else 400
    order = strat.pivot_order
    min_len = strat.max_span_bars + order * 2 + 5
    pts = strat._pivot_series(df_all, is_high=True)
    # 预过滤跟着触发口径走：确认口径看 i-order 是否两侧极值，当根口径看 i 是否盖过前 order 根
    tail_mode = not strat.confirmed_shoulders
    if tail_mode:
        candidates = {
            i
            for i in range(order, len(pts))
            if float(pts.iloc[i]) > float(pts.iloc[i - order : i].max())
            or float(pts.iloc[i]) < float(pts.iloc[i - order : i].min())
        }
    else:
        candidates = {i for i, _ in strat._find_pivot_highs(pts)} | {
            i for i, _ in strat._find_pivot_lows(pts)
        }
    out = []
    scan = strat.on_bar_strict if strict_tier else strat.on_bar
    for i in range(min_len - 1, len(df_all)):
        if (i if tail_mode else i - order) not in candidates:
            continue
        win = df_all.iloc[max(0, i - window) : i + 1]
        sig = scan(win, symbol=symbol)
        if sig is not None:
            d = getattr(sig, "detail", None) or {}
            # 假反转只做后验标记（回测含右肩后的 K 线；实盘当根看不到未来，不能当发射闸）
            if "pattern_kind" not in d and d.get("head") is not None:
                if d.get("side") == "short":
                    d["is_fake_reverse"] = float(
                        df_all["high"].iloc[i:].max()
                    ) > float(d["head"])
                else:
                    d["is_fake_reverse"] = float(
                        df_all["low"].iloc[i:].min()
                    ) < float(d["head"])
            out.append(
                {
                    "when": str(df_all.iloc[i]["datetime"]),
                    "reason": sig.reason,
                    "detail": d,
                }
            )
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=15)
    ap.add_argument("--symbols", type=str, required=True)
    ap.add_argument("--tf", type=int, default=60)
    ap.add_argument("--max", type=int, default=6)
    ap.add_argument("--outdir", type=str, default="_audit")
    args = ap.parse_args()

    outdir = ROOT / args.outdir
    outdir.mkdir(parents=True, exist_ok=True)
    conn = connect()
    cutoff = datetime.now() - timedelta(days=args.days)
    rendered = 0
    meta: list[dict] = []
    for symbol in [s.strip() for s in args.symbols.split(",") if s.strip()]:
        bars = load_bars(conn, symbol, args.tf, start=cutoff)
        if len(bars) < 120:
            continue
        loose = replay_detail(symbol, args.tf, bars, False)
        strict = replay_detail(symbol, args.tf, bars, True)
        strict_when = {s["when"] for s in strict}
        killed = [s for s in loose if s["when"] not in strict_when]
        print(f"{symbol}: 改前 {len(loose)} 改后 {len(strict)} 被滤 {len(killed)}", flush=True)
        for s in killed:
            if rendered >= args.max:
                break
            png = render_hs_chart(symbol, args.tf, s["when"], s["detail"], conn)
            if not png:
                print(f"    跳过（无图） {s['when']}")
                continue
            rendered += 1
            path = outdir / f"killed_{rendered:02d}_{symbol.replace('@', '_').replace('.', '')}.png"
            path.write_bytes(png)
            meta.append({"file": path.name, "symbol": symbol, "when": s["when"], "reason": s["reason"]})
            print(f"    {path.name}  {s['when']}  {s['reason']}", flush=True)
        if rendered >= args.max:
            break
    (outdir / "meta_killed.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    conn.close()


if __name__ == "__main__":
    main()
