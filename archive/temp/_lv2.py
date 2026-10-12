import sys, re, os, subprocess, csv, io
sys.stdout.reconfigure(encoding='utf-8')
B = os.getcwd()
s = open(os.path.join(B,'viewer','server.py'), encoding='utf-8').read()
ls = s.splitlines()
print('server.py lines', len(ls))
print()
print('=== api_consensus 定义行 ===')
for n,l in enumerate(ls,1):
    if re.match(r'^def (api_consensus|api_cohold_recent|api_learn|api_strategy)\b', l):
        print('%5d: %s' % (n, l.strip()[:130]))
print()
print('=== 路由区（含 api_summary / consensus / niusan）===')
for n,l in enumerate(ls,1):
    if 'route == "/api/' in l and re.search(r'(consensus|summary|niusan|learn|strategy|dashboard)', l):
        print('%5d: %s' % (n, l.strip()[:130]))
print()
print('=== db.py 表清单 / Database.counts ===')
dbp = os.path.join(B,'viewer','db.py')
db = open(dbp, encoding='utf-8').read()
for n,l in enumerate(db.splitlines(),1):
    if re.search(r'CREATE TABLE', l):
        print('%5d: %s' % (n, l.strip()[:130]))
print()
print('=== 进程 ===')
tl = subprocess.run(['tasklist','/FO','CSV','/NH'], capture_output=True, text=True, encoding='utf-8', errors='ignore').stdout
for r in csv.reader(io.StringIO(tl)):
    if len(r)>=2 and r[0].lower() in ('python.exe','pythonw.exe'):
        print('  ', r[0], 'PID', r[1], 'mem', r[4] if len(r)>4 else '')
