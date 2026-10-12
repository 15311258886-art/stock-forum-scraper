# -*- coding: utf-8 -*-
"""QA(Edward) 独立验证 · 第2项：各解析器是否存在 1:N 路径。
自构造样本（不信任工程师结论）。sina/eastmoney/ths 各造一条「标题含多只票 + 单个动作词」的行，
看是否 >1 条产出。
"""
from __future__ import annotations
import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from scraper.parsers.sina import SinaParser          # noqa: E402
from scraper.parsers.eastmoney import EastMoneyParser  # noqa: E402
from scraper.parsers.ths import ThsParser             # noqa: E402


def dump(label, trades):
    rows = list(trades)
    print(f"  [{label}] 产出 {len(rows)} 条")
    for t in rows:
        sym = getattr(t, "symbol", None)
        act = getattr(t, "action", None)
        nmv = getattr(t, "symbol_name", None)
        print(f"      action={act} symbol={sym} name={nmv}")
    return rows


print("#" * 90)
print("# sina：一行标题含「买入 + 多只票」")
print("#" * 90)
sina_html = """
<table>
 <tr>
  <td><a href="?s=thread&tid=111&bid=1">买入600519 贵州茅台 卖出000001 平安银行 600000 浦发银行</a></td>
  <td class="author"><div class="author"><a href="/u/1" title="真实选手甲">真实选手甲</a></div></td>
  <td>今天08:22</td>
 </tr>
</table>
"""
sp = SinaParser({})
sina_rows = dump("sina page_url=name=sh600000", sp.parse_trades(sina_html, "http://guba.sina.com.cn/?s=bar&name=sh600000"))
print("  说明：URL 决定唯一 symbol，结构上每行最多 1 条 →", "无1:N" if len(sina_rows) <= 1 else "存在1:N!!")

print()
print("#" * 90)
print("# eastmoney：一行标题含「买入 + 多个代码」")
print("#" * 90)
em_html = """
<ul>
 <li class="listitem">
  <div class="title"><a href="/news,600519,99999.html">今天买入600519，也买了000001和600000</a></div>
  <div class="author">真实选手乙</div>
  <div class="update">2026-09-24 15:20:00</div>
 </li>
</ul>
"""
em = EastMoneyParser({})
em_rows = dump("eastmoney page_url=list,600519.html", em.parse_trades(em_html, "https://guba.eastmoney.com/list,600519.html"))
print("  说明：CODE_RE.search 只取第一个代码 →", "无1:N" if len(em_rows) <= 1 else "存在1:N!!")

print()
print("#" * 90)
print("# ths：一条 feed 含「买入 + 多个代码」")
print("#" * 90)
ths_html = """
<div class="feed-item">
  <div class="feed-item-title"><a href="http://t.10jqka.com.cn/circle/12345/">买入600519 000001 600000 全部上车</a></div>
  <div class="feed-item-content"><div class="word-content">今天买入600519，顺手买了000001和600000</div></div>
  <div class="feed-item-broker"><span class="broker-name">真实选手丙</span></div>
  <div class="feed-item-timeline-time">09-24 15:20</div>
</div>
"""
th = ThsParser({})
th_rows = dump("ths", th.parse_trades(ths_html, "http://t.10jqka.com.cn/"))
print("  说明：CODE_RE.search 只取第一个代码 →", "无1:N" if len(th_rows) <= 1 else "存在1:N!!")
