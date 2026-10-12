"""逐源实测新增数据源的真实产出（条目数 / 状态 / 耗时 / 样例）。"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from overseas.sources import fetch_source, load_overseas_config, sources_of  # noqa: E402

NEW_KEYS = {
    "hackernews", "techcrunch", "theverge", "arstechnica", "wired", "engadget",
    "zdnet", "slashdot", "nature", "science_news", "phys_org", "live_science",
    "new_scientist", "mit_tech_review", "cnbc_economy", "cnbc_tech", "fortune",
    "nasdaq_news", "peertube", "github_trending",
    "zaobao_world", "zaobao_sea", "zaobao_singapore",
}


def main() -> None:
    data = load_overseas_config()
    print("=" * 84)
    print(f"YAML 解析 OK | sources={len(data['sources'])} "
          f"| region_weights={data['region_weights']} | topics={len(data.get('topics') or {})}")
    print("=" * 84)
    total = 0
    bad = []
    for cfg in sources_of(data, enabled_only=True):
        if cfg.get("key") not in NEW_KEYS:
            continue
        r = fetch_source(cfg, timeout=10)
        total += len(r.items)
        flag = "✅" if r.items else "⚠️ "
        print(f"  {flag} {r.key:<18} {r.status:<12} n={len(r.items):<4} {r.latency:>5.1f}s  {r.error[:56]}")
        if r.items:
            print(f"       └ {r.items[0].title[:76]}")
        else:
            bad.append(r.key)
    print("=" * 84)
    print(f"新增源合计条目: {total}")
    if bad:
        print("无条目的源:", bad)


if __name__ == "__main__":
    main()
