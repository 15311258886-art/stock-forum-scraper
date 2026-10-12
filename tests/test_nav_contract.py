# -*- coding: utf-8 -*-
"""导航 ↔ 面板 契约测试（规格书 A7「UI 关键渲染冒烟」）。

防的是我们踩过的那类坑：**改了导航 / 重命名 tab，却漏了对应的面板容器**，
结果点进去是空白或串到别的面板。本测试纯静态解析，无需起服务。

规则：
  app.js 里 `const NAV = [...]` 的每个 `tab: "xxx"`，都必须在 index.html 里有
  对应的面板容器 `id="p-xxx"`；少数面板由运行时动态创建（学习轨道等），列入豁免名单。

运行：
    .venv\\Scripts\\python.exe -m pytest tests/test_nav_contract.py -q
"""
from __future__ import annotations

import io
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP_JS = os.path.join(ROOT, "viewer", "static", "app.js")
INDEX_HTML = os.path.join(ROOT, "viewer", "static", "index.html")

# 运行时动态创建面板的 tab（不写在 index.html 里，由 app.js 渲染时插入容器）。
DYNAMIC_TABS = {
    "prediction",        # renderPrediction 动态建 #p-prediction
    "policy",            # 政策面板动态建
    "growth",            # 复盘成长动态建
    "learn_zdx_stock",   # 学习轨道动态建
}

# 非导航直达的诊断/运维面板（有容器，但入口不在左侧导航）。
ORPHAN_ALLOW = {
    "archive",    # 存档
    "fetchlog",   # 抓取日志
    "sources",    # 数据源健康
}

# 运行时由 itemsOfCat() 的 learn 分支注入的导航项（静态 NAV 里看不到，见 app.js:8148 注释）。
RUNTIME_NAV_TABS = {
    "buffett",         # 巴菲特选股
    "bili_authors",    # B站博主
    "bili_techvalue",  # B站技术·价值
}

# 形如 { tab: "today", label: "..." }  —— 引号用字符类匹配，避免转义问题
_TAB_RE = re.compile(r"tab:\s*[\"']([A-Za-z0-9_\-]+)[\"']")
_PANEL_RE = re.compile(r"id=[\"']p-([A-Za-z0-9_\-]+)[\"']")
_NAV_START = "const NAV = ["


def _read(path: str) -> str:
    return io.open(path, encoding="utf-8").read()


def _nav_block() -> str:
    """用**括号配对**精确切出 NAV 数组（不能按 '\\n];' 找——数组里嵌套项会让它提前截断）。"""
    js = _read(APP_JS)
    i = js.find(_NAV_START)
    assert i >= 0, "app.js 里找不到 `const NAV = [`"
    start = js.index("[", i)
    depth = 0
    k = start
    in_str = None
    while k < len(js):
        ch = js[k]
        if in_str:
            if ch == "\\":
                k += 2
                continue
            if ch == in_str:
                in_str = None
            k += 1
            continue
        if ch in ('"', "'", "`"):
            in_str = ch
            k += 1
            continue
        if ch == "/" and k + 1 < len(js) and js[k + 1] == "/":
            nl = js.find("\n", k)
            k = len(js) if nl < 0 else nl + 1
            continue
        if ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
            if depth == 0:
                return js[i:k + 1]
        k += 1
    raise AssertionError("NAV 数组未正常闭合")


def nav_tabs() -> list:
    return _TAB_RE.findall(_nav_block())


def panel_ids() -> set:
    return set(_PANEL_RE.findall(_read(INDEX_HTML)))


def test_nav_has_tabs():
    tabs = nav_tabs()
    assert len(tabs) >= 40, "NAV 解析出的 tab 太少（%d），解析逻辑可能失效" % len(tabs)
    return True


def test_every_nav_tab_has_panel():
    tabs = nav_tabs()
    panels = panel_ids()
    missing = [t for t in tabs if t not in panels and t not in DYNAMIC_TABS]
    assert not missing, (
        "以下 NAV 标签没有对应的面板容器 #p-<tab>（会点出空白页）：%s\n"
        "若确为运行时动态创建，请加入 tests/test_nav_contract.py 的 DYNAMIC_TABS" % missing
    )
    return True


def test_no_tab_duplicated():
    tabs = nav_tabs()
    dup = sorted({t for t in tabs if tabs.count(t) > 1})
    assert not dup, "NAV 里有重复 tab：%s" % dup
    return True


def test_panels_have_nav_entry():
    """反向：静态面板若既不在 NAV（含运行时注入项）、也不在豁免名单，多半是「孤儿面板」。"""
    tabs = set(nav_tabs()) | RUNTIME_NAV_TABS
    orphans = sorted(p for p in panel_ids()
                     if p not in tabs and p not in ORPHAN_ALLOW and p not in DYNAMIC_TABS)
    assert not orphans, "存在孤儿面板（有容器但 NAV 里进不去）：%s" % orphans
    return True


if __name__ == "__main__":
    import sys

    tests = [test_nav_has_tabs, test_every_nav_tab_has_panel,
             test_no_tab_duplicated, test_panels_have_nav_entry]
    fails = []
    for t in tests:
        try:
            ok = t()
            print(("PASS " if ok else "FAIL ") + t.__name__)
            if not ok:
                fails.append(t.__name__)
        except AssertionError as e:
            print("FAIL " + t.__name__ + " :: " + str(e))
            fails.append(t.__name__)
        except Exception as e:  # noqa: BLE001
            print("ERROR " + t.__name__ + " :: " + repr(e))
            fails.append(t.__name__ + "(error)")
    print("\n==== %d/%d PASS ====" % (len(tests) - len(fails), len(tests)))
    sys.exit(1 if fails else 0)
