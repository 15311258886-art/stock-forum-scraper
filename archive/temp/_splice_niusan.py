# -*- coding: utf-8 -*-
"""把「牛散追踪」接进查看器前端：NAV 项 + 面板 + 状态/加载/渲染 + 按需渲染分支。
   顺手修 renderLhbSectors 的面板 id 拼写 bug（p-lhb_sector -> p-lhb_sectors）。"""
import os, io, sys

BASE = r"F:\AI Agent部署知识库\2026-09-22-11-49-42\stock-forum-scraper"
JS = os.path.join(BASE, "viewer", "static", "app.js")
HTML = os.path.join(BASE, "viewer", "static", "index.html")

def rd(p):
    return io.open(p, encoding="utf-8").read()

def wr(p, t):
    with io.open(p, "w", encoding="utf-8", newline="\n") as f:
        f.write(t)

js = rd(JS)
h = rd(HTML)
report = []

# ---------- 1) 修 renderLhbSectors 的面板 id 拼写 ----------
old_id = '$("#p-lhb_sector")'
if old_id in js:
    js = js.replace(old_id, '$("#p-lhb_sectors")')
    report.append("FIX  p-lhb_sector -> p-lhb_sectors : 1 处")
else:
    report.append("SKIP p-lhb_sector 已是正确 id（或未找到）")

# ---------- 2) NAV 加「牛散追踪」----------
nav_old = '    { tab: "lhb_sectors", label: "板块动向" },\n'
nav_new = nav_old + '    { tab: "niusan", label: "牛散追踪" },\n'
if 'tab: "niusan"' in js:
    report.append("SKIP NAV 已含 niusan")
elif nav_old in js:
    js = js.replace(nav_old, nav_new, 1)
    report.append("ADD  NAV 牛散追踪 : 1 处")
else:
    report.append("FAIL NAV 锚点未命中"); sys.exit(1)

# ---------- 3) 插入 NIUSAN 模块（放在 LBHS 定义之前）----------
anchor = "const LBHS = { data: null };"
BLOCK = '''/* ================= 牛散追踪 · 股东协同 =================
   对标「看究竟·股东协同」/ Dataroma 共识持仓。
   数据源：上市公司定期报告/临时公告披露的自然人十大股东（东财 F10），
   由 holders_fetch.py 抓取后落库（stock_holders）。本模块只读、不重算。
   两个视角：① 牛散榜（跨多只股建仓的自然人）② 协同股（多名牛散同持一只股）。 */
const NIUSAN = { data: null, scope: "free", minHold: 2 };
function loadNiusan() {
  const q = "?scope=" + encodeURIComponent(NIUSAN.scope) + "&min_hold=" + NIUSAN.minHold;
  return api("/api/niusan" + q).then((d) => { NIUSAN.data = d; renderNiusan(); });
}
function renderNiusan() {
  const box = $("#p-niusan");
  if (!box) return;
  const d = NIUSAN.data;
  if (!d) { box.innerHTML = `<div class="empty">牛散数据加载中…</div>`; return; }
  const s = d.summary || {};
  if (!(d.investors || []).length && !(d.cohold || []).length) {
    box.innerHTML = `<div class="empty">暂无股东数据。该板块由 holders_fetch.py 抓取上市公司披露的自然人股东后生成。</div>`;
    return;
  }
  const seg = (v, t) => `<span class="chip-s" role="button" data-ns-scope="${escAttr(v)}" ` +
    `style="cursor:pointer;${v === NIUSAN.scope ? "background:var(--accent,#0E7490);color:#fff;" : ""}">${esc(t)}</span>`;
  const inv = (d.investors || []).map((p) => {
    const holds = (p.holdings || []).map((x) => {
      const tip = (x.symbol_name || "") + " " + x.symbol +
        " | 占流通 " + (x.ratio == null ? "—" : x.ratio + "%") +
        " | " + (x.change || "变动未披露") + " | 披露期 " + (x.end_date || "—");
      return `<span class="kline-chip chip-s" data-sym="${escAttr(x.symbol)}" style="margin:1px 3px 1px 0" ` +
        `title="${escAttr(tip)} 点击看详情">${esc(x.symbol_name || x.symbol)}</span>`;
    }).join("");
    return `<tr>
      <td><b>${esc(p.name)}</b></td>
      <td class="num"><b>${p.stock_count}</b> 只</td>
      <td class="num">${(p.total_ratio || 0).toFixed(2)}%</td>
      <td class="num">${p.new_cnt ? `<span class="bull">${p.new_cnt}</span>` : "—"}</td>
      <td class="num">${p.cut_cnt ? `<span class="bear">${p.cut_cnt}</span>` : "—"}</td>
      <td>${holds || "—"}</td>
    </tr>`;
  }).join("");
  const coh = (d.cohold || []).map((c) => {
    const ps = (c.people || []).map((x) =>
      `<span class="chip-s" style="margin:1px 3px 1px 0">${esc(x.name)}` +
      `${x.ratio == null ? "" : " " + x.ratio.toFixed(2) + "%"}</span>`).join("");
    return `<tr>
      <td>${lhbSymChip(c.symbol)}</td>
      <td><b>${esc(c.symbol_name || "")}</b></td>
      <td class="num"><b class="bull">${c.people_count}</b> 位</td>
      <td class="num">${(c.star_ratio_sum || 0).toFixed(2)}%</td>
      <td>${ps}</td>
    </tr>`;
  }).join("");
  box.innerHTML = `
    <div class="hint" style="margin-bottom:10px">
      数据源：上市公司<b>定期报告 / 临时公告</b>披露的自然人十大股东（东财 F10）。
      当前口径 <b>${esc(s.scope_label || "")}</b> · 最新披露期 <b>${esc(s.latest_date || "—")}</b> ·
      自然人 <b>${s.person_total || 0}</b> 名 · 覆盖 <b>${s.stock_total || 0}</b> 只股。
      <b>披露有时滞</b>（季报约 1 个月、临时公告约 1-2 日），仅作方向参考，不构成交易信号。
      <br>口径切换：${seg("free", "十大流通股东")} ${seg("total", "十大股东")}
    </div>
    <div class="detail-sub">① 牛散榜 —— 跨 ≥${s.min_hold || 2} 只股的自然人（按持股只数排序；点击股票名看详情）</div>
    ${inv ? `<table><thead><tr><th>牛散</th><th class="num">持股数</th><th class="num">合计占流通</th>
      <th class="num">新进</th><th class="num">减持</th><th>持仓明细</th></tr></thead><tbody>${inv}</tbody></table>`
      : "<span class='s'>暂无符合条件的牛散</span>"}
    <div class="detail-sub">② 股东协同 —— 多名<b>牛散</b>同时持有同一只股（已剔除家族 / 创始团队持股）</div>
    ${coh ? `<table><thead><tr><th>代码</th><th>名称</th><th class="num">牛散数</th>
      <th class="num">合计占流通</th><th>牛散名单</th></tr></thead><tbody>${coh}</tbody></table>`
      : "<span class='s'>暂无牛散协同持仓</span>"}
    <div class="ks-src" style="margin-top:10px">${esc(d.basis || "")}</div>`;
  $$("#p-niusan [data-ns-scope]").forEach((el) => {
    el.onclick = () => {
      NIUSAN.scope = el.dataset.nsScope;
      loadNiusan().catch((e) => reportLoadError("牛散追踪", e));
    };
  });
}
'''
if "function renderNiusan" in js:
    report.append("SKIP NIUSAN 模块已存在")
elif anchor in js:
    js = js.replace(anchor, BLOCK + anchor, 1)
    report.append("ADD  NIUSAN 模块（state+load+render）: 1 处")
else:
    report.append("FAIL NIUSAN 锚点未命中"); sys.exit(1)

# ---------- 4) 按需渲染分支 ----------
sched_old = '''    else loadLhbSectors().catch((e) => reportLoadError("板块动向", e));
  }
}'''
sched_new = '''    else loadLhbSectors().catch((e) => reportLoadError("板块动向", e));
  }
  // 牛散追踪：进入时按需（重）渲染
  if (tab === "niusan") {
    if (NIUSAN.data) renderNiusan();
    else loadNiusan().catch((e) => reportLoadError("牛散追踪", e));
  }
}'''
if 'tab === "niusan"' in js:
    report.append("SKIP 按需渲染已含 niusan")
elif sched_old in js:
    js = js.replace(sched_old, sched_new, 1)
    report.append("ADD  按需渲染分支 niusan : 1 处")
else:
    report.append("FAIL 按需渲染锚点未命中"); sys.exit(1)

wr(JS, js)

# ---------- 5) index.html 加面板 ----------
panel_anchor = '<div class="panel" id="p-lhb_sectors"></div>'
panel_new = panel_anchor + '\n      <div class="panel" id="p-niusan"></div>'
if 'id="p-niusan"' in h:
    report.append("SKIP index 面板已存在")
elif panel_anchor in h:
    h = h.replace(panel_anchor, panel_new, 1)
    report.append("ADD  index 面板 p-niusan : 1 处")
else:
    report.append("FAIL index 面板锚点未命中"); sys.exit(1)

wr(HTML, h)

print("===== splice 结果 =====")
for r in report:
    print("  " + r)
print()
print("app.js 行数:", len(rd(JS).splitlines()))
