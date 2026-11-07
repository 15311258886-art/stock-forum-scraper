#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""server.py 按域抽取工具（一次性重构辅助，可保留为拆分文档工具）。

把指定域的 api_* 函数**物理搬运**到 viewer/handlers/<domain>.py：
  - 函数体源码原样截取（零逻辑改动）；
  - handler 顶部 `from viewer.server import (依赖符号)` 把 server 模块级符号
    重定向进本地命名空间（裸名解析等价）；标准库本地导入；
  - 同域内 api 互相依赖不重导入（本地可见，避免循环导入）；
  - server.py 原位置删除这些函数，并在 `if __name__` 前**重建完整的、
    按依赖拓扑序排序的 re-export 块**（被依赖域在前），保持命名空间兼容 →
    routes.py 与既有测试（S.api_*）无需改动，且彻底消除跨域 api 的循环/顺序导入。

用法：
  python tools/split_server.py --domains a,b,c [--dry-run]
"""
from __future__ import annotations

import argparse
import ast
import keyword
import os
from pathlib import Path


def safe_dom(d: str) -> str:
    """域前缀若为 Python 关键字（如 global）则加后缀，避免生成非法模块名/import。"""
    return d + "_domain" if keyword.iskeyword(d) else d

ROOT = Path(__file__).resolve().parent.parent
SERVER = ROOT / "viewer" / "server.py"
HANDLERS = ROOT / "viewer" / "handlers"
REEXPORT_MARKER = "# ── 2026-11-07 拆分：以下 api_* 已迁至 viewer/handlers/"

STDLIB = {
    "json", "re", "logging", "threading", "time", "datetime", "os", "sqlite3", "math",
    "sys", "urllib", "collections", "functools", "itertools", "random", "hashlib",
    "base64", "pathlib", "copy", "enum", "typing", "contextlib", "io", "string", "bisect",
    "heapq", "operator", "warnings", "traceback", "inspect", "signal", "socket", "gzip",
    "hmac", "ctypes", "subprocess", "shutil", "tempfile", "glob", "fnmatch",
    "struct", "array", "weakref", "decimal", "fractions", "statistics", "csv",
}


def collect_top(src: str):
    tree = ast.parse(src)
    TOP = set()
    for n in tree.body:
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            TOP.add(n.name)
        elif isinstance(n, ast.Assign):
            for t in n.targets:
                for nm in ast.walk(t):
                    if isinstance(nm, ast.Name):
                        TOP.add(nm.id)
        elif isinstance(n, (ast.Import, ast.ImportFrom)):
            for a in n.names:
                TOP.add(a.asname or a.name.split(".")[0])
    return tree, TOP


def local_names(fn) -> set:
    s = set()
    for node in ast.walk(fn):
        if isinstance(node, ast.arg):
            s.add(node.arg)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            s.add(node.name)
        elif isinstance(node, ast.Assign):
            for t in node.targets:
                for nm in ast.walk(t):
                    if isinstance(nm, ast.Name):
                        s.add(nm.id)
        elif isinstance(node, (ast.For, ast.AsyncFor)):
            for nm in ast.walk(node.target):
                if isinstance(nm, ast.Name):
                    s.add(nm.id)
        elif isinstance(node, (ast.With, ast.AsyncWith)):
            for it in node.items:
                if it.optional_vars:
                    for nm in ast.walk(it.optional_vars):
                        if isinstance(nm, ast.Name):
                            s.add(nm.id)
        elif isinstance(node, ast.Import):
            for a in node.names:
                s.add(a.asname or a.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            for a in node.names:
                s.add(a.asname or a.name)
        elif isinstance(node, ast.ExceptHandler) and node.name:
            s.add(node.name)
        elif isinstance(node, ast.comprehension):
            for nm in ast.walk(node.target):
                if isinstance(nm, ast.Name):
                    s.add(nm.id)
    return s


def api_deps(api_node, TOP: set) -> set:
    loc = local_names(api_node)
    deps = set()
    for node in ast.walk(api_node):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            if node.id in TOP and node.id not in loc:
                deps.add(node.id)
    return deps


def extract_range(node):
    start = node.lineno
    if node.decorator_list:
        start = min(d.lineno for d in node.decorator_list)
    return start, node.end_lineno


def build_handler(dom: str, nodes, TOP: set) -> tuple[str, list[str], list[str], str]:
    """返回 (stem, server_deps, stdlib_deps, body_text)。"""
    stdlib_deps: set = set()
    server_deps: set = set()
    own_names = {nd.name for nd in nodes}
    blocks = []
    for nd in nodes:
        d = api_deps(nd, TOP)
        for x in d:
            if x in own_names:
                continue  # 同域本地定义，无需从 server 重导入（避免循环/冗余）
            if x in STDLIB:
                stdlib_deps.add(x)
            else:
                server_deps.add(x)
        s, e = extract_range(nd)
        blocks.append((s, e))
    src_lines = SERVER.read_text(encoding="utf-8").splitlines(keepends=True)
    body = ""
    for s, e in blocks:
        body += "".join(src_lines[s - 1:e])
    return dom, sorted(server_deps), sorted(stdlib_deps), body


def scan_handler_cross_deps(handler_path: Path, handler_domains: set) -> set:
    """返回该 handler 依赖的其他 handler 域集合（跨域 api_* 依赖）。"""
    src = handler_path.read_text(encoding="utf-8")
    tree = ast.parse(src)
    cross = set()
    for n in tree.body:
        if isinstance(n, ast.ImportFrom) and n.module == "viewer.server":
            for a in n.names:
                if a.name.startswith("api_"):
                    d = safe_dom(a.name[4:].split("_")[0])
                    if d != handler_path.stem and d in handler_domains:
                        cross.add(d)
    return cross


def topo_order(handler_domains: set) -> list[str]:
    dom_deps = {d: scan_handler_cross_deps(HANDLERS / f"{d}.py", handler_domains)
                for d in handler_domains}
    visited: set = set()
    order: list = []

    def visit(d, stack):
        if d in visited:
            return
        if d in stack:
            return  # 环：忽略该边，避免死循环（单向依赖下不会出现）
        stack.add(d)
        for dep in sorted(dom_deps.get(d, set())):
            visit(dep, stack)
        stack.discard(d)
        visited.add(d)
        order.append(d)

    for d in sorted(handler_domains):
        visit(d, set())
    return order


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--domains", required=True, help="逗号分隔的域前缀")
    ap.add_argument("--dry-run", action="store_true", help="只打印计划，不落盘")
    args = ap.parse_args()
    domains = [d.strip() for d in args.domains.split(",") if d.strip()]

    src = SERVER.read_text(encoding="utf-8")
    tree, TOP = collect_top(src)
    lines = src.splitlines(keepends=True)

    apis = {}
    for n in tree.body:
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name.startswith("api_"):
            dom = safe_dom(n.name[4:].split("_")[0])
            apis.setdefault(dom, []).append(n)

    plan = {}
    for dom in domains:
        if dom not in apis:
            print(f"!! 警告：域 {dom} 在 server.py 无 api_*，跳过")
            continue
        plan[dom] = apis[dom]

    if not plan:
        print("无可用域，退出")
        return

    HANDLERS.mkdir(exist_ok=True)
    handler_files: list[tuple[Path, str]] = []
    reexports: dict[str, list[str]] = {}
    removed_ranges: list[tuple[int, int]] = []

    for dom, nodes in plan.items():
        stem, server_deps, stdlib_deps, body = build_handler(dom, nodes, TOP)
        stdlib_imports = ("import " + ", ".join(stdlib_deps) + "\n") if stdlib_deps else ""
        if server_deps:
            server_imports_block = (
                "from viewer.server import (  # noqa: E402,F401\n"
                + "".join(f"    {x},\n" for x in server_deps)
                + ")\n"
            )
        else:
            server_imports_block = ""
        hdr = (
            f'# -*- coding: utf-8 -*-\n'
            f'"""{dom} 域 API 处理函数（从 viewer/server.py 物理抽取，2026-11-07 拆分重构）。\n\n'
            f"抽取规则：函数体逻辑零改动；依赖的 server 模块级符号经 `from viewer.server import (...)`\n"
            f"重定向进本地命名空间；标准库本地导入。server.py 末尾 re-export 保持命名空间兼容，\n"
            f"routes.py 与既有测试（S.api_*）无需改动。\n"
            f'"""\n'
            f"from __future__ import annotations\n\n"
            f"{stdlib_imports}"
            f"{server_imports_block}\n"
        )
        out = HANDLERS / f"{stem}.py"
        handler_files.append((out, hdr + body))
        reexports[dom] = [nd.name for nd in nodes]
        for nd in nodes:
            s, e = extract_range(nd)
            removed_ranges.append((s, e))
        print(f"[plan] {out.name}")
        print(f"        api: {[nd.name for nd in nodes]}")
        print(f"        server_deps: {server_deps}")
        print(f"        stdlib_deps: {stdlib_deps}")

    if args.dry_run:
        print("\n[DRY-RUN] 未落盘。确认依赖无误后去掉 --dry-run 执行。")
        return

    # ── 落盘 handler ──
    for out, content in handler_files:
        out.write_text(content, encoding="utf-8")
        print(f"[write] {out}")

    # ── 改 server.py：删除目标函数 ──
    removed_ranges.sort(reverse=True)
    new_lines = lines[:]
    for s, e in removed_ranges:
        del new_lines[s - 1:e]
    # 清理删除后产生的大段连续空行（保留最多一个空行分隔）
    cleaned = []
    blank_run = 0
    for ln in new_lines:
        if ln.strip() == "":
            blank_run += 1
            if blank_run <= 1:
                cleaned.append(ln)
        else:
            blank_run = 0
            cleaned.append(ln)
    new_lines = cleaned

    # ── 删除旧 re-export 块（若存在）──
    no_reexport = []
    skip = False
    for ln in new_lines:
        if skip:
            if ln.strip().startswith("if __name__"):
                skip = False
                no_reexport.append(ln)
            continue
        if REEXPORT_MARKER in ln:
            skip = True
            continue
        no_reexport.append(ln)
    new_lines = no_reexport

    # ── 重建完整 re-export 块（所有 handlers 域，按依赖拓扑序）──
    existing = {f[:-3] for f in os.listdir(HANDLERS)
                if f.endswith(".py") and f != "__init__.py"}
    ordered = topo_order(existing)
    reexport_text = (
        "\n" + REEXPORT_MARKER + "，re-export 保持命名空间兼容 ──\n"
    )
    for dom in ordered:
        # 该 handler 定义的 api 名
        hsrc = (HANDLERS / f"{dom}.py").read_text(encoding="utf-8")
        htree = ast.parse(hsrc)
        names = [n.name for n in htree.body
                 if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                 and n.name.startswith("api_")]
        reexport_text += (
            f"from viewer.handlers.{dom} import ({', '.join(names)})"
            f"  # 2026-11-07 拆分 re-export\n"
        )
    insert_at = len(new_lines)
    for i, l in enumerate(new_lines):
        if l.strip().startswith("if __name__"):
            insert_at = i
            break
    new_lines.insert(insert_at, reexport_text + "\n")
    SERVER.write_text("".join(new_lines), encoding="utf-8")
    print(
        f"\n[done] server.py 已更新：删除 {len(removed_ranges)} 个函数，"
        f"重建 re-export 块（{len(ordered)} 域，拓扑序）"
    )


if __name__ == "__main__":
    main()
