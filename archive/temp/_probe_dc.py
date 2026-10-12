"""在 datacenter-web 上探索资金流排行相关报表。"""
from __future__ import annotations
import urllib.request, urllib.parse, json, time

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
REF = "https://data.eastmoney.com/"


def get(url, referer=REF):
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA, "Referer": referer})
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, r.read(200_000).decode("utf-8", "ignore")
    except urllib.error.HTTPError as e:
        return e.code, e.read(2000).decode("utf-8", "ignore")
    except Exception as e:
        return None, repr(e)


# 候选报表名（按"资金流/主力/排行"语义猜）
CANDIDATES = [
    "RPT_MUTUAL_MAIN_HZ",          # 主力横向
    "RPT_MUTUAL_MAIN_RANK",        # 主力排行
    "RPT_BLK_FUND_FLOW_RANK",      # 板块资金流排行
    "RPT_HK_HOLD_RANK",            # 港股通持股排行（不是资金流）
    "RPT_F10_FINANCE_MAINFORCE",   # F10 财务主力
    "RPT_BIG_TRADE_RANK",          # 大宗交易排行
    "RPT_FLOW_BOARD_DATA",         # 资金流板块数据
    "RPT_FLOW_BK_DATA",            # 资金流板块
    "RPT_FLOW_STOCK_DATA",         # 资金流个股
    "RPT_RZRQ_RANK",               # 融资融券排行
    "RPT_MUTUAL_BUY_RANK",         # 主力买入排行
    "RPT_MUTUAL_SELL_RANK",        # 主力卖出排行
    "RPT_NORTHSTA_RANK",           # 北向持股排行
    "RPT_MAIN_FLOW_DAY",           # 主力资金流日
    "RPT_BK_MONEY_FLOW",           # 板块资金流
    "RPT_HY_MONEY_FLOW",           # 行业资金流
    "RPT_SECTOR_MONEY_FLOW",       # 板块资金流
    "RPT_FUND_FLOW_RANK",          # 资金流排行
    "RPT_FUND_FLOW_HIST",          # 资金流历史
    "RPT_HK_CONNECT_RANK",         # 沪深港通排行
]

print("=" * 70)
print("探活 datacenter-web 资金流/主力相关报表")
for rn in CANDIDATES:
    qs = urllib.parse.urlencode({
        "reportName": rn, "columns": "ALL", "pageNumber": 1, "pageSize": 1,
        "source": "WEB", "client": "WEB", "_": int(time.time() * 1000),
    })
    st, txt = get(f"https://datacenter-web.eastmoney.com/api/data/v1/get?{qs}")
    if st == 200:
        try:
            d = json.loads(txt)
            succ = d.get("success")
            code = d.get("code")
            msg = d.get("message") or ""
            if succ and (d.get("result") or {}).get("data"):
                print(f"  [{rn}] ✓ 成功 code={code} data count={(d['result']['data'])}")
            else:
                print(f"  [{rn}] ✗ code={code} msg={msg[:80]}")
        except Exception as e:
            print(f"  [{rn}] parse err: {e}")

# 同时也探一下哪些报表名是被数据中心页面真实使用的（从 data.eastmoney.com 上 grep）
print("\n" + "=" * 70)
print("看看数据中心资金流排行页用的是什么报表")
# 试一些更贴近语义的英文/拼音
EXTRA = [
    "RPT_STOCK_FUND_FLOW",
    "RPT_STOCK_FUND_FLOW_RANK",
    "RPT_STOCK_CAPITAL_FLOW_RANK",
    "RPT_STOCK_MAIN_FORCE_RANK",
    "RPT_MAIN_FORCE_FUND_FLOW",
    "RPT_MUTUAL_FUND_FLOW",
    "RPT_MUTUAL_FUND_RANK",
    "RPT_MUTUAL_HOLD_RANK",
    "RPT_FUND_FLOW",
    "RPT_RANK_LIST",
    "RPT_BLOCK_FUND_FLOW",
    "RPT_HY_FUND_FLOW",
    "RPT_GN_FUND_FLOW",
    "RPT_DQ_FUND_FLOW",
    "RPT_MUTUAL_FUND_HOLDER",
    "RPT_INTEREST_RANK",
    "RPT_TRADE_RANK",
]
for rn in EXTRA:
    qs = urllib.parse.urlencode({
        "reportName": rn, "columns": "ALL", "pageNumber": 1, "pageSize": 1,
        "source": "WEB", "client": "WEB", "_": int(time.time() * 1000),
    })
    st, txt = get(f"https://datacenter-web.eastmoney.com/api/data/v1/get?{qs}")
    if st == 200:
        try:
            d = json.loads(txt)
            succ = d.get("success")
            code = d.get("code")
            msg = d.get("message") or ""
            data = (d.get("result") or {}).get("data")
            status = "✓" if succ and data else "✗"
            print(f"  [{rn}] {status} code={code} msg={msg[:60]}")
        except Exception as e:
            print(f"  [{rn}] parse err: {e}")