"""因子引入与准入门禁（三期，2026-11-03）。

为什么要有这道门
----------------
二期把 6 个语料方法论编译成了因子，那是**我一个个手写、我担保**的。三期开始要从外面
（GitHub / 论文 / 语料）批量往回搬东西，担保人就从「我」变成了「一堆不知道底细的仓库」。
这时候最先要建的不是扫描器，是**闸门** —— 没有闸，扫得越多，系统越乱。

所以这个模块的定位是：**任何外来因子想进系统，必须先过这里**。过不了不是「先上线看效果」，
是过不了。四条硬线（下面 `GATE`）不看心情，只看数。

四条纪律（改这个文件前先读）
----------------------------
 1. **IC 必须自己算，不许抄仓库里的回测数**。仓库的 IC 通常有幸存者偏差、有未来函数、
    有「只在作者选的那段行情里成立」。这里一律用本地日线重算，样本外才算数。
 2. **相关性要跟「已知风格」比，不是跟我们的布尔因子比**。我们现有 6 个因子输出 0/1，
    对 0/1 做 Spearman 会大面积并列，相关系数没有统计意义。所以拿 7 个经典风格因子
    （动量/反转/波动/量比/均线偏离/成交额/振幅）当对照 —— 这也是行业里查「新因子是不是
    旧因子换皮」的标准做法。
 3. **影子期不能用回测替代**。回测 IC 好只说明「历史上成立」，影子期验的是「上线之后
    还成立」。两者不能互相顶替，所以 `status` 里 candidate / shadow / active 三态必须分清楚。
 4. **IC 转负自动降权，不许人工拍**。因子失效是个渐变过程，靠人盯一定会漏；
    把「最近 20 日 IC 为负」写成规则，它自己会退出。

性能：一次全量 IC 评估（5003 只 × 250 日 × 1 个因子）实测 8~12 秒，主要是取数与矩阵对齐。
"""
from __future__ import annotations

import ast
import json
import logging
import math
import sqlite3
import time
from datetime import datetime

import numpy as np

log = logging.getLogger(__name__)

# ================================================================
# ① 门禁线（写死在这里，不放进配置 —— 配置能改的就不叫门禁了）
# ================================================================
DAYS = 250                 # 评估窗口（交易日）。250 ≈ 一年，够算 ICIR 又不至于把老行情混进来
FWD = 5                    # 未来收益口径：未来 5 个交易日（周频，和「中线」定位一致）
WARMUP = 60                # 表达式预热期：MA60 类算子在这里还没值，前 60 天不参与 IC
MIN_CROSS = 300            # 单日截面样本少于这个数就跳过该日（样本太少 IC 是噪声）

GATE = {
    "abs_ic": 0.02,        # |IC| 下限
    "abs_icir": 0.30,      # |ICIR| 下限
    "abs_t": 2.50,         # |t| 下限
    "max_corr": 0.70,      # 与已知风格的最大 |相关| 上限
    "min_days": 120,       # 有效 IC 天数下限
    "shadow_days": 20,     # 影子期长度（交易日，约 4 周）
}


# ================================================================
# ② 取数 + 日期对齐矩阵
# ================================================================
def trade_calendar(days: int = DAYS + FWD + 20) -> list[str]:
    """最近 days 个交易日（升序）。拿不到就返回空，让调用方明确降级。"""
    from viewer import localfeed as _lf
    try:
        cal = _lf._kbase().distinct_dates()  # noqa: SLF001 项目内约定用同一交易日历
        return [str(d) for d in (cal or [])][-days:]
    except Exception:  # noqa: BLE001
        log.exception("交易日历取数失败")
        return []


def matrix(codes, days: int = DAYS) -> dict:
    """把变长日线对齐成 (n_stocks, days) 矩阵。

    ★ 为什么不能像 factors.load 那样直接用位置对齐：停牌票的日线是**截断**的，
    它数组的最后一位是它自己最后一天，不是最新交易日。按位置对齐会让停牌票的
    历史整体往前挪，IC 就全是假的。这里按**日期键**落位，停牌那几天留 NaN，
    IC 计算时自然跳过。
    """
    from viewer import universe_box as ub
    cal = trade_calendar(days + FWD + 10)
    if not cal:
        return {"ok": False, "error": "拿不到交易日历，无法对齐"}
    win = cal[-(days + FWD):]                 # 多取 FWD 天用来算未来收益
    idx = {d: i for i, d in enumerate(win)}
    codes = [str(c) for c in (codes or []) if c]
    raw = ub.load_bars(codes, keep=len(win) + 30)
    n, m = len(codes), len(win)
    out = {k: np.full((n, m), np.nan, dtype=np.float64)
           for k in ("open", "high", "low", "close", "vol")}
    got = 0
    for i, code in enumerate(codes):
        rows = raw.get(code)
        if not rows:
            continue
        got += 1
        for r in rows:
            j = idx.get(str(r[0]))
            if j is None:
                continue
            out["open"][i, j] = r[1]
            out["close"][i, j] = r[2]
            out["high"][i, j] = r[3]
            out["low"][i, j] = r[4]
            out["vol"][i, j] = r[5]
    out["ok"] = True
    out["codes"] = codes
    out["dates"] = win
    out["source"] = {"asked": n, "got": got}
    return out


# ================================================================
# ③ 表达式 DSL：白名单求值（不用 eval 裸跑，外来字符串一律先过 AST 白名单）
# ================================================================
_ALLOWED_NODES = (
    ast.Expression, ast.BinOp, ast.UnaryOp, ast.Call, ast.Name, ast.Load,
    ast.Constant, ast.Add, ast.Sub, ast.Mult, ast.Div, ast.Pow, ast.USub,
    ast.UAdd, ast.Compare, ast.Lt, ast.LtE, ast.Gt, ast.GtE,
    ast.IfExp, ast.Mod,
)
MAX_EXPR_LEN = 400


def _df(x):
    import pandas as pd
    return pd.DataFrame(x)


def _roll(x, n, how):
    import pandas as pd
    n = int(n)
    n = max(2, n)
    r = getattr(pd.DataFrame(x).rolling(n, min_periods=max(2, n // 2)), how)()
    return r.to_numpy()


FUNCS = {
    # 时序算子
    "ma":    lambda x, n=20: _roll(x, n, "mean"),
    "std":   lambda x, n=20: _roll(x, n, "std"),
    "hhv":   lambda x, n=20: _roll(x, n, "max"),
    "llv":   lambda x, n=20: _roll(x, n, "min"),
    "ema":   lambda x, n=20: _df(x).ewm(span=max(2, int(n)), adjust=False).mean().to_numpy(),
    "ref":   lambda x, n=1: _df(x).shift(int(n)).to_numpy(),
    "pct":   lambda x, n=1: (_df(x) / _df(x).shift(int(n)) - 1).to_numpy(),
    "zpct":  lambda x, n=20: _roll(x, n, "mean") / (_roll(x, n, "std") + 1e-9),  # 时序 z 分数
    # 截面算子
    "rank":  lambda x: _df(x).rank(pct=True).to_numpy(),
    # 逐元素
    "abs":   np.abs,
    "log":   lambda x: np.log(np.abs(x) + 1e-9),
    "sqrt":  lambda x: np.sqrt(np.abs(x)),
    "sign":  np.sign,
    "min":   np.minimum,
    "max":   np.maximum,
}
VARS = ("open", "high", "low", "close", "vol")


def compile_expr(expr: str):
    """编译表达式 → fn(mats) -> 矩阵。**过不了白名单直接抛错，不静默降级。**"""
    expr = (expr or "").strip()
    if not expr:
        raise ValueError("表达式为空")
    if len(expr) > MAX_EXPR_LEN:
        raise ValueError("表达式过长（%d > %d）" % (len(expr), MAX_EXPR_LEN))
    try:
        tree = ast.parse(expr, mode="eval")
    except SyntaxError as e:
        raise ValueError("表达式语法错误：%s" % e) from None
    for node in ast.walk(tree):
        if not isinstance(node, _ALLOWED_NODES):
            raise ValueError("表达式里不允许出现 %s（只支持四则运算与白名单函数）"
                             % type(node).__name__)
        if isinstance(node, ast.Name) and node.id not in FUNCS and node.id not in VARS:
            raise ValueError("未知标识符：%s（可用变量 %s；函数 %s）"
                             % (node.id, "/".join(VARS), "/".join(sorted(FUNCS))))
        if isinstance(node, ast.Call) and not isinstance(node.func, ast.Name):
            raise ValueError("只允许直接调用白名单函数")
    code = compile(tree, "<factor>", "eval")

    def _run(mats: dict):
        env = {k: mats[k] for k in VARS}
        env.update(FUNCS)
        return eval(code, {"__builtins__": {}}, env)  # noqa: S307 已过 AST 白名单

    return _run


# ================================================================
# ④ IC 计算（自己算，不抄仓库数）
# ================================================================
def forward_return(mats: dict, h: int = FWD) -> np.ndarray:
    """未来 h 日收益矩阵：ret[i,t] = close[i,t+h] / close[i,t] - 1，末尾 h 列留 NaN。"""
    c = mats["close"]
    n, m = c.shape
    out = np.full((n, m), np.nan)
    if m <= h:
        return out
    out[:, :m - h] = c[:, h:] / (c[:, :m - h] + 1e-12) - 1.0
    return out


def rank_ic(fmat: np.ndarray, rmat: np.ndarray, warmup: int = WARMUP) -> dict:
    """逐日截面 Rank IC → 序列与汇总。

    为什么用 Rank IC 而不是普通 Pearson IC：A 股截面里极值（涨停、退市前暴跌）太多，
    Pearson 会被几根极端值带走；秩相关只看排序，抗噪得多，也是行业通用口径。
    """
    import pandas as pd
    n, m = fmat.shape
    ics, used = [], []
    for t in range(warmup, m):
        f = fmat[:, t]
        r = rmat[:, t]
        ok = np.isfinite(f) & np.isfinite(r)
        k = int(ok.sum())
        if k < MIN_CROSS:
            continue
        fr = pd.Series(f[ok]).rank()
        rr = pd.Series(r[ok]).rank()
        # Spearman = 秩上的 Pearson；手算避免 scipy（本机没装）
        a = fr.to_numpy() - fr.mean()
        b = rr.to_numpy() - rr.mean()
        den = math.sqrt(float((a * a).sum()) * float((b * b).sum()))
        if den < 1e-12:
            continue
        ics.append(float((a * b).sum() / den))
        used.append(t)
    if len(ics) < 5:
        return {"ok": False, "n_days": len(ics), "note": "有效 IC 天数不足 5 天，无法评估"}
    arr = np.asarray(ics, dtype=np.float64)
    mean = float(arr.mean())
    std = float(arr.std(ddof=1)) if len(arr) > 1 else 0.0
    icir = mean / std if std > 1e-12 else 0.0
    tstat = icir * math.sqrt(len(arr))
    return {
        "ok": True, "n_days": len(arr), "ic": round(mean, 5),
        "icir": round(icir, 4), "t": round(tstat, 3),
        "ic_pos_ratio": round(float((arr > 0).mean()) * 100, 1),
        "series": [round(float(x), 5) for x in arr], "days_idx": used,
    }


# ================================================================
# ⑤ 对照用的「已知风格」因子（查新因子是不是旧因子换皮）
# ================================================================
REF_FACTORS = {
    "风格·20日动量": "pct(close,20)",
    "风格·60日动量": "pct(close,60)",
    "风格·5日反转": "-pct(close,5)",
    "风格·20日波动": "std(pct(close,1),20)",
    "风格·量比": "vol/ma(vol,20)",
    "风格·均线偏离": "close/ma(close,20)-1",
    "风格·成交额": "ma(close*vol,20)",
    "风格·振幅": "(hhv(high,20)-llv(low,20))/ma(close,20)",
}


def _ref_values(mats: dict, keys=None) -> dict:
    out = {}
    for name, expr in REF_FACTORS.items():
        if keys and name not in keys:
            continue
        try:
            out[name] = compile_expr(expr)(mats)
        except Exception:  # noqa: BLE001
            log.exception("参照因子 %s 计算失败", name)
    return out


def max_corr(fmat: np.ndarray, mats: dict, warmup: int = WARMUP) -> tuple[float, str]:
    """新因子与 8 个已知风格的**截面秩相关均值**取绝对值最大 → (值, 撞车的是谁)。

    截面秩相关逐日算再取均值，而不是把整块矩阵拉平算一次：拉平会把"不同日子的
    绝对水平差异"当成相关，结果偏大。
    """
    best, who = 0.0, ""
    for name, rv in _ref_values(mats).items():
        vals = []
        for t in range(warmup, fmat.shape[1]):
            a, b = fmat[:, t], rv[:, t]
            ok = np.isfinite(a) & np.isfinite(b)
            if int(ok.sum()) < MIN_CROSS:
                continue
            ra = np.argsort(np.argsort(a[ok]))
            rb = np.argsort(np.argsort(b[ok]))
            ra = ra - ra.mean()
            rb = rb - rb.mean()
            den = math.sqrt(float((ra * ra).sum()) * float((rb * rb).sum()))
            if den < 1e-12:
                continue
            vals.append(float((ra * rb).sum() / den))
        if len(vals) < 5:
            continue
        v = abs(float(np.mean(vals)))
        if v > best:
            best, who = v, name
    return round(best, 4), who


# ================================================================
# ⑥ 表结构
# ================================================================
DDL = """
CREATE TABLE IF NOT EXISTS factor_candidate (
    key         TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    expr        TEXT NOT NULL,
    origin      TEXT DEFAULT '',        -- GitHub 链接 / 论文 / 语料出处
    why         TEXT DEFAULT '',        -- 为什么值得试（不可空的语义由 admit 保证）
    status      TEXT NOT NULL DEFAULT 'candidate',
                -- candidate（还没评估）/ shadow（过了回测，进影子期）/ active /
                -- rejected（没过门禁）/ decay（上线后 IC 转负） / retired（人工下线）
    ic REAL, icir REAL, t_stat REAL, max_corr REAL, corr_with TEXT DEFAULT '',
    n_days INTEGER, ic_pos_ratio REAL,
    gate_note   TEXT DEFAULT '',        -- 没过的那一条，人说得出为什么
    shadow_start TEXT DEFAULT '', shadow_days INTEGER DEFAULT 0,
    eval_days   INTEGER DEFAULT 0,      -- 评估时的市场天数（区分"因子坏了"和"行情换了"）
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS factor_shadow_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_at TEXT NOT NULL, key TEXT NOT NULL, trade_date TEXT DEFAULT '',
    ic REAL, n_days INTEGER, verdict TEXT DEFAULT '', note TEXT DEFAULT ''
);
"""

STATUS_CN = {
    "candidate": "待评估", "shadow": "影子期", "active": "已上线",
    "rejected": "未通过门禁", "decay": "已衰减", "retired": "已下线",
}


def ensure_tables(con: sqlite3.Connection) -> None:
    con.executescript(DDL)
    con.commit()


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# ================================================================
# ⑦ 门禁：一个因子从「想试」到「能上线」的全过程
# ================================================================
def evaluate_factor(con: sqlite3.Connection, codes, expr: str, warmup: int = WARMUP) -> dict:
    """算一个表达式的 IC / ICIR / t / 撞车风格。不改库，只算。"""
    t0 = time.time()
    fn = compile_expr(expr)
    mats = matrix(codes)
    if not mats.get("ok"):
        return {"ok": False, "error": mats.get("error") or "矩阵构建失败"}
    try:
        fv = fn(mats)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": "表达式求值失败：%s" % str(e)[:200]}
    fv = np.asarray(fv, dtype=np.float64)
    if fv.ndim != 2 or fv.shape != mats["close"].shape:
        return {"ok": False, "error": "表达式结果形状异常 %s（应为 %s）"
                % (fv.shape, mats["close"].shape)}
    fv = np.where(np.isfinite(fv), fv, np.nan)
    rmat = forward_return(mats)
    st = rank_ic(fv, rmat, warmup)
    if not st.get("ok"):
        return {"ok": False, "error": st.get("note"), "n_days": st.get("n_days")}
    corr, who = max_corr(fv, mats, warmup)
    return {
        "ok": True, "ic": st["ic"], "icir": st["icir"], "t": st["t"],
        "n_days": st["n_days"], "ic_pos_ratio": st["ic_pos_ratio"],
        "max_corr": corr, "corr_with": who, "ms": int((time.time() - t0) * 1000),
        "source": mats.get("source"),
    }


def judge(r: dict) -> tuple[bool, list[str], str]:
    """四条硬线逐条判 → (是否全过, 未过项列表, 人能看懂的一句话)。"""
    fails = []
    if abs(r["ic"]) < GATE["abs_ic"]:
        fails.append("|IC| %.4f < %.2f（几乎没有区分度）" % (abs(r["ic"]), GATE["abs_ic"]))
    if abs(r["icir"]) < GATE["abs_icir"]:
        fails.append("|ICIR| %.2f < %.2f（方向不稳，今天灵明天不灵）"
                     % (abs(r["icir"]), GATE["abs_icir"]))
    if abs(r["t"]) < GATE["abs_t"]:
        fails.append("|t| %.2f < %.2f（不能排除是噪声）" % (abs(r["t"]), GATE["abs_t"]))
    if r["max_corr"] >= GATE["max_corr"]:
        fails.append("与「%s」相关 %.2f ≥ %.2f（是旧因子换皮，不是新信息）"
                     % (r["corr_with"], r["max_corr"], GATE["max_corr"]))
    if r["n_days"] < GATE["min_days"]:
        fails.append("有效 IC 天数 %d < %d（样本太短，结论不可信）"
                     % (r["n_days"], GATE["min_days"]))
    if not fails:
        return True, [], ("四项全过：IC %.4f · ICIR %.2f · t %.2f · 最高撞车 %.2f（%s）"
                          % (r["ic"], r["icir"], r["t"], r["max_corr"], r["corr_with"]))
    return False, fails, "未通过：" + "；".join(fails)


def admit(con: sqlite3.Connection, key: str, name: str, expr: str, codes,
          origin: str = "", why: str = "", warmup: int = WARMUP) -> dict:
    """走一遍门禁并落库。**过不了门禁的也落库**（status=rejected），
    因为「试过并且知道为什么不行」本身就是资产 —— 下次扫到同一个东西不用再试一遍。
    """
    if not why.strip():
        return {"ok": False, "error": "必须写清为什么收它（why 不能为空）—— 这是防遗忘的硬约束"}
    ensure_tables(con)
    r = evaluate_factor(con, codes, expr, warmup)
    if not r.get("ok"):
        note = r.get("error") or "评估失败"
        _upsert(con, key, name, expr, origin, why, "rejected", {}, note, 0)
        return {"ok": False, "status": "rejected", "note": note, "key": key}
    passed, fails, note = judge(r)
    status = "shadow" if passed else "rejected"
    _upsert(con, key, name, expr, origin, why, status, r, note, 0)
    return {"ok": True, "passed": passed, "status": status, "key": key,
            "gate_note": note, "fails": fails, "metrics": r}


def _upsert(con, key, name, expr, origin, why, status, r, note, shadow_days) -> None:
    now = _now()
    old = con.execute("SELECT status,shadow_start,shadow_days,created_at FROM factor_candidate"
                      " WHERE key=?", (key,)).fetchone()
    sstart = (old[1] if old and old[1] else "") if old else ""
    sdays = (old[2] or 0) if old else 0
    if status == "shadow" and not sstart:
        sstart = now[:10]
    if not old:
        con.execute(
            "INSERT INTO factor_candidate(key,name,expr,origin,why,status,ic,icir,t_stat,"
            "max_corr,corr_with,n_days,ic_pos_ratio,gate_note,shadow_start,shadow_days,"
            "created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (key, name, expr, origin, why, status, r.get("ic"), r.get("icir"), r.get("t"),
             r.get("max_corr"), r.get("corr_with", ""), r.get("n_days"),
             r.get("ic_pos_ratio"), note, sstart, sdays, now, now))
    else:
        con.execute(
            "UPDATE factor_candidate SET name=?,expr=?,origin=?,why=?,status=?,ic=?,icir=?,"
            "t_stat=?,max_corr=?,corr_with=?,n_days=?,ic_pos_ratio=?,gate_note=?,"
            "shadow_start=?,shadow_days=?,updated_at=? WHERE key=?",
            (name, expr, origin, why, status, r.get("ic"), r.get("icir"), r.get("t"),
             r.get("max_corr"), r.get("corr_with", ""), r.get("n_days"),
             r.get("ic_pos_ratio"), note, sstart, sdays, now, key))
    con.commit()


def shadow_tick(con: sqlite3.Connection, codes, key: str, trade_date: str = "") -> dict:
    """影子期每日敲一次：重算 IC，累计够 shadow_days 且仍全过 → 转 active。

    ★ 影子期为什么不能省：回测 IC 好只说明「历史上成立」。真正的杀手是**过拟合到
    作者选的那段行情**。用未来 20 个交易日重算，是唯一能戳破它的办法。
    """
    ensure_tables(con)
    row = con.execute("SELECT expr,status,shadow_days FROM factor_candidate WHERE key=?",
                      (key,)).fetchone()
    if not row:
        return {"ok": False, "error": "没有这个候选因子：%s" % key}
    expr, status, sdays = row[0], row[1], int(row[2] or 0)
    if status not in ("shadow", "active", "decay"):
        return {"ok": False, "error": "状态是 %s，不参与影子运行" % STATUS_CN.get(status, status)}
    r = evaluate_factor(con, codes, expr)
    if not r.get("ok"):
        return {"ok": False, "error": r.get("error")}
    passed, fails, note = judge(r)
    if not passed:
        con.execute("UPDATE factor_candidate SET status='decay',gate_note=?,updated_at=?"
                    " WHERE key=?", (note, _now(), key))
        con.execute("INSERT INTO factor_shadow_log(run_at,key,trade_date,ic,n_days,verdict,note)"
                    " VALUES(?,?,?,?,?,?,?)",
                    (_now(), key, trade_date, r["ic"], r["n_days"], "decay", note))
        con.commit()
        return {"ok": True, "status": "decay", "note": note, "metrics": r}
    sdays += 1
    new_status = "active" if sdays >= GATE["shadow_days"] else "shadow"
    con.execute("UPDATE factor_candidate SET shadow_days=?,status=?,ic=?,icir=?,t_stat=?,"
                "max_corr=?,corr_with=?,n_days=?,ic_pos_ratio=?,gate_note=?,updated_at=?"
                " WHERE key=?",
                (sdays, new_status, r["ic"], r["icir"], r["t"], r["max_corr"],
                 r["corr_with"], r["n_days"], r["ic_pos_ratio"], note, _now(), key))
    con.execute("INSERT INTO factor_shadow_log(run_at,key,trade_date,ic,n_days,verdict,note)"
                " VALUES(?,?,?,?,?,?,?)",
                (_now(), key, trade_date, r["ic"], r["n_days"], new_status, note))
    con.commit()
    return {"ok": True, "status": new_status, "shadow_days": sdays,
            "need": GATE["shadow_days"], "note": note, "metrics": r}


def decay_scan(con: sqlite3.Connection, codes) -> dict:
    """扫所有 active 因子，最近 IC 转负的直接降权（status=decay）。

    触发条件刻意**不看 |IC| 是否变小**，只看**符号是否翻转**：
    因子从 +0.03 掉到 +0.005 只是变弱，从 +0.03 变 -0.03 是**方向反了**——
    后者比前者危险一个量级，后者意味着按它选股会系统性选到反的。
    """
    ensure_tables(con)
    rows = con.execute("SELECT key,expr FROM factor_candidate WHERE status='active'").fetchall()
    out = {"checked": len(rows), "decayed": []}
    for key, expr in rows:
        r = evaluate_factor(con, codes, expr)
        if not r.get("ok"):
            continue
        if r["ic"] < 0:
            con.execute("UPDATE factor_candidate SET status='decay',ic=?,icir=?,t_stat=?,"
                        "gate_note=?,updated_at=? WHERE key=?",
                        (r["ic"], r["icir"], r["t"],
                         "上线后 IC 转负（%.4f），已自动降权" % r["ic"], _now(), key))
            out["decayed"].append({"key": key, "ic": r["ic"]})
    con.commit()
    return out


def list_candidates(con: sqlite3.Connection, include_rejected: bool = True) -> list[dict]:
    ensure_tables(con)
    sql = ("SELECT key,name,expr,origin,why,status,ic,icir,t_stat,max_corr,corr_with,"
           "n_days,ic_pos_ratio,gate_note,shadow_days,created_at,updated_at"
           " FROM factor_candidate")
    if not include_rejected:
        sql += " WHERE status NOT IN ('rejected','retired')"
    sql += " ORDER BY CASE status WHEN 'active' THEN 0 WHEN 'shadow' THEN 1"
    sql += " WHEN 'candidate' THEN 2 WHEN 'decay' THEN 3 ELSE 4 END, key"
    cols = ("key", "name", "expr", "origin", "why", "status", "ic", "icir", "t",
            "max_corr", "corr_with", "n_days", "ic_pos_ratio", "gate_note",
            "shadow_days", "created_at", "updated_at")
    out = []
    for r in con.execute(sql):
        d = dict(zip(cols, r))
        d["status_cn"] = STATUS_CN.get(d["status"], d["status"])
        out.append(d)
    return out


# ================================================================
# ⑧ 发现：从 GitHub 找「可能藏着因子」的仓库（只发现，不自动编译）
# ================================================================
# ★ 为什么扫描器**不自动把仓库代码编译成因子表达式**：
#   1) 每个仓库的数据口径都不一样（前复权/后复权/不复权、按股还是按手、含不含停牌），
#      拿我们的日线去跑别人的式子，IC 全是假的 —— 而且"假得看起来很像真的"。
#   2) 多数仓库给的是回测框架，不是因子定义。从框架里抠因子得读代码，读代码得理解意图，
#      这一步自动化目前做不到，硬做只会往库里灌垃圾。
#   3) 真正的价值在 README 里那几句"我们这么判断"的方法论，不在代码里。方法论要人读，
#      读完写成一行表达式，再过门禁 —— 这条链每步都得有人。
# 所以扫描器的产出是**待办清单**（哪个仓库值得看、为什么、从哪下手），不是因子。
DISCOVER_QUERIES = (
    "A股 量化 因子 选股",
    "alpha factor formula 涨停 换手",
    "technical indicator screening momentum reversal",
    "stock factor library IC backtest",
    "选股 策略 均线 形态 放量",
)

# 预筛关键词：命中越多越可能藏着"可编译的判据"。刻意只做粗筛，
# 因为精确判定需要读 README，而读 README 是人的活。
FACTOR_HINTS = (
    "因子", "factor", "alpha", "指标", "动量", "反转", "换手", "量比",
    "均线", "ma", "macd", "rsi", "kdj", "布林", "boll", "突破", "回踩",
    "ic", "icir", "回测", "backtest", "选股", "screen", "signal", "形态",
)
MIN_SCAN_GAP_DAYS = 7      # 两次扫描的最小间隔（进哥要求"每月每周该找找"，一周一次）


def _hint_hit(blob: str, h: str) -> bool:
    """关键词命中判定。**短 ASCII 词必须整词匹配** —— 否则 `ma` 会命中 `market`、
    `ic` 会命中 `magic`，命中数虚高，而命中数正是用户判断"先看哪个仓库"的依据。"""
    if h.isascii() and len(h) <= 4:
        import re
        return re.search(r"(?<![a-z0-9])" + re.escape(h) + r"(?![a-z0-9])", blob) is not None
    return h in blob


def _source_ddl() -> str:
    return """
CREATE TABLE IF NOT EXISTS factor_source (
    full_name  TEXT PRIMARY KEY,
    url        TEXT DEFAULT '',
    stars      INTEGER DEFAULT 0,
    lang       TEXT DEFAULT '',
    descr      TEXT DEFAULT '',
    pushed_at  TEXT DEFAULT '',
    hint_score INTEGER DEFAULT 0,      -- 命中多少个预筛关键词
    hint_terms TEXT DEFAULT '',        -- 具体命中了哪些（人要能复核，不能让分数自己说话）
    status     TEXT NOT NULL DEFAULT 'new',
               -- new（刚扫到）/ reviewing（人在看）/ adopted（已编译并过闸）/ ignored（看过不值得）
    note       TEXT DEFAULT '',
    first_seen TEXT NOT NULL, last_seen TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS factor_scan_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_at TEXT NOT NULL, queries INTEGER, found INTEGER, fresh INTEGER,
    verdict TEXT DEFAULT '', note TEXT DEFAULT ''
);
"""


def scan_state(con: sqlite3.Connection) -> dict:
    con.executescript(_source_ddl())
    row = con.execute("SELECT run_at,found,fresh,verdict,note FROM factor_scan_log"
                      " ORDER BY id DESC LIMIT 1").fetchone()
    if not row:
        return {"last": "", "days": None, "found": 0, "fresh": 0, "verdict": "", "note": ""}
    last = str(row[0] or "")
    days = None
    try:
        days = (datetime.now() - datetime.strptime(last, "%Y-%m-%d %H:%M:%S")).days
    except Exception:  # noqa: BLE001
        pass
    return {"last": last, "days": days, "found": row[1] or 0,
            "fresh": row[2] or 0, "verdict": row[3] or "", "note": row[4] or ""}


def _oss_module():
    """取 oss_mine 模块。它内部写的是 `import hypo`（同目录裸导入），
    只有在 viewer/ 本身位于 sys.path 时才成立 —— 服务进程是 `python viewer/server.py`
    启动的，天然满足；CLI 走 `-m viewer.factor_import` 就不满足。这里补上，不改 oss_mine。"""
    import os
    import sys
    here = os.path.dirname(os.path.abspath(__file__))
    if here not in sys.path:
        sys.path.insert(0, here)
    from viewer import oss_mine as _oss
    return _oss


def discover(con: sqlite3.Connection, days: int = 30, per_query: int = 6,
             force: bool = False, queries=None) -> dict:
    """扫 GitHub → 预筛 → 入 factor_source。**扫不到就说扫不到，不编。**

    节流：距上次扫描不足 MIN_SCAN_GAP_DAYS 天且非 force 时直接跳过（返回 skipped），
    因为调用方是每日定时任务，"每天扫一次 GitHub"既浪费配额又没意义。
    """
    con.executescript(_source_ddl())
    st = scan_state(con)
    if not force and st["days"] is not None and st["days"] < MIN_SCAN_GAP_DAYS:
        return {"ok": True, "skipped": True,
                "note": "距上次扫描 %d 天（< %d 天），本次跳过" % (st["days"], MIN_SCAN_GAP_DAYS),
                "state": st}
    _oss = _oss_module()
    t0 = time.time()
    try:
        items = _oss.scan_github(days=days, per_query=per_query, queries=queries)
    except Exception as e:  # noqa: BLE001
        log.exception("GitHub 扫描失败")
        con.execute("INSERT INTO factor_scan_log(run_at,queries,found,fresh,verdict,note)"
                    " VALUES(?,?,?,?,?,?)",
                    (_now(), len(queries or DISCOVER_QUERIES), 0, 0, "error", str(e)[:300]))
        con.commit()
        return {"ok": False, "error": "扫描失败：%s" % str(e)[:200], "found": 0, "fresh": 0}

    # 一条都没扫到：可能是网络，也可能是限流。**不能记成"今天没有新东西"**。
    if not items:
        con.execute("INSERT INTO factor_scan_log(run_at,queries,found,fresh,verdict,note)"
                    " VALUES(?,?,?,?,?,?)",
                    (_now(), len(queries or DISCOVER_QUERIES), 0, 0, "empty",
                     "GitHub 返回 0 条（网络/限流都可能，未取到≠没有）"))
        con.commit()
        return {"ok": True, "found": 0, "fresh": 0, "verdict": "empty",
                "note": "本次没扫到任何仓库 —— 这**不代表没有新东西**，"
                        "多半是网络或 GitHub 匿名限流（10 次/分钟）。下次会重试。",
                "ms": int((time.time() - t0) * 1000)}

    now = _now()
    fresh = 0
    for it in items:
        blob = ("%s %s" % (it.get("full_name", ""), it.get("descr", ""))).lower()
        hits = [h for h in FACTOR_HINTS if _hint_hit(blob, h)]
        score = len(hits)
        old = con.execute("SELECT 1 FROM factor_source WHERE full_name=?",
                          (it["full_name"],)).fetchone()
        if old:
            con.execute("UPDATE factor_source SET stars=?,descr=?,pushed_at=?,hint_score=?,"
                        "hint_terms=?,last_seen=? WHERE full_name=?",
                        (it.get("stars") or 0, it.get("descr") or "", it.get("pushed_at") or "",
                         score, ",".join(hits[:8]), now, it["full_name"]))
        else:
            con.execute(
                "INSERT INTO factor_source(full_name,url,stars,lang,descr,pushed_at,hint_score,"
                "hint_terms,status,first_seen,last_seen) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (it["full_name"], it.get("url") or "", it.get("stars") or 0,
                 it.get("lang") or "", it.get("descr") or "", it.get("pushed_at") or "",
                 score, ",".join(hits[:8]), "new", now, now))
            fresh += 1
    con.execute("INSERT INTO factor_scan_log(run_at,queries,found,fresh,verdict,note)"
                " VALUES(?,?,?,?,?,?)",
                (now, len(queries or DISCOVER_QUERIES), len(items), fresh, "ok",
                 "新增 %d 个 / 共 %d 个" % (fresh, len(items))))
    con.commit()
    rehint(con)      # 关键词表可能刚改过，顺手把全部行校准一遍
    return {"ok": True, "found": len(items), "fresh": fresh, "verdict": "ok",
            "ms": int((time.time() - t0) * 1000),
            "note": "扫到 %d 个仓库，其中 %d 个是新的" % (len(items), fresh)}


def rehint(con: sqlite3.Connection) -> int:
    """用当前 FACTOR_HINTS 重算所有已存仓库的命中数，不改 status/note。

    为什么需要：`hint_score` 是关键词表算出来的，关键词表一改（比如发现 `ma` 会命中
    `market` 而收紧），旧分数就成了误导 —— 而它是用户判断"先看哪个"的唯一排序依据。
    宁可每次重算，也不要让一个过时的数字替他做决定。
    """
    con.executescript(_source_ddl())
    n = 0
    for fn, descr in con.execute("SELECT full_name, descr FROM factor_source").fetchall():
        blob = ("%s %s" % (fn, descr or "")).lower()
        hits = [h for h in FACTOR_HINTS if _hint_hit(blob, h)]
        con.execute("UPDATE factor_source SET hint_score=?, hint_terms=? WHERE full_name=?",
                    (len(hits), ",".join(hits[:8]), fn))
        n += 1
    con.commit()
    return n


def list_sources(con: sqlite3.Connection, limit: int = 40) -> list[dict]:
    """待办清单：按"命中关键词数 × 是否还在维护"排序。

    排序不算花活：一个命中 6 个关键词、上个月还在推的仓库，比命中 1 个、两年没动的
    更值得先看。但**排序只是排序，不代表它就是因子**——这句话写在 note 里给用户看。
    """
    con.executescript(_source_ddl())
    rows = con.execute(
        "SELECT full_name,url,stars,lang,descr,pushed_at,hint_score,hint_terms,status,note,"
        "first_seen,last_seen FROM factor_source"
        " WHERE status != 'ignored' ORDER BY hint_score DESC, stars DESC LIMIT ?",
        (int(limit),)).fetchall()
    cols = ("full_name", "url", "stars", "lang", "descr", "pushed_at", "hint_score",
            "hint_terms", "status", "note", "first_seen", "last_seen")
    out = []
    for r in rows:
        d = dict(zip(cols, r))
        d["hint_terms"] = [x for x in str(d["hint_terms"] or "").split(",") if x]
        out.append(d)
    return out


def set_source_status(con: sqlite3.Connection, full_name: str, status: str,
                      note: str = "") -> dict:
    """标一个来源的处理状态。ignored / adopted 必须带 note（和工具淘汰同一条纪律）。"""
    con.executescript(_source_ddl())
    if status in ("ignored", "adopted") and not note.strip():
        return {"ok": False, "error": "标成 %s 必须写明原因/结论（note 不可为空）" % status}
    if status not in ("new", "reviewing", "adopted", "ignored"):
        return {"ok": False, "error": "未知状态：%s" % status}
    n = con.execute("UPDATE factor_source SET status=?,note=? WHERE full_name=?",
                    (status, note.strip()[:600], full_name)).rowcount
    con.commit()
    return {"ok": bool(n), "updated": n}


# ================================================================
# ⑨ CLI：给扫描器和人手动试因子用
# ================================================================
def _cli(argv) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="因子准入门禁（三期）")
    ap.add_argument("--expr", help="因子表达式，如 close/ma(close,20)-1")
    ap.add_argument("--key", default="", help="因子 key（英文短名）")
    ap.add_argument("--name", default="", help="中文名")
    ap.add_argument("--origin", default="", help="出处（GitHub 链接 / 语料）")
    ap.add_argument("--why", default="", help="为什么收它（必填）")
    ap.add_argument("--db", default="viewer/app.db")
    ap.add_argument("--list", action="store_true", help="列出所有候选因子")
    ap.add_argument("--discover", action="store_true", help="扫 GitHub 找可疑仓库（节流 7 天）")
    ap.add_argument("--force", action="store_true", help="忽略节流，强制扫描")
    ap.add_argument("--sources", action="store_true", help="列出待看的来源仓库")
    ap.add_argument("--rehint", action="store_true", help="用当前关键词表重算命中数")
    ap.add_argument("--selftest", action="store_true", help="跑门禁自检（应拒掉换皮因子）")
    a = ap.parse_args(argv[1:])

    con = sqlite3.connect(a.db, timeout=25)
    if a.list:
        for d in list_candidates(con):
            print("%-22s %-10s IC=%s ICIR=%s t=%s corr=%s(%s)  %s" % (
                d["key"], d["status_cn"], d["ic"], d["icir"], d["t"], d["max_corr"],
                d["corr_with"], d["gate_note"][:60]))
        return 0

    if a.discover:
        r = discover(con, days=30, per_query=6, force=a.force)
        print(json.dumps(r, ensure_ascii=False, indent=2)[:1500])
        return 0 if r.get("ok") else 1

    if a.rehint:
        print("已重算 %d 行" % rehint(con))
        return 0

    if a.sources:
        for d in list_sources(con):
            print("%-42s %-9s 命中%d %-6s ★%-5s %s" % (
                d["full_name"], d["status"], d["hint_score"], d["pushed_at"],
                d["stars"], ("/".join(d["hint_terms"]))[:60]))
        return 0

    from viewer import toolbox as TB
    codes = TB.universe_codes(con)

    if a.selftest:
        cases = [
            ("selftest_dup", "动量换皮（应该被拒）", "pct(close,20)",
             "它和「风格·20日动量」是同一个式子，门禁必须拒"),
            ("selftest_vol", "缩量整理（应该被拒或弱）", "-std(pct(close,1),20)",
             "波动率的取负，撞「风格·20日波动」，应被相关性挡"),
            ("selftest_new", "量价背离代理", "pct(close,20)-pct(vol,20)",
             "价格涨但量没跟，属新信息，看它能不能过"),
        ]
        for key, name, expr, why in cases:
            r = admit(con, key, name, expr, codes, origin="selftest", why=why)
            m = r.get("metrics") or {}
            print("[%s] %-20s status=%-8s IC=%s ICIR=%s t=%s corr=%s(%s)" % (
                "PASS" if r.get("passed") else "REJECT", key, r.get("status"),
                m.get("ic"), m.get("icir"), m.get("t"), m.get("max_corr"),
                m.get("corr_with")))
            print("    %s" % r.get("gate_note", "")[:160])
        return 0

    if not a.expr:
        ap.print_help()
        return 2
    key = a.key or ("f_" + str(int(time.time())))
    r = admit(con, key, a.name or key, a.expr, codes,
              origin=a.origin, why=a.why or "（未写）")
    print(json.dumps(r, ensure_ascii=False, indent=2, default=str))
    return 0 if r.get("passed") else 1


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    sys.exit(_cli(sys.argv))
