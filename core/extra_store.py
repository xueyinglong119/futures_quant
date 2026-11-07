"""换月记录 / Tick 存储"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Iterable

from pymysql.connections import Connection

from core.mysql_kline_store import connect as _connect
from config.settings import MysqlConfig, load_mysql_config

ROLL_TABLE = "cont_roll_history"
TICK_TABLE = "tick_bars"

CREATE_ROLL_SQL = f"""
CREATE TABLE IF NOT EXISTS `{ROLL_TABLE}` (
  `id` BIGINT NOT NULL AUTO_INCREMENT,
  `main_symbol` VARCHAR(64) NOT NULL COMMENT '主连 KQ.m@...',
  `trade_date` DATE NOT NULL,
  `underlying` VARCHAR(64) NOT NULL COMMENT '当日主力标的合约',
  `created_at` TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `updated_at` TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_main_date` (`main_symbol`, `trade_date`),
  KEY `idx_underlying` (`underlying`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='主力主连换月/标的历史';
"""

CREATE_TICK_SQL = f"""
CREATE TABLE IF NOT EXISTS `{TICK_TABLE}` (
  `id` BIGINT NOT NULL AUTO_INCREMENT,
  `symbol` VARCHAR(64) NOT NULL,
  `datetime_end` DATETIME(6) NOT NULL,
  `last_price` DOUBLE NOT NULL,
  `highest` DOUBLE NULL,
  `lowest` DOUBLE NULL,
  `average` DOUBLE NULL,
  `volume` BIGINT NOT NULL DEFAULT 0,
  `amount` DOUBLE NULL,
  `open_interest` BIGINT NULL,
  `bid_price1` DOUBLE NULL,
  `ask_price1` DOUBLE NULL,
  `bid_volume1` BIGINT NULL,
  `ask_volume1` BIGINT NULL,
  `created_at` TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_sym_dt` (`symbol`, `datetime_end`),
  KEY `idx_sym_dt` (`symbol`, `datetime_end`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='Tick研究数据';
"""

UPSERT_ROLL_SQL = f"""
INSERT INTO `{ROLL_TABLE}` (main_symbol, trade_date, underlying)
VALUES (%(main_symbol)s, %(trade_date)s, %(underlying)s)
ON DUPLICATE KEY UPDATE underlying=VALUES(underlying);
"""

UPSERT_TICK_SQL = f"""
INSERT INTO `{TICK_TABLE}` (
  symbol, datetime_end, last_price, highest, lowest, average,
  volume, amount, open_interest,
  bid_price1, ask_price1, bid_volume1, ask_volume1
) VALUES (
  %(symbol)s, %(datetime_end)s, %(last_price)s, %(highest)s, %(lowest)s, %(average)s,
  %(volume)s, %(amount)s, %(open_interest)s,
  %(bid_price1)s, %(ask_price1)s, %(bid_volume1)s, %(ask_volume1)s
)
ON DUPLICATE KEY UPDATE
  last_price=VALUES(last_price),
  highest=VALUES(highest),
  lowest=VALUES(lowest),
  average=VALUES(average),
  volume=VALUES(volume),
  amount=VALUES(amount),
  open_interest=VALUES(open_interest),
  bid_price1=VALUES(bid_price1),
  ask_price1=VALUES(ask_price1),
  bid_volume1=VALUES(bid_volume1),
  ask_volume1=VALUES(ask_volume1);
"""


def connect(cfg: MysqlConfig | None = None) -> Connection:
    return _connect(cfg or load_mysql_config())


def ensure_extra_tables(conn: Connection) -> None:
    with conn.cursor() as cur:
        cur.execute(CREATE_ROLL_SQL)
        cur.execute(CREATE_TICK_SQL)
    conn.commit()


def upsert_rolls(conn: Connection, rows: Iterable[dict[str, Any]], batch_size: int = 2000) -> int:
    data = list(rows)
    if not data:
        return 0
    total = 0
    with conn.cursor() as cur:
        for i in range(0, len(data), batch_size):
            chunk = data[i : i + batch_size]
            cur.executemany(UPSERT_ROLL_SQL, chunk)
            total += len(chunk)
    conn.commit()
    return total


def upsert_ticks(conn: Connection, rows: Iterable[dict[str, Any]], batch_size: int = 5000) -> int:
    data = list(rows)
    if not data:
        return 0
    total = 0
    with conn.cursor() as cur:
        for i in range(0, len(data), batch_size):
            chunk = data[i : i + batch_size]
            cur.executemany(UPSERT_TICK_SQL, chunk)
            total += len(chunk)
    conn.commit()
    return total


def count_table(conn: Connection, table: str, symbol: str | None = None) -> int:
    if symbol:
        sql = f"SELECT COUNT(*) FROM `{table}` WHERE symbol=%s"
        params: tuple[Any, ...] = (symbol,)
        if table == ROLL_TABLE:
            sql = f"SELECT COUNT(*) FROM `{table}` WHERE main_symbol=%s"
    else:
        sql = f"SELECT COUNT(*) FROM `{table}`"
        params = ()
    with conn.cursor() as cur:
        cur.execute(sql, params)
        row = cur.fetchone()
    return int(row[0]) if row else 0
