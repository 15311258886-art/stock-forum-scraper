# -*- coding: utf-8 -*-
"""追加「主线研判」样式到 app.css（幂等：已存在则跳过）。"""
import os, sys, io

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

B = r'F:\AI Agent部署知识库\2026-09-22-11-49-42\stock-forum-scraper'
P = os.path.join(B, 'viewer', 'static', 'app.css')

CSS = """

/* ================= 主线研判（今日看板）2026-09-25 ================= */
/* 三档评级配色：hot=重点方向 / warm=跟进关注 / cool=仅观察
   刻意避开红涨绿跌，改用强调青 + 琥珀 + 灰，避免与行情色混淆 */
.ml-focus {
  margin: 0 0 14px;
  padding: 12px 14px;
  border-radius: 10px;
  background: linear-gradient(180deg, rgba(14,116,144,.10), rgba(14,116,144,.03));
  border: 1px solid rgba(14,116,144,.32);
}
.ml-focus-t {
  font-size: var(--fs-sm);
  font-weight: 700;
  color: var(--accent, #0E7490);
  margin-bottom: 9px;
  letter-spacing: .3px;
}
.ml-focus-list { display: flex; flex-wrap: wrap; gap: 8px; }
.ml-focus-item {
  display: inline-flex; align-items: center; gap: 8px;
  padding: 7px 12px 7px 8px;
  border-radius: 8px;
  background: var(--panel, #fff);
  border: 1px solid var(--border, #E3E7EB);
  box-shadow: 0 1px 2px rgba(15, 39, 48, .05);
}
.ml-focus-item.lv-hot {
  border-color: var(--accent, #0E7490);
  background: rgba(14,116,144,.07);
  box-shadow: 0 2px 6px rgba(14,116,144,.16);
}
.ml-focus-item.lv-warm { border-color: #D9A441; background: rgba(217,164,65,.08); }
.ml-focus-item.lv-cool { opacity: .82; }
.ml-focus-rank {
  display: inline-flex; align-items: center; justify-content: center;
  width: 20px; height: 20px; border-radius: 50%;
  background: var(--accent, #0E7490); color: #fff;
  font-size: var(--fs-2xs); font-weight: 700;
}
.ml-focus-item.lv-warm .ml-focus-rank { background: #D9A441; }
.ml-focus-item.lv-cool .ml-focus-rank { background: #98A2B3; }
.ml-focus-name { font-size: var(--fs-md); font-weight: 700; }
.ml-focus-meta { font-size: var(--fs-xs); color: var(--muted, #6B7280); }
.ml-focus-note {
  margin-top: 9px; font-size: var(--fs-2xs); color: var(--muted, #6B7280); line-height: 1.6;
}

/* ---- 主线卡 ---- */
.ml-card {
  border: 1px solid var(--border, #E3E7EB);
  border-left-width: 4px;
  border-radius: 9px;
  padding: 11px 13px 12px;
  margin-bottom: 11px;
  background: var(--panel, #fff);
}
.ml-card.lv-hot  { border-left-color: var(--accent, #0E7490); background: rgba(14,116,144,.035); }
.ml-card.lv-warm { border-left-color: #D9A441; }
.ml-card.lv-cool { border-left-color: #C4CBD4; }

.ml-head { display: flex; align-items: center; flex-wrap: wrap; gap: 9px; margin-bottom: 8px; }
.ml-rating {
  display: inline-block; padding: 2px 9px; border-radius: 4px;
  font-size: var(--fs-2xs); font-weight: 700; letter-spacing: .3px;
  border: 1px solid transparent;
}
.ml-rating.lv-hot  { background: var(--accent, #0E7490); color: #fff; }
.ml-rating.lv-warm { background: #D9A441; color: #fff; }
.ml-rating.lv-cool { background: #EDF1F5; color: #5B6675; border-color: #D6DCE3; }
.ml-sector { font-size: var(--fs-lg); letter-spacing: .2px; }
.ml-why { font-size: var(--fs-xs); color: var(--muted, #6B7280); }
.ml-stats { margin-left: auto; font-size: var(--fs-xs); color: var(--muted, #6B7280); }
.ml-stats b { color: var(--text, #1F2933); font-size: var(--fs-sm); }

.ml-row { display: flex; gap: 9px; padding: 5px 0; border-top: 1px dashed var(--border, #EDF1F5); align-items: flex-start; }
.ml-row:first-of-type { border-top: none; }
.ml-k {
  flex: 0 0 62px; font-size: var(--fs-xs); color: var(--muted, #6B7280);
  padding-top: 3px; text-align: right; white-space: nowrap;
}
.ml-v { flex: 1; display: flex; flex-wrap: wrap; gap: 5px 9px; align-items: center; font-size: var(--fs-sm); }
.ml-num { font-size: var(--fs-xs); font-weight: 600; }
.ml-num-sm { font-size: var(--fs-2xs); font-weight: 500; opacity: .9; }
.ml-tag {
  display: inline-block; padding: 1px 7px; border-radius: 3px;
  font-size: var(--fs-2xs); background: #EDF1F5; color: #5B6675;
}
.ml-tag-leader { background: rgba(14,116,144,.13); color: var(--accent, #0E7490); font-weight: 600; }
.ml-star { color: #D9A441; font-weight: 700; }
.ml-row-fwd { background: rgba(217,164,65,.06); border-radius: 5px; padding: 6px 7px; margin-top: 2px; }
.ml-branches { gap: 6px; }
.ml-branch {
  display: inline-flex; align-items: center; gap: 4px;
  padding: 2px 8px; border-radius: 11px;
  background: #F2F5F8; border: 1px solid var(--border, #E3E7EB);
  font-size: var(--fs-2xs); color: #47515E;
}
.ml-branch i {
  font-style: normal; font-weight: 700; color: var(--accent, #0E7490);
  font-size: var(--fs-2xs);
}
"""

marker = '主线研判（今日看板）2026-09-25'
t = open(P, encoding='utf-8').read()
if marker in t:
    print('ALREADY_PRESENT  跳过（样式已存在）')
else:
    with open(P, 'a', encoding='utf-8') as f:
        f.write(CSS)
    t2 = open(P, encoding='utf-8').read()
    print('APPENDED  app.css', len(t), '->', len(t2), 'bytes')
    print('新增行数:', len(CSS.splitlines()))
# 自检
t3 = open(P, encoding='utf-8').read()
for k in ['.ml-focus', '.ml-card', '.ml-rating.lv-hot', '.ml-row-fwd', '.ml-branch']:
    print('  自检 %-20s %d' % (k, t3.count(k)))
