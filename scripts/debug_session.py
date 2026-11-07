"""快速诊断 session K 线合成与策略信号（短区间）"""

import sys
from datetime import date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from config.settings import load_settings
from core.session_klines import SessionKlineFeed, quote_datetime_to_ns
from strategies.factory import create_strategy
from tqsdk import TqApi, TqAuth, TqBacktest
from tqsdk.exceptions import BacktestFinished


def main() -> None:
    settings = load_settings()
    sym = "KQ.m@CZCE.MA"
    api = TqApi(
        auth=TqAuth(settings.auth.user, settings.auth.password),
        backtest=TqBacktest(start_dt=date(2026, 1, 1), end_dt=date(2026, 3, 1)),
    )
    feed = SessionKlineFeed(api, [sym])
    strategy = create_strategy(settings)

    sync_count = 0
    max_visible = 0
    signals = 0
    try:
        while api.wait_update():
            quote = api.get_quote(sym)
            ns = quote_datetime_to_ns(quote.datetime)
            for k in feed.iter_new_bars(sym, ns):
                sync_count += 1
                n = len(k)
                if n > max_visible:
                    max_visible = n
                if n >= 73:
                    sig = strategy.on_bar(k, max_volume=8, symbol=sym)
                    if sig:
                        signals += 1
                        t = datetime.fromtimestamp(k.iloc[-1]["datetime"] / 1e9)
                        print(f"信号 #{signals} @ {t}: {sig.action} {sig.reason[:60]}")
    except BacktestFinished:
        pass
    finally:
        api.close()

    print(f"sync触发={sync_count} 最大可见bar={max_visible} "
          f"合成总数={len(feed._session_rows[sym])} 分钟线={feed._minute_len[sym]} 信号={signals}")


if __name__ == "__main__":
    main()
