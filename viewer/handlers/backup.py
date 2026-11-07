# -*- coding: utf-8 -*-
"""backup 域 API 处理函数（从 viewer/server.py 物理抽取，2026-11-07 拆分重构）。

抽取规则：函数体逻辑零改动；依赖的 server 模块级符号经 `from viewer.server import (...)`
重定向进本地命名空间；标准库本地导入。server.py 末尾 re-export 保持命名空间兼容，
routes.py 与既有测试（S.api_*）无需改动。
"""
from __future__ import annotations

import sys, time
from viewer.server import (  # noqa: E402,F401
    DB,
    Path,
    ROOT,
    _shutil,
)

def api_backup() -> dict:
    """POST /api/backup：把当前 app.db（含 -wal/-shm）快照到 backups/。"""
    try:
        src_p = Path(DB.path)
        if not src_p.exists():
            return {"ok": False, "error": "app.db 不存在"}
        stamp = time.strftime("%Y%m%d-%H%M%S")
        out_dir = ROOT / "viewer" / "backups"
        out_dir.mkdir(parents=True, exist_ok=True)
        dst = out_dir / ("app.db.%s.bak" % stamp)
        _shutil.copy2(src_p, dst)
        for ext in ("-wal", "-shm"):
            pe = src_p.parent / (src_p.name + ext)
            if pe.exists():
                _shutil.copy2(pe, out_dir / (dst.name + ext))
        try:
            _mod = sys.modules.get("viewer.server", sys.modules[__name__])
            if hasattr(_mod, "_rotate_backups"):
                _mod._rotate_backups()
        except Exception:  # noqa: BLE001
            pass
        return {"ok": True, "path": str(dst), "size": dst.stat().st_size}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)[:200]}
def api_backup_restore(path: str | None = None) -> dict:
    """POST /api/backup/restore：从备份恢复 app.db（恢复前先自动备份当前）。"""
    if not path:
        return {"ok": False, "error": "未指定备份路径"}
    p = Path(path)
    allowed = ROOT / "viewer" / "backups"
    if not p.exists() or (allowed != p.parent and allowed not in p.parents):
        return {"ok": False, "error": "仅允许从 viewer/backups 恢复"}
    try:
        snap = api_backup()
        src_p = Path(DB.path)
        _shutil.copy2(p, src_p)
        for ext in ("-wal", "-shm"):
            pe = p.parent / (p.name + ext)
            if pe.exists():
                _shutil.copy2(pe, src_p.parent / (src_p.name + ext))
        return {"ok": True, "restored_from": str(p), "pre_restore_backup": snap.get("path")}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)[:200]}
