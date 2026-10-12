"""一次性启动器：强制 VIEWER_PORT=8821 后运行 viewer/server.py（当前代码，含 B4-B8 路由）。
绕过 PowerShell 子进程 env 不继承的问题：在 import server 之前就 set 好环境变量。"""
import os
import runpy
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
os.environ["VIEWER_PORT"] = "8821"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
runpy.run_path(str(ROOT / "viewer" / "server.py"), run_name="__main__")
