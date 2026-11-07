"""模拟交易入口（回测验证通过后使用）"""

from __future__ import annotations

from datetime import datetime

from tqsdk import TqApi, TqAuth, TqKq

from config.settings import get_max_volume, load_settings
from core.symbol_manager import TradeSymbolManager
from strategies.factory import create_strategy
from utils.symbols import get_display_name, parse_hhmm, should_force_close


def run_simulation() -> None:
    settings = load_settings()
    strategy = create_strategy(settings)
    day_close = parse_hhmm(settings.risk.force_close_day_time)
    night_close = parse_hhmm(settings.risk.force_close_night_time)

    api = TqApi(
        auth=TqAuth(settings.auth.user, settings.auth.password),
        account=TqKq(),
    )

    try:
        manager = TradeSymbolManager(
            api, settings.symbols, settings.backtest.kline_seconds
        )

        print("模拟交易已启动（TqKq 快期模拟账户）")
        print(f"策略: {settings.strategy.name}")
        print(f"品种: {', '.join(get_display_name(s) for s in settings.symbols)}")
        print("按 Ctrl+C 停止")
        print("-" * 50)

        while api.wait_update():
            for main_symbol in settings.symbols:
                klines = manager.klines[main_symbol]

                if not api.is_changing(klines):
                    continue

                bar_time = datetime.fromtimestamp(
                    klines.iloc[-1]["datetime"] / 1e9
                )

                if (
                    settings.risk.enable_force_close
                    and should_force_close(bar_time, day_close, night_close)
                ):
                    manager.flat_all_for_main(main_symbol)
                    continue

                pos = manager.get_position_for_main(main_symbol)
                if hasattr(strategy, "check_exit"):
                    exit_signal = strategy.check_exit(klines, main_symbol, pos)
                    if exit_signal is not None:
                        manager.set_target_for_main(
                            main_symbol, exit_signal.target_volume
                        )
                        continue

                signal = strategy.on_bar(
                    klines,
                    max_volume=get_max_volume(main_symbol),
                    symbol=main_symbol,
                )
                if signal is not None:
                    manager.set_target_for_main(main_symbol, signal.target_volume)

    except KeyboardInterrupt:
        print("\n模拟交易已停止")
    finally:
        api.close()
