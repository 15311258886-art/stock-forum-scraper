# -*- coding: utf-8 -*-
import re, io, os
B = r"F:\AI Agent部署知识库\2026-09-22-11-49-42\stock-forum-scraper"
js = io.open(os.path.join(B, "viewer", "static", "app.js"), encoding="utf-8").read()
h = io.open(os.path.join(B, "viewer", "static", "index.html"), encoding="utf-8").read()

print("=== NAV 归属（实盘操作类下的子标签）===")
for n, l in enumerate(js.splitlines(), 1):
    for t in ("players", "lhb", "lhb_sectors", "niusan", "consensus", "cohold"):
        if 'tab: "%s"' % t in l:
            print("  %5d: %s" % (n, l.strip()[:130]))

print()
print("=== niusan 关键行 ===")
for n, l in enumerate(js.splitlines(), 1):
    if "niusan" in l.lower() or "牛散" in l:
        print("  %5d: %s" % (n, l.strip()[:140]))

print()
print("=== index 面板 ===")
ids = [m.group(1) for m in re.finditer(r'id="(p-[a-z_]+)"', h)]
print("  含 p-niusan:", "p-niusan" in ids, "| 面板总数", len(ids))

print()
print("=== renderLhbSectors 面板 id（应已修正）===")
for n, l in enumerate(js.splitlines(), 1):
    if "p-lhb_sector" in l:
        print("  %5d: %s" % (n, l.strip()[:140]))
