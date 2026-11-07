"""整理甲醇头肩形态完整交易流水（开仓 + 平仓配对）"""
import csv
import json
import re
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CSV_PATH = ROOT / "reports" / "trades_20260615_152244.csv"
OUT_TXT = ROOT / "reports" / "ma_head_shoulders_trades.txt"
OUT_CSV = ROOT / "reports" / "ma_head_shoulders_trades.csv"


def parse_pattern(reason: str) -> dict:
    m = re.match(
        r"头肩(顶|底)右肩 左(\d+) 头(\d+) 右(\d+) 颈(\d+)",
        reason,
    )
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


def fmt_time(t: str) -> str:
    return t[:19].replace("T", " ")


def load_rows() -> list[dict]:
    with CSV_PATH.open(encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def pair_trades(rows: list[dict]) -> list[dict]:
    trades: list[dict] = []
    open_trade: dict | None = None

    for row in rows:
        reason = row.get("reason", "")
        action = row["action"]
        pos_before = int(row["pos_before"])
        volume = int(row["volume"])

        is_hs_entry = "头肩" in reason and action in ("buy", "sell")

        if is_hs_entry and pos_before == 0:
            if open_trade and open_trade.get("exit_time") is None:
                open_trade["exit_time"] = row["time"]
                open_trade["exit_type"] = "未记录平仓(可能换月/回测结束)"
                open_trade["exit_reason"] = "下一笔开仓前仍持仓"
                trades.append(open_trade)
            open_trade = {
                "entry_time": row["time"],
                "entry_action": action,
                "contract": row["trade_symbol"],
                "volume": abs(volume),
                **parse_pattern(reason),
                "entry_reason": reason,
                "exit_time": None,
                "exit_type": None,
                "exit_reason": None,
            }
            continue

        if is_hs_entry and pos_before != 0:
            if open_trade and open_trade.get("exit_time") is None:
                open_trade["exit_time"] = row["time"]
                open_trade["exit_type"] = "反向开仓"
                open_trade["exit_reason"] = reason
                trades.append(open_trade)
            open_trade = {
                "entry_time": row["time"],
                "entry_action": action,
                "contract": row["trade_symbol"],
                "volume": abs(volume),
                **parse_pattern(reason),
                "entry_reason": reason,
                "exit_time": None,
                "exit_type": None,
                "exit_reason": None,
            }
            continue

        if open_trade and action in ("take_profit", "stop_loss"):
            if (pos_before > 0 and open_trade["entry_action"] == "buy") or (
                pos_before < 0 and open_trade["entry_action"] == "sell"
            ):
                open_trade["exit_time"] = row["time"]
                open_trade["exit_type"] = "止盈" if action == "take_profit" else "止损"
                open_trade["exit_reason"] = reason
                trades.append(open_trade)
                open_trade = None

    if open_trade and open_trade.get("exit_time") is None:
        open_trade["exit_time"] = ""
        open_trade["exit_type"] = "回测结束仍持仓"
        open_trade["exit_reason"] = ""
        trades.append(open_trade)

    return trades


def main() -> None:
    rows = load_rows()
    trades = pair_trades(rows)

    tp = sum(1 for t in trades if t["exit_type"] == "止盈")
    sl = sum(1 for t in trades if t["exit_type"] == "止损")
    rev = sum(1 for t in trades if t["exit_type"] == "反向开仓")
    open_end = sum(1 for t in trades if t["exit_type"] == "回测结束仍持仓")
    other = len(trades) - tp - sl - rev - open_end

    lines = [
        "甲醇 MA 头肩策略 — 完整交易流水",
        "回测区间: 2026-01-01 ~ 2026-06-13 | K线: 30分钟 | 手数: 8",
        f"头肩开仓 {len(trades)} 笔 | 止盈 {tp} | 止损 {sl} | 反向开仓 {rev} | 回测末持仓 {open_end}",
        "",
        "序号 | 形态 | 方向 | 合约 | 开仓时间 | 平仓时间 | 结果 | 左/头/右/颈",
        "-" * 110,
    ]

    for i, t in enumerate(trades, 1):
        prices = f"{t.get('left','')}/{t.get('head','')}/{t.get('right','')}/{t.get('neck','')}"
        exit_t = fmt_time(t["exit_time"]) if t["exit_time"] else "—"
        lines.append(
            f"{i:2d} | {t.get('pattern','')} | {t.get('side','')} | {t['contract']} | "
            f"{fmt_time(t['entry_time'])} | {exit_t} | {t['exit_type']} | {prices}"
        )

    lines += ["", "明细", "-" * 110]
    for i, t in enumerate(trades, 1):
        lines.append(f"\n【{i}】{t.get('pattern')} · {t.get('side')} · {t['contract']}")
        lines.append(f"  开仓: {fmt_time(t['entry_time'])}  {t['entry_reason']}")
        if t["exit_time"]:
            lines.append(f"  平仓: {fmt_time(t['exit_time'])}  [{t['exit_type']}] {t['exit_reason']}")
        else:
            lines.append(f"  平仓: [{t['exit_type']}]")

    text = "\n".join(lines) + "\n"
    OUT_TXT.write_text(text, encoding="utf-8")

    fieldnames = [
        "seq", "pattern", "side", "contract", "entry_time", "exit_time",
        "exit_type", "left", "head", "right", "neck", "entry_reason", "exit_reason",
    ]
    with OUT_CSV.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        w.writeheader()
        for i, t in enumerate(trades, 1):
            t["seq"] = i
            w.writerow(t)

    print(f"整理 {len(trades)} 笔 -> {OUT_TXT}")


if __name__ == "__main__":
    main()
