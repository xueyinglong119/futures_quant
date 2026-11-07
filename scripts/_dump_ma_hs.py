import json
import logging
import sys
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
STATUS = ROOT / "reports" / "_status.txt"

def log(msg: str) -> None:
    STATUS.parent.mkdir(parents=True, exist_ok=True)
    with STATUS.open("a", encoding="utf-8") as f:
        f.write(msg + "\n")

try:
    log("start")
    logging.getLogger("tqsdk").setLevel(logging.CRITICAL)

    from config.settings import load_settings
    from core.backtest import BacktestEngine

    s = load_settings()
    s.symbols = ["KQ.m@CZCE.MA"]
    log("before run")
    r = BacktestEngine(s).run()
    log(f"after run keys={list(r.keys())} trade_count={r['trade_count']}")

    entries = [
        row
        for row in r.get("trade_log", [])
        if "头肩" in row.get("reason", "") and row["action"] in ("buy", "sell")
    ]
    exits = [
        row
        for row in r.get("trade_log", [])
        if "头肩" in row.get("reason", "") and row["action"] not in ("buy", "sell")
    ]
    log(f"entries={len(entries)} exits={len(exits)}")

    out_json = ROOT / "reports" / "ma_head_shoulders.json"
    out_txt = ROOT / "reports" / "ma_head_shoulders_times.txt"
    out_json.write_text(
        json.dumps({"entries": entries, "exits": exits}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    lines = [
        f"回测: {s.backtest.start} ~ {s.backtest.end}",
        f"开仓 {len(entries)} 次",
        "",
    ]
    for i, row in enumerate(entries, 1):
        tag = "头肩顶·空" if row["action"] == "sell" else "头肩底·多"
        lines.append(
            f"{i}. {row['time'][:19]} | {tag} | {row['trade_symbol']} | {row['reason']}"
        )
    if exits:
        lines.append("")
        lines.append("平仓:")
        for i, row in enumerate(exits, 1):
            lines.append(f"{i}. {row['time'][:19]} | {row['action']} | {row['reason']}")

    out_txt.write_text("\n".join(lines) + "\n", encoding="utf-8")
    log(f"written {out_txt}")
    print("OK", len(entries), str(out_txt))
except Exception:
    log(traceback.format_exc())
    raise
