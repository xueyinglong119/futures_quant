"""回测引擎"""

from __future__ import annotations

import csv
from datetime import datetime

from zoneinfo import ZoneInfo

from tqsdk import TqApi, TqAuth, TqBacktest
from tqsdk.exceptions import BacktestFinished

from config.settings import Settings, get_max_volume, load_settings
from core.session_klines import SessionKlineFeed, quote_datetime_to_ns
from core.symbol_manager import TradeSymbolManager
from strategies.factory import create_strategy
from utils.symbols import get_display_name, parse_hhmm, should_force_close

TZ_SHANGHAI = ZoneInfo("Asia/Shanghai")


class BacktestEngine:
    def __init__(self, settings: Settings | None = None):
        self.settings = settings or load_settings()
        self.strategy = create_strategy(self.settings)
        risk = self.settings.risk
        self.force_close_enabled = risk.enable_force_close
        self.day_close = parse_hhmm(risk.force_close_day_time)
        self.night_close = parse_hhmm(risk.force_close_night_time)

    def run(self) -> dict:
        settings = self.settings
        symbols = settings.symbols

        api = TqApi(
            auth=TqAuth(settings.auth.user, settings.auth.password),
            backtest=TqBacktest(
                start_dt=settings.backtest.start,
                end_dt=settings.backtest.end,
            ),
        )

        trade_log: list[dict] = []
        start_balance: float | None = None

        try:
            session_aligned = settings.backtest.session_aligned
            manager = TradeSymbolManager(
                api,
                symbols,
                settings.backtest.kline_seconds,
                use_minute_for_session=session_aligned,
            )
            kline_feed = (
                SessionKlineFeed(api, symbols) if session_aligned else None
            )

            kline_desc = (
                "30 分钟(国内小节休市规则)"
                if session_aligned
                else f"{settings.backtest.kline_seconds // 60} 分钟(TqSdk原生)"
            )
            print(f"回测区间: {settings.backtest.start} ~ {settings.backtest.end}")
            print(f"K线周期: {kline_desc}")
            print(f"交易品种: {', '.join(get_display_name(s) for s in symbols)}")
            print(f"策略: {settings.strategy.name}")
            print("信号源: 主连 | 交易: 具体合约 (underlying_symbol)")
            print("-" * 50)

            try:
                while api.wait_update():
                    account = api.get_account()
                    if start_balance is None:
                        start_balance = account.balance

                    for main_symbol in symbols:
                        if session_aligned:
                            assert kline_feed is not None
                            quote = manager.quotes[main_symbol]
                            current_ns = quote_datetime_to_ns(quote.datetime)
                            bar_iter = kline_feed.iter_new_bars(main_symbol, current_ns)
                        else:
                            klines = manager.klines[main_symbol]
                            if not api.is_changing(klines):
                                continue
                            bar_iter = [klines]

                        for klines in bar_iter:
                            bar_time = datetime.fromtimestamp(
                                klines.iloc[-1]["datetime"] / 1e9, tz=TZ_SHANGHAI
                            ).replace(tzinfo=None)
                            in_backtest_range = (
                                settings.backtest.start
                                <= bar_time.date()
                                <= settings.backtest.end
                            )

                            if not in_backtest_range:
                                continue

                            if self.force_close_enabled and should_force_close(
                                bar_time, self.day_close, self.night_close
                            ):
                                pos_before = manager.flat_all_for_main(main_symbol)
                                if pos_before != 0:
                                    trade_log.append(
                                        {
                                            "time": bar_time.isoformat(),
                                            "main_symbol": main_symbol,
                                            "trade_symbol": manager.current_trade.get(
                                                main_symbol, ""
                                            ),
                                            "action": "force_close",
                                            "volume": 0,
                                            "pos_before": pos_before,
                                        }
                                    )
                                continue

                            pos = manager.get_position_for_main(main_symbol)
                            if hasattr(self.strategy, "check_exit"):
                                exit_signal = self.strategy.check_exit(
                                    klines, main_symbol, pos
                                )
                                if exit_signal is not None:
                                    result = manager.set_target_for_main(
                                        main_symbol, exit_signal.target_volume
                                    )
                                    if result and exit_signal.target_volume != result[0]:
                                        trade_log.append(
                                            {
                                                "time": bar_time.isoformat(),
                                                "main_symbol": main_symbol,
                                                "trade_symbol": result[1],
                                                "action": exit_signal.action,
                                                "volume": exit_signal.target_volume,
                                                "pos_before": result[0],
                                                "reason": exit_signal.reason,
                                            }
                                        )
                                    continue

                            signal = self.strategy.on_bar(
                                klines,
                                max_volume=get_max_volume(main_symbol),
                                symbol=main_symbol,
                            )
                            if signal is None:
                                continue

                            result = manager.set_target_for_main(
                                main_symbol, signal.target_volume
                            )
                            if result is None:
                                continue

                            pos_before, trade_symbol = result
                            if signal.target_volume != pos_before:
                                trade_log.append(
                                    {
                                        "time": bar_time.isoformat(),
                                        "main_symbol": main_symbol,
                                        "trade_symbol": trade_symbol,
                                        "action": signal.action,
                                        "volume": signal.target_volume,
                                        "pos_before": pos_before,
                                        "reason": signal.reason,
                                    }
                                )
            except BacktestFinished:
                pass

            account = api.get_account()
            initial = start_balance if start_balance is not None else account.balance
            result = {
                "start_balance": initial,
                "end_balance": account.balance,
                "profit": account.balance - initial,
                "trade_count": len(trade_log),
                "trade_log": trade_log,
            }

            self._save_trade_log(trade_log)
            self._print_summary(result, trade_log)
            return result

        finally:
            api.close()

    def _save_trade_log(self, trade_log: list[dict]) -> None:
        reports_dir = self.settings.reports_dir
        reports_dir.mkdir(parents=True, exist_ok=True)

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = reports_dir / f"trades_{timestamp}.csv"

        if not trade_log:
            print("无交易记录，跳过报告写入")
            return

        with path.open("w", newline="", encoding="utf-8-sig") as f:
            writer = csv.DictWriter(f, fieldnames=trade_log[0].keys())
            writer.writeheader()
            writer.writerows(trade_log)

        print(f"交易记录已保存: {path}")

    def _print_summary(self, result: dict, trade_log: list[dict]) -> None:
        print("-" * 50)
        print("回测结果摘要")
        print(f"  初始资金: {result['start_balance']:.2f}")
        print(f"  最终资金: {result['end_balance']:.2f}")
        print(f"  盈亏:     {result['profit']:.2f}")
        print(f"  收益率:   {result['profit'] / result['start_balance'] * 100:.2f}%")
        print(f"  信号次数: {result['trade_count']}")

        by_symbol: dict[str, int] = {}
        for row in trade_log:
            key = row["main_symbol"]
            by_symbol[key] = by_symbol.get(key, 0) + 1

        if by_symbol:
            print("  各品种信号次数:")
            for symbol, count in by_symbol.items():
                print(f"    {get_display_name(symbol)}: {count}")
