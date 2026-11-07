"""采集燃油 2701 最近 15 天 session 30m K 线，扫描头肩顶/底并入库"""

from __future__ import annotations

import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from config.settings import load_mysql_config, load_settings
from core.historical_session_klines import fetch_session_bars
from core.mysql_kline_store import connect, ensure_table, upsert_bars
from strategies.factory import create_strategy

SYMBOL = "SHFE.fu2701"
LOOKBACK_DAYS = 15
# 形态最长 60 根 30m ≈ 约 5 个交易日；再加拐点确认缓冲，多拉 20 天预热
WARMUP_DAYS = 20


def _bar_dt(bar: dict) -> datetime:
    return datetime.fromisoformat(bar["datetime_end"])


def main() -> None:
    settings = load_settings()
    end = date.today()
    window_start = end - timedelta(days=LOOKBACK_DAYS - 1)
    fetch_start = window_start - timedelta(days=WARMUP_DAYS)

    print(f"拉取 {SYMBOL} {fetch_start} ~ {end}（分析窗口 {window_start} ~ {end}）...")
    bars = fetch_session_bars(SYMBOL, fetch_start, end, settings)
    print(f"合成 session K 线 {len(bars)} 根")

    if not bars:
        print("无数据，退出")
        return

    # 入库（最近 15 天 + 预热一并写入，便于核对）
    cfg = load_mysql_config()
    conn = connect(cfg)
    try:
        ensure_table(conn)
        n = upsert_bars(conn, SYMBOL, bars)
        print(f"已写入 MySQL {cfg.database}.session_kline_30m: {n} 条")
    finally:
        conn.close()

    # 逐根扫描头肩（与回测策略参数一致）
    strategy = create_strategy(settings)
    df = pd.DataFrame(
        [
            {
                "datetime": b["datetime_end"],
                "open": b["open"],
                "high": b["high"],
                "low": b["low"],
                "close": b["close"],
                "volume": b["volume"],
                "open_oi": b["open_oi"],
            }
            for b in bars
        ]
    )

    hits: list[dict] = []
    for i in range(1, len(df) + 1):
        window = df.iloc[:i]
        signal = strategy.on_bar(window, max_volume=1, symbol=SYMBOL)
        if signal is None:
            continue
        confirm_dt = datetime.fromisoformat(str(window.iloc[-1]["datetime"]))
        if confirm_dt.date() < window_start:
            continue
        kind = "头肩顶" if signal.action == "sell" else "头肩底"
        hits.append(
            {
                "confirm_time": confirm_dt.isoformat(timespec="seconds"),
                "trade_date": confirm_dt.date().isoformat(),
                "kind": kind,
                "action": signal.action,
                "reason": signal.reason,
                "close": float(window.iloc[-1]["close"]),
            }
        )

    out = ROOT / "reports" / "fu2701_hs_last15d.txt"
    lines = [
        f"合约: {SYMBOL}",
        f"采集区间: {fetch_start} ~ {end}",
        f"分析窗口: {window_start} ~ {end}（最近 {LOOKBACK_DAYS} 天）",
        f"K线: 国内规则 30 分钟 | 策略: head_shoulders（右肩确认）",
        f"总 K 线: {len(bars)} | 窗口内确认形态: {len(hits)}",
        "",
        "序号 | 确认日期 | 确认时刻 | 形态 | 收盘 | 细节",
        "-" * 100,
    ]
    for i, h in enumerate(hits, 1):
        lines.append(
            f"{i:2d} | {h['trade_date']} | {h['confirm_time']} | {h['kind']} | "
            f"{h['close']:.0f} | {h['reason']}"
        )
    if not hits:
        lines.append("（最近 15 天内未确认到头肩顶/头肩底）")

    text = "\n".join(lines) + "\n"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")
    print(text)
    print(f"报告: {out}")


if __name__ == "__main__":
    main()
