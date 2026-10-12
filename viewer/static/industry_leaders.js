/* 行业龙头全景（王总 2026-09 拍板）前端：独立脚本，零改动 app.js 渲染核心（仅挂一个 NAV 入口）。
 *
 * 数据来自 /api/industry_leaders（viewer/industry_leaders.py）：
 *   - 本机直连 akshare → 真实市值排名，返回 {available:true, industries:[...]}（全量成分股，Top N 标 is_leader）
 *   - 沙箱 / 无 akshare / 取数失败 → {available:false, message:"...", industries:[]}
 *
 * 红线：available:false 时只显示提示，绝不渲染任何假表格 / 假龙头。
 * 头部标记：is_leader=true 的成分股，代码前 + 名称前都加「*」并红色加粗（王总原话：一眼看出头部）。
 */
(function () {
  "use strict";
  const esc = (s) => String(s == null ? "" : s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
  const api = (p) => fetch(p).then((r) => r.json());
  const fmtCap = (v) => (v == null ? "—" : (v >= 1e8 ? (v / 1e8).toFixed(1) + "亿" : (v / 1e4).toFixed(0) + "万"));
  const fmtPct = (v) => (v == null ? "—" : (v > 0 ? "+" : "") + Number(v).toFixed(2) + "%");
  const fmtPrice = (v) => (v == null ? "—" : Number(v).toFixed(2));

  const ST = { data: null, loading: false, error: null };

  function hostOf() { return document.getElementById("p-industry-leaders"); }

  // app.js selectNav 在切到 industry-leaders 标签时调用
  function loadIndustryLeaders() {
    const host = hostOf();
    if (!host) return;
    if (ST.data) { render(host, ST.data); return; }   // 已拉过 → 只重绘
    if (ST.loading) return;
    ST.loading = true;
    host.innerHTML = `<div class="empty">正在拉取申万一级行业真实市值排名…（需本机直连 akshare）</div>`;
    api("/api/industry_leaders?top=5")
      .then((d) => { ST.data = d; ST.error = null; ST.loading = false; render(host, d); })
      .catch((e) => {
        ST.loading = false;
        ST.error = (e && e.message) || String(e);
        host.innerHTML = `<div class="il-warn"><div class="il-warn-title">⚠ 请求失败</div>` +
          `<div class="il-warn-body">${esc(ST.error)}</div></div>`;
      });
  }

  function render(host, d) {
    // 红线：沙箱 / 取数失败 → 只提示，不渲染假表格
    if (!d || d.available === false) {
      const msg = (d && d.message) || _UNAVAILABLE_MSG;
      host.innerHTML =
        `<div class="il-head"><h2>行业龙头全景</h2></div>
         <div class="il-warn">
           <div class="il-warn-title">⚠ 暂无可用的真实市值数据</div>
           <div class="il-warn-body">${esc(msg)}</div>
           <div class="muted" style="margin-top:10px">本功能不展示任何编造 / 演示数据。请在本机（已 <code>pip install akshare</code>、可直连网络）运行 <code>python viewer/server.py</code> 后重新打开本页。</div>
         </div>`;
      return;
    }
    const top = d.top || 5;
    const inds = d.industries || [];
    const legend =
      `<div class="il-legend">共 <b>${inds.length}</b> 个申万一级行业 · 每个行业按<b>总市值</b>客观降序排列，
       <span class="il-leader">*头部 Top${top}</span>（代码前与名称前均标 <b>*</b>，红色加粗）即该行业龙头。</div>`;
    const blocks = inds.map((ind) => {
      const rows = (ind.members || []).map((m) => {
        const leadCls = m.is_leader ? " il-leader" : "";
        const star = m.is_leader ? "*" : "";
        const pctCls = (m.pct == null) ? "" : (m.pct > 0 ? " up" : (m.pct < 0 ? " down" : ""));
        return `<tr class="${m.is_leader ? "il-lead-row" : ""}">
          <td class="${leadCls}">${star}${esc(m.code)}</td>
          <td class="${leadCls}">${star}${esc(m.name)}</td>
          <td class="num">${fmtPrice(m.price)}</td>
          <td class="num ${pctCls}">${fmtPct(m.pct)}</td>
          <td class="num">${fmtCap(m.market_cap)}</td>
          <td class="num">${m.is_leader ? '<span class="il-tag">★ 头部</span>' : ""}</td>
        </tr>`;
      }).join("");
      return `<section class="il-ind">
        <div class="il-ind-hd"><span class="il-ind-name">${esc(ind.name)}</span>
          <span class="muted">共 ${ind.count} 家 · 头部 Top${top} 标 *</span></div>
        <table class="wt-tb il-tb"><thead><tr>
          <th>代码</th><th>名称</th><th class="num">最新价</th><th class="num">涨跌幅</th><th class="num">总市值</th><th class="num">头部</th>
        </tr></thead><tbody>${rows}</tbody></table>
      </section>`;
    }).join("");
    host.innerHTML =
      `<div class="il-head"><h2>行业龙头全景</h2>
        <span class="muted">数据生成于 ${esc(d.generated_at || "")} · 真实市值来源 akshare</span></div>
       ${legend}
       <div class="il-list">${blocks}</div>`;
  }

  const _UNAVAILABLE_MSG =
    "行业市值排名需在本机（直连网络 + akshare）运行，沙箱无法获取真实市值；当前环境未能取得真实数据，已停止渲染以避免展示编造的头部名单。";

  window.loadIndustryLeaders = loadIndustryLeaders;
  window.INDUSTRY_LEADERS = ST;
})();
