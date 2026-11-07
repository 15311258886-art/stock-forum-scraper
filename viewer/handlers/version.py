# -*- coding: utf-8 -*-
"""version 域 API 处理函数（从 viewer/server.py 物理抽取，2026-11-07 拆分重构）。

抽取规则：函数体逻辑零改动；依赖的 server 模块级符号经 `from viewer.server import (...)`
重定向进本地命名空间；标准库本地导入。server.py 末尾 re-export 保持命名空间兼容，
routes.py 与既有测试（S.api_*）无需改动。
"""
from __future__ import annotations

import os, re, time
from viewer.server import (  # noqa: E402,F401
    STATIC_DIR,
    _BUILD_ID,
    _STARTED_AT,
)

def api_version() -> dict:
    """前端版本号（唯一真源 = index.html 的 <meta name="app-version" content="...">）。

    2026-10-02 加：王总反馈「集合竞价功能做了但桌面窗口里看不到」。根因是**已在运行的页面不会重新加载自己**——
    页面里的 JS 只定时拉 /api/dashboard 刷数据，不会重新拉 index.html / app.js，所以昨晚开的窗口
    永远跑着昨晚那版代码，接口把新数据给过去它也渲染不出来。
    对策：前端每 60s 比一次版本，不一致就在顶部弹提示条让用户点一下刷新（比让他猜「要按 F5」可靠）。
    """
    # P3-19：index.html 的 app-version 用 st_mtime 缓存，避免 60s 轮询每次 read_text+regex。
    # 文件变动（F5 部署新前端）后 mtime 变化才重新解析。
    try:
        p = STATIC_DIR / "index.html"
        mt = p.stat().st_mtime
        if _api_ver_cache["mtime"] != mt:
            html = p.read_text(encoding="utf-8")
            m = re.search(r'<meta\s+name=["\']app-version["\']\s+content=["\']([^"\']+)["\']\s*/?>', html)
            _api_ver_cache["mtime"] = mt
            _api_ver_cache["version"] = m.group(1) if m else None
    except (OSError, UnicodeDecodeError):
        pass
    # 2026-10-05（优化报告 建议1）：带上服务进程 pid。桌面端起新实例前可以用它
    # 判断「是不是撞上已经在跑的那一台」，避免再开一个窗口出来（1 server + N desktop）。
    return {"version": _api_ver_cache["version"], "served_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "pid": os.getpid(),
            # 2026-10-16：进程启动时定格的代码指纹 + 启动时刻。供 desktop_app 判断
            # 「8765 上跑的是不是当前磁盘上的代码」——是旧代码就必须接管重启，
            # 否则新加的路由在这个进程里根本不存在（表现为 404，界面看着还是新版）。
            "build_id": _BUILD_ID,
            "started_at": _STARTED_AT,
            # 2026-10-15：当前正在跑的调度任务名。用于现场诊断"整站被冻住"——
            # 若某个纯 Python 任务长时间占住 GIL，这个字段会一直停在该任务名上。
            "sched_task": globals().get("_SCHED_CURRENT", "-")}
