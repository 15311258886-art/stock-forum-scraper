# -*- coding: utf-8 -*-
"""行业龙头分级（金/银/铜牌）· 2026-10-21 进哥需求

口径来源（不是拍脑袋）：
  中证龙头企业指数（931802）编制方案 / 中证龙头企业指数（000960）选样方法：
  按二级行业分类，在行业内对「日均总市值、日均成交额、营业收入、净利润、毛利率、ROE」
  六项分别降序排名，把六项排名相加得综合排名，取行业头部。

本实现与之对应（差异如实标注）：
  · 行业口径：东财行业（业绩报表 PUBLISHNAME，约 86 个二级行业），不是中证二级，但同为二级颗粒度；
  · 报告期：用最近一期"基本披露完毕"的报告期（自动挑覆盖率 ≥80% 的最新期，当前为 2026 中报），
    不是严格的 TTM；同一行业内所有公司用同一期，横向可比；
  · 市值/成交额：腾讯实时行情（总市值、当日成交额）；中证用"过去一年日均"，此处为快照值。
  → 结论：用于"这只票在它所在行业里算不算头部"的**相对分档**足够；不用于指数复制。

三档（进哥 2026-10-21 定的放宽标准，比"真龙头"更宽）：
  金牌 = 行业内综合排名前 5%（真龙头口径）
  银牌 = 前 10%
  铜牌 = 前 15%
另有硬门槛：ST/*ST 不发牌（退市风险股不该挂龙头）；行业内有效样本 < 8 只不发牌（排名无统计意义）。

产物：
  data/dragon_tier.json   全量明细（含六项原值、六项分项排名），供审计与详情页
  /api/dragon_map         前端用的轻量映射 {code: tier} + meta
  /api/dragon?code=xxx    单只明细（弹窗里展示"行业第几/共几家/金牌理由"）
"""
from __future__ import annotations

import json
import math
import os
import threading
import time
import urllib.parse
import urllib.request
from datetime import datetime

_THIS = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_THIS)
OUT_JSON = os.path.join(_THIS, "data", "dragon_tier.json")

_TTL_H = 24.0            # 缓存有效期：24 小时（财报一季度才变一次，市值日变）
_MIN_IND_N = 3           # 行业内有效样本下限（不足 3 家不发牌）
_SMALL_IND_N = 8         # 小行业：样本 <8 时按固定名次 1金/2银/3铜（整数取整会误杀第2名）
SILVER = 0.10
BRONZE = 0.15
GOLD = 0.05

# 报告期候选（新→旧）：覆盖率 ≥80% 的最新一期即采用
_PERIODS = ["2026-09-30", "2026-06-30", "2026-03-31", "2025-12-31"]
_PERIOD_LABEL = {
    "2026-09-30": "2026三季报", "2026-06-30": "2026中报",
    "2026-03-31": "2026一季报", "2025-12-31": "2025年报",
}
_DC = "https://datacenter-web.eastmoney.com/api/data/v1/get"
_UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
_COLS = ("SECURITY_CODE,SECURITY_NAME_ABBR,PUBLISHNAME,TOTAL_OPERATE_INCOME,"
         "PARENT_NETPROFIT,WEIGHTAVG_ROE,XSMLL,UPDATE_DATE,DATATYPE")

_build_lock = threading.Lock()
_build_st = {"running": False, "started_at": 0.0, "err": None, "at": 0.0}
_mem = {"mtime": 0.0, "data": None}


def _opener():
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _get_json(url: str, timeout: int = 20):
    req = urllib.request.Request(url, headers=_UA)
    with _opener().open(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


# ───────────────────────── 1. 财报（行业 + 四项财务） ─────────────────────────
def _report_page(period: str, page: int, size: int = 500) -> dict:
    flt = urllib.parse.quote("(REPORTDATE='%s')" % period)
    url = ("%s?reportName=RPT_LICO_FN_CPD&columns=%s&pageNumber=%d&pageSize=%d"
           "&filter=%s&source=WEB&client=WEB" % (_DC, _COLS, page, size, flt))
    return _get_json(url)


_a_cache = {"codes": None}


def a_codes() -> set:
    """A 股白名单：取本机全市场前复权日K库的代码（5550 只）。

    为什么要白名单：东财业绩报表把新三板也返回了（2026 中报 11442 个 6 位代码，
    约一半是 43/83/87 开头的新三板），不筛会把新三板公司当成「行业内龙头」。
    """
    if _a_cache["codes"] is not None:
        return _a_cache["codes"]
    out = set()
    db = os.path.join(_ROOT, "data", "kline_daily.db")
    try:
        import sqlite3
        con = sqlite3.connect(db, timeout=10)
        try:
            out = {str(r[0]) for r in con.execute("SELECT DISTINCT code FROM kline")}
        finally:
            con.close()
    except Exception:  # noqa: BLE001
        out = set()
    _a_cache["codes"] = out
    return out


def fetch_period(period: str) -> dict:
    """取某一报告期全部已披露公司，按代码去重（同代码多行取 UPDATE_DATE 最新）。"""
    first = _report_page(period, 1, 1)
    res = first.get("result") or {}
    total = int(res.get("count") or 0)
    out = {}
    page, size = 1, 500
    while True:
        try:
            d = _report_page(period, page, size)
        except Exception:  # noqa: BLE001
            time.sleep(0.5)
            d = _report_page(period, page, size)
        rows = ((d.get("result") or {}).get("data")) or []
        if not rows:
            break
        allowed = a_codes()
        for r in rows:
            code = str(r.get("SECURITY_CODE") or "").strip()
            if len(code) != 6:
                continue
            if allowed and code not in allowed:
                continue
            old = out.get(code)
            if old is None or str(r.get("UPDATE_DATE") or "") >= str(old.get("UPDATE_DATE") or ""):
                out[code] = r
        if page * size >= total:
            break
        page += 1
        if page > 40:      # 安全阀：500×40=2 万行，远超单期总量
            break
    return out


def pick_period() -> tuple:
    """挑最近一个覆盖率 ≥80% 的报告期（覆盖率按全市场 5550 只近似）。"""
    best = None
    for p in _PERIODS:
        try:
            r = _report_page(p, 1, 1)
            n = int(((r.get("result") or {}).get("count")) or 0)
        except Exception:  # noqa: BLE001
            n = 0
        cov = n / 5550.0
        if cov >= 0.80:
            return p, cov
        if best is None or n > best[1]:
            best = (p, n)
    return (best[0], best[1] / 5550.0) if best else (_PERIODS[-1], 0.0)


# ───────────────────────── 2. 行情（总市值 + 成交额） ─────────────────────────
def _mkt_prefix(code: str) -> str:
    if code.startswith("6"):
        return "sh"
    if code.startswith(("0", "3")):
        return "sz"
    return "bj"


def fetch_quotes(codes: list, batch: int = 60) -> dict:
    """腾讯实时行情：总市值(亿) + 成交额(万元) + 名称。失败的公司留空，不影响排名。"""
    out = {}
    op = _opener()
    for i in range(0, len(codes), batch):
        chunk = codes[i:i + batch]
        q = ",".join(_mkt_prefix(c) + c for c in chunk)
        try:
            req = urllib.request.Request("https://qt.gtimg.cn/q=" + q, headers=_UA)
            with op.open(req, timeout=15) as r:
                txt = r.read().decode("gbk", "replace")
        except Exception:  # noqa: BLE001
            time.sleep(0.4)
            continue
        for line in txt.split("\n"):
            if "=" not in line or '"' not in line:
                continue
            try:
                arr = line.split('"')[1].split("~")
                code = str(arr[2]).strip()
                if len(code) != 6:
                    continue
                def fv(idx):
                    try:
                        v = float(arr[idx])
                        return v if v > 0 else None
                    except Exception:  # noqa: BLE001
                        return None
                out[code] = {"mktcap": fv(45), "amount": fv(37), "name": arr[1] or ""}
            except Exception:  # noqa: BLE001
                continue
        time.sleep(0.12)
    return out


# ───────────────────────── 3. 综合排名 → 三档 ─────────────────────────
_DIMS = [("mktcap", 1.0), ("amount", 1.0), ("rev", 1.0), ("np", 1.0), ("gross", 1.0), ("roe", 1.0)]


def _rank_desc(vals: dict) -> dict:
    """按数值降序给排名（1 最好）。

    None 的处理：**并列末尾**——同一维度全为空（典型：银行/保险没有销售毛利率），
    这些公司拿同一个名次，避免"按代码顺序"凭空制造名次差、污染综合排名。
    """
    valid = [(c, v) for c, v in vals.items() if v is not None]
    valid.sort(key=lambda kv: (-kv[1], kv[0]))
    out = {c: i + 1 for i, (c, _v) in enumerate(valid)}
    tail = len(valid) + 1
    for c, v in vals.items():
        if v is None:
            out[c] = tail
    return out


def score(rows: dict, quotes: dict, period: str) -> dict:
    """rows: code → 财报行；quotes: code → 行情。返回全量明细。"""
    stocks = {}
    for code, r in rows.items():
        q = quotes.get(code) or {}
        name = str(r.get("SECURITY_NAME_ABBR") or q.get("name") or "")
        ind = str(r.get("PUBLISHNAME") or "").strip()
        if not ind:
            continue
        try:
            rev = float(r.get("TOTAL_OPERATE_INCOME")) if r.get("TOTAL_OPERATE_INCOME") is not None else None
        except Exception:  # noqa: BLE001
            rev = None
        try:
            np_ = float(r.get("PARENT_NETPROFIT")) if r.get("PARENT_NETPROFIT") is not None else None
        except Exception:  # noqa: BLE001
            np_ = None
        try:
            roe = float(r.get("WEIGHTAVG_ROE")) if r.get("WEIGHTAVG_ROE") is not None else None
        except Exception:  # noqa: BLE001
            roe = None
        try:
            gross = float(r.get("XSMLL")) if r.get("XSMLL") is not None else None
        except Exception:  # noqa: BLE001
            gross = None
        stocks[code] = {
            "code": code, "name": name, "ind": ind,
            "mktcap": q.get("mktcap"), "amount": q.get("amount"),
            "rev": rev, "np": np_, "roe": roe, "gross": gross,
            "st": ("ST" in name.upper().replace("*", "")),
        }

    # 按行业分桶
    by_ind = {}
    for code, s in stocks.items():
        by_ind.setdefault(s["ind"], []).append(code)

    detail = {}
    for ind, codes in by_ind.items():
        n_all = len(codes)
        # 六个维度分别排名（行业内）
        sub = {}
        for dim, _w in _DIMS:
            sub[dim] = _rank_desc({c: stocks[c].get(dim) for c in codes})
        comp = {}
        for c in codes:
            comp[c] = sum(sub[dim][c] for dim, _w in _DIMS)
        order = sorted(codes, key=lambda c: (comp[c], c))
        n = n_all
        # 用「名次阈值」而不是浮点百分比：19 家的行业里第 1 名 pct=5.26% 会误踢出金牌。
        g_cut = max(1, int(math.ceil(GOLD * n)))
        s_cut = max(g_cut, int(math.ceil(SILVER * n)))
        b_cut = max(s_cut, int(math.ceil(BRONZE * n)))
        for i, c in enumerate(order):
            rank = i + 1
            pct = rank / float(n)
            tier = 0
            if not stocks[c]["st"]:
                if n < _MIN_IND_N:
                    tier = 0
                elif n < _SMALL_IND_N:
                    # 小行业（典型：保险Ⅱ 5 家）不能因为"样本不够"就不认龙头，
                    # 固定 第1=金 / 第2=银 / 第3=铜。
                    tier = 3 if rank == 1 else (2 if rank == 2 else (1 if rank == 3 else 0))
                elif rank <= g_cut:
                    tier = 3      # 金
                elif rank <= s_cut:
                    tier = 2      # 银
                elif rank <= b_cut:
                    tier = 1      # 铜
            rec = dict(stocks[c])
            rec.update({"ind_n": n, "ind_rank": rank, "pct": round(pct, 4),
                        "comp": comp[c], "tier": tier,
                        "ranks": {d: sub[d][c] for d, _w in _DIMS}})
            detail[c] = rec

    counts = {"gold": 0, "silver": 0, "bronze": 0, "none": 0}
    for r in detail.values():
        k = {3: "gold", 2: "silver", 1: "bronze"}.get(r["tier"], "none")
        counts[k] += 1

    # 行业自身也标一档：行业里有金牌的说明该行业已识别出真龙头
    ind_stats = {}
    for r in detail.values():
        s = ind_stats.setdefault(r["ind"], {"n": 0, "gold": 0, "top": None, "top_comp": 9e9})
        s["n"] += 1
        if r["tier"] == 3:
            s["gold"] += 1
        if r["ind_rank"] == 1 and r["comp"] < s["top_comp"]:
            s["top"], s["top_comp"] = r["code"], r["comp"]

    return {"period": period, "period_label": _PERIOD_LABEL.get(period, period),
            "industries": len(by_ind), "stocks": len(detail),
            "counts": counts, "detail": detail, "ind_stats": ind_stats}


# ───────────────────────── 4. 构建 / 读取 ─────────────────────────
def build(verbose: bool = False) -> dict:
    t0 = time.time()
    period, cov = pick_period()
    if verbose:
        print("[dragon] 采用报告期 %s（%s），覆盖率 %.1f%%" % (period, _PERIOD_LABEL.get(period, ""), cov * 100))
    rows = fetch_period(period)
    if verbose:
        print("[dragon] 财报复用 %d 家（去重后）" % len(rows))
    if not rows:
        raise RuntimeError("财报接口未取到任何数据，停止构建（不写假数据）")
    quotes = fetch_quotes(sorted(rows.keys()))
    if verbose:
        print("[dragon] 腾讯行情取到 %d 家" % len(quotes))
    if len(quotes) < len(rows) * 0.5:
        raise RuntimeError("行情覆盖不足（%d/%d），停止构建（不写假数据）" % (len(quotes), len(rows)))

    res = score(rows, quotes, period)
    payload = {
        "ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "period": res["period"], "period_label": res["period_label"],
        "industries": res["industries"], "stocks": res["stocks"],
        "counts": res["counts"],
        "basis": "中证龙头企业指数六项综合排名口径（总市值/成交额/营收/净利润/毛利率/ROE，行业内排名求和）"
                 "；报告期=%s；市值与成交额为腾讯实时快照" % res["period_label"],
        "rules": {"gold": "行业内综合排名前5%", "silver": "前10%", "bronze": "前15%",
                  "min_ind_n": _MIN_IND_N, "st_excluded": True},
        "detail": res["detail"],
        # 前端只用 {code: tier}；预写在这里，首拉不必现场遍历 5500 条明细（省 ~2s）
        "m": {c: int(r["tier"]) for c, r in res["detail"].items() if int(r.get("tier") or 0)},
    }
    os.makedirs(os.path.dirname(OUT_JSON), exist_ok=True)
    tmp = OUT_JSON + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False)
    os.replace(tmp, OUT_JSON)
    _mem["mtime"] = 0.0
    _mem["data"] = None
    if verbose:
        print("[dragon] 完成 %d 只 / %d 行业 / %s / %.1fs -> %s" % (
            payload["stocks"], payload["industries"], payload["counts"], time.time() - t0, OUT_JSON))
    return payload


def load(force: bool = False) -> dict:
    """读缓存（内存 + 文件 mtime），过期返回旧数据但由调用方决定是否后台重建。"""
    try:
        mt = os.path.getmtime(OUT_JSON)
    except OSError:
        return {}
    if not force and _mem["data"] is not None and _mem["mtime"] == mt:
        return _mem["data"]
    try:
        with open(OUT_JSON, encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception:  # noqa: BLE001
        return {}
    _mem["data"] = data
    _mem["mtime"] = mt
    return data


def age_hours() -> float:
    try:
        return (time.time() - os.path.getmtime(OUT_JSON)) / 3600.0
    except OSError:
        return 1e9


def build_async() -> dict:
    with _build_lock:
        if _build_st["running"]:
            return dict(_build_st)
        _build_st.update({"running": True, "started_at": time.time(), "err": None})

    def _run():
        try:
            build(verbose=True)
            _build_st["at"] = time.time()
        except Exception as e:  # noqa: BLE001
            _build_st["err"] = str(e)
            import logging
            logging.exception("[dragon] 构建失败")
        finally:
            _build_st["running"] = False

    threading.Thread(target=_run, name="dragon-build", daemon=True).start()
    return dict(_build_st)


def maybe_build(max_age_h: float = _TTL_H) -> bool:
    """调度器每轮调用：过期就后台重建。返回是否触发了重建。"""
    if _build_st["running"]:
        return False
    if age_hours() <= max_age_h:
        return False
    build_async()
    return True


def map_payload() -> dict:
    """前端轻量映射：{code: tier} + meta（不含明细，控制体积）。"""
    d = load()
    if not d:
        return {"ok": False, "reason": "龙头分级尚未生成", "m": {}, "meta": {}}
    m = d.get("m")
    if not isinstance(m, dict) or not m:
        m = {}
        for code, r in (d.get("detail") or {}).items():
            t = int(r.get("tier") or 0)
            if t:
                m[code] = t
    return {"ok": True, "m": m,
            "meta": {"ts": d.get("ts"), "period": d.get("period"),
                     "period_label": d.get("period_label"),
                     "counts": d.get("counts"), "industries": d.get("industries"),
                     "stocks": d.get("stocks"), "rules": d.get("rules"),
                     "basis": d.get("basis"),
                     "stale": age_hours() > _TTL_H}}


def one(code: str) -> dict:
    d = load()
    code = str(code or "").strip()[-6:]
    r = ((d.get("detail") or {}).get(code)) if d else None
    if not r:
        return {"ok": False, "reason": "无该股分级数据"}
    return {"ok": True, "row": r, "meta": {"period_label": d.get("period_label"),
                                           "ts": d.get("ts"), "rules": d.get("rules")}}


if __name__ == "__main__":
    import sys
    if "--build" in sys.argv:
        build(verbose=True)
    else:
        print(json.dumps(map_payload()["meta"], ensure_ascii=False, indent=2))
