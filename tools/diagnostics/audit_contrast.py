"""对比度体检：各主题 accent 上的文字该用什么颜色。

顺带核对既有 --warn/--err/--bull/--bear 与其底色的对比度，
以判断新增令牌时能否直接复用。
"""
import glob
import io
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TH = os.path.join(ROOT, "viewer", "static", "themes")


def lum(hexs):
    h = hexs.lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    r, g, b = (int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))

    def f(c):
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b)


def ratio(a, b):
    la, lb = lum(a), lum(b)
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


WHITE, INK = "#FFFFFF", "#0B0E14"

for p in sorted(glob.glob(os.path.join(TH, "*.css"))):
    name = os.path.basename(p).replace(".css", "")
    s = io.open(p, encoding="utf-8").read()
    v = dict(re.findall(r"(--[a-z0-9-]+)\s*:\s*([^;]+);", s))
    acc, acc2 = v.get("--accent", "").strip(), v.get("--accent2", "").strip()
    bg = v.get("--bg", "").strip()
    print("=" * 66)
    print(f"{name}   accent={acc}  accent2={acc2}  bg={bg}")
    for label, col in (("accent", acc), ("accent2", acc2)):
        w, k = ratio(col, WHITE), ratio(col, INK)
        print(f"  {label:<8} vs #fff = {w:5.2f}   vs #0B0E14 = {k:5.2f}"
              f"   → 应选 {'白字' if w >= k else '深色字'}"
              f"（{max(w, k):.2f}:1 {'✓' if max(w, k) >= 4.5 else '✗ 不足 4.5'}）")
    # 语义色与各自底色
    for fgk, bgk in (("--warn", "--warn-bg"), ("--err", "--err-bg"),
                     ("--bull", "--bull-bg"), ("--bear", "--bear-bg")):
        if fgk in v and bgk in v:
            r = ratio(v[fgk].strip(), v[bgk].strip())
            print(f"  {fgk:<8} on {bgk:<12} = {r:5.2f}  {'✓' if r >= 4.5 else '✗'}")
