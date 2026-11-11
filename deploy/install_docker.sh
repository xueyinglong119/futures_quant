#!/usr/bin/env bash
# 阿里云 ECS Docker 部署
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/futures_quant}"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PKG_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

echo "==> 安装目录: $APP_DIR"
mkdir -p "$APP_DIR"
# 保留服务器上已填写的 .env，不要被同步删掉
if command -v rsync >/dev/null 2>&1; then
  rsync -a --delete \
    --exclude '.venv' \
    --exclude '__pycache__' \
    --exclude 'dist' \
    --exclude '.git' \
    --exclude '.env' \
    "$PKG_ROOT/" "$APP_DIR/"
else
  tar -C "$PKG_ROOT" \
    --exclude .venv \
    --exclude __pycache__ \
    --exclude dist \
    --exclude .git \
    --exclude .env \
    -cf - . | tar -C "$APP_DIR" -xf -
fi

cd "$APP_DIR"

if [[ ! -f .env ]]; then
  cp .env.example .env
  echo "请先编辑 $APP_DIR/.env 填写账号，再执行: cd $APP_DIR && docker compose up -d --build"
  exit 1
fi

docker compose up -d --build
docker compose ps
echo
echo "完成。访问: http://服务器公网IP:8000/"
echo "服务: futures-api / futures-ingest / futures-monitor"
echo "日志: docker compose logs -f"
echo "      tail -f reports/ingest.log reports/monitor.log"
