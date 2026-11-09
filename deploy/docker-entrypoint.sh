#!/bin/sh
# 前台：HTTP API（手机 App 连这个）
# 后台：开盘订阅行情，K 线收盘入库，再做头肩告警
set -u

mkdir -p /app/data /app/reports

pipeline_loop() {
  while true; do
    echo "[pipeline] start $(date '+%F %T')"
    python -u scripts/run_realtime_pipeline.py \
      || echo "[pipeline] exit $?"
    echo "[pipeline] 15 秒后重启"
    sleep 15
  done
}

pipeline_loop >> /app/reports/realtime.log 2>&1 &
exec python -u main_api.py --host 0.0.0.0 --port 8000
