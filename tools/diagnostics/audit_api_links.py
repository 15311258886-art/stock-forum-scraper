#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""API 链路静态差集审计（2026-10-15 新增）

用途
----
一次性找出「前端调用了、后端却未注册」的接口断链。这类 bug 在浏览器里只表现为
`HTTP 404 {"error": "not found"}`，而面板本身靠 GET 有数据，所以肉眼极难发现——
用户看到的是「点按钮没反应 + 顶部一条报错」。

背景（为什么值得写个脚本）
--------------------------
同一个坑已经复发两次：
  · /api/prediction/run —— 2026-10-06 修（见 server.py _do_POST 注释）
  · /api/pillars/run    —— 2026-10-15 修（本轮）
两次都是「生成/写入接口只注册在 _do_GET，前端却用 POST 调用」。
本脚本把这类回归变成一条命令，避免第三次。

做法
----
  1) 扫 viewer/static/*.js 里所有 api("...") / fetch("...") 调用，提取路径 + HTTP 方法
     （跨行、字符串拼接、模板串都处理；无法静态判定的标注 dynamic）
  2) 扫 viewer/server.py 的 _do_GET / _do_POST 两段，提取 route == "..." / route in (...)
     / route.startswith(...) 注册表
  3) 对撞输出差集：前端用了某方法、后端同方法没注册 → 高危

用法
----
  python tools/audit_api_links.py            # 默认只扫 app.js（主前端）
  python tools/audit_api_links.py --all      # 扫 static 下所有 js
退出码：0 = 无高危；1 = 存在高危断链
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "viewer" / "static"
SERVER = ROOT / "viewer" / "server.py"

CALL_RE = re.compile(r"\b(api|fetch)\s*\(")
# 片段开头的第一个字符串字面量（单/双/反引号）
HEAD_STR_RE = re.compile(r"""^\s*(["'`])((?:[^"'`\\]|\\.)*?)\1""")
POST_RE = re.compile(r"""method\s*:\s*["']POST["']""", re.I)
API_EQ_RE = re.compile(r"""route\s*==\s*["'](/api/[^"']*)["']""")
API_IN_RE = re.compile(r"""route\s+in\s*\(([^)]*)\)""", re.S)
API_SW_RE = re.compile(r"""route\.startswith\(\s*["']([^"']+)["']""")
STR_IN_TUPLE_RE = re.compile(r"""["']([^"']+)["']""")


def _slice_paren(text: str, open_idx: int, limit: int = 900) -> str:
    """从 open_idx 处的 '(' 开始做括号配对，返回调用片段（最多 limit 字符）。"""
    depth = 0
    i = open_idx
    n = min(len(text), open_idx + limit)
    while i < n:
        ch = text[i]
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return text[open_idx + 1:i]
        i += 1
    return text[open_idx + 1:n]


def scan_frontend(js_files: list[Path]) -> list[tuple[str, str, str]]:
    """返回 [(file, path, method)]，method ∈ {GET, POST, DYNAMIC}。"""
    found: list[tuple[str, str, str]] = []
    for f in js_files:
        try:
            text = f.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for m in CALL_RE.finditer(text):
            frag = _slice_paren(text, m.end() - 1)
            hm = HEAD_STR_RE.match(frag)
            if not hm:
                # api(path_var, ...) —— 路径是变量，静态判不了
                found.append((f.name, "<dynamic:%s>" % frag.split(",")[0].strip()[:40], "DYNAMIC"))
                continue
            raw = hm.group(2)
            path = raw.split("?")[0].split("#")[0].strip()
            if not path.startswith("/api"):
                continue
            method = "POST" if POST_RE.search(frag) else "GET"
            # 拼接式："/api/stock?code=" + code → 已 strip；"/api/rate/" + x → 前缀式
            if "+" in raw.split("?")[0]:
                path = raw[: raw.find("+")].split("?")[0]
                method = method + "(prefix)"
            found.append((f.name, path, method))
    return found


def scan_backend() -> tuple[set[str], set[str], list[str]]:
    """返回 (GET 注册集, POST 注册集, startswith 前缀列表)。"""
    lines = SERVER.read_text(encoding="utf-8", errors="replace").splitlines()
    get_lo = get_hi = post_lo = post_hi = None
    for i, ln in enumerate(lines, 1):
        if ln.strip().startswith("def _do_GET("):
            get_lo = i
        elif ln.strip().startswith("def do_POST("):
            get_hi = i
            post_lo = i
        elif ln.strip().startswith("def _do_POST("):
            post_lo = i
        elif ln.strip().startswith("def log_message(") and post_hi is None and post_lo:
            post_hi = i
    if not (get_lo and get_hi and post_lo and post_hi):
        raise SystemExit("server.py 路由分段定位失败（_do_GET/_do_POST/log_message 未找到）")

    src = "\n".join(lines)
    # 行号偏移：用字符偏移把匹配映射回行号不可靠，改为逐段解析
    def regs(lo: int, hi: int) -> set[str]:
        chunk = "\n".join(lines[lo - 1: hi - 1])
        out: set[str] = set()
        out.update(API_EQ_RE.findall(chunk))
        for tup in API_IN_RE.findall(chunk):
            for s in STR_IN_TUPLE_RE.findall(tup):
                if s.startswith("/api"):
                    out.add(s)
        return out

    starts: list[str] = []
    for seg in (lines[get_lo:get_hi], lines[post_lo:post_hi]):
        starts.extend(API_SW_RE.findall("\n".join(seg)))
    return regs(get_lo, get_hi), regs(post_lo, post_hi), starts


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--all", action="store_true", help="扫描 static 下所有 js（默认仅 app.js）")
    args = ap.parse_args()

    files = sorted(STATIC.glob("*.js")) if args.all else [STATIC / "app.js"]
    files = [f for f in files if f.exists()]
    calls = scan_frontend(files)
    gets, posts, starts = scan_backend()

    high: list[str] = []
    warn: list[str] = []
    ok = 0
    seen: set[tuple[str, str]] = set()
    for fname, path, method in calls:
        key = (path, method)
        if key in seen:
            continue
        seen.add(key)
        if method == "DYNAMIC":
            warn.append(f"  [动态路径] {fname}: {path} —— 需人工确认")
            continue
        base = method.replace("(prefix)", "")
        pool = posts if base == "POST" else gets
        hit = path in pool or any(path.startswith(p) for p in starts)
        if method.endswith("(prefix)"):
            hit = any(reg.startswith(path) for reg in pool) or hit
        if hit:
            ok += 1
        else:
            high.append(f"  ✗ {base:4s} {path}   ← 由 {fname} 调用，后端 {base} 段未注册")

    print("=" * 68)
    print(f"前端调用点：{len(calls)} 条（去重后 {len(seen)} 条唯一 路径×方法）")
    print(f"后端注册：GET {len(gets)} 个 / POST {len(posts)} 个 / startswith 前缀 {starts}")
    print("=" * 68)
    if high:
        print(f"\n【高危】前端调了、后端未注册（浏览器表现为 404 not found）：{len(high)} 条")
        print("\n".join(high))
    if warn:
        print(f"\n【需人工确认】{len(warn)} 条")
        print("\n".join(warn))
    print(f"\n【通过】{ok} 条唯一调用在后端有对应注册")
    print("\n结论：" + ("发现高危断链，见上" if high else "无高危断链 ✅"))
    return 1 if high else 0


if __name__ == "__main__":
    sys.exit(main())
