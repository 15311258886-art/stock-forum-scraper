/* 2026-09-28 验证「板块资金流排行」代表股列：
   把真实 API 数据喂给 app.js 里**真实的**渲染片段（不是复刻），断言产出的 HTML。
   再做一张静态预览页（引用查看器真实 app.css），供肉眼核对排版。 */
const fs = require("fs");
const http = require("http");
const path = require("path");

const ROOT = __dirname;
const src = fs.readFileSync(path.join(ROOT, "viewer/static/app.js"), "utf8");

function between(a, b) {
  const i = src.indexOf(a);
  if (i < 0) throw new Error("找不到起始标记: " + a.slice(0, 60));
  const j = src.indexOf(b, i);
  if (j < 0) throw new Error("找不到结束标记: " + b.slice(0, 60));
  return src.slice(i, j + b.length);
}
function grabFunc(name) {
  const i = src.indexOf("function " + name + "(");
  if (i < 0) throw new Error("找不到函数: " + name);
  let s = src.indexOf("{", i), depth = 0, j = s;
  for (; j < src.length; j++) {
    if (src[j] === "{") depth++;
    else if (src[j] === "}") { depth--; if (!depth) break; }
  }
  return src.slice(i, j + 1);
}

// 抽取真实代码：工具函数 + board 分支的整段渲染（含表格）
const helpers = ["esc", "escAttr", "wtMoney", "wtPct", "featMoney"].map(grabFunc).join("\n");
const core = between(
  "    const k = mf.boardKind;",
  "      </tr></thead><tbody>${rows}</tbody></table></div>`;"
);

function get(p) {
  return new Promise((res, rej) => {
    http.get({ host: "127.0.0.1", port: 8765, path: p, timeout: 60000 }, (r) => {
      let b = "";
      r.setEncoding("utf8");
      r.on("data", (c) => (b += c));
      r.on("end", () => { try { res(JSON.parse(b)); } catch (e) { rej(e); } });
    }).on("error", rej);
  });
}

const renderBody = new Function(
  "mf", "d", "helpers", "core",
  helpers + "\n" + core + "\n return body;"
);

function count(re, s) { return (s.match(re) || []).length; }

(async () => {
  let pass = 0, fail = 0;
  const report = [];
  const check = (name, ok, extra) => {
    (ok ? pass++ : fail++);
    report.push(`${ok ? "PASS" : "FAIL"}  ${name}${extra ? "  — " + extra : ""}`);
  };

  const html = [];
  for (const kind of ["industry", "concept"]) {
    const snap = await get(`/api/board_flow_snapshot?kind=${kind}&pz=40&force=1`);
    const curve = await get(`/api/board_flow_intraday?kind=${kind}&top=16`);
    const mf = { mode: "board", boardKind: kind, snap, curve };
    const body = renderBody(mf, snap, helpers, core);
    const rows = snap.rows || [];
    const rowsWithMem = rows.filter((r) => (r.members || []).length).length;
    const nLink = count(/data-wt-open="/g, body);
    const nTr = count(/<tr>/g, body);
    const thN = count(/<th>/g, body);
    const colN = count(/<col[ >]/g, body);
    // 每行 td 数应一致（trs[0] 是表头行，故取第一条数据行）
    const trs = body.match(/<tr>[\s\S]*?<\/tr>/g) || [];
    const firstTr = trs[1] || "";
    const tdN = count(/<td/g, firstTr);

    check(`[${kind}] 表格用了紧凑列宽类 bf-tb`, body.includes("wt-tb bf-tb"));
    check(`[${kind}] 列宽用 colgroup 固定`, colN > 0, `col=${colN}`);
    check(`[${kind}] 表头列数 = 数据行列数`, thN === tdN, `th=${thN} td=${tdN}`);
    check(`[${kind}] 新浪源不渲染空的超大单/大单列（应为 6 列）`, thN === 6, `th=${thN}`);
    check(`[${kind}] 代表股链接已生成`, nLink > 0, `data-wt-open=${nLink} 条（带代表股的行=${rowsWithMem}）`);
    check(`[${kind}] 每条链接都是 6 位数字代码`,
      (body.match(/data-wt-open="([^"]*)"/g) || []).every((x) => /"\d{6}"$/.test(x)));
    check(`[${kind}] 无 NaN / undefined 泄漏`, !/NaN|undefined/.test(body));
    check(`[${kind}] 表格行数 = 返回行数（+1 表头）`, nTr - 1 === rows.length, `tr=${nTr} rows=${rows.length}`);
    check(`[${kind}] 代表股一列多只（首行应 12 只）`,
      count(/class="bf-mem"/g, firstTr) === 12, `首行 bf-mem=${count(/class="bf-mem"/g, firstTr)}`);
    // 龙头口径校验（2026-09-28 订正）：改为 sort=mktcap 后，代表股必须是行业龙头而非当日涨停小票。
    // 白酒板块若出现，首位必须是贵州茅台；汽车制造首位必须是比亚迪。
    // （若该板块当日不在返回行内则跳过，避免数据波动导致误报）
    const headNames = (r) => ((r.members || [])[0] ? String((r.members || [])[0].name).replace(/\s/g, "") : "");
    const expectLeader = (key, want) => {
      const row = rows.find((r) => String(r.board_name || "").includes(key));
      check(`[${kind}] 龙头口径：${key} 首位 = ${want}`,
        row ? headNames(row) === want : true,
        row ? `实际首位=${headNames(row) || "(无)"}` : "该板块不在返回行内，跳过");
    };
    expectLeader("酿酒", "贵州茅台");
    expectLeader("汽车制造", "比亚迪");
    const memCounts = rows.map((r) => (r.members || []).filter((m) => /^\d{6}$/.test(String(m.code || ""))).length);
    const memMax = memCounts.length ? Math.max(...memCounts) : 0;
    check(`[${kind}] 每行代表股 ≤ 12 只（对齐后端 MEM_NUM=12）`, memMax <= 12, `最大=${memMax}`);

    html.push(`<h2>${kind === "industry" ? "行业板块" : "概念板块"}（真实接口数据 · 真实渲染代码）</h2>
<div class="wrap">${body}</div>`);
  }

  console.log(report.join("\n"));
  console.log(`\n合计：${pass} PASS / ${fail} FAIL`);

  // 静态预览页（引用查看器真实样式）
  const page = `<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<title>板块资金流排行 · 代表股改造预览</title>
<link rel="stylesheet" href="http://127.0.0.1:8765/static/app.css">
<style>
:root{--bg:#f7f8fa;--panel:#fff;--text:#1f2328;--muted:#6b7280;--border:#e5e7eb;--accent:#0E7490;
      --bull:#c81e1e;--bear:#15803d;}
body{background:var(--bg);color:var(--text);font:14px/1.6 system-ui,-apple-system,"Microsoft YaHei",sans-serif;
     margin:0;padding:20px 24px 60px;}
h1{font-size:17px;margin:0 0 4px;}
h2{font-size:14px;margin:26px 0 8px;color:var(--muted);font-weight:600;}
.note{font-size:12.5px;color:var(--muted);margin:0 0 14px;}
.wrap{background:var(--panel);border:1px solid var(--border);border-radius:10px;padding:12px 14px;}
.wt-tw{overflow-x:auto;}
table{width:100%;border-collapse:collapse;font-size:13px;}
th,td{text-align:left;border-bottom:1px solid var(--border);}
th{color:var(--muted);font-weight:600;background:#fafbfc;}
.muted{color:var(--muted);}
.bull{color:var(--bull);}.bear{color:var(--bear);}
</style></head><body>
<h1>板块资金流排行 · 「代表股」列改造预览</h1>
<p class="note">下面是查看器里<b>同一份渲染代码</b>用<b>真实接口数据</b>产出的 HTML：
代表股一列并排 8 只、每只可点（点开即个股详情）；<b>口径为「按总市值取行业龙头」</b>
（如酿酒行业出贵州茅台/五粮液，汽车制造出比亚迪/潍柴动力），不再是当日涨停小票。
上方各列已收窄，宽度让给代表股。
超大单 / 大单两列因新浪源无数据已不渲染（东财源可用时会自动出现）。</p>
${html.join("\n")}
</body></html>`;
  fs.writeFileSync(path.join(ROOT, "_preview_boardflow.html"), page, "utf8");
  console.log("预览页已生成: _preview_boardflow.html");
})();
