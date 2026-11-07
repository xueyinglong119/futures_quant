"""列出甲醇 MA 回测中触发的头肩形态时间"""
import contextlib
import io
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

logging.getLogger("tqsdk").setLevel(logging.CRITICAL)

from config.settings import load_settings
from core.backtest import BacktestEngine

s = load_settings()
s.symbols = ["KQ.m@CZCE.MA"]

with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
    r = BacktestEngine(s).run()

entries: list[dict] = []
exits: list[dict] = []
for row in r["trade_log"]:
    reason = row.get("reason", "")
    if "头肩" not in reason:
        continue
    if row["action"] in ("buy", "sell"):
        entries.append(row)
    else:
        exits.append(row)

out = ROOT / "reports" / "ma_head_shoulders_times.txt"
lines = [
    f"回测区间: {s.backtest.start} ~ {s.backtest.end}",
    f"品种: 甲醇 MA (KQ.m@CZCE.MA)",
    f"K线: 30分钟 | 策略: 头肩顶/底（右肩确认开仓）",
    f"头肩开仓次数: {len(entries)}",
    "",
    "序号 | 时间 | 方向 | 合约 | 左肩/头/右肩/颈线",
    "-" * 90,
]
for i, row in enumerate(entries, 1):
    direction = "做空·头肩顶" if row["action"] == "sell" else "做多·头肩底"
    lines.append(
        f"{i:2d} | {row['time'][:19]} | {direction} | {row['trade_symbol']} | {row['reason']}"
    )

if exits:
    lines += ["", "平仓记录", "-" * 90]
    for i, row in enumerate(exits, 1):
        lines.append(
            f"{i:2d} | {row['time'][:19]} | {row['action']} | {row['trade_symbol']} | {row['reason']}"
        )

text = "\n".join(lines) + "\n"
out.parent.mkdir(parents=True, exist_ok=True)
out.write_text(text, encoding="utf-8")
print(f"头肩开仓 {len(entries)} 次，平仓 {len(exits)} 次")
print(f"已保存: {out}")
