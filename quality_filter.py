# -*- coding: utf-8 -*-
"""
质量过滤（2026-09-25 王总要求）
  ① 剔除"非人轴"来源的噪声：东财 / 同花顺 / 集思录（帖子回溯不到具体的人）
  ② 剔除滞后 > 5 天的信息（滞后太久 = 噪声，干扰判断）
做法：加标记列 axis / stale，前端默认隐藏；不物理删除（可回滚、留证据）。
可重复执行（幂等）。
"""
import sqlite3
import os
import json
import datetime as dt
import shutil
import sys

B = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(B, 'viewer', 'app.db')
TODAY = dt.date.today()
STALE_DAYS = int(os.environ.get('STALE_DAYS', '5'))

# ---- 抓取轴定义 ----
# person     : 数据来自"人"的主页/维度，可回溯到具体的人
# person_id  : 有该人的身份 ID，可反查其主页，但当前入库的是帖子
# post       : 纯帖子轴，回溯不到具体的人（噪声来源）
AXIS = {
    'taoguba':   'person',
    'xueqiu':    'person',
    'sina':      'person_id',
    'eastmoney': 'post',
    'ths':       'post',
    'jisilu':    'post',
    'moer':      'post',
}
# 需要剔除（axis=post）的站点
DROP_SITES = [s for s, a in AXIS.items() if a == 'post']


def p(*a):
    print(*a, flush=True)


def parse_day(v):
    """取日期部分，返回 date 或 None（空/脏值均返回 None）"""
    if v is None:
        return None
    s = str(v).strip()
    if len(s) < 10:
        return None
    try:
        return dt.date.fromisoformat(s[:10])
    except ValueError:
        return None


def main():
    if not os.path.exists(DB):
        p('DB 不存在:', DB)
        return 1

    # ---------- 0) 备份 ----------
    stamp = dt.datetime.now().strftime('%Y%m%d-%H%M%S')
    bak = DB + '.bak.before-qualityfilter-' + stamp
    s = sqlite3.connect(DB)
    d = sqlite3.connect(bak)
    s.backup(d)
    d.close(); s.close()
    p('[0] 已备份 ->', os.path.basename(bak), '%.1f MB' % (os.path.getsize(bak) / 1048576))

    con = sqlite3.connect(DB, timeout=30)
    cur = con.cursor()

    # ---------- 1) 加列 ----------
    p()
    p('[1] 加标记列 axis / stale')
    for t in ('posts', 'news', 'trades'):
        cols = [r[1] for r in cur.execute('PRAGMA table_info(%s)' % t)]
        for c, ctype in (('axis', 'TEXT'), ('stale', 'INTEGER')):
            if c not in cols:
                cur.execute('ALTER TABLE %s ADD COLUMN %s %s' % (t, c, ctype))
                p('   + %s.%s' % (t, c))
        # 索引加速
        cur.execute('CREATE INDEX IF NOT EXISTS idx_%s_axis ON %s(axis, stale)' % (t, t))
    # players 也标轴
    pcols = [r[1] for r in cur.execute('PRAGMA table_info(players)')]
    if 'axis' not in pcols:
        cur.execute('ALTER TABLE players ADD COLUMN axis TEXT')
        p('   + players.axis')
    for sc in ('symbol', 'author'):
        if sc in [r[1] for r in cur.execute('PRAGMA table_info(positions)')] and 'axis' not in [r[1] for r in cur.execute('PRAGMA table_info(positions)')]:
            cur.execute('ALTER TABLE positions ADD COLUMN axis TEXT')
            cur.execute('ALTER TABLE positions ADD COLUMN stale INTEGER')
            p('   + positions.axis / positions.stale')
            break
    con.commit()

    # ---------- 2) 打标：posts / news / trades ----------
    p()
    p('[2] 打标（轴 + 时效）')
    TCOL = {'posts': 'post_time', 'news': 'publish_time', 'trades': 'post_time'}
    stat = {}
    for t, tcol in TCOL.items():
        rows = cur.execute('SELECT id, site, %s FROM %s' % (tcol, t)).fetchall()
        keep, drop_axis, drop_stale, drop_both = 0, 0, 0, 0
        for rid, site, tv in rows:
            axis = AXIS.get((site or '').strip(), 'post')
            d0 = parse_day(tv)
            stale = 1
            if d0 is not None:
                lag = (TODAY - d0).days
                stale = 1 if lag > STALE_DAYS else 0
            # 无时间也视为 stale（无法证实时效）
            if axis == 'post' and stale:
                drop_both += 1
            elif axis == 'post':
                drop_axis += 1
            elif stale:
                drop_stale += 1
            else:
                keep += 1
            cur.execute('UPDATE %s SET axis=?, stale=? WHERE id=?' % t, (axis, stale, rid))
        con.commit()
        stat[t] = dict(总=len(rows), 保留=keep, 剔非人轴=drop_axis, 剔超期=drop_stale, 两者都占=drop_both)
        p('  %-7s 总=%-5s 保留=%-5s 剔(非人轴)=%-5s 剔(超期)=%-5s' %
          (t, len(rows), keep, drop_axis + drop_both, drop_stale + drop_both))

    # ---------- 2b) 作者级机器号打标（2026-10-09 A：过滤"资讯号/播报号"污染）----------
    # 站点级 AXIS 名单只能处理"整站回溯不到人"的来源；人站（sina/xueqiu/taoguba）里仍可能
    # 混入**机器运营号**（如"新浪财经客"），其帖子会被当真人观点进入共识/共持统计。
    # 这里按 scraper/bot_filter.BOT_NAMES（作者级确证名单）把这些作者的 posts/trades/positions
    # 的 axis 置为 'bot' —— 前端默认只显示 axis IN ('person','person_id')，故自动隐藏。
    p()
    p('[2b] 作者级机器号打标')
    try:
        from scraper.bot_filter import BOT_NAMES  # 与解析器共用同一份名单
    except Exception as e:  # noqa: BLE001
        BOT_NAMES = {}
        p('   跳过（无法导入 bot_filter）:', e)
    if BOT_NAMES:
        n_bot = 0
        for t, acol in (('posts', 'author'), ('trades', 'author')):
            cols = [r[1] for r in cur.execute('PRAGMA table_info(%s)' % t)]
            if acol not in cols:
                continue
            for name in BOT_NAMES:
                cur.execute('UPDATE %s SET axis=? WHERE %s=?' % (t, acol), ('bot', name))
                n_bot += cur.rowcount
        pcols = [r[1] for r in cur.execute('PRAGMA table_info(positions)')]
        if 'player' in pcols:
            for name in BOT_NAMES:
                cur.execute('UPDATE positions SET axis=? WHERE player=?', ('bot', name))
                n_bot += cur.rowcount
        con.commit()
        p('  [2b] 机器号 %d 个，命中并改写 axis=bot 的行数 = %d' % (len(BOT_NAMES), n_bot))
        p('       名单: ' + '、'.join(sorted(BOT_NAMES)))

    # ---------- 3) 打标：players / positions ----------
    p()
    p('[3] 打标 选手 与 持仓')
    for site, axis in AXIS.items():
        cur.execute('UPDATE players SET axis=? WHERE site=?', (axis, site))
        try:
            cur.execute('UPDATE positions SET axis=? WHERE site=?', (axis, site))
        except sqlite3.OperationalError:
            pass
    con.commit()

    # positions 的时效：post_time 空 → stale=1（无法证实建仓日）
    try:
        rows = cur.execute('SELECT id, post_time FROM positions').fetchall()
        for pid, tv in rows:
            d0 = parse_day(tv)
            stale = 1
            if d0 is not None and (TODAY - d0).days <= STALE_DAYS:
                stale = 0
            cur.execute('UPDATE positions SET stale=? WHERE id=?', (stale, pid))
        con.commit()
        n_keep = cur.execute('SELECT COUNT(*) FROM positions WHERE stale=0').fetchone()[0]
        n_all = cur.execute('SELECT COUNT(*) FROM positions').fetchone()[0]
        p('  positions: 总 %s | 时效可信 %s | 待补日期 %s' % (n_all, n_keep, n_all - n_keep))
    except sqlite3.OperationalError as e:
        p('  positions 打标跳过:', e)

    # ---------- 4) 汇总 ----------
    p()
    p('[4] 结果汇总（前端默认只看 axis in (person,person_id) 且 stale=0）')
    for t in ('posts', 'news', 'trades'):
        n_all = cur.execute('SELECT COUNT(*) FROM %s' % t).fetchone()[0]
        n_show = cur.execute(
            "SELECT COUNT(*) FROM %s WHERE axis IN ('person','person_id') AND stale=0" % t).fetchone()[0]
        p('  %-7s 总=%-5s 展示=%-5s 隐藏=%s' % (t, n_all, n_show, n_all - n_show))
        for r in cur.execute(
                "SELECT axis, stale, COUNT(*) n FROM %s GROUP BY axis, stale ORDER BY axis, stale" % t):
            p('      axis=%-10s stale=%s  n=%s' % (r[0], r[1], r[2]))

    # 参考日
    p()
    p('  基准日 = %s  阈值 = 滞后 > %d 天视为噪声' % (TODAY, STALE_DAYS))

    con.close()
    p()
    p('[OK] 完成')
    return 0


if __name__ == '__main__':
    sys.exit(main())
