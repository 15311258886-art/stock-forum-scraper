"""来源审计：查清「此前列出的多个知识博主/实盘来源」到底停在哪一层。

四层分开查，不混为一谈：
  L1 声明层 —— 配置文件里写了哪些站点（以及有没有被注释掉）
  L2 能力层 —— 代码里有没有对应的解析器/适配器
  L3 事实层 —— 数据库里到底有没有真抓过（raw_pages / posts / trades / positions）
  L4 展示层 —— 界面上的"多平台"是从哪来的（真数据 or 合成示例数据）
"""
from __future__ import annotations

import io
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from viewer.db import Database  # noqa: E402

db = Database()
print("库路径：", db.path)
print()

# ---------------- L3 事实层：真实抓取痕迹 ----------------
print("=" * 72)
print("L3 事实层：数据库里到底抓到过什么（这是唯一不会被文字描述骗到的一层）")
print("=" * 72)
print("\n[raw_pages] 按 site 分组（真实抓取存档）")
rows = db.conn.execute(
    "SELECT site, COUNT(*) n, MIN(fetched_at) mn, MAX(fetched_at) mx "
    "FROM raw_pages GROUP BY site ORDER BY n DESC").fetchall()
for r in rows:
    print(f"   {r['site']:<14} {r['n']:>5} 页   {r['mn']} → {r['mx']}")
if not rows:
    print("   （空）")
print(f"   合计 {sum(r['n'] for r in rows)} 页")

for tbl, col in (("posts", "site"), ("trades", "site"), ("positions", "site")):
    print(f"\n[{tbl}] 按 {col} 分组")
    rr = db.conn.execute(
        f"SELECT {col} k, COUNT(*) n FROM {tbl} GROUP BY {col} ORDER BY n DESC").fetchall()
    for r in rr:
        print(f"   {str(r['k']):<14} {r['n']:>5} 行")
    if not rr:
        print("   （空）")

print("\n[players] 按 platform 分组（候选人池的「平台」字段）")
rr = db.conn.execute(
    "SELECT COALESCE(platform,'(空)') k, COUNT(*) n FROM players GROUP BY k ORDER BY n DESC").fetchall()
for r in rr:
    print(f"   {str(r['k']):<14} {r['n']:>5} 人")

# ---------------- 示例数据判定 ----------------
print("\n" + "=" * 72)
print("示例数据判定（决定「多平台」是真事还是合成）")
print("=" * 72)
print("  meta[demo] =", repr(db.get_meta("demo")))
print("  meta[last_ingest] =", repr(db.get_meta("last_ingest"))[:220])
for tbl, col in (("posts", "post_url"), ("trades", "post_url"),
                 ("positions", "post_url"), ("players", "source_url")):
    try:
        tot = db.conn.execute(f"SELECT COUNT(*) n FROM {tbl}").fetchone()["n"]
        demo = db.conn.execute(
            f"SELECT COUNT(*) n FROM {tbl} WHERE {col} LIKE '%example.local%'").fetchone()["n"]
        real = db.conn.execute(
            f"SELECT COUNT(*) n FROM {tbl} WHERE {col} IS NOT NULL AND {col}<>'' "
            f"AND {col} NOT LIKE '%example.local%'").fetchone()["n"]
        print(f"   {tbl:<11} 共 {tot:>4} 行：示例 {demo:>4} / 非示例 {real:>4}")
    except Exception as e:
        print(f"   {tbl}: {e}")

# ---------------- L1 声明层 ----------------
print("\n" + "=" * 72)
print("L1 声明层：配置文件里写了什么")
print("=" * 72)
cfgdir = ROOT / "config"
for p in sorted(cfgdir.glob("*")):
    if not p.is_file():
        continue
    txt = io.open(p, encoding="utf-8").read()
    print(f"\n--- {p.name} ({len(txt)} 字符) ---")
    if p.suffix in (".yaml", ".yml"):
        # 只打印非注释、非空行；注释单独统计出来
        body = [l for l in txt.splitlines() if l.strip() and not l.strip().startswith("#")]
        comments = [l for l in txt.splitlines() if l.strip().startswith("#")]
        print("  [生效行]")
        for l in body[:60]:
            print("   ", l[:140])
        if comments:
            print(f"  [被注释掉的行：{len(comments)} 条 —— 说明「写过但没启用」]")
            for l in comments[:25]:
                print("    ", l[:140])
    else:
        print(txt[:1200])

# ---------------- L2 能力层 ----------------
print("\n" + "=" * 72)
print("L2 能力层：代码里有没有对应解析器/适配器")
print("=" * 72)
ing = io.open(ROOT / "viewer" / "ingest.py", encoding="utf-8").read()
m = re.search(r"PARSER_REGISTRY\s*=\s*\{[^}]*\}", ing)
print("  ingest.py PARSER_REGISTRY =", m.group(0) if m else "未找到")
for name in ("TaogubaParser", "EastmoneyParser", "XueqiuParser", "ThsParser",
             "class .*Parser"):
    hits = re.findall(rf"class \w*{name.strip('.*')}\w*", ing)
    if hits:
        print(f"   {name:<20} -> {sorted(set(hits))}")
print("\n  各站名在源码里的出现位置（区分「真实现」与「仅显示名」）：")
for site in ("taoguba", "eastmoney", "xueqiu", "ths"):
    print(f"\n   [{site}]")
    for f in sorted(ROOT.glob("viewer/*.py")) + sorted(ROOT.glob("viewer/**/*.py")):
        if ".venv" in str(f) or "__pycache__" in str(f):
            continue
        s = io.open(f, encoding="utf-8").read()
        for i, line in enumerate(s.splitlines(), 1):
            if site in line:
                tag = "注释" if line.strip().startswith("#") else "代码"
                print(f"      {f.name}:{i} [{tag}] {line.strip()[:110]}")

# ---------------- L4 展示层 ----------------
print("\n" + "=" * 72)
print("L4 展示层：界面上「多平台」的来源")
print("=" * 72)
srv = io.open(ROOT / "viewer" / "server.py", encoding="utf-8").read()
m = re.search(r"SITE_DISPLAY_NAMES\s*=\s*\{[^}]*\}", srv)
print("  server.py SITE_DISPLAY_NAMES =", (m.group(0).replace("\n", " ") if m else "未找到"))
print("  → 这只是「代码→中文名」的映射表，有名字不等于有这个来源的抓取能力。")
print()
demo = ROOT / "viewer" / "demo_data.py"
if demo.exists():
    s = io.open(demo, encoding="utf-8").read()
    print(f"  demo_data.py 存在（{len(s)} 字符）—— 合成示例数据的生成器：")
    for i, line in enumerate(s.splitlines(), 1):
        for site in ("eastmoney", "xueqiu", "ths"):
            if site in line:
                print(f"      :{i} {line.strip()[:110]}")
                break
    print("  说明：示例数据里带着 4 个平台的名字，界面上就会显示 4 个平台——"
          "但那是**合成出来的**，不代表真的抓过。")
print()
print("=" * 72)
print("结论线索（供人工判断，不做自动结论）")
print("=" * 72)
raw_sites = sorted({r["site"] for r in db.conn.execute(
    "SELECT DISTINCT site FROM raw_pages WHERE site IS NOT NULL")})
print("  真被抓过的站点：", raw_sites or "无")
print("  配置里声明的站点：见 L1")
print("  有解析器的站点：见 L2")
print("  界面展示的站点：见 L4（可能来自合成数据）")
