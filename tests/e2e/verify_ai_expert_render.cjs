/* 一次性探针：把 app.js 里「四家专家团」的**纯字符串渲染函数**抽出来在 node 里跑，
   验证 markdown 渲染（先转义再处理标记）、卡片文案、状态徽标。
   不涉及 DOM：paintAiExpert / loadAiExpert 需要浏览器环境，由页面自检负责。 */
const fs = require("fs");
const src = fs.readFileSync("viewer/static/app.js", "utf8");
const start = src.indexOf("const AIE = {");
const end = src.indexOf("/* 把整块插到 AI 复盘");
if (start < 0 || end < 0) { console.log("EXTRACT_FAIL"); process.exit(2); }
const block = src.slice(start, end);

const esc = (s) => (s == null ? "" : String(s)).replace(/[&<>]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" }[c]));
const escAttr = (s) => esc(s).replace(/"/g, "&quot;").replace(/'/g, "&#39;");
const AIR = { provider: "workbuddy", data: null };
const api = () => Promise.resolve(null);

const fn = new Function("esc", "escAttr", "AIR", "api", block +
  "; return { AIE, aieVisible, aieMd, aieBadge, aieCard, aieHtml, aieBoardHtml };");
const M = fn(esc, escAttr, AIR, api);

let fail = 0;
const ok = (cond, msg) => { console.log((cond ? "PASS " : "FAIL ") + msg); if (!cond) fail++; };

// 1) markdown：转义优先（<script> 不许活）、粗体、列表、表格
const md = "### 大盘\n<script>alert(1)</script>\n- 缩量回踩\n| 板块 | 阶段 |\n|---|---|\n| 算力 | 加速 |\n结论 **偏多**";
const h = M.aieMd(md);
ok(h.indexOf("&lt;script&gt;") > 0, "md 先转义：<script> 被转成实体");
ok(h.indexOf("<script>") < 0, "md 里没有可执行 script 标签");
ok(h.indexOf("<b>偏多</b>") > 0, "粗体被渲染");
ok(h.indexOf("aie-tbl") > 0 && h.indexOf("<td>算力</td>") > 0, "表格被渲染");
ok(h.indexOf("aie-li") > 0, "列表被渲染");
ok(h.indexOf("aie-h") > 0, "标题被渲染");

// 2) 徽标三分支
ok(M.aieBadge({ installed: false, status: "missing" }).indexOf("专家包未安装") > 0, "未安装 → 专家包未安装");
ok(M.aieBadge({ installed: true, status: "missing" }).indexOf("今日未跑") > 0, "已装未跑 → 今日未跑");
ok(M.aieBadge({ installed: true, status: "ok", stance: "偏多", position: "半仓以内" }).indexOf("偏多 · 半仓以内") > 0,
   "有报告 → 立场·仓位");

// 3) 卡片：picks 只显示前 3 条 + 溢出提示
const card = M.aieCard({
  key: "stock-partner", name: "腾讯自选股股票投研专家团", tags: ["估值定价"], installed: true,
  status: "ok", stance: "偏多", position: "半仓以内", verdict: "主线仍是算力",
  picks: [{ code: "600519", name: "贵州茅台", action: "买" }, { code: "000001", name: "平安银行", action: "观察" },
          { code: "300750", name: "宁德时代", action: "买" }, { code: "600000", name: "浦发银行", action: "买" }]
});
ok((card.match(/class="aie-pick"/g) || []).length === 3, "卡片只渲染前 3 条 picks");
ok(card.indexOf("另有 1 只") > 0, "溢出提示出现");
ok(card.indexOf("onclick=\"aieToggle('stock-partner')\"") > 0, "卡片可点开全文");

// 4) 四家空态：仍然是 4 张卡
AIR.provider = "workbuddy";
M.AIE.data = { ok: true, date: "2026-11-03", latest_date: "2026-11-03", items: [
  { key: "a", name: "A", tags: [], installed: true, status: "missing", picks: [] },
  { key: "b", name: "B", tags: [], installed: false, status: "missing", picks: [] },
  { key: "c", name: "C", tags: [], installed: true, status: "missing", picks: [] },
  { key: "d", name: "D", tags: [], installed: true, status: "missing", picks: [] }] };
ok((M.aieHtml().match(/aie-card/g) || []).length === 4, "空库也是 4 张卡（一家都不能少）");

// 5) 只在 workbuddy 分支显示
AIR.provider = "deepseek";
ok(M.aieVisible() === false, "DeepSeek 分支不显示专家团");
AIR.provider = "workbuddy";
ok(M.aieVisible() === true, "WorkBuddy 分支显示专家团");

// 6) 今日荐股 · 合并榜（2026-11-04）
const board = {
  min_score: 85, n_strong: 2, n_scored: 4, n_teams: 2,
  items: [
    { key: "stock-partner", team: "腾讯自选股股票投研专家团", code: "600642", name: "申能股份",
      score: 92, action: "回踩买入", reason: "电力+新能源双属性，跌停 141 只的环境里逆势收红",
      entry: "8.85~9.05", stop: "8.70", horizon: "1-2周", strong: true, co_teams: 2 },
    { key: "finance-suite", team: "金融业务全流程专家团", code: "600900", name: "长江电力",
      score: 86, action: "持有", reason: "股息率托底", entry: "—", stop: "28.90",
      horizon: "1月以上", strong: true, co_teams: 1 },
    { key: "five-wave", team: "股票五浪法专家团", code: "601988", name: "中国银行",
      score: 70, action: "观察", reason: "浪型未走完，仅作观察", entry: "—", stop: "—",
      horizon: "1-3日", strong: false, co_teams: 1 }
  ]
};
let bh = M.aieBoardHtml({ board });
ok(bh.indexOf("今日荐股 · 合并榜") > 0, "榜单标题出现");
ok((bh.match(/onclick="aiePkToggle\(/g) || []).length === 3, "三行都渲染");
ok(bh.indexOf(">92<") > 0 && bh.indexOf("aie-score hot") > 0, "≥85 的分数用达标样式");
ok(bh.indexOf("2 家共推") > 0, "共推标记出现");
ok(bh.indexOf("aieOpenStock('600642',this)") > 0, "代码可点开个股详情（调 openStock）");
ok(bh.indexOf("601988") > 0 && bh.indexOf('class="aie-score">70<') >= 0, "不足 85 的照样列出但不高亮");
ok(bh.indexOf("买点：8.85~9.05") < 0, "默认收起买点/止损（点开才展开）");
M.AIE.openPk = "stock-partner|600642";
bh = M.aieBoardHtml({ board });
ok(bh.indexOf("买点：8.85~9.05") > 0, "点开的那一行展开买点/止损/周期");
M.AIE.openPk = "";

// 旧报告（没打分）：不许编分数，只给一句解释
const old = M.aieBoardHtml({ board: { min_score: 85, n_strong: 0, n_scored: 0, n_teams: 0, items: [] } });
ok(old.indexOf("没有评分") > 0, "无评分时如实说明，不显示空榜");
ok(M.aieBoardHtml({}) === "", "接口没给 board（老后端）时整块不渲染，不报错");

// 理由里的 HTML 必须转义（专家的理由文本来自模型输出，不能直接当 HTML）
const xss = M.aieBoardHtml({ board: { min_score: 85, n_strong: 1, n_scored: 1, n_teams: 1, items: [
  { key: "a", team: "T", code: "600000", name: "<img src=x onerror=1>", score: 90,
    action: "买", reason: "<script>alert(1)</script>", strong: true, co_teams: 1 }] } });
ok(xss.indexOf("<script>") < 0 && xss.indexOf("&lt;script&gt;") > 0, "榜单文本先转义，不执行脚本");
ok(xss.indexOf("<img src=x") < 0, "股票名里的标签也转义");

console.log(fail ? ("RESULT FAIL " + fail) : "RESULT PASS");
process.exit(fail ? 1 : 0);
