"""一键筛股（2026-10-11，王总需求）：把知识库战法变成「当前全市场哪些股票正走在买点上」。

王总原话：知识库一堆方式方法不用解释——「我抓取，立即抓取，抓取完之后就符合
现在目前这个K线走势的、符合这些短线战法的有哪些股票，给我一个一键筛选功能，
直接就出来了。我要最终的结论。」

口径（诚实声明，随 payload.basis 下发到界面）：
1. 战法信号 = strategy_sim.build_signals（与回测**同一份实现**，零口径漂移）对
   最新一根K线的 buy 判定；数据=腾讯前复权日K（**含开盘价**，270 根，覆盖
   MA250 / 120日新高等长窗口）。screener 的K线缓存没有开盘价，缺口/形态类
   战法离不开它，故自建缓存 data/bili_scan_bars.json.gz。
2. 「入手值得率」= 该规则在 strategy_sim_window（mid，近两年、库内候选池）的
   回测胜率。扫描范围是**全A股**，与回测候选池口径不同——胜率是「这条规则
   历史的胜率」，不是「这只票的胜率」。
3. 大盘择时类（需要指数/市场宽度上下文，不是个股信号）与止损纪律类（卖出规则）
   不参与个股扫描，如实剔除；未回测映射不到的规则不参与。
4. 盘中运行时最后一根K线是未收盘的实时K线，信号以收盘复核为准（界面标数据日期）。

性能设计（实测单线程 0.1~0.2s/票 × 5000 票 ≈ 10+ 分钟，不能点按钮现算）：
- 抓K线：24 线程 + 当日磁盘缓存（gzip）+ 失败冷却；
- 信号计算：**子进程**（python bili_scan.py --worker）内部 ProcessPoolExecutor(6)
  并行 ≈ 2~3 分钟；为什么用子进程而不是线程——纯 Python CPU 计算被 GIL 限死，
  为什么不在 server 进程里直接 multiprocessing——Windows spawn 会重导入 server
  主模块（带 DB/调度器副作用），子进程隔离最干净（同 extref 回测后台单飞模式）。
- 状态机：单飞线程 + 内存态 warming/done/error，前端 4s 轮询（同研报结论模式）。
- 每日自动刷新：scheduler 收盘后调 maybe_daily()；server 启动时 maybe_prewarm()。
"""
from __future__ import annotations

import gzip
import json
import os
import subprocess
import sys
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor

_HERE = os.path.dirname(os.path.abspath(__file__))
_DATA_DIR = os.path.join(_HERE, "data")
_BARS_CACHE = os.path.join(_DATA_DIR, "bili_scan_bars.json.gz")
_RESULT_CACHE = os.path.join(_DATA_DIR, "bili_scan_result.json")

# 参与扫描的策略 key 剔除项
_SKIP_TIMING = {"breadth_timing", "vol_regime_timing", "hist_vol_filter",
                "index_ma20_timing", "index_ma250_timing"}          # 需要指数/宽度上下文
_SKIP_STOP = {"fixed_stop_loss", "atr_trailing_stop",
              "trailing_drawdown_stop", "time_stop_20d"}            # 卖出规则
_SKIP_SCOPE = {"vol_target_sizing", "kelly_fraction_sizing",
               "dual_momentum", "monthly_rebalance"}                # 口径受限
SKIP_KEYS = _SKIP_TIMING | _SKIP_STOP | _SKIP_SCOPE

_SCAN_BARS = 270          # 覆盖 MA250 / 120日新高
_MIN_BARS = 60            # 少于 60 根不判（指标暖机不够）
_MAX_HITS = 200           # payload 最多返回的命中票数
_WORKERS_FETCH = 24
_WORKERS_CPU = 6
_FAIL_COOLDOWN = 1800

# ---------------------------------------------------------------- 状态机
_LOCK = threading.Lock()
_STATE = {"status": "idle", "phase": "", "done": 0, "total": 0, "note": "",
          "result": None, "error": None, "started_at": 0.0, "finished_at": 0.0}
_THREAD = None
_FAIL_MEM: dict = {}


def _set_state(**kw):
    with _LOCK:
        _STATE.update(kw)


def _get_state() -> dict:
    with _LOCK:
        return dict(_STATE)


# ---------------------------------------------------------------- 策略清单
def scan_keys(sim: dict) -> list:
    """参与扫描的策略 key = B站技术面方法映射 ∩ 已回测(sim) − 剔除项。保持方法内顺序。"""
    try:
        from bili_methods import METHODS
    except Exception:  # pragma: no cover
        return []
    keys, seen = [], set()
    for m in METHODS:
        if m.kind != "技术面":
            continue
        for k in m.strat:
            if k in SKIP_KEYS or k in seen or k not in sim:
                continue
            seen.add(k)
            keys.append(k)
    return keys


def method_of_key(sim: dict) -> dict:
    """策略 key → 所属方法 {key,name,kind}（B站方法词典）。"""
    try:
        from bili_methods import METHODS
    except Exception:  # pragma: no cover
        return {}
    out = {}
    for m in METHODS:
        for k in m.strat:
            out.setdefault(k, {"key": m.key, "name": m.name, "kind": m.kind})
    return out


# ---------------------------------------------------------------- K线抓取（含开盘价）
def _http_text(url: str, referer: str, timeout: int = 8) -> str:
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
        "Referer": referer})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "ignore")


def _prefix(code: str) -> str:
    return "sh" if code.startswith(("6", "9", "5")) else "sz"


_TX_KLINE = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
_TX_KLINE_ALT = "https://proxy.finance.qq.com/ifzqgtimg/appstock/app/newfqkline/get"


def _fetch_tx_full(code: str, bars: int) -> list:
    """腾讯日K（前复权）全字段含开盘价：[(date, open, high, low, close, vol)] 升序。

    qfqday 行序 = [date, open, close, high, low, volume]（本项目 2026-10-01 实测固化）。
    """
    sym = _prefix(code) + code
    for base in (_TX_KLINE, _TX_KLINE_ALT):
        try:
            txt = _http_text("%s?param=%s,day,,,%d,qfq" % (base, sym, bars), "https://gu.qq.com/")
            d = json.loads(txt)
            node = (d.get("data") or {}).get(sym) or {}
            arr = node.get("qfqday") or node.get("day") or []
            if not arr:
                continue
            out = []
            for r in arr:
                try:
                    out.append((str(r[0])[:10], float(r[1]), float(r[3]),
                                float(r[4]), float(r[2]), float(r[5])))
                except Exception:  # noqa: BLE001
                    continue
            if out:
                return out
        except Exception:  # noqa: BLE001
            continue
    return []


def _last_trade_date() -> str:
    try:
        sys.path.insert(0, _HERE)
        from screener import _last_trade_date_local
        return _last_trade_date_local()
    except Exception:  # noqa: BLE001
        import datetime as _dt
        return _dt.date.today().strftime("%Y-%m-%d")


def _bars_cache_load() -> dict:
    try:
        with gzip.open(_BARS_CACHE, "rt", encoding="utf-8") as f:
            return json.load(f)
    except Exception:  # noqa: BLE001
        return {}


def _bars_cache_save(data: dict) -> None:
    os.makedirs(_DATA_DIR, exist_ok=True)
    tmp = _BARS_CACHE + ".tmp"
    with gzip.open(tmp, "wt", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp, _BARS_CACHE)


def _load_bars(pool: dict, target: str, force: bool) -> tuple:
    """返回 (bars{code:[[d,o,h,l,c,v]...]}, scanned_names{code:name}, meta)。当日磁盘缓存。"""
    disk = {} if force else _bars_cache_load()
    if disk.get("day") != target:
        disk = {"day": target, "bars": {}, "fail": {}}
    bars = disk.get("bars") or {}
    fail = disk.get("fail") or {}
    names = {c: (n or "") for c, n in pool.items()}
    need = [c for c in pool if c not in bars]
    # 失败冷却期内的必败票直接跳过
    now = time.time()
    need = [c for c in need if now - fail.get(c, 0) > _FAIL_COOLDOWN]
    done = 0

    def _one(c):
        return c, _fetch_tx_full(c, _SCAN_BARS)

    if need:
        with ThreadPoolExecutor(max_workers=_WORKERS_FETCH) as ex:
            for c, ks in ex.map(_one, need):
                done += 1
                _set_state(phase="fetch", done=done, total=len(need),
                           note="抓取K线 %d/%d" % (done, len(need)))
                if ks and len(ks) >= _MIN_BARS:
                    bars[c] = [[r[0], round(r[1], 3), round(r[2], 3), round(r[3], 3),
                                round(r[4], 3), round(r[5], 1)] for r in ks]
                    fail.pop(c, None)
                else:
                    fail[c] = now
        _bars_cache_save({"day": target, "bars": bars, "fail": fail})
    meta = {"n_pool": len(pool), "n_need": len(need), "n_bars": len(bars)}
    return bars, names, meta


# ---------------------------------------------------------------- 信号计算（子进程并行）
def _mp_chunk(args):
    """子进程 worker：一批票 × 全部策略 key → 命中清单。必须模块顶层（Windows spawn）。"""
    keys, items = args
    try:
        import strategy_sim as sim
    except Exception:  # noqa: BLE001
        try:
            sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
            import strategy_sim as sim
        except Exception:  # noqa: BLE001
            return []
    out = []
    for code, name, raw in items:
        try:
            k = [{"d": r[0], "o": r[1], "h": r[2], "l": r[3], "c": r[4], "v": r[5]} for r in raw]
            hits = []
            for key in keys:
                try:
                    buy, _sell = sim.build_signals(key, k, None, code, name or "")
                except Exception:  # noqa: BLE001
                    continue
                if buy and buy[-1]:
                    hits.append(key)
            if hits:
                out.append([code, name, hits])
        except Exception:  # noqa: BLE001
            continue
    return out


def _compute_signals(keys: list, bars: dict, names: dict) -> list:
    """子进程并行计算，返回 [[code,name,[strat_key...]],...]。失败回退本进程串行。"""
    items = [[c, names.get(c, ""), bars[c]] for c in sorted(bars)
             if len(bars[c]) >= _MIN_BARS]
    if not items:
        return []
    nch = max(_WORKERS_CPU * 4, 1)
    size = max(1, (len(items) + nch - 1) // nch)
    chunks = [(keys, items[i:i + size]) for i in range(0, len(items), size)]
    in_path = os.path.join(_DATA_DIR, "_bili_scan_in.json.gz")
    out_path = os.path.join(_DATA_DIR, "_bili_scan_out.json")
    os.makedirs(_DATA_DIR, exist_ok=True)
    with gzip.open(in_path, "wt", encoding="utf-8") as f:
        json.dump({"keys": keys, "chunks": chunks}, f, ensure_ascii=False, separators=(",", ":"))
    cmd = [sys.executable, os.path.abspath(__file__), "--worker", in_path, out_path]
    try:
        r = subprocess.run(cmd, cwd=_HERE, timeout=1800,
                           stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        if r.returncode == 0 and os.path.exists(out_path):
            with open(out_path, "r", encoding="utf-8") as f:
                res = json.load(f)
            try:
                os.remove(out_path)
            except OSError:  # noqa: BLE001
                pass
            return res
        raise RuntimeError("worker rc=%s err=%s" % (r.returncode, (r.stderr or b"")[-300:]))
    except Exception:
        # 兜底：本进程串行（慢但保真）
        out = []
        for ch in chunks:
            out.extend(_mp_chunk(ch))
        return out


def _worker_main(in_path: str, out_path: str) -> None:
    with gzip.open(in_path, "rt", encoding="utf-8") as f:
        data = json.load(f)
    results = []
    for ch in data["chunks"]:
        results.extend(_mp_chunk(ch))
    tmp = out_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp, out_path)


# ---------------------------------------------------------------- 聚合
def _result_cache_load() -> dict | None:
    try:
        with open(_RESULT_CACHE, "r", encoding="utf-8") as f:
            d = json.load(f)
        return d if d.get("status") == "done" else None
    except Exception:  # noqa: BLE001
        return None


def _result_cache_save(d: dict) -> None:
    try:
        os.makedirs(_DATA_DIR, exist_ok=True)
        tmp = _RESULT_CACHE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, separators=(",", ":"))
        os.replace(tmp, _RESULT_CACHE)
    except Exception:  # noqa: BLE001
        pass


def _build_payload(conn, keys: list, raw_hits: list, bars: dict, target: str,
                   elapsed: float, meta: dict) -> dict:
    sim = bili_sim_rows(conn)  # {key: {name,win_rate,total_return,annual_return,rating,trades,rank}}
    mmap = method_of_key(sim)
    # 方法与规则统计
    mstat, sstat = {}, {}
    for code, name, hits in raw_hits:
        for sk in hits:
            sstat[sk] = sstat.get(sk, 0) + 1
            mk = (mmap.get(sk) or {}).get("key") or sk
            mstat[mk] = mstat.get(mk, 0) + 1

    from bili_methods import METHODS
    methods_out = []
    for m in METHODS:
        sks = [k for k in m.strat if k in sim]
        if not sks or sks[0] not in (set(keys) | set(sstat)):
            if not any(k in sstat for k in sks):
                continue
        strategies = []
        for k in m.strat:
            if k in sstat:
                s = dict(sim[k])
                s["n_hits"] = sstat[k]
                strategies.append(s)
        if not strategies:
            continue
        best = min(strategies, key=lambda s: s.get("rank") or 9999)
        methods_out.append({
            "key": m.key, "name": m.name, "kind": m.kind,
            "n_hits": mstat.get(m.key, 0),
            "best": best, "strategies": strategies,
        })
    methods_out.sort(key=lambda x: -x["n_hits"])

    # 命中票
    hits = []
    for code, name, hks in raw_hits:
        items = []
        for sk in hks:
            s = sim.get(sk)
            mm = mmap.get(sk) or {"key": sk, "name": sk, "kind": "技术面"}
            items.append({
                "mkey": mm["key"], "mname": mm["name"],
                "skey": sk, "sname": (s or {}).get("name", sk),
                "win_rate": (s or {}).get("win_rate"),
                "total_return": (s or {}).get("total_return"),
                "rating": (s or {}).get("rating"),
                "rank": (s or {}).get("rank"),
            })
        if not items:
            continue
        ks = bars.get(code) or []
        close = ks[-1][4] if ks else None
        pct = None
        if len(ks) >= 2 and ks[-2][4]:
            try:
                pct = (ks[-1][4] / ks[-2][4] - 1) * 100
            except Exception:  # noqa: BLE001
                pct = None
        ranks = [i.get("rank") for i in items if i.get("rank") is not None]
        wins = [i.get("win_rate") for i in items if i.get("win_rate") is not None]
        hits.append({
            "code": code, "name": name, "close": close,
            "pct": round(pct, 2) if pct is not None else None,
            "n": len(items), "items": items,
            "best_win": max(wins) if wins else None,
            "min_rank": min(ranks) if ranks else 9999,
        })
    hits.sort(key=lambda x: (-x["n"], x["min_rank"], -(x.get("best_win") or 0)))
    total_hits = len(hits)
    hits = hits[:_MAX_HITS]

    data_date = ""
    if bars:
        sample = next(iter(bars.values()))
        data_date = sample[-1][0] if sample else ""

    return {
        "status": "done", "date": target, "data_date": data_date,
        "universe": meta.get("n_pool", 0), "scanned": meta.get("n_bars", 0),
        "elapsed_s": round(elapsed, 1),
        "methods": methods_out, "hits": hits, "total_hits": total_hits,
        "keys_n": len(keys),
        "basis": [
            "信号判定 = 与回测同一份规则引擎（strategy_sim.build_signals）对最新一根日K的买点判定；K线为腾讯前复权日K（270根，含开盘价）。",
            "「入手值得率」= 该规则在近两年回测（库内候选池）的每笔交易胜率——是「规则的历史胜率」，不是这只票的预测；扫描范围为全A股（剔除北交所），与回测候选池口径不同。",
            "大盘择时类（需要指数/市场宽度）与止损纪律类（卖出规则）不参与个股扫描；盘中运行时最后一根K线未收盘，信号以收盘复核为准。",
        ],
    }


def bili_sim_rows(conn) -> dict:
    try:
        from bili_methods import _strat_rows
        return _strat_rows(conn)
    except Exception:  # noqa: BLE001
        return {}


# ---------------------------------------------------------------- 对外入口
def scan_payload(conn, force: bool = False) -> dict:
    """前端轮询入口。首次调用触发后台单飞扫描，返回 warming；完成后返回 done。"""
    global _THREAD
    st = _get_state()
    if st["status"] == "done" and st["result"]:
        cached = st["result"]
        if not force and cached.get("date") == _last_trade_date():
            return cached
    if st["status"] == "warming" and _THREAD and _THREAD.is_alive():
        return {"status": "warming", "phase": st.get("phase", ""),
                "done": st.get("done", 0), "total": st.get("total", 0),
                "note": st.get("note", ""), "elapsed_s": round(time.time() - st.get("started_at", 0), 0)}
    # 无线程在跑：当日缓存兜底（服务重启后内存态丢失）
    if not force:
        cached = _result_cache_load()
        if cached and cached.get("date") == _last_trade_date():
            _set_state(status="done", result=cached)
            return cached
        st = _get_state()
        if st["status"] == "done" and st["result"] and st["result"].get("date") == _last_trade_date():
            return st["result"]
    with _LOCK:
        if _THREAD and _THREAD.is_alive():
            return {"status": "warming", "note": "扫描已在进行中"}
        _THREAD = threading.Thread(target=_run_scan, args=(bool(force),), daemon=True)
        _THREAD.start()
    return {"status": "warming", "phase": "", "done": 0, "total": 0,
            "note": "扫描启动中", "elapsed_s": 0}


def _run_scan(force: bool) -> None:
    t0 = time.time()
    try:
        _set_state(status="warming", phase="fetch", done=0, total=0, error=None,
                   note="准备中", started_at=t0, result=None)
        try:
            from screener import _market_pool
            pool = _market_pool()
        except Exception as e:  # noqa: BLE001
            raise RuntimeError("全A清单获取失败：%s" % e)
        if not pool:
            raise RuntimeError("全A股清单为空（新浪源失败），请稍后重试")
        # 剔除北交所（腾讯K线必败）与退市整理
        pool = {c: n for c, n in pool.items()
                if not c.startswith(("4", "8", "92")) and "退" not in (n or "")}
        target = _last_trade_date()
        bars, names, meta = _load_bars(pool, target, force)
        _set_state(phase="compute", note="计算战法信号（%d 只，多进程并行）" % len(bars),
                   done=0, total=len(bars))
        conn = _conn_for_sim()
        keys = scan_keys(bili_sim_rows(conn))
        if not keys:
            raise RuntimeError("没有可扫描的已回测策略（strategy_sim_window 为空）")
        raw = _compute_signals(keys, bars, names)
        payload = _build_payload(conn, keys, raw, bars, target,
                                 time.time() - t0, meta)
        _result_cache_save(payload)
        _set_state(status="done", result=payload, phase="done",
                   finished_at=time.time(),
                   note="完成：%d 只命中 / 扫描 %d 只 / %.0f 秒"
                        % (payload["total_hits"], payload["scanned"], payload["elapsed_s"]))
    except Exception as e:  # noqa: BLE001
        _set_state(status="error", error=str(e), phase="error", finished_at=time.time())


def _conn_for_sim():
    """只读连接取 strategy_sim_window（独立连接，避免线程安全问题）。"""
    import sqlite3
    db = os.path.join(_HERE, "viewer", "app.db")
    try:
        con = sqlite3.connect("file:%s?mode=ro" % db.replace("\\", "/"), uri=True, timeout=15)
        con.row_factory = sqlite3.Row
        return con
    except Exception:  # noqa: BLE001
        return None


def maybe_daily() -> bool:
    """scheduler 收盘后自动刷新：交易日 15:10 后当日无结果则扫一次。"""
    try:
        target = _last_trade_date()
        import datetime as _dt
        now = _dt.datetime.now()
        if now.strftime("%Y-%m-%d") != target or now.hour < 15:
            return False
        st = _get_state()
        if st["status"] == "warming":
            return False
        cached = _result_cache_load()
        if cached and cached.get("date") == target:
            return False
        if _THREAD and _THREAD.is_alive():
            return False
        threading.Thread(target=_run_scan, args=(False,), daemon=True).start()
        return True
    except Exception:  # noqa: BLE001
        return False


def maybe_prewarm() -> None:
    """server 启动 45s 后，当日无结果则后台预热一次。"""
    def _go():
        time.sleep(45)
        try:
            target = _last_trade_date()
            cached = _result_cache_load()
            if cached and cached.get("date") == target:
                return
            st = _get_state()
            if st["status"] in ("warming",):
                return
            _run_scan(False)
        except Exception:  # noqa: BLE001
            pass
    threading.Thread(target=_go, daemon=True).start()


if __name__ == "__main__":
    if len(sys.argv) >= 4 and sys.argv[1] == "--worker":
        _worker_main(sys.argv[2], sys.argv[3])
        sys.exit(0)
