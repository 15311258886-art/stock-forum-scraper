#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""核对当前状态：导航结构 / 牛散函数 / 样式 / 持仓chip"""
import sys, re, os, sqlite3
sys.stdout.reconfigure(encoding='utf-8')
B = r'F:\AI Agent部署知识库\2026-09-22-11-49-42\stock-forum-scraper'
os.chdir(B)

js = open('viewer/static/app.js', encoding='utf-8').read()
css = open('viewer/static/app.css', encoding='utf-8').read()
html = open('viewer/static/index.html', encoding='utf-8').read()

print('app.js lines:', len(js.splitlines()))
print()
print('=== 1) NAV 子标签归属 ===')
i = js.find('const NAV')
j = js.find('\n];', i)
nav = js[i:j+3]
# 提取 key/label 和 items
cur_group = None
for ln in nav.splitlines():
    m = re.match(r'\s*\{\s*key:\s*"([a-z_]+)",\s*label:\s*"([^"]+)"', ln)
    if m:
        cur_group = m.group(2)
        print(' [组] %-10s %s' % (m.group(1), m.group(2)))
    m2 = re.match(r'\s*\{\s*tab:\s*"([a-z_]+)",\s*label:\s*"([^"]+)"', ln)
    if m2:
        print('     - %-16s %s' % (m2.group(1), m2.group(2)))

print()
print('=== 2) 牛散相关函数 ===')
for n, l in enumerate(js.splitlines(), 1):
    if re.match(r'^(async )?function (renderNiusan|loadNiusan|renderStarMoves|loadStarMoves)\b', l) or re.match(r'^const (NIU|STARM)\b', l):
        print('%5d: %s' % (n, l.strip()[:140]))

print()
print('=== 3) 样式 ===')
for k in ['ml-card', 'ml-lead', 'ml-forward', 'lv-hot', 'chip-time']:
    print('  css %-12s %d' % (k, css.count(k)))

print()
print('=== 4) 持仓 chip 渲染 ===')
for n, l in enumerate(js.splitlines(), 1):
    if 'chip-time' in l or 'hls.slice' in l or 'hlShown' in l:
        print('%5d: %s' % (n, l.strip()[:180]))

print()
print('=== 5) index.html panels ===')
print([m.group(1) for m in re.finditer(r'id="(p-[a-z_]+)"', html)])
