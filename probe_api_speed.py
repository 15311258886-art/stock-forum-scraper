# -*- coding: utf-8 -*-
"""面板加载接口的耗时基线探针（只读 GET，不触发写库/备份/AI 任务）。

用途：进哥 2026-10-30 提出「整体按我的复盘节奏重新优化」，需要先量清楚
「点开这个面板要等多久」的真实分布，而不是凭日志猜。

口径：
  · 每个接口连打 2 次，取第 2 次（热态）为主，第 1 次记冷态；
  · 单次超时 25s，超时按「≥25s」记录，不重试（重试会把慢的洗成快的）；
  · 输出按热态降序，方便一眼看出还有哪些「点开就要等」。
"""
import json
import sys
import time
import urllib.request
import urllib.error

BASE = "http://127.0.0.1:8765"
TIMEOUT = 25

PATHS = [
    "/api/version",
    "/api/dashboard",
    "/api/summary",
    "/api/today_decision",
    "/api/stocks",
    "/api/sentiment",
    "/api/watch_list",
    "/api/quant",
    "/api/etf",
    "/api/lhb",
    "/api/hotspots",
    "/api/backtest",
    "/api/screener",
    "/api/lowbox_breakouts",
    "/api/screener_decline",
    "/api/board_flow_intraday?kind=industry&top=16",
    "/api/board_flow_intraday?kind=concept&top=16",
    "/api/board_flow_snapshot",
    "/api/board_flow_range",
    "/api/prediction/today",
    "/api/prediction/scores",
    "/api/harvest",
    "/api/avoid",
    "/api/persons",
    "/api/smart_money",
    "/api/resonance",
    "/api/selfpick",
    "/api/evidence",
    "/api/freshness",
]


def one(path):
    url = BASE + path
    t0 = time.time()
    try:
        req = urllib.request.Request(url, headers={"X-Probe": "1"})
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            body = r.read()
        return time.time() - t0, len(body), ""
    except urllib.error.HTTPError as e:
        return time.time() - t0, 0, "HTTP%d" % e.code
    except Exception as e:  # noqa: BLE001
        return time.time() - t0, 0, type(e).__name__


def main():
    rows = []
    for p in PATHS:
        cold, n, err = one(p)
        time.sleep(0.2)
        hot, n2, err2 = one(p)
        rows.append({"path": p, "cold": round(cold, 2), "hot": round(hot, 2),
                     "bytes": n2 or n, "err": err2 or err})
        print("%8.2fs hot | %8.2fs cold | %9d B | %s %s"
              % (hot, cold, n2 or n, p, err2 or err), flush=True)
    rows.sort(key=lambda r: -r["hot"])
    print("\n=== 热态 Top 8（点开就要等的）===")
    for r in rows[:8]:
        print("%8.2fs  %s" % (r["hot"], r["path"]))
    with open("_api_speed.json", "w", encoding="utf-8") as fh:
        json.dump(rows, fh, ensure_ascii=False, indent=1)
    slow = [r for r in rows if r["hot"] >= 1.0]
    print("\n热态 ≥1s 的接口 %d 个 / 共 %d 个" % (len(slow), len(rows)))


if __name__ == "__main__":
    main()
