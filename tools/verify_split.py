#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""静态验证拆分正确性（不依赖运行时第三方包）：
1) handler 内 api_* 源码 与 拆分前备份逐字符一致（证明零改）；
2) handler 的 `from viewer.server import(...)` 符号全部 ∈ server 顶层 TOP（避免 ImportError）；
3) server 末尾 re-export 的 api 名 全部在对应 handler 中定义；
4) 被抽 api 已从 server 删除（无残留 def）；
5) 报告体积变化。
验证后删除。"""
import ast
from pathlib import Path

import subprocess
ROOT = Path(r"F:/AI Agent部署知识库/2026-09-22-11-49-42/stock-forum-scraper")
BASE_COMMIT = "c3725a0"  # 拆分前基线
SERVER = ROOT / "viewer" / "server.py"
HANDLERS = ROOT / "viewer" / "handlers"

def _git_show(rel: str) -> str:
    return subprocess.check_output(
        ["git", "show", f"{BASE_COMMIT}:{rel}"], cwd=str(ROOT)
    ).decode("utf-8")

def top_symbols(src):
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

def api_src_map(src):
    tree = ast.parse(src)
    out = {}
    lines = src.splitlines(keepends=True)
    for n in tree.body:
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name.startswith("api_"):
            s = n.lineno
            if n.decorator_list:
                s = min(d.lineno for d in n.decorator_list)
            out[n.name] = "".join(lines[s - 1:n.end_lineno]).strip()
    return out

# 1) 基线(拆分前)里的 api 源码
bak_src = _git_show("viewer/server.py")
bak_apis = api_src_map(bak_src)

# 2) 当前 server
srv_src = SERVER.read_text(encoding="utf-8")
stree, TOP = top_symbols(srv_src)
srv_lines = srv_src.splitlines()
server_left_apis = {n.name for n in stree.body
                    if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name.startswith("api_")}

# 3) 扫描 server re-export
reexport = {}  # handler_mod -> [apis]
for n in stree.body:
    if isinstance(n, ast.ImportFrom) and isinstance(n.module, str) and n.module.startswith("viewer.handlers."):
        mod = n.module.split(".")[-1]
        reexport.setdefault(mod, []).extend(a.name for a in n.names)

problems = []
# 4) 逐 handler 校验
handler_total_lines = 0
moved_ok = 0
for hf in sorted(HANDLERS.glob("*.py")):
    hsrc = hf.read_text(encoding="utf-8")
    htree, _ = top_symbols(hsrc)
    handler_apis = {n.name for n in htree.body
                    if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name.startswith("api_")}
    # handler 里来自 server 的 import
    srv_imports = set()
    for n in htree.body:
        if isinstance(n, ast.ImportFrom) and n.module == "viewer.server":
            srv_imports.update(a.name for a in n.names)
    bad = [x for x in srv_imports if x not in TOP]
    if bad:
        problems.append(f"{hf.name}: server import 缺失符号 {bad}")
    # re-export 一致性
    mod = hf.stem
    if mod in reexport:
        for a in reexport[mod]:
            if a not in handler_apis:
                problems.append(f"{hf.name}: re-export {a} 未在 handler 定义")
            if a in server_left_apis:
                problems.append(f"{hf.name}: re-export {a} 仍残留 server def")
    # 逐字符比对零改
    h_apis_src = api_src_map(hsrc)
    for name, hsrc_body in h_apis_src.items():
        if name in bak_apis:
            if hsrc_body == bak_apis[name]:
                moved_ok += 1
            else:
                problems.append(f"{hf.name}: {name} 源码与备份不一致（非零改）")
        else:
            problems.append(f"{hf.name}: {name} 在备份中找不到")
    handler_total_lines += len(hsrc.splitlines())

print("=== 拆分静态验证 ===")
print(f"server.py 行数: {len(srv_lines)} (备份 14668)")
print(f"handler 文件数: {len(list(HANDLERS.glob('*.py')))}, handler 总行数: {handler_total_lines}")
print(f"逐字符一致(零改)的 api 数: {moved_ok}")
print(f"re-export 域: {list(reexport.keys())}")
print(f"server 剩余 api_* 数: {len(server_left_apis)}")
print()
if problems:
    print("!! 发现问题:")
    for p in problems:
        print("   -", p)
else:
    print("✅ 全部校验通过：零改 + import/re-export 闭合 + 无残留 def")
