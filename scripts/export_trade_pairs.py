"""将回测交易流水整理为开平仓配对表"""

from __future__ import annotations

import argparse
import csv
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from utils.symbols import get_display_name

PATTERN_RE = re.compile(r"头肩(顶|底)右肩 左(\d+) 头(\d+) 右(\d+) 颈(\d+)")


def parse_pattern(reason: str) -> dict:
    m = PATTERN_RE.match(reason)
    if not m:
        return {}
    kind, left, head, right, neck = m.groups()
    return {
        "pattern": f"头肩{'顶' if kind == '顶' else '底'}",
        "side": "空" if kind == "顶" else "多",
        "left": int(left),
        "head": int(head),
        "right": int(right),
        "neck": int(neck),
    }


def pair_trades(rows: list[dict]) -> list[dict]:
    """按 main_symbol 分别维护持仓，生成开平仓配对。"""
    open_by_symbol: dict[str, dict | None] = {}
    trades: list[dict] = []

    def flush_open(main_symbol: str, exit_row: dict | None, exit_type: str, exit_reason: str) -> None:
        open_trade = open_by_symbol.get(main_symbol)
        if open_trade is None:
            return
        if exit_row is not None:
            open_trade["exit_time"] = exit_row["time"]
            open_trade["exit_contract"] = exit_row.get("trade_symbol", "")
            open_trade["exit_action"] = exit_row.get("action", "")
        else:
            open_trade["exit_time"] = ""
            open_trade["exit_contract"] = ""
            open_trade["exit_action"] = ""
        open_trade["exit_type"] = exit_type
        open_trade["exit_reason"] = exit_reason
        trades.append(open_trade)
        open_by_symbol[main_symbol] = None

    for row in rows:
        main_symbol = row["main_symbol"]
        reason = row.get("reason", "")
        action = row["action"]
        pos_before = int(row["pos_before"])
        volume = int(row["volume"])
        is_hs_entry = "头肩" in reason and action in ("buy", "sell")

        if is_hs_entry:
            if open_by_symbol.get(main_symbol) is not None:
                if pos_before == 0:
                    flush_open(
                        main_symbol,
                        row,
                        "未记录平仓",
                        "下一笔开仓前仍标记持仓",
                    )
                else:
                    flush_open(main_symbol, row, "反向开仓", reason)

            open_by_symbol[main_symbol] = {
                "main_symbol": main_symbol,
                "symbol_name": get_display_name(main_symbol),
                "entry_time": row["time"],
                "entry_action": action,
                "entry_contract": row["trade_symbol"],
                "volume": abs(volume),
                **parse_pattern(reason),
                "entry_reason": reason,
            }
            continue

        if action in ("take_profit", "stop_loss", "force_close"):
            open_trade = open_by_symbol.get(main_symbol)
            if open_trade is None:
                continue
            if (pos_before > 0 and open_trade["entry_action"] == "buy") or (
                pos_before < 0 and open_trade["entry_action"] == "sell"
            ):
                exit_type = {
                    "take_profit": "止盈",
                    "stop_loss": "止损",
                    "force_close": "强制平仓",
                }[action]
                flush_open(main_symbol, row, exit_type, reason)

    for main_symbol, open_trade in open_by_symbol.items():
        if open_trade is not None:
            flush_open(main_symbol, None, "回测结束仍持仓", "")

    trades.sort(key=lambda t: (t["entry_time"], t["main_symbol"]))
    for i, t in enumerate(trades, 1):
        t["seq"] = i
    return trades


def export_pairs(input_path: Path, output_path: Path) -> list[dict]:
    with input_path.open(encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))

    trades = pair_trades(rows)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    fieldnames = [
        "seq",
        "symbol_name",
        "main_symbol",
        "pattern",
        "side",
        "volume",
        "entry_contract",
        "entry_time",
        "exit_contract",
        "exit_time",
        "exit_type",
        "left",
        "head",
        "right",
        "neck",
        "entry_reason",
        "exit_reason",
    ]
    with output_path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(trades)

    return trades


def main() -> None:
    parser = argparse.ArgumentParser(description="导出开平仓配对表")
    parser.add_argument(
        "-i",
        "--input",
        type=Path,
        default=ROOT / "reports" / "trades_20260630_172805.csv",
        help="原始交易流水 CSV",
    )
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        default=ROOT / "reports" / "backtest_2026_session_pairs.csv",
        help="配对表输出路径",
    )
    args = parser.parse_args()

    trades = export_pairs(args.input, args.output)

    by_symbol: dict[str, int] = {}
    by_exit: dict[str, int] = {}
    for t in trades:
        by_symbol[t["symbol_name"]] = by_symbol.get(t["symbol_name"], 0) + 1
        by_exit[t["exit_type"]] = by_exit.get(t["exit_type"], 0) + 1

    print(f"配对完成: {len(trades)} 笔 -> {args.output}")
    print("各品种:", ", ".join(f"{k} {v}" for k, v in sorted(by_symbol.items())))
    print(
        "平仓类型:",
        ", ".join(f"{k} {v}" for k, v in sorted(by_exit.items())),
    )


if __name__ == "__main__":
    main()
