# -*- coding: utf-8 -*-
"""open 域 API 处理函数（从 viewer/server.py 物理抽取，2026-11-07 拆分重构）。

抽取规则：函数体逻辑零改动；依赖的 server 模块级符号经 `from viewer.server import (...)`
重定向进本地命名空间；标准库本地导入。server.py 末尾 re-export 保持命名空间兼容，
routes.py 与既有测试（S.api_*）无需改动。
"""
from __future__ import annotations

import os
from viewer.server import (  # noqa: E402,F401
    ROOT,
)

def api_open_dir(dirname: str | None = None) -> dict:
    """POST /api/open_dir：打开日志/备份目录（P3-17）。"""
    target = ROOT / "viewer" / "logs"
    if dirname in ("backup", "backups"):
        target = ROOT / "viewer" / "backups"
    elif dirname in ("data",):
        target = ROOT / "data"
    try:
        target.mkdir(parents=True, exist_ok=True)
        if os.name == "nt":
            os.startfile(str(target))  # type: ignore[attr-defined]
        else:
            import subprocess as _sp
            _sp.Popen(["xdg-open", str(target)])
        return {"ok": True, "dir": str(target)}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)[:200]}
