#!/usr/bin/env bash
# 阿里云 ECS 一键安装（非 Docker）
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/futures_quant}"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PKG_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

echo "==> 安装目录: $APP_DIR"
mkdir -p "$APP_DIR"
rsync -a --delete \
  --exclude '.venv' \
  --exclude '__pycache__' \
  --exclude 'dist' \
  --exclude '.git' \
  "$PKG_ROOT/" "$APP_DIR/"

cd "$APP_DIR"

if [[ ! -f .env ]]; then
  cp .env.example .env
  echo "已生成 .env，请编辑填写 TQ_USER / TQ_PASS / MYSQL_* 后重新执行本脚本，或手动 systemctl restart"
fi

python3 -m venv .venv
. .venv/bin/activate
pip install -U pip -i https://mirrors.aliyun.com/pypi/simple/
pip install -r requirements.txt -i https://mirrors.aliyun.com/pypi/simple/

mkdir -p reports data
cp -f deploy/futures-quant.service /etc/systemd/system/futures-quant.service
systemctl daemon-reload
systemctl enable futures-quant
systemctl restart futures-quant
systemctl --no-pager status futures-quant || true

echo
echo "完成。访问: http://服务器公网IP:8000/"
echo "健康检查: curl http://127.0.0.1:8000/health"
echo "日志: journalctl -u futures-quant -f"
