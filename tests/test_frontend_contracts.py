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
             test_no_unbounded_date_chip_row]
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
