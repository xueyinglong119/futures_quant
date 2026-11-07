# 阿里云部署说明

本包为 `futures_quant` 量化 HTTP 服务（FastAPI + 燃油 K 线页面）。

## 部署前准备

1. 阿里云 ECS（建议 Ubuntu 22.04 / CentOS 7+，2 核 4G 起）
2. 安全组放行 **TCP 8000**（或前面接 Nginx 后只开 80/443）
3. RDS MySQL 白名单加入 ECS 内网/公网 IP
4. 准备快期账号与 MySQL 连接信息

## 方式 A：systemd（推荐，简单）

```bash
# 上传并解压
scp futures_quant_aliyun_*.zip root@你的ECS公网IP:/tmp/
ssh root@你的ECS公网IP
cd /tmp && unzip -o futures_quant_aliyun_*.zip -d futures_quant
cd futures_quant

# 配置环境变量（不要提交到 git）
cp .env.example .env
vi .env   # 填写 TQ_USER / TQ_PASS / MYSQL_*

# 安装依赖并注册服务
chmod +x deploy/install.sh
sudo bash deploy/install.sh
```

访问：`http://公网IP:8000/`

常用命令：

```bash
systemctl status futures-quant
systemctl restart futures-quant
journalctl -u futures-quant -f
```

## 方式 B：Docker Compose（服务器已装 Docker 时用这个）

本机不要安装 Docker Desktop。公司网络（CorpLink / aTrust）依赖虚拟网卡，装过 Docker Desktop 会导致连不上公司网。镜像在阿里云上构建。

```bash
# 把 dist/futures_quant_aliyun_*.zip 上传到 ECS 后：
cd /tmp && unzip -o futures_quant_aliyun_*.zip -d futures_quant
cd futures_quant
cp .env.example .env
vi .env   # 填写 TQ_USER / TQ_PASS / MYSQL_*

chmod +x deploy/install_docker.sh deploy/docker-entrypoint.sh
sudo bash deploy/install_docker.sh
```

容器里同时跑两件事：8000 端口的 HTTP 接口，以及乙二醇头肩盯盘。告警写在数据卷 `data/alerts.json`，手机 App 把服务器地址改成 `http://ECS公网IP:8000`。

若 `docker compose build` 拉 `python:3.11-slim-bookworm` 超时，在 `/etc/docker/daemon.json` 配好阿里云镜像加速器后执行 `systemctl restart docker`，再重新 build。

安全组入方向放行 **TCP 8000**。

## .env 必填项

```env
TQ_USER=快期账号
TQ_PASS=快期密码
MYSQL_HOST=rm-xxxx.mysql.rds.aliyuncs.com
MYSQL_PORT=3306
MYSQL_USER=...
MYSQL_PASSWORD=...
MYSQL_DATABASE=future_ai
```

ECS 与 RDS 同地域时，优先填 **RDS 内网地址**，更稳更快。

## 接口一览

| 地址 | 说明 |
|------|------|
| `/` | 燃油昨日 K 线页面 |
| `/web/fu_kline.html` | 同上 |
| `/health` | 健康检查 |
| `/api/klines?symbol=SHFE.fu2701&date=2026-10-29` | K 线 JSON |
| `/docs` | Swagger 文档 |

## 注意

- 包内 **不含** `.env`，上线后必须自行配置
- TqSdk 需能访问外网（行情/回测）
- 生产建议用 Nginx 反代到 `127.0.0.1:8000`，并配置 HTTPS
