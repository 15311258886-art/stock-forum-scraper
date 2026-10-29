# -*- coding: utf-8 -*-
"""避坑标记 —— 把「别买它」从博主荐股换成当日量价事实。

2026-10-30 进哥拍板：
  ① 旧「博主反向警示」废弃 —— 它拿 **沉淀快照**（blogger_overlap，最后一次更新
     2026-10-27，博主源已停）去绑定个股。6 月推荐过的票到 11 月还在亮灯，
     荐股保鲜期实际不到 20 天，超期就该作废。
  ② 避坑功能本身要留 —— 但指标不能再由「谁推荐过」提供。

**为什么不是修窗口、而是换源**
  旧实现其实已有 `BLOGGER_RISK_WINDOW_DAYS` 时效过滤（10 交易日 × 1.4 ≈ 14 天），
  但只有 `avg_t5` 受窗口约束，`n_bloggers` 仍取自全期快照 —— 窗口内外混用一套口径，
  结果既可能误报、又必然随源停更而彻底失效。与其补漏，不如换到**每天必定更新**的源上。

**指标怎么来的（不是拍脑袋）**

  ⚠️ **2026-10-31 统一重标定：下方全部规则数字已重算，旧数字作废。**
  旧数字来自各写各的 SQL，用的是「t 日收盘买入」的**宽松口径** —— 信号是盘后才知道的，
  t 日收盘价根本买不到，强度因此被系统性高估（个别规则甚至全部效应都来自当日那根 K 线）。
  现在统一由 `viewer/hypo.py` 一个引擎、一个口径重算：

    d 日盘后知情 → d+1 收盘建仓 → d+1+5 收盘平仓，超额减同日全市场等权，t 按日聚合

    规则                        旧权  新权   T+5 超额     t      结论
    龙虎榜 换手≥15%（原分两档）    4     6    -6.12%   -9.05   合并升权
    极端放量 量比≥3                —     4    -1.81%   -2.59   新增
    大跌 ≥7%                       —     4    -1.15%   -2.99   新增
    股东减持公告 20日               2     3    -1.83%   -4.62   升权（全市场公告后）
    放量 量比2~3                   —     2    -0.89%   -4.13   新增
    当日下跌 5~7%                  3     2    -0.80%   -2.56   降权（从「暴跌」拆分）
    MACD 死叉                     2     2    -0.51%   -2.01   边缘，保留
    放量下跌（量比≥1.8且跌≥3%）      4     0    +0.01%   -1.73   **删除**
    缩量下跌                      2     0    -0.24%   -1.97   删除
    两日累跌≥6%                    1     0    -0.36%   -1.75   删除

  ★ 最关键的一条：**「放量下跌」在可执行口径下超额 +0.01%，等于零。**
    它原本占 4 分（20% 权重），但全部效应都发生在下跌当天那根 K 线上，次日买进去什么都
    不剩。而对照组「放量**上涨**」反而有 -0.97%（t=-3.81）——说明起作用的不是「跌」，
    是**极端放量本身**（注意力峰值）。这解释了为什么龙虎榜（本质就是极端放量异动）信号
    最强，也是本系统里机制最清晰的一条。

  ★ 门槛原则（比 t 值更重要）：**|t|≥2 只说明"不是噪声"，能否进规则还要看幅度。**
    双边手续费 + 滑点约 0.15%~0.25%，幅度小于 0.3% 的信号即使显著也没有操作价值。
    被删的三条 t 值分别是 -1.73/-1.97/-1.75，幅度全部 <0.4%，删得有理。

**公告维度：2026-10-30 夜二次复核，结论收窄（推翻了本文档第一版）**
  第一版按 cat 拆开后取「股东减持」「人事变动」，那些数字来自 `t 日收盘起算` 的**宽松口径**。
  但公告是**盘后**发的 —— t 日收盘价根本买不到，宽松口径会系统性地高估。改成
  「次日收盘起」重算、并补安慰剂（看公告**前** 20 日是否已经跑输）之后，结论变了：

    股东减持  T+5 严格 -0.37% t=-0.13  →  T+20 严格 -3.62% t=-1.70   ← 只有 20 日尺度能看
              安慰剂（事前 20 日）+1.24% t=+0.31  → 不是「弱势股标签」，是公告后的真实拖累
    人事变动  T+20 严格 -3.39% t=-1.57，T+5 -0.85% t=-0.74，安慰剂 +0.51%
              → **删除这条规则**。原 t=-3.33 是小样本 + 宽松口径的产物。

  所以只留「股东减持」一条，窗口 15→20 天。
  ⚠️ 2026-10-31 更新：**上面这段结论已被新数据推翻。** 当时 n=115（公告只覆盖盯盘池
  那 200 只票），全市场公告抓取（`viewer/ann_feed.py` 于 10-31 上线）之后样本变成
  n=1253 / 129 个交易日，严格口径 T+5 超额 −1.83%、t=−4.62 —— 强度回到可给分的水平，
  权重由 2 升到 3。教训：**在小样本上算出的 t 值，换个覆盖度就会变号**；公告类指标
  的结论必须标注样本来源与覆盖范围，否则下一个读代码的人会以为它天生没用。

  ★ 更根本的问题：`stock_ann` 日均只有 40 条，A 股实际每天上千条，**覆盖率约 3%**。
    原因是唯一写入方 `watchdesk.py` 只抓**盯盘池那几十只**。在这个样本上算出的任何
    公告 t 值都不可靠。已新增 `viewer/ann_feed.py` 抓全市场公告 —— 等新数据积累满
    3 个月，本节所有数字必须重估（「人事变动」可能被平反，「减持」也可能被降得更低）。
    **在此之前，公告类权重一律按保守值给，不按历史 t 值给。**

  仍然成立的教训：**「利空公告」不是一类东西，是一个筐** —— 拿 tone=bad 当指标，
  强度会被稀释到 1/4，还会把以下实证为**正向**的类别错标成风险：
  监管处罚 +1.47%（处罚落地＝利空出尽）、风险提示 +0.82%、诉讼仲裁 +0.42%、
  资产/股权冻结 +0.72%。

**龙虎榜：本项目目前最强的避坑信号（2026-10-30 新增）**
  来源 `lhb_stocks`（日更）。实证脚本 `tools/probe_lhb_ann.py` + `tools/probe_lhb_incr.py`，
  口径同样是「次日收盘起」（可执行）：

    全部上榜        n=994   T+5 -3.05%  t=-5.03  胜率 32.7%
    换手 15~20%     n=127   T+5 -3.04%  t=-2.13
    换手 20~30%     n=169   T+5 -5.56%  t=-9.05  胜率 24.9%
    换手 ≥30%       n=171   T+5 -5.82%  t=-7.11  胜率 19.3%
    换手 ≥30% 的 T+10  n=126  -9.92%  t=-9.34  胜率 12.7%

  **它不是「放量大涨」的影子。** 用「同日、同涨跌方向、同量比档」的未上榜股票做配对
  对照，净效应 -3.12%、t=-6.07，保留了原幅度的 **103%**。这与 HOLD 的结局正好相反
  （HOLD 扣掉前期涨幅后只剩 t=-1.79 被否决），所以这条经得起配对检验才敢上。

  **也不是「跌一下会反弹」**：T+1 -1.09%(t=-1.66) → T+3 -3.31% → T+5 -5.82% → T+10 -9.92%，
  逐级恶化、T+10 胜率只剩 12.7%。所以窗口取 10 个交易日，而不是 3~5 天。

  门槛为什么是 15%（2026-10-31 由 20% 下调）：统一引擎重算后，15~20% 与 ≥20% 两档
  强度**几乎相同**（−6.12% t=−9.05 vs −6.43% t=−9.02）；旧口径下的 −3.04%/t=−2.13
  是宽松口径的产物。既然门槛降到 15% 不损失强度、还能多覆盖一批票，就没必要分两档。
  <15% 仍不命中（−1.35%，t=−1.84，不显著）。
  换手率是这条规则里唯一有分辨力的变量 —— 涨幅（涨停反而不如小涨差）、净买卖方向
  （都显著负、差别不大）都不如它单调。

**被实证否决、刻意不要的（后人勿加回来）**
  · 破 MA20 / 破 MA20 且破 MA60 —— ex5 **+0.127% / +0.166%**，t≈+1.9。
    「跌破均线就躲」是错的：那更像超跌反弹区，标红会拦掉本该捡的票。
  · 超买类 RSI6>85 (+0.694%) / BIAS6>10 (+0.860%) / MA60 上方偏离>20% (+0.719%)。
    「涨太多别追」在本项目样本里也得不到支持。
  · 60 日高点回撤≥25%、MA20 下方偏离>10% —— 均值与日聚合 t 的**符号相反**，
    说明被少数极端日主导，不稳定，不进权重。
  · 监管处罚 / 风险提示 / 诉讼仲裁 / 资产冻结 —— 全部正向（见上表）。
  这些写在这里不是备忘，是**挡人的**：它们都是最容易被「常识」推回来的候选。

**为什么实时算而不建表**
  进哥的原话是「不能拿沉淀数据绑定一只股票」。最彻底的做法不是加时效字段，
  而是**根本不存**：每次调用都从 `stock_indicators_daily` 的最近 3 个交易日现算。
  代价是候选池几十上百只票查一次几十毫秒；收益是物理上不可能陈旧 ——
  没有表可以忘更新，也就没有第二次「博主反向警示」。
"""

from __future__ import annotations

import os
import sqlite3
from datetime import date as _date

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(ROOT, "viewer", "app.db")

MAX_SCORE = 19                      # 6+2+5+3+1+2+0（长样本重标定后）
LEVEL_HIGH = 7                      # 满分 19 的 35%，与历次改版保持同一比例
LEVEL_MID = 2                       # 单条低权重项（2分）不亮灯 —— 覆盖太广，单独亮灯会把
                                    # 半张表标红、提示失去区分度；叠加到 ≥2 才有意义
ANN_WINDOW_DAYS = 20                # 5/10 日均不显著；20 日窗口在严格口径下 t=-4.62
# 只认这一类公告：二次复核后「人事变动」被删（全周期不显著）。
# 其余 bad 类别（质押担保/减值计提/监管处罚/风险提示/诉讼/冻结）要么是稀释项，
# 要么实证为**正向**，一律不算 —— 尤其「监管处罚」，处罚落地更像利空出尽。
ANN_CATS = ("股东减持",)

# 龙虎榜：上榜后 10 个交易日内持续恶化（T+10 超额仍 -9.92%、胜率仅 12.7%），故窗口取 10 日
LHB_WINDOW_DAYS = 10
# 2026-10-31 修订：原分 15~20% / ≥20% 两档，重标定后发现**两档强度几乎相同**
# （-6.12% t=-9.05 vs -6.43% t=-9.02），分档只会把同一个信号拆成两行、还让权重分配失真。
# 合并为一档，门槛降到 15%：≥15% 全算强信号，<15% 仍不命中（-1.35%，t=-1.84 不显著）。
LHB_THRESHOLD = 15.0

# 量比档：本项目 volume_ratio = 当日量/含当日5日均量，数学上限≈5，
# 所以 3.0 已经是"极端放量"（对应真实量比约 5~7 倍）。不要按通达信量比理解。
VOL_EXTREME = 3.0
VOL_HEAVY = 2.0

# (key, 权重, 实证摘要) —— 顺序即展示顺序。
# 全部数字由 viewer/hypo.py 统一引擎在**严格可执行口径**下重算（2026-10-31）：
#   d 日盘后知情 → d+1 收盘建仓 → d+1+5 收盘平仓，超额减同日全市场等权，t 按日聚合。
# 上一版数字来自各写各的 SQL（含当日收盘买入，实际买不到），强度普遍被高估。
#
# 2026-10-31 二次重标定（**这一版才是可用的**）：因子表回补到 2016-01-04（2627 个交易日）
# 后，10 条可落谓词的 claim 全部用长样本重算 —— 原来那批数字只有 288 个交易日（单一牛市），
# 放量类效应被系统性高估。变动：极端放量 4→2（-1.81%→-0.84%）、放量 2→1（-0.89%→-0.18%）、
# MACD死叉 2→0（-0.51%→-0.08%，t=-1.09 已不显著）、大跌 4→5（-1.15%→-1.48%，t=-11.71 更强）。
# 未动：龙虎榜（-5.60%，但**数据只有 20 天**）、股东减持（-1.79%，数据 130 天）、当日下跌（-0.95%）。
RULES = (
    ("龙虎榜高换手", 6, "近10日上榜且换手≥15%，T+5超额-6.12%，t=-9.05，胜率19.3%"
                       "（长样本复核受限于龙虎榜数据仅 20 天，无法拉长）"),
    ("大跌", 5, "单日跌幅≥7%，长样本(2016起)T+5按日超额-1.48%，t=-11.71；"
                "注意逐样本均值为+0.89%——少数暴跌后暴力反弹日会掩盖日常的跑输"),
    ("股东减持", 3, "近20日减持公告，T+5超额-1.83%，t=-4.62（全市场公告 n=1253）"),
    ("极端放量", 2, "量比≥3，长样本T+5按日超额-0.84%，t=-5.59（原短样本-1.81%被高估一倍）"),
    ("当日下跌", 2, "单日跌幅5%~7%，长样本T+5按日超额-0.95%，t=-9.78"),
    ("放量", 1, "量比2~3，长样本T+5按日超额仅-0.18%，t=-2.55——显著但幅度可忽略"),
    ("MACD死叉", 0, "柱由正转负，长样本-0.08%，t=-1.09，已不显著：只展示、不给分"),
)


def _recent_dates(con, n=3):
    """最近 n 个**有数据**的交易日（降序）。"""
    rows = con.execute(
        "SELECT DISTINCT date FROM stock_indicators_daily ORDER BY date DESC LIMIT ?",
        (n,)).fetchall()
    return [r[0] for r in rows]


def _ann_ordinals(con, codes):
    """{code: {cat: 最近一次该 cat 公告的 ordinal}}（只取 ANN_CATS 两类）。"""
    if not codes:
        return {}
    ph = ",".join("?" * len(codes))
    cat_ph = ",".join("?" * len(ANN_CATS))
    out = {}
    for code, cat, nd in con.execute(
            "SELECT code, cat, MAX(notice_date) FROM stock_ann"
            " WHERE tone='bad' AND cat IN (%s) AND code IN (%s)"
            " GROUP BY code, cat" % (cat_ph, ph),
            list(ANN_CATS) + list(codes)):
        if not nd:
            continue
        try:
            out.setdefault(code, {})[cat] = _date.fromisoformat(str(nd)).toordinal()
        except ValueError:
            pass
    return out


def _lhb_turnover(con, codes, as_of, days=LHB_WINDOW_DAYS):
    """{code: 近 days 个交易日内上榜时的最高换手率}；没上榜的票不出现在结果里。

    为什么取「最高」而不是「最近一次」：换手 30% 和换手 16% 的上榜意义差一个量级
    （-5.82% vs -3.04%），一次高换手上榜就足以定性，后来的低换手上榜不该把它冲淡。

    **无前视**：只查 trade_date <= as_of 的记录。龙虎榜盘后公布，as_of 当日上榜
    在收盘后才知情，用它给「明天要不要躲」打分是合规的（收益口径也是次日收盘起算）。
    """
    if not codes:
        return {}
    row = con.execute(
        "SELECT DISTINCT date FROM stock_indicators_daily WHERE date <= ?"
        " ORDER BY date DESC LIMIT 1 OFFSET ?", (as_of, max(0, days - 1))).fetchone()
    start = row[0] if row else as_of
    ph = ",".join("?" * len(codes))
    out = {}
    for code, tr in con.execute(
            "SELECT security_code, MAX(CAST(turnoverrate AS REAL)) FROM lhb_stocks"
            " WHERE security_code IN (%s) AND trade_date >= ? AND trade_date <= ?"
            " GROUP BY security_code" % ph,
            list(codes) + [start, as_of]):
        if tr is not None:
            out[code] = tr
    return out


def compute(con, codes, as_of=None):
    """算一批股票的风险标记。

    参数
      con   : sqlite3 连接（server 传 DB.conn）
      codes : 股票代码可迭代；空则返回 {}
      as_of : 收盘日（YYYY-MM-DD），默认取指标表最新交易日

    返回 {code: {score, level, flags, detail, trade_date, stale}}
      · level : high(≥7) / mid(≥2) / None(未触发，不亮灯)
      · flags : [{key, weight, ev}]，ev 是实证摘要，前端浮层直接展示
      · stale : True 表示该票最新一根K线早于 as_of（停牌/退市），标记仅供参考

    口径：全部只用「当日及以前」的数据，无未来函数。
    """
    codes = [c for c in dict.fromkeys(codes or []) if c]
    if not codes:
        return {}
    dates = _recent_dates(con, 3)
    if not dates:
        return {}
    as_of = as_of or dates[0]
    earliest = dates[-1]

    ph = ",".join("?" * len(codes))
    rows = con.execute(
        "SELECT date, code, close, volume_ratio, macdh"
        " FROM stock_indicators_daily"
        " WHERE code IN (%s) AND date >= ? ORDER BY code, date" % ph,
        list(codes) + [earliest]).fetchall()

    # 按 code 归集（升序，最后一个就是最新）
    hist = {}
    for d, code, close, vr, macdh in rows:
        hist.setdefault(code, []).append((d, close, vr, macdh))

    ann_ord = _ann_ordinals(con, codes)
    lhb_tr = _lhb_turnover(con, codes, as_of)
    as_of_ord = _date.fromisoformat(as_of).toordinal()
    out = {}
    for code in codes:
        h = hist.get(code) or []
        if not h:
            out[code] = {"score": 0, "level": None, "flags": [], "detail": "",
                         "trade_date": None, "stale": True}
            continue
        last = h[-1]
        d, close, vr, macdh = last
        # 当日涨跌幅：与前一交易日比；不足两日则无法判定，只留公告维度
        pct = None
        if len(h) >= 2 and h[-2][1]:
            pct = (close / h[-2][1] - 1.0) * 100.0
        prev_macdh = h[-2][3] if len(h) >= 2 else None

        hit = {}
        # 量比分档**互斥**：同一天不该因为「放量」拿两次分 —— 那不是两条独立证据，
        # 而是同一个现象的两种刻度，叠加只会虚增分数。
        if vr is not None:
            if vr >= VOL_EXTREME:
                hit["极端放量"] = True
            elif vr >= VOL_HEAVY:
                hit["放量"] = True
        # 跌幅分档同理互斥：跌 8% 必然也满足"跌 5%"，只能算一次。
        if pct is not None:
            if pct <= -7:
                hit["大跌"] = True
            elif pct <= -5:
                hit["当日下跌"] = True
        if macdh is not None and prev_macdh is not None and prev_macdh >= 0 and macdh < 0:
            hit["MACD死叉"] = True
        # 公告：二次复核后只留「股东减持」（人事变动全周期不显著，已删，见 docstring）
        cmap = ann_ord.get(code) or {}
        _o = cmap.get("股东减持")
        if _o is not None and 0 <= as_of_ord - _o <= ANN_WINDOW_DAYS:
            hit["股东减持"] = True
        # 龙虎榜：≥15% 即算强信号（15~20% 与 ≥20% 实测同量级，不再分档）
        tr = lhb_tr.get(code)
        if tr is not None and tr >= LHB_THRESHOLD:
            hit["龙虎榜高换手"] = True

        score = 0
        flags = []
        for key, w, ev in RULES:
            if hit.get(key):
                score += w
                flags.append({"key": key, "weight": w, "ev": ev})
        level = ("high" if score >= LEVEL_HIGH else
                 "mid" if score >= LEVEL_MID else None)
        detail = "、".join(f["key"] for f in flags) if level else ""
        out[code] = {"score": score, "level": level, "flags": flags,
                     "detail": detail, "trade_date": d, "stale": d != as_of}
    return out


def summary(con, codes):
    """给接口用的薄封装：附带口径说明，前端直接渲染，不用自己拼文案。"""
    res = compute(con, codes)
    n_hit = sum(1 for v in res.values() if v["level"])
    return {"ok": True, "items": res, "n": len(res), "n_hit": n_hit,
            "as_of": next((v["trade_date"] for v in res.values() if v["trade_date"]), None),
            "max_score": MAX_SCORE,       # 前端用它渲染「x/N」，权重再变也不用改前端
            "rules": [{"key": k, "weight": w, "ev": ev} for k, w, ev in RULES],
            "caliber": "风险分=%d条实证指标加权(满分%d)：龙虎榜/量比/跌幅/MACD + 股东减持公告；"
                       "全部由统一引擎在严格可执行口径下回测（d+1 收盘建仓），"
                       "无博主数据、无未来函数" % (len(RULES), MAX_SCORE)}


# ------------------------------------------------------------------ CLI 自检
def _main():
    import sys
    con = sqlite3.connect(DB_PATH)
    codes = sys.argv[1:] or [r[0] for r in con.execute(
        "SELECT code FROM stock_indicators_daily"
        " WHERE date=(SELECT MAX(date) FROM stock_indicators_daily) LIMIT 400")]
    res = compute(con, codes)
    print("as_of=%s  样本=%d  命中=%d"
          % (next((v["trade_date"] for v in res.values() if v["trade_date"]), "-"),
             len(res), sum(1 for v in res.values() if v["level"])))
    from collections import Counter
    print("分档:", dict(Counter(v["level"] for v in res.values())))
    top = sorted((v for v in res.values() if v["level"]),
                 key=lambda v: -v["score"])[:15]
    for v in top:
        print("  %2d分 %-5s %s" % (v["score"], v["level"], v["detail"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
