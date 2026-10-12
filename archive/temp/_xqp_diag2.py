# -*- coding: utf-8 -*-
"""诊断2：雪球人主页的时间/条目结构到底藏在哪（HTML属性 or 内嵌JSON）。"""
import os, re, json

B = os.path.dirname(os.path.abspath(__file__))
CD = os.path.join(B, '_xq_person_pages')
p = os.path.join(CD, '9092514787.html')
raw = open(p, encoding='utf-8', errors='ignore').read()

out = []
out.append('文件 %s  %d 字节' % (os.path.basename(p), len(raw)))

# 1) 时间属性：datetime= / data-time= / created_at
for pat in [r'datetime="([^"]+)"', r'data-time="([^"]+)"', r'data-created[^=]*="([^"]+)"',
            r'created_at["\']?\s*[:=]\s*["\']?([\d\-: T]{10,25})', r'createdAt["\']?\s*[:=]\s*["\']?([\d\-: T]{10,25})']:
    hits = re.findall(pat, raw)
    out.append('  属性/JSON %-22s : %d  %s' % (pat[:20], len(hits), hits[:5]))

# 2) 时间戳（10位或13位）
ts13 = re.findall(r'\b(1[5-9]\d{11})\b', raw)   # 毫秒
ts10 = re.findall(r'\b(1[5-9]\d{9})\b', raw)     # 秒
out.append('  13位时间戳: %d  %s' % (len(ts13), ts13[:5]))
out.append('  10位时间戳: %d  %s' % (len(ts10), ts10[:5]))

# 3) 相对时间文本
for pat in [r'\d+月\d+日前', r'\d+分钟前', r'\d+小时前', r'今天\d{1,2}:\d{2}', r'昨天\d{1,2}:\d{2}', r'\d{2}-\d{2}\s+\d{2}:\d{2}', r'\d{4}-\d{2}-\d{2}']:
    hits = re.findall(pat, raw)
    out.append('  文本 %-18s : %d  %s' % (pat, len(hits), hits[:5]))

# 4) 内嵌 JSON 里的 status 字段
m = re.search(r'window\.__INITIAL_STATE__\s*=\s*(\{.*?\});?\s*</script>', raw, re.S)
out.append('  __INITIAL_STATE__: %s' % bool(m))
if m:
    try:
        js = json.loads(m.group(1))
        out.append('    top keys: %s' % list(js.keys())[:20])
    except Exception as e:
        out.append('    parse fail %s' % e)

# 5) 找 timeline__item 附近的原始片段（含时间线索）
idxs = [mm.start() for mm in re.finditer(r'timeline__item', raw)]
out.append('  timeline__item 位置数: %d' % len(idxs))
if idxs:
    seg = raw[max(0, idxs[0]-200): idxs[0]+2200]
    out.append('  ---- 第1个 timeline__item 附近原文 ----')
    out.append(seg[:2400])

open(os.path.join(B, '_xqp_diag2_out.txt'), 'w', encoding='utf-8').write('\n'.join(out))
print('\n'.join(out))
