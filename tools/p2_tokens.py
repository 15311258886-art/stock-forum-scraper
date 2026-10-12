"""P2a 令牌层：定义语义色 / 字号 / 圆角 / 间距令牌，校验对比度后写入。

为什么需要这个脚本，而不是手改 CSS：
  1. 色值 53 种、字号 17 种、圆角 14 种 —— 手改必漏；
  2. 新增令牌必须在**写入前**证明「文字色 / 底色」对比度 ≥4.5，
     否则会出现「深色主题下白字压在亮青底上」这种看不见的缺陷（改动前实测 1.81:1）。

分工：
  - 颜色令牌 → 写进 4 个主题文件（各主题取值不同：浅色用深字浅底，深色用浅字深底）
  - 字号 / 圆角 / 间距令牌 → 写进 app.css 的 :root（与主题无关，不必 4 份重复）

用法：
  python tools/p2_tokens.py --check    # 只校验对比度，不写文件
  python tools/p2_tokens.py --write     # 校验通过后写入令牌块
"""
import io
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TH = os.path.join(ROOT, "viewer", "static", "themes")
APP = os.path.join(ROOT, "viewer", "static", "app.css")

LIGHT_CAT = {
    # 层级 A~F 与平台/地区徽标：浅底深字
    "--c1-fg": "#92400E", "--c1-bg": "#FEF3C7",   # A 层 · 琥珀
    "--c2-fg": "#1D4ED8", "--c2-bg": "#DBEAFE",   # B 层 · 蓝
    "--c3-fg": "#15803D", "--c3-bg": "#DCFCE7",   # C 层 · 绿
    "--c4-fg": "#A21CAF", "--c4-bg": "#FAE8FF",   # D 层 · 紫红
    "--c5-fg": "#0369A1", "--c5-bg": "#E0F2FE",   # E 层 · 天蓝
    "--c6-fg": "#475569", "--c6-bg": "#F1F5F9",   # F 层 · 灰蓝
    "--c-orange-fg": "#C2410C", "--c-orange-bg": "#FFEDD5", "--c-orange-line": "#FDBA74",
    "--c-violet-fg": "#6D28D9", "--c-violet-bg": "#EDE9FE", "--c-violet-line": "#7C3AED",
    "--c-teal-fg": "#0F766E", "--c-teal-bg": "#CCFBF1", "--c-teal-line": "#14B8A6",
    # 状态：成功 / 警示 / 错误 / 中性
    "--ok": "#16A34A", "--ok-fg": "#15803D", "--ok-bg": "#DCFCE7", "--ok-strong": "#0A7D3C",
    "--warn-line": "#FCD34D", "--warn-strong": "#F59E0B",
    "--err-bg": "#FEE2E2", "--err-line": "#FECACA", "--err-strong": "#EF4444",
    "--info": "#38BDF8",
    "--neutral-fg": "#4B5563", "--neutral-bg": "#F3F4F6", "--neutral-line": "#9CA3AF",
}

DARK_CAT = {
    "--c1-fg": "#FCD34D", "--c1-bg": "#3A2A0A",
    "--c2-fg": "#93C5FD", "--c2-bg": "#16263F",
    "--c3-fg": "#6EE7B7", "--c3-bg": "#10291F",
    "--c4-fg": "#F0ABFC", "--c4-bg": "#2C1436",
    "--c5-fg": "#7DD3FC", "--c5-bg": "#0C2A3A",
    "--c6-fg": "#94A3B8", "--c6-bg": "#232B38",
    "--c-orange-fg": "#FDBA74", "--c-orange-bg": "#3A2109", "--c-orange-line": "#C2410C",
    "--c-violet-fg": "#C4B5FD", "--c-violet-bg": "#241A3D", "--c-violet-line": "#8B5CF6",
    "--c-teal-fg": "#5EEAD4", "--c-teal-bg": "#0C2A28", "--c-teal-line": "#14B8A6",
    "--ok": "#34D399", "--ok-fg": "#6EE7B7", "--ok-bg": "#10291F", "--ok-strong": "#34D399",
    "--warn-line": "#FCD34D", "--warn-strong": "#F59E0B",
    "--err-bg": "#2A1414", "--err-line": "#7F1D1D", "--err-strong": "#EF4444",
    "--info": "#38BDF8",
    "--neutral-fg": "#94A3B8", "--neutral-bg": "#1E2735", "--neutral-line": "#3A4757",
}

# 每套主题额外需要的「实心面文字色」（必须逐主题定：accent 有亮有暗）
ON_ACCENT = {
    "light-tech.css": "#FFFFFF",        # accent #0E7490 深青 → 白字 5.36:1
    "minimal-report.css": "#FFFFFF",    # accent #334155 石板 → 白字 10.35:1
    "card-warm.css": "#3B2410",         # accent #D97706 橙 → 白字仅 3.19:1，必须深字
    "dark-dashboard.css": "#06181D",    # accent #22D3EE 亮青 → 白字仅 1.81:1，必须深字
}

# 「成功实心徽标」的文字色必须独立于 --on-accent：
# 初版把两者混用，校验器当场算出 card-warm 上「深棕字 on 深绿底」= 2.77:1 —— 一个令牌
# 被两种底色共用就会这样崩。深色主题的 --ok-strong 是亮绿 #34D399，白字只有 1.92:1，
# 也必须深字。故两张表都得逐主题定。
ON_OK_STRONG = {
    "light-tech.css": "#FFFFFF",        # on #0A7D3C 深绿 → 5.23:1
    "minimal-report.css": "#FFFFFF",    # on #0A7D3C 深绿 → 5.23:1
    "card-warm.css": "#FFFFFF",         # on #0A7D3C 深绿 → 5.23:1
    "dark-dashboard.css": "#06181D",    # on #34D399 亮绿 → 9.45:1（白字只有 1.92:1）
}

# 字号 / 圆角 / 间距：与主题无关，统一放 app.css :root
SCALE = """  /* 字号阶梯（原 17 种取值 → 7 档；两个 clamp() 容器查询值有意保留，属 P1 成果） */
  --fs-2xs: 10px; --fs-xs: 11px; --fs-sm: 12px; --fs-md: 13px;
  --fs-base: 14px; --fs-lg: 16px; --fs-xl: 22px;
  /* 圆角（原 14 种 → 3 档；50% 圆形与 999px 胶囊按语义保留） */
  --r-sm: 6px; --r-md: 8px; --r-lg: 12px;
  /* 间距（统一到 4px 栅格；1~3px 的视觉微调保留，不参与栅格化） */
  --sp-1: 4px; --sp-2: 8px; --sp-3: 12px; --sp-4: 16px;
  --sp-5: 20px; --sp-6: 24px; --sp-7: 32px; --sp-8: 40px;"""


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


def var_of(path, name):
    s = io.open(path, encoding="utf-8").read()
    m = re.search(re.escape(name) + r"\s*:\s*([^;]+);", s)
    return m.group(1).strip() if m else None


def validate():
    """校验每套主题里所有「文字色 on 底色」组合的对比度。"""
    bad = []
    print("=" * 74)
    print("对比度校验（阈值 4.5:1，WCAG AA 正文）")
    print("=" * 74)
    for fn, cat in (("light-tech.css", LIGHT_CAT), ("minimal-report.css", LIGHT_CAT),
                    ("card-warm.css", LIGHT_CAT), ("dark-dashboard.css", DARK_CAT)):
        p = os.path.join(TH, fn)
        accent = var_of(p, "--accent")
        warn = var_of(p, "--warn")
        warn_bg = var_of(p, "--warn-bg")
        err = var_of(p, "--err")
        pairs = [
            ("--on-accent", ON_ACCENT[fn], "accent", accent),
            ("--c1-fg", cat["--c1-fg"], "--c1-bg", cat["--c1-bg"]),
            ("--c2-fg", cat["--c2-fg"], "--c2-bg", cat["--c2-bg"]),
            ("--c3-fg", cat["--c3-fg"], "--c3-bg", cat["--c3-bg"]),
            ("--c4-fg", cat["--c4-fg"], "--c4-bg", cat["--c4-bg"]),
            ("--c5-fg", cat["--c5-fg"], "--c5-bg", cat["--c5-bg"]),
            ("--c6-fg", cat["--c6-fg"], "--c6-bg", cat["--c6-bg"]),
            ("--c-orange-fg", cat["--c-orange-fg"], "--c-orange-bg", cat["--c-orange-bg"]),
            ("--c-violet-fg", cat["--c-violet-fg"], "--c-violet-bg", cat["--c-violet-bg"]),
            ("--c-teal-fg", cat["--c-teal-fg"], "--c-teal-bg", cat["--c-teal-bg"]),
            ("--ok-fg", cat["--ok-fg"], "--ok-bg", cat["--ok-bg"]),
            ("--on-ok-strong", ON_OK_STRONG[fn], "--ok-strong", cat["--ok-strong"]),
            ("--warn", warn, "--warn-bg", warn_bg),
            ("--err", err, "--err-bg", cat["--err-bg"]),
            ("--neutral-fg", cat["--neutral-fg"], "--neutral-bg", cat["--neutral-bg"]),
        ]
        print(f"\n{fn}  (accent={accent})")
        for fgname, fg, bgname, bg in pairs:
            if not fg or not bg:
                continue
            r = ratio(fg, bg)
            flag = "✓" if r >= 4.5 else "✗"
            if r < 4.5:
                bad.append((fn, fgname, fg, bgname, bg, r))
            print(f"  {flag} {fgname:<14} {fg:<9} on {bgname:<14} {bg:<9} = {r:5.2f}")
    print()
    if bad:
        print("!! 未达标的组合：")
        for b in bad:
            print("   ", b)
    else:
        print("全部组合 ≥4.5:1")
    return bad


def write():
    for fn, cat in (("light-tech.css", LIGHT_CAT), ("minimal-report.css", LIGHT_CAT),
                    ("card-warm.css", LIGHT_CAT), ("dark-dashboard.css", DARK_CAT)):
        p = os.path.join(TH, fn)
        s = io.open(p, encoding="utf-8").read()
        if "--c1-fg" in s:
            print(f"  跳过（已有令牌）: {fn}")
            continue
        blk = ["\n/* ---- P2 语义令牌层（2026-09-23）：徽标 / 状态色不再写死，4 套主题各一套 ---- */",
               ":root {"]
        blk.append(f"  --on-accent: {ON_ACCENT[fn]};")
        blk.append(f"  --on-ok-strong: {ON_OK_STRONG[fn]};")
        for k in ("--c1-fg", "--c1-bg", "--c2-fg", "--c2-bg", "--c3-fg", "--c3-bg",
                  "--c4-fg", "--c4-bg", "--c5-fg", "--c5-bg", "--c6-fg", "--c6-bg",
                  "--c-orange-fg", "--c-orange-bg", "--c-orange-line",
                  "--c-violet-fg", "--c-violet-bg", "--c-violet-line",
                  "--c-teal-fg", "--c-teal-bg", "--c-teal-line",
                  "--ok", "--ok-fg", "--ok-bg", "--ok-strong",
                  "--warn-line", "--warn-strong",
                  "--err-bg", "--err-line", "--err-strong", "--info",
                  "--neutral-fg", "--neutral-bg", "--neutral-line"):
            blk.append(f"  {k}: {cat[k]};")
        blk.append("}")
        io.open(p, "a", encoding="utf-8").write("\n".join(blk) + "\n")
        print(f"  已写入令牌块: {fn}  (+{len(blk) - 1} 行)")

    s = io.open(APP, encoding="utf-8").read()
    if "--fs-base:" in s:
        print("  跳过（app.css 已有尺度令牌）")
    else:
        anchor = "a { color: var(--accent); text-decoration: none; }"
        blk = ("\n/* ---- P2 尺度令牌层（2026-09-23）：字号 / 圆角 / 间距与主题无关，集中在此 ---- */\n"
               ":root {\n" + SCALE + "\n}\n")
        if anchor in s:
            s = s.replace(anchor, anchor + "\n" + blk, 1)
        else:
            s += "\n" + blk
        io.open(APP, "w", encoding="utf-8").write(s)
        print("  已写入尺度令牌块: app.css")


def main():
    bad = validate()
    if "--write" in sys.argv:
        if bad:
            print("\n存在未达标组合，拒绝写入。请先调整取值。")
            return 1
        print()
        print("=" * 74)
        print("写入")
        print("=" * 74)
        write()
    return 0 if not bad else 1


if __name__ == "__main__":
    sys.exit(main())
