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
    # 2026-10-22 P1-44：低位突破三面板（含子面板）运行时动态建容器，无 index.html 预置
    "lowbox",            # 低位突破（app.js 动态建 #p-lowbox）
    "lowbox_bottom",     # 长期底部埋伏（lowbox 子面板）
    "lowbox_radar",      # 突破雷达（lowbox 子面板）
    # 2026-10-22 P1-44：豆包三面板经独立 NAV 块注入（非主 `const NAV = [`），运行时可达
    "dy_reso",           # 豆包×主力共振
    "integrate",         # 整合图谱
    "tech",              # 豆包技术方法
}

# 非导航直达的诊断/运维面板（有容器，但入口不在左侧导航）。
ORPHAN_ALLOW = {
    "archive",    # 存档
    "fetchlog",   # 抓取日志
    "sources",    # 数据源健康

    # ============ 2026-10-30 `ea292b6` 论坛/博主链路退役：有意撤出 NAV，容器保留 ============
    # 依据 DECISIONS.md D20（「论坛/博主链路整体退役」）原文：
    #   「一级类『他人操作』（7 面板）整体撤出 NAV；『讨论热度』『板块热度』（资金动向·人气）
    #     与『近期热点』（消息政策）一并撤下。**渲染函数与后端路由全部保留**（不删代码），
    #     源恢复时把 NAV 里那几行加回即可。」
    # 因此这 10 个 #p-* 容器是**有意保留**，不是待清理项：
    #   * 严禁删 index.html 里的容器（恢复入口时直接加回 NAV 即可复用）；
    #   * 严禁删 app.js 里对应的 render*/load* 函数与 server.py 里对应的 /api/ 路由；
    #   * 本名单只豁免「NAV 里进不去」这一条，不代表面板可用（其数据源已停抓，多为空）。
    # --- 「他人操作」类 7 面板 ---
    "cohold",       # 共同持仓
    "consensus",    # 持仓共识
    "niusan",       # 牛散追踪
    "operating",    # 正在操作
    "persons",      # 人物库
    "players",      # 实盘选手
    "smart_money",  # 聪明钱共识（容器 #p-smart_money 仍被「消息面汇总」BRIEF_BOX 复用）
    # --- 论坛停抓后失去数据源的 3 面板 ---
    "stocks",       # 讨论热度（资金动向·人气）
    "plate_heat",   # 板块热度（资金动向·人气）
    "topics",       # 近期热点（消息政策）
}

# 运行时由 itemsOfCat() 的 learn 分支注入的导航项（静态 NAV 里看不到，见 app.js itemsOfCat）。
RUNTIME_NAV_TABS = {
    "buffett",         # 巴菲特选股
    "bili_authors",    # B站博主
    "bili_techvalue",  # B站技术·价值
    # 2026-10-24 进哥：方法共振。与 strategy_lab / fundmasters / kronos 并列，
    # 由 itemsOfCat() 内 `const fixed = [...]` 注入「学习」类子项 → 真实可达，不是孤儿。
    "funnel",
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


_COMMENT_LINE_RE = re.compile(r"^\s*//")


def _strip_line_comments(block: str) -> str:
    """剔除 NAV 块里的**整行注释**（`^\\s*//`）。

    行内 `//` 不在这里处理 —— 那归 `_nav_block()` 的"字符串感知 + 括号配对"逻辑管，
    改它有风险，且已正确（字符串里的 `//` 不会被误当注释）。

    为什么必须剥：`nav_tabs()` 原本对原文直接跑 `_TAB_RE`，于是把 NAV 条目注释掉
    （`// { tab: "xxx", label: "..." },`）后 tab 数**一点不变** —— 而"注释掉导航项"
    恰恰是停用一个面板最现实的手法（比删行更常见：删了不好回滚）。
    QA 实测注释 2 个、注释 8 个，`test_nav_has_tabs` 与 `test_panels_have_nav_entry`
    全都照样 PASS —— 两条测试当时是瞎的。
    """
    return "\n".join(ln for ln in block.splitlines() if not _COMMENT_LINE_RE.match(ln))


def nav_tabs() -> list:
    tabs = _TAB_RE.findall(_strip_line_comments(_nav_block()))
    # 护栏：剥注释若把整块剥空（正则写错 / 切片被改坏），必须在这里炸掉。
    # 否则下游 `test_every_nav_tab_has_panel` 会拿到空列表后"无可检项"而假绿。
    assert tabs, "剥注释后 NAV 一个 tab 都解析不出来（剥得太狠或 NAV 块被切坏）"
    return tabs


def panel_ids() -> set:
    return set(_PANEL_RE.findall(_read(INDEX_HTML)))


def test_nav_has_tabs():
    """NAV 解析健康度下限——只防「解析逻辑整体失效」，不锁定具体数量。

    数量基线：2026-10-30 `ea292b6`（论坛/博主链路退役，DECISIONS.md D20）把「他人操作」
    类 7 个面板 +「讨论热度」「板块热度」「近期热点」共 10 个面板整体撤出 NAV，
    NAV 由 49 降到 39。这是**有意变更**，不是 `_nav_block()` 解析失效。
    故阈值取 38（当前基线 41，留 3 个余量）：低于 38 才判定括号配对/正则被改坏。
    新增面板时**无需改阈值**；只有「数量腰斩」这类解析事故才会触发。
    基线随加面板自然上移，仅作记录：2026-10-31 加「公告实证」(ann_event) → 41（由 nav_tabs() 实测，含 RUNTIME_NAV_TABS 注入的 funnel）。
    """
    tabs = nav_tabs()
    assert len(tabs) >= 38, (
        "NAV 解析出的 tab 太少（%d，当前基线 41 / 下限 38），解析逻辑可能失效" % len(tabs))
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
