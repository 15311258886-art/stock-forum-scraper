# -*- coding: utf-8 -*-
"""watchdesk —— 主动盯盘（2026-10-01 建）

定位（王总 2026-10-01 明确要求）：
  旧「个股快讯/公告」是**被动**的——用户手输代码、现抓现算，输出是公告罗列 + 一句「机器初判」。
  这里改成**主动**：系统自己扫一批标的的公告，扫完直接给出结论——
  「风险主导 / 机会窗口 / 多空交织 / 信息真空」+ 风险清单 + 机会清单 + **改判触发条件**。

盯盘池（王总 2026-10-01 拍板：全扫）：淘股吧博主重仓池（positions 表去重）+ 用户自选（watch_items）。
  实测东财公告接口 0.149s/只，199 只约 30s/轮，挂在现有 scheduler（交易时段每 15 分钟）完全够用。

纪律（与 watchlib 同一套）：
  * 独立两张表 stock_ann / stock_desk，**零 JOIN、零外键**；
  * 只做「抓取 + 存储 + 规则判定」，不碰任何既有业务表；
  * 诚实口径：抓不到就是「无数据」，不补数、不臆造结论；未命中规则的公告不参与评分，
    但不隐藏（明细里照样列出来）。

对外：
  ensure_schema(conn)                       建表（幂等）
  target_codes(conn)                        → [(code, name), …] 盯盘池
  scan(conn, window=30)                     → 扫一轮；新增公告进 stock_ann(is_new=1)，重算 stock_desk
  list_desk(conn)                           → 盯盘总览（前端列表用）
  detail(conn, code, window)                → 单票研判（结论/风险/机会/触发点/公告流水）
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import time
import urllib.parse
import urllib.request

_WINDOW_DAYS = 30            # 研判窗口：只看近 30 天公告
_MAX_FIN_PER_ROUND = 40      # 每轮最多补 N 只财务（防东财限流）
_FIN_TTL = 6 * 3600          # 财务缓存 6 小时

# ---------------------------------------------------------------- 规则表
# (正则, 权重, 标签, 极性)  —— 权重即"这条对风险的贡献"，同类合并后封顶 3 分，避免刷屏
_RISK_RULES = [
    (r"立案(?:调查|侦查)?|涉嫌(?:欺骗|违法|违规)|被证监会", 3.0, "立案调查", "bad"),
    (r"退市(?:风险)?|终止(?:上市|挂牌)|\*ST", 3.0, "退市风险", "bad"),
    (r"警示(?:函)?|监管(?:函|措施)|问询(?:函)?|关注函|责令(?:改正|整改)|处罚决定|行政处罚", 2.0, "监管处罚", "bad"),
    (r"冻结|拍卖|强制执行|股份被|司法", 2.0, "资产/股权冻结", "bad"),
    (r"诉讼|仲裁|赔偿|侵权|违约", 1.5, "诉讼仲裁", "bad"),
    (r"减持|清仓|大宗交易|拟减持|减持计划|解禁", 2.0, "股东减持", "bad"),
    (r"质押|担保|借款|对外担保", 1.0, "质押担保", "bad"),
    (r"预减|预亏|首亏|亏损|下滑|下降|低于预期|下降|同比(?:减少|下降)|毛利率(?:下降|下滑)|不及预期", 2.0, "业绩承压", "bad"),
    (r"减值|坏账|计提|商誉", 1.5, "减值计提", "bad"),
    (r"辞职|离任|离职|被带走|失联", 0.8, "人事变动", "bad"),
    (r"风险提示|不确定性|提醒|悬案|存疑", 1.0, "风险提示", "bad"),
]
_OPP_RULES = [
    (r"预增|预盈|扭亏|超预期|业绩(?:大增|增长|上升)|创新高|创历史新高|同比(?:增长|增加)|净利润(?:增长|大增)", 3.0, "业绩向好", "good"),
    (r"回购|增持|自增持|买入(?:计划)?|长期(?:资金|承诺)", 2.5, "回购增持", "good"),
    (r"分红|派息|现金红利|利润分配|送股|转增", 2.0, "股东回报", "good"),
    (r"股权激励|员工持股|限制性股票", 1.5, "股权激励", "good"),
    (r"中标|重大合同|订单|框架协议|供货|产能(?:扩张|投产)|新建项目|投资建设项目|扩产", 2.5, "订单产能", "good"),
    (r"收购|并购|重组(?:完成|获准|通过)?|资产注入|吸收合并", 2.0, "并购重组", "good"),
    (r"获批|注册|上市许可|临床试验|新品(?:发布|上市)|技术突破|专利|签约", 2.0, "产品/资质突破", "good"),
    (r"半导体|人工智能|算力|机器人|固态电池|创新药|低空经济|自动驾驶|数据(?:中心|要素)", 0.6, "题材催化", "good"),
]
_COMPILED_RISK = [(re.compile(p), w, l, t) for p, w, l, t in _RISK_RULES]
_COMPILED_OPP = [(re.compile(p), w, l, t) for p, w, l, t in _OPP_RULES]
# 否定语义前置拦截（见 _classify docstring）：字面含「减持」但实际是承诺不卖/终止减持。
# 只拦「减持」相关的否定，不要扩大 —— 否则「未按期还款」这类真风险会被误放行。
_NEG_ANN = re.compile(r"不[^，。；]{0,4}减持|未[^，。；]{0,4}减持|终止[^，。；]{0,8}减持|放弃[^，。；]{0,4}减持")

# 列集与 server._F10_COLS 同口径（roe 字段是 ROEJQ 不是 WEIGHTAVG_ROE，实测过）
_FIN_COLS = ("REPORT_DATE,REPORT_DATE_NAME,EPSJB,EPSKCJB,BPS,TOTALOPERATEREVE,TOTALOPERATEREVETZ,"
             "PARENTNETPROFIT,PARENTNETPROFITTZ,KCFJCXSYJLR,ROEJQ,ROEKCJQ,XSMLL,XSJLL,ZZCJLL,MGJYXJJE")


def _em_suffix(code: str) -> str:
    return "SH" if code[:1] in ("6", "9") else ("BJ" if code[:1] in ("4", "8") else "SZ")


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _today() -> str:
    return time.strftime("%Y-%m-%d")


def _f(v):
    """空/脏值 → None（诚实口径：宁可显示 —，不写 0）。"""
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


# ---------------------------------------------------------------- 建表
def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS stock_ann (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            code           TEXT,
            name           TEXT,
            title          TEXT,
            url            TEXT,
            notice_date    TEXT,
            cat            TEXT,
            tone           TEXT,
            is_new         INTEGER DEFAULT 0,
            first_seen_at  TEXT,
            disclose_at   TEXT DEFAULT ''
        );
        CREATE INDEX IF NOT EXISTS ix_ann_code_date ON stock_ann(code, notice_date);
        CREATE INDEX IF NOT EXISTS ix_ann_isnew ON stock_ann(is_new);

        CREATE TABLE IF NOT EXISTS stock_desk (
            code        TEXT PRIMARY KEY,
            name        TEXT,
            stance      TEXT,
            verdict     TEXT,
            risk_score  INTEGER,
            opp_score   INTEGER,
            risks       TEXT,
            opps        TEXT,
            triggers    TEXT,
            finance     TEXT,
            fresh_cnt   INTEGER DEFAULT 0,
            scan_at     TEXT
        );
        """
    )
    conn.commit()


# ---------------------------------------------------------------- 盯盘池
def target_codes(conn: sqlite3.Connection) -> list[tuple[str, str]]:
    """盯盘池 = 博主重仓池去重 + 用户自选。两个来源都不参与 JOIN，各自 distinct 后合并。"""
    out: dict[str, str] = {}
    try:
        for c, n in conn.execute(
                "SELECT DISTINCT symbol, COALESCE(symbol_name,'') FROM positions "
                "WHERE symbol IS NOT NULL AND length(symbol)=6"):
            out[str(c).zfill(6)] = n or ""
    except sqlite3.Error:
        pass
    try:
        for c, n in conn.execute(
                "SELECT symbol, COALESCE(name,'') FROM watch_items "
                "WHERE symbol IS NOT NULL AND length(symbol)=6"):
            out[str(c).zfill(6)] = n or ""
    except sqlite3.Error:
        pass
    return [(c, n) for c, n in out.items()]


# ---------------------------------------------------------------- 财务（轻量，带缓存）
_FIN_MEMO: dict[str, tuple[float, dict | None]] = {}


def _f10(code: str) -> dict | None:
    """东财 F10 主财务指标（最近报告期）。**必须走 emapi.datacenter**——实测自造 urllib
    请求（source=HSF10+client=PC）会稳定返回空 result，而 emapi 的 datacenter-web 通道拿到数。
    失败一律返回 None：财务取不到是常态，不能让整轮扫描挂掉。"""
    hit = _FIN_MEMO.get(code)
    if hit and time.time() - hit[0] < _FIN_TTL:
        return hit[1]
    res = None
    try:
        import emapi
        r = emapi.datacenter(
            "RPT_F10_FINANCE_MAINFINADATA", columns=_FIN_COLS,
            flt='(SECUCODE="%s.%s")' % (code, _em_suffix(code)),
            sort_columns="REPORT_DATE", sort_types="-1",
            page_size=1, page_number=1, timeout=8)
        rows = r.get("rows") or []
        res = rows[0] if rows else None
    except Exception:  # noqa: BLE001
        res = None
    _FIN_MEMO[code] = (time.time(), res)
    return res


def _fin_sentence(fin: dict | None) -> str:
    """财务 → 一句话（只陈述事实，不含判断词）。"""
    if not fin:
        return "财务数据未取到（源未命中），本轮判定仅基于公告面。"
    rep = fin.get("report") or fin.get("REPORT_DATE_NAME") or (fin.get("REPORT_DATE") or "")[:10] or "最新报告期"
    rev, npf, roe, ocf = fin.get("revenue_yoy"), fin.get("net_profit_yoy"), fin.get("roe"), fin.get("ocf_ps")

    def _f1(v):
        return "—" if v is None else ("%+.1f%%" % v if abs(v) < 1000 else "%.0f%%" % v)

    parts = [f"最新报告期（{rep}）"]
    if rev is not None or npf is not None:
        parts.append("营收同比 %s / 归母净利同比 %s" % (_f1(rev), _f1(npf)))
    if roe is not None:
        parts.append("ROE(加权) %.1f%%" % roe)
    if ocf is not None:
        parts.append("每股经营现金流 %.2f 元" % ocf)
    return "，".join(parts) + "。"


# ---------------------------------------------------------------- 分类
def _classify(title: str) -> tuple[str, str]:
    """标题 → (标签, 极性 good/bad/neutral)；与后端 _ANN_TITLE_RULES 同口径。

    2026-10-30 修正：先过一遍**否定语义**。
    规则表里「减持|清仓|解禁」是朴素关键词，字面含「减持」就标 bad，于是把下面这些
    **利好/中性**公告误判成风险（全市场抓取后每天都有 10+ 条，实测样例）：

      天岳先进：关于控股股东、实际控制人自愿承诺**不减持**公司股份的公告     ← 承诺不卖，利好
      统联精密：董事、高管**未减持**公司股份并**提前终止**减持计划的公告      ← 终止减持，利好
      今创集团：5%以上股东**提前终止**减持计划暨减持股份结果的公告            ← 中性

    这些必须先拦掉，否则「避坑」会给刚承诺不减持的票标红 —— 逻辑上完全反了。
    归为「其他事务/neutral」而不是「利好」：承诺不减持的方向虽偏正，但本项目没有
    对应实证，按纪律不给权重。
    """
    t = str(title or "")
    if _NEG_ANN.search(t):
        return "其他事务", "neutral"
    for rx, w, lab, tone in _COMPILED_RISK:
        if rx.search(t):
            return lab, tone
    for rx, w, lab, tone in _COMPILED_OPP:
        if rx.search(t):
            return lab, tone
    return "其他事务", "neutral"


def _agg(items: list[dict]) -> list[dict]:
    """同类合并：同一标签只留一条，权重求和但**单类封顶 3.0**，避免同类刷屏盖过其它因子。"""
    buckets: dict[str, dict] = {}
    for it in items:
        b = buckets.setdefault(it["label"], {"label": it["label"], "w": 0.0,
                                             "items": [], "latest": it["d"]})
        b["w"] += it["w"]
        b["items"].append(it)
        if it["d"] and it["d"] > b["latest"]:
            b["latest"] = it["d"]
    out = []
    for lab, b in buckets.items():
        out.append({
            "label": lab,
            "w": round(min(b["w"], 3.0), 1),      # 单类封顶 3
            "count": len(b["items"]),
            "latest": b["latest"],
            "titles": [x["t"] for x in b["items"][:4]],
        })
    out.sort(key=lambda x: -x["w"])
    return out


# ---------------------------------------------------------------- 预判引擎（核心）
def _judge(rows: list[dict], fin: dict | None, lhb_net: float | None,
           window: int) -> dict:
    """由「近 N 天公告 + 财务 + 龙虎榜」→ 结论 + 风险/机会清单 + 改判触发点。"""
    risks_raw, opps_raw = [], []
    for r in rows:
        lab, tone = _classify(r.get("title") or "")
        if tone == "bad":
            w = next((ww for rx, ww, ll, _ in _COMPILED_RISK if ll == lab), 1.0)
            risks_raw.append({"label": lab, "w": w, "t": (r.get("title") or "")[:70], "d": r.get("notice_date") or ""})
        elif tone == "good":
            w = next((ww for rx, ww, ll, _ in _COMPILED_OPP if ll == lab), 1.0)
            opps_raw.append({"label": lab, "w": w, "t": (r.get("title") or "")[:70], "d": r.get("notice_date") or ""})

    risks, opps = _agg(risks_raw), _agg(opps_raw)
    risk = min(10, int(round(sum(x["w"] for x in risks))))
    opp = min(10, int(round(sum(x["w"] for x in opps))))

    # 财务因子参与（净利同比为负 → 风险 +1；现金流转负 → 再 +1；ROE 高 → 机会 +1）
    npf = (fin or {}).get("net_profit_yoy")
    ocf = (fin or {}).get("ocf_ps")
    roe = (fin or {}).get("roe")
    if npf is not None and npf < 0:
        risk += 1
    if ocf is not None and ocf < 0:
        risk += 1
    if roe is not None and roe >= 15:
        opp += 1
    risk, opp = min(10, risk), min(10, opp)

    hard = any(x["label"] in ("立案调查", "退市风险") for x in risks)
    hard_opp = any(x["label"] in ("回购增持", "业绩向好") for x in opps)
    n_new = len(rows)

    if hard or (risk - opp >= 2):
        stance = "风险主导"
    elif opp - risk >= 2:
        stance = "机会窗口"
    elif max(risk, opp) <= 1:
        stance = "信息真空"
    else:
        stance = "多空交织"

    # ---- 一句话结论（结论先行：给倾向 + 依据 + 下一步动作指向）
    rtxt = "、".join([x["label"] for x in risks[:3]]) or "无实质风险项"
    otxt = "、".join([x["label"] for x in opps[:3]]) or "无实质机会项"
    fin_s = _fin_sentence(fin)
    if stance == "风险主导":
        verdict = ("风险主导：近 %d 天 %d 条公告里风险项 %d 分（%s），机会项仅 %d 分（%s）；%s"
                   "——在利好落地前，倾向以规避/观望为主，等风险项出清再看。" % (window, n_new, risk, rtxt, opp, otxt, fin_s))
    elif stance == "机会窗口":
        verdict = ("机会窗口：近 %d 天 %d 条公告里机会项 %d 分（%s），风险项 %d 分（%s）；%s"
                   "——已有正面催化在先，确认放量/回踩不破再介入，别拿消息当成交。" % (window, n_new, opp, otxt, risk, rtxt, fin_s))
    elif stance == "信息真空":
        verdict = ("信息真空：近 %d 天 %d 条公告，无实质风险也无明确催化，%s——"
                   "事件面没给驱动，除非财报或新增公告出现，否则不加仓。" % (window, n_new, fin_s))
    else:
        verdict = ("多空交织：风险 %d 分（%s）对机会 %d 分（%s），两边都没压倒；%s"
                   "——现在不是单边行情，等一个明确信号（新公告或财报）再定方向。"
                   % (risk, rtxt, opp, otxt, fin_s))

    # ---- 改判触发点（明确写「出现什么 → 结论改成什么」）
    trig: list[str] = []
    if hard:
        trig.append("若补发正面公告（回购/增持/业绩预增），机会分 +≥3，结论可上调为「多空交织」；"
                    "若再出现立案或退市风险提示，风险维持 10 分封顶并置顶。")
    if any(x["label"] == "股东减持" for x in risks):
        trig.append("若减持比例超过总股本 1% 或出现续减计划，风险分 +3 直接封顶；若减持完成且无后续计划，风险项 -3 可降级。")
    if any(x["label"] == "业绩承压" for x in risks):
        trig.append("若下期财报净利同比回正且营收同步转正，风险分 -2，结论有机会上调一档。")
    if any(x["label"] in ("订单产能", "产品/资质突破", "并购重组") for x in opps):
        trig.append("若相关订单/产能落地为正式合同或收入确认，机会分 +2，可升级为「机会窗口」。")
    if any(x["label"] == "题材催化" for x in opps) and not hard_opp:
        trig.append("当前机会主要来自题材词（不构成业绩），若题材退潮，机会分 -3 将回落为「多空交织」。")
    # 龙虎榜只在「真有净额」时才写（否则会出现「净卖出 0.00 亿」这种噪音触发句）
    if lhb_net is not None and abs(lhb_net) >= 0.05:
        trig.append(("近 5 日龙虎榜净买入 %.2f 亿（游资在接）——若转为净卖出，机会项 -2。" % lhb_net)
                    if lhb_net > 0 else
                    ("近 5 日龙虎榜净卖出 %.2f 亿（游资在出）——若转为净买入，机会项 +2。" % abs(lhb_net)))
    if not trig:
        # 通用兜底（任何票都成立），别再写「连续 15 天无公告维持信息真空」这种
        # 只有「信息真空」才说得通的句 —— 风险主导的票挂着它读着像自相矛盾。
        trig.append("公告面无强信号：若下期财报净利同比继续为负，风险分 +2 维持当前判定；"
                    "若出现回购 / 增持 / 预增任一类公告，机会分 +2 可上调一档。")
    trig = trig[:4]

    return {
        "stance": stance, "verdict": verdict,
        "risk_score": risk, "opp_score": opp,
        "risks": risks, "opps": opps, "triggers": trig,
        "sentence": fin_s,
        "counts": {"total": n_new, "risk": len(risks), "opp": len(opps)},
    }


# ---------------------------------------------------------------- 扫描
def scan(conn: sqlite3.Connection, window: int = _WINDOW_DAYS, log=None) -> dict:
    """扫一轮盯盘池：抓公告 → 增量入库 → 重算每票研判。全程吞异常，单票失败不影响整轮。"""
    ensure_schema(conn)
    t0 = time.time()
    codes = target_codes(conn)
    names = {c: (n or "") for c, n in codes}     # code → 名称，供写结论行时补名
    baseline = conn.execute("SELECT COUNT(*) FROM stock_ann").fetchone()[0]   # 首轮=基线，不标新
    conn.execute("UPDATE stock_ann SET is_new=0")
    conn.commit()

    first_seen, added = _now(), 0
    scanned = failed = 0
    for code, name in codes:
        scanned += 1
        try:
            import emapi
            r = emapi.announcement(code, num=max(1, min(window, 100)))
            if not r.get("ok"):
                failed += 1
                continue
            for it in (r.get("rows") or []):
                art = it.get("art_code") or ""
                url = ("https://data.eastmoney.com/notices/detail/%s/%s.html" % (code, art)) if art else ""
                title = (it.get("title") or "").strip()
                if not title:
                    continue
                nd = str(it.get("notice_date") or "")[:10]
                lab, tone = _classify(title)
                cur = conn.execute(
                    "SELECT id FROM stock_ann WHERE code=? AND url=?", (code, url)).fetchone()
                if cur:
                    continue
                is_new = 0 if baseline == 0 else 1
                conn.execute(
                    "INSERT INTO stock_ann(code,name,title,url,notice_date,cat,tone,is_new,first_seen_at,disclose_at)"
                    " VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (code, name, title, url, nd, lab, tone, is_new, first_seen,
                     (nd + " 15:00:00") if nd else ""))
                added += 1
        except Exception:  # noqa: BLE001 —— 单票失败（限流/解析）不拖垮整轮
            failed += 1
            continue

    # 财务只对「本轮有新增」或「库里还没有」的票补，且每轮限量
    need = [c for c, _ in codes]
    fin_use = {}
    # 首轮（baseline==0）多补一些财务，让首次打开就有财务支撑的结论；之后回到限量，防限流
    fin_limit = _MAX_FIN_PER_ROUND * 3 if baseline == 0 else _MAX_FIN_PER_ROUND
    for code in need[:fin_limit]:
        try:
            f = _f10(code)
            if f:
                fin_use[code] = {
                    "report": ((f.get("REPORT_DATE_NAME") or (f.get("REPORT_DATE") or "")[:10]) or ""),
                    "revenue_yoy": _f(f.get("TOTALOPERATEREVETZ")),
                    "net_profit_yoy": _f(f.get("PARENTNETPROFITTZ")),
                    "roe": _f(f.get("ROEJQ")),
                    "ocf_ps": _f(f.get("MGJYXJJE")),
                }
        except Exception:  # noqa: BLE001
            pass

    # 逐票重算研判（只重算：本轮有新增 / 库里无记录 / 已超 6 小时）
    # 重算范围 = 全盯盘池（纯 SQL，毫秒级）。财务没取到也照样出结论 —— 那样只会落到「信息真空」口径，
    # 不能因为财务挂了就让 stock_desk 一片空白、页面没结论可看。
    recalc = [c for c, _ in codes]

    computed = 0
    for code in recalc:
        try:
            rows = conn.execute(
                "SELECT title, notice_date FROM stock_ann WHERE code=? "
                "AND notice_date>=date('now','localtime','-%d day')" % window, (code,)).fetchall()
            fin = fin_use.get(code)
            try:
                # 净额 = SUM(net_amt)（此前只累加正值，等于把净卖出算成了 0，是个静默的口径错误）
                lr = conn.execute(
                    "SELECT SUM(CAST(net_amt AS REAL))/100000000.0 "
                    "FROM (SELECT net_amt FROM lhb_stocks WHERE security_code=? "
                    "ORDER BY trade_date DESC LIMIT 5)", (code,)).fetchone()
                lhb_net = float(lr[0]) if lr and lr[0] is not None else None
            except sqlite3.Error:
                lhb_net = None
            # 名称三级回退：盯盘池（博主池 symbol_name / 自选 name）→ 已有结论行 → 公告库。
            # 此前只从 stock_desk 自己取，首轮该行还不存在 → 203 只名称全空，前端列表只有代码。
            nm = (names.get(code) or "").strip()
            if not nm:
                row = conn.execute("SELECT name FROM stock_desk WHERE code=?", (code,)).fetchone()
                nm = (row[0] if row else "") or ""
            if not nm:
                row = conn.execute(
                    "SELECT name FROM stock_ann WHERE code=? AND name IS NOT NULL AND name<>'' "
                    "ORDER BY notice_date DESC LIMIT 1", (code,)).fetchone()
                nm = (row[0] if row else "") or ""
            j = _judge([{"title": r[0], "notice_date": r[1]} for r in rows], fin, lhb_net, window)
            conn.execute(
                "INSERT INTO stock_desk(code,name,stance,verdict,risk_score,opp_score,risks,opps,triggers,finance,scan_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(code) DO UPDATE SET name=excluded.name, stance=excluded.stance,"
                " verdict=excluded.verdict, risk_score=excluded.risk_score, opp_score=excluded.opp_score,"
                " risks=excluded.risks, opps=excluded.opps, triggers=excluded.triggers,"
                " finance=excluded.finance, scan_at=excluded.scan_at",
                (code, nm, j["stance"], j["verdict"], j["risk_score"], j["opp_score"],
                 json.dumps(j["risks"], ensure_ascii=False), json.dumps(j["opps"], ensure_ascii=False),
                 json.dumps(j["triggers"], ensure_ascii=False),
                 json.dumps(fin, ensure_ascii=False) if fin else None, _now()))
            computed += 1
        except Exception:  # noqa: BLE001
            continue

    # 存量回填：升级前写入的结论行名称列全为空，直接从公告表补回，免得整池重扫一遍
    try:
        conn.execute(
            "UPDATE stock_desk SET name=(SELECT a.name FROM stock_ann a"
            " WHERE a.code=stock_desk.code AND a.name IS NOT NULL AND a.name<>''"
            " ORDER BY a.notice_date DESC LIMIT 1) WHERE name IS NULL OR name=''")
    except sqlite3.Error:
        pass
    conn.commit()
    # 注意：is_new 只在插入时按 baseline 决定（首轮=0 基线，之后=1 本轮新增）。
    # 这里曾有过「末尾统一刷 is_new=1」的兜底，导致首轮把 6000 条历史全标成新公告 —— 已删。

    stat = {"scanned": scanned, "added": added, "failed": failed,
            "computed": computed, "elapsed": round(time.time() - t0, 1)}
    if log:
        try:
            log("[watchdesk] 扫 %d 只 / 新增 %d / 重算 %d / 失败 %d / %.1fs"
                % (scanned, added, computed, failed, stat["elapsed"]))
        except Exception:  # noqa: BLE001
            pass
    return stat


# ---------------------------------------------------------------- 读
def list_desk(conn: sqlite3.Connection, limit: int = 200) -> dict:
    """盯盘总览（前端列表）：机会优先（王总 2026-10-01 拍板「机会高的靠前」）。

    排序链：机会分降序 → 净分（机会−风险）降序 → 风险分升序 → 有新公告的靠前 → 代码兜底。
    为什么不是「纯机会降序」：全池 7 成以上机会是 0 分，纯按机会排会让后 5/6 页面顺序随机；
    用净分做第二序、风险低做第三序，能让 0 分梯队里相对安全的自然冒头，同分段才有意义。
    """
    ensure_schema(conn)
    try:
        # 注意：SQL 必须显式用括号拼成一个字符串再传，不能靠「相邻字符串字面量」当多实参，
        # 那样 sqlite3 会按多参数解析直接抛 TypeError（2026-10-01 踩过）。
        sql = (
            "SELECT d.code, d.name, d.stance, d.verdict, d.risk_score, d.opp_score, d.risks, d.opps,"
            " d.triggers, d.finance, d.scan_at,"
            " (SELECT COUNT(*) FROM stock_ann a WHERE a.code=d.code AND a.is_new=1) AS fresh,"
            " (SELECT MAX(notice_date) FROM stock_ann a WHERE a.code=d.code) AS last_ann"
            " FROM stock_desk d"
            " ORDER BY d.opp_score DESC, (d.opp_score - d.risk_score) DESC,"
            " d.risk_score ASC,"
            " COALESCE(NULLIF((SELECT COUNT(*) FROM stock_ann a WHERE a.code=d.code AND a.is_new=1),0),0) DESC,"
            " d.code ASC"
            " LIMIT ?"
        )
        rows = conn.execute(sql, (limit,)).fetchall()
    except sqlite3.Error as e:
        return {"ok": False, "reason": str(e), "rows": [], "stats": {}}
    out = []
    for r in rows:
        out.append({
            "code": r[0], "name": r[1] or "", "stance": r[2] or "", "verdict": r[3] or "",
            "risk_score": r[4] or 0, "opp_score": r[5] or 0,
            "risks": json.loads(r[6] or "[]"), "opps": json.loads(r[7] or "[]"),
            "triggers": json.loads(r[8] or "[]"),
            "finance": json.loads(r[9] or "null") if r[9] else None,
            "scan_at": r[10] or "", "fresh": r[11] or 0, "last_ann": r[12] or "",
        })
    try:
        fresh_total = conn.execute("SELECT COUNT(*) FROM stock_ann WHERE is_new=1").fetchone()[0]
    except sqlite3.Error:
        fresh_total = 0
    return {"ok": True, "rows": out,
            "stats": {"total": len(out), "fresh": fresh_total, "last_scan": _now()}}


def detail(conn: sqlite3.Connection, code: str, window: int = _WINDOW_DAYS) -> dict:
    """单票研判：结论卡 + 风险/机会 + 触发点 + 公告流水明细。"""
    ensure_schema(conn)
    code = re.sub(r"\D", "", code or "")[:6]
    if len(code) != 6:
        return {"ok": False, "reason": "请输入 6 位 A 股代码", "anns": []}
    d = conn.execute(
        "SELECT code,name,stance,verdict,risk_score,opp_score,risks,opps,triggers,finance,scan_at"
        " FROM stock_desk WHERE code=?", (code,)).fetchone()
    fin = None
    if not d:
        f = _f10(code)
        if f:
            fin = {"report": (f.get("REPORT_DATE_NAME") or (f.get("REPORT_DATE") or "")[:10]),
                   "revenue_yoy": _f(f.get("TOTALOPERATEREVETZ")),
                   "net_profit_yoy": _f(f.get("PARENTNETPROFITTZ")),
                   "roe": _f(f.get("ROEJQ")), "ocf_ps": _f(f.get("MGJYXJJE"))}
    anns = []
    try:
        for r in conn.execute(
                "SELECT title,url,notice_date,cat,is_new FROM stock_ann WHERE code=? "
                "AND notice_date>=date('now','localtime','-%d day') ORDER BY notice_date DESC" % window, (code,)):
            anns.append({"title": r[0], "url": r[1] or "", "date": r[2] or "",
                         "cat": r[3] or "", "is_new": r[4] or 0})
    except sqlite3.Error:
        pass
    return {
        "ok": True, "code": code, "name": d[1] if d else "",
        "stance": d[2] if d else "", "verdict": d[3] if d else "",
        "risk_score": d[4] if d else 0, "opp_score": d[5] if d else 0,
        "risks": json.loads(d[6] or "[]") if d else [], "opps": json.loads(d[7] or "[]") if d else [],
        "triggers": json.loads(d[8] or "[]") if d else [],
        "finance": json.loads(d[9]) if (d and d[9]) else fin,
        "sentence": _fin_sentence(fin or (json.loads(d[9]) if (d and d[9]) else None)),
        "scan_at": d[10] if d else "", "anns": anns,
    }
