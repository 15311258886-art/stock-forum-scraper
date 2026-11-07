# -*- coding: utf-8 -*-
"""全市场箱体波谷扫描（universe_box v1，2026-11-02 圆桌后落地）。

## 为什么要有这个模块

中线板块（`/api/screener_lowbox`）此前的扫描池是「关注池 ∪ 涨幅 Top30」再截断到 64 只。
但箱体类指标全是**分位数类**指标（当前价在区间的百分位、变异系数、历史中位数窗口）——
在 64 个样本上算出来的"分位"没有统计意义，排出来的名次是假的。

进哥原话：「中线就是你找 5000 多只股票里边，属于那种总是在一段时间之内有周期性这种
波峰和波谷的。只要处在波谷期间之内，那就应该一直显示，然后评分越接近波谷、
在波谷震荡的时间越长，你应该更加关注、打分会更高。」

所以本模块做三件事：
  ① **两级扫描**：一级纯本地 K 线跑全市场（5428 只，实测 <1s），二级只对粗筛结果算结构指标。
     这样绕开"逐只外部接口"的成本，把池子从 64 只扩到全市场而不炸耗时。
  ② **波谷期间持续显示**：只要现价落在箱体下 1/3 就进榜（不因为当天涨 1% 就被踢出去）。
  ③ **谷底驻留越久分越高**：`dwell_ratio` 是近 250 日落在箱体下 1/3 的天数占比，
     `dwell_max_run` 是最长连续停留。打分里驻留占 35%，比"位置"权重还高 ——
     这是进哥"震荡时间越长打分越高"的直接落地。

## 与 lowbox 的关系（不是替代，是并列）

`api_screener_lowbox` 带有基本面/估值/国资加分（要逐只外部接口，成本高、池子必须小）。
本模块**只做形态与驻留**，不碰外部接口，所以能跑全市场。两者定位不同：
  · lowbox   = 小池子、深加工（基本面 + 估值 + 长窗 worth）
  · universe_box = 全市场、只筛形态（找"哪只票正在谷底磨"）
后续 P1 再把基本面闸门接成第二遍过滤，而不是一开始就逐只取数。

## 口径与已知边界

- 成交额近似：本地 K 线只有成交量 `v`（股），没有成交额。这里用 `close × v` 近似，
  单位是"元"，用于流动性门槛（≥3000 万）。**这是近似值**，与真实成交额有偏差，
  但用于"剔除僵尸股"这一目的足够；不要拿它做精确金额统计。
- 箱体固定 750 日窗口（约 3 年），随日期滚动 —— 这是"当前箱体"，不是历史固定锚。
- 上市不足 750 日的票不入池（算不出 3 年箱体），在 `skipped` 里如实计数。

## 第二把尺：动态箱体（布林通道，2026-11-03 D71）

静态箱体的高低点是**历史极值**，所以它有一个结构性缺陷（`docs/产品内核演进.md` 第 1 节已记录）：
一次极端行情把 `box_high` 拉上去之后，这只票此后的 `pos` 会长期贴着 0 ——
系统认为它"一直在箱底"，可它其实只是"比三年前那根天量 K 线低"。
箱体被永久拉宽，位置分失去区分度，这就是"中线名单很多天不变"的机制性原因之一。

布林通道是**动态**标尺：中轨是 20 日均线、带宽随 σ 自动缩放，波动率收缩时收窄、扩张时放宽，
不会被某一天的历史极值永久钉死。作为第二把尺与静态那套**并行**存在（不是替换）：
`BOX_MODE` 默认 `static`，线上输出一个字都不变；切到 `blend` / `dyn` 才启用。

选这个参数组不是拍的，有实证（`tools/exp_finrl_indicators.py`，见 DECISIONS.md D69-勘误）：
300 只票 / 520 日训练 + 250 日样本外，`boll_lb/close−1` 的 IC 训练期 +0.0614、测试期 +0.0626，
`布林%B` 的 IC 训练期 −0.0535、测试期 −0.0546，两维符号一致。
方向是「越贴下轨，后续收益越高」—— 与静态箱体的"越贴箱底越好"同向，差别只在标尺是否在动。
"""
from __future__ import annotations

import logging
import os
import statistics

log = logging.getLogger(__name__)

# ---- 一级粗筛门槛（纯本地，全市场跑）----
MIN_BARS = 750              # 至少 3 年日线：不足则箱体无意义
MIN_AMOUNT_AVG = 3.0e7      # 近 250 日均成交额 ≥3000 万（close×v 近似）
BOX_WIN = 750               # 箱体窗口（交易日）
PCT_MAX = 30.0              # 750 日价格分位上限（只保留低位）
CV_MAX = 40.0               # 变异系数上限（越低越横盘；超过 40 不是箱体是趋势）
SLOPE_MAX = 18.0            # 年化斜率绝对值上限（%）；超过说明单边，不是箱体

# ---- 二级结构门槛 ----
TOUCH_MIN = 2               # 触及箱顶/箱底次数下限（箱体有效性）
DWELL_BOTTOM_RATIO = 1.0 / 3.0   # 箱体下 1/3 视为"谷底区"
DWELL_LOOKBACK = 250        # 驻留统计回看窗口

# ---- 打分权重（合计 1.0；驻留最重，对应进哥"震荡越久分越高"）----
W_POS = 0.30                # 位置接近度
W_DWELL = 0.35              # 谷底驻留
W_REGULAR = 0.20            # 箱体规整度（CV + 斜率）
W_TOUCH = 0.15              # 触及次数（上下沿被反复验证）

# ---- 动态箱体参数（布林通道；2026-11-03 D71）----
# 口径照 stockstats / FinRL 的 boll 指标（`tools/exp_finrl_indicators.py` 里实测过的那套），
# 不另造参数：中轨 20 日、上下轨 ±2σ，σ 用总体标准差（除以 N，与本文件 `cv` 同口径）。
BOLL_WIN = 20               # 中轨窗口（交易日）
BOLL_K = 2.0                # 上下轨 = 中轨 ± K×σ
BOLL_RANK_WIN = 250         # %B 分位的回看窗口（约一个自然年）
W_POS_DYN = 0.5             # blend 模式下**动态分位分**在位置分里的占比，其余归静态位置分

# ---- 模式开关 ----
# 为什么默认 static 而不是默认新的：这次改的是"尺子"，尺子一换，全部分数、全部排名都平移。
# 线上用户没有要求换，就不能让他看到的东西变 —— 所以新模式必须由人显式打开才生效，
# 且随时能一句话退回（改环境变量重启即可，不涉及数据迁移）。
BOX_MODE_STATIC = "static"
BOX_MODES = ("static", "blend", "dyn")


def _norm_mode(m) -> str:
    """把外部传入的模式串收敛成三者之一；认不出来一律退回 static。

    为什么要"猜不出来就用旧的"：模式串来自环境变量，写错一个字母（比如 `blended`）
    不该让整张中线表换一把尺 —— 那是一次静默的全局口径变化，比报错更难查。
    所以这里记一条 warning 然后按 static 跑，宁可不变，不可乱变。
    """
    s = str(m or "").strip().lower()
    if s in BOX_MODES:
        return s
    if s:
        log.warning("未知箱体模式 %r，本轮回退 static（合法值：%s）", m, " / ".join(BOX_MODES))
    return BOX_MODE_STATIC


# 模块级默认模式：从环境变量读一次，缺省 static（线上不主动设这个变量，行为与 D65 完全一致）
BOX_MODE = _norm_mode(os.environ.get("UNIVERSE_BOX_MODE", BOX_MODE_STATIC))


def _pct_rank(values: list, cur: float) -> float:
    """当前值在序列中的百分位（0~100，越低越低位）。"""
    n = len(values)
    if n == 0:
        return 100.0
    below = sum(1 for v in values if v <= cur)
    return below / n * 100.0


def _slope_pct(closes: list) -> float | None:
    """线性回归年化斜率（%）。越接近 0 越横盘。"""
    n = len(closes)
    if n < 30:
        return None
    xs = range(n)
    mx = (n - 1) / 2.0
    my = sum(closes) / n
    denom = sum((x - mx) ** 2 for x in xs)
    if not denom:
        return None
    num = sum((x - mx) * (closes[x] - my) for x in xs)
    slope = num / denom
    return slope * 252.0 / my * 100.0 if my else None


LOAD_KEEP = BOX_WIN + 60        # 取数窗口：箱体 750 根 + 60 根余量（停牌/缺数据）


def _boll_fields(closes: list, cur: float) -> dict:
    """动态箱体（布林通道）五项指标。K 线不足 `BOLL_WIN` 根时全部给 None，由调用方降级。

    - `boll_pctb`      = (close − lower) / (upper − lower)，0=下轨、1=上轨
    - `boll_bw`        = (upper − lower) / mid，带宽：衡量波动率收缩/扩张（越小越横）
    - `boll_pctb_rank` = 当前 %B 在近 `BOLL_RANK_WIN` 日 %B 序列中的百分位（0~100）

    **为什么用分位而不是直接拿 %B 排序**：%B 是相对量，但"0.1"在不同票上不是同一件事 ——
    一只票半年振幅 15%、另一只 60%，同一个 %B 落在完全不同的处境里。换成"该票自己近一年
    %B 的分位"，全市场才落到同一把 0~100 的尺子上，跨票可比。这与静态箱体用 `pos`（相对箱体）
    而不是用绝对价格是同一个道理。

    **σ 的口径**：用 `statistics.pstdev`（总体标准差，除以 N 而不是 N−1），与文件内 `cv` 一致 ——
    近 20 日是这个票的**全部**观测，不是从更大总体里抽的样本，没有"自由度修正"的必要。

    **为什么历史序列不用 pstdev 逐日算**：250 日 %B 序列要算 250 次 σ，pstdev 是精确分数运算
    （实测 20 个元素一次 ~33µs），5000 只票就是 40 秒 —— 比整个扫描还慢一个量级，
    会把中线表从"10 秒"拖成"1 分钟"。所以历史序列用前缀和算总体方差（同一个量：E[x²]−E[x]²，
    数学上与 pstdev 等价），**当前这一天仍走 pstdev**。两者差异在第 13 位小数，
    对分位排名和打分没有可观测影响（`tools/ab_box_mode.py` 里带了自检对照）。
    """
    none_out = {"boll_upper": None, "boll_mid": None, "boll_lower": None,
                "boll_pctb": None, "boll_bw": None, "boll_pctb_rank": None}
    n = len(closes)
    if n < BOLL_WIN or cur is None or cur <= 0:
        return none_out

    win = closes[-BOLL_WIN:]
    mid = sum(win) / float(BOLL_WIN)
    sd = statistics.pstdev(win)
    upper = mid + BOLL_K * sd
    lower = mid - BOLL_K * sd
    band = upper - lower
    if mid <= 0 or band <= 0:
        # 20 日一字横盘（σ=0）→ 带宽为 0，%B 无定义。给 None 让 score 退回静态位置分，
        # 而不是除以 0 得到 inf 把整只票弄成 NaN 分。
        return none_out
    pctb = (cur - lower) / band

    # 历史 %B 序列：只算最近 BOLL_RANK_WIN 个窗口，且用前缀和避免逐日调 pstdev
    lo_i = max(BOLL_WIN - 1, n - BOLL_RANK_WIN)
    tail0 = max(0, lo_i - BOLL_WIN + 1)
    tail = closes[tail0:]
    pre = [0.0]
    pre2 = [0.0]
    for c in tail:
        pre.append(pre[-1] + c)
        pre2.append(pre2[-1] + c * c)
    series = []
    for i in range(lo_i, n):
        hi = i - tail0 + 1                      # tail 里的右开边界
        lo = hi - BOLL_WIN
        if lo < 0:
            continue
        m = (pre[hi] - pre[lo]) / float(BOLL_WIN)
        var = (pre2[hi] - pre2[lo]) / float(BOLL_WIN) - m * m
        if var <= 0.0 or m <= 0:
            continue
        s = var ** 0.5
        b = 2.0 * BOLL_K * s
        series.append((closes[i] - (m - BOLL_K * s)) / b)
    # 序列里塞进"当天用 pstdev 算的那个值"，保证分位对比的左边就是页面上显示的那个数
    rank = _pct_rank(series, pctb) if series else None

    return {
        "boll_upper": round(upper, 2),
        "boll_mid": round(mid, 2),
        "boll_lower": round(lower, 2),
        "boll_pctb": round(pctb, 4),
        "boll_bw": round(band / mid, 4),
        "boll_pctb_rank": round(rank, 1) if rank is not None else None,
    }


def _s_pos_dyn(m: dict) -> float | None:
    """动态位置分（0~100）：近一年 %B 分位越低（越贴下轨）分越高。算不出来时返回 None。

    为什么不在动态这边也套静态那套「下 1/3 封顶」：静态的 1/3 是「谷底区」这个业务概念的定义，
    动态这边没有对应定义；而实测 IC 在截面上是**单调**的（越贴下轨越好，不只在最低那一档），
    所以直接线性铺满 0~100 —— 保住全部排序信息，也避免人为制造一个饱和点。
    """
    r = m.get("boll_pctb_rank")
    if r is None:
        return None
    try:
        return max(0.0, min(1.0, 1.0 - float(r) / 100.0)) * 100.0
    except Exception:  # noqa: BLE001
        return None


def load_bars(codes=None, keep: int = LOAD_KEEP) -> dict:
    """从本地 kbase 批量取近 `keep` 个交易日日线 → {code: [(d, o, c, h, l, v), ...]}。

    **口径转换在这里做，调用方别自己转**：`kbase.ohlcv_bulk()` 返回的是
    `(date, o, h, l, c, v)`，而本模块 `scan_one()` 用的是 `(d, o, c, h, l, v)`
    —— 把 close 放在第 3 位，与 `localfeed.bars_map()` 的老口径一致。
    2026-11-02 曾因为 SQL 列序写成 `select code,c,d` 把日期当收盘价，
    粗筛结果全是 0，排查很久。这里显式逐字段重建元组，不靠下标对齐。

    `codes` 为 None 时取**干净宇宙**（`universe_filter.evaluation_universe()`），
    不是 kbase 里的全部 5590 只 —— 剔除池里的 ST / 停牌 / 僵尸票不该出现在中线上。
    """
    from viewer import localfeed as _lf
    if codes is None:
        from viewer import universe_filter as _uf
        codes = _uf.evaluation_universe()
    codes = [str(c) for c in (codes or []) if c]
    if not codes:
        return {}
    keep = max(MIN_BARS, int(keep or LOAD_KEEP))
    # 只取近 keep 个交易日：kbase 有 1709 万行，全量读要几十秒；
    # 按日期下界截断后 ≈ keep × 5500 行，实测 3~4 秒。
    min_date = ""
    try:
        cal = _lf._kbase().distinct_dates()          # noqa: SLF001 项目内约定用同一交易日历
        if cal:
            min_date = cal[-keep] if len(cal) > keep else cal[0]
    except Exception:  # noqa: BLE001
        min_date = ""
    try:
        raw = _lf._kbase().ohlcv_bulk(codes, min_date=min_date)   # noqa: SLF001
    except Exception:  # noqa: BLE001
        log.exception("universe_box 取 K 线失败（本地仓不可用）")
        return {}
    out = {}
    for code, rows in raw.items():
        if not rows:
            continue
        # (dt,o,h,l,c,v) → (dt,o,c,h,l,v)
        out[str(code)] = [(dt, o, c, h, l, v) for (dt, o, h, l, c, v) in rows[-keep:]]
    return out


def scan_one(bars: list) -> dict | None:
    """单只票的箱体/波谷指标。bars: [(d, o, c, h, l, v), ...] 升序。

    返回 None 表示一级门槛未过（数据不足 / 流动性不够 / 不在低位）。
    返回 dict 表示通过一级，进入二级打分。
    """
    if not bars or len(bars) < MIN_BARS:
        return None
    win = bars[-BOX_WIN:]
    closes = [float(b[2]) for b in win if b[2]]
    highs = [float(b[3]) for b in win if b[3]]
    lows = [float(b[4]) for b in win if b[4]]
    if len(closes) < MIN_BARS or not highs or not lows:
        return None
    cur = closes[-1]
    if cur <= 0:
        return None
    # 流动性：近 250 日均成交额（close×v 近似）
    tail = win[-250:]
    amounts = [float(b[2]) * float(b[5] or 0) for b in tail if b[2] and b[5]]
    if not amounts or (sum(amounts) / len(amounts)) < MIN_AMOUNT_AVG:
        return None

    box_high = max(highs)
    box_low = min(lows)
    if box_low <= 0 or box_high <= box_low:
        return None
    band = box_high - box_low
    pos = (cur - box_low) / band                 # 0=箱底，1=箱顶
    pct = _pct_rank(closes, cur)
    # 一级粗筛：只在低位才算候选
    if pct > PCT_MAX:
        return None

    mean = sum(closes) / len(closes)
    cv = (statistics.pstdev(closes) / mean * 100.0) if mean else 999.0
    slope = _slope_pct(closes)
    if cv > CV_MAX:
        return None
    if slope is not None and abs(slope) > SLOPE_MAX:
        return None

    # 触及次数：收盘落在箱顶/箱底 3% 带内的天数
    touches = sum(1 for c in closes if (box_high - c) / band <= 0.03 or (c - box_low) / band <= 0.03)

    # 谷底驻留：近 DWELL_LOOKBACK 日落在箱体下 1/3 的天数占比 + 最长连续
    edge = box_low + band * DWELL_BOTTOM_RATIO
    seg = closes[-DWELL_LOOKBACK:]
    hits = [1 if c <= edge else 0 for c in seg]
    dwell_ratio = (sum(hits) / len(hits)) if hits else 0.0
    max_run = run = 0
    for h in hits:
        run = run + 1 if h else 0
        if run > max_run:
            max_run = run

    # 动态箱体：与上面静态那套**并行**算，不参与一级/二级门槛（门槛与权重一律不动，
    # 这次只换"位置怎么量"）。放在门槛之后算是有意的：它是纯计算、不算门槛，
    # 没必要为了一级就淘汰的票白算 250 个窗口。
    boll = _boll_fields(closes, cur)

    return {
        "current": round(cur, 2),
        "box_high": round(box_high, 2),
        "box_low": round(box_low, 2),
        "pos": round(pos, 4),
        "pct750": round(pct, 1),
        "cv": round(cv, 1),
        "slope_yr": round(slope, 1) if slope is not None else None,
        "touches": touches,
        "dwell_ratio": round(dwell_ratio, 3),
        "dwell_max_run": max_run,
        "dwell_days": sum(hits),
        "amount_avg_yi": round((sum(amounts) / len(amounts)) / 1.0e8, 2),
        "box_years": round(len(win) / 252.0, 1),
        **boll,
    }


def score_one(m: dict, box_mode: str | None = None) -> dict:
    """把二级指标折算成 0~100 分，并给出可核对的分项。

    `box_mode` **只改「位置分」这一项**，其余三项（驻留 0.35 / 规整 0.20 / 触及 0.15）
    与它们之间的权重一字不动 —— 这次换的是"位置怎么量"的标尺，不是判据的取舍。
    这也保证了 A/B 能干净地归因：分数差异只可能来自位置分。

    `box_mode=None` 时用模块默认（环境变量 `UNIVERSE_BOX_MODE`，缺省 static）。
    """
    mode = _norm_mode(box_mode if box_mode is not None else BOX_MODE)
    # 位置（静态口径）：越贴箱底越高（pos=0 → 100；pos=1/3 → 66.7）
    s_pos_static = max(0.0, min(1.0, 1.0 - m["pos"] / DWELL_BOTTOM_RATIO)) * 100.0
    s_pos = s_pos_static
    s_dyn = None
    pos_src = BOX_MODE_STATIC
    if mode != BOX_MODE_STATIC:
        s_dyn = _s_pos_dyn(m)
        if s_dyn is None:
            # 动态标尺算不出来（K 线不足 20 根 / 20 日一字横盘导致带宽为 0）时，
            # **不剔除这只票**，退回静态位置分并在 parts 里标明来源。
            # 宁可这一只用旧尺，也不让它因为"新尺不适用"从榜上凭空消失。
            pos_src = "static_fallback"
        elif mode == "dyn":
            s_pos, pos_src = s_dyn, "dyn"
        else:                                   # blend
            s_pos = (1.0 - W_POS_DYN) * s_pos_static + W_POS_DYN * s_dyn
            pos_src = "blend"
    # 驻留：开方缓解长尾（估值的做法），0.25→50、0.64→80
    s_dwell = min(1.0, (m["dwell_ratio"]) ** 0.5) * 100.0
    # 规整：CV 越小越好（40→0 分，5→87.5），斜率越平越好（18→0）
    s_cv = max(0.0, (CV_MAX - m["cv"]) / CV_MAX) * 100.0
    slope = abs(m["slope_yr"]) if m["slope_yr"] is not None else SLOPE_MAX
    s_slope = max(0.0, (SLOPE_MAX - slope) / SLOPE_MAX) * 100.0
    s_regular = 0.6 * s_cv + 0.4 * s_slope
    # 触及：2 次起步，12 次封顶
    s_touch = max(0.0, min(1.0, (m["touches"] - TOUCH_MIN) / 10.0)) * 100.0
    score = W_POS * s_pos + W_DWELL * s_dwell + W_REGULAR * s_regular + W_TOUCH * s_touch
    parts = {"pos": round(s_pos, 1), "dwell": round(s_dwell, 1),
             "regular": round(s_regular, 1), "touch": round(s_touch, 1)}
    if mode != BOX_MODE_STATIC:
        # 只有开了新模式才多这两项：static 下 parts 的键必须与 D65 完全一致，
        # 否则前端/落盘 json 的差异会让"默认不变"这条纪律变成一句空话。
        parts["pos_static"] = round(s_pos_static, 1)
        parts["pos_dyn"] = round(s_dyn, 1) if s_dyn is not None else None
    return {"score": round(score, 2), "parts": parts, "box_mode": mode, "pos_src": pos_src}


def scan_universe(klines: dict, limit: int = 30, min_dwell: float = 0.15,
                  names: dict | None = None, keep_all: bool = False,
                  box_mode: str | None = None) -> dict:
    """全市场扫描。klines: {code: [(d,o,c,h,l,v), ...]}。

    返回 {rows, universe, passed1, passed2, skipped, elapsed_ms}。
    `min_dwell` 是驻留门槛：近 250 日至少有 15% 的天数待在谷底区，
    否则"刚到谷底"和"在谷底磨了半年"会被混在一起（前者正是进哥不要的）。
    `keep_all=True` 时额外返回 `all_rows`（未截断的全量），供历史快照写库用 ——
    这个键**不放进 payload**（693 行会让落盘 json 和前端传输都变胖），由调用方 pop 掉。
    `box_mode` 只影响位置分（见 `score_one`），**一二级门槛与 passed1 完全不受它影响** ——
    所以两种模式的 passed1 / passed2 必然相等，差异全部体现在排序与 Top N 组成上。
    这一点在 A/B 里必须如实说明，否则会被误读成"新尺筛掉了更多票"。
    """
    import time
    t0 = time.time()
    mode = _norm_mode(box_mode if box_mode is not None else BOX_MODE)
    nm = names or {}
    rows, passed1, skipped = [], 0, 0
    for code, bars in klines.items():
        m = scan_one(bars)
        if not m:
            skipped += 1
            continue
        passed1 += 1
        if m["dwell_ratio"] < min_dwell:
            continue
        if m["touches"] < TOUCH_MIN:
            continue
        sc = score_one(m, box_mode=mode)
        rows.append({"code": code, "name": nm.get(code, ""), **m, **sc})
    rows.sort(key=lambda r: -r["score"])
    for _i, _r in enumerate(rows):
        _r["rank"] = _i + 1
    out = {
        "rows": rows[:limit],
        "universe": len(klines),
        "passed1": passed1,
        "passed2": len(rows),
        "skipped": skipped,
        "limit": limit,
        "min_dwell": min_dwell,
        "params": {"box_win": BOX_WIN, "pct_max": PCT_MAX, "cv_max": CV_MAX,
                   "slope_max": SLOPE_MAX, "touch_min": TOUCH_MIN,
                   # 这两把尺到底是哪一把，必须能在 payload 里看见 —— 排查时最怕
                   # "分数变了但不知道换了什么"，那时只能靠重启猜。
                   "box_mode": mode,
                   "boll_win": BOLL_WIN, "boll_k": BOLL_K,
                   "boll_rank_win": BOLL_RANK_WIN, "w_pos_dyn": W_POS_DYN,
                   "weights": {"pos": W_POS, "dwell": W_DWELL,
                               "regular": W_REGULAR, "touch": W_TOUCH}},
        "elapsed_ms": int((time.time() - t0) * 1000),
    }
    if keep_all:
        out["all_rows"] = rows
    return out


# ------------------------------------------------ 历史快照 · 日变化（2026-11-03）
# 进哥原话：「中线名单很多天不变，这个没有问题，但是它每天可能会有起伏，那起伏的过程当中
# 你的评分肯定是在变的。你不能说股票不变、但评分也不变，这就不对了。」
#
# 这条批评是对的，而且指的不是 UI —— 是机制。原实现只有"当前分数"一个数：
#   · `pos`（现价在箱体的位置）和 `dwell_ratio`（谷底驻留）都吃最新收盘价，**逻辑上每天必变**；
#   · 但缓存按 6 小时 TTL 判断，不按交易日；且 payload 里没有任何"变化量"字段。
# 结果就是：系统确实在动，人却看不出来动没动。这里补两件事：
#   ① 每个交易日把**全量 passed2**（不只是截断后的 40 只）写进 `universe_box_hist`；
#   ② 跟上一交易日同 code 对比，算出 `d_score` / `d_rank` / `is_new` 挂到行上。
# 没有上一日数据时返回 None 并标 available=False —— **不编造变化量**（新装第一天就是没有）。
HIST_KEEP_DAYS = 60          # 保留 60 个交易日：够看一个季度的分数轨迹，也不至于把库撑大

_HIST_DDL = """
CREATE TABLE IF NOT EXISTS universe_box_hist (
    trade_date    TEXT NOT NULL,
    code          TEXT NOT NULL,
    name          TEXT,
    score         REAL,
    pos           REAL,
    dwell_ratio   REAL,
    dwell_max_run INTEGER,
    pct750        REAL,
    current       REAL,
    rank          INTEGER,
    boll_pctb     REAL,
    box_mode      TEXT NOT NULL DEFAULT 'static',
    PRIMARY KEY (trade_date, code, box_mode)
)
"""

# 正确的主键（含 box_mode）。老库是 (trade_date, code)，必须走重建迁移 —— 原因见 _rebuild_hist_pk。
_HIST_PK = ("trade_date", "code", "box_mode")

# 2026-11-03 D71：老库里这张表已经存在，`CREATE TABLE IF NOT EXISTS` 对它是**空操作** ——
# 不加这步，新列只会在全新机器上存在，老机器一 SELECT boll_pctb 就报错。
_HIST_NEW_COLS = (
    ("boll_pctb", "REAL"),
    ("box_mode", "TEXT NOT NULL DEFAULT 'static'"),
)


def _add_col_if_missing(con, table: str, col: str, ddl: str) -> bool:
    """给已存在的表补列。与 `viewer/toolbox.py :: _add_col_if_missing` 同一写法。

    为什么不在那边复用一个函数：toolbox 反过来要 import 本模块去算箱体，
    模块级 import 会成环。所以这里是同口径的小副本 —— 两份实现都只有 4 行，
    比为了 DRY 造一个第三方 common 模块再让两边都依赖它更省事。
    """
    cols = {r[1] for r in con.execute("PRAGMA table_info(%s)" % table)}
    if col in cols:
        return False
    con.execute("ALTER TABLE %s ADD COLUMN %s %s" % (table, col, ddl))
    return True


def _hist_pk_cols(con, table: str = "universe_box_hist") -> list:
    """取表的主键列（按主键内顺序）。用于判断"要不要迁移主键"。"""
    rows = con.execute("PRAGMA table_info(%s)" % table).fetchall()
    pk = sorted([r for r in rows if (r[5] or 0) > 0], key=lambda r: r[5])
    return [r[1] for r in pk]


def _rebuild_hist_pk(con) -> None:
    """把主键从 `(trade_date, code)` 迁到 `(trade_date, code, box_mode)`。

    **为什么必须迁**（QA 2026-11-03 实测复现的故障）：主键只有两列时，同一交易日先写
    static(90)、再写 blend(99)，`ON CONFLICT(trade_date,code) DO UPDATE` 会把当天那行
    **改成 blend(99)** —— static 的当日快照被吞。于是下一日 static 的 `prev_date` 会跳回大前天，
    `d_score` 实际是**三个交易日**的变化量，而 `delta.note` 仍写"与上一交易日对比"。
    这正好是 D71 想消灭的那种假跳变，只是换了个触发路径 —— 所以主键本身就得带上模式。

    SQLite 改不了主键，只能"建新表 → 搬 → 删旧 → 改名"。整个动作放进一个显式事务
    （`BEGIN IMMEDIATE` 抢写锁），避免两个子进程同时迁移时看到半成品。
    老行没有 `box_mode`（NULL/空串）一律填 `static` —— 2026-11-03 之前确实只有静态尺。

    **可重复执行**：搬完主键已经对了，下次 `_hist_pk_cols` 检查会直接跳过；
    即使两个进程同时进来，第二个也在锁后会看到已迁移的表，搬出来的数据仍然等价。
    """
    tmp = "universe_box_hist__mig"
    # 注意：executescript 会先隐式提交未决事务，再把脚本当整体跑；BEGIN/COMMIT 写在脚本里
    # 才能保证这 5 条语句要么全生效、要么全不生效。
    con.executescript("""
BEGIN IMMEDIATE;
DROP TABLE IF EXISTS {tmp};
CREATE TABLE {tmp} (
    trade_date    TEXT NOT NULL,
    code          TEXT NOT NULL,
    name          TEXT,
    score         REAL,
    pos           REAL,
    dwell_ratio   REAL,
    dwell_max_run INTEGER,
    pct750        REAL,
    current       REAL,
    rank          INTEGER,
    boll_pctb     REAL,
    box_mode      TEXT NOT NULL DEFAULT 'static',
    PRIMARY KEY (trade_date, code, box_mode)
);
INSERT INTO {tmp}(trade_date,code,name,score,pos,dwell_ratio,dwell_max_run,
                  pct750,current,rank,boll_pctb,box_mode)
    SELECT trade_date,code,name,score,pos,dwell_ratio,dwell_max_run,
           pct750,current,rank,boll_pctb,
           COALESCE(NULLIF(box_mode,''),'static')
      FROM universe_box_hist;
DROP TABLE universe_box_hist;
ALTER TABLE {tmp} RENAME TO universe_box_hist;
COMMIT;
""".replace("{tmp}", tmp))
    log.warning("universe_box_hist 主键已迁移为 (trade_date, code, box_mode)"
                "（老行 box_mode 一律填 static）")


def _ensure_hist(con) -> None:
    """建表 + 老库补列 + 主键迁移。幂等，每次用历史表前都调一次。"""
    con.execute(_HIST_DDL)                       # 全新库：一次就建对（含三列主键）
    for _col, _ddl in _HIST_NEW_COLS:            # 老库：先补列，才有东西可搬
        _add_col_if_missing(con, "universe_box_hist", _col, _ddl)
    if tuple(_hist_pk_cols(con)) != _HIST_PK:    # 老库：主键不对就重建
        _rebuild_hist_pk(con)


def _hist_con():
    import sqlite3
    from viewer.ann_feed import DB_PATH       # 与全站同一个库，避免第二套数据源
    con = sqlite3.connect(DB_PATH, timeout=30)
    con.execute("PRAGMA busy_timeout=30000")
    return con


def _attach_delta(trade_date: str, all_rows: list, box_mode: str | None = None) -> dict:
    """写当日全量快照 + 与上一交易日对比，把 d_score/d_rank/is_new 挂到 all_rows 的行上。

    为什么原地改行：`scan_universe` 返回的 `rows` 是 `all_rows` 的**切片**（同一批 dict 对象），
    原地加字段后截断后的 40 行也自动带上，不用再回填一次。

    ★ 2026-11-03 D71：对比**只跟同一把尺**的历史做。
    切换 box_mode 后分数水平会整段平移（位置分换了算法），跨模式相减出来的 Δ 是假跳变 ——
    用户会看到"全表突然涨/跌了 5 分"，而真实行情一动不动。所以按 box_mode 分组比，
    老数据（2026-11-03 之前写入、没有这一列）一律按 static 认 —— 那时候确实只有静态尺。
    """
    if not trade_date or not all_rows:
        return {"available": False, "reason": "无有效交易日或空名单"}
    mode = _norm_mode(box_mode if box_mode is not None else BOX_MODE)
    con = _hist_con()
    try:
        _ensure_hist(con)
        prev_date = None
        row = con.execute(
            "SELECT MAX(trade_date) d FROM universe_box_hist"
            " WHERE trade_date < ? AND COALESCE(NULLIF(box_mode, ''), 'static') = ?",
            (trade_date, mode)).fetchone()
        prev_date = (row and row[0]) or None
        prev = {}
        if prev_date:
            for r in con.execute(
                    "SELECT code, score, rank FROM universe_box_hist"
                    " WHERE trade_date = ? AND COALESCE(NULLIF(box_mode, ''), 'static') = ?",
                    (prev_date, mode)):
                prev[str(r[0])] = {"score": r[1], "rank": r[2]}
        # 先算 Δ（用昨日数据），再写今日快照 —— 顺序反了就会拿今天跟今天比
        if not prev_date:
            # 完全没有历史基准（首次上线 / 库被清过）：这不是"新进榜"，是"无从比较"。
            # is_new 用 None 而不是 True —— 否则首日全表 693 只都会标"新进"，
            # 读起来像"这些票昨天都不在"，是误导。前端约定：None/False + d_score None → 显示「—」。
            for x in all_rows:
                x["d_score"] = None
                x["d_rank"] = None
                x["is_new"] = None
        else:
            for x in all_rows:
                p = prev.get(x["code"])
                if not p:
                    x["d_score"] = None
                    x["d_rank"] = None
                    x["is_new"] = True       # 真有历史、但这只票昨天不在榜 = 真·新进
                    continue
                x["is_new"] = False
                try:
                    x["d_score"] = (round(x["score"] - float(p["score"]), 2)
                                    if p["score"] is not None else None)
                except Exception:  # noqa: BLE001
                    x["d_score"] = None
                try:
                    x["d_rank"] = (int(p["rank"]) - int(x["rank"])
                                   if p["rank"] is not None else None)
                except Exception:  # noqa: BLE001
                    x["d_rank"] = None
        con.executemany(
            "INSERT INTO universe_box_hist"
            "(trade_date,code,name,score,pos,dwell_ratio,dwell_max_run,pct750,current,rank,"
            " boll_pctb,box_mode)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT(trade_date,code,box_mode) DO UPDATE SET"
            " name=excluded.name, score=excluded.score, pos=excluded.pos,"
            " dwell_ratio=excluded.dwell_ratio, dwell_max_run=excluded.dwell_max_run,"
            " pct750=excluded.pct750, current=excluded.current, rank=excluded.rank,"
            " boll_pctb=excluded.boll_pctb",
            [(trade_date, x["code"], x.get("name") or "", x["score"], x["pos"],
              x["dwell_ratio"], x["dwell_max_run"], x["pct750"], x["current"], x["rank"],
              x.get("boll_pctb"), mode)
             for x in all_rows])
        # 只留最近 HIST_KEEP_DAYS 个交易日（按库里实际出现的日期切，不按自然日）
        dates = [r[0] for r in con.execute(
            "SELECT DISTINCT trade_date FROM universe_box_hist ORDER BY trade_date DESC")]
        stale = dates[HIST_KEEP_DAYS:]
        if stale:
            con.executemany("DELETE FROM universe_box_hist WHERE trade_date = ?",
                            [(d,) for d in stale])
        con.commit()
        moved = sum(1 for x in all_rows if x.get("d_score"))
        up = sum(1 for x in all_rows if (x.get("d_score") or 0) > 0)
        down = sum(1 for x in all_rows if (x.get("d_score") or 0) < 0)
        return {
            "available": bool(prev_date),
            "trade_date": trade_date,
            "prev_date": prev_date,
            "box_mode": mode,
            "n_today": len(all_rows),
            "n_prev": len(prev),
            "n_moved": moved,
            "n_up": up,
            "n_down": down,
            "n_new": sum(1 for x in all_rows if x.get("is_new")),
            "keep_days": HIST_KEEP_DAYS,
            "note": ("与上一交易日 %s 对比（%s 标尺）：%d 只分数上移、%d 只下移、%d 只新进。"
                     % (prev_date, mode, up, down, sum(1 for x in all_rows if x.get("is_new")))
                     if prev_date else
                     "首个交易日（%s 标尺），暂无对比基准（明天起会显示「较上日」变化）。" % mode),
        }
    finally:
        try:
            con.close()
        except Exception:  # noqa: BLE001
            pass


def history(code: str, days: int = 60, box_mode: str | None = None) -> list:
    """单只票的分数轨迹（按日期升序），给前端画小折线 / 查"它是不是越磨越久"。

    同样按 `box_mode` 过滤：把静态尺下算的 62 分和动态尺下算的 71 分画在同一条折线上，
    看起来是"这票分数涨了"，其实是换了一把尺 —— 那种图比没有图更误导。
    """
    code = str(code or "").strip()
    if not code:
        return []
    mode = _norm_mode(box_mode if box_mode is not None else BOX_MODE)
    con = _hist_con()
    try:
        _ensure_hist(con)
        rows = con.execute(
            "SELECT trade_date,score,pos,dwell_ratio,dwell_max_run,rank,boll_pctb"
            " FROM universe_box_hist"
            " WHERE code = ? AND COALESCE(NULLIF(box_mode, ''), 'static') = ?"
            " ORDER BY trade_date DESC LIMIT ?",
            (code, mode, max(1, int(days)))).fetchall()
        out = [{"trade_date": r[0], "score": r[1], "pos": r[2], "dwell_ratio": r[3],
                "dwell_max_run": r[4], "rank": r[5], "boll_pctb": r[6],
                "box_mode": mode} for r in rows]
        out.reverse()
        return out
    finally:
        try:
            con.close()
        except Exception:  # noqa: BLE001
            pass


def run(limit: int = 40, min_dwell: float = 0.15, codes=None,
        box_mode: str | None = None) -> dict:
    """一站式入口：干净宇宙 → 本地取数 → 扫描 → 补名称。给 API / 子进程调用。

    实测（2026-11-02，本机）：取数 ~3.6s、扫描 ~1.6s，合计约 5 秒 / 全市场 5264 只。
    所以**不能在请求线程里跑**（项目纪律：重计算一律离进程），走 `_variant_cached`。
    """
    from viewer import localfeed as _lf
    import time as _t
    mode = _norm_mode(box_mode if box_mode is not None else BOX_MODE)
    t_load0 = _t.time()
    kl = load_bars(codes)
    load_ms = int((_t.time() - t_load0) * 1000)
    nm = {}
    try:
        nm = _lf.pool(with_names=True)
    except Exception:  # noqa: BLE001
        nm = {}
    res = scan_universe(kl, limit=limit, min_dwell=min_dwell, names=nm, keep_all=True,
                        box_mode=mode)
    res["load_ms"] = load_ms
    res["trade_date"] = _lf.latest_date()

    # ⑤ 准入闸门（2026-11-03 进哥要求「基本面从加分项改闸门」）：
    #    形态分只回答"形状像不像谷底"，不回答"这家公司还能不能活"。
    #    硬否决直接剔除（净资产为负 / 微盘 / 资不抵债 / 深度亏损），
    #    软否决扣分不剔除（估值贵、负债偏高、营收净利双降）——
    #    实测硬否决只碰 3% 的票、且榜单前 10 名零命中，所以不会把好票杀掉。
    _all = res.pop("all_rows", []) or []
    _gate = {}
    _n_hard = 0
    try:
        from viewer import gate as _G
        _gc = _hist_con()
        try:
            _gate = _G.evaluate(_gc, [x["code"] for x in _all])
        finally:
            _gc.close()
        _kept = []
        for _x in _all:
            _g = _gate.get(_x["code"]) or {}
            if _g.get("hard"):
                _n_hard += 1
                continue
            _pen = _g.get("penalty") or 0.0
            if _pen:
                _x["tech_score"] = _x["score"]          # 原始形态分，留作核对
                _x["score"] = round(max(0.0, _x["score"] - _pen), 2)
                _x["gate_penalty"] = _pen
                _x["gate_soft"] = _g.get("soft") or []
            _kept.append(_x)
        _all = _kept
        _all.sort(key=lambda r: -r["score"])
        for _i, _x in enumerate(_all):
            _x["rank"] = _i + 1                    # 闸门会改分数 → 排名必须重算
        res["rows"] = _all[:limit]
        res["passed2"] = len(_all)
        res["gate"] = _G.summary(_gate)
        res["gate"]["n_hard_dropped"] = _n_hard
    except Exception:  # noqa: BLE001
        log.exception("准入闸门不可用（本轮按无闸门出榜，不误剔）")
        res["gate"] = {"available": False, "note": "闸门模块不可用，本轮未做估值/基本面过滤"}

    # ⑥ 日变化（2026-11-03 进哥当场指出）：名单可以连续多天不变，但**分数必须随行情每天变**。
    #    之前只有"当前分数"这一个数，用户看不出它到底动没动 —— 等于系统在动、人看不见。
    #    这里把当日全量（闸门后的 passed2）写进历史快照，再跟上一交易日对比算 Δ 分/Δ 排名，
    #    前端直接显示"较上日 +2.3"。没有上一日数据时如实返回 None，不编。
    try:
        _delta = _attach_delta(res["trade_date"], _all, box_mode=mode)
    except Exception:  # noqa: BLE001
        log.exception("universe_box 历史快照/日变化计算失败（不影响当日榜单）")
        _delta = {"available": False, "reason": "历史快照不可用"}
    res["delta"] = _delta

    res["ok"] = True
    res["note"] = (
        "全市场箱体波谷扫描：只要现价落在 750 日箱体下 1/3 且已在谷底区驻留≥%d%% 的交易日，"
        "就会一直留在榜上 —— 不因为当天涨一点就被踢出去。驻留越久、越贴箱底，分数越高。"
        "「较上日」是同一只票今天与上一交易日相比的分数变化：名单不变、分数照样每天在动。"
        % int(min_dwell * 100)
    )
    if mode != BOX_MODE_STATIC:
        # 只在非默认模式下追加这句：static 下 payload 必须与 D65 逐字节一致（默认不变的纪律）。
        res["note"] += (
            " 位置分当前用「%s」标尺（动态布林 %d 日 ±%.1fσ，动态权重 %.2f），"
            "它不是默认值 —— 默认是静态 750 日箱体。" % (mode, BOLL_WIN, BOLL_K, W_POS_DYN))
    return res
