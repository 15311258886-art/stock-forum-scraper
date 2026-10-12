# -*- coding: utf-8 -*-
"""临时探针：板块资金流接口连通性 + 分时曲线可行性（用完删）。"""
import json, sys, time
sys.path.insert(0, ".")
import emapi

UT = "fa5fd1943c7b386f172d6893dbfba10b"
ACC = "b2884a393a59ad64002292a3e90d46a5"


def show(tag, j, n=3):
    if not j:
        print(f"  [{tag}] 空/失败")
        return None
    rc = j.get("rc")
    data = j.get("data")
    if data is None:
        print(f"  [{tag}] rc={rc} data=None  keys={list(j.keys())[:8]}")
        return None
    diff = data.get("diff") if isinstance(data, dict) else None
    if isinstance(diff, dict):
        diff = [diff[k] for k in sorted(diff.keys(), key=lambda x: int(x) if str(x).isdigit() else x)]
    if diff:
        print(f"  [{tag}] rc={rc} total={data.get('total')} 条数={len(diff)}")
        for r in diff[:n]:
            print("      ", json.dumps(r, ensure_ascii=False)[:220])
    else:
        print(f"  [{tag}] rc={rc} data keys={list(data.keys())[:10]}")
    return diff


print("=" * 70)
print("A. 板块快照 clist —— 不同 fs / 端点 / ut 组合")
print("=" * 70)
for fs in ("m:90+t:2", "m:90+t:2+f:!50", "m:90+t:3+f:!50"):
    for host in ("https://push2.eastmoney.com", "https://push2his.eastmoney.com"):
        for ut in (UT, ACC):
            url = (f"{host}/api/qt/clist/get?fid=f62&po=1&pz=5&pn=1&np=1&fltt=2&invt=2"
                   f"&fs={emapi.urllib.parse.quote(fs)}&fields=f12,f14,f3,f62,f184,f66"
                   f"&ut={ut}&_={int(time.time()*1000)}")
            j = emapi.get_json(url, "https://data.eastmoney.com/zjlx/list.html", timeout=10, retries=0)
            show(f"fs={fs} host={host.split('//')[1].split('.')[0]} ut={ut[:4]}", j, 2)

print()
print("=" * 70)
print("B. 板块列表（拿 BK 代码，供分时曲线用）")
print("=" * 70)
url = (f"https://push2.eastmoney.com/api/qt/clist/get?fid=f3&po=1&pz=3&pn=1&np=1&fltt=2&invt=2"
       f"&fs={emapi.urllib.parse.quote('m:90+t:2+f:!50')}&fields=f12,f14,f3,f62&ut={UT}&_={int(time.time()*1000)}")
j = emapi.get_json(url, "https://quote.eastmoney.com/", timeout=10, retries=0)
d = show("board list", j, 3)

print()
print("=" * 70)
print("C. 板块分时资金流 fflow/kline（klt=1 分钟）—— 截图1 那种曲线的数据源")
print("=" * 70)
bk = None
if d:
    bk = d[0].get("f12")
bks = [x for x in (["BK0447", "BK0448", "BK0493"] if not bk else [bk, "BK0447", "BK0493"])]
for code in bks[:3]:
    for host in ("https://push2.eastmoney.com", "https://push2his.eastmoney.com"):
        for klt in (1, 101):
            url = (f"{host}/api/qt/stock/fflow/kline/get?secid=90.{code}"
                   f"&fields1=f1,f2,f3,f7"
                   f"&fields2=f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61,f62,f63,f64,f65"
                   f"&klt={klt}&lmt=0&ut={UT}&_={int(time.time()*1000)}")
            j = emapi.get_json(url, "https://quote.eastmoney.com/", timeout=10, retries=0)
            if not j:
                print(f"  [{code} klt={klt} {host.split('//')[1].split('.')[0]}] 失败")
                continue
            kl = ((j.get("data") or {}).get("klines")) or []
            nm = ((j.get("data") or {}).get("name")) or ""
            print(f"  [{code} klt={klt} {host.split('//')[1].split('.')[0]}] rc={j.get('rc')} name={nm} klines={len(kl)}")
            if kl:
                print("       首行:", kl[0])
                print("       末行:", kl[-1])
