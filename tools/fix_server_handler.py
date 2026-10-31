#!/usr/bin/env python
# 修复 server.py 的结构性损坏：顶层函数 api_indicators(code=None) 在 10959 行的 return 之后
# 没有正确闭合，把本应属于 class Handler 的 _do_GET / HEAVY_POSTS / _do_shutdown / do_POST /
# _do_POST / log_message 整段（行 10962..12184）误吞成了自己的嵌套函数，导致 Handler 实例
# 既没有 _do_GET 也没有 do_POST，所有 GET/POST 请求都 500（"internal error"）。
#
# 修复：把被吞段原样（缩进恰好是 Handler 方法级别）搬回 class Handler 内部（do_GET 之后），
# 并让 api_indicators 在 return 后自然闭合。被吞段仅引用模块级符号或 self. 方法，作用域安全。
import shutil, ast, sys, os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
P = os.path.join(ROOT, "viewer", "server.py")
BAK = P + ".bak-before-fix-20261028"

def main():
    if os.path.exists(BAK):
        print("备份已存在，跳过备份：", BAK)
    else:
        shutil.copy(P, BAK)
        print("已备份原文件 ->", BAK)

    with open(P, encoding="utf-8") as f:
        lines = f.read().split("\n")

    # 被吞段（1 起行号）：10962 .. 12184  -> 0 起切片 [10961:12184]
    START, END = 10961, 12184
    swallowed = lines[START:END]

    # 基本自检：被吞段首行应是 def _do_GET，末几行应属 log_message 之后、新 class 之前
    assert swallowed[0].strip().startswith("def _do_GET(self)"), "被吞段首行不符"
    assert lines[END].strip().startswith("class "), "被吞段之后应为新 class，实际: %r" % lines[END][:40]

    # 删除被吞段（api_indicators 在 10959 return 后自然闭合）
    del lines[START:END]

    # 插入到 class Handler 内部 do_GET 之后：do_GET 在 10806，方法体结束于 10831，
    # 10832（0 起）是类内空行，其后原为顶层 api 函数（0 缩进）会闭合 Handler，
    # 因此把被吞段插在 10832 之前即可成为 Handler 成员。
    INSERT_AT = 10832
    lines[INSERT_AT:INSERT_AT] = swallowed

    src = "\n".join(lines)

    # 语法校验
    tree = ast.parse(src)

    # 修复后断言：Handler 必须包含 _do_GET 与 do_POST
    handler_methods = []
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == "Handler":
            handler_methods = [n.name for n in node.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
    if "_do_GET" not in handler_methods or "do_POST" not in handler_methods:
        raise SystemExit("修复失败：Handler 仍缺 _do_GET/do_POST，请回滚 %s" % BAK)
    # 确认 api_indicators 不再吞这些成员
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == "api_indicators":
            nested = [n.name for n in node.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
            assert not nested, "api_indicators 仍含嵌套 def: %s" % nested

    with open(P, "w", encoding="utf-8") as f:
        f.write(src)

    print("修复写入成功。")
    print("  - 被吞段搬回 Handler：%d 行（%d..%d）" % (END - START, START + 1, END))
    print("  - 插入位置（0 起）: %d（do_GET 之后，类内）" % INSERT_AT)
    print("  - Handler 方法现含 _do_GET/do_POST：OK")
    print("  - 语法校验：OK")

if __name__ == "__main__":
    main()
