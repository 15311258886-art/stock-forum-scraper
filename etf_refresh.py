# -*- coding: utf-8 -*-
"""ETF 缓存手动/外部定时刷新入口（盘后跑一次即可）。

调用 etf.refresh_caches(force=True)，依次刷：
  - Sina ETF 实时行情/跟踪指数（akshare，需 venv 运行）
  - 集思录投顾指标（溢价率/规模/费率/真实份额净申赎，需进哥 jisilu cookie）
打印两源状态汇总。

可由三种方式触发：
  - 查看器内部调度器（每日盘后自动，走 etf.maybe_refresh）——主路径
  - 本脚本手动双击 / 命令行（venv python 运行）
  - Windows 计划任务（交易时段后，venv python 调用本脚本）

用法：<venv>/Scripts/python.exe etf_refresh.py
退出码：两源均成功=0；任一失败=1（便于计划任务判定告警）。
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "viewer"))   # etf.py 在 viewer/ 子目录
from etf import refresh_caches                       # noqa: E402


def main() -> int:
    print("[etf_refresh] 开始刷新 ETF B 栏缓存（Sina + 集思录）…")
    r = refresh_caches(force=True)
    sina = r.get("sina") or {}
    jl = r.get("jisilu") or {}
    print("  Sina 行情 : ok=%s n=%s as_of=%s" % (sina.get("ok"), sina.get("n"), sina.get("as_of")))
    print("  集思录    : ok=%s n=%s as_of=%s%s" % (
        jl.get("ok"), jl.get("n"), jl.get("as_of"),
        ("  ⚠ " + jl["warn"]) if jl.get("warn") else ""))
    if sina.get("error"):
        print("  Sina 错误 :", sina["error"])
    if jl.get("error"):
        print("  集思录错误:", jl["error"])
    print("  完整成功  :", r.get("full_ok"))
    return 0 if r.get("full_ok") else 1


if __name__ == "__main__":
    sys.exit(main())
