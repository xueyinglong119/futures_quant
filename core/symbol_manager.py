"""主连信号 + 具体合约交易的管理器"""

from __future__ import annotations

from tqsdk import TargetPosTask


class TradeSymbolManager:
    """
    主连(KQ.m@)用于 K 线与信号，实际下单使用 quote.underlying_symbol 指向的具体合约。
    主力换月时自动平掉旧合约持仓并切换到新合约。
    """

    def __init__(
        self,
        api,
        main_symbols: list[str],
        kline_seconds: int,
        *,
        use_minute_for_session: bool = False,
    ):
        self.api = api
        self.klines: dict[str, object] = {}
        self.quotes: dict[str, object] = {}
        self.current_trade: dict[str, str] = {}
        self.tasks: dict[str, TargetPosTask] = {}
        self.positions: dict[str, object] = {}

        for main_symbol in main_symbols:
            self.quotes[main_symbol] = api.get_quote(main_symbol)
            if not use_minute_for_session:
                self.klines[main_symbol] = api.get_kline_serial(
                    main_symbol, kline_seconds
                )

    def get_context(self, main_symbol: str):
        quote = self.quotes[main_symbol]
        trade_symbol = quote.underlying_symbol
        if not trade_symbol:
            return None

        old_trade = self.current_trade.get(main_symbol)
        if old_trade and old_trade != trade_symbol:
            if old_trade in self.tasks:
                self.tasks[old_trade].set_target_volume(0)

        self.current_trade[main_symbol] = trade_symbol
        if trade_symbol not in self.tasks:
            support_min_vol = trade_symbol.startswith("CZCE.")
            self.tasks[trade_symbol] = TargetPosTask(
                self.api,
                trade_symbol,
                support_open_min_volume=support_min_vol,
            )
            self.positions[trade_symbol] = self.api.get_position(trade_symbol)

        return self.tasks[trade_symbol], self.positions[trade_symbol], trade_symbol

    def flat_all_for_main(self, main_symbol: str) -> int:
        """平掉某主连当前对应具体合约的持仓，返回平仓前净持仓"""
        ctx = self.get_context(main_symbol)
        if ctx is None:
            return 0

        task, position, _ = ctx
        pos_before = position.pos
        if pos_before != 0:
            task.set_target_volume(0)
        return pos_before

    def set_target_for_main(self, main_symbol: str, volume: int) -> tuple[int, str] | None:
        ctx = self.get_context(main_symbol)
        if ctx is None:
            return None

        task, position, trade_symbol = ctx
        pos_before = position.pos
        task.set_target_volume(volume)
        return pos_before, trade_symbol

    def get_position_for_main(self, main_symbol: str) -> int:
        ctx = self.get_context(main_symbol)
        if ctx is None:
            return 0
        _, position, _ = ctx
        return position.pos
