"""刷新持仓主力月约表，并按检测窗口冷启动缺历史的合约。

用法（建议休市跑全量冷启动）::

    python scripts/refresh_dominants.py
    python scripts/refresh_dominants.py --no-bootstrap   # 只刷表不拉 K
    python scripts/refresh_dominants.py --symbol SHFE.ag2702  # 单合约补历史
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tqsdk import TqApi, TqAuth

from config.settings import load_settings
from core.dominant import (
    bootstrap_dominant_history,
    ensure_dominant_table,
    list_stored_dominants,
    refresh_dominants,
)
from core.kline_store import connect, ensure_table


def main() -> None:
    parser = argparse.ArgumentParser(description="刷新持仓主力月约并冷启动")
    parser.add_argument(
        "--no-bootstrap",
        action="store_true",
        help="只更新 dominant_contracts，不拉 K 线",
    )
    parser.add_argument(
        "--bootstrap-missing",
        action="store_true",
        help="刷新后对所有缺历史的主力补冷启动（休市全量用）",
    )
    parser.add_argument(
        "--symbol",
        default=None,
        help="只冷启动指定月约（跳过全表刷新）",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="只打印当前库内主力月约后退出",
    )
    args = parser.parse_args()

    conn = connect()
    ensure_table(conn)
    ensure_dominant_table(conn)

    if args.list:
        syms = list_stored_dominants(conn)
        for s in syms:
            print(s)
        print(f"合计 {len(syms)}", flush=True)
        conn.close()
        return

    settings = load_settings()
    api = TqApi(auth=TqAuth(settings.auth.user, settings.auth.password))
    try:
        if args.symbol:
            bootstrap_dominant_history(api, conn, args.symbol.strip())
        else:
            from core.dominant import needs_bootstrap

            # 日常刷新：换月才冷启动；--bootstrap-missing 才扫缺口全补
            syms = refresh_dominants(
                api,
                conn,
                bootstrap_history=not args.no_bootstrap,
            )
            if args.bootstrap_missing and not args.no_bootstrap:
                for s in syms:
                    if needs_bootstrap(conn, s):
                        print(f"[bootstrap-missing] {s}", flush=True)
                        bootstrap_dominant_history(api, conn, s)
            print(f"[done] 主力月约 {len(syms)} 个", flush=True)
            for s in syms:
                print(f"  {s}")
    finally:
        api.close()
        conn.close()


if __name__ == "__main__":
    main()
