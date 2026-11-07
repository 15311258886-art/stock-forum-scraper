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
  python tools/split_server.py --domains a,b,c [--dry-run] [--run-gate]
  python tools/split_server.py --verify            # 仅跑静态关门检查，不拆分

关门机制（2026-11-07 拆分教训固化）：
  - 抽取落盘后**自动**跑静态关门 verify_handlers()：py_compile 全扫 + 引用/import
    一致性扫描（递归收集 server 模块级绑定，含顶层 try/except 内可选别名与 global
    声明的缓存字典），复现当时漏掉的 NameError 类，立即报错而非等到 pytest。
  - --run-gate 额外跑运行时关门：真实 import viewer.server（捕捉循环导入/
    未定义全局）+ pytest -q 全量回归。
"""
from __future__ import annotations

import argparse
import ast
import builtins
import keyword
import os
import py_compile
import sys
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


BUILTIN = set(dir(builtins)) | {"True", "False", "None"}


def _collect_module_level_names(node, names):
    """递归收集**模块级**绑定名。

    穿透模块作用域内执行的 Try/If/With/For/While/ExceptHandler（这些仍属
    server 模块命名空间，如顶层 `try/except` 里的可选模块别名
    `import cacheutil as cu`）；**不进入**函数/类的局部作用域（其内赋值属局部
    变量），但会收集函数/类内部的 global/nonlocal 声明（它们指向的就是模块级名）。

    这是 2026-11-07 拆分的关门教训：原实现只扫 tree.body 直接子节点，导致顶层
    try 内的可选别名与 global 声明的缓存字典未被识别为 server 模块级符号，搬运后
    运行时 NameError。递归收集后，未来拆分会自动把这些符号带进 handler 的 import。
    """
    for child in ast.iter_child_nodes(node):
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(child.name)
            # 收集函数/类内部的 global/nonlocal 声明（它们引用的就是模块级名）
            for n in ast.walk(child):
                if isinstance(n, (ast.Global, ast.Nonlocal)):
                    names.update(n.names)
            continue
        if isinstance(child, (ast.Assign, ast.AnnAssign)):
            targets = child.targets if isinstance(child, ast.Assign) else [child.target]
            for t in targets:
                for nm in ast.walk(t):
                    if isinstance(nm, ast.Name):
                        names.add(nm.id)
        elif isinstance(child, (ast.Import, ast.ImportFrom)):
            for a in child.names:
                names.add(a.asname or a.name.split(".")[0])
        elif isinstance(child, (ast.Global, ast.Nonlocal)):
            names.update(child.names)
        elif isinstance(child, (ast.Try, ast.If, ast.With, ast.AsyncWith,
                                ast.For, ast.AsyncFor, ast.While, ast.ExceptHandler)):
            _collect_module_level_names(child, names)


def collect_top(src: str):
    tree = ast.parse(src)
    TOP = set()
    _collect_module_level_names(tree, TOP)
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


def verify_handlers() -> bool:
    """拆分后**静态关门**检查（不依赖运行环境，纯 AST）：

    1) 所有 handler + server.py 通过 py_compile（捕捉 global 关键字等语法炸弹）；
    2) 每个 handler 引用的 server 模块级符号**必须**已 `from viewer.server import`。

    返回 True 表示通过。任何一项失败即返回 False（应由调用方判失败退出）。
    这正是 2026-11-07 拆分中暴露、并在全量 pytest 关门才发现的遗漏类：
    被搬走的 api_* 引用了 server 顶层 try/except 内的可选模块别名或 global 声明的
    缓存字典，却未在 handler 内 import → 运行时 NameError。固化此检查后，未来拆分
    若在抽取时漏带符号，工具会在落盘后立即报错，而非等到 pytest。
    """
    ok = True
    # 1) 语法编译检查
    compile_err = []
    targets = [SERVER] + sorted(HANDLERS.glob("*.py"))
    for f in targets:
        try:
            py_compile.compile(str(f), doraise=True)
        except py_compile.PyCompileError:
            compile_err.append(f.name)
    if compile_err:
        ok = False
        print("[verify] !! 语法编译失败：", compile_err)

    # 2) 引用-import 一致性检查
    _, TOP = collect_top(SERVER.read_text(encoding="utf-8"))
    problems = []
    for f in sorted(HANDLERS.glob("*.py")):
        if f.name == "__init__.py":
            continue
        src = f.read_text(encoding="utf-8")
        htree = ast.parse(src)
        modbind = set()
        imports = set()
        for n in htree.body:
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                modbind.add(n.name)
            elif isinstance(n, ast.Assign):
                for t in n.targets:
                    for nm in ast.walk(t):
                        if isinstance(nm, ast.Name):
                            modbind.add(nm.id)
            elif isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name):
                modbind.add(n.target.id)
            elif isinstance(n, ast.Import):
                for a in n.names:
                    imports.add(a.asname or a.name.split(".")[0])
            elif isinstance(n, ast.ImportFrom):
                for a in n.names:
                    imports.add(a.asname or a.name)
        loads = {n.id for n in ast.walk(htree)
                 if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)}
        missing = sorted(x for x in loads
                         if x not in modbind and x not in imports
                         and x not in BUILTIN and x in TOP)
        if missing:
            problems.append((f.name, missing))
    if problems:
        ok = False
        print("[verify] !! 以下 handler 引用了 server 模块级符号却未 import：")
        for name, miss in problems:
            print(f"    {name}: {miss}")
    if ok:
        print("[verify] OK：全部 handler 语法通过，且引用的 server 模块级符号均已 import")
    return ok


def run_gate() -> bool:
    """运行时**最终**关门（需完整运行环境：yaml/bs4/requests/...）：

    - 真实 `import viewer.server`：捕捉循环导入 / 未定义全局（延迟导入模式唯一可能踩的坑）；
    - `pytest -q`：全量回归，应仅在基线已知的 flaky/环境失败上非零，不应出现新失败。

    返回 True 表示 import 闸门通过（pytest 非零仅提示，不硬判，因环境型 flaky 与拆分无关）。
    """
    import subprocess
    ok = True
    print("[gate] import viewer.server ...")
    r = subprocess.run([sys.executable, "-c", "import viewer.server"], cwd=str(ROOT))
    if r.returncode != 0:
        ok = False
        print("[gate] !! import viewer.server 失败（见上方回溯）")
    else:
        print("[gate] import viewer.server OK")
    print("[gate] pytest -q（需依赖齐全；flaky/环境失败需人工比对基线）...")
    subprocess.run([sys.executable, "-m", "pytest", "-q"], cwd=str(ROOT))
    return ok


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--domains", help="逗号分隔的域前缀（--verify 时不需要）")
    ap.add_argument("--dry-run", action="store_true", help="只打印计划，不落盘")
    ap.add_argument("--verify", action="store_true",
                    help="仅对当前 handlers 跑静态关门检查（语法+引用/import 一致性），不拆分")
    ap.add_argument("--run-gate", action="store_true",
                    help="抽取落盘后额外跑运行时关门：import viewer.server + pytest -q")
    args = ap.parse_args()

    if args.verify:
        sys.exit(0 if verify_handlers() else 1)

    if not args.domains:
        ap.error("--domains 为必填（除非使用 --verify）")
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

    # ── 静态关门：复现 2026-11-07 遗漏类 bug（语法+引用/import 一致性）──
    if not verify_handlers():
        print("\n[FAIL] 静态关门检查未通过，拆分结果存在隐患，请修复后再提交。")
        sys.exit(1)

    # ── 可选运行时关门 ──
    if args.run_gate:
        if not run_gate():
            sys.exit(1)


if __name__ == "__main__":
    main()
