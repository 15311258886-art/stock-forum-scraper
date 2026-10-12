"""P2 设计系统收敛：改动前的完整清单取证。

输出三张表，供写「令牌映射」用：
  A. 硬编码色逐条（行号 / 选择器 / 取值 / 原文），按取值分组 —— 用来判断语义归属
  B. font-size 取值 × 选择器
  C. border-radius / 间距取值 × 选择器

只读，不改任何文件。用法：python tools/audit_p2_tokens.py
"""
import collections
import glob
import io
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
APP = os.path.join(ROOT, "viewer", "static", "app.css")

RE_HEX = re.compile(r"#[0-9A-Fa-f]{3,8}\b")
RE_SEL = re.compile(r"^[^\n{]*\{")
RE_VARFB = re.compile(r"var\(--[a-z0-9-]+,\s*$")


def sel_map(lines):
    """给每一行标注它所属的选择器（简化：取最近一个 '{' 之前的选择器文本）。"""
    out, cur = [], ""
    for l in lines:
        st = l.strip()
        if "{" in l and not st.startswith("/*"):
            cur = l.split("{")[0].strip()[:46]
        out.append(cur)
    return out


def main() -> int:
    src = io.open(APP, encoding="utf-8").read()
    lines = src.splitlines()
    sels = sel_map(lines)

    print("=" * 78)
    print("A. 真·硬编码色（已排除 var(--x, #hex) 回退值），按取值分组")
    print("=" * 78)
    groups = collections.defaultdict(list)
    skip_fb = 0
    for i, l in enumerate(lines):
        for m in RE_HEX.finditer(l):
            pre = l[max(0, m.start() - 48):m.start()]
            if RE_VARFB.search(pre):
                skip_fb += 1
                continue
            groups[m.group(0).lower()].append((i + 1, sels[i]))
    total = sum(len(v) for v in groups.values())
    print(f"  真硬编码 {total} 处 / {len(groups)} 种"
          f"（另有 {skip_fb} 处 var() 回退值，属防御写法，不改）")
    print()
    for v, items in sorted(groups.items(), key=lambda x: -len(x[1])):
        print(f"  {v:<10} x{len(items):<3} 选择器: {sorted({c for _, c in items})}")

    print()
    print("=" * 78)
    print("B. font-size 取值 × 选择器")
    print("=" * 78)
    fs = collections.defaultdict(list)
    for i, l in enumerate(lines):
        for m in re.finditer(r"font-size\s*:\s*([^;]+);", l):
            fs[m.group(1).strip()].append(sels[i])
    for v in sorted(fs, key=lambda x: (len(x), x)):
        print(f"  {v:<26} x{len(fs[v]):<3} {sorted(set(fs[v]))[:5]}")
    print("  种类:", len(fs))

    print()
    print("=" * 78)
    print("C. border-radius / padding / margin / gap 取值")
    print("=" * 78)
    for prop in ("border-radius", "padding", "margin", "gap"):
        vals = collections.Counter()
        for l in lines:
            for m in re.finditer(re.escape(prop) + r"(?:-(?:top|right|bottom|left))?\s*:\s*([^;]+);", l):
                for tok in m.group(1).split():
                    vals[tok.strip()] += 1
        print(f"  --- {prop}: {len(vals)} 种 ---")
        print("     " + "  ".join(f"{k}×{v}" for k, v in sorted(vals.items(), key=lambda x: -x[1])))
    return 0


if __name__ == "__main__":
    sys.exit(main())
