"""通过 TqSdk 回测拉取指定区间的国内 30 分钟 session K 线"""

from __future__ import annotations

from datetime import date

from tqsdk import TqApi, TqAuth, TqBacktest
from tqsdk.exceptions import BacktestFinished

from config.settings import Settings, load_settings
from core.session_klines import SessionKlineFeed, quote_datetime_to_ns


def fetch_session_bars(
    symbol: str,
    start_dt: date,
    end_dt: date,
    settings: Settings | None = None,
) -> list[dict]:
    cfg = settings or load_settings()
    api = TqApi(
        auth=TqAuth(cfg.auth.user, cfg.auth.password),
        backtest=TqBacktest(start_dt=start_dt, end_dt=end_dt),
    )
    feed = SessionKlineFeed(api, [symbol])
    try:
        while api.wait_update():
            quote = api.get_quote(symbol)
            ns = quote_datetime_to_ns(quote.datetime)
            for _ in feed.iter_new_bars(symbol, ns):
                pass
    except BacktestFinished:
        pass
    finally:
        feed.rebuild_all_from_serial(symbol)
        api.close()

    return list(feed._session_rows.get(symbol, []))
