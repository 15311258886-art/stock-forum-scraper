@echo off
rem 股票论坛可视化查看器 —— 一键启动（自动激活 .venv，避免默认 python 缺 numpy/pandas）
cd /d "%~dp0.."
if not exist ".venv\Scripts\python.exe" (
    echo [错误] 未找到 .venv，请先执行: python -m venv .venv && .venv\Scripts\pip install -r requirements.lock.txt
    pause
    exit /b 1
)
call .venv\Scripts\activate.bat
python viewer/server.py
