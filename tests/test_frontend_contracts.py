# -*- coding: utf-8 -*-
"""前端契约测试（规格书 A6 / D2）——纯静态解析 app.js，无需起服务。

A6：缺失值必须走统一出口 renderMissing()（而不是留空白或写成 0）；
D2：后端恢复后必须撤掉断连横幅并提示。
"""
from __future__ import annotations

import io
import re
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP_JS = os.path.join(ROOT, "viewer", "static", "app.js")
APP_CSS = os.path.join(ROOT, "viewer", "static", "app.css")


def _read(p: str) -> str:
    return io.open(p, encoding="utf-8").read()


def test_missing_helpers_exist():
    """A6：缺失值统一出口必须存在。"""
    js = _read(APP_JS)
    for fn in ("function renderMissing(", "function missText(", "function isMissing(",
               "function numOr("):
        assert fn in js, "app.js 缺少 %s（A6 缺失值统一出口）" % fn
    return True


def test_mdtable_uses_render_missing():
    """A6：表格数值列的空单元格必须走 renderMissing，不得留空白。"""
    js = _read(APP_JS)
    i = js.find("function mdTable(")
    assert i > 0, "找不到 mdTable"
    seg = js[i:i + 1200]
    assert "renderMissing(" in seg, "mdTable 未使用 renderMissing 渲染缺失数值单元格"
    return True


def test_missing_css_present():
    """A6：.miss 样式必须存在（否则缺失标记与普通文字无法区分）。"""
    css = _read(APP_CSS)
    assert "span.miss{" in css.replace(" ", ""), "app.css 缺少 span.miss 样式"
    return True


def test_service_recovery_notice_wired():
    """D2：后端恢复路径必须接线（撤横幅 + 提示）。"""
    js = _read(APP_JS)
    assert "function markServiceHealthy(" in js, "app.js 缺少 markServiceHealthy（D2 恢复处理）"
    assert "markServiceHealthy()" in js.replace("function markServiceHealthy()", ""), \
        "markServiceHealthy 定义了但从未被调用（D2 恢复提示不会触发）"
    assert "function showToast(" in js, "app.js 缺少 showToast"
    return True


def test_drop_banner_sets_flag():
    """D2：断连横幅出现时必须置位 _dropBannerShown，否则恢复提示永远不触发。"""
    js = _read(APP_JS)
    i = js.find("function netDropBanner(")
    assert i > 0
    seg = js[i:i + 400]
    assert "_dropBannerShown = true" in seg, "netDropBanner 未置位 _dropBannerShown"
    return True


def test_day_chips_capped_and_queryable():
    """交易日/报告期横排：必须限量 + 提供查询入口（进哥 2026-10-12：无限铺开会拐行）。

    回归点：曾经龙虎榜/板块动向是 `(d.dates || []).map(...)`、牛散动向是 `rows.map(...)`
    全量铺开 —— 日子一多就换行。
    """
    js = _read(APP_JS)
    assert "function dayChips(" in js, "app.js 缺少 dayChips（日期横排限量渲染）"
    assert "DAY_CHIP_LIMIT = 10" in js, "DAY_CHIP_LIMIT 不是 10"
    # 三处日期选择器都必须改用 dayChips
    for attr in ("data-lhb-date", "data-lhs-date", "data-sm-date"):
        assert '"%s"' % attr in js, "找不到 %s 选择器" % attr
        idx = js.find('dayChips(')
        assert attr in js, "%s 未被使用" % attr
        # 旧的裸 map 写法不得残留（用属性名 + chip-s 组合定位）
        assert ('class="chip-s" role="button" %s=' % attr) not in js, \
            "%s 仍是全量铺开的旧写法（未走 dayChips）" % attr
    # 三处都必须有日期查询框接线
    assert js.count(".day-jump") >= 3, "日期查询框未在全部三处接线（当前 %d）" % js.count(".day-jump")
    return True


def test_kline_full_history_reachable():
    """K 线必须能看到「上市至今」全历史（2026-10-12 进哥要求）。

    回归点：滚轮缩放的上限曾被写成「当前已加载根数」，而首屏默认只拉 250 根，
    于是日K 缩到底也只有约 1 年、周K 约 5 年 —— 怎么缩都够不到上市首日。
    修法：① 工具条「全历史」按钮；② 缩到底自动拉全量并全览；③ 切周期保持全览。
    """
    js = _read(APP_JS)
    # ① 一键入口
    assert 'id="klineFull"' in js, "工具条缺少「全历史」按钮（#klineFull）"
    assert "function klineShowAll(" in js, "app.js 缺少 klineShowAll（全览函数）"
    assert "kf.onclick = () => klineShowAll()" in js, "「全历史」按钮未接线"
    # ② 滚轮缩到底 → 自动拉全量
    assert "e.deltaY > 0 && _kv.count >= _kv.rows.length && _kv.hasMore" in js, \
        "滚轮缩到底未接「自动拉全历史」"
    assert "all: true" in js, "未以 { all: true } 请求全量"
    # ③ 全览 = 铺满全部且从第 0 根起
    assert "count = rows.length;" in js and "offset = 0;" in js, "全览视口未铺满全部"
    # ④ 切周期保持全览意图
    assert "const wantAll = !!o.all || !!(_kv && _kv.all);" in js, "切周期未继承「全览」意图"
    # ⑤ 文案诚实：has_more 时不得谎称「已到最早一根」
    assert "已到本次加载的最早" in js, "缺少 has_more 时的准确提示"
    assert "已显示全部" in js, "全览后未标注「已显示全部」"
    return True


def test_kline_toolbar_no_squeeze():
    """K 线工具条：周期按钮不得被 range 长文本压扁换行（2026-10-12 实测踩过）。"""
    css = _read(APP_CSS)
    assert re.search(r"\.kline-periods\s*\{[^}]*flex:\s*0\s+0\s+auto", css), \
        "周期按钮未设为不可压缩（会被 range 长文本压成两行）"
    assert re.search(r"\.kbtn\s*\{[^}]*white-space:\s*nowrap", css), "周期按钮文字未禁止换行"
    assert re.search(r"\.kline-bar\s*\{[^}]*flex-wrap:\s*wrap", css), "工具条未允许整体换行"
    return True


def test_no_unbounded_date_chip_row():
    """通用防回退：不得再出现「日期数组直接 map 成 chip 行」的写法。"""
    js = _read(APP_JS)
    bad = re.findall(r"\((?:d|it|r)?\.?(?:dates|periods|snap_dates)\s*\|\|\s*\[\]\)\.map\(\s*\([^)]*\)\s*=>\s*[`\"']<span class=\"chip-s\"", js)
    assert not bad, "发现未限量的日期 chip 行：%s" % bad
    return True


if __name__ == "__main__":
    import sys

    tests = [test_missing_helpers_exist, test_mdtable_uses_render_missing,
             test_missing_css_present, test_service_recovery_notice_wired,
             test_drop_banner_sets_flag, test_day_chips_capped_and_queryable,
             test_no_unbounded_date_chip_row,
             test_kline_full_history_reachable, test_kline_toolbar_no_squeeze]
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
