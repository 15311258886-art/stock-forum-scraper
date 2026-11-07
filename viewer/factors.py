"""形态因子库（二期，2026-11-03）：把语料里的方法论编译成**可算的因子**。

进哥原话：「你看这里边除了这个 K 线预测以外，就包括技术价值教学呀，再包括什么豆包技术方法呀，
这些其实都是有价值的，里边可能会分为很多的方式方法技巧……分的时候你能不能把这些也都从
现有的能力当中给它拆出来，都拆成各种各样的这种工具。」

一期做的是「给已有能力装箱」；二期做的是**把纯文本的方法论变成代码**。语料里那些说法
（缩量回踩、双底、平台突破、MACD 背离、波浪位置、缠论中枢）原本只是人话，这里逐条落成
能在 5000+ 只票上跑的判据。

三条纪律（改这个文件前先读）

 1. **每个因子都写清「初版口径」**。语料本身说法互相矛盾，我不可能一次全对。函数 docstring
    里必须写明：判据是什么、为什么这么选、哪些情况它会误判。跑完看 `evidence` 的数据再调，
    而不是凭感觉调。
 2. **不夸大**。这些是**形态识别**，不是预测。识别到「像双底」和「会涨」之间隔着很远，
    所以每个因子的 note 都写"像什么"，不写"看涨"。
 3. **分母要说清楚**。任何比率都写明白是「占 750 日」「占近 60 日」还是「占近 20 日」——
    分位类指标在样本不足时是噪声（这一点 D65 已经踩过一次）。

性能：单因子在全市场（5000+ 只 × 750 日）实测 3~5 秒，主要是取 K 线。
**不做跨因子缓存**：一次缓存全宇宙要 70MB+ 常驻，服务端长跑不值得；靠
`universe_box.load_bars` 的分流（池子 ≥2000 走全量批量）把取数压到 4 秒。
"""
from __future__ import annotations

import logging
import math

import numpy as np

log = logging.getLogger(__name__)

MIN_BARS = 250          # 少于 250 根日线的票不参与形态判断（半年样本画不出形态）
# 取数窗口：形态因子最长只要 250 根（波浪），留 70 根余量应对停牌/缺数据。
# **不跟着 universe_box.LOAD_KEEP（810）走**：那是箱体要 750 根才定的。
# 实测把窗口从 810 缩到 320 **没有提速**（6.1s vs 6.6s，差异在噪声内）——瓶颈是
# 5003 次按代码的索引查找，不是行数。留着是因为内存少取 60%，且六个因子结果逐只一致。
LOAD_KEEP = 320
EPS = 1e-9


# ================================================================
# 取数与基础工具
# ================================================================
def load(codes=None, keep: int = LOAD_KEEP) -> dict:
    """取日线 → {code: dict(o=[], c=[], h=[], l=[], v=[], n=int)}（numpy 数组）。

    列序转换在 `universe_box.load_bars` 里已做过一次（它把 (d,o,h,l,c,v) 转成 (d,o,c,h,l,v)），
    这里再拆成命名数组 —— **不按下标猜**，D65 曾因下标错位把日期当收盘价，排查很久。
    """
    from viewer import universe_box as ub
    keep = max(MIN_BARS + 10, int(keep or LOAD_KEEP))
    if codes is not None and len(codes) >= 2000:
        # 与 toolbox._apply_box 同一条分流：显式传 5000 个 code 给 ohlcv_bulk 要 40 秒，
        # 不带 codes 走全宇宙批量只要 4 秒。大池子先取全量再内存求交集。
        full = ub.load_bars(None, keep=keep)
        raw = {c: full[c] for c in codes if c in full} if full else {}
    else:
        raw = ub.load_bars(codes, keep=keep)
    out = {}
    for code, rows in raw.items():
        if not rows:
            continue
        # 只取 5 列数值，**跳过日期字符串**（因子不用日期，留着只是白占内存）。
        # 这里必须整块交给 numpy 转换：第一版写成 `a[i,0]=r[1]` 逐元素赋值，
        # 5000 只 × 750 行 × 5 列 = 1875 万次标量赋值，实测吃掉 12 秒；
        # 换成一次性 np.asarray(列表) 后总取数从 17 秒降到约 5 秒。
        a = np.asarray([r[1:6] for r in rows], dtype=np.float64)
        out[str(code)] = {
            "n": len(rows),
            "o": a[:, 0], "c": a[:, 1], "h": a[:, 2], "l": a[:, 3], "v": a[:, 4],
        }
    return out


def _ema(x: np.ndarray, span: int) -> np.ndarray:
    """指数移动平均。用 pandas 的 C 实现（numpy 手写递推 750 次要 0.5ms，5000 只就是 2.5 秒）。"""
    import pandas as pd
    return pd.Series(x).ewm(span=span, adjust=False).mean().to_numpy()


def _rsi_proxy(x: np.ndarray, win: int = 14) -> float:
    """近 win 日涨跌幅均值比（RSI 的简化版，只用来量化"弱不弱"，不参与判定）。"""
    if len(x) < win + 1:
        return 50.0
    d = np.diff(x[-(win + 1):])
    up = float(np.clip(d, 0, None).sum())
    dn = float(np.clip(-d, 0, None).sum())
    if up + dn < EPS:
        return 50.0
    return round(100.0 * up / (up + dn), 1)


# ================================================================
# ① 缩量回踩（B站「技术·价值教学」语料）
# ================================================================
def f_shrink_pull(b: dict, p: dict) -> tuple[bool, float, str]:
    """缩量回踩均线：涨过一段之后回踩 MA20，且**量能萎缩**。

    判据（初版口径）：
      · 站上 MA20 且 MA20 本身向上（不是下跌趋势里的假回踩）
      · 近 5 日最低价摸到 MA20 的 ±3% 带内（真的踩到了）
      · 回踩期间 5 日均量 ≤ 前期 20 日均量 × shrink（默认 0.75，即缩量 25% 以上）
      · 不是断头铡刀：近 5 日单日最大跌幅 > 8% 直接否掉

    会误判的地方：均线走平期会反复触发；一字板后的缩量不是"惜售"而是"没成交"。
    """
    c, l, v = b["c"], b["l"], b["v"]
    n = b["n"]
    if n < 60:
        return False, 0.0, "日线不足 60 根"
    ma20 = float(np.mean(c[-20:]))
    ma20_prev = float(np.mean(c[-25:-5]))
    if c[-1] < ma20:
        return False, 0.0, "收盘在 MA20 下方（还没站上去）"
    if ma20 <= ma20_prev:
        return False, 0.0, "MA20 未向上（下跌趋势里的回踩不算）"
    band = abs(float(l[-5:].min()) - ma20) / (ma20 + EPS)
    if band > 0.03:
        return False, 0.0, "近 5 日最低价离 MA20 有 %.1f%%（没踩到）" % (band * 100)
    v5 = float(np.mean(v[-5:]))
    v20 = float(np.mean(v[-30:-10])) + EPS
    ratio = v5 / v20
    shrink = float(p.get("shrink", 0.75))
    if ratio > shrink:
        return False, 0.0, "回踩期量能是前期的 %.0f%%（未缩到 %.0f%% 以下，像出货不像惜售）" % (
            ratio * 100, shrink * 100)
    drops = (c[-5:] - c[-6:-1]) / (c[-6:-1] + EPS)
    if float(drops.min()) < -0.08:
        return False, 0.0, "回踩中出现单日 %.1f%% 大跌（是断头铡刀不是回踩）" % (drops.min() * 100)
    score = min(100.0, 50.0 + (1.0 - ratio / shrink) * 30.0 + band * 300.0)
    return True, round(score, 1), (
        "MA20 %.2f 向上 · 回踩差 %.1f%% · 量能 %.0f%%（缩量 %.0f%%）"
        % (ma20, band * 100, ratio * 100, (1 - ratio) * 100))


# ================================================================
# ② 双底 / W 底
# ================================================================
def f_wbottom(b: dict, p: dict) -> tuple[bool, float, str]:
    """双底：两次探底价位接近，中间有反弹，现在还没破颈线太多。

    判据（初版口径）：
      · 近 trade_win（默认 90）日内出现两个低点，二者间隔 ≥ gap_min（默认 15 日）
      · 两低点价差 ≤ tol（默认 5%）
      · 两底之间有过像样反弹（高点比低点高 ≥ 8%）
      · 现价在颈线下方或刚突破（不超过颈线 8%），**不追已经涨飞的**

    会误判的地方：下跌中继也会出现两个相近低点，所以必须要求中间有反弹；
    真 W 底要放量过颈线，这里只用"接近颈线"当代理，不假装能识别放量。
    """
    c, h, l = b["c"], b["h"], b["l"]
    n = b["n"]
    win = int(p.get("trade_win", 90))
    gap_min = int(p.get("gap_min", 15))
    tol = float(p.get("tol", 0.05))
    if n < win + 10:
        return False, 0.0, "日线不足 %d 根" % (win + 10)
    seg_l = l[-win:]
    i1 = int(np.argmin(seg_l))
    # 第二个低点必须在 i1 之后至少 gap_min 天，否则是同一个底
    rest = seg_l[i1 + gap_min:]
    if len(rest) < 5:
        return False, 0.0, "首个低点太靠后，凑不出第二个底"
    i2 = i1 + gap_min + int(np.argmin(rest))
    lo1, lo2 = float(seg_l[i1]), float(seg_l[i2])
    if abs(lo1 - lo2) / (min(lo1, lo2) + EPS) > tol:
        return False, 0.0, "两底价差 %.1f%%（> %.0f%%，不是同一水平）" % (
            abs(lo1 - lo2) / (min(lo1, lo2) + EPS) * 100, tol * 100)
    mid_high = float(h[-win:][i1:i2].max()) if i2 > i1 else 0.0
    if mid_high <= 0 or mid_high / (min(lo1, lo2) + EPS) - 1 < 0.08:
        return False, 0.0, "两底之间只反弹了 %.1f%%（< 8%%，像下跌中继不像 W）" % (
            (mid_high / (min(lo1, lo2) + EPS) - 1) * 100)
    # 中期趋势不能还在瀑布：下跌途中的「两个相近低点」满地都是，不加这条会筛出 20% 的市场。
    # 这不是为了让数字好看，是因为 W 底的前提是**下跌停住了**，MA20 还在 MA60 下面一大截的票
    # 根本谈不上筑底。阈值 0.98 允许轻微下行，只拦"还在加速跌"的。
    ma20 = float(np.mean(c[-20:]))
    ma60 = float(np.mean(c[-60:]))
    if ma20 < ma60 * 0.98:
        return False, 0.0, "MA20 仍在 MA60 下方 %.1f%%（中期还在下行，不算筑底）" % (
            (1 - ma20 / (ma60 + EPS)) * 100)
    neck = mid_high
    cur = float(c[-1])
    if cur > neck * 1.08:
        return False, 0.0, "已高出颈线 %.1f%%（追高不算双底买点）" % ((cur / neck - 1) * 100)
    if cur < min(lo1, lo2) * 0.97:
        return False, 0.0, "已跌破两底水平（形态破坏）"
    if cur < lo2 * 1.02:
        return False, 0.0, "现价还压在第二个底上（没离开底部，等反弹确认）"
    score = 60.0 + (1 - abs(lo1 - lo2) / (min(lo1, lo2) + EPS) / tol) * 25.0
    if cur <= neck:
        score += 10.0
    return True, round(min(100.0, score), 1), (
        "两底 %.2f / %.2f（价差 %.1f%%）· 间隔 %d 日 · 中间反弹 %.1f%% · 距颈线 %.1f%%"
        % (lo1, lo2, abs(lo1 - lo2) / (min(lo1, lo2) + EPS) * 100, i2 - i1,
           (mid_high / (min(lo1, lo2) + EPS) - 1) * 100, (neck / cur - 1) * 100))


# ================================================================
# ③ 平台突破
# ================================================================
def f_platform(b: dict, p: dict) -> tuple[bool, float, str]:
    """平台突破：先横向盘够久，再向上突破上沿。

    判据（初版口径）：
      · 突破前 plateau_win（默认 60）日振幅（最高/最低 − 1）≤ amp_max（默认 25%）
      · 近 10 日振幅 ≤ 8%（末段真的收敛了，不是一路波动）
      · 今日收盘 > 突破前 plate_win（默认 20）日最高 × 1.005
      · 量能配合：今日量 ≥ 20 日均量 × vol_min（默认 1.5）

    会误判的地方：一字板突破当天没有成交量放大（量能条件会否掉它，这是有意的——
    没有换手的突破不知道该信谁）；假突破率天然高，所以只做"识别"，不做"确认"。
    """
    c, h, l, v = b["c"], b["h"], b["l"], b["v"]
    n = b["n"]
    pw = int(p.get("plateau_win", 60))
    amp_max = float(p.get("amp_max", 0.25))
    if n < pw + 25:
        return False, 0.0, "日线不足 %d 根" % (pw + 25)
    hh = float(h[-pw - 1:-1].max())
    ll = float(l[-pw - 1:-1].min())
    amp = (hh - ll) / (ll + EPS)
    if amp > amp_max:
        return False, 0.0, "突破前 %d 日振幅 %.1f%%（> %.0f%%，不是平台是趋势）" % (
            pw, amp * 100, amp_max * 100)
    hh10, ll10 = float(h[-11:-1].max()), float(l[-11:-1].min())
    amp10 = (hh10 - ll10) / (ll10 + EPS)
    if amp10 > 0.08:
        return False, 0.0, "近 10 日振幅 %.1f%%（> 8%%，末段还在大幅波动）" % (amp10 * 100)
    hh20 = float(h[-21:-1].max())
    if c[-1] <= hh20 * 1.005:
        return False, 0.0, "收盘未突破前 20 日高点（差 %.1f%%）" % ((hh20 / c[-1] - 1) * 100)
    vr = float(v[-1]) / (float(np.mean(v[-21:-1])) + EPS)
    vol_min = float(p.get("vol_min", 1.5))
    if vr < vol_min:
        return False, 0.0, "突破日量能仅 %.1f 倍（< %.1f 倍，无量突破不敢认）" % (vr, vol_min)
    score = 55.0 + min(25.0, (amp_max - amp) / amp_max * 40.0) + min(20.0, (vr - vol_min) * 8.0)
    return True, round(min(100.0, score), 1), (
        "平台 %d 日振幅 %.1f%% · 近 10 日 %.1f%% · 破前 20 日高 %.1f%% · 量能 %.1f 倍"
        % (pw, amp * 100, amp10 * 100, (c[-1] / hh20 - 1) * 100, vr))


# ================================================================
# ④ MACD 底背离
# ================================================================
def f_macd_div(b: dict, p: dict) -> tuple[bool, float, str]:
    """MACD 底背离：价格创了更低的低点，但 DIF 的低点**抬高**了。

    判据（初版口径）：
      · 快慢线 12/26、信号 9（标准参数，不调；调了就不是同一个东西了）
      · 近 lb（默认 60）日里找两个价格低点：第一低点在前，第二低点在近 10 日内
      · 第二低点收盘 < 第一低点（价格确实创新低）
      · 第二低点处的 DIF > 第一低点处的 DIF（指标没跟着创新低 → 这就是背离）
      · 现价离开第二低点不超过 8%（还在低位区，不是已经涨飞了）
      · DIF 今日不低于昨日（在修复，不是继续往下）
      · 两个低点间隔 ≥ 10 日（隔太近是同一根针）

    ★ 第一版写的是「今日收盘是 60 日最低 + DIF 距窗口最低有 2% 价距 + DIF 连抬 3 日」，
    全市场跑出来 **0 只**。不是市场没有背离，是判据**自相矛盾**：
    "今日创 60 日新低"意味着今天在跌，"DIF 连抬三日"意味着今天在涨，两条几乎不可能同时成立。
    实测当日 318 只创 60 日新低，其中 DIF 能连抬三日的只有 2 只，交集为 0。
    改成比较两个低点之后，才是教科书里那个东西。

    会误判的地方：背离可以连做三次才真反转，识别 ≠ 反转；下跌趋势里会连续触发，
    所以要求"现价离开低点 ≤8%"来限定在低位区，但这也意味着它会漏掉 V 型反转。
    """
    c = b["c"]
    n = b["n"]
    lb = int(p.get("lookback", 60))
    if n < lb + 30:
        return False, 0.0, "日线不足 %d 根" % (lb + 30)
    dif = _ema(c, 12) - _ema(c, 26)
    seg, dw = c[-lb:], dif[-lb:]
    i2 = lb - 10 + int(np.argmin(seg[-10:]))     # 第二低点：近 10 日里最低的那天
    head = seg[:max(0, i2 - 10)]
    if len(head) < 10:
        return False, 0.0, "第二低点太靠前，前面凑不出第一低点"
    i1 = int(np.argmin(head))
    if seg[i2] >= seg[i1]:
        return False, 0.0, "第二低点 %.2f 没跌破第一低点 %.2f（价格没创新低）" % (seg[i2], seg[i1])
    if dw[i2] <= dw[i1]:
        return False, 0.0, "DIF 跟着创新低（%.4f → %.4f，真跌无背离）" % (dw[i1], dw[i2])
    cur = float(c[-1])
    if cur > float(seg[i2]) * 1.08:
        return False, 0.0, "已从第二低点涨了 %.0f%%（背离买点在低位，这里追不上）" % (
            (cur / float(seg[i2]) - 1) * 100)
    if dif[-1] < dif[-2]:
        return False, 0.0, "DIF 今日还在下行（背离还没开始修复）"
    gap = float(dw[i2] - dw[i1])
    score = 55.0 + min(30.0, gap / (abs(float(dw[i1])) + EPS) * 20.0)
    return True, round(min(100.0, score), 1), (
        "价 %.2f → %.2f（-%.1f%%）· DIF %.4f → %.4f（抬高 %.4f）· 两低点隔 %d 日" % (
            seg[i1], seg[i2], (1 - seg[i2] / (seg[i1] + EPS)) * 100,
            dw[i1], dw[i2], gap, i2 - i1))


# ================================================================
# ⑤ 波浪位置（豆包技术方法语料）
# ================================================================
def f_wave(b: dict, p: dict) -> tuple[bool, float, str]:
    """波浪位置粗定位：只区分「二浪回撤区」和「三浪启动区」，**不做精细数浪**。

    为什么只做粗定位：数浪本质是事后归因，同一条走势十个人能数出十一种浪。硬要精确
    数浪等于把主观判断包装成算法。这里只抓两个**结构上可证伪**的特征：

      · 二浪回撤区：低点必须在高点**之前**（先涨后回），且 250 日区间涨幅 ≥ 25%
        （确有一浪），当前从高点回撤 30%~55%，且没跌破 250 日低点。
      · 三浪启动区：250 日内出现过 ≥ 15% 的回撤（确有二浪），当前已站上 250 日高点
        的 98%（过前高＝三浪突破确认），且 60 日涨幅 10%~60%（太弱是没启动，
        太猛是加速末端不是启动）。

    ★ 第一版只用了「回撤幅度 + 60 日涨幅 + 分位」三个数，全市场筛出 1804 只（34%）——
    等于没筛。原因就是**这些数描述的是"现在长什么样"，不是"浪走到哪儿"**：
    今天是普涨日，60 日涨 15%~45% 且贴着 250 日高的票满街都是，它们可能是一浪延伸、
    也可能是从底部一路反弹上来的，浪型完全不同却都满足条件。加上"先涨后回"和
    "中途调过 ≥15%"这两个**顺序**约束之后才真的在数浪。

    会误判的地方：这两个区的边界是拍的，别当铁律；250 日窗口装不下 3 年的大级别浪。
    """
    c = b["c"]
    n = b["n"]
    if n < 250:
        return False, 0.0, "日线不足 250 根（数不了年级别的浪）"
    seg = c[-250:]
    i_hi, i_lo = int(np.argmax(seg)), int(np.argmin(seg))
    c250_h, c250_l = float(seg[i_hi]), float(seg[i_lo])
    cur = float(c[-1])
    drawdown = 1 - cur / (c250_h + EPS)
    gain60 = cur / (float(c[-60]) + EPS) - 1
    pos250 = (cur - c250_l) / (c250_h - c250_l + EPS)
    # 窗口内最大回撤（用于判断"中途调过没有"）
    run_max = np.maximum.accumulate(seg[:-1])
    max_dd = float((1 - seg[:-1] / (run_max + EPS)).max())

    if i_lo < i_hi and 0.30 <= drawdown <= 0.55 and cur > c250_l * 1.05:
        impulse = c250_h / (c250_l + EPS) - 1
        if impulse < 0.25:
            return False, 0.0, "回撤 %.0f%% 但 250 日区间只涨过 %.0f%%（没有一浪，是阴跌下来的）" % (
                drawdown * 100, impulse * 100)
        return True, round(min(100.0, 50.0 + (0.55 - drawdown) * 100), 1), (
            "二浪回撤区 · 一浪涨 %.0f%% · 距 250 日高回撤 %.0f%% · 250 日分位 %.0f%%" % (
                impulse * 100, drawdown * 100, pos250 * 100))

    if cur >= c250_h * 0.98 and 0.10 <= gain60 <= 0.60:
        if max_dd < 0.15:
            return False, 0.0, "站上前高但 250 日内没调过（最大回撤 %.0f%%，是一浪延伸不是三浪）" % (
                max_dd * 100)
        return True, round(min(100.0, 60.0 + min(30.0, gain60 * 60)), 1), (
            "三浪启动区 · 站上 250 日高 %.0f%% · 中途最大回撤 %.0f%% · 60 日涨 %.0f%%" % (
                cur / (c250_h + EPS) * 100, max_dd * 100, gain60 * 100))

    return False, 0.0, "不在二浪回撤区/三浪启动区（回撤 %.0f%%，60 日涨 %.0f%%，250 日分位 %.0f%%，一浪位置%s）" % (
        drawdown * 100, gain60 * 100, pos250 * 100, "成立" if i_lo < i_hi else "不成立")


# ================================================================
# ⑥ 缠论中枢（豆包技术方法语料）
# ================================================================
def f_chan(b: dict, p: dict) -> tuple[bool, float, str]:
    """中枢震荡：只做「末次下探不破中枢下沿」，不做笔/线段的严格递归。

    判据（初版口径）：
      · 中枢 = 近 win（默认 60）日的价格密集区（用 25%~75% 分位当上下沿，不取极值——
        极值受单根针影响太大，分位更能代表"成交密集"）
      · 中枢高度 (up-low)/low ≤ 15%（够窄才叫中枢，不然是趋势）
      · 现价在中枢内，且距下沿 ≥ 2%（没贴着下沿，更没跌破）
      · 末次下探（近 10 日最低）没破下沿

    会误判的地方：这不是严格缠论（严格版要处理包含关系、分型、笔、线段、中枢延伸），
    是"用缠论的直觉做一个可算的近似"。名字保留是为了让你知道它从哪来，不代表它等价。
    """
    c, h, l = b["c"], b["h"], b["l"]
    n = b["n"]
    win = int(p.get("win", 60))
    if n < win + 10:
        return False, 0.0, "日线不足 %d 根" % (win + 10)
    seg = c[-win:]
    low = float(np.percentile(seg, 25))
    up = float(np.percentile(seg, 75))
    if low <= 0:
        return False, 0.0, "价格异常"
    height = (up - low) / low
    if height > 0.15:
        return False, 0.0, "中枢高度 %.1f%%（> 15%%，是趋势不是中枢）" % (height * 100)
    cur = float(c[-1])
    if cur < low:
        return False, 0.0, "现价已跌破中枢下沿 %.2f（中枢失守）" % low
    if cur > up:
        return False, 0.0, "现价已冲出中枢上沿 %.2f（不是震荡了）" % up
    dist = (cur - low) / (low + EPS)
    if dist < 0.02:
        return False, 0.0, "现价贴着下沿（距 %.1f%%，2%% 以内不接）" % (dist * 100)
    last_low = float(l[-10:].min())
    if last_low < low * 0.995:
        return False, 0.0, "末次下探 %.2f 已破下沿 %.2f" % (last_low, low)
    score = 55.0 + min(30.0, (0.15 - height) / 0.15 * 40.0) + min(15.0, dist * 100)
    return True, round(min(100.0, score), 1), (
        "中枢 %.2f~%.2f（高 %.1f%%）· 现价距下沿 %.1f%% · 末次下探守住" % (
            low, up, height * 100, dist * 100))


# ================================================================
# ⑦ 情绪周期（市场级观察器，不是个股筛子）
# ================================================================
def market_emotion(bars: dict) -> dict:
    """全市场情绪温度：不筛个股，只回答「现在是哪一段」。

    口径：用当日**上涨家数占比**（广度）、全市场中位涨幅、涨幅 >9.5% 家数占比（强势股数量）
    三个数，配 5 日均值判断阶段。炸板率这类指标本地没有数据，**不算，也不编**。
    """
    pct, strong, ups, tot = [], 0, 0, 0
    for b in bars.values():
        if b["n"] < 2:
            continue
        prev, cur = float(b["c"][-2]), float(b["c"][-1])
        if prev <= 0:
            continue
        r = cur / prev - 1
        pct.append(r)
        tot += 1
        if r > 0:
            ups += 1
        if r > 0.095:
            strong += 1
    if not tot:
        return {"ok": False, "note": "没有可用的日线数据，无法判断情绪"}
    med = float(np.median(pct)) * 100
    upr = ups / tot * 100
    strongr = strong / tot * 100
    if upr >= 65 and med > 1.0:
        phase, desc = "回暖/亢奋", "普涨，做多氛围浓"
    elif upr >= 50:
        phase, desc = "修复中", "涨多跌少，但还没形成合力"
    elif upr >= 35:
        phase, desc = "分化", "涨跌各半，靠结构不靠普涨"
    else:
        phase, desc = "退潮", "普跌，逆势做多容易被打"
    return {"ok": True, "phase": phase, "desc": desc, "up_ratio": round(upr, 1),
            "median_pct": round(med, 2), "strong_ratio": round(strongr, 2),
            "n": tot}


# ================================================================
# 注册表：因子 key → (实现, 说明, 默认参数)
# ================================================================
FACTORS: dict = {
    "tv_shrink_pull": {
        "fn": f_shrink_pull, "name": "缩量回踩",
        "defaults": {"shrink": 0.75},
        "unit": "量能比",
    },
    "tv_wbottom": {
        "fn": f_wbottom, "name": "双底（W）",
        "defaults": {"trade_win": 90, "gap_min": 15, "tol": 0.05},
        "unit": "日",
    },
    "tv_platform": {
        "fn": f_platform, "name": "平台突破",
        "defaults": {"plateau_win": 60, "amp_max": 0.25, "vol_min": 1.5},
        "unit": "倍",
    },
    "tv_macd_div": {
        "fn": f_macd_div, "name": "MACD 底背离",
        "defaults": {"lookback": 60},
        "unit": "日",
    },
    "db_wave": {
        "fn": f_wave, "name": "波浪位置",
        "defaults": {},
        "unit": "",
    },
    "db_chan": {
        "fn": f_chan, "name": "缠论中枢",
        "defaults": {"win": 60},
        "unit": "日",
    },
}


def evaluate(bars: dict, key: str, codes, params: dict | None = None):
    """对一个池子跑单个因子 → (kept, dropped, score, marks, note)。"""
    spec = FACTORS.get(key)
    if not spec:
        raise KeyError("未登记因子：%s" % key)
    fn = spec["fn"]
    prm = dict(spec["defaults"])
    prm.update(params or {})
    kept, dropped, score, marks = [], {}, {}, {}
    for c in codes:
        b = bars.get(c)
        if not b:
            dropped[c] = "无本地日线（kbase 缺这只票）"
            continue
        if b["n"] < MIN_BARS:
            dropped[c] = "日线仅 %d 根（< %d，画不出形态）" % (b["n"], MIN_BARS)
            continue
        try:
            ok, sc, note = fn(b, prm)
        except Exception as e:  # noqa: BLE001
            log.exception("因子 %s 在 %s 上计算失败", key, c)
            dropped[c] = "计算异常：%s" % str(e)[:60]
            continue
        if not ok:
            dropped[c] = note
            continue
        kept.append(c)
        score[c] = sc
        marks[c] = note
    return kept, dropped, score, marks, "%s：%d 只 → %d 只" % (
        spec["name"], len(codes), len(kept))
