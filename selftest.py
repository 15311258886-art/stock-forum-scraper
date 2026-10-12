"""离线自测：解析器 + 合规过滤 + 凭据加解密 + 存档（不涉及真实网络 / 真实账号）。"""
import os
import shutil
import sys
import tempfile
from datetime import datetime as _dt
from pathlib import Path

sys.path.insert(0, os.path.dirname(__file__))

from scraper.config import get_site, load_config
from scraper.parsers.taoguba import TaogubaParser
from scraper.compliance import filter_public_only
from scraper.credentials import save_credential, get_credential, delete_credential
from scraper.archive import ExcelArchive

# 直接复用真实配置文件里的交易正则，保证自测与运行一致
data = load_config()
cfg = get_site(data, "taoguba")
cfg["_key"] = "taoguba"
cfg["selectors"] = {}  # 用占位选择器；测试 HTML 用默认选择器匹配

# 2026-10-05 重写（对应测试报告 P0-1 / P0-2）：
# 旧用例是 "买入 600519 1000股" —— 无括号的裸代码 + 代码与名称错位，
# 解析器抽不到两笔、还把茅台的 500 股扣给了 600519，assert 常年红着，
# 于是这套自测早就不是回归基线了。改用真实帖子的写法（名称+括号代码），
# 并明确三条断言口径：抽得到就要准、抽不到必须是 None（不能是硬编码 0/假值）。
HTML = """
<div class="post-item">
  <span class="author">选手A</span>
  <span class="time">2026-09-22 10:30</span>
  <div class="content">今日操作：买入 贵州茅台(600519) 1000股，价格 12.30。另外买入 平安银行(000001) 500股。尾盘又加仓了宁德时代(300750)。</div>
</div>
"""

p = TaogubaParser(cfg)
trades = list(p.parse_trades(HTML, "https://www.taoguba.com.cn/x"))
print("TRADES_FOUND:", len(trades))
for t in trades:
    print("  ", t.action, "| code=", t.symbol, "| name=", t.symbol_name,
          "| price=", t.price, "| qty=", t.quantity, "| author=", t.author)

# ① 有量有价的：数量与价格都要抽准（价格写在量后面也算同一笔）
t1 = next(t for t in trades if t.symbol == "600519")
assert t1.action == "BUY" and t1.quantity == 1000 and t1.price == 12.30, t1
# ② 只有数量、没报价的：数量抽到，价格必须是 None（前端渲染成"帖中未披露"）
t2 = next(t for t in trades if t.symbol == "000001")
assert t2.action == "BUY" and t2.quantity == 500 and t2.price is None, t2
# ③ 只报动作、没报量价的两项都得是 None——量价是**可空**字段，不是必填
t3 = next(t for t in trades if t.symbol == "300750")
assert t3.action == "BUY" and t3.price is None and t3.quantity is None, t3
assert len(trades) == 3, trades

# ④ 预测价绝对不能被当成成交价（实测样本里"跌到1100附近"这类比真成交价还多）
#    anchor 传标的名结束位置："中国国贸"=0~4
assert TaogubaParser(cfg)._extract_pq("中国国贸涨到200元是有投资逻辑", 4) == (None, None)
# ⑤ 小数不能当股数（"每股分0.25，手搓0.33股息"曾被 `0.33股` 抽成 0 股）
assert TaogubaParser(cfg)._extract_pq("公司中期每股分0.25，高抛低吸手搓0.33股息", 22) == (None, None)
# ⑥ 手 / 万股要按 A 股口径折算成股数（1 手 = 100 股），且认自己名下的量价
assert TaogubaParser(cfg)._extract_pq("津药药业6手", 4) == (None, 600)
assert TaogubaParser(cfg)._extract_pq("段永平买入3万股贵州茅台", 11) == (None, 30000)
print("PRICE_QTY_CASES_OK")

# 合规过滤：被禁字段必须被剔除
rec = {"author": "选手A", "action": "BUY", "symbol": "600519",
       "online_status": "online", "follower_count": 123, "age": 35}
clean = filter_public_only([rec])[0]
assert "online_status" not in clean and "follower_count" not in clean and "age" not in clean
print("COMPLIANCE_FILTER_OK keys=", sorted(clean.keys()))

# 凭据加解密往返
save_credential("selftest_site", "user1", "pass1")
c = get_credential("selftest_site")
assert c == {"username": "user1", "password": "pass1"}, c
delete_credential("selftest_site")
print("CREDENTIAL_ROUNDTRIP_OK")

# 存档模块：创建 / 去重 / 统计（离线）
tmp = tempfile.mkdtemp(prefix="arc_selftest_")
arc_path = Path(tmp) / "test_archive.xlsx"
arc = ExcelArchive(arc_path)
arc.ensure_file()
print("ARCHIVE_CREATED:", arc.path.exists())

s1 = {
    "source_site": "taoguba", "author": "选手A", "post_id": "p100",
    "post_url": "https://x/p100", "post_time": _dt(2026, 9, 22, 10, 30),
    "action": "BUY", "symbol": "600519", "symbol_name": "贵州茅台",
    "price": 1680.0, "quantity": 100, "raw_text": "买入 600519 100股",
}
s2 = {
    "source_site": "taoguba", "author": "选手B", "post_id": "p101",
    "post_url": "https://x/p101", "post_time": _dt(2026, 9, 22, 11, 0),
    "action": "SELL", "symbol": "000001", "symbol_name": "平安银行",
    "price": 12.3, "quantity": 500, "raw_text": "卖出 000001 500股",
}
n1_t = arc.append_trades([s1, s2])
n1_n = arc.append_news([{"source_site": "taoguba", "title": "盘前纪要", "url": "https://x/n1", "publish_time": None, "summary": "行情展望"}])
print(f"FIRST_APPEND trades={n1_t} news={n1_n}")
assert n1_t == 2 and n1_n == 1

# 重复追加应被去重
n2_t = arc.append_trades([s1, s2, s1])
print(f"SECOND_APPEND trades={n2_t} (should be 0)")
assert n2_t == 0

# 改日期再追加一条新交易
s3 = dict(s2); s3["post_id"] = "p102"; s3["symbol"] = "300750"
n3_t = arc.append_trades([s3])
print(f"THIRD_APPEND trades={n3_t} (should be 1)")
assert n3_t == 1

arc.log_run("taoguba", "all", 3, 1, "ok", "selftest")
st = arc.stats()
print("STATS:", st)
assert st["trades"] == 3 and st["news"] == 1 and st["runs"] == 1

# 重新打开文件，确认持久化
arc2 = ExcelArchive(arc_path)
assert arc2.stats()["trades"] == 3
print("ARCHIVE_PERSIST_OK")

shutil.rmtree(tmp, ignore_errors=True)
print("ARCHIVE_TESTS_PASSED")

print("\nALL_OFFLINE_TESTS_PASSED")
