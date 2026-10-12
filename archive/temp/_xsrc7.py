# -*- coding: utf-8 -*-
"""两站取时间可恢复性实证：
   A) 雪球：从已存搜索页(124KB)里找帖子时间格式
   B) 东财：用浏览器(过WAF)抓原帖，看发布时间能否取到
"""
import sqlite3, os, re, json, sys, ssl, urllib.request

B = os.getcwd()
sys.path.insert(0, B)

con = sqlite3.connect('file:viewer/app.db?mode=ro', uri=True)
con.row_factory = sqlite3.Row

# ---------- A) 雪球搜索页 ----------
print("=" * 78)
print("A) 雪球搜索页(124KB)里找时间")
print("=" * 78)
row = con.execute("SELECT html FROM raw_pages WHERE site='xueqiu' AND length(html)>10000 ORDER BY rowid DESC LIMIT 1").fetchone()
html = row["html"] if row else ""
print("  样本长度:", len(html))

# 库里的雪球帖子 id
pids = ["410469761", "410418920", "410589620", "410294815", "410555794"]
for pid in pids:
    i = html.find(pid)
    if i < 0:
        print("  pid %s: 页面中未出现" % pid)
        continue
    seg = html[max(0, i - 700): i + 300]
    seg = re.sub(r"<[^>]+>", " ", seg)
    seg = re.sub(r"\s+", " ", seg)
    print("  pid %s 上下文: %s" % (pid, seg[-320:]))

print()
print("  --- 页内所有时间样式串 ---")
for pat in [r"20\d{2}-\d{2}-\d{2}\s+\d{2}:\d{2}", r"\d{1,2}-\d{1,2}\s+\d{2}:\d{2}",
            r"今天\s*\d{2}:\d{2}", r"\d{2}分钟前", r"\d{1,2}小时前", r"\d+月\d+日"]:
    hits = re.findall(pat, html)
    print("    %-26s %3d 个  %s" % (pat, len(hits), hits[:5]))

print()
print("  --- 页里是否含 date/created_at 字段(JSON 内嵌) ---")
for key in ["created_at", "timeBefore", "date", "timestamp", "\"time\""]:
    n = html.count(key)
    if n:
        i = html.find(key)
        print("    %-14s x%-3d  样例: %s" % (key, n, re.sub(r"\s+", " ", html[max(0, i-80): i+120])))
con.close()

# ---------- B) 东财原帖 ----------
print()
print("=" * 78)
print("B) 东财原帖取发布时间（浏览器过 WAF）")
print("=" * 78)
try:
    from scraper import browser_fetch as bf
    print("  browser_fetch 可用, _USE_PROXY =", getattr(bf, "_USE_PROXY", "?"))
    urls = [
        "https://guba.eastmoney.com/news,000001,1766128176.html",
        "https://guba.eastmoney.com/news,000001,1776681769.html",
    ]
    for u in urls:
        try:
            r = bf.browser_get(u)
            t = getattr(r, "text", "") or ""
            st = getattr(r, "status_code", "?")
            print("  %s -> HTTP %s len=%s" % (u[-40:], st, len(t)))
            # 找时间
            pats = [r"20\d{2}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}",
                    r"20\d{2}-\d{2}-\d{2}\s+\d{2}:\d{2}",
                    r"\d{4}年\d{1,2}月\d{1,2}日\s*\d{2}:\d{2}",
                    r"\d{2}-\d{2}\s+\d{2}:\d{2}"]
            for p in pats:
                h = re.findall(p, t)
                if h:
                    print("       命中 %-30s %s" % (p, h[:4]))
            # 找页面标题附近
            m = re.search(r"<title>(.*?)</title>", t, re.S)
            if m:
                print("       title:", re.sub(r"\s+", " ", m.group(1))[:70])
        except Exception as e:
            print("  %s FAIL %s %s" % (u[-40:], type(e).__name__, str(e)[:110]))
except Exception as e:
    print("  browser_fetch 不可用:", type(e).__name__, str(e)[:160])
