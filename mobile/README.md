# 期货形态告警 App

可安装到安卓手机的告警客户端：监控到头肩顶/底后，推送通知并在列表展示。

## 功能

- 填写电脑/服务器 API 地址后连接
- 每 15 秒轮询告警（后台切回前台也会刷新）
- 新告警触发本机通知
- 注册 Expo Push Token 后，服务端可远程推送

## 一键安装 APK（推荐）

1. 安装 Node.js 18+
2. 注册 [Expo](https://expo.dev) 账号
3. 在本目录执行：

```bash
cd mobile
npm install
npx eas-cli login
npx eas-cli init
# 把生成的 projectId 写入 app.json -> expo.extra.eas.projectId
npm run build:apk
```

构建完成后在 Expo 网页下载 `.apk`，传到手机安装即可。

## 本地开发预览

```bash
cd mobile
npm install
npx expo start
```

手机装 **Expo Go**，扫码预览（真机通知/推送以正式 APK 为准）。

## 服务器配合

1. 电脑启动 API（需让手机能访问，监听 `0.0.0.0`）：

```bash
python main_api.py --host 0.0.0.0 --port 8000
```

2. 手机 App 里填：`http://你的电脑局域网IP:8000`  
   （同一 WiFi；阿里云则填公网 IP，并放行 8000 端口）

3. 跑监控脚本（命中形态会写入告警并尝试推送）：

```bash
python scripts/monitor_eg2701_hs_1m.py
```

## 接口

- `GET /api/alerts` 告警列表
- `POST /api/alerts` 创建告警（可选 push）
- `POST /api/devices/register` 注册设备推送 Token
