# -*- coding: utf-8 -*-
"""导航归拢（2026-09-25）
① 龙虎榜 + 板块动向 → 从「实盘操作」迁到「行情研判」（它们是方向参考，不是实盘选手）
② 牛散追踪 + 牛散动向 → 合并为一个标签（同源同群体，只是一个看榜单、一个看变动）
③ 实盘操作内部按「人 → 动作 → 共识」重排
"""
import io, os, re, sys
sys.stdout.reconfigure(encoding='utf-8')

B = os.path.dirname(os.path.abspath(__file__))
P = os.path.join(B, 'viewer', 'static', 'app.js')
H = os.path.join(B, 'viewer', 'static', 'index.html')
src = open(P, encoding='utf-8').read()
hsrc = open(H, encoding='utf-8').read()

# 备份
open(P + '.bak.before-navmerge', 'w', encoding='utf-8').write(src)
open(H + '.bak.before-navmerge', 'w', encoding='utf-8').write(hsrc)

changes = []

# ---------- ① NAV 重排 ----------
old_nav = '''const NAV = [
  { key: "overview", label: "总览", icon: "◎", items: [
    { tab: "dashboard", label: "今日看板" },
    { tab: "summary", label: "汇总" }] },
  { key: "acquire", label: "知识获取", icon: "⇣", items: [
    { tab: "sources", label: "数据源" },
    { tab: "archive", label: "原始存档" },
    { tab: "fetchlog", label: "抓取与登录记录" }] },
  { key: "research", label: "行情研判", icon: "▤", items: [
    { tab: "stocks", label: "当前讨论股票" },
    { tab: "sentiment", label: "舆论情绪" },
    { tab: "topics", label: "近期热点" },
    { tab: "overseas_sentiment", label: "美股港股舆情" },
    { tab: "overseas_hotspots", label: "国外驱动热点" }] },
  { key: "trade", label: "实盘操作", icon: "▦", items: [
    { tab: "operating", label: "正在操作股票" },
    { tab: "players", label: "实盘选手(渠道)" },
    { tab: "lhb", label: "游资龙虎榜" },
    { tab: "lhb_sectors", label: "板块动向" },
    { tab: "niusan", label: "牛散追踪" },
    { tab: "star_moves", label: "牛散动向" },
    { tab: "consensus", label: "共同持仓共识" },
    { tab: "cohold", label: "近5日共同买入" },
    { tab: "persons", label: "相关人物" }] },
  { key: "learn", label: "跟我学习", icon: "✎", items: [
    { tab: "learn_zdx_stock", label: "中短线炒股" },
    { tab: "learn_tracks", label: "学习轨道管理" }] },
];'''

new_nav = '''const NAV = [
  { key: "overview", label: "总览", icon: "◎", items: [
    { tab: "dashboard", label: "今日看板" },
    { tab: "summary", label: "汇总" }] },
  { key: "research", label: "行情研判", icon: "▤", items: [
    { tab: "lhb", label: "游资龙虎榜" },
    { tab: "lhb_sectors", label: "板块动向" },
    { tab: "stocks", label: "当前讨论股票" },
    { tab: "sentiment", label: "舆论情绪" },
    { tab: "topics", label: "近期热点" },
    { tab: "overseas_sentiment", label: "美股港股舆情" },
    { tab: "overseas_hotspots", label: "国外驱动热点" }] },
  { key: "trade", label: "实盘选手", icon: "▦", items: [
    { tab: "players", label: "选手与渠道" },
    { tab: "operating", label: "正在操作股票" },
    { tab: "niusan", label: "牛散追踪" },
    { tab: "consensus", label: "共同持仓共识" },
    { tab: "cohold", label: "近5日共同买入" },
    { tab: "persons", label: "相关人物" }] },
  { key: "acquire", label: "知识获取", icon: "⇣", items: [
    { tab: "sources", label: "数据源" },
    { tab: "archive", label: "原始存档" },
    { tab: "fetchlog", label: "抓取与登录记录" }] },
  { key: "learn", label: "跟我学习", icon: "✎", items: [
    { tab: "learn_zdx_stock", label: "中短线炒股" },
    { tab: "learn_tracks", label: "学习轨道管理" }] },
];'''

if old_nav in src:
    src = src.replace(old_nav, new_nav)
    changes.append('NAV 重排（龙虎榜/板块动向→行情研判；两个牛散合并；实盘操作改名为「实盘选手」并重排）')
else:
    print('[WARN] NAV 未匹配，跳过')

# ---------- ② renderNiusan：加「榜单 / 变动流」内部切换 ----------
old_ns = src[src.find('const NIUSAN = {'): src.find('\nconst LBHS = {')]
assert old_ns, 'NIUSAN 段未找到'

new_ns = '''const NIUSAN = { data: null, scope: "free", minHold: 2, view: "rank" };
function loadNiusan() {
  const q = "?scope=" + encodeURIComponent(NIUSAN.scope) + "&min_hold=" + NIUSAN.minHold;
  return api("/api/niusan" + q).then((d) => { NIUSAN.data = d; renderNiusan(); });
}
/* 牛散追踪（合并「牛散榜 + 变动流」）：同一群体（跨多只股建仓的自然人），两个视角 */
function renderNiusan() {
  const box = $("#p-niusan");
  if (!box) return;
  const seg = (v, t) => `<span class="chip-s" role="button" data-ns-view="${escAttr(v)}" ` +
    `style="cursor:pointer;${v === NIUSAN.view ? "background:var(--accent,#0E7490);color:#fff;" : ""}">${esc(t)}</span>`;
  const head = `<div class="hint" style="margin-bottom:10px">
      数据源：上市公司<b>定期报告 / 临时公告</b>披露的十大股东（东财 F10，交易所强制披露，免费公开）。
      <b>已做风险隔离</b>：实控人、家族同持（父子/夫妻等）不计入牛散口径。
      <br>视角切换：${seg("rank", "① 牛散榜（谁在买）")} ${seg("moves", "② 变动流（本期买/卖了什么）")}
    </div>
    <div id="nsBody"></div>`;
  box.innerHTML = head;
  $$("#p-niusan [data-ns-view]").forEach((el) => {
    el.onclick = () => {
      NIUSAN.view = el.dataset.nsView;
      renderNiusan();
      if (NIUSAN.view === "moves" && !STARM.data) {
        loadStarMoves().catch((e) => reportLoadError("牛散动向", e));
      }
    };
  });
  const body = $("#nsBody");
  if (!body) return;
  if (NIUSAN.view === "moves") renderNsMoves(body);
  else renderNsRank(body);
}
function renderNsRank(box) {
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
    <div class="s" style="margin-bottom:10px">
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
  $$("#nsBody [data-ns-scope]").forEach((el) => {
    el.onclick = () => {
      NIUSAN.scope = el.dataset.nsScope;
      loadNiusan().catch((e) => reportLoadError("牛散追踪", e));
    };
  });
  $$("#nsBody .kline-chip[data-sym]").forEach((el) => {
    el.onclick = () => openStock(el.dataset.sym, el);
  });
}
'''
src = src.replace(old_ns, new_ns)
changes.append('renderNiusan 拆为「榜单 / 变动流」双视角（合并牛散动向）')

# ---------- ③ renderStarMoves → renderNsMoves（渲染进 nsBody） ----------
old_sm = src[src.find('/* ---------- 牛散动向（报告期变动流'): src.find('\nfunction renderLhbSectors() {')]
assert old_sm, 'STARM 段未找到'

new_sm = '''/* ---------- 牛散动向（报告期变动流：新进 / 增持 / 减持 / 退出） ---------- */
const STARM = { data: null, sel: null };
function loadStarMoves() {
  return api("/api/star_moves").then((d) => { STARM.data = d; STARM.sel = null; renderNsMoves($("#nsBody")); });
}
function renderNsMoves(box) {
  if (!box) return;
  const d = STARM.data;
  if (!d) { box.innerHTML = `<div class="empty">牛散动向加载中…</div>`; return; }
  if (d.empty || !(d.rows || []).length) {
    box.innerHTML = `<div class="empty">暂无牛散动向数据。需先抓取权威股东披露（holders_fetch.py）。</div>`;
    return;
  }
  const rows = d.rows || [];
  const sel = STARM.sel || rows[0].date;
  const cur = rows.find((r) => r.date === sel) || rows[0];
  const chips = rows.map((r) =>
    `<span class="chip-s" role="button" data-sm-date="${escAttr(r.date)}" ` +
    `style="cursor:pointer;${r.date === cur.date ? "background:var(--accent,#0E7490);color:#fff;" : ""}">` +
    `${esc(r.date)} <span class="s">${r.counts.new}新/${r.counts.add}增/${r.counts.cut}减/${r.counts.exit}退</span></span>`).join(" ");
  const badge = (k) => {
    const m = { new: ["新进", "bull"], add: ["增持", "bull"], cut: ["减持", "bear"], exit: ["退出", "bear"] };
    const [t] = m[k];
    return `<span class="src-tag ${k === "new" || k === "add" ? "src-port" : "src-comm"}">${t}</span>`;
  };
  const sec = (key, title, arr) => {
    if (!arr.length) return `<div class="view-group"><div class="vg-head">${title}（0）</div><div class="vg-empty">无</div></div>`;
    const trs = arr.map((x) => `<tr>
        <td><b>${esc(x.name)}</b></td>
        <td><span class="kline-chip chip-s" data-sym="${escAttr(x.symbol)}" title="点击看这只股的详情">${esc(x.symbol)}</span> ${esc(x.symbol_name || "")}</td>
        <td class="num">${x.ratio != null ? Number(x.ratio).toFixed(2) + "%" : "—"}</td>
        <td class="num s">${esc(x.change || "")}</td>
      </tr>`).join("");
    return `<div class="view-group">
      <div class="vg-head">${badge(key)} ${title}（${arr.length}）</div>
      <table><thead><tr><th>牛散</th><th>股票</th><th class="num">占流通股</th><th class="num">变动</th></tr></thead><tbody>${trs}</tbody></table>
    </div>`;
  };
  const s = d.summary || {};
  box.innerHTML = `
    <div style="margin-bottom:10px"><b>报告期</b>：${chips}</div>
    <div class="s" style="margin-bottom:12px">
      共 <b>${s.star_count ?? "—"}</b> 位「牛散」（跨 ≥2 家上市公司的自然人），覆盖 ${s.period_count ?? "—"} 个报告期。
      变动口径：<b>新进</b>=本期新出现在十大流通股东；<b>增持/减持</b>=持股数较上期同向变动；<b>退出</b>=上期在榜、本期已不在。
      数据源：交易所定期披露（季报／临时公告），非实时。
    </div>
    ${sec("new", "新进", cur.new || [])}
    ${sec("add", "增持", cur.add || [])}
    ${sec("cut", "减持", cur.cut || [])}
    ${sec("exit", "退出", cur.exit || [])}
    ${d.basis ? `<div class="s" style="margin-top:10px">口径：${esc(d.basis)}</div>` : ""}`;
  $$("#nsBody [data-sm-date]").forEach((el) => {
    el.onclick = () => { STARM.sel = el.dataset.smDate; renderNsMoves($("#nsBody")); };
  });
  $$("#nsBody .kline-chip[data-sym]").forEach((el) => {
    el.onclick = () => openStock(el.dataset.sym, el);
  });
}
'''
src = src.replace(old_sm, new_sm)
changes.append('renderStarMoves 改为 renderNsMoves（渲染进合并面板）')

# ---------- ④ selectNav 分支：删除 star_moves，牛散分支改为双视角兜底 ----------
old_br = '''  // 牛散追踪：进入时按需（重）渲染
  if (tab === "niusan") {
    if (NIUSAN.data) renderNiusan();
    else loadNiusan().catch((e) => reportLoadError("牛散追踪", e));
  }
  if (tab === "star_moves") {
    if (STARM.data) renderStarMoves();
    else loadStarMoves().catch((e) => reportLoadError("牛散动向", e));
  }'''
new_br = '''  // 牛散追踪（含榜单 / 变动流双视角）：进入时按需（重）渲染
  if (tab === "niusan") {
    if (NIUSAN.data) renderNiusan();
    else loadNiusan().catch((e) => reportLoadError("牛散追踪", e));
  }'''
if old_br in src:
    src = src.replace(old_br, new_br)
    changes.append('selectNav 删除 star_moves 分支')
else:
    print('[WARN] selectNav 牛散分支未匹配')

# ---------- ⑤ loadAll 里的 loadStarMoves / loadNiusan ----------
m = re.findall(r'[^\n]*loadStarMoves\(\)[^\n]*', src)
print('loadStarMoves 剩余引用:')
for x in m:
    print('   ', x.strip()[:150])

open(P, 'w', encoding='utf-8').write(src)

# ---------- ⑥ index.html：删除 p-star_moves 面板 ----------
if '<div class="panel" id="p-star_moves"></div>' in hsrc:
    hsrc = hsrc.replace('    <div class="panel" id="p-star_moves"></div>\n', '')
    hsrc = hsrc.replace('<div class="panel" id="p-star_moves"></div>\n', '')
    hsrc = hsrc.replace('<div class="panel" id="p-star_moves"></div>', '')
    open(H, 'w', encoding='utf-8').write(hsrc)
    changes.append('index.html 删除 p-star_moves 面板')
else:
    print('[WARN] p-star_moves 面板未匹配')

print()
print('=== 已执行 ===')
for c in changes:
    print('  ✓', c)
print()
print('app.js 行数:', len(src.splitlines()))
print('p-star_moves 残留:', src.count('p-star_moves'), '(app.js) |', hsrc.count('p-star_moves'), '(html)')
