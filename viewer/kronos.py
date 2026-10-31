# viewer/kronos.py
"""Kronos K 线基础模型预测板块（2026-10-24 新增）。

设计哲学（与「每日预测 / 博主共振 / 量化」一致，并叠加 Kronos 自身的诚实纪律）：
- 离线算 → 落库 → 前端只读。
- Kronos 是开源的「K 线基础模型」（shiyu-coder/Kronos，MIT）：它输出的是
  **未来 OHLC 的概率分布预测**，不是确定性买卖信号。本板块只做「概率预测 +
  有限样本回测」，绝不包装成"必胜信号"。
- 模型权重是**冻结资产**（GitHub 源码 + HuggingFace 权重，版本钉死）；本机推理
  零 token、零 API 费用、可完全离线（首次需下载权重）。

依赖：torch（CPU 版）+ 内置 kronos_core/ 模型代码 + HuggingFace 权重。
这些在沙箱里可能没有，故**全部惰性导入**——即使没装 torch，本模块也能 import、
ensure_tables、status_payload（返回 ready=false + 指引），不会拖垮主后端启动。

数据：复用 predictor.load_kcache() 的本地 A 股日 K 缓存（open/high/low/close），
与「每日预测 / 共振 / 量化」同一份底座，不重复抓取、不硬联网。

参考（已确认的事实，非推测）：
- 官方示例 examples/prediction_wo_vol_example.py 证明：仅用 ['open','high','low','close']
  四列即可预测（无需 volume/amount）。
- KronosPredictor.predict(df, x_timestamp, y_timestamp, pred_len, T, top_p, sample_count,
  verbose) → pandas.DataFrame，列 open/high/low/close/volume/amount，索引为 y_timestamp。
- 权重加载：KronosTokenizer.from_pretrained(repo) / Kronos.from_pretrained(repo)
  （PyTorchModelHubMixin，经 huggingface_hub 下载；本工具改为 local_files_only 走本地快照）。
"""
from __future__ import annotations

import json
import math
import os
import sqlite3
import sys
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

ENGINE_VER = "kronos-v1.0"

# —— 推理 / 回测口径（集中、可配，避免散落魔法数）——
LOOKBACK = 400        # 喂给模型的历史 K 线根数（交易日）
PRED_LEN = 60         # 单次前瞻预测步数（交易日，≈3 个月）
MIN_BARS = 130        # 与 predictor.MIN_BARS 对齐（A 股技术面判定下限）
TOP_N_DEFAULT = 60    # 单次 run 最多覆盖的股票数（控算力）
WINDOWS = 6           # 回测滚动窗口数（控算力；非全历史穷举，诚实标注）
SAMPLE_COUNT = 1      # 采样数（>1 可估计离散度→置信度；默认 1 控算力）
THRESH = 0.02         # 2% 阈值（与官方 run_backtest_kronos 同口径：方向信号触发）
WINDOWS_FAST = 1      # 快速模式回测窗口数（1 窗即可给出方向有效性，耗时降到 1/6）

# —— 评分口径（2026-10-25 进哥：「你得给个评分，满分 100 最低 0，帮我判断力度」）——
# 四维加权，每维都记账（得分 / 满分 / 理由），前端可展开核对 —— 不做黑箱。
# 关键设计：不直接用「预测收益」当分数。模型爱吹幅度（万科A 曾预测 +47% 而回测夏普 -1.72），
# 所以方向强度只占 30 分且有 ±25% 截断，另外 45 分交给「回测能不能印证」与「风险可不可控」。
W_DIR, W_BT, W_RISK, W_CONF = 30.0, 25.0, 25.0, 20.0
RET_CAP = 0.25          # 预测收益映射上限：±25% 之外不再加分
DISP_LO, DISP_HI = 0.02, 0.60   # 预测离散率（disp / 最新价）映射区间：越小越可信
GRADES = [(80, "强"), (65, "较强"), (50, "一般"), (35, "弱"), (0, "不可用")]
GRADE_NOTE = {
    "强": "四维共振：方向明确、回测印证、回撤可控",
    "较强": "多数维度支持，可进候选",
    "一般": "信号存在但证据不足，只作观察",
    "弱": "至少一维严重拖累（多为预测幅度与回测矛盾）",
    "不可用": "回测无信息量或样本不足，不建议参考",
}
PREFIX_KEYS = ("0", "3", "6")   # 与「长期底部埋伏」同口径：代码首位
PREFIX_LABEL = {"0": "0开头", "3": "3开头", "6": "6开头"}

DDL = """
CREATE TABLE IF NOT EXISTS kronos_run (
  run_date   TEXT PRIMARY KEY,
  engine_ver TEXT,
  model_ver  TEXT,
  data_date  TEXT,
  status     TEXT,
  n_stocks   INTEGER,
  elapsed    REAL,
  note       TEXT,
  at         TEXT
);
CREATE TABLE IF NOT EXISTS kronos_item (
  run_date      TEXT,
  code          TEXT,
  name          TEXT,
  last_close    REAL,
  pred_close    REAL,
  pred_return   REAL,
  pred_dir      TEXT,
  disp          REAL,
  bt_total_ret  REAL,
  bt_annual     REAL,
  bt_vol        REAL,
  bt_sharpe     REAL,
  bt_maxdd      REAL,
  bt_winrate    REAL,
  bt_trades     INTEGER,
  err           TEXT,
  sample_json   TEXT,
  PRIMARY KEY (run_date, code)
);
"""

# 旧表迁移（err 列 2026-10-24b 新增；已存在的表 ALTER 报 duplicate column 则忽略）
_MIGRATE = [
    "ALTER TABLE kronos_item ADD COLUMN err TEXT",
]

_LOCK = threading.Lock()
_STATUS = {"stage": None, "detail": None, "at": None, "running": False}

_CORE_DIR = Path(__file__).resolve().parent / "kronos_core"
_REPO_CFG = _CORE_DIR / "model_repo.json"

_PREDICTOR_CACHE = {"obj": None}


# ================================================================ 基础工具
def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def _f(x):
    try:
        return float(x)
    except Exception:  # noqa: BLE001
        return None


def _is_ashare(code: str) -> bool:
    """与 quant._is_ashare / pillars 同口径：只认真 A 股（剔可转债 / B 股）。"""
    c = str(code or "")
    return len(c) == 6 and c.startswith(("60", "00", "30", "68", "92", "43", "83", "87"))


def _read_names(con) -> dict:
    try:
        rows = con.execute("SELECT code, name FROM stock_names").fetchall()
        return {r["code"]: r["name"] for r in rows}
    except Exception:  # noqa: BLE001
        return {}


def _kline_cache() -> dict:
    """读本地日 K 缓存（惰性导入 predictor，沙箱可无 predictor）。"""
    import predictor as P  # noqa: N806
    return P.load_kcache() or {}


def _materialize(bars_in: dict, codes) -> dict:
    """把选中的代码转成 [(date,o,h,l,c,v), ...]（升序）。

    2026-10-25：原实现无条件遍历全市场 5193 只再切片，等于把 1000 万根 K 线全部转一遍元组，
    纯属浪费。现在只物化真正要推理的那几十只。
    """
    out: dict = {}
    for code in codes:
        v = bars_in.get(code)
        if not v or len(v) < MIN_BARS:
            continue
        rows = []
        for b in v:
            try:
                rows.append((str(b.get("d"))[:10], _f(b.get("o")), _f(b.get("h")),
                             _f(b.get("l")), _f(b.get("c")), _f(b.get("v"))))
            except Exception:  # noqa: BLE001
                continue
        rows = [r for r in rows if r[0] and r[4]]
        if len(rows) >= MIN_BARS:
            rows.sort(key=lambda x: x[0])
            out[code] = rows
    return out


def _pick_codes(bars_in: dict, top_n: int, prefixes=None) -> list:
    """按板块均衡挑候选代码。

    为什么必须均衡：进哥要按「0 开头 / 3 开头 / 6 开头」筛。若沿用原来的「字典前 N 个」，
    取到的会全是 00 开头（本地缓存按代码升序），切到「6 开头」就一只都不剩 ——
    筛选项点了没东西，比没有筛选项更糟。故按首位分组后轮流取，保证每个板块都有货。
    P2（2026-10-26）：跳过剔除池（ST/博主噪声/无K线），从干净宇宙抽样，不再把垃圾送进模型推理。
    """
    excluded = set()
    try:
        from viewer.universe_filter import get_exclusion_pool
        excluded, _, _ = get_exclusion_pool()
    except Exception:  # noqa: BLE001
        excluded = set()
    buckets: dict = {}
    want = set(prefixes) if prefixes else set(PREFIX_KEYS)
    for code in bars_in:
        c = str(code or "")
        if c in excluded:
            continue
        if not _is_ashare(c):
            continue
        if len(bars_in[code] or []) < MIN_BARS:
            continue
        p = c[0]
        if p not in want:
            continue
        buckets.setdefault(p, []).append(c)
    if not buckets:
        return []
    keys = [k for k in PREFIX_KEYS if k in buckets]
    for k in sorted(buckets):          # 兜底：非 0/3/6 的也照顾到（如 68 已含在 6 里）
        if k not in keys:
            keys.append(k)
    lists = {k: sorted(buckets[k]) for k in keys}
    picked, idx = [], {k: 0 for k in keys}
    while len(picked) < top_n:
        moved = False
        for k in keys:
            if len(picked) >= top_n:
                break
            if idx[k] < len(lists[k]):
                picked.append(lists[k][idx[k]])
                idx[k] += 1
                moved = True
        if not moved:
            break
    return picked


def _universe_bars(only_codes=None):
    """兼容入口：未指定 only_codes 时返回全量（老调用方用），否则只物化指定代码。"""
    kc = _kline_cache()
    bars_in = kc.get("bars") or {}
    codes = list(only_codes) if only_codes else [
        c for c in bars_in if _is_ashare(c) and len(bars_in[c] or []) >= MIN_BARS]
    return _materialize(bars_in, codes), kc.get("updated")


def _future_dates(last_date_str: str, n: int) -> list:
    """从最后一根 K 线日期之后，生成 n 个跳过周末的"未来交易日"（用于前瞻预测的时间戳）。"""
    d = datetime.strptime(last_date_str, "%Y-%m-%d")
    out = []
    while len(out) < n:
        d += timedelta(days=1)
        if d.weekday() < 5:  # 0=Mon .. 4=Fri
            out.append(d.strftime("%Y-%m-%d"))
    return out


# ================================================================ 配置 / 就绪探测
def _load_repo_cfg() -> dict:
    default = {
        "model_repo": "NeoQuasar/Kronos-small",
        "tokenizer_repo": "NeoQuasar/Kronos-Tokenizer-base",
        "revision": "main",
        "max_context": 512,
        "weights_dir": "weights",
        "mirror": "https://hf-mirror.com",
    }
    try:
        with open(_REPO_CFG, encoding="utf-8") as f:
            cfg = json.load(f)
        default.update(cfg)
    except Exception:  # noqa: BLE001
        pass
    return default


_PROBE_MEMO = {"at": 0.0, "val": None}
_PROBE_TTL = 600.0        # 就绪状态 10 分钟一探即可（装没装 torch、权重在不在，不会秒秒变化）


def probe_ready_nonblock() -> dict:
    """给 HTTP 用的**非阻塞**版本：有缓存就回缓存；没有就起后台线程探测，本次返回「探测中」。

    为什么需要：`import torch` 首次要 13.8 秒，如果请求线程直接等，点开面板就是整站卡 13 秒。
    就绪状态本来就只需要「最终会知道」，不需要「这次请求立刻知道」。
    """
    import threading as _th
    hit = _PROBE_MEMO["val"]
    if hit is not None:
        return hit
    if not _PROBE_MEMO.get("probing"):
        _PROBE_MEMO["probing"] = True

        def _run():
            try:
                _probe_ready()
            finally:
                _PROBE_MEMO["probing"] = False
        _th.Thread(target=_run, name="kronos-probe", daemon=True).start()
    return {"ok": None, "reason": "probing", "hint": "正在探测 Kronos 引擎（首次加载 torch 约 10 秒），稍后自动就绪"}


def _probe_ready() -> dict:
    """探测推理链是否就绪（零 torch 也能跑，用于 status / 前端徽章）。

    2026-10-29：**必须记忆化**。`import torch` 实测要 **13.8 秒**（CPU 版首次加载大量
    .pyd/.dll），而 `/api/kronos/status` 每次打开面板都会调它 —— 13.8 秒里 GIL 被占满，
    整站其它接口一起卡。就绪与否是「静态事实」（装没装、权重在不在），缓存 10 分钟即可。
    """
    import time as _t
    hit = _PROBE_MEMO["val"]
    if hit is not None and (_t.time() - _PROBE_MEMO["at"]) < _PROBE_TTL:
        return hit
    res = _probe_ready_uncached()
    _PROBE_MEMO["val"] = res
    _PROBE_MEMO["at"] = _t.time()
    return res


def _probe_ready_uncached() -> dict:
    try:
        import torch  # noqa: F401
    except Exception:  # noqa: BLE001
        return {"ok": False, "reason": "torch_missing",
                "hint": "未安装 PyTorch(CPU)。请在查看器目录运行：python setup_kronos.py"}
    if not (_CORE_DIR / "kronos.py").exists():
        return {"ok": False, "reason": "code_missing",
                "hint": "未找到内置模型代码 kronos_core/，请运行：python setup_kronos.py"}
    cfg = _load_repo_cfg()
    wd = _CORE_DIR / (cfg.get("weights_dir") or "weights")
    if not wd.exists():
        return {"ok": False, "reason": "weights_missing",
                "hint": "未下载模型权重，请运行：python setup_kronos.py（首次需联网）"}
    return {"ok": True}


def _model_ver() -> str:
    cfg = _load_repo_cfg()
    return f"{cfg.get('model_repo')}@{cfg.get('revision')}"


# ================================================================ 惰性加载推理引擎
def _load_predictor():
    """惰性加载 Kronos 推理器；torch / 模型代码 / 权重任一缺失都抛清晰错误。"""
    ready = _probe_ready()
    if not ready.get("ok"):
        raise RuntimeError(ready.get("hint") or "Kronos 推理链未就绪")

    try:
        # 优先按包路径导入（server 以 python -m viewer.server 运行时可用）：
        # 不往 sys.path 里插 viewer 目录，避免 viewer 本地模块（如 bottleneck_hunter）
        # 与第三方包撞名时把真包挡住（pandas 依赖真 bottleneck，见 2026-10-24b 修复）。
        from viewer import kronos_core  # 内置、已打过补丁的 model 包（from .module import *）
    except ImportError:
        # 兜底：独立脚本/无包上下文场景，才把 viewer 目录加到 path 末尾（真包优先）
        if str(_CORE_DIR.parent) not in sys.path:
            sys.path.append(str(_CORE_DIR.parent))
        try:
            import kronos_core
        except Exception as e:  # noqa: BLE001
            raise RuntimeError(f"内置模型代码导入失败：{e}；请重新运行 setup_kronos.py")
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(f"内置模型代码导入失败：{e}；请重新运行 setup_kronos.py")

    cfg = _load_repo_cfg()
    wd = _CORE_DIR / (cfg.get("weights_dir") or "weights")
    model_dir = wd / _repo_folder(cfg["model_repo"])
    tok_dir = wd / _repo_folder(cfg["tokenizer_repo"])
    try:
        tokenizer = kronos_core.KronosTokenizer.from_pretrained(
            str(tok_dir), local_files_only=True)
        model = kronos_core.Kronos.from_pretrained(
            str(model_dir), local_files_only=True)
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(
            f"权重加载失败（请确认 setup_kronos.py 已成功下载 {cfg['model_repo']} / "
            f"{cfg['tokenizer_repo']}）：{e}")

    device = "cuda" if _has_cuda() else "cpu"
    predictor = kronos_core.KronosPredictor(
        model, tokenizer, device=device, max_context=int(cfg.get("max_context", 512)))
    return predictor


def _has_cuda() -> bool:
    try:
        import torch
        return torch.cuda.is_available()
    except Exception:  # noqa: BLE001
        return False


def _repo_folder(repo: str) -> str:
    """NeoQuasar/Kronos-mini → NeoQuasar__Kronos-mini（hf_hub_download 本地目录命名同义）。"""
    return repo.replace("/", "__")


# ================================================================ 单次预测 / 回测
def _predict_close(predictor, rows, start, pred_len, y_dates, sample_count=1):
    """对 [start-LOOKBACK, start) 历史预测未来 pred_len 根，返回 predicted close 列表。

    start=None 表示用最新一段历史做前瞻（y_dates 为合成未来交易日）。
    返回 (pred_close_list, y_dates)。
    """
    import pandas as pd  # 惰性：仅推理时需要

    if start is None:
        hist = rows[-LOOKBACK:]
        y_dates = _future_dates(hist[-1][0], pred_len)
    else:
        hist = rows[start - LOOKBACK:start]

    df = pd.DataFrame([[r[1], r[2], r[3], r[4]] for r in hist],
                      columns=["open", "high", "low", "close"])
    # 官方 calc_time_stamps 用 .dt 访问器，必须传 pandas Series（DatetimeIndex 会 AttributeError）
    x_ts = pd.Series([pd.Timestamp(r[0]) for r in hist])
    y_ts = pd.Series([pd.Timestamp(d) for d in y_dates])

    pred = predictor.predict(
        df=df, x_timestamp=x_ts, y_timestamp=y_ts, pred_len=pred_len,
        T=1.0, top_p=0.9, sample_count=sample_count, verbose=False)
    pred_close = [float(x) for x in pred["close"].tolist()]
    return pred_close, y_dates


def _backtest_stock(predictor, rows, windows=WINDOWS, sample_count=SAMPLE_COUNT):
    """在多个滚动窗口上做方向策略回测（样本有限，非全历史穷举）。

    策略（与官方 run_backtest_kronos 同口径的 2% 方向信号）：
      第 t 日，模型预测收盘相对前收的偏移 pm = pred[t]/prev - 1；
      pm > +2% → 做多(+1)；pm < -2% → 做空(-1)；否则空仓(0)。
      当日已实现收益 = 仓位 × (actual[t]/prev - 1)。
    跨所有窗口拼接日收益序列，汇总总收益/年化/波动/夏普/最大回撤/胜率/交易次数。
    """
    n = len(rows)
    lo, hi = LOOKBACK, n - PRED_LEN
    if hi <= lo:
        return {"error": f"数据不足（需 ≥ {LOOKBACK + PRED_LEN} 根，现有 {n}）"}
    starts = [lo + i * (hi - lo) // max(windows, 1) for i in range(windows)]
    daily = []
    sample = None
    for s in starts:
        actual = rows[s:s + PRED_LEN]
        if len(actual) < PRED_LEN:
            continue
        y_dates = [a[0] for a in actual]
        try:
            pred_close, _ = _predict_close(predictor, rows, s, PRED_LEN, y_dates, sample_count)
        except Exception as e:  # noqa: BLE001
            return {"error": f"窗口预测失败：{e}"}
        actual_closes = [a[4] for a in actual]
        prev = rows[s - 1][4]
        for t in range(PRED_LEN):
            pm = (pred_close[t] / prev - 1.0) if prev else 0.0
            pos = 1 if pm > THRESH else (-1 if pm < -THRESH else 0)
            ac = actual_closes[t]
            rr = pos * ((ac / prev - 1.0) if prev else 0.0)
            daily.append(rr)
            prev = ac
        if sample is None:
            sample = {"dates": y_dates, "pred": pred_close, "actual": actual_closes}
    if not daily:
        return {"error": "无有效回测窗口"}
    m = _metrics(daily)
    m["sample"] = sample
    return m


def _predict_forward(predictor, rows, sample_count=SAMPLE_COUNT):
    """用最新一段历史做前瞻预测，返回 (pred_close_last, pred_return, disp, pred_curve)。"""
    if len(rows) < LOOKBACK:
        return None
    y_dates = _future_dates(rows[-1][0], PRED_LEN)
    pred_close, _ = _predict_close(predictor, rows, None, PRED_LEN, y_dates, sample_count)
    last_close = rows[-1][4]
    pred_close_last = pred_close[-1]
    pred_return = (pred_close_last / last_close - 1.0) if last_close else 0.0
    disp = float(__import__("statistics").pstdev(pred_close)) if len(pred_close) > 1 else 0.0
    return {
        "pred_close_last": pred_close_last,
        "pred_return": pred_return,
        "disp": disp,
        "curve": {"dates": y_dates, "pred": pred_close,
                  "last_close": last_close, "last_date": rows[-1][0]},
    }


def _metrics(rets: list) -> dict:
    """从日收益序列汇总回测指标（无风险利率取 0）。"""
    n = len(rets)
    if n == 0:
        return {}
    total = 1.0
    eq = [1.0]
    for r in rets:
        total *= (1.0 + r)
        eq.append(total)
    total_ret = total - 1.0
    mean = sum(rets) / n
    var = sum((r - mean) ** 2 for r in rets) / n
    std = math.sqrt(var) if var > 0 else 0.0
    annual = (total ** (252.0 / n) - 1.0) if total > 0 else -1.0
    vol = std * math.sqrt(252.0)
    sharpe = (mean / std) * math.sqrt(252.0) if std > 0 else 0.0
    peak, mdd = eq[0], 0.0
    for v in eq:
        if v > peak:
            peak = v
        dd = (v - peak) / peak if peak else 0.0
        if dd < mdd:
            mdd = dd
    wins = sum(1 for r in rets if r > 0)
    trades = sum(1 for r in rets if r != 0)
    return {
        "bt_total_ret": total_ret, "bt_annual": annual, "bt_vol": vol,
        "bt_sharpe": sharpe, "bt_maxdd": mdd, "bt_winrate": wins / n,
        "bt_trades": trades, "bt_days": n,
    }


# ================================================================ 落库 / 运行
def _clamp(x, lo, hi):
    try:
        x = float(x)
    except Exception:  # noqa: BLE001
        return lo
    return lo if x < lo else (hi if x > hi else x)


def _grade(score: float) -> str:
    for th, name in GRADES:
        if score >= th:
            return name
    return "不可用"


def score_item(it: dict) -> dict:
    """0-100 综合评分（四维加权，逐维记账）。

    「评分」回答的是「这个判断值不值得参考」，不是「该不该买」。
    方向由 pred_return 的符号单独给出，两者分开看才不会把
    「预测很猛但回测亏成狗」的票误读成「强看多」。
    """
    pr = it.get("pred_return")
    sh = it.get("bt_sharpe")
    wr = it.get("bt_winrate")
    dd = it.get("bt_maxdd")
    vol = it.get("bt_vol")
    disp = it.get("disp")
    lc = it.get("last_close")
    trades = it.get("bt_trades")

    parts, flags = [], []

    # ① 方向强度 30：预测收益线性映射，±RET_CAP 截断（模型幅度不可信，不许单指标刷分）
    if pr is None:
        s1, w1 = 0.0, "无预测"
    else:
        s1 = W_DIR / 2.0 + _clamp(pr, -RET_CAP, RET_CAP) / RET_CAP * (W_DIR / 2.0)
        w1 = "预测 %+.2f%%（±%.0f%% 截断映射）" % (pr * 100, RET_CAP * 100)
    parts.append({"k": "方向强度", "got": round(s1, 1), "max": W_DIR, "why": w1})

    # ② 回测有效性 25：模型的方向判断到底有没有信息量（夏普 15 + 胜率 10）
    if sh is None:
        s2, w2 = W_BT / 2.0, "缺回测数据，给中位分"
    else:
        a = (_clamp(sh, -0.5, 2.0) + 0.5) / 2.5 * 15.0
        b = _clamp(((wr if wr is not None else 0.45) - 0.30) / 0.40, 0, 1) * 10.0
        s2 = a + b
        w2 = "夏普 %.2f → %.1f/15，胜率 %s → %.1f/10" % (
            sh, a, ("%.0f%%" % (wr * 100)) if wr is not None else "—", b)
    parts.append({"k": "回测有效性", "got": round(s2, 1), "max": W_BT, "why": w2})

    # ③ 风险可控 25：最大回撤 15 + 波动率 10（越浅 / 越低越好）
    if dd is None:
        r1, w3a = 7.5, "缺回撤"
    else:
        r1 = _clamp((dd + 0.70) / 0.60, 0, 1) * 15.0
        w3a = "回撤 %.1f%%" % (dd * 100)
    if vol is None:
        r2, w3b = 5.0, "缺波动"
    else:
        r2 = _clamp((0.80 - vol) / 0.65, 0, 1) * 10.0
        w3b = "波动 %.1f%%" % (vol * 100)
    s3 = r1 + r2
    parts.append({"k": "风险可控", "got": round(s3, 1), "max": W_RISK,
                  "why": "%s → %.1f/15，%s → %.1f/10" % (w3a, r1, w3b, r2)})

    # ④ 置信度 20：预测轨迹离散率 12 + 回测样本数 8
    if disp is not None and lc:
        dr = float(disp) / float(lc) if lc else 1.0
        c1 = _clamp((DISP_HI - dr) / (DISP_HI - DISP_LO), 0, 1) * 12.0
        w4a = "离散率 %.1f%%" % (dr * 100)
    else:
        c1, w4a = 6.0, "缺离散度"
    if trades is None:
        c2, w4b = 4.0, "缺样本数"
    else:
        c2 = _clamp(trades / 8.0, 0, 1) * 8.0
        w4b = "交易 %d 次" % int(trades)
    s4 = c1 + c2
    parts.append({"k": "置信度", "got": round(s4, 1), "max": W_CONF,
                  "why": "%s → %.1f/12，%s → %.1f/8" % (w4a, c1, w4b, c2)})

    # —— 矛盾检测：幅度吹很大、回测却不认 ——
    if pr is not None and sh is not None and abs(pr) >= 0.20 and sh < 0:
        flags.append("幅度与回测矛盾")
    if sh is not None and sh < -0.8:
        flags.append("回测为负")
    if dd is not None and dd < -0.60:
        flags.append("回撤过深")
    if trades is not None and trades < 3:
        flags.append("样本过少")
    if it.get("err"):
        flags.append("推理报错")

    # —— 显式扣分：四维加权是「加分逻辑」，压不住自相矛盾的票 ——
    # 万科A 曾拿 51 分（方向强度满 30），但它预测 +47% 而回测夏普 -1.72，
    # 这种「嘴上猛、手上亏」必须单独扣，否则用户会把 51 读成「还行」。
    penalty, why_p = 0.0, []
    if "幅度与回测矛盾" in flags:
        penalty += 10.0
        why_p.append("幅度与回测矛盾 -10")
    if "回测为负" in flags:
        penalty += 5.0
        why_p.append("回测为负 -5")
    if "回撤过深" in flags:
        penalty += 3.0
        why_p.append("回撤过深 -3")
    if "推理报错" in flags:
        penalty += 20.0
        why_p.append("推理报错 -20")
    if penalty:
        parts.append({"k": "严重扣分", "got": -round(penalty, 1), "max": 0,
                      "why": "；".join(why_p)})

    score = max(0.0, round(s1 + s2 + s3 + s4 - penalty, 1))
    g = _grade(score)
    dir_txt = "看涨" if (pr is not None and pr > 0) else ("看跌" if pr is not None else "无方向")
    bits = []
    if pr is not None:
        bits.append("预测 %+.1f%%" % (pr * 100))
    if sh is not None:
        bits.append("回测夏普 %.2f" % sh)
    if wr is not None:
        bits.append("胜率 %.0f%%" % (wr * 100))
    if dd is not None:
        bits.append("回撤 %.1f%%" % (dd * 100))
    verdict = "%s·%s（%.0f分）｜%s%s" % (
        dir_txt, g, score, "，".join(bits) if bits else "数据不足",
        ("｜⚠ " + "、".join(flags)) if flags else "")
    return {"score": score, "grade": g, "grade_note": GRADE_NOTE.get(g, ""),
            "dir": dir_txt, "parts": parts, "flags": flags, "verdict": verdict}


def _conclusion(items: list) -> dict:
    """把逐只结论汇成一句能直接用的话（进哥：「最终你得给我一个参考结论」）。"""
    if not items:
        return {"summary": "本轮没有可用结果。点「重新生成」跑一次，或放宽筛选条件。",
                "best": [], "warn": []}
    ups = [i for i in items if (i.get("pred_return") or 0) > 0]
    downs = [i for i in items if (i.get("pred_return") or 0) < 0]
    ok = [i for i in items if i["_sc"]["score"] >= 65]
    best = sorted(items, key=lambda x: -x["_sc"]["score"])[:3]
    warn = sorted([i for i in items if i["_sc"]["flags"]], key=lambda x: -x["_sc"]["score"])[:3]
    top = best[0]
    s = "本轮 %d 只：看涨 %d / 看跌 %d；评分≥65（较强以上）仅 %d 只。" % (
        len(items), len(ups), len(downs), len(ok))
    s += "最高分 %s(%s) %.0f 分·%s。" % (
        top.get("name") or "", top.get("code"), top["_sc"]["score"], top["_sc"]["grade"])
    if not ok:
        s += "⚠ 没有一只达到「较强」—— 说明这批票的模型信号整体不足以支撑决策，只能当观察名单。"
    if warn:
        w = warn[0]
        s += "另需注意 %s(%s)：%s。" % (w.get("name") or "", w.get("code"),
                                    "、".join(w["_sc"]["flags"]))

    def brief(i):
        return {"code": i.get("code"), "name": i.get("name") or "",
                "score": i["_sc"]["score"], "grade": i["_sc"]["grade"],
                "dir": i["_sc"]["dir"],
                "pred_return": i.get("pred_return"), "flags": i["_sc"]["flags"]}

    return {"summary": s, "best": [brief(i) for i in best], "warn": [brief(i) for i in warn]}


def ensure_tables(con: sqlite3.Connection) -> None:
    con.executescript(DDL)
    for sql in _MIGRATE:
        try:
            con.execute(sql)
        except sqlite3.OperationalError:
            pass  # 列已存在
    con.commit()


def run(con: sqlite3.Connection, td: str | None = None, force: bool = False, only_codes: list | None = None,
        top_n: int = TOP_N_DEFAULT, prefixes: list | None = None,
        fast: bool = False) -> dict:
    """M5 落库入口（POST /api/kronos/run）：载模型 → 对宇宙做预测+回测 → 落库。

    失败不崩主流程；返回结构化状态。算力较重（CPU 推理），前端走轮询 status。
    """
    with _LOCK:
        t0 = time.time()
        try:
            _STATUS.update(stage="加载推理引擎", detail="", at=_now(), running=True, t0=t0)
            predictor = _load_predictor()  # 缺失则抛清晰错误

            _STATUS.update(stage="载入K线底座", detail="", at=_now())
            _kc = _kline_cache()
            _bars_in = _kc.get("bars") or {}
            updated = _kc.get("updated")
            if not _bars_in:
                _STATUS.update(stage="失败", detail="本地 K 线缓存为空", at=_now(), running=False)
                return {"ok": False, "error": "本地 K 线缓存为空（请先让查看器抓取日 K）"}
            if only_codes:
                _oc = [str(c) for c in only_codes if str(c) in _bars_in
                       and len(_bars_in.get(str(c)) or []) >= MIN_BARS]
                bars_map = _materialize(_bars_in, _oc)
                codes = [c for c in _oc if c in bars_map]
                if not codes:
                    _STATUS.update(stage="失败", detail="指定代码无可用K线", at=_now(), running=False)
                    return {"ok": False,
                            "error": "指定代码无可用K线（根数不足 %d 或不在A股）" % MIN_BARS}
            else:
                # 按板块均衡挑（0/3/6 各占一份），保证前端按开头筛选时每个板块都有货
                codes = _pick_codes(_bars_in, max(1, int(top_n)), prefixes)
                if not codes:
                    _STATUS.update(stage="失败", detail="没有满足条件的候选", at=_now(), running=False)
                    return {"ok": False,
                            "error": "没有满足条件的候选（板块筛选过窄，或本地 K 线根数不足 %d）" % MIN_BARS}
                bars_map = _materialize(_bars_in, codes)
                codes = [c for c in codes if c in bars_map]
            if not codes:
                _STATUS.update(stage="失败", detail="候选物化后为空", at=_now(), running=False)
                return {"ok": False, "error": "候选股票 K 线物化失败，请检查本地日 K 缓存"}
            names = _read_names(con)
            model_ver = _model_ver()
            data_date = updated or _today()
            run_date = td or _today()

            if not force:
                row = con.execute(
                    "SELECT run_date, n_stocks AS n FROM kronos_run WHERE run_date=?",
                    (run_date,)).fetchone()
                if row:
                    _STATUS.update(stage="完成（命中缓存）", at=_now(), running=False)
                    return {"ok": True, "run_date": run_date, "cached": True,
                            "engine_ver": ENGINE_VER, "model_ver": model_ver,
                            "n_stocks": int((row["n"] if row["n"] is not None else 0))}

            if only_codes:
                _ph = ",".join("?" * max(1, len(codes)))
                con.execute("DELETE FROM kronos_item WHERE run_date=? AND code IN (%s)" % _ph,
                            [run_date] + list(codes))
            else:
                con.execute("DELETE FROM kronos_item WHERE run_date=?", (run_date,))
            done = 0
            for code in codes:
                rows = bars_map[code]
                last_real = rows[-1][4]   # 真实最新收盘（区别于预测末收）
                _STATUS.update(stage=f"推理中 {done+1}/{len(codes)}", detail=code, at=_now())
                try:
                    bt = _backtest_stock(predictor, rows,
                                         windows=(WINDOWS_FAST if fast else WINDOWS))
                    fwd = _predict_forward(predictor, rows)
                except Exception as e:  # noqa: BLE001
                    bt, fwd = {"error": str(e)[:200]}, None
                sample_json = ""
                err = None
                if isinstance(bt, dict) and bt.get("error"):
                    err = str(bt["error"])[:200]   # 推理失败必须落库可见，不许静默吞掉
                if isinstance(bt, dict) and bt.get("sample"):
                    s = bt.pop("sample")
                    sample_json = json.dumps({
                        "dates": s.get("dates"), "pred": s.get("pred"),
                        "actual": s.get("actual")}, ensure_ascii=False)
                pred_return = (fwd or {}).get("pred_return")
                pred_dir = ("up" if (pred_return or 0) >= 0 else "down")
                con.execute(
                    "INSERT OR REPLACE INTO kronos_item "
                    "(run_date,code,name,last_close,pred_close,pred_return,pred_dir,disp,"
                    "bt_total_ret,bt_annual,bt_vol,bt_sharpe,bt_maxdd,bt_winrate,bt_trades,err,sample_json) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (run_date, code, names.get(code, ""),
                     last_real,
                     (fwd or {}).get("pred_close_last"),
                     pred_return, pred_dir, (fwd or {}).get("disp"),
                     bt.get("bt_total_ret"), bt.get("bt_annual"), bt.get("bt_vol"),
                     bt.get("bt_sharpe"), bt.get("bt_maxdd"), bt.get("bt_winrate"),
                     bt.get("bt_trades"), err, sample_json))
                done += 1
                if done == 1:
                    _STATUS["t_first"] = time.time()   # ETA 基准点：跳过模型加载那段

            con.execute(
                "INSERT OR REPLACE INTO kronos_run "
                "(run_date,engine_ver,model_ver,data_date,status,n_stocks,elapsed,note,at) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (run_date, ENGINE_VER, model_ver, data_date, "done", len(codes),
                 round(time.time() - t0, 1),
                 json.dumps({"prefixes": sorted(prefixes) if prefixes else None,
                             "fast": bool(fast), "top_n": int(top_n),
                             "windows": WINDOWS_FAST if fast else WINDOWS},
                            ensure_ascii=False),
                 _now()))
            con.commit()
            _STATUS.update(stage="完成", detail="", at=_now(), running=False)
            return {"ok": True, "run_date": run_date, "engine_ver": ENGINE_VER,
                    "model_ver": model_ver, "n_stocks": len(codes),
                    "data_date": data_date, "elapsed": round(time.time() - t0, 1)}
        except Exception as e:  # noqa: BLE001
            _STATUS.update(stage="失败", detail=str(e)[:200], at=_now(), running=False)
            return {"ok": False, "error": str(e)[:300]}


# ================================================================ 异步入口
_RUNNER = {"on": False}


def run_async(con: sqlite3.Connection, td: str | None = None, force: bool = False, only_codes: list | None = None,
              top_n: int = TOP_N_DEFAULT, prefixes: list | None = None,
              fast: bool = True) -> dict:
    """后台线程跑推理，立即返回。

    为什么必须异步（2026-10-25 进哥第二次报「超时未响应」的同一个病根）：
    单只 ≈ 8s（快速）/ 28s（完整），60 只要 8 分钟以上。同步 return 会让 HTTP 请求挂 8 分钟，
    浏览器与网关都会先超时，用户看到「请求超时」，后台其实还在跑 —— 表现成「一直没结果」。
    现在：立即返回 accepted，前端轮询 /api/kronos/status 读 progress / eta_sec 显示进度。
    """
    with _LOCK:
        if _RUNNER["on"]:
            return {"ok": True, "accepted": False, "already": True,
                    "stage": _STATUS.get("stage"), "progress": _progress()}
        _RUNNER["on"] = True

    def _work():
        try:
            run(con, td=td, force=force, top_n=top_n,
                prefixes=prefixes, fast=fast, only_codes=only_codes)
        except Exception as e:  # noqa: BLE001
            _STATUS.update(stage="失败", detail=str(e)[:200], at=_now(), running=False)
        finally:
            _RUNNER["on"] = False

    threading.Thread(target=_work, daemon=True).start()
    return {"ok": True, "accepted": True, "top_n": top_n,
            "fast": bool(fast), "prefixes": prefixes,
            "hint": "已开始后台推理，可关闭页面，进度见面板。"}


# ================================================================ 只读 payload / status
def payload(con: sqlite3.Connection) -> dict:
    ensure_tables(con)
    ready = _probe_ready()
    row = con.execute("SELECT MAX(run_date) d FROM kronos_run").fetchone()
    rd = (row and row["d"]) or None
    items = []
    model_ver = _model_ver()
    data_date = None
    if rd:
        r = con.execute(
            "SELECT engine_ver,model_ver,data_date,n_stocks,elapsed,status,note "
            "FROM kronos_run WHERE run_date=?", (rd,)).fetchone()
        if r:
            model_ver = r["model_ver"] or model_ver
            data_date = r["data_date"]
        cols = ("code,name,last_close,pred_close,pred_return,pred_dir,disp,"
                "bt_total_ret,bt_annual,bt_vol,bt_sharpe,bt_maxdd,bt_winrate,bt_trades,err")
        rows = con.execute(
            f"SELECT {cols} FROM kronos_item WHERE run_date=? "
            f"ORDER BY pred_return DESC LIMIT 200", (rd,)).fetchall()
        for it in rows:
            d = dict(it)
            d["last_close"] = round(d["last_close"], 2) if d["last_close"] is not None else None
            d["pred_close"] = round(d["pred_close"], 2) if d["pred_close"] is not None else None
            for k in ("pred_return", "disp", "bt_total_ret", "bt_annual", "bt_vol",
                      "bt_sharpe", "bt_maxdd", "bt_winrate"):
                if d.get(k) is not None:
                    d[k] = round(d[k], 4)
            _sc = score_item(d)
            d["score"] = _sc["score"]
            d["grade"] = _sc["grade"]
            d["grade_note"] = _sc["grade_note"]
            d["dir_label"] = _sc["dir"]
            d["score_parts"] = _sc["parts"]
            d["flags"] = _sc["flags"]
            d["verdict"] = _sc["verdict"]
            d["prefix"] = (d.get("code") or "?")[0]
            d["_sc"] = _sc          # 内部用，返回前剔除
            items.append(d)
    # —— 板块 / 评级分布（前端筛选按钮上的数字，点了必须真有货）——
    prefix_counts = {k: 0 for k in PREFIX_KEYS}
    grade_counts = {g: 0 for _, g in GRADES}
    for it in items:
        p = it.get("prefix") or "?"
        prefix_counts[p] = prefix_counts.get(p, 0) + 1
        grade_counts[it["grade"]] = grade_counts.get(it["grade"], 0) + 1
    conclusion = _conclusion(items)
    for it in items:
        it.pop("_sc", None)

    # 本轮筛选口径（由 run 时写入 note，读侧只解析不猜测）
    try:
        filters = json.loads((r["note"] if rd and r else "") or "{}")
    except Exception:  # noqa: BLE001
        filters = {}

    caliber = [
        "口径1 · 数据底座：完全复用本地 A 股日 K 缓存（predictor.load_kcache），"
        "open/high/low/close 四列送入模型，不硬联网、不重复抓取。",
        "口径2 · 模型性质：Kronos 是开源 K 线「基础模型」的概率预测（MIT，shiyu-coder/Kronos），"
        "输出未来 OHLC 的**概率分布**，不是确定性买卖信号；本板块仅作研究展示，不构成投资建议。",
        "口径3 · 权重是冻结资产：GitHub 源码 + HuggingFace 权重，版本钉死在 model_repo.json；"
        "本机 CPU 推理，零 token、零 API 费用、可完全离线（首次需联网下载权重）。",
        "口径4 · 前瞻预测：取最新 400 根历史，预测未来 60 个交易日收盘轨迹；"
        "pred_return = 预测末收 / 最新收 - 1（方向性研究，非保证）。",
        "口径5 · 回测（示意·方向策略）：在 6 个滚动窗口上，按模型预测相对前收 >+2% 做多 / "
        "<-2% 做空 / 否则空仓，汇总总收益/年化/波动/夏普/最大回撤/胜率/交易次数。"
        "样本有限、非全历史穷举，仅用于粗略验证模型是否有信息量。",
        "口径6 · 诚实边界：小样本 + 基础模型天然有偏差；回测漂亮≠未来有效。"
        "指标仅作参考，严禁据此直接下单。",
        "口径7 · 评分（0-100）＝ 方向强度 30 + 回测有效性 25 + 风险可控 25 + 置信度 20，"
        "四维逐项可展开核对。方向强度按 ±25% 截断，防止模型吹出来的幅度把总分拉满；"
        "回测有效性看夏普与胜率，是「模型到底有没有本事」的硬证据。"
        "评分衡量的是**这个判断值不值得参考**，与涨跌方向分开看。",
        "口径8 · 股票池：按代码首位（0/3/6）均衡取样，保证每个板块都有票；"
        "快速模式只跑 1 个回测窗口（完整模式 6 个），单只耗时约为其 1/6，用于在可接受时间内扩大覆盖面。",
    ]
    return {
        "ok": True, "engine_ver": ENGINE_VER, "ready": ready,
        "model_ver": model_ver, "data_date": data_date, "run_date": rd,
        "n_stocks": len(items), "items": items, "caliber": caliber,
        "prefix_counts": prefix_counts, "grade_counts": grade_counts,
        "conclusion": conclusion, "filters": filters,
        "prefix_label": PREFIX_LABEL, "prefix_keys": list(PREFIX_KEYS),
        "note": "Kronos K 线基础模型预测板块（2026-10-24 新增）：独立顶层 tab，"
                "离线算→落库→只读。先点「重新生成」跑一次，再看预测与回测。",
    }


def _progress() -> dict:
    """从 _STATUS.stage 里的「推理中 k/n」解析进度，并按已完成部分估算剩余秒数。"""
    st = str(_STATUS.get("stage") or "")
    done = total = None
    if "/" in st:
        try:
            a, b = st.split("推理中", 1)[-1].split("/", 1)
            done, total = int(a.strip()), int(b.strip())
        except Exception:  # noqa: BLE001
            done = total = None
    eta = None
    # 用「第一只完成之后」的均速推算，否则模型加载那 100 多秒会被摊进单只耗时，
    # 实测第一版就这么把 30 只估成了 69 分钟（实际 9 分钟），比不给 ETA 更吓人。
    t1 = _STATUS.get("t_first")
    if done and total and t1 and done > 1:
        per = (time.time() - t1) / (done - 1)
        eta = int(round(per * (total - done)))
    return {"done": done, "total": total, "eta_sec": eta,
            "pct": round(done * 100.0 / total, 1) if (done and total) else None}


def status_payload(con: sqlite3.Connection) -> dict:
    ensure_tables(con)
    ready = probe_ready_nonblock()   # 2026-10-29：非阻塞（import torch 首加载 13.8s，不能拖住请求线程）
    row = con.execute(
        "SELECT run_date,model_ver,data_date,status,n_stocks,elapsed FROM kronos_run "
        "ORDER BY run_date DESC LIMIT 1").fetchone()
    last = dict(row) if row else {}
    return {
        "engine_ver": ENGINE_VER, "ready": ready,
        "model_ver": last.get("model_ver"), "data_date": last.get("data_date"),
        "last_run": last.get("run_date"), "status": last.get("status"),
        "n_stocks": last.get("n_stocks"), "elapsed": last.get("elapsed"),
        "stage": _STATUS.get("stage"), "running": bool(_STATUS.get("running")),
        "at": _STATUS.get("at"), "today": _today(),
        # 进度与预计剩余（「推理中 12/60」直接解析，别让用户对着转圈猜还要多久）
        "progress": _progress(),
    }
