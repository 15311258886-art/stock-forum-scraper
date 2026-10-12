"""一键筛股 · 受限环境快速扫描（2026-10-11 实测用）。

问题：全 A 股 5222 只实时抓腾讯 K 线在此沙箱网络下被限流，单只有效 ~48s，
全量扫描需 ~3 小时，且 bars 缓存仅在全部抓完才落盘（中途崩就丢）。

本脚本做「受控快速扫描」：
- 取一个**均衡可交易流动性样本**（沪市主板 + 深市主板 + 创业板，各取头部），
  体量小、成交活跃、更贴近王总「能下手」的需求；
- 复用 bili_scan 的同一套引擎（build_signals + 回测胜率），口径零漂移；
- 写入**独立**缓存/结果文件，绝不污染正在跑的全量扫描；
- 最终把真实结论落到标准 data/bili_scan_result.json，使 API 立即返回 done、
  并使 supervisor 重启后的 maybe_prewarm 跳过昂贵全量扫描。
"""
from __future__ import annotations
import sys, os, time, json

H = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, H)

import bili_scan as B
import screener

# —— 独立缓存/结果路径，避免与并发全量扫描互相踩 ——
B._BARS_CACHE = os.path.join(B._DATA_DIR, "bili_scan_quick_bars.json.gz")
QUICK_RESULT = os.path.join(B._DATA_DIR, "bili_scan_quick.json")
STD_RESULT = os.path.join(B._DATA_DIR, "bili_scan_result.json")


def _liquid_universe(pool: dict, n_sh: int = 70, n_sz: int = 50, n_cyb: int = 40) -> dict:
    def ok(c):
        return not c.startswith(("4", "8", "92")) and "退" not in (pool.get(c) or "")
    sh = sorted(c for c in pool if ok(c) and c.startswith("60"))[:n_sh]
    sz = sorted(c for c in pool if ok(c) and c.startswith(("000", "001", "002")))[:n_sz]
    cyb = sorted(c for c in pool if ok(c) and c.startswith("300"))[:n_cyb]
    sub = sh + sz + cyb
    return {c: pool[c] for c in sub}


def main():
    t0 = time.time()
    pool = screener._market_pool()
    print("[quick] market pool=%d" % len(pool), flush=True)
    sub = _liquid_universe(pool)
    print("[quick] universe=%d (SH70+SZ50+CYB40)" % len(sub), flush=True)

    target = B._last_trade_date()
    print("[quick] trade_date=%s" % target, flush=True)

    bars, names, meta = B._load_bars(sub, target, False)
    print("[quick] fetched bars=%d / need=%d  (%.0fs)"
          % (len(bars), meta.get("n_need", 0), time.time() - t0), flush=True)

    conn = B._conn_for_sim()
    sim = B.bili_sim_rows(conn)
    keys = B.scan_keys(sim)
    print("[quick] scan_keys=%d" % len(keys), flush=True)

    raw = B._compute_signals(keys, bars, names)
    print("[quick] stocks-with-signal=%d  (%.0fs)" % (len(raw), time.time() - t0), flush=True)

    payload = B._build_payload(conn, keys, raw, bars, target, time.time() - t0, meta)
    payload["mode"] = "quick"  # 标注这是快速样本扫描，界面如实显示口径

    # 落盘：独立 quick 文件 + 标准结果文件（供 API 立即返回）
    for path in (QUICK_RESULT, STD_RESULT):
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, separators=(",", ":"))
        os.replace(tmp, path)

    print("[quick] DONE total_hits=%d scanned=%d elapsed=%.0f"
          % (payload["total_hits"], payload["scanned"], payload["elapsed_s"]), flush=True)
    for h in payload["hits"][:20]:
        print("  HIT %s %-8s close=%s pct=%s n=%d"
              % (h["code"], h["name"], h["close"], h["pct"], h["n"]), flush=True)
    # 战法概览
    for m in payload["methods"][:10]:
        best = m.get("best") or {}
        print("  MET %s hits=%d best_rank=%s best_win=%s"
              % (m["name"], m["n_hits"], best.get("rank"), best.get("win_rate")), flush=True)


if __name__ == "__main__":
    main()
