"""弹窗修复的自检脚本（不打开任何窗口，纯逻辑验证）。

验证三件事：
1. 单实例锁：同名互斥体第二次获取必须失败（True → False）。
2. 端口探测：8765（当前有服务）应为 True，未占用端口应为 False。
3. 静态检查：由外层命令另行执行（node --check / py_compile）。
"""
import sys

sys.path.insert(0, r"F:\AI Agent部署知识库\2026-09-22-11-49-42\stock-forum-scraper")

from viewer.desktop_app import _acquire_single_instance, _port_in_use  # noqa: E402

first = _acquire_single_instance()
second = _acquire_single_instance()
print("mutex_first      =", first, "(期望 True)")
print("mutex_second     =", second, "(期望 False)")
print("port_8765_in_use =", _port_in_use("127.0.0.1", 8765), "(期望 True，服务在跑)")
print("port_8799_in_use =", _port_in_use("127.0.0.1", 8799), "(期望 False，空闲端口)")

ok = (first is True) and (second is False) and (_port_in_use("127.0.0.1", 8765) is True) \
     and (_port_in_use("127.0.0.1", 8799) is False)
print("RESULT:", "PASS" if ok else "FAIL")
