# -*- coding: utf-8 -*-
"""盘点并清理根目录临时文件：保留可复用工具与交付物，删除一次性草稿。"""
import os, datetime

B = os.path.dirname(os.path.abspath(__file__))

# 明确保留（可复用工具 / 交付物 / 核心脚本）
KEEP = {
    # 核心业务脚本
    'auto_update.py', 'lhb_fetch.py', 'holders_fetch.py', 'holders_tag.py',
    'lhb_sector_link.py', 'sector_chain.py', 'audit_health.py', 'run_backfill_local.py',
    'run_backfill_local.bat', 'clip_to_cookie.py', 'save_cookie_tool.py',
    'ingest_local.py', 'mark_discussion.py',
    'xueqiu_cube_crawler.cjs', 'xueqiu_cube_load.py', 'xueqiu_targets.json',
    'moer_crawler.cjs', 'moer_load.py', 'moer_targets.example.json',
    'xueqiu_targets.example.json', 'fill_xq_uids.py',
    # 验证工具
    'verify_viewer_ui.cjs', 'verify_kline_ui.cjs', 'verify_lhb_ui.cjs',
    'restart_viewer.py', 'merge_holdings.py',
    # 交付物（HTML/截图/JSON 结果）
    '数据完整性回测报告_20260925.html', '整体体检报告_20260925.html',
    '自动更新说明_20260925.html', '回填操作指南_演示步骤.html',
    '龙虎榜板块扩散分析_20260925.html', '产品对标与优化名录_20260925.html',
    '个股详情K线_渲染预览_20260925.png',
    'sector_link_result.json', 'auto_update_status.json',
    'cookies.txt',
    # 项目自带文件（非我生成，绝不能删）
    'main.py', 'requirements.txt', 'selftest.py',
    # 脚本缓存（删了会导致重跑 300+ 次 F10 请求）
    'sector_map_cache.json', 'sector_chain_cache.json',
    # 历史交付 HTML（有留存价值）
    '代码审查报告.html', '测试与优化方案.html',
}
# 保留前缀（可复用目标清单）
KEEP_PREFIX = ('第1步-', '第2步-', 'xueqiu_cubes')

SKIP_DIRS = {'.venv', 'node_modules', '__pycache__', '.git', '.browser-profile',
             'data', 'viewer', 'scraper', 'tools', 'config', 'docs', 'reports', 'tests'}

rows = []
for f in sorted(os.listdir(B)):
    p = os.path.join(B, f)
    if not os.path.isfile(p):
        continue
    if f in KEEP or f.startswith(KEEP_PREFIX):
        continue
    sz = os.path.getsize(p)
    m = datetime.datetime.fromtimestamp(os.path.getmtime(p))
    rows.append((f, sz, m))

print('=== 待清理候选（%d 个） ===' % len(rows))
total = 0
for f, sz, m in rows:
    total += sz
    print('  %s  %8dB  %s' % (m.strftime('%m-%d %H:%M'), sz, f))
print('合计 %.1f MB' % (total / 1048576))
print()
print('=== 保留清单 ===')
for f in sorted(KEEP):
    p = os.path.join(B, f)
    if os.path.exists(p):
        print('  OK   %-42s %s' % (f, os.path.getsize(p)))
