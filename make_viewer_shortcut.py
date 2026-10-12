"""在桌面创建「股票论坛可视化查看器」快捷方式（双击即启动原生桌面窗口）。"""
import os
import win32com.client

desktop = os.path.join(os.path.expanduser("~"), "Desktop")
lnk_path = os.path.join(desktop, "股票论坛可视化查看器.lnk")
venv_py = r"F:\AI Agent部署知识库\2026-09-22-11-49-42\stock-forum-scraper\.venv\Scripts\python.exe"
launch = r"F:\AI Agent部署知识库\2026-09-22-11-49-42\stock-forum-scraper\viewer\desktop_app.py"
workdir = r"F:\AI Agent部署知识库\2026-09-22-11-49-42\stock-forum-scraper"

shell = win32com.client.Dispatch("WScript.Shell")
sc = shell.CreateShortcut(lnk_path)
sc.TargetPath = venv_py
sc.Arguments = f'"{launch}"'
sc.WorkingDirectory = workdir
sc.Description = "股票论坛实盘/资讯可视化查看器（双击启动，原生桌面窗口）"
sc.Save()
print("LNK_WRITTEN", lnk_path, os.path.exists(lnk_path))
