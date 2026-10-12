# -*- coding: utf-8 -*-
"""时间伪造取证 + 按源统计口径核对。只读。"""
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
DB = ROOT / "viewer" / "app.db"
out = []

conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
conn.row_factory = sqlite3.Row

# 采集时刻（meta 里记录的）
last = conn.execute("SELECT value FROM meta WHERE key='overseas_last_collect'").fetchone()
last = last["value"] if last else ""
out.append(f"上次采集时刻 overseas_last_collect = {last}")
hhmm = last[:16]  # YYYY-MM-DD HH:MM

# ---------- A. 伪造铁证：publish_time 恰好等于采集时刻的条目数 ----------
out.append("")
out.append("=" * 74)
out.append("A) 铁证：publish_time 被填成『采集那一刻』的条目（analyze_item 的 datetime.now() 兜底）")
out.append("=" * 74)
q = ("SELECT source_key, source_name, COUNT(*) n FROM overseas_news "
     "WHERE substr(publish_time,1,16) = ? GROUP BY source_key, source_name ORDER BY n DESC")
tot_fake = 0
for r in conn.execute(q, (hhmm,)):
    tot_fake += r["n"]
    out.append(f"  {str(r['source_key'])[:22]:<24}{str(r['source_name'])[:30]:<32}{r['n']} 条")
out.append(f"  → 合计 {tot_fake} 条，publish_time 精确等于采集时刻（真实新闻不可能全部恰好发布在同一分钟）")

# 放宽到「采集当天」也算一分证据
tot_today = conn.execute(
    "SELECT COUNT(*) c FROM overseas_news WHERE substr(publish_time,1,10)=?",
    (last[:10],)).fetchone()["c"]
tot_all = conn.execute("SELECT COUNT(*) c FROM overseas_news").fetchone()["c"]
out.append(f"  → 放宽到『采集当天』：{tot_today} / {tot_all} 条（占 {round(tot_today/tot_all*100,1)}%）")

# ---------- B. 按源看时间分布：是否整源挤在同一天（= 该源不产出时间） ----------
out.append("")
out.append("=" * 74)
out.append("B) 按源看时间分布：整源挤在同一天 → 该源本身不产出时间戳")
out.append("=" * 74)
rows = conn.execute(
    "SELECT source_key, source_name, COUNT(*) n, COUNT(DISTINCT substr(publish_time,1,10)) days "
    "FROM overseas_news GROUP BY source_key, source_name ORDER BY days ASC, n DESC").fetchall()
out.append(f"  {'source_key':<24}{'name':<30}{'条数':<7}{'不同日期数'}")
for r in rows:
    flag = "   <== 全部同一天" if r["days"] <= 1 else ""
    out.append(f"  {str(r['source_key'])[:22]:<24}{str(r['source_name'])[:28]:<30}{r['n']:<7}{r['days']}{flag}")

# ---------- C. 面板 count 与实际入库数的口径差 ----------
out.append("")
out.append("=" * 74)
out.append("C) 口径差：面板 count（抓取返回数） vs 实际入库数（去重后）")
out.append("=" * 74)
actual = {r["source_key"]: r["n"] for r in conn.execute(
    "SELECT source_key, COUNT(*) n FROM overseas_news GROUP BY source_key")}
for r in conn.execute(
        "SELECT source_key, name, count, status FROM overseas_sources_status "
        "WHERE status LIKE '已接入%' ORDER BY count DESC"):
    k = r["source_key"]
    a = actual.get(k, 0)
    d = int(r["count"] or 0) - a
    tag = "" if d == 0 else ("   <== 多报 %d 条" % d if d > 0 else "   <== 少报 %d 条" % (-d))
    out.append(f"  {str(r['name'])[:26]:<28} 面板={int(r['count'] or 0):<5} 入库={a:<5}{tag}")

out.append("")
out.append("  说明：入库按 (url,title) 唯一去重（db.py:846 ON CONFLICT(url,title) DO UPDATE），")
out.append("       且 source_key 不在 UPDATE 列表中 —— 同一篇文章被多个源抓到只会留下最先写入的那个源，")
out.append("       于是『面板条数』是去重前的抓取数，必然 >= 实际入库数。")

# ---------- D. 示例条目挂真实源名的情况 ----------
out.append("")
out.append("=" * 74)
out.append("D) 示例条目挂在『真实源 key』下的情况（图例称「逐条可区分」，这些区分不了）")
out.append("=" * 74)
try:
    from overseas.seed import SEED_NEWS
    real_keys = {"eastmoney_guba", "ths", "xueqiu_hot", "cnbc", "marketwatch",
                 "seekingalpha", "hkex_news", "hkex_sehk", "aastocks", "reuters_world"}
    pairs = [(i.title[:300], i.source_key) for i in SEED_NEWS]
    hit = defaultdict(int)
    demo_prefixed = defaultdict(int)
    for r in conn.execute("SELECT source_key, title FROM overseas_news"):
        for t, k in pairs:
            if r["title"] == t and r["source_key"] == k:
                if str(k).startswith("示例源"):
                    demo_prefixed[k] += 1
                else:
                    hit[k] += 1
    out.append("  (1) 带『示例源·』前缀 —— 界面可区分：")
    for k, v in sorted(demo_prefixed.items(), key=lambda x: -x[1]):
        out.append(f"      {k}: {v} 条")
    out.append("  (2) 无任何前缀、直接挂在真实源名下 —— 界面【无法】区分：")
    for k, v in sorted(hit.items(), key=lambda x: -x[1]):
        st = conn.execute("SELECT status FROM overseas_sources_status WHERE source_key=?", (k,)).fetchone()
        out.append(f"      source_key={k}  {v} 条   （面板状态：{st['status'] if st else '无记录'}）")
except Exception as e:
    out.append(f"  失败：{e!r}")

txt = "\n".join(out)
with open(ROOT / "tools" / "_qa_source_time.txt", "w", encoding="utf-8") as f:
    f.write(txt + "\n")
print("written tools/_qa_source_time.txt")
