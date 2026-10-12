# -*- coding: utf-8 -*-
"""淘股吧公开内容抓取器（无需登录部分）

抓取内容：
  · 实盘话题列表（talkSeq/14088 等）—— 帖子标题、摘要、作者、阅读数、时间
  · 淘股论坛总版
落地保存（"同步保留"）：
  data/taoguba_live_<date>.json   结构化全量
  data/taoguba_bloggers.json      昵称 ↔ uid 映射（供你圈定要重点关注的博主）
  data/taoguba_live_report.html   可读报告
用法：
  python tools/fetch_taoguba_live.py
"""
import json
import os
import re
import time
from datetime import datetime

import requests
from bs4 import BeautifulSoup

HERE = os.path.dirname(os.path.abspath(__file__))
PROJ = os.path.dirname(HERE)
DATA = os.path.join(PROJ, "data")
BASE = "https://www.tgb.cn"
TODAY = datetime.now().strftime("%Y-%m-%d")

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36")
HEADERS = {"User-Agent": UA, "Accept-Language": "zh-CN,zh;q=0.9",
           "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"}

# 来源页（键=URL，值=板块兜底名）
SOURCES = [
    ("https://www.tgb.cn/talk/talkSeq/14088", "实盘"),
    ("https://www.tgb.cn/bbs/", "淘股论坛"),
    ("https://www.tgb.cn/talk/talkSeq/190344", "实盘比赛"),
    ("https://www.tgb.cn/talk/talkSeq/188464", "实盘比赛"),
    ("https://www.tgb.cn/talk/talkSeq/187669", "实盘比赛"),
]


def fetch(url):
    try:
        r = requests.get(url, headers=HEADERS, timeout=25, allow_redirects=True)
        return r.text or "", r.url, r.status_code
    except Exception as e:  # noqa: BLE001
        return "", url, "ERR:%s" % str(e)[:80]


def parse_posts(html, src_url, board_hint):
    soup = BeautifulSoup(html, "lxml")
    posts = []
    for item in soup.select("div.topic-new"):
        block = item.parent
        if block is None:
            continue
        tlink = block.select_one("a[href^='/a/']")
        if not tlink:
            continue
        p = tlink.select_one("p")
        title = (p.get_text(" ", strip=True) if p else tlink.get_text(" ", strip=True))[:200]
        info_el = block.select_one("p.topic-info")
        summary = info_el.get_text(" ", strip=True)[:600] if info_el else ""
        nick_el = item.select_one("span")
        author = nick_el.get_text(" ", strip=True) if nick_el else ""
        blog_a = item.select_one("a[href^='/blog/']")
        uid = blog_a["href"].rstrip("/").split("/")[-1] if blog_a else ""
        board_a = block.select_one("a[href^='/talk/talkSeq/']")
        board = (board_a.get_text(" ", strip=True).lstrip("# ").strip()
                 if board_a else board_hint)
        end_el = block.select_one("div.topic-end")
        end_txt = end_el.get_text(" ", strip=True) if end_el else ""
        read = None
        m = re.search(r"(\d+)\s*阅读数", end_txt)
        if m:
            read = int(m.group(1))
        ptime = None
        m = re.search(r"(\d{2}-\d{2}\s+\d{2}:\d{2})", end_txt)
        if m:
            ptime = m.group(1)
        href = tlink["href"]
        posts.append({
            "board": board or board_hint,
            "title": title,
            "url": href if href.startswith("http") else BASE + href,
            "summary": summary,
            "author": author,
            "author_uid": uid,
            "author_url": (BASE + blog_a["href"]) if blog_a else "",
            "read_count": read,
            "post_time": ptime,
            "source_page": src_url,
            "fetched_at": datetime.now().isoformat(timespec="seconds"),
        })
    return posts


def main():
    os.makedirs(DATA, exist_ok=True)
    all_posts, seen, src_report = [], set(), []
    bloggers = {}

    for url, hint in SOURCES:
        html, final_url, status = fetch(url)
        posts = parse_posts(html, url, hint) if html else []
        src_report.append({"url": url, "final_url": final_url, "status": status,
                           "html_len": len(html), "parsed": len(posts)})
        for p in posts:
            key = p["url"]
            if key in seen:
                continue
            seen.add(key)
            all_posts.append(p)
            if p["author_uid"]:
                b = bloggers.setdefault(p["author_uid"], {
                    "uid": p["author_uid"], "name": p["author"] or "",
                    "url": p["author_url"], "post_count": 0, "boards": [],
                })
                b["post_count"] += 1
                if p["board"] not in b["boards"]:
                    b["boards"].append(p["board"])
        time.sleep(1.2)   # 温和限速

    payload = {
        "site": "taoguba",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "logged_in": False,
        "note": "本次为公开页抓取（未登录）。受限内容（我的关注/付费实盘明细）需登录 Cookie。",
        "source_report": src_report,
        "post_count": len(all_posts),
        "blogger_count": len(bloggers),
        "posts": all_posts,
    }
    p_json = os.path.join(DATA, "taoguba_live_%s.json" % TODAY)
    with open(p_json, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    with open(os.path.join(DATA, "taoguba_bloggers.json"), "w", encoding="utf-8") as f:
        json.dump({"generated_at": payload["generated_at"],
                   "bloggers": sorted(bloggers.values(), key=lambda x: -x["post_count"])},
                  f, ensure_ascii=False, indent=2)

    # —— 可读报告 ——
    rows = []
    for p in all_posts:
        rows.append(
            "<tr><td class='b'>%s</td><td><a href='%s' target='_blank' rel='noopener'>%s</a>"
            "<div class='sum'>%s</div></td><td>%s<div class='uid'>uid %s</div></td>"
            "<td class='n'>%s</td><td class='t'>%s</td></tr>"
            % (p["board"], p["url"], p["title"], (p["summary"] or "")[:180],
               p["author"] or "-", p["author_uid"] or "-",
               p["read_count"] if p["read_count"] is not None else "-", p["post_time"] or "-"))
    bl = "".join(
        "<div class='bl'><span class='nm'>%s</span><span class='uid'>uid %s</span>"
        "<span class='cnt'>%d 帖</span><a href='%s' target='_blank' rel='noopener'>主页</a></div>"
        % (b["name"] or "(未署名)", b["uid"], b["post_count"], b["url"])
        for b in sorted(bloggers.values(), key=lambda x: -x["post_count"]))
    sr = "".join("<li><code>%s</code> → HTTP %s, %d 条</li>" % (s["url"], s["status"], s["parsed"])
                 for s in src_report)

    html = """<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<title>淘股吧公开内容抓取报告 %s</title>
<style>
:root{--bg:#0d1117;--pn:#161b22;--bd:#30363d;--tx:#e6edf3;--mu:#8b949e;--ac:#58a6ff;--rd:#f85149}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--tx);
font:14px/1.6 -apple-system,"Segoe UI","Microsoft YaHei",sans-serif;padding:28px}
h1{font-size:22px;margin:0 0 6px}h2{font-size:16px;margin:28px 0 10px;padding-left:9px;border-left:3px solid var(--ac)}
.meta{color:var(--mu);font-size:13px;margin-bottom:16px}
.kpi{display:flex;gap:12px;margin:16px 0 24px;flex-wrap:wrap}
.kpi div{background:var(--pn);border:1px solid var(--bd);border-radius:8px;padding:12px 18px;min-width:120px}
.kpi b{display:block;font-size:24px;color:var(--ac)}
.kpi span{color:var(--mu);font-size:12px}
table{width:100%%;border-collapse:collapse;background:var(--pn);border:1px solid var(--bd);border-radius:8px;overflow:hidden}
th,td{padding:10px 12px;border-bottom:1px solid var(--bd);text-align:left;vertical-align:top}
th{background:#1c2128;color:var(--mu);font-weight:600;font-size:12px}
tr:last-child td{border-bottom:none}
a{color:var(--ac);text-decoration:none}a:hover{text-decoration:underline}
.sum{color:var(--mu);font-size:12px;margin-top:4px}
.uid{color:#6e7681;font-size:11px}
.b{color:#d29922;white-space:nowrap;font-weight:600}
.n,.t{white-space:nowrap;color:var(--mu);font-size:12px}
.bl{display:flex;gap:12px;align-items:center;background:var(--pn);border:1px solid var(--bd);
border-radius:6px;padding:8px 14px;margin-bottom:8px}
.nm{font-weight:600;flex:1}.cnt{color:#3fb950;font-size:12px}
.note{background:#1c2128;border-left:3px solid var(--rd);padding:12px 16px;border-radius:6px;
color:#ffa198;font-size:13px;margin-bottom:8px}
ul{color:var(--mu);font-size:12px}code{color:#79c0ff}
</style></head><body>
<h1>淘股吧公开内容抓取报告</h1>
<div class="meta">生成时间 %s · 数据源 tgb.cn · 本次为<b>公开页抓取（未登录）</b></div>
<div class="note">受限内容未包含：本次未取得有效登录态，故「我的关注」、付费实盘明细等需登录的板块未抓取。详见登录诊断报告。</div>
<div class="kpi"><div><b>%d</b><span>抓到帖子</span></div><div><b>%d</b><span>涉及博主</span></div>
<div><b>%d</b><span>来源页</span></div></div>
<h2>抓取来源</h2><ul>%s</ul>
<h2>博主清单（昵称 ↔ uid，可用于指定重点关注）</h2>%s
<h2>帖子明细</h2>
<table><thead><tr><th>板块</th><th>标题 / 摘要</th><th>发起人</th><th>阅读</th><th>时间</th></tr></thead>
<tbody>%s</tbody></table>
</body></html>""" % (TODAY, payload["generated_at"], len(all_posts), len(bloggers),
                     len(src_report), sr, bl, "".join(rows))

    p_html = os.path.join(DATA, "taoguba_live_report.html")
    with open(p_html, "w", encoding="utf-8") as f:
        f.write(html)

    print(json.dumps({"json": p_json, "html": p_html, "posts": len(all_posts),
                      "bloggers": len(bloggers), "sources": src_report},
                     ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
