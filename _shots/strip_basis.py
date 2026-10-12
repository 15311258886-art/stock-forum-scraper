"""一次性脚本：删掉 screenerDeclineBody 内部那份 basisDetails 定义（已提到顶层共用）。"""
import io, sys

p = "viewer/static/app.js"
lines = io.open(p, encoding="utf-8").read().split("\n")

start = None
for i, l in enumerate(lines):
    if l.startswith("  const basisDetails = (d) =>"):
        start = i
        break
if start is None:
    print("NOT FOUND")
    sys.exit(0)

end = None
for j in range(start, min(start + 15, len(lines))):
    if lines[j].strip() == "</details>`;":
        end = j
        break
if end is None:
    print("END NOT FOUND")
    sys.exit(1)

print("removing lines", start + 1, "-", end + 1)
print("HEAD:", lines[start][:60])
print("TAIL:", lines[end][:60])
del lines[start:end + 1]
io.open(p, "w", encoding="utf-8", newline="\n").write("\n".join(lines))
print("done")
