#!/bin/sh
# 默认：只起 HTTP API。行情入库 / 监控告警由 compose 里独立服务跑。
set -u

mkdir -p /app/data /app/reports
exec python -u main_api.py --host 0.0.0.0 --port 8000
