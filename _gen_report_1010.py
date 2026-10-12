import sys, json, os, html
sys.path.insert(0, r'F:\AI Agent部署知识库\2026-09-22-11-49-42\stock-forum-scraper')
from viewer.db import Database

TODAY = '2026-10-10'
REPORT_DIR = r'F:\AI Agent部署知识库\2026-09-22-11-49-42\stock-forum-scraper\reports'
os.makedirs(REPORT_DIR, exist_ok=True)

db = Database()
cols = ['id','key','title','topic','driver_sources','driver_regions','direction',
        'related_markets','related_symbols','domestic_sectors','transmission',
        'amplification','cross_check','amplification_factor','confidence','heat',
        'first_seen','updated_at','overview','variables','sectors_up','sectors_down','decision','evidence']
col_sql = ','.join(cols)
cur = db.conn.execute(f"SELECT {col_sql} FROM overseas_hotspots ORDER BY heat DESC")
rows = cur.fetchall()
hot = [dict(zip(cols, r)) for r in rows]

def ev_split(ev_raw):
    try:
        arr = json.loads(ev_raw) if ev_raw else []
    except Exception:
        arr = []
    live = [e for e in arr if 'example.local' not in (e.get('url') or e.get('link') or '')]
    seed = [e for e in arr if 'example.local' in (e.get('url') or e.get('link') or '')]
    return arr, live, seed

for h in hot:
    arr, live, seed = ev_split(h.get('evidence'))
    h['_arr'] = arr; h['_live'] = live; h['_seed'] = seed
    h['_is_new'] = (h.get('first_seen') or '')[:10] == TODAY

# sources
scur = db.conn.execute("SELECT source_key,name,status,enabled,count,error,requires_vpn,requires_key FROM overseas_sources_status")
scols=['source_key','name','status','enabled','count','error','requires_vpn','requires_key']
srcs=[dict(zip(scols,r)) for r in scur.fetchall()]
from collections import Counter
sc=Counter()
for s in srcs:
    sc[s['status']]+=1

db.close()

KNAME = {
    'geo_conflict': '地缘冲突与战争',
    'natural_disaster': '洪涝等自然灾害',
    'research_paper': '重大科研论文发布',
    'food_shortage': '粮食歉收与减产',
}
four = {k: next((h for h in hot if h['key']==k), None) for k in KNAME}

L = []
L.append(f'# 国外热点传导日报 · {TODAY}\n')
L.append('> 数据模式：**联网实时**（collect 以 `live=True` 跑通，实抓 753 条 / 种子补 41 / 聚类 16 条）。\n')
L.append('> 板块涨跌沿用 A 股惯例：**红色=上涨、绿色=下跌**。所有热点结论均为「国外舆情→A 股板块」的主题映射推演，非投资建议。\n')

# ---- 源状态 ----
L.append('## 一、数据来源状态（诚实标注）\n')
L.append('| 状态 | 数量 | 说明 |')
L.append('| --- | --- | --- |')
L.append(f"| 已接入 | {sc.get('已接入',0)} | 本机实测抓取成功，含 CNBC 三路、MarketWatch、Seeking Alpha、HKEX 两路、Nature、联合早报等 |")
L.append(f"| 已连通但无条目 | {sc.get('已连通但无条目',0)} | 适配器正常但本轮无命中（Engadget） |")
L.append(f"| 待激活（需 VPN） | {sc.get('待激活（需 VPN）',0)} | Google News / Reuters / Yahoo Finance / SCMP / Investing.com / Reddit / 信报 HKEJ，被墙，需 VPN |")
L.append(f"| 待激活（需 API Key） | {sc.get('待激活（需 API Key）',0)} | YouTube 财经大咖，需 YouTube Data API v3 Key |")
L.append(f"| 未启用 | {sc.get('未启用',0)} | AAStocks / Science / 东方财富股吧 / 雪球 / 境外中继 等反爬或 JS 渲染，暂未启用 |")
L.append('')
L.append('> 注：任务书预期「CNBC/MarketWatch/Seeking Alpha/HKEX 被墙保持待激活」**与实际不符**——本轮这四路全部抓取成功（CNBC 三路各 30 条、HKEX 两路各 50 条、MarketWatch 10、Seeking Alpha 7），按实测标注「已接入」。Hacker News 此前（10-09）曾转「抓取失败」，本轮已恢复「已接入」cnt=20。\n')

# ---- 四类重点事件 ----
L.append('## 二、四类重点事件追踪（逐类点名）\n')
for k, label in KNAME.items():
    h = four[k]
    L.append(f'### ▶ {label}\n')
    if h is None:
        L.append('- 当前库内无对应热点条目。\n')
        continue
    if h['_is_new']:
        L.append(f'- **今日新增**（first_seen={h["first_seen"]}）。\n')
    else:
        L.append(f'- **今日无新增**（存量条目，首次入库 {h["first_seen"][:10]}；本轮仅刷新 updated_at={h["updated_at"][:10]}）。\n')
    L.append(f'- 事件概述：{h["overview"]}\n')
    L.append(f'- 关键传导变量：{h["variables"]}\n')
    L.append(f'- 预判上涨板块（红）：{h["sectors_up"]}\n')
    L.append(f'- 预判下跌板块（绿）：{h["sectors_down"]}\n')
    L.append(f'- 传导路径：{h["transmission"]}\n')
    L.append(f'- 放大/衰减：{h["amplification"]}（系数 {h["amplification_factor"]}，确定性 {h["confidence"]}，热度 {h["heat"]}）\n')
    # evidence
    if len(h['_live']) == 0 and len(h['_seed']) > 0:
        L.append(f'- ⚠️ **证据全为示例（{len(h["_seed"])} 条，实时 0 条），本节结论为模板推演，不可当作实时新闻引用。**\n')
    else:
        L.append(f'- 证据：实时 {len(h["_live"])} 条 / 示例 {len(h["_seed"])} 条。实时来源：\n')
        for e in h['_live'][:8]:
            L.append(f'  - [{e.get("source","?")}] {html.unescape(e.get("title","?"))} — {e.get("url","")}\n')
        if h['_seed']:
            L.append(f'  - （另有 {len(h["_seed"])} 条示例占位，未计入实时证据）\n')
    L.append('')

# ---- 全部热点 ----
L.append('## 三、全部热点结构化清单（16 条，按热度降序）\n')
for i, h in enumerate(hot, 1):
    tag = '【今日新增】' if h['_is_new'] else ''
    L.append(f'### {i}. {h["title"]} {tag}\n')
    L.append(f'- **方向**：{h["direction"]}　|　**主题**：{h["topic"]}　|　**确定性**：{h["confidence"]}　|　**热度**：{h["heat"]}')
    L.append(f'- **事件概述**：{h["overview"]}')
    L.append(f'- **关键传导变量**：{h["variables"]}')
    L.append(f'- **预判上涨板块（红）**：{h["sectors_up"] or "—"}')
    L.append(f'- **预判下跌板块（绿）**：{h["sectors_down"] or "—"}')
    L.append(f'- **传导路径**：{h["transmission"]}')
    L.append(f'- **放大/衰减判断**：{h["amplification"]}（放大系数 {h["amplification_factor"]}；交叉比对：{h["cross_check"]}）')
    # evidence
    if len(h['_live']) == 0 and len(h['_seed']) > 0:
        L.append(f'- **证据来源**：⚠️ 全部 {len(h["_seed"])} 条均为示例占位（含 `example.local`），实时 0 条 → 结论为模板推演，勿当实时新闻。')
    elif len(h['_live']) == 0 and len(h['_seed']) == 0:
        L.append(f'- **证据来源**：暂无可引用证据（实时 0 / 示例 0）。')
    else:
        L.append(f'- **证据来源**（实时 {len(h["_live"])} / 示例 {len(h["_seed"])}）：')
        for e in h['_live'][:6]:
            L.append(f'  - [{e.get("source","?")}] {html.unescape(e.get("title","?"))} — {e.get("url","")}')
        if h['_seed']:
            L.append(f'  - （另有 {len(h["_seed"])} 条示例占位未列出）')
    L.append('')

# ---- 收口 ----
L.append('## 四、收口摘要\n')
L.append(f'- 数据模式：**联网实时**（非示例）。实抓 {753} 条 / 种子 {41} / 聚类 {len(hot)} 条。')
L.append(f'- 证据总数：实时 {sum(len(h["_live"]) for h in hot)} 条 / 示例 {sum(len(h["_seed"]) for h in hot)} 条。')
L.append(f'- 四类重点事件（地缘冲突、自然灾害、科研论文、粮食减产）**今日均无新增**，全部为存量刷新。')
L.append(f'- 唯一证据全示例、需警惕的条目：**粮食歉收与减产（food_shortage）** 4 条证据全为示例，结论为模板推演。')
L.append(f'- 源状态：已接入 {sc.get("已接入",0)} / 已连通无条目 {sc.get("已连通但无条目",0)} / 待激活(需VPN) {sc.get("待激活（需 VPN）",0)} / 待激活(需API Key) {sc.get("待激活（需 API Key）",0)} / 未启用 {sc.get("未启用",0)}。')
L.append('')
L.append('---')
L.append('_本报告由定时任务自动生成，热点结论为舆情传导推演，不构成投资建议。_')

out = '\n'.join(L)
path = os.path.join(REPORT_DIR, f'国外热点传导日报-{TODAY}.md')
with open(path, 'w', encoding='utf-8') as f:
    f.write(out)
print('WROTE', path, 'bytes=', len(out.encode('utf-8')))
print('hot=', len(hot), 'live=', sum(len(h['_live']) for h in hot), 'seed=', sum(len(h['_seed']) for h in hot))
