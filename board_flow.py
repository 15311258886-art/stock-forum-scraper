# -*- coding: utf-8 -*-
"""板块资金流 —— 多源 / 节流缓存 / 分时采样累积（独立隔离模块）。

【为什么要重写】（2026-09-28，王总反馈「资金流向 → 板块资金流」显示
「接口异常：未取到板块资金流排行（东财接口不可达）」）

  原实现：moneyflow.board_rank() → emapi.clist()，**唯一依赖东财 push2**。
  2026-09-28 上午本机实测定性：
    · 东财 push2 / push2his / 数字前缀镜像：**对同一 IP 的密集请求会直接掐连接**
      （Python 侧一律 `RemoteDisconnected`，curl 侧卡在 TLS 重协商循环），
      浏览器里手动刷新没事，程序连打十几次请求必被封；冷却 75s 后仍未恢复。
    · 新浪 `vip.stock.finance.sina.com.cn/.../MoneyFlow.ssl_bkzj_bk`：连续 4 次实测
      全部 200，一次给 60 条行业板块，净额 / 占比 / 涨跌幅齐全，**长期稳定**。
    · 同花顺 `d.10jqka.com.cn`：资金流排行页需登录（401）；`ssl_bkzj_zjlrqs`
      （资金流入趋势）实测返回空数组，**无板块分钟资金流**。
  结论：不是代码写错，是**架构押注了单一脆弱源**。

【本模块架构】
  ① 快照（排行表）：新浪为主源（稳定），东财为兜底（可用时附带超大单 / 大单细分）。
  ② 分时曲线：两级，**不混算**
       a) 东财 `fflow/kline?klt=1` —— Top N 板块串行节流取，**一次拿到当日全部分钟**，
          能立刻画出从 09:30 起的完整曲线；失败则该板块跳过。
       b) 兜底：交易时段按节奏（viewer/rhythm.py，2026-10-30 起盘中每 30 分钟一轮）采快照
          入库（board_flow_samples），当天累积成曲线。
     响应里带 `source` 字段，界面按实际来源标注。
  ③ 节流缓存：快照 120s / 曲线按节奏（盘中 3600s、盘后 86400s，见 rhythm.intraday_curve_ttl）。
     把对外请求从「每次打开面板」降到「一小时一次」，从根上避开东财风控
     （王总明确：**不需要实时，几分钟一发即可**；2026-10-30 进哥进一步明确「一小时一次都行」）。

【口径诚实声明（重要）】
  · 新浪 `netamount` 是**全部资金净流入额**（元），`ratioamount` 为其占比；
    涨跌幅 `avg_changeratio` 是小数，此处已 ×100 归一为百分比。
  · 东财 `f62` 是**主力净流入**（= 超大单 + 大单），`f184` 为主力净占比。
  · 两者口径**不同**，故绝不把两个源的点拼进同一条曲线；每条曲线只有一个来源。
  · 取不到的字段一律 None（界面显示 «—»），**绝不写 0**。

对外函数：
  ensure_schema(conn)                          建表（幂等，1 张表 + 1 索引）
  snapshot(kind, pz, force)                    板块资金流快照（缓存 + 多源）
  intraday(kind, top, force)                   分时曲线（东财优先 → 采样兜底）
  maybe_sample(conn)                           交易时段节流采样一次（供后台线程调）
  clear_cache()                                清内存缓存（强制重试用）
"""
from __future__ import annotations

import concurrent.futures
import json
import logging
import os
import re
import sys
import threading
import time
import urllib.parse
import urllib.request
from datetime import datetime

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------- 配置常量
KINDS = {
    "industry": {
        "label": "行业板块",
        "em_fs": "m:90+t:2+f:!50",       # 东财行业板块（排除 f:!50 伪板块）
        "sina_fenlei": "0",              # 新浪 fenlei=0 → 行业（返回 new_* 代码）
    },
    "concept": {
        "label": "概念板块",
        "em_fs": "m:90+t:3+f:!50",       # 东财概念板块
        "sina_fenlei": "1",              # 新浪 fenlei=1 → 概念（返回 gn_* 代码）
    },
}
# ⚠️ 2026-09-28 实测订正（原先两个值接反了，导致「行业板块」里全是概念名）：
#    fenlei=1 → category=gn_wjqy / name=皖江区域 / cate_type=1 → **概念**
#    fenlei=0 → category=new_qczz / name=汽车制造 / cate_type=0 → **行业**
#    判据：gn_ = 概念代码前缀，new_ = 行业代码前缀。改前务必先用真实响应复核。

# 伪板块黑名单：新浪把「宽基 / 风格 / 资金属性」也算进板块排行，它们净额极大
# （等于把全市场资金重复计一遍），会把真正的行业/题材挤出曲线。2026-09-28 实测
# 榜首常年是「融资融券 / 深股通 / QFII重仓」这类项。只剔明显非行业·非题材的，
# 不做过度过滤（宁少剔，不错剔）。
_BOARD_NOISE = (
    "融资融券", "沪股通", "深股通", "港股通", "基金重仓", "QFII重仓", "社保重仓",
    "券商重仓", "机构重仓", "信托重仓", "保险重仓", "百元股", "低价股", "次新股",
    "ST板块", "高送转", "破净股", "预盈预增", "预亏预减", "上证50", "沪深300",
    "中证500", "央企50", "成份股", "深成指", "GDR", "B股", "AH股", "创业板",
    "科创板", "大盘", "中盘", "小盘",
)


def _is_noise_board(name: str) -> bool:
    """明显非「行业 / 题材」的宽基·风格·资金属性板块 → 不进曲线与排行。"""
    n = (name or "").strip().upper()
    if not n:
        return True
    return any(k.upper() in n for k in _BOARD_NOISE)
DEFAULT_KIND = "industry"

SNAP_TTL = 120            # 快照缓存（秒）
MEM_TTL = 300             # 板块代表股（成分股）缓存（秒）
MEM_FAIL_TTL = 60         # 代表股取数失败后的冷却（秒）——免得每次刷新都把同一批请求重打一遍
MEM_NUM = 12              # 每个板块给几只代表股（龙头口径）
                          # 2026-09-28：王总窗口最大化 2062×1246 → 代表股列可用宽约 1427px，
                          # 实测每只约 101px（含间隙），12 只≈1210px 仍留 ~215px 余量、单行不换行；
                          # 8 只只用 806px，右侧显得空。故定 12。
MEM_TOP = 60              # 只为表格前 N 行取代表股（每板块 1 次新浪请求，控总量）
                          # 2026-09-28：前端最多渲染 60 行（app.js snapRows.slice(0,60)），
                          # 故取满 60——否则第 21 行起「代表股」只剩快照自带单个 leader（灰字不可点），
                          # 下半张表看起来"没信息"。并发不变（仍 10 worker），每秒请求速率不变，冷启约 4s。
INTRADAY_TTL = 300        # 曲线缓存（秒）—— 2026-10-30 起仅作 rhythm 不可用时的兜底默认值；
                          # 运行时实际取值见 _r_curve_ttl()（盘中 3600 / 盘后 86400）
SAMPLE_GAP = 30           # 采样间隔（秒）—— 2026-10-30 起仅作 rhythm 不可用时的兜底默认值；
                          # 运行时实际取值见 _r_sample_gap()（盘中/盘前/午休 1800，盘后 0=不采）
TOP_DEFAULT = 16          # 曲线默认板块数（双向各 8；每板块 1 次东财请求，控制总量避风控）
SAMPLE_START_MIN = 9 * 60 + 30    # 采样窗口 09:30
SAMPLE_END_MIN = 15 * 60 + 5      # 采样窗口 15:05（收盘后 5 分钟补最后一笔）

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
_SINA_REF = "https://finance.sina.com.cn/"
_SINA_API = ("http://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"
             "MoneyFlow.ssl_bkzj_bk")
# 板块成分股（新浪同域接口）。板块资金流快照只给代表股「名字」(ts_name) 不给代码，
# 名字点不开；这里按板块代码补「代码 + 名称 + 涨跌幅」，代表股才可点击。
_SINA_MEM_API = ("http://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"
                 "Market_Center.getHQNodeData")
_EM_REF = "https://data.eastmoney.com/zjlx/list.html"
_EM_QUOTE_REF = "https://quote.eastmoney.com/"
_EM_UTS = ("fa5fd1943c7b386f172d6893dbfba10b", "b2884a393a59ad64002292a3e90d46a5")
_EM_HOSTS = ("push2.eastmoney.com", "1.push2.eastmoney.com", "push2his.eastmoney.com")

# 直连优先（显式忽略环境代理，本机若有失效代理变量会直接 ProxyError）→ 回退系统代理
_DIRECT = urllib.request.build_opener(urllib.request.ProxyHandler({}))
_PROXY = urllib.request.build_opener()

_cache: dict = {}
_cache_lock = threading.Lock()
_sample_lock = threading.Lock()
_db_lock = threading.Lock()          # 本模块 DB 写锁（采样落库 / 曲线落库 / ensure_schema 串行）
_last_sample_ts = {"v": 0.0}


# ---------------------------------------------------------------- 基础工具
def _ts() -> str:
    return str(int(time.time() * 1000))


def _num(v):
    """东财 / 新浪占位符（'-'、'--'、''）→ None；能转则 float。绝不写 0 冒充缺失。"""
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip().replace(",", "").replace("%", "")
    if s in ("", "-", "--", "—", "None", "null", "nan"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _http_text(url: str, referer: str, timeout: int = 10, retries: int = 1,
               encoding: str = "utf-8") -> str | None:
    """直连优先 → 系统代理兜底；失败返回 None（绝不抛给上层）。"""
    headers = {"User-Agent": _UA, "Referer": referer, "Accept": "*/*"}
    last = None
    for attempt in range(retries + 1):
        for opener in (_DIRECT, _PROXY):
            try:
                req = urllib.request.Request(url, headers=headers)
                with opener.open(req, timeout=timeout) as resp:
                    return resp.read().decode(encoding, "ignore")
            except Exception as e:  # noqa: BLE001
                last = e
        if attempt < retries:
            time.sleep(0.3 * (attempt + 1))
    logger.debug("board_flow 取数失败 %s（%s）", url[:110], last)
    return None


def _cache_get(key: str, ttl: int):
    with _cache_lock:
        node = _cache.get(key)
    if not node:
        return None
    # 2026-10-31：条目可以自带 TTL（只有分时曲线会带，见 _cache_put 的 ttl 参数）。
    # 取「条目 TTL 与调用方 TTL 的较小值」，保证「非定稿曲线只活 10 分钟」这条不会被
    # 调用方传进来的 _r_curve_ttl()（盘后 86400）重新放大。
    use_ttl = ttl
    node_ttl = node.get("ttl")
    if node_ttl is not None:
        try:
            use_ttl = min(int(ttl), int(node_ttl))
        except (TypeError, ValueError):
            use_ttl = ttl
    if time.time() - node.get("ts", 0) > use_ttl:
        return None
    return node.get("data")


def _cache_get_any(key: str):
    """取缓存里的旧值但**不判 TTL**，返回 `(data, age)`；没有就 `(None, None)`。

    2026-10-31（终验：板块分时首点 1.8~2.0s → 加 SWR）：`_cache_get` 一过期就返回 None，
    但 `_cache` 里的旧条目**并没有被删**（`_cache_put` 只覆盖 ts/data），旧值现成可用。
    于是「过期」不再等于「让用户干等 2 秒重抓」，而是「先给旧画面 + 后台单飞重算」。
    """
    with _cache_lock:
        node = _cache.get(key)
    if not node or node.get("data") is None:
        return None, None
    try:
        age = max(0.0, time.time() - float(node.get("ts") or 0))
    except (TypeError, ValueError):
        age = 0.0
    return node.get("data"), age


def _cache_put(key: str, data, ttl: int | None = None) -> None:
    """写缓存。`ttl` 可选：给了就随条目存下来，读的时候与调用方 TTL 取较小值。

    2026-10-31 为什么要它：盘后曲线 TTL 是 24 小时，但**非定稿**（截断）曲线若也锁 24 小时，
    就等于「拿一整天的残缺数据换 7 秒速度」。所以非定稿条目自带 10 分钟 TTL，过期后由
    SWR（先给旧值 + 后台重抓）自动补齐（见 intraday()）。其他调用方不传 ttl，行为一字不变。
    """
    with _cache_lock:
        node = {"ts": time.time(), "data": data}
        if ttl is not None:
            node["ttl"] = int(ttl)
        _cache[key] = node


def clear_cache() -> None:
    """清空内存缓存（前端「强制刷新」用）。"""
    with _cache_lock:
        _cache.clear()


# ---------------------------------------------------------------- 曲线 SWR（单飞重算）
# 2026-10-31（终验实测：/api/board_flow_intraday 首点 1.997s / 1.776s）。
# 根因：分时曲线**自己没有 SWR** —— `_cache_get` 一过期（或进程刚重启、内存为空）
# 就直接落到 `_em_intraday` 现抓（16 个板块串行 + 板间 sleep 0.55s），请求线程干等。
# 用户标准是「点开就要有画面」，所以盘中每小时那一次刷新也不该压在他的点击上：
# 过期旧值先返回（带 _stale/_cache_age），后台单飞重算，算完替换缓存。
# 单飞实现参照 viewer/server.py 的 `_swr_refresh`（同一 key 同时只允许一个刷新在飞）。
_INTRA_SWR_INFLIGHT: set = set()
_INTRA_SWR_LOCK = threading.Lock()


def _intra_swr_kick(key: str, kind: str, top: int, conn) -> bool:
    """后台重算一条分时曲线（同一 key 单飞）。返回是否真的起了线程。

    ⚠️ 必须用 `force=True` 调 `intraday()`：缓存里那条已经过期，若走非 force 分支，
    会再次命中「过期→起线程」的同一个判断，反复自我触发却永不重算。
    后台线程的异常只记日志 —— 用户手里已经有旧值了，绝不能让他看到失败。
    """
    with _INTRA_SWR_LOCK:
        if key in _INTRA_SWR_INFLIGHT:
            return False
        _INTRA_SWR_INFLIGHT.add(key)

    def _run() -> None:
        try:
            intraday(kind=kind, top=top, force=True, conn=conn)
        except Exception:  # noqa: BLE001
            logger.exception("板块资金流曲线后台重算失败（用户已拿到旧值，界面不受影响）")
        finally:
            with _INTRA_SWR_LOCK:
                _INTRA_SWR_INFLIGHT.discard(key)

    try:
        threading.Thread(target=_run, name="bf-intra-swr-%s" % kind, daemon=True).start()
    except Exception:  # noqa: BLE001 —— 起不了线程就退回「本次直接慢一次」，不影响正确性
        logger.exception("板块资金流曲线后台重算线程启动失败，本次退回同步抓取")
        with _INTRA_SWR_LOCK:
            _INTRA_SWR_INFLIGHT.discard(key)
        return False
    return True


def ensure_schema(conn) -> None:
    """采样表 + 曲线落库表（幂等）。零外键、零 JOIN，与既有业务表完全隔离。"""
    with _db_lock:
        conn.execute("""
        CREATE TABLE IF NOT EXISTS board_flow_samples (
            trade_date TEXT NOT NULL,   -- YYYY-MM-DD
            ts         TEXT NOT NULL,   -- HH:MM（采样时点）
            kind       TEXT NOT NULL,   -- industry / concept
            board_code TEXT NOT NULL,   -- 新浪 category（如 gn_wjqy）
            board_name TEXT NOT NULL,
            main_net   REAL,            -- 净流入额（元）
            pct        REAL,            -- 板块涨跌幅（%）
            PRIMARY KEY (trade_date, ts, kind, board_code)
        )
    """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_bfs_date_kind "
                     "ON board_flow_samples(trade_date, kind, ts)")
        # 东财分钟曲线落库：东财被风控是常态，但**成功一次就把当日完整曲线存下来**，
        # 之后即使它再被掐，界面依然有曲线可看（不会"时有时无"）。
        conn.execute("""
        CREATE TABLE IF NOT EXISTS board_flow_intraday (
            trade_date TEXT NOT NULL,   -- YYYY-MM-DD（跨日自动失效）
            kind       TEXT NOT NULL,   -- industry / concept
            payload    TEXT NOT NULL,   -- 完整曲线 JSON（axis + series）
            source     TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (trade_date, kind)
        )
    """)
        conn.commit()


def reset_sample_clock() -> None:
    """把「距上次采样时间」归零，让下一次 maybe_sample 立即执行（进程重启/手动触发用）。"""
    with _sample_lock:
        _last_sample_ts["v"] = 0.0


# ---------------------------------------------------------------- 快照：多源
def _sina_snapshot(kind: str, pz: int, asc: int = 0,
                   timeout: int = 10, retries: int = 1) -> list:
    """新浪板块资金流快照（主源，长期稳定）。

    字段：category(板块代码) name avg_changeratio(涨跌幅,小数) netamount(净额,元)
          ratioamount(净占比,小数) ts_name(代表股)

    asc=0 → 净流入榜；asc=1 → 净流出榜（「资金分布」两端都要看得见）。
    timeout / retries 可由采样等后台路径收紧（2026-10-20：默认不变，采样传 5s/0 次重试）。
    """
    cfg = KINDS.get(kind) or KINDS[DEFAULT_KIND]
    url = ("%s?page=1&num=%d&sort=netamount&asc=%d&fenlei=%s&_=%s"
           % (_SINA_API, max(1, min(int(pz or 50), 200)), 1 if asc else 0,
              cfg["sina_fenlei"], _ts()))
    txt = _http_text(url, _SINA_REF, timeout=timeout, retries=retries)
    if not txt:
        return []
    txt = txt.strip().lstrip("\ufeff")
    if not txt.startswith("["):
        return []
    try:
        arr = json.loads(txt)
    except Exception:  # noqa: BLE001
        return []
    out = []
    for x in arr:
        if not isinstance(x, dict):
            continue
        chg = _num(x.get("avg_changeratio"))
        ratio = _num(x.get("ratioamount"))
        out.append({
            "board_code": str(x.get("category") or "").strip(),
            "board_name": str(x.get("name") or "").strip(),
            "pct": (chg * 100.0) if chg is not None else None,
            "main_net": _num(x.get("netamount")),
            "main_pct": (ratio * 100.0) if ratio is not None else None,
            "huge_net": None,
            "big_net": None,
            "leader": str(x.get("ts_name") or "").strip(),
        })
    return [r for r in out if r["board_name"] and not _is_noise_board(r["board_name"])]


def _em_snapshot(kind: str, pz: int, asc: int = 0) -> list:
    """东财板块资金流快照（兜底；附带超大单 / 大单细分）。多主机 × 多 ut 轮询。"""
    cfg = KINDS.get(kind) or KINDS[DEFAULT_KIND]
    fields = "f12,f14,f3,f62,f184,f66,f72"
    po = 0 if asc else 1        # 东财 po=1 降序（净流入榜）/ po=0 升序（净流出榜）
    for host in _EM_HOSTS:
        for ut in _EM_UTS:
            url = ("https://%s/api/qt/clist/get?fid=f62&po=%d&pz=%d&pn=1&np=1&fltt=2&invt=2"
                   "&fs=%s&fields=%s&ut=%s&_=%s"
                   % (host, po, max(1, min(int(pz or 50), 200)), urllib.parse.quote(cfg["em_fs"]),
                      fields, ut, _ts()))
            txt = _http_text(url, _EM_REF, timeout=10, retries=0)
            if not txt:
                continue
            try:
                j = json.loads(txt)
            except Exception:  # noqa: BLE001
                continue
            if j.get("rc") not in (0, None):
                continue
            diff = ((j.get("data") or {}).get("diff")) or []
            if isinstance(diff, dict):
                diff = [diff[k] for k in sorted(diff.keys(),
                        key=lambda x: int(x) if str(x).isdigit() else x)]
            if not diff:
                continue
            out = []
            for x in diff:
                out.append({
                    "board_code": str(x.get("f12") or "").strip(),
                    "board_name": str(x.get("f14") or "").strip(),
                    "pct": _num(x.get("f3")),
                    "main_net": _num(x.get("f62")),
                    "main_pct": _num(x.get("f184")),
                    "huge_net": _num(x.get("f66")),
                    "big_net": _num(x.get("f72")),
                    "leader": "",
                })
            if out:
                logger.info("板块资金流快照走东财（%s，%d 条）", host, len(out))
                return out
    return []


def _merge_two_way(rows_in: list, rows_out: list) -> list:
    """净流入榜 + 净流出榜合并（按板块代码去重），净额降序。"""
    seen, out = set(), []
    for r in list(rows_in or []) + list(rows_out or []):
        code = r.get("board_code") or r.get("board_name")
        if not code or code in seen:
            continue
        seen.add(code)
        out.append(r)
    out.sort(key=lambda r: (r.get("main_net") if r.get("main_net") is not None else -1e18),
             reverse=True)
    return out


def _sina_members(board_code: str, num: int = MEM_NUM) -> list:
    """板块代表股：[{"code","name","pct","price"}]，按板块内总市值降序（即「行业龙头」口径）。

    口径说明（2026-09-28 订正）：原先按 changepercent 降序取，拿到的是当日涨停的小票
    （酿酒行业出「会稽山/古越龙山」而不是「贵州茅台/五粮液」），不具行业代表性——王总反馈后改为
    sort=mktcap（总市值）降序，取到的即该板块真正的龙头权重股。
    当日表现不丢：pct 字段仍取自响应里的 changepercent，所以「龙头是谁 + 今天涨跌」都能看到。
    实测：酿酒→茅台/五粮液/汾酒/泸州老窖/青啤/洋河；汽车→比亚迪/潍柴/福耀/上汽/中航沈飞/长城。
    只留 6 位纯数字 A 股代码（ETF/跨市场标的点开无意义，与项目其它表格的过滤一致）。
    成功缓存 MEM_TTL；失败缓存 MEM_FAIL_TTL 空值（冷却），避免反复重打同一批请求。
    """
    code = str(board_code or "").strip()
    if not code:
        return []
    num = max(1, min(int(num or MEM_NUM), 100))   # 2026-09-29 A1：板块详情页需全量成分股，上限放至新浪实际返回上限 100
    ck = "mem:mktcap:%s:%d" % (code, num)   # 键含口径(mktcap)，改排序口径不会误读旧缓存
    hit = _cache_get(ck, MEM_TTL)
    if hit is not None:
        return hit
    if _cache_get(ck + ":fail", MEM_FAIL_TTL) is not None:
        return []
    url = ("%s?page=1&num=%d&sort=mktcap&asc=0&node=%s&symbol=&_s_r_a=page&_=%s"
           % (_SINA_MEM_API, num, urllib.parse.quote(code), _ts()))
    txt = _http_text(url, _SINA_REF, timeout=6, retries=0)
    out = []
    if txt:
        txt = txt.strip().lstrip("\ufeff")
        if txt.startswith("["):
            try:
                arr = json.loads(txt)
            except Exception:  # noqa: BLE001
                arr = []
            for x in arr:
                if not isinstance(x, dict):
                    continue
                c = str(x.get("code") or "").strip()
                n = str(x.get("name") or "").strip()
                if not c or not n or not re.match(r"^\d{6}$", c):
                    continue
                out.append({"code": c, "name": n,
                            "pct": _num(x.get("changepercent")),
                            "price": _num(x.get("trade"))})
    if out:
        _cache_put(ck, out)
    else:
        _cache_put(ck + ":fail", 1)
    return out


def board_members(board_code: str, num: int = 100) -> list:
    """板块成分股全列表（最多 100 只，新浪单板块实际上限）。供「板块详情页」用。

    直接复用 _sina_members（已含新浪调用 / 缓存 / 6 位 A 股过滤 / 失败冷却）；
    仅放宽取数上限到 100。快照代表股仍走 _sina_members(num=MEM_NUM=12)，不受影响。
    """
    return _sina_members(board_code, num)


def attach_members(rows: list, top: int = MEM_TOP) -> list:
    """给前 N 行的板块补「代表股」（并发取数，每板块 1 次请求）。

    取不到的行留空数组 `members: []`——前端会退回显示快照自带的领涨股名（不可点），
    绝不编造代码。函数**不抛异常**：个股取数失败绝不能让板块资金流排行整体挂掉。
    """
    if not rows:
        return rows
    picked = [r for r in rows[:max(0, int(top))] if isinstance(r, dict) and r.get("board_code")]
    if not picked:
        return rows
    try:
        workers = max(1, min(10, len(picked)))
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
            futs = {ex.submit(_sina_members, r["board_code"]): r for r in picked}
            for fut, r in futs.items():
                try:
                    r["members"] = fut.result(timeout=15) or []
                except Exception:  # noqa: BLE001
                    r["members"] = []
    except Exception as e:  # noqa: BLE001
        logger.debug("板块代表股并发取数失败：%s", e)
        for r in picked:
            r.setdefault("members", [])
    return rows


def snapshot(kind: str = DEFAULT_KIND, pz: int = 50, force: bool = False) -> dict:
    """板块资金流排行快照（双向）。新浪优先（稳定），东财兜底。带 120s 缓存。

    双向 = 净流入榜一半 + 净流出榜一半：只给净流入榜看不出「资金分布」，
    且曲线里那些净流出板块在表里找不到会让人以为数据不对。
    """
    kind = kind if kind in KINDS else DEFAULT_KIND
    pz = max(1, min(int(pz or 50), 200))
    ck = "snap:%s:%d" % (kind, pz)
    if not force:
        hit = _cache_get(ck, SNAP_TTL)
        if hit:
            return hit

    half = max(3, pz // 2)
    rows, source = [], ""
    sina_in = _sina_snapshot(kind, half, asc=0)
    sina_out = _sina_snapshot(kind, half, asc=1)
    if sina_in or sina_out:
        rows, source = _merge_two_way(sina_in, sina_out), "sina"
    else:
        em_in = _em_snapshot(kind, half, asc=0)
        em_out = _em_snapshot(kind, half, asc=1)
        if em_in or em_out:
            rows, source = _merge_two_way(em_in, em_out), "eastmoney"

    if not rows:
        data = {"ok": False, "rows": [], "kind": kind, "source": "",
                "reason": ("未取到板块资金流排行：新浪与东财两个源都不可达。"
                           "请检查网络 / 代理（本面板只需国内网络，通常不需要 VPN）。")}
        _cache_put(ck, data)
        return data

    # 代表股：只补表格前 MEM_TOP 行（每板块 1 次请求 + 5 分钟缓存，控总量）。
    attach_members(rows)

    data = {
        "ok": True, "kind": kind, "source": source,
        "source_label": {"sina": "新浪财经", "eastmoney": "东方财富"}.get(source, source),
        "rows": rows,
        "two_way": True,
        "up_count": sum(1 for r in rows if (r.get("main_net") or 0) > 0),
        "down_count": sum(1 for r in rows if (r.get("main_net") or 0) < 0),
        # 数据日期对齐到「最近交易日」：休市日新浪快照给的就是上一交易日的收盘值，
        # 标成当天 = 给旧数据盖新戳（2026-10-03 教训）。
        "trade_date": latest_trade_date() or datetime.now().strftime("%Y-%m-%d"),
        "updated_at": datetime.now().strftime("%H:%M:%S"),
    }
    _cache_put(ck, data)
    return data


# ---------------------------------------------------------------- 曲线 A：东财分钟
# 2026-10-30：真打东财分钟接口的节流闸。此前每次进面板（或缓存过期）都会起 16 个串行请求
# （板间 time.sleep(0.55)）→ 冷态实测 7.5s，且占住 GIL 拖慢同进程所有接口。
# 用户明确「一小时更新一次都行」，于是盘中最多一小时抓一次；盘后（定稿）不再重复抓。
_last_live_fetch = {"ts": 0.0, "date": "", "final": False}
#   ts: monotonic 秒；date: 本进程最后一次抓的日期；final: 那次抓取是否发生在「收盘定稿后」
_live_fetch_lock = threading.Lock()
_FINAL_CAPTURE_FROM = "15:00:00"   # 曲线快照「算定稿」的最早落库时刻（收盘后才有完整尾盘）
# 2026-10-31：收盘后「库内非定稿」的补抓重试冷却（秒）。旧实现是「一天只补一次」，
# 那一次被东财限流打掉之后，截断曲线就锁一整天；改成 10 分钟一次的重试。
_FINAL_RETRY_COOL = 600.0
_last_final_attempt = {"ts": 0.0}   # monotonic 秒；最后一次「收盘后补抓」的尝试时刻
# 非定稿曲线的缓存上限（秒）：10 分钟后由 SWR 先回旧值、后台重抓，直到补成定稿。
_TTL_NONFINAL_MAX = 600


def _is_final_capture(cached_at: str | None) -> bool:
    """这份落库快照是不是「收盘后（≥15:00）抓的」= 当日完整曲线。

    为什么必须区分：库里当日的行可能是盘中抓的。实测（2026-10-30 生产库）：
      · industry 当日行落库于 14:40 → 曲线缺最后 20 分钟；
      · concept  当日行落库于 11:07 → 只有半天。
    若「定稿优先」不加这道校验，收盘后用户看到的会是一条**截断**的曲线，而且被缓存 24h，
    等于把「优化等待」变成了「数据不全」。加上校验后：盘中抓的行不算定稿，
    收盘后允许（且仅允许）补抓一次，抓完落库即成为真·定稿。格式按 "YYYY-MM-DD HH:MM:SS"
    的落库时间（board_flow_intraday.updated_at）解析；解析不出来一律当「非定稿」。
    """
    s = str(cached_at or "")
    if len(s) < 19:
        return False
    return s[11:19] >= _FINAL_CAPTURE_FROM


# 2026-10-31：曲线「跑满全天」的判据线（末个分钟点不得早于此）。
# 为什么不能只靠「落库时刻 ≥ 15:00」：休市日拿到的是**别的交易日**的行，落库时刻与那条
# 曲线的数据日无关（e.g. 周六补抓落库时刻 10:20，曲线却是上一个周五的全天数据）。
# 直接看曲线自己的末个时点最诚实。实测生产库 2026-10-30 concept 末点 11:08（半天）、
# industry 末点 14:41（缺尾盘）—— 这两条都不能当定稿给用户。
_COMPLETE_LAST_POINT = "14:50"


def _curve_is_complete(res) -> bool:
    """曲线末个分钟点是否已跑到 14:50 之后（= 尾盘完整）。轴格式 "HH:MM"。"""
    axis = (res or {}).get("axis") or []
    if not axis:
        return False
    last = str(axis[-1] or "")
    if len(last) < 5 or last[2:3] != ":":
        return False
    return last[:5] >= _COMPLETE_LAST_POINT


def _live_fetch_gate(force: bool, today_s: str, after_close: bool = False,
                     already_final: bool = False) -> bool:
    """现在是否允许真打东财分钟接口（2026-10-30 盘中降频）。

    规则（间隔全部来自 rhythm，本函数不自己判断时段）：
      · force=True（前端「立即刷新」）→ 一律放行，并记账（穿透节流，保留用户手动重试的能力）；
      · 今天还没抓过 → 放行一次。这条是必需的：否则「08:30 盘前抓过上一交易日的曲线」
        会占用节流窗口，导致 09:30 开盘后头一小时抓不到今天的曲线（面板一直显示昨天）；
        也让「收盘后才启动服务、库里没有当日曲线」的场景能补上（否则整晚停在更早的数据上）；
      · gap > 0（盘中/盘前/午休）→ 距上次抓取不足 gap 秒则拒绝；
      · 收盘后（after_close / gap=0）→ 分两种（2026-10-31 修回归）：
          - `already_final=True`（库内当日行已是收盘后抓的完整曲线）→ **拒绝**，这就是
            「收盘后不再重抓」，一个字没变；
          - `already_final=False`（库里是盘中抓的截断行）→ 允许补抓，但受 `_FINAL_RETRY_COOL`
            （600 秒）冷却限制，即每 10 分钟最多重试一次。
        ⚠️ 旧实现这里是「一天只允许补抓一次」。后果实测（2026-10-30 数据 + 东财限流）：
        那一次补抓失败（industry 1.88s 起头就挂），额度被烧掉 → 之后整个周末都返回
        concept 停在 11:08 的半天曲线，且缓存 24 小时。改造前 INTRADAY_TTL=300，5 分钟就重试，
        东财恢复即补全。现在改成有冷却的重试：东财正常时收盘后 10 分钟内补齐并彻底锁定，
        被限流时每 10 分钟后台试一次，用户侧拿到的始终是「秒回的旧值 + 明确归属日」。
    返回 True 表示这次真会去打接口（已记账）；False 表示调用方应直接用兜底分支。
    """
    gap = _r_live_fetch_gap()
    now_m = time.monotonic()
    with _live_fetch_lock:
        if force:
            _last_live_fetch["ts"] = now_m
            _last_live_fetch["date"] = today_s
            _last_live_fetch["final"] = bool(after_close and already_final)
            if after_close:
                _last_final_attempt["ts"] = now_m
            return True
        if _last_live_fetch.get("date") != today_s:
            _last_live_fetch["ts"] = now_m
            _last_live_fetch["date"] = today_s
            _last_live_fetch["final"] = False
            if after_close:
                _last_final_attempt["ts"] = now_m
            return True
        if after_close or gap <= 0:
            if already_final:
                logger.debug("东财分钟接口：库内已是收盘定稿，跳过重抓")
                return False
            last_try = float(_last_final_attempt.get("ts") or 0.0)
            if last_try and (now_m - last_try) < _FINAL_RETRY_COOL:
                logger.debug("东财分钟接口：非定稿补抓冷却中（已过 %.0fs，需 %.0fs），本次跳过",
                             now_m - last_try, _FINAL_RETRY_COOL)
                return False
            _last_final_attempt["ts"] = now_m
            _last_live_fetch["ts"] = now_m
            _last_live_fetch["final"] = False
            return True
        if now_m - float(_last_live_fetch.get("ts") or 0.0) < gap:
            logger.debug("东财分钟接口节流中（间隔 %ds 未到），本次跳过", gap)
            return False
        _last_live_fetch["ts"] = now_m
        _last_live_fetch["final"] = after_close
        return True


def _curve_cache_ttl(final: bool, after_close: bool, trading_now) -> int:
    """这条曲线结果该缓存多久（2026-10-31）。

    · 定稿（收盘后/休市 + 曲线末点跑满）→ 用 rhythm 的 TTL（盘后 86400），锁一天；
    · 收盘/休市窗口里的**非定稿**（截断行、兜底采样、失败体）→ 最多 600 秒。过期后由 SWR
      先回旧值、后台重抓，东财一恢复就自动补全 —— 这就是修「截断曲线锁 24 小时」的回归。
    · 盘中 → 一律用 rhythm 的 TTL（3600）。**刻意不在这里压到 600**：盘中压短会让
      缓存在一小时内反复过期，SWR 用 force=True 绕过闸门，等于把东财请求放大 6 倍，
      反而更容易被风控（那正是本次重构要避免的）。
    """
    base = _r_curve_ttl()
    if final:
        return base
    if after_close or trading_now is False:
        return min(_TTL_NONFINAL_MAX, base)
    return base


def _em_minute_curve(code: str) -> list | None:
    """东财单板块当日分钟资金流 → [(HH:MM, 主力净额)]。

    刻意**不用 emapi.fflow**（它内含 2 ut × 2 端点 = 最多 4 次请求/板块），
    这里每板块最多 2 次，把总请求量压到最低，避免触发 push2 风控。
    klt=1 & lmt=0 → 一次返回当日全部分钟点。
    """
    if not code:
        return None
    sid = code if code.startswith("90.") else "90." + code
    for ut in _EM_UTS:
        url = ("https://push2.eastmoney.com/api/qt/stock/fflow/kline/get?secid=%s"
               "&fields1=f1,f2,f3,f7"
               "&fields2=f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61,f62,f63,f64,f65"
               "&klt=1&lmt=0&ut=%s&_=%s" % (sid, ut, _ts()))
        txt = _http_text(url, _EM_QUOTE_REF, timeout=10, retries=0)
        if not txt:
            continue
        try:
            j = json.loads(txt)
        except Exception:  # noqa: BLE001
            continue
        if j.get("rc") not in (0, None):
            continue
        klines = ((j.get("data") or {}).get("klines")) or []
        pts, data_date = [], None
        for line in klines:
            p = str(line).split(",")
            if len(p) < 2:
                continue
            stamp = p[0].strip()          # "2026-09-30 09:31"
            dd = stamp[:10] if len(stamp) >= 10 else ""
            if dd:
                data_date = dd            # 接口自带日期，**绝不能丢**——休市日它吐的是上一交易日
            hhmm = stamp[-5:] if len(stamp) >= 5 else stamp
            net = _num(p[1])
            if net is None:
                continue
            pts.append((hhmm, net))
        if pts:
            return pts, data_date
    return None, None


def _em_intraday(kind: str, top: int) -> dict | None:
    """东财分钟曲线：先取 Top N 板块，再逐板块串行取分钟（节流 0.55s）。

    2026-10-30：本函数**只应由 intraday() 在 `_live_fetch_gate()` 放行后调用**。
    它一次要串行打 16 个板块（板间 sleep 0.55s），冷态实测 7.5s 且全程占 GIL，
    在请求线程里现算就是「点开面板卡 7 秒」。别再给它加新的调用点。
    """
    snap = _em_snapshot(kind, pz=max(int(top) * 2, 30))
    if not snap:
        return None
    picked = [r for r in snap if r.get("board_code")][:int(top)]
    if not picked:
        return None
    axis, series = None, []
    fails = 0
    data_date = None
    for i, r in enumerate(picked):
        pts, dd = _em_minute_curve(r.get("board_code"))
        if not pts:
            fails += 1
        else:
            if dd and (data_date is None or dd > data_date):
                data_date = dd
            if axis is None or len(pts) > len(axis):
                axis = [p[0] for p in pts]
            series.append({
                "code": r.get("board_code"), "name": r.get("board_name"),
                "pct": r.get("pct"),
                "main_net": r.get("main_net"),
                "points": [round(p[1] / 1e8, 4) for p in pts],   # 折算「亿」，前端直接用
            })
        if i < len(picked) - 1:
            time.sleep(0.55)      # 节流：串行 + 间隔，避免被 push2 掐连接
    if not series:
        logger.info("东财分钟曲线全部失败（%d/%d）", fails, len(picked))
        return None
    # 成功数太少视为不完整（典型：风控导致只拿到 1 个板块），不按最新曲线落库，
    # 让 intraday() 走采样/历史完整记录兜底，避免「只剩一根线」。
    min_ok = max(4, len(picked) // 2)
    if len(series) < min_ok:
        logger.info("东财分钟曲线不完整：仅成功 %d/%d（下限 %d），不按最新曲线落库",
                    len(series), len(picked), min_ok)
        return None
    logger.info("东财分钟曲线 OK：%d 条，各 %d 点（数据日 %s）", len(series), len(axis or []), data_date)
    return {"axis": axis or [], "series": series, "source": "eastmoney",
            "data_date": data_date,
            "source_label": "东方财富（分钟）", "fail_count": fails}


# ---------------------------------------------------------------- 交易日历
# 铁教训（2026-10-03 王总质疑「国庆没开市哪来的资金流」）：东财分钟接口在**休市日照样返回**，
# 吐的是**最近一个交易日**的完整曲线（10-03 抓到的其实是 09-30 的）。旧代码直接拿
# datetime.now() 当天当 trade_date 落库，界面又硬编码「当日累计净流入」且不显示日期，
# 于是旧数据顶着新日期。凡是「当日」类数据，一律先过这道闸。
# 交易日历一律以 auction.py 为唯一权威源（2026-10-03 收敛：此前本模块自己实现过一套，
# 与 auction 口径漂移，正是「09-30 曲线打 10-03 戳」的根因）。此处只 import，不再另实现；
# 沿用同名，下方所有调用点无需改动。
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)
from auction import latest_trade_date, is_trading_day, market_trade_days  # noqa: E402

# ---------------------------------------------------------------- 交易节奏（2026-10-30）
# 为什么改成向 rhythm 取数：本模块此前把「盘中该多快」写死在三个常量里
#   （INTRADAY_TTL=300 / SAMPLE_GAP=30 / live 抓取无间隔闸），等于把「实时」当默认目标。
# 后果（主理人实测）：① 盘中每 30 秒做一次全板块快照采样（industry+concept 各两次新浪请求）；
#   ② 每次进面板都可能重抓 16 个板块的东财分钟线（冷态 7.5s，串行 0.55s/板）；
#   ③ 收盘后（用户复盘、最需要快的时段）这套「实时」节奏**照旧全速跑**，白烧 CPU 与 GIL。
# 现在统一问 viewer/rhythm.py（唯一真源），本文件不再自己判断时段。
# 导入失败时退化为原有常量（宁可慢，也不能因为一个新模块把面板搞挂）。
try:  # noqa: E402
    from viewer import rhythm as _rhythm
except Exception:  # noqa: BLE001
    _rhythm = None  # type: ignore[assignment]

_LIVE_GAP_FALLBACK = 3600   # rhythm 不可用时的兜底：仍保留「一小时一次」的闸，不回退到无闸


def _r_is_after_close() -> bool:
    """交易日 15:05 之后（当日曲线已定稿）。rhythm 不可用 → False（保守：按原逻辑走）。"""
    if _rhythm is None:
        return False
    try:
        return bool(_rhythm.is_after_close())
    except Exception:  # noqa: BLE001
        return False


def _r_sample_gap() -> int:
    """采样间隔（秒）。rhythm 不可用 → SAMPLE_GAP。"""
    if _rhythm is None:
        return SAMPLE_GAP
    try:
        return int(_rhythm.intraday_sample_gap())
    except Exception:  # noqa: BLE001
        return SAMPLE_GAP


def _r_curve_ttl() -> int:
    """曲线缓存 TTL（秒）。rhythm 不可用 → INTRADAY_TTL。"""
    if _rhythm is None:
        return INTRADAY_TTL
    try:
        return int(_rhythm.intraday_curve_ttl())
    except Exception:  # noqa: BLE001
        return INTRADAY_TTL


def _r_live_fetch_gap() -> int:
    """允许重打东财分钟接口的最小间隔（秒）。rhythm 不可用 → _LIVE_GAP_FALLBACK。"""
    if _rhythm is None:
        return _LIVE_GAP_FALLBACK
    try:
        return int(_rhythm.intraday_live_fetch_gap())
    except Exception:  # noqa: BLE001
        return _LIVE_GAP_FALLBACK


def _r_note() -> str:
    """节奏说明一句话（拼进 market_note）。rhythm 不可用 → 空串（不比原来少信息）。"""
    if _rhythm is None:
        return ""
    try:
        return str(_rhythm.refresh_note() or "")
    except Exception:  # noqa: BLE001
        return ""


def _market_note(res: dict) -> dict:
    """给曲线结果补「数据属于哪天 / 今天开没开市」，前端照抄即可，不许自己猜日期。

    休市日（含法定假期）接口吐的是最近交易日的曲线，`stale=True` 时必须如实标注，
    不许顶着「当日」二字糊过去。
    """
    today = datetime.now().strftime("%Y-%m-%d")
    latest = latest_trade_date() or today
    trading = is_trading_day()
    has_date = bool(res.get("data_date"))
    res.setdefault("data_date", latest)
    res["trade_date"] = res["data_date"]
    res["is_trading_day"] = trading
    res["latest_trade_date"] = latest
    res["stale"] = bool(has_date and res["data_date"] != today)
    if trading:
        res["market_note"] = "今日交易日" + ("" if has_date else "，数据日 %s" % res["data_date"])
    else:
        how = "今日休市（%s）" % ("周末" if datetime.now().weekday() >= 5 else "节假日")
        res["market_note"] = "%s，曲线为最近交易日（%s）数据" % (how, res["data_date"])
    # 2026-10-30：再把「本时段的数据节奏」如实拼上一句（如「今日交易日 · 盘中·每小时刷新」
    # /「今日交易日 · 收盘定稿·不再实时抓取」）。用户盘中可以慢、可以旧，但**必须知道有多旧**，
    # 否则他会误以为面板卡了。
    # 2026-10-31 更正（QA 回归项 5 不通过，我原注释写错了）：原以为「前端 app.js 已直接渲染
    # market_note，无需改前端」，实际 app.js:`bf-note-inline` 只在 `!rOn`（分时曲线）分支渲染，
    # 而面板默认口径是区间（单日=最新交易日），`/api/board_flow_range` 一回来就接管整屏，
    # market_note 一个字都不显示。区间口径画的是**日线区间**，整句里「曲线为最近交易日 X 数据」
    # 那半句不成立（用户自己选了起止日期时更是错的），所以节奏那半句必须单独给出来，
    # 让前端在区间分支只显示纯节奏描述。只增字段，不改 market_note 的既有拼法（老调用方不受影响）。
    _rhythm_note = _r_note()
    res["rhythm_note"] = _rhythm_note
    if _rhythm_note:
        _base = (res.get("market_note") or "").strip()
        res["market_note"] = ("%s · %s" % (_base, _rhythm_note)) if _base else _rhythm_note
    return res


# ---------------------------------------------------------------- 曲线 B：本机采样累积
# 2026-10-20 采样减速修（王总反馈「启动过慢好久了」）：日志实测 board_flow_sample 单轮
# 81~95s（13:04 / 13:06 两条 WARNING），期间纯 Python 计算占 GIL 冻结所有接口——页面转圈。
# 根因：_do_sample 调完整 snapshot()，后者无条件 attach_members()（前 60 板块 × 1 请求，
# 直连+代理双路 × 6s 超时 × 重试），采样落库只需要净额/涨跌幅，代表股纯属白抓；
# MEM 缓存 300s 一过期，盘中每 30s 就重烧几十个请求，网络一抖即超时叠加。
# 修法：① 采样走轻量路径（短超时/不重试/不取代表股）；② 连续失败退避 30s→300s。
SAMPLE_FAIL_BACKOFF = 300   # 连续失败 ≥3 轮后的采样间隔（秒）
_sample_fail_streak = {"n": 0}


def _do_sample(conn, now: datetime) -> int:
    """抓一次快照写库（industry + concept 各一）。返回写入条数。

    采样专用轻量路径：只取新浪净额/涨跌幅（asc 双向），不取代表股、短超时、不重试、
    失败不落东财兜底（采样是累积型的，缺一轮无伤大雅；东财兜底留给页面实时路径）。
    """
    total = 0
    date_s = now.strftime("%Y-%m-%d")
    ts_s = now.strftime("%H:%M")
    half = 30          # 双向各 30，合并后 ≤60 行，与旧 pz=60 等量
    for kind in KINDS:
        sina_in = _sina_snapshot(kind, half, asc=0, timeout=5, retries=0)
        sina_out = _sina_snapshot(kind, half, asc=1, timeout=5, retries=0)
        if not (sina_in or sina_out):
            continue
        rows = [(date_s, ts_s, kind, r["board_code"], r["board_name"],
                 r.get("main_net"), r.get("pct"))
                for r in _merge_two_way(sina_in, sina_out) if r.get("board_name")]
        if not rows:
            continue
        conn.executemany(
            "INSERT OR REPLACE INTO board_flow_samples"
            "(trade_date, ts, kind, board_code, board_name, main_net, pct) "
            "VALUES (?,?,?,?,?,?,?)", rows)
        conn.commit()
        total += len(rows)
    return total


def maybe_sample(conn) -> int:
    """交易时段内按规定间隔节流采样一次。由后台线程调用；异常绝不外抛。

    2026-10-20 增加失败退避：连续 3 轮一无所获后，采样间隔从基础间隔（原 SAMPLE_GAP=30s）
    放宽到 SAMPLE_FAIL_BACKOFF(300s)，网络劣化期不再每 30s 空烧一轮冻结接口；
    任一轮成功即清零退避、恢复正常节奏。

    2026-10-30 节奏重构：基础间隔改为**运行时**问 viewer/rhythm.py（盘中/盘前/午休 1800s，
    盘后 0=不采样）。原因（主理人实测）：旧值 30s 意味着盘中每半分钟就 industry+concept
    各打两次新浪（一轮 4 个请求），而用户的原话是「一个小时更新一次都行」——
    这些请求对他没有价值，只在给接口抢 GIL。基础间隔是 0 时**直接不采样**（不是「每轮都采」）。
    """
    try:
        now = datetime.now()
        # 休市日不采：新浪快照接口给的是「当天最新值」——休市那天就是上一交易日的全日量，
        # 采下来当当日样本，等于把 09-30 的收盘额记在 10-01 头上。
        if is_trading_day() is False:
            return 0
        m = now.hour * 60 + now.minute
        if not (SAMPLE_START_MIN <= m <= SAMPLE_END_MIN):
            return 0
        base_gap = _r_sample_gap()
        if base_gap <= 0:
            # 0 = 本时段不采样（盘后已定稿）。必须在此处直接返回：
            # 若拿 0 去比「距上次采样是否 < 0」，条件恒不成立 → 会退化成每轮（10s）都采。
            return 0
        with _sample_lock:
            gap = base_gap if _sample_fail_streak["n"] < 3 else SAMPLE_FAIL_BACKOFF
            if now.timestamp() - _last_sample_ts["v"] < gap:
                return 0
            _last_sample_ts["v"] = now.timestamp()
        n = _do_sample(conn, now)
        if n:
            if _sample_fail_streak["n"] >= 3:
                logger.info("板块资金流采样恢复（此前连续失败 %d 轮，退避解除）",
                            _sample_fail_streak["n"])
            _sample_fail_streak["n"] = 0
            logger.info("板块资金流采样写入 %d 条（%s）", n, now.strftime("%H:%M"))
        else:
            _sample_fail_streak["n"] += 1
            if _sample_fail_streak["n"] >= 3:
                logger.warning("板块资金流采样连续 %d 轮无数据，退避至 %ds 后再试",
                               _sample_fail_streak["n"], SAMPLE_FAIL_BACKOFF)
        return n
    except Exception:  # noqa: BLE001 —— 采样失败绝不能拖垮调度线程
        logger.exception("板块资金流采样异常（已忽略）")
        return 0


def _sample_curve(conn, kind: str, top: int) -> dict | None:
    """从采样表还原当日曲线：取「最后一次采样」的 Top N 板块，再拉出全天各时点。"""
    today = datetime.now().strftime("%Y-%m-%d")
    last = conn.execute(
        "SELECT MAX(ts) FROM board_flow_samples WHERE trade_date=? AND kind=?",
        (today, kind)).fetchone()
    last_ts = last[0] if last else None
    if not last_ts:
        return None
    # 双向取：净流入前 half + 净流出前 half（只画一头不叫「资金分布」）
    limit = max(4, int(top or TOP_DEFAULT))
    half = max(2, limit // 2)
    sql = ("SELECT board_code, board_name, main_net, pct FROM board_flow_samples "
           "WHERE trade_date=? AND kind=? AND ts=? AND main_net IS NOT NULL "
           "ORDER BY main_net %s LIMIT ?")
    seen, heads = set(), []
    for order in ("DESC", "ASC"):
        for h in conn.execute(sql % order, (today, kind, last_ts, half)).fetchall():
            if h[0] in seen or _is_noise_board(h[1]):
                continue
            seen.add(h[0])
            heads.append(h)
    if not heads:
        return None
    codes = [h[0] for h in heads]
    ph = ",".join("?" * len(codes))
    rows = conn.execute(
        "SELECT ts, board_code, main_net FROM board_flow_samples "
        "WHERE trade_date=? AND kind=? AND board_code IN (%s) "
        "ORDER BY ts ASC" % ph, (today, kind, *codes)).fetchall()
    axis, buckets = [], {}
    for ts_s, code, net in rows:
        if ts_s not in buckets:
            buckets[ts_s] = {}
            axis.append(ts_s)
        if net is not None:
            buckets[ts_s][code] = round(net / 1e8, 4)     # 折算「亿」
    series = []
    for code, name, net, pct in heads:
        pts = [buckets.get(t, {}).get(code) for t in axis]
        last = next((v for v in reversed(pts) if v is not None), None)
        if last is None:
            continue
        series.append({"code": code, "name": name, "pct": pct,
                       "main_net": net, "last": last, "points": pts})
    if not series or len(axis) < 1:
        return None
    return {"axis": axis, "series": series, "source": "sina_sample",
            "source_label": "新浪财经（本机采样累积）", "fail_count": 0,
            "sample_count": len(axis), "last_ts": last_ts,
            "first_ts": axis[0] if axis else None, "head_count": len(series)}


# ---------------------------------------------------------------- 曲线落库
def _persist_intraday(conn, kind: str, payload: dict) -> None:
    """把东财抓到的曲线落库，**trade_date 用接口自带的真实数据日**（失败静默）。

    旧实现写 `datetime.now()`：休市日抓到上一交易日的曲线也顶着今天的日期，
    界面读「当日」就是旧数据装新瓶。现在以 `payload["data_date"]` 为准；
    若接口压根没给日期（理论上不该发生），才回落到「最近交易日」，绝不回落当天。
    """
    if conn is None:
        return
    data_date = payload.get("data_date") or latest_trade_date()
    if not data_date:
        return
    try:
        conn.execute(
            "INSERT OR REPLACE INTO board_flow_intraday"
            "(trade_date, kind, payload, source, updated_at) VALUES (?,?,?,?,?)",
            (data_date, kind,
             json.dumps({"axis": payload.get("axis") or [],
                         "series": payload.get("series") or []}, ensure_ascii=False),
             payload.get("source") or "eastmoney",
             datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
        conn.commit()
    except Exception:  # noqa: BLE001
        logger.debug("板块资金流曲线落库失败（忽略）", exc_info=True)


def _load_intraday(conn, kind: str, only_date: str | None = None) -> dict | None:
    """读已落库的东财曲线。

    按 kind 取**最近一条完整记录**——休市日/盘中抓到的曲线真实归属可能是别的日子，
    按「今天」查会查空，按日期降序找才能拿到；同时剔除残缺记录（如风控只成功 1 个板块），
    避免非交易日展示「只剩一根线」的脏数据。

    2026-10-30 新增 `only_date`：只认指定交易日（「收盘定稿优先」分支要用它判断
    「库里有没有今天这份」）。默认 None = 保持原有「最近一条」语义，老调用方行为不变。
    """
    if conn is None:
        return None
    try:
        rows = conn.execute(
            "SELECT trade_date, payload, source, updated_at FROM board_flow_intraday "
            "WHERE kind=? ORDER BY trade_date DESC LIMIT 10", (kind,)).fetchall()
    except Exception:  # noqa: BLE001
        return None
    for row in rows:
        if only_date and row[0] != only_date:
            continue
        try:
            body = json.loads(row[1] or "{}")
        except Exception:  # noqa: BLE001
            continue
        axis = body.get("axis") or []
        series = body.get("series") or []
        if not axis or not series:
            continue
        # 完整性门槛：至少 4 条序列 + 不少于 60 个时点（半日），否则视为残缺记录
        if len(series) < 4 or len(axis) < 60:
            continue
        data_date = row[0]
        return {"axis": axis, "series": series, "source": row[2] or "eastmoney",
                "data_date": data_date,
                "source_label": "东方财富（分钟 · 已缓存）",
                "cached_at": row[3], "fail_count": 0}
    return None


# ---------------------------------------------------------------- 对外：曲线
def intraday(kind: str = DEFAULT_KIND, top: int = TOP_DEFAULT,
             force: bool = False, conn=None) -> dict:
    """板块资金流分时曲线。

    优先级（每级口径独立，绝不把两个源的点拼进同一条曲线）：
      ⓵ 内存缓存新鲜命中 → 直接返回
      ⓶ 过期但旧值仍在（2026-10-31 SWR）→ 先返回旧值（带 _stale/_cache_age），后台单飞重算
      ⓿ 定稿（交易日收盘后 / 休市的最近交易日）→ 库内已有就直接返回，**绝不重抓东财**
      ① 东财分钟实时抓 → 成功即落库并返回（最完整 + 最新；受 _live_fetch_gate 节流）
      ② 本机采样累积   → 当日已积累 ≥2 个点则返回（最新，但可能短）
      ③ 东财库内快照   → 当日抓到过就返回（完整，但可能略旧）

    ⚠️ 返回结构自 2026-10-05 起被前端依赖（axis/series/source/source_label/updated_at/
    data_date/trade_date/stale/market_note/kind/kind_label/ok …）：本次重构**只调整顺序与
    节流**，一个字段都没改、没删、没改名。

    ⓿ 为什么要有：15:05 之后当日曲线不会再变，而旧实现仍会为了一次点击串行重抓 16 个
      板块（7.5s + 0.55s/板的 sleep，全程占 GIL）。用户盘后复盘时点开面板就要等 7 秒，
      纯属浪费——这正是本次「盘后复盘优先」重构要消灭的等待。
    """
    kind = kind if kind in KINDS else DEFAULT_KIND
    top = max(4, min(int(top or TOP_DEFAULT), 24))
    # 2026-10-30：缓存 key 里并入「今天」。
    # 为什么必须并：盘后曲线 TTL 被放宽到 86400s（24 小时，见 rhythm.intraday_curve_ttl）。
    # 若 key 仍只有 (kind, top)，昨天 15:00 之后缓存的曲线会在**次日盘中前 5 个多小时**
    # 继续命中（24h 还没过期）——用户早上打开就看到昨天的曲线，等于把「盘后 24h」
    # 这个优化变成了新 bug。并进日期后，跨日必 miss，第二天开盘自动重抓。
    ck = "intra:%s:%d:%s" % (kind, top, datetime.now().strftime("%Y-%m-%d"))
    if not force:
        # 2026-10-30：TTL 由 rhythm 决定（盘中 1h / 盘后 24h），不再是写死的 INTRADAY_TTL。
        hit = _cache_get(ck, _r_curve_ttl())
        if hit:
            return hit
        # 2026-10-31：过期但旧值还在 → **先给旧画面**，后台单飞重算（原先这里直接往下走，
        # 让请求线程等 `_em_intraday` 串行抓 16 个板块，实测首点 1.8~2.0s）。
        # 必须 copy 一份再加标记：缓存里那个 dict 是共享对象，原地加 _stale 会污染后续命中。
        _old, _age = _cache_get_any(ck)
        if isinstance(_old, dict) and _old:
            _intra_swr_kick(ck, kind, top, conn)
            _stale = dict(_old)
            _stale["_stale"] = True
            _stale["_cache_age"] = int(_age or 0)
            return _stale

    cfg = KINDS[kind]
    now_s = datetime.now().strftime("%H:%M:%S")
    today_s = datetime.now().strftime("%Y-%m-%d")

    # ⓿ 收盘定稿优先：库里已有「今天这份**收盘后抓的**完整曲线」就直接用，绝不打东财分钟接口。
    #   ⚠️ 必须校验「是不是收盘后抓的」：库里当日行可能来自盘中（实测 2026-10-30 生产库
    #   industry 落库 14:40、concept 落库 11:07），直接返回会给用户一条截断曲线且缓存 24h。
    #   非定稿行不算命中，往下走 ① → 由 _live_fetch_gate 放行「有冷却的补抓重试」（10 分钟一次）。
    #
    #   2026-10-31 扩充（终验：周末重启后首点仍 1.8s）：原判据只有 `is_after_close()`，
    #   于是周六周日/节假日**永远走不到这个分支** —— 明明库里躺着最近交易日的曲线，
    #   每次重启还要为一次点击重抓 2 秒。现补一条：
    #     · 交易日 15:05 后 → 认库内「当日 + 收盘后抓的」那行（原逻辑不变）；
    #     · 休市（`is_trading_day() is False`）→ 认库内「最近交易日」那行（这天不会再变了）。
    #   休市这条多加了一道「曲线末点 ≥ 14:50」的完整性校验：实测 10-30 的 concept 行末点
    #   只到 11:08（半天）、industry 只到 14:41，当定稿返回等于是拿截断曲线顶一整个周末。
    #   不完整就往下走正常路径补抓（每 10 分钟重试一次，补齐后落库即锁 24h）。
    #   注意 `is_trading_day()` 未知时返回 None，**不能**用 `not ...` 判（会把未知当休市）。
    _after_close = _r_is_after_close()
    _trading_now = is_trading_day()
    _final_row = None
    if conn is not None:
        if _after_close:
            _final_row = _load_intraday(conn, kind, only_date=today_s)
            if _final_row and not _is_final_capture(_final_row.get("cached_at")):
                _final_row = None
        elif _trading_now is False:
            _latest = latest_trade_date()
            if _latest:
                _final_row = _load_intraday(conn, kind, only_date=_latest)
                if _final_row and not _curve_is_complete(_final_row):
                    logger.info("板块资金流曲线：休市日库内 %s/%s 末点不足，按非定稿处理（将补抓一次）",
                                kind, _latest)
                    _final_row = None
    if _final_row:
        logger.info("板块资金流曲线：定稿命中库内快照（%s/%s，末点 %s），跳过东财重抓",
                    kind, _final_row.get("data_date") or today_s,
                    ((_final_row.get("axis") or [""])[-1] or ""))
        _final_row.update({"ok": True, "kind": kind, "kind_label": cfg["label"], "updated_at": now_s})
        _market_note(_final_row)
        # 真定稿 → 用 rhythm 的 TTL（盘后 24h），这份数据今天不会再变，锁一天是对的。
        _cache_put(ck, _final_row, ttl=_r_curve_ttl())
        return _final_row

    # ① 东财实时（受节流闸；force=True 可穿透）
    #    `already_final` 传 ⓿ 的判定结果：正常情况 ⓿ 命中定稿就直接 return 了，走不到这里；
    #    留着它是为了防止将来 ⓿ 改成"不 return、继续往下走"时把定稿又重抓一遍。
    res = None
    if _live_fetch_gate(force, today_s, after_close=_after_close,
                        already_final=bool(_final_row)):
        res = _em_intraday(kind, top)
        if res:
            res.update({"ok": True, "kind": kind, "kind_label": cfg["label"], "updated_at": now_s})
            _market_note(res)
            _persist_intraday(conn, kind, res)
            # 2026-10-31（修回归）：只有「收盘/休市窗口 + 曲线末点跑满」才算定稿、才配 24h。
            # 抓到的是截断曲线（东财提前收工/限流只回了部分板块之外的情形）就只留 600s，
            # 10 分钟后 SWR 会再补一次，直到补齐为止 —— 绝不把截断数据锁一整天。
            _final_ok = bool(_curve_is_complete(res) and (_after_close or _trading_now is False))
            _cache_put(ck, res, ttl=_curve_cache_ttl(_final_ok, _after_close, _trading_now))
            return res

    # ② 本机采样（当日、≥2 点才有意义）。**永远不算定稿**：采样曲线是兜底口径，
    #    锁 24h 会把"东财恢复了本该换成完整曲线"的机会一起锁死。
    sample_res = None
    if conn is not None:
        try:
            sample_res = _sample_curve(conn, kind, top)
        except Exception:  # noqa: BLE001
            logger.exception("读取板块资金流采样曲线失败")
    if sample_res and len(sample_res.get("axis") or []) >= 2:
        sample_res.update({"ok": True, "kind": kind, "kind_label": cfg["label"],
                           "updated_at": now_s})
        _market_note(sample_res)
        _cache_put(ck, sample_res, ttl=_curve_cache_ttl(False, _after_close, _trading_now))
        return sample_res

    # ③ 东财库内快照（可能略旧，但完整；跑满 + 收盘窗口才算定稿）
    db_res = _load_intraday(conn, kind)
    if db_res:
        db_res.update({"ok": True, "kind": kind, "kind_label": cfg["label"], "updated_at": now_s})
        _market_note(db_res)
        _final_db = bool(_curve_is_complete(db_res) and (_after_close or _trading_now is False))
        _cache_put(ck, db_res, ttl=_curve_cache_ttl(_final_db, _after_close, _trading_now))
        return db_res

    # 全都不行：若采样只有 1 个点，也返回出去（前端画成"起点"并如实标注）
    if sample_res:
        sample_res.update({"ok": True, "kind": kind, "kind_label": cfg["label"],
                           "updated_at": now_s, "warming_up": True})
        _market_note(sample_res)
        _cache_put(ck, sample_res, ttl=_curve_cache_ttl(False, _after_close, _trading_now))
        return sample_res

    data = {
        "ok": False, "kind": kind, "kind_label": cfg["label"], "axis": [], "series": [],
        "reason": ("分时曲线暂不可用：东财分钟接口被风控拦截（它会掐掉程序发起的密集请求），"
                   "本机采样尚未积累到数据。查看器保持开启即会自动采样（盘中每 30 分钟一次），"
                   "交易时段稍后再看就有曲线；东财恢复时会自动补全当日完整曲线。"),
    }
    # 失败体也不算定稿：收盘窗口里只留 600s，10 分钟后自动重试（旧实现是一天一次，
    # 那次失败就等于一整天都没曲线）。
    _cache_put(ck, data, ttl=_curve_cache_ttl(False, _after_close, _trading_now))
    return data


def status(conn=None) -> dict:
    """自检：两源连通性 + 本机采样存量（供 /api/board_flow_status 与护栏用）。"""
    out = {"ok": True, "kinds": {}, "sample": {}}
    for kind in KINDS:
        snap = snapshot(kind, pz=5)
        out["kinds"][kind] = {
            "label": KINDS[kind]["label"],
            "ok": bool(snap.get("ok")),
            "source": snap.get("source") or "",
            "rows": len(snap.get("rows") or []),
            "reason": snap.get("reason") or "",
        }
    if conn is not None:
        try:
            today = datetime.now().strftime("%Y-%m-%d")
            r = conn.execute("SELECT COUNT(*), COUNT(DISTINCT ts), MAX(ts) "
                             "FROM board_flow_samples WHERE trade_date=?", (today,)).fetchone()
            out["sample"] = {"rows": r[0] if r else 0,
                             "points": r[1] if r else 0,
                             "last_ts": (r[2] if r else None),
                             "date": today}
        except Exception:  # noqa: BLE001
            out["sample"] = {"error": "采样表不可读"}
    cur = intraday(conn=conn)
    out["intraday"] = {"ok": bool(cur.get("ok")), "source": cur.get("source") or "",
                       "series": len(cur.get("series") or []),
                       "points": len(cur.get("axis") or [])}
    return out


# ===== 智能选股·板块分类（2026-09-30，王总需求）=====
# 板块清单 → 点板块 → 成分股按「综合分」排序（知名度口径）：
# 综合分 = 市值50% + 成交额30% + 换手率20%（各按板块内排名归一 0~100）。
# 市值=规模权重；成交额=活跃度/知名度代理；换手率=市场关注度。

RICH_TTL = 600            # 成分股（含综合分）缓存（秒）
RICH_FAIL_TTL = 60        # 失败冷却（秒）
RICH_MAX_PAGES = 3        # 每页 100 只、最多 3 页（新浪 getHQNodeData 单页上限）


def board_list(kind: str = DEFAULT_KIND, pz: int = 90) -> dict:
    """板块清单（不取代表股，供智能选股「板块分类」入口用）。

    复用 _sina_snapshot（新浪主源、长期稳定），空了走 _em_snapshot 兜底；
    合并去重后按净额降序返回。失败返回 ok=False + reason，绝不抛异常打断面板。
    """
    try:
        rows = _sina_snapshot(kind, pz, asc=0)
        if not rows:
            rows = _em_snapshot(kind, pz, asc=0)
        rows = _merge_two_way(rows, [])
        return {"ok": bool(rows), "kind": kind, "rows": rows,
                "reason": None if rows else "新浪与东财板块清单均未取到（网络受限，请稍后重试）"}
    except Exception as e:  # noqa: BLE001
        logger.warning("board_list 失败：%s", e)
        return {"ok": False, "kind": kind, "rows": [], "reason": str(e)}


def _members_fetch_raw(board_code: str) -> list:
    """新浪 getHQNodeData 多页抓取（含 mktcap/amount/turnoverratio 原始字段）。

    单位实测（2026-09-30，贵州茅台 600519：mktcap=155133876.6 万 = 1.55 万亿）：
    mktcap=万元、amount=元、turnoverratio=%。
    只留 6 位纯数字 A 股代码，按代码去重；页不满 100 即止。
    """
    code = str(board_code or "").strip()
    if not code:
        return []
    seen, out = set(), []
    for page in range(1, RICH_MAX_PAGES + 1):
        url = ("%s?page=%d&num=100&sort=mktcap&asc=0&node=%s&symbol=&_s_r_a=page&_=%s"
               % (_SINA_MEM_API, page, urllib.parse.quote(code), _ts()))
        txt = _http_text(url, _SINA_REF, timeout=6, retries=0)
        got = 0
        if txt:
            txt = txt.strip().lstrip("\ufeff")
            if txt.startswith("["):
                try:
                    arr = json.loads(txt)
                except Exception:  # noqa: BLE001
                    arr = []
                for x in arr:
                    if not isinstance(x, dict):
                        continue
                    c = str(x.get("code") or "").strip()
                    n = str(x.get("name") or "").strip()
                    if not c or not n or not re.match(r"^\d{6}$", c) or c in seen:
                        continue
                    seen.add(c)
                    mcap_w = _num(x.get("mktcap"))       # 万元
                    out.append({
                        "code": c, "name": n,
                        "price": _num(x.get("trade")),
                        "pct": _num(x.get("changepercent")),
                        "mcap_w": mcap_w,
                        "mcap_yuan": (mcap_w * 1e4) if mcap_w is not None else None,
                        "amount": _num(x.get("amount")),         # 元
                        "turnover": _num(x.get("turnoverratio")),  # %
                    })
                    got += 1
        if got < 100:
            break
    return out


def members_rich(board_code: str) -> dict:
    """板块成分股 + 综合分排序（市值50% / 成交额30% / 换手率20%，板块内排名归一）。

    智能选股「板块分类」点板块后调用。返回 {ok, code, total, rows(按综合分降序)}。
    成功缓存 RICH_TTL；失败冷却 RICH_FAIL_TTL（返回 ok=False + reason）。
    """
    code = str(board_code or "").strip()
    if not code:
        return {"ok": False, "code": code, "rows": [], "reason": "缺少板块代码"}
    ck = "memrich:%s" % code
    hit = _cache_get(ck, RICH_TTL)
    if hit is not None:
        return hit
    if _cache_get(ck + ":fail", RICH_FAIL_TTL) is not None:
        return {"ok": False, "code": code, "rows": [], "reason": "取数失败冷却中，约 1 分钟后重试"}
    rows = _members_fetch_raw(code)
    if not rows:
        _cache_put(ck + ":fail", 1)
        return {"ok": False, "code": code, "rows": [],
                "reason": "新浪成分股接口未返回数据（网络受限或板块代码无效）"}
    n = len(rows)

    def _ranks(key):
        order = sorted(range(n), key=lambda i: (rows[i].get(key)
                        if rows[i].get(key) is not None else -1e18), reverse=True)
        r = [0] * n
        for rank, idx in enumerate(order):
            r[idx] = rank + 1
        return r

    r_mcap, r_amt, r_to = _ranks("mcap_yuan"), _ranks("amount"), _ranks("turnover")
    den = max(1, n - 1)
    for i, x in enumerate(rows):
        s = 0.0
        if x.get("mcap_yuan") is not None:
            s += 0.5 * (1.0 - (r_mcap[i] - 1) / den)
        if x.get("amount") is not None:
            s += 0.3 * (1.0 - (r_amt[i] - 1) / den)
        if x.get("turnover") is not None:
            s += 0.2 * (1.0 - (r_to[i] - 1) / den)
        x["score"] = round(s * 100.0, 1)
    rows.sort(key=lambda x: x["score"], reverse=True)
    out = {"ok": True, "code": code, "total": n, "rows": rows, "reason": None,
           "total_note": ("共 %d 只成分股，已按综合分排序。" % n) if n < 300
                         else ("成分股较多，仅展示综合分前 %d 只。" % n)}
    _cache_put(ck, out)
    return out
