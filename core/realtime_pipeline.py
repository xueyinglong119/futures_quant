"""开盘实时链路：订阅 K 线 → 收盘写入 MySQL → 头肩检测告警。

入库周期：1/3/5/10/15 分、日/周（直接订阅天勤）。
30m/1h/2h/3h/4h 不按天勤整点写库，而在 1 分钟收盘后按国内交易时段从 1m 合成：
  日盘 1h: 10:00 / 11:15 / 14:15 / 15:00
  日盘 2h: 11:15 / 15:00；3h: 14:15 / 15:00；4h: 15:00
月/季/年在日线收盘后由日线聚合。头肩仍只跑 HS_MONITOR_DURATIONS。

只把已经走完的 K 线入库。正在走的那根只留在行情里，收盘后下一拍写入。
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
from core.kline_fetch import (
    _is_ready,
    aggregate_monthly_from_daily,
    aggregate_quarterly_from_daily,
    aggregate_yearly_from_daily,
)
from core.paper_trade import PaperBroker
from core.kline_store import (
    DURATION_1D,
    DURATION_1M,
    DURATION_1MON,
    DURATION_1Q,
    DURATION_1Y,
    DURATION_LABELS,
    INGEST_DURATIONS,
    SESSION_BUILT_DURATIONS,
    connect,
    ensure_table,
    load_bars,
    upsert_bars,
)
from core.session_rebuild import refresh_symbol_session_recent
from utils.trading_session import get_all_session_trigger_minutes

TZ = ZoneInfo("Asia/Shanghai")

# 天勤直播序列只要短窗口：发现新收盘 K；头肩回看从 MySQL 加载，避免每次重启重拉几百根
LIVE_TAIL_LENGTH: dict[int, int] = {
    60: 60,         # 1m
    180: 40,        # 3m
    300: 40,        # 5m
    600: 30,        # 10m
    900: 30,        # 15m
    86400: 5,       # 1d
    604800: 4,      # 1w
}
# 非头肩周期内存裁剪用
TAIL_LENGTH = LIVE_TAIL_LENGTH

# 订阅阶段每批挂接品种数（订齐前不做入库/告警，尽量快挂满）
SUBSCRIBE_BATCH = 16

# 1m 走到这些北京时刻时，刷新会话 30m/1h/2h/3h/4h
_SESSION_REFRESH_MINUTES = get_all_session_trigger_minutes()
_SESSION_HS_DURS = tuple(
    d for d in SESSION_BUILT_DURATIONS if d in HS_MONITOR_DURATIONS
)


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


def _bar_payload_equal(a: dict, b: dict) -> bool:
    """判断两根 K 是否同一最终态（含量/仓，用于避免半截数据锁死）。"""
    if not _ohlc_equal(a, b):
        return False
    if int(a.get("volume") or 0) != int(b.get("volume") or 0):
        return False
    if int(a.get("open_oi") or 0) != int(b.get("open_oi") or 0):
        return False
    return True


# 最后一根：墙钟过结束时刻后再等这么久才当收盘（防半截；已出现下一根时不依赖此值）
_BAR_CLOSE_GRACE_SEC = 0.5


def _bar_closed(start_utc: datetime, duration_sec: int, now_utc: datetime) -> bool:
    """按墙钟判断该根是否已走完；未完成的只留在行情序列里，不入库。"""
    return (
        start_utc + timedelta(seconds=duration_sec + _BAR_CLOSE_GRACE_SEC) <= now_utc
    )


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
    """解析序列，只返回已收盘 K；正在走的最后一根不入库。

    - 已被下一根顶替的（[:-1]）视为收盘。
    - 序列最后一根仅当墙钟已过结束时刻 + 0.5 秒才入库（小节/收盘后收尾）。
    """
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
    closed = list(parsed[:-1])
    last = parsed[-1]
    if _bar_closed(last["utc"], duration_sec, now_utc):
        closed.append(last)
    return closed


@dataclass
class _SeriesState:
    clock: str | None = None
    watermark: datetime | None = None
    frame: pd.DataFrame = field(default_factory=pd.DataFrame)
    last_payload: dict | None = None


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
            ingest_labels = [DURATION_LABELS[d] for d in INGEST_DURATIONS]
            print(
                f"[pipeline] 品种 {len(symbols)}，入库周期 {','.join(ingest_labels)} "
                f"（先订满再监控；直播短窗口；回看用库；30m/1h/2h/3h/4h 由 1m 合成）",
                flush=True,
            )
            serials: dict[tuple[str, int], pd.DataFrame] = {}
            pending = list(symbols)
            armed: set[tuple[str, int]] = set()
            bootstrapped: set[str] = set()
            bootstrap_queue: list[str] = []
            total = len(symbols)
            subscribed_n = 0
            initial_catchup_done = False

            # —— 阶段1：只挂订阅，不入库/不告警/不做会话合成 ——
            while pending and in_trading_window(datetime.now(TZ)):
                batch = pending[:SUBSCRIBE_BATCH]
                del pending[:SUBSCRIBE_BATCH]
                for symbol in batch:
                    for dur in INGEST_DURATIONS:
                        length = LIVE_TAIL_LENGTH.get(dur, 30)
                        try:
                            serials[(symbol, dur)] = api.get_kline_serial(
                                symbol, dur, data_length=length
                            )
                        except Exception as exc:  # noqa: BLE001
                            print(
                                f"[pipeline] 订阅失败 {symbol} "
                                f"{DURATION_LABELS.get(dur, dur)}: {exc}",
                                flush=True,
                            )
                    try:
                        api.wait_update(
                            deadline=time_monotonic_deadline(api, 0.05)
                        )
                    except Exception:
                        pass
                subscribed_n += len(batch)
                # 轻量泵一下行情，让序列开始填充，但仍不 ingest
                try:
                    api.wait_update(deadline=time_monotonic_deadline(api, 0.2))
                except Exception:
                    pass
                ready_n = sum(1 for kl in serials.values() if _is_ready(kl))
                print(
                    f"[pipeline] 订阅进度 {subscribed_n}/{total} "
                    f"序列 {len(serials)} 已就绪 {ready_n} "
                    f"本批 {batch[0]}..{batch[-1]}",
                    flush=True,
                )

            if not in_trading_window(datetime.now(TZ)):
                return

            print(
                f"[pipeline] 订阅已挂满 {total}，等待序列就绪后开始监控",
                flush=True,
            )

            while in_trading_window(datetime.now(TZ)):
                api.wait_update(deadline=time_monotonic_deadline(api, 0.25))

                newly: list[tuple[str, int]] = []
                for key, kl in serials.items():
                    if key in armed:
                        continue
                    if _is_ready(kl):
                        armed.add(key)
                        newly.append(key)

                # 首次：对已就绪序列做一轮补齐；未就绪的稍后纳入
                if newly and not initial_catchup_done:
                    wrote = 0
                    alerts = 0
                    for symbol, dur in newly:
                        n, a = self._ingest(
                            conn,
                            symbol,
                            dur,
                            serials[(symbol, dur)],
                            session_refresh=False,
                        )
                        wrote += n
                        alerts += a
                        if (
                            dur == DURATION_1M
                            and symbol not in bootstrapped
                            and symbol not in bootstrap_queue
                        ):
                            bootstrap_queue.append(symbol)
                    print(
                        f"[pipeline] 启动补齐 新就绪 {len(newly)}，监控中 {len(armed)}/"
                        f"{len(serials)}，入库 {wrote}，告警 {alerts}",
                        flush=True,
                    )
                    if len(armed) >= len(serials):
                        initial_catchup_done = True
                        print("[pipeline] 全部序列就绪，进入实时监控", flush=True)
                    newly = []  # 本批已 ingest，避免下面重复

                if newly and initial_catchup_done:
                    wrote = 0
                    alerts = 0
                    for symbol, dur in newly:
                        n, a = self._ingest(
                            conn,
                            symbol,
                            dur,
                            serials[(symbol, dur)],
                            session_refresh=True,
                        )
                        wrote += n
                        alerts += a
                        if (
                            dur == DURATION_1M
                            and symbol not in bootstrapped
                            and symbol not in bootstrap_queue
                        ):
                            bootstrap_queue.append(symbol)
                    print(
                        f"[pipeline] 新就绪 {len(newly)} 序列，监控中 {len(armed)}/"
                        f"{len(serials)}，补齐入库 {wrote}，告警 {alerts}",
                        flush=True,
                    )

                if not armed:
                    continue

                wrote = 0
                alerts = 0
                for key in list(armed):
                    kl = serials.get(key)
                    if kl is None or not api.is_changing(kl):
                        continue
                    n, a = self._ingest(
                        conn,
                        key[0],
                        key[1],
                        kl,
                        session_refresh=True,
                    )
                    wrote += n
                    alerts += a
                if wrote or alerts:
                    print(
                        f"[pipeline] 入库 {wrote} 根，新告警 {alerts}",
                        flush=True,
                    )

                # 分批会话 K 启动补齐（每圈少量）
                if bootstrap_queue:
                    chunk = bootstrap_queue[:4]
                    del bootstrap_queue[:4]
                    for symbol in chunk:
                        self._bootstrap_one_symbol(conn, symbol)
                        bootstrapped.add(symbol)
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

    def _ingest(
        self,
        conn,
        symbol: str,
        duration_sec: int,
        klines: pd.DataFrame,
        *,
        session_refresh: bool = True,
    ) -> tuple[int, int]:
        # 保留亚秒，配合 0.5s 收盘宽限；勿截成整秒否则宽限失效
        now_utc = datetime.now(timezone.utc).replace(tzinfo=None)
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
            if state.watermark is not None and stored < state.watermark:
                continue
            # 同时间戳：仅当 OHLC/量发生变化时覆盖（修正半截入库），否则跳过
            if state.watermark is not None and stored == state.watermark:
                last = state.last_payload or _frame_last_bar(state.frame)
                if last is not None and _bar_payload_equal(last, row):
                    continue
                fresh.append({**row, "stored": stored, "correction": True})
                continue
            fresh.append({**row, "stored": stored, "correction": False})
        if not fresh:
            return 0, 0
        fresh.sort(key=lambda r: r["stored"])
        alerts = 0
        do_hs = duration_sec in HS_MONITOR_DURATIONS
        lookback = (
            TF_CONFIGS[duration_sec].lookback_bars
            if do_hs
            else TAIL_LENGTH.get(duration_sec, 200)
        )
        # 先更新内存窗口并告警，再写库——缩短「K线时间 → 发现时间」
        for row in fresh:
            correction = bool(row.get("correction"))
            if do_hs:
                state.frame = _upsert_frame(state.frame, row["stored"], row, lookback)
            state.watermark = row["stored"]
            state.last_payload = {
                "open": row["open"],
                "high": row["high"],
                "low": row["low"],
                "close": row["close"],
                "volume": row["volume"],
                "open_oi": row["open_oi"],
            }
            # 修正半截 K：只更新检测窗口，不重复模拟成交/告警
            if correction:
                continue
            if not do_hs:
                continue
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
        if any(not r.get("correction") for r in fresh) and duration_sec == DURATION_1D:
            _refresh_htf_from_daily(conn, symbol)
        if session_refresh and duration_sec == DURATION_1M:
            self._maybe_refresh_session_from_1m(conn, symbol, fresh)
        label = DURATION_LABELS.get(duration_sec, str(duration_sec))
        if alerts:
            print(
                f"[pipeline] {symbol} {label} 新 K 线 {len(fresh)} 根，告警 {alerts}",
                flush=True,
            )
        return len(fresh), alerts

    def _bootstrap_one_symbol(self, conn, symbol: str) -> None:
        """某品种 1m 就绪后，用库内 1m 重算最近会话 30m/1h/2h/3h/4h。"""
        try:
            stats = refresh_symbol_session_recent(
                conn, symbol, around=datetime.now(TZ).replace(tzinfo=None)
            )
            parts = [
                f"{k}={int(stats.get(f'{k}_upserted') or 0)}"
                for k in ("30m", "1h", "2h", "3h", "4h")
                if int(stats.get(f"{k}_upserted") or 0)
            ]
            if parts:
                print(
                    f"[pipeline] {symbol} 会话K就绪 {' '.join(parts)}",
                    flush=True,
                )
            for dur in _SESSION_HS_DURS:
                frame = load_tf_df(symbol, dur, conn=conn)
                if frame is None or frame.empty:
                    continue
                state = self._states.get((symbol, dur))
                if state is None:
                    state = _SeriesState(clock="beijing")
                    self._states[(symbol, dur)] = state
                state.frame = frame
                state.clock = "beijing"
                state.watermark = _as_naive(
                    pd.Timestamp(frame.iloc[-1]["datetime"]).to_pydatetime()
                )
                state.last_payload = _frame_last_bar(frame)
                self.engine._last_bar_dt[
                    self.engine._key(symbol, dur)
                ] = state.watermark
        except Exception:
            traceback.print_exc()
            print(f"[pipeline] 会话K重建失败 {symbol}", flush=True)

    def _bootstrap_session_bars(self, conn, symbols: list[str]) -> None:
        """兼容：批量会话重建。"""
        for symbol in symbols:
            self._bootstrap_one_symbol(conn, symbol)

    def _maybe_refresh_session_from_1m(
        self, conn, symbol: str, fresh_1m: list[dict]
    ) -> None:
        """1m 收盘落在小节边界时，重算并写入会话周期，再跑头肩。"""
        need = False
        pivot: datetime | None = None
        for row in fresh_1m:
            if row.get("correction"):
                continue
            stored = _as_naive(row["stored"])
            # 统一到北京时刻判断触发点
            bj = stored
            if not _in_beijing_session(bj):
                # 库内若是 UTC，换到北京再比
                bj = stored + timedelta(hours=8)
            if bj.time().replace(second=0, microsecond=0) in _SESSION_REFRESH_MINUTES:
                need = True
                pivot = bj
        if not need or pivot is None:
            return
        try:
            stats = refresh_symbol_session_recent(conn, symbol, around=pivot)
        except Exception:
            traceback.print_exc()
            return
        parts = []
        for key in ("30m", "1h", "2h", "3h", "4h"):
            n = int(stats.get(f"{key}_upserted") or 0)
            if n:
                parts.append(f"{key}={n}")
        if parts:
            print(
                f"[pipeline] {symbol} 会话合成 {' '.join(parts)} @ {pivot:%H:%M}",
                flush=True,
            )
        for dur in _SESSION_HS_DURS:
            self._evaluate_session_tf(conn, symbol, dur)

    def _evaluate_session_tf(self, conn, symbol: str, duration_sec: int) -> int:
        """会话 K 写入后，刷新检测窗口并评估最新一根。"""
        frame = load_tf_df(symbol, duration_sec, conn=conn)
        if frame is None or frame.empty:
            return 0
        state = self._states.get((symbol, duration_sec))
        if state is None:
            state = _SeriesState(clock="beijing")
            self._states[(symbol, duration_sec)] = state
        last_dt = _as_naive(pd.Timestamp(frame.iloc[-1]["datetime"]).to_pydatetime())
        last_payload = _frame_last_bar(frame)
        prev_wm = state.watermark
        state.frame = frame
        state.clock = "beijing"
        state.watermark = last_dt
        state.last_payload = last_payload
        if prev_wm is not None and last_dt <= prev_wm:
            return 0
        self.engine._last_bar_dt[self.engine._key(symbol, duration_sec)] = (
            prev_wm if prev_wm is not None else last_dt - timedelta(seconds=1)
        )
        if self.broker is not None and last_payload is not None:
            try:
                self.broker.on_bar(symbol, duration_sec, last_dt, last_payload)
            except Exception:
                traceback.print_exc()
        return self.engine.evaluate_df(
            symbol,
            duration_sec,
            frame,
            alert_log=self.alert_log,
            push=True,
            toast=False,
            emit=True,
        )

    def _load_state(
        self,
        conn,
        symbol: str,
        duration_sec: int,
        closed: list[dict],
    ) -> _SeriesState:
        recent = load_bars(conn, symbol, duration_sec, limit=40)
        clock = detect_clock(recent, closed)
        frame = pd.DataFrame()
        watermark = None
        last_payload = None
        if duration_sec in HS_MONITOR_DURATIONS:
            frame = load_tf_df(symbol, duration_sec, conn=conn)
            if frame is not None and not frame.empty:
                watermark = _as_naive(
                    pd.Timestamp(frame.iloc[-1]["datetime"]).to_pydatetime()
                )
                self.engine._last_bar_dt[
                    self.engine._key(symbol, duration_sec)
                ] = watermark
                last_payload = _frame_last_bar(frame)
        if watermark is None and recent:
            watermark = _as_naive(recent[-1]["datetime_end"])
            last_payload = {
                "open": recent[-1]["open"],
                "high": recent[-1]["high"],
                "low": recent[-1]["low"],
                "close": recent[-1]["close"],
                "volume": recent[-1]["volume"],
                "open_oi": recent[-1]["open_oi"],
            }
        return _SeriesState(
            clock=clock,
            watermark=watermark,
            frame=frame,
            last_payload=last_payload,
        )


def _refresh_htf_from_daily(conn, symbol: str) -> None:
    """日线有新收盘后，重算月/季/年并入库。"""
    daily = load_bars(conn, symbol, DURATION_1D)
    if not daily:
        return
    upsert_bars(conn, symbol, DURATION_1MON, aggregate_monthly_from_daily(daily))
    upsert_bars(conn, symbol, DURATION_1Q, aggregate_quarterly_from_daily(daily))
    upsert_bars(conn, symbol, DURATION_1Y, aggregate_yearly_from_daily(daily))


def _frame_last_bar(frame: pd.DataFrame) -> dict | None:
    if frame is None or frame.empty:
        return None
    last = frame.iloc[-1]
    return {
        "open": float(last["open"]),
        "high": float(last["high"]),
        "low": float(last["low"]),
        "close": float(last["close"]),
        "volume": int(last.get("volume") or 0),
        "open_oi": int(last.get("open_oi") or 0),
    }


def _upsert_frame(
    frame: pd.DataFrame, stored: datetime, row: dict, lookback: int
) -> pd.DataFrame:
    """追加新 K，或覆盖同时间戳末根（最终态修正）。"""
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
        last_dt = _as_naive(pd.Timestamp(frame.iloc[-1]["datetime"]).to_pydatetime())
        if last_dt == _as_naive(stored):
            merged = pd.concat([frame.iloc[:-1], extra], ignore_index=True)
        else:
            merged = pd.concat([frame, extra], ignore_index=True)
    if len(merged) > lookback:
        merged = merged.iloc[-lookback:].reset_index(drop=True)
    return merged


def time_monotonic_deadline(api: TqApi, seconds: float) -> float:
    return float(api._loop.time()) + seconds
