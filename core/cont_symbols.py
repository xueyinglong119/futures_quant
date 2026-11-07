"""全市场主力主连品种列表"""

from __future__ import annotations

from tqsdk import TqApi

# 国内期货交易所主连
FUTURES_EXCHANGES = ("CFFEX", "CZCE", "DCE", "GFEX", "INE", "SHFE")
# 商品期货（监控告警默认范围，不含中金所股指/国债）
COMMODITY_EXCHANGES = ("CZCE", "DCE", "GFEX", "INE", "SHFE")


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
    """商品期货主力主连（SHFE/DCE/CZCE/INE/GFEX）。"""
    if api is None:
        from tqsdk import TqAuth

        from config.settings import load_settings

        settings = load_settings()
        tmp = TqApi(auth=TqAuth(settings.auth.user, settings.auth.password))
        try:
            return [
                s
                for s in list_main_continuous(tmp)
                if any(s.startswith(f"KQ.m@{ex}.") for ex in COMMODITY_EXCHANGES)
            ]
        finally:
            tmp.close()
    return [
        s
        for s in list_main_continuous(api)
        if any(s.startswith(f"KQ.m@{ex}.") for ex in COMMODITY_EXCHANGES)
    ]
