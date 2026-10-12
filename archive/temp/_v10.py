# -*- coding: utf-8 -*-
import sys, os, py_compile
sys.stdout.reconfigure(encoding='utf-8')

for f in ['strategy_data.py', 'strategy_sim.py']:
    print('%-20s exists=%-6s %s' % (f, os.path.exists(f), (os.path.getsize(f) if os.path.exists(f) else '')))
    if os.path.exists(f):
        t = open(f, encoding='utf-8').read()
        print('   curly:', t.count(chr(0x201C)), t.count(chr(0x201D)), '| ascii-dq:', t.count(chr(34)), '| ascii-sq:', t.count(chr(39)))
        try:
            py_compile.compile(f, doraise=True); print('   PY_OK')
        except Exception as e:
            print('   PY_FAIL', str(e)[:220])

print()
print('=== strategy_data 内容 ===')
try:
    sys.path.insert(0, os.getcwd())
    import strategy_data as SD
    print('  BASICS groups:', len(getattr(SD, 'BASICS', []) or []))
    print('  STRATEGIES:', len(getattr(SD, 'STRATEGIES', []) or []))
    print('  BUFFETT:', len(getattr(SD, 'BUFFETT', []) or []))
    if getattr(SD, 'STRATEGIES', None):
        print('  样例策略:', [s.get('name') for s in SD.STRATEGIES[:6]])
    if getattr(SD, 'BASICS', None):
        print('  知识分组:', [g.get('group') for g in SD.BASICS])
except Exception as e:
    print('  IMPORT_FAIL', type(e).__name__, str(e)[:220])
