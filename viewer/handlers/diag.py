# -*- coding: utf-8 -*-
"""diag 域 API 处理函数（从 viewer/server.py 物理抽取，2026-11-07 拆分重构）。

抽取规则：函数体逻辑零改动；依赖的 server 模块级符号经 `from viewer.server import (...)`
重定向进本地命名空间；标准库本地导入。server.py 末尾 re-export 保持命名空间兼容，
routes.py 与既有测试（S.api_*）无需改动。
"""
from __future__ import annotations

import re, time
from viewer.server import (  # noqa: E402,F401
    _DIAG_TTL,
    _announcements_cached,
    _em_secid,
    _expert_verdict,
    _stock_moneyflow_any,
    api_quote,
    cf,
    valuation,
)

def api_diag(symbol: str, force: bool = False) -> dict:
    """个股诊断聚合接口：一次返回行情/财务/分红/概况/估值评分/资金流/公告/专家分析。"""
    key = (symbol or "").strip()
    if not re.match(r"^\d{6}$", key):
        return {"ok": False, "reason": "请输入 6 位 A 股数字代码（如 600519）"}
    if not force:
        hit = _DIAG_CACHE.get(key)
        if hit and time.time() - hit[0] < _DIAG_TTL:
            return hit[1]
    market, code, _ = _em_secid(key)
    with cf.ThreadPoolExecutor(max_workers=4) as ex:
        f_q = ex.submit(api_quote, key, bool(force))
        f_v = ex.submit(valuation.score, key)
        f_m = ex.submit(_stock_moneyflow_any, key)
        f_a = ex.submit(_announcements_cached, code, 8)
        q = f_q.result()
        v = f_v.result()
        m = f_m.result()
        ann = f_a.result()
    if not isinstance(q, dict) or q.get("error"):
        # 行情失败不整页报废：估值/资金流/公告仍可用，行情段如实标注
        q_err = (q or {}).get("error") if isinstance(q, dict) else "行情获取失败"
        q_fin: dict = {}
        q_dv = None
        q_name = ""
        q_quote = None
        q_org = None
    else:
        q_err = None
        q_fin = q.get("finance") or {}
        q_dv = q.get("dividend")
        q_name = q.get("name") or ""
        q_quote = q.get("quote")
        q_org = q.get("org")
    expert = _expert_verdict(q_fin, v if isinstance(v, dict) else {},
                             m if isinstance(m, dict) else {}, q_dv)
    payload = {
        "ok": True, "symbol": code, "name": q_name,
        "quote_error": q_err,
        "quote": q_quote, "qval": (q.get("valuation") if isinstance(q, dict) else None),
        "org": q_org, "finance": q_fin, "dividend": q_dv,
        "valuation": v if isinstance(v, dict) else {"ok": False},
        "moneyflow": m if isinstance(m, dict) else {"ok": False},
        "announcements": ann if isinstance(ann, dict) else {"ok": False},
        "expert": expert,
        "disclaimer": "本页评分与建议均由本地规则引擎基于公开数据生成，仅供研究参考，不构成投资建议。",
    }
    _DIAG_CACHE[key] = (time.time(), payload)
    return payload
