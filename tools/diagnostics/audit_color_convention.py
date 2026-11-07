# -*- coding: utf-8 -*-
"""配色约定门禁（规格书 A4-2 / A4-3）。

全站约定：**涨=红、跌=绿**（中国市场惯例），且不得随主题反转。
硬编码色应走语义变量（--bull / --bear / --up / --down 等），以便四套主题一致。

本脚本做两件事：
  1. 从 app.css 抽出"涨/跌"语义变量的实际色值，判定其色相是否符合红涨绿跌；
  2. 扫描 CSS 里是否存在**反色**写法（把 .up/.bull 系绑到绿色、.down/.bear 系绑到红色）。

退出码：0=通过，2=发现违规（供 CI / pytest 引用）。
用法：
    .venv\\Scripts\\python.exe tools/audit_color_convention.py
"""
from __future__ import annotations

import io
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATIC = os.path.join(ROOT, "viewer", "static")
# 语义变量定义在 themes/*.css；类规则散在 app.css（也顺带扫主题文件）
CSS_TARGETS = [os.path.join(STATIC, "app.css")] + sorted(
    os.path.join(STATIC, "themes", f)
    for f in (os.listdir(os.path.join(STATIC, "themes"))
              if os.path.isdir(os.path.join(STATIC, "themes")) else [])
    if f.endswith(".css")
)

# 涨/跌语义的"变量名关键词"与"类名关键词"（大小写不敏感）
UP_KEYS = ("bull", "up", "raise", "rise", "zhang")
DOWN_KEYS = ("bear", "down", "fall", "die", "green")


def _read(path: str) -> str:
    return io.open(path, encoding="utf-8").read()


def _rgb(hexstr: str):
    """'#c81e1e' / '#abc' → (r,g,b)；非法返回 None。"""
    s = hexstr.strip().lstrip("#")
    if len(s) == 3:
        s = "".join(c * 2 for c in s)
    if len(s) != 6:
        return None
    try:
        return tuple(int(s[i:i + 2], 16) for i in (0, 2, 4))
    except ValueError:
        return None


def _is_reddish(rgb, strict: bool = True) -> bool:
    """strict=True 用于前景色（需明显偏红）；False 用于底色/浅色调（只判色相方向）。"""
    r, g, b = rgb
    if strict:
        return r > g + 25 and r > b + 25
    return r > g and r > b


def _is_greenish(rgb, strict: bool = True) -> bool:
    r, g, b = rgb
    if strict:
        return g > r + 15 and g >= b
    return g > r and g >= b


def _is_bg_var(name: str) -> bool:
    """底色/浅色调类变量（--bull-bg / --bear-soft / --up-bg …）——只判色相方向，不判强度。"""
    low = name.lower()
    return any(k in low for k in ("-bg", "_bg", "tint", "-soft", "_soft", "bg-"))


def _extract_var_defs(css: str) -> dict:
    """抽 `--name: #hex` 定义（多主题下同名会覆盖，这里保留全部取值列表）。"""
    out: dict = {}
    for m in re.finditer(r"(--[a-zA-Z0-9_-]+)\s*:\s*(#[0-9a-fA-F]{3,8})", css):
        out.setdefault(m.group(1).lower(), []).append(m.group(2))
    return out


def _extract_class_defs(css: str):
    """抽 `.xxx { ... color: #hex ... }` 里 类名关键词 → 色值。"""
    hits = []
    for m in re.finditer(r"\.([a-zA-Z0-9_-]+)\s*\{([^}]*)\}", css):
        cls, body = m.group(1), m.group(2)
        for cm in re.finditer(r"(?:^|;|\s)(color|border-left-color|background(?:-color)?)\s*:\s*(#[0-9a-fA-F]{3,8})", body):
            hits.append((cls, cm.group(1), cm.group(2)))
    return hits


def main() -> int:
    problems = []
    var_count = 0
    scanned = []

    for css_path in CSS_TARGETS:
        if not os.path.exists(css_path):
            continue
        scanned.append(os.path.relpath(css_path, ROOT))
        css = _read(css_path)

        var_defs = _extract_var_defs(css)
        var_count += len(var_defs)
        for name, vals in sorted(var_defs.items()):
            low = name.lower()
            is_up = any(k in low for k in UP_KEYS)
            is_down = any(k in low for k in DOWN_KEYS)
            if not (is_up or is_down):
                continue
            for v in vals:
                rgb = _rgb(v)
                if not rgb:
                    continue
                strict = not _is_bg_var(name)
                if is_up and not _is_reddish(rgb, strict):
                    problems.append("%s：变量 %s = %s 不是红色系（涨应为红）"
                                    % (os.path.basename(css_path), name, v))
                if is_down and not _is_greenish(rgb, strict):
                    problems.append("%s：变量 %s = %s 不是绿色系（跌应为绿）"
                                    % (os.path.basename(css_path), name, v))

        for cls, prop, v in _extract_class_defs(css):
            low = cls.lower()
            rgb = _rgb(v)
            if not rgb:
                continue
            is_up = any(k in low for k in ("bull", "up", "raise", "rise")) and not low.startswith("update")
            is_down = any(low.startswith(k) or ("-" + k) in low for k in ("bear", "down", "fall"))
            if is_up and _is_greenish(rgb) and not _is_reddish(rgb):
                problems.append("%s：类 .%s 的 %s=%s 呈绿色（涨应为红，疑似反色）"
                                % (os.path.basename(css_path), cls, prop, v))
            if is_down and _is_reddish(rgb) and not _is_greenish(rgb):
                problems.append("%s：类 .%s 的 %s=%s 呈红色（跌应为绿，疑似反色）"
                                % (os.path.basename(css_path), cls, prop, v))

    # 去重
    seen, uniq = set(), []
    for p in problems:
        if p not in seen:
            seen.add(p)
            uniq.append(p)

    print("配色约定门禁（红涨绿跌）—— 扫描 %d 个样式文件" % len(scanned))
    for s in scanned:
        print("  · " + s)
    print("  语义变量定义数：%d" % var_count)
    if not uniq:
        print("  PASS：未发现涨跌反色")
        return 0
    for p in uniq:
        print("  FAIL：" + p)
    print("\n共 %d 处可疑，请核对（若确为中性用途请改用非 up/down 语义的类名或变量）。" % len(uniq))
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
