"""基于 MySQL 水位的头肩监控（不订天勤 K 线）。

ingest 服务负责写库；本模块轮询各品种/周期最新 datetime_end，
发现新收盘 K 后跑头肩告警与模拟盘。天勤连接仅用于开仓时取合约保证金字段。
收盘前 15 分钟另扫一遍未收线的会话 K，出抢跑提醒（只提醒，不开仓）。
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
from core.hs_fanout import HsFanout, KeyResult, detect_one
from core.hs_monitor import (
    HS_MONITOR_DURATIONS,
    PROVISIONAL_DURATIONS,
    HsMonitorEngine,
    in_provisional_window,
)
from core.kline_store import (
    DURATION_LABELS,
    connect,
    ensure_table,
    list_symbols,
    load_watermarks,
)
from core.paper_trade import PaperBroker
from core.realtime_pipeline import (
    TZ,
    _as_naive,
    in_trading_window,
    session_bar_open,
)

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
        # 抢跑专用引擎：半截 K 的结构、`_traded_patterns` 都不能碰正式路径那套实例。
        # 它的 on_signal 永远是 None —— 抢跑只提醒，不开仓。
        self.prov_engine = HsMonitorEngine()
        # 子进程在 run_forever 里、api 与库都起来之后才拉起（见那里）。
        self.fanout: HsFanout | None = None
        self.broker: PaperBroker | None = None
        self._seen: dict[tuple[str, int], datetime] = {}
        self._prov_minute: tuple[int, ...] | None = None
        self._primed = False

    def run_forever(self) -> None:
        cfg = self.settings
        api = TqApi(auth=TqAuth(cfg.auth.user, cfg.auth.password))
        conn = connect()
        ensure_table(conn)
        self.engine.on_signal = None
        try:
            # 分片 worker 在 HsFanout 构造时就 spawn，所以必须等 api 和库都起来之后再拉：
            # 启动失败的出路是外层循环 15s 后新建一个 DbHsMonitor，提前 spawn 会每次漏 3 个子进程。
            self.fanout = HsFanout()
        except Exception:
            traceback.print_exc()
            self.fanout = None
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
                f"poll={self.poll_sec}s workers={self.fanout.active_workers if self.fanout else 1}"
                "（只读库，不订 K 线）",
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
            if self.fanout is not None:
                self.fanout.close()
            conn.close()
            api.close()

    def _symbols(self, conn, api: TqApi) -> list[str]:
        try:
            return list_commodity_mains(api)
        except Exception:
            traceback.print_exc()
            return list_symbols(conn, duration_sec=60) or list_symbols(conn)

    def _pending_keys(self, marks: dict) -> list[tuple[str, int, bool]]:
        """水位前进、且不是半截会话 K 的键；顺手记水位，保证每根只算一次。"""
        now = datetime.now(TZ).replace(tzinfo=None)
        pending: list[tuple[str, int, bool]] = []
        for (symbol, dur), dt_end in marks.items():
            last = _as_naive(dt_end)
            prev = self._seen.get((symbol, dur))
            if prev is not None and last <= prev:
                continue
            # 半截会话 K：不告警、不开仓、也不记水位，等它真正收线
            if session_bar_open(dur, last, now):
                continue
            self._seen[(symbol, dur)] = last
            pending.append((symbol, dur, False))
        return pending

    def _provisional_keys(self, marks: dict, now: datetime) -> list[tuple[str, int, bool]]:
        """收盘前 15 分钟，把还开着的 30m/1h/2h 半截 K 也送去检测，只出抢跑提醒。

        半截 K 的内容每根 1 分钟才变一次，所以按分钟节流，别在 0.5s 的轮询里重复审。
        """
        if not in_provisional_window(now):
            self._prov_minute = None
            return []
        minute = (now.year, now.month, now.day, now.hour, now.minute)
        if self._prov_minute == minute:
            return []
        self._prov_minute = minute
        out: list[tuple[str, int, bool]] = []
        for (symbol, dur), dt_end in marks.items():
            if dur not in PROVISIONAL_DURATIONS:
                continue
            # 只扫真·半截的：水位已到收线时刻的交给常规路径，别重复出告警
            if not session_bar_open(dur, _as_naive(dt_end), now):
                continue
            out.append((symbol, dur, True))
        return out

    def _scan_stats(self, results: list[KeyResult]) -> str:
        if self.fanout is None:
            return "workers=1 serial"
        return self.fanout.stats(results)

    def _detect(self, conn, keys: list[tuple[str, int, bool]]) -> list[KeyResult]:
        """分片子进程只算形态；分片不可用就在本进程串行兜底，副作用永远在父进程。"""
        if self.fanout is not None:
            results = self.fanout.detect(keys)
            if results is not None:
                return results
        out: list[KeyResult] = []
        for symbol, dur, prov in keys:
            res = detect_one(
                self.prov_engine if prov else self.engine,
                conn,
                symbol,
                dur,
                force=prov,
                prov=prov,
            )
            if res is not None:
                out.append(res)
        return out

    def _prime(self, conn, symbols: list[str]) -> None:
        """启动时对齐水位，不发历史告警。"""
        marks = load_watermarks(conn, HS_MONITOR_DURATIONS, symbols)
        keys = self._pending_keys(marks)
        t0 = time.perf_counter()
        results = self._detect(conn, keys)
        print(
            f"[monitor] 水位预热 symbols≈{len(symbols)} keys={len(self._seen)} "
            f"loaded={len(results)} in {time.perf_counter() - t0:.1f}s "
            f"{self._scan_stats(results)}",
            flush=True,
        )
        self._primed = True

    def _tick(self, conn, api: TqApi) -> int:
        symbols = self._symbols(conn, api)
        if not self._primed:
            self._prime(conn, symbols)
            return 0
        if self.broker is not None:
            try:
                self.broker.retry_pending()
                self.broker.check_loss_caps()
            except Exception:
                traceback.print_exc()

        marks = load_watermarks(conn, HS_MONITOR_DURATIONS, symbols)
        now = datetime.now(TZ).replace(tzinfo=None)
        pending = self._pending_keys(marks)
        prov_pending = self._provisional_keys(marks, now)
        pending.extend(prov_pending)
        if not pending:
            return 0
        t0 = time.perf_counter()
        results = self._detect(conn, pending)
        scan_sec = time.perf_counter() - t0
        # 分片回来是按片号排的，告警与开仓统一按 K 线时刻推
        results.sort(key=lambda r: (r.last_dt, r.symbol, r.duration_sec))
        alerts = 0
        for res in results:
            if res.prov:
                alerts += self._deliver_provisional(res)
                continue
            # 模拟盘：用最新一根 OHLC 推进止盈止损（那根已随检测结果带回来，不再查库）
            if self.broker is not None:
                try:
                    self.broker.on_bar(
                        res.symbol,
                        res.duration_sec,
                        res.last_dt,
                        res.bar,
                    )
                except Exception:
                    traceback.print_exc()
            if res.det is None and res.det_strict is None:
                continue
            # 严格档先出：同形态时它要顶掉常规档那条，模拟盘也按它的 50% 手数口径
            for det in (res.det_strict, res.det):
                if det is None:
                    continue
                n = self.engine.deliver(
                    det,
                    alert_log=self.alert_log,
                    push=True,
                    toast=False,
                )
                if n:
                    label = DURATION_LABELS.get(res.duration_sec, str(res.duration_sec))
                    tag = "严格告警" if det.strict else "告警"
                    print(
                        f"[monitor] {tag} {res.symbol} {label} @ {res.last_dt:%Y-%m-%d %H:%M}",
                        flush=True,
                    )
                alerts += n
        # 每轮都打：分片后单轮普遍 <1s，按秒数设阈值会把要看的数正好滤掉
        print(
            f"[monitor] 本轮扫描 pending={len(pending)} prov={len(prov_pending)} "
            f"{self._scan_stats(results)} 用时 {scan_sec:.2f}s",
            flush=True,
        )
        return alerts

    def _deliver_provisional(self, res: KeyResult) -> int:
        """抢跑告警：只落盘推送，不喂模拟盘 —— 那根 K 还没收线，价格随时会走样。"""
        if res.det is None:
            return 0
        n = self.prov_engine.deliver(
            res.det,
            alert_log=self.alert_log,
            push=True,
            toast=False,
            provisional=True,
        )
        if n:
            label = DURATION_LABELS.get(res.duration_sec, str(res.duration_sec))
            print(
                f"[monitor] 抢跑告警 {res.symbol} {label} @ {res.last_dt:%Y-%m-%d %H:%M}",
                flush=True,
            )
        return n
