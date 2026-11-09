"""全品种多周期头肩形态检测与告警"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

import pandas as pd

from core.alert_store import add_alert
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


@dataclass(frozen=True)
class HsTfConfig:
    duration_sec: int
    pivot_order: int
    min_span_bars: int
    max_span_bars: int
    lookback_bars: int


# 不同周期用不同跨度，避免 1 分钟和日线共用同一 bar 数
TF_CONFIGS: dict[int, HsTfConfig] = {
    DURATION_1M: HsTfConfig(DURATION_1M, 8, 25, 240, 500),
    DURATION_5M: HsTfConfig(DURATION_5M, 6, 20, 120, 400),
    DURATION_30M: HsTfConfig(DURATION_30M, 4, 10, 120, 300),
    DURATION_1H: HsTfConfig(DURATION_1H, 3, 8, 80, 250),
    DURATION_2H: HsTfConfig(DURATION_2H, 3, 6, 50, 200),
    DURATION_1D: HsTfConfig(DURATION_1D, 2, 8, 100, 220),
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
    alert_log: Path | None = None,
    push: bool = True,
    toast: bool = True,
) -> None:
    label = DURATION_LABELS.get(duration_sec, str(duration_sec))
    name = get_display_name(symbol)
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

    try:
        alert = add_alert(
            symbol=symbol,
            kind=kind,
            reason=f"[{name} · {label}] {reason}",
            bar_time=when.isoformat(timespec="seconds"),
            extra={
                "duration": label,
                "duration_sec": duration_sec,
                "name": name,
                "display": f"{name}({symbol})",
            },
        )
        if push:
            push_result = send_alert_push(alert)
            print(f"App推送结果: {push_result}", flush=True)
    except Exception as exc:  # noqa: BLE001
        print(f"写入/推送 App 告警失败: {exc}", flush=True)


class HsMonitorEngine:
    """持久化策略实例，按 symbol+周期在新 K 线出现时检测一次。"""

    def __init__(self) -> None:
        self._strategies: dict[str, HeadShouldersStrategy] = {}
        self._last_bar_dt: dict[str, datetime] = {}
        self._alerted: set[str] = set()
        self.on_signal: Callable[[str, int, datetime, pd.DataFrame, Any], None] | None = None

    def _key(self, symbol: str, duration_sec: int) -> str:
        return f"{symbol}|{duration_sec}"

    def _strategy(self, symbol: str, duration_sec: int) -> HeadShouldersStrategy:
        key = self._key(symbol, duration_sec)
        if key not in self._strategies:
            self._strategies[key] = make_strategy(duration_sec)
        return self._strategies[key]

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
        """用给定的已收盘 K 线做一次检测。最后一根没变则跳过。"""
        if df is None or df.empty:
            return 0

        key = self._key(symbol, duration_sec)
        last_dt = pd.Timestamp(df.iloc[-1]["datetime"]).to_pydatetime()
        if last_dt.tzinfo is not None:
            last_dt = last_dt.replace(tzinfo=None)
        last_dt = last_dt.replace(microsecond=0)
        prev = self._last_bar_dt.get(key)
        if not force and prev is not None and prev == last_dt:
            return 0

        strategy = self._strategy(symbol, duration_sec)
        signal = strategy.on_bar(df, max_volume=1, symbol=key)
        self._last_bar_dt[key] = last_dt
        if signal is None or not emit:
            return 0

        kind = "头肩顶" if signal.action == "sell" else "头肩底"
        alert_key = f"{key}|{kind}|{last_dt.isoformat()}|{signal.reason}"
        if alert_key in self._alerted:
            return 0
        self._alerted.add(alert_key)
        emit_alert(
            symbol=symbol,
            duration_sec=duration_sec,
            kind=kind,
            when=last_dt,
            reason=signal.reason,
            alert_log=alert_log,
            push=push,
            toast=toast,
        )
        if self.on_signal is not None:
            try:
                self.on_signal(symbol, duration_sec, last_dt, df, signal)
            except Exception as exc:  # noqa: BLE001
                print(f"[paper] 开仓处理失败 {symbol}: {exc}", flush=True)
        return 1
