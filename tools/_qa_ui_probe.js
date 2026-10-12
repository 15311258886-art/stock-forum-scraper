/* 数据一致性体检（只读）：把各 GET 接口拉一遍，统计条数并对账。
 * 用法：node tools/_qa_ui_probe.js [端口]   默认 8765
 * 只发 GET；不触碰任何写接口（/api/demo /api/fetch /api/overseas /api/login_check /api/sync_names）。
 * 输出：tools/_qa_ui_api.json（原始聚合） + 控制台文本
 */
const http = require("http");
const fs = require("fs");
const path = require("path");

const PORT = process.argv[2] || "8765";
const ROOT = path.join(__dirname, "..");
const OUT_JSON = path.join(ROOT, "tools", "_qa_ui_api.json");

function get(p) {
  return new Promise((resolve, reject) => {
    const req = http.get({ host: "127.0.0.1", port: PORT, path: p, agent: false }, (res) => {
      let d = "";
      res.on("data", (c) => (d += c));
      res.on("end", () => {
        let j = null, err = null;
        try { j = JSON.parse(d); } catch (e) { err = e.message + " | body[:200]=" + d.slice(0, 200); }
        resolve({ path: p, status: res.statusCode, json: j, parseError: err, rawLen: d.length });
      });
    });
    req.on("error", reject);
    req.setTimeout(20000, () => { req.destroy(new Error("timeout")); });
  });
}

(async () => {
  const paths = [
    "/api/summary", "/api/stocks", "/api/operating", "/api/persons", "/api/players",
    "/api/consensus?min_holders=2", "/api/topics", "/api/sentiment", "/api/positions",
    "/api/posts?limit=20", "/api/login_status", "/api/stock_names",
    "/api/overseas_sentiment", "/api/overseas_hotspots", "/api/overseas_sources",
  ];
  const res = {};
  for (const p of paths) {
    try { res[p] = await get(p); } catch (e) { res[p] = { path: p, error: String(e.message || e) }; }
  }
  const S = res["/api/summary"] ? res["/api/summary"].json : null;
  const arr = (x) => (Array.isArray(x) ? x : null);
  const len = (p) => { const j = res[p] && res[p].json; return Array.isArray(j) ? j.length : (j && typeof j === "object" ? Object.keys(j).length : null); };

  const stocks = arr(res["/api/stocks"] && res["/api/stocks"].json) || [];
  const operating = arr(res["/api/operating"] && res["/api/operating"].json) || [];
  const persons = arr(res["/api/persons"] && res["/api/persons"].json) || [];
  const players = arr(res["/api/players"] && res["/api/players"].json) || [];
  const consensus = arr(res["/api/consensus?min_holders=2"] && res["/api/consensus?min_holders=2"].json) || [];
  const topics = arr(res["/api/topics"] && res["/api/topics"].json) || [];
  const positions = arr(res["/api/positions"] && res["/api/positions"].json) || [];
  const posts20 = arr(res["/api/posts?limit=20"] && res["/api/posts?limit=20"].json) || [];
  const sentiment = res["/api/sentiment"] && res["/api/sentiment"].json;
  const ovs = res["/api/overseas_sentiment"] && res["/api/overseas_sentiment"].json;
  const ovh = arr(res["/api/overseas_hotspots"] && res["/api/overseas_hotspots"].json) || [];

  const sum = (a) => a.reduce((x, y) => x + (y || 0), 0);
  const mentionsSum = sum(stocks.map((x) => x.mentions));

  const report = {
    port: PORT,
    httpStatus: Object.fromEntries(Object.entries(res).map(([k, v]) => [k, v.status || v.error])),
    parseErrors: Object.fromEntries(Object.entries(res).filter(([, v]) => v.parseError).map(([k, v]) => [k, v.parseError])),
    counts: S ? S.counts : null,
    last_ingest: S ? S.last_ingest : null,
    sites: S ? S.sites : null,
    login_status: S ? S.login_status : null,
    listLengths: {
      stocks: stocks.length, operating: operating.length, persons: persons.length,
      players: players.length, consensus_min2: consensus.length, topics: topics.length,
      positions: positions.length, posts20: posts20.length,
      overseas_hotspots: ovh.length,
      overseas_news: ovs ? (ovs.news || []).length : null,
      overseas_symbols: ovs ? (ovs.symbols || []).length : null,
      overseas_daily: ovs ? (ovs.daily || []).length : null,
      sentiment_map: sentiment ? (sentiment.map || []).length : null,
    },
    crosscheck: {
      stock_mentions_count_from_summary: S ? S.counts.stock_mentions : null,
      sum_mentions_over_api_stocks: mentionsSum,
      stocks_api_len_vs_mentions: `${stocks.length} 只 / 提及合计 ${mentionsSum}`,
      posts_total: S ? S.counts.posts : null,
      topics_total: S ? S.counts.topics : null,
      topics_api_len: topics.length,
      players_total: S ? S.counts.players : null,
      positions_total: S ? S.counts.positions : null,
      operating_len_le_200: operating.length <= 200,
      consensus_holders_min: consensus.length ? Math.min(...consensus.map((x) => x.holders)) : null,
      sentiment_dist: sentiment ? sentiment.distribution : null,
      sentiment_dist_sum: sentiment ? sum(Object.values(sentiment.distribution || {})) : null,
      sentiment_name_covered: sentiment ? sentiment.name_covered : null,
      sentiment_name_total: sentiment ? sentiment.name_total : null,
      overseas_total: ovs ? ovs.total : null,
      overseas_net_mood: ovs ? ovs.net_mood : null,
      overseas_region_dist: ovs ? ovs.region_dist : null,
      overseas_daily_days: ovs ? [...new Set((ovs.daily || []).map((d) => d.day))].sort() : null,
      overseas_news_regions: ovs ? (ovs.news || []).reduce((m, n) => (m[n.region] = (m[n.region] || 0) + 1, m), {}) : null,
      overseas_news_missing_time: ovs ? (ovs.news || []).filter((n) => !n.publish_time).length : null,
      overseas_sources_status: res["/api/overseas_sources"] ? (res["/api/overseas_sources"].json.sources || []).map((s) => `${s.name}:${s.status}${s.count ? "(n=" + s.count + ")" : ""}`) : null,
    },
    samples: {
      stocks: stocks.slice(0, 5).map((x) => ({ symbol: x.symbol, name: x.name, mentions: x.mentions, last_time: x.last_time, name_missing: x.name_missing })),
      operating: operating.slice(0, 3).map((t) => ({ author: t.author, post_time: t.post_time, action: t.action, symbol: t.symbol, symbol_name: t.symbol_name, author_tier: t.author_tier, author_platform: t.author_platform })),
      posts: posts20.slice(0, 5).map((p) => ({ author: p.author, post_time: p.post_time, sentiment: p.sentiment })),
      overseas_news: ovs ? (ovs.news || []).slice(0, 5).map((n) => ({ region: n.region, source_name: n.source_name, title: (n.title || "").slice(0, 30), publish_time: n.publish_time, url: n.url })) : null,
      consensus: consensus.slice(0, 3).map((x) => ({ symbol: x.symbol, symbol_name: x.symbol_name, holders: x.holders, weighted: x.weighted_holders, discuss_heat: x.discuss_heat })),
    },
    raw_endpoint_status: res,
  };
  fs.writeFileSync(OUT_JSON, JSON.stringify(report, null, 2), "utf8");
  // 控制台只打摘要
  console.log(JSON.stringify({
    port: PORT, last_ingest: report.last_ingest,
    httpStatus: report.httpStatus, parseErrors: report.parseErrors,
    listLengths: report.listLengths, crosscheck: report.crosscheck,
  }, null, 2));
})().catch((e) => { console.error("PROBE ERROR: " + (e && e.stack || e)); process.exit(2); });
