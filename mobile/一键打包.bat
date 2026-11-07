@echo off
chcp 65001 >nul
cd /d "%~dp0"

echo ===== 期货形态告警 App 打包 =====
echo.

echo [1/3] 检查登录状态...
call npm run eas:whoami
if errorlevel 1 (
  echo.
  echo 尚未登录。请在接下来的提示中登录 Expo（免费注册: https://expo.dev ）
  call npm run eas:login
  if errorlevel 1 (
    echo 登录失败
    pause
    exit /b 1
  )
)

echo.
echo [2/3] 初始化 EAS 项目...
call npm run eas:init
if errorlevel 1 (
  echo eas init 失败，若项目已存在可忽略后继续
)

echo.
echo [3/3] 云端打包 APK（大约 10~20 分钟）...
call npx eas build -p android --profile preview
echo.
echo 完成后请复制终端里的 APK 下载链接，用手机下载安装。
pause
