"""品种与交易时间工具"""

from __future__ import annotations

import re
from datetime import datetime, time

# 各品种交易时段（简化版，用于日内强制平仓判断）
DAY_SESSION = (time(9, 0), time(11, 30), time(13, 30), time(15, 0))
NIGHT_SESSION = (time(21, 0), time(23, 0))

SYMBOL_NAMES = {
    "KQ.m@SHFE.fu": "燃料油FU",
    "KQ.m@INE.lu": "低硫燃油LU",
    "KQ.m@CZCE.MA": "甲醇MA",
    "KQ.m@DCE.eg": "乙二醇EG",
}

# 裸合约代码 -> 交易所（用于 normalize_contract_symbol）
PRODUCT_EXCHANGE: dict[str, str] = {
    "MA": "CZCE",
    "FU": "SHFE",
    "LU": "INE",
    "EG": "DCE",
    "fu": "SHFE",
    "lu": "INE",
    "eg": "DCE",
}


def parse_hhmm(value: str) -> time:
    hour, minute = value.split(":")
    return time(int(hour), int(minute))


def get_display_name(symbol: str) -> str:
    return SYMBOL_NAMES.get(symbol, symbol)


def normalize_contract_symbol(symbol: str) -> str:
    """
    将用户输入的合约号规范为 TqSdk 格式。

    支持示例:
      MA609 / ma609 -> CZCE.MA609
      fu2605        -> SHFE.fu2605
      CZCE.MA609    -> CZCE.MA609
      KQ.m@CZCE.MA  -> KQ.m@CZCE.MA（主连原样返回）
    """
    raw = symbol.strip()
    if not raw:
        raise ValueError("合约号不能为空")

    if raw.startswith("KQ."):
        return raw

    if "." in raw:
        exchange, code = raw.split(".", 1)
        exchange = exchange.upper()
        if exchange == "CZCE":
            return f"CZCE.{code.upper()}"
        if exchange in ("SHFE", "INE", "DCE", "GFEX", "CFFEX"):
            return f"{exchange}.{code.lower()}"
        return raw

    m = re.fullmatch(r"([A-Za-z]+)(\d+)", raw)
    if not m:
        raise ValueError(
            f"无法识别合约号: {symbol!r}，请使用如 MA609、SHFE.fu2605、KQ.m@CZCE.MA"
        )

    product, month = m.group(1), m.group(2)
    exchange = PRODUCT_EXCHANGE.get(product) or PRODUCT_EXCHANGE.get(product.upper())
    if exchange == "CZCE":
        return f"CZCE.{product.upper()}{month}"
    if exchange:
        return f"{exchange}.{product.lower()}{month}"

    raise ValueError(
        f"未知品种 {product!r}，请在 utils/symbols.PRODUCT_EXCHANGE 中补充映射，"
        f"或使用带交易所前缀的完整合约号"
    )


def is_in_session(dt: datetime, start: time, end: time) -> bool:
    t = dt.time()
    return start <= t <= end


def should_force_close(
    dt: datetime,
    day_close: time,
    night_close: time,
) -> bool:
    """判断当前 K 线是否触发日内强制平仓"""
    if dt.weekday() >= 5:
        return False

    t = dt.time()
    # 日盘结束前
    if time(9, 0) <= t <= time(15, 0) and t >= day_close:
        return True
    # 夜盘结束前
    if time(21, 0) <= t <= time(23, 0) and t >= night_close:
        return True
    return False
