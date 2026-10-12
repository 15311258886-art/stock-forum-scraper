# -*- coding: utf-8 -*-
"""打印帖子的精确包裹结构"""
import os

from bs4 import BeautifulSoup

HERE = os.path.dirname(os.path.abspath(__file__))
html = open(os.path.join(HERE, "_calib_u0.html"), encoding="utf-8").read()
soup = BeautifulSoup(html, "lxml")
out = []
w = out.append

cont = soup.select_one("div.topic-content.left")
block = [c for c in cont.find_all(recursive=False)][1]
w("包裹容器: <%s class='%s'>  直接子元素 %d 个"
  % (block.name, ".".join(block.get("class") or []) or "-", len(block.find_all(recursive=False))))
w("=" * 78)
for i, c in enumerate(block.find_all(recursive=False)[:2]):
    w("---- 帖子 %d  outerHTML ----" % (i + 1))
    s = str(c)
    w(s[:2600])
    w("")

w("=" * 78)
w("【结构化提取测试】")
for i, c in enumerate(block.find_all(recursive=False)[:6]):
    title = c.select_one("a[href^='/a/']")
    author = c.select_one("div.topic-new a[href^='/blog/']")
    end = c.select_one("div.topic-end")
    w("%d) 标题=%r" % (i + 1, title.get_text(" ", strip=True)[:60] if title else None))
    w("   链接=%s" % (title["href"] if title else None))
    w("   作者=%r  uid=%s" % (author.get_text(" ", strip=True) if author else None,
                              author["href"].split("/")[-1] if author else None))
    w("   尾部=%r" % (end.get_text(" ", strip=True)[:70] if end else None))

with open(os.path.join(HERE, "_topic_struct2.txt"), "w", encoding="utf-8") as f:
    f.write("\n".join(str(x) for x in out))
print("ok")
