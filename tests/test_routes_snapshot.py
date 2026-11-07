# -*- coding: utf-8 -*-
"""P1-33：路由快照 golden test。

把当前所有 /api/ 路由名固化进 tests/routes_snapshot.json。后续把 155 个 if 分支机械
转成路由注册表时，任何「漏迁 / 错迁 / 删除路由」都会让本测试失败——保证搬迁过程不丢不错。

策略（与 brief 「函数体先一行不动」一致）：**先快照、后重构**。当前阶段只做快照固化，
路由表化留待后续。本测试不 import server（纯静态解析，无副作用）。

首次运行若快照不存在会自动生成（写入后需重新提交该 json 作为 golden）。
"""
from __future__ import annotations

import io
import json
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# P1 重构后路由已从 server.py 抽到 routes.py；路由清单的权威来源是 routes.py。
SERVER = os.path.join(ROOT, "viewer", "routes.py")
SNAPSHOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "routes_snapshot.json")


def _read(p: str) -> str:
    return io.open(p, encoding="utf-8").read()


def extract_routes(src: str) -> list:
    pats = re.findall(r'route\s*==\s*["\'](/api/[^"\']+)["\']', src)
    pats += re.findall(r'route\.startswith\(\s*["\'](/api/[^"\']+)["\']', src)
    for m in re.finditer(r'route\s+in\s*\(([^)]*)\)', src):
        pats += re.findall(r'["\'](/api/[^"\']+)["\']', m.group(1))
    # 2026-11-07（#4 路由注册表统一·分批）：从 ROUTE_HANDLERS 字典提取已迁移的精确路由键
    mh = re.search(r'ROUTE_HANDLERS\s*=\s*\{([^}]*)\}', src, re.S)
    if mh:
        pats += re.findall(r'["\'](/api/[^"\']+)["\']\s*:', mh.group(1))
    # 规整 startswith 前缀（去掉末尾通配，保留稳定前缀用于对比）
    norm = set()
    for p in pats:
        norm.add(p)
    return sorted(norm)


def test_routes_snapshot_matches():
    src = _read(SERVER)
    current = extract_routes(src)
    if not os.path.exists(SNAPSHOT):
        with io.open(SNAPSHOT, "w", encoding="utf-8") as f:
            json.dump(current, f, ensure_ascii=False, indent=1)
        assert current, "提取到 0 条路由，提取逻辑可能失效"
        return  # 首次生成 golden
    with io.open(SNAPSHOT, encoding="utf-8") as f:
        golden = json.load(f)
    assert current == golden, (
        "路由表与快照不一致（重构时漏/错/删了路由？）\n"
        "仅新增: %s\n仅消失: %s"
        % (sorted(set(current) - set(golden)), sorted(set(golden) - set(current)))
    )
    assert len(current) >= 100, "路由数异常偏少（%d），提取逻辑可能失效" % len(current)
