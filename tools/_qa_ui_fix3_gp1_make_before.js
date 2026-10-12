/* =============================================================================
 * make_before.js —— 生成「改动前」静态资源副本，用于 A/B 对照
 * 做法：读取仓库当前（已修）app.js / app.css，逐条**反向**替换回改动前的写法，
 *       产物 before_app.js / before_app.css 仅供 ui_qa_regression.js 在
 *       GP1_BEFORE=1 模式下由请求拦截替换响应，**不写回仓库**。
 * 注意：源文件为 CRLF 换行，替换前统一归一化为 LF，写出时再还原 CRLF。
 * 每条替换断言命中次数恰为 1，并核验「仅这几处发生变化」。
 * ========================================================================== */
"use strict";
const fs = require("fs");
const path = require("path");
const REPO = "F:/AI Agent部署知识库/2026-09-22-11-49-42/stock-forum-scraper/viewer/static";

function rev(src, pairs, label) {
  let out = src;
  for (const [now, was] of pairs) {
    const n = out.split(now).length - 1;
    if (n !== 1) throw new Error(`[${label}] 期望命中 1 次，实际 ${n} 次：${now.slice(0, 70).replace(/\n/g, "\\n")}`);
    out = out.split(now).join(was);
  }
  return out;
}
/* 逐行比较，确认改动落在预期范围内（防止误替换/漏替换） */
function diffReport(a, b) {
  const A = a.split("\n"), B = b.split("\n");
  const delta = B.length - A.length;
  let firstDiff = -1, lastDiff = -1, changed = 0;
  const n = Math.max(A.length, B.length);
  for (let i = 0; i < n; i++) if (A[i] !== B[i]) { changed++; if (firstDiff < 0) firstDiff = i; lastDiff = i; }
  return { delta, changed, firstDiff, lastDiff, aLines: A, bLines: B };
}
function printRegion(d, label) {
  console.log(`--- ${label} 变更区间：行 ${d.firstDiff + 1} ~ ${d.lastDiff + 1}（原始行号） ---`);
  for (let i = Math.max(0, d.firstDiff - 1); i <= Math.min(d.aLines.length - 1, d.lastDiff + 1); i++) {
    if (d.aLines[i] !== d.bLines[i]) console.log(`  L${i + 1} 改前> ${(d.aLines[i] || "").slice(0, 130)}`);
  }
}

const APP_JS_PAIRS = [
  /* (1) 「真实」角标：chip-real → chip-demo（改动前两者共用一个类） */
  [
    `<td>\${esc(t.author)} <span class="chip-s \${demo ? "chip-demo" : "chip-real"}">\${demo ? "示例" : "真实"}</span><div class="src-line">\${ch}</div></td>`,
    `<td>\${esc(t.author)} <span class="chip-s chip-demo">\${demo ? "示例" : "真实"}</span><div class="src-line">\${ch}</div></td>`,
  ],
  /* (2) 共识「持仓选手明细」角标 + 渠道占位：还原为无条件渲染 / 空数组直接 join */
  [
    `    // channel 为空时不渲染角标：/api/consensus 的 trades 兜底源把 channel 置 NULL（db.py NULL AS channel），
    // 而 ch-* 类自带背景色（app.css:207）——空文本 + 有色背景会渲染成一个无文字色块（UI 回归 C4）。
    // 这里刻意沿用原「渠道字母」紧凑写法而非 channelBadge()：后者输出「A 实盘大赛」全称，
    // 会把每行选手明细撑长，与本区块的紧凑排版不符。
    const players = toArr(x.players).map((pl) => {
      const chBadge = pl.channel
        ? \`<span class="badge ch-\${escAttr(pl.channel)}">\${esc(pl.channel)}</span> \`
        : "";
      return \`<div class="chip-row">\${chBadge}\${esc(pl.player)} \${actionBadge(pl.action)} \${pl.ratio != null ? esc(pl.ratio) + "%" : ""}</div>\`;
    }).join("");
    const sources = toArr(x.sources).map((s) => srcLink(s, "↗", { arrow: false })).join(" ");
    // 覆盖渠道为空数组时给占位「—」，与抽屉渲染（app.js:1149 \`\${... || "—"}\`）口径统一（UI 回归 C6）
    const channels = toArr(x.channels).filter(Boolean);`,
    `    const players = toArr(x.players).map((pl) =>
      \`<div class="chip-row"><span class="badge ch-\${escAttr(pl.channel || 'A')}">\${esc(pl.channel)}</span> \${esc(pl.player)} \${actionBadge(pl.action)} \${pl.ratio != null ? esc(pl.ratio) + "%" : ""}</div>\`
    ).join("");
    const sources = toArr(x.sources).map((s) => srcLink(s, "↗", { arrow: false })).join(" ");`,
  ],
  /* (3) 覆盖渠道单元格 */
  [
    `      <td>\${channels.length ? channels.map(channelBadge).join(" ") : "—"}</td>`,
    `      <td>\${toArr(x.channels).map(channelBadge).join(" ")}</td>`,
  ],
];

const APP_CSS_PAIRS = [
  /* (4) 删掉新增的 .chip-real 规则块 */
  [
    `/* 示例数据 chip（用在表格行内、紧贴选手名字） */
.chip-demo { background: #FEF3C7 !important; color: #92400E !important; border-color: #FBBF24 !important;
  font-weight: 600; }
/* 真实数据 chip —— 与上面的「示例」成对出现。
   2026-09-25：原先「真实」和「示例」共用 .chip-demo，结果 200 个「真实」角标被画成琥珀底 + 虚线框
   （琥珀/虚线是「示例」专属视觉语言），扫一眼容易把真实数据误判成示例（UI 回归 S1）。
   此处给「真实」一个中性正向的弱绿色**实线**观感，与「示例」的琥珀**虚线**形成可辨差异。
   色值硬编码（与 .chip-demo 同理）以保证四套主题下观感一致；前景/底色对比度 ≈ 6.5:1，高于「示例」chip 的 6.4:1。 */
.chip-real { display:inline-block; margin-left:4px; padding:1px 6px; border:1px solid #86EFAC;
  border-radius:6px; font-size:10px; font-weight:600; background:#DCFCE7 !important;
  color:#166534 !important; vertical-align:middle; }`,
    `/* 示例数据 chip（用在表格行内、紧贴选手名字） */
.chip-demo { background: #FEF3C7 !important; color: #92400E !important; border-color: #FBBF24 !important;
  font-weight: 600; }`,
  ],
];

const jsRaw = fs.readFileSync(path.join(REPO, "app.js"), "utf-8");
const cssRaw = fs.readFileSync(path.join(REPO, "app.css"), "utf-8");
const jsNow = jsRaw.replace(/\r\n/g, "\n");
const cssNow = cssRaw.replace(/\r\n/g, "\n");
const jsBefore = rev(jsNow, APP_JS_PAIRS, "app.js");
const cssBefore = rev(cssNow, APP_CSS_PAIRS, "app.css");

const jsDiff = diffReport(jsNow, jsBefore);
const cssDiff = diffReport(cssNow, cssBefore);
console.log("app.js  行数差 = " + jsDiff.delta + "，变更行数 = " + jsDiff.changed);
console.log("app.css 行数差 = " + cssDiff.delta + "，变更行数 = " + cssDiff.changed);
printRegion(jsDiff, "app.js");
printRegion(cssDiff, "app.css");
if (jsDiff.changed === 0 || cssDiff.changed === 0) throw new Error("未发生任何替换");
if (Math.abs(jsDiff.delta) > 20 || Math.abs(cssDiff.delta) > 20) throw new Error("行数变化超出预期范围");
if (jsRaw.length - jsBefore.replace(/\n/g, "\r\n").length > 1200) throw new Error("app.js 体积差超出预期");

fs.writeFileSync(path.join(__dirname, "before_app.js"), jsBefore.replace(/\n/g, "\r\n"), "utf-8");
fs.writeFileSync(path.join(__dirname, "before_app.css"), cssBefore.replace(/\n/g, "\r\n"), "utf-8");
console.log("OK：before_app.js(" + jsRaw.length + "B) / before_app.css(" + cssRaw.length + "B) 已生成（CRLF 已还原）");
