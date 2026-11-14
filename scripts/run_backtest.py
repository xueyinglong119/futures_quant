"""容器化回测一次性任务：回归计数 + 胜率 + 命中图画廊。

默认范围：10 常用品种 × 近 60 天信号 × 全部监控周期（1m/5m/30m/1h/2h/1d）。
长周期加载更长历史预热（形态跨周），只统计近 60 天触发，与实盘口径一致。
跑完即退出（compose restart: "no"），部署/up 时重跑；BACKTEST_WORKERS 限并发。
结果写 reports/backtest/，浏览器经 API /backtest 看画廊。
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from audit_killed_samples import replay_detail  # noqa: E402
from build_hits_gallery import build_gallery  # noqa: E402
from core.hs_chart import render_hs_chart  # noqa: E402
from core.hs_monitor import bars_to_strategy_df  # noqa: E402
from core.kline_store import DURATION_LABELS, connect, load_bars  # noqa: E402
from replay_winrate import simulate_outcomes  # noqa: E402

DEFAULT_SYMBOLS = [
    "KQ.m@SHFE.fu",
    "KQ.m@INE.lu",
    "KQ.m@DCE.eg",
    "KQ.m@DCE.jm",
    "KQ.m@DCE.j",
    "KQ.m@DCE.i",
    "KQ.m@CZCE.MA",
    "KQ.m@CZCE.TA",
    "KQ.m@CZCE.AP",
    "KQ.m@DCE.bb",
]
DEFAULT_TFS = [60, 300, 1800, 3600, 7200, 86400]
# 长周期形态跨周，需更长历史预热（与实盘加载满窗口一致），信号仍只统计近 days 天
LOAD_DAYS = {60: 60, 300: 60, 1800: 90, 3600: 120, 7200: 150, 86400: 300}


def _render(symbol: str, tf: int, sigs: list[dict], tdir: Path, prefix: str, conn) -> list[dict]:
    meta = []
    if not sigs:
        return meta
    tdir.mkdir(parents=True, exist_ok=True)
    for k, s in enumerate(sigs, 1):
        png = render_hs_chart(symbol, tf, s["when"], s["detail"], conn, future_bars=15)
        if not png:
            continue
        name = f"{prefix}_{symbol.replace('@', '_').replace('.', '')}_{k:02d}.png"
        (tdir / name).write_bytes(png)
        meta.append(
            {"file": name, "symbol": symbol, "when": s["when"], "reason": s["reason"]}
        )
    return meta


def _job(job: tuple[str, int, int, str, bool, int, int, float, bool]) -> dict:
    symbol, tf, days, outdir, allow_gap, pivot_order, load_days, prominence, no_time_gates = job
    cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
    label = DURATION_LABELS.get(tf, str(tf))
    conn = connect()
    try:
        bars = load_bars(
            conn,
            symbol,
            tf,
            start=datetime.now() - timedelta(days=load_days or LOAD_DAYS.get(tf, days)),
        )
        if len(bars) < 120:
            return {
                "tf": tf, "symbol": symbol, "loose": 0,
                "meta": [], "kmeta": [], "rows": [],
            }
        loose = [
            s
            for s in replay_detail(
                symbol, tf, bars, False,
                pivot_order=pivot_order or None,
                prominence=prominence or None,
                no_time_gates=no_time_gates,
            )
            if s["when"] >= cutoff
        ]
        hits = [
            s
            for s in replay_detail(
                symbol,
                tf,
                bars,
                True,
                allow_session_gap=allow_gap,
                pivot_order=pivot_order or None,
                prominence=prominence or None,
                no_time_gates=no_time_gates,
            )
            if s["when"] >= cutoff
        ]
        hit_when = {s["when"] for s in hits}
        killed = [s for s in loose if s["when"] not in hit_when]
        rows = simulate_outcomes(symbol, tf, bars_to_strategy_df(bars), hits)
        meta = _render(symbol, tf, hits, Path(outdir) / label, "hit", conn)
        kmeta = _render(symbol, tf, killed, Path(outdir) / "killed" / label, "killed", conn)
    finally:
        conn.close()
    return {
        "tf": tf, "symbol": symbol, "loose": len(loose),
        "meta": meta, "kmeta": kmeta, "rows": rows,
    }


def _stats(rows: list[dict]) -> dict:
    w = sum(1 for r in rows if r["result"] == "win")
    l = sum(1 for r in rows if r["result"] == "loss")
    t = sum(1 for r in rows if r["result"] == "timeout")
    decided = w + l
    return {
        "signals": len(rows),
        "win": w,
        "loss": l,
        "timeout": t,
        "win_rate": round(w / decided, 4) if decided else None,
        "expectancy": round((w * 0.8 - l * 0.2) / decided, 4) if decided else None,
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=60)
    ap.add_argument(
        "--workers", type=int, default=int(os.environ.get("BACKTEST_WORKERS", "2"))
    )
    ap.add_argument("--tfs", type=str, default=",".join(str(t) for t in DEFAULT_TFS))
    ap.add_argument("--symbols", type=str, default=",".join(DEFAULT_SYMBOLS))
    ap.add_argument(
        "--allow-session-gap",
        action="store_true",
        help="回测对比用：放开 1m/5m 形态跨收盘断点（实盘口径不变）",
    )
    ap.add_argument(
        "--pivot-override",
        type=str,
        default="",
        help="回测对比用：如 300:1 表示 5m 拐点只要求左右各 1 根（实盘口径不变）",
    )
    ap.add_argument(
        "--load-days",
        type=int,
        default=0,
        help="回测对比用：覆盖各周期固定预热窗口（0=按 LOAD_DAYS）。确认根数调小后成本随根数三次方涨，需要时用它缩窗口",
    )
    ap.add_argument(
        "--prominence",
        type=float,
        default=0.0,
        help="回测对比用：肩突出门槛占头高比例（0=按实盘 0.25）。如 0.2 放宽到 0.2×头高（实盘口径不变）",
    )
    ap.add_argument(
        "--no-time-gates",
        action="store_true",
        help="回测对比用：去掉 130 分钟跨断点闸 + 5 交易日跨度闸（实盘口径不变）",
    )
    ap.add_argument("--outdir", type=str, default="")
    args = ap.parse_args()

    if args.prominence < 0:
        ap.error("--prominence 不能为负（0=按实盘 0.25）")

    tfs = [int(x) for x in args.tfs.split(",") if x.strip()]
    symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
    pivots: dict[int, int] = {}
    for part in args.pivot_override.split(","):
        if not part.strip():
            continue
        sec_s, order_s = part.split(":")
        sec, order = int(sec_s), int(order_s)
        if order < 1:
            ap.error("--pivot-override 的确认根数必须 ≥1")
        pivots[sec] = order
    outdir = Path(
        args.outdir
        or (
            ROOT
            / "reports"
            / (
                "backtest_pivot"
                if pivots
                else (
                    "backtest_time"
                    if args.no_time_gates
                    else (
                        "backtest_gap"
                        if args.allow_session_gap
                        else ("backtest_prom" if args.prominence else "backtest")
                    )
                )
            )
        )
    )
    shutil.rmtree(outdir, ignore_errors=True)
    outdir.mkdir(parents=True, exist_ok=True)

    jobs = [
        (
            s, tf, args.days, str(outdir), args.allow_session_gap,
            pivots.get(tf, 0), args.load_days, args.prominence, args.no_time_gates,
        )
        for tf in tfs
        for s in symbols
    ]
    piv_desc = (
        ",".join(f"{DURATION_LABELS.get(k, k)}={v}" for k, v in pivots.items())
        or "按实盘"
    )
    print(
        f"[backtest] 品种 {len(symbols)} × 周期 {tfs} × {args.days} 天，"
        f"预热 {args.load_days or '按周期'} 天，"
        f"workers={args.workers}，作业 {len(jobs)}，"
        f"跨断点={'放开(仅回测)' if args.allow_session_gap else '按实盘'}，"
        f"枢轴={piv_desc}{'(仅回测)' if pivots else ''}，"
        f"肩突出={args.prominence or 0.25:g}h{'(仅回测)' if args.prominence else ''}，"
        f"时间闸={'去掉(仅回测)' if args.no_time_gates else '按实盘'}",
        flush=True,
    )

    results = []
    t0 = datetime.now()
    with ProcessPoolExecutor(max_workers=args.workers) as ex:
        for r in ex.map(_job, jobs, chunksize=1):
            results.append(r)
            print(
                f"[backtest] {len(results)}/{len(jobs)} {r['symbol']} "
                f"{DURATION_LABELS.get(r['tf'], r['tf'])} 改前 {r['loose']} "
                f"改后 {len(r['meta'])} 已用 {(datetime.now() - t0).total_seconds():.0f}s",
                flush=True,
            )

    summary: dict = {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "days": args.days,
        "load_days": {
            DURATION_LABELS.get(tf, str(tf)): args.load_days or LOAD_DAYS.get(tf, args.days)
            for tf in tfs
        },
        "symbols": symbols,
        "tfs": tfs,
        "allow_session_gap": args.allow_session_gap,
        "pivot_overrides": {DURATION_LABELS.get(k, str(k)): v for k, v in pivots.items()},
        "shoulder_prominence_ratio": args.prominence or 0.25,
        "no_time_gates": args.no_time_gates,
        "by_tf": {},
    }
    dirs = []
    kdirs = []
    all_rows: list[dict] = []
    total_loose = total_strict = total_killed = 0
    for tf in tfs:
        rs = [r for r in results if r["tf"] == tf]
        label = DURATION_LABELS.get(tf, str(tf))
        meta = [m for r in rs for m in r["meta"]]
        kmeta = [m for r in rs for m in r["kmeta"]]
        rows = [row for r in rs for row in r["rows"]]
        all_rows.extend(rows)
        loose_n = sum(r["loose"] for r in rs)
        total_loose += loose_n
        total_strict += len(meta)
        total_killed += len(kmeta)
        if meta:
            tdir = outdir / label
            (tdir / f"meta_{label}.json").write_text(
                json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8"
            )
            dirs.append(tdir)
        if kmeta:
            kdir = outdir / "killed" / label
            (kdir / "meta_killed.json").write_text(
                json.dumps(kmeta, ensure_ascii=False, indent=1), encoding="utf-8"
            )
            kdirs.append(kdir)
        st = _stats(rows)
        st["loose"] = loose_n
        st["strict"] = len(meta)
        st["killed"] = len(kmeta)
        summary["by_tf"][label] = st
        wr = f"{st['win_rate']:.1%}" if st["win_rate"] is not None else "-"
        ex_ = f"{st['expectancy']:+.3f}" if st["expectancy"] is not None else "-"
        print(
            f"[backtest] {label}: 改前 {loose_n} -> 改后 {len(meta)} 滤掉 {len(kmeta)} | "
            f"胜 {st['win']} 负 {st['loss']} 超时 {st['timeout']} 胜率 {wr} 期望 {ex_}",
            flush=True,
        )
    summary["total"] = _stats(all_rows)
    summary["total"]["loose"] = total_loose
    summary["total"]["strict"] = total_strict
    summary["total"]["killed"] = total_killed
    (outdir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    t = summary["total"]
    wr = f"{t['win_rate']:.1%}" if t["win_rate"] is not None else "-"
    ex_ = f"{t['expectancy']:+.3f}" if t["expectancy"] is not None else "-"
    print(
        f"[backtest] 合计: 改前 {total_loose} -> 改后 {total_strict} 滤掉 {total_killed} | "
        f"胜 {t['win']} 负 {t['loss']} 超时 {t['timeout']} 胜率 {wr} 期望 {ex_}",
        flush=True,
    )

    labels = "/".join(DURATION_LABELS.get(tf, str(tf)) for tf in tfs)
    base = (
        "/backtest/pivot"
        if pivots
        else (
            "/backtest/time"
            if args.no_time_gates
            else (
                "/backtest/gap"
                if args.allow_session_gap
                else ("/backtest/prom" if args.prominence else "/backtest")
            )
        )
    )
    mods = []
    if pivots:
        mods.append(f"枢轴{piv_desc}")
    if args.allow_session_gap:
        mods.append("放开跨断点")
    if args.prominence:
        mods.append(f"肩突出{args.prominence:g}h")
    if args.no_time_gates:
        mods.append("去掉时间闸")
    tag = "v4 标定" + (f" · {' / '.join(mods)}(仅回测)" if mods else "")
    subtitle = (
        f"{len(symbols)} 品种 × {args.days} 天 × {labels} · {tag} · "
        f"严格 {total_strict} / 改前 {total_loose} · 胜率 {wr} · 期望(以 swing 计) {ex_} · "
        f"生成 {summary['generated_at']}"
    )
    n = build_gallery(
        dirs, True, outdir / "gallery_single.html", subtitle,
        nav=f'<a href="{base}/killed">查看被严格闸滤掉的信号 →</a>',
    )
    ksub = (
        f"{len(symbols)} 品种 × {args.days} 天 × {labels} · {tag} · "
        f"改前 {total_loose} 条中滤掉 {total_killed} 条 · 生成 {summary['generated_at']}"
    )
    kn = build_gallery(
        kdirs, True, outdir / "gallery_killed.html", ksub,
        title="被滤信号", nav=f'<a href="{base}">← 返回命中画廊</a>',
    )
    print(
        f"[backtest] 完成：命中画廊 {n} 张 -> {outdir / 'gallery_single.html'}；"
        f"被滤画廊 {kn} 张 -> {outdir / 'gallery_killed.html'}",
        flush=True,
    )


if __name__ == "__main__":
    main()