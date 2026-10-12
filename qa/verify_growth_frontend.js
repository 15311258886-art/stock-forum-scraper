'use strict';
// qa/verify_growth_frontend.js
// 复盘成长面板 · 前端验证：在 Node vm 中加载真实 app.js，执行 growthSvg 验证「折线数 = 输入序列数（动态、绝不硬编码 6/12）」
// 同时做语法校验：node --check app.js / verify_growth.cjs
const fs = require('fs');
const vm = require('vm');
const path = require('path');
const assert = require('assert');

const ROOT = path.resolve(__dirname, '..');
const APP_JS = path.join(ROOT, 'viewer', 'static', 'app.js');
const VERIFY_CJS = path.join(ROOT, 'viewer', 'static', 'verify_growth.cjs');

let fails = 0;
function check(cond, msg) {
  console.log((cond ? '  PASS: ' : '  FAIL: ') + msg);
  if (!cond) fails++;
}

// ---------- 1) 语法校验 ----------
function syntaxCheck(file) {
  const code = fs.readFileSync(file, 'utf8');
  new vm.Script(code, { filename: file }); // 抛错即语法错误
  console.log('  PASS: 语法校验通过 ' + path.basename(file) + '（' + code.split('\n').length + ' 行）');
}
console.log('[1] 语法校验');
syntaxCheck(APP_JS);
syntaxCheck(VERIFY_CJS);

// ---------- 2) 在 vm 中加载真实 app.js ----------
console.log('[2] 加载真实 app.js 并执行 growthSvg（动态维数证明）');
// 通用 DOM / 浏览器全局桩：任何属性访问都返回一个可调用、可赋值、可继续访问的代理
function makeStub() {
  const fn = function () { return stub; };
  const stub = new Proxy(fn, {
    get(_t, p) {
      if (p === 'length') return 0;
      if (p === Symbol.iterator) return function* () {};
      if (p === 'forEach' || p === 'map' || p === 'filter' || p === 'reduce') return function () { return stub; };
      if (p === 'dataset' || p === 'style' || p === 'classList') return makeStub();
      if (p === 'then') return undefined; // 不是 Promise
      return makeStub();
    },
    set() { return true; },
    apply() { return makeStub(); },
  });
  return stub;
}
const docStub = makeStub();
const winStub = makeStub();
const sandbox = {
  console,
  document: Object.assign(docStub, { addEventListener() {}, querySelector() { return makeStub(); }, querySelectorAll() { return makeStub(); }, createElement() { return makeStub(); }, getElementById() { return makeStub(); }, body: makeStub() }),
  window: Object.assign(winStub, { addEventListener() {}, devicePixelRatio: 1, innerWidth: 1280, innerHeight: 800, location: { hash: '', href: '' } }),
  navigator: { userAgent: 'node' },
  location: { hash: '', href: '', pathname: '', search: '', reload() {}, assign() {} },
  history: { pushState() {}, replaceState() {} },
  localStorage: { getItem() { return null; }, setItem() {} },
  setTimeout() { return 0; },
  setInterval() { return 0; },
  clearTimeout() {},
  clearInterval() {},
  requestAnimationFrame() { return 0; },
  cancelAnimationFrame() {},
  fetch() { return Promise.reject(new Error('no network in test')); },
  Math, Date, JSON, Promise, RegExp, Array, Object, String, Number, parseInt, parseFloat, isNaN, Symbol,
};
class Observer { observe() {} disconnect() {} unobserve() {} }
sandbox.MutationObserver = Observer;
sandbox.IntersectionObserver = Observer;
sandbox.ResizeObserver = Observer;
sandbox.getComputedStyle = function () { return makeStub(); };
sandbox.CSS = { supports() { return false; } };
sandbox.globalThis = sandbox;
const ctx = vm.createContext(sandbox);
const appSrc = fs.readFileSync(APP_JS, 'utf8');
vm.runInContext(appSrc, ctx, { filename: APP_JS });

const growthSvg = ctx.growthSvg;
check(typeof growthSvg === 'function', 'growthSvg 已从 app.js 真实加载为函数');

function series(n, pts) {
  const arr = [];
  for (let i = 0; i < n; i++) {
    const points = [];
    for (let k = 0; k < pts; k++) points.push({ x: k / Math.max(1, pts - 1), y: 0.1 + 0.01 * k });
    arr.push({ key: 's' + i, color: '#2563eb', points, name: 'S' + i });
  }
  return arr;
}
function countPoly(svg) { return (svg.match(/<polyline/g) || []).length; }

// 核心：折线数 = 输入序列数（每条 >=2 点才画）
const s12 = series(12, 3);
const out12 = growthSvg(s12, { label: 'x' });
check(countPoly(out12) === 12, '12 条序列 → 12 条 <polyline>（绝不硬编码 6）');

const s7 = series(7, 3);
check(countPoly(growthSvg(s7, { label: 'x' })) === 7, '7 条序列 → 7 条 <polyline>（随输入长度变化）');

const s3_1 = series(3, 3).concat(series(1, 1)); // 3 条 3 点 + 1 条 1 点(不画)
check(countPoly(growthSvg(s3_1, { label: 'x' })) === 3, '4 条序列中 1 条仅 1 点 → 仅 3 条线（<2 点不连线）');

// <3 点画虚线：单条 2 点 → 有 stroke-dasharray；单条 3 点 → 无
const two = growthSvg(series(1, 2), { label: 'x' });
check(/stroke-dasharray/.test(two), '2 点序列画虚线（stroke-dasharray 存在）');
const three = growthSvg(series(1, 3), { label: 'x' });
check(!/stroke-dasharray/.test(three), '3 点序列不画虚线（solid）');

// 空序列不崩
check(countPoly(growthSvg([], { label: 'x' })) === 0, '空序列 → 0 条线且不抛错');
check(typeof growthSvg(undefined, { label: 'x' }) === 'string', 'growthSvg(undefined) 不抛错（空安全）');

console.log();
if (fails) { console.error('❌ verify_growth_frontend 失败 ' + fails + ' 项'); process.exit(1); }
console.log('✅ verify_growth_frontend 全部通过');
