"""B站「技术·价值教学」方法卡片聚合（2026-10-11，王总需求）。

王总原话痛点：视频列表「谁发的/什么时候发的/忽悠人的描述没有用」——
要的是：① 每个方法起一个短名 + 一句话说人话；② 回测的准确率/回报率看得见；
③ 卡片一格一方法，让他能判断「这方法好不好使」。

口径（诚实声明，会随 payload.basis 下发到前端展示）：
1. 方法归类 = 关键词词典命中标题（离线规则，非 LLM、非语义理解）；
   一条视频可同时命中多个方法；同族指标（family）只归入顺序最前的方法。
   未命中任何方法的视频计入 unmatched，不硬塞。
2. 卡片上的「准确率/回报率」不是对 B站视频本身的验证——是该方法映射到的
   **可量化规则**在 strategy_sim_window（window_label='mid'，近两年、库内候选池）
   的真实回测结果（每笔交易胜率 / 区间总收益 / 评级）。
   映射不到可量化规则的方法（如财报排雷、红利收息）如实标「未回测」。
3. 本模块零写库、零网络，只读 sqlite 只读连接即可。
"""
from __future__ import annotations

# 86 条回测策略的中文名（strategy_data.STRATEGIES 在项目根，server 启动时已在 sys.path）
try:
    from strategy_data import STRATEGIES as _SD_STRATEGIES
except Exception:  # pragma: no cover - 容错降级：拿不到名就用 key
    _SD_STRATEGIES = []

_STRAT_NAME = {}
for _s in _SD_STRATEGIES:
    if isinstance(_s, dict) and _s.get("key"):
        _STRAT_NAME[_s["key"]] = _s.get("name") or _s["key"]


class _M:
    """方法定义：kws 全部小写（匹配时对标题 lower()）；family 同族只归第一个命中的。"""
    __slots__ = ("key", "name", "summary", "kind", "family", "kws", "strat")

    def __init__(self, key, name, summary, kind, family, kws, strat):
        self.key, self.name, self.summary = key, name, summary
        self.kind, self.family, self.kws, self.strat = kind, family, [k.lower() for k in kws], strat


# 顺序 = 归类优先级（越具体的放越前）。strat 里的 key 必须真实存在于 strategy_sim_window。
METHODS = [
    # ---------- 技术面 ----------
    _M("macd_div", "MACD背离法", "价格新高/新低但 MACD 不跟——顶背离减仓、底背离抄底，抓趋势拐点。",
       "技术面", "macd", ["macd背离", "背离战法", "顶背离", "底背离", "背离"],
       ["macd_divergence_bull"]),
    _M("macd_gold", "MACD金叉法", "MACD 金叉/红绿柱切换做买卖点，低位金叉优先。",
       "技术面", "macd", ["macd", "dif", "dea"],
       ["macd_gold_low", "macd_gold_any", "macd_hist_turn", "macd_zero_axis_gold", "macd_double_gold"]),
    _M("kdj", "KDJ战法", "KDJ 低位金叉买入、J 值超买超卖做短差。",
       "技术面", "kdj", ["kdj", "随机指标", "j值"],
       ["kdj_j_negative", "kdj_strong_gold", "kdj_low_gold"]),
    _M("rsi", "RSI战法", "RSI 超卖反弹、强弱线交叉抓拐点。",
       "技术面", "rsi", ["rsi", "相对强弱"],
       ["rsi_reversal", "rsi2_extreme", "rsi_cross_mid"]),
    _M("boll", "布林带战法", "价格触布林下轨反弹、收口待突破，做区间与变盘。",
       "技术面", "boll", ["布林", "boll"],
       ["boll_lower_double", "boll_lower", "boll_squeeze", "boll_squeeze_breakout"]),
    _M("osc", "超卖指标法", "威廉/CCI 等超卖指标到底部区域后的反弹机会。",
       "技术面", "osc", ["威廉", "cci", "wr指标"],
       ["williams_oversold", "williams_cross", "cci_oversold", "cci_zero_cross"]),
    _M("ma_gold", "均线金叉法", "短中期均线金叉/多头排列顺势买入，双均线定趋势。",
       "技术面", "ma", ["均线", "多头排列", "ma5", "ma10", "ma20", "ma60", "ema", "金叉"],
       ["ma_cross_5_20", "ma_cross_10_30", "ma_cross_10_60", "ma_cross_20_60",
        "ma_cross_second_gold", "ema_cross_12_26", "ema_cross_20_50",
        "ma_bull_align", "ma_bull_align_strict"]),
    _M("pullback", "回踩买入法", "上升趋势中回踩均线（MA10/20/60）企稳再上车，不追高。",
       "技术面", "pullback", ["回踩", "回调买入", "缩量回调"],
       ["pullback_ma10", "pullback_ma20", "pullback_ma60"]),
    _M("breakout", "突破新高法", "突破前高/平台/箱体时买入，创 N 日新高跟趋势（海龟打法）。",
       "技术面", "breakout", ["突破", "新高", "平台", "海龟", "箱体", "楔形", "收敛", "整理"],
       ["break_10d_high", "break_20d_high", "break_60d_high", "break_120d_high",
        "turtle_donchian_20_10", "turtle_donchian_55_20", "box_breakout",
        "box_breakout_retest", "wedge_breakout", "nr7_breakout", "low_vol_breakout"]),
    _M("gap", "缺口战法", "跳空缺口不回补是强势信号，缺口回踩/回补做短差。",
       "技术面", "gap", ["缺口", "跳空"],
       ["gap_up_break", "gap_down_revert", "gap_hold_unfilled"]),
    _M("limitup", "涨停打板法", "首板/连板/涨停复制的短线情绪打法，波动大纪律要求高。",
       "技术面", "limitup", ["涨停", "连板", "首板", "打板", "封板", "涨停复制"],
       ["consecutive_limit_up", "first_limit_up"]),
    _M("volume", "量价战法", "放量突破、缩量回踩、量价齐升/背离，用量能验证价格。",
       "技术面", "vol", ["放量", "缩量", "倍量", "地量", "量价", "量能", "成交量", "obv", "能量潮"],
       ["volume_breakout", "vol_surge_3d", "vol_ma_cross", "vol_ma_expand",
        "vol_ratio_break", "vol_price_divergence", "obv_ma_cross", "obv_new_high"]),
    _M("pattern", "K线形态法", "早晨之星/锤头/红三兵/W底/头肩底等经典形态抄底与判顶。",
       "技术面", "pattern", ["早晨之星", "启明星", "锤头", "锤子线", "吞没", "红三兵",
                          "w底", "双底", "头肩底", "仙人指路", "十字星", "n型"],
       ["morning_star", "hammer_bottom", "bullish_engulf", "three_white_soldiers",
        "w_bottom", "head_shoulder_bottom"]),
    _M("revert", "超跌反弹法", "连续大跌/恐慌杀跌后的均值回归反弹，左侧埋伏。",
       "技术面", "revert", ["超跌", "超卖", "抄底", "恐慌", "黄金坑", "连跌", "暴跌后"],
       ["mean_revert_ma20", "mean_revert_ma60", "zscore_revert_20", "bias_revert_6",
        "consecutive_down_4"]),
    _M("shrink", "洗盘识别法", "缩量回踩/震仓洗盘与真出货的区分，洗盘结束再进。",
       "技术面", "shrink", ["洗盘", "震仓", "甩下车", "骗筹码"],
       ["shrink_pullback", "shrink_bottom_reversal"]),
    _M("exit", "出货逃顶法", "高位放量滞涨/诱多形态识别，及时落袋逃顶。",
       "技术面", "exit", ["出货", "逃顶", "诱多", "见顶", "顶部", "砸盘"],
       []),
    _M("stop", "止损纪律法", "固定止损/移动止盈/时间止损——先保命再谈赚钱。",
       "技术面", "stop", ["止损", "止盈", "风控", "纪律"],
       ["fixed_stop_loss", "atr_trailing_stop", "trailing_drawdown_stop", "time_stop_20d"]),
    _M("timing", "大盘择时法", "看大盘环境决定仓位轻重：牛市满仓干、弱市空仓等。",
       "技术面", "timing", ["大盘", "仓位", "择时", "满仓", "空仓", "牛市", "熊市"],
       ["breadth_timing", "vol_regime_timing", "hist_vol_filter",
        "index_ma20_timing", "index_ma250_timing"]),
    _M("leader", "龙头强势法", "只做板块龙头/强势股/主线人气股，强者恒强。",
       "技术面", "leader", ["龙头", "强势股", "主线", "领涨", "人气股"],
       ["momentum_20", "momentum_60", "momentum_accel", "momentum_risk_adj",
        "relative_strength_20", "price_above_ma250"]),
    # ---------- 价值面 ----------
    _M("val_cheap", "低估值选股法", "PE/PB 处于历史低位、破净股里捡便宜，等价值回归。",
       "价值面", "val_cheap", ["低估", "估值", "市盈率", "pe", "便宜", "破净", "pb", "百分位"],
       []),
    _M("val_fund", "财报排雷法", "看财报识别应收/票据/商誉/审计问题，先避雷再谈收益。",
       "价值面", "val_fund", ["财报", "避雷", "暴雷", "应收", "票据", "商誉", "退市", "审计", "造假"],
       []),
    _M("val_quality", "赚钱能力选股法", "ROE/毛利率/现金流对比，只买赚钱能力强的公司。",
       "价值面", "val_quality", ["赚钱能力", "roe", "毛利率", "净利率", "护城河", "现金流"],
       []),
    _M("val_div", "红利收息法", "高股息/持续分红的公司收息等增值，波动小拿得住。",
       "价值面", "val_div", ["红利", "股息", "分红", "高股息"],
       []),
    _M("val_cycle", "周期库存法", "行业去库存/产能出清到拐点时左侧布局，等周期反转。",
       "价值面", "val_cycle", ["去库存", "库存周期", "猪周期", "产能", "供给侧", "拐点"],
       []),
]


def _strat_rows(conn):
    """strategy_sim_window·mid（近两年主窗口）→ {key: {...}}；拿不到就空（前端显未回测）。"""
    out = {}
    try:
        cur = conn.execute(
            "SELECT strategy_key, win_rate, total_return, annual_return, rating, trades, rank "
            "FROM strategy_sim_window WHERE window_label='mid'")
        cols = [d[0] for d in cur.description]
        for row in cur.fetchall():
            d = dict(zip(cols, row))
            out[d["strategy_key"]] = {
                "key": d["strategy_key"],
                "name": _STRAT_NAME.get(d["strategy_key"], d["strategy_key"]),
                "win_rate": d["win_rate"],
                "total_return": d["total_return"],
                "annual_return": d["annual_return"],
                "rating": d["rating"],
                "trades": d["trades"],
                "rank": d["rank"],
            }
    except Exception:
        pass
    return out


def methods_payload(conn, cat: str = "technical,value",
                    per_method_videos: int = 40) -> dict:
    """聚合方法卡片数据。conn 用只读或业务连接均可，本函数零写库。"""
    cats = [c.strip() for c in (cat or "").split(",") if c.strip()]
    rows = []
    if cats:
        ph = ",".join("?" * len(cats))
        rows = conn.execute(
            f"SELECT cat, blogger, bvid, title, vdate, url FROM bili_pile "
            f"WHERE cat IN ({ph}) ORDER BY vdate DESC", cats).fetchall()
    colnames = ["cat", "blogger", "bvid", "title", "vdate", "url"]
    vids = [dict(zip(colnames, r)) for r in rows]

    sim = _strat_rows(conn)
    buckets = {m.key: [] for m in METHODS}
    for v in vids:
        t = (v.get("title") or "").lower()
        if not t:
            continue
        seen_families = set()
        for m in METHODS:
            if m.family in seen_families:
                continue
            if any(kw in t for kw in m.kws):
                buckets[m.key].append(v)
                seen_families.add(m.family)

    matched_n = set()
    out_methods = []
    for m in METHODS:
        vs = buckets[m.key]
        for v in vs:
            matched_n.add(id(v))
        bloggers = {}
        for v in vs:
            b = v.get("blogger") or "未知"
            bloggers[b] = bloggers.get(b, 0) + 1
        top = sorted(bloggers.items(), key=lambda x: -x[1])[:3]
        strats = [sim[k] for k in m.strat if k in sim]
        # 卡片头数字 = 映射规则里 rank 最好（回测综合排名最靠前）的一条
        best = min(strats, key=lambda s: s.get("rank") or 9999) if strats else None
        last_date = max((v.get("vdate") or "") for v in vs) if vs else ""
        out_methods.append({
            "key": m.key,
            "name": m.name,
            "summary": m.summary,
            "kind": m.kind,
            "n": len(vs),
            "bloggers_n": len(bloggers),
            "top_bloggers": [b[0] for b in top],
            "last_date": last_date,
            "mapped": bool(strats),
            "best": best,
            "strategies": sorted(strats, key=lambda s: s.get("rank") or 9999),
            "samples": [
                {"title": v.get("title"), "url": v.get("url"), "vdate": v.get("vdate")}
                for v in vs[:per_method_videos]
            ],
        })

    # 排序：已回测（按最佳规则回测排名）在前，未回测按视频量
    out_methods.sort(key=lambda x: (
        0 if x["mapped"] else 1,
        (x["best"] or {}).get("rank") or 9999,
        -x["n"],
    ))

    return {
        "cat": cat,
        "total": len(vids),
        "unmatched": len(vids) - len(matched_n),
        "methods": out_methods,
        "sim_window": "mid",
        "basis": [
            "方法归类=关键词词典命中视频标题（离线规则，非语义理解）；一条视频可命中多个方法，同族指标只归第一个命中的方法。",
            "「准确率/回报率」不是对B站视频的验证——是该方�法映射到的可量化规则在策略回测引擎的近两年真实回测（每笔交易胜率/区间总收益/评级，库内候选池）。",
            "标「未回测」的方法＝暂时没有可量化的选股规则，不编造数字。",
        ],
    }
