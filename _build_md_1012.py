import sys, json
sys.path.insert(0, r'F:\AI Agent部署知识库\2026-09-22-11-49-42\stock-forum-scraper')

TODAY = '2026-10-12'
BASE = r'F:\AI Agent部署知识库\2026-09-22-11-49-42\stock-forum-scraper'

with open(BASE + r'\_ex_1012.json', encoding='utf-8') as f:
    hot = json.load(f)
with open(BASE + r'\_src_1012.json', encoding='utf-8') as f:
    srcs = json.load(f)

by_id = {h['id']: h for h in hot}

# source status aggregation
from collections import OrderedDict
agg = OrderedDict()
for s in srcs:
    agg.setdefault(s['status'], []).append(s['name'])
status_lines = []
for st, names in agg.items():
    status_lines.append(f"- **{st}**：{len(names)} 个（{', '.join(names)}）")

# four key categories: id list
CATS = [
    ('地缘冲突与战争', [1644, 1650, 1655]),
    ('洪涝等自然灾害', [1647]),
    ('重大科研论文发布', [1641, 1640]),
    ('粮食歉收与减产', [1652]),
]

def today_new(ids):
    return sum(1 for i in ids if by_id.get(i) and (by_id[i]['first_seen'][:10] == TODAY))

L = []
L.append(f"# 国外热点传导日报 · {TODAY}")
L.append("")
L.append(f"> 数据模式：**联网实时（live=True）**，非示例模式。生成时间 2026-10-12 09:04（北京时间）。")
L.append("")
L.append("## 一、抓取与聚类概览")
L.append("")
L.append("- 实抓条目：748 条 / 写入 748 条 / 种子补 41 条")
L.append("- 聚类热点：16 条（全为存量刷新，**今日新增 0 条**）")
L.append("- 数据日覆盖：2026-01-02 ~ 2026-10-12（共 62 个采样日）")
L.append("- 状态：联网成功，无 import/抓取致命错误")
L.append("")
L.append("## 二、数据来源状态（诚实标注）")
L.append("")
L.append(f"共 {len(srcs)} 个源，按状态聚合：")
L.append("")
L.append("\n".join(status_lines))
L.append("")
L.append("> 说明：CNBC / MarketWatch / Seeking Alpha / HKEX 等任务书预期「被墙待激活」的源，本机实测**均已接入**并正常返回条目，按实测标注；Yahoo Finance、Reddit、Reuters、SCMP 等 7 个源因本机无境外出口保持「待激活（需 VPN）」，YouTube 财经「待激活（需 API Key）」，AAStocks / 雪球 / 东财股吧等 5 个源「未启用」（反爬或需渲染）。")
L.append("")
L.append("## 三、四类重点事件逐类点名")
L.append("")
for label, ids in CATS:
    n = today_new(ids)
    members = []
    for i in ids:
        h = by_id.get(i)
        if h:
            members.append(f"#{i} {h['title']}（首见 {h['first_seen'][:10]}）")
    if n == 0:
        new_txt = "**今日无新增**"
    else:
        new_txt = f"**今日新增 {n} 条**"
    L.append(f"### {label} — {new_txt}")
    L.append("")
    L.append("相关存量条目：" + "；".join(members))
    L.append("")
L.append("> 四类重点事件本轮均**无当日新增**（first_seen 最早 2026-09-25、最晚 2026-10-11，无一等于今日）。所有条目为历史存量刷新，结论为既有推演的延续，非新发新闻。禁止将存量条目标注为今日新增。")
L.append("")
L.append("## 四、全部热点逐条传导结构")
L.append("")
L.append("> 板块涨跌沿用 A 股惯例：**（涨·红）** / **（跌·绿）**。证据来源按 `example.local` 判定实时/示例：示例证据为模板占位，不计入实时抓取。")
L.append("")

for h in sorted(hot, key=lambda x: -x['heat']):
    L.append(f"### #{h['id']} {h['title']}")
    L.append("")
    L.append(f"- **主题**：{h['topic']} ｜ **确定性 confidence**：{h['confidence']} ｜ **热度 heat**：{h['heat']}")
    L.append(f"- **首见 first_seen**：{h['first_seen']} ｜ **更新 updated_at**：{h['updated_at']}")
    L.append(f"- **驱动源 driver_sources**：{h['driver_sources'] or '（空）'} ｜ **驱动区域**：{h['driver_regions']}")
    L.append("")
    L.append(f"**事件概述**：{h['overview']}")
    L.append("")
    L.append(f"**关键传导变量**：{h['variables']}")
    L.append("")
    up = h['sectors_up'] or '（无）'
    down = h['sectors_down'] or '（无）'
    L.append(f"**预判可能上涨板块（涨·红）**：{up}")
    L.append("")
    L.append(f"**预判可能下跌板块（跌·绿）**：{down}")
    L.append("")
    L.append(f"**传导路径**：{h['transmission']}")
    L.append("")
    L.append(f"**放大或衰减判断**：{h['amplification']}")
    L.append("")
    L.append(f"**交叉验证 cross_check**：{h['cross_check']}")
    L.append("")
    # evidence
    live = h['live_urls']
    seed_n = h['seed_n']
    live_n = h['live_n']
    if live_n == 0:
        L.append(f"**⚠️ 证据来源（实时={live_n} / 示例={seed_n}）**：本事件**实时证据为 0、证据全部为示例占位（url 含 example.local），结论为模板推演，非实时抓取结果，仅作传导框架参考，不可作为交易依据**。")
    else:
        L.append(f"**证据来源（实时={live_n} / 示例={seed_n}）**：")
        L.append("")
        for e in live:
            L.append(f"  - [{e['title']}]({e['url']}) — {e['source']}")
        if seed_n:
            L.append(f"  - （另有 {seed_n} 条示例占位证据，未列出）")
    L.append("")
    L.append(f"**决策参考 decision**：{h['decision']}")
    L.append("")
    L.append("---")
    L.append("")

md = "\n".join(L)
outpath = BASE + r'\reports\国外热点传导日报-' + TODAY + '.md'
import os
os.makedirs(BASE + r'\reports', exist_ok=True)
with open(outpath, 'w', encoding='utf-8') as f:
    f.write(md)
print("WROTE", outpath, "chars=", len(md), file=sys.stderr)
