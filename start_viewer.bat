@echo off
chcp 65001 >nul
REM ============================================================
REM  股票论坛查看器 / 智能选股 后端启动器
REM  双击本文件即可启动，访问 http://127.0.0.1:8765
REM  关闭本窗口 = 停止服务；要重新启动再双击一次即可。
REM  注：本启动器以普通 Windows 进程运行，不依赖 AI 会话，
REM      关掉 WorkBuddy 也照样常驻，比在 AI 里拉起更稳。
REM  2026-10-22 工程化说明（P3-16）：经本启动器拉起的服务默认启用随机写接口令牌（P1-45），
REM      令牌落盘于 config/viewer_token.txt；设置面板读写经 /api/config/* 端点（P1-50 / P1-51）。
REM ============================================================
cd /d "F:\AI Agent部署知识库\2026-09-22-11-49-42\stock-forum-scraper"
if not exist ".venv\Scripts\python.exe" (
  echo [错误] 未找到虚拟环境 .venv\Scripts\python.exe
  pause
  exit /b 1
)
echo ============================================================
echo  智能选股后端启动中...（端口 8765）
echo  浏览器打开：http://127.0.0.1:8765
echo  关闭此窗口即停止服务
echo ============================================================
".venv\Scripts\python.exe" viewer/server.py
pause
