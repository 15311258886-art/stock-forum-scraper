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
       b) 兜底：交易时段每 60s 采一次快照入库（board_flow_samples），当天累积成曲线。
     响应里带 `source` 字段，界面按实际来源标注。
  ③ 节流缓存：快照 120s / 曲线 300s。把对外请求从「每次打开面板」降到「几分钟一次」，
     从根上避开东财风控（王总明确：**不需要实时，几分钟一发即可**）。

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
INTRADAY_TTL = 300        # 曲线缓存（秒）
SAMPLE_GAP = 30           # 采样间隔（秒）—— 半分钟一个点，收盘即有 400+ 点
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
    if time.time() - node.get("ts", 0) > ttl:
        return None
    return node.get("data")


def _cache_put(key: str, data) -> None:
    with _cache_lock:
        _cache[key] = {"ts": time.time(), "data": data}


def clear_cache() -> None:
    """清空内存缓存（前端「强制刷新」用）。"""
    with _cache_lock:
        _cache.clear()


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
def _sina_snapshot(kind: str, pz: int, asc: int = 0) -> list:
    """新浪板块资金流快照（主源，长期稳定）。

    字段：category(板块代码) name avg_changeratio(涨跌幅,小数) netamount(净额,元)
          ratioamount(净占比,小数) ts_name(代表股)

    asc=0 → 净流入榜；asc=1 → 净流出榜（「资金分布」两端都要看得见）。
    """
    cfg = KINDS.get(kind) or KINDS[DEFAULT_KIND]
    url = ("%s?page=1&num=%d&sort=netamount&asc=%d&fenlei=%s&_=%s"
           % (_SINA_API, max(1, min(int(pz or 50), 200)), 1 if asc else 0,
              cfg["sina_fenlei"], _ts()))
    txt = _http_text(url, _SINA_REF, timeout=10, retries=1)
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
    """东财分钟曲线：先取 Top N 板块，再逐板块串行取分钟（节流 0.55s）。"""
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
    return res


# ---------------------------------------------------------------- 曲线 B：本机采样累积
def _do_sample(conn, now: datetime) -> int:
    """抓一次快照写库（industry + concept 各一）。返回写入条数。"""
    total = 0
    date_s = now.strftime("%Y-%m-%d")
    ts_s = now.strftime("%H:%M")
    for kind in KINDS:
        snap = snapshot(kind, pz=60, force=True)
        if not snap.get("ok"):
            continue
        rows = [(date_s, ts_s, kind, r["board_code"], r["board_name"],
                 r.get("main_net"), r.get("pct"))
                for r in snap.get("rows", []) if r.get("board_name")]
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
    """交易时段内按 SAMPLE_GAP 节流采样一次。由后台线程调用；异常绝不外抛。"""
    try:
        now = datetime.now()
        # 休市日不采：新浪快照接口给的是「当天最新值」——休市那天就是上一交易日的全日量，
        # 采下来当当日样本，等于把 09-30 的收盘额记在 10-01 头上。
        if is_trading_day() is False:
            return 0
        m = now.hour * 60 + now.minute
        if not (SAMPLE_START_MIN <= m <= SAMPLE_END_MIN):
            return 0
        with _sample_lock:
            if now.timestamp() - _last_sample_ts["v"] < SAMPLE_GAP:
                return 0
            _last_sample_ts["v"] = now.timestamp()
        n = _do_sample(conn, now)
        if n:
            logger.info("板块资金流采样写入 %d 条（%s）", n, now.strftime("%H:%M"))
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


def _load_intraday(conn, kind: str) -> dict | None:
    """读已落库的东财曲线。

    按 kind 取**最近一条完整记录**——休市日/盘中抓到的曲线真实归属可能是别的日子，
    按「今天」查会查空，按日期降序找才能拿到；同时剔除残缺记录（如风控只成功 1 个板块），
    避免非交易日展示「只剩一根线」的脏数据。
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
      ① 东财分钟实时抓 → 成功即落库并返回（最完整 + 最新）
      ② 本机采样累积   → 当日已积累 ≥2 个点则返回（最新，但可能短）
      ③ 东财库内快照   → 当日抓到过就返回（完整，但可能略旧）
    """
    kind = kind if kind in KINDS else DEFAULT_KIND
    top = max(4, min(int(top or TOP_DEFAULT), 24))
    ck = "intra:%s:%d" % (kind, top)
    if not force:
        hit = _cache_get(ck, INTRADAY_TTL)
        if hit:
            return hit

    cfg = KINDS[kind]
    now_s = datetime.now().strftime("%H:%M:%S")

    # ① 东财实时
    res = _em_intraday(kind, top)
    if res:
        res.update({"ok": True, "kind": kind, "kind_label": cfg["label"], "updated_at": now_s})
        _market_note(res)
        _persist_intraday(conn, kind, res)
        _cache_put(ck, res)
        return res

    # ② 本机采样（当日、≥2 点才有意义）
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
        _cache_put(ck, sample_res)
        return sample_res

    # ③ 东财库内快照
    db_res = _load_intraday(conn, kind)
    if db_res:
        db_res.update({"ok": True, "kind": kind, "kind_label": cfg["label"], "updated_at": now_s})
        _market_note(db_res)
        _cache_put(ck, db_res)
        return db_res

    # 全都不行：若采样只有 1 个点，也返回出去（前端画成"起点"并如实标注）
    if sample_res:
        sample_res.update({"ok": True, "kind": kind, "kind_label": cfg["label"],
                           "updated_at": now_s, "warming_up": True})
        _market_note(sample_res)
        _cache_put(ck, sample_res)
        return sample_res

    data = {
        "ok": False, "kind": kind, "kind_label": cfg["label"], "axis": [], "series": [],
        "reason": ("分时曲线暂不可用：东财分钟接口被风控拦截（它会掐掉程序发起的密集请求），"
                   "本机采样尚未积累到数据。查看器保持开启即会自动采样（每 30 秒一次），"
                   "交易时段稍后再看就有曲线；东财恢复时会自动补全当日完整曲线。"),
    }
    _cache_put(ck, data)
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
