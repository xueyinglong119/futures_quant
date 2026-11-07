"""乙二醇 2701：1 分钟 K 线头肩形态监控。

默认窗口：昨夜 21:00 开盘 → 当日 15:00 收盘。
先回扫已走完的 K 线，再实时盯盘；确认头肩顶/底时控制台告警 + Windows 气泡 + 写报告。
"""

from __future__ import annotations

import argparse
import math
import subprocess
import sys
import time as time_module
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
from tqsdk import TqApi, TqAuth

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from config.settings import load_settings
from core.alert_store import add_alert
from core.push_notify import send_alert_push
from strategies.head_shoulders import HeadShouldersStrategy
from utils.symbols import normalize_contract_symbol

TZ = ZoneInfo("Asia/Shanghai")
ALERT_TAG = "!!! HS_ALERT !!!"
DEFAULT_SYMBOL = "DCE.eg2701"
MONITOR_SYMBOL = DEFAULT_SYMBOL


def _session_start(today: date) -> datetime:
    """昨夜 21:00（相对「今日」交易日）。"""
    return datetime.combine(today - timedelta(days=1), datetime.min.time()).replace(
        hour=21, minute=0, second=0
    )


def _session_end(today: date) -> datetime:
    return datetime.combine(today, datetime.min.time()).replace(
        hour=15, minute=0, second=0
    )


def _ns_to_dt(dt_ns: float | int) -> datetime | None:
    if dt_ns is None or (isinstance(dt_ns, float) and math.isnan(dt_ns)):
        return None
    return datetime.fromtimestamp(dt_ns / 1e9, tz=TZ).replace(tzinfo=None)


def _in_monitor_window(dt: datetime, start: datetime, end: datetime) -> bool:
    if dt < start or dt > end:
        return False
    t = dt.time()
    # 夜盘 21:00-23:00；日盘 09:00-15:00（含小节休市空档，休市无成交 K 通常很少）
    if t >= datetime.strptime("21:00", "%H:%M").time():
        return t <= datetime.strptime("23:00", "%H:%M").time()
    if t >= datetime.strptime("09:00", "%H:%M").time():
        return t <= datetime.strptime("15:00", "%H:%M").time()
    return False


def _klines_to_df(klines: pd.DataFrame, start: datetime, end: datetime) -> pd.DataFrame:
    rows: list[dict] = []
    for i in range(len(klines)):
        dt = _ns_to_dt(klines.iloc[i]["datetime"])
        if dt is None or not _in_monitor_window(dt, start, end):
            continue
        rows.append(
            {
                "datetime": dt,
                "open": float(klines.iloc[i]["open"]),
                "high": float(klines.iloc[i]["high"]),
                "low": float(klines.iloc[i]["low"]),
                "close": float(klines.iloc[i]["close"]),
                "volume": int(klines.iloc[i]["volume"]),
                "open_oi": int(klines.iloc[i]["open_oi"]),
            }
        )
    if not rows:
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
    return pd.DataFrame(rows).sort_values("datetime").reset_index(drop=True)


def _windows_toast(title: str, message: str) -> None:
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
        "$ni.ShowBalloonTip(10000); "
        "Start-Sleep -Seconds 11; "
        "$ni.Dispose()"
    )
    try:
        subprocess.Popen(
            [
                "powershell",
                "-NoProfile",
                "-WindowStyle",
                "Hidden",
                "-Command",
                ps,
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError:
        pass


def notify(kind: str, when: datetime, reason: str, alert_path: Path) -> None:
    title = f"乙二醇2701 {kind}"
    body = f"{when.strftime('%m-%d %H:%M')} {reason}"
    line = f"{ALERT_TAG} {title} | {body}"
    print(line, flush=True)
    alert_path.parent.mkdir(parents=True, exist_ok=True)
    with alert_path.open("a", encoding="utf-8") as f:
        f.write(
            f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} | {kind} | "
            f"{when.isoformat(timespec='seconds')} | {reason}\n"
        )
    _windows_toast(title, body)
    try:
        alert = add_alert(
            symbol=MONITOR_SYMBOL,
            kind=kind,
            reason=reason,
            bar_time=when.isoformat(timespec="seconds"),
        )
        push_result = send_alert_push(alert)
        print(f"App推送结果: {push_result}", flush=True)
    except Exception as exc:  # noqa: BLE001 — 推送失败不影响盯盘
        print(f"写入/推送 App 告警失败: {exc}", flush=True)


def _make_strategy() -> HeadShouldersStrategy:
    # 1 分钟参数：比 30 分钟更宽的跨度，拐点略放大以抑噪
    return HeadShouldersStrategy(
        pivot_order=8,
        shoulder_tolerance=0.03,
        min_span_bars=25,
        max_span_bars=240,
        max_right_above_left_ratio=0.015,
        min_head_neck_ratio=0.008,
        max_volume=1,
    )


def _scan_all(
    df: pd.DataFrame,
    symbol: str,
    alert_path: Path,
    seen: set[str],
) -> None:
    """完整回放扫描；已告警形态用 seen 去重。"""
    strategy = _make_strategy()
    for i in range(1, len(df) + 1):
        window = df.iloc[:i]
        signal = strategy.on_bar(window, max_volume=1, symbol=symbol)
        if signal is None:
            continue
        when = pd.Timestamp(window.iloc[-1]["datetime"]).to_pydatetime()
        kind = "头肩顶" if signal.action == "sell" else "头肩底"
        key = f"{kind}|{when.isoformat()}|{signal.reason}"
        if key in seen:
            continue
        seen.add(key)
        notify(kind, when, signal.reason, alert_path)


def main() -> None:
    parser = argparse.ArgumentParser(description="EG2701 1分钟头肩监控")
    parser.add_argument("--symbol", default=DEFAULT_SYMBOL)
    parser.add_argument(
        "--trade-date",
        default="",
        help="交易日 YYYY-MM-DD，默认今天；监控昨夜21:00至该日15:00",
    )
    parser.add_argument(
        "--until",
        default="",
        help="结束时刻 YYYY-MM-DDTHH:MM，默认交易日 15:00",
    )
    args = parser.parse_args()

    settings = load_settings()
    symbol = normalize_contract_symbol(args.symbol)
    global MONITOR_SYMBOL
    MONITOR_SYMBOL = symbol
    today = date.fromisoformat(args.trade_date) if args.trade_date else date.today()
    start = _session_start(today)
    end = (
        datetime.fromisoformat(args.until)
        if args.until
        else _session_end(today)
    )

    alert_path = ROOT / "reports" / "eg2701_hs_1m_alerts.txt"
    status_path = ROOT / "reports" / "eg2701_hs_1m_status.txt"

    print(
        f"监控启动 {symbol} | 1分钟K | {start} → {end}",
        flush=True,
    )
    print(f"告警文件: {alert_path}", flush=True)

    seen: set[str] = set()

    api = TqApi(auth=TqAuth(settings.auth.user, settings.auth.password))
    try:
        # 覆盖昨夜至今 + 缓冲
        klines = api.get_kline_serial(symbol, 60, data_length=2000)
        deadline = time_module.time() + 45
        while time_module.time() < deadline:
            api.wait_update(deadline=deadline)
            if len(klines) > 0 and not (
                isinstance(klines.iloc[-1]["datetime"], float)
                and math.isnan(klines.iloc[-1]["datetime"])
            ):
                break
        else:
            raise TimeoutError(f"等待 {symbol} 1分钟K线超时")

        df = _klines_to_df(klines, start, end)
        print(f"回扫窗口内已有 K 线: {len(df)} 根", flush=True)
        if len(df):
            print(
                f"首根 {df.iloc[0]['datetime']} | 末根 {df.iloc[-1]['datetime']}",
                flush=True,
            )

        _scan_all(df, symbol, alert_path, seen)
        if not seen:
            print("回扫完成：尚未确认到头肩顶/底", flush=True)
        else:
            print(f"回扫完成：已通知 {len(seen)} 次形态", flush=True)

        # 实时盯盘至 15:00
        last_dt_ns = klines.iloc[-1]["datetime"] if len(klines) else None
        last_heartbeat = time_module.time()
        while datetime.now() < end:
            api.wait_update(deadline=time_module.time() + 5.0)
            now = datetime.now()

            if time_module.time() - last_heartbeat >= 60:
                last_dt = df.iloc[-1]["datetime"] if len(df) else "无"
                msg = (
                    f"心跳 {now.strftime('%H:%M:%S')} | K线{len(df)}根 | "
                    f"末根 {last_dt} | 已告警 {len(seen)}"
                )
                print(msg, flush=True)
                status_path.write_text(msg + "\n", encoding="utf-8")
                last_heartbeat = time_module.time()

            cur_dt_ns = klines.iloc[-1]["datetime"] if len(klines) else None
            if cur_dt_ns == last_dt_ns and not api.is_changing(klines):
                continue
            last_dt_ns = cur_dt_ns

            df = _klines_to_df(klines, start, end)
            _scan_all(df, symbol, alert_path, seen)

        print(
            f"监控结束 {datetime.now().strftime('%H:%M:%S')} | 累计告警 {len(seen)}",
            flush=True,
        )
        status_path.write_text(
            f"ENDED {datetime.now().isoformat(timespec='seconds')} alerts={len(seen)}\n",
            encoding="utf-8",
        )
    finally:
        api.close()


if __name__ == "__main__":
    main()
