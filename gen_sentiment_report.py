"""生成「情绪地图详解 + 多空观点明细（谁看多/看空 + 理由）」合并报告。
数据实拉自 viewer/app.db，保证与查看器界面一致。"""
from __future__ import annotations
import sqlite3, html, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DB = ROOT / "viewer" / "app.db"
OUT = ROOT / "情绪地图与多空观点明细报告.html"

c = sqlite3.connect(DB)
c.row_factory = sqlite3.Row

# 名称解析（复用 viewer.stock_names 的离线逻辑，保证一致）
import sys
sys.path.insert(0, str(ROOT))
from viewer.stock_names import get_resolver  # 复用，离线优先
resolver = get_resolver(db=None, online=False)

def named(code):
    info = resolver.resolve(code)
    return info["name"] or "名称待补全", info["company"] or "", info["market"] or ""

# 拉取 (股票代码, 作者, 立场, 原话, 时间, 站点)
rows = c.execute("""
  SELECT sm.symbol AS symbol, p.author AS author, p.sentiment AS sentiment,
         p.text AS text, p.post_time AS post_time, p.site AS site
  FROM stock_mentions sm JOIN posts p ON p.id = sm.post_id_ref
  ORDER BY sm.symbol, p.sentiment, p.post_time DESC
""").fetchall()

# 聚合：每只股票 -> {bull:[...], bear:[...], neutral:[...]}
agg = {}
for r in rows:
    sym = r["symbol"]
    cat = {"看多": "bull", "看空": "bear"}.get(r["sentiment"], "neutral")
    agg.setdefault(sym, {"bull": [], "bear": [], "neutral": []})
    agg[sym][cat].append({
        "author": r["author"] or "匿名",
        "text": r["text"] or "",
        "post_time": r["post_time"] or "",
        "site": r["site"] or "",
    })

# 整体分布
dist = {}
for r in c.execute("SELECT sentiment, COUNT(*) n FROM posts GROUP BY sentiment"):
    dist[r["sentiment"]] = r["n"]

now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")

def esc(s): return html.escape(str(s or ""))

# 渲染每只股票的多空明细块
blocks = []
for sym in sorted(agg.keys(), key=lambda s: -(len(agg[s]["bull"]) + len(agg[s]["bear"]))):
    name, company, market = named(sym)
    b, e, n = agg[sym]["bull"], agg[sym]["bear"], agg[sym]["neutral"]
    def items(lst, cls):
        if not lst:
            return f'<div class="vg-empty">（无）</div>'
        out = []
        for v in lst:
            out.append(f'''<div class="view-item">
              <div class="vi-head"><b class="vi-author">{esc(v['author'])}</b>
                <span class="vi-time">{esc(v['post_time'])}</span>
                {f'<span class="vi-site">{esc(v["site"])}</span>' if v['site'] else ''}</div>
              <div class="vi-reason">{esc(v['text'])}</div></div>''')
        return "".join(out)
    blocks.append(f'''
    <div class="stock-card">
      <div class="sc-head">
        <span class="stk-code">{esc(sym)}</span>
        <span class="sc-name"><b>{esc(name)}</b></span>
        <span class="sc-corp">{esc(company)}</span>
        <span class="stk-board">{esc(market)}</span>
        <span class="sc-count">多 {len(b)} · 空 {len(e)} · 中性 {len(n)}</span>
      </div>
      <div class="views">
        <div class="view-group v-bull"><div class="vg-head">看多（{len(b)}）—— 谁看好、为什么</div>{items(b,'bull')}</div>
        <div class="view-group v-bear"><div class="vg-head">看空（{len(e)}）—— 谁看淡、为什么</div>{items(e,'bear')}</div>
        <div class="view-group v-neu"><div class="vg-head">中性 / 未定（{len(n)}）</div>{items(n,'neu')}</div>
      </div>
    </div>''')

html_doc = f'''<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>情绪地图与多空观点明细报告</title>
<style>
* {{ box-sizing: border-box; }}
body {{ font-family: -apple-system,"PingFang SC","Microsoft YaHei",sans-serif; margin:0; padding:0;
  background:#f5f6f8; color:#1f2430; line-height:1.7; }}
.wrap {{ max-width: 980px; margin: 0 auto; padding: 32px 24px 80px; }}
h1 {{ font-size: 26px; margin: 0 0 6px; }}
h2 {{ font-size: 20px; margin: 38px 0 12px; padding-left: 10px; border-left: 4px solid #C81E1E; }}
h3 {{ font-size: 16px; margin: 22px 0 8px; color:#444; }}
.lede {{ color:#555; font-size:14px; margin: 4px 0 0; }}
.note {{ background:#fff8e6; border:1px solid #f0d58a; border-radius:10px; padding:12px 16px; font-size:13.5px; color:#6b5418; margin:16px 0; }}
.card {{ background:#fff; border:1px solid #e6e8ec; border-radius:12px; padding:18px 20px; margin:14px 0; }}
.kv {{ display:flex; gap:10px; padding:5px 0; font-size:14px; }}
.kv .k {{ flex:0 0 120px; color:#888; }}
.kv .v {{ flex:1; }}
code {{ background:#eef0f3; border-radius:5px; padding:1px 6px; font-size:12.5px; }}
.legend-row {{ display:flex; gap:10px; padding:4px 0; font-size:14px; }}
.legend-row .lab {{ flex:0 0 130px; font-weight:700; }}
.bars {{ display:flex; gap:12px; margin:10px 0 4px; }}
.bar {{ flex:1; text-align:center; border-radius:10px; padding:12px; }}
.bar.bull {{ background:#FDE7E6; color:#C81E1E; }}
.bar.bear {{ background:#DEF3E3; color:#15803D; }}
.bar.neu  {{ background:#eef0f3; color:#555; }}
.bar .num {{ font-size:24px; font-weight:800; }}
.stock-card {{ background:#fff; border:1px solid #e6e8ec; border-radius:12px; padding:14px 16px; margin:14px 0; }}
.sc-head {{ display:flex; align-items:center; gap:10px; flex-wrap:wrap; margin-bottom:10px; }}
.stk-code {{ font-family:ui-monospace,Consolas,monospace; font-size:13px; letter-spacing:.6px; background:#eef0f3; border:1px solid #dde0e4; border-radius:6px; padding:2px 8px; }}
.sc-name {{ font-size:16px; }}
.sc-corp {{ font-size:12.5px; color:#666; }}
.stk-board {{ font-size:11px; color:#555; border:1px solid #dde0e4; border-radius:999px; padding:1px 8px; }}
.sc-count {{ margin-left:auto; font-size:13px; color:#C81E1E; font-weight:700; }}
.views {{ display:flex; flex-direction:column; gap:10px; }}
.view-group {{ border:1px solid #e6e8ec; border-radius:10px; padding:8px 12px; }}
.view-group.v-bull {{ border-left:4px solid #C81E1E; background:#fdf3f2; }}
.view-group.v-bear {{ border-left:4px solid #15803D; background:#f1f9f3; }}
.view-group.v-neu  {{ border-left:4px solid #999; background:#fafafa; }}
.vg-head {{ font-weight:700; font-size:13.5px; margin-bottom:6px; }}
.vg-empty {{ font-size:12.5px; color:#999; }}
.view-item {{ padding:6px 0; border-top:1px dashed #e6e8ec; }}
.view-item:first-of-type {{ border-top:none; }}
.vi-head {{ display:flex; align-items:center; gap:8px; flex-wrap:wrap; }}
.vi-author {{ font-size:13.5px; }}
.vi-time {{ font-size:11px; color:#999; }}
.vi-site {{ font-size:11px; color:#2563eb; background:#eaf1ff; border-radius:6px; padding:1px 7px; }}
.vi-reason {{ font-size:13px; line-height:1.6; margin-top:3px; }}
.foot {{ color:#999; font-size:12.5px; margin-top:30px; text-align:center; }}
</style></head>
<body><div class="wrap">

<h1>情绪地图详解 & 多空观点明细（谁看多 / 谁看空 + 理由）</h1>
<p class="lede">生成时间：{now} ｜ 数据来源：查看器本地库（示例数据，口径见文末说明）｜ 与 <code>http://127.0.0.1:8765</code> 界面实时一致</p>

<div class="note"><b>先回答您最关心的问题：</b>情绪地图上那些数字（如 <code>600519</code>）是<b>股票代码，不是序号、也不是排名</b>。
它对应 <b>贵州茅台</b>（公司全称：贵州茅台酒股份有限公司）。每张卡片现在都标注了简称 + 公司全称，并新增「多空观点明细」，
逐条写明<strong>具体是谁、针对这只股票看多还是看空、以及其原话理由</strong>。</div>

<h2>一、情绪地图的"数字"到底是什么</h2>
<div class="card">
  <div class="kv"><div class="k">600519</div><div class="v"><b>股票代码</b>（6 位）。沪市以 60/68 开头、深市以 00/30 开头、北交所以 43/83/87 开头。它是某只具体股票的"身份证"，与排名无关。</div></div>
  <div class="kv"><div class="k">贵州茅台</div><div class="v">该代码对应的<b>股票简称</b>，卡片副标题还会显示公司全称。</div></div>
  <div class="kv"><div class="k">多 N / 空 N</div><div class="v"><b>多 N</b>＝有 N 条带"看多"情绪的帖子提到了这只股票；<b>空 N</b>＝有 N 条带"看空"情绪的帖子提到它；中性 N＝情绪词库未判出明确多空的提及。</div></div>
  <div class="kv"><div class="k">配色惯例</div><div class="v">已按 A 股习惯统一为 <span style="color:#C81E1E;font-weight:700">看多=红</span>、<span style="color:#15803D;font-weight:700">看空=绿</span>（涨红跌绿）。</div></div>
</div>

<h2>二、整体多空温度计（全样本）</h2>
<div class="bars">
  <div class="bar bull"><div class="num">{dist.get('看多',0)}</div><div>看多帖</div></div>
  <div class="bar bear"><div class="num">{dist.get('看空',0)}</div><div>看空帖</div></div>
  <div class="bar neu"><div class="num">{dist.get('中性',0)}</div><div>中性帖</div></div>
</div>
<p class="lede">说明：以上是按"帖子"维度统计的整体情绪；下文按"股票"维度拆解，能看到每只票内部的多空分歧——这正是"谁看多、谁看空"的关键。</p>

<h2>三、逐只股票的多空观点明细（WHO / WHAT / WHY）</h2>
<p class="lede">以下每只股票分三栏：<b>看多</b>列出看好它的发帖人及其理由、<b>看空</b>列出看淡的发帖人及其理由、<b>中性</b>为未明确表态者。
"标的"即卡片对应的那只股票（WHAT）；"谁"是发帖署名（论坛公开昵称）；"为什么"是该帖原文（判断依据与逻辑）。</p>
{''.join(blocks)}

<h2>四、数据口径与诚实说明</h2>
<div class="card">
  <h3>口径</h3>
  <ul>
    <li><b>多空判定层级：</b>情绪标签打在<b>帖子</b>上（作者在该帖的整体立场），再按"帖子提及了哪只股票"归到对应票。因此一帖若同时提多只票（如"看好宁德、警惕五粮液"），其立场会同时计入相关各票。</li>
    <li><b>"谁"是谁：</b>指发帖人的<b>公开署名昵称</b>（如"短线作手老王"），属公开发帖披露内容，非隐私抓取。</li>
    <li><b>"为什么"从哪来：</b>直接取自该帖原文，未做改写；若原文只是情绪词（"割肉离场"）而无展开论证，明细中也如实呈现短句。</li>
    <li><b>当前为示例数据：</b>以上帖子为演示库内置样本，用于展示"谁看多/看空 + 理由"的结构。接入真实淘股吧抓取后，此处会自动替换为真实发帖人与原话。</li>
  </ul>
  <h3>如何查看实时数据</h3>
  <p>刷新 <code>http://127.0.0.1:8765</code> → 「情绪地图」面板点击任意股票卡 → 抽屉内「③ 多空观点明细」即展示与本文一致的逐条 WHO/WHAT/WHY。冷门代码若显示"名称待补全"，点面板顶部「补全股票名称」即可联网补齐。</p>
</div>

<div class="foot">本报告由查看器数据自动生成 · 与界面同源 · 多空立场以发帖原文为准</div>
</div></body></html>'''

OUT.write_text(html_doc, encoding="utf-8")
print("written:", OUT, "size", len(html_doc), "stocks", len(blocks))
