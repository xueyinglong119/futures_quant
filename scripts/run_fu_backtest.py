import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from config.settings import load_settings
from core.backtest import BacktestEngine

s = load_settings()
s.symbols = ["KQ.m@SHFE.fu"]
r = BacktestEngine(s).run()
pct = r["profit"] / r["start_balance"] * 100
print(f"FU: profit={r['profit']:.0f} ({pct:.2f}%) trades={r['trade_count']} end={r['end_balance']:.0f}")
