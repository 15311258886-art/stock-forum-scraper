# -*- coding: utf-8 -*-
"""因子信号回测（P4，2026-10-26；2026-10-29 加扣费口径）。

治 C5（信号无回测、等权）：把 P1 因子层产生的买卖信号在本地历史 K 线上做前向收益回测，
量化每个信号因子（RSI超卖/WR超卖/MACD金叉/KDJ/PDI多头/Supertrend/CCI…）的真实胜率/平均收益/夏普。

数据全在本地 stock_indicators_daily（code,date PK + close + 52 因子，全市场 ~135 万行历史），不依赖出网。
前向收益直接取同表 close[i+fwd]/close[i]-1，无需再读 kline 库。

2026-10-29 两处改动（都由「信号战绩」上线后的实测数字倒逼出来）：

  1) **扣费口径**。此前所有 avg_ret / 超额都是**税前**。实测 T+1 平均只有 -0.586%，
     T+5 -3.105%，而双边成本（佣金+印花税+滑点）保守 0.15%。不扣费的结论会系统性地
     高估信号——尤其 T+1，0.15% 足以把「微幅正超额」直接翻成负。默认 fee=0.0015，可改。

  2) **逐格取值提速**。原写法 `df[c].iloc[i]` 是 pandas 标量访问，17 列 × 250 行 × 5300 只
     ≈ 2260 万次，单次约 5µs，占掉绝大部分耗时（全量实测 11.8 分钟）。
     改为一次性转 numpy 后按行列取值，数值完全等价（NaN/inf 同样转 None）。

用法：
    .venv/Scripts/python.exe -m viewer.signal_backtest                 # 全量回测（默认回看250日，前向5/10/20日）
    .venv/Scripts/python.exe -m viewer.signal_backtest --fee 0.003    # 用更保守的成本假设重算
    .venv/Scripts/python.exe -m viewer.signal_backtest --limit 200 --lookback 120   # 小批量自测
落库 signal_backtest(...) + signal_baseline(...)，含税前/税后两套指标。
"""
import os
import sys
import time
import sqlite3
import math
import argparse

import numpy as np
import pandas as pd

from viewer import units as _U
from viewer.kernel import params as _P

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

# 双边综合成本：佣金万2.5×2 + 卖出印花税万5 + 滑点（保守）= 约 0.15%
# 2026-10-31：值提到 `params.BACKTEST_FEE_ROUNDTRIP`（成本口径表要和它并列展示），
# 这里保留名字是为了不动所有调用方；**不要在此另写数字**。
DEFAULT_FEE = _P.BACKTEST_FEE_ROUNDTRIP


# 2026-10-31：口径版本戳。凡数学口径变化（年化因子 / 方向符号 / 成交价）都必须改这个值，
# 否则同一张表里会同时存在两种不可比的数字，而它们长得一模一样。
CALC_VERSION = "v3-t1open-netsign"


def _table_exists(con: sqlite3.Connection, table: str) -> bool:
    return con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone() is not None


# 写回语句里出现的全部字段。放在模块级，是为了让"插入的字段"和"校验的字段"
# 只有一份来源 —— 两处各写一遍，加列时必然只改一处，这正是这次事故的成因。
_BT_COLS = ("reason", "direction", "fwd", "n", "winrate", "avg_ret", "sharpe",
            "updated", "fee", "winrate_net", "avg_ret_net", "sharpe_net",
            "t_naive", "t_adj", "n_eff", "calc_version", "p_raw", "q_bh", "n_codes",
            "lookback_days")
_RG_COLS = ("reason", "direction", "fwd", "regime", "n", "winrate", "avg_ret",
            "sharpe", "winrate_net", "avg_ret_net", "sharpe_net", "edge_wr",
            "edge_ret", "fee", "updated", "calc_version", "lookback_days")
# 信号交集表（2026-10-31 决策③）。字段口径与 signal_backtest 一致，
# 差别只在主键多了 reason_b、以及两个"跟谁比"的边：
#   edge_vs_base  —— 相对同窗口全市场基线（回答"能不能用来选股"）
#   edge_vs_parts —— 相对两个成分信号的加权平均（回答"组合是否真的优于单独用"）
# 两个都要，因为前者可能为正而后者为负：那说明赚钱来自市况，不是来自组合。
_CB_COLS = ("reason_a", "reason_b", "direction", "fwd", "n", "winrate", "avg_ret",
            "sharpe", "winrate_net", "avg_ret_net", "sharpe_net", "t_naive",
            "t_adj", "n_eff", "p_raw", "q_bh", "edge_vs_base", "edge_vs_parts",
            "part_a_ret_net", "part_b_ret_net", "usable", "calc_version", "fee",
            "updated", "lookback_days")


def _ensure_col(con: sqlite3.Connection, table: str, col: str, decl: str) -> None:
    """幂等加列（老库平滑升级，不重建表、不丢历史值）。

    2026-10-31 整改（线上事故复现）：原实现把 ALTER 的失败 `except: pass` 掉了。
    实测后果 —— 某轮全量回测跑到最后一句 INSERT 才炸：
        sqlite3.OperationalError: table signal_backtest has no column named n_codes
    也就是**5,337 只票算完（十几分钟）之后，因为一个开头就能发现的问题全部作废**。
    迁移失败必须当场说话：只有"列已存在"可以忽略，其它一律冒泡。
    """
    try:
        cols = [d[1] for d in con.execute("PRAGMA table_info(%s)" % table).fetchall()]
    except sqlite3.Error as e:
        raise RuntimeError("读表结构失败 table=%s: %s" % (table, e)) from e
    if col not in cols:
        try:
            con.execute("ALTER TABLE %s ADD COLUMN %s %s" % (table, col, decl))
        except sqlite3.OperationalError as e:
            # 只有"列已存在"可以忽略（幂等）；其它一律包成 RuntimeError 冒泡。
            if "duplicate column" not in str(e).lower():
                raise RuntimeError(
                    "补列失败 table=%s col=%s: %s" % (table, col, e)) from e


def _assert_schema(con: sqlite3.Connection, table: str, needed) -> None:
    """建表/补列之后的**后置断言**。

    为什么要这一步：`_ensure_col` 即使不吞异常，也可能因为"当时库被锁"而失败，
    而失败发生的时刻与"用不上这一列"的时刻相隔极远。回测结尾一次性校验，
    才能把代价从"白跑一遍"压回"启动即失败"。
    """
    cols = {d[1] for d in con.execute("PRAGMA table_info(%s)" % table).fetchall()}
    missing = [c for c in needed if c not in cols]
    if missing:
        raise RuntimeError(
            "表 %s 缺少字段 %s —— 迁移没成功，拒绝继续（继续下去会在结果写回时才失败，"
            "那时全部计算已作废）。请检查库是否被其它进程占用。" % (table, missing))


def ensure_tables(con: sqlite3.Connection):
    con.execute("""CREATE TABLE IF NOT EXISTS signal_backtest (
        reason TEXT NOT NULL, direction TEXT NOT NULL, fwd INTEGER NOT NULL,
        n INTEGER, winrate REAL, avg_ret REAL, sharpe REAL, updated TEXT,
        PRIMARY KEY (reason, direction, fwd))""")
    # 2026-10-29：基线必须落库。
    # 原因：此前 base 只在控制台打印，导致「超额胜率」这个唯一能判断信号有无 alpha 的指标
    # 无法被任何下游消费。没有基线时，一个 48% 胜率的信号看着"过半"，而全市场 20 日
    # 基线胜率本来就是 45~48% —— 等于没有信息。落库后 evidence 层才能算 edge。
    con.execute("""CREATE TABLE IF NOT EXISTS signal_baseline (
        fwd INTEGER PRIMARY KEY, n INTEGER, winrate REAL, avg_ret REAL,
        updated TEXT)""")
    # 2026-10-29：扣费口径（老库用 ALTER 平滑升级）
    for t in ("signal_backtest", "signal_baseline"):
        _ensure_col(con, t, "fee", "REAL DEFAULT 0")
        _ensure_col(con, t, "winrate_net", "REAL")
        _ensure_col(con, t, "avg_ret_net", "REAL")
    _ensure_col(con, "signal_backtest", "sharpe_net", "REAL")
    # 2026-10-31 整改（测评 P0-C）：新增三个字段 + 一个口径版本戳。
    # 为什么必须有 calc_version：旧的 sharpe 是"按 √244 年化的 fwd 日收益"，
    # 新的是"按 √(244/fwd) 年化的"，两者**不可比**。没有版本戳的话，
    # 同一张表里会同时躺着两种口径的数字，而它们长得一模一样。
    for _c in ("t_naive", "t_adj"):
        _ensure_col(con, "signal_backtest", _c, "REAL")
    _ensure_col(con, "signal_backtest", "n_eff", "INTEGER")
    _ensure_col(con, "signal_backtest", "calc_version", "TEXT")
    _ensure_col(con, "signal_regime", "calc_version", "TEXT")
    # 2026-10-31 整改（测评 P1「多重比较」项）：新增 BH 校正后的 q 值。
    # 为什么必须有它：这张表一次跑出 51 个（方向×原因×前向）组合、153 个分状态格，
    # 全部在做"这个 t 显著吗"的检验。检验 51 次、每次用 0.05 的线，**期望误报 2.5 个**，
    # 而报告里被引用的恰恰是"最显著那几个"。不校正就等于从 51 个噪声里挑最好看的那个。
    # q_bh 的含义是"这一族检验里，q<0.05 可以期望假发现占比 ≤5%"，与单次 p 不是一回事。
    _ensure_col(con, "signal_backtest", "q_bh", "REAL")
    _ensure_col(con, "signal_backtest", "p_raw", "REAL")
    # 2026-10-31 整改（测评「分母必须是被扫描的域」项）：补 n_codes。
    # 这张表按 (reason, direction, fwd) 聚合，**聚合了几只票从来没被记录过**。
    # 后果很具体：一次 `backtest(limit=60)` 的调试运行会用 60 只的聚合
    # `INSERT OR REPLACE` 掉全市场 5,337 只的结果，而两批数字长得一模一样 ——
    # 面板照读不误，没人能发现"这行夏普其实只来自 60 只"。
    # 有了 n_codes，"这个数覆盖多大范围"就和数字本身一起存在，不靠外部记忆。
    _ensure_col(con, "signal_backtest", "n_codes", "INTEGER")
    # 2026-10-31：样本窗口也成了"隐藏口径"。
    # 事实经过：同一个 calc_version（v3-t1open-netsign），窗口 250 日（1 年）的结论是
    # 「9 个信号只有 1 个正超额、基线为负」，窗口 1250 日（5 年）的结论是
    # 「4 个正超额、基线转正、3 个在弱势稳健有效」。**结论完全相反**，
    # 但两张表除了数字什么都一样，谁也分不出哪个是几年的样本。
    # calc_version 挡的是"数学口径变了"，挡不住"样本窗口变了" —— 同一类病，换个字段又犯一次。
    _ensure_col(con, "signal_backtest", "lookback_days", "INTEGER")
    # signal_regime 由 viewer/regime.py 建表，本函数只负责给它补列。
    # 表还没建时**跳过**（不是吞错）：调用方紧接着会 RG.ensure_tables 建表，
    # 建完再回补。这里显式判存在，是为了不把"表根本没有"和"ALTER 被锁失败"
    # 混成同一个沉默分支 —— 后者必须炸。
    if _table_exists(con, "signal_regime"):
        _ensure_col(con, "signal_regime", "calc_version", "TEXT")
        _ensure_col(con, "signal_regime", "lookback_days", "INTEGER")
    # 2026-10-31 决策③：信号两两交集。
    # 单信号全负的时候，「两个负的叠在一起会不会变正」是个真问题 ——
    # 但也正因为它是"从 51 个格子里再挑好的"，多重比较的风险比单信号大得多
    # （9 信号 × C(9,2)=36 × 2 方向 × 3 窗口 = 216 个检验）。所以：
    #   · 单独做一族 BH，不和单信号混算（混算会把单信号的门槛也拉高）；
    #   · usable 列显式标记样本是否够，禁止下游拿 n 很小的组合当结论。
    con.execute("""CREATE TABLE IF NOT EXISTS signal_combo (
        reason_a TEXT NOT NULL, reason_b TEXT NOT NULL, direction TEXT NOT NULL,
        fwd INTEGER NOT NULL, n INTEGER, winrate REAL, avg_ret REAL, sharpe REAL,
        winrate_net REAL, avg_ret_net REAL, sharpe_net REAL,
        t_naive REAL, t_adj REAL, n_eff INTEGER, p_raw REAL, q_bh REAL,
        edge_vs_base REAL, edge_vs_parts REAL, part_a_ret_net REAL,
        part_b_ret_net REAL, usable INTEGER, calc_version TEXT, fee REAL,
        updated TEXT, lookback_days INTEGER,
        PRIMARY KEY (reason_a, reason_b, direction, fwd))""")
    # 老库补列（DDL 之后才 ALTER：表不存在时 ALTER 会炸，而不是"跳过"）
    _ensure_col(con, "signal_combo", "lookback_days", "INTEGER")
    con.commit()
    # 后置断言：INSERT 里用到几个字段，这里就验几个字段。
    # 只要有一列没加上，就在**启动时**失败，而不是在几十分钟后写回时失败。
    _assert_schema(con, "signal_backtest", _BT_COLS)
    if _table_exists(con, "signal_regime"):
        _assert_schema(con, "signal_regime", _RG_COLS)
    _assert_schema(con, "signal_combo", _CB_COLS)


def _sharpe(returns, fwd: int = 1) -> float:
    """年化夏普。**fwd 必须传，否则等于把 fwd 日收益当日收益年化。**

    2026-10-31 整改（测评 P0-C ① / 硬伤 1）：原实现的因子是写死的 √244。
    20 日持有期一年只能重复 244/20 ≈ 12 次，正确因子是 √(244/20) = 3.49，
    而不是 √244 = 15.62 —— 比值恰为 √20 = 4.47。
    这解释了界面上那个 1.076：它不是夏普，是一个被放大了 √fwd 倍的数。
    同一行代码在 fwd=1 时是对的，换了持有期就错，而代码一个字没改，只有输入变了
    —— 所以这类缺陷靠 code review 抓不住，只能靠"单位写进参数名"抓住。
    """
    if len(returns) < 2:
        return 0.0
    a = np.asarray(returns, dtype=float)
    sd = a.std(ddof=1)
    if sd == 0 or np.isnan(sd):
        return 0.0
    return float(a.mean() / sd * math.sqrt(244.0 / max(1, int(fwd))))


# ── 2026-10-29：改为「累积统计量」而非「存全量收益列表」─────────────────────────
# 动机：要按市场状态分桶（3 档 × 51 组），若每笔收益都 append 成 list，内存里会同时
# 存在总表 + 分状态表两份 ~400 万 float，且跨桶合并时还会临时翻倍。
# 用 [n, sum, sumsq, wins_pre, wins_net] 五个数就能还原全部指标：
#   avg = sum/n ； sd(ddof=1) = sqrt((sumsq - n*avg²)/(n-1)) ；
#   税后胜率 = P(ret > fee)（因为 ret-fee>0 ⟺ ret>fee），与旧口径 np.mean(net>0) 等价。
# 副作用是内存恒定、且总表与分状态表来自同一次遍历，不存在两份数字对不上的可能。
def _dir_ret(ret_raw: float, direction: str) -> float:
    """按方向给收益定符号。抽成独立函数**只为可测**。

    2026-10-31 整改（测评 P0-C ②）：原实现对 buy / sell 用同一个收益，
    做空被当成做多记账 —— 而这类符号错误有三个特点：
      ① 不报错、不告警；② 方向永远对自己有利（看空信号也显示赚钱）；
      ③ 从结果分布上看不出来，只能从"符号该不该翻"这一步看出来。
    所以它必须被一条能跑的断言盯着，而不是被一段注释提醒。
    """
    return ret_raw if direction == "buy" else -ret_raw


def _p_two_sided(t):
    """双侧 p 值（正态近似）。

    为什么用正态近似而不是 t 分布：`t_adj` 已经把重叠样本折掉了 √fwd，
    n_eff 通常几百到几千，t 与正态在此区间差别在千分位；真用 t 分布要引 scipy ——
    为一个千分位增一个重依赖不划算。这个选择写在这里，否则下一个人会以为 p 是精确的。
    """
    if t is None:
        return None
    try:
        x = float(t)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(x):
        return None
    return math.erfc(abs(x) / math.sqrt(2.0))


def _bh_adjust(pairs):
    """Benjamini-Hochberg 校正。输入 [(key, p) ...]，返回 {key: q 或 None}。

    做法：按 p 升序，q_(i) = p_(i) × m / i，再**强制单调**（q_(i) = min_{j≥i} q_(j)）。
    不强制单调会输出"更显著的项 q 反而更大"这种自相矛盾的结果，比不校正更糟。
    p 为 None 的项不进分母 m —— 它们不是"检验过"，而是"算不出来"，
    混进去会把 q 稀释掉。
    """
    pairs = list(pairs or [])
    out = {k: None for k, _p in pairs}
    ok = [(k, float(p)) for k, p in pairs if p is not None]
    m = len(ok)
    if m == 0:
        return out
    ok.sort(key=lambda x: x[1])
    qs = [(k, min(1.0, p * m / i)) for i, (k, p) in enumerate(ok, 1)]
    running = 1.0
    for i in range(len(qs) - 1, -1, -1):
        k, q = qs[i]
        running = min(running, q)
        out[k] = running
    return out


def _bump(store: dict, key, ret: float, fee: float) -> None:
    b = store.get(key)
    if b is None:
        b = [0, 0.0, 0.0, 0, 0]
        store[key] = b
    b[0] += 1
    b[1] += ret
    b[2] += ret * ret
    if ret > 0.0:
        b[3] += 1
    if ret > fee:
        b[4] += 1


def _stats(b, fee: float, fwd: int = 1):
    """由五个累积量还原全部指标。

    2026-10-31 整改（测评 P0-C ①）：`sharpe` 的年化因子按 fwd 走 √(244/fwd)。
    同时新增三个字段，因为"只给一个夏普"本身就是在制造确定性：
      · t_naive —— 把 n 个样本当独立样本算出的 t（重叠抽样下**偏乐观**，放大约 √fwd）；
      · t_adj   —— 除以 √fwd 的重叠修正 t（偏保守）。**判显著性只许看这个**。
      · n_eff   —— 有效独立样本数 ≈ n/fwd。它和 n 差几倍，就直接说明"样本量"那个数
                   不能拿来当置信度用（judge 那条 JUDGE_MIN_N 门槛尤其要注意）。
    """
    n, s, ss, w, wn = b
    if n < 1:
        return None
    avg = s / n
    var = (ss - n * avg * avg) / (n - 1) if n > 1 else 0.0
    sd = math.sqrt(var) if var > 0 else 0.0
    h = max(1, int(fwd))
    sh = (avg / sd * math.sqrt(244.0 / h)) if sd > 1e-12 else 0.0
    t_naive = (avg / (sd / math.sqrt(n))) if (sd > 1e-12 and n > 1) else 0.0
    t_adj = t_naive / math.sqrt(h)
    return {"n": n, "winrate": w / n, "avg_ret": avg, "sharpe": sh,
            "winrate_net": wn / n, "avg_ret_net": avg - fee, "sharpe_net": sh,
            "t_naive": t_naive, "t_adj": t_adj, "n_eff": int(n / h)}


def backtest(lookback_days: int = 250, forward_days=(5, 10, 20), limit=None, asof=None,
             fee: float = DEFAULT_FEE, diagnose: bool = False, db_path: str = None):
    """跑一遍全量聚合回测。

    2026-10-31：新增 `db_path`。此前库路径硬编码，导致**任何一次小样本试跑都会
    覆盖生产表**（`INSERT OR REPLACE` 会把 5,337 只的结果换成 200 只的，而两批
    数字长得一模一样）。`n_codes` 那列就是为这件事补的补丁 —— 但补丁治不了根：
    根是"跑测试必须能选一个别的库"。传 None 仍走生产库，行为不变。
    """
    from viewer.indicators import _derive_signals, IND_COLS
    from viewer import regime as RG
    _db = db_path or os.path.join(ROOT, "viewer", "app.db")
    con = sqlite3.connect(_db, timeout=60)
    # 库被旁路进程占用时，默认的 0ms 锁等待会让 ALTER 立刻失败（见 _ensure_col 注释）。
    # 给 60s 的 busy_timeout：宁可启动时等，也不要在收尾时丢结果。
    con.execute("PRAGMA busy_timeout=60000")
    # 2026-10-31 性能整改：默认 page cache 只有 2MB，而 app.db 有 8.9GB。
    # 每只票都要翻几十个页，2MB 缓存基本一定 miss —— 实测 86% 时间在等磁盘。
    # 200MB / 300MB 是"够用且不抢系统内存"的量级（本机内存远大于此）。
    con.execute("PRAGMA cache_size=-200000")
    # 2026-10-31 整改（测评 P1「成交价」项）：`stock_indicators_daily` 只有 close，
    # 没有 open/high/low，所以 T+1 也只能拿 T+1 收盘价成交 —— 而报告要求的是
    # "T+1 开盘价或 T+1 VWAP"。开高低在 data/kline_daily.db 的 kline 表里（o/h/l），
    # 两库的 close 实测完全一致（例 000001 于 20261030：app.db 11.64 = kline 11.64），
    # 所以 ATTACH 过来按 (code, 日期整数) 对齐，用开盘价入场、开盘价出场。
    # 这与产品自己的研究口径（selection.py：d+1 开盘买入 → d+1+h 开盘卖出）一致。
    _kline_path = os.path.join(ROOT, "data", "kline_daily.db")
    _has_open = False
    if os.path.exists(_kline_path):
        try:
            con.execute("ATTACH DATABASE ? AS kc", (_kline_path,))
            _has_open = True
            # kline 库 1.6GB，同样给大缓存；ATTACH 的库要单独设，不会继承主库的。
            try:
                con.execute("PRAGMA kc.cache_size=-300000")
            except sqlite3.Error:
                pass
        except sqlite3.Error:
            _has_open = False
    # 2026-10-31：顺序是「先建 signal_regime、再 ensure_tables」。
    # 原来只有一次 ensure_tables，它会给**还没建**的 signal_regime 加列，
    # ALTER 失败被吞掉，于是 calc_version 这列在真实库里可能从来没加上过。
    RG.ensure_tables(con)
    ensure_tables(con)
    reg_map = RG.load_map(con)
    if not reg_map:
        print("[signal_backtest] 警告：market_regime 为空，本轮分状态结果将全部落进 range。"
              "先跑 `python -m viewer.regime`。", flush=True)
    t0 = time.time()

    # 2026-10-31（决策④）：取码先过标的判定 —— 指标库里混着债券（111024，
    # 2010-2014 的老债券，名称却被写成 2021 年的「澳弘转债」）。
    # 它当前落在回测窗口之外，所以历史结论不受影响；但窗口一旦拉长就会静默进样本，
    # 而债券的涨跌停规则、成交额量纲、波动结构全都和股票不同，混进来不会报错。
    # 这里不删任何数据，只在入口把非股票剔掉，并把剔掉的名单打出来可核对。
    # 性能：先走索引取 DISTINCT code，只有代码段判不出的才回查名称
    # （实测 GROUP BY code 全表要扫 1,316 万行，而转债靠代码段就能认出来）。
    codes, _dropped, _unknown = [], [], []
    for (_c,) in con.execute("SELECT DISTINCT code FROM stock_indicators_daily"):
        _k = _U.stock_kind(_c)
        if _k == _U.KIND_STOCK:
            codes.append(_c)
        elif _k == _U.KIND_UNKNOWN:
            _unknown.append(_c)
        else:
            _dropped.append((_c, _k))
    if _unknown:
        # 判不出的代码用名称复判一次；仍然判不出就排除（不默认当股票）。
        for _c in _unknown:
            _r = con.execute("SELECT name FROM stock_indicators_daily WHERE code=?"
                             " AND name IS NOT NULL LIMIT 1", (_c,)).fetchone()
            _nm = (_r[0] if _r else "") or ""
            _k = _U.stock_kind(_c, _nm)
            if _k == _U.KIND_STOCK:
                codes.append(_c)
            else:
                _dropped.append((_c, _k))
    if _dropped:
        print("[signal_backtest] 非股票标的已排除 %d 只：%s"
              % (len(_dropped),
                 ", ".join("%s(%s)" % (c, k) for c, k in _dropped[:10])), flush=True)
    if limit:
        codes = codes[:limit]
    print("[signal_backtest] 回测 %d 只 | 回看%d日 | 前向%s日 | 扣费%.2f%% | 成交价口径=%s"
          % (len(codes), lookback_days, forward_days, fee * 100,
             "T+1开盘价" if _has_open else "T+1收盘价(kline缺失，已降级)"), flush=True)

    # 分桶累积（键 → [n, sum, sumsq, wins, wins_net]）
    acc = {}        # (direction, reason, fwd)
    acc_r = {}      # (direction, reason, fwd, regime)
    base = {}       # fwd                   全样本基线
    base_r = {}     # (fwd, regime)         同期同状态基线 ← 判断"信号有没有 alpha"的真正参照
    # diagnose：再按「前半段 / 后半段」切一刀，用来验证分状态结论是不是只在半段成立。
    # 意义见 tools/regime_robust.py：分状态**基线**（市场层面）在这份数据上几乎全部前后翻转，
    # 只有截面**超额**（信号 vs 同日全市场）才有可能是稳定规律 —— 但必须证，不能假设。
    acc_s = {}
    acc_c = {}      # (direction, reason_a, reason_b, fwd) 信号两两**交集**（同日同票同时触发）
    base_seg = {}   # (fwd, regime, seg) 诊断用的分段基线（注意别与下文聚合用的 base_s 撞名）
    h1 = set()
    if diagnose:
        # 2026-10-31 修正：原来取**全库**日期切前后半段，而回测只看 lookback_days 窗口。
        # 扩到 1250 日后，全库前半段（2010–2018）与回测窗口（2022–2026）完全不相交，
        # 于是每个格子都落进 h2、h1 永远为空，输出成片的「—」和「样本不足」——
        # 表面上"跑了稳健性检验"，实际**一次比较都没发生**。
        # 这里改成只用回测真正覆盖的那一段日期来切，否则这个开关等于装饰。
        _alld = [r[0] for r in con.execute(
            "SELECT DISTINCT date FROM stock_indicators_daily ORDER BY date DESC LIMIT ?",
            (int(lookback_days) + 30,))]
        _alld.reverse()
        h1 = set(_alld[:len(_alld) // 2])
        print("[signal_backtest] 诊断模式：窗口内前半段 %d 日 / 后半段 %d 日（%s 起）"
              % (len(h1), len(_alld) - len(h1), _alld[0] if _alld else "?"), flush=True)
    done = 0
    # 不可成交样本的计数（2026-10-31 新增）：剔除是对的，**静默**剔除是错的 ——
    # 如果一字板剔除量很大，说明这套信号系统性地指向"买不进的票"，
    # 那个结论比任何收益数字都重要，所以必须打出来。
    n_unbuyable = 0      # T+1 一字板涨停：买不进
    n_unsellable = 0     # 出场日一字板跌停：卖不出
    n_open_fallback = 0  # 开盘价缺失、退回收盘价的样本数（口径混合计数）
    n_pairs = 0          # 通过校验的价格对总数 —— 缺价比例的正确分母
    # ── 取数口径（2026-10-31 性能整改）────────────────────────────────────
    # 原来每只票都 `SELECT *` 取**全量历史**（8.9GB 库里约 4,000 行 × 几十列），
    # 然后只用最后 lookback_days 行；而且每只票还各自跑一次 PRAGMA table_info。
    # 实测后果：5,337 只里跑完 500 只时，进程 CPU 只用了 112 秒（19 分钟里 86% 在等 IO），
    # 按那个速度全量要 8 小时。两个改动：
    #   ① 只取真正用到的列（date + IND_COLS，共 56 列，跳过 name/amount 等）；
    #   ② 只取真正用到的行（lookback + 最大前向 + 缓冲），老票不足则自然取满。
    # 表结构校验提到循环外做一次 —— 缺列必须**启动时**炸，不能在几小时后写回时炸。
    _tcols = [d[1] for d in con.execute("PRAGMA table_info(stock_indicators_daily)")]
    _lack = [c for c in IND_COLS if c not in _tcols]
    if _lack:
        raise RuntimeError(
            "stock_indicators_daily 缺少回测所需列：%s（%d 列缺失）。"
            "先跑指标回填，不要在缺列的表上出回测结论。" % (_lack[:6], len(_lack)))
    _sel_cols = ["date"] + list(IND_COLS)
    _sel_sql = ("SELECT " + ",".join(_sel_cols) +
                " FROM stock_indicators_daily WHERE code=? ORDER BY date DESC LIMIT ?")
    _row_need = int(lookback_days) + int(max(forward_days or [0])) + 30
    for code in codes:
        rows = con.execute(_sel_sql, (code, _row_need)).fetchall()
        if len(rows) < 60:
            continue
        rows.reverse()          # 上面按 date DESC 取，翻回升序（下游全部假定升序）
        df = pd.DataFrame(rows, columns=_sel_cols)
        closes = df["close"].to_numpy(dtype=float)
        n = len(df)
        start = max(0, n - lookback_days)
        # 见文件头「逐格取值提速」：一次性转 numpy，避免 2260 万次 pandas 标量访问
        mat = df[IND_COLS].apply(pd.to_numeric, errors="coerce").to_numpy(dtype=float)
        pos = {c: k for k, c in enumerate(IND_COLS)}
        # 入场日 → 市场状态。预先把本段日期映射成列表，循环里只做下标取值。
        # 注意：状态取自**真实交易日**而非序列位置 —— 停牌股的序列短，位置索引会错位。
        dates = df["date"].tolist()
        # T+1 开盘价序列（与 df 的行一一对齐）。取不到的那一行留 NaN，
        # 由主循环显式退回收盘价并计数 —— 绝不静默混用两种口径。
        opens = np.full(n, np.nan)
        highs = np.full(n, np.nan)
        lows = np.full(n, np.nan)
        if _has_open:
            kmap = {}
            # 只取需要的那一段（回看窗口 + 最大前向 + 余量），不拉整条历史。
            # 实测差异很大：全量取每只约 3,000 行 × 5,337 只 ≈ 1,600 万行，
            # 首版这么写让全量回测从 25 分钟涨到 80 分钟以上；限位后降到约 280 行/只。
            _need = (n - start) + int(max(forward_days or [0])) + 8
            for d_, o_, h_, l_ in con.execute(
                    "SELECT d, o, h, l FROM kc.kline WHERE code=? ORDER BY d DESC LIMIT ?",
                    (code, _need)).fetchall():
                try:
                    kmap[int(d_)] = (o_, h_, l_)
                except (TypeError, ValueError):
                    continue
            for k_ in range(start, n):
                t_ = kmap.get(int(str(dates[k_]).replace("-", "")))
                if t_:
                    opens[k_] = t_[0] if t_[0] else np.nan
                    highs[k_] = t_[1] if t_[1] else np.nan
                    lows[k_] = t_[2] if t_[2] else np.nan
        regs = [reg_map.get(dt, "range") for dt in dates[start:n]]
        segs = [("h1" if dt in h1 else "h2") for dt in dates[start:n]] if diagnose else None
        for i in range(start, n):
            reg = regs[i - start]
            seg = segs[i - start] if diagnose else None
            ri = mat[i]
            row = {}
            for c in IND_COLS:
                v = float(ri[pos[c]])
                row[c] = None if (math.isnan(v) or math.isinf(v)) else v
            buy, sell = _derive_signals(row)
            for fwd in forward_days:
                # 2026-10-31 整改（测评 P0-C ③）：成交价改 T+1。
                # 原：`ret = closes[i+fwd] / closes[i] - 1`，即"用信号当日的收盘价成交"。
                # 而信号本身由当日 close 派生的因子算出 —— 要在收盘那一刻同时知道收盘价、
                # 又用这个收盘价成交，中间没有可执行的时间窗口。这是前视，且方向永远对自己有利。
                # 现：入场 closes[i+1]，出场 closes[i+1+fwd]。代价是每组少一个尾部样本。
                ei = i + 1
                xj = ei + fwd
                if ei >= n or xj >= n:
                    continue
                if closes[ei] in (0, None) or np.isnan(closes[ei]):
                    continue
                # 二次整改（P1「成交价」）：入场/出场都改用**开盘价**（报告要求 open 或 VWAP）。
                # 上面那次整改只做到"T+1 收盘价成交"，仍不是可执行口径 —— 你要在 T+1 收盘
                # 那一刻才下单，而决定下单的信号来自 T 日收盘，中间隔了一整天。
                # 取不到开盘价时退回收盘价并计数（n_open_fallback），口径混合必须可见。
                ep = opens[ei]
                if np.isnan(ep) or ep <= 0:
                    ep = closes[ei]
                    n_open_fallback += 1
                xp = opens[xj]
                if np.isnan(xp) or xp <= 0:
                    xp = closes[xj]
                    n_open_fallback += 1
                # ── 一字板不可成交（涨停买不进 / 跌停卖不出）─────────────────
                # 判据是定义性规则（units.price_limit_pct），不是经验阈值：
                # 当日 开=高=低 且 开盘价已达涨停价 ⇒ 全天一个价，排队也买不到。
                # 报告要求"标记并剔除"，这里剔除并分别计数，不做静默丢弃。
                _pct = _U.price_limit_pct(code, "")
                if _pct is not None:
                    _lu = round(float(closes[i]) * (1.0 + _pct) + 1e-9, 2)
                    _ld = round(float(closes[i]) * (1.0 - _pct) + 1e-9, 2)
                    if (not np.isnan(highs[ei]) and highs[ei] == lows[ei]
                            and opens[ei] >= _lu - 0.011):
                        n_unbuyable += 1
                        continue
                    if (not np.isnan(highs[xj]) and highs[xj] == lows[xj]
                            and opens[xj] <= _ld + 0.011):
                        n_unsellable += 1
                        continue
                ret_raw = xp / ep - 1.0
                if np.isnan(ret_raw) or np.isinf(ret_raw):
                    continue
                n_pairs += 1
                # 基线保持"买入持有"多头口径，不取负 —— 它是参照，不是策略。
                _bump(base, fwd, ret_raw, fee)                  # 全样本基线
                _bump(base_r, (fwd, reg), ret_raw, fee)         # 同状态基线
                if diagnose:
                    _bump(base_seg, (fwd, reg, seg), ret_raw, fee)
                for direction, reasons in (("buy", buy), ("sell", sell)):
                    if not reasons:
                        continue
                    # 2026-10-31 整改（测评 P0-C ②）：sell 方向取负。
                    # 原实现对 buy / sell 用**同一个** ret —— 做空被当成做多记账，
                    # 于是"看空信号"的收益符号整体反了。这类错误不报错、不告警，
                    # 唯一表现是"看空信号和看多信号一样赚钱"，而这最容易被当成好消息。
                    ret = _dir_ret(ret_raw, direction)
                    _keys = []
                    for r in reasons:
                        key = r.split("<")[0].split(">")[0].strip()
                        if key not in _keys:
                            _keys.append(key)
                        _bump(acc, (direction, key, fwd), ret, fee)
                        _bump(acc_r, (direction, key, fwd, reg), ret, fee)
                        if diagnose:
                            _bump(acc_s, (direction, key, fwd, reg, seg), ret, fee)
                    # ── 交集（决策③）：只有**同方向**的组合才算 ──────────────────
                    # 不做 buy×sell「冲突组合」：那类样本没有明确交易含义
                    # （既是买又是卖时你到底做不做？），算出来也无法变成动作。
                    # 排序后再配对，保证 (A,B) 和 (B,A) 落进同一个桶。
                    if len(_keys) >= 2:
                        _keys = sorted(_keys)
                        for _i in range(len(_keys)):
                            for _j in range(_i + 1, len(_keys)):
                                _bump(acc_c, (direction, _keys[_i], _keys[_j], fwd), ret, fee)
        done += 1
        if done % 500 == 0:
            print("[signal_backtest] %d/%d codes" % (done, len(codes)), flush=True)

    if not acc:
        con.close()
        print("[signal_backtest] 没有可回测的样本（检查 stock_indicators_daily 是否有数据）", flush=True)
        return {"ok": False, "error": "no samples"}

    # 不可成交统计：剔除本身是对的，静默剔除是错的 —— 一字板剔除量要是很大，
    # 说明这套信号系统性地指向"买不进的票"，那个结论比任何收益数字都重要。
    # n_codes_used = 有足量 bar、真正进入回测的代码数（不是 codes 列表长度，
    # 也不是总票数）—— 它就是这个聚合的分母来源。
    n_codes_used = int(done)
    _tot = n_unbuyable + n_unsellable
    print("[signal_backtest] 不可成交剔除：涨停买不进 %d / 跌停卖不出 %d（合计 %d 样本）"
          % (n_unbuyable, n_unsellable, _tot), flush=True)
    if n_open_fallback:
        # 2026-10-31 修正分母：原式是 fallback/(fallback+1)，12 次缺价会显示成 92%，
        # 看着像"九成样本口径混合"，实际是 12 个价格点 / 上千万次取值。
        # 每对样本有 2 个价格点（入场 + 出场），所以分母取 2×样本对。
        _den = max(1, 2 * n_pairs)
        print("[signal_backtest] 注意：%d 个价格点缺开盘价，已退回收盘价"
              "（占价格点次约 %.4f%%）" % (n_open_fallback, 100.0 * n_open_fallback / _den),
              flush=True)

    now = time.strftime("%Y-%m-%d %H:%M:%S")

    # ── 总表（全样本，不分状态）────────────────────────────────────────────
    # 2026-10-31 整改（测评 P1「多重比较」项）：先算 p，再对**整族**做 BH 校正，最后才写库。
    # 顺序不能颠倒 —— 单条算完就写，等于每条都按"只检验了一次"来解读。
    cells = []
    for (direction, reason, fwd), b in acc.items():
        s = _stats(b, fee, fwd)
        if not s:
            continue
        cells.append((direction, reason, fwd, s, _p_two_sided(s.get("t_adj"))))
    qmap = _bh_adjust([((d, r, f), p) for d, r, f, _s, p in cells])
    m_tests = len(cells)
    n_sig_raw = sum(1 for _d, _r, _f, _s, p in cells if p is not None and p < 0.05)
    n_sig_bh = sum(1 for k, q in qmap.items() if q is not None and q < 0.05)

    recs, summary = [], []
    for direction, reason, fwd, s, p in cells:
        q = qmap.get((direction, reason, fwd))
        recs.append((reason, direction, fwd, s["n"], round(s["winrate"], 4),
                     round(s["avg_ret"], 4), round(s["sharpe"], 3), asof or "",
                     round(fee, 6), round(s["winrate_net"], 4), round(s["avg_ret_net"], 4),
                     round(s["sharpe_net"], 3),
                     round(s["t_naive"], 3), round(s["t_adj"], 3), int(s["n_eff"]),
                     CALC_VERSION,
                     None if p is None else round(p, 6),
                     None if q is None else round(q, 6),
                     n_codes_used, int(lookback_days)))
        summary.append((direction, reason, fwd, s["n"], s["winrate"], s["avg_ret"],
                        s["sharpe"], s["winrate_net"], s["avg_ret_net"]))
    if recs:
        con.executemany(
            "INSERT OR REPLACE INTO signal_backtest"
            "(reason,direction,fwd,n,winrate,avg_ret,sharpe,updated,fee,winrate_net,avg_ret_net,sharpe_net,"
            "t_naive,t_adj,n_eff,calc_version,p_raw,q_bh,n_codes,lookback_days)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", recs)
    con.commit()

    # 多重比较必须说出来，否则下面那些"最显著"的排名会在读者脑子里自动变成"最强的信号"。
    print("[signal_backtest] 多重比较：本族共 %d 个（方向×原因×前向）检验；"
          "未校正 p<0.05 的 %d 个，BH 校正后 q<0.05 的 %d 个"
          % (m_tests, n_sig_raw, n_sig_bh), flush=True)
    if m_tests and n_sig_raw > n_sig_bh:
        print("[signal_backtest] —— 也就是说，%d 个「显著」里有 %d 个是「检验了 %d 次」的产物，"
              "只能当作待验假设，不能当作结论。"
              % (n_sig_raw, n_sig_raw - n_sig_bh, m_tests), flush=True)

    # ── 基线：全样本 + 分状态 ─────────────────────────────────────────────
    # 分状态基线是本轮的核心：只有它才能回答"这个信号在同一种市况里，比全市场平均好多少"。
    base_s, base_rows = {}, []
    for fwd in forward_days:
        b = base.get(fwd)
        s = _stats(b, fee, fwd) if b else None
        if not s:
            continue
        base_s[fwd] = s
        base_rows.append((fwd, s["n"], round(s["winrate"], 4), round(s["avg_ret"], 4),
                          asof or "", round(fee, 6), round(s["winrate_net"], 4),
                          round(s["avg_ret_net"], 4)))
    if base_rows:
        con.executemany(
            "INSERT OR REPLACE INTO signal_baseline"
            "(fwd,n,winrate,avg_ret,updated,fee,winrate_net,avg_ret_net)"
            " VALUES (?,?,?,?,?,?,?,?)", base_rows)

    base_rs, bsr_rows = {}, []
    for (fwd, reg), b in base_r.items():
        s = _stats(b, fee, fwd)
        if not s:
            continue
        base_rs[(fwd, reg)] = s
        bsr_rows.append((fwd, reg, s["n"], round(s["winrate"], 4), round(s["avg_ret"], 4),
                         round(s["winrate_net"], 4), round(s["avg_ret_net"], 4),
                         round(fee, 6), now, CALC_VERSION, int(lookback_days)))
    if bsr_rows:
        con.executemany(
            "INSERT OR REPLACE INTO signal_baseline_regime"
            "(fwd,regime,n,winrate,avg_ret,winrate_net,avg_ret_net,fee,updated,calc_version,lookback_days)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?)", bsr_rows)

    # ── 分状态信号表 ──────────────────────────────────────────────────────
    sr_rows, reg_summary = [], []
    for (direction, reason, fwd, reg), b in acc_r.items():
        s = _stats(b, fee, fwd)
        if not s:
            continue
        bse = base_rs.get((fwd, reg))
        edge_wr = (s["winrate_net"] - bse["winrate_net"]) if bse else None
        edge_ret = (s["avg_ret_net"] - bse["avg_ret_net"]) if bse else None
        sr_rows.append((reason, direction, fwd, reg, s["n"], round(s["winrate"], 4),
                        round(s["avg_ret"], 4), round(s["sharpe"], 3),
                        round(s["winrate_net"], 4), round(s["avg_ret_net"], 4),
                        round(s["sharpe_net"], 3),
                        round(edge_wr, 4) if edge_wr is not None else None,
                        round(edge_ret, 5) if edge_ret is not None else None,
                        round(fee, 6), now, CALC_VERSION, int(lookback_days)))
        reg_summary.append((direction, reason, fwd, reg, s["n"], edge_wr, edge_ret,
                            s["winrate_net"], s["avg_ret_net"]))
    if sr_rows:
        con.execute("DELETE FROM signal_regime")
        # 2026-10-31：补上 calc_version。**这一列此前从来没有被写入过** ——
        # 表里有、断言也验了它存在，但 INSERT 的列清单里没有它，
        # 于是永远落成 NULL。查库时表现为"整表 153 行 calc_version 全是 None"，
        # 看上去像"数据是旧口径的"，实际是"新口径跑了但版本没写进去"。
        # 教训：_assert_schema 只能保证列存在，保证不了 INSERT 覆盖它；
        # 所以这里把列清单和元组长度对齐写在同一个地方，改一处必须改另一处。
        con.executemany(
            "INSERT OR REPLACE INTO signal_regime"
            "(reason,direction,fwd,regime,n,winrate,avg_ret,sharpe,winrate_net,avg_ret_net,"
            "sharpe_net,edge_wr,edge_ret,fee,updated,calc_version,lookback_days)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", sr_rows)
    con.commit()

    # ── 信号交集（决策③）────────────────────────────────────────────────────
    # 两个参照系都给，因为**它们的差本身就是信息**：
    #   edge_vs_base  相对全市场基线    → 回答"拿它选股能不能赚钱"
    #   edge_vs_parts 相对两个成分信号的均值 → 回答"组合是否真的赢过单独用"
    # 前者正、后者负 = 赚的是市况不是组合；两个都正才叫"新发现"。
    # 只给前者，就会出现"某个组合超额 +2%"而实际两个成分各自就有 +2% 的假结论。
    _part_net = {}
    for (_d, _r, _f), _b in acc.items():
        _s = _stats(_b, fee, _f)
        if _s:
            _part_net[(_d, _r, _f)] = _s["avg_ret_net"]
    cb_cells = []
    for (direction, ra, rb, fwd), b in acc_c.items():
        s = _stats(b, fee, fwd)
        if not s:
            continue
        cb_cells.append((direction, ra, rb, fwd, s, _p_two_sided(s.get("t_adj"))))
    # 交集单独做一族 BH。混进单信号那 51 个里，会把单信号的门槛一起拉高，
    # 于是两边都变得"什么都不显著"，分不清是真没有还是被校正掉了。
    cq = _bh_adjust([((d, a, bb, f), p) for d, a, bb, f, _s, p in cb_cells])
    n_cb_raw = sum(1 for x in cb_cells if x[5] is not None and x[5] < 0.05)
    n_cb_bh = sum(1 for q in cq.values() if q is not None and q < 0.05)
    n_cb_usable = 0
    cb_rows, cb_top = [], []
    for direction, ra, rb, fwd, s, p in cb_cells:
        q = cq.get((direction, ra, rb, fwd))
        bse = base_s.get(fwd)
        evb = (s["avg_ret_net"] - bse["avg_ret_net"]) if bse else None
        pa = _part_net.get((direction, ra, fwd))
        pb = _part_net.get((direction, rb, fwd))
        evp = (s["avg_ret_net"] - (pa + pb) / 2.0) if (pa is not None and pb is not None) else None
        usable = 1 if s["n"] >= _P.MIN_COMBO_N else 0
        n_cb_usable += usable
        cb_rows.append((ra, rb, direction, fwd, s["n"], round(s["winrate"], 4),
                        round(s["avg_ret"], 4), round(s["sharpe"], 3),
                        round(s["winrate_net"], 4), round(s["avg_ret_net"], 4),
                        round(s["sharpe_net"], 3), round(s["t_naive"], 3),
                        round(s["t_adj"], 3), int(s["n_eff"]),
                        None if p is None else round(p, 6),
                        None if q is None else round(q, 6),
                        None if evb is None else round(evb, 5),
                        None if evp is None else round(evp, 5),
                        None if pa is None else round(pa, 5),
                        None if pb is None else round(pb, 5),
                        usable, CALC_VERSION, round(fee, 6), now, int(lookback_days)))
        if usable and evb is not None and evb > 0:
            cb_top.append((direction, ra, rb, fwd, s["n"], evb, evp, q))
    if cb_rows:
        con.execute("DELETE FROM signal_combo")
        con.executemany(
            "INSERT OR REPLACE INTO signal_combo"
            "(reason_a,reason_b,direction,fwd,n,winrate,avg_ret,sharpe,winrate_net,"
            "avg_ret_net,sharpe_net,t_naive,t_adj,n_eff,p_raw,q_bh,edge_vs_base,"
            "edge_vs_parts,part_a_ret_net,part_b_ret_net,usable,calc_version,fee,updated,lookback_days)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", cb_rows)
    con.commit()
    print("[signal_backtest] 交集：%d 个组合格，其中样本≥%d 的 %d 个；"
          "本族多重比较 未校正 p<0.05 共 %d 个，BH 后 q<0.05 共 %d 个"
          % (len(cb_rows), _P.MIN_COMBO_N, n_cb_usable, n_cb_raw, n_cb_bh), flush=True)
    if cb_top:
        cb_top.sort(key=lambda x: -(x[5] or -9.0))
        print("=== 交集·税后超额 vs 全市场基线 · Top8（样本足够且超额为正）===")
        for _d, _a, _b2, _f, _n, _evb, _evp, _q in cb_top[:8]:
            print("  %-12s × %-12s %s fwd=%-2d n=%-6d 超额基准%+6.2f%% 超额成分%+6.2f%% q=%s"
                  % (_a, _b2, _d, _f, _n, _evb * 100,
                     (_evp * 100) if _evp is not None else float("nan"),
                     "—" if _q is None else "%.3f" % _q), flush=True)
    # 连接**不在这里关**：后面的诊断块还要把稳定性写回 signal_regime.stab。
    # 教训：此前在这里 close，导致诊断块的 ALTER/UPDATE 全在已关闭连接上执行、被
    # except 静默吞掉 —— 表面上打印了结论，表里却一列都没写。连接统一在 return 前关闭。

    print("[signal_backtest] 共 %d 个 (方向×原因×前向) 组合 / %d 个分状态格，用时 %.1fs"
          % (len(recs), len(sr_rows), time.time() - t0), flush=True)
    for fwd in forward_days:
        if fwd in base_s:
            s = base_s[fwd]
            print("=== 基线 前向%d日: 胜率%.1f%%/均值%+.2f%% ｜ 扣费后 胜率%.1f%%/均值%+.2f%% ==="
                  % (fwd, s["winrate"] * 100, s["avg_ret"] * 100,
                     s["winrate_net"] * 100, s["avg_ret_net"] * 100))

    # 分状态基线差异 —— 这个数字本身就是本轮最大的发现（若差异巨大，混算必然误导）
    print("=== 分状态基线（税后均值 = 同一市况下全市场平均水平）===")
    for fwd in forward_days:
        parts = []
        for reg in RG.REGIMES:
            s = base_rs.get((fwd, reg))
            if s:
                parts.append("%s %+.2f%%(n=%d)" % (RG.REGIME_CN[reg], s["avg_ret_net"] * 100, s["n"]))
        if parts:
            print("  前向%-2d日  " % fwd + " ｜ ".join(parts))

    print("=== 买信号·前向10日·超额胜率 Top10（扣费后相对扣费后基线）===")
    buy10 = sorted([s for s in summary if s[0] == "buy" and s[2] == 10 and s[3] >= 20],
                   key=lambda x: (-(x[7] - (base_s.get(10) or {}).get("winrate_net", 0.5)), -x[8]))[:10]
    for d, reason, fwd, nn, wr, avg, sh, wrn, avgn in buy10:
        bwn = (base_s.get(fwd) or {}).get("winrate_net", 0.5)
        ban = (base_s.get(fwd) or {}).get("avg_ret_net", 0.0)
        print("  %-22s n=%-5d 胜率%.1f%%(超额%+.1f) 净值%+.2f%% 税后超额%+.2f%%"
              % (reason, nn, wr * 100, (wr - bwn) * 100, avgn * 100, (avgn - ban) * 100))

    # 三档一致性：一个信号只有在**多档**都为正超额才算稳健，只在单档有效说明它在赌市况。
    print("=== 三档一致性（买信号·前向10日·样本≥200，税后超额胜率 pt）===")
    by_sig = {}
    for d, reason, fwd, reg, nn, ewr, eret, wrn, avgn in reg_summary:
        if d != "buy" or fwd != 10 or nn < 200:
            continue
        by_sig.setdefault(reason, {})[reg] = (ewr or 0.0) * 100
    ranked = sorted(by_sig.items(), key=lambda kv: -(sum(kv[1].values()) / max(1, len(kv[1]))))[:12]
    print("  %-22s %8s %8s %8s" % ("信号", "强势", "震荡", "弱势"))
    def _pt(x):
        return "%+8.1f" % x if x == x else "%8s" % "—"
    for reason, m in ranked:
        print("  %-22s %s %s %s"
              % (reason, _pt(m.get("up", float("nan"))), _pt(m.get("range", float("nan"))),
                 _pt(m.get("down", float("nan")))))
    # ── 诊断：分状态超额在前后半段是否同号（唯一能区分"规律"与"这一段行情"的检验）──
    if diagnose and acc_s:
        print("\n=== 分状态超额·前后半段一致性（买信号·前向10日·税后超额胜率 pt，每格 n≥100）===")
        bs = {k: _stats(v, fee, k[0]) for k, v in base_seg.items()}
        cells = {}
        for (d, reason, fwd, reg, seg), b in acc_s.items():
            if d != "buy" or fwd != 10:
                continue
            s2 = _stats(b, fee, fwd)
            bsg = bs.get((fwd, reg, seg))
            if not s2 or not bsg or s2["n"] < 100:
                continue
            cells.setdefault(reason, {})[(reg, seg)] = \
                (s2["winrate_net"] - bsg["winrate_net"]) * 100

        def _fmt(x):
            return "%+6.1f" % x if x is not None else "%6s" % "—"

        print("  %-20s %13s %13s %13s  %s"
              % ("信号", "强势 h1/h2", "震荡 h1/h2", "弱势 h1/h2", "判定"))
        # 三分类：稳健有效（两段都 ≥+1pt）/ 稳健有害（两段都 ≤-1pt）/ 不稳定（异号或幅度不足）。
        # 只判「同号」会把「三档全是 -6pt」的放量误判成"稳健"——它确实稳定，但是稳定地有害。
        good_all, bad_all, uns_all = [], [], []
        for reason, m in sorted(cells.items()):
            good, bad, uns, parts = [], [], [], []
            for reg in RG.REGIMES:
                a, b2 = m.get((reg, "h1")), m.get((reg, "h2"))
                parts.append("%s/%s" % (_fmt(a), _fmt(b2)))
                if a is None or b2 is None:
                    continue
                if a >= 1.0 and b2 >= 1.0:
                    good.append(reg)
                elif a <= -1.0 and b2 <= -1.0:
                    bad.append(reg)
                else:
                    uns.append(reg)
            tag = []
            if good:
                tag.append("有效:" + "/".join(RG.REGIME_CN[r] for r in good))
            if bad:
                tag.append("有害:" + "/".join(RG.REGIME_CN[r] for r in bad))
            if uns:
                tag.append("不稳:" + "/".join(RG.REGIME_CN[r] for r in uns))
            print("  %-20s %13s %13s %13s  %s"
                  % (reason, parts[0], parts[1], parts[2], " ".join(tag) or "样本不足"))
            if good:
                good_all.append((reason, good))
            if bad:
                bad_all.append((reason, bad))
            if uns:
                uns_all.append((reason, uns))

        # 落库：把稳定性标进 signal_regime.stab，供前端如实标注，
        # 避免把"只在半段成立"的数字当成规律展示给用户。
        try:
            _c = [d2[1] for d2 in con.execute("PRAGMA table_info(signal_regime)").fetchall()]
            if "stab" not in _c:
                con.execute("ALTER TABLE signal_regime ADD COLUMN stab TEXT")
        except sqlite3.Error as e:
            print("  [warn] 稳定性落库失败：%s" % e)
        # 先清空本口径下的旧标记：本轮判定变了（比如新增 unstable），旧值不清会与新的混在一起
        try:
            con.execute("UPDATE signal_regime SET stab=NULL WHERE direction='buy' AND fwd=10")
        except sqlite3.Error:
            pass
        ups = [("ok", r, g) for r, gs in good_all for g in gs] + \
              [("bad", r, g) for r, bs_ in bad_all for g in bs_] + \
              [("unstable", r, g) for r, gs in uns_all for g in gs]
        if ups:
            con.executemany(
                "UPDATE signal_regime SET stab=? WHERE reason=? AND direction='buy' "
                "AND fwd=10 AND regime=?", ups)
            con.commit()

        print("\n  判读：某档 h1/h2 都 ≥+1pt 才算「稳健有效」，都 ≤-1pt 是「稳健有害」；")
        print("  异号或幅度不足 = 不稳定，属于半段行情特征，不能据此按市况切换权重。")
        print("  稳健有效：%s" % ("；".join("%s→%s" % (r, "/".join(RG.REGIME_CN[x] for x in g))
                                          for r, g in good_all) or "无"))
        print("  稳健有害：%s" % ("；".join("%s→%s" % (r, "/".join(RG.REGIME_CN[x] for x in b2_))
                                          for r, b2_ in bad_all) or "无"))

    con.close()
    return {"ok": True, "combos": len(recs), "regime_cells": len(sr_rows),
            "secs": round(time.time() - t0, 1), "fee": fee,
            "calc_version": CALC_VERSION,
            "entry_basis": "t1_open" if _has_open else "t1_close",
            "unbuyable": n_unbuyable, "unsellable": n_unsellable,
            "open_fallback": n_open_fallback, "n_codes": n_codes_used}


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--lookback", type=int, default=250)
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--asof", type=str, default=None)
    ap.add_argument("--fee", type=float, default=DEFAULT_FEE,
                    help="双边综合成本（默认 %.4f=%.2f%%，含佣金＋印花税＋滑点）"
                         % (DEFAULT_FEE, DEFAULT_FEE * 100))
    ap.add_argument("--diagnose", action="store_true",
                    help="额外按前半段/后半段检验分状态结论是否稳定（约多 20%% 耗时）")
    a = ap.parse_args()
    backtest(lookback_days=a.lookback, limit=a.limit, asof=a.asof, fee=a.fee,
             diagnose=a.diagnose)
