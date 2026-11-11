"""渲染改后（strict）命中的形态图，供目视检查信噪比。"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from audit_killed_samples import replay_detail
from core.hs_chart import render_hs_chart
from core.kline_store import connect, load_bars


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=15)
    ap.add_argument("--symbols", type=str, required=True)
    ap.add_argument("--tf", type=int, default=60)
    ap.add_argument("--outdir", type=str, default="_hits")
    args = ap.parse_args()

    outdir = ROOT / args.outdir
    outdir.mkdir(parents=True, exist_ok=True)
    conn = connect()
    cutoff = datetime.now() - timedelta(days=args.days)
    meta = []
    for symbol in [s.strip() for s in args.symbols.split(",") if s.strip()]:
        bars = load_bars(conn, symbol, args.tf, start=cutoff)
        if len(bars) < 120:
            continue
        hits = replay_detail(symbol, args.tf, bars, True)
        print(f"{symbol}: 改后命中 {len(hits)}", flush=True)
        for k, s in enumerate(hits, 1):
            png = render_hs_chart(symbol, args.tf, s["when"], s["detail"], conn)
            if not png:
                continue
            name = f"hit_{symbol.replace('@', '_').replace('.', '')}_{k:02d}.png"
            (outdir / name).write_bytes(png)
            meta.append({"file": name, "symbol": symbol, "when": s["when"], "reason": s["reason"]})
            print(f"    {name} {s['when']}", flush=True)
    (outdir / f"meta_{args.symbols.split(',')[0].split('.')[-1]}.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    conn.close()


if __name__ == "__main__":
    main()
