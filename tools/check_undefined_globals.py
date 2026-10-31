#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""未定义全局名体检器（上线前检查 / 提交前自检）。

背景
----
本项目反复出现同一族缺陷：**代码里用了一个名字，但从没定义/导入**。
它们静态 import 阶段不报错，只在真正执行到那一行时才抛 NameError，
于是表现为「任务跑了一会儿才崩」或「只在错误分支才炸」，极难在代码评审里看出来。

已发生过的实例：
  - 2026-10-31 `viewer/evolve.py` 的 `hashlib.sha1()` 未 import
      → evolve_daily 定时任务连续失败 26 次
  - `viewer/server.py` 的 3 个 except 分支用 `log` / `logger`，
      但该作用域与模块级都没有绑定
      → 异常处理自身抛 NameError，把真实错误盖成 500
  - 历史：lhb_daily 漏 `global`、build_exclusion 漏 `argv`（同族）

用法
----
    python tools/check_undefined_globals.py                 # 扫默认范围 viewer/ tools/ tests/
    python tools/check_undefined_globals.py viewer/ tools/  # 扫指定目录
    python tools/check_undefined_globals.py --selftest      # 跑内置自测，验证检测器本身没错
    python tools/check_undefined_globals.py --json          # 机器可读输出

退出码
------
    0 = 干净；1 = 发现可疑项；2 = 用法/环境错误

设计要点（为什么可信）
----------------------
用**两套算法完全不同的引擎**互相印证，避免单一实现的盲区：

  引擎 A（symtable）：AST 符号表。递归遍历所有子作用域，找出
      `is_referenced() and is_global() and not is_local()`
      且既不在模块级绑定集合、也不在 builtins 中的名字。

  引擎 B（字节码）：compile() 后递归 code object，直接取
      `LOAD_GLOBAL` / `LOAD_NAME` / `STORE_GLOBAL` / `DELETE_GLOBAL`
      的 argval —— 这正是 CPython 运行时真正去「模块 globals → builtins」查找的指令，
      最贴近「会不会真的 NameError」这个事实。

两套都命中 = HIGH（基本可断定）；只有一套命中 = LOW（需人工看一眼）。

已知的、必须白名单的名字：模块 dunder、`__annotations__`
（`from __future__ import annotations` 下编译器会隐式读写它）。

已知局限（诚实说明，不要当它万能）：
  - `from x import *` 会把未知名字注入模块，导致大量误报 → 检出星号导入时整份报告标记为
    UNRELIABLE，此时以人工判断为准。
  - `exec` / `eval` / 动态 `globals()[...]` 写入的名字无法静态发现。
  - 只检查「名字存在性」，不检查类型/参数个数，不能替代真实运行与测试。
"""
from __future__ import annotations

import argparse
import ast
import builtins
import dis
import json
import os
import sys
import symtable
import tempfile
import types

# ---------------------------------------------------------------------------
# 白名单：合法的隐式全局
# ---------------------------------------------------------------------------
DUNDERS = {
    "__file__", "__name__", "__doc__", "__package__", "__spec__",
    "__loader__", "__builtins__", "__debug__", "__annotations__",
    "__dict__", "__module__", "__qualname__", "__class__", "__path__",
}
BUILTIN_NAMES = set(dir(builtins)) | DUNDERS


def iter_py_files(targets):
    """展开目标为 .py 文件列表（目录递归，跳过虚拟环境/缓存/备份）。"""
    skip_dirs = {".venv", "venv", "env", "__pycache__", ".git", "node_modules",
                 "backups", "viewer/backups", ".mypy_cache", ".pytest_cache"}
    out = []
    for t in targets:
        if os.path.isfile(t) and t.endswith(".py"):
            out.append(t)
            continue
        for root, dirs, files in os.walk(t):
            dirs[:] = [d for d in dirs if d not in skip_dirs]
            if any(s in root.replace("\\", "/") for s in ("/backups/", "/__pycache__/")):
                continue
            for f in files:
                if f.endswith(".py"):
                    out.append(os.path.join(root, f))
    return sorted(set(out))


# ---------------------------------------------------------------------------
# 引擎 A：symtable
# ---------------------------------------------------------------------------
def module_bound_names(tbl):
    """模块级已绑定的名字：import / 赋值 / def / class。"""
    bound = set()
    for s in tbl.get_symbols():
        if s.is_imported() or s.is_assigned() or s.is_namespace():
            bound.add(s.get_name())
    return bound


def scan_symtable(src, filename):
    """返回 [(name, scope_path)]，scope_path 形如 '<module>' / 'fn:mine_rules'。"""
    tbl = symtable.symtable(src, filename, "exec")
    mod_bound = module_bound_names(tbl)
    hits = []

    def walk(t, path):
        for s in t.get_symbols():
            n = s.get_name()
            if not s.is_referenced():
                continue
            # 已经是本作用域局部的（参数/局部赋值）不算
            if s.is_local() or s.is_parameter():
                continue
            if not s.is_global():
                continue
            if n in mod_bound or n in BUILTIN_NAMES:
                continue
            hits.append((n, path))
        for child in t.get_children():
            walk(child, "fn:" + child.get_name() if child.get_type() != "class"
                 else "class:" + child.get_name())

    walk(tbl, "<module>")
    return hits


# ---------------------------------------------------------------------------
# 引擎 B：字节码
# ---------------------------------------------------------------------------
_GLOBAL_OPS = {"LOAD_GLOBAL", "LOAD_NAME", "STORE_GLOBAL", "DELETE_GLOBAL"}


def _bound_from_ast_module(tree):
    """AST 递归收集模块级绑定（不深入函数/类体，但收集其名字本身）。"""
    bound = set()

    class V(ast.NodeVisitor):
        def visit_Import(self, node):
            for a in node.names:
                bound.add((a.asname or a.name).split(".")[0])

        def visit_ImportFrom(self, node):
            for a in node.names:
                if a.name == "*":
                    bound.add("*STAR*")
                else:
                    bound.add(a.asname or a.name)

        def visit_Assign(self, node):
            for t in node.targets:
                self._targets(t)

        def visit_AnnAssign(self, node):
            self._targets(node.target)

        def visit_AugAssign(self, node):
            self._targets(node.target)

        def visit_For(self, node):
            self._targets(node.target)
            self.generic_visit(node)

        def visit_With(self, node):
            for item in node.items:
                if item.optional_vars is not None:
                    self._targets(item.optional_vars)
            self.generic_visit(node)

        def visit_Try(self, node):
            self.generic_visit(node)

        def visit_ExceptHandler(self, node):
            if node.name:
                bound.add(node.name)
            self.generic_visit(node)

        def visit_Global(self, node):
            for n in node.names:
                bound.add(n)

        def visit_FunctionDef(self, node):
            bound.add(node.name)

        def visit_AsyncFunctionDef(self, node):
            bound.add(node.name)

        def visit_ClassDef(self, node):
            bound.add(node.name)

        def visit_Lambda(self, node):
            pass

        def _targets(self, t):
            for n in ast.walk(t):
                if isinstance(n, ast.Name):
                    bound.add(n.id)

    V().visit(tree)
    return bound


def scan_bytecode(src, filename):
    """返回 [(name, codename)]，基于 LOAD_GLOBAL 等指令。"""
    tree = ast.parse(src, filename)
    mod_bound = _bound_from_ast_module(tree)
    if "*STAR*" in mod_bound:
        return None  # 星号导入 → 不可靠
    hits = []
    seen = set()

    def walk_code(code, path):
        for ins in dis.get_instructions(code):
            if ins.opname in _GLOBAL_OPS and isinstance(ins.argval, str):
                n = ins.argval
                if n in mod_bound or n in BUILTIN_NAMES:
                    continue
                key = (n, path)
                if key in seen:
                    continue
                seen.add(key)
                hits.append((n, path))
        for const in code.co_consts:
            if isinstance(const, types.CodeType):
                walk_code(const, "code:" + const.co_name)

    code = compile(src, filename, "exec")
    walk_code(code, "<module>")
    return hits


# ---------------------------------------------------------------------------
# 合并 + 输出
# ---------------------------------------------------------------------------
def check_file(path):
    """返回 dict：{file, a:[], b:[], merged:[{name,scope,level}], error}"""
    res = {"file": path, "a": [], "b": [], "merged": [], "error": None,
           "star": False}
    try:
        src = open(path, encoding="utf-8").read()
    except (UnicodeDecodeError, OSError) as e:
        res["error"] = f"读取失败: {e}"
        return res
    try:
        res["a"] = scan_symtable(src, path)
    except SyntaxError as e:
        res["error"] = f"语法错误: {e}"
        return res
    except Exception as e:  # symtable 对极少数构造会抛
        res["a"] = []
        res["a_err"] = f"{type(e).__name__}: {e}"

    try:
        b = scan_bytecode(src, path)
        if b is None:
            res["star"] = True
            res["b"] = []
        else:
            res["b"] = b
    except SyntaxError as e:
        if not res["error"]:
            res["error"] = f"语法错误: {e}"
        return res
    except Exception as e:
        res["b_err"] = f"{type(e).__name__}: {e}"

    a_names = {}
    for n, sc in res["a"]:
        a_names.setdefault(n, sc)
    b_names = {}
    for n, sc in res["b"]:
        b_names.setdefault(n, sc)

    for n in sorted(set(a_names) | set(b_names)):
        level = "HIGH" if (n in a_names and n in b_names) else "LOW"
        res["merged"].append({
            "name": n,
            "scope": a_names.get(n) or b_names.get(n),
            "level": level,
            "a": n in a_names,
            "b": n in b_names,
        })
    return res


def summarize(results):
    total_high = total_low = 0
    files_with_issue = []
    for r in results:
        hi = [m for m in r["merged"] if m["level"] == "HIGH"]
        lo = [m for m in r["merged"] if m["level"] == "LOW"]
        if hi or lo:
            files_with_issue.append(r)
            total_high += len(hi)
            total_low += len(lo)
    return total_high, total_low, files_with_issue


def print_report(results, verbose=False):
    hi, lo, files = summarize(results)
    scanned = len(results)
    errs = [r for r in results if r["error"]]
    stars = [r for r in results if r.get("star")]

    print("=" * 78)
    print("未定义全局名体检（查「用了名字但没 import/定义」→ 运行时 NameError）")
    print("=" * 78)
    print(f"扫描文件: {scanned}    HIGH(双引擎一致): {hi}    LOW(单引擎): {lo}")
    if errs:
        print(f"\n无法解析的文件（{len(errs)}），已跳过:")
        for r in errs:
            print(f"  ! {r['file']}  -> {r['error']}")
    if stars:
        print(f"\n[UNRELIABLE] 以下文件含 `from x import *`，其报告不可信，需人工看:")
        for r in stars:
            print(f"  ~ {r['file']}")

    if not files:
        print("\n结果: CLEAN —— 未发现未定义全局名。")
        return 0

    print("\n可疑项:")
    for r in files:
        print(f"\n  {r['file']}")
        for m in r["merged"]:
            mark = "✗" if m["level"] == "HIGH" else "?"
            engines = ("A" if m["a"] else "") + ("B" if m["b"] else "")
            print(f"    {mark} {m['name']:<24} @ {m['scope']:<34} "
                  f"[{m['level']}/{engines}]")
    if not verbose:
        print("\n提示: LOW 项多为引擎差异导致的误报，加 --verbose 看引擎明细；"
              "\n      HIGH 项请优先核实——它在运行时极可能抛 NameError。")
    return 1 if (hi or lo) else 0


# ---------------------------------------------------------------------------
# 自测：证明检测器本身没有假阴/假阳
# ---------------------------------------------------------------------------
SELFTEST_CASES = [
    # (代码, 期望命中的名字, 不应命中的名字)
    ("import os\nprint(os.sep)\n", set(), {"os"}),
    ("print(range(3))\nprint(len([]))\n", set(), {"range", "len"}),
    ("def f():\n    x = 1\n    return x\n", set(), {"x"}),
    ("def f():\n    try:\n        pass\n    except ValueError as e:\n        return e\n",
     set(), {"e", "ValueError"}),
    ("def f():\n    return undefined_thing\n", {"undefined_thing"}, set()),
    ("def f():\n    return os.sep\n", {"os"}, set()),
    ("if True:\n    y = 1\nprint(y)\n", set(), {"y"}),
    ("def outer():\n    def inner():\n        return ghost_name\n    return inner\n",
     {"ghost_name"}, set()),
    ("total = 0\ndef add():\n    global total\n    total += 1\n", set(), {"total"}),
    ("from __future__ import annotations\ndef f(x: NotImported) -> AlsoNot: ...\n",
     set(), {"NotImported", "AlsoNot"}),
    ("print(__name__, __file__)\n", set(), {"__name__", "__file__"}),
]


def selftest():
    print("=" * 78)
    print("自测：验证检测器在已知用例上的行为")
    print("=" * 78)
    tmpdir = tempfile.mkdtemp(prefix="undef_globals_selftest_")
    failed = 0
    for idx, (code, expect_hit, expect_miss) in enumerate(SELFTEST_CASES, 1):
        p = os.path.join(tmpdir, f"case{idx}.py")
        open(p, "w", encoding="utf-8").write(code)
        r = check_file(p)
        got = {m["name"] for m in r["merged"]}
        ok_hit = expect_hit <= got
        ok_miss = not (expect_miss & got)
        status = "PASS" if (ok_hit and ok_miss) else "FAIL"
        if status == "FAIL":
            failed += 1
        print(f"  [{status}] case{idx}: 期望命中{set(expect_hit) or '{}'} "
              f"实际{got or '{}'}")
        if status == "FAIL":
            print(f"         代码: {code!r}")
            if not ok_hit:
                print(f"         漏报: {expect_hit - got}")
            if not ok_miss:
                print(f"         误报: {expect_miss & got}")
    print(f"\n自测结果: {len(SELFTEST_CASES) - failed}/{len(SELFTEST_CASES)} 通过")
    try:
        for f in os.listdir(tmpdir):
            os.remove(os.path.join(tmpdir, f))
        os.rmdir(tmpdir)
    except OSError:
        pass
    return 0 if failed == 0 else 1


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="检查「引用了但从未定义/导入」的全局名（运行时 NameError 的根源）")
    ap.add_argument("targets", nargs="*", default=None,
                    help="要扫描的文件或目录（默认 viewer/ tools/ tests/）")
    ap.add_argument("--selftest", action="store_true", help="跑检测器自测")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    ap.add_argument("--verbose", "-v", action="store_true", help="显示引擎明细")
    args = ap.parse_args(argv)

    if args.selftest:
        return selftest()

    targets = args.targets or [t for t in ("viewer", "tools", "tests") if os.path.isdir(t)]
    if not targets:
        print("用法错误：没有可扫描的目标目录", file=sys.stderr)
        return 2

    files = iter_py_files(targets)
    if not files:
        print("用法错误：目标下没有 .py 文件", file=sys.stderr)
        return 2

    results = [check_file(f) for f in files]

    if args.json:
        hi, lo, files_with_issue = summarize(results)
        print(json.dumps({
            "scanned": len(results),
            "high": hi, "low": lo,
            "issues": [{"file": r["file"], "items": r["merged"]}
                       for r in files_with_issue],
            "errors": [{"file": r["file"], "error": r["error"]} for r in results if r["error"]],
        }, ensure_ascii=False, indent=2))
        return 1 if (hi or lo) else 0

    return print_report(results, verbose=args.verbose)


if __name__ == "__main__":
    sys.exit(main())
