from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from config.settings import load_settings
from core.mysql_kline_store import connect
from strategies.factory import create_strategy

conn = connect()
cur = conn.cursor()
cur.execute(
    "SELECT datetime_end, open, high, low, close FROM session_kline_30m "
    "WHERE symbol=%s ORDER BY datetime_end",
    ("SHFE.fu2701",),
)
rows = cur.fetchall()
conn.close()

df = pd.DataFrame(rows, columns=["datetime", "open", "high", "low", "close"])
df["datetime"] = pd.to_datetime(df["datetime"])

strategy = create_strategy(load_settings())
target = pd.Timestamp("2026-10-20 10:00:00")
idx = df.index[df["datetime"] == target][0]
window = df.iloc[: idx + 1].reset_index(drop=True)

ph = strategy._find_pivot_highs(window.high)
pl = strategy._find_pivot_lows(window.low)
last_idx = len(window) - 1

for i in range(len(pl) - 2):
    idx_l, price_l = pl[i]
    idx_h, price_h = pl[i + 1]
    idx_r, price_r = pl[i + 2]
    span = idx_r - idx_l
    if span < strategy.min_span_bars or span > strategy.max_span_bars:
        continue
    if not (price_h < price_l and price_h < price_r):
        continue
    if not strategy._shoulders_balanced(price_l, price_r, price_h):
        continue
    if not strategy._right_shoulder_not_too_high(price_l, price_r, price_h):
        continue
    neck = strategy._neckline_for_bottom(ph, idx_l, idx_h, idx_r, window.close)
    if neck is None or not strategy._head_neck_valid_bottom(price_h, neck):
        continue
    if not strategy._right_shoulder_confirmed(idx_r, last_idx):
        continue

    print("MATCHED bottom:")
    print("  L", window.iloc[idx_l]["datetime"], price_l)
    print("  H", window.iloc[idx_h]["datetime"], price_h)
    print("  R", window.iloc[idx_r]["datetime"], price_r)
    print("  neck", round(neck, 1), "confirm", window.iloc[last_idx]["datetime"])
    for j in range(idx_l, last_idx + 1):
        r = window.iloc[j]
        mark = ""
        if j == idx_l:
            mark = " <<L"
        if j == idx_h:
            mark = " <<HEAD"
        if j == idx_r:
            mark = " <<R"
        if j == last_idx:
            mark += " <<CONFIRM"
        print(
            f"  {r['datetime']} H{r['high']:.0f} L{r['low']:.0f} C{r['close']:.0f}{mark}"
        )

# also top on Oct 15
target2 = pd.Timestamp("2026-10-15 22:00:00")
idx2 = df.index[df["datetime"] == target2][0]
window2 = df.iloc[: idx2 + 1].reset_index(drop=True)
ph2 = strategy._find_pivot_highs(window2.high)
pl2 = strategy._find_pivot_lows(window2.low)
last2 = len(window2) - 1
print("\n--- Oct15 top ---")
for i in range(len(ph2) - 2):
    idx_l, price_l = ph2[i]
    idx_h, price_h = ph2[i + 1]
    idx_r, price_r = ph2[i + 2]
    span = idx_r - idx_l
    if span < strategy.min_span_bars or span > strategy.max_span_bars:
        continue
    if not (price_h > price_l and price_h > price_r):
        continue
    if not strategy._shoulders_balanced(price_l, price_r, price_h):
        continue
    if not strategy._right_shoulder_not_too_high(price_l, price_r, price_h):
        continue
    neck = strategy._neckline_for_top(pl2, idx_l, idx_h, idx_r, window2.close)
    if neck is None or not strategy._head_neck_valid_top(price_h, neck):
        continue
    if not strategy._right_shoulder_confirmed(idx_r, last2):
        continue
    print("MATCHED top:")
    print("  L", window2.iloc[idx_l]["datetime"], price_l)
    print("  H", window2.iloc[idx_h]["datetime"], price_h)
    print("  R", window2.iloc[idx_r]["datetime"], price_r)
    print("  neck", round(neck, 1))
