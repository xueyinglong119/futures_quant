"""品种与交易时间工具"""

from __future__ import annotations

import re
from datetime import datetime, time

# 各品种交易时段（简化版，用于日内强制平仓判断）
DAY_SESSION = (time(9, 0), time(11, 30), time(13, 30), time(15, 0))
NIGHT_SESSION = (time(21, 0), time(23, 0))

# 品种代码（大小写不敏感）-> 中文名
PRODUCT_NAMES: dict[str, str] = {
    # SHFE
    "cu": "沪铜",
    "al": "沪铝",
    "zn": "沪锌",
    "pb": "沪铅",
    "ni": "沪镍",
    "sn": "沪锡",
    "au": "黄金",
    "ag": "白银",
    "rb": "螺纹钢",
    "hc": "热卷",
    "wr": "线材",
    "ss": "不锈钢",
    "ru": "橡胶",
    "bu": "沥青",
    "fu": "燃料油",
    "sp": "纸浆",
    "br": "丁二烯橡胶",
    "ao": "氧化铝",
    "ad": "铸造铝合金",
    "op": "胶版纸",
    # INE
    "sc": "原油",
    "lu": "低硫燃油",
    "nr": "20号胶",
    "bc": "国际铜",
    "ec": "集运欧线",
    # DCE
    "a": "豆一",
    "b": "豆二",
    "m": "豆粕",
    "y": "豆油",
    "p": "棕榈油",
    "c": "玉米",
    "cs": "淀粉",
    "i": "铁矿石",
    "j": "焦炭",
    "jm": "焦煤",
    "l": "塑料",
    "v": "PVC",
    "pp": "聚丙烯",
    "eg": "乙二醇",
    "eb": "苯乙烯",
    "pg": "LPG",
    "jd": "鸡蛋",
    "lh": "生猪",
    "rr": "粳米",
    "lg": "原木",
    "fb": "纤维板",
    "bb": "胶合板",
    "bz": "纯苯",
    # CZCE
    "TA": "PTA",
    "MA": "甲醇",
    "SA": "纯碱",
    "FG": "玻璃",
    "UR": "尿素",
    "SR": "白糖",
    "CF": "棉花",
    "CY": "棉纱",
    "RM": "菜粕",
    "OI": "菜油",
    "RS": "菜籽",
    "PK": "花生",
    "AP": "苹果",
    "CJ": "红枣",
    "SF": "硅铁",
    "SM": "锰硅",
    "PF": "短纤",
    "SH": "烧碱",
    "PX": "对二甲苯",
    "PL": "丙烯",
    "PR": "瓶片",
    "ZC": "动力煤",
    "WH": "强麦",
    "PM": "普麦",
    "RI": "早籼稻",
    "LR": "晚籼稻",
    "JR": "粳稻",
    # GFEX
    "si": "工业硅",
    "lc": "碳酸锂",
    "ps": "多晶硅",
    "pt": "铂",
    "pd": "钯",
    # CFFEX
    "IF": "沪深300",
    "IH": "上证50",
    "IC": "中证500",
    "IM": "中证1000",
    "T": "十年债",
    "TF": "五年债",
    "TS": "两年债",
    "TL": "三十年债",
}

SYMBOL_NAMES = {
    "KQ.m@SHFE.fu": "燃料油主连",
    "KQ.m@INE.lu": "低硫燃油主连",
    "KQ.m@CZCE.MA": "甲醇主连",
    "KQ.m@DCE.eg": "乙二醇主连",
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


def _product_code_from_symbol(symbol: str) -> str:
    raw = symbol.strip()
    if raw.startswith("KQ.m@"):
        raw = raw.split("@", 1)[1]
    if "." in raw:
        raw = raw.split(".", 1)[1]
    m = re.match(r"^([A-Za-z]+)", raw)
    return m.group(1) if m else raw


def get_display_name(symbol: str) -> str:
    """返回可读名称，如 线材主连 / 乙二醇2701。"""
    if symbol in SYMBOL_NAMES:
        return SYMBOL_NAMES[symbol]

    product = _product_code_from_symbol(symbol)
    name = (
        PRODUCT_NAMES.get(product)
        or PRODUCT_NAMES.get(product.lower())
        or PRODUCT_NAMES.get(product.upper())
        or product
    )

    if symbol.startswith("KQ.m@"):
        return f"{name}主连"

    m = re.search(r"(\d{3,4})$", symbol.strip())
    if m:
        return f"{name}{m.group(1)}"
    return name


def parse_hhmm(value: str) -> time:
    hour, minute = value.split(":")
    return time(int(hour), int(minute))


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
