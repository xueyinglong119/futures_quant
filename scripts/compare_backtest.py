import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from config.settings import load_settings
from core.backtest import BacktestEngine

out = ROOT / "reports" / "compare_summary.txt"
lines: list[str] = []

for sym, name in [("KQ.m@CZCE.MA", "MA"), ("KQ.m@SHFE.fu", "FU")]:
    s = load_settings()
    s.symbols = [sym]
    r = BacktestEngine(s).run()
    pct = r["profit"] / r["start_balance"] * 100
    line = (
        f"{name}: profit={r['profit']:.0f} ({pct:.2f}%) "
        f"trades={r['trade_count']} end={r['end_balance']:.0f}"
    )
    lines.append(line)
    print(line, flush=True)

out.parent.mkdir(parents=True, exist_ok=True)
out.write_text("\n".join(lines) + "\n", encoding="utf-8")
print(f"summary saved: {out}", flush=True)
