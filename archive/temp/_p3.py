# -*- coding: utf-8 -*-
"""人轴端到端回测：逐站实测「人主页 → 持仓/操作线索」能否提取（浏览器过 WAF）"""
import os, re, sys, json, sqlite3, time

B = r'F:\AI Agent部署知识库\2026-09-22-11-49-42\stock-forum-scraper'
os.chdir(B)
sys.path.insert(0, B)
out = []
W = out.append

from scraper.browser_fetch import browser_get

CODE_RE = re.compile(r'\$([^$()]{1,20})\(((?:SH|SZ|BJ|HK)?\d{6})\)\$')
DATE_RE = re.compile(r'(20\d{2})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日')
ACT = ('建仓', '清仓', '加仓', '减仓', '买入', '卖出', '抄底', '补仓', '止损', '止盈', '抢权', '持仓')

con = sqlite3.connect('file:' + os.path.join(B, 'viewer', 'app.db') + '?mode=ro', uri=True)
con.row_factory = sqlite3.Row


def probe(label, url, timeout=45):
    W('')
    W('=' * 70)
    W('[%s] %s' % (label, url))
    W('=' * 70)
    try:
        t0 = time.time()
        r = browser_get(url, timeout=timeout)
        txt = getattr(r, 'text', '') or ''
        status = getattr(r, 'status_code', '?')
        W('  HTTP=%s  用时=%.1fs  长度=%s' % (status, time.time() - t0, len(txt)))
        if len(txt) < 2000:
            W('  !! 内容过短，疑似 WAF 拦截页')
            W('  片段: %s' % re.sub(r'\s+', ' ', txt[:200]))
            return
        codes = CODE_RE.findall(txt)
        dates = DATE_RE.findall(txt)
        acts = [a for a in ACT if a in txt]
        W('  携带 $名称(代码)$ 的标的数: %d' % len(codes))
        for nm, cd in list(dict.fromkeys(codes))[:8]:
            W('        %s (%s)' % (nm, cd))
        W('  中文日期数: %d  样例: %s' % (len(dates), dates[:4]))
        W('  出现的动作词: %s' % ('/'.join(acts) if acts else '无'))
        # 抽一段含动作的原文
        for a in acts[:2]:
            i = txt.find(a)
            if i > 0:
                seg = re.sub(r'\s+', ' ', re.sub(r'<[^>]+>', ' ', txt[max(0, i - 260):i + 160]))
                W('  【%s】上下文: %s' % (a, seg[:330]))
                break
    except Exception as e:
        W('  EXC %s: %s' % (type(e).__name__, str(e)[:180]))


# 1) 雪球：取一个有主页的选手
row = con.execute("SELECT name, source_url FROM players WHERE site='xueqiu' AND source_url LIKE '%/u/%' LIMIT 1").fetchone()
if row:
    probe('雪球·人主页', row['source_url'])

# 2) 淘股吧：已知有人轴持仓
row = con.execute("SELECT name, source_url FROM players WHERE site='taoguba' AND source_url LIKE '%/blog/%' LIMIT 1").fetchone()
if row:
    probe('淘股吧·人主页', row['source_url'])

# 3) 新浪股吧：人主页
row = con.execute("SELECT name, source_url FROM players WHERE site='sina' AND source_url LIKE '%/u/%' LIMIT 1").fetchone()
if row:
    probe('新浪·人主页', row['source_url'])

# 4) 东财：从帖子反推 uid
row = con.execute("SELECT author, post_url FROM trades WHERE site='eastmoney' AND post_url LIKE '%guba.eastmoney.com%' LIMIT 1").fetchone()
if row:
    probe('东财·原帖(取uid用)', row['post_url'])

# 5) 集思录
row = con.execute("SELECT player, post_url FROM positions WHERE site='jisilu' LIMIT 1").fetchone()
if row:
    probe('集思录·来源帖', row['post_url'])

con.close()
open(os.path.join(B, '_p3_out.txt'), 'w', encoding='utf-8').write('\n'.join(out))
print('\n'.join(out))
