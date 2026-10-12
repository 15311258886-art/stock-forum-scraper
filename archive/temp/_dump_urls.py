# -*- coding: utf-8 -*-
"""临时脚本：导出 09-27 / 09-28 真实条目的标题+URL（带链接）。用后即删。"""
import os, sqlite3
ROOT = os.path.dirname(os.path.abspath(__file__))
os.chdir(ROOT)
con = sqlite3.connect(os.path.join(ROOT, "viewer", "app.db")); con.row_factory = sqlite3.Row
out = []
for d in ("2026-09-28", "2026-09-27"):
    out.append("######## %s ########" % d)
    for r in con.execute(
        "SELECT title, url, source_name FROM overseas_news "
        "WHERE publish_time LIKE ? AND url NOT LIKE '%example.local%' ORDER BY source_name",
        (d + "%",)):
        out.append("%s | %s\n    %s" % (r["source_name"], r["title"], r["url"]))
con.close()
open(os.path.join(ROOT, "_urls.txt"), "w", encoding="utf-8").write("\n".join(out))
print("OK", len(out))
