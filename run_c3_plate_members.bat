@echo off
REM ============================================================
REM  C3 一键闭环：抓取东方财富官方板块成分股（17 个漏斗板块）
REM  用途：填 viewer/app.db 的 plate_members 表，使「方法共振漏斗」
REM        的 mode=official 走权威全量成分（不再回退行业映射）。
REM  前置：本机已安装 venv（项目根 .venv），且能访问东方财富网站。
REM  用法：把本文件放在项目根目录，双击即可。
REM ============================================================
cd /d "%~dp0"

echo.
echo [C3] 开始抓取东方财富官方板块成分股（17 个漏斗板块）...
echo [C3] 需要联网；若长时间无输出请检查网络/防火墙。
echo.

.venv\Scripts\python.exe tools\fetch_plate_members.py

echo.
if errorlevel 1 (
    echo [C3] 抓取失败（错误码 %errorlevel%）。请确认：1)本机已联网 2)项目根 .venv 存在 3)东方财富可访问。
) else (
    echo [C3] 抓取完成，plate_members 表已更新。
)
echo.
echo 按任意键关闭窗口...
pause >nul
