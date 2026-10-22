/* 智能选股增强 B4/B5/B7/B8/B6 前端（独立脚本，2026-10-07）
 * 由 app.js 的 renderScreener 末尾钩子 window.SC_EXTRA.init(st, host) 驱动。
 * 不改动 app.js 的渲染逻辑，所有新 UI 挂在 .sc-extra 容器内。
 * 依赖 window.SC_EXTRA_API（app.js 暴露：loadScreener / renderScreener / scCurrentRows / FEAT）
 * 与 window.openStock（个股详情）。
 */
(function () {
  "use strict";
  const esc = (s) => String(s == null ? "" : s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
  const today = () => new Date().toISOString().slice(0, 10);
  const api = (p, opts) => fetch(p, opts).then((r) => r.json());

  function hint(host, msg) {
    const el = host && host.querySelector("#scHint2");
    if (el) { el.textContent = msg; el.style.opacity = 1; clearTimeout(el._t); el._t = setTimeout(() => { el.style.opacity = 0; }, 3500); }
  }
  // 成功提示用 toast 挂到 body（而非 #scHint2）：nlRun 会触发 loadScreener→renderScreener 重建 .sc-extra，
  // 挂在 host 内的提示会被一并抹掉，导致「已按」一闪即逝；toast 在 body 上不受重渲染影响。
  function toast(host, msg) {
    let t = document.getElementById("scToast");
    if (!t) {
      t = document.createElement("div");
      t.id = "scToast";
      t.style.cssText = "position:fixed;left:50%;bottom:32px;transform:translateX(-50%);z-index:9999;background:#0E7490;color:#fff;padding:10px 18px;border-radius:8px;font-size:14px;box-shadow:0 4px 16px rgba(0,0,0,.25);opacity:0;transition:opacity .2s;pointer-events:none;max-width:80vw;text-align:center";
      document.body.appendChild(t);
    }
    t.textContent = msg;
    t.style.opacity = "1";
    clearTimeout(t._t);
    t._t = setTimeout(() => { t.style.opacity = "0"; }, 3500);
  }
  function modal(title, html) {
    let m = document.getElementById("scModal");
    if (!m) {
      m = document.createElement("div"); m.id = "scModal"; m.className = "sc-modal";
      document.body.appendChild(m);
      m.addEventListener("click", (e) => { if (e.target === m) m.style.display = "none"; });
    }
    m.innerHTML = `<div class="sc-modal-box"><div class="sc-modal-hd"><b>${esc(title)}</b><button class="sc-x" onclick="document.getElementById('scModal').style.display='none'">✕</button></div><div class="sc-modal-bd">${html}</div></div>`;
    m.style.display = "flex";
  }

  const SC = {
    st: null, host: null,
    init(st, host) {
      this.st = st; this.host = host;
      if (host.querySelector(".sc-extra")) return; // 已在（理论上 innerHTML 重建后不会命中）
      const box = document.createElement("div");
      box.className = "sc-extra";
      box.innerHTML = `
        <div class="sc-extra-row">
          <input id="scNlInput" class="sc-in2" placeholder="说人话选股：例如「低估值的白酒股，股息率>3%」" />
          <button class="btn sm" id="scNlRun">生成筛选</button>
          <span class="sc-sep"></span>
          <button class="btn sm" id="scValRun" title="算 PE/PB/PS 近3年历史分位">估值分位</button>
          <button class="btn sm" id="scBtRun" title="对当前结果做历史区间回测">回测</button>
          <button class="btn sm" id="scThemeRun" title="题材/概念映射">题材</button>
          <span class="sc-sep"></span>
          <button class="btn sm" id="scGear" title="设置 DeepSeek API Key">⚙ 设置</button>
          <span id="scHint2" class="sc-hint-msg muted" style="opacity:0"></span>
        </div>
        <div class="sc-bt-range" id="scBtRange" style="display:none">
          回测区间 <input id="scBtStart" class="sc-in2" type="date" value="2024-01-01" /> ~
          <input id="scBtEnd" class="sc-in2" type="date" value="${today()}" />
          <button class="btn sm" id="scBtGo">运行回测</button>
        </div>`;
      const tools = host.querySelector(".sc-tools");
      if (tools) tools.after(box); else host.appendChild(box);
      box.querySelector("#scNlRun").onclick = () => this.nlRun();
      box.querySelector("#scValRun").onclick = () => this.valRun();
      box.querySelector("#scBtRun").onclick = () => { const r = host.querySelector("#scBtRange"); if (r) r.style.display = r.style.display === "none" ? "flex" : "none"; };
      box.querySelector("#scBtGo").onclick = () => this.btRun();
      box.querySelector("#scThemeRun").onclick = () => this.themeRun();
      box.querySelector("#scGear").onclick = () => this.settings();
    },
    curCodes() {
      const rows = (window.SC_EXTRA_API && window.SC_EXTRA_API.scCurrentRows)
        ? window.SC_EXTRA_API.scCurrentRows(this.st)
        : (this.st.data && this.st.data.rows) || [];
      return rows.map((r) => r.code).filter(Boolean);
    },
    /* ── B6 自然语言选股 ── */
    nlRun() {
      const text = this.host.querySelector("#scNlInput").value.trim();
      if (!text) { hint(this.host, "请输入描述"); return; }
      api("/api/nl_screener", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ text }) })
        .then((d) => {
          if (!d.ok) { hint(this.host, d.error || "调用失败"); return; }
          const c = d.cond || {}; const A = window.SC_EXTRA_API;
          if (!A) { hint(this.host, "引擎未就绪"); return; }
          const st = this.st;
          if (Array.isArray(c.screens) && c.screens.length) st.screens = c.screens;
          if (c.thr && typeof c.thr === "object") st.thr = Object.assign({}, st.thr, c.thr);
          if (c.board) st.industryFilter = c.board;
          A.loadScreener();
          toast(this.host, "已按：" + (c.explain || text));
        }).catch((e) => hint(this.host, "网络错误：" + ((e && e.message) || e)));
    },
    /* ── B4 估值分位 ── */
    valRun() {
      const codes = this.curCodes();
      if (!codes.length) { hint(this.host, "当前无结果可算分位"); return; }
      api("/api/screener/valuation?codes=" + codes.join(",")).then((d) => {
        if (!d.ok) { hint(this.host, d.error); return; }
        const data = d.data || {};
        const rows = codes.map((c) => {
          const v = data[c] || {};
          if (v.ok === false) {
            return `<tr><td>${esc(c)}</td><td colspan="4" style="color:#c0392b">未获取真实数据：${esc(v.error || "请检查 akshare/网络")}</td></tr>`;
          }
          const p = (x) => (x == null ? "—" : (x * 100).toFixed(0) + "%");
          return `<tr><td>${esc(c)}</td><td>${p(v.pe_pct)}</td><td>${p(v.pb_pct)}</td><td>${p(v.ps_pct)}</td><td>${v.pe_now == null ? "—" : v.pe_now}</td></tr>`;
        }).join("");
        modal("估值分位（近 3 年）",
          `<table class="wt-tb sc-tb"><thead><tr><th>代码</th><th>PE分位</th><th>PB分位</th><th>PS分位</th><th>PE(TTM)</th></tr></thead><tbody>${rows}</tbody></table>
           <div class="muted" style="margin-top:8px">分位越低越便宜（仅基于真实行情计算）。取不到时显示「未获取真实数据」，请在本机执行 pip install akshare/baostock/pandas 后重启 server。</div>`);
      }).catch((e) => hint(this.host, "估值分位失败：" + ((e && e.message) || e)));
    },
    /* ── B7 策略回测（统一口径：所有指标来自同一条等权组合净值曲线）── */
    btRun() {
      const codes = this.curCodes();
      if (!codes.length) { hint(this.host, "当前无结果可回测"); return; }
      const start = this.host.querySelector("#scBtStart").value || "2024-01-01";
      const end = this.host.querySelector("#scBtEnd").value || today();
      api("/api/screener/backtest?codes=" + codes.join(",") + "&start=" + start + "&end=" + end).then((d) => {
        if (!d.ok) { hint(this.host, d.error || "未获取真实行情"); return; }
        const v = d;
        const pct = (x) => (x == null ? "—" : (x * 100).toFixed(1) + "%");
        const num = (x) => (x == null ? "—" : (x * 100).toFixed(2) + "%");
        const f2 = (x) => (x == null ? "—" : Number(x).toFixed(2));
        const ins = v.in_sample || {}, outs = v.out_of_sample || {};
        const caveats = (v.caveats || []).map((c) => `<li>${esc(c)}</li>`).join("");
        const monthlyRows = (v.monthly || []).map((x) =>
          `<tr><td>${esc(x.month)}</td><td>${pct(x.ret)}</td></tr>`).join("");
        const cons = v.constraints || {};
        const excl = (v.codes_excluded && v.codes_excluded.length)
          ? `<div class="muted" style="color:#c0392b">已剔除无真实K线标的：${esc(v.codes_excluded.join(","))}</div>` : "";
        modal("策略回测（" + start + " ~ " + end + "）",
          `<div class="muted" style="margin-bottom:6px">${esc(v.method || "事件驱动等权组合回测")}</div>
           <table class="wt-tb sc-tb"><tbody>
             <tr><td>样本数（实际参与）</td><td>${v.n}</td></tr>
             <tr><td>组合总收益（几何）</td><td>${num(v.total_return)}</td></tr>
             <tr><td>年化收益</td><td>${num(v.annual_return)}</td></tr>
             <tr><td>最大回撤（曲线）</td><td>${pct(v.max_drawdown)}</td></tr>
             <tr><td>夏普</td><td>${f2(v.sharpe)}</td></tr>
             <tr><td>索提诺</td><td>${f2(v.sortino)}</td></tr>
             <tr><td>卡玛</td><td>${f2(v.calmar)}</td></tr>
             <tr><td>胜率（正收益日占比）</td><td>${pct(v.win_rate)}</td></tr>
             <tr><td>盈亏比</td><td>${f2(v.profit_loss_ratio)}</td></tr>
             <tr><td>交易日数</td><td>${v.trading_days}</td></tr>
             <tr><td>期末净值</td><td>${f2(v.final_nav)}</td></tr>
           </tbody></table>

           <div style="margin-top:10px;font-weight:600">成本敏感性（2 倍成本情景）</div>
           <table class="wt-tb sc-tb"><tbody>
             <tr><td></td><td>基准成本</td><td>2×成本</td></tr>
             <tr><td>总收益</td><td>${num(v.total_return)}</td><td>${num(v.metrics_2x && v.metrics_2x.total_return)}</td></tr>
             <tr><td>最大回撤</td><td>${pct(v.max_drawdown)}</td><td>${pct(v.metrics_2x && v.metrics_2x.max_drawdown)}</td></tr>
             <tr><td>夏普</td><td>${f2(v.sharpe)}</td><td>${f2(v.metrics_2x && v.metrics_2x.sharpe)}</td></tr>
           </tbody></table>

           <div style="margin-top:10px;font-weight:600">样本内 / 样本外（70/30 walk-forward）</div>
           <table class="wt-tb sc-tb"><tbody>
             <tr><td></td><td>样本内</td><td>样本外</td></tr>
             <tr><td>区间</td><td>${ins.start || "—"}~${ins.end || "—"}</td><td>${outs.start || "—"}~${outs.end || "—"}</td></tr>
             <tr><td>总收益</td><td>${num(ins.total_return)}</td><td>${num(outs.total_return)}</td></tr>
             <tr><td>年化</td><td>${num(ins.annual_return)}</td><td>${num(outs.annual_return)}</td></tr>
             <tr><td>夏普</td><td>${f2(ins.sharpe)}</td><td>${f2(outs.sharpe)}</td></tr>
           </tbody></table>
           <div class="muted" style="margin-top:4px">${esc(v.oos_discount_note || "")}</div>

           <div style="margin-top:10px;font-weight:600">约束生效统计</div>
           <table class="wt-tb sc-tb"><tbody>
             <tr><td>涨停买不进（跳过）</td><td>${cons.limit_up_skipped || 0}</td></tr>
             <tr><td>跌停卖不出（跳过）</td><td>${cons.limit_down_skipped || 0}</td></tr>
             <tr><td>停牌跳过（次）</td><td>${cons.suspended_skipped || 0}</td></tr>
             <tr><td>退市强平（次）</td><td>${cons.delisted_exits || 0}</td></tr>
             <tr><td>再平衡次数（T+1开盘）</td><td>${cons.rebalances || 0}</td></tr>
           </tbody></table>

           ${excl}
           <div class="muted" style="margin-top:6px;color:#b9770e">幸存者偏差：${esc(v.survivorship_note || "")}</div>

           <details style="margin-top:10px"><summary class="muted">坑位提示 / 口径说明（点开）</summary>
             <ul class="muted" style="margin:6px 0 0 18px;line-height:1.5">${caveats}</ul></details>
           <details style="margin-top:6px"><summary class="muted">月度收益分布（${ (v.monthly||[]).length } 个月）</summary>
             <table class="wt-tb sc-tb" style="margin-top:6px"><tbody>${monthlyRows}</tbody></table></details>

           <div style="margin-top:10px"><button class="btn sm" id="scBtExportCsv">导出 CSV</button> <span class="muted">（净值曲线 / 逐笔 / 月度，含 BOM，可直接 Excel 打开）</span></div>
           <div class="muted" style="margin-top:8px">以上所有指标均来自同一条后复权等权组合净值曲线（含成本/约束）。取不到真实行情时如实提示「未获取真实行情」，绝不用假数据。</div>`);
      const _mdl = document.getElementById("scModal");
      if (_mdl) { const _eb = _mdl.querySelector("#scBtExportCsv"); if (_eb) _eb.onclick = () => this.exportCsv(v); }
      }).catch((e) => hint(this.host, "回测失败：" + ((e && e.message) || e)));
    },
    /* ── 导出回测结果为 CSV（净值曲线 / 逐笔 / 月度）── */
    exportCsv(v) {
      if (!v || v.ok === false) { hint(this.host, "没有可导出的回测结果"); return; }
      const L = [];
      L.push("回测区间," + (v.start || "") + "~" + (v.end || ""));
      L.push("样本数," + (v.n != null ? v.n : ""));
      L.push("方法," + (v.method || "").replace(/,/g, " "));
      L.push("");
      L.push("指标,数值");
      [["组合总收益", v.total_return], ["年化收益", v.annual_return], ["最大回撤", v.max_drawdown],
       ["夏普", v.sharpe], ["索提诺", v.sortino], ["卡玛", v.calmar],
       ["胜率", v.win_rate], ["盈亏比", v.profit_loss_ratio], ["期末净值", v.final_nav],
       ["交易日数", v.trading_days]].forEach(([k, x]) => L.push(k + "," + (x == null ? "" : x)));
      L.push("");
      L.push("净值曲线");
      L.push("日期,净值");
      (v.nav_curve || []).forEach((p) => L.push(p.date + "," + p.nav));
      L.push("");
      L.push("逐笔成交");
      L.push("日期,代码,方向,价格,金额,成本,股数");
      (v.trades || []).forEach((t) =>
        L.push([t.date, t.code, t.side, t.price, t.amount, t.cost, t.shares].join(",")));
      L.push("");
      L.push("月度收益");
      L.push("月份,收益");
      (v.monthly || []).forEach((m) => L.push(m.month + "," + m.ret));
      const csv = "﻿" + L.join("\n");
      const blob = new Blob([csv], { type: "text/csv;charset=utf-8" });
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = "backtest_" + (v.start || "na") + "_" + (v.end || "na") + ".csv";
      document.body.appendChild(a); a.click(); a.remove();
      setTimeout(() => URL.revokeObjectURL(url), 2000);
      hint(this.host, "已导出 CSV");
    },
    /* ── B8 题材/概念 ── */
    themeRun() {
      api("/api/screener/themes").then((d) => {
        if (!d.ok) { hint(this.host, d.error); return; }
        const list = d.data || [];
        const chips = list.map((t) => `<button class="sc-chip" data-theme="${esc(t.theme)}">${esc(t.theme)} <span class="muted">${t.count}</span></button>`).join("");
        modal("题材 / 概念（点选查看成分）",
          `<div class="sc-themes">${chips}</div><div id="scThemeMembers" class="sc-theme-members"></div>
           <div class="muted" style="margin-top:8px">题材/概念映射（仅真实数据）。若提示「未能获取真实概念板块数据」，请在本机执行 pip install akshare 后重启 server。</div>`);
        document.querySelectorAll("#scModal [data-theme]").forEach((b) => {
          b.onclick = () => {
            const t = b.dataset.theme;
            const item = list.find((x) => x.theme === t);
            const mem = (item ? item.members : []).map((m) => `<span class="sc-mem" data-code="${esc(m.code)}">${esc(m.name)}</span>`).join("");
            const box2 = document.getElementById("scThemeMembers");
            if (box2) box2.innerHTML = mem;
            document.querySelectorAll("#scModal .sc-mem").forEach((s) => {
              s.onclick = () => { if (window.openStock) window.openStock(s.dataset.code, s); document.getElementById("scModal").style.display = "none"; };
            });
          };
        });
      }).catch((e) => hint(this.host, "题材失败：" + ((e && e.message) || e)));
    },
    /* ── 设置：DeepSeek API Key ── */
    settings() {
      api("/api/settings/deepseek_key").then((d) => {
        const configured = d && d.configured;
        modal("设置 · DeepSeek API Key",
          `<div class="sc-set">
            <p class="muted">Key 经 Fernet 加密后写入本机凭据库（与浏览器 Cookie 同库），<b>不再明文落盘、不上传、不经浏览器</b>，填一次即可。</p>
            <p class="muted">获取：登录 platform.deepseek.com → API Keys → 创建密钥；并在开放平台单独充值（网页会员 ≠ API 余额）。</p>
            <input id="scDsKey" class="sc-in2" type="password" placeholder="sk-..." style="width:100%;margin:6px 0" />
            <button class="btn sm" id="scDsSave">保存</button>
            <span id="scDsMsg" class="muted" style="margin-left:8px"></span>
            <p class="muted" style="margin-top:8px">当前状态：${configured ? "已配置 ✓" : "未配置"}</p>
          </div>`);
        const save = document.getElementById("scDsSave");
        if (save) save.onclick = () => {
          const key = document.getElementById("scDsKey").value.trim();
          if (!key) { document.getElementById("scDsMsg").textContent = "请输入 key"; return; }
          api("/api/settings/deepseek_key", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ key }) })
            .then((r) => { document.getElementById("scDsMsg").textContent = r.ok ? "已保存 ✓" : "保存失败：" + ((r.error) || ""); })
            .catch((e) => { document.getElementById("scDsMsg").textContent = "错误：" + ((e && e.message) || e); });
        };
      }).catch((e) => hint(this.host, "设置读取失败：" + ((e && e.message) || e)));
    },
  };
  window.SC_EXTRA = SC;
})();
