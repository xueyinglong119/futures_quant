"""2026 国内 30 分钟 K 线头肩形态回测（分品种 + 合计）"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from config.settings import load_settings
from core.backtest import BacktestEngine
from utils.symbols import get_display_name

SYMBOLS = [
    ("KQ.m@SHFE.fu", "FU"),
    ("KQ.m@INE.lu", "LU"),
    ("KQ.m@CZCE.MA", "MA"),
]


def main() -> None:
    base = load_settings()
    base.backtest.session_aligned = True

    lines: list[str] = [
        f"回测区间: {base.backtest.start} ~ {base.backtest.end}",
        "K线: 30 分钟(国内小节休市规则)",
        "策略: 头肩顶/底",
        "-" * 50,
    ]

    combined_profit = 0.0
    combined_trades = 0

    for symbol, short in SYMBOLS:
        settings = load_settings()
        settings.symbols = [symbol]
        settings.backtest.session_aligned = True
        result = BacktestEngine(settings).run()
        profit = result["profit"]
        ret = profit / result["start_balance"] * 100
        trades = result["trade_count"]
        combined_profit += profit
        combined_trades += trades
        lines.append(
            f"{short} ({get_display_name(symbol)}): "
            f"盈亏 {profit:+.2f}  收益率 {ret:+.2f}%  信号 {trades} 次"
        )

    lines.extend(
        [
            "-" * 50,
            f"分品种合计(独立账户): 盈亏 {combined_profit:+.2f}  信号 {combined_trades} 次",
            "",
            "说明: 上表为各品种单独 1000 万资金回测；合并账户请运行 python main_backtest.py",
        ]
    )

    report = "\n".join(lines)
    out = base.reports_dir / "backtest_2026_session.txt"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(report, encoding="utf-8")
    print("\n" + report)
    print(f"\n报告已保存: {out}")


if __name__ == "__main__":
    main()
