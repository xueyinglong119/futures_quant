"""基于 MySQL 水位的头肩监控（不订天勤 K 线）。

ingest 服务负责写库；本模块轮询各品种/周期最新 datetime_end，
发现新收盘 K 后跑头肩告警与模拟盘。天勤连接仅用于开仓时取合约保证金字段。
"""

from __future__ import annotations

import time
import traceback
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from tqsdk import TqApi, TqAuth

from config.settings import Settings, load_settings
from core.cont_symbols import list_commodity_mains
from core.hs_monitor import HS_MONITOR_DURATIONS, HsMonitorEngine, load_tf_df
from core.kline_store import (
    DURATION_LABELS,
    connect,
    ensure_table,
    list_symbols,
    load_bars,
    load_watermarks,
)
from core.paper_trade import PaperBroker
from core.realtime_pipeline import TZ, _as_naive, in_trading_window

POLL_SEC = 0.5
IDLE_SEC = 15.0


class DbHsMonitor:
    def __init__(
        self,
        settings: Settings | None = None,
        alert_log: Path | None = None,
        *,
        poll_sec: float = POLL_SEC,
    ):
        self.settings = settings or load_settings()
        self.alert_log = alert_log
        self.poll_sec = poll_sec
        self.engine = HsMonitorEngine()
        self.broker: PaperBroker | None = None
        self._seen: dict[tuple[str, int], datetime] = {}
        self._primed = False

    def run_forever(self) -> None:
        cfg = self.settings
        api = TqApi(auth=TqAuth(cfg.auth.user, cfg.auth.password))
        conn = connect()
        ensure_table(conn)
        self.engine.on_signal = None
        try:
            self.broker = PaperBroker(conn, api)
            self.engine.on_signal = self.broker.on_signal
            print("[monitor] 模拟账户已挂接", flush=True)
        except Exception:
            traceback.print_exc()
            print("[monitor] 模拟账户初始化失败，本进程只告警不开仓", flush=True)
            self.broker = None
        try:
            print(
                f"[monitor] 启动 周期={','.join(DURATION_LABELS[d] for d in HS_MONITOR_DURATIONS)} "
                f"poll={self.poll_sec}s（只读库，不订 K 线）",
                flush=True,
            )
            while True:
                now = datetime.now(TZ)
                if not in_trading_window(now):
                    print(
                        f"[monitor] 休市 {now:%Y-%m-%d %H:%M:%S}，{IDLE_SEC:.0f}s 后再检查",
                        flush=True,
                    )
                    time.sleep(IDLE_SEC)
                    continue
                try:
                    alerts = self._tick(conn, api)
                    if alerts:
                        print(f"[monitor] 本轮新告警 {alerts}", flush=True)
                except Exception:
                    traceback.print_exc()
                    print("[monitor] 本轮异常，短暂退避", flush=True)
                    time.sleep(3)
                    try:
                        conn.ping(reconnect=True)
                    except Exception:
                        conn.close()
                        conn = connect()
                        if self.broker is not None:
                            self.broker.conn = conn
                time.sleep(self.poll_sec)
        finally:
            conn.close()
            api.close()

    def _symbols(self, conn, api: TqApi) -> list[str]:
        try:
            return list_commodity_mains(api)
        except Exception:
            traceback.print_exc()
            return list_symbols(conn, duration_sec=60) or list_symbols(conn)

    def _prime(self, conn, symbols: list[str]) -> None:
        """启动时对齐水位，不发历史告警。"""
        marks = load_watermarks(conn, HS_MONITOR_DURATIONS, symbols)
        for key, dt_end in marks.items():
            self._seen[key] = _as_naive(dt_end)
            symbol, dur = key
            df = load_tf_df(symbol, dur, conn=conn)
            if df is not None and not df.empty:
                self.engine.evaluate_df(
                    symbol,
                    dur,
                    df,
                    emit=False,
                    push=False,
                    toast=False,
                )
        print(
            f"[monitor] 水位预热 symbols≈{len(symbols)} keys={len(self._seen)}",
            flush=True,
        )
        self._primed = True

    def _tick(self, conn, api: TqApi) -> int:
        symbols = self._symbols(conn, api)
        if not self._primed:
            self._prime(conn, symbols)
            return 0

        marks = load_watermarks(conn, HS_MONITOR_DURATIONS, symbols)
        alerts = 0
        for (symbol, dur), dt_end in marks.items():
            last = _as_naive(dt_end)
            prev = self._seen.get((symbol, dur))
            if prev is not None and last <= prev:
                continue
            self._seen[(symbol, dur)] = last
            df = load_tf_df(symbol, dur, conn=conn)
            if df is None or df.empty:
                continue
            # 模拟盘：用最新一根 OHLC 推进止盈止损
            if self.broker is not None:
                bar_rows = load_bars(conn, symbol, dur, limit=1)
                if bar_rows:
                    b = bar_rows[-1]
                    try:
                        self.broker.on_bar(
                            symbol,
                            dur,
                            _as_naive(b["datetime_end"]),
                            b,
                        )
                    except Exception:
                        traceback.print_exc()
            n = self.engine.evaluate_df(
                symbol,
                dur,
                df,
                alert_log=self.alert_log,
                push=True,
                toast=False,
                emit=True,
            )
            if n:
                label = DURATION_LABELS.get(dur, str(dur))
                print(
                    f"[monitor] 告警 {symbol} {label} @ {last:%Y-%m-%d %H:%M}",
                    flush=True,
                )
            alerts += n
        return alerts
