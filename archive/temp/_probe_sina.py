# -*- coding: utf-8 -*-
"""临时探针6：新浪板块资金流是否有「时序/趋势」口径（决定曲线主源）（用完删）。"""
import json, time, urllib.request

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36"
BASE = "http://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"


def get(path):
    op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    u = BASE + path + ("&" if "?" in path else "?") + "_=" + str(int(time.time() * 1000))
    try:
        with op.open(urllib.request.Request(u, headers={"User-Agent": UA,
                  "Referer": "https://finance.sina.com.cn/"}), timeout=12) as r:
            return r.getcode(), r.read().decode("utf-8", "replace")
    except Exception as e:
        return None, f"{type(e).__name__}: {str(e)[:110]}"


cands = [
    ("ssl_bkzj_zjlrqs（资金流入趋势？）", "MoneyFlow.ssl_bkzj_zjlrqs?page=1&num=3&fenlei=1"),
    ("ssl_bkzj_zjlrqs fenlei=0", "MoneyFlow.ssl_bkzj_zjlrqs?page=1&num=3&fenlei=0"),
    ("ssl_bkzj_ssggzj（实时个股资金？）", "MoneyFlow.ssl_bkzj_ssggzj?page=1&num=3&fenlei=1"),
    ("ssl_bkzj_bk 带 sort=netamount", "MoneyFlow.ssl_bkzj_bk?page=1&num=3&sort=netamount&asc=0&fenlei=1"),
    ("ssl_bkzj_bk 单板块明细(带category)", "MoneyFlow.ssl_bkzj_bk?page=1&num=3&fenlei=1&category=gn_wjqy"),
]
for tag, p in cands:
    code, txt = get(p)
    print("=" * 74)
    print(f"[{tag}]  http={code}  bytes={len(txt) if txt else 0}")
    if txt and txt.startswith("["):
        try:
            arr = json.loads(txt)
            print(f"   条数={len(arr)}  字段={list(arr[0].keys()) if arr else '-'}")
            print("   样例:", json.dumps(arr[0], ensure_ascii=False)[:420])
        except Exception as e:
            print("   解析失败:", e, txt[:200])
    else:
        print("   原文:", (txt or "")[:300])
    print()
