"""设计系统一致性 + 信息架构 静态审计（只读，不修改任何界面文件）。

输出：
  [1] NAV 两级导航清单
  [2] 字号阶梯分布
  [3] 间距(padding/margin/gap)分布 → 是否遵循 4/8 栅格
  [4] 圆角取值分布
  [5] 硬编码颜色（非 var）TOP 清单 + 所在选择器
  [6] 4 套主题变量集合是否对齐
  [7] 说明文字 .hint 的重复度
  [8] 各面板渲染函数位置 + 回测相关代码位置
"""
import io
import os
import re
from collections import Counter, defaultdict

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CSS = os.path.join(ROOT, "viewer", "static", "app.css")
JS = os.path.join(ROOT, "viewer", "static", "app.js")
HTML = os.path.join(ROOT, "viewer", "static", "index.html")
THEMES = os.path.join(ROOT, "viewer", "static", "themes")

css = io.open(CSS, encoding="utf-8").read()
js = io.open(JS, encoding="utf-8").read()
html = io.open(HTML, encoding="utf-8").read()


def line_of(text, idx):
    return text.count("\n", 0, idx) + 1


print("=" * 78)
print("[1] NAV 两级导航清单")
print("=" * 78)
m = re.search(r"const NAV = \[(.*?)\n\];", js, re.S)
if m:
    body = m.group(1)
    cats = re.findall(r'key:\s*"([^"]+)",\s*label:\s*"([^"]+)"', body)
    items = re.findall(r'\{\s*tab:\s*"([^"]+)",\s*label:\s*"([^"]+)"', body)
    print("  大类:", len(cats), " 子项:", len(items))
    for k, l in cats:
        print(f"    - {k:<14} {l}")
    print("  子项:", ", ".join(f"{t}" for t, _ in items))

print()
print("=" * 78)
print("[2] 字号阶梯分布（app.css + themes）")
print("=" * 78)
allcss = css
for f in sorted(os.listdir(THEMES)):
    allcss += io.open(os.path.join(THEMES, f), encoding="utf-8").read()
fs = Counter(re.findall(r"font-size:\s*([\d.]+px)", allcss))
print(f"  共 {len(fs)} 种取值：")
for v, n in sorted(fs.items(), key=lambda x: float(x[0][:-2])):
    print(f"    {v:>8}  ×{n}")
rem = [v for v in fs if float(v[:-2]) % 2 != 0 and float(v[:-2]) not in (9.5, 12.5, 11.5, 13.5)]
print(f"  非整偶数取值（阶梯易乱）: {sorted(rem, key=lambda x: float(x[:-2]))}")

print()
print("=" * 78)
print("[3] 间距分布（padding/margin/gap）→ 4/8 栅格?")
print("=" * 78)
sp = Counter(re.findall(r"(?:padding|margin|gap)(?:-[a-z]+)?:\s*([\d.]+px)", allcss))
print(f"  共 {len(sp)} 种取值：")
off_grid = []
for v, n in sorted(sp.items(), key=lambda x: float(x[0][:-2])):
    px = float(v[:-2])
    ok = (px % 4 == 0)
    if not ok:
        off_grid.append((v, n))
    print(f"    {v:>8}  ×{n:<4} {'栅格内' if ok else '** 非4倍数'}")
print(f"  非 4 倍数取值共 {len(off_grid)} 种，出现 {sum(n for _, n in off_grid)} 次")

print()
print("=" * 78)
print("[4] 圆角取值分布")
print("=" * 78)
rad = Counter(re.findall(r"border-radius:\s*([^;]+);", allcss))
for v, n in sorted(rad.items(), key=lambda x: -x[1]):
    print(f"    {v.strip():<34} ×{n}")

print()
print("=" * 78)
print("[5] 硬编码颜色（未走 var(--*)）TOP，含所在选择器")
print("=" * 78)
hits = []
for mm in re.finditer(r"(#[0-9A-Fa-f]{3,8}\b|rgba?\([^)]*\))", css):
    col = mm.group(0)
    # 找出紧邻上方的选择器
    start = css.rfind("\n", 0, max(0, mm.start() - 400))
    seg = css[max(0, mm.start() - 400):mm.start()]
    sel = ""
    for s in reversed(re.split(r"[{}]", seg)):
        if s.strip() and ":" not in s.split("\n")[-1]:
            sel = s.strip().split("\n")[-1]
            break
    hits.append((col, line_of(css, mm.start()), sel[:56]))
print(f"  app.css 中硬编码颜色共 {len(hits)} 处")
c = Counter(h[0] for h in hits)
print("  按取值聚合 TOP 20：")
for col, n in c.most_common(20):
    lines = [h[1] for h in hits if h[0] == col][:6]
    print(f"    {col:<26} ×{n:<4} 行 {lines}")

print()
print("=" * 78)
print("[6] 4 套主题变量集合是否对齐")
print("=" * 78)
sets = {}
for f in sorted(os.listdir(THEMES)):
    s = io.open(os.path.join(THEMES, f), encoding="utf-8").read()
    root = re.search(r":root\s*\{(.*?)\}", s, re.S)
    names = set(re.findall(r"(--[a-z0-9-]+)\s*:", root.group(1))) if root else set()
    sets[f] = names
    print(f"  {f:<26} 变量 {len(names)} 个")
if sets:
    base = max(sets.values(), key=len)
    base_name = [k for k, v in sets.items() if v is base][0]
    print(f"  以 {base_name}（{len(base)} 个）为基准比对：")
    for k, v in sets.items():
        miss = base - v
        extra = v - base
        print(f"    {k:<26} 缺 {len(miss):<3} 多 {len(extra):<3} "
              f"{'缺:' + ','.join(sorted(miss)[:6]) if miss else ''}")

print()
print("=" * 78)
print("[7] 说明文字密度：.hint / 大段解释")
print("=" * 78)
hints = re.findall(r'class="hint"[^>]*>(.*?)</div>', js, re.S)
print(f"  app.js 中 hint 块共 {len(hints)} 处")
clean = [re.sub(r"<[^>]+>", "", h).strip() for h in hints]
clean = [re.sub(r"\$\{[^}]*\}", "…", t) for t in clean]
for i, t in enumerate(clean, 1):
    print(f"    {i:>2}. ({len(t):>4}字) {t[:88]}")
print(f"  hint 文字合计 {sum(len(t) for t in clean)} 字")
print(f"  其它说明样式出现次数：")
for cls in ["class='empty'", 'class="empty"', "class='s'", 'class="s"', "class='mut'", 'class="mut"']:
    print(f"    {cls:<18} ×{js.count(cls)}")

print()
print("=" * 78)
print("[8] 面板渲染函数 + 回测相关代码位置")
print("=" * 78)
for mm in re.finditer(r"^function (render\w+|loadKb\w*|ensureKb\w*)\(.*", js, re.M):
    print(f"    JS:{line_of(js, mm.start()):<6} {mm.group(1)}")
print()
print("  回测相关命中：")
for f, txt in (("app.js", js), ("kb.py", None)):
    pass
for mm in re.finditer(r"^.*(backtest|回测|策略回测|table_key).*$", js, re.M | re.I):
    print(f"    app.js:{line_of(js, mm.start()):<6} {mm.group(0).strip()[:110]}")
kb = os.path.join(ROOT, "viewer", "kb.py")
if os.path.exists(kb):
    k = io.open(kb, encoding="utf-8").read()
    for mm in re.finditer(r"^.*(backtest|附录B|策略回测).*$", k, re.M):
        print(f"    kb.py:{line_of(k, mm.start()):<6} {mm.group(0).strip()[:110]}")
