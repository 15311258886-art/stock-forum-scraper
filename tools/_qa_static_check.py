# -*- coding: utf-8 -*-
"""验证静态资源未被 P0 修复误伤（只读）。"""
import urllib.request

op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
lines = []
for p in ["/", "/index.html", "/static/app.js", "/static/app.css",
          "/static/nonexistent.js", "/static/"]:
    try:
        with op.open("http://127.0.0.1:8765" + p, timeout=10) as r:
            b = r.read()
        lines.append(f"{p:<26} HTTP {r.status}  {len(b):>7} bytes  {r.headers.get('Content-Type','')}")
    except Exception as e:
        lines.append(f"{p:<26} {e!r}")

txt = "\n".join(lines)
with open("tools/_qa_static_check.txt", "w", encoding="utf-8") as f:
    f.write(txt + "\n")
print("done")
