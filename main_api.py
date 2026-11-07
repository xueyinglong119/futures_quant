"""HTTP API 入口

启动:
  python main_api.py
  python main_api.py --host 0.0.0.0 --port 8000

接口:
  GET /                     燃油昨日 K 线页面
  GET /web/fu_kline.html    同上
  GET /health
  GET /api/klines?symbol=MA609&date=2026-06-15&minutes=30
  GET /api/alerts           形态告警列表（供手机 App）
  POST /api/alerts          创建告警并可推送
  POST /api/devices/register 注册 App 推送 Token
  文档: http://127.0.0.1:8000/docs

手机 App:
  见 mobile/README.md（Expo 可打 APK 安装）
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import uvicorn

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="启动期货量化 HTTP API")
    parser.add_argument("--host", default="127.0.0.1", help="监听地址")
    parser.add_argument("--port", type=int, default=8000, help="监听端口")
    parser.add_argument("--reload", action="store_true", help="开发模式热重载")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    uvicorn.run(
        "api.app:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
    )


if __name__ == "__main__":
    main()
