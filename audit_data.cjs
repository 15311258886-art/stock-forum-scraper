// 数据打通 + 数据连贯性 回归审计 v2（修正参数名 + 连贯性解析 + 失败分类）
const BASE = 'http://127.0.0.1:8765';
const sleep = (ms) => new Promise(r => setTimeout(r, ms));

function recCount(body) {
  if (body == null) return 0;
  if (Array.isArray(body)) return body.length;
  if (typeof body === 'object') {
    if (body.ok === false) return -1;
    if (body.error) return -1;
    for (const k of ['items','list','data','rows','stocks','reports','records','picks','signals','sources','tracks','persons','players','posts','depts','seats','branches','tiers','quotes','codes','results','points','edges','nodes','pools','alerts','catalog','strategies'])
      if (Array.isArray(body[k])) return body[k].length;
    if (body.data && typeof body.data === 'object' && Array.isArray(body.data.items)) return body.data.items.length;
    // 有真实字段的非空字典
    const ks = Object.keys(body);
    if (ks.length === 0) return 0;
    return ks.length;
  }
  return 0;
}

function classify(res) {
  if (res.err) return 'NET_FAIL:' + res.err;
  if (res.status && res.status !== 200) return 'HTTP_' + res.status;
  if (res.parseErr) return 'JSON_PARSE_FAIL';
  if (!res.json) return 'NO_BODY';
  const j = res.json;
  if (j.ok === false) {
    const reason = (j.reason || j.error || '').toString();
    if (/东财|eastmoney|不可达|限流|屏蔽|push2/.test(reason)) return 'SRC_EM_DOWN';
    if (/代码|输入|required|symbol|name|id|参数/.test(reason)) return 'PARAM_ISSUE';
    return 'OK_FALSE:' + reason.slice(0, 60);
  }
  if (j.error) return 'ERR:' + (j.error || '').toString().slice(0, 60);
  const c = recCount(j);
  if (c === 0) return 'EMPTY_OK';
  return 'OK';
}

async function get(path, timeoutMs = 20000) {
  const ctrl = new AbortController();
  const t = setTimeout(() => ctrl.abort(), timeoutMs);
  try {
    const r = await fetch(BASE + path, { signal: ctrl.signal, headers: { Accept: 'application/json' } });
    const txt = await r.text();
    let json = null, parseErr = false;
    try { json = JSON.parse(txt); } catch (e) { parseErr = true; }
    return { status: r.status, bytes: txt.length, json, parseErr };
  } catch (e) {
    return { status: 0, bytes: 0, json: null, parseErr: false, err: e.name === 'AbortError' ? 'TIMEOUT' : e.message.slice(0, 100) };
  } finally { clearTimeout(t); }
}

// 无参 GET（不含需要 symbol 的 valuation/diag/research_rating，它们放带参）
const NO_PARAM = [
  '/api/summary','/api/stocks','/api/operating','/api/persons','/api/strategies','/api/fund_strategies',
  '/api/public_records','/api/sentiment','/api/topics','/api/hotspots','/api/refresh_status','/api/scheduler/status',
  '/api/kb/list','/api/raw_pages','/api/players','/api/overseas_sentiment','/api/overseas_hotspots','/api/overseas_sources',
  '/api/geo_events','/api/extref_signals','/api/extref_backtest','/api/positions','/api/lhb','/api/lhb_sectors',
  '/api/niusan','/api/star_moves','/api/dashboard','/api/consensus','/api/cohold_recent','/api/posts',
  '/api/login_status','/api/prediction/today','/api/prediction/scores','/api/prediction/track','/api/prediction/status',
  '/api/prediction/dates','/api/screener','/api/board_flow_snapshot','/api/board_flow_status','/api/stock_names',
  '/api/watch_list','/api/watch_alerts','/api/research_reports','/api/multiquote_preset','/api/news_market',
  '/api/policy_desk'
];

(async () => {
  const rows = [];
  const log = (tag, name, res) => {
    const cls = classify(res);
    const c = res.json ? recCount(res.json) : 0;
    const ok = cls === 'OK' || cls === 'EMPTY_OK';
    rows.push({ name, cls, status: res.status || 0, bytes: res.bytes, count: c, issues: cls === 'OK' || cls === 'EMPTY_OK' ? [] : [cls] });
    console.log(`${ok ? (cls==='EMPTY_OK'?'·':'✓') : '✗'} [${tag}] ${name} | HTTP:${res.status||0} 字节:${res.bytes} 记录:${c} | ${cls}`);
  };

  console.log('=== ① 数据打通：无参 GET 端点 ===');
  for (const p of NO_PARAM) { log('nop', p, await get(p)); await sleep(40); }

  // 动态取真实参数（用"第一个数组值"兜底，避免猜键名出错）
  const firstArr = (j) => { if (!j) return []; if (Array.isArray(j)) return j; const v = Object.values(j).find(x => Array.isArray(x)); return v || []; };
  const players = await get('/api/players'); const pArr = firstArr(players.json);
  const persons = await get('/api/persons'); const perArr = firstArr(persons.json);
  const ex = await get('/api/extref_signals'); const exArr = firstArr(ex.json);
  const cons = await get('/api/consensus'); const consArr = firstArr(cons.json);
  const bf = await get('/api/board_flow_snapshot'); const bArr = firstArr(bf.json);
  const posts = await get('/api/posts'); const postArr = firstArr(posts.json);

  const SYM = '600938';
  const playerName = (pArr[0] && (pArr[0].name || pArr[0].stock_name)) || '';
  const personName = (perArr[0] && (perArr[0].name || perArr[0].stock_name)) || playerName;
  const exSid = (exArr[0] && (exArr[0].sid)) || '';
  const exKey = (exArr[0] && (exArr[0].source_key || exArr[0].key)) || '';
  const consItem = consArr[0] || {};
  const sector = consItem.sector || consItem.name || '';
  const branch = consItem.branch || '';
  const boardCode = (bArr[0] && (bArr[0].board_code || bArr[0].code || bArr[0].secid)) || 'new_fdc';
  const detailId = (postArr[0] && (postArr[0].id)) || '';

  console.log(`\n动态参数: SYM=${SYM} player=${playerName} person=${personName} exSid=${exSid} exKey=${exKey} sector=${sector} branch=${branch} board=${boardCode} detailId=${detailId}`);

  const PARAMED = [
    [`/api/stock?symbol=${SYM}`, '个股页'],
    [`/api/quote?symbol=${SYM}`, '实时行情(东财)'],
    [`/api/kline?symbol=${SYM}`, 'K线(东财)'],
    [`/api/moneyflow_stock?symbol=${SYM}`, '个股资金流(东财)'],
    [`/api/valuation?symbol=${SYM}`, '估值报表'],
    [`/api/diag?symbol=${SYM}`, '体检诊断'],
    [`/api/research_rating?symbol=${SYM}`, '研报评级'],
    [`/api/news_stock?symbol=${SYM}`, '个股新闻'],
    [`/api/multiquote_lookup?symbol=${SYM}`, '多资产查询'],
    [`/api/player?name=${encodeURIComponent(playerName)}`, '主力详情'],
    [`/api/person?name=${encodeURIComponent(personName)}`, '人物详情'],
    [`/api/board?code=${boardCode}`, '板块详情'],
    // 注意：该接口的参数是 kind/top，**不认 code**（签名为 api_board_flow_intraday(kind, top, force)）。
    // 曾误传 code → 参数被忽略 → 落到"东财风控 + 无采样"分支 → 假红。
    // 另：非交易时段 + 东财分钟风控时它本就返回 ok:false（自带诚实说明），属已知外部限制。
    [`/api/board_flow_intraday?kind=industry&top=12`, '板块资金流分时'],
    [`/api/detail?kind=post&id=${detailId}`, '明细详情'],
    [`/api/detail_list?kind=posts&limit=20`, '明细列表'],
    [`/api/branch_detail?sector=%E7%A6%8F%E5%BB%BA%E6%9D%BF%E5%9D%97&branch=%E6%95%B0%E5%AD%97%E7%BB%8F%E6%B5%8E`, '分支明细'],
  ];
  if (exSid) PARAMED.push([`/api/extref_signal_detail?sid=${exSid}`, '外部信号明细']);
  if (exKey) PARAMED.push([`/api/extref_source_detail?key=${exKey}`, '外部来源明细']);

  console.log('\n=== ① 带参端点（正确参数名）===');
  for (const [p, label] of PARAMED) { log('par', label+' '+p.split('?')[0], await get(p, 25000)); await sleep(80); }

  // ② 数据连贯性
  console.log('\n=== ② 数据连贯性 ===');
  const cont = {};
  const pd = await get('/api/prediction/dates');
  const datesList = (pd.json&&pd.json.dates||[]).map(d=>d.run_date).filter(Boolean);
  cont.predictionDates = datesList;
  cont.predictionCount = pd.json&&pd.json.count;
  cont.todayInDates = datesList.includes('2026-09-29');
  cont.datesDesc = datesList.length>1 && datesList.every((d,i)=> i===0 || d < datesList[i-1]);
  console.log(`预测已生成(${datesList.length}): ${datesList.join(', ')} | 含今日09-29:${cont.todayInDates} | 降序:${cont.datesDesc}`);

  const pt = await get('/api/prediction/today');
  cont.todayRunDate = pt.json&&pt.json.run_date;
  cont.todayHasData = !!(pt.json&&pt.json.has_data);
  cont.todayItems = pt.json ? recCount(pt.json.items||pt.json.signals||pt.json) : 0;
  console.log(`今日预测 run_date=${cont.todayRunDate} has_data=${cont.todayHasData} 记录:${cont.todayItems}`);

  const h29 = await get('/api/prediction/history?date=2026-09-29');
  const h28 = await get('/api/prediction/history?date=2026-09-28');
  const r29 = h29.json&&h29.json.run_date, r28 = h28.json&&h28.json.run_date;
  const c29 = h29.json?recCount(h29.json.items||h29.json):0, c28 = h28.json?recCount(h28.json.items||h28.json):0;
  cont.isolation = { req29:r29, req28:r28, count29:c29, count28:c28, isolated: r29==='2026-09-29'&&r28==='2026-09-28'&&c29>0&&c28>0 };
  console.log(`预测隔离：09-29→run_date=${r29}(${c29})；09-28→run_date=${r28}(${c28}) → ${cont.isolation.isolated?'各自独立✓':'缺失/串味✗'}`);

  const ps = await get('/api/prediction/status');
  cont.catchup = ps.json&&(ps.json.catchup||null);
  console.log(`预测引擎 status: ${JSON.stringify(cont.catchup).slice(0,140)}`);

  const lhb = await get('/api/lhb');
  cont.lhbLatest = (lhb.json&&(lhb.json.trade_date||lhb.json.date))||null;
  const lhb29 = await get('/api/lhb?date=2026-09-29');
  cont.lhbTodayHasData = !!(lhb29.json && (recCount(lhb29.json)>0));
  console.log(`龙虎榜最新交易日: ${cont.lhbLatest} | 今日09-29是否有数据: ${cont.lhbTodayHasData}（断档对照预测 ${datesList[0]||'无'}）`);

  const bfj = await get('/static/buffett/index.json');
  const bfReps = (bfj.json&&(bfj.json.reports||(bfj.json.periods&&bfj.json.periods.reports)))||[];
  cont.buffett = { updated_at: bfj.json&&bfj.json.updated_at, periods: bfReps.map(r=>r.period) };
  console.log(`巴菲特期次: ${cont.buffett.periods.join(', ')} (updated ${cont.buffett.updated_at})`);

  // 汇总
  // 真故障 = 只有「既不是 OK、也不是合法空态、也不是已知东财外部限制」才算。
  // 此前把 SRC_EM_DOWN 也计进真故障 → 同一端点被报两次（真故障 + 东财不可达），属统计缺陷。
  const realFail = rows.filter(r => !['OK', 'EMPTY_OK', 'SRC_EM_DOWN'].includes(r.cls));
  const emDown = rows.filter(r => r.cls==='SRC_EM_DOWN');
  const result = {
    generated_at: new Date().toISOString(),
    focus: 'data-connectivity + data-continuity',
    endpoints_probed: rows.length,
    real_failures: realFail.map(r=>({ep:r.name, cls:r.cls, status:r.status, count:r.count})),
    eastmoney_down: emDown.map(r=>({ep:r.name})),
    continuity: cont,
    summary: {
      endpoints: rows.length,
      ok: rows.filter(r=>r.cls==='OK').length,
      empty_ok: rows.filter(r=>r.cls==='EMPTY_OK').length,
      real_failures: realFail.length,
      eastmoney_down: emDown.length,
      prediction_today: cont.todayRunDate,
      prediction_isolated: cont.isolation.isolated,
      lhb_latest: cont.lhbLatest,
      lhb_today_data: cont.lhbTodayHasData,
      buffett_periods: cont.buffett.periods
    }
  };
  require('fs').writeFileSync('audit_data_result.json', JSON.stringify(result, null, 2));
  console.log(`\n=== 结论 ===`);
  console.log(`端点: ${rows.length} | OK:${result.summary.ok} | 空态OK:${result.summary.empty_ok} | 真故障:${realFail.length} | 东财不可达:${emDown.length}`);
  if (realFail.length) console.log('真故障:\n' + realFail.map(r=>`  ✗ ${r.name} [${r.cls}]`).join('\n'));
  if (emDown.length) console.log('东财源不可达(已知外部限制):\n' + emDown.map(r=>`  ⚠ ${r.name}`).join('\n'));
  console.log(`连贯性: 预测今日=${cont.todayRunDate}(has_data=${cont.todayHasData}) 隔离=${cont.isolation.isolated?'OK':'FAIL'} | 龙虎榜最新=${cont.lhbLatest} 今日数据=${cont.lhbTodayHasData} | 巴菲特=${cont.buffett.periods.join(',')}`);
  console.log('结果已写 audit_data_result.json');
})().catch(e => { console.error('FATAL', e); process.exit(1); });
