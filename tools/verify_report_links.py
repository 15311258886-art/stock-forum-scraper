# -*- coding: utf-8 -*-
"""
报告链接自检工具（《国外热点传导日报》配套）

用途：把 reports/国外热点传导日报-<date>.md 中出现的全部链接，逐条比对
      overseas_news.url 集合，找出「臆造链接」与「误引示例链接」。

用法（在项目根目录 stock-forum-scraper 下执行）：
    .venv\\Scripts\\python.exe tools/verify_report_links.py              # 自动取最新一期
    .venv\\Scripts\\python.exe tools/verify_report_links.py 2026-09-26   # 指定日期

退出码：0 = 未命中为 0（通过）；1 = 存在未命中（修正后再交付）

背景：本机 Bash 工具的 grep/awk 可用但中文路径易出问题，故统一用本脚本核对。
      2026-09-26 首次实跑即抓出 3 处联合早报 URL 日期段笔误（story20260925-* 应为
      story20260926-*），证明该自检必须作为交付前的强制步骤。
"""
import sys, io, os, re, glob

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))) or '.')
from viewer.db import Database

REPORT_DIR = 'reports'


def norm(u):
    """归一化：去 fragment、去 query、去尾斜杠、转小写"""
    u = u.split('#')[0].split('?')[0]
    return u.rstrip('/').lower()


def pick_report(arg):
    if arg:
        p = os.path.join(REPORT_DIR, '国外热点传导日报-%s.md' % arg)
        if not os.path.exists(p):
            raise SystemExit('报告不存在: %s' % p)
        return p
    cands = sorted(glob.glob(os.path.join(REPORT_DIR, '国外热点传导日报-*.md')))
    if not cands:
        raise SystemExit('未找到任何报告，请检查 %s/ 目录' % REPORT_DIR)
    return cands[-1]


def main():
    report = pick_report(sys.argv[1] if len(sys.argv) > 1 else None)
    md = io.open(report, encoding='utf-8').read()

    links = re.findall(r'https?://[^\s\)\]\*<>"|]+', md)
    links = [l.rstrip('.,;`') for l in links]

    db = Database()
    dburls = set()
    for (u,) in db.conn.execute("SELECT url FROM overseas_news"):
        if u:
            dburls.add(norm(u))
    db.close()

    uniq, seen = [], set()
    for l in links:
        n = norm(l)
        if n in seen:
            continue
        seen.add(n)
        uniq.append(l)

    miss, seed = [], []
    for l in uniq:
        n = norm(l)
        if 'example.local' in n:
            seed.append(l)
        elif n not in dburls:
            miss.append(l)

    print('报告: %s' % report)
    print('链接总数(去重后)=%d' % len(uniq))
    print('真实命中=%d' % (len(uniq) - len(miss) - len(seed)))
    print('引用示例链接=%d' % len(seed))
    print('未命中(必须修正)=%d' % len(miss))
    for m in miss:
        print('   MISS: %s' % m)
    for s in seed:
        print('   SEED: %s' % s)

    return 1 if miss else 0


if __name__ == '__main__':
    sys.exit(main())
