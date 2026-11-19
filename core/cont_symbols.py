"""全市场主力主连品种列表"""

from __future__ import annotations

from collections.abc import Iterable

from tqsdk import TqApi

# 国内期货交易所主连
FUTURES_EXCHANGES = ("CFFEX", "CZCE", "DCE", "GFEX", "INE", "SHFE")
# 商品期货（监控告警默认范围，不含中金所股指/国债）
COMMODITY_EXCHANGES = ("CZCE", "DCE", "GFEX", "INE", "SHFE")

# 低流动性品种：近 90 日 1d 日均成交量 < 5000 手（同期全市场中位数 16.2 万手）。
# 其中粳稻/普麦/早籼稻/强麦/动力煤五个常年零成交，K 线还在走但没有对手盘，
# 画出来的形态纯是噪声；留着还每根白占一个检测键。要复核口径见 HS_RULES.md §1。
# 品种码大小写按交易所惯例：CZCE 大写，SHFE/DCE 小写。
ILLIQUID_PRODUCTS = frozenset(
    {
        "JR",  # 粳稻 0 手
        "PM",  # 普麦 0 手
        "RI",  # 早籼稻 0 手
        "WH",  # 强麦 0 手
        "ZC",  # 动力煤 0 手
        "RS",  # 菜籽 3727 手
        "wr",  # 线材 21 手
        "bb",  # 胶合板 320 手
        "fb",  # 纤维板 1280 手
        "rr",  # 粳米 2941 手
        "lg",  # 原木 3950 手
    }
)


def drop_illiquid(symbols: Iterable[str]) -> list[str]:
    """剔掉低流动性品种，其余原样返回（顺序不动，调用方已排好序）。"""
    return [s for s in symbols if s.rsplit(".", 1)[-1] not in ILLIQUID_PRODUCTS]


def list_main_continuous(api: TqApi) -> list[str]:
    """返回全部主力主连代码，如 KQ.m@DCE.eg。"""
    raw = list(api.query_quotes(ins_class="CONT"))
    symbols = [
        s
        for s in raw
        if s.startswith("KQ.m@")
        and any(s.startswith(f"KQ.m@{ex}.") for ex in FUTURES_EXCHANGES)
    ]
    return sorted(symbols)


def list_commodity_mains(api: TqApi | None = None) -> list[str]:
    """商品期货主力主连（SHFE/DCE/CZCE/INE/GFEX），已剔掉低流动性品种。"""
    if api is None:
        from tqsdk import TqAuth

        from config.settings import load_settings

        settings = load_settings()
        tmp = TqApi(auth=TqAuth(settings.auth.user, settings.auth.password))
        try:
            return _commodity_mains(list_main_continuous(tmp))
        finally:
            tmp.close()
    return _commodity_mains(list_main_continuous(api))


def _commodity_mains(symbols: list[str]) -> list[str]:
    return drop_illiquid(
        s
        for s in symbols
        if any(s.startswith(f"KQ.m@{ex}.") for ex in COMMODITY_EXCHANGES)
    )
