# -*- coding: utf-8 -*-
"""龙虎榜「板块扩散」可行性探测
目标：验证 ① 个股→所属板块 映射可得 ② 板块成分股历史行情可得 ③ 扩散效应可量化
策略：映射走东财 push2（节流+重试），行情走新浪（稳定、全历史）
"""
import json
import os
import ssl
import sqlite3
import sys
import time
import urllib.parse
import urllib.request

BASE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(BASE, "viewer", "app.db")
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
import netctx  # noqa: E402  —— 统一出网 TLS 口径（整改 #27）
_CTX = netctx.https_context("push2.eastmoney.com")  # 整改 #27：默认不校验，按环境变量可开启
_OP = urllib.request.build_opener(urllib.request.ProxyHandler({}),
                                  urllib.request.HTTPSHandler(context=_CTX))
EM_UT = "fa5fd1943c7b386f172d6893dbfba10b"


def fetch(url, referer="https://quote.eastmoney.com/", retries=3, gap=1.0, timeout=15):
    """带节流与退避的取数；失败返回 None。"""
    last = None
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA, "Referer": referer})
            with _OP.open(req, timeout=timeout) as r:
                return r.read().decode("utf-8-sig", "ignore")
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(gap * (i + 1))
    print(f"    [fetch-fail] {type(last).__name__} {url[:88]}", flush=True)
    return None


def load_sector_whitelist():
    """行业板块 + 概念板块白名单 {BK代码: (名称, 类型)}"""
    wl = {}
    for fs, label in (("m:90+t:2+f:!50", "行业"), ("m:90+t:3+f:!50", "概念")):
        t = fetch(f"https://push2.eastmoney.com/api/qt/clist/get?pn=1&pz=1000&po=1&np=1"
                  f"&fltt=2&invt=2&fid=f12&fs={fs}&fields=f12,f14")
        if not t:
            print(f"  !! 白名单[{label}] 拉取失败")
            continue
        for x in (json.loads(t).get("data") or {}).get("diff") or []:
            wl[x["f12"]] = (x["f14"], label)
        time.sleep(0.8)
    return wl


def stock_sectors(code, wl):
    """一只股票所属板块（已按白名单过滤掉指数/两融等非题材板块）"""
    secid = ("1." if code[0] in "659" else "0.") + code
    t = fetch(f"https://push2.eastmoney.com/api/qt/slist/get?spt=3&fltt=1&invt=2"
              f"&fields=f12,f13,f14&secid={secid}&pn=1&pz=80&po=1&np=1&fid=f3&ut={EM_UT}")
    if not t:
        return None
    d = (json.loads(t).get("data") or {}).get("diff") or []
    return [(x["f12"], wl[x["f12"]][0], wl[x["f12"]][1])
            for x in d if x.get("f12") in wl]


def sector_members(bk):
    """板块成分股（代码列表）"""
    t = fetch(f"https://push2.eastmoney.com/api/qt/clist/get?pn=1&pz=500&po=1&np=1"
              f"&fltt=2&invt=2&fid=f3&fs=b:{bk}&fields=f12,f14")
    if not t:
        return []
    return [(x["f12"], x["f14"]) for x in (json.loads(t).get("data") or {}).get("diff") or []]


def sina_kline(code, datalen=10000):
    """新浪日K全历史（不复权），返回 [{date, open, high, low, close, volume}]"""
    sym = ("sh" if code[0] in "659" else "sz") + code
    t = fetch(f"https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/"
              f"CN_MarketData.getKLineData?symbol={sym}&scale=240&ma=no&datalen={datalen}",
              referer="https://finance.sina.com.cn/", retries=2, gap=0.6)
    if not t:
        return []
    try:
        raw = json.loads(t)
    except Exception:  # noqa: BLE001
        return []
    out = []
    for r in raw or []:
        try:
            out.append({"date": str(r["day"])[:10], "open": float(r["open"]),
                        "high": float(r["high"]), "low": float(r["low"]),
                        "close": float(r["close"]), "volume": float(r.get("volume") or 0)})
        except Exception:  # noqa: BLE001
            continue
    return out


def ret_after(kl, base_date, n):
    """以 base_date 收盘为基准，之后第 n 个交易日的收益率%"""
    idx = next((i for i, k in enumerate(kl) if k["date"] >= base_date), None)
    if idx is None or idx + n >= len(kl):
        return None
    c0, c1 = kl[idx]["close"], kl[idx + n]["close"]
    return None if not c0 else round((c1 / c0 - 1) * 100, 2)


def main():
    print("=" * 70)
    print("[1] 板块白名单")
    wl = load_sector_whitelist()
    print(f"    行业+概念共 {len(wl)} 个")
    if not wl:
        print("!! 东财 push2 不可用，中止")
        return 1

    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    rows = [dict(r) for r in con.execute(
        "SELECT security_code, security_name, trade_date, net_amt, change_rate, "
        "d1_chg, d3_chg, d5_chg FROM lhb_stocks "
        "WHERE trade_date=(SELECT MAX(trade_date) FROM lhb_stocks) "
        "ORDER BY net_amt DESC LIMIT 6")]
    con.close()
    print()
    print(f"[2] 库内最新上榜日样本（{rows[0]['trade_date'] if rows else '-'}，净买 Top6）")
    for r in rows:
        print(f"    {r['security_code']} {r['security_name']:<8} "
              f"净买{(r['net_amt'] or 0)/1e8:>6.2f}亿 当日{r['change_rate']}% "
              f"D1={r['d1_chg']} D3={r['d3_chg']}")

    print()
    print("[3] 个股 → 所属板块 映射（前 3 只）")
    mapped = []
    for r in rows[:3]:
        secs = stock_sectors(r["security_code"], wl)
        if secs is None:
            print(f"    {r['security_code']} 取数失败")
            continue
        mapped.append((r, secs))
        print(f"    {r['security_code']} {r['security_name']}: {len(secs)} 个板块")
        print(f"       {[s[1] for s in secs][:12]}")
        time.sleep(0.8)

    print()
    print("[4] 板块成分股 + 新浪行情 → 扩散计算可行性")
    if mapped:
        r0, secs = mapped[0]
        # 挑一个成分股数量适中的板块
        bk = None
        for s in secs:
            mem = sector_members(s[0])
            time.sleep(0.8)
            if 8 <= len(mem) <= 120:
                bk = (s, mem)
                break
        if bk:
            (bkc, bkn, bkt), mem = bk
            print(f"    板块「{bkn}」({bkc}/{bkt}) 成分 {len(mem)} 只")
            base = r0["trade_date"]
            sample = mem[:6]
            print(f"    以 {base} 为基准，抽样算后续涨幅：")
            for c, n in sample:
                kl = sina_kline(c)
                if not kl:
                    print(f"      {c} {n}: 无行情")
                    continue
                r1, r3 = ret_after(kl, base, 1), ret_after(kl, base, 3)
                mark = " ★上榜" if c == r0["security_code"] else ""
                print(f"      {c} {n:<8} T+1={r1}%  T+3={r3}%{mark}")
        else:
            print("    未找到成分数量适中的板块（跳过）")

    print()
    print("[5] push2his 板块指数是否可用")
    t = fetch(f"https://push2his.eastmoney.com/api/qt/stock/kline/get?secid=90.BK0493"
              f"&fields1=f1,f2,f3&fields2=f51,f52,f53,f54,f55,f56,f57&klt=101&fqt=1"
              f"&beg=20260901&end=20500101&ut={EM_UT}", retries=2)
    if t:
        kl = (json.loads(t).get("data") or {}).get("klines") or []
        print(f"    可用，{len(kl)} 根；末: {kl[-1] if kl else None}")
    else:
        print("    不可用（将用成分股等权自算板块涨幅）")
    print("=" * 70)
    return 0


if __name__ == "__main__":
    sys.exit(main())
