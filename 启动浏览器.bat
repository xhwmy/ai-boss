@echo off
chcp 65001 >nul
title BOSS直聘 - 启动 Edge 浏览器
echo ========================================
echo   启动 Edge 浏览器
echo   KIMI Webbridge 将自动加载
echo ========================================
echo.
echo   ⚠️ 请在此浏览器中：
echo     1. 打开 https://www.zhipin.com
echo     2. 扫码登录 BOSS 直聘
echo     3. 保持浏览器一直打开
echo.
echo 按任意键启动浏览器...
pause >nul

start "" "C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"

echo.
echo ✅ 浏览器已启动！
echo.
echo 请完成登录，然后运行「启动控制面板.bat」
echo.
pause
