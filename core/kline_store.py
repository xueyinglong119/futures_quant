"""多周期主连 K 线 MySQL 存储"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Iterable

from pymysql.connections import Connection

from config.settings import MysqlConfig, load_mysql_config
from core.mysql_kline_store import connect as _connect

TABLE_NAME = "kline_bars"

# 与常见行情软件周期对齐（季/年由日线聚合）
DURATION_1M = 60
DURATION_3M = 3 * 60
DURATION_5M = 5 * 60
DURATION_10M = 10 * 60
DURATION_15M = 15 * 60
DURATION_30M = 30 * 60
DURATION_1H = 60 * 60
DURATION_2H = 2 * 60 * 60
DURATION_3H = 3 * 60 * 60
DURATION_4H = 4 * 60 * 60
DURATION_1D = 24 * 60 * 60
DURATION_1W = 7 * 24 * 60 * 60
DURATION_1MON = 30 * 24 * 60 * 60  # 逻辑月线
DURATION_1Q = 90 * 24 * 60 * 60  # 逻辑季线
DURATION_1Y = 365 * 24 * 60 * 60  # 逻辑年线

DURATION_LABELS = {
    DURATION_1M: "1m",
    DURATION_3M: "3m",
    DURATION_5M: "5m",
    DURATION_10M: "10m",
    DURATION_15M: "15m",
    DURATION_30M: "30m",
    DURATION_1H: "1h",
    DURATION_2H: "2h",
    DURATION_3H: "3h",
    DURATION_4H: "4h",
    DURATION_1D: "1d",
    DURATION_1W: "1w",
    DURATION_1MON: "1mon",
    DURATION_1Q: "1q",
    DURATION_1Y: "1y",
}

LABEL_TO_DURATION = {v: k for k, v in DURATION_LABELS.items()}

# 专业版可直接下载的周期（不含月/季/年，后三者由日线聚合）
DOWNLOADABLE_DURATIONS = [
    DURATION_1M,
    DURATION_3M,
    DURATION_5M,
    DURATION_10M,
    DURATION_15M,
    DURATION_30M,
    DURATION_1H,
    DURATION_2H,
    DURATION_3H,
    DURATION_4H,
    DURATION_1D,
    DURATION_1W,
]

# 30m/1h/2h/3h/4h 不按天勤整点入库，改由 1m 按国内交易时段合成
# 日盘标注：1h=10:00/11:15/14:15/15:00；2h=11:15/15:00；3h=14:15/15:00；4h=15:00
SESSION_BUILT_DURATIONS = (
    DURATION_30M,
    DURATION_1H,
    DURATION_2H,
    DURATION_3H,
    DURATION_4H,
)

# 实时订阅/直接入库：去掉会话合成周期，避免整点 11:00 覆盖 11:15
INGEST_DURATIONS = [
    d for d in DOWNLOADABLE_DURATIONS if d not in SESSION_BUILT_DURATIONS
]
AGGREGATE_FROM_DAILY = [DURATION_1MON, DURATION_1Q, DURATION_1Y]

# 历史补洞：相对早期只跑 1m/5m 时缺的可下载周期（不含会话合成的 2/3/4h）
DOWNLOAD_GAP_DURATIONS = [
    DURATION_3M,
    DURATION_10M,
    DURATION_15M,
]

CREATE_TABLE_SQL = f"""
CREATE TABLE IF NOT EXISTS `{TABLE_NAME}` (
  `id` BIGINT NOT NULL AUTO_INCREMENT,
  `symbol` VARCHAR(64) NOT NULL COMMENT 'TqSdk 主连代码',
  `duration_sec` INT NOT NULL COMMENT 'K线周期秒数',
  `trade_date` DATE NOT NULL COMMENT 'K线结束日(自然日)',
  `datetime_end` DATETIME NOT NULL COMMENT 'K线结束时刻',
  `open` DOUBLE NOT NULL,
  `high` DOUBLE NOT NULL,
  `low` DOUBLE NOT NULL,
  `close` DOUBLE NOT NULL,
  `volume` BIGINT NOT NULL,
  `open_oi` BIGINT NOT NULL DEFAULT 0,
  `created_at` TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `updated_at` TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_sym_dur_end` (`symbol`, `duration_sec`, `datetime_end`),
  KEY `idx_sym_dur_date` (`symbol`, `duration_sec`, `trade_date`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='全主力多周期K线';
"""

UPSERT_SQL = f"""
INSERT INTO `{TABLE_NAME}` (
  symbol, duration_sec, trade_date, datetime_end,
  open, high, low, close, volume, open_oi
) VALUES (
  %(symbol)s, %(duration_sec)s, %(trade_date)s, %(datetime_end)s,
  %(open)s, %(high)s, %(low)s, %(close)s, %(volume)s, %(open_oi)s
)
ON DUPLICATE KEY UPDATE
  trade_date=VALUES(trade_date),
  open=VALUES(open),
  high=VALUES(high),
  low=VALUES(low),
  close=VALUES(close),
  volume=VALUES(volume),
  open_oi=VALUES(open_oi);
"""

SELECT_BARS_SQL = f"""
SELECT datetime_end, open, high, low, close, volume, open_oi
FROM `{TABLE_NAME}`
WHERE symbol=%s AND duration_sec=%s
ORDER BY datetime_end
"""

SELECT_BARS_TAIL_SQL = f"""
SELECT datetime_end, open, high, low, close, volume, open_oi FROM (
  SELECT datetime_end, open, high, low, close, volume, open_oi
  FROM `{TABLE_NAME}`
  WHERE symbol=%s AND duration_sec=%s
  ORDER BY datetime_end DESC
  LIMIT %s
) t ORDER BY datetime_end
"""


def connect(cfg: MysqlConfig | None = None) -> Connection:
    return _connect(cfg or load_mysql_config())


def ensure_table(conn: Connection) -> None:
    with conn.cursor() as cur:
        cur.execute(CREATE_TABLE_SQL)
    conn.commit()


def bar_to_row(symbol: str, duration_sec: int, bar: dict[str, Any]) -> dict[str, Any]:
    dt_end = bar["datetime_end"]
    if isinstance(dt_end, str):
        dt_end = datetime.fromisoformat(dt_end)
    return {
        "symbol": symbol,
        "duration_sec": int(duration_sec),
        "trade_date": dt_end.date(),
        "datetime_end": dt_end,
        "open": float(bar["open"]),
        "high": float(bar["high"]),
        "low": float(bar["low"]),
        "close": float(bar["close"]),
        "volume": int(bar.get("volume") or 0),
        "open_oi": int(bar.get("open_oi") or 0),
    }


def upsert_bars(
    conn: Connection,
    symbol: str,
    duration_sec: int,
    bars: Iterable[dict[str, Any]],
    batch_size: int = 5000,
) -> int:
    rows = [bar_to_row(symbol, duration_sec, b) for b in bars]
    if not rows:
        return 0
    total = 0
    with conn.cursor() as cur:
        for i in range(0, len(rows), batch_size):
            chunk = rows[i : i + batch_size]
            cur.executemany(UPSERT_SQL, chunk)
            total += len(chunk)
    conn.commit()
    return total


def load_bars(
    conn: Connection,
    symbol: str,
    duration_sec: int,
    limit: int | None = None,
    start: datetime | None = None,
    end: datetime | None = None,
) -> list[dict[str, Any]]:
    with conn.cursor() as cur:
        if limit is not None and limit > 0 and start is None and end is None:
            cur.execute(SELECT_BARS_TAIL_SQL, (symbol, duration_sec, int(limit)))
        elif start is not None or end is not None:
            clauses = ["symbol=%s", "duration_sec=%s"]
            params: list[Any] = [symbol, duration_sec]
            if start is not None:
                clauses.append("datetime_end>=%s")
                params.append(start)
            if end is not None:
                clauses.append("datetime_end<=%s")
                params.append(end)
            sql = (
                f"SELECT datetime_end, open, high, low, close, volume, open_oi "
                f"FROM `{TABLE_NAME}` WHERE {' AND '.join(clauses)} "
                f"ORDER BY datetime_end"
            )
            if limit is not None and limit > 0:
                sql += " LIMIT %s"
                params.append(int(limit))
            cur.execute(sql, params)
        else:
            cur.execute(SELECT_BARS_SQL, (symbol, duration_sec))
        rows = cur.fetchall()
    out: list[dict[str, Any]] = []
    for r in rows:
        out.append(
            {
                "datetime_end": r[0],
                "open": float(r[1]),
                "high": float(r[2]),
                "low": float(r[3]),
                "close": float(r[4]),
                "volume": int(r[5] or 0),
                "open_oi": int(r[6] or 0),
            }
        )
    return out


def list_symbols(conn: Connection, duration_sec: int | None = None) -> list[str]:
    if duration_sec is None:
        sql = f"SELECT DISTINCT symbol FROM `{TABLE_NAME}` ORDER BY symbol"
        params: tuple[Any, ...] = ()
    else:
        sql = (
            f"SELECT DISTINCT symbol FROM `{TABLE_NAME}` "
            f"WHERE duration_sec=%s ORDER BY symbol"
        )
        params = (duration_sec,)
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return [r[0] for r in cur.fetchall()]


def count_bars(
    conn: Connection,
    symbol: str | None = None,
    duration_sec: int | None = None,
) -> int:
    clauses: list[str] = []
    params: list[Any] = []
    if symbol is not None:
        clauses.append("symbol=%s")
        params.append(symbol)
    if duration_sec is not None:
        clauses.append("duration_sec=%s")
        params.append(duration_sec)
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    sql = f"SELECT COUNT(*) FROM `{TABLE_NAME}`{where}"
    with conn.cursor() as cur:
        cur.execute(sql, params)
        row = cur.fetchone()
    return int(row[0]) if row else 0


def max_datetime_end(
    conn: Connection, symbol: str, duration_sec: int
) -> datetime | None:
    sql = f"""
    SELECT MAX(datetime_end) FROM `{TABLE_NAME}`
    WHERE symbol=%s AND duration_sec=%s
    """
    with conn.cursor() as cur:
        cur.execute(sql, (symbol, duration_sec))
        row = cur.fetchone()
    return row[0] if row and row[0] else None


def load_watermarks(
    conn: Connection,
    durations: Iterable[int],
    symbols: Iterable[str] | None = None,
) -> dict[tuple[str, int], datetime]:
    """批量取各品种各周期最新 datetime_end，供监控服务水位轮询。"""
    durs = [int(d) for d in durations]
    if not durs:
        return {}
    placeholders = ",".join(["%s"] * len(durs))
    params: list[Any] = list(durs)
    sql = (
        f"SELECT symbol, duration_sec, MAX(datetime_end) FROM `{TABLE_NAME}` "
        f"WHERE duration_sec IN ({placeholders})"
    )
    if symbols is not None:
        syms = [s for s in symbols if s]
        if not syms:
            return {}
        sym_ph = ",".join(["%s"] * len(syms))
        sql += f" AND symbol IN ({sym_ph})"
        params.extend(syms)
    sql += " GROUP BY symbol, duration_sec"
    out: dict[tuple[str, int], datetime] = {}
    with conn.cursor() as cur:
        cur.execute(sql, params)
        for symbol, duration_sec, dt_end in cur.fetchall():
            if dt_end is None:
                continue
            out[(str(symbol), int(duration_sec))] = dt_end
    return out
