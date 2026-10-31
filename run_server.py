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
    # 必须把 main() 的退出码透传出去：launch_viewer 用 2/3/4/5 区分
    # 「端口被别的程序占着 / 健康校验超时 / 拉子进程失败 / 版本不符」，
    # 直接裸调 main() 会让所有这些失败在调用方看来都是"成功"。
    sys.exit(launch_viewer.main())
