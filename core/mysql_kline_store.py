"""MySQL K 线存储"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import pymysql
from pymysql.connections import Connection

from config.settings import MysqlConfig, load_mysql_config

TABLE_NAME = "session_kline_30m"

CREATE_TABLE_SQL = f"""
CREATE TABLE IF NOT EXISTS `{TABLE_NAME}` (
  `id` BIGINT NOT NULL AUTO_INCREMENT,
  `symbol` VARCHAR(64) NOT NULL COMMENT 'TqSdk 合约/主连',
  `trade_date` DATE NOT NULL COMMENT '自然日',
  `datetime_start` DATETIME NOT NULL COMMENT 'K线开始标注时刻',
  `datetime_end` DATETIME NOT NULL COMMENT 'K线结束标注时刻',
  `open` DOUBLE NOT NULL,
  `high` DOUBLE NOT NULL,
  `low` DOUBLE NOT NULL,
  `close` DOUBLE NOT NULL,
  `volume` BIGINT NOT NULL,
  `open_oi` BIGINT NOT NULL,
  `created_at` TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `updated_at` TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_symbol_datetime_end` (`symbol`, `datetime_end`),
  KEY `idx_symbol_trade_date` (`symbol`, `trade_date`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='国内规则30分钟K线';
"""

UPSERT_SQL = f"""
INSERT INTO `{TABLE_NAME}` (
  symbol, trade_date, datetime_start, datetime_end,
  open, high, low, close, volume, open_oi
) VALUES (
  %(symbol)s, %(trade_date)s, %(datetime_start)s, %(datetime_end)s,
  %(open)s, %(high)s, %(low)s, %(close)s, %(volume)s, %(open_oi)s
)
ON DUPLICATE KEY UPDATE
  trade_date=VALUES(trade_date),
  datetime_start=VALUES(datetime_start),
  open=VALUES(open),
  high=VALUES(high),
  low=VALUES(low),
  close=VALUES(close),
  volume=VALUES(volume),
  open_oi=VALUES(open_oi);
"""


def connect(cfg: MysqlConfig | None = None) -> Connection:
    c = cfg or load_mysql_config()
    return pymysql.connect(
        host=c.host,
        port=c.port,
        user=c.user,
        password=c.password,
        database=c.database,
        charset="utf8mb4",
        autocommit=False,
        connect_timeout=30,
    )


def ensure_table(conn: Connection) -> None:
    with conn.cursor() as cur:
        cur.execute(CREATE_TABLE_SQL)
    conn.commit()


def _parse_dt(value: str) -> datetime:
    return datetime.fromisoformat(value)


def bar_to_row(symbol: str, bar: dict[str, Any]) -> dict[str, Any]:
    dt_end = _parse_dt(bar["datetime_end"])
    return {
        "symbol": symbol,
        "trade_date": dt_end.date(),
        "datetime_start": _parse_dt(bar["datetime_start"]),
        "datetime_end": dt_end,
        "open": float(bar["open"]),
        "high": float(bar["high"]),
        "low": float(bar["low"]),
        "close": float(bar["close"]),
        "volume": int(bar["volume"]),
        "open_oi": int(bar["open_oi"]),
    }


def upsert_bars(conn: Connection, symbol: str, bars: list[dict]) -> int:
    if not bars:
        return 0
    rows = [bar_to_row(symbol, bar) for bar in bars]
    with conn.cursor() as cur:
        cur.executemany(UPSERT_SQL, rows)
    conn.commit()
    return len(rows)


def count_bars(conn: Connection, symbol: str, year: int, month: int) -> int:
    sql = f"""
    SELECT COUNT(*) FROM `{TABLE_NAME}`
    WHERE symbol=%s AND YEAR(trade_date)=%s AND MONTH(trade_date)=%s
    """
    with conn.cursor() as cur:
        cur.execute(sql, (symbol, year, month))
        row = cur.fetchone()
    return int(row[0]) if row else 0
