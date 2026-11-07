# -*- coding: utf-8 -*-
"""evidence 域 API 处理函数（从 viewer/server.py 物理抽取，2026-11-07 拆分重构）。

抽取规则：函数体逻辑零改动；依赖的 server 模块级符号经 `from viewer.server import (...)`
重定向进本地命名空间；标准库本地导入。server.py 末尾 re-export 保持命名空间兼容，
routes.py 与既有测试（S.api_*）无需改动。
"""
from __future__ import annotations


def api_evidence() -> dict:
    """实证账本（2026-10-31）：系统「凭什么这么说」的唯一清单。

    每条形如：规则 / 样本量 / 覆盖交易日 / T+H 超额 / t 值 / 状态（active·weak·refuted·watch）
    / 可复现脚本。另附「已证伪禁区」——那些反直觉但实测相反的常识。

    **同一份数据同时喂三个地方**：本接口（界面依据）、ai_review 的上下文（模型边界）、
    avoid 的权重来源。这正是之前缺的那一环 —— 结论散在代码注释里，模型读不到，
    于是它会一本正经地引用我们早已推翻的说法。
    """
    try:
        import evidence as EV
        d = EV.summary()
        d["ok"] = True
        return d
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "reason": "evidence 不可用：%s" % str(e)[:160]}
