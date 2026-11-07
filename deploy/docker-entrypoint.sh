#!/bin/sh
# 前台：HTTP API（手机 App 连这个）
# 后台：全商品主力多周期头肩盯盘，告警写入 /app/data/alerts.json
set -u

mkdir -p /app/data /app/reports

monitor_loop() {
  while true; do
    echo "[monitor] start $(date '+%F %T')"
    python -u scripts/monitor_all_mains_hs.py \
      --interval 60 \
      --durations 1m,5m,30m,1h,2h,1d \
      || echo "[monitor] exit $?"
    # 15:00 日盘结束后等到 20:50 再启动，避免休市时空转
    hour=$(date +%H)
    min=$(date +%M)
    hm=$((10#$hour * 60 + 10#$min))
    if [ "$hm" -ge 900 ] && [ "$hm" -lt 1250 ]; then
      wait_min=$((1250 - hm))
      echo "[monitor] 休市，${wait_min} 分钟后再启动"
      sleep $((wait_min * 60))
    else
      echo "[monitor] 30 秒后重启"
      sleep 30
    fi
  done
}

monitor_loop >> /app/reports/monitor.log 2>&1 &
exec python -u main_api.py --host 0.0.0.0 --port 8000
