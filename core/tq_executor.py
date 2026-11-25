"""快期模拟账户（TqKq）下单桥。

MySQL `paper_trade` 仍是策略账本（手数、止盈止损、看板）；
本模块把**严格档**持仓的净手数同步到天勤 `TargetPosTask`，由 TqKq 真实发单。
同一合约多周期持仓合并为净仓：多 +lots、空 −lots。
"""

from __future__ import annotations

import time
from typing import Any

from tqsdk import TargetPosTask


def is_strict_tq_kind(kind: str) -> bool:
    """仅「·严格」同步到天勤；超严格 / 手动 / 常规不入账。"""
    text = kind or ""
    return "·严格" in text and "超严格" not in text


class TqKqExecutor:
    def __init__(self, api: Any):
        self.api = api
        self.tasks: dict[str, TargetPosTask] = {}
        self._last_target: dict[str, int] = {}

    def resolve_trade_symbol(self, symbol: str) -> str | None:
        """月约直接下；主连 KQ.m@ 取 underlying_symbol。"""
        if not symbol.startswith("KQ.m@"):
            return symbol
        try:
            quote = self.api.get_quote(symbol)
            self.pump(1.0)
            underlying = getattr(quote, "underlying_symbol", None)
            if not underlying:
                raw = getattr(self.api, "_data", {}).get("quotes", {}).get(symbol, {})
                underlying = raw.get("underlying_symbol")
            if isinstance(underlying, str) and underlying:
                return underlying
        except Exception as exc:  # noqa: BLE001
            print(f"[tq] 解析主连标的失败 {symbol}: {exc}", flush=True)
        return None

    def _task(self, trade_symbol: str) -> TargetPosTask:
        task = self.tasks.get(trade_symbol)
        if task is not None:
            return task
        task = TargetPosTask(
            self.api,
            trade_symbol,
            support_open_min_volume=trade_symbol.startswith("CZCE."),
        )
        self.tasks[trade_symbol] = task
        return task

    def set_net(self, symbol: str, net_lots: int) -> str | None:
        """把 strategy 合约的目标净仓同步到天勤，返回实际下单合约。"""
        trade = self.resolve_trade_symbol(symbol)
        if not trade:
            print(f"[tq] 跳过 {symbol}：没有可下单合约", flush=True)
            return None
        target = int(net_lots)
        if self._last_target.get(trade) == target:
            return trade
        self._task(trade).set_target_volume(target)
        self._last_target[trade] = target
        print(f"[tq] 目标仓位 {trade} = {target}（来自 {symbol}）", flush=True)
        # TargetPosTask 只在 wait_update 里真正发单
        self.pump(2.0)
        return trade

    def pump(self, seconds: float = 0.25) -> None:
        """推进天勤事件循环，让挂单/成交继续走。"""
        if seconds <= 0:
            return
        deadline = time.time() + seconds
        while True:
            try:
                if not self.api.wait_update(deadline=deadline):
                    break
            except Exception as exc:  # noqa: BLE001
                print(f"[tq] wait_update 异常: {exc}", flush=True)
                break
