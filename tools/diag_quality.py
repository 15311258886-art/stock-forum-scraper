"""数据质量诊断：找「其他」主题大头、无条目源、误匹配来源。"""
from __future__ import annotations

import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from viewer.db import Database  # noqa: E402


def main() -> None:
    db = Database()
    c = db.conn

    print("=" * 78)
    print("① 「已连通但无条目」的源")
    print("=" * 78)
    cols = [r[1] for r in c.execute("PRAGMA table_info(overseas_sources_status)")]
    print("   [status 表列名]", cols)
    for r in c.execute("SELECT * FROM overseas_sources_status WHERE status LIKE '%无条目%'"):
        d = dict(r)
        print(f"   · {d.get('source_key', d.get('key','')):<18} {d.get('name',''):<22} {(d.get('note') or '')[:40]}")

    print("\n" + "=" * 78)
    print("② 各数据源贡献的新闻量（top 20）")
    print("=" * 78)
    rows = c.execute("SELECT source_name, COUNT(*) n FROM overseas_news GROUP BY source_name ORDER BY n DESC LIMIT 20").fetchall()
    for r in rows:
        print(f"   {r['n']:>4}  {r['source_name']}")

    print("\n" + "=" * 78)
    print("③ 「其他」主题的来源分布（按来源看是谁贡献的）")
    print("=" * 78)
    rows = c.execute("SELECT source_name, COUNT(*) n FROM overseas_news WHERE topic='其他' GROUP BY source_name ORDER BY n DESC LIMIT 15").fetchall()
    for r in rows:
        print(f"   {r['n']:>4}  {r['source_name']}")

    print("\n" + "=" * 78)
    print("④ 「其他」主题样例（判断该归到哪个主题）")
    print("=" * 78)
    rows = c.execute("SELECT source_name,title FROM overseas_news WHERE topic='其他' ORDER BY RANDOM() LIMIT 22").fetchall()
    for r in rows:
        print(f"   [{r['source_name'][:16]:<16}] {r['title'][:64]}")

    print("\n" + "=" * 78)
    print("⑤ PeerTube 条目样例（判断娱乐内容占比）")
    print("=" * 78)
    rows = c.execute("SELECT title,topic FROM overseas_news WHERE source_name LIKE 'PeerTube%' LIMIT 18").fetchall()
    for r in rows:
        print(f"   [{r['topic']:<10}] {r['title'][:66]}")

    print("\n" + "=" * 78)
    print("⑥ 误匹配排查：geo_conflict 热点的证据标题")
    print("=" * 78)
    import json
    for key in ("geo_conflict", "natural_disaster"):
        row = c.execute("SELECT title,evidence FROM overseas_hotspots WHERE key=?", (key,)).fetchone()
        if not row:
            continue
        print(f"   --- {key} | {row['title'][:60]}")
        try:
            ev = json.loads(row["evidence"] or "[]")
            for e in ev[:5]:
                print(f"       · {(e.get('title') or '')[:70]}")
        except Exception:  # noqa: BLE001
            print("       (evidence 解析失败)")
    db.close()


if __name__ == "__main__":
    main()
