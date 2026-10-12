"""在用户桌面创建指向 Excel 累积存档的 .lnk 快捷方式。"""
import os
import win32com.client

desktop = os.path.join(os.path.expanduser("~"), "Desktop")
lnk_path = os.path.join(desktop, "股票论坛抓取历史存档.lnk")
target = r"F:\AI Agent部署知识库\2026-09-22-11-49-42\stock-forum-scraper\archive\股票论坛抓取历史存档.xlsx"
workdir = r"F:\AI Agent部署知识库\2026-09-22-11-49-42\stock-forum-scraper"

shell = win32com.client.Dispatch("WScript.Shell")
sc = shell.CreateShortcut(lnk_path)
sc.TargetPath = target
sc.WorkingDirectory = workdir
sc.Description = "股票论坛实盘交易与资讯每日累积存档 - 双击查看"
sc.Save()

print("LNK_WRITTEN", lnk_path, os.path.exists(lnk_path))
