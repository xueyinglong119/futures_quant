"""收盘后校验入库 K 线准确性（可手动跑）。

默认对照刚结束时段自动取窗，发现问题会修复。
仅检查：加 --check-only。

示例:
  python scripts/audit_session_klines.py
  python scripts/audit_session_klines.py --check-only
  python scripts/audit_session_klines.py --symbols KQ.m@SHFE.fu
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.kline_audit import run_post_close_audit  # noqa: E402
from core.kline_store import INGEST_DURATIONS, LABEL_TO_DURATION  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description="收盘后校验/修复入库 K 线")
    parser.add_argument(
        "--symbols",
        default=None,
        help="逗号分隔主连，默认全部商品主力",
    )
    parser.add_argument(
        "--durations",
        default=None,
        help="逗号分隔入库周期，默认全部 INGEST_DURATIONS",
    )
    parser.add_argument(
        "--check-only",
        action="store_true",
        help="只检查不修复",
    )
    parser.add_argument(
        "--report-dir",
        default=str(ROOT / "reports"),
        help="报告目录",
    )
    args = parser.parse_args()

    symbols = None
    if args.symbols:
        symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]

    durations = list(INGEST_DURATIONS)
    if args.durations:
        durations = []
        for part in args.durations.split(","):
            key = part.strip().lower()
            if key not in LABEL_TO_DURATION:
                raise SystemExit(f"未知周期: {key}")
            durations.append(LABEL_TO_DURATION[key])

    report = run_post_close_audit(
        symbols=symbols,
        fix=not args.check_only,
        report_dir=Path(args.report_dir),
        durations=durations,
    )
    raise SystemExit(0 if report.ok else 1)


if __name__ == "__main__":
    main()
