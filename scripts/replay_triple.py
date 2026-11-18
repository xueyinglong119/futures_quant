"""三重顶/底历史回归：逐根滚动跑同一段历史，数三重出多少条、和头肩撞同根的有多少、胜率如何。

三重是第三条独立通道（同根两条都发），所以这里同时回放常规档头肩做对照：
`撞头肩同根`说明那笔形态头肩本来也报，三重只是补充；没撞上的才是净新增覆盖面。
止盈止损沿用 0.8/0.2×形态高度（形态高度 = 最高顶 − 颈线），平衡胜率 20%。

默认顺带把三重命中的 K 线图出成画廊（`reports/backtest_triple/`，生产机 `/backtest/triple`
打开），卡片标出这条是三重独占还是同根已有头肩——这条通道能不能留，得先目视那些独占的
画出来到底是不是三个等高顶。`--no-gallery` 只出数字。
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from audit_killed_samples import replay_detail  # noqa: E402
from build_hits_gallery import build_gallery, render_hits  # noqa: E402
from core.hs_monitor import (  # noqa: E402
    HS_MONITOR_DURATIONS,
    bars_to_strategy_df,
    make_strategy,
    make_triple_strategy,
)
from core.kline_store import (  # noqa: E402
    DURATION_LABELS,
    TABLE_NAME,
    connect,
    load_bars,
)
from replay_winrate import BALANCE, _min_bars, simulate_outcomes, summarize  # noqa: E402


def _job(j: tuple[str, int, int]) -> tuple[list[dict], int, str, int, list[dict]]:
    symbol, tf, days = j
    rows, n_bars, hits = _outcome(symbol, tf, days)
    return rows, n_bars, symbol, tf, hits


def _outcome(symbol: str, tf: int, days: int) -> tuple[list[dict], int, list[dict]]:
    """返回 (胜率行, 根数不足时的实际根数, 三重命中明细)。

    命中明细带回主进程再统一出图：三重在 5m 上很密（本机 2 品种 45 天就 133 条），
    全市场几千条若在 worker 里各画各的，画廊会变成打不开的几百 MB。
    """
    conn = connect()
    try:
        bars = load_bars(conn, symbol, tf, start=datetime.now() - timedelta(days=days))
        # 加载窗不够就一根都出不了，必须报出来：否则 0 会被读成「这个品种没形态」
        if len(bars) < _min_bars(tf):
            return [], len(bars), []
        df = bars_to_strategy_df(bars)
        tri = replay_detail(symbol, tf, bars, False, factory=make_triple_strategy)
        hs = replay_detail(symbol, tf, bars, False, factory=make_strategy)
        hs_whens = {s["when"] for s in hs}
        reason_by_when = {s["when"]: s["reason"] for s in tri}
        rows = simulate_outcomes(symbol, tf, df, tri)
        for r in rows:
            # 撞车只看时刻：两条通道各自记账，同根时两条都发
            r["with_hs"] = r["when"] in hs_whens
            r["reason"] = reason_by_when.get(r["when"], "")
        hits = [
            {
                "when": s["when"],
                "reason": s["reason"],
                "detail": s["detail"],
                "with_hs": s["when"] in hs_whens,
            }
            for s in tri
        ]
    finally:
        conn.close()
    return rows, 0, hits


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=60)
    ap.add_argument("--symbols", type=str, default="", help="逗号分隔品种，缺省全主力")
    ap.add_argument("--tfs", type=str, default="", help="逗号分隔周期秒，缺省全部监控周期")
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--samples", type=int, default=6, help="每周期打印的样例条数")
    ap.add_argument(
        "--gallery-max",
        type=int,
        default=800,
        help="画廊最多渲染多少张（按时间取最近的，0=全出；800 张 base64 约 30MB，再多浏览器打不开）",
    )
    ap.add_argument("--no-gallery", action="store_true", help="只出数字，不渲染 K 线图")
    args = ap.parse_args()

    tfs = (
        [int(x) for x in args.tfs.split(",") if x.strip()]
        if args.tfs
        else list(HS_MONITOR_DURATIONS)
    )
    outdir = Path(ROOT / "reports" / "backtest_triple")
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
    gal = (
        "不出画廊"
        if args.no_gallery
        else f"画廊上限 {args.gallery_max or '全出'} 张"
    )
    print(
        f"品种 {len(symbols)}，回溯 {args.days} 天，周期 {tfs}，"
        f"三重顶/底独立通道（平衡胜率 {BALANCE:.1%}），{gal}",
        flush=True,
    )

    jobs = [(s, tf, args.days) for tf in tfs for s in symbols]
    rows: list[dict] = []
    hits: list[dict] = []
    skipped: dict[int, list[tuple[str, int]]] = {}
    done = 0
    t_start = datetime.now()
    with ProcessPoolExecutor(max_workers=args.workers) as ex:
        for chunk, n_bars, symbol, tf, chunk_hits in ex.map(_job, jobs, chunksize=2):
            done += 1
            if done % 40 == 0:
                el = (datetime.now() - t_start).total_seconds()
                print(f"进度 {done}/{len(jobs)} 已用 {el:.0f}s", flush=True)
            rows.extend(chunk)
            for h in chunk_hits:
                h["symbol"] = symbol
                h["tf"] = tf
            hits.extend(chunk_hits)
            if n_bars:
                skipped.setdefault(tf, []).append((symbol, n_bars))

    print("\n== 逐周期（撞车 = 同根也有头肩告警，即净新增之外的重复）==", flush=True)
    for tf in tfs:
        rs = [r for r in rows if r["tf"] == tf]
        label = DURATION_LABELS.get(tf, str(tf))
        both = sum(1 for r in rs if r.get("with_hs"))
        got = skipped.get(tf, [])
        hint = f"，另有 {len(got)} 个品种根数不足 {_min_bars(tf)} 根被跳过" if got else ""
        print(f"  {label}: 三重 {len(rs)} 条（撞头肩同根 {both}）{hint}", flush=True)
        if rs:
            print(f"    {summarize(rs)}", flush=True)

    print("\n== 合计 ==", flush=True)
    both_all = sum(1 for r in rows if r.get("with_hs"))
    print(f"  三重 {len(rows)} 条 / 撞头肩同根 {both_all} 条 / 独占 {len(rows) - both_all} 条", flush=True)
    print(f"  {summarize(rows)}", flush=True)

    if not args.no_gallery and hits:
        shutil.rmtree(outdir, ignore_errors=True)
        picked = sorted(hits, key=lambda h: h["when"])
        if args.gallery_max:
            picked = picked[-args.gallery_max :]
        dropped = len(hits) - len(picked)
        dirs = []
        conn = connect()
        try:
            for tf in tfs:
                group = [h for h in picked if h["tf"] == tf]
                if not group:
                    continue
                label = DURATION_LABELS.get(tf, str(tf))
                tdir = outdir / label
                meta = []
                for symbol in dict.fromkeys(h["symbol"] for h in group):
                    sigs = [h for h in group if h["symbol"] == symbol]
                    meta.extend(
                        render_hits(
                            symbol,
                            tf,
                            sigs,
                            tdir,
                            "triple",
                            conn,
                            tags={
                                h["when"]: "同根有头肩"
                                if h["with_hs"]
                                else "三重独占"
                                for h in sigs
                            },
                        )
                    )
                if meta:
                    (tdir / f"meta_{label}.json").write_text(
                        json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8"
                    )
                    dirs.append(tdir)
        finally:
            conn.close()
        labels = "/".join(DURATION_LABELS.get(tf, str(tf)) for tf in tfs)
        n = build_gallery(
            dirs,
            True,
            outdir / "gallery_single.html",
            (
                f"{len(symbols)} 品种 × {args.days} 天 × {labels} · 三重 {len(rows)} 条"
                f"（独占 {len(rows) - both_all}）· 出图 {len(picked)} 张"
                f"{f'，更早 {dropped} 条未画（加大 --gallery-max）' if dropped else ''} · "
                f"{summarize(rows)} · 生成 {datetime.now():%Y-%m-%d %H:%M:%S}"
            ),
            title="三重顶/底画廊",
            nav='<a href="/backtest">← 返回头肩常规档画廊</a>',
        )
        print(f"\n画廊 {n} 张 -> {outdir / 'gallery_single.html'}", flush=True)

    print("\n== 样例（每周期最近的几条）==", flush=True)
    for tf in tfs:
        rs = sorted((r for r in rows if r["tf"] == tf), key=lambda r: r["when"], reverse=True)
        if not rs:
            continue
        label = DURATION_LABELS.get(tf, str(tf))
        for r in rs[: args.samples]:
            hit = "同根有头肩" if r.get("with_hs") else "三重独占"
            print(
                f"  {label} {r['symbol']} {r['when']} {hit} {r['result']} {r['reason'][:56]}",
                flush=True,
            )


if __name__ == "__main__":
    main()
