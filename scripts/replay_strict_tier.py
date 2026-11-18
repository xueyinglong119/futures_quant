"""严格档历史回归：逐根滚动跑同一段历史，统计严格档出数、其中与常规档撞同形态的有多少。

两档同刻触发，同形态时常规档那条由 `tier_dets` 直接不发，所以「同形态 K」就是**被让位**的
常规档条数，「独占 M−K」是只有严格档才告出来的新形态。常规档口径不变（同一实例分别跑两档，
两套 `_traded_*` 互不影响），这里的「常规档 N」可以直接和改前的告警数对比。
`HS_STRICT_SIGNALS=0` 会让严格档整体静默，用来出对照。

默认顺带把严格档命中的 K 线图出成画廊（`reports/backtest_strict/`，生产机 `/backtest/strict`
打开），卡片标题标出这条是严格档独占还是与常规档同形态——判断这一档值不值 50% 手数要目视，
光看计数看不出画出来的是什么东西。`--no-gallery` 只出数字。
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

from build_hits_gallery import build_gallery, render_hits  # noqa: E402
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
    """返回 (常规档条数, 严格档明细, 严格档里能在常规档找到同形态的条数)。"""
    strat = make_strategy(duration_sec)
    df = bars_to_strategy_df(bars)
    win = TF_CONFIGS[duration_sec].lookback_bars
    min_len = _min_bars(duration_sec)
    normal_n = 0
    normal_pat: set[str] = set()
    strict: list[dict] = []
    for i in range(min_len - 1, len(df)):
        win_df = df.iloc[max(0, i - win) : i + 1]
        sig = strat.on_bar(win_df, symbol=symbol)
        if sig is not None:
            normal_n += 1
            normal_pat.add(str((sig.detail or {}).get("pattern_id")))
        s = strat.on_bar_strict(win_df, symbol=symbol)
        if s is not None:
            pid = str((s.detail or {}).get("pattern_id"))
            strict.append(
                {
                    "when": str(df.iloc[i]["datetime"]),
                    "pattern_id": pid,
                    "reason": s.reason,
                    "detail": s.detail or {},
                    "mark": "同形态让位" if pid in normal_pat else "严格档独占",
                }
            )
    hit = sum(1 for r in strict if r["mark"] == "同形态让位")
    return normal_n, strict, hit


def _job(job: tuple[str, int, int]) -> tuple[int, str, int, int, int, int, list[dict]]:
    symbol, tf, days = job
    conn = connect()
    try:
        bars = load_bars(conn, symbol, tf, start=datetime.now() - timedelta(days=days))
        # 加载窗不够就一根都出不了，必须报出来：否则 0 会被读成「这个周期没信号」
        need = _min_bars(tf)
        if len(bars) < need:
            return tf, symbol, 0, 0, 0, len(bars), []
        normal_n, strict, hit = replay(symbol, tf, bars)
    finally:
        conn.close()
    # 明细回主进程再统一出图：截断要全局按时间排，在 worker 里各画各的去不掉旧信号
    return tf, symbol, normal_n, len(strict), hit, 0, strict


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=60)
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--tfs", type=str, default="", help="逗号分隔周期秒，缺省全部监控周期")
    ap.add_argument("--symbols", type=str, default="", help="逗号分隔品种，缺省全主力")
    ap.add_argument("--samples", type=int, default=6, help="每周期打印的严格档样例条数")
    ap.add_argument(
        "--gallery-max",
        type=int,
        default=800,
        help="画廊最多渲染多少张（按时间取最近的，0=全出）",
    )
    ap.add_argument("--no-gallery", action="store_true", help="只出数字，不渲染 K 线图")
    args = ap.parse_args()

    tfs = (
        [int(x) for x in args.tfs.split(",") if x.strip()]
        if args.tfs
        else list(HS_MONITOR_DURATIONS)
    )
    outdir = Path(ROOT / "reports" / "backtest_strict")
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
    gal = (
        "不出画廊"
        if args.no_gallery
        else f"画廊上限 {args.gallery_max or '全出'} 张"
    )
    print(
        f"品种 {len(symbols)}，回溯 {args.days} 天，周期 {tfs}，"
        f"HS_STRICT_SIGNALS={os.environ.get('HS_STRICT_SIGNALS', '1')}，{gal}",
        flush=True,
    )

    jobs = [(s, tf, args.days) for tf in tfs for s in symbols]
    agg: dict[int, list[int]] = {}
    skipped: dict[int, list[tuple[str, int]]] = {}
    hits: list[dict] = []
    done = 0
    t_start = datetime.now()
    with ProcessPoolExecutor(max_workers=args.workers) as ex:
        for tf, symbol, n_normal, n_strict, n_hit, n_bars, detail in ex.map(
            _job, jobs, chunksize=2
        ):
            done += 1
            if done % 40 == 0:
                el = (datetime.now() - t_start).total_seconds()
                print(f"进度 {done}/{len(jobs)} 已用 {el:.0f}s", flush=True)
            a = agg.setdefault(tf, [0, 0, 0])
            a[0] += n_normal
            a[1] += n_strict
            a[2] += n_hit
            for r in detail:
                r["symbol"] = symbol
                r["tf"] = tf
            hits.extend(detail)
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

    if not args.no_gallery and hits:
        shutil.rmtree(outdir, ignore_errors=True)
        picked = sorted(hits, key=lambda r: r["when"])
        if args.gallery_max:
            picked = picked[-args.gallery_max :]
        dropped = len(hits) - len(picked)
        dirs = []
        for tf in tfs:
            group = [r for r in picked if r["tf"] == tf]
            if not group:
                continue
            label = DURATION_LABELS.get(tf, str(tf))
            tdir = outdir / label
            meta = []
            for symbol in dict.fromkeys(r["symbol"] for r in group):
                sigs = [r for r in group if r["symbol"] == symbol]
                meta.extend(
                    render_hits(
                        symbol,
                        tf,
                        sigs,
                        tdir,
                        "strict",
                        conn,
                        tags={r["when"]: r["mark"] for r in sigs},
                    )
                )
            if meta:
                (tdir / f"meta_{label}.json").write_text(
                    json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8"
                )
                dirs.append(tdir)
        labels = "/".join(DURATION_LABELS.get(tf, str(tf)) for tf in tfs)
        n = build_gallery(
            dirs,
            True,
            outdir / "gallery_single.html",
            (
                f"{len(symbols)} 品种 × {args.days} 天 × {labels} · 严格档 {tot[1]} 条"
                f"（独占 {tot[1] - tot[2]}）· 出图 {len(picked)} 张"
                f"{f'，更早 {dropped} 条未画（加大 --gallery-max）' if dropped else ''} · "
                f"生成 {datetime.now():%Y-%m-%d %H:%M:%S}"
            ),
            title="严格档画廊",
            nav='<a href="/backtest">← 返回常规档（改前/改后）画廊</a>',
        )
        print(f"\n画廊 {n} 张 -> {outdir / 'gallery_single.html'}", flush=True)

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
        for r in strict[-args.samples :]:
            print(f"    {r['when']} [{r['mark']}] {r['reason'][:72]}", flush=True)
    conn.close()


if __name__ == "__main__":
    main()
