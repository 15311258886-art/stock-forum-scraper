#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import sys, re, os
sys.stdout.reconfigure(encoding='utf-8')
B = r'F:\AI Agent部署知识库\2026-09-22-11-49-42\stock-forum-scraper'
os.chdir(B)
js = open('viewer/static/app.js', encoding='utf-8').read()
ls = js.splitlines()

print('=== A) renderNiusan 全文 ===')
i = js.find('function renderNiusan')
m = re.search(r'\nfunction [a-zA-Z]', js[i+10:])
j = i+10+m.start()+1 if m else i+4000
print(js[i:j])
print()
print('=== B) STARM 段 + loadStarMoves + renderStarMoves ===')
i = js.find('const STARM')
m = re.search(r'\nfunction [a-zA-Z]', js[i+10:])
j2 = i+10+m.start()+1 if m else i+5000
print(js[i:j2])
