"""工具箱「筛台」后端（2026-11-05）：单工具结果缓存 + 单线程后台 worker + 筛道求交。

为什么需要这个模块（进哥原话）

    「你这策略有这么多，我点一下，然后就应该是比如筛取一半，再点又筛一半，再点又筛一半……
      你这操作界面我感觉一点儿都不智能，特别的繁琐」

原来 `toolbox.run_funnel()` 是**每次从头重算整条链**：点一次「加入漏斗」不起算，
再点「跑这条链」才把每一级重算一遍 —— 箱体谷底单级实测 11.6 秒，六级链就是 40 秒起步，
而且第二级和第一级用同一份缓存都没用，因为它根本不看缓存。

所以这里换掉的是**计算模型**，不是界面：

    旧：一条链 = 一次全量重算（O(级数 × 全市场)）
    新：每个工具对全宇宙只算一次并落盘 → 筛道只剩集合求交（O(级数 × 名单大小)）

纪律（改这个文件前先读）

 1. **不在 HTTP 请求线程里做重活**（项目铁律，见 server.py `_log_access` 上方注释）。
    本模块的重活一律进 `_worker_loop()` 那个**单线程**队列；`run_stack()` 只读落盘，
    绝不能顺手调 `apply_tool()` 补算 —— 一旦破例，请求线程就会被钉在 11 秒上，
    那就绕回原来的问题了。

 2. **串行不是偷懒**：箱体类工具单次 7~12 秒，是纯 Python 逐只扫 750 日 K 线。
    并发跑会把这台机器的 CPU 和磁盘打满，用户正在用的其它面板会一起变卡。
    一件一件算，算完就有，第二次是毫秒级 —— 用户体感比并发好得多。

 3. **不改 `toolbox.py` 的判据**：这里只调用 `toolbox.apply_tool()`。工具收不收、
    为什么收、什么条件下淘汰人，全在 `toolbox.py` 里，一个字都不在这里改。

 4. **按交易日失效**：日 K 一变，任何工具的结果都可能变。落盘里带着
    `trade_date`，和最新交易日不一致就视为未命中 —— 绝不拿昨天的名单冒充今天的。

落盘结构（每个工具 × 每组参数 × 每个交易日 两个文件）

    <tool>_<date>_<digest>.json       核心：kept / score / marks / ms / note（几十 KB）
    <tool>_<date>_<digest>.drop.json  淘汰原因：dropped（300~600 KB）

    为什么拆两个：卡片上「现在点：675 → 38」只需要 `kept` 求交，而 `dropped` 只在
    渲染最终名单的淘汰原因时要。合成一个文件时，打开页面（20 个工具）要解析 10MB+ JSON，
    拆开后打开页面只解析核心文件，淘汰原因只读**真正用到的那几级**。
    `load_result()` 仍按规格返回合并后的完整结构，调用方不用关心拆分。
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import queue
import re
import threading
import time
from datetime import datetime

log = logging.getLogger(__name__)

# ================================================================
# ① 常量
# ================================================================
CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         "cache", "toolbox_calc")

# 缓存结构版本：将来改了落盘字段就把它加一，老文件自然不命中（不用写迁移脚本）。
CACHE_VER = "1"

MAX_STEPS = 8            # 与 /api/toolbox/funnel 保持一致
KEEP_DATES = 3           # 每个「工具+参数」最多保留 3 个交易日的文件，防止无限膨胀
FRESH_SEC = 120.0        # 落盘 120 秒内算出来的，标注为「刚算完」而不是「缓存」
FAIL_COOLDOWN = 180.0    # 某个工具算失败后 3 分钟内不再重复入队（防止疯狂重试）

# ================================================================
# ② 缓存读写
# ================================================================
_SAFE_RE = re.compile(r"[^0-9A-Za-z_]")


def _safe(s) -> str:
    """把工具名 / 交易日压成文件名安全的形式（交易日里的 `-` 会被去掉）。"""
    out = _SAFE_RE.sub("", str(s or ""))
    return out or "x"


def tool_digest(tool_key: str, params: dict | None) -> str:
    """参数签名：同一工具、不同参数 = 两份互不干扰的缓存。

    用 `sort_keys=True` 而不是自己拼字符串：字典顺序不稳，拼字符串会让
    同一组参数算出两个摘要，缓存永远不命中（而且这种 bug 不会报错，只会变慢）。

    ⚠️ 不要在这里做数值归一化（别把 1.0 转成 1、也别把 1 转成 1.0）：
    摘要一变，已经落盘的缓存全部失效 —— 20 个预热好的工具（7.7MB）要重算约 90 秒，
    用户点第一张卡就得白等。int/float 的差异在 `_merge_params()` 里消化掉，
    函数的职责是「同样的参数 → 同样的 key」，不是「把不同的 key 抹平」。
    """
    payload = json.dumps({"t": str(tool_key or ""), "p": params or {}},
                         sort_keys=True, ensure_ascii=False)
    return hashlib.md5(payload.encode("utf-8")).hexdigest()[:16]


def _core_path(trade_date: str, tool_key: str, digest: str) -> str:
    return os.path.join(CACHE_DIR, "%s_%s_%s.json"
                        % (_safe(tool_key), _safe(trade_date), digest))


def _drop_path(core_path: str) -> str:
    return (core_path[:-5] if core_path.endswith(".json") else core_path) + ".drop.json"


def cache_path(trade_date: str, tool_key: str, params: dict | None) -> str:
    """该工具该参数该交易日的核心文件路径（运维排查用：可以直接去看文件在不在）。"""
    return _core_path(trade_date, tool_key, tool_digest(tool_key, params))


def _ensure_dir() -> None:
    try:
        os.makedirs(CACHE_DIR, exist_ok=True)
    except OSError:  # noqa: BLE001 —— 建不了目录时上层会退化成「永远不命中」，不该崩
        log.exception("建缓存目录失败：%s", CACHE_DIR)


def _read_json(path: str):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return None
    except Exception:  # noqa: BLE001 —— 半截文件 / 手改坏了：当作未命中，重算一次就好
        log.exception("读取缓存失败（按未命中处理）：%s", path)
        return None


def _write_json_atomic(path: str, obj) -> bool:
    """先写 tmp 再 os.replace：进程被杀在写一半时，读的一方永远看不到半个 JSON。"""
    _ensure_dir()
    tmp = "%s.tmp.%d.%d" % (path, os.getpid(), threading.get_ident())
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False)
        os.replace(tmp, path)
        return True
    except Exception:  # noqa: BLE001
        log.exception("写缓存失败：%s", path)
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except OSError:
            pass
        return False


def _core_of(obj) -> dict | None:
    """把落盘 dict 校验成「能用的核心结果」，不合格返回 None（宁可重算，不用坏数据）。"""
    if not isinstance(obj, dict):
        return None
    if str(obj.get("ver") or "") != CACHE_VER:
        return None
    kept = obj.get("kept")
    if not isinstance(kept, list):
        return None
    core = {
        "kept": [str(c) for c in kept if c],
        "score": obj.get("score") if isinstance(obj.get("score"), dict) else {},
        "marks": obj.get("marks") if isinstance(obj.get("marks"), dict) else {},
        "ms": int(obj.get("ms") or 0),
        "note": str(obj.get("note") or ""),
        "in": int(obj.get("in") or 0),
        "tool_key": str(obj.get("tool_key") or ""),
        "tool_name": str(obj.get("tool_name") or ""),
        "universe_n": int(obj.get("universe_n") or 0),
        "trade_date": str(obj.get("trade_date") or ""),
        "saved_at": str(obj.get("saved_at") or ""),
        "mtime": 0.0,
    }
    return core


def load_core(trade_date: str, tool_key: str, params: dict | None) -> dict | None:
    """只读核心（不含 dropped）—— 卡片数字、筛道求交、预热计数都走这里。

    交易日不一致 → 未命中（不能拿昨天的名单当今天）。这是硬规则：
    用户看着「675 只」去买，结果那是前天的 675 只，比慢一百倍更糟。
    """
    fp = cache_path(trade_date, tool_key, params)
    core = _core_of(_read_json(fp))
    if not core:
        return None
    if core["trade_date"] != str(trade_date or ""):
        return None
    try:
        core["mtime"] = os.path.getmtime(fp)
    except OSError:
        core["mtime"] = 0.0
    return core


def load_dropped(trade_date: str, tool_key: str, params: dict | None) -> dict:
    """读淘汰原因（大文件）。只有渲染名单里「死在第几级、为什么死」时才需要。"""
    fp = _drop_path(cache_path(trade_date, tool_key, params))
    obj = _read_json(fp)
    if isinstance(obj, dict):
        d = obj.get("dropped")
        if isinstance(d, dict):
            return {str(k): str(v) for k, v in d.items()}
    return {}


def load_result(trade_date: str, tool_key: str, params: dict | None) -> dict | None:
    """命中则返回 {kept, dropped, score, marks, ms, note, cached=True}；未命中返回 None。

    这是模块对外的主读口（规格 3.1）。内部高频路径用 `load_core()`，
    两者的区别只是要不要顺带把 5000 条淘汰原因读进内存。
    """
    core = load_core(trade_date, tool_key, params)
    if not core:
        return None
    core["dropped"] = load_dropped(trade_date, tool_key, params)
    core["cached"] = True
    return core


def _prune_old_dates(tool_key: str, digest: str) -> None:
    """同一组参数只保留最近 KEEP_DATES 个交易日 —— 每天 20 个工具 × 600KB，
    不清理的话半年就是十个 G，而且没人会记得去清。"""
    prefix = _safe(tool_key) + "_"
    suffix = "_" + digest + ".json"
    try:
        names = os.listdir(CACHE_DIR)
    except OSError:
        return
    mine = sorted(n for n in names
                  if n.startswith(prefix) and n.endswith(suffix) and ".drop." not in n)
    if len(mine) <= KEEP_DATES:
        return
    for n in mine[:len(mine) - KEEP_DATES]:
        for p in (os.path.join(CACHE_DIR, n), _drop_path(os.path.join(CACHE_DIR, n))):
            try:
                if os.path.exists(p):
                    os.remove(p)
            except OSError:
                pass


# ================================================================
# ③ 算一次（只在 worker 线程里被调用）
# ================================================================
def compute_and_save(con, trade_date: str, tool_key: str, params: dict | None,
                     universe, universe_n: int = 0) -> dict:
    """调 `toolbox.apply_tool()` 算一遍并原子落盘，返回核心结构（带 ok / error）。

    ⚠️ `apply_tool` 会往 `tool_run_log` 写一条记录 —— **保留，不要绕过**。
       它既是「最近一次运行」的来源，也是三期自动淘汰的账本。预热时每个工具只算一次，
       这条留痕本身是有价值的（谁在什么时候跑过、跑了多少只、多久）。

    ⚠️ `dropped` 里可能有 5000 条原因（约 300~600KB），这是可接受的：它单独落一个文件，
       只在最终名单展示时按需读取，不做全书架加载。
    """
    from viewer import toolbox as _tb

    params = dict(params or {})
    uni = [str(c) for c in (universe or [])]
    uni_n = int(universe_n or len(uni))
    t0 = time.time()
    try:
        r = _tb.apply_tool(con, tool_key, uni, params, funnel_id="stack", step_idx=0)
    except Exception as e:  # noqa: BLE001 —— apply_tool 自己会兜异常，这里兜的是意外
        log.exception("工具 %s 计算异常", tool_key)
        r = {"ok": False, "error": "%s 计算异常：%s" % (tool_key, str(e)[:200])}
    ms = int((time.time() - t0) * 1000)

    if not r.get("ok"):
        err = str(r.get("error") or "未知错误")[:300]
        _mark_fail(tool_key, params, err)
        return {"ok": False, "error": err, "kept": [], "dropped": {}, "score": {},
                "marks": {}, "ms": ms, "note": "", "in": len(uni), "universe_n": uni_n}

    kept = [str(c) for c in (r.get("kept") or [])]
    core = {
        "ver": CACHE_VER,
        "tool_key": tool_key,
        "tool_name": str(r.get("tool_name") or tool_key),
        "trade_date": str(trade_date or ""),
        "universe_n": uni_n,
        "in": len(uni),
        "kept": kept,
        "score": {str(k): v for k, v in (r.get("score") or {}).items()},
        "marks": {str(k): str(v) for k, v in (r.get("marks") or {}).items() if v},
        "ms": int(r.get("ms") or ms),
        "note": str(r.get("note") or ""),
        "saved_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    dropped = {str(k): str(v) for k, v in (r.get("dropped") or {}).items()}

    fp = cache_path(trade_date, tool_key, params)
    ok1 = _write_json_atomic(fp, core)
    ok2 = _write_json_atomic(_drop_path(fp), {"dropped": dropped})
    if not (ok1 and ok2):
        # 落盘失败不影响本次结果，但要留痕：否则「为什么永远不命中」会查很久。
        log.warning("工具 %s 结果落盘失败（下次会重算）", tool_key)
    else:
        _prune_old_dates(tool_key, tool_digest(tool_key, params))
        _clear_fail(tool_key, params)

    core["dropped"] = dropped
    core["ok"] = True
    core["cached"] = False
    return core


# ================================================================
# ④ 失败记录（防止算不出来的工具被无限重试，也让人知道它算不出来）
# ================================================================
_FAILS: dict[str, tuple[float, str]] = {}
_FAIL_LOCK = threading.Lock()


def _fail_key(tool_key: str, params: dict | None) -> str:
    return "%s|%s" % (tool_key, tool_digest(tool_key, params))


def _mark_fail(tool_key: str, params: dict | None, err: str) -> None:
    with _FAIL_LOCK:
        _FAILS[_fail_key(tool_key, params)] = (time.time(), err)


def _clear_fail(tool_key: str, params: dict | None) -> None:
    with _FAIL_LOCK:
        _FAILS.pop(_fail_key(tool_key, params), None)


def recent_fail(tool_key: str, params: dict | None) -> str:
    """最近一次失败原因（冷却窗口内有效）。前端据此显示「这个工具算不出来」，而不是永远转圈。"""
    with _FAIL_LOCK:
        item = _FAILS.get(_fail_key(tool_key, params))
    if not item:
        return ""
    ts, err = item
    if time.time() - ts > FAIL_COOLDOWN:
        return ""
    return err


# ================================================================
# ⑤ 单线程后台 worker
# ================================================================
_QUEUE: "queue.Queue" = queue.Queue()
_QLOCK = threading.Lock()
_QUEUED: set[str] = set()        # 在队列里等着的 digest
_INFLIGHT: set[str] = set()      # 正在算的 digest
_WORKER: threading.Thread | None = None
_WORKER_LOCK = threading.Lock()
_DB = None                       # 由 server 注入（Database 实例，内部是线程本地连接）
_FALLBACK_DB = None
_STATS = {"done": 0, "failed": 0, "last_tool": "", "last_ms": 0, "last_at": ""}


def configure(db) -> None:
    """由 server 注入它那个 Database 实例。

    为什么要注入而不是自己 `Database()`：`Database()` 构造会跑一遍建表脚本，
    进程里已经有连接池了就别再开一套。`Database.conn` 是**线程本地**的，
    所以 worker 线程拿到的是一条属于它自己的连接，不存在跨线程共享 SQLite 对象的问题。
    """
    global _DB
    _DB = db


def _get_conn():
    """worker 线程自己的连接。没注入就懒建一个（独立脚本直接调本模块时走这条）。"""
    global _DB, _FALLBACK_DB
    if _DB is not None:
        return _DB.conn
    with _WORKER_LOCK:
        if _FALLBACK_DB is None:
            from viewer.db import Database
            _FALLBACK_DB = Database()
            _DB = _FALLBACK_DB
    return _DB.conn


def _universe_for(con, trade_date: str) -> list[str]:
    """worker 每次算之前重新取一遍全宇宙（实测 37 毫秒，相对单工具 6~20 秒可忽略）。

    ★ 刻意**不做**缓存：缓存会让「池子白名单改了」这种情况变成死循环 ——
      `run_stack` 发现落盘里的 universe_n 和当前宇宙不一致就当作未命中、
      重新入队；而 worker 若还在用缓存里的旧宇宙，算出来的还是旧 universe_n，
      于是永远不命中。宁可选 37 毫秒，也不要一个「看着在算、其实永远算不对」的循环。
    """
    from viewer import toolbox as _tb
    try:
        return [str(c) for c in (_tb.universe_codes(con) or [])]
    except Exception:  # noqa: BLE001
        log.exception("干净宇宙取数失败（worker 本轮跳过）")
        return []


def _latest_date() -> str:
    try:
        from viewer import localfeed as _lf
        return _lf.latest_date() or ""
    except Exception:  # noqa: BLE001
        log.exception("取最新交易日失败")
        return ""


def ensure_worker() -> None:
    """惰性启动单线程 daemon worker，串行消费队列（幂等）。"""
    global _WORKER
    if _WORKER is not None and _WORKER.is_alive():
        return
    with _WORKER_LOCK:
        if _WORKER is not None and _WORKER.is_alive():
            return
        t = threading.Thread(target=_worker_loop, name="toolbox-stack-worker", daemon=True)
        t.start()
        _WORKER = t
        log.info("筛台后台 worker 已启动（单线程串行）")


def request_compute(tool_key: str, params: dict | None, trade_date: str) -> bool:
    """把 (工具, 参数, 交易日) 放进队列。同一签名已在队列/在算 → 不重复入队。

    返回 True 表示这次真的入队了（调用方一般不用它，测试会看）。
    """
    tool_key = str(tool_key or "").strip()[:48]
    if not tool_key:
        return False
    params = dict(params or {})
    dg = tool_digest(tool_key, params)
    if recent_fail(tool_key, params):
        return False
    with _QLOCK:
        if dg in _QUEUED or dg in _INFLIGHT:
            return False
        _QUEUED.add(dg)
    try:
        ensure_worker()
        _QUEUE.put({"digest": dg, "tool": tool_key, "params": params,
                    "trade_date": str(trade_date or "")})
    except Exception:  # noqa: BLE001 —— 入队失败必须把占位撤回，否则这个签名永远算不了
        with _QLOCK:
            _QUEUED.discard(dg)
        log.exception("筛台入队失败：%s", tool_key)
        return False
    return True


def _worker_loop() -> None:
    """串行算：一件一件来。任何异常都吃掉写进 _STATS，绝不让 daemon 线程静默死掉。"""
    while True:
        item = _QUEUE.get()
        try:
            dg = item["digest"]
            with _QLOCK:
                _QUEUED.discard(dg)
                _INFLIGHT.add(dg)
            try:
                con = _get_conn()
                # 队列里的交易日只是提示：跨日之后要按**当前**最新交易日算，
                # 否则刚过零点的新结果会被写成昨天的名字，明天照样全部未命中。
                td = _latest_date() or item.get("trade_date") or ""
                uni = _universe_for(con, td)
                if not uni:
                    _mark_fail(item["tool"], item["params"], "干净宇宙为空，无法计算")
                    _STATS["failed"] += 1
                    continue
                r = compute_and_save(con, td, item["tool"], item["params"], uni, len(uni))
                if r.get("ok"):
                    _STATS["done"] += 1
                    _STATS["last_tool"] = item["tool"]
                    _STATS["last_ms"] = int(r.get("ms") or 0)
                else:
                    _STATS["failed"] += 1
                    log.warning("筛台预热失败 %s：%s", item["tool"], r.get("error"))
                _STATS["last_at"] = datetime.now().strftime("%H:%M:%S")
            finally:
                with _QLOCK:
                    _INFLIGHT.discard(dg)
        except Exception:  # noqa: BLE001
            log.exception("筛台 worker 单项失败（继续下一项）")
            _STATS["failed"] += 1
        finally:
            _QUEUE.task_done()


def worker_state() -> dict:
    """排查用：队列里还有多少、算完几个、失败几个。"""
    with _QLOCK:
        q = len(_QUEUED)
        inflight = len(_INFLIGHT)
    return dict(_STATS, queued=q, inflight=inflight,
                alive=bool(_WORKER and _WORKER.is_alive()))


def prewarm(con, trade_date: str = "") -> dict:
    """把货架上**每个可用工具**用它的默认参数算一次（只入队，不在本线程算）。

    这是「打开工具箱 3 秒内就有数」的前提：页面一进来先要清单，
    清单接口顺手把 20 个工具排进后台队列，用户还在看卡片的时候，后面的数字已经一个个填上了。
    实测 20 个工具串行约 60~70 秒（箱体谷底 11.6 秒 + 六个形态因子各 6~7 秒 + 其余秒级）。
    """
    from viewer import toolbox as _tb
    td = trade_date or _latest_date()
    out = {"total": 0, "ready": 0, "queued": 0}
    try:
        tools = _tb.list_tools(con, include_retired=True)
    except Exception:  # noqa: BLE001
        log.exception("预热读取货架失败")
        return out
    for t in tools:
        if t.get("status") == "retired":
            continue
        out["total"] += 1
        key = t["tool_key"]
        prm = dict(t.get("default_params") or {})
        if load_core(td, key, prm):
            out["ready"] += 1
            continue
        if request_compute(key, prm, td):
            out["queued"] += 1
    return out


# ================================================================
# ⑥ 筛道求交（请求线程只做这个：读落盘 + 集合运算）
# ================================================================
def _normalize_spec(spec) -> list[dict]:
    out: list[dict] = []
    for s in (spec or []):
        if not isinstance(s, dict):
            continue
        key = str(s.get("tool") or s.get("tool_key") or "").strip()[:48]
        if not key:
            continue
        prm = s.get("params")
        out.append({"tool": key, "params": dict(prm) if isinstance(prm, dict) else {}})
        if len(out) >= MAX_STEPS:
            break
    return out


def _num_eq(a, b) -> bool:
    """两个值是否「数值相等」—— 只认数字，bool 不算（`True == 1` 成立，但布尔参数不是数字）。

    `1 == 1.0` 在 Python 里为 True，但 `json.dumps({"x": 1})` 和 `json.dumps({"x": 1.0})`
    是两个字符串（`{"x": 1}` / `{"x": 1.0}`），摘要因此不同。浏览器只有一个 number 类型，
    `JSON.stringify({pos_max: 1.0})` 出来的就是 `{"pos_max":1}` —— 前端拿不到「1.0」这种写法，
    所以这个差异只能在服务端消化。
    """
    if isinstance(a, bool) or isinstance(b, bool):
        return False
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        try:
            return float(a) == float(b)
        except (TypeError, ValueError, OverflowError):
            return False
    return False


def _merge_params(tool: dict, params: dict | None) -> dict:
    """把「工具默认参数 + 调用方给的参数」合成**唯一**的一组参数。

    为什么必须先合成再算摘要：`apply_tool()` 内部就是 `defaults.update(params)`，
    所以 `{}` 和 `{"min_dwell":0.15,"pos_max":1.0,"min_years":0.5}` 是**同一份结果**，
    但签名不同 —— 不归一化的话同一个结果会被算两遍（箱体谷底一次 11~20 秒），
    而且「预热算好的」和「前端点出来的」永远对不上，用户会看到「这一关正在算」白等。

    2026-11-05 补充（QA 实测首访白等 63 秒）：传入值与该键默认值**数值相等**时，
    直接沿用默认值那个原始对象，而不是用传入值覆盖。默认值是 `1.0`（float）时，
    前端传上来的 `1`（int）会被退回 `1.0`，摘要与预热那份完全一致，点一下就是缓存命中。
    注意这**不是**全局归一化：只影响「和默认值相等」的键，用户真改过的值原样保留。
    """
    prm = dict((tool or {}).get("default_params") or {})
    for k, v in (params or {}).items():
        if k in prm and _num_eq(v, prm[k]):
            continue                     # 数值相等 → 保留默认值原本的写法（1.0 不变成 1）
        prm[k] = v
    return prm


def run_stack(con, spec: list[dict] | None, limit: int = 200) -> dict:
    """按筛道顺序逐级求交，返回规格 3.1 约定的结构。

    spec = [{"tool": "box_bottom", "params": {...}}, ...]（顺序即筛道顺序）

    关键点：**这里一次 `apply_tool` 都不调**。没命中的那一级入队后就 break ——
    后面的级没有输入可算（前一级的 kept 就是后一级的输入），所以不必入队，
    前端看到 `pending` 会 1.5 秒后自己回来问。
    """
    from viewer import toolbox as _tb

    try:
        limit = max(20, min(500, int(limit or 200)))
    except (TypeError, ValueError):
        limit = 200
    clean = _normalize_spec(spec)
    trade_date = _latest_date()

    try:
        universe = [str(c) for c in (_tb.universe_codes(con) or [])]
    except Exception:  # noqa: BLE001
        log.exception("干净宇宙取数失败（筛台按空宇宙返回）")
        universe = []
    uni_n = len(universe)
    cur = set(universe)

    steps: list[dict] = []
    kept_sets: list[set] = []
    step_specs: list[dict] = []          # 与 steps 一一对应（读淘汰原因时要工具+参数）
    core_index: dict[tuple, dict] = {}   # (tool, digest) -> core，供 counted 复用
    errors: dict[str, str] = {}
    scores: dict[str, float] = {}
    marks: dict[str, str] = {}
    score_src = ""
    score_src_key = ""
    pending = None
    _drop_memo: dict[int, dict] = {}
    # ★ 2026-11-06 审计 P1-02：这里曾经是"出错就 continue，池子不变，最后照样 ok=True"。
    #   实测调用 retired 工具时接口返回 ok=true / final=5263 / 某一步 error=已淘汰 ——
    #   调用方只要看顶层 ok 就会以为成功，页面也照样展示"5263 只"。这不是容错，是撒谎。
    #   现在：第一步硬错误就记下来，顶层 ok=False，final 置 null，另给中间值，
    #   前端据此锁定结果区（不再展示那份"5263 只推荐"）。
    first_error: dict | None = None

    # ★ 资格判定一次判完整条筛道（共用一份上下文：市况 + 箱体），后面每级只查表。
    #   为什么不放在循环里逐级判：`_box_context()` 要读筛台缓存、`regime.current()` 要查库，
    #   5 级筛道就是 5 遍重复 IO，而且同一轮里市况不会变，重复判毫无信息增量。
    try:
        from viewer import tool_router as _rt
        _verdicts = _rt.screen_verdicts(con, [str(s.get("tool") or "") for s in clean])
    except Exception:  # noqa: BLE001
        log.exception("筛道资格判定失败（本轮不做资格拦截，结果需人工复核）")
        _verdicts = {}

    def _fail(idx: int, key: str, msg: str, name: str) -> None:
        nonlocal first_error
        if first_error is None:
            first_error = {"idx": idx, "tool_key": key, "tool": name or key,
                           "reason": msg}

    def _drop_of(i: int) -> dict:
        """第 i 级的淘汰原因（懒加载：只有真要渲染原因时才读那个 600KB 文件）。"""
        if i not in _drop_memo:
            sp = step_specs[i]
            _drop_memo[i] = load_dropped(trade_date, sp["tool"], sp["params"])
        return _drop_memo[i]

    for idx, st in enumerate(clean):
        key, prm = st["tool"], st["params"]
        try:
            t = _tb.get_tool(con, key)
        except Exception:  # noqa: BLE001
            log.exception("读取工具 %s 登记信息失败", key)
            t = None
        if not t:
            _msg = "未登记的工具：%s" % key
            steps.append({"tool": key, "name": key, "in": len(cur), "out": len(cur),
                          "drop": 0, "ms": 0, "source": "cache",
                          "error": _msg, "note": ""})
            _fail(idx, key, _msg, key)
            kept_sets.append(set(cur))
            step_specs.append(st)
            continue
        if t.get("status") == "retired":
            _msg = ("该工具已淘汰，不能参与筛选：%s"
                    % str(t.get("retire_reason") or "")[:160])
            steps.append({"tool": key, "name": t.get("name") or key, "in": len(cur),
                          "out": len(cur), "drop": 0, "ms": 0, "source": "cache",
                          "error": _msg, "note": ""})
            _fail(idx, key, _msg, t.get("name") or key)
            kept_sets.append(set(cur))
            step_specs.append(st)
            continue

        # ★ 2026-11-06 审计 T02 / §8.4 第 6 条：**筛道也是「正向产出」**，必须过资格判定。
        #   改前：这条路上只有 `apply_tool()` 的静态检查（有没有契约、淘不淘汰），
        #   行情与「反向证据不得正向用」两条完全没接线 —— 实测 `pick_ma_bull`
        #   （下跌 40 日超额 −3.683pp）可以在筛道里跑出 155 只，前端照单全收。
        #   现在：动手之前先问 `tool_router.screen_verdicts()`，判 blocked 的级不进计算，
        #   整条筛道置 ok=False 并写清原因 —— 不允许「跑出来再说」。
        vd = _verdicts.get(key) or {}
        if str(vd.get("decision") or "") == "blocked":
            _msg = "；".join(vd.get("reasons") or []) or "该工具在当前行情不能作为正向筛选器"
            steps.append({"tool": key, "name": t.get("name") or key, "in": len(cur),
                          "out": len(cur), "drop": 0, "ms": 0, "source": "cache",
                          "error": _msg, "note": "", "blocked_kind": vd.get("blocked_kind") or ""})
            _fail(idx, key, _msg, t.get("name") or key)
            kept_sets.append(set(cur))
            step_specs.append(st)
            continue

        prm = _merge_params(t, prm)
        core = load_core(trade_date, key, prm)
        if core is not None and uni_n and core.get("universe_n") not in (0, uni_n):
            # 宇宙变了（池子白名单调整 / 新股入库）：这份结果是拿另一个输入集算的，
            # 直接求交会给出一个「看起来正常但其实错」的数字，所以当作未命中重算。
            core = None
        if core is None:
            request_compute(key, prm, trade_date)
            pending = {"tool": key, "name": t.get("name") or key, "idx": idx}
            # 算不出来的（例如本机缺依赖）要**当场说清楚**，否则前端会一直「这一关正在算」，
            # 用户等 90 秒然后什么也没得到。这里只读内存里的失败记录，不触发任何计算。
            _pf = recent_fail(key, prm)
            if _pf:
                errors[key] = _pf
                _fail(idx, key, _pf, t.get("name") or key)
            break

        kept_list = core.get("kept") or []
        kept_set = set(kept_list)
        core_index[(key, tool_digest(key, prm))] = core
        n_in = len(cur)
        cur = cur & kept_set
        age = time.time() - float(core.get("mtime") or 0.0)
        steps.append({
            "tool": key,
            "name": core.get("tool_name") or t.get("name") or key,
            "kind": t.get("kind"),
            "in": n_in, "out": len(cur), "drop": n_in - len(cur),
            "keep_total": len(kept_list),   # 该工具对**全宇宙**留下多少（卡片文案的另一半）
            "ms": int(core.get("ms") or 0),
            "source": "fresh" if 0 <= age <= FRESH_SEC else "cache",
            "error": "", "note": str(core.get("note") or "")[:300],
        })
        step_specs.append({"tool": key, "params": prm})
        kept_sets.append(kept_set)
        for c, v in (core.get("score") or {}).items():
            try:
                scores[str(c)] = float(v)
            except (TypeError, ValueError):
                continue
        if core.get("score"):
            score_src = steps[-1]["name"]
            # 方向按工具 key 查表（见 toolbox._SCORE_DIR）：名字会变、key 不会
            score_src_key = str(steps[-1].get("tool") or "")
        for c, v in (core.get("marks") or {}).items():
            if v:
                c = str(c)
                marks[c] = (marks.get(c, "") + " ｜ " + str(v)).strip(" ｜")

    # ---- counted：所有未进筛道的可用工具，对**当前池子**还能剩多少 ----
    counted: dict[str, int | None] = {}
    ready = 0
    total = 0
    sel_keys = {sp["tool"] for sp in step_specs}
    try:
        tools_all = _tb.list_tools(con, include_retired=True)
    except Exception:  # noqa: BLE001
        log.exception("读取货架失败（counted 为空）")
        tools_all = []
    for t in tools_all:
        if t.get("status") == "retired":
            continue
        total += 1
        key = t["tool_key"]
        prm = dict(t.get("default_params") or {})
        core = core_index.get((key, tool_digest(key, prm)))
        if core is None:
            core = load_core(trade_date, key, prm)
            if core is not None and uni_n and core.get("universe_n") not in (0, uni_n):
                core = None
        if core is None:
            request_compute(key, prm, trade_date)
            counted[key] = None
            err = recent_fail(key, prm)
            if err:
                errors[key] = err
            continue
        ready += 1
        if key in sel_keys:
            continue                     # 已在筛道里，卡片显示的是「已在筛道」，不需要这个数
        counted[key] = len(cur & set(core.get("kept") or ()))

    # ---- rows：最终名单 + 被淘汰者的「死在第几级、为什么」 ----
    try:
        names = {str(r[0]): (r[1] or "") for r in
                 con.execute("SELECT code,name FROM stock_names")}
    except Exception:  # noqa: BLE001
        log.exception("读取股票名称失败（名单只显示代码）")
        names = {}

    n_step = len(steps)
    rows: list[dict] = []
    # 排序方向与 toolbox.run_funnel 同一张表（`_SCORE_DIR`）：+1 降序、-1 升序、
    # 0 不按分数（代码升序）。两处必须一致，否则「单工具页」和「筛台」会排出两个顺序。
    _sd = _tb.score_dir_of(score_src_key)
    if _sd == 0:
        final_sorted = sorted(cur)
    else:
        final_sorted = sorted(cur, key=lambda c: -_sd * float(scores.get(c, 0.0)))
    for c in final_sorted[:limit]:
        rows.append({
            "code": c, "name": names.get(c, ""),
            "score": round(float(scores.get(c, 0.0)), 2),
            "trace": [1] * n_step, "marks": marks.get(c, ""),
            "failed_at": -1, "reason": "",
        })

    tail = [c for c in universe if c not in cur]
    # 淘汰样本**单独给一份 limit 预算**，不从最终名单里抢额度。
    # 规格原文是「最多补到 limit 条」，但如果按「总数不超过 limit」实现，
    # 只要最终池 ≥ limit（例如 箱体谷底+准入闸门 = 679 只 > 200），
    # 「显示被淘汰的 N 只」永远是空的 —— 那这个勾选框就成了摆设。
    # 所以这里各给 limit：最终名单 ≤ limit，淘汰样本 ≤ limit，前端按勾选切换显示。
    room = limit
    for c in tail[:room]:
        fa = n_step - 1 if n_step else 0
        reason = ""
        for i, ks in enumerate(kept_sets):
            if c not in ks:
                fa = i
                reason = _drop_of(i).get(c) or ""
                break
        rows.append({
            "code": c, "name": names.get(c, ""),
            "score": round(float(scores.get(c, 0.0)), 2),
            "trace": [1] * fa + [0] * (n_step - fa),
            "marks": marks.get(c, ""),
            "failed_at": fa,
            "reason": reason or "未知（不在任一级的通过名单里）",
        })

    return {
        # ok 的口径（2026-11-06 改）：只有在**没有硬错误**时才是 True。
        # 「还有一步在算」（pending）不算失败 —— 那是进度，不是错误。
        "ok": first_error is None,
        "trade_date": trade_date,
        "universe": uni_n,
        "steps": steps,
        # final=null 是刻意的：链断了就不该再有一个"最终多少只"的数字，
        # 否则前端和脚本都会把它当成有效结论。中间值单独放 interim_final，
        # 只用于让用户看见"断之前走到了多少"。
        "final": None if first_error else len(cur),
        "interim_final": len(cur),
        "aborted_at": first_error["idx"] if first_error else None,
        "failed_tool": first_error["tool_key"] if first_error else "",
        "failed_tool_name": first_error["tool"] if first_error else "",
        "reason": first_error["reason"] if first_error else "",
        "stage_sizes": [uni_n] + [s["out"] for s in steps],
        "counted": counted,
        "pending": pending,
        "rows": [] if first_error else rows,
        "n_failed": len(tail),
        "score_src": score_src,
        "score_src_key": score_src_key,
        "score_dir": _tb.score_dir_of(score_src_key),
        "score_note": _tb.score_note_of(score_src_key),
        "errors": errors,
        "warm": {"ready": ready, "total": total},
        "spec": clean,
    }
