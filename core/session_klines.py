"""国内小节休市规则下的 30 分钟 K 线合成（回测/仿真用）"""

from __future__ import annotations

import math
from datetime import date, datetime
from zoneinfo import ZoneInfo

import pandas as pd

from utils.trading_session import aggregate_session_bars

TZ_SHANGHAI = ZoneInfo("Asia/Shanghai")


def quote_datetime_to_ns(dt_val) -> int:
    """TqSdk quote.datetime：回测为字符串，实盘/部分场景为纳秒整数。"""
    if dt_val is None or (isinstance(dt_val, float) and math.isnan(dt_val)):
        return 0
    if isinstance(dt_val, (int, float)):
        return int(dt_val)
    dt = datetime.fromisoformat(str(dt_val))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=TZ_SHANGHAI)
    return int(dt.timestamp() * 1e9)


def _parse_minute_row(klines: pd.DataFrame, index: int) -> dict | None:
    dt_ns = klines.iloc[index]["datetime"]
    if dt_ns is None or (isinstance(dt_ns, float) and math.isnan(dt_ns)):
        return None
    dt = datetime.fromtimestamp(dt_ns / 1e9, tz=TZ_SHANGHAI).replace(tzinfo=None)
    return {
        "datetime": dt,
        "open": float(klines.iloc[index]["open"]),
        "high": float(klines.iloc[index]["high"]),
        "low": float(klines.iloc[index]["low"]),
        "close": float(klines.iloc[index]["close"]),
        "volume": int(klines.iloc[index]["volume"]),
        "open_oi": int(klines.iloc[index]["open_oi"]),
    }


def bars_to_kline_dataframe(bars: list[dict]) -> pd.DataFrame:
    if not bars:
        return pd.DataFrame(
            columns=[
                "datetime",
                "open",
                "high",
                "low",
                "close",
                "volume",
                "open_oi",
            ]
        )
    rows = []
    for bar in bars:
        dt_end = datetime.fromisoformat(bar["datetime_end"])
        if dt_end.tzinfo is None:
            dt_end = dt_end.replace(tzinfo=TZ_SHANGHAI)
        rows.append(
            {
                "datetime": int(dt_end.timestamp() * 1e9),
                "open": bar["open"],
                "high": bar["high"],
                "low": bar["low"],
                "close": bar["close"],
                "volume": bar["volume"],
                "open_oi": bar["open_oi"],
            }
        )
    return pd.DataFrame(rows)


def _bar_end_ns(bar: dict) -> int:
    dt_end = datetime.fromisoformat(bar["datetime_end"])
    if dt_end.tzinfo is None:
        dt_end = dt_end.replace(tzinfo=TZ_SHANGHAI)
    return int(dt_end.timestamp() * 1e9)


class SessionKlineFeed:
    """
    从 1 分钟 K 线合成国内 30 分钟 K 线。
    回测中按 quote 时间逐步「解锁」已完成的 bar，避免一次性灌入未来数据。
    """

    # 约 5 万根 1 分钟 K 线，覆盖 6 个月回测；配合增量 ingest 避免环形缓冲失效
    MINUTE_LENGTH = 50000

    def __init__(self, api, main_symbols: list[str]):
        self.api = api
        self.minute_klines: dict[str, pd.DataFrame] = {
            s: api.get_kline_serial(s, 60, data_length=self.MINUTE_LENGTH)
            for s in main_symbols
        }
        self.session_klines: dict[str, pd.DataFrame] = {
            s: bars_to_kline_dataframe([]) for s in main_symbols
        }
        self._last_minute_ns: dict[str, int | None] = {s: None for s in main_symbols}
        self._day_minutes: dict[str, dict[date, list[dict]]] = {
            s: {} for s in main_symbols
        }
        self._session_rows: dict[str, list[dict]] = {s: [] for s in main_symbols}
        self._known_ends: dict[str, set[str]] = {s: set() for s in main_symbols}
        self._visible_counts: dict[str, int] = {s: 0 for s in main_symbols}

    # 日盘/夜盘最长有效成交约 30 分钟；回测中未收盘分钟线 OHLC 会随 tick 变化，需反复刷新
    _MINUTE_TAIL_REFRESH = 35

    @staticmethod
    def _upsert_day_minute(day_list: list[dict], minute: dict) -> bool:
        for i, existing in enumerate(day_list):
            if existing["datetime"] == minute["datetime"]:
                if (
                    existing["open"] == minute["open"]
                    and existing["high"] == minute["high"]
                    and existing["low"] == minute["low"]
                    and existing["close"] == minute["close"]
                    and existing["volume"] == minute["volume"]
                    and existing["open_oi"] == minute["open_oi"]
                ):
                    return False
                day_list[i] = minute
                return True
        day_list.append(minute)
        return True

    def _sync_day_bars(self, main_symbol: str, trade_date: date) -> None:
        day_buf = self._day_minutes[main_symbol].get(trade_date, [])
        if not day_buf:
            return

        rows = self._session_rows[main_symbol]
        known = self._known_ends[main_symbol]

        # 增量 ingest 时同一交易日会多次触发合成；必须整日复算，否则首根分钟线
        # 就会固化成 open=high=low=close 的错误 OHLC。
        stale_ends: set[str] = set()
        kept: list[dict] = []
        for row in rows:
            dt_end = datetime.fromisoformat(row["datetime_end"])
            if dt_end.date() == trade_date:
                stale_ends.add(row["datetime_end"])
            else:
                kept.append(row)

        for end in stale_ends:
            known.discard(end)

        for bar in aggregate_session_bars(day_buf, trade_date, time_label="end"):
            known.add(bar["datetime_end"])
            kept.append(bar)

        kept.sort(key=_bar_end_ns)
        self._session_rows[main_symbol] = kept

        visible = self._visible_counts[main_symbol]
        if visible > 0:
            self.session_klines[main_symbol] = bars_to_kline_dataframe(kept[:visible])

    def rebuild_all_from_serial(self, main_symbol: str) -> None:
        """用 minute serial 全量重建 session K 线（历史拉取结束后调用）。"""
        minute_df = self.minute_klines[main_symbol]
        self._day_minutes[main_symbol].clear()
        self._session_rows[main_symbol].clear()
        self._known_ends[main_symbol].clear()
        self._last_minute_ns[main_symbol] = None

        day_buf = self._day_minutes[main_symbol]
        touched_days: set[date] = set()
        newest_ns: int | None = None

        for i in range(len(minute_df)):
            minute = _parse_minute_row(minute_df, i)
            if minute is None:
                continue
            dt_ns = int(
                minute["datetime"].replace(tzinfo=TZ_SHANGHAI).timestamp() * 1e9
            )
            trade_date = minute["datetime"].date()
            day_list = day_buf.setdefault(trade_date, [])
            self._upsert_day_minute(day_list, minute)
            touched_days.add(trade_date)
            if newest_ns is None or dt_ns > newest_ns:
                newest_ns = dt_ns

        self._last_minute_ns[main_symbol] = newest_ns
        for trade_date in sorted(touched_days):
            self._sync_day_bars(main_symbol, trade_date)

    def ingest_minutes(self, main_symbol: str) -> None:
        minute_df = self.minute_klines[main_symbol]
        if len(minute_df) == 0:
            return

        last_ns = self._last_minute_ns[main_symbol]
        pending: list[dict] = []
        refreshed = 0

        for i in range(len(minute_df) - 1, -1, -1):
            minute = _parse_minute_row(minute_df, i)
            if minute is None:
                continue
            dt_ns = int(
                minute["datetime"]
                .replace(tzinfo=TZ_SHANGHAI)
                .timestamp()
                * 1e9
            )
            if last_ns is not None and dt_ns <= last_ns:
                refreshed += 1
                if refreshed > self._MINUTE_TAIL_REFRESH:
                    break
            pending.append(minute)

        if not pending:
            return

        pending.reverse()
        touched_days: set[date] = set()
        day_buf = self._day_minutes[main_symbol]
        newest_ns = last_ns
        changed = False

        for minute in pending:
            dt_ns = int(
                minute["datetime"]
                .replace(tzinfo=TZ_SHANGHAI)
                .timestamp()
                * 1e9
            )
            trade_date = minute["datetime"].date()
            day_list = day_buf.setdefault(trade_date, [])
            if self._upsert_day_minute(day_list, minute):
                changed = True
                touched_days.add(trade_date)
            if newest_ns is None or dt_ns > newest_ns:
                newest_ns = dt_ns

        if not changed:
            return

        self._last_minute_ns[main_symbol] = newest_ns
        for trade_date in touched_days:
            self._sync_day_bars(main_symbol, trade_date)

    def advance_visible(self, main_symbol: str, current_ns: int) -> int:
        """返回新解锁的 30 分钟 bar 数量（可能 >1，需逐根处理策略）。"""
        rows = self._session_rows[main_symbol]
        visible = 0
        for bar in rows:
            if _bar_end_ns(bar) <= current_ns:
                visible += 1
            else:
                break

        prev = self._visible_counts[main_symbol]
        if visible <= prev:
            return 0

        self._visible_counts[main_symbol] = visible
        self.session_klines[main_symbol] = bars_to_kline_dataframe(rows[:visible])
        return visible - prev

    def iter_new_bars(self, main_symbol: str, current_ns: int):
        """按回测时钟逐根 yield 新完成的 session K 线（含完整历史前缀）。"""
        self.ingest_minutes(main_symbol)
        rows = self._session_rows[main_symbol]
        visible = 0
        for bar in rows:
            if _bar_end_ns(bar) <= current_ns:
                visible += 1
            else:
                break

        prev = self._visible_counts[main_symbol]
        if visible <= prev:
            return

        for end in range(prev + 1, visible + 1):
            self._visible_counts[main_symbol] = end
            klines = bars_to_kline_dataframe(rows[:end])
            self.session_klines[main_symbol] = klines
            yield klines

    def sync(self, main_symbol: str, current_ns: int) -> bool:
        """兼容旧接口：仅判断是否有新 bar。"""
        self.ingest_minutes(main_symbol)
        return self.advance_visible(main_symbol, current_ns) > 0

    def get_klines(self, main_symbol: str) -> pd.DataFrame:
        return self.session_klines[main_symbol]
