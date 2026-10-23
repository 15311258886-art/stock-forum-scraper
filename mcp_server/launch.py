"""mcp_server/launch.py — MCP 服务器启动入口。

用法：
    python mcp_server/launch.py --db <path-to-viewer/app.db>

MCP 客户端（WorkBuddy / Cursor 等）会把它作为子进程拉起，
通过 stdio 收发 JSON-RPC 消息。

注意（2026-10-23）：MCP 客户端有时用带控制台的 python.exe 拉起本脚本，
屏幕上就会闪出一个黑框（窗口标题正是 python.exe 的完整路径）。这里在启动
时主动把自己的控制台窗口隐藏掉，保证上游用 python.exe 还是 pythonw.exe
都不会再弹窗。调试时需要保留窗口，设环境变量 STOCK_MCP_KEEP_CONSOLE=1。
"""
import argparse
import os
import sys


def _hide_console() -> None:
    """把自己进程的控制台窗口藏起来（Windows 专用，失败静默忽略）。

    只作用于本进程自己的 console window，不杀进程、不影响 stdio 通信。
    """
    if os.name != "nt":
        return
    if os.environ.get("STOCK_MCP_KEEP_CONSOLE") == "1":
        return
    try:
        import ctypes

        k32 = ctypes.windll.kernel32
        u32 = ctypes.windll.user32
        hwnd = k32.GetConsoleWindow()
        if not hwnd:
            return  # 已经无控制台（pythonw / DETACHED），无需处理
        u32.ShowWindow(hwnd, 0)  # SW_HIDE
        try:
            k32.SetConsoleTitleW("stock-forum-mcp (hidden)")
        except Exception:  # noqa: BLE001
            pass
    except Exception:  # noqa: BLE001
        pass


_hide_console()

# 让 mcp_server 包可被 import
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from mcp_server import server


def main():
    ap = argparse.ArgumentParser(description="Stock Forum MCP Server (read-only)")
    ap.add_argument("--db", default=None,
                    help="path to viewer/app.db (default: auto-detect)")
    args = ap.parse_args()
    server.run(args.db)


if __name__ == "__main__":
    main()
