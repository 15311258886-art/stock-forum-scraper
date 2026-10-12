# -*- coding: utf-8 -*-
"""「今日看板」前端接线：导航 + 面板 + 加载 + 渲染"""
import os, io, shutil, datetime

BASE = r'F:\AI Agent部署知识库\2026-09-22-11-49-42\stock-forum-scraper'
os.chdir(BASE)
JS = 'viewer/static/app.js'
HTML = 'viewer/static/index.html'

buf = []
def P(*a): buf.append(' '.join(str(x) for x in a))

# ---------- 0) 备份 ----------
ts = datetime.datetime.now().strftime('%Y%m%d-%H%M%S')
for f in (JS, HTML):
    shutil.copyfile(f, f + '.bak.dashboard-' + ts)
P('[备份] %s / %s -> *.bak.dashboard-%s' % (JS, HTML, ts))

js = io.open(JS, encoding='utf-8').read()
html = io.open(HTML, encoding='utf-8').read()

# ---------- 1) NAV：把「今日看板」设为总览首项（=默认落地页） ----------
old_nav = '''  { key: "overview", label: "总览", icon: "◎", items: [
    { tab: "summary", label: "汇总" }] },'''
new_nav = '''  { key: "overview", label: "总览", icon: "◎", items: [
    { tab: "dashboard", label: "今日看板" },
    { tab: "summary", label: "汇总" }] },'''
assert js.count(old_nav) == 1, 'NAV 锚点命中 %d 次' % js.count(old_nav)
js = js.replace(old_nav, new_nav)
P('[1] NAV 已加「今日看板」并置于总览首项')

# ---------- 2) index.html：补面板 ----------
old_panel = '      <div class="panel" id="p-stocks"></div>'
new_panel = ('      <div class="panel" id="p-dashboard"></div>\n'
             '      <div class="panel" id="p-stocks"></div>')
assert html.count(old_panel) == 1, 'index 面板锚点命中 %d 次' % html.count(old_panel)
html = html.replace(old_panel, new_panel)
P('[2] index.html 已补 <div id="p-dashboard">')

# ---------- 3) loadAll：并行拉看板 ----------
old_loadall = '''function loadAll() {
  // 与各面板并行拉取刷新状态；renderX 尾部会调用 applyFreshness() 兜底重算时效标记。
  loadRefreshStatus().then(refreshAcquirePanels);'''
new_loadall = '''function loadAll() {
  // 与各面板并行拉取刷新状态；renderX 尾部会调用 applyFreshness() 兜底重算时效标记。
  loadRefreshStatus().then(refreshAcquirePanels);
  // 今日看板：决策入口，页面加载即拉（后端已做容错，单模块失败不拖垮整页）
  api("/api/dashboard").then((d) => { DASH.data = d; renderDashboard(); })
    .catch((e) => reportLoadError("今日看板", e));'''
assert js.count(old_loadall) == 1, 'loadAll 锚点命中 %d 次' % js.count(old_loadall)
js = js.replace(old_loadall, new_loadall)
P('[3] loadAll 已接入 /api/dashboard')

# ---------- 4) 按需渲染分支 ----------
old_cond = '''  // 知识获取三块：进入时按需渲染（archive 会打接口，故只在进入时拉）'''
new_cond = '''  // 今日看板：决策入口（默认落地页）——进入时按需（重）渲染，兜住"先拿数据、后面板才建"的时序
  if (tab === "dashboard") {
    if (DASH.data) renderDashboard();
    else loadDashboard().catch((e) => reportLoadError("今日看板", e));
  }
  // 知识获取三块：进入时按需渲染（archive 会打接口，故只在进入时拉）'''
assert js.count(old_cond) == 1, '按需渲染锚点命中 %d 次' % js.count(old_cond)
js = js.replace(old_cond, new_cond)
P('[4] selectNav 已加 dashboard 按需渲染分支')

# ---------- 5) 状态 + 加载 + 渲染 ----------
BLOCK = r'''
/* ---------- 今日看板（2026-09-25 新增）：决策入口 ----------
 * 组织维度是「今天该看什么」而非「数据从哪来」：
 *   ① 资金主线（龙虎榜最新日：个股净买 / 机构 / 营业部）
 *   ② 板块多点共振（同一天多个票上榜的题材方向 + T+1/T+3 验证）
 *   ③ 牛散共识（多位牛散同持的股）
 *   ④ 数据新鲜度（一眼看出哪条数据陈旧）
 */
const DASH = { data: null };

function loadDashboard() {
  return api("/api/dashboard").then((d) => { DASH.data = d; renderDashboard(); });
}

function renderDashboard() {
  const box = $("#p-dashboard");
  if (!box) return;
  const d = DASH.data;
  if (!d) { box.innerHTML = `<div class="empty">今日看板加载中…</div>`; return; }

  const yi = (v) => (v == null ? "—" : (Number(v) / 1e8).toFixed(2) + " 亿");
  const pctS = (v) => (v == null ? "—" : (Number(v) >= 0 ? "+" : "") + Number(v).toFixed(2) + "%");
  const symChip = (code, name) =>
    `<span class="kline-chip chip-s" data-sym="${escAttr(code)}" style="margin:1px 3px 1px 0"
       title="${escAttr((name || "") + " " + code)} 点击看详情">${esc(name || code)}</span>`;

  // ---- ① 龙虎榜 ----
  const lhb = d.lhb || {};
  const lhbRows = (lhb.top_stocks || []).map((s) => {
    const c = s.change_rate;
    const cls = c == null ? "" : (c >= 0 ? "bull" : "bear");
    return `<tr>
      <td>${symChip(s.symbol, s.name)}</td>
      <td class="num ${cls}">${pctS(c)}</td>
      <td class="num"><b>${yi(s.net_amt)}</b></td>
      <td class="s">${esc((s.reasons || []).join(" / ").replace(/日/g, ""))}</td>
    </tr>`;
  }).join("");
  const instRows = (lhb.inst_top || []).map((x) =>
    `<tr><td>${symChip(x.symbol, x.name)}</td>
      <td class="num"><b>${yi(x.net)}</b></td>
      <td class="num">${x.buy_times}/${x.sell_times}</td>
      <td class="num">${x.ratio == null ? "—" : Number(x.ratio).toFixed(2) + "%"}</td></tr>`).join("");
  const deptRows = (lhb.dept_top || []).map((x) =>
    `<tr><td class="s">${esc((x.name || "").slice(0, 22))}</td>
      <td class="num">${x.appear}</td>
      <td class="num">${x.stock_count}</td>
      <td class="num"><b>${yi(x.net)}</b></td></tr>`).join("");

  // ---- ② 板块多点共振 ----
  const sec = d.sectors || {};
  const secRows = (sec.rows || []).map((r) => {
    const ex = r.excess_t1;
    const cls = ex == null ? "" : (ex >= 0 ? "bull" : "bear");
    return `<tr>
      <td><b>${esc(r.sector)}</b></td>
      <td class="num"><b>${r.onlist_n}</b> 只</td>
      <td class="num">${yi(r.net)}</td>
      <td class="num">${pctS(r.t1_avg)}</td>
      <td class="num ${cls}">${ex == null ? "—" : (ex >= 0 ? "+" : "") + Number(ex).toFixed(2) + "%"}</td>
    </tr>`;
  }).join("");

  // ---- ③ 牛散共识 ----
  const con = d.consensus || {};
  const coholdRows = (con.rows || []).map((c) =>
    `<tr>
      <td>${symChip(c.symbol, c.symbol_name)}</td>
      <td class="num"><b>${c.people_count}</b> 位</td>
      <td class="s">${esc((c.people || []).map((p) => p.name).slice(0, 6).join("、"))}</td>
    </tr>`).join("");

  // ---- ④ 新鲜度 ----
  const fresh = (d.freshness || []).map((f) =>
    `<span class="chip-s" style="margin:2px 4px 2px 0">${esc(f.label)} <b>${esc(f.latest)}</b></span>`).join(" ");

  box.innerHTML = `
    <div class="hint" style="margin-bottom:12px">
      <b>今日看板</b> —— 一屏看完「今天什么值得看」。生成于 ${esc(d.generated_at || "—")}。
      板块与题材只作<b>方向参考</b>，龙虎榜个股多为游资/量化拉升，<b>不构成买卖建议</b>。
    </div>

    <div class="detail-sub" style="margin-top:6px">
      ① 资金主线 —— 龙虎榜最新交易日 <b>${esc(lhb.date || "—")}</b>
      （上榜 <b>${lhb.stock_count || 0}</b> 家 · 净买合计 <b>${yi(lhb.net_total)}</b>）
    </div>
    ${lhbRows ? `<table><thead><tr><th>个股</th><th class="num">涨跌</th><th class="num">龙虎榜净买</th><th>上榜原因</th></tr></thead><tbody>${lhbRows}</tbody></table>` : `<div class="empty">暂无龙虎榜数据</div>`}
    ${instRows ? `<div class="detail-sub">机构专用席位净买（Top）</div>
      <table><thead><tr><th>个股</th><th class="num">机构净买</th><th class="num">买/卖次数</th><th class="num">占成交比</th></tr></thead><tbody>${instRows}</tbody></table>` : ""}
    ${deptRows ? `<div class="detail-sub">活跃营业部（净买 Top）</div>
      <table><thead><tr><th>营业部</th><th class="num">上榜次数</th><th class="num">涉及股数</th><th class="num">净买</th></tr></thead><tbody>${deptRows}</tbody></table>` : ""}

    <div class="detail-sub">② 板块多点共振 —— ${esc(sec.date || "—")}
      （同一天多只票上榜的题材方向；T+1 超额 = 板块内未上榜股平均涨幅 − 大盘）
    </div>
    ${secRows ? `<table><thead><tr><th>板块</th><th class="num">上榜</th><th class="num">净买</th><th class="num">T+1 均涨</th><th class="num">T+1 超额</th></tr></thead><tbody>${secRows}</tbody></table>` : `<div class="empty">暂无板块动向数据</div>`}

    <div class="detail-sub">③ 牛散共识 —— 共 <b>${con.star_count == null ? "—" : con.star_count}</b> 位牛散（跨≥2只股票），多方同持的股：</div>
    ${coholdRows ? `<table><thead><tr><th>个股</th><th class="num">同持牛散</th><th>名单</th></tr></thead><tbody>${coholdRows}</tbody></table>` : `<div class="empty">暂无协同持股</div>`}

    <div class="detail-sub">④ 数据新鲜度（一眼看出哪条陈旧）</div>
    <div style="margin-bottom:16px">${fresh || "<span class='s'>—</span>"}</div>
  `;

  // 个股芯片 → 打开个股详情
  $$("#p-dashboard .kline-chip[data-sym]").forEach((el) => {
    el.onclick = () => openStock(el.dataset.sym, el);
  });
}

'''
anchor = 'function renderLhbSectors() {'
assert js.count(anchor) == 1, 'renderLhbSectors 锚点命中 %d 次' % js.count(anchor)
js = js.replace(anchor, BLOCK.lstrip('\n') + anchor)
P('[5] 已插入 DASH 状态 / loadDashboard / renderDashboard')

io.open(JS, 'w', encoding='utf-8').write(js)
io.open(HTML, 'w', encoding='utf-8').write(html)

P('')
P('=== 校验 ===')
for k in ['const DASH = { data: null }', 'function loadDashboard', 'function renderDashboard',
          'tab: "dashboard"', 'renderLhbSectors']:
    P('  app.js %-34s %d' % (k, js.count(k)))
P('  index p-dashboard: %s' % ('p-dashboard' in html))
P('  app.js lines: %d' % len(js.splitlines()))

open('_splice_dash_out.txt', 'w', encoding='utf-8').write('\n'.join(buf))
print('\n'.join(buf))
