# -*- coding: utf-8 -*-
"""前端契约测试（规格书 A6 / D2）——纯静态解析 app.js，无需起服务。

A6：缺失值必须走统一出口 renderMissing()（而不是留空白或写成 0）；
D2：后端恢复后必须撤掉断连横幅并提示。
"""
from __future__ import annotations

import io
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


if __name__ == "__main__":
    import sys

    tests = [test_missing_helpers_exist, test_mdtable_uses_render_missing,
             test_missing_css_present, test_service_recovery_notice_wired,
             test_drop_banner_sets_flag]
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
