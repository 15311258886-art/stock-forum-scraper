/* 供应链瓶颈猎手（2026-10-24 进哥拍板）前端：独立脚本，零改动 app.js 渲染核心（仅挂一个 NAV 入口）。
 *
 * 数据来自 /api/bottleneck?theme=xxx（viewer/bottleneck.py，纯标准库、KB 驱动、无网络依赖）：
 *   - ok=true：返回 {theme_name, summary, as_of, disclaimer, layers, bottlenecks, ranking, themes}
 *   - ok=false：返回 {ok:false, error:"...", themes:[...]}（主题未找到 / 知识库缺失）
 *
 * 红线：估值快照为 KB 内真实但非实时数据，界面显式标注 as_of，绝不编造市值/估值。
 * 一页纸摘要：点排名表任意一行 → 抽屉展示「为什么是瓶颈 / 为什么是这家 / 正反逻辑 / 估值门槛」。
 */
(function () {
  "use strict";
  const esc = (s) => String(s == null ? "" : s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
  const api = (p) => fetch(p).then((r) => r.json());

  // 6 维判据（与 KB / 引擎一致）
  const DIMS = ["concentration", "expansion", "substitutability", "utilization", "demand", "qualification"];
  const DIM_LABEL = {
    concentration: "供给集中度", expansion: "扩产周期", substitutability: "替代难度",
    utilization: "产能利用率", demand: "需求增速", qualification: "客户验证",
  };
  const RATING_COLOR = { s: "#d97706", a: "#0E7490", b: "#64748b" };

  const ST = { data: null, loading: false, theme: null };

  const hostOf = () => document.getElementById("p-bottleneck");

  // app.js selectNav 在切到 bottleneck 标签时调用
  function loadBottleneck() {
    const host = hostOf();
    if (!host) return;
    if (ST.data) { render(host, ST.data); return; }   // 已拉过 → 只重绘
    if (ST.loading) return;
    fetchTheme(null);
  }

  function fetchTheme(key) {
    const host = hostOf();
    if (!host) return;
    ST.loading = true;
    host.innerHTML = `<div class="empty">正在分析「${esc(key || "默认主题")}」产业链瓶颈…</div>`;
    api("/api/bottleneck?theme=" + encodeURIComponent(key || ""))
      .then((d) => {
        ST.loading = false;
        ST.data = d;
        if (key) ST.theme = key;
        if (d && d.ok && d.theme_key) ST.theme = d.theme_key;
        render(host, d);
      })
      .catch((e) => {
        ST.loading = false;
        ST.data = null;
        host.innerHTML = `<div class="bn-warn"><div class="bn-warn-title">⚠ 请求失败</div>` +
          `<div class="bn-warn-body">${esc((e && e.message) || String(e))}</div></div>`;
      });
  }

  function renderChips(d) {
    const themes = (d && d.themes) || [];
    if (!themes.length) return "";
    return `<div class="bn-chips">` + themes.map((t) =>
      `<button class="bn-chip${t.key === ST.theme ? " active" : ""}" data-theme="${esc(t.key)}">${esc(t.name)}</button>`
    ).join("") + `</div>`;
  }

  function renderChain(d) {
    const layers = d.layers || [];
    if (!layers.length) return "";
    const cols = layers.map((L) =>
      `<div class="bn-layer">
        <div class="bn-layer-hd">L${L.layer} · ${esc(L.name)}</div>
        <div class="bn-layer-nodes">${(L.nodes || []).map((n) => `<span class="bn-node">${esc(n)}</span>`).join("")}</div>
      </div>`).join("");
    return `<div class="bn-chain">${cols}</div>`;
  }

  function renderBottlenecks(d) {
    const bk = d.bottlenecks || [];
    if (!bk.length) return `<div class="muted">该主题暂无环节数据。</div>`;
    const order = { S: 0, A: 1, B: 2, C: 3 };
    const groups = { S: [], A: [], B: [], C: [] };
    bk.forEach((b) => { (groups[b.rating] = groups[b.rating] || []).push(b); });
    const card = (b) =>
      `<div class="bn-card bn-${b.rating.toLowerCase()}">
        <div class="bn-card-hd">
          <span class="bn-rating ${b.rating.toLowerCase()}">${b.rating}</span>
          <span class="bn-seg">${esc(b.name)}</span>
          <span class="bn-layer-tag">L${b.layer}</span>
        </div>
        <div class="bn-dims">${DIMS.map((dim) => {
          const red = b.scores[dim];
          const note = b.score_notes[dim] || "";
          return `<span class="bn-dim ${red ? "red" : "green"}" title="${esc(note)}">${red ? "●" : "○"} ${esc(DIM_LABEL[dim])}</span>`;
        }).join("")}</div>
        <div class="bn-thesis">${esc(b.thesis)}</div>
        <div class="bn-pc"><span class="bn-pro">✓ ${esc(b.pro)}</span><span class="bn-con">✕ ${esc(b.con)}</span></div>
        <div class="bn-cands">候选股：${(b.candidates || []).map((c) => `<code>${esc(c)}</code>`).join(" ")}</div>
      </div>`;
    const sections = [];
    [["S", "S 级 · 单点故障级瓶颈（≥4 红）"], ["A", "A 级 · 强瓶颈（3 红）"], ["B", "B 级 · 关注（1-2 红）"]].forEach(([r, title]) => {
      if (groups[r] && groups[r].length) {
        sections.push(`<div class="bn-grp"><div class="bn-grp-hd bn-${r.toLowerCase()}">${title}</div><div class="bn-cards">${groups[r].map(card).join("")}</div></div>`);
      }
    });
    return `<div class="bn-grps">${sections.join("")}</div>`;
  }

  function renderRanking(d) {
    const rows = (d.ranking || []).map((r, i) =>
      `<tr class="bn-row" data-code="${esc(r.code)}">
        <td class="num">${i + 1}</td>
        <td><code>${esc(r.code)}</code></td>
        <td class="bn-name">${esc(r.name)}</td>
        <td class="muted bn-seg-cell">${esc(r.segment)}</td>
        <td><span class="bn-rating sm ${r.rating.toLowerCase()}">${r.rating}</span></td>
        <td class="bn-signal">${"★".repeat(r.signal)}<span class="bn-signal-off">${"★".repeat(5 - r.signal)}</span></td>
        <td><span class="bn-light ${r.light}">${r.light === "red" ? "红" : r.light === "yellow" ? "黄" : "绿"}</span></td>
        <td class="num">${r.ps == null ? "—" : r.ps + "x"}</td>
        <td class="num">${r.pe == null ? "—" : r.pe + "x"}</td>
        <td class="num">${r.market_cap == null ? "—" : r.market_cap + "亿"}</td>
        <td class="num">${r.cap_vs_tam == null ? "—" : (r.cap_vs_tam * 100).toFixed(0) + "%"}</td>
        <td class="muted bn-margin">${esc(r.margin_note)}</td>
      </tr>`).join("");
    return `<table class="wt-tb bn-tb"><thead><tr>
        <th class="num">#</th><th>代码</th><th>名称</th><th>所在环节</th><th>评级</th>
        <th>信号</th><th>估值灯</th><th class="num">PS</th><th class="num">PE</th>
        <th class="num">市值</th><th class="num">占TAM</th><th>安全边际</th>
      </tr></thead><tbody>${rows}</tbody></table>`;
  }

  function render(host, d) {
    if (!d || d.ok === false) {
      const msg = (d && d.error) || "未知错误";
      const chips = renderChips(d || {});
      host.innerHTML =
        `<div class="bn-head"><h2>供应链瓶颈猎手</h2></div>
         <div class="bn-warn">
           <div class="bn-warn-title">⚠ ${esc(msg)}</div>
           ${chips ? `<div class="bn-warn-body">可选主题：</div>${chips}` : ""}
         </div>`;
      bindChips(host);
      return;
    }
    const header =
      `<div class="bn-head">
        <h2>供应链瓶颈猎手 · ${esc(d.theme_name)}</h2>
        <span class="bn-meta">估值快照 @ ${esc(d.as_of || "—")} · 生成于 ${esc(d.generated_at || "")}</span>
      </div>
      <div class="bn-summary">${esc(d.summary || "")}</div>
      ${renderChips(d)}
      <div class="bn-disclaimer">${esc(d.disclaimer || "")}</div>`;

    const chainSec =
      `<div class="bn-sec"><div class="bn-sec-hd">① 产业链物理拆解（Layer 0→4）</div>${renderChain(d)}</div>`;
    const bnSec =
      `<div class="bn-sec"><div class="bn-sec-hd">② 瓶颈识别（6 维红标：供给集中度 / 扩产周期 / 替代难度 / 产能利用率 / 需求增速 / 客户验证）</div>${renderBottlenecks(d)}</div>`;
    const rankSec =
      `<div class="bn-sec"><div class="bn-sec-hd">③ 机会排名（信号强度 ★1-5 = 评级基础分 − 估值红灯数；估值灯：红=透支 / 黄=偏高 / 绿=合理）</div>${renderRanking(d)}<div class="bn-tip muted">提示：点任意一行查看「为什么是瓶颈 / 为什么是这家 / 正反逻辑 / 估值门槛」一页纸。</div></div>`;

    host.innerHTML = header + chainSec + bnSec + rankSec;
    bindChips(host);
    host.querySelectorAll(".bn-row").forEach((tr) => {
      tr.onclick = () => openDrawer(tr.dataset.code);
    });
  }

  function bindChips(host) {
    host.querySelectorAll(".bn-chip").forEach((b) => {
      b.onclick = () => fetchTheme(b.dataset.theme);
    });
  }

  function openDrawer(code) {
    const r = (ST.data && ST.data.ranking || []).find((x) => x.code === code);
    if (!r) return;
    const ov = document.createElement("div");
    ov.className = "bn-drawer-mask";
    ov.innerHTML =
      `<div class="bn-drawer">
        <div class="bn-drawer-hd">
          <b>${esc(r.name)}</b> <code>${esc(r.code)}</code>
          <span class="bn-rating sm ${r.rating.toLowerCase()}">${r.rating}</span>
          <span class="bn-signal">${"★".repeat(r.signal)}</span>
          <button class="bn-close" title="关闭">✕</button>
        </div>
        <div class="bn-drawer-body">
          <div class="bn-dsec"><h4>为什么是瓶颈（${esc(r.segment)}）</h4><p>${esc(r.why_bottleneck)}</p></div>
          <div class="bn-dsec"><h4>为什么是这家</h4><p>${esc(r.why_this)}</p></div>
          <div class="bn-dsec bn-grid2">
            <div><h4>正向逻辑</h4><p class="bn-pro">${esc(r.pro)}</p></div>
            <div><h4>反向（芒格式六问）</h4><p class="bn-con">${esc(r.con)}</p></div>
          </div>
          <div class="bn-dsec">
            <h4>估值与门槛</h4>
            <p>市值 ${r.market_cap == null ? "—" : r.market_cap + "亿"} · PS ${r.ps == null ? "—" : r.ps + "x"} · PE ${r.pe == null ? "—" : r.pe + "x"} · 市值 / 环节 TAM ${r.cap_vs_tam == null ? "—" : (r.cap_vs_tam * 100).toFixed(0) + "%"}</p>
            <p class="muted">${esc(r.margin_note)}</p>
            ${r.red_reasons && r.red_reasons.length ? `<p class="bn-red">红灯：${r.red_reasons.map(esc).join("；")}</p>` : ""}
            ${r.yellow_reasons && r.yellow_reasons.length ? `<p class="bn-yellow">黄灯：${r.yellow_reasons.map(esc).join("；")}</p>` : ""}
          </div>
        </div>
      </div>`;
    ov.querySelector(".bn-close").onclick = () => ov.remove();
    ov.onclick = (e) => { if (e.target === ov) ov.remove(); };
    document.body.appendChild(ov);
  }

  window.loadBottleneck = loadBottleneck;
  window.BOTTLENECK = ST;
})();
