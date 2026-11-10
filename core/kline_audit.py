"""收盘后入库准确性校验。

- 入库周期（1/3/5/10/15m、1d、1w）：用天勤已收盘 K 对照 MySQL
- 会话周期（30m/1h/2h/3h/4h）：用库内 1m 按国内小节规则重算后对照
发现缺失/OHLC 不一致时可自动修复（upsert / 会话重建）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterable
from zoneinfo import ZoneInfo

from tqsdk import TqApi, TqAuth

from config.settings import Settings, load_settings
from core.cont_symbols import list_commodity_mains
from core.kline_fetch import wait_klines_ready
from core.kline_store import (
    DURATION_LABELS,
    INGEST_DURATIONS,
    connect,
    ensure_table,
    load_bars,
    upsert_bars,
)
from core.realtime_pipeline import (
    _as_naive,
    _bar_payload_equal,
    _ohlc_equal,
    detect_clock,
    serial_to_rows,
)
from core.session_rebuild import (
    ALL_SESSION_TFS,
    TF_DURATION,
    aggregate_symbol_session,
    detect_1m_clock,
    minutes_as_beijing,
    rebuild_symbol_session_klines,
)

TZ = ZoneInfo("Asia/Shanghai")

# 收盘复核用的序列长度：覆盖一整段日盘/夜盘
AUDIT_TAIL_LENGTH: dict[int, int] = {
    60: 420,
    180: 160,
    300: 120,
    600: 80,
    900: 60,
    86400: 8,
    604800: 4,
}

SUBSCRIBE_BATCH = 6


@dataclass
class DiffItem:
    symbol: str
    label: str
    kind: str  # missing | mismatch | extra
    when: datetime
    detail: str = ""


@dataclass
class AuditReport:
    window_label: str
    start: datetime
    end: datetime
    checked_symbols: int = 0
    ingest_ok: int = 0
    ingest_bad: int = 0
    session_ok: int = 0
    session_bad: int = 0
    fixed: int = 0
    diffs: list[DiffItem] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.ingest_bad == 0 and self.session_bad == 0


def session_audit_window(now: datetime | None = None) -> tuple[datetime, datetime, str]:
    """刚结束的那一段交易时段对应的北京时间校验窗。"""
    if now is None:
        now = datetime.now(TZ)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=TZ)
    else:
        now = now.astimezone(TZ)

    hm = now.hour * 60 + now.minute
    naive_now = now.replace(tzinfo=None, second=0, microsecond=0)

    # 午休：校验上午
    if 11 * 60 + 30 <= hm < 13 * 60 + 28:
        start = naive_now.replace(hour=8, minute=55)
        end = naive_now.replace(hour=11, minute=35)
        return start, end, "morning"

    # 日盘收盘后到夜盘前：校验全天日盘
    if 15 * 60 <= hm < 20 * 60 + 58:
        start = naive_now.replace(hour=8, minute=55)
        end = naive_now.replace(hour=15, minute=5)
        return start, end, "day"

    # 夜盘结束后到早盘前（含周六凌晨）
    if hm <= 8 * 60 + 55:
        end = naive_now.replace(hour=2, minute=40)
        prev = naive_now - timedelta(days=1)
        start = prev.replace(hour=20, minute=55)
        return start, end, "night"

    # 刚进夜盘等边界：退回上一完整日盘
    start = naive_now.replace(hour=8, minute=55)
    end = naive_now.replace(hour=15, minute=5)
    return start, end, "day"


def _bj_of_row(row: dict) -> datetime:
    return _as_naive(row["beijing"])


def _filter_tq_window(rows: list[dict], start: datetime, end: datetime) -> list[dict]:
    out = []
    for row in rows:
        bj = _bj_of_row(row)
        if start <= bj <= end:
            out.append(row)
    return out


def _db_index(bars: list[dict]) -> dict[datetime, dict]:
    return {_as_naive(b["datetime_end"]): b for b in bars}


def _compare_ingest(
    symbol: str,
    label: str,
    db_bars: list[dict],
    tq_rows: list[dict],
    clock: str,
) -> list[DiffItem]:
    diffs: list[DiffItem] = []
    by_dt = _db_index(db_bars)
    seen: set[datetime] = set()
    for row in tq_rows:
        stored = _as_naive(row["utc"] if clock == "utc" else row["beijing"])
        seen.add(stored)
        db = by_dt.get(stored)
        if db is None:
            # 钟别猜错时再试另一侧
            alt = _as_naive(row["beijing"] if clock == "utc" else row["utc"])
            db = by_dt.get(alt)
            if db is not None:
                stored = alt
                seen.add(stored)
        if db is None:
            diffs.append(
                DiffItem(
                    symbol,
                    label,
                    "missing",
                    stored,
                    f"TQ O={row['open']} H={row['high']} L={row['low']} C={row['close']}",
                )
            )
            continue
        if not _bar_payload_equal(db, row):
            diffs.append(
                DiffItem(
                    symbol,
                    label,
                    "mismatch",
                    stored,
                    f"DB OHLCV={db['open']}/{db['high']}/{db['low']}/{db['close']}/{db['volume']} "
                    f"TQ={row['open']}/{row['high']}/{row['low']}/{row['close']}/{row['volume']}",
                )
            )
    # 库内多出来的（同窗内无 TQ 对应）只在 OHLC 对不上任意 TQ 时记 extra
    tq_by_ohlc = []
    for row in tq_rows:
        tq_by_ohlc.append(row)
    for dt, db in by_dt.items():
        if dt in seen:
            continue
        if any(_ohlc_equal(db, row) for row in tq_by_ohlc):
            continue
        diffs.append(
            DiffItem(
                symbol,
                label,
                "extra",
                dt,
                f"DB O={db['open']} H={db['high']} L={db['low']} C={db['close']}",
            )
        )
    return diffs


def _load_db_window(
    conn,
    symbol: str,
    duration_sec: int,
    start: datetime,
    end: datetime,
) -> list[dict]:
    # UTC/北京最多差 8h，两侧各扩，再在内存按两侧钟过滤
    pad_start = start - timedelta(hours=10)
    pad_end = end + timedelta(hours=10)
    return load_bars(conn, symbol, duration_sec, start=pad_start, end=pad_end)


def _db_bars_in_window(
    bars: list[dict],
    start: datetime,
    end: datetime,
    *,
    as_beijing: bool,
) -> list[dict]:
    out = []
    for b in bars:
        dt = _as_naive(b["datetime_end"])
        bj = dt if as_beijing else dt + timedelta(hours=8)
        if start <= bj <= end:
            out.append(b)
    return out


def audit_symbol_session(
    conn,
    symbol: str,
    start: datetime,
    end: datetime,
) -> list[DiffItem]:
    """用 1m 重算会话周期，对照库内已存。"""
    from core.kline_store import DURATION_1M

    q_start = start - timedelta(days=1)
    q_end = end + timedelta(days=1)
    raw_1m = load_bars(conn, symbol, DURATION_1M, start=q_start, end=q_end)
    if not raw_1m:
        return [
            DiffItem(symbol, "1m", "missing", start, "无 1 分钟数据，无法校验会话周期")
        ]
    clock = detect_1m_clock(raw_1m)
    minute_bj = minutes_as_beijing(raw_1m, clock)
    clipped = [
        m
        for m in minute_bj
        if start - timedelta(hours=1) <= m["datetime"] <= end + timedelta(hours=1)
    ]
    built = aggregate_symbol_session(clipped, ALL_SESSION_TFS, include_night=True)
    diffs: list[DiffItem] = []
    for tf in ALL_SESSION_TFS:
        dur = TF_DURATION[tf]
        expect = [
            r
            for r in built.get(tf, [])
            if start <= _as_naive(r["datetime_end"]) <= end
        ]
        raw_db = _load_db_window(conn, symbol, dur, start, end)
        # 会话周期按北京结束标注入库
        db_win = _db_bars_in_window(raw_db, start, end, as_beijing=True)
        by_dt = _db_index(db_win)
        seen: set[datetime] = set()
        for row in expect:
            dt = _as_naive(row["datetime_end"])
            seen.add(dt)
            db = by_dt.get(dt)
            if db is None:
                diffs.append(
                    DiffItem(
                        symbol,
                        tf,
                        "missing",
                        dt,
                        f"期望 C={row['close']}",
                    )
                )
                continue
            if not _ohlc_equal(db, row):
                diffs.append(
                    DiffItem(
                        symbol,
                        tf,
                        "mismatch",
                        dt,
                        f"DB={db['open']}/{db['high']}/{db['low']}/{db['close']} "
                        f"期望={row['open']}/{row['high']}/{row['low']}/{row['close']}",
                    )
                )
        for dt, db in by_dt.items():
            if dt in seen:
                continue
            if any(_ohlc_equal(db, row) for row in expect):
                continue
            diffs.append(
                DiffItem(
                    symbol,
                    tf,
                    "extra",
                    dt,
                    f"DB C={db['close']}（可能是天勤整点残留）",
                )
            )
    return diffs


def _fix_session(conn, symbol: str, start: datetime, end: datetime) -> int:
    stats = rebuild_symbol_session_klines(
        conn,
        symbol,
        tfs=ALL_SESSION_TFS,
        start=start,
        end=end,
        execute=True,
        include_night=True,
    )
    return sum(int(stats.get(f"{tf}_upserted", 0) or 0) for tf in ALL_SESSION_TFS)


def _fix_ingest_rows(
    conn,
    symbol: str,
    duration_sec: int,
    tq_rows: list[dict],
    clock: str,
) -> int:
    bars = []
    for row in tq_rows:
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
    return upsert_bars(conn, symbol, duration_sec, bars) if bars else 0


def audit_symbol_ingest(
    api: TqApi,
    conn,
    symbol: str,
    start: datetime,
    end: datetime,
    durations: Iterable[int],
    *,
    fix: bool,
) -> tuple[list[DiffItem], int]:
    """对照天勤校验并可选修复入库周期。返回 (diffs, fixed_bars)。"""
    from datetime import timezone

    now_utc = datetime.now(timezone.utc).replace(tzinfo=None, microsecond=0)
    serials: dict[int, Any] = {}
    for dur in durations:
        length = AUDIT_TAIL_LENGTH.get(dur, 100)
        try:
            serials[dur] = api.get_kline_serial(symbol, dur, data_length=length)
        except Exception as exc:  # noqa: BLE001
            print(
                f"[audit] 订阅失败 {symbol} {DURATION_LABELS.get(dur, dur)}: {exc}",
                flush=True,
            )
    if not serials:
        return (
            [DiffItem(symbol, "-", "missing", start, "全部周期订阅失败")],
            0,
        )
    wait_klines_ready(api, list(serials.values()), timeout_sec=90)

    diffs: list[DiffItem] = []
    fixed = 0
    for dur, kl in serials.items():
        label = DURATION_LABELS.get(dur, str(dur))
        closed = serial_to_rows(kl, dur, now_utc)
        tq_win = _filter_tq_window(closed, start, end)
        raw_db = _load_db_window(conn, symbol, dur, start, end)
        clock = detect_clock(raw_db[-40:] if raw_db else [], closed) if closed else "beijing"
        db_win = _db_bars_in_window(
            raw_db, start, end, as_beijing=(clock == "beijing")
        )
        # 日线/周线：窗可能只含 1 根，放宽用全部 closed 里落在窗自然日的
        if dur >= 86400:
            tq_win = [
                r
                for r in closed
                if start.date() <= _bj_of_row(r).date() <= end.date()
            ]
            db_win = [
                b
                for b in raw_db
                if start.date()
                <= _as_naive(b["datetime_end"]).date()
                <= end.date() + timedelta(days=1)
            ]
        part = _compare_ingest(symbol, label, db_win, tq_win, clock)
        diffs.extend(part)
        if fix and part and tq_win:
            fixed += _fix_ingest_rows(conn, symbol, dur, tq_win, clock)
    return diffs, fixed


def run_post_close_audit(
    *,
    settings: Settings | None = None,
    symbols: list[str] | None = None,
    now: datetime | None = None,
    fix: bool = True,
    report_dir: Path | None = None,
    durations: list[int] | None = None,
) -> AuditReport:
    """收盘后全量校验；默认发现问题自动修复。"""
    settings = settings or load_settings()
    start, end, window_label = session_audit_window(now)
    report = AuditReport(window_label=window_label, start=start, end=end)
    durations = list(durations or INGEST_DURATIONS)
    report_dir = report_dir or Path("reports")
    report_dir.mkdir(parents=True, exist_ok=True)

    print(
        f"[audit] 开始校验 window={window_label} "
        f"{start:%Y-%m-%d %H:%M} ~ {end:%Y-%m-%d %H:%M} fix={fix}",
        flush=True,
    )

    api = TqApi(auth=TqAuth(settings.auth.user, settings.auth.password))
    conn = connect()
    ensure_table(conn)
    try:
        if symbols is None:
            symbols = list_commodity_mains(api)
        report.checked_symbols = len(symbols)

        # 1) 会话周期：不依赖天勤，先快校
        for i, symbol in enumerate(symbols, 1):
            s_diffs = audit_symbol_session(conn, symbol, start, end)
            if not s_diffs:
                report.session_ok += 1
            else:
                report.diffs.extend(s_diffs)
                if fix:
                    n = _fix_session(conn, symbol, start, end)
                    report.fixed += n
                    report.session_ok += 1
                    print(
                        f"[audit] 会话修复 {symbol} upsert≈{n} "
                        f"diffs={len(s_diffs)} ({i}/{len(symbols)})",
                        flush=True,
                    )
                else:
                    report.session_bad += 1
            if i % 20 == 0:
                print(
                    f"[audit] 会话进度 {i}/{len(symbols)} "
                    f"ok={report.session_ok} bad={report.session_bad}",
                    flush=True,
                )

        # 2) 入库周期：分批订天勤
        pending = list(symbols)
        done = 0
        while pending:
            batch = pending[:SUBSCRIBE_BATCH]
            del pending[:SUBSCRIBE_BATCH]
            for symbol in batch:
                i_diffs, fixed = audit_symbol_ingest(
                    api, conn, symbol, start, end, durations, fix=fix
                )
                done += 1
                hard = [d for d in i_diffs if d.kind in ("missing", "mismatch")]
                if hard:
                    report.diffs.extend(i_diffs)
                    report.fixed += fixed
                    if fix and fixed > 0:
                        report.ingest_ok += 1
                    else:
                        report.ingest_bad += 1
                    print(
                        f"[audit] 入库差异 {symbol} "
                        f"hard={len(hard)} fixed_bars={fixed} "
                        f"({done}/{len(symbols)})",
                        flush=True,
                    )
                else:
                    report.ingest_ok += 1
                    report.fixed += fixed
            print(
                f"[audit] 入库进度 {done}/{len(symbols)} "
                f"ok={report.ingest_ok} bad={report.ingest_bad}",
                flush=True,
            )
    finally:
        conn.close()
        api.close()

    stamp = datetime.now(TZ).strftime("%Y%m%d_%H%M%S")
    path = report_dir / f"kline_audit_{stamp}.txt"
    _write_report(path, report)
    status = "PASS" if report.ok else "FAIL"
    print(
        f"[audit] {status} symbols={report.checked_symbols} "
        f"ingest_ok={report.ingest_ok} ingest_bad={report.ingest_bad} "
        f"session_ok={report.session_ok} session_bad={report.session_bad} "
        f"fixed={report.fixed} diffs={len(report.diffs)} report={path}",
        flush=True,
    )
    return report


def _write_report(path: Path, report: AuditReport) -> None:
    lines = [
        f"window={report.window_label} {report.start} .. {report.end}",
        f"symbols={report.checked_symbols}",
        f"ingest_ok={report.ingest_ok} ingest_bad={report.ingest_bad}",
        f"session_ok={report.session_ok} session_bad={report.session_bad}",
        f"fixed={report.fixed} diffs={len(report.diffs)}",
        f"result={'PASS' if report.ok else 'FAIL'}",
        "",
    ]
    for d in report.diffs[:500]:
        lines.append(
            f"{d.kind:8} {d.symbol} {d.label} {d.when:%Y-%m-%d %H:%M} {d.detail}"
        )
    if len(report.diffs) > 500:
        lines.append(f"... truncated {len(report.diffs) - 500} more")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
