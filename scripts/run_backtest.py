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
from replay_hs_history import replay  # noqa: E402
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


def _job(job: tuple[str, int, int, str]) -> dict:
    symbol, tf, days, outdir = job
    cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
    conn = connect()
    try:
        bars = load_bars(
            conn, symbol, tf, start=datetime.now() - timedelta(days=LOAD_DAYS.get(tf, days))
        )
        if len(bars) < 120:
            return {"tf": tf, "symbol": symbol, "loose": 0, "meta": [], "rows": []}
        loose = sum(1 for w, _ in replay(symbol, tf, bars, False) if w >= cutoff)
        hits = [s for s in replay_detail(symbol, tf, bars, True) if s["when"] >= cutoff]
        rows = simulate_outcomes(symbol, tf, bars_to_strategy_df(bars), hits)
        meta = []
        tdir = Path(outdir) / DURATION_LABELS.get(tf, str(tf))
        tdir.mkdir(parents=True, exist_ok=True)
        for k, s in enumerate(hits, 1):
            png = render_hs_chart(symbol, tf, s["when"], s["detail"], conn)
            if not png:
                continue
            name = f"hit_{symbol.replace('@', '_').replace('.', '')}_{k:02d}.png"
            (tdir / name).write_bytes(png)
            meta.append(
                {"file": name, "symbol": symbol, "when": s["when"], "reason": s["reason"]}
            )
    finally:
        conn.close()
    return {"tf": tf, "symbol": symbol, "loose": loose, "meta": meta, "rows": rows}


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
    ap.add_argument("--outdir", type=str, default=str(ROOT / "reports" / "backtest"))
    args = ap.parse_args()

    tfs = [int(x) for x in args.tfs.split(",") if x.strip()]
    symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
    outdir = Path(args.outdir)
    shutil.rmtree(outdir, ignore_errors=True)
    outdir.mkdir(parents=True, exist_ok=True)

    jobs = [(s, tf, args.days, str(outdir)) for tf in tfs for s in symbols]
    print(
        f"[backtest] 品种 {len(symbols)} × 周期 {tfs} × {args.days} 天，"
        f"workers={args.workers}，作业 {len(jobs)}",
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
            DURATION_LABELS.get(tf, str(tf)): LOAD_DAYS.get(tf, args.days) for tf in tfs
        },
        "symbols": symbols,
        "tfs": tfs,
        "by_tf": {},
    }
    dirs = []
    all_rows: list[dict] = []
    total_loose = total_strict = 0
    for tf in tfs:
        rs = [r for r in results if r["tf"] == tf]
        label = DURATION_LABELS.get(tf, str(tf))
        meta = [m for r in rs for m in r["meta"]]
        rows = [row for r in rs for row in r["rows"]]
        all_rows.extend(rows)
        loose_n = sum(r["loose"] for r in rs)
        total_loose += loose_n
        total_strict += len(meta)
        if meta:
            tdir = outdir / label
            (tdir / f"meta_{label}.json").write_text(
                json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8"
            )
            dirs.append(tdir)
        st = _stats(rows)
        st["loose"] = loose_n
        st["strict"] = len(meta)
        summary["by_tf"][label] = st
        wr = f"{st['win_rate']:.1%}" if st["win_rate"] is not None else "-"
        ex_ = f"{st['expectancy']:+.3f}" if st["expectancy"] is not None else "-"
        print(
            f"[backtest] {label}: 改前 {loose_n} -> 改后 {len(meta)} | "
            f"胜 {st['win']} 负 {st['loss']} 超时 {st['timeout']} 胜率 {wr} 期望 {ex_}",
            flush=True,
        )
    summary["total"] = _stats(all_rows)
    summary["total"]["loose"] = total_loose
    summary["total"]["strict"] = total_strict
    (outdir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    t = summary["total"]
    wr = f"{t['win_rate']:.1%}" if t["win_rate"] is not None else "-"
    ex_ = f"{t['expectancy']:+.3f}" if t["expectancy"] is not None else "-"
    print(
        f"[backtest] 合计: 改前 {total_loose} -> 改后 {total_strict} | "
        f"胜 {t['win']} 负 {t['loss']} 超时 {t['timeout']} 胜率 {wr} 期望 {ex_}",
        flush=True,
    )

    labels = "/".join(DURATION_LABELS.get(tf, str(tf)) for tf in tfs)
    subtitle = (
        f"{len(symbols)} 品种 × {args.days} 天 × {labels} · v4 标定 · "
        f"胜率 {wr} · 期望(以 swing 计) {ex_} · 生成 {summary['generated_at']}"
    )
    n = build_gallery(dirs, True, outdir / "gallery_single.html", subtitle)
    print(f"[backtest] 完成：画廊 {n} 张 -> {outdir / 'gallery_single.html'}", flush=True)


if __name__ == "__main__":
    main()