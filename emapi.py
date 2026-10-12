# -*- coding: utf-8 -*-
"""东财取数基座（增强功能共用，纯标准库，零第三方依赖）。

v2 修正（2026-09-27，真机接口实测后重写）：
  · 所有请求补 `_=<毫秒时间戳>`（东财风控要求）。
  · clist 的 `diff` 可能是对象（{"0":{...}}）也可能是数组，统一归一为 list。
  · datacenter 必须 source=WEB & client=WEB，filter 用 urlencode 正常编码
    （双引号 %22 / 单引号 %27 / 大小于 %3C %3E），否则 400 / 9501 / 9701。
  · 新增 ulist（批量快照）、fflow（资金流 K 线）、latest_trade_date（回退找最近交易日）、
    get_jsonp（reportapi 的 JSONP 剥离）、fast_news / announcement（资讯）。
统一口径：直连优先 → 回退系统代理；失败一律返回空/None，绝不写 0、绝不抛给上层。
"""
from __future__ import annotations
import concurrent.futures
import json
import logging
import threading
import time
import urllib.parse
import urllib.request

_EM_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
_EM_UT = "fa5fd1943c7b386f172d6893dbfba10b"
_REF_Q = "https://quote.eastmoney.com/"
_REF_DC = "https://data.eastmoney.com/"

# 直连优先（显式忽略环境代理；本机若有失效 http(s)_proxy 会直接 ProxyError）。
_DIRECT = urllib.request.build_opener(urllib.request.ProxyHandler({}))
_PROXY = urllib.request.build_opener()

_TRADE_DATE_CACHE = {"date": None, "ts": 0.0}


# --------------------------------------------------------------------------- #
# 三源竞速基座（2026-10-09）：新浪 / 腾讯 / 东财 **并发**取价，「谁先回用谁」。
#   背景：此前东财优先串行降级——东财 push2 被本机风控掐连接时，按 5s×多端点阻塞，
#         慢源把整个请求拖到十几秒（王总：点哪刷哪、8-10s 内要拿到当下数据）。
#   现在：三个源同时发，先回的先采纳，慢的/hang 的直接忽略。
#   · 整体硬超时（默认 ≤3s）—— 绝不让某个源把请求拖过 5s；
#   · 线程来自**模块级复用**的有界线程池（8 worker），绝不为单次请求新建一批线程；
#   · 只采纳「成功结果」，失败/超时静默忽略（上层原有兜底 / 负缓存保持不变）。
#   · ⚠️ 传入的 callable 内部务必自带超时（推荐 timeout≤3、retries=0），
#     否则会长期占用池内线程（future.cancel 拦不住已在跑的请求）。
# --------------------------------------------------------------------------- #
_RACE_POOL = None
_RACE_POOL_LOCK = threading.Lock()
_RACE_MAX_WORKERS = 8


def race_pool() -> "concurrent.futures.ThreadPoolExecutor":
    """模块级复用的有界线程池（懒初始化，双重检查锁）。"""
    global _RACE_POOL
    if _RACE_POOL is None:
        with _RACE_POOL_LOCK:
            if _RACE_POOL is None:
                _RACE_POOL = concurrent.futures.ThreadPoolExecutor(
                    max_workers=_RACE_MAX_WORKERS, thread_name_prefix="mkt-race")
    return _RACE_POOL


def _not_none(x) -> bool:
    return x is not None


def race_first(calls, timeout: float = 3.0, accept=None):
    """并发执行若干无参 callable，返回**第一个通过 accept 的结果**（不等最慢的那个）。

    - calls：无参可调用列表，各自独立取数（内部已做自身超时兜底）；
    - timeout：整体硬上限（秒）。到点即返回 None，绝不让某源 hang 住把请求拖长；
    - accept(x)->bool：判定「可用」的谓词，默认「非 None 即可用」。
    返回第一个通过 accept 的结果；全部失败 / 超时返回 None。线程来自 race_pool()。
    """
    if not calls:
        return None
    if accept is None:
        accept = _not_none
    deadline = time.time() + max(0.1, float(timeout))
    pool = race_pool()
    futs = [pool.submit(c) for c in calls]
    pending = set(futs)
    try:
        while pending:
            remain = deadline - time.time()
            if remain <= 0:
                break
            done, pending = concurrent.futures.wait(
                pending, timeout=remain,
                return_when=concurrent.futures.FIRST_COMPLETED)
            if not done:
                break
            for f in done:
                try:
                    r = f.result()
                except Exception:  # noqa: BLE001
                    r = None
                if accept(r):
                    return r
    finally:
        for f in pending:
            f.cancel()
    return None


def race_merge(calls, merge, complete, timeout: float = 3.0) -> dict:
    """并发**批量**取数：边完成边用 merge(acc, result) 合并；complete(acc) 为真即提前收工。

    返回 acc（即使超时也返回已合并到的部分）。
    - merge 只在**主线程**等待循环里调用 → acc 不存在并发写；工作线程只负责取数。
    - 建议 merge 内做「有值不回退」保护：不要用缺失值覆盖已取到的有效值。
    """
    acc: dict = {}
    if not calls:
        return acc
    deadline = time.time() + max(0.1, float(timeout))
    pool = race_pool()
    futs = [pool.submit(c) for c in calls]
    pending = set(futs)
    try:
        while pending:
            remain = deadline - time.time()
            if remain <= 0:
                break
            done, pending = concurrent.futures.wait(
                pending, timeout=remain,
                return_when=concurrent.futures.FIRST_COMPLETED)
            if not done:
                break
            for f in done:
                try:
                    r = f.result()
                except Exception:  # noqa: BLE001
                    r = None
                if r:
                    merge(acc, r)
            if complete(acc):
                break
    finally:
        for f in pending:
            f.cancel()
    return acc


def _safe_result(f):
    """取 future 结果；异常一律折算为 None（竞速里失败源不该把整个请求带崩）。"""
    try:
        return f.result()
    except Exception:  # noqa: BLE001
        return None


def race_priority(primary, others, grace: float = 1.4, timeout: float = 3.0, accept=None):
    """**主源优先 + 限时**的并发竞速（2026-10-09，王总「单股愿等、但要字段全」）。

    语义（三个源同时发，绝不串行干等）：
      · primary 在 grace 秒内成功返回 → 用 primary（字段最全的源，如东财含 valuation.bps）；
      · grace 内 primary 未成功（失败或还没回）→ **立即**改用「最先成功」的任一 other，不干等；
      · 到 timeout 仍无成功结果 → None。
    关键点：primary **快速失败**（如被风控 RST）时会立刻走「谁快用谁」，不会白等满 grace；
    只有 primary「挂住不回」才耗到 grace 上限，从而在「字段完整」与「不拖慢」之间两全。
    线程来自 race_pool()。
    """
    if accept is None:
        accept = _not_none
    deadline = time.time() + max(0.1, float(timeout))
    grace_dl = min(time.time() + max(0.0, float(grace)), deadline)
    pool = race_pool()
    f_primary = pool.submit(primary)
    f_others = [pool.submit(c) for c in others]
    allf = [f_primary] + f_others
    finished: dict = {}
    order: list = []           # 按（近似）完成先后记录，供「谁先回用谁」按序取
    pending = set(allf)

    def _harvest(done):
        for f in done:
            pending.discard(f)
            finished[f] = _safe_result(f)
            order.append(f)

    try:
        # 阶段1：给主源 grace 窗口；期间任何源完成都收割，但暂不采纳非主源
        while f_primary not in finished and pending and time.time() < grace_dl:
            done, _ = concurrent.futures.wait(
                pending, timeout=max(0.0, grace_dl - time.time()),
                return_when=concurrent.futures.FIRST_COMPLETED)
            if not done:
                break
            _harvest(done)
        if f_primary in finished and accept(finished[f_primary]):
            return finished[f_primary]

        def _pick_any():
            for f in order:
                if f is not f_primary and accept(finished.get(f)):
                    return finished[f]
            return None

        # 阶段2：谁先成功用谁（先看已完成的其它源，再等剩余到 timeout）
        r = _pick_any()
        if r is not None:
            return r
        while pending and time.time() < deadline:
            done, _ = concurrent.futures.wait(
                pending, timeout=max(0.0, deadline - time.time()),
                return_when=concurrent.futures.FIRST_COMPLETED)
            if not done:
                break
            _harvest(done)
            if f_primary in finished and accept(finished[f_primary]):
                return finished[f_primary]
            r = _pick_any()
            if r is not None:
                return r
        # 主源可能在 grace 之后才成功 —— 最后再兜一次
        if f_primary in finished and accept(finished[f_primary]):
            return finished[f_primary]
        return _pick_any()
    finally:
        for f in allf:
            if not f.done():
                f.cancel()


def _ts() -> str:
    return str(int(time.time() * 1000))


def _num(v):
    """东财常用占位符（'-'、'--'、''、None）统一折算为 None；能转则转 float。"""
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip().replace(",", "")
    if s in ("", "-", "--", "—", "None", "null"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def num2(v, d=2):
    """两位小数展示，缺值返回 —。"""
    n = _num(v)
    return "—" if n is None else f"{n:.{d}f}"


def secid(symbol: str) -> str:
    """任意代码 → 东财 secid（A股 / 基金 / 美股 / 债券 / 指数）。
    - A股：sh/sz/bj 前缀，或 6/0/3/688/8/4 开头数字
    - 基金：51/58 → 沪(1.)；15/16/18/50 → 深(0.)
    - 美股：usXXXX / XXXX.US / 纯字母 → 105.TICKER
    - 指数：000001→1.000001（上证）等需显式带前缀
    """
    s = (symbol or "").strip()
    low = s.lower()
    if low.startswith("us") and len(low) > 2:
        return "105." + low[2:].upper()
    if ".us" in low:
        return "105." + low.split(".us")[0].upper()
    if low.isalpha():                     # 纯字母 → 视为美股 ticker
        return "105." + low.upper()
    if low.startswith(("sh", "sz", "bj")):
        code = low[2:]
        market = "1" if low.startswith("sh") else "0"
        return f"{market}.{code}"
    if low.isdigit():
        code = low
        if code[:2] in ("51", "58"):
            market = "1"
        elif code[:2] in ("15", "16", "18", "50"):
            market = "0"
        elif code[:1] == "6":
            market = "1"
        else:
            market = "0"
        return f"{market}.{code}"
    if "." in low:                        # 已是 secid，如 1.600519 / 100.NDX
        return low
    return s


def secid_parts(symbol: str):
    sid = secid(symbol)
    m, c = sid.split(".", 1)
    return m, c, sid


# 腾讯快照 / K 线：免费 / 无 ut 风控 / 对 push2 限流极其稳健，作为多资产面板的终极 fallback
def tencent_quote(symbol: str, secid_override: str = "", timeout: int = 8):
    """腾讯 qt.gtimg.cn 单只快照（A 股 / ETF / 港股 / 美股）。
    入参支持：6位数字代码（A 股/ETF）或 'usAAPL'/'hk02318' 或 secid '1.510300'。
    返回 {'code','name','market','price','pct','open','high','low','prev_close','time'}，
    失败返回 None。"""
    if not symbol:
        return None
    s = symbol.strip()
    if "." in s:                            # 已是 secid
        m, c = s.split(".", 1)
    elif s.startswith("us"):
        m, c = "105", s[2:]
    elif s.startswith("hk"):
        m, c = "116", s[2:]
    elif s.startswith(("sh", "sz")):
        m, c = ("1" if s.startswith("sh") else "0"), s[2:]
    elif s.isdigit():
        m, c = ("1" if s.startswith(("5", "6", "9")) else "0"), s
    else:
        return None
    prefix = {"1": "sh", "0": "sz", "105": "us", "116": "hk"}.get(m, "")
    sym = prefix + c
    url = f"http://qt.gtimg.cn/q={sym}"
    headers = {"User-Agent": "Mozilla/5.0", "Referer": "https://gu.qq.com/"}
    try:
        req = urllib.request.Request(url, headers=headers)
        with _DIRECT.open(req, timeout=timeout) as resp:
            raw = resp.read().decode("gbk", "ignore")
    except Exception:
        try:
            req = urllib.request.Request(url, headers=headers)
            with _PROXY.open(req, timeout=timeout) as resp:
                raw = resp.read().decode("gbk", "ignore")
        except Exception:
            return None
    if "=" not in raw:
        return None
    txt = raw.split("=", 1)[1].strip().strip(";").strip('"')
    parts = txt.split("~")
    if len(parts) < 32:
        return None
    def g(i):
        return parts[i] if i < len(parts) else ""
    p = _num(g(3)); pc = _num(g(32))
    return {
        "code": c, "market": m, "name": g(1),
        "price": p, "change": _num(g(31)), "pct": pc,
        "open": _num(g(5)), "high": _num(g(33)), "low": _num(g(34)),
        "prev_close": _num(g(4)),
        "time": g(30),
    }


def tencent_quote_batch(symbols, timeout: int = 10):
    """腾讯批量快照；输入 secid 列表或带 us/前缀的代码，逐个请求（避免单次单接口限流）。"""
    out = []
    for sym in symbols:
        r = tencent_quote(sym)
        if r:
            out.append(r)
    return out


# secid 市场前缀 → 腾讯符号前缀（与 tencent_quote 内部映射保持一致）
_TX_SECID_PREFIX = {"1": "sh", "0": "sz", "105": "us", "116": "hk"}


def tencent_quote_multi(secids, timeout: int = 3) -> dict:
    """腾讯批量快照（**单请求** qt.gtimg.cn/q=sym1,sym2,...），供多资产面板竞速用。

    入参 secid 列表（如 ["1.000001","1.510300","105.AAPL"]）；不支持的市场
    （如美股指数 100.* —— 腾讯无对应符号）自动跳过（返回里就没有，交给其它源）。
    返回 {secid: {'code','name','price','pct','prev_close','time'}}；失败返回 {}。
    """
    sym2sid: dict = {}
    for sid in secids or []:
        s = (sid or "").strip()
        if not s:
            continue
        if "." not in s:
            s = secid(s)
        try:
            m, c = s.split(".", 1)
        except ValueError:
            continue
        pfx = _TX_SECID_PREFIX.get(m)
        if not pfx:
            continue
        sym2sid[pfx + c] = sid
    if not sym2sid:
        return {}
    url = "http://qt.gtimg.cn/q=" + ",".join(sym2sid.keys())
    headers = {"User-Agent": "Mozilla/5.0", "Referer": "https://gu.qq.com/"}
    raw = None
    for opener in (_DIRECT, _PROXY):
        try:
            req = urllib.request.Request(url, headers=headers)
            with opener.open(req, timeout=timeout) as resp:
                raw = resp.read().decode("gbk", "ignore")
            break
        except Exception:  # noqa: BLE001
            raw = None
    if not raw:
        return {}
    out: dict = {}
    for line in raw.splitlines():
        line = line.strip()
        if not line.startswith("v_") or "=" not in line:
            continue
        head, body = line.split("=", 1)
        sid = sym2sid.get(head[2:].strip())
        if not sid:
            continue
        parts = body.strip().strip(";").strip('"').split("~")
        if len(parts) < 33:
            continue
        out[sid] = {
            "code": parts[2] if len(parts) > 2 else "",
            "name": parts[1] if len(parts) > 1 else "",
            "price": _num(parts[3]),
            "pct": _num(parts[32]),
            "prev_close": _num(parts[4]),
            "time": parts[30] if len(parts) > 30 else "",
        }
    return out


def _get_text(url: str, referer: str, timeout: int = 12, retries: int = 2,
              encoding: str = "utf-8-sig"):
    headers = {"User-Agent": _EM_UA, "Referer": referer, "Accept": "*/*"}
    last_err = None
    for attempt in range(retries + 1):
        for opener in (_DIRECT, _PROXY):
            try:
                req = urllib.request.Request(url, headers=headers)
                with opener.open(req, timeout=timeout) as resp:
                    return resp.read().decode(encoding, "ignore")
            except Exception as e:  # noqa: BLE001
                last_err = e
        if attempt < retries:
            time.sleep(0.3 * (attempt + 1))
    logging.debug("emapi 取数失败 %s（%s）", url[:110], last_err)
    return None


def get_json(url: str, referer: str = _REF_DC, timeout: int = 12, retries: int = 2):
    txt = _get_text(url, referer, timeout=timeout, retries=retries)
    if not txt:
        return None
    try:
        return json.loads(txt)
    except Exception:  # noqa: BLE001
        return None


def get_jsonp(url: str, referer: str = _REF_DC, timeout: int = 12, retries: int = 2):
    """reportapi 等返回 JSONP（cb({...})），剥壳后 json 化。"""
    txt = _get_text(url, referer, timeout=timeout, retries=retries)
    if not txt:
        return None
    txt = txt.strip()
    i = txt.find("(")
    if i >= 0 and txt.endswith(")"):
        txt = txt[i + 1:-1]
    try:
        return json.loads(txt)
    except Exception:  # noqa: BLE001
        return None


# --------------------------------------------------------------------------- #
# push2 行情
# --------------------------------------------------------------------------- #
def quote(sid: str, timeout: int = 8, retries: int = 1):
    """东财 push2 单只快照（通用，含基金/美股/债券）。返回
    {price,pct,name,code,open,high,low,prev_close,pe,pb} 或 None。"""
    if "." not in (sid or ""):
        sid = secid(sid)
    url = ("https://push2.eastmoney.com/api/qt/stock/get"
           f"?secid={sid}&fields=f43,f44,f45,f46,f57,f58,f60,f162,f167,f168,f169,f170"
           f"&ut={_EM_UT}&invt=2&fltt=2&_={_ts()}")
    j = get_json(url, _REF_Q, timeout=timeout, retries=retries)
    d = (j or {}).get("data")
    if not d:
        return None
    return {"code": d.get("f57"), "name": d.get("f58"), "price": _num(d.get("f43")),
            "pct": _num(d.get("f170") if d.get("f170") is not None else d.get("f169")),
            "open": _num(d.get("f46")), "high": _num(d.get("f44")),
            "low": _num(d.get("f45")), "prev_close": _num(d.get("f60")),
            "pe": _num(d.get("f162")), "pb": _num(d.get("f167"))}


def ulist(secids, fields: str = "f2,f3,f4,f12,f14,f15,f16,f17,f18", timeout: int = 8):
    """批量快照（指数/ETF/美股一网打尽）。secids 为列表或逗号串。
    返回 [{code,name,price,pct,...}]，失败返回 []。
    v3（2026-09-27 晚）：多 ut + push2/push2his 双端点 fallback，同 clist 行为一致。"""
    if isinstance(secids, (list, tuple)):
        secids = ",".join(secids)
    last_err = None
    for ut in UT_CANDIDATES:
        for host in ("https://push2.eastmoney.com/api/qt/ulist.np/get",
                     "https://push2his.eastmoney.com/api/qt/ulist.np/get"):
            url = (f"{host}?fltt=2&invt=2&secids={urllib.parse.quote(secids)}"
                   f"&fields={fields}&ut={ut}&_={_ts()}")
            j = get_json(url, _REF_Q, timeout=timeout, retries=0)
            if not j:
                last_err = "no-json"; continue
            if j.get("rc") not in (0, None):
                last_err = f"rc={j.get('rc')}"; continue
            diff = (((j or {}).get("data") or {}).get("diff")) or []
            if isinstance(diff, dict):
                diff = [diff[k] for k in sorted(diff.keys(), key=lambda x: int(x) if str(x).isdigit() else x)]
            if not diff:
                last_err = "empty-diff"; continue
            out = []
            for d in diff or []:
                out.append({"code": d.get("f12"), "name": d.get("f14"), "price": _num(d.get("f2")),
                            "pct": _num(d.get("f3")), "chg": _num(d.get("f4")),
                            "high": _num(d.get("f15")), "low": _num(d.get("f16")),
                            "open": _num(d.get("f17")), "prev_close": _num(d.get("f18"))})
            return out
    return []


# 多个 ut 候选（push2 风控绑定 ut，老 ut 易触发 102/限流）
UT_CANDIDATES = ("fa5fd1943c7b386f172d6893dbfba10b",
                 "b2884a393a59ad64002292a3e90d46a5")

# 资金流排行推荐 fs（带 +f:!2 排除停牌，含北交所）
FS_FUND_RANK = ("m:0+t:6+f:!2,m:0+t:13+f:!2,m:0+t:80+f:!2,"
                "m:1+t:2+f:!2,m:1+t:23+f:!2,m:0+t:7+f:!2,m:1+t:3+f:!2")

# 板块资金流
FS_BOARD_FUND = "m:90+t:2"


def _best_ut(idx: int = 0) -> str:
    return UT_CANDIDATES[idx % len(UT_CANDIDATES)]


def clist(fs: str, fields: str, pz: int = 30, pn: int = 1, fid: str = "f3", po: int = 1,
          referer: str = _REF_Q, timeout: int = 12, retries: int = 1):
    """东财 clist 排行/列表（资金流、选股等）。fs 为板块/筛选表达式。
    统一把 diff（对象或数组）归一为 list。失败返回 []。
    v3（2026-09-27 晚）：多 ut 重试，按 fid 自动推荐 referer。
    2026-10-09：新增 timeout / retries 参数——盘中口径竞速时用 timeout=2、retries=0
    快速失败，避免 push2 被风控时把请求拖长。"""
    # 资金流类请求的 referer 应为数据中心
    if fid in ("f62", "f184", "f164", "f174", "f267"):
        referer = "https://data.eastmoney.com/zjlx/list.html"
    last_err = None
    for ut in UT_CANDIDATES:
        url = ("https://push2.eastmoney.com/api/qt/clist/get"
               f"?fid={fid}&fs={urllib.parse.quote(fs)}&fields={fields}"
               f"&po={po}&pz={pz}&pn={pn}&ut={ut}&invt=2&fltt=2&np=1&_={_ts()}")
        j = get_json(url, referer, timeout=timeout, retries=retries)
        if not j:
            last_err = "no-json"; continue
        if j.get("rc") not in (0, None):
            last_err = f"rc={j.get('rc')}"; continue
        diff = ((j.get("data") or {}).get("diff")) or []
        if isinstance(diff, dict):
            diff = [diff[k] for k in sorted(diff.keys(),
                    key=lambda x: int(x) if str(x).isdigit() else x)]
        if diff:
            return list(diff)
        last_err = "empty-diff"
    logging.debug("clist 三 ut 全败：%s", last_err)
    return []


def fflow(sid: str, klt: int = 101, lmt: int = 6, timeout: int = 10):
    """东财资金流 K 线（个股/指数）。返回 [{date,main_net,small_net,mid_net,big_net,huge_net,
    main_pct,close,pct,amount}]，失败返回 []。
    字段序（官方）：f51日期 f52主力 f53小单 f54中单 f55大单 f56超大单
                   f57主力占比 f58小单占比 f59中单占比 f60大单占比 f61超大单占比
                   f62收盘 f63涨跌幅 f64成交额 f65冗余
    v3（2026-09-27 晚）：多 ut + push2/push2his 双端点 fallback。"""
    if "." not in (sid or ""):
        sid = secid(sid)
    endpoints = ("https://push2.eastmoney.com/api/qt/stock/fflow/kline/get",
                 "https://push2his.eastmoney.com/api/qt/stock/fflow/kline/get")
    last_err = None
    for ep in endpoints:
        for ut in UT_CANDIDATES:
            url = (f"{ep}?secid={sid}"
                   f"&fields1=f1,f2,f3,f7"
                   f"&fields2=f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61,f62,f63,f64,f65"
                   f"&klt={klt}&lmt={lmt}&ut={ut}&_={_ts()}")
            j = get_json(url, _REF_Q, timeout=timeout, retries=1)
            if not j:
                last_err = "no-json"; continue
            if j.get("rc") not in (0, None):
                last_err = f"rc={j.get('rc')}"; continue
            klines = ((j.get("data") or {}).get("klines")) or []
            out = []
            for line in klines:
                p = str(line).split(",")
                if len(p) < 13:
                    continue
                out.append({"date": p[0], "main_net": _num(p[1]), "small_net": _num(p[2]),
                            "mid_net": _num(p[3]), "big_net": _num(p[4]), "huge_net": _num(p[5]),
                            "main_pct": _num(p[6]), "close": _num(p[11]), "pct": _num(p[12]),
                            "amount": _num(p[13]) if len(p) > 13 else None})
            if out:
                return out
            last_err = "empty-klines"
    logging.debug("fflow 全败：%s", last_err)
    return []


# --------------------------------------------------------------------------- #
# datacenter 结构化报表
# --------------------------------------------------------------------------- #
def datacenter(report_name: str, columns: str = "ALL", flt: str = "", sort_columns: str = "",
               sort_types: str = "-1", page_size: int = 50, page_number: int = 1,
               quote_columns: str = "", timeout: int = 12) -> dict:
    """东财 datacenter 报表。返回 {ok, rows, err, code}。"""
    params = {
        "reportName": report_name, "columns": columns, "quoteColumns": quote_columns,
        "filter": flt, "pageNumber": str(page_number), "pageSize": str(page_size),
        "sortColumns": sort_columns, "sortTypes": sort_types,
        "source": "WEB", "client": "WEB", "_": _ts(),
    }
    url = "https://datacenter-web.eastmoney.com/api/data/v1/get?" + urllib.parse.urlencode(params)
    j = get_json(url, _REF_DC, timeout=timeout, retries=1)
    if not j:
        return {"ok": False, "rows": [], "err": "东财接口不可达或返回非 JSON", "code": None}
    res = j.get("result")
    if res and res.get("data"):
        return {"ok": True, "rows": res["data"], "err": None, "code": 0}
    return {"ok": False, "rows": [], "err": j.get("message") or "无数据", "code": j.get("code")}


def latest_valuation_date(force: bool = False, back: int = 15, max_seconds: int = 6) -> str | None:
    """最近一个有**估值数据**的日期（YYYY-MM-DD），带 10 分钟缓存。

    ⚠️ 命名故意区别于 auction.latest_trade_date()：交易日历 = 开不开市；
    本函数 = 东财估值表里哪天有值（停牌/未披露时可能晚于交易日）。
    二者是**不同的尺**，不许同名——2026-10-03 起改名，避免调用方分不清用的是哪把。
    取数顺序：先问权威交易日历（auction），拿到空/None 才逐日回退试探估值接口。

    max_seconds：整体超时预算（默认 6s）。东财不可达时逐日回退会反复撞超时，
    必须在预算内放弃，否则上层 _val_snapshot 会被拖挂死（见 2026-10 智能选股死按钮修复）。
    """
    now = time.time()
    today = time.strftime("%Y-%m-%d")
    if (not force and _TRADE_DATE_CACHE["date"]
            and now - _TRADE_DATE_CACHE["ts"] < 600
            and str(_TRADE_DATE_CACHE["date"]) >= today):
        return _TRADE_DATE_CACHE["date"]
    deadline = now + max_seconds
    try:
        from auction import latest_trade_date as _au_ltd
        au = _au_ltd()
        if au:
            _TRADE_DATE_CACHE.update({"date": au, "ts": now})
            return au
    except Exception:  # noqa: BLE001 —— 日历不可用时退回逐日试探，不阻断
        pass
    import datetime as _dt
    d = _dt.date.today()
    for _ in range(back):
        if time.time() > deadline:        # 整体预算耗尽，立即放弃，绝不挂死
            return None
        ds = d.strftime("%Y-%m-%d")
        r = datacenter("RPT_VALUEANALYSIS_DET", flt="(TRADE_DATE='%s')" % ds, page_size=1)
        if r["ok"]:
            _TRADE_DATE_CACHE.update({"date": ds, "ts": now})
            return ds
        # 9201=该日无数据 → 继续回退；9701=限流 → 短暂等待后重试一次
        if r.get("code") == 9701:
            time.sleep(0.6)
        d -= _dt.timedelta(days=1)
    return None


# --------------------------------------------------------------------------- #
# 资金流排行（datacenter 结构化报表 —— 未受 push2 风控影响，作为主源）
# --------------------------------------------------------------------------- #
# 2026-10-09 定案：东财 push2 / push2his 对本机密集程序请求触发风控（RST / 空响应），
# 但 datacenter-web（本文件 datacenter()）实测稳定可用：
#   · 个股主力净流入排行 → RPT_DMSK_TS_STOCKNEW
#   · 行业板块资金流     → RPT_INDUSTRY_FUNDFLOW
#   · 概念板块资金流     → RPT_CONCEPT_FUNDFLOW
# 口径：PRIME_INFLOW / NET_INFLOW = 主力净流入 = 超大单净 + 大单净，单位：元。
_FF_DATE_CACHE: dict = {}     # report_name -> {"date":..., "ts":...}
_FF_DATE_TTL = 600            # 10 分钟

# 个股主力净流入排行所需字段（datacenter 报表字段名，非 push2 的 f 系）
_STOCK_FF_COLS = ("SECURITY_CODE,SECURITY_NAME_ABBR,PRIME_INFLOW,CLOSE_PRICE,CHANGE_RATE,"
                  "SUPERDEAL_INFLOW,SUPERDEAL_OUTFLOW,BIGDEAL_INFLOW,BIGDEAL_OUTFLOW")
# 板块资金流报表按 kind 选择（行业 / 概念）
_BOARD_FF_REPORT = {"industry": "RPT_INDUSTRY_FUNDFLOW",
                    "concept": "RPT_CONCEPT_FUNDFLOW"}


def _diff(a, b):
    """净额 = 流入 - 流出；两端都缺时返回 None（绝不写 0 冒充缺失）。"""
    va, vb = _num(a), _num(b)
    if va is None and vb is None:
        return None
    return (va or 0.0) - (vb or 0.0)


def latest_report_date(report_name: str, force: bool = False, back: int = 12,
                       max_seconds: float = 8.0) -> str | None:
    """某个 datacenter 日报表里「最近一个有数据的交易日」（YYYY-MM-DD）。

    ⚠️ 与 auction.latest_trade_date() 的区别：交易日历答的是「今天开不开市」，
    本函数答的是「该报表哪一天已经有值」。二者可能相差一天——次日盘前，日历已是新交易日，
    但风控报表数据还没生成（实测 2026-10-09 盘前日历=10-09，而 RPT_DMSK_TS_STOCKNEW 只有
    10-08 有值）。故一律以「有数据」为准，**绝不硬编码日期**，也绝不拿日历日冒充数据日。
    带 10 分钟缓存；起探日优先用权威交易日历（auction）。
    """
    now = time.time()
    today = time.strftime("%Y-%m-%d")
    hit = _FF_DATE_CACHE.get(report_name)
    if (not force and hit and now - float(hit.get("ts") or 0) < _FF_DATE_TTL
            and str(hit.get("date") or "") >= today):
        return hit.get("date")
    import datetime as _dt
    start = None
    try:
        from auction import latest_trade_date as _au
        start = _au()
    except Exception:  # noqa: BLE001 —— 日历不可用就用今天起探，不阻断
        start = None
    d = None
    if start:
        try:
            d = _dt.date(int(start[:4]), int(start[5:7]), int(start[8:10]))
        except Exception:  # noqa: BLE001
            d = None
    if d is None:
        d = _dt.date.today()
    deadline = now + max_seconds
    for _ in range(max(1, back)):
        if time.time() > deadline:        # 超预算立即放弃，绝不挂死
            break
        ds = d.strftime("%Y-%m-%d")
        r = datacenter(report_name, columns="ALL",
                       flt="(TRADE_DATE='%s')" % ds, page_size=1)
        if r.get("ok"):
            _FF_DATE_CACHE[report_name] = {"date": ds, "ts": now}
            return ds
        if r.get("code") == 9701:          # 限流 → 稍候重试同一天一次
            time.sleep(0.6)
            r2 = datacenter(report_name, columns="ALL",
                            flt="(TRADE_DATE='%s')" % ds, page_size=1)
            if r2.get("ok"):
                _FF_DATE_CACHE[report_name] = {"date": ds, "ts": now}
                return ds
        d -= _dt.timedelta(days=1)
    return None


def fundflow_rank(pz: int = 30, date: str = "", timeout: int = 12) -> dict:
    """个股主力净流入排行（datacenter RPT_DMSK_TS_STOCKNEW，未受 push2 风控影响）。

    返回 {ok, rows, date, source, reason}。rows 每项：
      code / name / price / pct / main_net(主力净流入,元) / huge_net(超大单净,元)
      / big_net(大单净,元) / main_pct(该报表未提供主力净占比列，恒为 None，前端显示 «—»)。
    date 为真实数据交易日（自动回退到最近有数据的交易日），绝不硬编码。
    """
    pz = max(1, min(int(pz or 30), 100))
    ds = date or latest_report_date("RPT_DMSK_TS_STOCKNEW")
    if not ds:
        return {"ok": False, "rows": [], "date": None, "source": "eastmoney_datacenter",
                "reason": "datacenter 个股资金流报表回退 12 日内均无数据"}
    r = datacenter("RPT_DMSK_TS_STOCKNEW", columns=_STOCK_FF_COLS,
                   flt="(TRADE_DATE='%s')" % ds,
                   sort_columns="PRIME_INFLOW", sort_types="-1",
                   page_size=pz, timeout=timeout)
    if not r.get("ok"):
        return {"ok": False, "rows": [], "date": ds, "source": "eastmoney_datacenter",
                "reason": r.get("err") or "datacenter 个股资金流报表取数失败"}
    rows = []
    for x in r.get("rows") or []:
        rows.append({
            "code": x.get("SECURITY_CODE"),
            "name": x.get("SECURITY_NAME_ABBR"),
            "price": _num(x.get("CLOSE_PRICE")),
            "pct": _num(x.get("CHANGE_RATE")),
            "main_net": _num(x.get("PRIME_INFLOW")),
            "main_pct": None,          # 该报表无主力净占比列，如实留空，绝不硬造
            "huge_net": _diff(x.get("SUPERDEAL_INFLOW"), x.get("SUPERDEAL_OUTFLOW")),
            "big_net": _diff(x.get("BIGDEAL_INFLOW"), x.get("BIGDEAL_OUTFLOW")),
        })
    return {"ok": bool(rows), "rows": rows, "date": ds,
            "source": "eastmoney_datacenter", "reason": None if rows else "无数据"}


def board_fundflow_rank(kind: str = "industry", pz: int = 30, date: str = "",
                        timeout: int = 12) -> dict:
    """板块主力净流入排行（datacenter RPT_INDUSTRY_FUNDFLOW / RPT_CONCEPT_FUNDFLOW）。

    返回 {ok, rows, date, source, reason}。rows 每项：
      board_code / board_name / pct / main_net(主力净流入,元) / main_pct(主力净占比,%)
      / huge_net(超大单净,元) / big_net(大单净,元)。
    口径：NET_INFLOW = 主力净流入 = SUPERDEAL_NET + BIGDEAL_NET（实测两细分之和与之一致）。
    """
    report = _BOARD_FF_REPORT.get(kind) or _BOARD_FF_REPORT["industry"]
    pz = max(1, min(int(pz or 30), 200))
    ds = date or latest_report_date(report)
    if not ds:
        return {"ok": False, "rows": [], "date": None, "source": "eastmoney_datacenter",
                "reason": "datacenter 板块资金流报表回退 12 日内均无数据"}
    r = datacenter(report, columns="ALL", flt="(TRADE_DATE='%s')" % ds,
                   sort_columns="NET_INFLOW", sort_types="-1",
                   page_size=pz, timeout=timeout)
    if not r.get("ok"):
        return {"ok": False, "rows": [], "date": ds, "source": "eastmoney_datacenter",
                "reason": r.get("err") or "datacenter 板块资金流报表取数失败"}
    rows = []
    for x in r.get("rows") or []:
        rows.append({
            "board_code": x.get("BOARD_CODE"),
            "board_name": x.get("BOARD_NAME"),
            "pct": _num(x.get("CHANGE_RATE")),
            "main_net": _num(x.get("NET_INFLOW")),
            "main_pct": _num(x.get("NET_INFLOW_RATIO")),
            "huge_net": _num(x.get("SUPERDEAL_NET")),
            "big_net": _num(x.get("BIGDEAL_NET")),
        })
    return {"ok": bool(rows), "rows": rows, "date": ds,
            "source": "eastmoney_datacenter", "reason": None if rows else "无数据"}


# --------------------------------------------------------------------------- #
# 研报 / 资讯
# --------------------------------------------------------------------------- #
def report_list(page_size: int = 30, page_no: int = 1, begin_time: str = "2026-01-01",
                end_time: str = "", code: str = "", qtype: int = 0, timeout: int = 12):
    """东财研报列表（reportapi）。返回 {ok,hits,rows,err}。rows 保留原始字段。"""
    import datetime as _dt
    if not end_time:
        end_time = _dt.date.today().strftime("%Y-%m-%d")
    params = {
        "cb": "cb", "pageSize": str(page_size), "pageNo": str(page_no),
        "beginTime": begin_time, "endTime": end_time, "qType": str(qtype),
        "fields": "", "rating": "", "ratingChange": "",
        "industryCode": "*", "industry": "*", "_": _ts(),
    }
    if code:
        params["code"] = code
    url = "https://reportapi.eastmoney.com/report/list?" + urllib.parse.urlencode(params)
    j = get_jsonp(url, referer="https://data.eastmoney.com/report/", timeout=timeout, retries=1)
    if not j:
        return {"ok": False, "rows": [], "hits": 0, "err": "研报接口不可达"}
    return {"ok": True, "rows": j.get("data") or [], "hits": j.get("hits") or 0, "err": None}


def fast_news(num: int = 30, column: int = 102, timeout: int = 10):
    """东财 7×24 快讯。返回 {ok, rows, err}。"""
    url = ("https://np-listapi.eastmoney.com/comm/web/getFastNewsList"
           f"?client=web&biz=web_724&fastColumn={column}&sortEnd=&pageSize={num}"
           f"&req_trace={_ts()}")
    j = get_json(url, referer="https://kuaixun.eastmoney.com/", timeout=timeout, retries=1)
    d = (j or {}).get("data") or {}
    rows = d.get("fastNewsList") or []
    if not rows and not (j or {}).get("data"):
        return {"ok": False, "rows": [], "err": (j or {}).get("message") or "资讯接口不可达"}
    return {"ok": True, "rows": rows, "err": None}


def announcement(code: str, num: int = 30, timeout: int = 10):
    """东财个股公告。返回 {ok, rows, err}。"""
    url = ("https://np-anotice-stock.eastmoney.com/api/security/ann"
           f"?sr=-1&page_size={num}&page_index=1&ann_type=A&client_source=web"
           f"&stock_list={urllib.parse.quote(code)}&f_node=0&s_node=0&_={_ts()}")
    j = get_json(url, referer="https://data.eastmoney.com/notices/", timeout=timeout, retries=1)
    d = (j or {}).get("data") or {}
    rows = d.get("list") or []
    if j is None:
        return {"ok": False, "rows": [], "err": "公告接口不可达"}
    return {"ok": True, "rows": rows, "err": None}
