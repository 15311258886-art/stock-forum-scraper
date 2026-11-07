# -*- coding: utf-8 -*-
"""RL 择时可行性探针（一次性实验，不是产品代码 —— 2026-11-03）。

背景：进哥发来一个 FinRL（金融强化学习）的视频，问能不能拆成工具包里的工具。
FinRL 原版是美股 + Gym + stable-baselines3，而我们这里：torch 装了但 `torch.optim`
坏了（`torch._dynamo` 与 numpy 2.5.3 不兼容），只能手写 SGD；gym/SB3 都没装。

所以这个脚本只回答一个问题：**在我们这套 A 股数据上，RL 到底能不能学到东西？**
不能学，就不值得拆；能学，也得先看清它在样本外衰减多少。

三条防作弊纪律（这是 RL 量化最容易骗自己的地方）
------------------------------------------------
 1. **严格 T+1**：t 日收盘后决策 → t+1 日才生效。绝不允许"用当天收盘价决定当天买卖"。
 2. **特征只用 t 日及以前**：所有指标都是 rolling 出来的，天然不含未来。
 3. **一字板不成交**：t+1 日 high==low 且涨跌幅 >9.5%，视为无法执行，仓位保持不变。
    A 股涨跌停买不进卖不出的钱，回测里不能算成收益。

另外还有一层，比回测数字更硬：**把这 12 维状态逐维送进三期的因子门禁**。
门禁是 |IC|>=0.02 / |ICIR|>=0.30 / |t|>=2.5。如果一维都过不了，那结论不是
"RL 这个算法不行"，而是"这套日频特征里根本没有可学的信号"——换任何算法都白搭。
这个结论跟用什么算法无关，所以它比回测更有说服力。

数据切分（2026-11-03 实测校正）
------------------------------
kbase 里 2655 只票的日线完整覆盖 2023-04-27 → 2026-11-02（840+ 根）。
第一版以为只有 660 根（那是 2024 年才上市的活跃票，恰恰成交额排名靠前，
把候选池全占了 —— 教训：按成交额选票之前必须先按根数过滤）。

  预热 60 根（MA60 需要） → 训练 520 日 → 测试 250 日（整一年）

用法：python tools/exp_rl_probe.py [--n 300] [--episodes 300] [--per-ep 25]
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import time

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

KEEP = 850          # 取最近 850 个交易日
WARM = 60           # 预热（MA60 需要满 60 根）
TRAIN_DAYS = 520
TEST_DAYS = 250     # 整一年
NEED = WARM + TRAIN_DAYS + TEST_DAYS + 2      # 832，留 2 根余量

COST = 0.0010       # 单边综合成本（佣金+滑点）
STAMP = 0.0010      # 卖出印花税另计
LIMIT = 0.095
POS = (0.0, 0.5, 1.0)

GATE = {"abs_ic": 0.02, "abs_icir": 0.30, "abs_t": 2.50}


# ================================================================
# 数据
# ================================================================
def load(kb_path: str, n: int) -> tuple:
    """先按"根数够"筛出可用池，**再**在池内按近 120 日成交额排序取前 n 只。

    为什么必须先过滤根数：第一版是 `ORDER BY amt DESC LIMIT n*2` 再在 Python 里
    按 len 过滤，结果成交额前 600 的票几乎全是 2024 年才上市的（只有 650~661 根），
    一只都留不下，`segs` 为空直接崩。成交额排序和"数据够长"这两件事在 A 股里
    是负相关的（新股换手高），所以顺序很关键。
    """
    con = sqlite3.connect("file:%s?mode=ro" % kb_path, uri=True, timeout=20)
    dates = [r[0] for r in con.execute(
        "SELECT DISTINCT date FROM kline ORDER BY date DESC LIMIT ?", (KEEP,))]
    # dates 是**倒序**的（NEWEST FIRST）。第一版写 dates[-120:] 取到了最老的 120 天。
    d0, d_pick = min(dates), min(dates[:120])
    top = con.execute(
        "SELECT code, SUM(c*v) amt FROM kline WHERE date>=? GROUP BY code"
        " HAVING COUNT(*)>=? ORDER BY amt DESC LIMIT ?",
        (d0, NEED, n * 2)).fetchall()
    codes = [r[0] for r in top]
    if not codes:
        con.close()
        return {}, dates
    ph = ",".join("?" * len(codes))
    rows = con.execute(
        "SELECT code,date,o,h,l,c,v FROM kline WHERE date>=? AND code IN (%s)"
        " ORDER BY code,date" % ph, [d0] + codes).fetchall()
    con.close()
    by = {}
    for code, dt, o, h, l, c, v in rows:
        by.setdefault(code, []).append((dt, o, h, l, c, v))
    out = {}
    for code in codes:
        rr = by.get(code) or []
        if len(rr) < NEED:
            continue
        a = np.asarray([r[1:7] for r in rr], dtype=np.float64)
        out[code] = {"d": [r[0] for r in rr], "o": a[:, 0], "h": a[:, 1],
                     "l": a[:, 2], "c": a[:, 3], "v": a[:, 4]}
        if len(out) >= n:
            break
    return out, dates


# ================================================================
# 特征（12 维，全部只用 t 日及以前）
# ================================================================
FEAT_NAMES = ("5日动量", "20日动量", "60日动量", "20日波动率", "离MA20",
              "离MA60", "量比5/20", "20日位置", "RSI代理", "距20日高",
              "距20日低", "当前仓位")


def _ma(c, k):
    cs = np.cumsum(np.insert(c, 0, 0.0))
    out = np.full(len(c), np.nan)
    out[k - 1:] = (cs[k:] - cs[:-k]) / k
    return out


def features(b: dict) -> np.ndarray:
    """12 维状态，全部只用 t 日及以前。rolling 交给 pandas（C 实现），
    第一版用 Python 循环算 std/min/max，200 只票要 20 秒。"""
    import pandas as pd
    c = pd.Series(b["c"])
    h = pd.Series(b["h"])
    l = pd.Series(b["l"])
    v = pd.Series(b["v"])
    n = len(c)
    f = np.zeros((n, 12), dtype=np.float32)

    ma20 = c.rolling(20, min_periods=1).mean()
    ma60 = c.rolling(60, min_periods=1).mean()
    std20 = c.rolling(20, min_periods=1).std().fillna(0.0)
    hi20 = h.rolling(20, min_periods=1).max()
    lo20 = l.rolling(20, min_periods=1).min()
    d = c.diff().fillna(0.0)
    up = d.clip(lower=0).rolling(14, min_periods=1).sum()
    dn = (-d.clip(upper=0)).rolling(14, min_periods=1).sum()
    v5 = v.rolling(5, min_periods=1).mean()
    v20 = v.rolling(20, min_periods=1).mean()

    f[:, 0] = (c / c.shift(5) - 1).fillna(0) * 10
    f[:, 1] = (c / c.shift(20) - 1).fillna(0) * 5
    f[:, 2] = (c / c.shift(60) - 1).fillna(0) * 3
    f[:, 3] = (std20 / (c + 1e-12) * 20).fillna(0)
    f[:, 4] = ((c / ma20 - 1) * 10).fillna(0)
    f[:, 5] = ((c / ma60 - 1) * 5).fillna(0)
    f[:, 6] = ((v5 / (v20 + 1e-9) - 1).clip(-3, 3)).fillna(0)
    f[:, 7] = (((c - lo20) / (hi20 - lo20 + 1e-12)) * 2 - 1).fillna(0)
    f[:, 8] = ((up / (up + dn + 1e-12)) * 2 - 1).fillna(0)
    f[:, 9] = ((c / hi20 - 1) * 5).fillna(0)
    f[:, 10] = ((c / lo20 - 1) * 3).fillna(0)
    f[:, 11] = 0.0        # 当前仓位，每步由环境写入
    return np.nan_to_num(np.clip(f, -5, 5), nan=0.0, posinf=5.0, neginf=-5.0)


def tradable(b: dict) -> np.ndarray:
    c, h, l = b["c"], b["h"], b["l"]
    ret = np.zeros(len(c))
    ret[1:] = c[1:] / (c[:-1] + 1e-12) - 1
    flat = (h - l) <= (c * 1e-6 + 1e-9)
    return ~(flat & (np.abs(ret) > LIMIT))


# ================================================================
# 环境：一段连续交易日上的择时（三档仓位）
# ================================================================
class Seg:
    """在 [lo, hi) 上做择时。仓位状态属于实例，训练/测试各一个，互不串。"""

    def __init__(self, feat, c, tr, lo, hi):
        self.f, self.c, self.tr, self.lo, self.hi = feat, c, tr, lo, hi
        self.t = lo
        self.pos = 0.0

    def reset(self):
        self.t = self.lo
        self.pos = 0.0
        s = self.f[self.lo:self.lo + 1].copy()
        s[0, 11] = 0.0
        return s, 0.0

    def step(self, a):
        """t 日决策 → t+1 日收益。返回 (s2, r, done, pos)。"""
        t = self.t
        p_old = self.pos
        p = POS[a]
        if t + 1 >= self.hi:
            self.t = self.hi
            return self.f[t:t + 1].copy(), 0.0, True, p_old
        if not self.tr[t + 1]:
            p = p_old                                     # 一字板：想动也动不了
        nxt = self.c[t + 1] / (self.c[t] + 1e-12) - 1
        cost = COST * abs(p - p_old) + STAMP * max(0.0, p_old - p)
        r = p * nxt - cost
        self.pos = p
        self.t = t + 1
        s2 = self.f[self.t:self.t + 1].copy()
        s2[0, 11] = p
        return s2, float(r), self.t >= self.hi - 1, p

    def run(self, policy):
        """policy(s, pos) -> action。返回日收益序列。"""
        s, p = self.reset()
        out = []
        done = False
        while not done:
            s, r, done, p = self.step(policy(s, p))
            if done and r == 0.0 and self.t >= self.hi:
                break
            out.append(r)
        return np.asarray(out)


# ================================================================
# 特征门禁（复用三期的口径，自己算 Rank IC，不抄任何仓库的数字）
# ================================================================
def _rank(a: np.ndarray) -> np.ndarray:
    """平均秩（处理并列）。本机没有 scipy，手写一个。"""
    order = np.argsort(a, kind="mergesort")
    r = np.empty(len(a), dtype=np.float64)
    r[order] = np.arange(1, len(a) + 1, dtype=np.float64)
    # 并列取平均秩
    sa = a[order]
    i = 0
    while i < len(sa):
        j = i
        while j + 1 < len(sa) and sa[j + 1] == sa[i]:
            j += 1
        if j > i:
            r[order[i:j + 1]] = (i + j) / 2.0 + 1.0
        i = j + 1
    return r


def _spearman(x: np.ndarray, y: np.ndarray) -> float:
    if len(x) < 30:
        return float("nan")
    rx, ry = _rank(x), _rank(y)
    rx = rx - rx.mean()
    ry = ry - ry.mean()
    den = np.sqrt((rx * rx).sum() * (ry * ry).sum())
    if den <= 0:
        return float("nan")
    return float((rx * ry).sum() / den)


def build_panel(data: dict, lo: int, hi: int) -> tuple:
    """把逐票序列摊成「日期 × 票 × 维度」面板。

    对齐方式：按**日期**对齐，不按位置 —— 不同票的缺失日不同，按位置会把不同
    日历日的票凑成一个截面，算出来的 IC 是假的。
    """
    codes = list(data.keys())
    dset = set()
    for d in data.values():
        dset.update(d["b"]["d"][lo:hi])
    dates = sorted(dset)
    di = {x: i for i, x in enumerate(dates)}
    nS, nD = len(codes), len(dates)
    Fa = np.full((nD, nS, 12), np.nan, dtype=np.float64)
    Ra = np.full((nD, nS), np.nan, dtype=np.float64)
    for j, code in enumerate(codes):
        b, f = data[code]["b"], data[code]["f"]
        c = b["c"]
        hi2 = min(hi, len(c) - 1)
        if hi2 <= lo:
            continue
        idx = np.array([di[x] for x in b["d"][lo:hi2]])
        Fa[idx, j, :] = f[lo:hi2]
        Ra[idx, j] = c[lo + 1:hi2 + 1] / c[lo:hi2] - 1      # t -> t+1 的收益
    return dates, codes, Fa, Ra


def dim_crowding(Fa: np.ndarray) -> list:
    """这 11 维彼此有多像。返回每维与其余维中的最大 |相关|。

    为什么必须看这个：如果 11 维里 8 维互相关 0.8，那"12 维状态"就是个说法，
    真正独立的信号可能只有一两个 —— RL 再怎么学也只能从这一两个里学。
    """
    X = Fa.reshape(-1, 12)[:, :11]
    m = np.isfinite(X).all(axis=1)
    X = X[m]
    if len(X) < 100:
        return []
    C = np.corrcoef(X, rowvar=False)
    out = []
    for k in range(11):
        row = np.abs(C[k]).copy()
        row[k] = 0.0
        out.append(float(row.max()))
    return out


def gate_scan(Fa: np.ndarray, Ra: np.ndarray) -> list:
    """在面板上逐维算 Rank IC（按日期截面，与三期因子门禁同一口径）。"""
    nD = Fa.shape[0]
    out = []
    for k in range(11):                                    # 第 12 维是仓位，不算
        ics = []
        for i in range(nD):
            x, y = Fa[i, :, k], Ra[i, :]
            m = np.isfinite(x) & np.isfinite(y)
            if m.sum() < 50:
                continue
            v = _spearman(x[m], y[m])
            if np.isfinite(v):
                ics.append(v)
        if len(ics) < 20:
            out.append({"k": k, "n": len(ics), "ic": 0.0, "icir": 0.0,
                        "t": 0.0, "ok": False, "why": "有效截面天数不足 20"})
            continue
        a = np.asarray(ics)
        ic, sd = float(a.mean()), float(a.std(ddof=1))
        icir = ic / sd if sd > 0 else 0.0
        t = icir * np.sqrt(len(a))
        why = []
        if abs(ic) < GATE["abs_ic"]:
            why.append("IC %.4f 不到 %.2f" % (ic, GATE["abs_ic"]))
        if abs(icir) < GATE["abs_icir"]:
            why.append("ICIR %.2f 不到 %.2f" % (icir, GATE["abs_icir"]))
        if abs(t) < GATE["abs_t"]:
            why.append("t %.2f 不到 %.1f" % (t, GATE["abs_t"]))
        out.append({"k": k, "n": len(a), "ic": ic, "icir": icir, "t": t,
                    "ok": not why, "why": "、".join(why)})
    return out


# ================================================================
# DQN（手写 SGD —— torch.optim 在本机不可用）
# ================================================================
def build_net(hidden=128):
    import torch
    return torch.nn.Sequential(
        torch.nn.Linear(12, hidden), torch.nn.ReLU(),
        torch.nn.Linear(hidden, hidden), torch.nn.ReLU(),
        torch.nn.Linear(hidden, 3))


def train(segs, episodes=300, per_ep=25, batch=256, gamma=0.95, lr=3e-4, seed=7):
    """每跑完一条 episode 做 per_ep 次梯度更新。

    ★ 第一版把"步"当成梯度步，结果每次"一步"都跑完一整条 520 天的 episode，
    12000 步 = 600 万次环境交互，跑了半小时没出来。这里改成 episodes × per_ep：
    300 条 episode ≈ 15 万次交互 + 7500 次梯度更新。
    """
    import torch
    torch.manual_seed(seed)
    rng = np.random.default_rng(seed)
    net, tgt = build_net(), build_net()
    tgt.load_state_dict(net.state_dict())
    buf = []
    eps = 1.0
    order = []
    for ep in range(episodes):
        if not order:
            order = list(rng.permutation(len(segs)))
        seg = segs[int(order.pop())]
        s, pos = seg.reset()
        done = False
        while not done:
            if rng.random() < eps:
                a = int(rng.integers(0, 3))
            else:
                with torch.no_grad():
                    a = int(torch.argmax(net(torch.from_numpy(s))).item())
            s2, r, done, pos = seg.step(a)
            buf.append((s[0], a, r, s2[0], float(done)))
            s = s2
            if len(buf) > 40000:
                del buf[:8000]
        eps = max(0.05, eps * 0.995)
        if len(buf) < batch:
            continue
        for _ in range(per_ep):
            idx = rng.integers(0, len(buf), batch)
            S = torch.from_numpy(np.stack([buf[j][0] for j in idx]))
            S2 = torch.from_numpy(np.stack([buf[j][3] for j in idx]))
            A = torch.tensor([buf[j][1] for j in idx], dtype=torch.long)
            R = torch.tensor([buf[j][2] for j in idx], dtype=torch.float32)
            D = torch.tensor([buf[j][4] for j in idx], dtype=torch.float32)
            q = net(S).gather(1, A.view(-1, 1)).squeeze(1)
            with torch.no_grad():
                y = R + gamma * (1 - D) * tgt(S2).max(1).values
            torch.nn.functional.smooth_l1_loss(q, y).backward()
            with torch.no_grad():
                for p_ in net.parameters():
                    if p_.grad is not None:
                        p_ -= lr * p_.grad
            net.zero_grad(set_to_none=True)
        if ep % 10 == 0:
            tgt.load_state_dict(net.state_dict())
    return net


def rl_policy(net, acts_out=None):
    import torch

    def pol(s, pos):
        with torch.no_grad():
            a = int(torch.argmax(net(torch.from_numpy(s))).item())
        if acts_out is not None:
            acts_out.append(a)
        return a
    return pol


# ================================================================
# 基准
# ================================================================
def bench_hold(b, lo, hi):
    c = b["c"]
    r = np.zeros(hi - lo)
    r[:] = c[lo + 1:hi + 1] / c[lo:hi] - 1
    return r


def bench_ma20(b, lo, hi):
    c = b["c"]
    ma = _ma(c, 20)
    tr = tradable(b)
    r, pos = [], 0.0
    for t in range(lo, hi):
        p = 1.0 if (ma[t] == ma[t] and c[t] > ma[t]) else 0.0
        nxt = c[t + 1] / (c[t] + 1e-12) - 1
        if not tr[t + 1]:
            p = pos
        cost = COST * abs(p - pos) + STAMP * max(0.0, pos - p)
        r.append(p * nxt - cost)
        pos = p
    return np.asarray(r)


def med_by_date(Fa: np.ndarray, dates: list, k: int) -> dict:
    """每一天、第 k 维在截面上的中位数。用于「比别人好就拿住」这种最笨的规则。"""
    out = {}
    for i, d in enumerate(dates):
        col = Fa[i, :, k]
        col = col[np.isfinite(col)]
        out[d] = float(np.median(col)) if len(col) >= 30 else None
    return out


def bench_feat(b, f, lo, hi, k: int, sign: float, med: dict) -> np.ndarray:
    """最笨的单因子择时：当日该维**优于截面中位数**就满仓，否则空仓。

    sign 由 IC 符号决定（IC<0 表示越大越该卖，所以规则要反过来）。
    这一条基准存在的意义：如果它就能打到 RL 的水平，那 RL 的那套训练毫无必要。
    """
    c = b["c"]
    tr = tradable(b)
    ds = b["d"]
    r, pos = [], 0.0
    for t in range(lo, hi):
        mv = med.get(ds[t])
        if mv is None:
            p = pos
        else:
            p = 1.0 if (f[t, k] - mv) * sign > 0 else 0.0
        nxt = c[t + 1] / (c[t] + 1e-12) - 1
        if not tr[t + 1]:
            p = pos
        cost = COST * abs(p - pos) + STAMP * max(0.0, pos - p)
        r.append(p * nxt - cost)
        pos = p
    return np.asarray(r)


# ================================================================
# 指标
# ================================================================
def stats(rets: np.ndarray, days: int) -> dict:
    if len(rets) == 0:
        return {"ann": 0.0, "sharpe": 0.0, "mdd": 0.0}
    eq = np.cumprod(1 + rets)
    ann = eq[-1] ** (250.0 / days) - 1 if eq[-1] > 0 else -1.0
    sd = rets.std(ddof=1) if len(rets) > 1 else 0.0
    sharpe = rets.mean() / sd * np.sqrt(250) if sd > 0 else 0.0
    peak = np.maximum.accumulate(eq)
    return {"ann": float(ann), "sharpe": float(sharpe),
            "mdd": float((eq / peak - 1).min())}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=300)
    ap.add_argument("--episodes", type=int, default=300)
    ap.add_argument("--per-ep", type=int, default=25)
    ap.add_argument("--seed", type=int, default=7)
    a = ap.parse_args()

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    kb = os.path.join(root, "viewer", "kbase.db")
    t0 = time.time()
    bars, alldates = load(kb, a.n)
    print("取数 %d 只（窗口 %s ~ %s），耗时 %.1fs" % (
        len(bars), min(alldates), max(alldates), time.time() - t0))
    if not bars:
        print("没取到票，终止")
        return 1

    t1 = time.time()
    data = {}
    for code, b in bars.items():
        if len(b["c"]) < NEED:
            continue
        data[code] = {"b": b, "f": features(b), "tr": tradable(b)}
    print("可用 %d 只，算特征耗时 %.1fs（训练 %d 日 / 测试 %d 日 / 预热 %d 日）" % (
        len(data), time.time() - t1, TRAIN_DAYS, TEST_DAYS, WARM))
    if not data:
        print("没有够长的票，终止")
        return 1

    lo1, hi1 = WARM, WARM + TRAIN_DAYS
    lo2, hi2 = hi1, hi1 + TEST_DAYS

    # ---------- 1. 特征门禁：这套特征里到底有没有信号 ----------
    print("\n" + "=" * 78)
    print("一、12 维状态逐维过三期因子门禁（|IC|>=%.2f / |ICIR|>=%.2f / |t|>=%.1f）"
          % (GATE["abs_ic"], GATE["abs_icir"], GATE["abs_t"]))
    print("=" * 78)
    dtr, ctr, Ftr, Rtr = build_panel(data, lo1, hi1)
    dte, cte, Fte, Rte = build_panel(data, lo2, hi2)
    crow = dim_crowding(Ftr)
    gtr, gte = gate_scan(Ftr, Rtr), gate_scan(Fte, Rte)
    for tag, g in (("训练期", gtr), ("测试期", gte)):
        print("\n[%s] %d 个截面日" % (tag, g[0]["n"] if g else 0))
        print("  %-12s %8s %7s %7s %7s  %s" % ("维度", "IC", "ICIR", "t", "最像另一维", "门禁"))
        for r in sorted(g, key=lambda x: -abs(x["ic"])):
            print("  %-12s %+8.4f %+7.2f %+7.2f %+7.2f  %s" % (
                FEAT_NAMES[r["k"]], r["ic"], r["icir"], r["t"],
                crow[r["k"]] if crow else 0.0,
                "过" if r["ok"] else "拒（%s）" % r["why"]))
        passed = [r for r in g if r["ok"]]
        mb = max(abs(r["ic"]) for r in g) if g else 0.0
        print("  → 过门禁 %d / 11 维，最强单维 |IC| = %.4f（门禁线 %.2f）" % (
            len(passed), mb, GATE["abs_ic"]))
    if crow:
        hi_c = sum(1 for x in crow if x >= 0.70)
        print("\n  11 维之间：%d 维与另一维的相关 >= 0.70（跟三期因子门禁的撞车线同口径），"
              "平均最像 %.2f" % (hi_c, float(np.mean(crow))))
        print("  → 名义 11 维、实际独立的信号远少于此。RL 能学的信息量被这一步就框死了。")

    # 选一维做主基准：**按训练期 IC 选**，测试期只用来验证，不参与选择
    best = max(gtr, key=lambda x: abs(x["ic"]))
    bk, bsign = best["k"], (1.0 if best["ic"] > 0 else -1.0)
    print("\n  单因子基准用训练期最强的一维：%s（IC %+.4f，方向 %s），"
          "测试期该维 IC %+.4f" % (
              FEAT_NAMES[bk], best["ic"], "越大越拿" if bsign > 0 else "越大越躲",
              [x for x in gte if x["k"] == bk][0]["ic"]))
    med_tr = med_by_date(Ftr, dtr, bk)
    med_te = med_by_date(Fte, dte, bk)

    # ---------- 2. 训练 ----------
    segs = [Seg(d["f"], d["b"]["c"], d["tr"], lo1, hi1) for d in data.values()]
    t2 = time.time()
    net = train(segs, episodes=a.episodes, per_ep=a.per_ep, seed=a.seed)
    print("\n" + "=" * 78)
    print("二、训练 DQN：%d 条 episode / %d 次梯度更新，耗时 %.1fs（seed=%d）" % (
        a.episodes, a.episodes * a.per_ep, time.time() - t2, a.seed))
    print("=" * 78)

    def evaluate(lo, hi, tag, med):
        rows = []
        acts = []
        for code, d in data.items():
            seg = Seg(d["f"], d["b"]["c"], d["tr"], lo, hi)
            r = seg.run(rl_policy(net, acts))
            rows.append((r, bench_hold(d["b"], lo, hi), bench_ma20(d["b"], lo, hi),
                         bench_feat(d["b"], d["f"], lo, hi, bk, bsign, med)))
        names = ("RL 择时", "满仓持有", "MA20 择时", "单因子择时")
        print("\n[%s] %d 个交易日，%d 只票各自独立跑再平均" % (tag, hi - lo, len(rows)))
        base = np.array([stats(x[1], hi - lo)["ann"] for x in rows])
        res = {}
        for k, nm in enumerate(names):
            s = [stats(x[k], hi - lo) for x in rows]
            ann = np.array([q["ann"] for q in s])
            res[nm] = (ann.mean(), np.mean([q["sharpe"] for q in s]),
                       np.mean([q["mdd"] for q in s]))
            print("  %-8s 年化 %+7.2f%%   夏普 %+6.2f   最大回撤 %6.2f%%   跑赢满仓 %5.1f%%" % (
                nm, ann.mean() * 100,
                np.mean([q["sharpe"] for q in s]),
                np.mean([q["mdd"] for q in s]) * 100,
                float((ann > base).mean()) * 100))
        return res, acts

    test_res, test_acts = evaluate(lo2, hi2, "样本外 · 测试集（网络完全不动）", med_te)
    train_res, _ = evaluate(lo1, hi1, "样本内 · 训练集（只看拟合，不认收益）", med_tr)

    import collections
    cnt = collections.Counter(test_acts)
    tot = max(1, len(test_acts))
    print("\nRL 在测试集的仓位分布：空仓 %.1f%% · 半仓 %.1f%% · 满仓 %.1f%%" % (
        cnt[0] / tot * 100, cnt[1] / tot * 100, cnt[2] / tot * 100))

    print("\n" + "=" * 78)
    print("三、结论所需的三组对照")
    print("=" * 78)
    tr_rl, tr_hd = train_res["RL 择时"][0], train_res["满仓持有"][0]
    te_rl, te_hd = test_res["RL 择时"][0], test_res["满仓持有"][0]
    print("  训练集 RL %+.2f%% vs 满仓 %+.2f%% → 差 %+.2f 个点" % (
        tr_rl * 100, tr_hd * 100, (tr_rl - tr_hd) * 100))
    print("  测试集 RL %+.2f%% vs 满仓 %+.2f%% → 差 %+.2f 个点" % (
        te_rl * 100, te_hd * 100, (te_rl - te_hd) * 100))
    print("  RL 自身 训练→测试 衰减 %+.2f 个点" % ((te_rl - tr_rl) * 100))
    print("  测试集 单因子 %+.2f%% vs RL %+.2f%% → 差 %+.2f 个点（夏普 %+.2f vs %+.2f）" % (
        test_res["单因子择时"][0] * 100, te_rl * 100,
        (test_res["单因子择时"][0] - te_rl) * 100,
        test_res["单因子择时"][1], test_res["RL 择时"][1]))
    print("\n总耗时 %.1fs" % (time.time() - t0))
    return 0


if __name__ == "__main__":
    sys.exit(main())
