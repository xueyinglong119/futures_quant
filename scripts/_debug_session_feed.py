import sys
sys.path.insert(0, ".")
from datetime import date
from tqsdk import TqApi, TqAuth, TqBacktest
from config.settings import load_settings
from core.session_klines import SessionKlineFeed

cfg = load_settings()
api = TqApi(
    auth=TqAuth(cfg.auth.user, cfg.auth.password),
    backtest=TqBacktest(start_dt=date(2026, 1, 1), end_dt=date(2026, 3, 1)),
)
feed = SessionKlineFeed(api, ["KQ.m@CZCE.MA"])
sym = "KQ.m@CZCE.MA"
updates = 0
new_bars = 0
try:
    while True:
        api.wait_update()
        if api.is_changing(feed.minute_klines[sym]):
            updates += 1
            if feed.is_changing(sym):
                new_bars += 1
                n = len(feed.get_klines(sym))
                if new_bars <= 5 or new_bars % 50 == 0:
                    print(f"session bars={n} at update {updates}")
except Exception as e:
    print("done", type(e).__name__)
    print("minute is_changing count", updates)
    print("new session bar events", new_bars)
    print("final session bars", len(feed.get_klines(sym)))
finally:
    api.close()
