"""开盘实时链路：订阅 K 线 → 收盘写入 MySQL → 头肩检测告警。

只把已经走完的 K 线入库并参与检测。正在走的那根只留在行情里，收盘后下一拍写入。
时间戳沿用该品种该周期库里已有的钟（UTC 或北京时间），避免同一根 K 线写两遍。
"""

from __future__ import annotations

import math
import traceback
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
from tqsdk import TqApi, TqAuth

from config.settings import Settings, load_settings
from core.cont_symbols import list_commodity_mains
from core.hs_monitor import (
    HS_MONITOR_DURATIONS,
    TF_CONFIGS,
    HsMonitorEngine,
    bars_to_strategy_df,
    load_tf_df,
)
from core.kline_fetch import wait_klines_ready
from core.paper_trade import PaperBroker
from core.kline_store import (
    DURATION_LABELS,
    connect,
    ensure_table,
    load_bars,
    upsert_bars,
)

TZ = ZoneInfo("Asia/Shanghai")

# 覆盖周五收盘到周一上午的缺口，同时给检测留够回看
TAIL_LENGTH: dict[int, int] = {
    60: 2000,
    300: 600,
    1800: 300,
    3600: 200,
    7200: 120,
    86400: 40,
}


def in_trading_window(now: datetime) -> bool:
    """商品期货可能有成交的时段（日盘、夜盘，含开盘前 2 分钟）。

    11:30–13:30 午休不看行情。上午留到 11:35，让 11:30 那根收完；
    下午 13:28 再连，留出重新订阅的时间。
    """
    if now.tzinfo is None:
        now = now.replace(tzinfo=TZ)
    else:
        now = now.astimezone(TZ)
    wd = now.weekday()  # 周一=0 … 周日=6
    hm = now.hour * 60 + now.minute
    if hm >= 20 * 60 + 58:
        return wd in (6, 0, 1, 2, 3, 4)  # 周日到周五夜盘
    if hm <= 2 * 60 + 40:
        return wd in (0, 1, 2, 3, 4, 5)  # 夜盘延续到次日凌晨，含周六凌晨
    if wd > 4:
        return False
    if 8 * 60 + 58 <= hm <= 11 * 60 + 35:
        return True
    if 13 * 60 + 28 <= hm <= 15 * 60 + 5:
        return True
    return False


def _as_naive(dt: datetime) -> datetime:
    if isinstance(dt, pd.Timestamp):
        dt = dt.to_pydatetime()
    if dt.tzinfo is not None:
        dt = dt.replace(tzinfo=None)
    return dt.replace(microsecond=0)


def _ns_to_utc(ns: int) -> datetime:
    return _as_naive(pd.to_datetime(int(ns), unit="ns").to_pydatetime())


def _ns_to_beijing(ns: int) -> datetime:
    return datetime.fromtimestamp(int(ns) / 1e9, tz=TZ).replace(tzinfo=None, microsecond=0)


def _ohlc_equal(a: dict, b: dict) -> bool:
    for key in ("open", "high", "low", "close"):
        if abs(float(a[key]) - float(b[key])) > 1e-4:
            return False
    return True


def _in_beijing_session(dt: datetime) -> bool:
    """这个钟点落在商品期货日盘或夜盘里（含 02:30 前的夜盘延续）。"""
    hm = dt.hour * 60 + dt.minute
    if 9 * 60 <= hm <= 11 * 60 + 30:
        return True
    if 13 * 60 + 30 <= hm <= 15 * 60:
        return True
    if 21 * 60 <= hm <= 23 * 60 + 59:
        return True
    if hm <= 2 * 60 + 30:
        return True
    return False


def detect_clock(db_bars: list[dict], serial_rows: list[dict]) -> str:
    """库里这根 K 线的时间是 UTC 裸时间还是北京时间。

    先用 OHLC 对上序列里的同一根。对不上时，只要库里出现交易时段以外的钟点
    （例如 1 小时线的 05:00），就按 UTC 续写，避免同一根 K 线再插一条北京时间。
    """
    if not db_bars:
        return "beijing"
    for db_bar in reversed(db_bars):
        db_dt = _as_naive(db_bar["datetime_end"])
        for row in serial_rows:
            if not _ohlc_equal(db_bar, row):
                continue
            if row["utc"] == db_dt:
                return "utc"
            if row["beijing"] == db_dt:
                return "beijing"
    for db_bar in db_bars:
        if not _in_beijing_session(_as_naive(db_bar["datetime_end"])):
            return "utc"
    return "beijing"


def _valid_ns(ns) -> bool:
    if ns is None or (isinstance(ns, float) and math.isnan(ns)):
        return False
    return True


def serial_to_rows(klines: pd.DataFrame, duration_sec: int, now_utc: datetime) -> list[dict]:
    """解析序列。最后一根若还没收盘，不放进结果。"""
    if klines is None or len(klines) == 0:
        return []
    parsed: list[dict] = []
    for i in range(len(klines)):
        ns = klines.iloc[i]["datetime"]
        if not _valid_ns(ns):
            continue
        close = klines.iloc[i]["close"]
        if close is None or (isinstance(close, float) and math.isnan(close)):
            continue
        oi = klines.iloc[i]["open_oi"]
        if isinstance(oi, float) and math.isnan(oi):
            oi = 0
        vol = klines.iloc[i]["volume"]
        if isinstance(vol, float) and math.isnan(vol):
            vol = 0
        start_utc = _ns_to_utc(int(ns))
        parsed.append(
            {
                "utc": start_utc,
                "beijing": _ns_to_beijing(int(ns)),
                "open": float(klines.iloc[i]["open"]),
                "high": float(klines.iloc[i]["high"]),
                "low": float(klines.iloc[i]["low"]),
                "close": float(close),
                "volume": int(vol),
                "open_oi": int(oi),
            }
        )
    if not parsed:
        return []
    closed = parsed[:-1]
    last = parsed[-1]
    # 收盘后若不再长出新 K 线（小节结束），晚 3 秒把最后一根也收进来
    if last["utc"] + timedelta(seconds=duration_sec + 3) <= now_utc:
        closed.append(last)
    return closed


@dataclass
class _SeriesState:
    clock: str | None = None
    watermark: datetime | None = None
    frame: pd.DataFrame = field(default_factory=pd.DataFrame)


class RealtimePipeline:
    def __init__(self, settings: Settings | None = None, alert_log: Path | None = None):
        self.settings = settings or load_settings()
        self.alert_log = alert_log
        self.engine = HsMonitorEngine()
        self.broker: PaperBroker | None = None
        self._states: dict[tuple[str, int], _SeriesState] = {}

    def run_session(self) -> None:
        """连上行情，订阅商品主力多周期，直到离开交易时段。"""
        cfg = self.settings
        api = TqApi(auth=TqAuth(cfg.auth.user, cfg.auth.password))
        conn = connect()
        ensure_table(conn)
        self.engine.on_signal = None
        try:
            self.broker = PaperBroker(conn, api)
            self.engine.on_signal = self.broker.on_signal
        except Exception:
            traceback.print_exc()
            print("[paper] 模拟账户初始化失败，本时段只告警不开仓", flush=True)
            self.broker = None
            self.engine.on_signal = None
        try:
            symbols = list_commodity_mains(api)
            print(f"[pipeline] 品种 {len(symbols)}", flush=True)
            serials: dict[tuple[str, int], pd.DataFrame] = {}
            for symbol in symbols:
                for dur in HS_MONITOR_DURATIONS:
                    length = TAIL_LENGTH.get(dur, 200)
                    try:
                        serials[(symbol, dur)] = api.get_kline_serial(
                            symbol, dur, data_length=length
                        )
                    except Exception as exc:  # noqa: BLE001
                        print(f"[pipeline] 订阅失败 {symbol} {dur}: {exc}", flush=True)
            print(f"[pipeline] 已订阅 {len(serials)} 条序列，等待行情...", flush=True)
            wait_klines_ready(api, list(serials.values()), timeout_sec=180)
            self._catch_up(conn, serials)
            while in_trading_window(datetime.now(TZ)):
                api.wait_update(deadline=time_monotonic_deadline(api, 5))
                changed = [key for key, kl in serials.items() if api.is_changing(kl)]
                if not changed:
                    continue
                wrote = 0
                alerts = 0
                for key in changed:
                    n, a = self._ingest(conn, key[0], key[1], serials[key])
                    wrote += n
                    alerts += a
                if wrote or alerts:
                    print(
                        f"[pipeline] 入库 {wrote} 根，新告警 {alerts}",
                        flush=True,
                    )
        finally:
            conn.close()
            api.close()

    def _catch_up(self, conn, serials: dict[tuple[str, int], pd.DataFrame]) -> None:
        wrote = 0
        alerts = 0
        for (symbol, dur), kl in serials.items():
            n, a = self._ingest(conn, symbol, dur, kl)
            wrote += n
            alerts += a
        print(f"[pipeline] 启动补齐 入库 {wrote} 根，新告警 {alerts}", flush=True)

    def _ingest(self, conn, symbol: str, duration_sec: int, klines: pd.DataFrame) -> tuple[int, int]:
        now_utc = datetime.now(timezone.utc).replace(tzinfo=None, microsecond=0)
        closed = serial_to_rows(klines, duration_sec, now_utc)
        if not closed:
            return 0, 0
        state = self._states.get((symbol, duration_sec))
        if state is None:
            state = self._load_state(conn, symbol, duration_sec, closed)
            self._states[(symbol, duration_sec)] = state
        clock = state.clock or "beijing"
        fresh: list[dict] = []
        for row in closed:
            stored = row["utc"] if clock == "utc" else row["beijing"]
            if state.watermark is not None and stored <= state.watermark:
                continue
            fresh.append({**row, "stored": stored})
        if not fresh:
            return 0, 0
        fresh.sort(key=lambda r: r["stored"])
        upsert_bars(
            conn,
            symbol,
            duration_sec,
            [
                {
                    "datetime_end": row["stored"],
                    "open": row["open"],
                    "high": row["high"],
                    "low": row["low"],
                    "close": row["close"],
                    "volume": row["volume"],
                    "open_oi": row["open_oi"],
                }
                for row in fresh
            ],
        )
        alerts = 0
        lookback = TF_CONFIGS[duration_sec].lookback_bars
        for row in fresh:
            state.frame = _append_frame(state.frame, row["stored"], row, lookback)
            state.watermark = row["stored"]
            if self.broker is not None:
                try:
                    self.broker.on_bar(symbol, duration_sec, row["stored"], row)
                except Exception:
                    traceback.print_exc()
            alerts += self.engine.evaluate_df(
                symbol,
                duration_sec,
                state.frame,
                alert_log=self.alert_log,
                push=True,
                toast=False,
                emit=True,
            )
        label = DURATION_LABELS.get(duration_sec, str(duration_sec))
        if alerts:
            print(
                f"[pipeline] {symbol} {label} 新 K 线 {len(fresh)} 根，告警 {alerts}",
                flush=True,
            )
        return len(fresh), alerts

    def _load_state(
        self,
        conn,
        symbol: str,
        duration_sec: int,
        closed: list[dict],
    ) -> _SeriesState:
        recent = load_bars(conn, symbol, duration_sec, limit=40)
        clock = detect_clock(recent, closed)
        frame = load_tf_df(symbol, duration_sec, conn=conn)
        watermark = None
        if frame is not None and not frame.empty:
            watermark = _as_naive(pd.Timestamp(frame.iloc[-1]["datetime"]).to_pydatetime())
            self.engine._last_bar_dt[self.engine._key(symbol, duration_sec)] = watermark
        elif recent:
            watermark = _as_naive(recent[-1]["datetime_end"])
        return _SeriesState(clock=clock, watermark=watermark, frame=frame)


def _append_frame(frame: pd.DataFrame, stored: datetime, row: dict, lookback: int) -> pd.DataFrame:
    extra = bars_to_strategy_df(
        [
            {
                "datetime_end": stored,
                "open": row["open"],
                "high": row["high"],
                "low": row["low"],
                "close": row["close"],
                "volume": row["volume"],
                "open_oi": row["open_oi"],
            }
        ]
    )
    if frame is None or frame.empty:
        merged = extra
    else:
        merged = pd.concat([frame, extra], ignore_index=True)
    if len(merged) > lookback:
        merged = merged.iloc[-lookback:].reset_index(drop=True)
    return merged


def time_monotonic_deadline(api: TqApi, seconds: float) -> float:
    return float(api._loop.time()) + seconds
