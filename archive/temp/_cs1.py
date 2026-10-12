import sys, re
sys.stdout.reconfigure(encoding='utf-8')
js = open('viewer/static/app.js', encoding='utf-8').read()
ls = js.splitlines()

print('=== 定位共识/cohold 相关函数 ===')
for n, l in enumerate(ls, 1):
    if re.match(r'^(async )?function \w*[Cc]o\w*\(', l) or re.match(r'^const CO\w*', l):
        print('%5d: %s' % (n, l.strip()[:150]))

print()
print('=== 含 cohold / consensus 的行 ===')
for n, l in enumerate(ls, 1):
    if 'cohold' in l.lower() or 'consensus' in l.lower():
        print('%5d: %s' % (n, l.rstrip()[:175]))
