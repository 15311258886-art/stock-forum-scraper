# -*- coding: utf-8 -*-
"""修复 strategy_data.py 中「字符串内部误用 ASCII 双引号当中文引号」的问题。
只替换既非前导也非后置定界符的引号，语法分隔符原样保留。"""
import re, sys

P = r"F:\AI Agent部署知识库\2026-09-22-11-49-42\stock-forum-scraper\strategy_data.py"
src = open(P, encoding="utf-8").read()

DELIM_PREV = set(":,{[(")
DELIM_NEXT = set(":,}])")

out = []
toggle = 0          # 0 -> “, 1 -> ”
replaced = 0


def prev_nonspace(s, i):
    j = i - 1
    while j >= 0 and s[j] in " \t":
        j -= 1
    return s[j] if j >= 0 else ""


def next_nonspace(s, i):
    j = i + 1
    while j < len(s) and s[j] in " \t":
        j += 1
    return s[j] if j < len(s) else ""


i = 0
while i < len(src):
    ch = src[i]
    if ch == '"':
        p = prev_nonspace(src, i)
        n = next_nonspace(src, i)
        is_open_delim = p in DELIM_PREV or p == ""
        is_close_delim = n in DELIM_NEXT
        if is_open_delim or is_close_delim:
            out.append('"')
        else:
            out.append('\u201c' if toggle == 0 else '\u201d')
            toggle ^= 1
            replaced += 1
    else:
        out.append(ch)
    i += 1

new = "".join(out)
open(P, "w", encoding="utf-8").write(new)
print("replaced inner quotes:", replaced)
print("toggle end state:", toggle, "(0=balanced)")

# 语法校验
import py_compile
try:
    py_compile.compile(P, doraise=True)
    print("PY_OK")
except Exception as e:
    print("PY_FAIL", e)
    # 打印出错行
    import traceback
    m = re.search(r"line (\d+)", str(e))
    if m:
        ln = int(m.group(1))
        for k, l in enumerate(new.splitlines(), 1):
            if abs(k - ln) <= 2:
                print("  %4d: %s" % (k, l[:170]))
