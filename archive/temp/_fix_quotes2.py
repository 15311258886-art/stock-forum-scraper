# -*- coding: utf-8 -*-
"""二级修复：
1) 先把上一步误换成的中文引号 “ ” 还原为 ASCII "
2) 保护三引号 docstring 定界符
3) 只把「两侧贴中文/全角字符」的引号换成中文引号
4) 还原三引号并做语法+结构校验
"""
import re, py_compile, sys

P = r"F:\AI Agent部署知识库\2026-09-22-11-49-42\stock-forum-scraper\strategy_data.py"
src = open(P, encoding="utf-8").read()

# 1) 还原
src = src.replace("\u201c", '"').replace("\u201d", '"')

# 2) 保护三引号
src = src.replace('"""', "\x00TQ\x00").replace("'''", "\x00TS\x00")

CJK = re.compile(r"[\u3000-\u303f\u4e00-\u9fff\uff00-\uffef]")
DELIM_PREV = set(":,{[(")
DELIM_NEXT = set(":,}])")


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


out = []
toggle = 0
replaced = 0
i = 0
while i < len(src):
    ch = src[i]
    if ch == '"':
        p = prev_nonspace(src, i)
        n = next_nonspace(src, i)
        p_cjk = bool(p) and CJK.match(p)
        n_cjk = bool(n) and CJK.match(n)
        if p_cjk or n_cjk:
            out.append("\u201c" if toggle == 0 else "\u201d")
            toggle ^= 1
            replaced += 1
        else:
            out.append('"')
    else:
        out.append(ch)
    i += 1
new = "".join(out)

# 4) 还原三引号
new = new.replace("\x00TQ\x00", '"""').replace("\x00TS\x00", "'''")

open(P, "w", encoding="utf-8").write(new)
print("replaced inner quotes:", replaced, "| toggle:", toggle)

try:
    py_compile.compile(P, doraise=True)
    print("PY_OK")
except Exception as e:
    print("PY_FAIL:", e)
    ln = None
    m = re.search(r"line (\d+)", str(e))
    if m:
        ln = int(m.group(1))
    if ln:
        ls = new.splitlines()
        for k in range(max(1, ln - 3), min(len(ls), ln + 3) + 1):
            print("  %4d: %s" % (k, ls[k - 1][:180]))
