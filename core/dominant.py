"""持仓量主力月约：解析、换月确认、落库与冷启动。

口径（用户 11-23 拍板）：
  - 日常主力 = 该品种未下市期货里 **持仓量最大** 的月份；
  - 换月：新月持仓连续 ``ROLL_CONFIRM_DAYS`` 个刷新周期领先才切换
    （同日若成交也更大则加分，但不单独靠成交切）；
  - 检测/采集订阅月约（如 ``SHFE.ag2702``），不再用 ``KQ.m@`` 拼价主连；
  - 冷启动按检测 lookback 窗口拉历史，日线约一年，日内约 60～90 天。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from pymysql.connections import Connection
from tqsdk import TqApi

from core.cont_symbols import COMMODITY_EXCHANGES, drop_illiquid, list_main_continuous
from core.hs_monitor import TF_CONFIGS
from core.kline_fetch import (
    LIVE_MAX_LENGTH,
    fetch_download_bars,
    fetch_live_bars,
    time_monotonic,
)
from core.kline_store import (
    DURATION_1D,
    DURATION_1M,
    DURATION_5M,
    DURATION_LABELS,
    count_bars,
    ensure_table,
    upsert_bars,
)
from core.session_rebuild import rebuild_symbol_session_klines
from utils.symbols import _product_code_from_symbol

# 新月持仓连续领先这么多次刷新才切（ingest 每开盘时段至少刷一次 ≈ 按交易日）
ROLL_CONFIRM_DAYS = 2

TABLE_NAME = "dominant_contracts"

_CREATE_SQL = f"""
CREATE TABLE IF NOT EXISTS `{TABLE_NAME}` (
  `product_key` VARCHAR(32) NOT NULL COMMENT '交易所.品种 如 SHFE.ag',
  `symbol` VARCHAR(64) NOT NULL COMMENT '当前主力月约',
  `challenger` VARCHAR(64) NULL COMMENT '持仓领先的挑战月',
  `lead_days` INT NOT NULL DEFAULT 0 COMMENT '挑战月连续领先次数',
  `open_interest` DOUBLE NULL,
  `volume` DOUBLE NULL,
  `updated_at` DATETIME NOT NULL,
  PRIMARY KEY (`product_key`),
  KEY `idx_symbol` (`symbol`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
COMMENT='持仓量主力月约（检测/采集用）'
"""

# 冷启动日历窗：日线约一年；日内覆盖 lookback（2h≈200 根需约 60～90 天）
INIT_CALENDAR_DAYS: dict[int, int] = {
    DURATION_1M: 90,
    DURATION_5M: 90,
    DURATION_1D: 400,
}
DEFAULT_INIT_DAYS = 90
INIT_DURATIONS = (DURATION_1M, DURATION_5M, DURATION_1D)

# 库内根数少于此值视为需要冷启动（略低于 lookback，避免差一点就反复全量拉）
MIN_BARS_READY: dict[int, int] = {
    DURATION_1M: 400,
    DURATION_5M: 300,
    DURATION_1D: 180,
}


@dataclass
class DominantRow:
    product_key: str
    symbol: str
    challenger: str | None
    lead_days: int
    open_interest: float | None
    volume: float | None
    updated_at: datetime


def ensure_dominant_table(conn: Connection) -> None:
    with conn.cursor() as cur:
        cur.execute(_CREATE_SQL)
    conn.commit()


def cont_to_product_key(cont_symbol: str) -> str:
    """KQ.m@SHFE.ag -> SHFE.ag"""
    raw = cont_symbol.strip()
    if raw.startswith("KQ.m@"):
        return raw.split("@", 1)[1]
    return raw


def product_key_from_symbol(symbol: str) -> str:
    """SHFE.ag2702 / KQ.m@SHFE.ag -> SHFE.ag"""
    raw = symbol.strip()
    if raw.startswith("KQ.m@"):
        return cont_to_product_key(raw)
    if "." not in raw:
        raise ValueError(f"无法解析品种键: {symbol}")
    exchange, _rest = raw.split(".", 1)
    product = _product_code_from_symbol(raw)
    if exchange.upper() == "CZCE":
        return f"CZCE.{product.upper()}"
    return f"{exchange.upper()}.{product.lower()}"


def list_commodity_product_keys(api: TqApi) -> list[str]:
    """商品 CONT 主连 → 品种键列表（已剔低流动性）。"""
    conts = drop_illiquid(
        [
            s
            for s in list_main_continuous(api)
            if any(s.startswith(f"KQ.m@{ex}.") for ex in COMMODITY_EXCHANGES)
        ]
    )
    return sorted({cont_to_product_key(c) for c in conts})


def _live_futures(api: TqApi, product_key: str) -> list[str]:
    exchange, product = product_key.split(".", 1)
    raw = list(
        api.query_quotes(
            ins_class="FUTURE",
            exchange_id=exchange,
            product_id=product,
            expired=False,
        )
    )
    out: list[str] = []
    for s in raw:
        code = s.split(".", 1)[-1]
        if any(ch.isdigit() for ch in code):
            out.append(s)
    return out


def _quote_oi_vol(api: TqApi, symbols: list[str]) -> dict[str, tuple[float, float]]:
    quotes = {s: api.get_quote(s) for s in symbols}
    try:
        api.wait_update(deadline=time_monotonic(api) + 0.5)
    except Exception:
        pass
    out: dict[str, tuple[float, float]] = {}
    for s, q in quotes.items():
        oi = getattr(q, "open_interest", None)
        vol = getattr(q, "volume", None)
        try:
            oi_f = float(oi) if oi is not None and oi == oi else 0.0
        except (TypeError, ValueError):
            oi_f = 0.0
        try:
            vol_f = float(vol) if vol is not None and vol == vol else 0.0
        except (TypeError, ValueError):
            vol_f = 0.0
        out[s] = (oi_f, vol_f)
    return out


def pick_max_oi(api: TqApi, product_key: str) -> tuple[str, float, float] | None:
    """未下市月约里持仓最大者；持仓相同看成交。"""
    futures = _live_futures(api, product_key)
    if not futures:
        return None
    stats = _quote_oi_vol(api, futures)
    best = max(
        futures,
        key=lambda s: (stats.get(s, (0.0, 0.0))[0], stats.get(s, (0.0, 0.0))[1]),
    )
    oi, vol = stats.get(best, (0.0, 0.0))
    return best, oi, vol


def _load_row(conn: Connection, product_key: str) -> DominantRow | None:
    sql = (
        f"SELECT product_key, symbol, challenger, lead_days, open_interest, volume, updated_at "
        f"FROM `{TABLE_NAME}` WHERE product_key=%s"
    )
    with conn.cursor() as cur:
        cur.execute(sql, (product_key,))
        r = cur.fetchone()
    if not r:
        return None
    return DominantRow(
        product_key=r[0],
        symbol=r[1],
        challenger=r[2],
        lead_days=int(r[3] or 0),
        open_interest=float(r[4]) if r[4] is not None else None,
        volume=float(r[5]) if r[5] is not None else None,
        updated_at=r[6],
    )


def _upsert_row(
    conn: Connection,
    *,
    product_key: str,
    symbol: str,
    challenger: str | None,
    lead_days: int,
    open_interest: float | None,
    volume: float | None,
) -> None:
    sql = f"""
    INSERT INTO `{TABLE_NAME}`
      (product_key, symbol, challenger, lead_days, open_interest, volume, updated_at)
    VALUES (%s,%s,%s,%s,%s,%s,%s)
    ON DUPLICATE KEY UPDATE
      symbol=VALUES(symbol),
      challenger=VALUES(challenger),
      lead_days=VALUES(lead_days),
      open_interest=VALUES(open_interest),
      volume=VALUES(volume),
      updated_at=VALUES(updated_at)
    """
    with conn.cursor() as cur:
        cur.execute(
            sql,
            (
                product_key,
                symbol,
                challenger,
                lead_days,
                open_interest,
                volume,
                datetime.now(),
            ),
        )
    conn.commit()


def apply_roll(
    prev: DominantRow | None,
    candidate: str,
    *,
    confirm_days: int = ROLL_CONFIRM_DAYS,
) -> tuple[str, str | None, int, bool]:
    """返回 (symbol, challenger, lead_days, switched)。

    首次建档直接采用 candidate；之后需连续 confirm_days 次刷新领先才切。
    """
    if prev is None:
        return candidate, None, 0, True
    if candidate == prev.symbol:
        return prev.symbol, None, 0, False
    if prev.challenger == candidate:
        lead = prev.lead_days + 1
    else:
        lead = 1
    if lead >= confirm_days:
        return candidate, None, 0, True
    return prev.symbol, candidate, lead, False


def needs_bootstrap(conn: Connection, symbol: str) -> bool:
    """1m/5m/1d 任一不足检测窗就认为要冷启动。"""
    for dur, floor in MIN_BARS_READY.items():
        if count_bars(conn, symbol=symbol, duration_sec=dur) < floor:
            return True
    return False


def refresh_dominants(
    api: TqApi,
    conn: Connection,
    *,
    confirm_days: int = ROLL_CONFIRM_DAYS,
    bootstrap_history: bool = True,
) -> list[str]:
    """刷新全部商品主力月约，返回当前应订阅的合约列表。"""
    ensure_table(conn)
    ensure_dominant_table(conn)
    keys = list_commodity_product_keys(api)
    symbols: list[str] = []
    to_bootstrap: list[str] = []
    for pk in keys:
        picked = pick_max_oi(api, pk)
        if picked is None:
            print(f"[dominant] {pk} 无未下市月约，跳过", flush=True)
            continue
        cand, oi, vol = picked
        prev = _load_row(conn, pk)
        symbol, challenger, lead, did_switch = apply_roll(
            prev, cand, confirm_days=confirm_days
        )
        store_oi = oi if symbol == cand else (prev.open_interest if prev else oi)
        store_vol = vol if symbol == cand else (prev.volume if prev else vol)
        _upsert_row(
            conn,
            product_key=pk,
            symbol=symbol,
            challenger=challenger,
            lead_days=lead,
            open_interest=store_oi,
            volume=store_vol,
        )
        symbols.append(symbol)
        if did_switch:
            old = prev.symbol if prev else "-"
            print(
                f"[dominant] 切换 {pk}: {old} → {symbol} (oi={oi:.0f} vol={vol:.0f})",
                flush=True,
            )
            # 仅换月/首建时冷启动；日常缺口用 scripts/refresh_dominants.py 休市补
            if bootstrap_history and needs_bootstrap(conn, symbol):
                to_bootstrap.append(symbol)
        elif challenger:
            print(
                f"[dominant] 挑战 {pk}: 现{symbol} ← {challenger} 领先{lead}/{confirm_days}日",
                flush=True,
            )
    for sym in to_bootstrap:
        try:
            bootstrap_dominant_history(api, conn, sym)
        except Exception as exc:  # noqa: BLE001
            print(f"[dominant] 冷启动失败 {sym}: {exc}", flush=True)
    return sorted(set(symbols))


def list_stored_dominants(conn: Connection) -> list[str]:
    ensure_dominant_table(conn)
    with conn.cursor() as cur:
        cur.execute(f"SELECT symbol FROM `{TABLE_NAME}` ORDER BY product_key")
        rows = cur.fetchall()
    return [r[0] for r in rows]


def list_commodity_dominants(
    api: TqApi | None = None,
    conn: Connection | None = None,
    *,
    refresh: bool = True,
    bootstrap_history: bool = True,
) -> list[str]:
    """检测/采集用的主力月约列表。"""
    own_conn = False
    own_api = False
    if conn is None:
        from core.kline_store import connect

        conn = connect()
        own_conn = True
    try:
        ensure_dominant_table(conn)
        if refresh and api is not None:
            return refresh_dominants(
                api, conn, bootstrap_history=bootstrap_history
            )
        stored = list_stored_dominants(conn)
        if stored:
            return stored
        if api is None:
            from tqsdk import TqAuth

            from config.settings import load_settings

            settings = load_settings()
            api = TqApi(auth=TqAuth(settings.auth.user, settings.auth.password))
            own_api = True
        return refresh_dominants(api, conn, bootstrap_history=bootstrap_history)
    finally:
        if own_api and api is not None:
            api.close()
        if own_conn and conn is not None:
            conn.close()


def init_days_for_duration(duration_sec: int) -> int:
    if duration_sec in INIT_CALENDAR_DAYS:
        return INIT_CALENDAR_DAYS[duration_sec]
    if duration_sec in TF_CONFIGS:
        return DEFAULT_INIT_DAYS
    return DEFAULT_INIT_DAYS


def _has_tq_dl(api: TqApi) -> bool:
    try:
        return bool(api._auth._has_feature("tq_dl"))
    except Exception:
        return False


def _fetch_init_bars(
    api: TqApi,
    symbol: str,
    duration_sec: int,
    days: int,
) -> list[dict[str, Any]]:
    end_dt = datetime.now()
    start_dt = end_dt - timedelta(days=days)
    if _has_tq_dl(api):
        try:
            return fetch_download_bars(
                api,
                symbol,
                duration_sec,
                start_dt.date(),
                end_dt.date(),
            )
        except Exception as exc:  # noqa: BLE001
            print(
                f"[dominant] download 失败改 live {symbol} "
                f"{DURATION_LABELS.get(duration_sec, duration_sec)}: {exc}",
                flush=True,
            )
    if duration_sec == DURATION_1M:
        length = min(LIVE_MAX_LENGTH, max(800, days * 400))
    elif duration_sec == DURATION_5M:
        length = min(LIVE_MAX_LENGTH, max(400, days * 80))
    elif duration_sec == DURATION_1D:
        length = min(LIVE_MAX_LENGTH, max(220, days))
    else:
        length = min(LIVE_MAX_LENGTH, 500)
    return fetch_live_bars(
        api,
        symbol,
        duration_sec,
        start_dt,
        end_dt,
        data_length=length,
    )


def bootstrap_dominant_history(
    api: TqApi,
    conn: Connection,
    symbol: str,
    *,
    durations: tuple[int, ...] = INIT_DURATIONS,
) -> dict[str, int]:
    """为新主力拉足够检测窗口的 1m/5m/1d，并重算会话周期。"""
    ensure_table(conn)
    stats: dict[str, int] = {}
    for dur in durations:
        days = init_days_for_duration(dur)
        try:
            bars = _fetch_init_bars(api, symbol, dur, days)
        except Exception as exc:  # noqa: BLE001
            print(
                f"[dominant] 拉历史失败 {symbol} {DURATION_LABELS.get(dur, dur)}: {exc}",
                flush=True,
            )
            stats[DURATION_LABELS.get(dur, str(dur))] = 0
            continue
        n = upsert_bars(conn, symbol, dur, bars) if bars else 0
        stats[DURATION_LABELS.get(dur, str(dur))] = n
        print(
            f"[dominant] 冷启动 {symbol} {DURATION_LABELS.get(dur, dur)} "
            f"days≈{days} bars={n}",
            flush=True,
        )
    # 会话周期从 1m 合成，覆盖日内 lookback
    lookback = init_days_for_duration(DURATION_1M)
    try:
        pivot = datetime.now()
        start = (pivot - timedelta(days=lookback)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        end = (pivot + timedelta(days=1)).replace(
            hour=23, minute=59, second=59, microsecond=0
        )
        sess = rebuild_symbol_session_klines(
            conn,
            symbol,
            start=start,
            end=end,
            execute=True,
            include_night=True,
        )
        stats["session"] = sum(
            int(sess.get(f"{k}_upserted") or 0)
            for k in ("30m", "1h", "2h", "3h", "4h")
        )
        print(
            f"[dominant] 会话合成 {symbol} upserted={stats['session']}",
            flush=True,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"[dominant] 会话合成失败 {symbol}: {exc}", flush=True)
        stats["session"] = 0
    return stats
