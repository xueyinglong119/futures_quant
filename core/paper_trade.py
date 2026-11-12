"""头肩告警触发后的模拟账户。

本金 5 万元。保证金按交易所每手保证金折成比例后再加 1 个百分点。
手续费按交易所每手手续费再加 1 分（0.01 元）。
止盈、止损都从开仓价量出去：头肩顶用「头 − 左肩与头之间的低谷」，
头肩底用「左肩与头之间的高峰 − 头」，这段高度的 0.8 为止盈距离，0.2 为止损距离。
开仓价是确认那根已收盘 K 线的收盘价。同一品种同一周期同时只持有一笔。
每笔最多 8 手，不把可用资金一次开满。看板按最新入库价刷新浮动盈亏，也可手动平仓。
每笔另设金额止损：浮动亏损超过 MAX_LOSS_PER_TRADE 元即触价平仓；
与止盈/形态止损同根触碰时按亏损更大的价格成交。
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo
from pathlib import Path
from typing import Any

from pymysql.connections import Connection
from pymysql.cursors import DictCursor

from config.settings import PROJECT_ROOT
from core.kline_store import DURATION_1M, DURATION_5M, DURATION_LABELS, load_bars
from utils.symbols import get_display_name

SHANGHAI = ZoneInfo("Asia/Shanghai")
# 30m/1h/2h/3h/4h 已改为会话北京标注，不再按 UTC 裸时间
UTC_DURATIONS: set[int] = set()

INITIAL_CASH = 50_000.0
MARGIN_ADD = 0.01
FEE_ADD = 0.01
MAX_LOTS = 8
MAX_LOSS_PER_TRADE = 1_000.0
# 开盘瞬间行情连接未就绪时取不到合约资料，信号先挂起，每 10 秒重试，最多 5 分钟
PENDING_RETRY_SEC = 10.0
PENDING_TTL_SEC = 300.0
# 浮亏上限的实时盯盘间隔：会话 K 要等收线才推进，止损不能只挂在 K 线水位上
CAP_CHECK_SEC = 10.0
# 小于这个数时，交易所字段更像成交额费率，而不是每手元
_RATIO_CEILING = 0.02

_LOG_PATH = PROJECT_ROOT / "reports" / "paper_trades.log"

CREATE_ACCOUNT_SQL = """
CREATE TABLE IF NOT EXISTS `paper_account` (
  `id` INT NOT NULL,
  `initial_cash` DOUBLE NOT NULL,
  `cash` DOUBLE NOT NULL,
  `updated_at` TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='模拟账户'
"""

CREATE_TRADE_SQL = """
CREATE TABLE IF NOT EXISTS `paper_trade` (
  `id` BIGINT NOT NULL AUTO_INCREMENT,
  `symbol` VARCHAR(64) NOT NULL,
  `name` VARCHAR(32) NOT NULL,
  `duration_sec` INT NOT NULL,
  `duration` VARCHAR(8) NOT NULL,
  `side` VARCHAR(8) NOT NULL COMMENT 'long/short',
  `kind` VARCHAR(16) NOT NULL,
  `open_time` DATETIME NOT NULL,
  `open_price` DOUBLE NOT NULL,
  `lots` INT NOT NULL,
  `multiplier` DOUBLE NOT NULL,
  `exchange_margin` DOUBLE NOT NULL COMMENT '交易所每手保证金',
  `margin_rate` DOUBLE NOT NULL,
  `margin` DOUBLE NOT NULL COMMENT '本笔占用保证金',
  `exchange_fee` DOUBLE NOT NULL COMMENT '交易所每手手续费',
  `fee_per_lot` DOUBLE NOT NULL,
  `fee_open` DOUBLE NOT NULL,
  `swing` DOUBLE NOT NULL COMMENT '头到左肩之间低谷或高峰的高度',
  `extreme` DOUBLE NOT NULL,
  `head` DOUBLE NOT NULL,
  `tp_price` DOUBLE NOT NULL,
  `sl_price` DOUBLE NOT NULL,
  `equity_after_open` DOUBLE NOT NULL,
  `last_price` DOUBLE NOT NULL,
  `status` VARCHAR(16) NOT NULL,
  `close_time` DATETIME NULL,
  `close_price` DOUBLE NULL,
  `close_reason` VARCHAR(16) NULL,
  `fee_close` DOUBLE NULL,
  `gross_pnl` DOUBLE NULL,
  `net_pnl` DOUBLE NULL,
  `equity_after_close` DOUBLE NULL,
  `reason` VARCHAR(255) NOT NULL,
  `alert_id` VARCHAR(36) NULL COMMENT '触发本笔的告警 id',
  PRIMARY KEY (`id`),
  KEY `idx_status` (`status`),
  KEY `idx_symbol_dur` (`symbol`, `duration_sec`, `status`),
  KEY `idx_alert` (`alert_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='模拟成交'
"""


@dataclass
class ContractSpec:
    multiplier: float
    price_tick: float
    exchange_margin: float
    exchange_fee: float
    pre_settlement: float


@dataclass
class OpenPosition:
    trade_id: int
    symbol: str
    name: str
    duration_sec: int
    duration: str
    side: str
    open_time: datetime
    open_price: float
    lots: int
    multiplier: float
    margin: float
    fee_per_lot: float
    fee_open: float
    tp_price: float
    sl_price: float
    last_price: float


@dataclass
class PendingEntry:
    """已出信号但合约资料暂时取不到，等行情连接就绪后再开仓。"""

    symbol: str
    duration_sec: int
    when: datetime
    side: str
    entry: float
    swing: float
    extreme: float
    head: float
    kind: str
    reason: str
    alert_id: str | None
    next_try: float
    deadline: float


def _num(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(number) or math.isinf(number):
        return None
    return number


def round_tick(price: float, tick: float) -> float:
    if tick <= 0 or math.isnan(tick):
        return price
    decimals = max(0, min(8, _tick_decimals(tick)))
    return round(round(price / tick) * tick, decimals)


def _tick_decimals(tick: float) -> int:
    text = f"{tick:.8f}".rstrip("0")
    if "." not in text:
        return 0
    return len(text.split(".", 1)[1])


def money(value: float) -> float:
    return round(float(value), 2)


def target_prices(side: str, entry: float, swing: float, tick: float) -> tuple[float, float]:
    """返回 (止盈价, 止损价)。距离分别是形态高度的 0.8 和 0.2。"""
    tp_dist = swing * 0.8
    sl_dist = swing * 0.2
    if side == "short":
        tp_price, sl_price = entry - tp_dist, entry + sl_dist
    else:
        tp_price, sl_price = entry + tp_dist, entry - sl_dist
    return round_tick(tp_price, tick), round_tick(sl_price, tick)


def margin_quote(entry: float, multiplier: float, exchange_margin: float, pre_settlement: float) -> tuple[float, float]:
    """返回 (保证金比例, 每手保证金)。比例 = 交易所比例 + 1 个百分点。"""
    ref = pre_settlement if pre_settlement and pre_settlement > 0 else entry
    notional_ref = ref * multiplier
    if notional_ref <= 0 or entry <= 0 or multiplier <= 0:
        raise ValueError("合约名义价值无效")
    if 0 < exchange_margin < 1:
        rate = exchange_margin
    else:
        rate = exchange_margin / notional_ref
    rate += MARGIN_ADD
    per_lot = entry * multiplier * rate
    return rate, per_lot


def fee_quote(exchange_fee: float, notional: float) -> float:
    """每手手续费 = 交易所收费 + 0.01 元。小于 0.02 的数按成交额费率理解。"""
    if 0 <= exchange_fee < _RATIO_CEILING:
        base = notional * exchange_fee
    else:
        base = exchange_fee
    return base + FEE_ADD


def max_lots(available: float, margin_per_lot: float, fee_per_lot: float) -> int:
    cost = margin_per_lot + fee_per_lot
    if available <= 0 or cost <= 0:
        return 0
    return min(MAX_LOTS, int(available // cost))


def resolve_exit(
    side: str,
    open_price: float,
    high: float,
    low: float,
    tp_price: float,
    sl_price: float,
) -> tuple[str, float] | None:
    """这根 K 线是否该平仓。同一根同时碰到止盈和止损时按止损成交。跳空按开盘价成交。"""
    if side == "short":
        if open_price >= sl_price:
            return "止损", open_price
        if open_price <= tp_price:
            return "止盈", open_price
        hit_sl = high >= sl_price
        hit_tp = low <= tp_price
    else:
        if open_price <= sl_price:
            return "止损", open_price
        if open_price >= tp_price:
            return "止盈", open_price
        hit_sl = low <= sl_price
        hit_tp = high >= tp_price
    if hit_sl:
        return "止损", sl_price
    if hit_tp:
        return "止盈", tp_price
    return None


def price_pnl(side: str, entry: float, exit_price: float, multiplier: float, lots: int) -> float:
    sign = -1.0 if side == "short" else 1.0
    return (exit_price - entry) * sign * multiplier * lots


class ContractBook:
    """从天勤合约资料读取乘数、保证金和手续费。主连用标的合约的数字。"""

    def __init__(self, api: Any):
        self.api = api
        self._cache: dict[str, ContractSpec] = {}
        self.last_error: str | None = None

    def get(self, symbol: str) -> ContractSpec | None:
        cached = self._cache.get(symbol)
        if cached is not None:
            return cached
        try:
            spec = self._load(symbol)
        except Exception as exc:  # noqa: BLE001
            # 夜盘开盘第一个 tick 常见「获取行情信息超时」，连接就绪后可重试
            self.last_error = str(exc)
            return None
        self.last_error = None
        if spec is not None:
            self._cache[symbol] = spec
        return spec

    def _load(self, symbol: str) -> ContractSpec | None:
        symbols = [symbol]
        underlying = self._field(symbol, "underlying_symbol")
        if isinstance(underlying, str) and underlying and underlying != symbol:
            symbols.append(underlying)
        margin = fee = multiple = tick = presettle = None
        for src in symbols:
            margin = margin if margin is not None else self._num_field(src, "margin")
            fee = fee if fee is not None else self._num_field(src, "commission")
            multiple = multiple if multiple is not None else self._num_field(src, "volume_multiple")
            tick = tick if tick is not None else self._num_field(src, "price_tick")
            presettle = presettle if presettle is not None else self._num_field(src, "pre_settlement")
        if multiple is None or multiple <= 0 or margin is None or margin <= 0 or fee is None or fee < 0:
            return None
        return ContractSpec(
            multiplier=float(multiple),
            price_tick=float(tick or 0),
            exchange_margin=float(margin),
            exchange_fee=float(fee),
            pre_settlement=float(presettle or 0),
        )

    def _field(self, symbol: str, key: str) -> Any:
        quote = self.api.get_quote(symbol)
        if key in quote and quote[key] not in ("", None):
            return quote[key]
        raw = getattr(self.api, "_data", {}).get("quotes", {}).get(symbol, {})
        return raw.get(key)

    def _num_field(self, symbol: str, key: str) -> float | None:
        return _num(self._field(symbol, key))


class PaperBroker:
    def __init__(self, conn: Connection, api: Any, log_path: Path | None = None):
        self.conn = conn
        self.specs = ContractBook(api)
        self.log_path = log_path or _LOG_PATH
        self.initial_cash = INITIAL_CASH
        self.cash = INITIAL_CASH
        self._open: dict[tuple[str, int], OpenPosition] = {}
        self._pending: dict[tuple[str, int], PendingEntry] = {}
        self._last_cap_check = 0.0
        ensure_schema(conn)
        self._load()

    def on_signal(
        self,
        symbol: str,
        duration_sec: int,
        when: datetime,
        df: Any,
        signal: Any,
        alert_id: str | None = None,
    ) -> None:
        self._sync_from_db()
        detail = getattr(signal, "detail", None) or {}
        side = str(detail.get("side") or "")
        swing = _num(detail.get("swing"))
        extreme = _num(detail.get("extreme"))
        head = _num(detail.get("head"))
        name = get_display_name(symbol)
        if side not in ("long", "short") or swing is None or swing <= 0 or extreme is None or head is None:
            self._log(f"跳过 {name} {when}：没有可用的头到左肩高度")
            return
        if (symbol, duration_sec) in self._open:
            self._log(f"跳过 {name} {when}：该周期已有持仓")
            return
        if df is None or len(df) == 0:
            return
        entry = float(df.iloc[-1]["close"])
        if entry <= 0:
            return
        kind = "头肩顶" if side == "short" else "头肩底"
        reason = str(getattr(signal, "reason", "") or "")
        spec = self.specs.get(symbol)
        if spec is None:
            self._defer(
                symbol=symbol,
                duration_sec=duration_sec,
                when=when,
                side=side,
                entry=entry,
                swing=swing,
                extreme=extreme,
                head=head,
                kind=kind,
                reason=reason,
                alert_id=alert_id,
            )
            return
        self._open_trade(
            symbol=symbol,
            duration_sec=duration_sec,
            when=_as_naive(when),
            side=side,
            entry=entry,
            swing=swing,
            extreme=extreme,
            head=head,
            spec=spec,
            kind=kind,
            reason=reason,
            alert_id=alert_id,
        )

    def _defer(
        self,
        *,
        symbol: str,
        duration_sec: int,
        when: datetime,
        side: str,
        entry: float,
        swing: float,
        extreme: float,
        head: float,
        kind: str,
        reason: str,
        alert_id: str | None,
    ) -> None:
        """合约资料取不到时先挂起，等行情连接就绪后按原信号价补开。"""
        when = _as_naive(when)
        now = time.monotonic()
        key = (symbol, duration_sec)
        replaced = key in self._pending
        self._pending[key] = PendingEntry(
            symbol=symbol,
            duration_sec=duration_sec,
            when=when,
            side=side,
            entry=entry,
            swing=swing,
            extreme=extreme,
            head=head,
            kind=kind,
            reason=reason,
            alert_id=alert_id,
            next_try=now + PENDING_RETRY_SEC,
            deadline=now + PENDING_TTL_SEC,
        )
        name = get_display_name(symbol)
        label = DURATION_LABELS.get(duration_sec, str(duration_sec))
        error = self.specs.last_error or "缺少保证金/手续费字段"
        prefix = "改挂" if replaced else "暂缓"
        self._log(
            f"{prefix} {name} {label} {when:%m-%d %H:%M}：取合约资料失败（{error}），"
            f"每 {PENDING_RETRY_SEC:.0f}s 重试，最多 {PENDING_TTL_SEC / 60:.0f} 分钟"
        )

    def retry_pending(self) -> None:
        """每个行情 tick 调一次：行情连接恢复后把挂起的信号补开。"""
        if not self._pending:
            return
        now = time.monotonic()
        for key, item in list(self._pending.items()):
            if now < item.next_try:
                continue
            name = get_display_name(item.symbol)
            label = DURATION_LABELS.get(item.duration_sec, str(item.duration_sec))
            if now >= item.deadline:
                self._pending.pop(key, None)
                self._log(
                    f"放弃 {name} {label} {item.when:%m-%d %H:%M}："
                    f"{PENDING_TTL_SEC / 60:.0f} 分钟内没取到合约保证金/手续费"
                )
                continue
            spec = self.specs.get(item.symbol)
            if spec is None:
                item.next_try = now + PENDING_RETRY_SEC
                continue
            self._pending.pop(key, None)
            if key in self._open:
                self._log(f"跳过 {name} {label} {item.when:%m-%d %H:%M}：该周期已有持仓")
                continue
            try:
                self._open_trade(
                    symbol=item.symbol,
                    duration_sec=item.duration_sec,
                    when=item.when,
                    side=item.side,
                    entry=item.entry,
                    swing=item.swing,
                    extreme=item.extreme,
                    head=item.head,
                    spec=spec,
                    kind=item.kind,
                    reason=item.reason,
                    alert_id=item.alert_id,
                )
            except Exception as exc:  # noqa: BLE001
                self._log(f"重试开仓失败 {name} {label} {item.when:%m-%d %H:%M}：{exc}")

    def on_bar(self, symbol: str, duration_sec: int, when: datetime, bar: dict) -> None:
        self._sync_from_db()
        pos = self._open.get((symbol, duration_sec))
        if pos is None:
            return
        when = _as_naive(when)
        high = float(bar["high"])
        low = float(bar["low"])
        opened = float(bar["open"])
        close = float(bar["close"])
        hit = resolve_exit(pos.side, opened, high, low, pos.tp_price, pos.sl_price)
        cap_hit = self._loss_cap_hit(pos, opened, high, low)
        if hit is not None and cap_hit is not None:
            if pos.side == "long":
                hit = min(hit, cap_hit, key=lambda h: h[1])
            else:
                hit = max(hit, cap_hit, key=lambda h: h[1])
        elif cap_hit is not None:
            hit = cap_hit
        if hit is None:
            pos.last_price = close
            self._update_last_price(pos)
            return
        reason, exit_price = hit
        self._close_trade(pos, when, reason, exit_price)

    def _loss_cap_hit(
        self, pos: OpenPosition, opened: float, high: float, low: float
    ) -> tuple[str, float] | None:
        if pos.multiplier <= 0 or pos.lots <= 0:
            return None
        dist = MAX_LOSS_PER_TRADE / (pos.multiplier * pos.lots)
        cap_price = pos.open_price - dist if pos.side == "long" else pos.open_price + dist
        no_tp = math.inf if pos.side == "long" else -math.inf
        hit = resolve_exit(pos.side, opened, high, low, no_tp, cap_price)
        if hit is None:
            return None
        return f"止损(浮亏超{MAX_LOSS_PER_TRADE:.0f})", hit[1]

    def check_loss_caps(self) -> int:
        """按最新入库价实时了结浮亏超限的持仓，返回平仓笔数。

        `on_bar` 只在 K 线水位前进时被调用，会话周期一根要等几十分钟才收线，
        期间浮亏早已越界却没人平仓，所以金额止损必须脱离 K 线单独盯。
        """
        now_mono = time.monotonic()
        if now_mono - self._last_cap_check < CAP_CHECK_SEC:
            return 0
        self._last_cap_check = now_mono
        self._sync_from_db()
        if not self._open:
            return 0
        wall = datetime.now(SHANGHAI).replace(tzinfo=None, microsecond=0)
        closed = 0
        for pos in list(self._open.values()):
            mark = latest_mark_price(self.conn, pos.symbol, pos.duration_sec)
            if mark is None or mark <= 0:
                continue
            if mark != pos.last_price:
                pos.last_price = mark
                self._update_last_price(pos)
            pnl = price_pnl(pos.side, pos.open_price, mark, pos.multiplier, pos.lots)
            if pnl > -MAX_LOSS_PER_TRADE:
                continue
            self._close_trade(pos, wall, f"止损(浮亏超{MAX_LOSS_PER_TRADE:.0f})", mark)
            closed += 1
        return closed

    def snapshot(self) -> dict[str, Any]:
        return account_snapshot(self.conn)

    def _open_trade(
        self,
        *,
        symbol: str,
        duration_sec: int,
        when: datetime,
        side: str,
        entry: float,
        swing: float,
        extreme: float,
        head: float,
        spec: ContractSpec,
        kind: str,
        reason: str,
        alert_id: str | None = None,
    ) -> None:
        rate, margin_lot = margin_quote(entry, spec.multiplier, spec.exchange_margin, spec.pre_settlement)
        notional = entry * spec.multiplier
        fee_lot = fee_quote(spec.exchange_fee, notional)
        lots = max_lots(self._available(), margin_lot, fee_lot)
        label = DURATION_LABELS.get(duration_sec, str(duration_sec))
        name = get_display_name(symbol)
        if lots < 1:
            self._log(
                f"跳过 {name} {label} {when:%m-%d %H:%M}："
                f"可用 {self._available():.2f} 不够 1 手保证金 {margin_lot:.2f}"
            )
            return
        tp_price, sl_price = target_prices(side, entry, swing, spec.price_tick)
        fee_open = money(fee_lot * lots)
        margin = money(margin_lot * lots)
        if self.cash - fee_open <= 0:
            self._log(f"跳过 {name} {label}：开仓手续费后本金不足")
            return
        self.cash = money(self.cash - fee_open)
        equity = money(self._equity_assuming(extra_unrealized=0))
        cur = self.conn.cursor()
        try:
            cur.execute(
                """
                INSERT INTO paper_trade (
                  symbol, name, duration_sec, duration, side, kind,
                  open_time, open_price, lots, multiplier,
                  exchange_margin, margin_rate, margin,
                  exchange_fee, fee_per_lot, fee_open,
                  swing, extreme, head, tp_price, sl_price,
                  equity_after_open, last_price, status, reason, alert_id
                ) VALUES (
                  %s,%s,%s,%s,%s,%s,
                  %s,%s,%s,%s,
                  %s,%s,%s,
                  %s,%s,%s,
                  %s,%s,%s,%s,%s,
                  %s,%s,'open',%s,%s
                )
                """,
                (
                    symbol, name, duration_sec, label, side, kind,
                    when, entry, lots, spec.multiplier,
                    spec.exchange_margin, rate, margin,
                    spec.exchange_fee, fee_lot, fee_open,
                    swing, extreme, head, tp_price, sl_price,
                    equity, entry, reason[:255], alert_id,
                ),
            )
            trade_id = int(cur.lastrowid)
            self._save_cash(cur)
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            self.cash = money(self.cash + fee_open)
            raise
        finally:
            cur.close()
        pos = OpenPosition(
            trade_id=trade_id,
            symbol=symbol,
            name=name,
            duration_sec=duration_sec,
            duration=label,
            side=side,
            open_time=when,
            open_price=entry,
            lots=lots,
            multiplier=spec.multiplier,
            margin=margin,
            fee_per_lot=fee_lot,
            fee_open=fee_open,
            tp_price=tp_price,
            sl_price=sl_price,
            last_price=entry,
        )
        self._open[(symbol, duration_sec)] = pos
        side_text = "空" if side == "short" else "多"
        self._log(
            f"开仓 {name} {label} {side_text} {when:%m-%d %H:%M} "
            f"价{entry:g} {lots}手 保证金{margin:.2f} 比例{rate:.4f} "
            f"手续费{fee_open:.2f} 止盈{tp_price:g} 止损{sl_price:g} 本金{equity:.2f}"
        )

    def _close_trade(self, pos: OpenPosition, when: datetime, reason: str, exit_price: float) -> None:
        fee_close = money(pos.fee_per_lot * pos.lots)
        gross = money(price_pnl(pos.side, pos.open_price, exit_price, pos.multiplier, pos.lots))
        net = money(gross - pos.fee_open - fee_close)
        self._open.pop((pos.symbol, pos.duration_sec), None)
        self.cash = money(self.cash + gross - fee_close)
        equity = money(self._equity_assuming(extra_unrealized=0))
        cur = self.conn.cursor()
        try:
            cur.execute(
                """
                UPDATE paper_trade
                SET status='closed', close_time=%s, close_price=%s, close_reason=%s,
                    fee_close=%s, gross_pnl=%s, net_pnl=%s, equity_after_close=%s, last_price=%s
                WHERE id=%s AND status='open'
                """,
                (when, exit_price, reason, fee_close, gross, net, equity, exit_price, pos.trade_id),
            )
            self._save_cash(cur)
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            self.cash = money(self.cash - gross + fee_close)
            self._open[(pos.symbol, pos.duration_sec)] = pos
            raise
        finally:
            cur.close()
        side_text = "空" if pos.side == "short" else "多"
        self._log(
            f"平仓 {pos.name} {pos.duration} {side_text} {reason} {when:%m-%d %H:%M} "
            f"价{exit_price:g} 价差盈亏{gross:.2f} 平仓手续费{fee_close:.2f} "
            f"开仓手续费{pos.fee_open:.2f} 净盈亏{net:.2f} 本金{equity:.2f}"
        )

    def _update_last_price(self, pos: OpenPosition) -> None:
        cur = self.conn.cursor()
        try:
            cur.execute(
                "UPDATE paper_trade SET last_price=%s WHERE id=%s AND status='open'",
                (pos.last_price, pos.trade_id),
            )
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise
        finally:
            cur.close()

    def _available(self) -> float:
        # 浮动盈利不算可开仓资金，浮动亏损要先扣掉
        return self.cash + min(0.0, self._unrealized()) - self._margin_used()

    def _margin_used(self) -> float:
        return sum(pos.margin for pos in self._open.values())

    def _unrealized(self) -> float:
        return sum(
            price_pnl(pos.side, pos.open_price, pos.last_price, pos.multiplier, pos.lots)
            for pos in self._open.values()
        )

    def _equity_assuming(self, extra_unrealized: float) -> float:
        return self.cash + self._unrealized() + extra_unrealized

    def _save_cash(self, cur: Any) -> None:
        cur.execute("UPDATE paper_account SET cash=%s WHERE id=1", (self.cash,))

    def _sync_from_db(self) -> None:
        """看板手动平仓后，内存账户要跟库里的本金和持仓对齐。"""
        cur = self.conn.cursor(DictCursor)
        try:
            cur.execute("SELECT initial_cash, cash FROM paper_account WHERE id=1")
            row = cur.fetchone()
            if row is None:
                return
            self.initial_cash = float(row["initial_cash"])
            self.cash = float(row["cash"])
            cur.execute(
                """
                SELECT id, symbol, name, duration_sec, duration, side, open_time, open_price,
                       lots, multiplier, margin, fee_per_lot, fee_open, tp_price, sl_price, last_price
                FROM paper_trade WHERE status='open' ORDER BY id
                """
            )
            opened: dict[tuple[str, int], OpenPosition] = {}
            for item in cur.fetchall():
                pos = OpenPosition(
                    trade_id=int(item["id"]),
                    symbol=str(item["symbol"]),
                    name=str(item["name"]),
                    duration_sec=int(item["duration_sec"]),
                    duration=str(item["duration"]),
                    side=str(item["side"]),
                    open_time=_as_naive(item["open_time"]),
                    open_price=float(item["open_price"]),
                    lots=int(item["lots"]),
                    multiplier=float(item["multiplier"]),
                    margin=float(item["margin"]),
                    fee_per_lot=float(item["fee_per_lot"]),
                    fee_open=float(item["fee_open"]),
                    tp_price=float(item["tp_price"]),
                    sl_price=float(item["sl_price"]),
                    last_price=float(item["last_price"]),
                )
                opened[(pos.symbol, pos.duration_sec)] = pos
            self._open = opened
        finally:
            cur.close()

    def _load(self) -> None:
        cur = self.conn.cursor(DictCursor)
        try:
            cur.execute("SELECT initial_cash, cash FROM paper_account WHERE id=1")
            row = cur.fetchone()
            if row is None:
                cur.execute(
                    "INSERT INTO paper_account (id, initial_cash, cash) VALUES (1, %s, %s)",
                    (INITIAL_CASH, INITIAL_CASH),
                )
                self.conn.commit()
        finally:
            cur.close()
        self._sync_from_db()
        self._log(f"账户就绪 初始{self.initial_cash:.2f} 当前本金{self.cash:.2f} 持仓{len(self._open)}笔")

    def _log(self, text: str) -> None:
        line = f"[paper] {text}"
        print(line, flush=True)
        try:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            with self.log_path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
        except OSError:
            pass


def ensure_schema(conn: Connection) -> None:
    with conn.cursor() as cur:
        cur.execute(CREATE_ACCOUNT_SQL)
        cur.execute(CREATE_TRADE_SQL)
        cur.execute(
            """
            SELECT COUNT(*) FROM information_schema.COLUMNS
            WHERE TABLE_SCHEMA = DATABASE()
              AND TABLE_NAME = 'paper_trade'
              AND COLUMN_NAME = 'alert_id'
            """
        )
        row = cur.fetchone()
        has_alert_id = bool(row) and int(row[0]) > 0
        if not has_alert_id:
            cur.execute(
                "ALTER TABLE paper_trade "
                "ADD COLUMN `alert_id` VARCHAR(36) NULL COMMENT '触发本笔的告警 id', "
                "ADD KEY `idx_alert` (`alert_id`)"
            )
    conn.commit()


def latest_mark_price(conn: Connection, symbol: str, duration_sec: int) -> float | None:
    """优先用 1 分钟、5 分钟最新收盘，没有再用本周期。"""
    for dur in (DURATION_1M, DURATION_5M, int(duration_sec)):
        bars = load_bars(conn, symbol, dur, limit=1)
        if bars:
            return float(bars[-1]["close"])
    return None


def refresh_open_marks(conn: Connection, positions: list[dict[str, Any]]) -> None:
    """看板读数前把持仓最新价刷到库里，浮动盈亏才不是开仓价。"""
    if not positions:
        return
    cur = conn.cursor()
    try:
        for pos in positions:
            mark = latest_mark_price(conn, str(pos["symbol"]), int(pos["duration_sec"]))
            if mark is None:
                continue
            pos["last_price"] = mark
            cur.execute(
                "UPDATE paper_trade SET last_price=%s WHERE id=%s AND status='open'",
                (mark, int(pos["id"])),
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        cur.close()


def position_unrealized(pos: dict[str, Any]) -> float:
    return money(
        price_pnl(
            str(pos["side"]),
            float(pos["open_price"]),
            float(pos["last_price"]),
            float(pos["multiplier"]),
            int(pos["lots"]),
        )
    )


def account_snapshot(conn: Connection, limit: int = 100) -> dict[str, Any]:
    ensure_schema(conn)
    cur = conn.cursor(DictCursor)
    try:
        cur.execute("SELECT initial_cash, cash FROM paper_account WHERE id=1")
        account = cur.fetchone()
        if account is None:
            cur.execute(
                "INSERT INTO paper_account (id, initial_cash, cash) VALUES (1, %s, %s)",
                (INITIAL_CASH, INITIAL_CASH),
            )
            conn.commit()
            account = {"initial_cash": INITIAL_CASH, "cash": INITIAL_CASH}
        cur.execute(
            """
            SELECT id, symbol, name, duration_sec, duration, side, kind, open_time, open_price, lots,
                   margin_rate, margin, fee_open, swing, extreme, head, tp_price, sl_price,
                   last_price, multiplier, equity_after_open, reason
            FROM paper_trade WHERE status='open' ORDER BY id
            """
        )
        positions = [_row(item) for item in cur.fetchall()]
        cur.execute(
            """
            SELECT id, symbol, name, duration, side, kind, status,
                   open_time, open_price, lots, fee_open, tp_price, sl_price,
                   close_time, close_price, close_reason, fee_close,
                   gross_pnl, net_pnl, equity_after_open, equity_after_close, reason
            FROM paper_trade ORDER BY id DESC LIMIT %s
            """,
            (max(1, min(limit, 200)),),
        )
        trades = [_row(item) for item in cur.fetchall()]
    finally:
        cur.close()

    refresh_open_marks(conn, positions)
    unrealized = 0.0
    margin_used = 0.0
    for pos in positions:
        pnl = position_unrealized(pos)
        pos["unrealized"] = pnl
        unrealized += pnl
        margin_used += float(pos["margin"])
    cash = float(account["cash"])
    equity = cash + unrealized
    available = cash + min(0.0, unrealized) - margin_used
    return {
        "initial_cash": float(account["initial_cash"]),
        "cash": money(cash),
        "equity": money(equity),
        "unrealized": money(unrealized),
        "margin_used": money(margin_used),
        "available": money(available),
        "open_count": len(positions),
        "positions": positions,
        "trades": trades,
    }


def store_clock_now(duration_sec: int) -> datetime:
    """按该周期的库存时区写入平仓时间。"""
    now = datetime.now(SHANGHAI).replace(tzinfo=None, microsecond=0)
    if int(duration_sec) in UTC_DURATIONS:
        return now - timedelta(hours=8)
    return now


def manual_close(conn: Connection, trade_id: int) -> dict[str, Any]:
    """看板手动平仓。按最新入库价成交，原因记为手动。"""
    ensure_schema(conn)
    cur = conn.cursor(DictCursor)
    try:
        cur.execute(
            """
            SELECT id, symbol, name, duration_sec, duration, side, open_price, lots,
                   multiplier, fee_per_lot, fee_open, last_price
            FROM paper_trade WHERE id=%s AND status='open'
            """,
            (int(trade_id),),
        )
        row = cur.fetchone()
        if row is None:
            raise ValueError("这笔单已经平仓或不存在")
        cur.execute("SELECT cash FROM paper_account WHERE id=1")
        account = cur.fetchone()
        if account is None:
            raise ValueError("模拟账户不存在")
        cash = float(account["cash"])
        mark = latest_mark_price(conn, str(row["symbol"]), int(row["duration_sec"]))
        if mark is None:
            mark = float(row["last_price"])
        if mark <= 0:
            raise ValueError("没有可用平仓价")
        when = store_clock_now(int(row["duration_sec"]))
        fee_close = money(float(row["fee_per_lot"]) * int(row["lots"]))
        gross = money(
            price_pnl(
                str(row["side"]),
                float(row["open_price"]),
                mark,
                float(row["multiplier"]),
                int(row["lots"]),
            )
        )
        net = money(gross - float(row["fee_open"]) - fee_close)
        cash = money(cash + gross - fee_close)
        cur.execute(
            """
            UPDATE paper_trade
            SET status='closed', close_time=%s, close_price=%s, close_reason=%s,
                fee_close=%s, gross_pnl=%s, net_pnl=%s, equity_after_close=%s, last_price=%s
            WHERE id=%s AND status='open'
            """,
            (when, mark, "手动", fee_close, gross, net, cash, mark, int(trade_id)),
        )
        if cur.rowcount != 1:
            raise ValueError("这笔单已经平仓或不存在")
        cur.execute("UPDATE paper_account SET cash=%s WHERE id=1", (cash,))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        cur.close()
    return {
        "id": int(trade_id),
        "close_price": mark,
        "fee_close": fee_close,
        "gross_pnl": gross,
        "net_pnl": net,
        "cash": cash,
        "close_reason": "手动",
    }


def alert_execution(conn: Connection, alert_id: str) -> dict[str, Any]:
    """这条告警对应的模拟执行情况：开没开仓、开在哪、现在浮亏浮盈或净盈亏。"""
    ensure_schema(conn)
    cur = conn.cursor(DictCursor)
    try:
        cur.execute(
            """
            SELECT id, symbol, name, duration, duration_sec, side, kind, status,
                   open_time, open_price, lots, multiplier, margin, margin_rate,
                   fee_open, fee_close, swing, extreme, head, tp_price, sl_price,
                   last_price, close_time, close_price, close_reason,
                   gross_pnl, net_pnl, equity_after_open, equity_after_close, alert_id
            FROM paper_trade WHERE alert_id=%s ORDER BY id
            """,
            (alert_id,),
        )
        rows = [_row(item) for item in cur.fetchall()]
    finally:
        cur.close()
    if not rows:
        return {"opened": False, "trade": None, "note": "这条告警没有开仓（可能该周期已有持仓或资金不够）"}
    trade = rows[-1]
    if trade.get("status") == "open":
        mark = latest_mark_price(conn, str(trade["symbol"]), int(trade["duration_sec"]))
        if mark is not None:
            trade["last_price"] = mark
        trade["unrealized"] = position_unrealized(trade)
        trade["note"] = "持仓中"
    else:
        trade["unrealized"] = None
        trade["note"] = f"已平仓（{trade.get('close_reason') or ''}）"
    open_bj = beijing_clock(_parse_dt(trade["open_time"]), int(trade["duration_sec"]))
    trade["open_time_bj"] = format_bj(open_bj)
    if trade.get("close_time"):
        close_bj = beijing_clock(_parse_dt(trade["close_time"]), int(trade["duration_sec"]))
        trade["close_time_bj"] = format_bj(close_bj)
    return {"opened": True, "trade": trade, "note": trade["note"]}


def beijing_clock(when: datetime, duration_sec: int) -> datetime:
    """把库存时间换成北京时间。1 小时、30 分钟等按 UTC 存，其余已经是北京时间。"""
    base = when.replace(tzinfo=None, microsecond=0) if when.tzinfo else when.replace(microsecond=0)
    if int(duration_sec) in UTC_DURATIONS:
        return base + timedelta(hours=8)
    return base


def session_day(when: datetime) -> date:
    """商品交易日。21:00 及之后的夜盘算下一个交易日，周五夜盘算下周一。"""
    minutes = when.hour * 60 + when.minute
    if minutes >= 21 * 60:
        nxt = when.date() + timedelta(days=1)
        while nxt.weekday() >= 5:
            nxt += timedelta(days=1)
        return nxt
    if minutes < 8 * 60 and when.weekday() == 5:
        return when.date() + timedelta(days=2)
    return when.date()


def format_bj(when: datetime) -> str:
    return when.strftime("%m-%d %H:%M")


def trading_board(conn: Connection, day: date | None = None) -> dict[str, Any]:
    """某一天的模拟交易汇总。day 为空时用北京时间的今天。"""
    snap = account_snapshot(conn)
    now = datetime.now(SHANGHAI).replace(tzinfo=None)
    today = session_day(now)
    selected = day or today
    cur = conn.cursor(DictCursor)
    try:
        cur.execute(
            """
            SELECT id, symbol, name, duration_sec, duration, side, kind, status,
                   open_time, open_price, lots, fee_open, fee_close,
                   close_time, close_price, close_reason,
                   gross_pnl, net_pnl, equity_after_open, equity_after_close,
                   tp_price, sl_price, margin, last_price, multiplier, alert_id
            FROM paper_trade
            ORDER BY id
            """
        )
        rows = [_row(item) for item in cur.fetchall()]
    finally:
        cur.close()
    open_marks = {
        int(pos["id"]): pos
        for pos in snap.get("positions") or []
        if pos.get("id") is not None
    }
    for row in rows:
        if row.get("status") != "open":
            continue
        marked = open_marks.get(int(row["id"]))
        if marked is None:
            continue
        row["last_price"] = marked.get("last_price", row.get("last_price"))
        row["unrealized"] = marked.get("unrealized")
    return _assemble_board(rows, snap, selected, today)


def _assemble_board(
    rows: list[dict[str, Any]],
    snap: dict[str, Any],
    selected: date,
    today: date,
) -> dict[str, Any]:
    days: set[date] = {today}
    prepared: list[dict[str, Any]] = []
    for row in rows:
        open_bj = beijing_clock(_parse_dt(row["open_time"]), int(row["duration_sec"]))
        close_raw = row.get("close_time")
        close_bj = beijing_clock(_parse_dt(close_raw), int(row["duration_sec"])) if close_raw else None
        days.add(session_day(open_bj))
        if close_bj is not None:
            days.add(session_day(close_bj))
        prepared.append({**row, "_open_bj": open_bj, "_close_bj": close_bj})

    opened = closed = wins = losses = 0
    net_pnl = gross_pnl = fee = 0.0
    equity_events: list[tuple[datetime, float]] = []
    trades: list[tuple[datetime, dict[str, Any]]] = []
    for row in prepared:
        open_bj = row["_open_bj"]
        close_bj = row["_close_bj"]
        did_open = session_day(open_bj) == selected
        did_close = close_bj is not None and session_day(close_bj) == selected
        held_today = selected == today and row.get("status") == "open"
        if did_open:
            opened += 1
            fee += float(row.get("fee_open") or 0)
            if row.get("equity_after_open") is not None:
                equity_events.append((open_bj, float(row["equity_after_open"])))
        if did_close:
            closed += 1
            fee += float(row.get("fee_close") or 0)
            net_value = float(row.get("net_pnl") or 0)
            gross_value = float(row.get("gross_pnl") or 0)
            net_pnl += net_value
            gross_pnl += gross_value
            if net_value > 0:
                wins += 1
            elif net_value < 0:
                losses += 1
            if row.get("equity_after_close") is not None:
                equity_events.append((close_bj, float(row["equity_after_close"])))
        if not (did_open or did_close or held_today):
            continue
        if did_open and did_close:
            mark = "开平"
        elif did_open:
            mark = "开仓"
        elif did_close:
            mark = "平仓"
        else:
            mark = "持仓"
        trades.append((open_bj, _board_trade(row, open_bj, close_bj, mark)))

    trades.sort(key=lambda item: item[0], reverse=True)
    ordered = [item[1] for item in trades]
    equity_events.sort(key=lambda item: item[0])
    if selected == today:
        equity = snap.get("equity")
        unrealized = snap.get("unrealized")
        open_positions = snap.get("open_count")
    else:
        equity = equity_events[-1][1] if equity_events else None
        unrealized = None
        open_positions = None
    day_list = [item.isoformat() for item in sorted(days, reverse=True)]
    return {
        "today": today.isoformat(),
        "date": selected.isoformat(),
        "days": day_list,
        "summary": {
            "opened": opened,
            "closed": closed,
            "wins": wins,
            "losses": losses,
            "win_rate": None if closed == 0 else round(wins / closed, 4),
            "net_pnl": money(net_pnl),
            "gross_pnl": money(gross_pnl),
            "fee": money(fee),
            "equity": None if equity is None else money(float(equity)),
            "unrealized": None if unrealized is None else money(float(unrealized)),
            "open_positions": open_positions,
            "is_today": selected == today,
        },
        "trades": ordered,
    }


def _board_trade(row: dict[str, Any], open_bj: datetime, close_bj: datetime | None, mark: str) -> dict[str, Any]:
    open_status = row.get("status") == "open"
    unrealized = row.get("unrealized")
    if open_status and unrealized is None and row.get("last_price") is not None:
        unrealized = position_unrealized(row)
    return {
        "id": row["id"],
        "name": row.get("name") or "",
        "symbol": row.get("symbol") or "",
        "duration": row.get("duration") or "",
        "side": row.get("side") or "",
        "side_text": "空" if row.get("side") == "short" else "多",
        "kind": row.get("kind") or "",
        "status": row.get("status") or "",
        "mark": mark,
        "can_close": open_status,
        "open_time": format_bj(open_bj),
        "close_time": None if close_bj is None else format_bj(close_bj),
        "open_price": row.get("open_price"),
        "close_price": row.get("close_price"),
        "last_price": row.get("last_price") if open_status else None,
        "lots": row.get("lots"),
        "fee_open": row.get("fee_open"),
        "fee_close": row.get("fee_close"),
        "net_pnl": row.get("net_pnl"),
        "unrealized": unrealized,
        "close_reason": row.get("close_reason"),
        "tp_price": row.get("tp_price"),
        "sl_price": row.get("sl_price"),
        "alert_id": row.get("alert_id"),
    }


def _parse_dt(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value
    text = str(value).replace("T", " ")
    return datetime.fromisoformat(text)


def _row(item: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in item.items():
        if isinstance(value, datetime):
            out[key] = value.isoformat(sep=" ", timespec="seconds")
        else:
            out[key] = value
    return out


def _as_naive(value: datetime) -> datetime:
    if isinstance(value, datetime) and value.tzinfo is not None:
        return value.replace(tzinfo=None).replace(microsecond=0)
    if isinstance(value, datetime):
        return value.replace(microsecond=0)
    return value
