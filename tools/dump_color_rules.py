"""列出 app.css 中含硬编码色的规则块全文（供 P2 语义映射决策，不修改文件）。"""
import io
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
P = os.path.join(ROOT, "viewer", "static", "app.css")
s = io.open(P, encoding="utf-8").read()
lines = s.splitlines()

# 跳过 var(--x, #hex) 内的回退值
def is_real(line, m):
    pre = line[max(0, m.start() - 44):m.start()]
    return not re.search(r"var\(--[a-z0-9-]+,\s*$", pre)

# 按规则块收集
blocks = []
cur_sel, cur_start, cur_lines = None, None, []
depth = 0
for i, l in enumerate(lines, 1):
    if cur_sel is None:
        if "{" in l and not l.strip().startswith("/*"):
            cur_sel = l.split("{")[0].strip()
            cur_start = i
            cur_lines = [l]
            depth = l.count("{") - l.count("}")
    else:
        cur_lines.append(l)
        depth += l.count("{") - l.count("}")
        if depth <= 0:
            body = "\n".join(cur_lines)
            if any(is_real(x, m) for x in cur_lines for m in re.finditer(r"#[0-9A-Fa-f]{3,8}\b", x)):
                blocks.append((cur_start, cur_sel, body))
            cur_sel, cur_start, cur_lines = None, None, []

print("含硬编码色的规则块：%d 个" % len(blocks))
print()
for st, sel, body in blocks:
    hexes = []
    for x in body.splitlines():
        for m in re.finditer(r"#[0-9A-Fa-f]{3,8}\b", x):
            if is_real(x, m):
                hexes.append(m.group(0).lower())
    print("-" * 78)
    print("L%-5d %s   %s" % (st, sel[:70], " ".join(sorted(set(hexes)))))
    print(body.strip()[:420])
