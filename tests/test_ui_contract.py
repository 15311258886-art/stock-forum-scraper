# -*- coding: utf-8 -*-
"""UI 契约守卫：data-testid / 属性标识的**数量**与**位置**（2026-11-01）

为什么要有这个文件
------------------
e2e（`tests/e2e/run.cjs`）原来只能靠 `.si-item` / `.ks-grid` / innerText 长度这类**结构特征**
去定位元素。结构不是契约 —— 改一次版式，选择器就静默失配，测试变绿（读到 null 也算"跑过了"）
而真实缺陷漏过。2026-11-01 给关键位置补了 5 个稳定标识，本文件把它们钉死。

为什么不是"包含字符串"那种断言
------------------------------
`assert "data-testid" in src` 是**松守卫**：删掉其中一处、留另一处，它照样绿。
松守卫等于没有守卫，还会给人"已经守住了"的错觉。所以这里一律断言：
  ① **恰好出现 N 次**（不是 ≥1）—— 多一处意味着有人复制粘贴了模板，少一处意味着被删了；
  ② **出现在哪个函数里**（按函数边界切段再匹配）—— 同一串出现在别处不算数；
  ③ **相邻上下文**（如 vol-quote 必须紧挨着 `siVol(q.volume, volUnitOf(q,"hand"))`）。
数量一变，pytest 就红，跑一次 0.2 秒，比 e2e 便宜三个数量级。

标识清单（与 `tests/e2e/run.cjs` 的用法一一对应）
--------------------------------------------------
| 标识                                   | 位置                          | 谁在用                      |
|----------------------------------------|-------------------------------|-----------------------------|
| `data-testid="stk-drawer"`             | openStock 打开抽屉后挂上      | e2e 个股抽屉 / needs_testid |
| `data-testid="vol-quote"`              | 抽屉「行情」区的成交量格      | e2e P0-1 单位一致性         |
| `data-testid="vol-kline"`              | 抽屉 K 线快照的成交量格       | 同上                        |
| `data-testid="watch-row"`              | 关注池每行 `<tr>`             | e2e 自选增删                |
| `.sub-tab[data-cat]`                   | 横条每个子标签                | e2e 分类归属断言            |
| `.panel[data-ready="1"]`               | 面板渲染完成时                | e2e 面板就绪（不再 sleep）  |
| `data-testid="fm-frame"`               | 基金经理策略 iframe           | e2e 探子文档（不量父文档）  |
| `data-testid="fm-fallback"`            | 该 iframe 失败时的显性空态    | 人看；e2e 断言空态带原因    |

另外三条**行为**守卫（不是标识，但同样按"恰好 N 处"守）：
  ⑨ 折叠区 `emitted` 记账（子项不得渲染两遍）
  ⑩ 豆包×主力共振的 null 三态（不许出现字面量 `null`）
  ⑪ 基金经理策略 iframe 面板（可探活 + 空态不白板 + 就绪判定认 iframe）
"""
from __future__ import annotations

import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP_JS = os.path.join(ROOT, "viewer", "static", "app.js")
RUN_CJS = os.path.join(ROOT, "tests", "e2e", "run.cjs")
DOUYIN_JS = os.path.join(ROOT, "viewer", "static", "douyin_corpus.js")
SERVER_PY = os.path.join(ROOT, "viewer", "server.py")
INDEX_HTML = os.path.join(ROOT, "viewer", "static", "index.html")


def _read(path: str) -> str:
    with open(path, encoding="utf-8") as f:
        return f.read()


def _fn_body(src: str, header_re: str, size: int = 40000) -> str:
    """按函数头切出一段（到下一个顶层 `function `/`const X = ` 之前），用于"只在某函数里"断言。"""
    m = re.search(header_re, src)
    assert m, "找不到函数头：%s（被重命名或删除了？）" % header_re
    start = m.start()
    tail = src[start + 1:]
    nxt = re.search(r"\n(?:function |const [A-Za-z_$][\w$]* = |/\* )", tail)
    end = start + 1 + (nxt.start() if nxt else min(len(tail), size))
    return src[start:end]


def _count(src: str, needle: str) -> int:
    return src.count(needle)


# ---------------------------------------------------------------------------
# ① 个股抽屉
# ---------------------------------------------------------------------------
def test_stock_drawer_testid_exists_exactly_once():
    """`data-testid="stk-drawer"` 必须**恰好一处**，且在 openStock 里挂、打开之后挂。

    恰好一处很重要：抽屉是共用的（板块成员 / 研报也走 openDrawer），
    如果别处也挂这个名字，"先看个股、再看板块成员"时属性会残留，
    e2e 就会拿个股的标识去找板块成员的内容。
    """
    src = _read(APP_JS)
    assert _count(src, 'setAttribute("data-testid", "stk-drawer")') == 1, \
        "stk-drawer 必须恰好挂一处（当前 %d 处）" % _count(src, 'setAttribute("data-testid", "stk-drawer")')
    body = _fn_body(src, r"function openStock\(")
    assert 'setAttribute("data-testid", "stk-drawer")' in body, "stk-drawer 没挂在 openStock 里"
    # 必须挂在 openDrawer(...) 之后 —— 之前挂会被 openDrawer 的 removeAttribute 清掉
    i_open = body.index("openDrawer(`股票 · ${symbol}`")
    i_tid = body.index('setAttribute("data-testid", "stk-drawer")')
    assert i_tid > i_open, "stk-drawer 挂在 openDrawer 之前，会被抽屉清除逻辑抹掉"
    return True


def test_drawer_clears_previous_testid():
    """openDrawer 必须**每开必清**上一轮的标识（抽屉共用，属性残留会指向错的面板内容）。"""
    src = _read(APP_JS)
    body = _fn_body(src, r"function openDrawer\(")
    assert body.count('removeAttribute("data-testid")') == 1, \
        "openDrawer 里应恰好有一处 removeAttribute(\"data-testid\")（抽屉共用，必须每开先清）"
    return True


# ---------------------------------------------------------------------------
# ② 两个成交量格（P0-1 单位一致性的守门项）
# ---------------------------------------------------------------------------
def test_vol_kline_testid_on_the_right_cell():
    """`vol-kline` 恰好一处，且紧贴 `siVol(c.volume, klineVolUnit(c, d))` 那一格。

    只断言"出现一次"不够：这个格子在抽屉里，同一文件还有另一套 K 线渲染（别的面板），
    标识挂错到那套上去，e2e 读的就是另一个面板的数字 —— 绿得毫无意义。
    """
    src = _read(APP_JS)
    assert _count(src, 'data-testid="vol-kline"') == 1, "vol-kline 必须恰好一处"
    pat = re.compile(r'<div data-testid="vol-kline"><span>成交量</span><b>\$\{siVol\(c\.volume, klineVolUnit\(c, d\)\)\}</b></div>')
    assert pat.search(src), "vol-kline 没有挂在「成交量 + klineVolUnit」那一格上"
    return True


def test_vol_quote_testid_passed_through_item_helper():
    """`vol-quote` 恰好一处，且通过 item() 的第 5 参挂到「成交量」格上。

    行情区那格是 `item(标签, 值, ...)` 生成的，标识只能从 helper 注入；
    直接改字符串会绕过 helper，别的格子的样式/结构就不一致了。
    """
    src = _read(APP_JS)
    assert _count(src, '"vol-quote"') == 1, 'vol-quote 字符串必须恰好一处（当前 %d）' % _count(src, '"vol-quote"')
    assert re.search(r'item\("成交量", siVol\(q\.volume, volUnitOf\(q, "hand"\)\), "", "", "vol-quote"\)', src), \
        'vol-quote 没有作为 item() 的第 5 参挂到「成交量」格上'
    # helper 必须真的支持第 5 参并渲染成属性，否则传了也白传
    assert _count(src, 'data-testid="${tid}"') == 1, "item() 里的 data-testid 注入点必须恰好一处"
    return True


# ---------------------------------------------------------------------------
# ③ 自选行
# ---------------------------------------------------------------------------
def test_watch_row_testid_exactly_once_in_watch_row():
    """`watch-row` 恰好一处，且在 watchRow() 的 `<tr>` 上，并同时带 data-symbol。

    带 data-symbol 是为了"按代码找行"；只给一个通用 testid，e2e 还得遍历所有行自己比对文本。
    """
    src = _read(APP_JS)
    assert _count(src, 'data-testid="watch-row"') == 1, "watch-row 必须恰好一处"
    body = _fn_body(src, r"function watchRow\(")
    assert 'data-testid="watch-row"' in body, "watch-row 不在 watchRow() 里"
    assert 'data-symbol="${escAttr(it.symbol || "")}"' in body, "watch-row 没带 data-symbol（按代码找行会退回文本比对）"
    return True


# ---------------------------------------------------------------------------
# ④ 横条 data-cat
# ---------------------------------------------------------------------------
def test_sub_tab_data_cat_defined_once_used_three_times():
    """`data-cat` 在 paintSubnav 里**定义一次、用三次**（主项 / 子项 / 独立项三个模板）。

    定义一次是刻意的：三处各写一遍字符串，改一处漏两处的那天没人会发现。
    这里同时守住"三处模板都用了"——少用一处，那条路径上的 tab 就没有 data-cat。
    """
    src = _read(APP_JS)
    body = _fn_body(src, r"function paintSubnav\(")
    assert body.count('data-cat="${escAttr(cat.key)}"') == 1, \
        "data-cat 的定义处必须恰好一处（当前 %d）" % body.count('data-cat="${escAttr(cat.key)}"')
    assert body.count("${dcatAttr}") == 3, \
        "三个 sub-tab 模板都必须引用 dcatAttr（当前 %d 处）" % body.count("${dcatAttr}")
    # 三个模板确实都在（防止有人把某个模板删了、顺手把引用也删掉，"3 处"变成 2 处却更隐蔽）
    for cls in ("sub-tab sub-lead", "sub-tab sub-child", 'class="sub-tab${i.tab === tab'):
        assert cls in body, "sub-tab 模板少了一类：%s" % cls
    return True


# ---------------------------------------------------------------------------
# ⑤ 面板 data-ready
# ---------------------------------------------------------------------------
def test_panel_ready_marked_in_one_place_and_watcher_started_once():
    """面板就绪标记只能有一个**设置点**（markPanelReady），且观察者只启动一次。

    设置点多于一处 = 有人又回到了"每个 render 末尾手工加一行"，那正是这个方案要避免的
    （几十个渲染函数，每个都得记得加）。启动点多于一处 = 挂了两个观察者，翻倍开销。
    """
    src = _read(APP_JS)
    assert _count(src, 'setAttribute("data-ready", "1")') == 1, \
        "data-ready 的设置点必须恰好一处（当前 %d）" % _count(src, 'setAttribute("data-ready", "1")')
    body = _fn_body(src, r"function markPanelReady\(")
    assert 'setAttribute("data-ready", "1")' in body, "data-ready 不在 markPanelReady 里设置"
    assert _count(src, "function initPanelReadyWatcher(") == 1, "initPanelReadyWatcher 定义必须唯一"
    assert _count(src, "initPanelReadyWatcher();") == 1, "观察者必须只启动一次"
    # 必须观察 .panel 容器（subtree），否则 render 之后不会重算
    wbody = _fn_body(src, r"function initPanelReadyWatcher\(")
    assert "MutationObserver" in wbody, "就绪判定不是靠观察者，靠 sleep 猜吗"
    assert "subtree: true" in wbody, "观察者没有 subtree，面板内部重绘不会触发就绪重算"
    return True


def test_panel_ready_ignores_loading_placeholder():
    """占位文案不能算"已就绪" —— 否则 e2e 会在"正在加载数据…"那一刻就往下跑。"""
    src = _read(APP_JS)
    body = _fn_body(src, r"function markPanelReady\(")
    assert "panel-loading" in body, "markPanelReady 没排除 .panel-loading 占位"
    assert "removeAttribute(\"data-ready\")" in body, "占位态必须把 data-ready 收回，否则会保留上一轮的 ready"
    return True


# ---------------------------------------------------------------------------
# ⑥ 命令面板：鼠标/键盘必须共用一个 open 函数
# ---------------------------------------------------------------------------
def test_cmd_palette_paths_share_one_open_function():
    """Ctrl+K（键盘）与点击（鼠标）必须走**同一个** openStockFromCmd，且代码以输入框为准。

    事故原型：键盘路径 Enter 时按 DOM 现况现查 `#cmdList .cmd-item[data-code]`，
    列表还停在上一帧时会落到第一条**命令**（如「一键刷新全部」）而不是打开个股 ——
    鼠标能开、快捷键不开，且不报错。
    """
    src = _read(APP_JS)
    assert _count(src, "function openStockFromCmd(") == 1, "openStockFromCmd 定义必须唯一"
    body = _fn_body(src, r"function bindCmdKeys\(")
    assert "openStockFromCmd((ci.value" in body, "Enter 分支没有以输入框的值调用 openStockFromCmd"
    assert "#cmdList .cmd-item[data-code]" not in body, \
        "Enter 分支又回去按 DOM 现况现查 data-code 了（这正是鼠标能开、键盘不开的根因）"
    act = _fn_body(src, r"function cmdActivate\(")
    assert "openStockFromCmd(el.dataset.code)" in act, "鼠标路径没有走 openStockFromCmd"
    return True


# ---------------------------------------------------------------------------
# ⑦ 今日决策：pct 缺失必须显示「—」，不能静默省略
# ---------------------------------------------------------------------------
def test_missing_pct_shows_dash_not_empty():
    """pct 为 null 时不能输出空串 —— 静默省略让用户分不清"没涨跌幅"和"页面漏了"。

    两处：可执行机会行、观察/待验证行。都改成 `<span class="td-pct muted">—</span>`。
    断言"恰好 2 处"是因为只有这两处是「今日决策卡片里的票」；
    其它面板（表格）本来就有自己的「—」出口，不该被这条规则牵连。
    """
    src = _read(APP_JS)
    bad = re.findall(r'x\.pct != null \?[^\n]*: ""\}', src)
    assert not bad, "还有 %d 处 pct 缺失时输出空串：%s" % (len(bad), bad[:2])
    assert _count(src, '<span class="td-pct muted"') == 2, \
        "「—」的出口应恰好两处（可执行行 + 观察行），当前 %d" % _count(src, '<span class="td-pct muted"')
    return True


# ---------------------------------------------------------------------------
# ⑧ e2e 侧：必须优先用 testid，结构只作兜底
# ---------------------------------------------------------------------------
def test_e2e_prefers_testid_over_structural_probe():
    """e2e 必须**优先** testid：结构探测只能作兜底，且兜底被用到要登记。

    这条守的是方向性：如果哪天有人把 `[data-testid="vol-quote"]` 那段删掉、只留 `.si-item` 结构探测，
    run.cjs 会继续绿灯（读到数字就算过），但已经退化成"改版就静默失守"的状态。
    """
    s = _read(RUN_CJS)
    # 成交量两格走 byTid("vol-quote"/"vol-kline") 的间接查询（选择器在 helper 里拼）
    assert 'byTid("vol-quote")' in s and 'byTid("vol-kline")' in s, \
        "e2e 没有按 testid 取成交量"
    assert 'data-testid="watch-row"' in s, "e2e 没有按 testid 找自选行"
    assert 'data-testid="stk-drawer"' in s, "e2e 没有按 testid 找个股抽屉"
    # 兜底必须仍然存在（否则服务端没更新时 e2e 直接崩，报错比缺陷更吵）
    assert ".si-item, .ks-grid > div" in s, "结构兜底被删了：老版本页面下 e2e 会直接报错而不是降级"
    assert "viaTestid" in s, "没有记录「这次是靠 testid 还是靠结构」，兜底命中会被静默吞掉"
    return True


# ---------------------------------------------------------------------------
# ⑨ 导航横条：折叠区里「主项 + 同 mg 子项」不得渲染两遍
# ---------------------------------------------------------------------------
def test_fold_area_does_not_render_lead_children_twice():
    """折叠区里子项只能被画一次：`emitted` 记账必须**声明在两层循环之外**，且两路共用。

    事故原型（e2e 抓到的真 FAIL，不是缺标识）：折叠区里 lead 的收拢循环先画了一遍同 mg 子项，
    外层循环随后又走到同一批子项 —— `if (i.child && !inFold) continue` 在 inFold=true 时根本不跳过，
    于是 `lowbox_bottom / lowbox_radar / sentiment / overseas_sentiment / geo_events` 各出现两份。
    用 DOM 枚举到 58 条、去重 53 个，用户看到两个一模一样入口，e2e「点第几个」还会指错元素。

    为什么断言"声明位置"而不只是"有没有"：把 `const emitted` 写进 `if (i.lead)` 分支里也能绿，
    但那样平铺区和折叠区各有一份集合，切换路径时旧账就丢了 —— 正是这个 bug 的形状。
    """
    src = _read(APP_JS)
    body = _fn_body(src, r"function paintSubnav\(")
    assert body.count("const emitted = new Set();") == 1, \
        "emitted 集合必须恰好声明一处（当前 %d）" % body.count("const emitted = new Set();")
    i_decl = body.index("const emitted = new Set();")
    i_loop = body.index("for (let k = 0; k < list.length; k++)")
    i_has = body.index("emitted.has(i.tab)")
    i_add = body.index("emitted.add(c.tab)")
    assert i_decl < i_loop, "emitted 必须声明在外层循环之前（否则两路各一份，切换路径就回潮）"
    assert i_loop < i_has, "外层循环里没有用 emitted.has 跳过已画过的子项"
    assert i_has < i_add, "先声明跳过、后记账的顺序反了（记账必须发生在收拢循环里）"
    stmt = "if (i.tab && !i.lead && emitted.has(i.tab)) continue;"
    assert body.count(stmt) == 1, "外层循环的跳过语句必须恰好一处：%s" % stmt
    # 平铺区的原有跳过逻辑不能顺手删掉：删了平铺区会开始重复画子项
    assert body.count("if (i.child && !inFold) continue;") == 1, \
        "平铺区的 `if (i.child && !inFold) continue;` 被改了 —— 平铺区会重新重复渲染子项"
    return True


# ---------------------------------------------------------------------------
# ⑩ 豆包×主力共振：null 不许漏成字面量 "null"
# ---------------------------------------------------------------------------
def test_resonance_row_never_prints_literal_null():
    """`合成-/null` 这种字面量必须彻底消失：后端把「未覆盖」显性化，前端按三态分别写。

    根因是两处配合失误：后端缺省值写成 `("-", None)`（"-" 是假哨兵），
    前端又用 `String(x.card_score)` 直接拼 ⇒ 渲染出 `合成-/null`。
    null 是"没算出来"，"-" 不是合法档位（res_stock_card 的 tier 只有 A/S/B/C），
    两者都必须显性写成「—」或「合成层未覆盖」，不能伪装成 0 也不能伪装成空。

    这里同时守两端：只改前端，别的调用方还会拿到 "-"；只改后端，前端还会把 null 拼出来。
    """
    js = _read(DOUYIN_JS)
    # 允许 `String(card_score)` 存在的**唯一**地点是 cardCell 的 null 三态里（有前置判空），
    # 别处出现就说明有人又在模板里直接拼了 —— 那正是 `合成-/null` 的来源。
    cell = _fn_body(js, r"function cardCell\(")
    rest = js.replace(cell, "")
    assert "String(x.card_score)" not in rest, "cardCell 之外又直接 String(card_score) 拼模板了（会渲染 null）"
    assert "String(x.score)" not in rest, "cardCell 之外又直接 String(score) 拼模板了（会渲染 null）"
    assert "String(x.card_score)" in cell, "cardCell 里没有分数出口了（三态被改回单一分支？）"
    assert "x.card_score == null" in cell, "cardCell 里分数没做 null 判空就直接 String()（会渲染 null）"
    assert "x.card_tier == null" in cell, "cardCell 里档位没做 null 判空（会渲染空档位）"
    assert "'合成' + esc(x.card_tier)" not in js, "合成层那一格又回去直接拼 tier 了"
    assert js.count("function cardCell(") == 1, "cardCell 必须恰好定义一处（三态的唯一出口）"
    assert js.count("var cc = cardCell(x)") == 1, "cardCell 必须恰好被调用一处（每行那一格）"
    assert "has_card" in js, "前端没有读后端的 has_card —— 分不清「维度不存在」和「算了没值」"
    assert '"合成层未覆盖"' in js, "没有「合成层未覆盖」这一档（会把「没这个维度」错写成 0 分）"
    assert "分' + nv(x.score)" in js, "评分格没用 nv() 兜底（score 为空会渲染 null）"
    assert "function nv(" in js, "nv() 兜底函数被删了"

    py = _read(SERVER_PY)
    assert 'card.get(code, ("-", None))' not in py, \
        "后端还在用 \"-\" 假哨兵表示「未覆盖」（前端无从区分）"
    assert 't, rs = card.get(code, (None, None))' in py, "后端缺省值应显式是 None，不是 \"-\""
    assert '"has_card": has_card' in py, "后端没有回吐 has_card（前端只能靠猜）"
    assert py.count('has_card = code in card') == 1, "has_card 的判定必须恰好一处（与 card.get 同源）"
    return True


# ---------------------------------------------------------------------------
# ⑪ 基金经理策略：iframe 面板不许留白板，也不许量错口径
# ---------------------------------------------------------------------------
def test_fundmasters_iframe_panel_is_probeable_and_never_blank():
    """外挂整页面板：要给可探活的标识，失败时必须**写出原因和下一步**，不能留一块纯色。

    e2e 报「本面板 innerText 为空」在父文档里是必然的 —— innerText/textContent 不跨子文档。
    真正的风险是 iframe 挂掉时用户看到白板，分不清"加载失败"和"本来就没数据"。
    所以要求：① 稳定标识（e2e 改探 contentDocument，别再量父文档文本）；
             ② 空态文案含原因与下一步；③ 就绪判定认 iframe（否则这面板永远没有 data-ready）。
    """
    src = _read(APP_JS)
    assert src.count('data-testid="fm-frame"') == 1, "fm-frame 标识必须恰好一处（e2e 靠它探子文档）"
    assert src.count('data-testid="fm-fallback"') == 1, "fm-fallback 必须恰好一处"
    assert src.count('data-panel-frame="fundmasters"') == 3, \
        "data-panel-frame 应恰好三处（iframe 自身 + 幂等判重 + 取句柄），当前 %d" % src.count('data-panel-frame="fundmasters"')

    body = _fn_body(src, r"function loadFundMasters\(")
    assert 'box.querySelector(\'iframe[data-panel-frame="fundmasters"]\')) return;' in body, \
        "loadFundMasters 缺幂等判重：每次切回本 tab 都会重建 iframe（页面内筛选状态全丢）"
    assert "contentDocument" in body, "没有探测子文档正文，改不了「白板」只能靠猜"
    assert "showFallback" in body and 'data-why' in body, "空态没有带原因（用户无法区分失败与无数据）"
    assert "markPanelReady(box)" in body, "加载成功后没有给面板打就绪标记"
    # 空态文案必须同时给出「原因」和「下一步」，只有一句"暂无数据"不算。
    # 文案在同文件的 FM_FALLBACK_HTML 常量里（模块级，便于一次性看清），单独切出来断言。
    m = re.search(r"const FM_FALLBACK_HTML = ''(.*?);\n", src, re.S)
    assert m, "FM_FALLBACK_HTML 常量不见了（空态文案被挪走或删了？）"
    copy = m.group(1)
    for kw in ("可能原因", "下一步", "在新窗口打开"):
        assert kw in copy, "空态文案缺「%s」—— 用户仍不知道发生了什么、该怎么办" % kw

    # 就绪判定必须认 iframe，且仍然只有 markPanelReady 一个设置点
    mbody = _fn_body(src, r"function markPanelReady\(")
    assert 'iframe[data-panel-frame]' in mbody, "markPanelReady 没认 iframe，本面板永远拿不到 data-ready"
    assert 'dataset.loaded === "1"' in mbody, "markPanelReady 判 iframe 就绪没用 data-loaded 标记"
    assert _count(src, 'setAttribute("data-ready", "1")') == 1, \
        "data-ready 设置点必须仍是一处（别在 loadFundMasters 里又手写一行）"
    return True


# ---------------------------------------------------------------------------
# ⑫ 启动顺序：首屏渲染必须等到所有同步 <script> 解析完
# ---------------------------------------------------------------------------
def test_app_boot_waits_for_all_later_scripts():
    """`initNav/loadAll` 不许在 app.js **被解析时**就执行。

    事故原型（2026-11-01）：app.js 的启动段写在文件末尾，解析到那行就执行 —— 但
    `douyin_corpus.js / funnel.js / industry_leaders.js / bottleneck.js / screener_extra.js`
    都是排在 app.js **之后**的 <script>，此刻 `window.DOUYIN` 还是 undefined。
    `loadTab` 里 `if (tab === "dy_reso" && window.DOUYIN)` 这类守卫于是**静默跳过**：
    深链接（或首屏）直接落在这些面板上 → 永远停在「正在加载数据…」；点别的 tab 再点回来就正常。
    这就是「没有报错的故障」的典型 —— 浏览器不报错、接口不 404、面板就是空的。

    为什么这样断言（而不是"包含 bootViewer 字样"这种松守卫）：
      ① 顶层调用必须是**零**个（按行首匹配：缩进为 0 的 `initNav();`/`loadAll();` 就是解析期执行）；
      ② 真正的调用点必须落在 `bootViewer()` 函数体里；
      ③ `bootViewer` 必须只能从 DOMContentLoaded 进（外加 readyState 兜底那一条）。

    这三条一起才等价于"顺序不再是变量"。只断言 ③ 也能被"注册了却另外还顶层跑一遍"绕过。
    """
    src = _read(APP_JS)
    # ① 行首（无缩进）= 顶层语句 = 解析期执行。一个都不许有。
    for stmt in ("initNav();", "loadAll();", "bootViewer();"):
        top = re.findall(r"(?m)^%s" % re.escape(stmt), src)
        assert not top, ("`%s` 出现在**文件顶层**（行首无缩进）—— 它会在 app.js 解析时立刻执行，"
                         "而排在 app.js 之后的脚本此刻还没解析（window.DOUYIN / FUNNEL 都是 undefined），"
                         "深链接落在那些面板上就永远停在「正在加载数据…」" % stmt)
    # ② 调用点必须在 bootViewer 里
    body = _fn_body(src, r"function bootViewer\(\)")
    assert "initNav();" in body, "bootViewer 里没有调 initNav（导航不建就不出首屏面板）"
    assert "loadAll();" in body, "bootViewer 里没有调 loadAll"
    assert src.count("function bootViewer(") == 1, "bootViewer 必须恰好定义一处"
    # ③ 只能从 DOMContentLoaded 进（+ readyState 兜底）
    assert src.count('document.addEventListener("DOMContentLoaded", bootViewer') == 1, \
        "bootViewer 必须恰好挂在 DOMContentLoaded 上一次（多挂一次会重复建导航）"
    i_def = src.index("function bootViewer(")
    i_reg = src.index('document.addEventListener("DOMContentLoaded", bootViewer')
    assert i_reg > i_def, "DOMContentLoaded 的注册写在 bootViewer 定义之前，会引用到未定义变量"
    assert "document.readyState" in src[i_reg - 400:i_reg + 400], \
        "缺少 readyState 兜底：脚本被动态插入已解析完的文档时（readyState≠loading）永远不启动"
    return True


def test_index_html_scripts_after_app_js_are_synchronous_and_ordered():
    """index.html 里排在 app.js 之后的脚本必须是**同步按序**执行的，不许 `async`。

    这是上一条的另一半：boot 推迟到 DOMContentLoaded 之所以成立，前提是"所有同步 <script>
    在 DOMContentLoaded 之前执行完"。一旦有人给后面某个脚本加 `async`，它可能晚于
    DOMContentLoaded 才执行 —— boot 就会在它之前跑，症状与那只老 bug 一模一样。
    `defer` 是允许的（defer 之间仍按文档顺序、且在 DOMContentLoaded 之前执行）。
    """
    html = _read(INDEX_HTML)
    tags = re.findall(r"<script\b[^>]*\bsrc\s*=\s*\"([^\"]+)\"[^>]*>", html)
    assert tags, "index.html 里一个外链 <script> 都没有？解析方式变了就得改这条"
    app_idx = None
    for i, s in enumerate(tags):
        if os.path.basename(s.split("?")[0]) == "app.js":
            app_idx = i
            break
    assert app_idx is not None, "index.html 里找不到 app.js 的 <script src>"
    later = tags[app_idx + 1:]
    assert later, ("app.js 之后已经没有脚本了 —— 如果确实如此，这条守卫的前提消失，"
                   "请连同 test_app_boot_waits_for_all_later_scripts 一起重新论证，别直接删")
    # 逐个检查 app.js 之后那几条 <script> 的属性
    for s in later:
        name = os.path.basename(s.split("?")[0])
        m = re.search(r"<script\b[^>]*%s[^>]*>" % re.escape(s), html)
        assert m, "解析不出 %s 的 <script> 标签" % s
        tag = m.group(0)
        assert " async" not in tag, ("%s 带 async：它会晚于 DOMContentLoaded 执行，"
                                     "boot 会在它之前跑，模块又是 undefined" % name)
    return True


if __name__ == "__main__":
    import sys
    tests = [(k, v) for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    fails = []
    for name, fn in tests:
        try:
            fn()
            print("PASS " + name)
        except AssertionError as e:
            print("FAIL " + name + " :: " + str(e))
            fails.append(name)
        except Exception as e:  # noqa: BLE001
            print("ERROR " + name + " :: " + repr(e))
            fails.append(name + "(error)")
    print("\n==== %d/%d PASS ====" % (len(tests) - len(fails), len(tests)))
    sys.exit(1 if fails else 0)
