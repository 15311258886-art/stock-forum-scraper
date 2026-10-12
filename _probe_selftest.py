# -*- coding: utf-8 -*-
import os, sys
ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)
from scraper.config import get_site, load_config
from scraper.parsers.taoguba import TaogubaParser

data = load_config(); cfg = get_site(data, "taoguba")
cfg["_key"] = "taoguba"; cfg["selectors"] = {}

HTML = """
<div class="post-item">
  <span class="author">选手A</span>
  <span class="time">2026-09-22 10:30</span>
  <div class="content">今日操作：买入 贵州茅台(600519) 1000股，价格 12.30。另外买入 平安银行(000001) 500股。尾盘又加仓了宁德时代(300750)。</div>
</div>
"""
p = TaogubaParser(cfg)
for t in p.parse_trades(HTML, "https://www.taoguba.com.cn/x"):
    print(t.action, "| code=", t.symbol, "| name=", t.symbol_name,
          "| price=", t.price, "| qty=", t.quantity)
