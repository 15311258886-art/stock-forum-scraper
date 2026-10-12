# -*- coding: utf-8 -*-
"""把「牛散动向」(star_moves) 接进前端：NAV + 面板 + 加载 + 渲染 + 按需渲染。"""
import io, os, re, sys

B = r'F:\AI Agent部署知识库\2026-09-22-11-49-42\stock-forum-scraper'
JS = os.path.join(B, 'viewer', 'static', 'app.js')
HTML = os.path.join(B, 'viewer', 'static', 'index.html')

js = io.open(JS, encoding='utf-8').read()
html = io.open(HTML, encoding='utf-8').read()
report = []

# ---------- 1) NAV：在 niusan 之后加 star_moves ----------
old = '    { tab: "niusan", label: "牛散追踪" },\n'
new = '    { tab: "niusan", label: "牛散追踪" },\n    { tab: "star_moves", label: "牛散动向" },\n'
if 'tab: "star_moves"' in js:
    report.append('NAV   skip (already)')
elif old in js:
    js = js.replace(old, new, 1); report.append('NAV   ok')
else:
    report.append('NAV   FAIL anchor missing'); sys.exit(1)

# ---------- 2) index.html：加面板 ----------
oldp = '<div class="panel" id="p-niusan"></div>\n'
newp = '<div class="panel" id="p-niusan"></div>\n      <div class="panel" id="p-star_moves"></div>\n'
if 'id="p-star_moves"' in html:
    report.append('HTML  skip (already)')
elif oldp in html:
    html = html.replace(oldp, newp, 1); report.append('HTML  ok')
else:
    report.append('HTML  FAIL anchor missing'); sys.exit(1)

# ---------- 3) 状态 + load + render ----------
BLOCK = r'''
/* ---------- 牛散动向（报告期变动流：新进 / 增持 / 减持 / 退出） ---------- */
const STARM = { data: null, sel: null };
function loadStarMoves() {
  return api("/api/star_moves").then((d) => { STARM.data = d; STARM.sel = null; renderStarMoves(); });
}
function renderStarMoves() {
  const box = $("#p-star_moves");
  if (!box) return;
  const d = STARM.data;
  if (!d || d.empty || !(d.rows || []).length) {
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
    const [t, c] = m[k];
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
      <b>已做风险隔离</b>：实控人、家族同持（父子/夫妻等）不计入。数据源：交易所定期披露（季报／临时公告），非实时。
    </div>
    ${sec("new", "新进", cur.new || [])}
    ${sec("add", "增持", cur.add || [])}
    ${sec("cut", "减持", cur.cut || [])}
    ${sec("exit", "退出", cur.exit || [])}
    ${d.basis ? `<div class="s" style="margin-top:10px">口径：${esc(d.basis)}</div>` : ""}`;
  $$("#p-star_moves [data-sm-date]").forEach((el) => {
    el.onclick = () => { STARM.sel = el.dataset.smDate; renderStarMoves(); };
  });
  $$("#p-star_moves .kline-chip[data-sym]").forEach((el) => {
    el.onclick = () => openStock(el.dataset.sym, el);
  });
}

'''

ANCHOR = 'function renderLhbSectors() {'
if 'function renderStarMoves()' in js:
    report.append('JS    skip (already)')
elif ANCHOR in js:
    js = js.replace(ANCHOR, BLOCK.lstrip('\n') + ANCHOR, 1); report.append('JS    ok')
else:
    report.append('JS    FAIL anchor missing'); sys.exit(1)

# ---------- 4) selectNav 按需渲染 ----------
olds = '''  if (tab === "niusan") {
    if (NIUSAN.data) renderNiusan();
    else loadNiusan().catch((e) => reportLoadError("牛散追踪", e));
  }'''
news = olds + '''
  if (tab === "star_moves") {
    if (STARM.data) renderStarMoves();
    else loadStarMoves().catch((e) => reportLoadError("牛散动向", e));
  }'''
if 'tab === "star_moves"' in js:
    report.append('NAV2  skip (already)')
elif olds in js:
    js = js.replace(olds, news, 1); report.append('NAV2  ok')
else:
    report.append('NAV2  FAIL anchor missing')

# ---------- 5) loadAll 预取 ----------
oldl = '  api("/api/dashboard").then((d) => { DASH.data = d; renderDashboard(); })'
newl = oldl + '\n    .catch(() => null);\n  api("/api/star_moves").then((d) => { STARM.data = d; }).catch(() => null)'
if 'api("/api/star_moves")' in js:
    report.append('LOAD  skip (already)')
elif oldl in js:
    js = js.replace(oldl, newl, 1); report.append('LOAD  ok')
else:
    report.append('LOAD  FAIL anchor missing（非致命，进入标签时会按需加载）')

io.open(JS, 'w', encoding='utf-8').write(js)
io.open(HTML, 'w', encoding='utf-8').write(html)
print('\n'.join(report))
print('JS lines:', len(js.splitlines()))
