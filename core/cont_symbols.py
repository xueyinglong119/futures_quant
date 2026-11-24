"""全市场主力品种列表（CONT 主连枚举 + 持仓主力月约订阅）。"""

from __future__ import annotations

import re
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

_PRODUCT_RE = re.compile(r"^([A-Za-z]+)")


def illiquid_product_token(symbol: str) -> str:
    """用于 ILLIQUID 比对的产品码（保留交易所惯例大小写）。

    - ``KQ.m@SHFE.wr`` → ``wr``
    - ``SHFE.ag2702`` → ``ag``
    - ``CZCE.RS605`` → ``RS``
    """
    raw = symbol.strip()
    if raw.startswith("KQ.m@"):
        return raw.rsplit(".", 1)[-1]
    code = raw.split(".", 1)[-1] if "." in raw else raw
    m = _PRODUCT_RE.match(code)
    return m.group(1) if m else code


def drop_illiquid(symbols: Iterable[str]) -> list[str]:
    """剔掉低流动性品种，其余原样返回（顺序不动，调用方已排好序）。"""
    return [s for s in symbols if illiquid_product_token(s) not in ILLIQUID_PRODUCTS]


def list_main_continuous(api: TqApi) -> list[str]:
    """返回全部主力主连代码，如 KQ.m@DCE.eg（仅枚举/回填用，检测勿直接订阅）。"""
    raw = list(api.query_quotes(ins_class="CONT"))
    symbols = [
        s
        for s in raw
        if s.startswith("KQ.m@")
        and any(s.startswith(f"KQ.m@{ex}.") for ex in FUTURES_EXCHANGES)
    ]
    return sorted(symbols)


def list_commodity_mains(
    api: TqApi | None = None,
    conn=None,
    *,
    refresh: bool = True,
    bootstrap_history: bool = True,
) -> list[str]:
    """商品期货当前持仓主力月约（如 SHFE.ag2702），已剔低流动性。

    2026-11-23 起检测/采集改订真实月约，避免 KQ.m@ 换月拼价污染形态几何。
    品种覆盖面仍由 CONT ∩ 商品所 − ILLIQUID 决定；月约由 ``core.dominant`` 解析。
    """
    from core.dominant import list_commodity_dominants

    return list_commodity_dominants(
        api,
        conn,
        refresh=refresh,
        bootstrap_history=bootstrap_history,
    )


def list_commodity_cont_mains(api: TqApi | None = None) -> list[str]:
    """商品期货 KQ.m@ 主连列表（历史回填/对照用，不做形态检测订阅）。"""
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
