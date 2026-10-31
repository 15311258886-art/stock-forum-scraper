"""P2-3 硬编码色收口 · 安全批量替换器（与 audit_p2_tokens.py 配套）。

仅替换「裸 hex 且不在 var(--token, #hex) 回退值里」的颜色，确保：
  ① 回退值（防御写法）原样保留，不出现 var(--x, var(--x)) 嵌套
  ② 裸 hex 与浅色主题 light-tech.css 令牌值完全一致 -> 浅色零变化、暗色自动修复
上下文相关 / 无现成令牌的色（#fff、#b08900、#22c55e…）本轮不动。

用法：python tools/apply_p23_colors.py
"""
import io
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
APP = os.path.join(ROOT, "viewer", "static", "app.css")

# 裸 hex(小写，不含 #) -> 主题令牌名。每个都已在 light-tech.css 中校验 == 该令牌浅色值。
MAP = {
    "b45309": "warn",
    "fef3c7": "warn-bg",
    "ef4444": "err-strong",
    "0e7490": "accent",
    "b91c1c": "err",
    "15803d": "ok-fg",
    "dbeafe": "c2-bg",
    "dcfce7": "ok-bg",
    "1d4ed8": "c2-fg",
    "92400e": "c1-fg",
    "fee2e2": "err-bg",
    "16a34a": "ok",
    "6b7280": "muted",
    "f59e0b": "warn-strong",
    "e0f2fe": "c5-bg",
    "f1f5f9": "c6-bg",
    "475569": "c6-fg",
    "7c3aed": "c-violet-line",
    "fae8ff": "c4-bg",
    "a21caf": "c4-fg",
    "0369a1": "c5-fg",
    "ede9fe": "c-violet-bg",
    "6d28d9": "c-violet-fg",
    "9ca3af": "neutral-line",
}

RE_VARFB = re.compile(r"var\(--[a-z0-9-]+,\s*$")  # 回退值判定：hex 前 48 字内含「var(--token,」
PAT = re.compile(r"#(" + "|".join(MAP.keys()) + r")\b", re.I)


def main() -> int:
    src = io.open(APP, encoding="utf-8").read()
    before = src
    repl_count = 0
    skip_count = 0
    per = {}

    def repl(m):
        nonlocal repl_count, skip_count
        pre = src[max(0, m.start() - 48): m.start()]
        if RE_VARFB.search(pre):
            skip_count += 1
            return m.group(0)  # 回退值：原样保留
        hexv = m.group(1).lower()
        tok = MAP[hexv]
        per[tok] = per.get(tok, 0) + 1
        repl_count += 1
        return "var(--" + tok + ")"

    src = PAT.sub(repl, src)
    io.open(APP, "w", encoding="utf-8").write(src)

    print("=" * 72)
    print("P2-3 安全替换（仅真·硬编码，回退值保留）")
    print("=" * 72)
    for tok, n in sorted(per.items(), key=lambda x: -x[1]):
        print(f"  --{tok:<14} 替换 {n} 处")
    ob, cb = src.count("{"), src.count("}")
    print("-" * 72)
    print(f"  真·替换 {repl_count} 处；回退值跳过 {skip_count} 处")
    print(f"  花括号 {{ = {ob}  }} = {cb}  {'OK' if ob == cb else 'MISMATCH!'}")
    print(f"  文件大小 {len(before)} -> {len(src)} 字节")
    return 0


if __name__ == "__main__":
    sys.exit(main())
