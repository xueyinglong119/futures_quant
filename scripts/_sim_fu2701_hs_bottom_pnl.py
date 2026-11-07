"""按策略规则回放 2026-10-20 头肩底这笔：开仓→止盈/止损，估算盈亏"""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from config.settings import load_settings
from core.mysql_kline_store import connect
from strategies.factory import create_strategy

SYMBOL = "SHFE.fu2701"
# 燃油期货：10吨/手；开平手续费近似按交易所常见水平，仅作估算
MULTIPLIER = 10
# 回测用 TargetPosTask 实际手续费以 TqSdk 为准；这里给出价差盈亏为主


def main() -> None:
    conn = connect()
    cur = conn.cursor()
    cur.execute(
        "SELECT datetime_end, open, high, low, close FROM session_kline_30m "
        "WHERE symbol=%s ORDER BY datetime_end",
        (SYMBOL,),
    )
    rows = cur.fetchall()
    conn.close()

    df = pd.DataFrame(rows, columns=["datetime", "open", "high", "low", "close"])
    df["datetime"] = pd.to_datetime(df["datetime"])

    settings = load_settings()
    hs = settings.strategy.head_shoulders
    strategy = create_strategy(settings)

    entry_time = pd.Timestamp("2026-10-20 10:00:00")
    entry_i = df.index[df["datetime"] == entry_time][0]

    # 重建到开仓时刻的信号与止盈止损
    window = df.iloc[: entry_i + 1].reset_index(drop=True)
    signal = strategy.on_bar(window, max_volume=1, symbol=SYMBOL)
    meta = strategy._active_trades[SYMBOL]
    entry_price = float(window.iloc[-1]["close"])

    print("=== 开仓（策略逻辑）===")
    print("时间:", entry_time)
    print("信号:", signal.action, signal.reason)
    print("开仓价(按当根收盘):", entry_price)
    print("止损:", round(meta.stop, 1), "  止盈(颈线):", round(meta.target, 1))
    print("头:", meta.head, "  参数 stop_beyond_head_ratio=", hs.stop_beyond_head_ratio)

    # 从下一根开始检查出场
    exit_time = None
    exit_price = None
    exit_reason = None
    for j in range(entry_i + 1, len(df)):
        bar = df.iloc[j]
        high = float(bar["high"])
        low = float(bar["low"])
        # 多头：先判止损再判止盈（与策略 check_exit 一致）
        if low <= meta.stop:
            exit_time = bar["datetime"]
            exit_price = meta.stop
            exit_reason = "止损"
            break
        if hs.take_profit_at_neck and high >= meta.target:
            exit_time = bar["datetime"]
            exit_price = meta.target
            exit_reason = "止盈颈线"
            break

    if exit_time is None:
        last = df.iloc[-1]
        exit_time = last["datetime"]
        exit_price = float(last["close"])
        exit_reason = "数据末仍持仓(按收盘估算)"

    points = exit_price - entry_price
    pnl = points * MULTIPLIER * 1  # 1 手

    print("\n=== 平仓 ===")
    print("时间:", exit_time)
    print("原因:", exit_reason)
    print("平仓价:", round(exit_price, 1))
    print("价差(点):", round(points, 1))
    print(f"合约乘数: {MULTIPLIER} 吨/手 × 1 手")
    print(f"毛盈亏(未扣手续费): {pnl:.0f} 元")

    # 若实际曾摸到更高/更低，补充路径信息
    path = df.iloc[entry_i : (df.index[df["datetime"] == exit_time][0] + 1)]
    print("\n持仓期间 high/low:", float(path["high"].max()), "/", float(path["low"].min()))
    print("持仓 K 线根数:", len(path))


if __name__ == "__main__":
    main()
