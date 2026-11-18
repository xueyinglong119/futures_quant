"""全品种多周期头肩形态检测与告警"""

from __future__ import annotations

import os
import subprocess
import threading
from dataclasses import dataclass
from datetime import datetime, time
from pathlib import Path
from typing import Any, Callable

import pandas as pd

from core.alert_store import add_alert
from core.feishu_notify import send_alert_feishu
from core.kline_store import (
    DURATION_1D,
    DURATION_1H,
    DURATION_1M,
    DURATION_2H,
    DURATION_5M,
    DURATION_30M,
    DURATION_LABELS,
    connect,
    ensure_table,
    load_bars,
)
from core.push_notify import send_alert_push
from strategies.head_shoulders import HeadShouldersStrategy
from strategies.triple_pattern import TriplePatternStrategy
from utils.symbols import get_display_name

# 监控周期：1分 / 5分 / 30分 / 1小时 / 2小时 / 日线
HS_MONITOR_DURATIONS: tuple[int, ...] = (
    DURATION_1M,
    DURATION_5M,
    DURATION_30M,
    DURATION_1H,
    DURATION_2H,
    DURATION_1D,
)

# 收盘前抢跑：日盘 14:45、夜盘 22:30 之后，最后几根会话 K 还是半截的。
# 这段时间用未收线的半截 K 提前提醒右肩，免得等到 15:00/23:00 收线才告警已经买不到。
PROVISIONAL_DURATIONS: tuple[int, ...] = (DURATION_30M, DURATION_1H, DURATION_2H)
PROVISIONAL_WINDOWS: tuple[tuple[time, time], ...] = (
    (time(14, 45), time(15, 0)),
    (time(22, 30), time(23, 0)),
)

# 严格档：与常规档同刻触发，多两肩价差 Dev + 形态根数 + 三段均量递减三道闸，优先级最高
# （同形态时只出严格档那条）。HS_STRICT_SIGNALS=0 可整体关掉。
STRICT_SIGNALS = os.environ.get("HS_STRICT_SIGNALS", "1") != "0"

# 三重顶/底：独立第三通道，与头肩两条各自记账、同刻同根都发（HS_TRIPLE_SIGNALS=0 可整体关掉）。
TRIPLE_SIGNALS = os.environ.get("HS_TRIPLE_SIGNALS", "1") != "0"


def in_provisional_window(now: datetime) -> bool:
    hm = now.hour * 60 + now.minute
    return any(
        start.hour * 60 + start.minute <= hm < end.hour * 60 + end.minute
        for start, end in PROVISIONAL_WINDOWS
    )


@dataclass(frozen=True)
class HsTfConfig:
    duration_sec: int
    pivot_order: int
    min_span_bars: int
    max_span_bars: int
    lookback_bars: int


# 各周期形态左肩→右肩最长均为 110 根
TF_CONFIGS: dict[int, HsTfConfig] = {
    DURATION_1M: HsTfConfig(DURATION_1M, 8, 25, 110, 500),
    DURATION_5M: HsTfConfig(DURATION_5M, 6, 20, 110, 400),
    DURATION_30M: HsTfConfig(DURATION_30M, 4, 10, 110, 300),
    DURATION_1H: HsTfConfig(DURATION_1H, 3, 8, 110, 250),
    DURATION_2H: HsTfConfig(DURATION_2H, 3, 6, 110, 200),
    DURATION_1D: HsTfConfig(DURATION_1D, 2, 8, 110, 220),
}


def make_strategy(duration_sec: int) -> HeadShouldersStrategy:
    cfg = TF_CONFIGS.get(duration_sec, TF_CONFIGS[DURATION_30M])
    return HeadShouldersStrategy(
        pivot_order=cfg.pivot_order,
        shoulder_tolerance=0.03,
        min_span_bars=cfg.min_span_bars,
        max_span_bars=cfg.max_span_bars,
        max_right_above_left_ratio=0.015,
        min_head_neck_ratio=0.008,
        max_volume=1,
        duration_sec=duration_sec,
        strict_signals=STRICT_SIGNALS,
    )


def make_triple_strategy(duration_sec: int) -> TriplePatternStrategy:
    """三重顶/底：拐点、跨度、形态高度三件套都取头肩同一张表，避免两套判据。

    规格里的 `MIN_FORM_CANDLE=10` 是全局值，但短周期 10 根只是十几分钟，
    所以按下限取 `max(10, 该周期 min_span_bars)`。
    量能闸按规格先剔脉冲巨量（`PULSE_MULTIPLE=3`）再求段均量：一根天量会把整段
    均值顶上去，把「第三段确实在缩量」误判成放量。头肩那套脉冲闸仍只归严格档，不动。
    """
    cfg = TF_CONFIGS.get(duration_sec, TF_CONFIGS[DURATION_30M])
    return TriplePatternStrategy(
        pivot_order=cfg.pivot_order,
        min_span_bars=cfg.min_span_bars,
        max_span_bars=cfg.max_span_bars,
        min_head_neck_ratio=0.008,
        max_volume=1,
        duration_sec=duration_sec,
        dev_threshold=0.01,
        min_form_bars=max(10, cfg.min_span_bars),
        time_dev_threshold=0.5,
        time_filter=False,
        strict_pulse_filter=True,
        strict_pulse_multiple=3.0,
    )


def bars_to_strategy_df(records: list[dict[str, Any]]) -> pd.DataFrame:
    if not records:
        return pd.DataFrame(
            columns=["datetime", "open", "high", "low", "close", "volume", "open_oi"]
        )
    rows = []
    for r in records:
        rows.append(
            {
                "datetime": r["datetime_end"],
                "open": float(r["open"]),
                "high": float(r["high"]),
                "low": float(r["low"]),
                "close": float(r["close"]),
                "volume": int(r.get("volume") or 0),
                "open_oi": int(r.get("open_oi") or 0),
            }
        )
    return pd.DataFrame(rows).sort_values("datetime").reset_index(drop=True)


def load_tf_df(
    symbol: str,
    duration_sec: int,
    lookback: int | None = None,
    conn=None,
) -> pd.DataFrame:
    cfg = TF_CONFIGS.get(duration_sec, TF_CONFIGS[DURATION_30M])
    n = lookback or cfg.lookback_bars
    own_conn = conn is None
    if own_conn:
        conn = connect()
        ensure_table(conn)
    try:
        records = load_bars(conn, symbol, duration_sec, limit=n)
    finally:
        if own_conn:
            conn.close()
    return bars_to_strategy_df(records)


def windows_toast(title: str, message: str) -> None:
    safe_title = title.replace("'", "''")
    safe_msg = message.replace("'", "''")[:180]
    ps = (
        "Add-Type -AssemblyName System.Windows.Forms; "
        "Add-Type -AssemblyName System.Drawing; "
        "$ni = New-Object System.Windows.Forms.NotifyIcon; "
        "$ni.Icon = [System.Drawing.SystemIcons]::Warning; "
        f"$ni.BalloonTipTitle = '{safe_title}'; "
        f"$ni.BalloonTipText = '{safe_msg}'; "
        "$ni.Visible = $true; "
        "$ni.ShowBalloonTip(8000); "
        "Start-Sleep -Seconds 9; "
        "$ni.Dispose()"
    )
    try:
        subprocess.Popen(
            ["powershell", "-NoProfile", "-WindowStyle", "Hidden", "-Command", ps],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError:
        pass


def emit_alert(
    *,
    symbol: str,
    duration_sec: int,
    kind: str,
    when: datetime,
    reason: str,
    detail: dict[str, Any] | None = None,
    alert_log: Path | None = None,
    push: bool = True,
    toast: bool = True,
    provisional: bool = False,
    strict: bool = False,
) -> str | None:
    """写告警并推送，返回告警 id，供模拟账户把这笔单和告警对上。"""
    label = DURATION_LABELS.get(duration_sec, str(duration_sec))
    name = get_display_name(symbol)
    if provisional:
        kind = f"{kind}·抢跑"
    if strict:
        kind = f"{kind}·严格"
    title = f"{name} {label} {kind}"
    body = f"K线 {when.strftime('%m-%d %H:%M')} | {reason}"
    print(f"!!! HS_ALERT !!! {title} | {symbol} | {body}", flush=True)

    if alert_log is not None:
        alert_log.parent.mkdir(parents=True, exist_ok=True)
        with alert_log.open("a", encoding="utf-8") as f:
            f.write(
                f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} | {name} | {symbol} | "
                f"{label} | {kind} | {when.isoformat(timespec='seconds')} | {reason}\n"
            )

    if toast:
        windows_toast(title, body)

    extra: dict[str, Any] = {
        "duration": label,
        "duration_sec": duration_sec,
        "name": name,
        "display": f"{name}({symbol})",
    }
    if provisional:
        # App 的「抢跑」标签页只取带这个标记的告警
        extra["provisional"] = True
    if strict:
        extra["strict"] = True
    pattern_id = str((detail or {}).get("pattern_id") or "")
    # 顶掉用的键必须带上品种和周期：pattern_id 只由三根 K 线时刻组成，不同品种会同名
    pattern_key = f"{symbol}|{duration_sec}|{pattern_id}" if pattern_id else None
    if pattern_key:
        # 两档对同一形态的记账键，严格档用它找到自己的常规档那条再顶掉
        extra["pattern"] = pattern_key
    if detail:
        extra["detail"] = detail

    alert_id: str | None = None
    try:
        alert = add_alert(
            symbol=symbol,
            kind=kind,
            reason=f"[{name} · {label}] {reason}",
            bar_time=when.isoformat(timespec="seconds"),
            extra=extra,
            supersedes=pattern_key if strict else None,
        )
        alert_id = alert.id
        if push:
            # 推送走后台线程，不堵住行情循环里的「发现时间」
            def _push() -> None:
                try:
                    push_result = send_alert_push(alert)
                    print(f"App推送结果: {push_result}", flush=True)
                except Exception as exc:  # noqa: BLE001
                    print(f"App推送失败: {exc}", flush=True)
                if strict:
                    # App 被系统杀掉就收不到轮询，严格档另走飞书（亮屏交给飞书自己的保活）
                    try:
                        fs_result = send_alert_feishu(alert)
                        print(f"飞书推送结果: {fs_result}", flush=True)
                    except Exception as exc:  # noqa: BLE001
                        print(f"飞书推送失败: {exc}", flush=True)

            threading.Thread(target=_push, name="hs-push", daemon=True).start()
    except Exception as exc:  # noqa: BLE001
        print(f"写入/推送 App 告警失败: {exc}", flush=True)
    return alert_id


@dataclass
class Detection:
    """一次「本根确认了新形态」的结果，不含任何副作用。"""

    symbol: str
    duration_sec: int
    last_dt: datetime
    kind: str
    signal: Any
    df: pd.DataFrame
    strict: bool = False


def _pattern_of(det: Detection) -> str:
    return str((getattr(det.signal, "detail", None) or {}).get("pattern_id") or "")


def tier_dets(strict: Detection | None, normal: Detection | None) -> list[Detection]:
    """本轮该发的检测，严格档排在前面。

    两档同刻触发：同一个形态（左/头/右三根 K 线时刻全同）时常规档让位，不重复占一条，
    模拟盘那笔也按严格档的半仓口径下。
    """
    if strict is not None and normal is not None:
        pat = _pattern_of(strict)
        if pat and pat == _pattern_of(normal):
            return [strict]
    return [d for d in (strict, normal) if d is not None]


def alert_tag(det: Detection) -> str:
    """日志前缀：让严格档、三重通道和普通头肩在监控输出里一眼分开。"""
    if det.strict:
        return "严格告警"
    if det.kind.startswith("三重"):
        return "三重告警"
    return "告警"


class HsMonitorEngine:
    """持久化策略实例，按 symbol+周期在新 K 线出现时检测一次。"""

    def __init__(self) -> None:
        self._strategies: dict[str, HeadShouldersStrategy] = {}
        self._triple_strategies: dict[str, TriplePatternStrategy] = {}
        self._last_bar_dt: dict[str, datetime] = {}
        self._last_triple_dt: dict[str, datetime] = {}
        self._alerted: set[str] = set()
        self.on_signal: Callable[[str, int, datetime, pd.DataFrame, Any], None] | None = None

    def _key(self, symbol: str, duration_sec: int) -> str:
        return f"{symbol}|{duration_sec}"

    def _strategy(self, symbol: str, duration_sec: int) -> HeadShouldersStrategy:
        key = self._key(symbol, duration_sec)
        if key not in self._strategies:
            self._strategies[key] = make_strategy(duration_sec)
        return self._strategies[key]

    def _triple_strategy(self, symbol: str, duration_sec: int) -> TriplePatternStrategy:
        key = self._key(symbol, duration_sec)
        if key not in self._triple_strategies:
            self._triple_strategies[key] = make_triple_strategy(duration_sec)
        return self._triple_strategies[key]

    def check_symbol_tf(
        self,
        symbol: str,
        duration_sec: int,
        *,
        alert_log: Path | None = None,
        push: bool = True,
        toast: bool = False,
        force: bool = False,
        emit: bool = True,
        conn=None,
    ) -> int:
        """
        检测单个品种单个周期。
        仅在出现新 K 线（或 force=True）时检测；返回本轮新告警条数。
        emit=False 时只预热状态（启动回扫历史用），不发告警。
        """
        df = load_tf_df(symbol, duration_sec, conn=conn)
        return self.evaluate_df(
            symbol,
            duration_sec,
            df,
            alert_log=alert_log,
            push=push,
            toast=toast,
            force=force,
            emit=emit,
        )

    def detect(
        self,
        symbol: str,
        duration_sec: int,
        df: pd.DataFrame,
        *,
        force: bool = False,
    ) -> Detection | None:
        """只算形态：最后一根没变就跳过，变了才跑一次 `on_bar`。

        不发告警、不推送、不开仓，也不碰 `_alerted`——这些副作用统一留给
        `deliver`，好让检测能搬到子进程里跑。
        """
        if df is None or df.empty:
            return None

        key = self._key(symbol, duration_sec)
        last_dt = self._last_dt(df)
        prev = self._last_bar_dt.get(key)
        if not force and prev is not None and prev == last_dt:
            return None

        strategy = self._strategy(symbol, duration_sec)
        signal = strategy.on_bar(df, max_volume=1, symbol=key)
        self._last_bar_dt[key] = last_dt
        if signal is None:
            return None

        kind = "头肩顶" if signal.action == "sell" else "头肩底"
        return Detection(
            symbol=symbol,
            duration_sec=duration_sec,
            last_dt=last_dt,
            kind=kind,
            signal=signal,
            df=df,
        )

    def detect_strict(
        self,
        symbol: str,
        duration_sec: int,
        df: pd.DataFrame,
    ) -> Detection | None:
        """严格档单独扫一遍，与常规档同一根触发，只是多几道闸。

        它不查 `_last_bar_dt`：同一根重复扫会被 `_traded_strict` 和 `_alerted`
        各自挡掉，不会出两条。
        """
        if df is None or df.empty:
            return None
        key = self._key(symbol, duration_sec)
        strategy = self._strategy(symbol, duration_sec)
        signal = strategy.on_bar_strict(df, max_volume=1, symbol=key)
        if signal is None:
            return None
        kind = "头肩顶" if signal.action == "sell" else "头肩底"
        return Detection(
            symbol=symbol,
            duration_sec=duration_sec,
            last_dt=self._last_dt(df),
            kind=kind,
            signal=signal,
            df=df,
            strict=True,
        )

    def detect_triple(
        self,
        symbol: str,
        duration_sec: int,
        df: pd.DataFrame,
        *,
        force: bool = False,
    ) -> Detection | None:
        """三重顶/底：第三个极值当根触发，与头肩共用同一套点位与拐点口径。

        常规档用 `_last_bar_dt` 记过这根之后还要能被这里记一次，
        所以另开一张 `_last_triple_dt`，否则三重永远抢不到新 K 线。
        """
        if df is None or df.empty or not TRIPLE_SIGNALS:
            return None

        key = self._key(symbol, duration_sec)
        last_dt = self._last_dt(df)
        prev = self._last_triple_dt.get(key)
        if not force and prev is not None and prev == last_dt:
            return None

        strategy = self._triple_strategy(symbol, duration_sec)
        signal = strategy.on_bar(df, max_volume=1, symbol=key)
        self._last_triple_dt[key] = last_dt
        if signal is None:
            return None

        detail = getattr(signal, "detail", None) or {}
        kind = str(detail.get("pattern_kind") or ("三重底" if signal.action == "buy" else "三重顶"))
        return Detection(
            symbol=symbol,
            duration_sec=duration_sec,
            last_dt=last_dt,
            kind=kind,
            signal=signal,
            df=df,
        )

    @staticmethod
    def _last_dt(df: pd.DataFrame) -> datetime:
        last_dt = pd.Timestamp(df.iloc[-1]["datetime"]).to_pydatetime()
        if last_dt.tzinfo is not None:
            last_dt = last_dt.replace(tzinfo=None)
        return last_dt.replace(microsecond=0)

    def deliver(
        self,
        det: Detection,
        *,
        alert_log: Path | None = None,
        push: bool = True,
        toast: bool = False,
        provisional: bool = False,
    ) -> int:
        """发告警 + 通知模拟账户，同一 (品种周期, 类型, K线时刻, reason) 只发一次。

        严格档那条还会把同 (品种, 周期, 形态) 的**已在库**常规档记录（含抢跑那一条）
        打上 superseded；同刻一起出的常规档在 `tier_dets` 里直接不发。
        """
        key = self._key(det.symbol, det.duration_sec)
        alert_key = f"{key}|{det.kind}|{det.last_dt.isoformat()}|{det.signal.reason}"
        if alert_key in self._alerted:
            return 0
        self._alerted.add(alert_key)
        alert_id = emit_alert(
            symbol=det.symbol,
            duration_sec=det.duration_sec,
            kind=det.kind,
            when=det.last_dt,
            reason=det.signal.reason,
            detail=getattr(det.signal, "detail", None),
            alert_log=alert_log,
            push=push,
            toast=toast,
            provisional=provisional,
            strict=det.strict,
        )
        if self.on_signal is not None:
            try:
                self.on_signal(
                    det.symbol,
                    det.duration_sec,
                    det.last_dt,
                    det.df,
                    det.signal,
                    alert_id,
                )
            except Exception as exc:  # noqa: BLE001
                print(f"[paper] 开仓处理失败 {det.symbol}: {exc}", flush=True)
        return 1

    def evaluate_df(
        self,
        symbol: str,
        duration_sec: int,
        df: pd.DataFrame,
        *,
        alert_log: Path | None = None,
        push: bool = True,
        toast: bool = False,
        force: bool = False,
        emit: bool = True,
    ) -> int:
        """串行路径：用给定的已收盘 K 线做一次检测并按需告警，严格档先于常规档。

        三重是第三条独立通道，排在最后：同一根上头肩与三重都成立时两条都发，
        但模拟盘 (品种,周期) 只有一个坑，先到的头肩拿到开仓权。
        """
        strict = self.detect_strict(symbol, duration_sec, df)
        det = self.detect(symbol, duration_sec, df, force=force)
        triple = self.detect_triple(symbol, duration_sec, df, force=force)
        if not emit:
            return 0
        n = 0
        for item in tier_dets(strict, det) + ([triple] if triple is not None else []):
            n += self.deliver(item, alert_log=alert_log, push=push, toast=toast)
        return n
