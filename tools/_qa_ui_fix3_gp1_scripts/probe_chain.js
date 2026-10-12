"use strict";
const puppeteer = require("puppeteer-core");
const EXE = "C:/Users/野比&大熊/AppData/Local/ms-playwright/chromium-1234/chrome-win64/chrome.exe";
const PORT = process.argv[2] || "8765";
const wait = (ms) => new Promise((r) => setTimeout(r, ms));
(async () => {
  const browser = await puppeteer.launch({ executablePath: EXE, headless: true, args: ["--no-sandbox"] });
  const page = await browser.newPage();
  await page.setViewport({ width: 1440, height: 900 });
  await page.evaluateOnNewDocument(() => { window.confirm = () => false; });
  await page.setRequestInterception(true);
  page.on("request", (r) => { if (/\/api\/(demo|overseas|fetch)\b/.test(r.url())) return r.abort().catch(()=>{}); r.continue().catch(()=>{}); });
  page.on("dialog", (d) => d.dismiss().catch(()=>{}));
  await page.goto(`http://127.0.0.1:${PORT}`, { waitUntil: "networkidle2", timeout: 40000 });
  await wait(1200);
  // 遍历全部子面板，定位所有 .chip-demo 元素
  const cats = await page.evaluate(() => [...document.querySelectorAll("#sidenav .nav-item")].map(a=>a.dataset.cat));
  const found = [];
  for (const c of cats) {
    await page.evaluate((k)=>document.querySelector(`#sidenav .nav-item[data-cat="${k}"]`).click(), c);
    await wait(500);
    const subs = await page.evaluate(() => [...document.querySelectorAll("#subnav .sub-tab")].map(a=>a.dataset.tab));
    for (const t of subs) {
      await page.evaluate((x)=>{const a=document.querySelector(`#subnav .sub-tab[data-tab="${x}"]`); if(a)a.click();}, t);
      await wait(900);
      const d = await page.evaluate((tab) => {
        const out = [];
        document.querySelectorAll("#p-"+tab+" .chip-demo").forEach((e)=>{
          const cs = getComputedStyle(e);
          out.push({ text:(e.textContent||"").trim().slice(0,20), cls:e.className, bg:cs.backgroundColor, panel:tab,
            hint:(e.parentElement?e.parentElement.innerText:"").replace(/\s+/g," ").slice(0,60) });
        });
        return out;
      }, t);
      if (d.length) found.push(...d);
    }
  }
  console.log("=== 全站 .chip-demo 元素（共 "+found.length+"） ===");
  const byPanel = {};
  found.forEach((f)=>{ (byPanel[f.panel]=byPanel[f.panel]||[]).push(f); });
  Object.keys(byPanel).forEach((p)=>{ console.log("  ["+p+"] "+byPanel[p].length+' 个，样例：'+JSON.stringify(byPanel[p][0])); });

  // 祖先背景链（在 operating 面板）
  await page.evaluate(()=>document.querySelector('#sidenav .nav-item[data-cat="trade"]').click());
  await wait(400);
  await page.evaluate(()=>{const a=document.querySelector('#subnav .sub-tab[data-tab="operating"]');if(a)a.click();});
  await wait(1500);
  const chain = await page.evaluate(() => {
    const el = document.querySelector("#p-operating .chip-s.chip-real");
    const out = [];
    let n = el;
    while (n && n !== document.documentElement) {
      const cs = getComputedStyle(n);
      out.push({ tag: n.tagName.toLowerCase(), cls: (n.className||"").toString().slice(0,60), bg: cs.backgroundColor, id: n.id||"" });
      n = n.parentElement;
    }
    return out;
  });
  console.log("=== 「真实」chip 祖先背景链 ===");
  chain.slice(0, 12).forEach((c,i)=>console.log("  "+i+" "+c.tag+(c.id?"#"+c.id:"")+(c.cls?"."+c.cls:"")+"  bg="+c.bg));
  await browser.close();
})().catch(e=>{console.log("ERR "+(e&&e.stack||e));process.exit(2);});
