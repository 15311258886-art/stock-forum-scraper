/* 避雷风险标记 · 逻辑级回归（不依赖线上服务，纯前端逻辑验证）
 * 把 app.js 里新增的 avoidDecorate / buildAvoidSet / avoidTooltip 原样搬来，
 * 配最小 DOM 桩，喂入与 /api/avoid 同构的假数据，断言：
 *   ① 匹配 AVOID_SET 的代码锚点自动挂 .avoid-badge（红标）
 *   ② 不匹配的代码不挂
 *   ③ 同一锚点重复调用不重复插入（data-av 去重）
 *   ④ title 含风险标签文案
 */
"use strict";

// ---- 最小 DOM 桩 ----
function makeEl(tag) {
  return {
    tag, attributes: {}, children: [], parent: null, className: "", textContent: "", title: "",
    setAttribute(k, v) { this.attributes[k] = String(v); },
    getAttribute(k) { return k in this.attributes ? this.attributes[k] : null; },
    insertAdjacentElement(pos, node) {
      if (pos === "afterend" && this.parent) {
        const i = this.parent.children.indexOf(this);
        this.parent.children.splice(i + 1, 0, node);
        node.parent = this.parent;
      }
      return node;
    },
    // 仅支持本工程用到的选择器：':scope > .avoid-badge[data-av-for="X"]'
    querySelector(sel) {
      const m = sel.match(/^\s*:scope\s*>\s*\.([\w-]+)\[data-av-for="([^"]+)"\]\s*$/);
      if (!m) return null;
      const cls = m[1], val = m[2];
      return this.children.find((c) =>
        ((" " + c.className + " ").indexOf(" " + cls + " ") >= 0) &&
        c.getAttribute("data-av-for") === val) || null;
    },
  };
}
function walk(el, fn) { fn(el); for (const c of el.children) walk(c, fn); }
// 根节点 querySelectorAll：匹配本工程四种个股锚点属性之一
const SEL_ATTRS = ["data-wt-open", "data-code-open", "data-feat-open"];
function rootQueryAll(root) {
  const out = [];
  walk(root, (el) => {
    if (el.tag === "span" && el.attributes["data-sym"]) { out.push(el); return; }
    if (SEL_ATTRS.some((a) => a in el.attributes)) out.push(el);
  });
  return out;
}
const document = {
  createElement: makeEl,
  querySelectorAll: (sel) => rootQueryAll(globalRoot),
};

// ---- 桩依赖 ----
function toArr(x) { return Array.isArray(x) ? x : []; }
function escAttr(s) { return String(s == null ? "" : s); }
let AVOID_SET = {};
function api() { return Promise.resolve({}); }
function setTimeout(fn) { fn(); return 0; }
const globalRoot = makeEl("body");

// ============ 以下为 app.js 原样搬入 ============
function buildAvoidSet(d) {
  const map = {};
  const items = toArr((d && d.items) || []);
  for (const it of items) {
    const c = String((it && it.code) == null ? "" : it.code).trim();
    if (!/^\d{6}$/.test(c)) continue;
    map[c] = it;
  }
  AVOID_SET = map;
}

function avoidTooltip(it) {
  if (!it) return "避雷名单 · 下跌风险";
  const rs = (it.res_score == null ? "" : "（低分 " + it.res_score + "）");
  const reasons = toArr(it.reasons).map((r) =>
    "· " + (r && r.tag ? r.tag : "") + (r && r.why ? "：" + r.why : "")).join("\n");
  return "⚠ 避雷名单 " + escAttr(rs) + (reasons ? "\n风险标签：\n" + reasons : "");
}

function avoidDecorate(root) {
  if (!AVOID_SET || !Object.keys(AVOID_SET).length) return;
  const box = root && root.querySelectorAll ? root : document;
  const SEL = "[data-wt-open],[data-code-open],[data-feat-open],span.kline-chip[data-sym]";
  const els = box.querySelectorAll(SEL);
  for (const el of els) {
    if (el.getAttribute("data-av") === "1") continue;
    el.setAttribute("data-av", "1");
    const code = el.getAttribute("data-wt-open") || el.getAttribute("data-code-open")
      || el.getAttribute("data-feat-open") || el.getAttribute("data-sym") || "";
    const it = AVOID_SET[String(code).trim()];
    if (!it) continue;
    if (el.parentNode && el.parentNode.querySelector(':scope > .avoid-badge[data-av-for="' + code + '"]')) continue;
    const sp = document.createElement("span");
    sp.className = "avoid-badge";
    sp.setAttribute("data-av-for", code);
    sp.textContent = "⚠避雷";
    sp.title = avoidTooltip(it);
    if (el.insertAdjacentElement) el.insertAdjacentElement("afterend", sp);
  }
}
// ================================================

// ---- 构造测试 DOM（模拟「长期埋伏」面板里出现的两只避雷股 + 一只普通股）----
const panel = makeEl("div"); panel.parent = globalRoot; globalRoot.children.push(panel);
function addCodeLink(code, attr) {
  const a = makeEl("a");
  a.setAttribute(attr, code);
  a.parent = panel;
  panel.children.push(a);
  return a;
}
const hit1 = addCodeLink("600519", "data-code-open");   // 在避雷名单
const hit2 = addCodeLink("000001", "data-code-open");   // 在避雷名单（另一入口）
const miss = addCodeLink("300750", "data-code-open");   // 不在避雷名单
const kchip = makeEl("span"); kchip.setAttribute("data-sym", "600519"); kchip.parent = panel; panel.children.push(kchip); // K线小胶囊也应命中

// ---- 假数据（与 /api/avoid 同构）----
const payload = {
  ok: true,
  items: [
    { code: "600519", name: "贵州茅台", res_score: -4.7, reasons: [{ tag: "有龙虎榜异动", why: "近5日3次上榜" }, { tag: "情绪偏空", why: "净空头占优" }] },
    { code: "000001", name: "平安银行", res_score: -2.1, reasons: [{ tag: "无明确策略", why: "无人给出买点" }] },
  ],
};

// ---- 执行 ----
buildAvoidSet(payload);
avoidDecorate(globalRoot);
avoidDecorate(globalRoot); // 二次调用验证去重

// ---- 断言 ----
let pass = 0, fail = 0;
function assert(cond, msg) { if (cond) { pass++; console.log("  PASS:", msg); } else { fail++; console.log("  FAIL:", msg); } }

const badgeOf = (anchor) => anchor.parent && anchor.parent.children.find((c) =>
  c !== anchor && (" " + c.className + " ").indexOf(" avoid-badge ") >= 0 && c.getAttribute("data-av-for") === anchor.getAttribute("data-code-open"));

assert(!!badgeOf(hit1), "600519 锚点挂上 ⚠避雷 红标");
assert(!!badgeOf(hit2), "000001 锚点挂上 ⚠避雷 红标");
assert(!badgeOf(miss), "300750（非避雷）未挂标");
const kb = kchip.parent.children.find((c) => (" " + c.className + " ").indexOf(" avoid-badge ") >= 0 && c.getAttribute("data-av-for") === "600519");
assert(!!kb, "K线小胶囊(data-sym=600519)也命中红标");

// 去重：600519 应只出现 1 枚徽章（hit1 与 kchip 是不同锚点，应各 1 枚 = 2 枚；检查不重复即 OK）
const allBadges = [];
walk(globalRoot, (el) => { if ((" " + el.className + " ").indexOf(" avoid-badge ") >= 0) allBadges.push(el); });
assert(allBadges.length === 3, "共挂 3 枚标（600519 两次出现各1 + 000001 1），无重复插入，实际=" + allBadges.length);

// tooltip 文案
const b1 = badgeOf(hit1);
assert(/避雷名单/.test(b1.title) && /有龙虎榜异动/.test(b1.title), "红标 title 含风险标签文案");

console.log("\n结果: " + pass + " passed, " + fail + " failed");
process.exit(fail ? 1 : 0);
