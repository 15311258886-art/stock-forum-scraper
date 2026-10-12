"""2026-10-05：修 rate_timeline 被顶层 def 截断的锅。
_ME：把 _rate_track / _pick 这两个顶层 def 插到了 rate_timeline 函数体中间，
Python 于是认为 rate_timeline 到那行就结束（返回 None），后面整段时间轴逻辑掉到模块层。
本脚本把这两个 def 整块搬到 def rate_timeline 之前，恢复函数体连续。"""
import ast
import io
import sys

sys.path.insert(0, ".")
p = "screener.py"
src = io.open(p, encoding="utf-8").read()
lines = src.split("\n")
tree = ast.parse(src)

blocks, names = [], []
for node in tree.body:
    if isinstance(node, ast.FunctionDef) and node.name in ("_rate_track", "_pick"):
        names.append(node.name)
        blocks.append((node.lineno, node.end_lineno))
if not blocks:
    print("没找到待移动的 def，可能已经修过了")
    raise SystemExit(0)

# 1) 从原位置整块删除（倒序删，免得行号错位），删完把该处多余空行收干净
for a, b in sorted(blocks, reverse=True):
    del lines[a - 1:b]
    while a - 1 < len(lines) and lines[a - 1].strip() == "":
        del lines[a - 1]
        a -= 1
    if a - 1 < len(lines) and lines[a - 1].strip() != "":
        lines.insert(a, "")
        lines.insert(a + 1, "")

# 2) 搬到 def rate_timeline 之前（顺序按源码原序）
blocks.sort()
moved = []
for a, b in blocks:
    moved.extend(lines[a - 1:b])
tl = next((i for i, l in enumerate(lines) if l.startswith("def rate_timeline")), None)
if tl is None:
    print("找不到 def rate_timeline")
    raise SystemExit(1)
lines[tl:tl] = [""] + moved + ["", ""]

io.open(p, "w", encoding="utf-8").write("\n".join(lines))
re = ast.parse(io.open(p, encoding="utf-8").read())
print("已移动:", ", ".join(names))
for node in re.body:
    if isinstance(node, ast.FunctionDef) and node.name in ("rate_timeline", "_rate_track", "_pick"):
        end = getattr(node, "end_lineno", "?")
        print("  def %-14s -> 行 %s ~ %s" % (node.name, node.lineno, end))
