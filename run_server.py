"""启动器（统一入口，2026-10-15 P1-1 整改）。

旧版强制 VIEWER_PORT=8821 后直接拉起 viewer.server，会产生孤儿后端
（端口争用、请求命中旧代码、日志里 8799/8765 交替）。现收敛到浏览器模式入口
launch_viewer.py（端口 8765，自带单实例端口守卫），不再强制 8821。

需要命令行启动查看器时，直接运行 launch_viewer.py 即可；本文件仅作兼容转发。
"""
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import launch_viewer  # noqa: E402

if __name__ == "__main__":
    launch_viewer.main()
