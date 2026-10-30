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
import concurrent.futures as cf
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

# 战法方向映射（2026-10-14 进哥需求：战法列要"说清楚"——几根看涨 / 看跌 / 中立）。
# 流派（school）→ 方向标签：bull=看涨，neu=中立（条件/防御/风控），bear=看跌。
# 说明：本战法库全部为做多触发型信号，没有真正的看跌战法，故 bear 正常为 0；
# 保留该格子并标注原因，绝不臆造"看跌"数字。
_STANCE_BY_SCHOOL = {
    "趋势跟踪": "bull", "突破": "bull", "量价": "bull", "摆动指标": "bull",
    "形态": "bull", "动量与相对强弱": "bull", "资金面": "bull",
    "基本面": "bull", "板块": "bull",
    "均值回归": "neu", "波动率与风控": "neu", "组合/仓位/择时": "neu",
    "事件": "neu",
}
_STANCE_OVERRIDE = {            # 个别战法显式覆盖（名字已点明"防御/背离"等）
    "vol_price_divergence": "neu",   # 量价背离（防御）
}
_KEY_SCHOOL: dict = {}
_KEY_STANCE: dict = {}

def _load_stance_maps():
    global _KEY_SCHOOL, _KEY_STANCE
    if _KEY_SCHOOL:
        return
    try:
        import strategy_data as _sd
        for s in getattr(_sd, "STRATEGIES", []) or []:
            k = s.get("key")
            if not k:
                continue
            _KEY_SCHOOL[k] = s.get("school")
            _KEY_STANCE[k] = _STANCE_OVERRIDE.get(k) or _STANCE_BY_SCHOOL.get(s.get("school"), "neu")
    except Exception:  # noqa: BLE001 —— strategy_data 缺失则全按中立兜底
        pass

def _stance_of(key):
    _load_stance_maps()
    return _KEY_STANCE.get(key, "neu")

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

# ── 2026-10-30 D34：日更扫描只在**晚间**跑（19:45–23:00），白天把闸让给整条计算链 ────
# ⚠️ 先把这句话读准：**"占闸 1575 秒"只对日更路径成立**。坑位是父进程 `_spawn_offproc`
#    代持的（`viewer/server.py`），本模块自己**没有任何 brake 代码**、也不经过 `_spawn_offproc`。
#    所以：
#      · 日更路径（`tools/sched_job.py` 子进程）→ 占刹车闸 FETCH 1/1 约 26 分钟；
#      · 手动路径（`scan_payload()`，用户在面板点「立即扫描」）→ **不占闸**，
#        只是在服务进程内跑 26 分钟抢 CPU 与抓取带宽。
#    两者受限的原因不同：前者挤掉的是别的日更任务的坑位，后者只是让接口变慢。
#    2026-10-30 有人（包括我自己）据此误推过"手动路径也占闸"，故此处显式写死。
# 为什么（算出来的，不是拍脑袋）：`run_daily_sync()` 改成**同步**之后，日更路径会实打实
# 占住刹车闸的 `FETCH 1/1` **1575 秒 = 26 分钟**（`data/bili_scan_result.json` 实测
# elapsed_s=1575.1）。而 `_sched_tasks()` 里 bili_scan 排在 kline_daily / indicators_daily
# **前面**：它早跑 26 分钟，就把后面两个各 ~20~50 分钟的重活整体后推，`indicators_daily`
# 一旦被推过 18:00，龙虎榜（也是 FETCH/app 域、18:00 才发布、必须当晚落地）当场被挡。
# 2026-10-30 实测就是这样：`predictor_daily` 从 15:07 占到 16:37，闸一松手，
# bili → kline → indicators 一路顺延，18:00 那一轮直接没有坑位。
# 反过来，bili_scan 的结果按**交易日**去重（`_result_cache_load()` 比对 `date`），
# 第二天开盘前看到就行 —— **晚四小时扫是零成本的**，白天每分钟闸都更贵。
# 「15:10 之后就扫」这个原语义从来没生效过（A1 的 UnboundLocalError 挡了它整个生命周期，
# 见 `_start_scan_thread` 的注释），所以挪到晚间**不构成任何行为回退**。
# 单位＝当日分钟数，闭区间 [19*60+45, 23*60]：19:45 放行、23:00 放行、23:01 不放行。
# 【23:00 之后也不跑】的理由：跨日会污染次日 `_last_trade_date()` 的去重判断
# （扫出来的 payload 带的是"昨天"的交易日，次日 15:00 前又被当成"已有结果"跳过）。
# 只挂在日更入口（`_daily_gate` / `maybe_prewarm`）；`scan_payload()` 是用户手动触发，
# **不受**此窗限制 —— 用户白天要看就是他要，反正它也不占闸（见开头那三行）。
# 测试里置为 `()` 即"不设窗、恒放行"（见 tests/test_bili_scan_scheduling.py）。
_DAILY_WINDOW = (19 * 60 + 45, 23 * 60)


def _in_window(now) -> bool:
    """`now`（`time.struct_time`）是否落在「允许跑日更扫描」的时段内。

    `_DAILY_WINDOW` 置空（`()` / `None`）表示不设窗、恒为 True —— 测试用，
    生产恒为闭区间 [19:45, 23:00]。
    """
    if not _DAILY_WINDOW:
        return True
    lo, hi = _DAILY_WINDOW
    return lo <= now.tm_hour * 60 + now.tm_min <= hi


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


# ---------------------------------------------------------------- 关注池专用复算
# bars 磁盘缓存 IO 锁：全市场扫描（_load_bars）与关注池补抓可能并发写同一文件
_BARS_IO_LOCK = threading.Lock()
# 当日补抓到的 bars 内存兜底：磁盘写失败 / 被并发扫描冲掉时仍可复算
_WATCH_BARS_MEM: dict = {}


def signals_for_watch(codes, budget_s: float = 8.0) -> dict:
    """关注池专用：为池内票单独复算战法信号（2026-10-14，不再看 Top200 截断榜）。

    根因：bili_scan_result.json 的 hits 只存 Top200（按命中条数排序，上榜门槛 n≥6），
    关注池里只命中 1~5 条的票全被挡在榜外 → 界面战法信号列全 «—»（是匹配逻辑缺陷，
    不是「全不符合」）。

    口径（与全市场扫描同一份规则引擎，零漂移）：
      ① 读 bars 磁盘缓存取本地 bars；
      ② 缺 bars 的票用 _fetch_tx_full 并行补抓（max_workers=8，总预算约 budget_s 秒；
         抓不到就跳过，**绝不臆造**）；
      ③ keys = scan_keys(bili_sim_rows(conn))（conn 用只读连接）；
      ④ 每只票 × 每个 key 跑 build_signals，buy[-1] 为真即命中（基于最新一根日K）；
      ⑤ 返回 {"by_code": {code: [{"sname","win_rate","mname","skey"}...]},
              "signal_date": _last_trade_date(), "fetched": n, "cached": n}。
    信号基于「最新一根日K」——调用方必须把 signal_date 带到界面（盘中最后一根未收盘，
    以收盘复核为准）。
    """
    want, seen = [], set()
    for c in (codes or []):
        c = str(c or "").strip()
        if c and c not in seen:
            seen.add(c)
            want.append(c)
    signal_date = _last_trade_date()
    if not want:
        return {"by_code": {}, "signal_date": signal_date, "fetched": 0, "cached": 0}
    # ① 本地 bars 缓存（当日有效；非当日视为无缓存，走补抓）
    disk = _bars_cache_load()
    if disk.get("day") != signal_date:
        disk = {"day": signal_date, "bars": {}}
    bars = dict(disk.get("bars") or {})
    have = {c: bars[c] for c in want if c in bars and len(bars[c]) >= _MIN_BARS}
    for c in want:                      # 内存兜底（磁盘写失败时仍可复算）
        if c not in have and c in _WATCH_BARS_MEM and len(_WATCH_BARS_MEM[c]) >= _MIN_BARS:
            have[c] = _WATCH_BARS_MEM[c]
    cached = len(have)
    # ② 缺 bars 的票并行补抓（带总预算；超时未完成的不等待、不写缓存、不造假）
    need = [c for c in want if c not in have]
    fetched = 0
    if need:
        ex = ThreadPoolExecutor(max_workers=8)
        try:
            futs = {ex.submit(_fetch_tx_full, c, _SCAN_BARS): c for c in need}
            done, pending = cf.wait(futs, timeout=budget_s)
            for fut in done:
                code = futs[fut]
                try:
                    ks = fut.result()
                except Exception:  # noqa: BLE001
                    ks = []
                if ks and len(ks) >= _MIN_BARS:
                    rows = [[r[0], round(r[1], 3), round(r[2], 3), round(r[3], 3),
                             round(r[4], 3), round(r[5], 1)] for r in ks]
                    bars[code] = rows
                    _WATCH_BARS_MEM[code] = rows
                    fetched += 1
            for fut in pending:
                fut.cancel()
        finally:
            ex.shutdown(wait=False)
        if fetched:
            # 合并落盘（只追加、不清空既有票；先重读再合并，降低与全市场扫描的写竞争）
            try:
                with _BARS_IO_LOCK:
                    fresh = _bars_cache_load()
                    same_day = fresh.get("day") == signal_date
                    merged = dict(fresh.get("bars") or {}) if same_day else {}
                    merged.update({c: bars[c] for c in need if c in bars})
                    _bars_cache_save({"day": signal_date, "bars": merged,
                                      "fail": (fresh.get("fail") or {}) if same_day else {}})
            except Exception:  # noqa: BLE001 —— 缓存写失败不阻断（有内存兜底）
                pass
    # ③ 策略 key（只读连接取 strategy_sim_window，避免线程安全问题）
    conn = _conn_for_sim()
    try:
        sim = bili_sim_rows(conn)
        keys = scan_keys(sim)
        mmap = method_of_key(sim)
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass
    try:
        import strategy_sim as sim_mod
    except Exception:  # noqa: BLE001
        try:
            sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
            import strategy_sim as sim_mod
        except Exception:  # noqa: BLE001
            sim_mod = None
    # ④ 逐票 × 逐 key 复算（与 _mp_chunk 同一口径：build_signals 对最新一根日K 的 buy 判定）
    by_code: dict = {}
    if sim_mod is not None:
        for code in want:
            raw = have.get(code)
            if not raw or len(raw) < _MIN_BARS:
                continue                 # 缺 K 线的票保持 «—»，严禁臆造
            try:
                k = [{"d": r[0], "o": r[1], "h": r[2], "l": r[3], "c": r[4], "v": r[5]} for r in raw]
            except Exception:  # noqa: BLE001
                continue
            hits = []
            for key in keys:
                try:
                    buy, _sell = sim_mod.build_signals(key, k, None, code, "")
                except Exception:  # noqa: BLE001
                    continue
                if buy and buy[-1]:
                    s = sim.get(key) or {}
                    mm = mmap.get(key) or {"key": key, "name": key, "kind": "技术面"}
                    hits.append({"sname": s.get("name", key), "win_rate": s.get("win_rate"),
                                 "mname": mm.get("name", key), "skey": key,
                                 "school": _KEY_SCHOOL.get(key), "stance": _stance_of(key)})
            if hits:
                by_code[code] = hits
    return {"by_code": by_code, "signal_date": signal_date,
            "fetched": fetched, "cached": cached}


def _start_scan_thread(force: bool) -> bool:
    """（单飞）后台启动一次扫描线程，返回是否真的启动。

    2026-10-30 D33：把「`global _THREAD` + 单飞判断 + 建线程」收成**一处**。
    此前这段代码在 `scan_payload` / `maybe_daily` / `maybe_prewarm._go` 里各抄了一份，
    而**只有 `scan_payload` 写了 `global _THREAD`** —— 另两处里 `_THREAD` 因为下面这行赋值
    变成了**局部名**，于是前面那句 `if _THREAD and _THREAD.is_alive()` 直接
    `UnboundLocalError: cannot access local variable '_THREAD'`，又被外层
    `except Exception` 一把吞掉。后果（用 sys.settrace 跟实际执行行验过）：
      · `maybe_daily()` 每次必抛、永远 return False → `sched-bili_scan.log` 里每 15 分钟
        一条 `ok 0.2s False`，看着像"当日已完成所以跳过"，其实是**从来没进来过**；
      · `maybe_prewarm()` 的 45 秒预热线程同样没建成（实测 1.2s 后 `_THREAD` 仍是 None）。
    也就是说这个「收盘自动扫一次」的能力**从上线起一次都没工作过**。
    """
    global _THREAD
    with _LOCK:
        if _THREAD and _THREAD.is_alive():
            return False
        _THREAD = threading.Thread(target=_run_scan, args=(bool(force),),
                                   name="bili-scan", daemon=True)
        _THREAD.start()
    return True


# ---------------------------------------------------------------- 对外入口
def scan_payload(conn, force: bool = False) -> dict:
    """前端轮询入口。首次调用触发后台单飞扫描，返回 warming；完成后返回 done。

    **本入口不受 `_DAILY_WINDOW` 限制**（用户点了「立即扫描」就是他要，白天也该给）。
    也**不占刹车闸**：坑位由父进程 `_spawn_offproc` 代持，而这个入口是在**服务进程内**
    起线程跑（`_start_scan_thread`），本模块里没有任何 brake 代码。它在白天占用的是
    CPU 与抓取带宽，代价是接口变慢，不会挤掉别的日更任务 —— 与日更路径受限的原因是两回事。
    """
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
    if not _start_scan_thread(bool(force)):
        return {"status": "warming", "note": "扫描已在进行中"}
    return {"status": "warming", "phase": "", "done": 0, "total": 0,
            "note": "扫描启动中", "elapsed_s": 0}


def _run_scan(force: bool) -> dict:
    """跑一轮完整扫描，把结果写进 `_STATE` + 落盘缓存，同时**返回**本轮结果。

    2026-10-30 D33：原来返回 None、结果只写 `_STATE`，于是"同步调用"拿不到成败
    （`run_daily_sync` 要靠它给 sched_job 一个 `ok`）。线程调用方忽略返回值即可。
    """
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
        return {"ok": True, "elapsed": round(time.time() - t0, 1),
                "hits": payload.get("total_hits"), "scanned": payload.get("scanned"),
                "error": None}
    except Exception as e:  # noqa: BLE001
        _set_state(status="error", error=str(e), phase="error", finished_at=time.time())
        return {"ok": False, "elapsed": round(time.time() - t0, 1),
                "hits": None, "scanned": None, "error": str(e)[:300]}


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


def _daily_gate():
    """收盘刷新的闸：返回 None 表示"该跑"，否则返回跳过原因。

    2026-10-30 D33：`maybe_daily`（bool 兼容别名）与 `run_daily_sync`（子进程入口）
    **共用这一份**。判断顺序逐条照搬历史实现（交易日 → 15 点后 → 不在 warming →
    当日已有结果），只搬家不改语义；D34 在 `pre_close` 之后加了一条 `day_brake_window`：
    **只有 19:45–23:00 允许跑**，其余时段把闸让给 kline_daily / indicators_daily /
    universe_scan / lhb_daily 整条计算链（理由与 23:00 后也不跑的原因见 `_DAILY_WINDOW`）。
    """
    target = _last_trade_date()
    now = time.localtime()
    if time.strftime("%Y-%m-%d", now) != target:
        return "not_trading_day"
    if now.tm_hour < 15:
        return "pre_close"
    if not _in_window(now):
        return "day_brake_window"
    st = _get_state()
    if st["status"] == "warming":
        return "warming"
    cached = _result_cache_load()
    if cached and cached.get("date") == target:
        return "done"
    return None


def run_daily_sync(conn=None) -> dict:
    """**同步**跑完一轮收盘扫描，跑完才返回（供 `tools/sched_job.py` 的子进程调用）。

    2026-10-30 D33：和 `lhb_daily` 同一个病 —— 原 `maybe_daily()` 起 daemon 线程后
    立即 `return True`，而调用方 `tools/sched_job.py::_bili_scan()` 是**短命子进程**：
    print 完就 `return 0` 退出，CPython 关解释器时把 daemon 线程直接杀掉 → 一次都扫不成。
    （在此之前还被 `_THREAD` 那个 UnboundLocalError 挡着，连"起了个线程"都没做到。）

    实测一轮真实扫描（`data/bili_scan_result.json`）`elapsed_s=1575.1`，做成同步后子进程
    要跑满约 25 分钟 —— `_JOB_TIMEOUT`（默认 3600s）兜得住，别往小改。
    `ok=False` 会被 `sched_job.main()` 判成 rc=1，交父进程退避重试；
    `skipped` 非空表示被闸挡住（不算失败），取值见 `_daily_gate`：
    `not_trading_day` / `pre_close` / `day_brake_window`（不在 19:45–23:00）/ `warming` / `done`。
    """
    t0 = time.time()
    started = time.strftime("%Y-%m-%d %H:%M:%S")
    skip = _daily_gate()
    if skip:
        return {"ok": True, "started": started, "skipped": skip,
                "elapsed": round(time.time() - t0, 1)}
    res = _run_scan(False)
    return {"ok": bool(res.get("ok")), "started": started,
            "elapsed": round(time.time() - t0, 1),
            "hits": res.get("hits"), "scanned": res.get("scanned"),
            "error": res.get("error")}


def maybe_daily() -> bool:
    """兼容别名（返回 bool）。**已改为同步**，调度路径里不再起线程。

    sched_job 已改调 `run_daily_sync`（要 `ok` 语义）；这个名字留给"跑了吗"这类调用方。
    历史缺陷：函数体里有 `_THREAD = ...` 赋值 → `_THREAD` 成了**局部名** → 前面那句
    `if _THREAD and _THREAD.is_alive()` 必抛 UnboundLocalError，被
    `except Exception: return False` 吞掉 —— **这个入口从上线起一次都没成功触发过扫描**。
    """
    try:
        res = run_daily_sync()
        return bool(res.get("ok")) and not res.get("skipped")
    except Exception:  # noqa: BLE001
        return False


def maybe_prewarm() -> None:
    """server 启动 45s 后，当日无结果则后台预热一次。

    `_go()` 里的顺序很关键，改之前先读完这段：

      1. **缓存已是当日的** → 直接 return。绝大多数白天重启都走这条（`_last_trade_date()`
         有 15:05 截断，09:00 时它返回的也是上一个交易日，与缓存自然相等）—— 所以
         "白天重启别抢闸"这个目的**由缓存判断就实现了**，不需要靠时间窗。
      2. **缓存不是当日的**，分两种，长得一样但处置相反：
         · 早盘（15:05 之前）→ `target` 还是上一个交易日，缓存比它还旧 = **昨晚日更真没跑成**；
           这时**必须预热**，否则用户早上点「一键筛股」就是 26 分钟 warming。
           这是 prewarm 唯一不可替代的价值，别用时间窗把它关掉（2026-10-30 我就关错过一次）。
         · 15:05 之后、日更 19:45 之前 → `target` 已变成今天，缓存是昨天属于**正常**，
           别抢闸，交给 19:45 那轮。
      3. 19:45–23:00 之内（日更时段）→ 缓存不新鲜就预热，和日更同一份判断（`_in_window`）。

    实测这台机器的 server 今天重启了 5 次（11:06 / 11:12 / 15:07 …），所以第 2、3 条不能省。
    """
    def _go():
        time.sleep(45)
        try:
            target = _last_trade_date()
            cached = _result_cache_load()
            if cached and cached.get("date") == target:
                return                  # ① 已有当日结果：不需要预热（也不需要抢闸）
            now = time.localtime()
            if now.tm_hour >= 15 and not _in_window(now):
                # ② 下午/傍晚且不在日更窗口：target 已是今天、缓存还是昨天，属于"日更还没到点"
                return
            # ③ 早盘（昨晚真没跑成）或已在日更窗口：宁可抢一次闸，也别让用户干等 26 分钟
            st = _get_state()
            if st["status"] in ("warming",):
                return
            _start_scan_thread(False)   # ← 统一走单飞入口（原来这里也漏了 global _THREAD）
        except Exception:  # noqa: BLE001
            pass
    threading.Thread(target=_go, daemon=True).start()


if __name__ == "__main__":
    if len(sys.argv) >= 4 and sys.argv[1] == "--worker":
        _worker_main(sys.argv[2], sys.argv[3])
        sys.exit(0)
