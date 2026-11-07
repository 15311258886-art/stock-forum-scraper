# -*- coding: utf-8 -*-
"""config 域 API 处理函数（从 viewer/server.py 物理抽取，2026-11-07 拆分重构）。

抽取规则：函数体逻辑零改动；依赖的 server 模块级符号经 `from viewer.server import (...)`
重定向进本地命名空间；标准库本地导入。server.py 末尾 re-export 保持命名空间兼容，
routes.py 与既有测试（S.api_*）无需改动。
"""
from __future__ import annotations

import json, logging
from viewer.server import (  # noqa: E402,F401
    DB,
    _cfg_key,
)

def api_config_get(key: str) -> dict:
    """GET /api/config/get：读一项用户配置。先查用户覆盖（meta），回退 AppConfig 默认。"""
    key = (key or "").strip()
    if not key:
        return {"ok": False, "error": "key 为空"}
    try:
        from config import load_config
        cfg = load_config()
        raw = DB.get_meta(_cfg_key(key))
        default = cfg.as_dict().get(key, None)
        return {"ok": True, "key": key, "value": raw, "default": default}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)[:200]}
def api_config_set(key: str, value) -> dict:
    """POST /api/config/set：持久化一项用户配置到 meta（并同步 user_config.json）。"""
    key = (key or "").strip()
    if not key:
        return {"ok": False, "error": "key 为空"}
    try:
        DB.set_meta(_cfg_key(key), json.dumps(value, ensure_ascii=False))
        try:
            from config import USER_CONFIG_PATH
            overrides = {}
            if USER_CONFIG_PATH.exists():
                overrides = json.loads(USER_CONFIG_PATH.read_text(encoding="utf-8"))
            overrides[key] = value
            USER_CONFIG_PATH.write_text(json.dumps(overrides, ensure_ascii=False, indent=1),
                                        encoding="utf-8")
        except Exception:  # noqa: BLE001
            logging.exception("同步 user_config.json 失败（不影响 meta 落盘）")
        return {"ok": True, "key": key}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)[:200]}
