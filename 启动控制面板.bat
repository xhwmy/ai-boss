@echo off
chcp 65001 >nul
rem ========================================
rem   若使用 conda 虚拟环境，请将下面命令中的
rem   python 替换为你的 python.exe 绝对路径，
rem   例如：
rem     "D:\anaconda3\envs\my_env\python.exe" "%~dp0app.py"
rem ========================================
echo ========================================
echo   BOSS 直聘 AI 自动投递 - 控制面板
echo ========================================
echo.
echo 正在启动服务器...
echo 请稍候，浏览器将自动打开
echo.
echo 如果浏览器未自动打开，请手动访问：
echo   http://localhost:5000
echo.
echo 按 Ctrl+C 停止服务器
echo ========================================
echo.

start http://localhost:5000
python "%~dp0app.py"

pause
