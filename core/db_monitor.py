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
from core.cont_symbols import drop_illiquid, list_commodity_mains
from core.hs_fanout import HsFanout, KeyResult, detect_one
from core.hs_monitor import (
    HS_MONITOR_DURATIONS,
    LARGER_MAP,
    PROVISIONAL_DURATIONS,
    HsMonitorEngine,
    alert_tag,
    in_provisional_window,
    load_tf_df,
    tier_dets,
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
from core.tf_conflict import PROBE_BARS, TF_CONFLICT, ConflictJudge

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
        # 大小周期冲突探针：只在父进程挂（分片 worker 不做副作用），两个引擎共用一份
        # 大周期形态缓存。判到 block 会拦住严格档/超严格的模拟盘开仓。
        self.conn = None
        self.conflict_judge: ConflictJudge | None = None
        if TF_CONFLICT:
            self.conflict_judge = ConflictJudge(LARGER_MAP, self._load_tf_df)
            self.engine.conflict_judge = self.conflict_judge
            self.prov_engine.conflict_judge = self.conflict_judge
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
        self.conn = conn
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
            if self.conflict_judge is None:
                print("[monitor] 周期冲突判定未启用（HS_TF_CONFLICT=1 才开）", flush=True)
            else:
                pairs = sum(len(v) for v in LARGER_MAP.values())
                print(
                    f"[monitor] 周期冲突判定已启用 大周期配对={pairs} "
                    f"探针={PROBE_BARS}根 拦截=block不开仓",
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
                        self.conn = conn
                        if self.broker is not None:
                            self.broker.conn = conn
                time.sleep(self.poll_sec)
        finally:
            if self.fanout is not None:
                self.fanout.close()
            self.conn = None
            conn.close()
            api.close()

    def _load_tf_df(self, symbol: str, duration_sec: int):
        """冲突探针取大周期历史：有在用的连接就复用，别为一次判定单开一条。"""
        if self.conn is None:
            return load_tf_df(symbol, duration_sec)
        return load_tf_df(symbol, duration_sec, conn=self.conn)

    def _symbols(self, conn, api: TqApi) -> list[str]:
        try:
            # 读库优先：ingest 已刷新 dominant_contracts；避免 monitor 再刷一遍 OI/冷启动
            return list_commodity_mains(
                api, conn, refresh=False, bootstrap_history=False
            )
        except Exception:
            traceback.print_exc()
            try:
                from core.dominant import list_stored_dominants

                stored = list_stored_dominants(conn)
                if stored:
                    return stored
            except Exception:
                traceback.print_exc()
            # 最后兜底：库里非 KQ.m@ 的月约（剔除低流动性）
            raw = list_symbols(conn, duration_sec=60) or list_symbols(conn)
            month = [s for s in raw if not s.startswith("KQ.m@")]
            return drop_illiquid(month or raw)

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
            dets = tier_dets(res.det_strict, res.det)
            # 三重是第三条独立通道，排在最后：同根上头肩与三重都成立时两条都发，
            # 但模拟盘每个 (品种, 周期) 只有一个坑，先到的头肩拿到开仓权。
            if res.det_triple is not None:
                dets.append(res.det_triple)
            # 只有严格档开仓；常规档与三重只告警（11-22 用户裁决）
            for det in dets:
                n = self.engine.deliver(
                    det,
                    alert_log=self.alert_log,
                    push=True,
                    toast=False,
                    trade=bool(det.strict),
                )
                if n:
                    label = DURATION_LABELS.get(res.duration_sec, str(res.duration_sec))
                    tag = alert_tag(det)
                    print(
                        f"[monitor] {tag} {res.symbol} {label} @ {res.last_dt:%Y-%m-%d %H:%M}",
                        flush=True,
                    )
                alerts += n
            # 超严格也开仓（11-22 用户裁决：只有严格/超严格开仓）
            if res.det_super is not None:
                n = self.engine.deliver(
                    res.det_super,
                    alert_log=self.alert_log,
                    push=True,
                    toast=False,
                )
                if n:
                    label = DURATION_LABELS.get(res.duration_sec, str(res.duration_sec))
                    print(
                        f"[monitor] {alert_tag(res.det_super)} {res.symbol} {label} "
                        f"@ {res.last_dt:%Y-%m-%d %H:%M}",
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
        """抢跑告警：只落盘推送，不喂模拟盘 —— 那根 K 还没收线，价格随时会走样。

        严格档与三重照正式路径的顺序发（11-20 用户裁决：这两个通道也要提前提醒）：
        同形态时 `tier_dets` 只留严格档，三重独立追加；三条都只用抢跑引擎，不与
        正式路径共用 `_traded_patterns`。
        """
        dets = tier_dets(res.det_strict, res.det)
        if res.det_triple is not None:
            dets.append(res.det_triple)
        n = 0
        for det in dets:
            got = self.prov_engine.deliver(
                det,
                alert_log=self.alert_log,
                push=True,
                toast=False,
                provisional=True,
            )
            if got:
                label = DURATION_LABELS.get(res.duration_sec, str(res.duration_sec))
                print(
                    f"[monitor] 抢跑告警 {alert_tag(det)} {res.symbol} {label} "
                    f"@ {res.last_dt:%Y-%m-%d %H:%M}",
                    flush=True,
                )
            n += got
        return n
