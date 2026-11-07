"""第二轮体检：带真实参数的「下钻」取数点（点开一只股票/一个板块时同时打的那一批）。

用法：python _probe_drill.py [关键字]
"""
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

BASE = "http://127.0.0.1:8765"
OP = urllib.request.build_opener(urllib.request.ProxyHandler({}))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass


def get(path, timeout=45):
    t = time.time()
    code, body, err = None, None, None
    try:
        with OP.open(BASE + path, timeout=timeout) as r:
            code, body = r.status, r.read()
    except urllib.error.HTTPError as e:
        code, body = e.code, e.read()
    except Exception as e:  # noqa: BLE001
        err = type(e).__name__
    return code, body, time.time() - t, err


def post(path, payload, timeout=90):
    t = time.time()
    code, body, err = None, None, None
    data = json.dumps(payload).encode()
    req = urllib.request.Request(BASE + path, data=data,
                                headers={"Content-Type": "application/json"}, method="POST")
    try:
        with OP.open(req, timeout=timeout) as r:
            code, body = r.status, r.read()
    except urllib.error.HTTPError as e:
        code, body = e.code, e.read()
    except Exception as e:  # noqa: BLE001
        err = type(e).__name__
    return code, body, time.time() - t, err


def q(**kw):
    return "?" + urllib.parse.urlencode({k: v for k, v in kw.items() if v is not None})


def judge(sec, code, body, err):
    if err or code != 200 or not body:
        return "DEAD"
    try:
        o = json.loads(body)
        if isinstance(o, dict):
            if o.get("ok") is False:
                return "DEAD"
            if o.get("computing") is True:
                return "WAIT"
    except Exception:  # noqa: BLE001
        pass
    return "DEAD" if sec >= 30 else ("BAD" if sec >= 8 else ("SLOW" if sec >= 1.5 else "OK"))


def main():
    kw = (sys.argv[1] if len(sys.argv) > 1 else "").lower()
    # 取真实参数
    sym = "002415"
    try:
        _, b, _, _ = get("/api/watch_list", 20)
        items = json.loads(b).get("items") or []
        if items:
            sym = items[0].get("symbol") or sym
    except Exception:  # noqa: BLE001
        pass
    sec = ""
    try:
        _, b, _, _ = get("/api/watch_list", 20)
        for it in (json.loads(b).get("items") or []):
            if it.get("sector"):
                sec = it["sector"]
                break
    except Exception:  # noqa: BLE001
        pass
    board = ""
    try:
        _, b, _, _ = get("/api/screener_boards" + q(kind="industry", pz=5), 60)
        bl = json.loads(b).get("boards") or json.loads(b).get("items") or []
        if bl:
            board = bl[0].get("name") or bl[0].get("board") or ""
    except Exception:  # noqa: BLE001
        pass

    print("参数：symbol=%s sector=%s board=%s" % (sym, sec or "(空)", board or "(空)"), flush=True)

    cases = [
        ("个股报价", "/api/quote" + q(symbol=sym), None),
        ("个股行情快取", "/api/market_cache/quote" + q(symbol=sym), None),
        ("日K线", "/api/kline" + q(symbol=sym, period="day", limit=250), None),
        ("周K线", "/api/kline" + q(symbol=sym, period="week", limit=120), None),
        ("个股概览", "/api/stock" + q(symbol=sym), None),
        ("F10资料", "/api/stock_f10" + q(symbol=sym), None),
        ("换手率", "/api/stock_turnover" + q(symbol=sym), None),
        ("个股诊断", "/api/diag" + q(symbol=sym), None),
        ("估值", "/api/valuation" + q(symbol=sym), None),
        ("个股新闻", "/api/news_stock" + q(symbol=sym), None),
        ("个股资金流", "/api/moneyflow_stock" + q(symbol=sym), None),
        ("研报评级", "/api/research_rating" + q(symbol=sym), None),
        ("博主覆盖", "/api/blogger/perf" + q(code=sym), None),
        ("共识持仓(个股)", "/api/consensus" + q(min_holders=1, symbol=sym), None),
        ("板块成分", "/api/board" + q(code="BK0475", kind="industry", name=""), None),
        ("分支详情", "/api/branch_detail" + q(sector=sec or "半导体"), None),
        ("多资产查询", "/api/multiquote_lookup" + q(symbol="600519"), None),
        ("人物详情", "/api/person" + q(name="赵老哥"), None),
        ("选手详情", "/api/player" + q(name=""), None),
        ("共识持仓", "/api/consensus" + q(min_holders=2), None),
        ("共同持有", "/api/cohold_recent" + q(days=10, min_holders=2), None),
        ("详情列表", "/api/detail_list" + q(kind="post", limit=1000), None),
        ("选股器", "/api/screener" + q(page=1, pz=50, sort="change_pct", order="desc"), None),
        ("选股器板块", "/api/screener_boards" + q(kind="industry", pz=90), None),
        ("板块成分股", "/api/screener_board_members" + q(code="BK0475", num=100), None),
        ("跌幅榜", "/api/screener_decline" + q(days=5, down=10, limit=80, sort="down"), None),
        ("突破箱体", "/api/screener_lowbox" + q(page=1, pz=50), None),
        ("换手榜", "/api/screener_turnover_top" + q(days=5, limit=50), None),
        ("底部形态", "/api/screener_bottom" + q(page=1, pz=50), None),
        ("入手概率时间线", "/api/rate/timeline" + q(days_window=30, per_day=5), None),
        ("外部参考采集", "/api/extref_collect" + q(src_key=""), None),
        ("板块分时", "/api/board_flow_intraday" + q(kind="industry", top=16), None),
        ("板块资金快照", "/api/board_flow_snapshot" + q(kind="industry", pz=60), None),
        ("板块资金区间", "/api/board_flow_range" + q(kind="industry", start="2026-09-01"), None),
        ("主题库", "/api/screener/themes", None),
        ("Kronos状态", "/api/kronos/status", None),
        ("博主重叠", "/api/blogger/overlap", None),
        ("低箱突破(旧)", "/api/lowbox_breakouts", None),
        ("当前讨论股票", "/api/stocks", None),
        ("AI选股(POST)", "/api/nl_screener", {"q": "找出近5日放量的半导体股"}),
    ]
    todo = [c for c in cases if kw in c[0].lower() or kw in c[1].lower()]
    rows = []
    for name, path, payload in todo:
        if payload is not None:
            code, body, sec_, err = post(path, payload)
        else:
            code, body, sec_, err = get(path)
        tag = judge(sec_, code, body, err)
        note = ""
        if body:
            try:
                o = json.loads(body)
                if isinstance(o, dict) and o.get("error"):
                    note = str(o["error"])[:40]
                elif isinstance(o, dict):
                    note = ",".join(list(o.keys())[:4])
            except Exception:  # noqa: BLE001
                pass
        rows.append({"name": name, "path": path, "tag": tag, "sec": round(sec_, 2),
                     "code": code, "bytes": len(body or b""), "err": err, "note": note})
        flag = {"OK": "  ", "SLOW": "  ~", "BAD": " !!", "DEAD": " XX", "WAIT": " .."}[tag]
        print("%s %-14s %-52s %-4s %7.2fs %8dB %s" % (
            flag, name, path[:52], tag, sec_, len(body or b""), note), flush=True)
    print("\n=== 汇总 ===")
    for tag in ("DEAD", "BAD", "SLOW", "WAIT", "OK"):
        sub = [r for r in rows if r["tag"] == tag]
        if sub:
            print("%-5s %2d：%s" % (tag, len(sub), ", ".join(x["name"] for x in sub)))
    print("合计 %.1fs / %d 个" % (sum(r["sec"] for r in rows), len(rows)))
    with open("_probe_drill_result.json", "w", encoding="utf-8") as f:
        json.dump(rows, f, ensure_ascii=False, indent=1)


if __name__ == "__main__":
    main()
