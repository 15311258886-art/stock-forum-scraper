"""端到端真实联网采集验证：跑一次 live 采集并输出多维统计。"""
from __future__ import annotations

import sys
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from overseas.collect import collect  # noqa: E402
from viewer.db import Database  # noqa: E402


def main() -> None:
    db = Database()
    t0 = time.time()
    res = collect(db=db, include_seed=True, reset=True, live=True)
    dur = time.time() - t0
    print("=" * 80)
    print(f"真实联网采集完成，耗时 {dur:.1f}s")
    print("=" * 80)
    if isinstance(res, dict):
        for k, v in res.items():
            print(f"  {k}: {v if not isinstance(v, list) else f'list[{len(v)}]'}")

    news = db.conn.execute("SELECT region, topic, source_name FROM overseas_news").fetchall()
    print(f"\n新闻总数: {len(news)}")
    print("区域分布:", dict(Counter(r["region"] for r in news).most_common()))
    print("主题分布:", dict(Counter(r["topic"] for r in news).most_common()))
    print("来源数:", len(set(r["source_name"] for r in news)))

    hs = db.conn.execute("SELECT key, title FROM overseas_hotspots").fetchall()
    print(f"\n热点数: {len(hs)}")
    for h in hs[:14]:
        print("   ·", h["key"], "|", (h["title"] or "")[:56])

    st = db.conn.execute("SELECT status, COUNT(*) n FROM overseas_sources_status GROUP BY status").fetchall()
    print("\n数据源状态:", {r["status"]: r["n"] for r in st})
    db.close()


if __name__ == "__main__":
    main()
