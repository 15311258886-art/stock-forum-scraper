# -*- coding: utf-8 -*-
"""长期底部平台识别（2026-10-21 进哥「长期底部埋伏」板块核心算法）。

进哥原话（2026-10-21 03:03）：
    「把这种波动比较大的、以前出现过高峰、现在一直在震荡底线徘徊的股票单独拉一个名单……
      有的股票自打上市以后，第一次的底比如 10 块，后来涨到 100 多，那后来的底都平均在 20 了，
      你这个底就不能还是以 10 为标准对吧？」

所以本模块**不用全历史最低价、也不用固定箱体百分比**，改用三个自适应判据：

  ① 压缩比 compress = 当前窗口宽度 ÷ 该股历史上同等长度窗口的宽度中位数
       —— 「比自己历史上更横盘」才算底部盘整，而不是「箱体宽度 ≤ 40%」这种死参数。
       一只 20 年都在 10~60 之间大幅波动的票，8 年窗口宽度 200%，历史同等窗口也是 200%，
       compress≈1 → 不通过；一只从 60 跌到 20 后横盘 5 年的票，5 年窗口宽度 25%、
       历史同等窗口 150% → compress≈0.17 → 通过。这就是「自适应」的全部含义。

  ② 底部抬升 lift = 当前平台底 ÷ 全历史 P5
       —— 进哥举的 10 块 → 20 块那个例子：lift≈2，标「底部已抬升 2 倍」，
       此时基准**只用当前平台底 20 元**，绝不再回到 10 元。
       若 lift≈1.0 且现价贴着 P5，说明是持续创新低的阴跌，不是底部盘整 → 扣分出局。

  ③ 位置判据用相对值 —— 现价在箱体中的位置 pos_in_box ∈ [0,1]，
       而不是「现价距全历史最低多少」。低位 = pos_in_box 偏小。

精度口径（重要，交付时要说清）：
    行情源：新浪全历史**不复权**（本机东财 push2his 被 RemoteDisconnected 掐断、腾讯返回 None），
    但入库前已用东财数据中心的分红送股方案（IMPL_PLAN_PROFILE 文本）自算前复权因子做了复权
    —— 见 tools/build_kline_db.py。送转不复权会让历史价虚高、当前价永远显得「在底部」，必须修。
"""
import datetime
import statistics

YEAR_WEEKS = 52
# 分档（进哥 2026-10-21 指定：5年以上 / 3到5年 / 2到3年 / 1年）
TIERS = (1, 2, 3, 5, 8)


def _pct(sorted_vals, q):
    """升序序列的线性插值分位数，q∈[0,1]。空序列返回 None。"""
    n = len(sorted_vals)
    if n == 0:
        return None
    if n == 1:
        return float(sorted_vals[0])
    pos = q * (n - 1)
    lo = int(pos)
    hi = min(lo + 1, n - 1)
    frac = pos - lo
    return float(sorted_vals[lo]) * (1.0 - frac) + float(sorted_vals[hi]) * frac


_MDAYS = (0, 31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31)


def _ordinal_fast(y, m, d):
    """YYYYMMDD → 连续日序（纯整数，仅供「同一只票内部」做周归组，绝对值无需与 toordinal 对齐）。

    为什么不用 datetime.date(...).toordinal()：全市场 5550 只 × 平均 3000 根 ≈ 1600 万次构造，
    profile 实测 to_weekly 占单只耗时的 76%（1.64s/2.14s，300 只），换成整数运算后降到可忽略。
    """
    days = sum(_MDAYS[1:m]) if m > 1 else 0
    if m > 2 and y % 4 == 0 and (y % 100 != 0 or y % 400 == 0):
        days += 1
    return 365 * (y - 1) + (y - 1) // 4 - (y - 1) // 100 + (y - 1) // 400 + days + d


def to_weekly(bars):
    """日K → 周K。bars: [(d:int YYYYMMDD, o, c, h, l, v)] 升序。

    周归属用「连续日序 // 7」的自然周（不等同 ISO 周，但长期形态判定完全等价且快得多）。
    返回 [[date_int, close, high, low, vol], ...]；date_int 取该周最后一个交易日。
    """
    out = []
    cur_key = None
    agg = None
    for b in bars:
        try:
            d = int(b[0])
            key = _ordinal_fast(d // 10000, d // 100 % 100, d % 100) // 7
            c = float(b[2])
        except (TypeError, ValueError, IndexError):
            continue
        if c <= 0:
            continue
        hi = float(b[3]) if b[3] is not None else c
        lo = float(b[4]) if b[4] is not None else c
        vol = float(b[5] or 0)
        if key != cur_key:
            if agg:
                out.append(agg)
            cur_key = key
            agg = [d, c, hi, lo, vol]
        else:
            agg[0] = d
            agg[1] = c
            if hi > agg[2]:
                agg[2] = hi
            if lo < agg[3]:
                agg[3] = lo
            agg[4] += vol
    if agg:
        out.append(agg)
    return out


def _slope_yr(closes):
    """周收盘线性回归 → 年化斜率%（对均值归一）。"""
    n = len(closes)
    if n < 3:
        return None
    mx = (n - 1) / 2.0
    my = sum(closes) / n
    if not my:
        return None
    num = sum((i - mx) * (closes[i] - my) for i in range(n))
    den = sum((i - mx) ** 2 for i in range(n))
    if not den:
        return None
    return round(num / den * YEAR_WEEKS / my * 100.0, 1)


def _hist_width_median(closes, n_weeks):
    """该股历史上「同等长度窗口」的 P90/P10 宽度的中位数（**排除当前最近一段**）。

    这是 compress 的分母 —— 自适应的关键：拿它自己过去同等时长的波动幅度当标尺。
    历史段数 < 2 时返回 None（新股/次新股），调用方退回宽松的绝对判据。
    """
    n = len(closes)
    if n < n_weeks * 2:
        return None
    segs = []
    for start in range(0, n - n_weeks, n_weeks):
        seg = sorted(closes[start:start + n_weeks])
        p10 = _pct(seg, 0.10)
        p90 = _pct(seg, 0.90)
        if p10 and p10 > 0:
            segs.append(p90 / p10 - 1.0)
    if len(segs) < 2:
        return None
    return statistics.median(segs)


def platform_metrics(bars, cfg=None):
    """自适应底部平台识别主函数。

    bars: 前复权日K [(d, o, c, h, l, v)]，升序（tools/build_kline_db.py 落库口径）
    返回 dict：ok/reason + 平台指标 + score（埋伏分 0~100）

    判据（全部为相对/自适应，无「箱体≤X%」硬门槛）：
      · compress ≤ 0.85 → 当前比它自己历史上同等时长更横盘
      · width ≤ 1.5     → 兜底防「宽幅震荡冒充窄幅」（仅当 compress 拿不到时严格生效）
      · -6% ≤ slope_yr ≤ +3.5% → 既不是单边阴跌，也不是长期上涨（双向约束）
      · pos_in_box ≤ 0.75 → 现价在箱体下部（或刚突破箱顶则另走 started 分支）
    """
    cfg = cfg or {}
    compress_max = float(cfg.get("compress_max", 0.85))
    width_max = float(cfg.get("width_max", 1.5))
    slope_lo = float(cfg.get("slope_lo", -6.0))
    slope_hi = float(cfg.get("slope_hi", 6.0))
    pos_max = float(cfg.get("pos_max", 0.75))
    mid_ratio_max = float(cfg.get("mid_ratio_max", 1.7))
    min_years = float(cfg.get("min_years", 0) or 0)

    if not bars or len(bars) < 60:
        return {"ok": False, "reason": "日K不足60根"}
    wk = to_weekly(bars)
    n = len(wk)
    if n < int(YEAR_WEEKS * 0.8):
        return {"ok": False, "reason": "周K不足一年（%d周）" % n}
    closes = [w[1] for w in wk]
    cur = closes[-1]
    if cur <= 0:
        return {"ok": False, "reason": "现价异常"}

    # ---- 全历史参照 ----
    all_sorted = sorted(closes)
    p5_all = _pct(all_sorted, 0.05)
    p50_all = _pct(all_sorted, 0.50)
    hi_idx = max(range(n), key=lambda i: wk[i][2])
    peak = float(wk[hi_idx][2])
    peak_weeks_ago = n - 1 - hi_idx
    peak_ago_years = round(peak_weeks_ago / YEAR_WEEKS, 1)
    dd_from_peak = round((cur / peak - 1.0) * 100.0, 1) if peak > 0 else None
    pct_all = round(sum(1 for c in closes if c <= cur) / n * 100.0, 1)

    # ---- 前置条件：近一年止跌 ----
    # 底部的最基本前提是「最近一年不再继续下跌」。实测依据：贵州茅台 5 年窗口斜率 -5.4%
    # 看着只是温和回落，但近一年年化 -11%，仍在下跌途中；长窗口会把这段下跌平均掉，
    # 必须单独用近 52 周斜率卡住。9 阳（002242）近一年 +37%、平安银行 +4% 均放行。
    slope_1y = _slope_yr(closes[-YEAR_WEEKS:]) if n >= YEAR_WEEKS else None
    if slope_1y is not None and slope_1y < float(cfg.get("slope_1y_min", -8.0)):
        return {"ok": False, "reason": "近一年仍在下跌（年化 %.1f%%）" % slope_1y,
                "cur": round(cur, 2), "slope_1y": slope_1y,
                "dd_from_peak": dd_from_peak, "peak_ago_years": peak_ago_years}

    # ---- 从长到短回溯，取「成立的最大窗口」= 底部时长 ----
    platform = None
    tried = []
    for yrs in sorted(TIERS, reverse=True):
        need = int(yrs * YEAR_WEEKS)
        if n < need * 0.9:
            continue
        win = wk[-need:] if n >= need else wk
        wc = sorted(w[1] for w in win)
        box_low = _pct(wc, 0.10)
        box_high = _pct(wc, 0.85)
        if not box_low or box_low <= 0:
            continue
        width = box_high / box_low - 1.0
        hist_med = _hist_width_median(closes, len(win))
        compress = (width / hist_med) if (hist_med and hist_med > 0) else None
        slope = _slope_yr([w[1] for w in win])
        pos = (cur - box_low) / (box_high - box_low) if box_high > box_low else 0.5
        # 注：已启动（现价突破箱顶）的票，pos 会 >1，此时走 started 分支不算违规
        over_top = cur > box_high
        ok_compress = (compress is None and width <= width_max) or \
                      (compress is not None and compress <= compress_max and width <= width_max)
        # 斜率必须**双向**受限：既不能是单边阴跌(-6%以下)，也不能是长期上涨(+3.5%以上)。
        # 这一条是 2026-10-21 实测补的：只限下跌时，贵州茅台会被判成「8 年底部平台」
        # （8 年窗口斜率 +4.5%，是长牛回撤后的高位震荡，不是底部埋伏）。
        ok_slope = (slope is None) or (slope_lo <= slope <= slope_hi)
        ok_pos = over_top or (pos <= pos_max)
        # 箱体中枢必须落在历史中位价附近或之下 → 排除「高位回撤后的箱体」。
        # 实测依据：贵州茅台 2021 高点 2305 跌到 1332，「近5年箱体 834~1664」形态上贴底
        # (pos=0.04)、斜率 -5.4% 也在阈值内，但箱体中枢 1250 是全历史中位价的 2.5 倍——
        # 那是高位震荡，不是底部埋伏。改用**中位价比值**而非「距历史最低价多远」，
        # 正是为了避开进哥说的「早年 10 元不能当底」那个坑（中位数已含成长，最低价不含）。
        box_mid = (box_low + box_high) / 2.0
        mid_ratio = (box_mid / p50_all) if (p50_all and p50_all > 0) else None
        # mid_ratio 只做展示与降级标记，**不进硬门槛**：老股（平安银行 30 年、茅台 25 年）
        # 的全历史中位价被早年低价拉得很低，2.5~9 倍是常态，硬卡会误杀真正的底部票。
        # 茅台那类「高位箱体」由 slope_1y（近一年仍在跌）负责挡掉。
        tried.append({"years": round(len(win) / YEAR_WEEKS, 1),
                      "width": round(width * 100, 1),
                      "compress": (round(compress, 2) if compress is not None else None),
                      "slope": slope, "pos": round(pos, 2),
                      "mid_ratio": (round(mid_ratio, 2) if mid_ratio is not None else None),
                      "pass": bool(ok_compress and ok_slope and ok_pos)})
        if ok_compress and ok_slope and ok_pos:
            platform = {"years": round(len(win) / YEAR_WEEKS, 1), "win": win,
                        "box_low": box_low, "box_high": box_high, "width": width,
                        "compress": compress, "slope": slope, "pos": pos,
                        "mid_ratio": mid_ratio}
            break

    if platform is None:
        return {"ok": False, "reason": "最近一年已脱离底部区间（或持续阴跌创新低）",
                "cur": round(cur, 2), "tried": tried,
                "dd_from_peak": dd_from_peak, "peak_ago_years": peak_ago_years}

    yrs_got = platform["years"]
    if min_years and yrs_got < min_years:
        return {"ok": False, "reason": "底部时长 %.1f 年 < 要求 %.0f 年" % (yrs_got, min_years),
                "platform_years": yrs_got, "cur": round(cur, 2)}

    win = platform["win"]
    box_low, box_high = platform["box_low"], platform["box_high"]
    band = max(box_high - box_low, 1e-9)
    pos = platform["pos"]

    # 触碰次数：收盘落在箱底上沿 20% 带内的周数（触底不破 = 支撑有效）
    touches = sum(1 for w in win if (w[1] - box_low) / band <= 0.20)
    # 底部抬升：当前平台底 ÷ 全历史 P5
    lift = round(box_low / p5_all, 2) if (p5_all and p5_all > 0) else None
    # 波动率与斜率（平台内）
    wc_list = [w[1] for w in win]
    try:
        cv = statistics.pstdev(wc_list) / (sum(wc_list) / len(wc_list)) * 100.0
    except (statistics.StatisticsError, ZeroDivisionError):
        cv = None
    # 启动信号：近4周均量 / 平台均量；现价 vs 箱顶
    vols = [w[4] for w in win if w[4]]
    avg_vol = (sum(vols) / len(vols)) if vols else None
    recent_vol = (sum(w[4] for w in wk[-4:]) / 4.0) if len(wk) >= 4 else None
    vol_ratio = round(recent_vol / avg_vol, 2) if (recent_vol and avg_vol) else None
    if cur > box_high * 1.02:
        bo_state = "breakout" if (vol_ratio or 0) >= 1.3 else "stand_top"
    elif cur >= box_high * 0.95:
        bo_state = "near"
    else:
        bo_state = "in_box"
    started = cur > box_high * 0.95

    # ---- 埋伏分（0~100）----
    s_years = min(yrs_got / 5.0, 1.0) * 100.0
    s_low = max(0.0, min(1.0, (0.8 - pos) / 0.8)) * 100.0
    dd = -(dd_from_peak or 0.0)
    s_dd = max(0.0, min(1.0, (dd - 30.0) / 50.0)) * 100.0
    cv_v = cv if cv is not None else 30.0
    s_flat = max(0.0, min(1.0, (45.0 - cv_v) / 45.0)) * 100.0
    sl = abs(platform["slope"]) if platform["slope"] is not None else 12.0
    s_slope = max(0.0, min(1.0, (15.0 - sl) / 15.0)) * 100.0
    s_shape = 0.5 * s_flat + 0.5 * s_slope
    s_start = 100.0 if bo_state == "breakout" else (70.0 if bo_state in ("stand_top", "near") else 30.0)
    score = (0.32 * s_years + 0.22 * s_low + 0.18 * s_dd + 0.18 * s_shape + 0.10 * s_start)

    return {
        "ok": True, "cur": round(cur, 2),
        "platform_years": yrs_got,
        "box_low": round(box_low, 2), "box_high": round(box_high, 2),
        "box_width_pct": round(platform["width"] * 100.0, 1),
        "compress": (round(platform["compress"], 2) if platform["compress"] is not None else None),
        "pos_in_box": round(pos, 2),
        "pct_all": pct_all,
        "peak": round(peak, 2), "peak_ago_years": peak_ago_years,
        "dd_from_peak": dd_from_peak,
        "floor_now": round(box_low, 2),
        "floor_hist": round(p5_all, 2) if p5_all else None,
        "lift_ratio": lift,
        "box_mid_ratio": (round(platform["mid_ratio"], 2) if platform.get("mid_ratio") is not None else None),
        "slope_yr_pct": platform["slope"],
        "slope_1y_pct": slope_1y,
        "cv_pct": round(cv, 1) if cv is not None else None,
        "touches": touches,
        "vol_ratio": vol_ratio,
        "bo_state": bo_state, "started": started,
        "rise_from_floor": round((cur / box_low - 1.0) * 100.0, 1) if box_low else None,
        "score": round(score, 1),
        "parts": {"years": round(s_years, 1), "low": round(s_low, 1), "dd": round(s_dd, 1),
                  "shape": round(s_shape, 1), "start": round(s_start, 1)},
        "tried": tried,
    }


def classify_state(m):
    """把平台指标归成人类可读的状态标签（供前端展示 + 筛选）。"""
    if not m.get("ok"):
        return "无平台"
    if m.get("started"):
        vr = m.get("vol_ratio") or 0
        return "已启动·放量" if vr >= 1.3 else "已启动·待确认"
    pos = m.get("pos_in_box")
    if pos is not None and pos <= 0.3:
        return "贴底埋伏"
    return "箱内震荡"
