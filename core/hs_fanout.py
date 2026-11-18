"""监控检测分片：子进程只算形态，父进程独占告警落盘、推送与模拟盘。

分片键用品种而不是 (品种, 周期)：同一品种的各周期落同一个 worker，
策略实例与 `_last_bar_dt` / `_traded_patterns` 就不会跨进程漂。
`hash()` 每进程加盐，所以这里用 crc32 保证父进程算出的片和子进程理解的一致。
"""

from __future__ import annotations

import multiprocessing as context_mp
import os
import queue as queue_mod
import time
import traceback
import zlib
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import pandas as pd

from core.hs_monitor import Detection, HsMonitorEngine, load_tf_df
from core.kline_store import connect, ensure_table

SHARD_COUNT = int(os.environ.get("HS_WORKERS", "3"))
BATCH_TIMEOUT_SEC = float(os.environ.get("HS_WORKER_TIMEOUT", "30"))
MAX_STRIKE = 3


@dataclass
class KeyResult:
    """一个 (品种, 周期) 键本轮的检测产物，全部是可 pickle 的纯数据。"""

    symbol: str
    duration_sec: int
    last_dt: datetime
    bar: dict[str, float]
    det: Detection | None
    ms: float
    prov: bool = False
    det_strict: Detection | None = None
    det_triple: Detection | None = None


def shard_of(symbol: str, count: int) -> int:
    return zlib.crc32(symbol.encode("utf-8")) % count


def last_bar_of(df: pd.DataFrame) -> tuple[datetime, dict[str, float]]:
    """取最后一根的 K 线时刻与 OHLC，父进程靠它推止盈止损，省一次 `load_bars`。"""
    row = df.iloc[-1]
    last_dt = pd.Timestamp(row["datetime"]).to_pydatetime()
    if last_dt.tzinfo is not None:
        last_dt = last_dt.replace(tzinfo=None)
    bar = {
        "datetime_end": last_dt,
        "open": float(row["open"]),
        "high": float(row["high"]),
        "low": float(row["low"]),
        "close": float(row["close"]),
    }
    return last_dt.replace(microsecond=0), bar


def detect_one(
    engine: HsMonitorEngine,
    conn: Any,
    symbol: str,
    duration_sec: int,
    *,
    force: bool = False,
    prov: bool = False,
) -> KeyResult | None:
    """取数 + 算形态，不发告警。数据为空返回 None。

    严格档与常规档同刻出（右肩当根），同形态时由 `tier_dets` 只留优先级高的严格档那条；
    抢跑那半截 K 只做提醒，不进严格档。三重顶底同理，也是收线才发，抢跑那半截不算。
    """
    t0 = time.perf_counter()
    df = load_tf_df(symbol, duration_sec, conn=conn)
    if df is None or df.empty:
        return None
    last_dt, bar = last_bar_of(df)
    det_strict = None if prov else engine.detect_strict(symbol, duration_sec, df)
    det = engine.detect(symbol, duration_sec, df, force=force)
    det_triple = None if prov else engine.detect_triple(symbol, duration_sec, df)
    return KeyResult(
        symbol=symbol,
        duration_sec=duration_sec,
        last_dt=last_dt,
        bar=bar,
        det=det,
        ms=(time.perf_counter() - t0) * 1000.0,
        prov=prov,
        det_strict=det_strict,
        det_triple=det_triple,
    )


def _worker_loop(shard: int, task_q, out_q) -> None:
    """子进程主循环：一批键进来，一批结果出去。

    抢跑键走独立引擎：半截 K 的结构不能进正式路径的 `_traded_patterns`，
    否则抢跑提醒会把真正收线那根的告警一起吃掉。
    """
    engine = HsMonitorEngine()
    prov_engine = HsMonitorEngine()
    conn = None
    while True:
        item = task_q.get()
        if item is None:
            break
        seq, keys = item
        results: list[KeyResult] = []
        strikes = 0
        for symbol, duration_sec, prov in keys:
            try:
                if conn is None:
                    conn = connect()
                    ensure_table(conn)
                res = detect_one(
                    prov_engine if prov else engine,
                    conn,
                    symbol,
                    duration_sec,
                    force=prov,
                    prov=prov,
                )
                if res is not None:
                    results.append(res)
                strikes = 0
            except Exception:
                traceback.print_exc()
                strikes += 1
                try:
                    if conn is not None:
                        conn.close()
                except Exception:
                    pass
                conn = None  # 下一个键重连
                if strikes >= MAX_STRIKE:
                    # 连不上库就别把整批键白烧一遍，报个数不对让父进程退回串行
                    break
        try:
            out_q.put((seq, len(keys), results))
        except Exception:
            traceback.print_exc()
            break


class HsFanout:
    """父进程侧的分片调度。任何异常都只有一条出路：`broken` 置位，调用方串行回退。"""

    def __init__(self, count: int = SHARD_COUNT) -> None:
        self.ctx = context_mp.get_context("spawn")
        self.count = int(count)
        self.broken = self.count < 2
        self._procs: list[Any] = []
        self._task_qs: list[Any] = []
        self._out_qs: list[Any] = []
        self._seq = 0
        if not self.broken:
            try:
                self._start()
            except Exception:
                traceback.print_exc()
                self.broken = True
                print("[monitor] 检测分片启动失败，本进程单串行", flush=True)

    @property
    def active_workers(self) -> int:
        return 1 if self.broken else self.count

    def _start(self) -> None:
        for shard in range(self.count):
            task_q = self.ctx.Queue()
            out_q = self.ctx.Queue()
            proc = self.ctx.Process(
                target=_worker_loop,
                args=(shard, task_q, out_q),
                name=f"hs-detect-{shard}",
                daemon=True,
            )
            proc.start()
            self._procs.append(proc)
            self._task_qs.append(task_q)
            self._out_qs.append(out_q)
        print(f"[monitor] 检测分片已启动 workers={self.count}", flush=True)

    def _broke(self, why: str) -> None:
        self.broken = True
        print(
            f"[monitor] 检测分片不可用（{why}），本轮起退回单进程串行",
            flush=True,
        )

    def detect(
        self, keys: list[tuple[str, int, bool]]
    ) -> list[KeyResult] | None:
        """把 keys 按品种分到各片，收齐结果。返回 None 表示需要调用方串行兜底。

        键是 (品种, 周期, 是否抢跑)：一次往返里把常规键和抢跑键一起发出去，
        否则父进程要串行等两轮。
        """
        if not keys:
            return []
        if self.broken:
            return None
        if not all(p.is_alive() for p in self._procs):
            self._broke("worker 已退出")
            return None

        buckets: list[list[tuple[str, int, bool]]] = [[] for _ in range(self.count)]
        for symbol, duration_sec, prov in keys:
            buckets[shard_of(symbol, self.count)].append((symbol, duration_sec, prov))

        self._seq += 1
        seq = self._seq
        sent = 0
        for shard, bucket in enumerate(buckets):
            if not bucket:
                continue
            self._task_qs[shard].put((seq, bucket))
            sent += 1

        deadline = time.monotonic() + BATCH_TIMEOUT_SEC
        results: list[KeyResult] = []
        for shard in range(self.count):
            if not buckets[shard]:
                continue
            remain = deadline - time.monotonic()
            try:
                got_seq, got_n, got = self._out_qs[shard].get(
                    timeout=max(remain, 0.001)
                )
            except queue_mod.Empty:
                self._broke(f"片 {shard} 超过 {BATCH_TIMEOUT_SEC:.0f}s 未回")
                return None
            if got_seq != seq:
                self._broke(f"片 {shard} 结果序号 {got_seq} != {seq}")
                return None
            if got_n != len(buckets[shard]):
                self._broke(f"片 {shard} 结果数 {got_n} != {len(buckets[shard])}")
                return None
            results.extend(got)
        return results

    def stats(self, results: list[KeyResult]) -> str:
        if not results:
            return "keys=0"
        slowest = max(results, key=lambda r: r.ms)
        return (
            f"keys={len(results)} sum={sum(r.ms for r in results) / 1000.0:.2f}s "
            f"max={slowest.symbol}/{slowest.duration_sec} {slowest.ms:.0f}ms"
        )

    def close(self) -> None:
        if self.broken:
            return
        for task_q in self._task_qs:
            try:
                task_q.put(None)
            except Exception:
                pass
        for proc in self._procs:
            proc.join(timeout=5)
            if proc.is_alive():
                proc.terminate()
        for q in self._task_qs + self._out_qs:
            try:
                q.cancel_join_thread()
                q.close()
            except Exception:
                pass
