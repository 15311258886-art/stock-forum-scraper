import sys, re
sys.stdout.reconfigure(encoding='utf-8')
ls = open('viewer/static/app.js', encoding='utf-8').read().splitlines()
print('=== renderConsensus 全文 (2105-2170) ===')
for i in range(2104, 2170):
    print('%5d: %s' % (i+1, ls[i][:195]))
print()
print('=== renderCohold + coholdTable (2200-2300) ===')
for i in range(2199, 2300):
    print('%5d: %s' % (i+1, ls[i][:195]))
