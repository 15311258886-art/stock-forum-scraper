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


# ============================================================================
# 2026-11-01 测评 P1-2「产品聚焦」：7 类 → 6 组的分组契约
# ============================================================================
#
# 上面四条守的是「tab ↔ 面板容器」；下面这一组守的是**分组重构本身**。
# 并类是有代价的：任何一处漏迁，结果都是「某个面板再也打不开」，而且界面上不会有任何
# 报错（没有异常、没有 404，只是点不到）。所以把三条铁律写成可机判的断言：
#
#   ① 数量不变：NAV 的静态 tab 一个不少（43）。数错一个就等于删了一个面板。
#   ② 标识不变：tab key 全局唯一且与改动前一致（它是 #p-<tab> 容器与深链接的唯一键）。
#   ③ 深链接不失效：对**每一个** tab，用**每一个**旧类名拼出来的 hash 都必须落到同一面板。
#      列表从 NAV 常量现算 —— 不手写清单；手写清单跟代码一起错的时候，测试会一起通过。

#: 改动前的 7 个一级分类 key。这是**历史事实**，无法从现在的代码推出来，故只能写死；
#: 它只用于"造旧链接"，与 NAV 里现在有什么无关 —— 即使 NAV 删掉一个 tab，本清单也不变，
#: 从而保证"删了 tab"一定会被抓到。
OLD_CATS = ("overview", "money", "info", "opinion", "watch", "research", "learn")
#: 更早一层（2026-09-29 之前）的类名。
ANCIENT_CATS = ("trade", "extref", "enhance")
#: 改动前的静态 tab 总数。锚点，用来守"一个不删"。
EXPECTED_TAB_COUNT = 43
#: 目标分组（顺序即左栏顺序）。
EXPECT_GROUPS = ["today", "mine", "market", "stock", "strategy", "system"]

_NAV_OBJ_RE = re.compile(r'\{[^{}]*\btab:\s*"([A-Za-z0-9_\-]+)"[^{}]*\}')


def nav_cats() -> dict:
    """{cat_key: [{"tab":…, "fold":bool, "child":bool, "lead":bool}, …]}（保持源码顺序）。"""
    block = _strip_line_comments(_nav_block())
    starts = [m.start() for m in re.finditer(r'\{\s*key:\s*"', block)]
    cats = {}
    for n, s in enumerate(starts):
        e = starts[n + 1] if n + 1 < len(starts) else len(block)
        chunk = block[s:e]
        key = re.search(r'\{\s*key:\s*"([A-Za-z_]+)"', chunk)
        if not key:
            continue
        items = []
        for m in _NAV_OBJ_RE.finditer(chunk):
            obj = m.group(0)
            items.append({
                "tab": m.group(1),
                "fold": bool(re.search(r"\bfold:\s*true", obj)),
                "child": bool(re.search(r"\bchild:\s*true", obj)),
                "lead": bool(re.search(r"\blead:\s*true", obj)),
            })
        cats[key.group(1)] = items
    return cats


def _flat_map(var_name: str) -> dict:
    """解析 `const X = { a: "b", … }`。"""
    src = _read(APP_JS)
    i = src.index("const %s = {" % var_name)
    j = src.index("}", i)
    return dict(re.findall(r'([A-Za-z_]+)\s*:\s*"([A-Za-z_]+)"', src[i:j]))


def _tab_home(cats: dict) -> dict:
    m = {}
    for k, items in cats.items():
        for it in items:
            m[it["tab"]] = k
    return m


def _resolve(cat: str, tab: str, cats: dict, legacy: dict) -> tuple:
    """复刻 app.js `migrateHashRaw` + `navFromHash` 的优先级（顺序不能换）。

      ① 分类名本身有效 → 原样
      ② 否则按 tab 反查新归属（覆盖"一个旧类被拆到多个新组"）
      ③ 否则按 LEGACY_CAT 翻译类名（纯类名深链兜底）
    换顺序的后果很具体：`#overview/avoid` 会先命中 LEGACY_CAT → 送到 today，
    而 avoid 实际在 today —— 但 `#overview/lhb` 会被送到 today 而 lhb 在 market，打错面板。
    """
    if cat in cats:
        return (cat, tab)
    if tab and tab in _tab_home(cats):
        return (_tab_home(cats)[tab], tab)
    if cat in legacy:
        return (legacy[cat], tab)
    return (cat, tab)


def test_groups_are_the_planned_six():
    """一级分组必须是计划的 6 组且顺序一致（顺序 = 左栏顺序 = 使用频率）。"""
    cats = nav_cats()
    assert list(cats.keys()) == EXPECT_GROUPS, \
        "一级分组不是预期的 6 组：%s" % list(cats.keys())
    return True


def test_static_tab_count_unchanged():
    """43 个静态入口一个不少 —— 这是「不删面板」的可执行版本。"""
    cats = nav_cats()
    total = sum(len(v) for v in cats.values())
    assert total == EXPECTED_TAB_COUNT, \
        "静态 tab 数从 %d 变成 %d（少一个就是少一个面板）" % (EXPECTED_TAB_COUNT, total)
    return True


def test_every_tab_has_a_unique_home():
    """每个 tab 属于且只属于一个组（重复会让 TAB_HOME 被后者覆盖 → 深链接随机落错组）。"""
    cats = nav_cats()
    seen = {}
    for k, items in cats.items():
        for it in items:
            t = it["tab"]
            assert t not in seen, "tab %s 同时出现在 %s 与 %s" % (t, seen.get(t), k)
            seen[t] = k
    assert len(seen) == EXPECTED_TAB_COUNT
    return True


def test_first_item_of_each_group_is_a_main_tab():
    """每组首项必须是非 fold、非 child 的主项 —— firstRealTab() 用它做「点大类」的落地点。"""
    for k, items in nav_cats().items():
        first = items[0]
        assert not first["fold"], "%s 的首项 %s 被折进了研究工具区" % (k, first["tab"])
        assert not first["child"], "%s 的首项 %s 是子项" % (k, first["tab"])
    return True


def test_each_group_folds_the_tail():
    """每个组只平铺 1~4 个主项，其余进折叠区（「少」与「全」同时成立的关键）。"""
    for k, items in nav_cats().items():
        n_main = len([i for i in items if not i["fold"] and not i["child"]])
        n_fold = len([i for i in items if i["fold"]])
        if len(items) == 1:
            continue      # 「数据与系统」只有 health 一项，无可折叠
        assert 1 <= n_main <= 4, "%s 平铺了 %d 个主项（目标 2~4）" % (k, n_main)
        assert n_fold >= 1, "%s 没有任何折叠项（等于全部平铺，等于没做收敛）" % k
    return True


def test_legacy_cat_covers_every_old_group():
    legacy = _flat_map("LEGACY_CAT")
    for c in OLD_CATS + ANCIENT_CATS:
        assert c in legacy, "旧类名 %s 没登记在 LEGACY_CAT（纯类名深链会落到空白）" % c
    cats = nav_cats()
    for c, target in legacy.items():
        assert target in cats, "LEGACY_CAT[%s] = %s 不是现在的一级分组" % (c, target)
    return True


def test_every_tab_reachable_from_every_old_cat():
    """铁律③：43 个 tab × 10 个历史类名，全部必须落到同一面板。"""
    cats = nav_cats()
    legacy = _flat_map("LEGACY_CAT")
    home = _tab_home(cats)
    bad = []
    for tab, want_cat in home.items():
        for old in OLD_CATS + ANCIENT_CATS:
            got = _resolve(old, tab, cats, legacy)
            if got != (want_cat, tab):
                bad.append("#%s/%s → #%s/%s（应为 #%s/%s）"
                           % (old, tab, got[0], got[1], want_cat, tab))
    assert not bad, "有 %d 条旧深链接会打不开或打错面板：\n%s" % (len(bad), "\n".join(bad[:25]))
    return True


def test_every_tab_reachable_from_its_own_group():
    """新链接自证：#<新类>/<tab> 必须原样解析到自身。"""
    cats = nav_cats()
    legacy = _flat_map("LEGACY_CAT")
    for tab, cat in _tab_home(cats).items():
        assert _resolve(cat, tab, cats, legacy) == (cat, tab), "#%s/%s 解析不到自己" % (cat, tab)
    return True


def test_tab_home_is_computed_from_nav():
    """TAB_HOME 必须是**现算**的：40 个 tab 换家时手工表必漏一行，漏一行就是打错面板。"""
    src = _read(APP_JS)
    i = src.index("const TAB_HOME =")
    seg = src[i:i + 400]
    assert "NAV.forEach" in seg, "TAB_HOME 又变回手工登记了"
    return True


def test_subnav_renders_fold_and_auto_opens_for_active_tab():
    """折叠区必须真的渲染，且当前 tab 落在折叠区时自动展开（否则用户看不见自己在哪）。"""
    src = _read(APP_JS)
    i = src.index("function paintSubnav(")
    seg = src[i:i + 7000]
    assert "研究工具（" in seg, "横条里没有「研究工具（N）」折叠区"
    assert "i.fold" in seg.replace("item.fold", "i.fold"), "没按 fold 标记分流"
    assert 'hasActive ? " open" : ""' in seg.replace("'", '"'), \
        "折叠区不会因当前 tab 在其中而自动展开"
    return True


def test_items_of_cat_merges_instead_of_replacing():
    """itemsOfCat 必须「合并 + 去重」，不是 tracks.concat(fixed) 那种整体重建。

    旧写法会把 NAV 里的静态项整体丢弃，只能靠 fixed 再抄一遍救回来 —— 两份清单写同一批 tab。
    """
    src = _read(APP_JS)
    i = src.index("function itemsOfCat(")
    seg = src[i:i + 2400]
    assert "cat.items.forEach(push)" in seg, "静态 NAV 项没并回（旧 bug：策略回测/轨道管理会消失）"
    assert "KB_FIXED_TABS" in seg, "固定子板块没并回"
    assert "seen.has(it.tab)" in seg, "没按 tab 去重（会出现两个同名入口）"
    return True


def test_dyn_category_renamed_with_track_group_mapping():
    """learn 类并进 strategy 后，yaml 的 group 名（learn）必须仍有翻译层。

    没有翻译层时 tracksOfCat 按 group 过滤 → 一条知识轨道都拉不出来，而且**不报错**。
    """
    src = _read(APP_JS)
    m = re.search(r"const DYN_CATS = \{\s*([A-Za-z_]+):", src)
    assert m, "DYN_CATS 定义丢失"
    assert m.group(1) == "strategy", "DYN_CATS 没跟随分组改名（仍是 %s）" % m.group(1)
    assert "DYN_TRACK_GROUP" in src, "缺「导航 key → yaml group」翻译层"
    assert m.group(1) in nav_cats(), "DYN_CATS 挂在不存在的分组上"
    return True


def test_kb_fixed_tabs_still_reachable():
    """8 个固定子板块从未写进 NAV，但必须仍然可达（挂在 strategy 的折叠区里）。"""
    src = _read(APP_JS)
    i = src.index("const KB_FIXED_TABS = [")
    j = src.index("];", i)
    fixed = re.findall(r'tab:\s*"([A-Za-z0-9_\-]+)"', src[i:j])
    for t in ("fundmasters", "bili_authors", "bili_techvalue", "tech",
              "integrate", "dy_reso", "kronos", "funnel"):
        assert t in fixed, "固定子板块 %s 丢了" % t
    return True


def test_today_page_has_five_elements():
    """首页五要素（P1-2 验收线）：市场状态 / 方向 / 风险 / 持仓触发与失效 / 数据健康。"""
    src = _read(APP_JS)
    need = [
        ("① 当前市场状态", "缺「当前市场状态」（第 1 项）"),
        ("② 今日方向", "缺「今日方向」（第 2 项）"),
        ("④ 今日风险", "缺「今日风险」（第 3 项）"),
        ("⑤ 我的票提示", "缺「我的票提示」（第 4 项）"),
        ("数据健康", "缺「数据健康」（第 5 项）"),
        ("tdInvalidateText", "我的票没有「失效条件」列（第 4 项只做了触发）"),
        ("状态未判定", "市场状态判不出来时没有「未判定」出口（会变成编一个）"),
    ]
    missing = [why for tok, why in need if tok not in src]
    assert not missing, "首页五要素缺项：%s" % missing
    return True



# ---------------------------------------------------------------------------
# 2026-11-01 P1-2 第 3 项：DOM 瘦身（隐藏面板首绘延迟）
# ---------------------------------------------------------------------------
# 实测（verify_dom_weight.cjs）：首屏 12840 个节点里 95.7% 是隐藏的，其中 8359 个
# （65%）集中在三个「当前 tab 根本看不见」的面板：海外舆情 5164 / 龙虎榜 1958 / 国际局势 1237。
# 做法是把**首绘**推迟到面板第一次可见（数据照旧首屏并行拉）。
# 这里守两件事，缺一条就会退化成"看不见也照画"或"点进去永远空"：
#   ① 三个重面板的绘制入口必须走 deferPaint；
#   ② selectNav 必须 flush —— 它是唯一能让面板变可见的入口，漏了就是永久白屏。

def test_heavy_hidden_panels_paint_is_deferred():
    """三个最重的隐藏面板，绘制必须经 deferPaint（而不是数据一到直接 render）。"""
    src = _read(APP_JS)
    seg = src[src.index("function loadAll("):]
    seg = seg[:seg.index("\n}")]
    assert 'deferPaint("overseas_sentiment"' in seg, "海外舆情(5164 节点)又变回首屏直接画"
    assert 'deferPaint("geo_events"' in seg, "国际局势(1237 节点)又变回首屏直接画"
    i = src.index("function loadLhb(")
    assert 'deferPaint("lhb"' in src[i:i + 600], "龙虎榜(1958 节点)又变回首屏直接画"
    return True


def test_select_nav_flushes_deferred_paint():
    """selectNav 必须 flush 待补绘的面板 —— 它覆盖点导航/横条/改 hash/深链接/showTab 全部路径。"""
    src = _read(APP_JS)
    i = src.index("function selectNav(")
    seg = src[i:i + 30000]
    assert "flushPaint(tab);" in seg, "selectNav 没有 flushPaint(tab) → 被推迟的面板点进去永远「正在加载数据…」"
    assert 'flushPaint("lhb")' in seg, "lhb 分支没有 flush，会和通铺 flush 重复画一次"
    # surface 不得靠定时器补救：setTimeout 只是把节点晚几百毫秒建出来，总量一分没少
    j = seg.index("flushPaint(tab);")
    assert "setTimeout" not in seg[max(0, j - 200):j], "flushPaint 被包进了 setTimeout（等于没瘦身）"
    return True


def test_defer_paint_has_shell_preparer_for_overseas():
    """海外舆情的两个子视图共用一个壳：谁先回来谁画，壳必须由 flush 保证存在。"""
    src = _read(APP_JS)
    i = src.index("const DEFER_PREP = {")
    seg = src[i:i + 400]
    assert "overseas_sentiment" in seg and "ensureOverseasShell" in seg, \
        "DEFER_PREP 没给海外舆情备好壳 → 热点子视图先到时会往不存在的 #ov-sub-hotspots 里画"
    return True
