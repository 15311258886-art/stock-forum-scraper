# -*- coding: utf-8 -*-
"""tests/test_ann_event_panel.py —— 「公告实证」面板 + VIZ 图表基元 回归

为什么需要这个测试：
  2026-10-31 做这个面板时，同一个 bug 犯了两次 —— 前端已经有 P()（自带 % 的格式化函数），
  调用处又手写了一个 '%'，渲染出来是 "-1.95%%"。第一次靠肉眼看截图发现，改完第二次
  又犯在另一处。纯靠人工目检不可靠，所以把两类检查固化下来：

  A. 源码静态检查（不需要跑 JS、不需要 node）：
     · P(...) / VIZ.pct(...) 后面不许紧跟字面量 '%'（会造成双百分号）
     · ann_event.js 不许写死 #hex 色值（必须走主题变量，否则浅色主题会瞎）

  B. 数据一致性检查：
     · ann_event_data.js 能被解析，行数 = 类别数 × 3
     · 每行 sig 必须严格等于 |t| >= 3.0（判据单一真源）
     · 数据文件与 research/out/announcement_event_study.md **同源**：
       重跑 gen_ann_event_js.py 后内容必须一致，否则说明有人手改了数据文件
     · meta 里的 tests / sig_n 与实际行数自洽

纪律：不联网、不读生产库、不写任何文件（重生成在内存里比对）。
"""
import io
import json
import os
import re
import sys

import pytest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATIC = os.path.join(BASE, "viewer", "static")
DATA_JS = os.path.join(STATIC, "ann_event_data.js")
PANEL_JS = os.path.join(STATIC, "ann_event.js")
VIZ_JS = os.path.join(STATIC, "viz.js")
MD = os.path.join(BASE, "research", "out", "announcement_event_study.md")
GEN = os.path.join(BASE, "research", "gen_ann_event_js.py")


# ──────────────────────────────────────────────────────────────────────
# 辅助：从 "window.ANN_EV = {...};" 里抠出 JSON
# ──────────────────────────────────────────────────────────────────────
def _load_payload():
    raw = io.open(DATA_JS, encoding="utf-8").read()
    start = raw.index("{")
    end = raw.rindex("}")
    return json.loads(raw[start:end + 1])


def _read(p):
    return io.open(p, encoding="utf-8").read()


requires_data = pytest.mark.skipif(
    not os.path.exists(DATA_JS), reason="ann_event_data.js 未生成"
)


# ──────────────────────────────────────────────────────────────────────
# A. 源码静态检查
# ──────────────────────────────────────────────────────────────────────
# 匹配 `P(xxx)` / `VIZ.pct(xxx)` 后紧跟 ` + '%` 或 ` + "%` 的写法。
# 用非贪婪 + 限定长度，避免跨行误伤；P() 的参数里可能嵌一层括号，故允许一层嵌套。
_DOUBLE_PCT = re.compile(
    r"""(?:\bP|\bVIZ\.pct)\(\s*(?:[^()]|\([^()]*\))*\)\s*\+\s*['"]%"""
)


@pytest.mark.parametrize("path", [PANEL_JS, VIZ_JS])
def test_no_double_percent_literal(path):
    """P() 已自带 %，调用处不许再拼一个字面量 % （否则渲染成 -1.95%%）。"""
    src = _read(path)
    hits = _DOUBLE_PCT.findall(src)
    assert not hits, "发现双百分号写法 %d 处：%r" % (len(hits), hits[:5])


@pytest.mark.parametrize("path", [PANEL_JS, VIZ_JS])
def test_no_hardcoded_hex(path):
    """图表配色必须走 CSS 变量，写死 #hex 会让浅色主题不可读。

    例外：viz.js / ann_event.js 里的 var(--xxx, #fallback) 兜底值是**允许**的 ——
    那是 CSS 变量缺失时的降级，不是硬编码。所以先剥掉 var(...) 再查。
    """
    src = _read(path)
    # 去掉注释，避免注释里的色值误报
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    # 剥掉 var(--name, #fallback) 里的兜底色
    stripped = re.sub(r"var\([^()]*(?:\([^()]*\))?[^()]*\)", "var()", src)
    hexes = re.findall(r"#[0-9a-fA-F]{3,8}\b", stripped)
    # 允许 3 位/6 位纯数字以外的极少数（如 #fff 用于 final fallback 已在 var() 内）
    assert not hexes, "发现未经 var() 兜底的硬编码色值：%r" % (hexes[:8],)


def test_panel_registered_in_index_html():
    """新面板必须同时具备：容器 div + 三个脚本 + 样式表，缺一个就是白屏。"""
    html = _read(os.path.join(STATIC, "index.html"))
    assert 'id="p-ann_event"' in html, "index.html 缺少面板容器 #p-ann_event"
    for f in ("static/viz.js", "static/ann_event_data.js", "static/ann_event.js"):
        assert f in html, "index.html 缺少脚本 %s" % f
    assert "static/ann_event.css" in html, "index.html 缺少样式表 ann_event.css"
    # 加载顺序：viz.js（提供 window.VIZ）必须在 ann_event.js 之前
    assert html.index("static/viz.js") < html.index("static/ann_event.js")
    # 且两者都必须排在 app.js **之前** —— app.js 顶层就执行 initNav()，
    # 深链接会当场调 selectNav → ANN.load()；排在后面会落到「脚本未加载」兜底提示。
    assert html.index("static/ann_event.js") < html.index("static/app.js"), (
        "ann_event.js 必须排在 app.js 之前，否则深链接进入面板时 window.ANN 还不存在"
    )


def test_nav_entry_exists():
    """导航里必须真的挂上，否则面板存在但用户点不到。"""
    app = _read(os.path.join(STATIC, "app.js"))
    assert '{ tab: "ann_event"' in app, "app.js 的 NAV 里没有 ann_event 入口"
    assert 'if (tab === "ann_event")' in app, "app.js 的 selectNav 没有 ann_event 懒加载分支"
    assert "window.ANN.load()" in app, "selectNav 未调用 ANN.load()"


# ──────────────────────────────────────────────────────────────────────
# B. 数据一致性
# ──────────────────────────────────────────────────────────────────────
@requires_data
def test_payload_shape():
    d = _load_payload()
    assert d["cats"], "类别列表为空"
    rows = d["rows"]
    assert len(rows) == len(d["cats"]) * 3, (
        "行数 %d != 类别数 %d × 3" % (len(rows), len(d["cats"]))
    )
    hs = sorted({r["h"] for r in rows})
    assert hs == [1, 5, 20], "持有期应为 [1,5,20]，实际 %r" % (hs,)


@requires_data
def test_sig_flag_matches_criterion():
    """sig 必须是 |t| >= criterion 的**唯一**派生，不许手工标。"""
    d = _load_payload()
    crit = d["meta"]["criterion"]
    for r in d["rows"]:
        want = abs(r["t"]) >= crit
        assert r["sig"] == want, (
            "%s T+%d: t=%s 应为 sig=%s，实际 %s" % (r["cat"], r["h"], r["t"], want, r["sig"])
        )


@requires_data
def test_meta_self_consistent():
    d = _load_payload()
    m = d["meta"]
    rows = d["rows"]
    assert m["tests"] == len(rows), "meta.tests=%d 与实际行数 %d 不符" % (m["tests"], len(rows))
    assert m["sig_n"] == sum(1 for r in rows if r["sig"]), "meta.sig_n 与实际显著条数不符"
    # 边缘区（3.0 ~ strict_t）必须是 sig=True 且 |t| < strict_t
    n_band = len(m.get("borderline") or [])
    real_band = sum(1 for r in rows if m["criterion"] <= abs(r["t"]) < m["strict_t"])
    assert n_band == real_band, "borderline 条数 %d 与实际 %d 不符" % (n_band, real_band)


@requires_data
def test_data_file_is_generated_not_hand_edited():
    """数据文件必须能被 md 重新生成出**完全相同**的内容。

    这是防手改：有人直接改 ann_event_data.js 里的数字，md 就成了假真源。
    重生成在临时目录做（gen 脚本支持改输出路径不现实，故用内存比对 md→rows）。
    """
    sys.path.insert(0, os.path.join(BASE, "research"))
    import gen_ann_event_js as gen  # noqa: E402

    md = _read(MD)
    from_md = []
    for line in md.splitlines():
        mm = gen.ROW_RE.match(line.strip())
        if mm:
            from_md.append(mm.groupdict())
    js = _load_payload()
    assert len(from_md) == len(js["rows"]), (
        "md 解析出 %d 行，数据文件有 %d 行 —— 数据文件被手改或 md 已更新未重生成"
        % (len(from_md), len(js["rows"]))
    )
    for a, b in zip(from_md, js["rows"]):
        assert a["cat"].strip() == b["cat"]
        assert int(a["h"]) == b["h"]
        assert abs(float(a["ex"]) - b["ex"]) < 1e-9
        assert abs(float(a["t"]) - b["t"]) < 1e-9
        assert int(a["n"]) == b["n"]


@requires_data
def test_every_category_has_tone():
    """语义分组缺项会静默落到 neutral，展开口径就错了 —— 这里显式拦住。"""
    sys.path.insert(0, os.path.join(BASE, "research"))
    import gen_ann_event_js as gen  # noqa: E402

    d = _load_payload()
    missing = [c for c in d["cats"] if c not in gen.TONE]
    assert not missing, "以下类别没配语义分组（会按 neutral 处理）：%r" % (missing,)
    for r in d["rows"]:
        assert r["tone"] in ("bad", "good", "neutral"), r
