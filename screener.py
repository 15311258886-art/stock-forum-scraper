# -*- coding: utf-8 -*-
"""增强功能② 智能选股（东财「价值分析」报表 RPT_VALUEANALYSIS_DET）。

字段实测（2026-09-27 真机）：SECURITY_CODE / SECURITY_NAME_ABBR / BOARD_NAME /
PE_TTM / PE_LAR / PB_MRQ / PS_TTM / PEG_CAR / PCF_OCF_TTM / TOTAL_MARKET_CAP /
CHANGE_RATE / TRADE_DATE。**该报表没有 ROE / 股息率字段**，故预设策略不含这两项。
"""
from __future__ import annotations
import datetime as _dt
import json as _json
import os as _os
import threading as _threading
import time as _time

import emapi

# ===== 参数化指标定义（2026-10-07 王总：阈值可调 + 合并持股档 + 质量指标扩充）=====
# kind:
#   val   —— 东财「价值分析」报表 RPT_VALUEANALYSIS_DET 字段，可直接拼进东财过滤表达式；
#   hold  —— 股东户数地图二次过滤，阈值单位「万户」（×1e4 转户数）；
#   qual  —— 质量指标（ROE/毛利率/负债率/股息率），走 quality_map() 二次过滤（数据源见 _quality_map）；
#   score —— 「入手值得率」评分（0~100，口径同「入手概率」页换手率五维评分），走 _worth_scores() 二次过滤。
# op: lt / gt ；default: 建议默认值（前端输入框初始值）；scale: 阈值×scale = 东财字段实际值；
# unit: 单位（仅展示）。旧 key(value/lowpb/...) 在 run() 里映射到新 key（见 _LEGACY）。
METRICS = {
    "pe":      {"kind": "val",  "field": "PE_TTM",         "op": "lt", "default": 20, "name": "估值低·PE<",      "unit": ""},
    "pb":      {"kind": "val",  "field": "PB_MRQ",         "op": "lt", "default": 1,  "name": "破净·PB<",        "unit": ""},
    "ps":      {"kind": "val",  "field": "PS_TTM",         "op": "lt", "default": 2,  "name": "低市销·PS<",      "unit": ""},
    "peg":     {"kind": "val",  "field": "PEG_CAR",        "op": "lt", "default": 1,  "name": "低PEG·PEG<",      "unit": ""},
    "pcf":     {"kind": "val",  "field": "PCF_OCF_TTM",    "op": "lt", "default": 10, "name": "高现金流·PCF<",   "unit": ""},
    "cap":     {"kind": "val",  "field": "TOTAL_MARKET_CAP","op": "lt", "default": 50, "name": "中小盘·市值<",   "unit": "亿", "scale": 1e8},
    "holders": {"kind": "hold", "field": "HOLDER_NUM",     "op": "lt", "default": 1,  "name": "股东户数<",       "unit": "万户", "scale": 1e4},
    # 「入手值得率」档（2026-10-07 王总需求）：按 0~100 评分阈值过滤，默认 70=值得持有档
    #    （>=85 积极关注 / 70-84 值得持有 / 50-69 中性观望）。评分需日 K+流通股本，
    #    全走磁盘缓存：首跑要补 K 线较慢，当天再筛秒出。评分缺失的票视为不满足。
    "worth":   {"kind": "score","field": "WORTH",          "op": "gte","default": 70, "name": "值得率≥",         "unit": ""},
    # 质量指标（巴菲特口径）：数据源当前在本运行环境受限（见 _quality_map 注释），选中时返回诚实提示。
    "roe":     {"kind": "qual", "field": "ROE",           "op": "gt", "default": 15, "name": "高ROE>",   "unit": "%"},
    "gm":      {"kind": "qual", "field": "GROSS_MARGIN",  "op": "gt", "default": 30, "name": "高毛利率>", "unit": "%"},
    "dr":      {"kind": "qual", "field": "DEBT_RATIO",    "op": "lt", "default": 50, "name": "低负债率<", "unit": "%"},
    "dy":      {"kind": "qual", "field": "DIV_YIELD",     "op": "gt", "default": 2,  "name": "高股息率>", "unit": "%"},
}
VAL_KEYS = [k for k, v in METRICS.items() if v["kind"] == "val"]
QUAL_KEYS = [k for k, v in METRICS.items() if v["kind"] == "qual"]
SCORE_KEYS = [k for k, v in METRICS.items() if v["kind"] == "score"]
# 旧 screen key → (新 key, 默认阈值)。holdconc/holdrel 合并为 holders（非常集中=1万户 / 相对集中=2万户）。
_LEGACY = {
    "value": ("pe", 20), "lowpb": ("pb", 1), "lowps": ("ps", 2), "lowpeg": ("peg", 1),
    "cashcow": ("pcf", 10), "smallcap": ("cap", 50),
    "holdconc": ("holders", 1), "holdrel": ("holders", 2),
}


def _fmt(v) -> str:
    """阈值转东财过滤串用的字符串：整数值不带小数，浮点保留精度。"""
    f = float(v)
    if f == int(f):
        return str(int(f))
    return ("%.6f" % f).rstrip("0").rstrip(".")


def _qual_ok(q: dict, k: str, thr_k: float) -> bool:
    """单只股票的质量字典 q 是否满足指标 k 的阈值。"""
    m = METRICS[k]
    v = q.get(m["field"])
    if v is None:
        return False
    return (v > thr_k) if m["op"] == "gt" else (v < thr_k)


# 值得率评分当日缓存（内存 + 落盘），见 _worth_scores
_WORTH_LOCK2 = _threading.Lock()
_WORTH_MEM = {"date": None, "scores": {}}
_WORTH_SCORES_CACHE = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)),
                                    "data", "worth_scores_cache.json")


def _worth_scores(codes: list, days: int = 30) -> dict:
    """{code: 入手值得率评分(0~100) 或 None}——复用「入手概率」页的评分机器。

    2026-10-07 王总反馈连点条件太慢：评分结果按交易日缓存（内存 + 落盘
    data/worth_scores_cache.json），当天同一只票绝不重复算分，只对缓存缺失的票现算；
    日 K 走 decline_kcache 当天磁盘缓存、流通股本走 7 天缓存（与 turnover_top 同源）。
    K 线不足 / 成交量缺失 / 评分异常的票记 None（按不满足阈值处理，不硬评分；
    None 不落缓存，下次自动重试——与「抓失败不标新鲜」纪律一致）。

    2026-10-13 进哥：评分窗口可调（20~100 天）。缓存按 (code, days) 分键，
    否则切换窗口会拿到旧窗口的分数、误判。窗口上限夹在日 K 缓存上限内
    (_DECLINE_KEEP_BARS-1=139)，超过则降级到该上限——长窗值得率见板块二用 120 日。
    """
    codes = [str(c) for c in codes if c]
    if not codes:
        return {}
    days = max(5, min(int(days or 30), _DECLINE_KEEP_BARS - 1))
    today = _dt.date.today().isoformat()

    def _key(c):
        return "%s|%d" % (c, days)

    with _WORTH_LOCK2:
        if _WORTH_MEM.get("date") != today:
            mem = {}
            try:
                if _os.path.exists(_WORTH_SCORES_CACHE):
                    with open(_WORTH_SCORES_CACHE, encoding="utf-8") as fh:
                        dd = _json.load(fh)
                    if dd.get("date") == today and isinstance(dd.get("scores"), dict):
                        mem = {str(k): v for k, v in dd["scores"].items()}
            except Exception:
                mem = {}
            _WORTH_MEM.update({"date": today, "scores": mem})
        else:
            mem = _WORTH_MEM["scores"]
        todo = [c for c in codes if _key(c) not in mem]
    out = {c: mem.get(_key(c)) for c in codes}
    if not todo:
        return out
    try:
        bars_map, _meta = _load_bars_map({c: "" for c in todo}, days)
    except Exception:  # noqa: BLE001
        return out
    fmap = _float_map(list(bars_map.keys())) if bars_map else {}
    tset = set(todo)
    fresh = {}
    for code, bars in bars_map.items():
        if code not in tset or len(bars) < days + 1 or not bars[-1][4]:
            continue
        ent = fmap.get(code) or (None, None, 0.0, 0.0)
        try:
            ev = _turnover_eval(bars, ent[0], ent[1], days, to_scale=_to_scale_of(code, ent))
        except Exception:  # noqa: BLE001
            ev = None
        if ev and ev.get("score") is not None:
            fresh[_key(code)] = ev["score"]
    if fresh:
        with _WORTH_LOCK2:
            _WORTH_MEM["scores"].update(fresh)
            try:
                tmp = _WORTH_SCORES_CACHE + ".tmp"
                with open(tmp, "w", encoding="utf-8") as fh:
                    _json.dump({"date": today, "scores": _WORTH_MEM["scores"]}, fh, ensure_ascii=False)
                _os.replace(tmp, _WORTH_SCORES_CACHE)
            except Exception:
                pass
    out.update(fresh)
    return out


def _row(x: dict) -> dict:
    return {
        "code": x.get("SECURITY_CODE"),
        "name": x.get("SECURITY_NAME_ABBR"),
        "industry": x.get("BOARD_NAME") or "",
        "pe": emapi._num(x.get("PE_TTM")),
        "pb": emapi._num(x.get("PB_MRQ")),
        "ps": emapi._num(x.get("PS_TTM")),
        "peg": emapi._num(x.get("PEG_CAR")),
        "pcf": emapi._num(x.get("PCF_OCF_TTM")),
        "mcap": emapi._num(x.get("TOTAL_MARKET_CAP")),
        "pct": emapi._num(x.get("CHANGE_RATE")),
        "roe": None, "gm": None, "dr": None, "dy": None,
        "trade_date": str(x.get("TRADE_DATE") or "")[:10],
    }


# METRICS val key → _row() 结果里的字段名（快照本地过滤用）
_ROWKEY = {"pe": "pe", "pb": "pb", "ps": "ps", "peg": "peg", "pcf": "pcf", "cap": "mcap"}

# ===== 当日全市场估值快照（2026-10-07 王总反馈「每点一次条件都等 10~20 秒」提速）=====
# 根因：此前每个条件组合都现场向东财翻页拉数，换一组条件就重翻一轮。
# 改法：每个交易日只拉一次全市场快照（约 11 页×500 行），内存 → 磁盘(data/screener_val_cache.json)
# → 东财 三级缓存；之后所有条件组合都在内存里毫秒级过滤，换天自动重拉。
_VAL_LOCK = _threading.Lock()
_VAL_MEM = None            # {"date": d, "rows": [_row 结果]}
_VAL_NEG_UNTIL = 0.0       # 拉取失败后的短期冷却，避免连点反复撞东财
_VAL_CACHE = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)),
                           "data", "screener_val_cache.json")


def _val_snapshot() -> tuple:
    """(rows, trade_date, err)：当日全市场估值快照；失败返回 (None, None, 原因)。

    整体超时预算（默认 8s）：东财估值报表在本运行环境被网络策略拦截，
    若不做预算，latest_valuation_date 会逐日回退撞超时、datacenter 翻页也会拖挂死，
    导致 /api/screener 请求永久卡死（即 2026-10「智能选股死按钮」根因）。
    预算内取不到就快速返回 None（上层 run 诚实降级），绝不挂死。
    """
    global _VAL_MEM, _VAL_NEG_UNTIL
    with _VAL_LOCK:
        if _VAL_MEM is not None:
            return _VAL_MEM["rows"], _VAL_MEM["date"], None
        if _time.time() < _VAL_NEG_UNTIL:
            return None, None, "估值快照刚拉取失败（东财无响应），冷却中请稍后再试"
        today = _dt.date.today().isoformat()
        try:
            if _os.path.exists(_VAL_CACHE):
                with open(_VAL_CACHE, encoding="utf-8") as fh:
                    dd = _json.load(fh)
                if dd.get("date") == today and isinstance(dd.get("rows"), list) and len(dd["rows"]) > 100:
                    _VAL_MEM = {"date": dd["date"], "rows": dd["rows"]}
                    return _VAL_MEM["rows"], _VAL_MEM["date"], None
        except Exception:
            pass
        # 预算给足一次「全市场快照重建」的时间（东财 datacenter 在本环境秒回，约 18~25s 翻完 ~40 页）；
        # 若东财真不可达/极慢，到预算即放弃（返回 None 由上层诚实降级），绝不挂死。
        deadline = _time.time() + 25
        d = emapi.latest_valuation_date(max_seconds=6)
        if not d:
            _VAL_NEG_UNTIL = _time.time() + 300
            return None, None, "未取到最近交易日（东财估值报表不可达，请检查网络/稍后重试）"
        rows, seen = [], set()
        try:
            for page in range(1, 40):
                if _time.time() > deadline:
                    break
                r = emapi.datacenter("RPT_VALUEANALYSIS_DET",
                                     flt="(TRADE_DATE='%s')" % d,
                                     sort_columns="TOTAL_MARKET_CAP", sort_types="1",
                                     page_size=500, page_number=page)
                if not r["ok"]:
                    if page == 1:
                        rows = []
                    break
                for x in r["rows"]:
                    rr = _row(x)
                    c = str(rr.get("code") or "")
                    if c and c not in seen:
                        seen.add(c)
                        rows.append(rr)
                if len(r["rows"]) < 500:
                    break
                _time.sleep(0.3)
        except Exception:
            rows = []
        if len(rows) < 100:
            _VAL_NEG_UNTIL = _time.time() + 300
            return None, None, "估值快照拉取不完整（东财数据中心无响应），请稍后重试"
        _VAL_MEM = {"date": d, "rows": rows}
        try:
            tmp = _VAL_CACHE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                _json.dump({"date": d, "rows": rows}, fh, ensure_ascii=False)
            _os.replace(tmp, _VAL_CACHE)
        except Exception:
            pass
        return _VAL_MEM["rows"], _VAL_MEM["date"], None


def run(screen: str = "pe", limit: int = 40, params: dict = None) -> dict:
    """多条件组合筛选（2026-10-07 阈值可调 + 合并持股档 + 质量扩充）。

    screen: 逗号分隔的选中指标 key（如 "pe,pb,holders"）；旧 key(value/lowpb/holdconc...) 自动映射。
    params: {key: 阈值数值}，缺省用 METRICS 默认值。各选中条件 AND 叠加。
    - val  指标 → 当日全市场估值快照（_val_snapshot，内存/磁盘当天缓存）本地过滤；
    - holders → 股东户数地图二次过滤（阈值单位万户，×1e4）；
    - qual 指标 → quality_map() 二次过滤；数据源受限时返回诚实失败原因（见 _quality_map）。
    - score（值得率）→ 只对过滤后幸存者评分（评分当日缓存，见 _worth_scores）。
    提速（2026-10-07 王总反馈连点条件等 10~20 秒）：快照/户数/评分全部当天缓存后，
    除当日首跑外，任何条件组合都是本地毫秒级过滤，不再逐组合翻东财。
    """
    # 1) 解析并归一化选中指标（旧 key 映射 + 去重，最多 8 个）
    raw = [s.strip() for s in str(screen or "pe").split(",") if s.strip()]
    seen = {}
    for s in raw:
        if s in METRICS:
            seen.setdefault(s, None)
        elif s in _LEGACY:
            seen.setdefault(*_LEGACY[s])
    keys = list(seen.keys())[:8]
    if not keys:
        keys = ["pe"]

    # 2) 解析各指标阈值（前端 p 传入优先，否则用默认值）
    params = params or {}
    thr = {}
    for k in keys:
        v = params.get(k)
        if v in (None, ""):
            thr[k] = METRICS[k]["default"]
        else:
            try:
                thr[k] = float(v)
            except (TypeError, ValueError):
                thr[k] = METRICS[k]["default"]

    val_keys = [k for k in keys if METRICS[k]["kind"] == "val"]
    hold_keys = [k for k in keys if METRICS[k]["kind"] == "hold"]
    qual_keys = [k for k in keys if METRICS[k]["kind"] == "qual"]
    score_keys = [k for k in keys if METRICS[k]["kind"] == "score"]
    limit = max(1, min(int(limit or 40), 200))
    desc = " + ".join("%s%s%s" % (METRICS[k]["name"], _fmt(thr[k]), METRICS[k]["unit"]) for k in keys)

    # 2.5) 当日全市场估值快照（内存→磁盘→东财 三级缓存）——仅在选中「估值/持股/质量」类指标时才需要；
    #      纯「值得率」类（换手率五维评分，数据源=腾讯/新浪日 K）不依赖东财估值快照，
    #      用新浪全 A 股清单作候选宇宙即可筛选，东财不可达也能出数（根治「智能选股死按钮」）。
    need_snap = bool(val_keys or hold_keys or qual_keys)
    d = _dt.date.today().isoformat()
    snap, serr = None, None
    if need_snap:
        snap, d, serr = _val_snapshot()
        if snap is None:
            return {"ok": False, "screen": screen, "desc": desc, "rows": [], "reason": serr}

    # 3) 质量指标：数据源受限则诚实返回（不伪造数据）
    qmap = None
    if qual_keys:
        qmap, qreason = _quality_map()
        if qmap is None:
            return {"ok": False, "screen": screen, "desc": desc, "rows": [], "trade_date": d,
                    "reason": qreason}

    # 4) 股东户数地图（合并为单一可调档，阈值单位万户；当天磁盘缓存）
    hmap = _holder_conc_map() if hold_keys else None
    if hold_keys and hmap is None:
        return {"ok": False, "screen": screen, "desc": desc, "rows": [], "trade_date": d,
                "reason": "股东户数数据暂不可取（东财数据中心无响应），请稍后重试"}
    hmax = min((thr[k] * METRICS[k].get("scale", 1.0) for k in hold_keys), default=None)

    def _attach(rows):
        if hmap:
            for r in rows:
                h = hmap.get(str(r.get("code") or ""))
                if h:
                    r["holder_num"] = h["num"]; r["holder_ratio"] = h["ratio"]; r["holder_end"] = h["end"]
        if qmap:
            for r in rows:
                q = qmap.get(str(r.get("code") or ""))
                if q:
                    r["roe"] = q.get("ROE"); r["gm"] = q.get("GROSS_MARGIN")
                    r["dr"] = q.get("DEBT_RATIO"); r["dy"] = q.get("DIV_YIELD")
        return rows

    def _qual_pass(code):
        q = qmap.get(str(code or "")) if qmap else None
        if q is None:
            return False
        return all(_qual_ok(q, k, thr[k]) for k in qual_keys)

    def _vsort(rows):
        """按首个估值指标升序（与旧东财排序口径一致）；无估值条件时保持快照原序（市值升序）。"""
        if val_keys:
            rk = _ROWKEY[val_keys[0]]
            return sorted(rows, key=lambda x: (x.get(rk) is None,
                                               x.get(rk) if x.get(rk) is not None else 0))
        return rows

    # 5) 本地 AND 过滤：估值 + 持股 + 质量（快照在内存里，毫秒级）
    if snap is not None:
        rows = list(snap)
    else:
        # 纯「值得率」类：无需东财估值快照，用新浪全 A 股清单（_market_pool，当日缓存）作候选宇宙，
        # 再按换手率五维评分阈值过滤——数据源=腾讯/新浪日 K，东财不可达也能出真实数据。
        pool = _market_pool()
        if not pool:
            return {"ok": False, "screen": screen, "desc": desc, "rows": [],
                    "reason": "全市场清单获取失败（新浪 hs_a 暂不可用），请稍后重试"}
        rows = [{"code": c, "name": (n or "")} for c, n in pool.items()]
    for k in val_keys:
        m = METRICS[k]; rk = _ROWKEY[k]; v = thr[k] * m.get("scale", 1.0)
        if m["op"] == "lt":
            rows = [x for x in rows if x.get(rk) is not None and 0 < x[rk] < v]
        else:
            rows = [x for x in rows if x.get(rk) is not None and x[rk] > v]
    if hold_keys:
        rows = [x for x in rows
                if hmap.get(str(x.get("code") or "")) is not None
                and hmap[str(x.get("code"))]["num"] < hmax]
    if qual_keys:
        rows = [x for x in rows if _qual_pass(x.get("code"))]
    rows = _attach(rows)

    # 6) 值得率：只对过滤后的幸存者评分（评分当日缓存，越点越快）
    if score_keys:
        if val_keys or hold_keys or qual_keys:
            # 与旧口径一致：先按首估值指标升序，分块评分、凑满 limit 即停（少算很多分）
            rows = _vsort(rows)
            out, CH = [], 120
            for i in range(0, len(rows), CH):
                chunk = rows[i:i + CH]
                smap = _worth_scores([c.get("code") for c in chunk])
                for r0 in chunk:
                    r0["worth"] = smap.get(str(r0.get("code") or ""))
                out.extend(r0 for r0 in chunk
                           if r0.get("worth") is not None
                           and all(r0["worth"] >= thr[k] for k in score_keys))
                if len(out) >= limit:
                    break
            rows = out
        else:
            # 纯值得率：需要全市场评分排名（当日首跑补分较慢，之后走评分缓存秒出）
            smap = _worth_scores([x.get("code") for x in rows])
            for r0 in rows:
                r0["worth"] = smap.get(str(r0.get("code") or ""))
            rows = [r0 for r0 in rows
                    if r0.get("worth") is not None
                    and all(r0["worth"] >= thr[k] for k in score_keys)]
            rows.sort(key=lambda x: -(x.get("worth") or 0))
    elif hold_keys and not val_keys:
        rows.sort(key=lambda x: (x.get("holder_num") or 1 << 62))

    # 7) 行业归组（2026-10-07 王总：同行业的票并列放一起，一个板块一个板块往下走）。
    #    稳定排序：先按行业名分块，块内保留上面算好的相关度顺序（值得率降序/户数升序等）。
    rows.sort(key=lambda x: x.get("industry") or "其他")
    return {"ok": True, "screen": screen, "desc": desc, "trade_date": d,
            "rows": rows[:limit], "reason": None}


# ===== 连续阴跌扫描 + 换手率策略评分（2026-09-30，王总需求）=====
# 口径1（阴跌）：近 N 个交易日中，收盘低于前一日收盘（前复权）的天数 >= M。
# 口径2（换手率策略，「入手值得率」0~100 五维合成）：
#   ① 位置分位 30 分 —— 现价在近 N 日高低区间的相对位置（低位=吸筹语境，高位=兑现风险）；
#   ② 场景+温度 30 分 —— 换手率四大实战场景（低位高换手=吸筹 / 低位低换手=洗盘 /
#      高位低换手=锁仓 / 高位高换手=出货）+ 温度刻度（按流通市值分档，蓝筹基线低）；
#   ③ 换手持续性 15 分 —— 近3日均换手 vs 前10日均换手，温和抬升最健康，异常脉冲（疑似对倒）扣分；
#   ④ 量价配合 15 分 —— 下跌日缩量=承接、低点不创新低=洗盘特征、放量下跌/长上影=警示；
#   ⑤ 企稳信号 10 分 —— 当日转红、跌势收窄、收复 5 日前收盘。
#   档位：>=85 积极关注 / 70-84 值得持有 / 50-69 中性观望 / 30-49 偏弱回避 / <30 技术面弱势（技术面评分，非风险事件）。
# 扫描范围（活跃池）由 server.py 给出：智能选股6策略并集 + 关注池 + 近10日龙虎榜。
# 日 K 复用腾讯源（与 extref.backtest 同源同接口），缓存 data/decline_kcache.json ——
#   当天抓过即复用，调参数秒出。换手率 = 成交量(手)×100 ÷ 流通股本；
#   流通股本由腾讯实时行情（流通市值/现价）换算，缓存 data/float_shares_cache.json，7 天刷新。
# ⚠️ 纪律（预测缓存污染同款教训）：只有成功抓到 K 线（含成交量）才允许把条目标成"新鲜"，
#    抓失败的留旧值、下次扫描自动重试，绝不把失败标记成已处理。

import datetime as _dt
import json as _json
import os as _os
import re as _re
import threading as _threading
import time as _time
from concurrent.futures import ThreadPoolExecutor as _TPE

from extref.backtest import fetch_daily, _http_text, _prefix, TX_KLINE

# ===== 多条件组合筛选 + 持股集中度（2026-10-07，王总需求）=====
# ① 多选 AND：screen 参数支持逗号分隔（如 value,lowpb），把各条件的过滤表达式拼接即可；
# ② 持股集中度（王总 2026-10-07：合并原「非常集中<1万户 / 相对集中<2万户」两档为单一可调档）：
#    股东户数 <X 万户，X 由用户在前端自由输入（建议值沿用原标准：非常集中=1 / 相对集中=2）。
#    数据源=东财数据中心全市场股东户数报表 RPT_HOLDERNUM_DET
#    （字段实测 2026-10-07：SECURITY_CODE / HOLDER_NUM / HOLDER_NUM_RATIO / END_DATE），
#    按户数升序翻完全市场所有页（不再以 2 万户硬停，具体阈值由 run() 按 hmax 取），
#    每股保留最新一期披露（END_DATE 降序去重），近 400 天内有披露的才认。
#    首跑约全市场升序翻页（~10 页×500 行），落盘 data/holder_conc_cache.json 当天复用。
_HOLDER_LOCK = _threading.Lock()
_HOLDER_MEM: dict | None = None
_HOLDER_NEG_UNTIL = 0.0    # 拉取失败后的短期冷却（600s），避免连点反复翻 45 页
_HOLDER_CACHE = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)),
                              "data", "holder_conc_cache.json")
_HOLDER_PAGE_SIZE = 500
_HOLDER_MAX_PAGES = 60


def _holder_conc_map() -> dict | None:
    """全市场「股东户数」地图：code -> {"num","ratio","end"}；失败返回 None。

    升序翻完全市场所有页（不再以 2 万户硬停）——具体阈值在 run() 里按 hmax 取，
    因此前端无论设 1 万 / 2 万 / 5 万户都能正确过滤。每股保留最新一期披露。
    """
    global _HOLDER_MEM, _HOLDER_NEG_UNTIL
    with _HOLDER_LOCK:
        if _HOLDER_MEM is not None:
            return _HOLDER_MEM
        if _time.time() < _HOLDER_NEG_UNTIL:
            return None
        today = _dt.date.today().isoformat()
        try:
            if _os.path.exists(_HOLDER_CACHE):
                with open(_HOLDER_CACHE, encoding="utf-8") as fh:
                    d = _json.load(fh)
                if d.get("date") == today and isinstance(d.get("map"), dict):
                    _HOLDER_MEM = d["map"]
                    return _HOLDER_MEM
        except Exception:
            pass
        cutoff = (_dt.date.today() - _dt.timedelta(days=400)).isoformat()
        latest: dict = {}
        deadline = _time.time() + 25          # 整体预算：东财不可达/极慢时快速放弃，绝不挂死
        try:
            for page in range(1, _HOLDER_MAX_PAGES + 1):
                if _time.time() > deadline:
                    break
                r = emapi.datacenter(
                    "RPT_HOLDERNUM_DET",
                    flt="(END_DATE>='%s')" % cutoff,
                    sort_columns="HOLDER_NUM", sort_types="1",
                    page_size=_HOLDER_PAGE_SIZE, page_number=page)
                if not r["ok"] or not r["rows"]:
                    break
                for x in r["rows"]:
                    code = x.get("SECURITY_CODE") or ""
                    n = emapi._num(x.get("HOLDER_NUM"))
                    if not code or not n or n <= 0:
                        continue
                    end = str(x.get("END_DATE") or "")[:10]
                    prev = latest.get(code)
                    if prev is None or end > prev["end"]:
                        latest[code] = {"num": n,
                                        "ratio": emapi._num(x.get("HOLDER_NUM_RATIO")),
                                        "end": end}
                # 全市场地图：升序翻完所有页（不再以 2 万户硬停），具体阈值在 run() 按 hmax 取。
                # 末页（不足一页）即全市场末尾，停止翻页。
                if len(r["rows"]) < _HOLDER_PAGE_SIZE:
                    break
                _time.sleep(0.35)
        except Exception:
            latest = {}
        if not latest:
            _HOLDER_NEG_UNTIL = _time.time() + 600   # 失败不落缓存，只短冷却，下回合重试
            return None
        _HOLDER_MEM = latest
        try:
            tmp = _HOLDER_CACHE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                _json.dump({"date": today, "map": latest}, fh, ensure_ascii=False)
            _os.replace(tmp, _HOLDER_CACHE)
        except Exception:
            pass
        return _HOLDER_MEM


def _quality_map() -> tuple:
    """全市场「质量指标」地图：code -> {"ROE","GROSS_MARGIN","DEBT_RATIO","DIV_YIELD"}。

    数据源（2026-10-07 王总决策：东财 push2 接口）：
    本运行环境的网络策略**整体掐断** push2.eastmoney.com（单只/全市场均 "Remote end closed connection"），
    而 datacenter-web / datacenter 的财务表（RPT_FINANCE_MAINFINANCE 等）返回「报表配置不存在」或字段契约对不上，
    AkShare 安装亦被沙箱 guard 拦截 —— 当前**确实取不到 ROE/毛利率/负债率/股息率**。

    策略：管线完整接好（run() 已按 qual 类二次过滤 + _attach 附质量字段 + _qual_ok 校验）。
    本函数**不伪造数据**：数据源不可达时返回 (None, 诚实原因)，由 run() 诚实降级（ok:False + reason）。
    环境解封或换源后，只需在此填入真实取数逻辑，前端质量筛选即自动启用。

    returns: (qmap: dict|None, reason: str)
    """
    reason = ("质量指标数据源（ROE / 毛利率 / 资产负债率 / 股息率）在当前运行环境暂不可取："
              "东财 push2 接口被网络策略拦截，财务数据中心表亦不可用，AkShare 无法安装。"
              "取数管线已就绪，环境解封后将自动启用。")
    # TODO(数据源解封后补齐): 走 push2 全市场财务快照，按 code 聚合最新一期 ROE/毛利率/负债率/股息率。
    return None, reason


_DECLINE_LOCK = _threading.Lock()
_DECLINE_MEM: dict = {}
_DECLINE_CACHE = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)),
                               "data", "decline_kcache.json")
_DECLINE_WORKERS = 24  # 2026-10-06 提速：实测 24 并发与 12 失败率一致、未触发腾讯限流，全市场首跑由 ~2 分钟压到 ~1 分钟
_DECLINE_KEEP_BARS = 140
_FAIL_PATH = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)),
                           "data", "decline_fail_cooldown.json")
_FAIL_MEM: dict | None = None   # code -> 最近 K 线抓取完全失败的时间戳（见 _fail_table）
_FAIL_COOLDOWN = 1800     # 失败冷却 30 分钟：期内不重试必败票，到期自动恢复重试


def _fail_table() -> dict:
    """失败冷却表（code -> 失败时间戳），**跨进程持久化**。

    2026-10-01 修 BUG 报告 P0「换手率 Top 冷启动 112s」：这张表原先只在内存，
    服务一重启就被清空，于是 348 只北交所必败票（920xxx）又被重新拉一遍，
    每次冷启动白等 1~2 分钟才出结果。改成落盘后，重启/并发都直接跳过冷却期内的必败票。
    TTL 仍是 30 分钟 —— 冷却到期自动恢复重试，不会把票永久拉黑。
    """
    global _FAIL_MEM
    if _FAIL_MEM is None:
        _FAIL_MEM = {}
        try:
            with open(_FAIL_PATH, encoding="utf-8") as fh:
                d = _json.load(fh)
            if isinstance(d, dict):
                now = _time.time()
                for k, v in d.items():
                    if isinstance(v, (int, float)) and now - float(v) < _FAIL_COOLDOWN:
                        _FAIL_MEM[k] = float(v)
        except Exception:  # noqa: BLE001
            pass
    return _FAIL_MEM


def _fail_flush() -> None:
    """把冷却表写回磁盘（只在一个扫描周期结束时写一次，别每只票都写）。"""
    try:
        tmp = _FAIL_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            _json.dump(_FAIL_MEM or {}, fh, ensure_ascii=False)
        _os.replace(tmp, _FAIL_PATH)
    except Exception:  # noqa: BLE001
        pass

_TX_QT = "https://qt.gtimg.cn/q="
_FLOAT_LOCK = _threading.Lock()
_FLOAT_MEM: dict = {}
_FLOAT_CACHE = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)),
                             "data", "float_shares_cache.json")

# 口径自述（2026-10-01 自查）：本档位名是「技术面换手形态综合评分（0~100）」的分段，
# **不是**风险事件分级，与 watchdesk 的「退市风险 / 立案调查 / 股东减持」那套风险标签无关。
# 改前最低档叫「高风险预警」，字面像风险信号，实则是技术面弱 —— 与「筹码集中度」同型错配。
TIERS = [(85, 5, "积极关注"), (70, 4, "值得持有"), (50, 3, "中性观望"),
         (30, 2, "偏弱回避"), (0, 1, "技术面弱势")]

# 2026-10-05 王总要求（新「入手概率」板块）：档位再拆细 + 描述再清晰。
# 与上面 TIERS **并行存在、互不影响**：
#   TIERS → 既有界面（连续阴跌榜 / 个股诊断抽屉 / 前端 sc-t1..sc-t5 配色）口径，一处不动；
#   GRADE → 新板块的细粒度 7 级，只产出 grade / grade_desc / advice 三个新字段。
# 动 TIERS 会连带改掉既有配色映射（app.js 的 tierCls 写死 1..5）与 decline 排序语义，故不碰。
# 字段：(下限分, 等级, 一句话判据定义, 动作建议)
GRADE = [
    (92, "A+", "强吸筹：低位放量抬升＋量价配合＋多项企稳信号同时成立（≥92 分）",
     "可分批建仓；跌破入市价 −6% 离场"),
    (85, "A",  "积极关注：低位温和放量·吸筹特征（85~91 分）",
     "可小仓试探；回踩不破前低再补"),
    (75, "B+", "值得持有：低位缩量企稳 / 温和放量但仅单项信号成立（75~84 分）",
     "持有观察，等放量突破再加仓"),
    (62, "B",  "中性偏多：位置中下沿＋换手中性，方向未定（62~74 分）",
     "轻仓或等信号更明确"),
    (45, "C+", "中性观望：量价平淡，五维得分均衡（45~61 分）",
     "观望为主，暂不介入"),
    (30, "C",  "偏弱回避：阴跌未止 / 高位滞涨，换手或位置已进警戒区（30~44 分）",
     "回避，等企稳信号出现"),
    (0,  "D",  "技术面弱势：跌势未止、放量下跌或换手数据缺失（<30 分）",
     "不参与（技术面弱势，非风险事件）"),
]
# 档位 → CSS 类后缀（前端把它接成 .gr-a-plus / .gr-a / … ；C+ 与 C 同级配色，
# 只靠文字区分，避免 7 级要 7 种颜色把表格染色块搞脏）
GRADE_CLS = {"A+": "a-plus", "A": "a", "B+": "b-plus", "B": "b",
             "C+": "c-plus", "C": "c", "D": "d"}


def grade_of(score) -> tuple:
    """按分数取细粒度档位，返回 (grade, grade_desc, advice)。分数为 None 时给「—」档。"""
    try:
        s = float(score)
    except (TypeError, ValueError):
        return ("—", "暂无评分（日 K 或换手数据不足）", "先等数据补齐再判")
    for th, g, desc, adv in GRADE:
        if s >= th:
            return (g, desc, adv)
    return (GRADE[-1][1], GRADE[-1][2], GRADE[-1][3])

BASIS = [
    "位置分位=现价在近N日收盘高低区间的相对位置（低位≤35%、高位≥70%）",
    "换手温度刻度按流通市值分档：≥500亿用 1/3/8%，≥100亿用 2/5/12%，其余用 3/8/15%（行业差异：蓝筹日常换手远低于小盘科技股）",
    "换手率=K线成交量×标定系数；系数由腾讯官方换手率÷官方成交量实时标定（同源同快照，自动消除K线量单位差异与流通市值口径偏差），官方值缺失时按流通股本估算兜底（7天缓存）",
    "四大场景源自换手率实战框架：低位高换手=吸筹 / 低位低换手=洗盘（须低点不创新低，否则阴跌回避）/ 高位低换手=锁仓 / 高位高换手=出货",
    "持续性与量价配合看连续多日变化，单日冲高（近3日均换手>前10日3.5倍）按疑似对倒扣分",
    "本评分为技术面规则打分（0~100），回答「当前换手形态像哪种主力行为」，不预测涨跌、不构成投资建议",
]


def _last_trade_date_local(now=None) -> str:
    """本地推算最近已收盘交易日（15:05 前算上一交易日；跳过周末，不识别节假日——
    误差只导致多抓一次，无害）。"""
    t = now or _dt.datetime.now()
    cutoff = t.replace(hour=15, minute=5, second=0, microsecond=0)
    d = t if t > cutoff else t - _dt.timedelta(days=1)
    while d.weekday() >= 5:
        d -= _dt.timedelta(days=1)
    return d.strftime("%Y-%m-%d")


def _decline_cache_load() -> dict:
    try:
        with open(_DECLINE_CACHE, "r", encoding="utf-8") as f:
            return _json.load(f)
    except Exception:  # noqa: BLE001
        return {}


def _decline_cache_save(data: dict) -> None:
    try:
        _os.makedirs(_os.path.dirname(_DECLINE_CACHE), exist_ok=True)
        tmp = _DECLINE_CACHE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            _json.dump(data, f, ensure_ascii=False)
        _os.replace(tmp, _DECLINE_CACHE)
    except Exception:  # noqa: BLE001
        pass


_TX_KLINE_ALT = "https://proxy.finance.qq.com/ifzqgtimg/appstock/app/newfqkline/get"


def _fetch_tencent_full(code: str, bars: int) -> list:
    """腾讯日 K（前复权）全字段：[(date, close, high, low, vol手)] 升序。失败返回 []。

    2026-09-30：主源 web.ifzq.gtimg.cn 本机时通时断（501），加 proxy.finance.qq.com
    同格式兜底（实测 2026-09-30 主源 501 时兜底正常出 141 根）。
    """
    sym = _prefix(code) + code
    for base in (TX_KLINE, _TX_KLINE_ALT):
        try:
            url = "%s?param=%s,day,,,%d,qfq" % (base, sym, bars)
            d = _json.loads(_http_text(url, "https://gu.qq.com/"))
            node = (d.get("data") or {}).get(sym) or {}
            arr = node.get("qfqday") or node.get("day") or []
            if not arr:
                continue
            out = []
            for row in arr:
                try:
                    out.append((str(row[0])[:10], float(row[2]), float(row[3]),
                                float(row[4]), float(row[5])))
                except Exception:  # noqa: BLE001
                    continue
            if out:
                return out
        except Exception:  # noqa: BLE001
            continue
    return []


def _bars_fresh(ent: dict, target: str, days: int) -> bool:
    """新鲜 = updated 是目标交易日 + 根数够 + 末 3 根都带成交量（换手率算得出来）。"""
    bars = ent.get("bars") or []
    if ent.get("updated") != target or len(bars) < days + 1:
        return False
    return all(len(b) >= 5 and b[4] is not None for b in bars[-3:])


def _load_bars_map(pool: dict, days: int) -> tuple:
    """返回 (bars_map{code: [(date, close, high, low, vol)...]}, meta{...})。

    缓存新鲜判定见 _bars_fresh。腾讯全字段失败时退回 fetch_daily（只收盘价，
    换手率缺失、条目仍进表但不新鲜——下次扫描自动重试腾讯）。
    抓取完全失败的代码不写缓存，只记进 meta.failures；但记入 _FAIL_MEM 冷却表
    （30 分钟内不重试，防止全市场扫描时 348 只北交所等必败票每次拖慢 1~2 分钟，
    冷却到期后照常自动重试——不违反"失败不落缓存"纪律）。
    """
    target = _last_trade_date_local()
    disk = dict(_DECLINE_MEM) if _DECLINE_MEM else _decline_cache_load()
    bars_map, need = {}, []
    for code in pool:
        ent = disk.get(code)
        if ent and _bars_fresh(ent, target, days):
            bars_map[code] = [(str(b[0]), float(b[1]),
                               float(b[2]) if len(b) > 2 and b[2] is not None else None,
                               float(b[3]) if len(b) > 3 and b[3] is not None else None,
                               float(b[4]) if len(b) > 4 and b[4] is not None else None)
                              for b in ent["bars"][-_DECLINE_KEEP_BARS:]]
        elif _time.time() - _fail_table().get(code, 0) > _FAIL_COOLDOWN:
            need.append(code)
        # 冷却期内的失败票直接跳过（既不重试也不算成功）
    n_cached = len(pool) - len(need)
    n_fetched, failures = 0, []

    if need:
        def _one(c):
            try:
                bars = _fetch_tencent_full(c, _DECLINE_KEEP_BARS)
                if bars:
                    return c, bars, True
            except Exception:  # noqa: BLE001
                pass
            try:
                closes = fetch_daily(c, _DECLINE_KEEP_BARS, retry=0) or []
                return c, [(d, v, None, None, None) for d, v in closes], False
            except Exception:  # noqa: BLE001
                return c, [], False

        with _TPE(max_workers=_DECLINE_WORKERS) as ex:
            for c, bars, full in ex.map(_one, need):
                if bars:
                    norm = [(str(b[0]), float(b[1]), b[2], b[3], b[4]) for b in bars]
                    bars_map[c] = norm
                    disk[c] = {"updated": target, "bars": norm, "full": bool(full)}
                    _fail_table().pop(c, None)
                    _fail_flush()      # 扫描周期结束统一落盘一次（清掉恢复成功的票）
                    n_fetched += 1
                else:
                    _fail_table()[c] = _time.time()
                    _fail_flush()
                    failures.append(c)
        _fail_flush()
        _DECLINE_MEM.clear()
        _DECLINE_MEM.update(disk)
        _decline_cache_save(disk)
    meta = {"target": target, "n_pool": len(pool), "n_cached": n_cached,
            "n_fetched": n_fetched, "n_fail": len(failures), "failures": failures[:20]}
    return bars_map, meta


def _float_cache_load() -> dict:
    try:
        with open(_FLOAT_CACHE, "r", encoding="utf-8") as f:
            return _json.load(f)
    except Exception:  # noqa: BLE001
        return {}


def _float_cache_save(data: dict) -> None:
    try:
        _os.makedirs(_os.path.dirname(_FLOAT_CACHE), exist_ok=True)
        tmp = _FLOAT_CACHE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            _json.dump(data, f, ensure_ascii=False)
        _os.replace(tmp, _FLOAT_CACHE)
    except Exception:  # noqa: BLE001
        pass


def _float_map(codes: list, max_age_days: int = 7) -> dict:
    """{code: (流通股本股数, 流通市值亿, 官方换手率%, 官方成交量)}。

    腾讯实时行情批量（60/次），磁盘缓存 7 天；取不到的不写缓存（下次重试），
    调用方按缺失降级。
    2026-09-30 v2 标定：增采官方换手率 f38 与官方成交量 f6——二者同源同快照，
    fs_eff = f6×单位股数×100/f38 可反推真实流通股本，自动修正 f44 流通市值
    口径偏差（实测托伦斯 301583 f44 偏大约 10 倍）。旧版缓存条目缺 vol_rt
    视为不新鲜，自动重采。
    """
    now = _dt.datetime.now()
    disk = dict(_FLOAT_MEM) if _FLOAT_MEM else _float_cache_load()
    out, need = {}, []
    for c in codes:
        ent = disk.get(c)
        if ent and ent.get("fs") and "vol_rt" in ent and ent.get("updated") and \
                (now - _dt.datetime.strptime(str(ent["updated"])[:10], "%Y-%m-%d")).days < max_age_days:
            out[c] = (float(ent["fs"]), float(ent.get("fmv") or 0),
                      float(ent.get("to_rt") or 0), float(ent.get("vol_rt") or 0))
        else:
            need.append(c)
    if need:
        for i in range(0, len(need), 60):
            batch = need[i:i + 60]
            syms = ",".join(_prefix(c) + c for c in batch)
            try:
                txt = _http_text(_TX_QT + syms, "https://gu.qq.com/", timeout=8)
            except Exception:  # noqa: BLE001
                continue  # 失败不写缓存，下次重试
            for m in _re.finditer(r'v_(?:sh|sz|bj)(\d{6})="([^"]*)"', txt):
                code, body = m.group(1), m.group(2)
                f = body.split("~")
                try:
                    price = float(f[3])
                    fmv = float(f[44])   # 流通市值（亿）
                    vol_rt = float(f[6])   # 官方成交量（K线同单位：主板/创业板=手，科创板=股）
                    to_rt = float(f[38])   # 官方换手率 %
                except Exception:  # noqa: BLE001
                    continue
                if price <= 0 or fmv <= 0:
                    continue
                fs = fmv * 1e8 / price
                out[code] = (fs, fmv, to_rt, vol_rt)
                disk[code] = {"fs": fs, "fmv": fmv, "to_rt": to_rt,
                              "vol_rt": vol_rt, "updated": now.strftime("%Y-%m-%d")}
            _time.sleep(0.15)
        _FLOAT_MEM.clear()
        _FLOAT_MEM.update(disk)
        _float_cache_save(disk)
    return out


_SPU_FALLBACK_PREFIX = ("68",)  # 科创板 K 线量单位=股；其余按手（仅兜底路径用，标定路径单位自动抵消）


def _to_scale_of(code: str, ent: tuple) -> float | None:
    """每 1 单位 K 线成交量对应的换手率（%），供 _turnover_eval 把 vol 直接换算成 %。

    标定路径：官方换手率/官方成交量（同源同快照），单位与流通股本口径误差自动抵消；
    兜底路径：官方换手缺失时按「手/股 × 100 ÷ 流通股本」估。
    """
    fs, _fmv, to_rt, vol_rt = ent
    if to_rt > 0 and vol_rt > 0:
        return to_rt / vol_rt
    if fs and fs > 0:
        spu = 1.0 if str(code).startswith(_SPU_FALLBACK_PREFIX) else 100.0
        return spu * 100.0 / fs
    return None


def _turnover_eval(bars: list, fs: float, fmv: float, days: int,
                   to_scale: float | None = None) -> dict:
    """换手率策略五维评分（0~100）。bars 至少 days+1 根 (date,close,high,low,vol)。

    to_scale=每 1 单位 K 线量对应的换手率%（_to_scale_of 标定）；缺失时退回
    v×100÷流通股本 旧口径——该旧口径只对科创板（K线量=股）成立，主板/创业板
    会偏小 100 倍（2026-09-30 实证修复，勿删 to_scale 参数）。
    """
    w = bars[-(days + 1):]
    closes = [b[1] for b in w]
    highs = [(b[2] if b[2] is not None else b[1]) for b in w]
    lows = [(b[3] if b[3] is not None else b[1]) for b in w]
    vols = [b[4] for b in w]
    if to_scale and to_scale > 0:
        tos = [(v * to_scale) for v in vols]
    else:
        tos = [(v * 100.0 / fs) for v in vols] if (fs and fs > 0) else None
    chgs = [closes[i] / closes[i - 1] - 1.0 for i in range(1, len(closes))]

    # ① 位置分位（近 days 日收盘高低区间）
    hi, lo = max(closes), min(closes)
    pos = (closes[-1] - lo) / (hi - lo) if hi > lo else 0.5
    s_pos = int(round(30 * (1.0 - pos)))

    # 温度刻度（按流通市值分档，蓝筹基线低）
    if fmv and fmv >= 500:
        t1, t2, t3 = 1.0, 3.0, 8.0
    elif fmv and fmv >= 100:
        t1, t2, t3 = 2.0, 5.0, 12.0
    else:
        t1, t2, t3 = 3.0, 8.0, 15.0

    # ② 场景 + 温度
    to_today = tos[-1] if tos else None
    to5 = (sum(tos[-5:]) / 5.0) if tos else None
    rising = bool(tos and len(tos) >= 15 and to5 > 1.3 * (sum(tos[-15:-5]) / 10.0))
    new_low_hold = bool(len(lows) >= 20 and min(lows[-5:]) >= min(lows[:-5][-15:]))
    low_pos, high_pos = pos <= 0.35, pos >= 0.70
    if to_today is None:
        scenario, hint, s_scene = "未知", "换手率数据缺失（未取到流通股本），仅按走势评分", 12
    elif low_pos and rising and to5 >= t1 and (closes[-1] / closes[-6] - 1.0) < 0.15:
        scenario, hint, s_scene = "吸筹", "低位温和放量·吸筹特征", 30
    elif low_pos and to5 < t1:
        if new_low_hold:
            scenario, hint, s_scene = "洗盘", "低位缩量震荡·低点不创新低（洗盘特征）", 24
        else:
            scenario, hint, s_scene = "阴跌", "低位缩量仍创新低·阴跌回避", 8
    elif high_pos and to_today >= t2:
        scenario = "出货"
        hint = "高位高换手·警惕主力兑现出货" + ("（换手已破极端区 %.0f%%）" % t3 if to_today >= t3 else "")
        s_scene = 0 if to_today >= t3 else 4
    elif high_pos:
        scenario, hint, s_scene = "锁仓", "高位低换手·锁仓拉升（题材退潮防砸盘）", 18
    else:
        scenario, hint, s_scene = "中性", "换手中性·方向待选", 12

    # ③ 换手持续性（近3日均 vs 前10日均）
    if tos:
        r3 = sum(tos[-3:]) / 3.0
        base = tos[:-3]
        b10 = (sum(base[-10:]) / 10.0) if len(base) >= 10 else \
              (sum(base) / len(base) if base else 0.0)
        ratio = (r3 / b10) if b10 > 0 else 1.0
        if ratio >= 3.5:
            s_persist = 4    # 单日/短端异常脉冲，疑似对倒
        elif ratio >= 2.0:
            s_persist = 13   # 显著放量
        elif ratio >= 1.1:
            s_persist = 15   # 温和抬升，最健康
        elif ratio >= 0.6:
            s_persist = 10   # 平稳
        else:
            s_persist = 8    # 极度缩量
    else:
        s_persist = 7

    # ④ 量价配合
    s_vp = 0
    if tos and chgs:
        dn_to = [tos[i + 1] for i in range(len(chgs)) if chgs[i] < 0]
        up_to = [tos[i + 1] for i in range(len(chgs)) if chgs[i] >= 0]
        if dn_to and up_to:
            davg = sum(dn_to) / len(dn_to)
            uavg = sum(up_to) / len(up_to)
            if davg < uavg * 0.8:
                s_vp += 8        # 下跌缩量=抛压轻
            elif davg > uavg * 1.3:
                s_vp += 0        # 放量下跌
            else:
                s_vp += 4
    if new_low_hold:
        s_vp += 4                # 低点不创新低
    if closes[-1]:
        hb = (highs[-1] - closes[-1]) / closes[-1]
        if hb > 0.025 and chgs[-1] < 0:
            s_vp -= 3            # 长上影 + 收跌
    if len(lows) >= 11 and lows[-1] > min(lows[:-1][-10:]) and chgs[-1] >= 0:
        s_vp += 3                # 当日未破前10日低点且收红
    s_vp = max(0, min(15, s_vp))

    # ⑤ 企稳信号
    s_st = 0
    if chgs and chgs[-1] >= 0:
        s_st += 4
    a5 = sum(chgs[-5:]) / 5.0 if len(chgs) >= 5 else (sum(chgs) / len(chgs) if chgs else 0)
    a10 = sum(chgs[-10:]) / 10.0 if len(chgs) >= 10 else a5
    if a5 > a10:
        s_st += 3                # 跌势收窄
    if len(closes) >= 6 and closes[-1] > closes[-6]:
        s_st += 3
    s_st = max(0, min(10, s_st))

    score = max(0, min(100, s_pos + s_scene + s_persist + s_vp + s_st))
    tier, tlabel = TIERS[0][1], TIERS[0][2]
    for th, tv, lbl in TIERS:
        if score >= th:
            tier, tlabel = tv, lbl
            break
    # 细粒度档位（2026-10-05 新增，与 tier 并行、互不影响）
    gname, gdesc, gadv = grade_of(score)
    return {
        "to_today": round(to_today, 2) if to_today is not None else None,
        "to_avg5": round(to5, 2) if to5 is not None else None,
        "pos_pct": round(pos * 100.0, 1),
        "scenario": scenario, "hint": hint,
        "score": score, "tier": tier, "tier_label": tlabel,
        "grade": gname, "grade_desc": gdesc, "advice": gadv,
        "grade_cls": GRADE_CLS.get(gname, ""),
        "dim": {"pos": s_pos, "scene": s_scene, "persist": s_persist,
                "vp": s_vp, "stab": s_st},
    }


def decline_scan(pool: dict, days: int = 30, down: int = 25, limit: int = 80,
                 sort: str = "down") -> dict:
    """近 days 个交易日中下跌天数 >= down 的股票（活跃池内），默认按下跌天数降序。

    下跌 = 当日收盘 < 前一日收盘（前复权）。K 线不足 days+1 根的跳过（次新/长期停牌）。
    sort="score" 时按「入手值得率」降序。每行附换手率策略评分（见文件头口径2）。
    同一时刻只跑一次扫描（进程锁），并发调用排队。
    """
    days = max(5, min(int(days or 30), 120))
    down = max(1, min(int(down or 25), days))
    limit = max(1, min(int(limit or 80), 200))
    sort = "score" if str(sort or "").lower() == "score" else "down"
    pool = {str(c)[:6]: (n or "") for c, n in (pool or {}).items()
            if str(c).isdigit() and len(str(c)) == 6}
    if not pool:
        return {"ok": False, "days": days, "down": down, "rows": [],
                "reason": "扫描池为空（智能选股 / 关注池 / 龙虎榜均无数据）"}
    with _DECLINE_LOCK:
        bars_map, meta = _load_bars_map(pool, days)
        rows, skipped = [], 0
        for code, closes in bars_map.items():
            w = closes[-(days + 1):]
            if len(w) < days + 1:
                skipped += 1
                continue
            chgs = [w[i][1] / w[i - 1][1] - 1.0 for i in range(1, len(w))]
            downs = [c for c in chgs if c < 0]
            if len(downs) < down:
                continue
            streak = best = 0
            for c in chgs:
                streak = streak + 1 if c < 0 else 0
                best = max(best, streak)
            rows.append({
                "code": code, "name": pool.get(code, ""),
                "price": w[-1][1],
                "last_pct": round(chgs[-1] * 100.0, 2),
                "down_days": len(downs), "days": days,
                "cum_pct": round((w[-1][1] / w[0][1] - 1.0) * 100.0, 2),
                "avg_dn_pct": round((sum(downs) / len(downs)) * 100.0, 2),
                "max_streak": best,
            })
        # 换手率策略评分：只对入选行取流通股本（腾讯批量，7 天缓存）
        if rows:
            fmap = _float_map([r["code"] for r in rows])
            for r in rows:
                ent = fmap.get(r["code"]) or (None, None, 0.0, 0.0)
                fs, fmv = ent[0], ent[1]
                try:
                    ev = _turnover_eval(bars_map[r["code"]], fs, fmv, days,
                                        to_scale=_to_scale_of(r["code"], ent))
                except Exception:  # noqa: BLE001
                    ev = None
                if ev:
                    r.update(ev)
                    r["fmv_yi"] = round(fmv, 1) if fmv else None
                else:
                    r.update({"to_today": None, "to_avg5": None, "pos_pct": None,
                              "scenario": "未知", "hint": "评分数据不足", "score": None,
                              "tier": 0, "tier_label": "—", "dim": None, "fmv_yi": None,
                              "grade": "—", "grade_desc": "评分数据不足（日 K 或换手缺失）",
                              "advice": "先等数据补齐再判", "grade_cls": ""})
        if sort == "score":
            rows.sort(key=lambda r: (-(r.get("score") if r.get("score") is not None else -1),
                                     -r["down_days"]))
        else:
            rows.sort(key=lambda r: (-r["down_days"], r["cum_pct"]))
        rows = rows[:limit]
        return {"ok": True, "days": days, "down": down, "limit": limit, "sort": sort,
                "trade_date": meta["target"], "pool_size": len(pool),
                "scanned": len(bars_map), "skipped_bars": skipped,
                "refresh": {"fetched": meta["n_fetched"], "cached": meta["n_cached"],
                            "fail": meta["n_fail"], "failures": meta["failures"]},
                "tiers": [{"min": th, "tier": tv, "label": lbl} for th, tv, lbl in TIERS],
                "basis": BASIS,
                "rows": rows, "reason": None}


def turnover_single(code: str, days: int = 30) -> dict:
    """单票换手率实战评分（个股详情抽屉用，2026-09-30 王总需求）。

    复用阴跌扫描的 K 线磁盘缓存与流通股本缓存，任意 6 位 A 股代码可用；
    上市不足 days+1 根 K 线的次新股按实际根数评分（partial=True 如实标注）。
    """
    code = str(code or "").strip()
    if not (code.isdigit() and len(code) == 6):
        return {"ok": False, "reason": "代码格式不正确（需 6 位 A 股数字代码）"}
    days = max(5, min(int(days or 30), 120))
    bars_map, meta = _load_bars_map({code: ""}, days)
    bars = bars_map.get(code) or []
    if not bars or len(bars) < 6:
        return {"ok": False, "code": code,
                "reason": "日 K 获取失败或根数过少（行情源暂不可用），请稍后重试"}
    ent = _float_map([code]).get(code) or (None, None, 0.0, 0.0)
    fs, fmv = ent[0], ent[1]
    try:
        ev = _turnover_eval(bars, fs, fmv, days, to_scale=_to_scale_of(code, ent))
    except Exception:  # noqa: BLE001
        ev = None
    if not ev:
        return {"ok": False, "code": code, "reason": "评分计算失败"}
    ev.update({
        "ok": True, "code": code, "days": days, "bars_used": len(bars),
        "partial": len(bars) < days + 1,
        "trade_date": meta.get("target"),
        "fmv_yi": round(fmv, 1) if fmv else None,
        "basis": BASIS,
    })
    return ev


# ======================================================================
# 个股 F10：公司简介 + 十大流通股东(机构/自然人分类) + 股东户数
# （2026-09-30 晚，王总需求：每只股票简介 + 机构/散户持股结构）。
# 数据源：东方财富 datacenter（emapi.datacenter 封装）。
#   RPT_F10_BASIC_ORGINFO  公司概况（全称/板块/行业/主营业务/法定代表人…）
#   RPT_F10_EH_FREEHOLDERS 十大流通股东（IS_HOLDORG 是否机构、FREE_HOLDNUM_RATIO 占流通比）
#   RPT_F10_EH_HOLDERNUM   股东户数（户数/环比/人均流通股/集中度/前十大流通合计）
# 口径提醒：机构占比含「产业资本/一般法人」（东财把 IS_HOLDORG=1 全算机构类）；
#           散户占比 = 100% − 前十大流通合计，系估算值（季报滞后，基金一/三季报不全）。
_F10_CACHE = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)),
                           "data", "f10_cache.json")


def _f10_cache_load() -> dict:
    try:
        with open(_F10_CACHE, "r", encoding="utf-8") as f:
            return _json.load(f)
    except Exception:  # noqa: BLE001
        return {}


def _f10_cache_save(data: dict) -> None:
    try:
        _os.makedirs(_os.path.dirname(_F10_CACHE), exist_ok=True)
        tmp = _F10_CACHE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            _json.dump(data, f, ensure_ascii=False)
        _os.replace(tmp, _F10_CACHE)
    except Exception:  # noqa: BLE001
        pass


def _f10_classify(h: dict) -> str:
    """东财 IS_HOLDORG=1 视为机构类（含基金/社保/保险/券商/信托/QFII/一般法人/国家队/外资）；
    0 视为自然人（个人大股东 / 牛散）。"""
    return "org" if str(h.get("IS_HOLDORG") or "0") == "1" else "person"


def stock_f10(code: str, max_age_days: int = 90) -> dict:
    """个股 F10 聚合：公司简介 + 持股结构（机构/自然人/散户估算）+ 股东户数。

    返回 {ok, code, cached, profile, holders[], holder_summary, holdernum, report_date, reason}。
    """
    code = str(code or "").strip()
    if not (code.isdigit() and len(code) == 6):
        return {"ok": False, "reason": "代码格式不正确（需 6 位 A 股数字代码）"}
    cache = _f10_cache_load()
    now = _time.time()
    if code in cache and now - cache[code].get("ts", 0) < max_age_days * 86400:
        return {"ok": True, "code": code, "cached": True, **cache[code]["data"]}
    flt = '(SECURITY_CODE="%s")' % code
    org_r = emapi.datacenter("RPT_F10_BASIC_ORGINFO", columns="ALL", flt=flt, page_size=1)
    fh_r = emapi.datacenter("RPT_F10_EH_FREEHOLDERS", columns="ALL", flt=flt,
                            page_size=80, sort_columns="END_DATE,HOLD_NUM", sort_types="-1,-1")
    hn_r = emapi.datacenter("RPT_F10_EH_HOLDERNUM", columns="ALL", flt=flt,
                            page_size=4, sort_columns="END_DATE", sort_types="-1")
    # —— 公司简介 ——
    profile = {}
    if org_r["ok"] and org_r["rows"]:
        o = org_r["rows"][0]
        profile = {
            "name": o.get("SECURITY_NAME_ABBR"),
            "org_name": o.get("ORG_NAME"),
            "market": o.get("SECURITY_TYPE"),
            "industry": o.get("EM2016") or o.get("INDUSTRYCSRC1") or "",
            "main_business": (o.get("MAIN_BUSINESS") or "").strip(),
            "founded": (str(o.get("FOUND_DATE") or "")[:10]) or None,
            "listed": (str(o.get("LISTING_DATE") or "")[:10]) or None,
            "reg_capital": o.get("REG_CAPITAL"),
            "emp_num": o.get("EMP_NUM"),
            "chairman": o.get("CHAIRMAN"),
            "address": o.get("ADDRESS"),
        }
    # —— 十大流通股东分类 ——
    # 注意：该报表同一股东在多个报告期会重复出现，先只保留最新一期（END_DATE 最大）的一批。
    fh_rows = fh_r.get("rows") or []
    if fh_rows:
        _latest = max(str(r.get("END_DATE") or "") for r in fh_rows)
        fh_rows = [r for r in fh_rows if str(r.get("END_DATE") or "") == _latest]
        try:
            fh_rows.sort(key=lambda r: float(r.get("HOLD_NUM") or 0), reverse=True)
        except (TypeError, ValueError):
            pass
    holders, org_pct, person_pct, top10_pct = [], 0.0, 0.0, 0.0
    if fh_r["ok"]:
        for h in fh_rows[:12]:
            try:
                ratio = float(h.get("FREE_HOLDNUM_RATIO") or 0)
            except (TypeError, ValueError):
                ratio = 0.0
            cat = _f10_classify(h)
            if cat == "org":
                org_pct += ratio
            else:
                person_pct += ratio
            top10_pct += ratio
            _chg = h.get("HOLDNUM_CHANGE_NAME") or h.get("HOLD_NUM_CHANGE") or "不变"
            _chg = {"增加": "增持", "减少": "减持"}.get(_chg, "不变")
            holders.append({
                "rank": h.get("HOLDER_RANK"),
                "name": h.get("HOLDER_NAME"),
                "cat": cat,
                "type": h.get("HOLDER_TYPE") or h.get("HOLDER_STATE") or ("机构" if cat == "org" else "自然人"),
                "ratio": round(ratio, 2),
                "change": _chg,
            })
    # —— 股东户数（散户集中度代理）——
    hn = {}
    if hn_r["ok"] and hn_r["rows"]:
        r = hn_r["rows"][0]
        hn = {
            "total": r.get("HOLDER_TOTAL_NUM"),
            "qoq": r.get("TOTAL_NUM_RATIO"),
            "avg_free_shares": r.get("AVG_FREE_SHARES"),
            "focus": r.get("HOLD_FOCUS"),
            "top10_free_pct": r.get("FREEHOLD_RATIO_TOTAL"),
            "end_date": (str(r.get("END_DATE") or "")[:10]),
        }
    retail_pct = round(max(0.0, 100.0 - (top10_pct or 0)), 2)
    data = {
        "profile": profile,
        "holders": holders,
        "holder_summary": {
            "org_pct": round(org_pct, 2),
            "person_pct": round(person_pct, 2),
            "top10_free_pct": round(top10_pct, 2),
            "retail_pct": retail_pct,
        },
        "holdernum": hn,
        "report_date": hn.get("end_date") or profile.get("listed") or None,
    }
    cache[code] = {"ts": now, "data": data}
    _f10_cache_save(cache)
    return {"ok": True, "code": code, "cached": False, **data}


_MARKET_POOL_CACHE = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)),
                                   "data", "market_pool_cache.json")
_SINA_HS_A = ("https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"
              "Market_Center.getHQNodeData")


def _market_pool() -> dict:
    """全 A 股清单（新浪 hs_a 节点，总市值降序翻页，磁盘缓存当日）：{code: name}。

    失败返回 {}（不落缓存，下次重试）。约 5600 只、56 页。
    """
    today = _dt.date.today().strftime("%Y-%m-%d")
    try:
        with open(_MARKET_POOL_CACHE, "r", encoding="utf-8") as f:
            ent = _json.load(f)
        if ent.get("day") == today and ent.get("pool"):
            return ent["pool"]
    except Exception:  # noqa: BLE001
        pass
    pool, seen = {}, set()
    for page in range(1, 90):
        url = ("%s?page=%d&num=100&sort=mktcap&asc=0&node=hs_a&symbol=&_s_r_a=page&_=%d"
               % (_SINA_HS_A, page, int(_time.time() * 1000)))
        try:
            txt = _http_text(url, "https://finance.sina.com.cn/", timeout=8)
        except Exception:  # noqa: BLE001
            txt = ""
        got = 0
        if txt:
            try:
                arr = _json.loads(txt.strip().lstrip("\ufeff"))
            except Exception:  # noqa: BLE001
                arr = []
            for x in (arr if isinstance(arr, list) else []):
                if not isinstance(x, dict):
                    continue
                c = str(x.get("code") or "").strip()
                n = str(x.get("name") or "").strip()
                if c.isdigit() and len(c) == 6 and c not in seen:
                    seen.add(c)
                    pool[c] = n
                    got += 1
        if got < 100:
            break
        _time.sleep(0.2)
    if len(pool) < 1000:
        return {}
    try:
        _os.makedirs(_os.path.dirname(_MARKET_POOL_CACHE), exist_ok=True)
        with open(_MARKET_POOL_CACHE, "w", encoding="utf-8") as f:
            _json.dump({"day": today, "pool": pool}, f, ensure_ascii=False)
    except Exception:  # noqa: BLE001
        pass
    return pool


def turnover_top(days: int = 30, limit: int = 30) -> dict:
    """全市场换手率实战 Top N：全 A 股按「入手值得率」降序（2026-09-30 王总需求）。

    首跑需逐只补 K 线（约 5500 只，数分钟）；K 线/流通股本/标定系数全走磁盘缓存，
    当天再跑秒出。K 线不足或成交量缺失的票跳过（不硬评分）。
    """
    days = max(5, min(int(days or 30), 120))
    limit = max(10, min(int(limit or 30), 100))
    pool = _market_pool()
    if not pool:
        return {"ok": False, "mode": "top", "days": days, "rows": [],
                "reason": "全市场清单获取失败（新浪 hs_a 暂不可用），请稍后重试"}
    bars_map, meta = _load_bars_map(pool, days)
    fmap = _float_map(list(bars_map.keys())) if bars_map else {}
    buy_map = rate_buy_map()      # 锚点表很小，一次全捞，行内「跟踪」列要用
    rows, skipped = [], 0
    for code, bars in bars_map.items():
        if len(bars) < days + 1 or not bars[-1][4]:
            skipped += 1
            continue
        ent = fmap.get(code) or (None, None, 0.0, 0.0)
        try:
            ev = _turnover_eval(bars, ent[0], ent[1], days, to_scale=_to_scale_of(code, ent))
        except Exception:  # noqa: BLE001
            ev = None
        if not ev or ev.get("score") is None:
            skipped += 1
            continue
        closes = [b[1] for b in bars[-(days + 1):]]
        r = {"code": code, "name": pool.get(code, ""),
             "price": closes[-1],
             "last_pct": (round((closes[-1] / closes[-2] - 1.0) * 100.0, 2)
                          if len(closes) >= 2 and closes[-2] else None),
             "bars_used": len(bars), "partial": len(bars) < days + 1}
        r.update(ev)
        fmv = ent[1]
        r["fmv_yi"] = round(fmv, 1) if fmv else None
        # 「最值得入手价」：按这只票自己的振幅/支撑算，前端行内直接显示并可手填覆盖
        ib = ideal_buy_point(bars, r.get("price"))
        r["ideal_buy"], r["ideal_gap"], r["ideal_dist"] = ib
        rows.append(r)
    rows.sort(key=lambda x: (-(x.get("score") or 0), -(x.get("to_today") or 0)))
    rows = rows[:limit]
    return {"ok": True, "mode": "top", "days": days, "limit": limit,
            "trade_date": meta.get("target"), "pool_size": len(pool),
            "buy_map": buy_map,          # 锚点（首次标注锁死）→ 前端行内「跟踪」列直接用
            "scanned": len(bars_map), "skipped_bars": skipped,
            "refresh": {"fetched": meta.get("n_fetched"), "cached": meta.get("n_cached"),
                        "fail": meta.get("n_fail"), "failures": (meta.get("failures") or [])[:20]},
            "tiers": [{"min": th, "tier": tv, "label": lbl} for th, tv, lbl in TIERS],
            "grades": [{"min": th, "grade": g, "cls": GRADE_CLS.get(g, ""),
                        "desc": d, "advice": a} for th, g, d, a in GRADE],
            "basis": BASIS, "rows": rows, "reason": None}


# ======================================================================
# 入手概率 · 时间轴（2026-10-05 王总需求：单独立板块 + 时间轴看「建议入市价 → 后来收盘价」）
#
# 思路：**先把「今天这版榜」落库存档**，过几天再拿实时日线把每个 T+1/3/5/10 的收盘价
# 补回去算比值。没落过库的既往时点**无法回溯**（当时没这份数据），只能从第一次归档起攒，
# 这也是本表天然的记录起点。
#
# 三条口径纪律（写库前先定死，避免以后歪嘴说数据不对）：
#   ① 归档日 = 自然日（你点归档那天），不是行情数据日；表里两列分开存（snap_date / trade_date），
#      否则"16 号归档、看的是 15 号盘"会算错天数。
#   ② 基准价 = 归档当天那笔快照的 price（当时看到的价格）；T+N 的收盘价从日线上往后数 N 根。
#      不足 N 根的显式记 pending，**不许填 0**（0 会被当成"没涨没跌"，是假信号）。
#   ③ 同一天重复归档按「同日覆盖」（UNIQUE(snap_date,code) + INSERT OR REPLACE），
#      不堆重复行；换参数重跑后，以最新一版为准。
# ======================================================================
import sqlite3 as _sqlite3

try:                                     # 尊重 VIEWER_DB 隔离变量（与 viewer/db.py 同源）
    from viewer.db import DB_PATH as _VIEWER_DB
except Exception:                        # 单跑 screener.py 时退回路经推导
    _VIEWER_DB = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)),
                               "viewer", "app.db")

_RATE_TTL = (1, 3, 5, 10)               # 时间轴刻度：T+1 / T+3 / T+5 / T+10（交易日）
_RATE_TBL = "rate_snap"
_RATE_DDL = """CREATE TABLE IF NOT EXISTS rate_snap (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  snap_date TEXT NOT NULL,          -- 归档自然日（做判断那天）
  trade_date TEXT DEFAULT '',       -- 表里那份行情的数据日
  code TEXT NOT NULL, name TEXT DEFAULT '', rank INTEGER DEFAULT 0,
  price REAL,                       -- 归档时的入市价（基准）
  score REAL, tier INTEGER, tier_label TEXT,
  grade TEXT, grade_cls TEXT, grade_desc TEXT, advice TEXT,
  pos_pct REAL, to_today REAL, to_avg5 REAL, scenario TEXT, hint TEXT,
  src TEXT DEFAULT 'auto', created_at TEXT,
  ideal REAL DEFAULT NULL,            -- 归档那天的「最值得入手价」（行内跟踪的比对基准之一）
  UNIQUE(snap_date, code)
)"""
_RATE_IDX = "CREATE INDEX IF NOT EXISTS idx_rate_snap_date ON rate_snap(snap_date)"

# ===== 「最值得入手价」+ 首次标注锚点（2026-10-05 王总需求）=====
# ① 理想买点：由**这只票自己的脾气**决定回调多深——近 20 日平均振幅 ×3 当作一次常规回调幅度，
#    夹在 5%~25%；再用近 60 日收盘 20% 分位当支撑（跌到那儿大概率有承接）兜底：
#    支撑比算出来的深位还高，说明"跌不了那么多"，就按支撑给。绝不高于现价。
# ② rate_buy_point 锚点表：某票**第一次**被标注那天，把（日期 + 理想买点 + 当天现价）锁死；
#    之后每天只刷新"现在值多少"，行内的跟踪比对永远对着这第一次的判断，才叫"当时判断 vs 后来实盘"。
_RATE_DDL_BUY = """CREATE TABLE IF NOT EXISTS rate_buy_point (
  code TEXT PRIMARY KEY,
  anchor_date TEXT DEFAULT '',     -- 首次标注日（自然日，锁死）
  anchor_target REAL,              -- 首次标注的理想买点
  anchor_price REAL,               -- 首次标注那天的现价（对照用）
  manual REAL,                     -- 手工覆盖值（NULL＝用算法值）
  updated_at TEXT
)"""


def ideal_buy_point(bars, price):
    """(理想买点价, 买点相对现价的百分比, 距买点还差百分比) 三项都是 None 即算不出。

    dist＝现价/买点-1：正数＝还要再跌这么多才到买点（等），负数＝已经在买点下方（可买）。
    前端直接把 dist 当"离买点还有多远"显示，比(bool)"是否已到"好用得多。
    """
    try:
        p = float(price or 0)
        if p <= 0 or not bars:
            return None, None, False
        win = list(bars)[-60:]
        # 修复 P1-20：调用方传入的 bar 元组格式为 (date, close, high, low, vol)
        # （见同文件 _fetch_tencent_full docstring 与 b[1]=close），故 b[2]=high、b[3]=low。
        highs = [float(b[2]) for b in win if len(b) > 2 and b[2] is not None]
        lows = [float(b[3]) for b in win if len(b) > 3 and b[3] is not None]
        amp = 0.02
        if highs and lows and len(highs) == len(lows):
            seg = [(h, l) for h, l in zip(highs[-20:], lows[-20:])
                   if h and l and l > 0 and h >= l]
            if seg:
                amp = sum((h - l) / l for h, l in seg) / len(seg)
        if amp <= 0:                       # 高低价缺，退回收盘日波动近似振幅
            cs = [float(b[1]) for b in win if b[1]]
            rr = [abs(cs[i] / cs[i - 1] - 1.0) for i in range(1, len(cs)) if cs[i - 1]]
            if rr:
                amp = sum(rr) / len(rr)
        pull = min(max(amp * 3.0, 0.05), 0.25)
        ideal = p * (1 - pull)
        cs = sorted(float(b[1]) for b in win if b[1])   # 近 60 日收盘 20% 分位＝支撑
        if cs:
            k = min(int(len(cs) * 0.20), len(cs) - 1)
            if cs[k] > ideal:
                ideal = cs[k]
        if ideal > p:
            ideal = p
        # 第三项给「距买点还差多少」（正数＝还要跌这么多才够得着买点，负数＝已经在买点下方）。
        # 别用「现价 ≤ 买点×1.03」判"已到买点"——3% 容差下九成票都绿，等于没标。
        dist = round((p / ideal - 1.0) * 100.0, 2) if ideal else None
        return round(ideal, 2), round((ideal / p - 1.0) * 100.0, 2), dist
    except Exception:  # noqa: BLE001
        return None, None, None


def _rate_anchor(conn, code, target, price, today=None):
    """首次标注锁死锚点；已存在就一个字都不动（这正是「锚点」的含义）。返回是否新建。"""
    try:
        row = conn.execute("SELECT anchor_date FROM rate_buy_point WHERE code=?", (code,)).fetchone()
        if row:
            return False
        conn.execute(
            "INSERT INTO rate_buy_point (code, anchor_date, anchor_target, anchor_price, manual, updated_at)"
            " VALUES (?,?,?,?,NULL,?)",
            (code, today or _rate_today(), target, price,
             _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
        return True
    except Exception:  # noqa: BLE001
        return False


def rate_buy_map() -> dict:
    """{code: {anchor_date, anchor_target, anchor_price, manual, updated_at}}，前端/时间轴共用。"""
    out = {}
    try:
        conn = _rate_conn()
        try:
            conn.execute(_RATE_DDL_BUY)
            for r in conn.execute("SELECT code, anchor_date, anchor_target, anchor_price, manual,"
                                  " updated_at FROM rate_buy_point").fetchall():
                out[str(r["code"])] = {"anchor_date": str(r["anchor_date"] or ""),
                                       "anchor_target": r["anchor_target"],
                                       "anchor_price": r["anchor_price"],
                                       "manual": r["manual"], "updated_at": str(r["updated_at"] or "")}
        finally:
            conn.close()
    except Exception:  # noqa: BLE001
        pass
    return out


def set_buy_manual(code, price) -> dict:
    """手工覆盖/清空某票的理想买点。没锚点的顺手建（手工值即锚点）。"""
    code = str(code or "")[:6]
    price = None if price in (None, "", "null") else float(price)
    if price is not None and price <= 0:
        price = None
    if not code:
        return {"ok": False, "reason": "代码为空"}
    try:
        conn = _rate_conn()
        try:
            conn.execute(_RATE_DDL_BUY)
            row = conn.execute("SELECT anchor_date FROM rate_buy_point WHERE code=?", (code,)).fetchone()
            if row:
                conn.execute("UPDATE rate_buy_point SET manual=?, updated_at=? WHERE code=?",
                             (price, _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"), code))
                conn.commit()
                return {"ok": True, "code": code, "manual": price}
            conn.execute(
                "INSERT INTO rate_buy_point (code, anchor_date, anchor_target, anchor_price, manual, updated_at)"
                " VALUES (?,?,?,?,?,?)",
                (code, _rate_today(), price, None, price,
                 _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
            conn.commit()      # 不 commit 这行等于没存，下一个连接（rate_buy_map）读不到
            return {"ok": True, "code": code, "manual": price, "fresh": True}
        finally:
            conn.close()
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "reason": "写入失败：%s" % e}


def _rate_conn():
    # row_factory=Row：本模块多处用 dict(row) 直接转字典，默认 tuple 会炸
    c = _sqlite3.connect(str(_VIEWER_DB), timeout=15)
    try:
        c.row_factory = _sqlite3.Row
        c.execute("PRAGMA busy_timeout=15000")
    except Exception:  # noqa: BLE001
        pass
    return c


def _rate_today() -> str:
    return _dt.date.today().strftime("%Y-%m-%d")


def snapshot_top(rows: list, src: str = "auto", snap_date: str = None, trade_date: str = "") -> dict:
    """把当前这版榜落成当日快照（幂等覆盖）。rows 即 turnover_top 的 rows。

    trade_date＝这批行情的 data 日（入市价是那天收盘价），时间轴据此定基准 K 线；
    行级没带就沿用榜单级的，都缺才留空。
    """
    snap_date = snap_date or _rate_today()
    trade_date = str(trade_date or "")
    now = _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    payload = []
    for i, r in enumerate(rows or []):
        if not r.get("code") or r.get("score") is None:
            continue
        payload.append((
            snap_date, trade_date or str(r.get("trade_date") or ""), str(r.get("code"))[:6],
            str(r.get("name") or ""), int(i + 1),
            float(r.get("price") or 0.0), float(r["score"]),
            int(r.get("tier") or 0), str(r.get("tier_label") or ""),
            str(r.get("grade") or "—"), str(r.get("grade_cls") or ""),
            str(r.get("grade_desc") or ""), str(r.get("advice") or ""),
            _num_or_none(r.get("pos_pct")), _num_or_none(r.get("to_today")),
            _num_or_none(r.get("to_avg5")), str(r.get("scenario") or ""),
            str(r.get("hint") or ""), str(src), now,
            _num_or_none(r.get("ideal_buy")),
        ))
    if not payload:
        return {"ok": True, "snapped": 0, "snap_date": snap_date, "src": src,
                "trade_date": trade_date, "reason": "无可归档行（榜单为空或评分缺失）"}
    conn = _rate_conn()
    try:
        conn.execute(_RATE_DDL)
        conn.execute(_RATE_IDX)
        conn.execute(_RATE_DDL_BUY)
        # 幂等加列：CREATE TABLE 只管新库，老库得自己 ALTER，否则老库归档直接炸
        _cols = {r[1] for r in conn.execute("PRAGMA table_info(rate_snap)")}
        if "ideal" not in _cols:
            conn.execute("ALTER TABLE rate_snap ADD COLUMN ideal REAL DEFAULT NULL")
        conn.executemany(
            "INSERT OR REPLACE INTO rate_snap (snap_date, trade_date, code, name, rank,"
            " price, score, tier, tier_label, grade, grade_cls, grade_desc, advice,"
            " pos_pct, to_today, to_avg5, scenario, hint, src, created_at, ideal)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", payload)
        # 锚点：这批票里还没标注过的，第一次锁死（日期 + 理想买点 + 当天现价）。
        # 必须在同一个 conn 上做——上一个坑就是关了连接再查库报 closed database。
        n_anchor = 0
        for row in payload:
            if _rate_anchor(conn, str(row[2])[:6], row[-1], row[5], str(row[0])[:10]):
                n_anchor += 1
        conn.commit()
    except Exception as e:  # noqa: BLE001
        try:
            conn.rollback()
        except Exception:  # noqa: BLE001
            pass
        return {"ok": False, "snapped": 0, "snap_date": snap_date, "reason": "归档失败：%s" % e}
    finally:
        conn.close()
    return {"ok": True, "snapped": len(payload), "snap_date": snap_date, "src": src,
            "anchors": n_anchor}


def _num_or_none(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def snap_dates(limit: int = 20) -> list:
    """已有归档日清单（新→旧），每行带当日 Top1 便于一眼看出那天最强的是谁。"""
    limit = max(1, min(int(limit or 20), 90))
    conn = _rate_conn()
    try:
        conn.execute(_RATE_DDL)
        rs = conn.execute(
            "SELECT snap_date, COUNT(*) n, MAX(score) top_score,"
            " (SELECT code FROM rate_snap x WHERE x.snap_date=rate_snap.snap_date"
            " ORDER BY rank LIMIT 1) top_code,"
            " (SELECT name FROM rate_snap x WHERE x.snap_date=rate_snap.snap_date"
            " ORDER BY rank LIMIT 1) top_name"
            " FROM rate_snap GROUP BY snap_date ORDER BY snap_date DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rs]
    except Exception:  # noqa: BLE001
        import traceback as _tb
        _tb.print_exc()
        return []
    finally:
        conn.close()


def _rate_bars(codes: list, base_date: str, bars_days: int = 60):
    """取时间轴用的日线：缓存没盖住 base_date 就现场抓，避免拿旧 K 线当基准算错一格。

    缓存与阴跌扫描共用 data/decline_kcache.json，全程在 _DECLINE_LOCK 下读写。
    """
    meta = {"target": None, "n_fetched": 0, "n_cached": 0, "n_fail": 0}
    codes = [c for c in codes if c]
    if not codes:
        return {}, meta
    with _DECLINE_LOCK:              # 与阴跌扫描共用 K 线缓存文件，避免并发互踩
        bars_map, meta = _load_bars_map({c: "" for c in codes}, bars_days)
    stale = []
    for c in codes:
        b = bars_map.get(c) or []
        last = str(b[-1][0]) if b else ""
        if len(b) < _RATE_TTL[-1] + 3 or last < (base_date or ""):
            stale.append(c)
    for c in stale:                  # 缓存太旧 → 直接抓一次（腾讯源，几十毫秒级）
        try:
            with _DECLINE_LOCK:
                fresh = fetch_daily(c, bars=bars_days)
            if fresh:
                bars_map[c] = fresh
                meta["n_fetched"] = meta.get("n_fetched", 0) + 1
        except Exception:  # noqa: BLE001
            meta["n_fail"] = meta.get("n_fail", 0) + 1
    return bars_map, meta





def rate_timeline(days_window: int = 10, per_day: int = 5, bars_days: int = 60) -> dict:
    """时间轴：对每个归档日取前 per_day 名，补出 T+1/3/5/10 的收盘价与「入市价 → 后来价」比值。

    日线仍走 _load_bars_map（复用阴跌扫描的 K 线缓存，当天抓过就不再抓）。
    """
    days_window = max(1, min(int(days_window or 10), 60))
    per_day = max(1, min(int(per_day or 5), 30))
    bars_days = max(_RATE_TTL[-1] + 5, min(int(bars_days or 60), 240))
    dates = snap_dates(days_window)
    if not dates:
        return {"ok": True, "has_snap": False, "days_window": days_window,
                "per_day": per_day, "days": [], "summary": {},
                "ttl": list(_RATE_TTL),
                "reason": "还没有任何归档（先跑一次「全市场榜 / 归档今天」，时间轴才开始攒数据）"}
    conn = _rate_conn()
    rows = []
    try:
        # 真实归档量 ≠ 展示条数：per_day 只管「一天列几名」，归档量得单独 GROUP BY 一次。
        # 必须在 conn.close() 之前查 —— 2026-10-05 踩过：先前这段代码把 close 放在取完 rows 的
        # finally 里，主循环再查就抛 "Cannot operate on a closed database"，被兜底吞成 n。
        total_map = {}
        if dates:
            total_map = {str(t[0]): int(t[1]) for t in conn.execute(
                "SELECT snap_date, COUNT(*) FROM rate_snap WHERE snap_date IN (%s) GROUP BY snap_date"
                % ",".join("?" * len(dates)), tuple(x["snap_date"] for x in dates))}
        for d in dates:
            rs = conn.execute(
                "SELECT * FROM rate_snap WHERE snap_date=? ORDER BY rank LIMIT ?",
                (d["snap_date"], per_day)).fetchall()
            rows.extend([dict(r) for r in rs])
    finally:
        conn.close()
    codes = sorted({r["code"] for r in rows})
    base_date = min((d["snap_date"] for d in dates), default="")
    bars_map, meta = _rate_bars(codes, base_date, bars_days)
    buy_map = rate_buy_map()      # 锚点：首次标注那天锁死的「最值得入手价」

    def _idx(bars, date):
        """基准位置：最后一根 date 及以前的 K 线（当天还没出日线就退到最近一个已收盘日）。"""
        i = -1
        for k, b in enumerate(bars):
            if str(b[0]) <= date:
                i = k
            else:
                break
        return i if i >= 0 else (0 if bars else -1)

    out_days, summary = [], {}
    agg = {int(n): [] for n in _RATE_TTL}
    for d in dates:
        sd = str(d["snap_date"])
        items_ = [r for r in rows if str(r.get("snap_date")) == sd]
        if not items_:
            continue
        day = {"snap_date": sd, "n": len(items_),
               "total": int(d.get("n") or len(items_)), "trade_date": sd, "items": []}
        for r in items_:
            code = str(r["code"])
            bars = bars_map.get(code) or []
            i = _idx(bars, sd)
            base = bars[i] if i >= 0 else None
            base_p = float(base[1]) if base and base[1] else None
            bdate = str(base[0]) if base else ""
            ticks, pending = {}, []
            for n in _RATE_TTL:
                if not base or (i + int(n)) >= len(bars):
                    pending.append(int(n))
                    last = str(bars[-1][0]) if bars else ""
                    ticks[str(n)] = {"price": None, "ret": None, "date": None, "vs_target": None,
                                     "why": "T+%d 还没走完（可获取的 K 线只到 %s），走完这天自动出数，不会补 0"
                                            % (n, last or "无")}
                    continue
                b = bars[i + int(n)]
                p = float(b[1]) if b[1] else None
                tk = {"price": p, "date": str(b[0]),
                      "ret": round((p / base_p - 1.0) * 100.0, 2) if (p and base_p) else None}
                if p and base_p:
                    agg[int(n)].append((p / base_p - 1.0) * 100.0)
                tg = _rate_target(buy_map.get(code) or {})
                if tg and p:
                    tk["vs_target"] = round((p / tg - 1.0) * 100.0, 2)
                ticks[str(n)] = tk
            tr = _rate_track(buy_map.get(code) or {}, r.get("price"))
            day["items"].append({
                "rank": r.get("rank"), "code": code, "name": r.get("name") or "",
                "price": _num_or_none(r.get("price")),
                "grade": r.get("grade") or "",
                "grade_cls": r.get("grade_cls") or "",
                "grade_desc": r.get("grade_desc") or "",
                "base_bar": {"date": bdate}, "ticks": ticks,
                "pending": sorted(set(pending)), "track": tr})
        out_days.append(day)
    for n in _RATE_TTL:
        v = agg.get(int(n)) or []
        if v:
            win = sum(1 for x in v if x >= 0)
            summary[str(n)] = {"n": len(v), "win": win,
                               "win_rate": round(win * 100.0 / len(v), 1),
                               "avg": round(sum(v) / len(v), 2)}
    return {"ok": True, "has_snap": True, "days_window": days_window,
            "per_day": per_day, "days": out_days, "summary": summary,
            "ttl": list(_RATE_TTL), "base_date": base_date, "meta": meta,
            "buy_count": len(buy_map), "reason": ""}


def _rate_target(a: dict):
    """该票当前生效的「最值得入手价」：手填优先，否则算法锚点。"""
    try:
        v = (a or {}).get("manual")
        if v is None:
            v = (a or {}).get("anchor_target")
        return float(v) if v not in (None, "") else None
    except Exception:  # noqa: BLE001
        return None


def _rate_track(anchor: dict, price):
    """锚点跟踪：从首次标注那天（锁死，永不变）算到今天的自然日天数，
    外加「标注价 → 现价」的涨跌幅——直接回答王总那句「过了 10 天 20 天，它涨了还是跌了」。
    """
    out = {"ok": False, "from": "", "days": None, "target": None, "price": None, "diff": None}
    try:
        d0 = str((anchor or {}).get("anchor_date") or "").strip()
        if not d0 or len(d0) < 10:
            return out
        p = None
        try:
            p = float(price) if price not in (None, "") else None
        except (TypeError, ValueError):
            p = None
        out["ok"] = True
        out["from"] = d0[:10]
        out["target"] = _rate_target(anchor or {})
        out["price"] = p
        try:
            out["days"] = (_dt.date.today()
                           - _dt.datetime.strptime(d0[:10], "%Y-%m-%d").date()).days
        except Exception:  # noqa: BLE001
            out["days"] = None
        if out["target"] and p:
            out["diff"] = round((p / out["target"] - 1.0) * 100.0, 2)
    except Exception:  # noqa: BLE001
        pass
    return out


