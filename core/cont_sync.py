"""连续主力（KQ.m@）日终补库。

盘中检测/采集已改订持仓月约；主连仅作长序列对照，每天下午 15:30 起补最近若干日。
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from tqsdk import TqApi, TqAuth

from config.settings import load_settings
from core.cont_symbols import list_commodity_cont_mains
from core.kline_fetch import (
    LIVE_MAX_LENGTH,
    aggregate_monthly_from_daily,
    aggregate_quarterly_from_daily,
    aggregate_yearly_from_daily,
    wait_klines_ready,
)
from core.kline_store import (
    DURATION_1D,
    DURATION_1M,
    DURATION_1MON,
    DURATION_1Q,
    DURATION_1Y,
    DURATION_5M,
    DURATION_LABELS,
    connect,
    ensure_table,
    load_bars,
    upsert_bars,
)
from core.realtime_pipeline import detect_clock, serial_to_rows
from core.session_rebuild import rebuild_symbol_session_klines

TZ = ZoneInfo("Asia/Shanghai")

# 日终补库周期：与盘中月约主路径对齐的核心三档 + 会话合成
CONT_SYNC_DURATIONS = (DURATION_1M, DURATION_5M, DURATION_1D)
# 覆盖周末缺口：周五收盘后补到下周一还能接上
LOOKBACK_CALENDAR_DAYS = 5
# 每批订阅品种数，避免一次挂太多次序列
SUBSCRIBE_BATCH = 8

_DATA_LENGTH: dict[int, int] = {
    DURATION_1M: min(LIVE_MAX_LENGTH, LOOKBACK_CALENDAR_DAYS * 500),
    DURATION_5M: min(LIVE_MAX_LENGTH, LOOKBACK_CALENDAR_DAYS * 120),
    DURATION_1D: min(LIVE_MAX_LENGTH, LOOKBACK_CALENDAR_DAYS + 5),
}


def _beijing_now() -> datetime:
    return datetime.now(TZ)


def should_run_cont_sync(now: datetime | None = None, *, last_done: date | None = None) -> bool:
    """工作日 15:30～20:50，且当天还没跑过。"""
    now = now or _beijing_now()
    if now.tzinfo is None:
        now = now.replace(tzinfo=TZ)
    else:
        now = now.astimezone(TZ)
    if now.weekday() > 4:
        return False
    hm = now.hour * 60 + now.minute
    if hm < 15 * 60 + 30 or hm >= 20 * 60 + 50:
        return False
    if last_done == now.date():
        return False
    return True


def _sync_one(api: TqApi, conn, symbol: str, since: date) -> dict[str, int]:
    now_utc = datetime.now(timezone.utc).replace(tzinfo=None, microsecond=0)
    serials = {}
    for dur in CONT_SYNC_DURATIONS:
        length = _DATA_LENGTH.get(dur, 200)
        try:
            serials[dur] = api.get_kline_serial(symbol, dur, data_length=length)
        except Exception as exc:  # noqa: BLE001
            print(
                f"[cont-sync] 订阅失败 {symbol} {DURATION_LABELS.get(dur, dur)}: {exc}",
                flush=True,
            )
    if not serials:
        return {}
    wait_klines_ready(api, list(serials.values()), timeout_sec=90)

    wrote: dict[str, int] = {}
    for dur, kl in serials.items():
        label = DURATION_LABELS.get(dur, str(dur))
        closed = serial_to_rows(kl, dur, now_utc)
        if not closed:
            wrote[label] = 0
            continue
        recent = load_bars(conn, symbol, dur, limit=40)
        clock = detect_clock(recent, closed)
        bars = []
        for row in closed:
            bj = row["beijing"]
            if bj.date() < since:
                continue
            stored = row["utc"] if clock == "utc" else row["beijing"]
            bars.append(
                {
                    "datetime_end": stored,
                    "open": row["open"],
                    "high": row["high"],
                    "low": row["low"],
                    "close": row["close"],
                    "volume": row["volume"],
                    "open_oi": row["open_oi"],
                }
            )
        n = upsert_bars(conn, symbol, dur, bars) if bars else 0
        wrote[label] = n

    if wrote.get("1d", 0) > 0:
        daily = load_bars(conn, symbol, DURATION_1D)
        upsert_bars(conn, symbol, DURATION_1MON, aggregate_monthly_from_daily(daily))
        upsert_bars(conn, symbol, DURATION_1Q, aggregate_quarterly_from_daily(daily))
        upsert_bars(conn, symbol, DURATION_1Y, aggregate_yearly_from_daily(daily))
        wrote["htf"] = 1

    # 会话 30m/1h/2h… 从 1m 合成
    try:
        end = datetime.now()
        start = (end - timedelta(days=LOOKBACK_CALENDAR_DAYS)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        sess = rebuild_symbol_session_klines(
            conn, symbol, start=start, end=end, execute=True, include_night=True
        )
        wrote["session"] = sum(
            int(sess.get(f"{k}_upserted") or 0)
            for k in ("30m", "1h", "2h", "3h", "4h")
        )
    except Exception as exc:  # noqa: BLE001
        print(f"[cont-sync] 会话合成失败 {symbol}: {exc}", flush=True)
        wrote["session"] = 0
    return wrote


def run_cont_mains_sync(*, lookback_days: int = LOOKBACK_CALENDAR_DAYS) -> dict[str, int]:
    """补全商品 CONT 主连最近 lookback_days 日历日的 K 线。返回汇总计数。"""
    settings = load_settings()
    api = TqApi(auth=TqAuth(settings.auth.user, settings.auth.password))
    conn = connect()
    ensure_table(conn)
    summary = {"symbols": 0, "bars": 0, "errors": 0}
    try:
        symbols = list_commodity_cont_mains(api)
        since = (_beijing_now().date() - timedelta(days=max(1, lookback_days) - 1))
        print(
            f"[cont-sync] 开始 CONT 主连 {len(symbols)} 个 since={since} "
            f"周期={','.join(DURATION_LABELS[d] for d in CONT_SYNC_DURATIONS)}",
            flush=True,
        )
        t0 = datetime.now()
        for i in range(0, len(symbols), SUBSCRIBE_BATCH):
            batch = symbols[i : i + SUBSCRIBE_BATCH]
            for j, symbol in enumerate(batch, i + 1):
                try:
                    wrote = _sync_one(api, conn, symbol, since)
                    n = sum(v for k, v in wrote.items() if k not in ("htf",) and isinstance(v, int))
                    summary["symbols"] += 1
                    summary["bars"] += n
                    detail = ", ".join(f"{k}={v}" for k, v in wrote.items() if v)
                    print(
                        f"[cont-sync] [{j}/{len(symbols)}] {symbol} bars={n} {detail}",
                        flush=True,
                    )
                except Exception as exc:  # noqa: BLE001
                    summary["errors"] += 1
                    print(f"[cont-sync] 失败 {symbol}: {exc}", flush=True)
        elapsed = (datetime.now() - t0).total_seconds()
        print(
            f"[cont-sync] 完成 symbols={summary['symbols']} bars={summary['bars']} "
            f"errors={summary['errors']} elapsed={elapsed:.1f}s",
            flush=True,
        )
        return summary
    finally:
        conn.close()
        api.close()
