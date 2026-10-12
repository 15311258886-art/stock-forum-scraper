# -*- coding: utf-8 -*-
"""修复 strategy_data.py 被花引号污染的定界符。

规则（基于 Python 代码结构本身是 ASCII 这一点）：
- 花引号出现在「定界符位置」→ 还原为 ASCII 双引号
  定界符位置判定：前一个非空字符属于 ([{,:  或行首 → 开引号；
                  后一个非空字符属于 ,)]}:   或行尾 → 闭引号。
- 其余（处在字符串内部）→ 统一换成中文书名式角括号「」，并按出现顺序配对。
"""
import sys, os, py_compile

TARGET = 'strategy_data.py'
OPEN_BEFORE = set('([{,:')
CLOSE_AFTER = set(',)]}:')

src = open(TARGET, encoding='utf-8').read()
n = len(src)

def prev_nonspace(i):
    j = i - 1
    while j >= 0 and src[j] in ' \t':
        j -= 1
    return src[j] if j >= 0 else '\n'

def next_nonspace(i):
    j = i + 1
    while j < n and src[j] in ' \t':
        j += 1
    return src[j] if j < n else '\n'

out = []
i = 0
in_str = False       # 在 ASCII "..." 内部
in_trip = False      # 在三引号内部
inner_open = False   # 当前字符串内是否已开了一个「

while i < n:
    ch = src[i]

    # 三引号整体处理
    if src.startswith('"""', i) or src.startswith("'''", i):
        if not in_str:
            delim = src[i:i + 3]
            in_trip = not in_trip
            out.append(delim); i += 3; continue

    if in_trip:
        out.append(ch); i += 1; continue

    if ch == '\\':
        out.append(src[i:i + 2]); i += 2; continue

    if ch == '"':
        in_str = not in_str
        if not in_str:
            inner_open = False
        out.append(ch); i += 1; continue

    if ch in ('\u201c', '\u201d'):
        p = prev_nonspace(i)
        q = next_nonspace(i)
        is_open_delim = (not in_str) and (p in OPEN_BEFORE or p == '\n')
        is_close_delim = in_str and (q in CLOSE_AFTER or q == '\n')
        if is_open_delim or (not in_str and ch == '\u201c'):
            out.append('"'); in_str = True; inner_open = False
        elif is_close_delim:
            out.append('"'); in_str = False; inner_open = False
        else:
            # 字符串内部：按出现顺序配对成「」
            if not inner_open:
                out.append('\u300c'); inner_open = True
            else:
                out.append('\u300d'); inner_open = False
        i += 1; continue

    out.append(ch); i += 1

fixed = ''.join(out)
open(TARGET, 'w', encoding='utf-8').write(fixed)

left = fixed.count('\u201c') + fixed.count('\u201d')
print('剩余花引号:', left, '| in_str:', in_str, '| in_trip:', in_trip)
try:
    py_compile.compile(TARGET, doraise=True)
    print('PY_COMPILE_OK')
except Exception as e:
    print('PY_FAIL', str(e)[:300])
