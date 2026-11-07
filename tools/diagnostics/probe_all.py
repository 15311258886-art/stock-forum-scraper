"""全站取数点体检（只读接口，串行，逐条计时）。

用法：
  python _probe_all.py            # 测全部
  python _probe_all.py <关键字>   # 只测匹配的路径

判定：
  OK      <1.5s
  SLOW    1.5~8s
  BAD     8~30s
  DEAD    >30s 或异常 / 非 200 / 返回体为空或 ok=false
"""
import json
import sys
import time
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:8765"
OP = urllib.request.build_opener(urllib.request.ProxyHandler({}))

# 只读接口清单（写操作/带副作用的 POST 不在体检范围）
READ = [
    "/api/version", "/api/refresh_status", "/api/scheduler/status", "/api/freshness",
    "/api/market_freshness",
    "/api/dashboard", "/api/summary", "/api/info_brief",
    "/api/stocks", "/api/sentiment", "/api/operating", "/api/players", "/api/persons",
    "/api/pillars", "/api/pillars/status", "/api/backtest?source=pillars",
    "/api/resonance", "/api/selfpick", "/api/avoid",
    "/api/quant", "/api/quant/recipe", "/api/etf", "/api/kronos", "/api/kronos/status",
    "/api/hotspots", "/api/overseas_hotspots", "/api/overseas_sentiment",
    "/api/overseas_sources", "/api/geo_events", "/api/global_market",
    "/api/lhb", "/api/lhb_sectors", "/api/niusan", "/api/star_moves", "/api/smart_money",
    "/api/dragon_map", "/api/moneyflow_rank",
    "/api/board_flow_snapshot", "/api/board_flow_dates", "/api/board_flow_range",
    "/api/board_flow_intraday",
    "/api/watch_list", "/api/watch_alerts", "/api/watch_brief", "/api/watchdesk",
    "/api/strategies", "/api/signal/backtest", "/api/extref_signals", "/api/extref_backtest",
    "/api/blogger/facets", "/api/blogger/overlap", "/api/blogger/perf", "/api/blogger/stats",
    "/api/bili/consensus", "/api/bili/methods", "/api/bili/pile",
    "/api/douyin/stances",
    "/api/contest/rounds", "/api/contest/detail",
    "/api/lowbox_breakouts",
    "/api/screener/themes", "/api/multiquote_preset", "/api/kb/list",
    "/api/today_decision", "/api/public_records", "/api/evolve/status",
    "/api/warehouse/stats",
]

# 2026-10-29 复查修正：以下路径**只有 POST**（或必须带业务参数），用 GET 探测必然 404/400，
# 之前每轮体检都把它们算成 DEAD，把「真实 DEAD」淹没在噪声里（连着几轮都以为这 7 个是坏的，
# 实际逐个核实全是 POST 接口，前端调用完全正常）。这里单列，报告里只作提示、不计入 DEAD。
POST_ONLY = {
    "/api/brief": "前端仅出现在路径→名称映射表里，非实际请求",
    "/api/overseas": "POST（需 confirm:true 才重建，GET 404）",
    "/api/branch_detail": "GET 但必须带 branch 参数，缺参返回 400",
    "/api/douyin": "POST",
    "/api/rate/snapshot": "POST（前端带 rows 秒回；不带会重跑全市场榜，约 35s）",
    "/api/rate/buypoint": "POST（需 rows/参数，缺参 400）",
    "/api/login_check": "POST（登录态检测）",
}


def judge(seconds, code, body):
    if code != 200:
        return "DEAD"
    if body is None:
        return "DEAD"
    try:
        obj = json.loads(body)
        if isinstance(obj, dict):
            if obj.get("ok") is False:
                return "DEAD"
            if obj.get("computing") is True:
                return "WAIT"
    except Exception:  # noqa: BLE001
        pass
    if seconds >= 30:
        return "DEAD"
    if seconds >= 8:
        return "BAD"
    if seconds >= 1.5:
        return "SLOW"
    return "OK"


def one(path, timeout=45):
    t = time.time()
    code, body, err = None, None, None
    try:
        with OP.open(BASE + path, timeout=timeout) as r:
            code = r.status
            body = r.read()
    except urllib.error.HTTPError as e:
        code, body = e.code, e.read()
    except Exception as e:  # noqa: BLE001
        err = type(e).__name__
    dt = time.time() - t
    tag = judge(dt, code, body)
    if err:
        tag = "DEAD"
    size = len(body) if body else 0
    return {"path": path, "tag": tag, "sec": round(dt, 2), "code": code,
            "bytes": size, "err": err}


def main():
    kw = sys.argv[1] if len(sys.argv) > 1 else ""
    todo = [p for p in READ if kw.lower() in p.lower()]
    rows = []
    for i, p in enumerate(todo, 1):
        r = one(p)
        rows.append(r)
        flag = {"OK": "  ", "SLOW": "  ~", "BAD": " !!", "DEAD": " XX", "WAIT": " .."}[r["tag"]]
        print("%s %-42s %-4s %7.2fs %8sB %s" % (
            flag, r["path"], r["tag"], r["sec"], r["bytes"], r["err"] or ""), flush=True)
    print("\n=== 汇总 ===")
    for tag in ("DEAD", "BAD", "SLOW", "WAIT", "OK"):
        sub = [r for r in rows if r["tag"] == tag]
        if sub:
            print("%-5s %2d 个：%s" % (tag, len(sub), ", ".join(x["path"] for x in sub)))
    print("总耗时 %.1fs / %d 个" % (sum(r["sec"] for r in rows), len(rows)))
    if not kw:
        print("\n（以下 %d 个为 POST/需参接口，不在 GET 体检范围，不计入 DEAD）" % len(POST_ONLY))
        for p, why in POST_ONLY.items():
            print("  · %-26s %s" % (p, why))
    with open("_probe_result.json", "w", encoding="utf-8") as f:
        json.dump(rows, f, ensure_ascii=False, indent=1)


if __name__ == "__main__":
    main()
