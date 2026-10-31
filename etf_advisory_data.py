# -*- coding: utf-8 -*-
"""ETF 投顾数据刷新脚本（B 栏数据链：今天打通、沉淀到本地缓存）。

数据来源：akshare.fund_etf_category_sina(symbol="ETF基金")
  —— 新浪 ETF 实时列表，含 最新价 / 涨跌幅 / 成交量 / 成交额（真实、可联网直连，无需东财）。
  东财 push2delay 接口在本沙箱被代理+直连双重拦截（ProxyError / ConnectionError），故 B 栏
  资金流/行情走 Sina；溢价率/规模/费率/跟踪误差需东财，环境不可用 → 诚实标「待接入」。

产出：etf_advisory_cache.json（与 predict_kcache.json 同级）
  {
    source, as_of,
    quotes: { code: {name, price, pct, amount, volume} },   # 56 只 ETF 实时行情
    track_index: { code: "沪深300" },                       # 由 ETF 名称解析的跟踪指数
    n_total
  }
etf.py 的 etf_advisory() 离线读此缓存（请求路径不依赖 akshare/联网），保证稳定可重现。
"""
from __future__ import annotations
import sys, os, json, re
from datetime import datetime

BASE = os.path.dirname(os.path.abspath(__file__))
CACHE = os.path.join(BASE, "etf_advisory_cache.json")

# 基金简称后缀（公司名/品牌），用于从 "沪深300ETF嘉实" 中剥离出 "沪深300"
COMPANIES = [
    "华泰柏瑞", "景顺长城", "万家", "国联安", "国泰君安", "海富通", "永赢", "中金", "兴业",
    "国寿安保", "浦银安盛", "西藏东财", "民生加银", "创金合信", "前海开源", "融通", "诺安",
    "西部利得", "山西证券", "天弘", "嘉实", "易方达", "华夏", "华安", "广发", "汇添富",
    "工银", "大成", "建信", "鹏华", "银华", "南方", "博时", "富国", "招商", "平安",
    "华宝", "国泰", "华泰", "长城", "申万菱信", "国投瑞银", "交银", "中欧", "银华",
    "浙商", "东海", "泰康", "新华", "弘毅", " Wave", "湘财", "恒生", "摩根", "外资",
]

ETF_CODE_PREFIXES = ("51", "58", "56", "15", "16", "159")


def parse_track_index(name: str) -> str:
    """从 ETF 名称解析跟踪指数/标的（如 '沪深300ETF嘉实' -> '沪深300'）。"""
    if not name:
        return ""
    head = name.split("ETF")[0]  # '沪深300ETF嘉实' -> '沪深300'; '纳指ETF广发' -> '纳指'
    idx = head
    for c in COMPANIES:
        cs = c.strip()
        if cs and idx.endswith(cs):
            idx = idx[: -len(cs)]
            break
    return idx.strip() or name


def strip_exch(x):
    s = str(x).strip().lower()
    if len(s) == 8 and s[:2] in ("sh", "sz", "bj"):
        return s[2:]
    return s


def fetch(force: bool = False, progress=None) -> dict:
    sys.path.insert(0, os.path.join(BASE, "extlib"))
    # Sina 直连可用；清掉代理环境变量避免被代理劫持东财式失败
    for k in ["http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY", "all_proxy", "ALL_PROXY"]:
        os.environ.pop(k, None)
    import akshare as ak

    df = ak.fund_etf_category_sina(symbol="ETF基金")
    df["n"] = df["代码"].map(strip_exch)

    quotes: dict = {}
    track: dict = {}
    for _, r in df.iterrows():
        code = str(r["n"])
        if len(code) != 6 or not code.isdigit() or not any(code.startswith(p) for p in ETF_CODE_PREFIXES):
            continue
        try:
            price = float(r["最新价"]) if r["最新价"] == r["最新价"] else None
            pct = float(r["涨跌幅"]) if r["涨跌幅"] == r["涨跌幅"] else None
            amount = float(r["成交额"]) if r["成交额"] == r["成交额"] else None
            volume = float(r["成交量"]) if r["成交量"] == r["成交量"] else None
        except Exception:
            price = pct = amount = volume = None
        name = str(r["名称"])
        quotes[code] = {"name": name, "price": price, "pct": pct,
                        "amount": amount, "volume": volume}
        track[code] = parse_track_index(name)

    out = {
        "source": "akshare.fund_etf_category_sina(symbol='ETF基金')",
        "as_of": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "quotes": quotes,
        "track_index": track,
        "n_total": len(quotes),
    }
    if force or not os.path.exists(CACHE) or True:
        with open(CACHE, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, indent=1)
    if progress:
        progress(out)
    return out


if __name__ == "__main__":
    def _p(o):
        print("已写入 %s  |  ETF 行情 %d 只  |  示例:" % (CACHE, o["n_total"]))
        items = list(o["quotes"].items())[:5]
        for c, v in items:
            print("  %s %s  最新价=%s 涨跌幅=%s%% 成交额=%s 跟踪指数=%s" % (
                c, v["name"], v["price"], v["pct"], v["amount"], o["track_index"].get(c)))
    fetch(force=True, progress=_p)
