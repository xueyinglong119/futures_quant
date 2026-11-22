"""超严格头肩底/顶 A/B：在现有严格档命中上再套六条，数命中与胜率。

严格档命中来自同一套 `on_bar_strict`（右肩收盘当根）；本脚本只做后验过滤，不改线上。
规则 5 需要右肩之后 3 根，回测里用后续 K 线做**后验**标记；实盘当根看不到，所以
「超严格(1-4/6)」才是当根能发射的集合，「+规则5」是事后收紧后的参考口径。
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
from core.hs_monitor import TF_CONFIGS, bars_to_strategy_df  # noqa: E402
from core.kline_store import DURATION_LABELS, TABLE_NAME, connect, load_bars  # noqa: E402
from replay_winrate import _min_bars, simulate_outcomes, summarize  # noqa: E402
from strategies.head_shoulders import _format_bar_time  # noqa: E402
from strategies.hs_super_strict import check, pullback_ok  # noqa: E402


def _tag_hits(symbol: str, tf: int, bars: list[dict], hits: list[dict]):
    df = bars_to_strategy_df(bars)
    win = TF_CONFIGS[tf].lookback_bars
    when_to_i = {str(df.iloc[i]["datetime"]): i for i in range(len(df))}
    super_hits: list[dict] = []
    r5_pass: list[dict] = []
    r5_fail = r5_pending = 0
    for h in hits:
        i = when_to_i.get(h["when"])
        if i is None:
            continue
        d = h["detail"]
        start = max(0, i - win)
        try:
            li = start + int(d["left_idx"])
            hi = start + int(d["head_idx"])
            ri = start + int(d["right_idx"])
        except (KeyError, TypeError, ValueError):
            continue
        if not (0 <= li < hi < ri < len(df)):
            continue
        tmap = {
            _format_bar_time(df.iloc[j]["datetime"]): j
            for j in range(start, i + 1)
        }
        n1 = tmap.get(str(d.get("neck_l_time")))
        n2 = tmap.get(str(d.get("neck_r_time")))
        if n1 is None or n2 is None:
            continue
        bottom = d.get("side") == "long"
        ok, _info = check(
            bottom=bottom,
            left_price=float(d["left"]),
            left_bar_high=float(df.iloc[li]["high"]),
            left_bar_low=float(df.iloc[li]["low"]),
            head_price=float(d["head"]),
            right_price=float(d["right"]),
            right_bar_high=float(df.iloc[ri]["high"]),
            right_bar_low=float(df.iloc[ri]["low"]),
            neck_l=(n1, float(df.iloc[n1]["close"])),
            neck_r=(n2, float(df.iloc[n2]["close"])),
            right_idx=ri,
        )
        if not ok:
            continue
        super_hits.append(h)
        r5 = pullback_ok(
            df, ri,
            float(df.iloc[ri]["high"]), float(df.iloc[ri]["low"]),
            bottom=bottom,
        )
        if r5 is None:
            r5_pending += 1
        elif r5:
            r5_pass.append(h)
        else:
            r5_fail += 1
    super_ids = {id(h) for h in super_hits}
    filtered = [h for h in hits if id(h) not in super_ids]
    return df, super_hits, r5_pass, r5_fail, r5_pending, filtered


def _job(job: tuple[str, int, int]):
    symbol, tf, days = job
    conn = connect()
    try:
        bars = load_bars(conn, symbol, tf, start=datetime.now() - timedelta(days=days))
        if len(bars) < _min_bars(tf):
            return tf, symbol, 0, 0, 0, 0, 0, len(bars), [], [], []
        hits = replay_detail(symbol, tf, bars, False, strict_tier=True)
        df, super_hits, r5_pass, r5_fail, r5_pending, filtered = _tag_hits(
            symbol, tf, bars, hits
        )
    finally:
        conn.close()
    for h in super_hits + filtered:
        h["symbol"] = symbol
        h["tf"] = tf
    return (
        tf, symbol, len(hits), len(super_hits), len(r5_pass), r5_fail, r5_pending,
        len(bars),
        simulate_outcomes(symbol, tf, df, hits),
        simulate_outcomes(symbol, tf, df, super_hits),
        simulate_outcomes(symbol, tf, df, r5_pass),
        super_hits,
        filtered,
    )


def _render_gallery(
    hits: list[dict],
    outdir: Path,
    prefix: str,
    tag: str,
    max_n: int,
    title: str,
    nav: str,
) -> None:
    shutil.rmtree(outdir, ignore_errors=True)
    outdir.mkdir(parents=True, exist_ok=True)
    picked = sorted(hits, key=lambda h: h["when"])
    if max_n:
        picked = picked[-max_n:]
    dropped = len(hits) - len(picked)
    by_tf: dict[int, dict[str, list[dict]]] = {}
    for h in picked:
        by_tf.setdefault(int(h["tf"]), {}).setdefault(str(h["symbol"]), []).append(h)
    dirs: list[Path] = []
    conn = connect()
    try:
        for tf, by_symbol in by_tf.items():
            label = DURATION_LABELS.get(tf, str(tf))
            tdir = outdir / label
            meta: list[dict] = []
            for symbol, sigs in by_symbol.items():
                meta.extend(
                    render_hits(
                        symbol, tf, sigs, tdir, prefix, conn,
                        tags={s["when"]: tag for s in sigs},
                    )
                )
            if meta:
                (tdir / f"meta_{label}.json").write_text(
                    json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8"
                )
                dirs.append(tdir)
    finally:
        conn.close()
    if not dirs:
        print(f"\n{title}: 无图", flush=True)
        return
    labels = "/".join(d.name for d in dirs)
    n = build_gallery(
        dirs,
        True,
        outdir / "gallery_single.html",
        (
            f"{len(hits)} 条，出图 {len(picked)} 张"
            f"{f'，更早 {dropped} 条未画（加大 --gallery-max）' if dropped else ''}"
            f" · {labels} · {tag}"
        ),
        title=title,
        nav=nav,
    )
    print(f"\n{title}: {n} 张 -> {outdir / 'gallery_single.html'}", flush=True)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=60)
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--tfs", type=str, default="", help="逗号分隔周期秒，缺省全部监控周期")
    ap.add_argument("--symbols", type=str, default="", help="逗号分隔品种，缺省全主力")
    ap.add_argument("--gallery", action="store_true", help="渲染命中/被过滤两张画廊")
    ap.add_argument(
        "--gallery-max",
        type=int,
        default=300,
        help="每张画廊最多渲染多少张（按时间取最近的，0=全出）",
    )
    args = ap.parse_args()

    tfs = (
        [int(x) for x in args.tfs.split(",") if x.strip()]
        if args.tfs
        else [60, 300, 1800, 3600, 7200, 86400]
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
        f"品种 {len(symbols)}，回溯 {args.days} 天，周期 {tfs}，"
        "超严格 = 严格档 + 规则1/2/3/4/6；规则5 后验标记",
        flush=True,
    )
    jobs = [(s, tf, args.days) for tf in tfs for s in symbols]
    strict_n = super_n = r5p = r5f = r5pd = 0
    strict_rows: list[dict] = []
    super_rows: list[dict] = []
    r5_rows: list[dict] = []
    super_all: list[dict] = []
    filtered_all: list[dict] = []
    by_tf: dict[int, list[int]] = {tf: [0, 0, 0, 0, 0] for tf in tfs}
    skipped: dict[int, int] = {}
    done = 0
    t0 = datetime.now()
    with ProcessPoolExecutor(max_workers=args.workers) as ex:
        for (
            tf, _symbol, n_strict, n_super, n_pass, n_fail, n_pending,
            n_bars, rows_strict, rows_super, rows_r5, sup_hits, filt_hits,
        ) in ex.map(_job, jobs, chunksize=2):
            done += 1
            if done % 40 == 0:
                el = (datetime.now() - t0).total_seconds()
                print(f"进度 {done}/{len(jobs)} 已用 {el:.0f}s", flush=True)
            if n_bars < _min_bars(tf):
                skipped[tf] = skipped.get(tf, 0) + 1
            strict_n += n_strict
            super_n += n_super
            r5p += n_pass
            r5f += n_fail
            r5pd += n_pending
            strict_rows.extend(rows_strict)
            super_rows.extend(rows_super)
            r5_rows.extend(rows_r5)
            super_all.extend(sup_hits)
            filtered_all.extend(filt_hits)
            agg = by_tf[tf]
            agg[0] += n_strict
            agg[1] += n_super
            agg[2] += n_pass
            agg[3] += n_fail
            agg[4] += n_pending

    print("\n== 逐周期 ==", flush=True)
    for tf in tfs:
        n_strict, n_super, n_pass, n_fail, n_pending = by_tf[tf]
        label = DURATION_LABELS.get(tf, str(tf))
        skip = f"，{skipped[tf]} 个品种根数不足" if tf in skipped else ""
        print(
            f"  {label}: 严格 {n_strict} → 超严格(1-4/6) {n_super}"
            f"（规则5 过 {n_pass} / 不过 {n_fail} / 待定 {n_pending}）{skip}",
            flush=True,
        )
        if n_strict:
            print(f"    严格: {summarize([r for r in strict_rows if r['tf'] == tf])}", flush=True)
            print(f"    超严格: {summarize([r for r in super_rows if r['tf'] == tf])}", flush=True)
            print(f"    +规则5: {summarize([r for r in r5_rows if r['tf'] == tf])}", flush=True)

    print("\n== 合计 ==", flush=True)
    print(
        f"  严格 {strict_n} → 超严格(1-4/6) {super_n}"
        f"（规则5 过 {r5p} / 不过 {r5f} / 待定 {r5pd}）",
        flush=True,
    )
    print(f"  严格: {summarize(strict_rows)}", flush=True)
    print(f"  超严格: {summarize(super_rows)}", flush=True)
    print(f"  +规则5: {summarize(r5_rows)}", flush=True)

    if args.gallery:
        base = ROOT / "reports" / "backtest_super"
        _render_gallery(
            super_all, base / "hits", "super", "超严格命中",
            args.gallery_max, "超严格命中画廊",
            '<a href="/backtest/super/filtered">→ 被超严格过滤</a>',
        )
        _render_gallery(
            filtered_all, base / "filtered", "super_filtered", "被超严格过滤",
            args.gallery_max, "超严格被过滤画廊",
            '<a href="/backtest/super">← 超严格命中</a>',
        )


if __name__ == "__main__":
    main()