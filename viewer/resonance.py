# -*- coding: utf-8 -*-
"""博主共振融合引擎（resonance-v1.0，2026-10-15 进哥需求落地）。

定位（架构红线，与 pillars.py 一致）：本模块只在**后端批量任务**里跑，
查看器只读本模块产出的表，绝不在请求线程里现场研判。全流程**零网络**（纯本地库 + K 线缓存）。

三支柱（对应「博主共振」面板三个子板块）：
  A  stock_card  个股共振 —— 一只票的多源信号叠加 + 战法命中 + 资金印证 + 完整证据链
  B  strategy    战法体系 —— 战法回测档案（含样本外验证）+ 当日命中 + 知识图谱互证
  C  source      信源画像 —— 博主 / 战法 / 龙虎榜席位 / 板块的历史战绩与信誉分（分市场环境）

P0 地基（不做这个后面全在沙子上）：
  1. res_stock         股票主数据归一（以 stock_names 5588 行为锚）
  2. sig_unified       统一信号总线（博主/战法/龙虎榜/情绪/主引擎 → 同一 schema）
  3. 统一回填口径        t1/t3/t5/t10 + **全市场等权基准**超额
  4. market_state_log   市场环境（结构/震荡/普跌）历史回填

P2/P3：
  - res_divergence     分歧预警（博主看多但资金在跑 / 战法命中但板块情绪转空 …）
  - kb_strategy_node   战法知识图谱（视频语结构化 ↔ 战法回测互证）

诚实口径（写进 payload.caliber，前端原样展示）：
  - 超额基准 = **全市场等权中位数涨幅**（decline_kcache 全市场池离线算），不是沪深300。
    理由：博主荐股以中小盘题材股为主，等权基准更贴近其真实收益来源；且离线可靠、零外部依赖。
  - 龙虎榜只作**印证项**（cross-check + 少量加分），**不进评分主干** ——
    上榜本身是结果不是原因（因果倒置），故不做预判输入。
  - 跨源强度靠**百分位归一**（源内先排名 → 映射 0~100），不做量纲硬拼。
  - 样本不足（n < MIN_N_SHOW）一律标注「样本不足」，不给信誉分、不参与排序。
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from datetime import datetime, timedelta

import predictor as P          # viewer/ 下，同 pillars.py 的依赖方式

ENGINE_VER = "resonance-v1.0"
ROOT = P.ROOT
DB_PATH = P.DB_PATH
DECLINE_KCACHE = os.path.join(ROOT, "data", "decline_kcache.json")

# ---- 口径常量（改动须附证据，不许拍脑袋）--------------------------------------
LOOKBACK_DAYS = 30          # 信号总线回看自然日
CARD_WINDOW = 20            # 个股共振卡：多久内被提到算「近期声量」
FILL_WINDOWS = (1, 3, 5, 10)
MIN_N_SHOW = 5              # 样本不足下限：低于此不给信誉分
BENCH_MODE = "equal_weight_median"
BENCH_LABEL = "全市场等权中位数"

# 共振分权重（透明可追溯；合计 1.0）
# ★ 2026-10-30 进哥拍板：博主维度**永久下线**，W_BLOGGER 归 0。
#   原值 0.34，其份额按比例并入战法/舆情/引擎三项（见 _NO_BLOGGER_FALLBACK）。
#   依据：博主战绩 15 人/898 样本，最高 |t|=1.17、有效窗口 10.4，属纯噪声；
#   且 evolve 自我进化早已把它从 34% 压到 13.6%，方向一致。
W_BLOGGER = 0.0             # 博主维度（已下线，恒 0；保留常量只为兼容旧表列 evo_weight.w_blogger）
W_STRAT = 0.5152            # 战法维度（命中数 × 战法评级权重）
W_SENTI = 0.2121            # 舆情维度
W_ENGINE = 0.2727           # 主引擎交叉
# ★ dy8（2026-10-21）博主分档阈值：卡片口径 bw = Σstrength（单条 0.2~100）。
# 训练集口径 bw = Σstrength/100，故训练集 1.5 ⇔ 卡片 150（实证：强提及仅 21/369 票）。
BLOGGER_STRONG_BW = 150.0   # ≥ 此值为"强提及"（多位/高信誉）；以下为"弱提及"（负面标签）
TURN_CHASE_RATIO = 2.5      # 量比 > 2.5 视为放量追高
TURN_CHASE_PENALTY_MAX = 15.0  # 追高最多扣 15 分（非清零，保留强信号票）
# ★ dy8c（2026-10-21）：龙虎榜上榜本身是**负面**标签，不分方向都扣分。
# 实证（40747 行面板）：正向 -1.591%(t=-3.92)、负向 -1.860%(t=-2.43)，都显著负；
# 原 +5「印证加成」是反向操作，改为 -5。
LHB_BONUS = -5.0            # 龙虎榜上榜（多方向）扣分
LHB_PENALTY = -6.0          # 负面印证（换手/振幅上榜等实证为负的形态）

# 战法关键词 → strategy_key 映射（知识图谱 ↔ 战法回测互证的唯一映射表）
KW_TO_STRAT = [
    (("macd", "MACD", "金叉", "死叉", "背离"), "macd_gold_any"),
    (("低位金叉", "超跌金叉"), "macd_gold_low"),
    (("底背离", "背离"), "macd_divergence_bull"),
    (("均线", "回踩", "MA20", "支撑"), "pullback_ma20"),
    (("多头排列", "均线多头"), "ma_bull_align"),
    (("金叉", "均线交叉", "短期金叉"), "ma_cross_5_20"),
    (("双均线", "趋势跟踪", "长期趋势"), "ma_cross_20_60"),
    (("放量", "量价", "量能", "倍量"), "volume_breakout"),
    (("新高", "突破", "海龟"), "break_20d_high"),
    (("60日新高", "长期新高"), "break_60d_high"),
    (("RSI", "超卖", "超跌反弹"), "rsi_reversal"),
    (("KDJ", "超卖金叉"), "kdj_low_gold"),
    (("布林", "下轨", "BOLL"), "boll_lower"),
    (("均值回归", "偏离", "乖离"), "mean_revert_ma20"),
    (("动量", "强势", "涨幅榜"), "momentum_20"),
    (("择时", "市场宽度", "大盘", "仓位"), "breadth_timing"),
    (("波动率", "风险控制", "止损"), "hist_vol_filter"),
    (("指数", "均线择时", "大盘趋势"), "index_ma20_timing"),
]

# 择时 / 市场级战法：**不是选股信号**，不进「个股共振」的战法命中计数。
# 实证依据：hist_vol_filter 在 919 只候选里命中 758 只（82%）——纯择时类，计入即污染共振分
# （这正是「战法命中率 85% 等于没筛选」那条坑的具体表现）。
TIMING_STRATS = {"breadth_timing", "index_ma20_timing", "hist_vol_filter",
                 "vol_regime_timing", "ma_regime_timing", "market_regime",
                 "index_ma20_below", "breadth_filter"}

DDL = """
-- 1. 股票主数据归一
CREATE TABLE IF NOT EXISTS res_stock (
  code       TEXT PRIMARY KEY,
  name       TEXT,
  company    TEXT,
  market     TEXT,
  src        TEXT,
  updated_at TEXT
);
-- 2. 统一信号总线
CREATE TABLE IF NOT EXISTS sig_unified (
  sid        TEXT PRIMARY KEY,   -- src_type|src_key|code|ts
  ts         TEXT NOT NULL,      -- 信号日 YYYY-MM-DD
  src_type   TEXT NOT NULL,      -- blogger|strategy|lhb|sentiment|engine|bili
  src_key    TEXT NOT NULL,      -- 博主名 / 战法key / 席位档 / 引擎名
  code       TEXT NOT NULL,
  name       TEXT,
  direction  TEXT,               -- 多 | 空 | 中性
  strength   REAL,               -- 0~100（源内百分位归一后）
  raw_score  REAL,               -- 归一前原始分（可追溯）
  horizon    TEXT,               -- T+1 | T+5 | mid
  evidence   TEXT,               -- 原话 / 链接 / 理由（证据链）
  ret_t1 REAL, ret_t3 REAL, ret_t5 REAL, ret_t10 REAL,
  bench_t1 REAL, bench_t5 REAL,
  ex_t1 REAL, ex_t5 REAL,
  fill_status TEXT,              -- ok | pending
  mkt_state  TEXT,               -- 信号当日市场环境
  built_at   TEXT
);
CREATE INDEX IF NOT EXISTS idx_sig_uni_ts   ON sig_unified(ts);
CREATE INDEX IF NOT EXISTS idx_sig_uni_code ON sig_unified(code);
CREATE INDEX IF NOT EXISTS idx_sig_uni_src  ON sig_unified(src_type, src_key);

-- 3. 信号源画像（信源信誉分）
CREATE TABLE IF NOT EXISTS res_source (
  src_type    TEXT NOT NULL,
  src_key     TEXT NOT NULL,
  label       TEXT,
  n_sig       INTEGER DEFAULT 0,
  n_filled    INTEGER DEFAULT 0,
  win_t1      REAL,
  win_t5      REAL,
  avg_ret_t1  REAL,
  avg_ex_t1   REAL,
  avg_ex_t5   REAL,
  median_ex_t5 REAL,
  best_code   TEXT, best_ex REAL,
  worst_code  TEXT, worst_ex REAL,
  by_state_json TEXT,            -- 按市场环境分档 {结构:{...},震荡:{...},普跌:{...}}
  pick_skill  REAL,              -- 选股力（平均超额）
  timing_skill REAL,             -- 择时力（胜率-基准胜率）
  score       REAL,              -- 信誉分 0~100
  tier        TEXT,              -- A/B/C/D
  note        TEXT,
  updated_at  TEXT,
  PRIMARY KEY (src_type, src_key)
);

-- 4. 个股共振卡（支柱 A）
CREATE TABLE IF NOT EXISTS res_stock_card (
  build_date  TEXT NOT NULL,
  code        TEXT NOT NULL,
  name        TEXT,
  res_score   REAL,
  tier        TEXT,
  n_blogger   INTEGER DEFAULT 0,
  blogger_w   REAL,              -- 信誉加权声量
  bloggers    TEXT,              -- 博主名单（证据）
  n_strat     INTEGER DEFAULT 0,
  strat_keys  TEXT,
  strat_families TEXT,
  strat_weight REAL,
  lhb_confirm TEXT,              -- 印证结论（非预判）
  lhb_bonus   REAL,
  senti_net   REAL,              -- 舆情净多空
  senti_n     INTEGER DEFAULT 0,
  sector      TEXT,
  sector_flow REAL,
  engine_cross TEXT,             -- 与主引擎/四支柱交叉
  is_watch    INTEGER DEFAULT 0,
  evidence_json TEXT,
  verdict     TEXT,
  PRIMARY KEY (build_date, code)
);
CREATE INDEX IF NOT EXISTS idx_card_score ON res_stock_card(build_date, res_score DESC);

-- 5. 战法档案（支柱 B）
CREATE TABLE IF NOT EXISTS res_strategy (
  strategy_key TEXT NOT NULL,
  window_label TEXT NOT NULL,
  name        TEXT,
  school      TEXT,
  annual_return REAL,
  total_return REAL,
  win_rate    REAL,
  max_dd      REAL,
  beat_bench  REAL,
  trades      INTEGER,
  score       REAL,
  rank        INTEGER,
  rating      TEXT,
  oos_return  REAL,
  oos_beat_bench REAL,
  oos_validated INTEGER,
  hit_n       INTEGER DEFAULT 0,
  hit_codes   TEXT,
  kb_n        INTEGER DEFAULT 0,  -- 知识图谱互证条目数
  updated_at  TEXT,
  PRIMARY KEY (strategy_key, window_label)
);

-- 6. 战法知识图谱（视频语结构化 ↔ 回测互证）
CREATE TABLE IF NOT EXISTS kb_strategy_node (
  nid        TEXT PRIMARY KEY,
  doc_key    TEXT,
  track      TEXT,
  title      TEXT,
  body       TEXT,
  strat_key  TEXT,
  consistency TEXT,              -- 一致 | 存疑 | 无回测
  verdict    TEXT,
  tags       TEXT,
  updated_at TEXT
);

-- 7. 分歧预警
CREATE TABLE IF NOT EXISTS res_divergence (
  build_date TEXT NOT NULL,
  code       TEXT NOT NULL,
  name       TEXT,
  kind       TEXT NOT NULL,
  severity   INTEGER,
  detail     TEXT,
  PRIMARY KEY (build_date, code, kind)
);
CREATE INDEX IF NOT EXISTS idx_div_sev ON res_divergence(build_date, severity DESC);
"""

_LOCK = threading.Lock()
_STATUS: dict = {"stage": "", "detail": "", "at": ""}


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def ensure_tables(con: sqlite3.Connection) -> None:
    con.executescript(DDL)
    con.commit()


# ================================================================ K 线底座
def load_bars() -> dict:
    """合并两套本地 K 线缓存 → {code: [(date, close, high, low, vol), ...]}（升序）。

    decline_kcache.json（全市场 5224 只，格式 [date, close, high, low, vol]）
    predict_kcache.json（候选池 201 只，格式 {d,o,h,l,c,v}，历史更长）
    合并策略：两处都有则取**更长**的一份；单侧有则用单侧。
    """
    # 修复 P0-02：两套缓存复权口径不同 —— decline_kcache 是腾讯前复权(qfq)，predict_kcache 是
    # 新浪日线不复权(raw，实测新浪忽略 adj 参数，见 P0-01)。跨 adj 混拼会让基准中位数与个股
    # 收益率不在同一尺度上。故**拒绝跨 adj 混拼**：以 canonical=raw(predict) 为唯一口径，同一 code
    # 若两套都有则只保留 canonical 并告警计数；仅当 canonical 缺该 code 时才退回另一套。
    CANONICAL_ADJ = "raw"          # predict_kcache：新浪日线不复权（P0-01 实测口径）
    decline_adj = "qfq"            # decline_kcache：腾讯前复权
    predict_adj = CANONICAL_ADJ
    cross_adj: dict = {}

    def _parse_decline(ent):
        bb = (ent or {}).get("bars") or []
        rows = []
        for b in bb:
            try:
                rows.append((str(b[0])[:10], float(b[1]),
                             _f(b[2]), _f(b[3]), _f(b[4])))
            except Exception:  # noqa: BLE001
                continue
        if rows:
            rows.sort(key=lambda x: x[0])
        return rows

    def _parse_predict(v):
        rows = []
        for b in v:
            try:
                rows.append((str(b.get("d"))[:10], float(b.get("c")),
                             _f(b.get("h")), _f(b.get("l")), _f(b.get("v"))))
            except Exception:  # noqa: BLE001
                continue
        if rows:
            rows.sort(key=lambda x: x[0])
        return rows

    out: dict = {}                 # code -> (rows, adj)
    dk_path = DECLINE_KCACHE
    if os.path.exists(dk_path):
        try:
            with open(dk_path, "r", encoding="utf-8") as f:
                dk = json.load(f)
            for code, ent in dk.items():
                rows = _parse_decline(ent)
                if rows:
                    out[str(code)] = (rows, decline_adj)
        except Exception:  # noqa: BLE001
            pass
    pk_path = os.path.join(ROOT, "predict_kcache.json")
    if os.path.exists(pk_path):
        try:
            with open(pk_path, "r", encoding="utf-8") as f:
                pk = json.load(f)
            for code, v in (pk.get("bars") or {}).items():
                rows = _parse_predict(v)
                if not rows:
                    continue
                cur = out.get(str(code))
                if cur is None:
                    out[str(code)] = (rows, predict_adj)
                elif cur[1] == predict_adj:
                    # 同 adj（canonical）内：取更长的一份
                    if len(rows) > len(cur[0]):
                        out[str(code)] = (rows, predict_adj)
                else:
                    # 跨 adj 混拼：拒绝 qfq，强制用 canonical(raw) 口径覆盖并告警
                    cross_adj[str(code)] = True
                    out[str(code)] = (rows, predict_adj)
        except Exception:  # noqa: BLE001
            pass
    if cross_adj:
        # 2026-10-29：原文用全角箭头 "↔"，该字符不在 Windows GBK 码页内，
        # 子进程（sched_job，stdout 指向日志文件、默认 GBK）执行到这行就抛
        # UnicodeEncodeError: 'gbk' codec can't encode character '\u2194'，
        # 把 resonance_build / selfpick_build 整轮打断（连带四支柱停更两天）。
        # 改 ASCII "<->"：只改展示字符，不动任何口径。
        print("[resonance] 警告(P0-02)：%d 只票存在跨 adj 混拼（腾讯qfq <-> 新浪不复权），"
              "已统一保留 canonical(%s) 口径" % (len(cross_adj), CANONICAL_ADJ), flush=True)
    return {c: r[0] for c, r in out.items()}


def _f(x):
    try:
        return float(x)
    except Exception:  # noqa: BLE001
        return None


def build_indices(bars: dict) -> tuple:
    """→ (idx_by_date{code:{date:i}}, bench{date: 全市场中位涨幅%})"""
    idx: dict = {}
    by_date: dict = {}
    for code, rows in bars.items():
        m = {}
        prev = None
        for i, (d, c, _h, _l, _v) in enumerate(rows):
            m[d] = i
            if prev and c and prev > 0:
                by_date.setdefault(d, []).append((c / prev - 1.0) * 100.0)
            prev = c
        idx[code] = m
    bench = {}
    for d, arr in by_date.items():
        arr.sort()
        bench[d] = round(arr[len(arr) // 2], 4) if arr else None
    return idx, bench


def state_history(bench: dict) -> dict:
    """市场环境三态历史（离线口径：全市场等权中位涨幅 + 等权净值 20 日线）。

    普跌 = 当日全市场中位涨幅 ≤ -1.2%
    震荡 = 中位涨幅 < -0.45% 或 等权净值低于 20 日线
    结构 = 其余（资金集中于主线方向）
    """
    dates = sorted([d for d, v in bench.items() if v is not None])
    nav = 1.0
    nav_hist = []
    for d in dates:
        nav *= (1 + bench[d] / 100.0)
        nav_hist.append((d, nav))
    out = {}
    for i, (d, _n) in enumerate(nav_hist):
        pcts = []
        # 用基准自身的中位数无法算 up_ratio，故回退为「中位涨幅 + 净值位置」双条件
        m = bench[d]
        below20 = False
        if i >= 20:
            ma20 = sum(x[1] for x in nav_hist[i - 20:i]) / 20.0
            below20 = nav_hist[i][1] < ma20
        if m is None:
            st = "未知"
        elif m <= -1.2:
            st = "普跌"
        elif m < -0.45 or below20:
            st = "震荡"
        else:
            st = "结构"
        out[d] = st
    return out


def fill_state_log(con: sqlite3.Connection, states: dict) -> int:
    """市场环境历史回填（只补缺失的交易日，不覆盖已有）。"""
    exist = {r["trade_date"] for r in con.execute("SELECT trade_date FROM market_state_log")}
    n = 0
    for d, st in states.items():
        if d in exist:
            continue
        con.execute("INSERT OR IGNORE INTO market_state_log(trade_date,state,breadth_json,note,created_at)"
                    " VALUES(?,?,?,?,?)",
                    (d, st, json.dumps({"bench_median_pct": None}, ensure_ascii=False),
                     "共振引擎回填：普跌=中位涨幅≤-1.2%；震荡=中位涨幅<-0.45% 或 等权净值低于20日线；结构=其余",
                     _now()))
        n += 1
    con.commit()
    return n


# ================================================================ 主数据归一
def sync_stock_master(con: sqlite3.Connection) -> int:
    """把 stock_names 全量镜像进 res_stock（主数据锚点）。

    注意（2026-10-15 全站回测评测）：stock_names 里含极少数**非标准 A 股**标的
    （可转债如 113710 四方转债、B 股等，market 记为「未知板块」，实测 3 只）。
    这里**照实全量保留**、不删主数据（名称查询等仍可能用到）；
    下游「个股共振」由 build() 里的代码规则闸门（_A_SHARE_SQL）把它们挡在外面，
    不在融合层制造污染。"""
    con.execute("DELETE FROM res_stock")
    n = 0
    for r in con.execute("SELECT code,name,company,market FROM stock_names"):
        con.execute("INSERT OR REPLACE INTO res_stock(code,name,company,market,src,updated_at)"
                    " VALUES(?,?,?,?,?,?)",
                    (r["code"], r["name"], r["company"] or "", r["market"] or "",
                     "stock_names", _now()))
        n += 1
    con.commit()
    return n


# ================================================================ 信号抽取（六路）
def _since(days: int) -> str:
    return (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")


def _latest_trade_date() -> str:
    """本地 K 线里最新的交易日（信号 ts 的兜底锚点）。

    为什么需要：情绪这类「当日快照」信号若用自然日（今天）当 ts，
    而 K 线最后一根是上一交易日 → 索引里找不到 → 回填必然 pending。
    """
    d = ""
    for _c, rows in _BARS.items():
        if rows and rows[-1][0] > d:
            d = rows[-1][0]
    return d or _today()


def _blogger_credibility(con: sqlite3.Connection) -> dict:
    """博主信誉系数（0.5~1.5）。

    2026-10-29 重写。旧版直接用 `AVG(blogger_perf.r5)` 定档，有两个致命问题：
      1) **没减基线**。样本期 2026-08~10 全市场是弱势（同期 T+5 基线为负），
         所有博主的绝对 r5 一起变负，公式 1.0 + a5*0.03 + (win5-0.5)*1.5 把
         全员压到 0.5~0.6 —— 把「大盘跌」错算成「博主无能」。
      2) **没控独立样本**。同一博主单日最多推 56 只，同日所有票共享市场因子，
         把「博主×股票」当独立样本，n 从几十被吹到上千，小样本博主也会被定档。
    现在改读 `blogger_score`（viewer/blogger_score.py 算）：日度超额、向 0 收缩、
    样本量门槛、前后半段检验都在那里完成，这里只取 cred。
    D 级（独立观测不足）一律中性 1.0 —— 样本不足不等于差，不该被惩罚性降权。
    """
    stats = {}
    try:
        rows = con.execute(
            "SELECT blogger, cred, grade, edge_shrunk, n_days, t_stat, stab,"
            " med_per_day FROM blogger_score").fetchall()
    except sqlite3.Error:
        rows = []
    if rows:
        for r in rows:
            stats[r["blogger"]] = {
                "cred": float(r["cred"] or 1.0),
                "note": "" if r["grade"] != "D" else "独立观测不足(%s 天)" % r["n_days"],
                "a5": r["edge_shrunk"], "win5": None, "n": r["n_days"],
                "grade": r["grade"], "t": r["t_stat"], "stab": r["stab"],
                "per_day": r["med_per_day"],
            }
        return stats
    # 兜底：blogger_score 尚未计算过时退回旧口径（仅保证不炸，数值不作结论）
    rows = con.execute("SELECT blogger, AVG(r5) a5, AVG(r1) a1,"
                       " SUM(CASE WHEN r5>0 THEN 1 ELSE 0 END) w5,"
                       " SUM(CASE WHEN r5 IS NOT NULL THEN 1 ELSE 0 END) n5,"
                       " COUNT(*) n FROM blogger_perf GROUP BY blogger").fetchall()
    for r in rows:
        n5 = r["n5"] or 0
        if n5 < MIN_N_SHOW:
            stats[r["blogger"]] = {"cred": 1.0, "note": "样本不足(n=%d)" % n5,
                                   "a5": None, "win5": None, "n": r["n"]}
            continue
        a5 = r["a5"] or 0.0
        win5 = (r["w5"] or 0) / float(n5) if n5 else 0.0
        # 系数：以 r5=0 为 1.0，每 +10% 升 0.3；胜率每 ±10% 再调 0.15
        cred = 1.0 + (a5 / 100.0) * 3.0 + (win5 - 0.5) * 1.5
        cred = max(0.5, min(1.5, cred))
        stats[r["blogger"]] = {"cred": round(cred, 3), "note": "",
                               "a5": round(a5, 2), "win5": round(win5, 3), "n": r["n"]}
    return stats


def _pull_blogger(con: sqlite3.Connection, cred: dict) -> list:
    """博主荐股 → 信号 —— **2026-10-30 起永久停用，恒返回空表**。

    进哥 2026-10-29/30 两次拍板的合并结论：
      · 「咱们从头到尾一直测的这些博主全都是负面信息，短期中期长期都是」；
      · 「荐股类的博主以后不关注了，不需要这些信息，是噪声」；
      · 「情绪有很多地方都可以搜集得到，之前那些博主的舆论已经够用了」。

    实证支持：博主战绩（blogger_score）15 人 / 898 样本，最高 |t|=1.17、
    有效独立窗口仅 10.4 个 —— 连"略微有效"都够不上，是纯噪声。

    为什么**保留函数体与调用点**而不是连函数一起删：
      ① 下游 `build_exclusion` 仍用历史 blogger_overlap 做「博主推过 + 历史负收益 → 剔除」，
         那是**负面证据**，与进哥判断同向，删了反而丢风控依据；
      ② 留一个恒空的桩，比删掉更不容易被后人误加回来（此处有明确的口径注释挡着）。
    想恢复只需把下面 3 行换成原来的 SQL 循环。
    """
    _ = (con, cred)      # 显式声明不用，避免 lint 误判
    return []


def _pull_engine(con: sqlite3.Connection) -> list:
    """主引擎预测 + 四支柱 → 信号（作为交叉验证的另一路）。"""
    out = []
    for r in con.execute("SELECT run_date, symbol, name, tier, score, reason FROM prediction_item"
                         " WHERE run_date>=?", (_since(30),)):
        out.append({"ts": r["run_date"], "src_type": "engine", "src_key": "每日预测",
                    "code": r["symbol"], "name": r["name"], "direction": "多",
                    "raw_score": 60.0, "horizon": "T+5",
                    "evidence": "主引擎%s｜%s" % (r["tier"] or "", (r["reason"] or "")[:140])})
    for r in con.execute("SELECT run_date, symbol, name, pillar, score, reason FROM pillar_item"
                         " WHERE run_date>=?", (_since(30),)):
        out.append({"ts": r["run_date"], "src_type": "engine", "src_key": "四支柱·" + (r["pillar"] or ""),
                    "code": r["symbol"], "name": r["name"], "direction": "多",
                    "raw_score": 60.0, "horizon": "T+5",
                    "evidence": "%s｜%s" % (r["pillar"], (r["reason"] or "")[:140])})
    return out


def _pull_lhb(con: sqlite3.Connection) -> list:
    """龙虎榜 → 信号（**仅作印证**：direction 按原因档位定，负档标为空）。"""
    out = []
    for r in con.execute("SELECT trade_date, security_code, security_name, change_rate, reason"
                         " FROM lhb_stocks WHERE trade_date>=?", (_since(LOOKBACK_DAYS),)):
        rs = r["reason"] or ""
        d, sc = "中性", 50.0
        for k, v in P.LHB_CALIB["reason"].items():
            if any(kw in rs for kw in v["kw"]):
                sc = 50.0 + v["f"] * 25.0
                d = "多" if v["f"] > 0 else ("空" if v["f"] < 0 else "中性")
                break
        out.append({"ts": r["trade_date"], "src_type": "lhb", "src_key": "龙虎榜·" + rs[:12],
                    "code": r["security_code"], "name": r["security_name"],
                    "direction": d, "raw_score": sc, "horizon": "T+3",
                    "evidence": "上榜原因：%s（T+1 实证档位系数 %.2f）" % (rs, sc / 50.0 - 1)})
    return out


def _pull_sentiment(con: sqlite3.Connection, ts: str | None = None) -> list:
    """论坛情绪 → 信号（近 10 日：票的提及热度 × 多空倾向）。

    ts 用**最新交易日**而非自然日（自然日在 K 线里没有收盘价，回填必然 pending）。
    """
    ts = ts or _latest_trade_date()
    since = _since(10)
    rows = con.execute(
        "SELECT m.symbol, m.symbol_name, p.sentiment, p.sentiment_score "
        "FROM stock_mentions m JOIN posts p ON p.id=m.post_id_ref "
        "WHERE p.post_time>=? AND m.symbol<>''", (since,)).fetchall()
    agg: dict = {}
    for r in rows:
        a = agg.setdefault(r["symbol"], {"name": r["symbol_name"], "n": 0, "s": 0.0,
                                         "bull": 0, "bear": 0})
        a["n"] += 1
        try:
            a["s"] += float(r["sentiment_score"] or 0)
        except Exception:  # noqa: BLE001
            pass
        if r["sentiment"] == "看多":
            a["bull"] += 1
        elif r["sentiment"] == "看空":
            a["bear"] += 1
    out = []
    for code, a in agg.items():
        if a["n"] < 3:
            continue
        net = a["bull"] - a["bear"]
        raw = 50.0 + (net / max(1, a["n"])) * 50.0
        out.append({"ts": ts, "src_type": "sentiment", "src_key": "论坛情绪",
                    "code": code, "name": a["name"],
                    "direction": "多" if net > 0 else ("空" if net < 0 else "中性"),
                    "raw_score": max(0.0, min(100.0, raw)), "horizon": "T+1",
                    "evidence": "近10日被提及 %d 次，看多 %d / 看空 %d" % (a["n"], a["bull"], a["bear"])})
    return out


def _load_sig_bars(codes: set) -> dict:
    """给 strategy_sim 用的**标准 bars 格式** [{d,o,h,l,c,v}]。

    ⚠️ 第一版实测踩的坑：直接把内部 tuple 格式 (d,c,h,l,v) 喂给 SS.build_signals，
    每个战法都抛异常被吞 → 战法一路**全军覆没（命中 0）且不报错**。
    故此处做格式适配，并优先用 predict_kcache（含 open 真值）。

    open 来源：predict_kcache 有真 open；decline_kcache 只有 (close,high,low,vol)，
    缺失的 open 用**前一根收盘价**近似（日线级别可接受，口径已写入 caliber）。
    """
    pk = {}
    pk_path = os.path.join(ROOT, "predict_kcache.json")
    if os.path.exists(pk_path):
        try:
            with open(pk_path, "r", encoding="utf-8") as f:
                pk = (json.load(f).get("bars") or {})
        except Exception:  # noqa: BLE001
            pk = {}
    out: dict = {}
    for c in codes:
        v = pk.get(c)
        if v and len(v) >= P.MIN_BARS:
            rows = []
            for b in v:
                try:
                    rows.append({"d": str(b.get("d"))[:10], "o": b.get("o"), "h": b.get("h"),
                                 "l": b.get("l"), "c": b.get("c"), "v": b.get("v")})
                except Exception:  # noqa: BLE001
                    pass
            if len(rows) >= P.MIN_BARS:
                out[c] = rows
                continue
        rws = _BARS.get(c) or []
        if len(rws) >= P.MIN_BARS:
            rows, prev = [], None
            for d, cc, h, l, vv in rws:
                rows.append({"d": d, "o": prev if prev is not None else cc,
                             "h": h if h is not None else cc,
                             "l": l if l is not None else cc,
                             "c": cc, "v": vv if vv is not None else 0})
                prev = cc
            out[c] = rows
    return out


def _pull_strategy(con: sqlite3.Connection, bars: dict, codes: set) -> tuple:
    """战法命中 → 信号（只对候选票算，控制耗时）+ 返回 {code: [strategy_key,...]}。"""
    sub = _load_sig_bars(codes)
    if not sub:
        return [], {}
    try:
        strat = P._load_strategies()
    except Exception:  # noqa: BLE001
        strat = []
    if not strat:
        return [], {}
    try:
        hits = P.sig_tech(sub, strat)
    except Exception:  # noqa: BLE001
        hits = {}
    by_code: dict = {}
    out = []
    last_d = ""
    for c in sub:
        rows = sub[c]
        if rows:
            last_d = max(last_d, rows[-1].get("d") or "")
    for code, h in hits.items():
        keys = h.get("strategies") or []
        by_code[code] = keys
        out.append({"ts": last_d or _today(), "src_type": "strategy", "src_key": "战法",
                    "code": code, "name": "",
                    "direction": "多", "raw_score": 40.0 + len(keys) * 8.0,
                    "horizon": "T+5",
                    "evidence": "命中 %d 个战法：%s（%s）" % (
                        len(keys), "、".join(keys[:8]), "、".join(h.get("schools") or []))})
    return out, by_code


def normalize_strength(sigs: list) -> None:
    """**源内百分位归一**：每个 (src_type) 内部按 raw_score 排名 → 映射 0~100。"""
    groups: dict = {}
    for s in sigs:
        groups.setdefault(s["src_type"], []).append(s)
    for _t, arr in groups.items():
        arr.sort(key=lambda x: x["raw_score"])
        n = len(arr)
        for i, s in enumerate(arr):
            s["strength"] = round(100.0 * (i + 1) / n, 1) if n > 1 else 60.0


# ================================================================ 统一回填
def fill_returns(sigs: list, idx: dict, bench: dict) -> None:
    """统一回填口径：t1/t3/t5/t10 + 全市场等权基准超额。"""
    for s in sigs:
        code, ts = s["code"], s["ts"]
        m = idx.get(code)
        if not m or ts not in m:
            s.update({"fill_status": "pending", "ret_t1": None, "ret_t3": None,
                      "ret_t5": None, "ret_t10": None, "bench_t1": None,
                      "bench_t5": None, "ex_t1": None, "ex_t5": None})
            continue
        i0 = m[ts]
        codes_rows = _BARS.get(code) or []
        if not codes_rows:
            s["fill_status"] = "pending"
            continue
        base = codes_rows[i0][1] if i0 < len(codes_rows) else None
        if not base:
            s["fill_status"] = "pending"
            continue
        for w in FILL_WINDOWS:
            j = i0 + w
            s["ret_t%d" % w] = (round((codes_rows[j][1] / base - 1.0) * 100.0, 2)
                                if j < len(codes_rows) and codes_rows[j][1] else None)
        # 基准：信号日之后 w 个交易日的累计中位涨幅（用 date 序列近似为逐日累乘）
        dseq = [r[0] for r in codes_rows]
        for w, key in ((1, "bench_t1"), (5, "bench_t5")):
            j = i0 + w
            if j < len(dseq):
                acc = 0.0
                for k in range(i0 + 1, j + 1):
                    bm = bench.get(dseq[k])
                    if bm is None:
                        acc = None
                        break
                    acc += bm
                s[key] = round(acc, 2) if acc is not None else None
            else:
                s[key] = None
        s["ex_t1"] = (round(s["ret_t1"] - s["bench_t1"], 2)
                      if s.get("ret_t1") is not None and s.get("bench_t1") is not None else None)
        s["ex_t5"] = (round(s["ret_t5"] - s["bench_t5"], 2)
                      if s.get("ret_t5") is not None and s.get("bench_t5") is not None else None)
        s["fill_status"] = ("ok" if (s.get("ret_t1") is not None or s.get("ret_t5") is not None)
                            else "pending")


_BARS: dict = {}


def _set_bars(bars: dict) -> None:
    global _BARS
    _BARS = bars


def _drop_blogger(w: dict) -> dict:
    """把博主维度的权重清零，其份额按比例还给其余三项（合计回到 1.0）。

    2026-10-30 进哥拍板博主维度永久下线。但 `evo_weight` 表里历史学出的行
    仍带 w_blogger=0.136 —— 若直接拿来用，三项之和只有 0.864，分数被整体压低 13.6%，
    且页面会显示「博主 13.6%」，等于删了又没删干净。
    故在**读权重这一处**统一收口：无论来自 evolve 还是默认，博主一律归 0 并重归一化。
    """
    try:
        bw = float(w.get("blogger") or 0)
    except (TypeError, ValueError):
        bw = 0.0
    rest = {k: max(0.0, float(w.get(k) or 0.0)) for k in ("strategy", "senti", "engine")}
    tot = sum(rest.values())
    if bw > 0 and tot > 0:
        for k in rest:
            rest[k] = round(rest[k] / tot, 4)
        w.update(rest)
        w["note"] = (w.get("note") or "") + "｜博主维度 2026-10-30 下线，其 %.1f%% 份额按比例并入其余三项" % (bw * 100)
    w["blogger"] = 0.0
    w["blogger_off"] = True
    return w


def _load_weights(con: sqlite3.Connection) -> dict:
    """共振分权重：**优先用 evolve 引擎学出的权重**（数据说话），没有才用默认。

    这是「自我提升」的反哺入口 —— 学出的权重必须真的改变产出，否则学习就是花架子。
    表不存在（evolve 还没跑过）或读失败时静默退回默认权重，绝不让主流程挂掉。
    """
    try:
        # 只认「不晚于最新交易日」的权重行（双保险）：早期版本 fit_date 曾漂到自然日，
        # 按倒序取最新会读到未校准的旧权重 —— 2026-10-15 深度审计发现。
        r = con.execute("SELECT * FROM evo_weight WHERE fit_date <= ? "
                        "ORDER BY fit_date DESC LIMIT 1", (_latest_trade_date(),)).fetchone()
        if r and r["w_blogger"] is not None:
            return _drop_blogger({"blogger": r["w_blogger"], "strategy": r["w_strategy"],
                                  "senti": r["w_senti"], "engine": r["w_engine"],
                                  "ic": (json.loads(r["ic_json"]) if r["ic_json"] else {}),
                                  "src": "learned", "fit_date": r["fit_date"], "note": r["note"] or ""})
    except Exception:  # noqa: BLE001
        pass
    return _drop_blogger({"blogger": W_BLOGGER, "strategy": W_STRAT, "senti": W_SENTI,
                          "engine": W_ENGINE, "src": "default", "fit_date": "", "note": ""})


# ================================================================ 画像 / 卡片 / 分歧
def build_source_profile(con: sqlite3.Connection, states: dict) -> dict:
    """信源画像：按 (src_type, src_key) 聚合胜率 / 超额 / 分市场环境 → 信誉分。"""
    con.execute("DELETE FROM res_source")
    rows = con.execute("SELECT * FROM sig_unified").fetchall()
    groups: dict = {}
    for r in rows:
        groups.setdefault((r["src_type"], r["src_key"]), []).append(r)
    label_map = {"blogger": "荐股博主", "strategy": "战法", "lhb": "龙虎榜",
                 "sentiment": "论坛情绪", "engine": "主引擎", "bili": "B站"}
    n_out = 0
    for (st, sk), arr in groups.items():
        if len(arr) < 2:
            continue          # 单条信号不构成可评估样本，不入榜（防「1 条=100%胜率」噪声）
        if st == "strategy" and sk == "战法":
            continue          # 战法聚合信号不单独评分：下面按 strategy_key 逐条出回测画像
        fill = [x for x in arr if x["fill_status"] == "ok" and x["ret_t5"] is not None]
        f1 = [x for x in arr if x["fill_status"] == "ok" and x["ret_t1"] is not None]
        n_sig, n_filled = len(arr), len(fill)
        if n_filled >= MIN_N_SHOW:
            ex5 = [x["ex_t5"] for x in fill if x["ex_t5"] is not None]
            ex1 = [x["ex_t1"] for x in f1 if x["ex_t1"] is not None]
            avg_ex5 = round(sum(ex5) / len(ex5), 2) if ex5 else None
            avg_ex1 = round(sum(ex1) / len(ex1), 2) if ex1 else None
            win5 = round(sum(1 for x in fill if (x["ret_t5"] or 0) > 0) / float(n_filled), 3)
            win1 = (round(sum(1 for x in f1 if (x["ret_t1"] or 0) > 0) / float(len(f1)), 3)
                    if f1 else None)
            avg_ret1 = round(sum(x["ret_t1"] for x in f1) / len(f1), 2) if f1 else None
            med = sorted(ex5)[len(ex5) // 2] if ex5 else None
            best = max(fill, key=lambda x: (x["ex_t5"] if x["ex_t5"] is not None else -999))
            worst = min(fill, key=lambda x: (x["ex_t5"] if x["ex_t5"] is not None else 999))
            # 分市场环境
            bs: dict = {}
            for x in fill:
                k = x["mkt_state"] or "未知"
                b = bs.setdefault(k, {"n": 0, "ex": 0.0, "win": 0})
                if x["ex_t5"] is not None:
                    b["n"] += 1
                    b["ex"] += x["ex_t5"]
                    b["win"] += 1 if (x["ret_t5"] or 0) > 0 else 0
            by_state = {k: {"n": v["n"],
                            "avg_ex": round(v["ex"] / v["n"], 2) if v["n"] else None,
                            "win": round(v["win"] / v["n"], 3) if v["n"] else None}
                        for k, v in bs.items()}
            pick = avg_ex5
            timing = (round(win5 - 0.5, 3) if win5 is not None else None)
            # 信誉分：**中位超额为主**（平均易被单票极值拉动），胜率补正，
            # 再按样本量收缩到 50（n/(n+10)），抑制「5 条样本就 96 分」的小样本虚高。
            score = 50.0
            if med is not None:
                score += max(-40.0, min(40.0, med * 4.0))
            if win5 is not None:
                score += max(-15.0, min(15.0, (win5 - 0.5) * 60.0))
            score = 50.0 + (score - 50.0) * (n_filled / float(n_filled + 10))
            score = round(max(0.0, min(100.0, score)), 1)
            tier = "A" if score >= 70 else ("B" if score >= 55 else ("C" if score >= 42 else "D"))
            note = ""
        else:
            avg_ex5 = avg_ex1 = win5 = win1 = avg_ret1 = med = None
            best = worst = None
            by_state = {}
            pick = timing = score = None
            tier = "-"
            note = "样本不足（可回填 %d 条 < %d），不给信誉分" % (n_filled, MIN_N_SHOW)
        con.execute(
            "INSERT OR REPLACE INTO res_source(src_type,src_key,label,n_sig,n_filled,win_t1,win_t5,"
            "avg_ret_t1,avg_ex_t1,avg_ex_t5,median_ex_t5,best_code,best_ex,worst_code,worst_ex,"
            "by_state_json,pick_skill,timing_skill,score,tier,note,updated_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (st, sk, label_map.get(st, st), n_sig, n_filled, win1, win5, avg_ret1, avg_ex1,
             avg_ex5, med,
             best["code"] if best else None, best["ex_t5"] if best else None,
             worst["code"] if worst else None, worst["ex_t5"] if worst else None,
             json.dumps(by_state, ensure_ascii=False), pick, timing, score, tier, note, _now()))
        n_out += 1

    # 战法源：**直接用回测档案**（不需要等信号到期，且比自算样本更权威）。
    # 评分主轴＝跑赢基准比例（beat_bench），胜率补正，再按交易次数收缩到 50。
    for r in con.execute(
            "SELECT strategy_key,name,annual_return,win_rate,beat_bench,rank,trades,"
            "oos_validated,rating FROM res_strategy WHERE window_label='mid'"):
        bb = r["beat_bench"] if r["beat_bench"] is not None else 50.0
        wr = r["win_rate"] if r["win_rate"] is not None else 45.0
        tr = r["trades"] or 0
        sc = 50.0 + max(-40.0, min(40.0, (bb - 50.0) * 1.2)) + \
            max(-10.0, min(10.0, (wr - 45.0) * 0.5))
        sc = round(max(0.0, min(100.0, 50.0 + (sc - 50.0) * (tr / float(tr + 30)))), 1)
        tier = "A" if sc >= 70 else ("B" if sc >= 55 else ("C" if sc >= 42 else "D"))
        note = "rank %s ｜ %s ｜ 跑赢基准 %.1f%% 的交易 ｜ 回测口径：等权单标的满仓、次日收盘成交、不计费用；样本外%s" % (
            r["rank"], r["rating"] or "-", bb,
            "已验证" if r["oos_validated"] else "未通过或未验证")
        # ⚠️ 语义纪律：avg_ex_t5 / median_ex_t5 在**信号源**里是「平均超额收益(%)」，
        # 战法没有这个口径 → 一律置 None，绝不拿 beat_bench 顶替
        # （否则前端会把「跑赢基准比例 88%」误显示成「+38% 超额收益」）。
        # 战法的「跑赢基准比例(%)」放 pick_skill；「年化%」放 avg_ret_t1（前端按 src_type 分列）。
        con.execute(
            "INSERT OR REPLACE INTO res_source(src_type,src_key,label,n_sig,n_filled,win_t1,win_t5,"
            "avg_ret_t1,avg_ex_t1,avg_ex_t5,median_ex_t5,pick_skill,timing_skill,score,tier,note,"
            "by_state_json,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("strategy", r["strategy_key"], "战法", tr, tr, None, round(wr / 100.0, 3),
             round(r["annual_return"] or 0, 2), None, None, None,
             round(bb, 2), round(bb - 50.0, 2), sc, tier, note, "{}", _now()))
        n_out += 1
    con.commit()
    return {"sources": n_out}


def build_cards(con: sqlite3.Connection, bars: dict, strat_by_code: dict,
                name_of: dict) -> dict:
    """个股共振卡（支柱 A）。"""
    con.execute("DELETE FROM res_stock_card")
    try:
        con.execute("ALTER TABLE res_stock_card ADD COLUMN actual_t5 REAL")
    except Exception:  # noqa: BLE001  列已存在则忽略
        pass
    # 预测↔实际闭环回填：取该票 ret_t5 均值（sig_unified 已回填真实收益）
    # ★ 2026-10-21 dy9 修正：原先限定 direction='多'，但实测同一票同日期「多/空」两行
    # 的 ret_t5 **完全相同**（例：000002 2026-09-30 两方向均为 -13.85）——
    # ret_t5 记的是**股票原始收益**，与信号方向无关。限定方向只会白白丢掉只有空信号的票。
    _actual_t5 = {r["code"]: r["ret"] for r in con.execute(
        "SELECT code, AVG(ret_t5) ret FROM sig_unified "
        "WHERE ret_t5 IS NOT NULL GROUP BY code")}
    today = _latest_trade_date()
    Wt = _load_weights(con)          # ★ 反哺入口：优先用 evolve 学出的权重
    # ★ 因子组合合理性门控（2026-10-20 进哥质疑整改）：
    # evolve 已按 IC 给权重，但对 |IC|<0.004 的“近似无效源”（如 strategy IC=0.002）
    # 仍可能占高权。此处对近似无效源降权，避免“堆因子反而不行”的陷阱。
    _ic = Wt.get("ic") or {}
    def _gate(w, icv):
        if icv is None:
            return w * 0.5
        if abs(icv) < 0.009:
            return w * 0.4          # 近似0（|IC|<0.009）→ 强降权（处理 strategy 类，2026-10-21 阈值上调防噪声抖动）
        return w                     # 正负 IC 交给 evolve 学的权重，不二次干预
    Wg = {k: _gate(Wt.get(k, 0.0), _ic.get(k)) for k in ("blogger", "strategy", "senti", "engine")}
    _wg_tot = sum(Wg.values()) or 1.0
    Wg = {k: v / _wg_tot for k, v in Wg.items()}
    win_start = _since(CARD_WINDOW)
    sigs = con.execute("SELECT * FROM sig_unified WHERE ts>=?", (win_start,)).fetchall()

    stocks: dict = {}

    def _get(code, name=""):
        s = stocks.setdefault(code, {"code": code, "name": name or name_of.get(code, ""),
                                     "bloggers": {}, "strat": [], "fams": set(),
                                     "lhb": [], "lhb_b": 0.0, "senti": 0.0, "senti_n": 0,
                                     "engine": [], "ev": []})
        if name and not s["name"]:
            s["name"] = name
        return s

    # 候选来源：近 CARD_WINDOW 日有信号的票 + 关注池
    for r in sigs:
        s = _get(r["code"], r["name"] or "")
        if r["src_type"] == "blogger":
            s["bloggers"][r["src_key"]] = max(s["bloggers"].get(r["src_key"], 0), r["strength"] or 0)
            if len(s["ev"]) < 6:
                s["ev"].append("【博主·%s】%s" % (r["src_key"], (r["evidence"] or "")[:150]))
        elif r["src_type"] == "strategy":
            for k in (r["evidence"] or "").split("：")[-1].split("、"):
                k = k.strip()
                if k and k not in s["strat"]:
                    s["strat"].append(k)
            if len(s["ev"]) < 6:
                s["ev"].append("【战法】%s" % (r["evidence"] or "")[:150])
        elif r["src_type"] == "lhb":
            s["lhb"].append(r["evidence"] or "")
            s["lhb_b"] += LHB_BONUS if r["direction"] == "多" else (
                LHB_PENALTY if r["direction"] == "空" else 0.0)
            s["ev"].append("【龙虎榜·印证】%s" % (r["evidence"] or "")[:130])
        elif r["src_type"] == "sentiment":
            s["senti"] += (r["raw_score"] or 50) - 50
            s["senti_n"] += 1
        elif r["src_type"] == "engine":
            s["engine"].append(r["src_key"])
            s["ev"].append("【主引擎】%s｜%s" % (r["src_key"], (r["evidence"] or "")[:130]))
    for r in con.execute("SELECT symbol, name FROM watch_items"):
        _get(r["symbol"], r["name"] or "")

    # 战法命中（来自 sig_tech 的结果，按票回填）
    # 流派归属：优先取 predictor.STRATEGIES（64 条战法与 sig_tech 同源），
    # 拿不到则退回 strategy_sim 表（只有 14 条主窗口战法，覆盖不全）。
    fam_of = {}
    for _s in (getattr(P, "STRATEGIES", None) or []):
        _k = _s.get("key")
        if _k:
            fam_of[_k] = _s.get("school") or ""
    if not fam_of:
        for _r in con.execute("SELECT strategy_key, school FROM strategy_sim"):
            fam_of[_r["strategy_key"]] = _r["school"] or ""
    rating_of = {r["strategy_key"]: (r["rating"] or "") for r in
                 con.execute("SELECT strategy_key, rating FROM strategy_sim_window WHERE window_label='mid'")}
    for code, s in stocks.items():
        for k in strat_by_code.get(code, []):
            if k in TIMING_STRATS:
                continue          # 择时类不计入个股共振（见 TIMING_STRATS 注释）
            if k not in s["strat"]:
                s["strat"].append(k)
            f = fam_of.get(k)
            if f:
                s["fams"].add(f)

    # ★ P2 换手率代理（dy8）：量比 = 当日成交量 / 近 20 日均量。
    # 换手率 = 成交量 / 流通股本，而流通股本短期近似不变 → 量比与
    # "换手率 / 20 日均换手率"等价，故无需流通股本即可代理换手率。
    def _turn_ratio(code, date):
        # refresh_cards 调本函数时 bars 传 None（历史签名），退回模块级 _BARS
        _bar_src = bars if bars is not None else _BARS
        b = (_bar_src or {}).get(code) or []
        if not b:
            return None
        i = None
        for k in range(len(b) - 1, -1, -1):
            if b[k][0] <= date:
                i = k
                break
        if i is None or i < 5 or len(b[i]) < 5:
            return None
        vols = [b[k][4] for k in range(max(0, i - 19), i + 1)
                if len(b[k]) > 4 and b[k][4]]
        if len(vols) < 5:
            return None
        ma = sum(vols) / float(len(vols))
        return (b[i][4] / ma) if ma else None

    # 池内百分位归一（平均排名法；**全池同值 → 中性 50**，
    # 否则「全池都是 0 个战法」会被误判成人人 100 分，这是第一版实测踩到的坑）
    def _pct_norm(arr_sorted, v):
        n = len(arr_sorted)
        if n == 0:
            return 50.0
        if arr_sorted[0] == arr_sorted[-1]:
            return 50.0
        lo = hi = 0
        for x in arr_sorted:
            if x < v:
                lo += 1
            if x <= v:
                hi += 1
        return round(100.0 * (lo + hi) / (2.0 * n), 1)

    bw = {c: sum(s["bloggers"].values()) for c, s in stocks.items()}
    sw = {c: len(s["strat"]) for c, s in stocks.items()}
    sen = {c: s["senti"] for c, s in stocks.items()}
    eng = {c: len(s["engine"]) for c, s in stocks.items()}
    _bw_arr = sorted(bw.values())
    _sw_arr = sorted(sw.values())
    _sen_arr = sorted(sen.values())
    _eng_arr = sorted(eng.values())

    # 板块资金维度：**本期不接入**（诚实口径，不留假象 —— 2026-10-15 深度审计处理）。
    # 原因：本地板块资金源 board_flow_daily 目前只有 2026-10-08 一天的数据，
    # 且补采时东财 clist 与新浪快照双双不可达（源侧问题，非本模块代码问题）。
    # 与其拿一天陈旧数据充"板块资金"，不如留空并在口径里说明。
    # 表字段 sector / sector_flow 保留，待源恢复后接入。
    n_card = 0
    for code, s in stocks.items():
        # 至少一路有效信号才出卡（避免关注池空票刷屏）
        if not s["bloggers"] and not s["strat"] and not s["engine"]:
            continue
        # ★ P1-b 博主分档（dy8 2026-10-21）：40787 行面板实证——弱提及 T+5 超额
        # -0.568%(t=-4.41 显著负)，强提及 +0.685%(t=0.46 不显著)，无提及 +2.187%。
        # 线性百分位把"弱/强"混成一个负 IC，正是"负 IC 反转"A 方案失败的根因
        # （反转会把"无人提及"顶到满分，与产品语义相反）。改为三分档：
        _bwv = bw[code]
        if _bwv <= 0:
            nb = 50.0                      # 无提及 → 中性
        elif _bwv < BLOGGER_STRONG_BW:
            nb = 0.0                       # 弱提及（零星 1-2 条）→ 负面标签，压到最低
        else:
            nb = 60.0                      # 强提及 → 中性偏正（统计不显著，不给满分）
        ns = _pct_norm(_sw_arr, sw[code])
        # ★ dy8c engine 分档：有提名 T+5 超额 -1.243%(t=-2.29) 显著负，
        # 线性百分位正向加权等于奖励落后票 → 改为「无提名中性 50 / 有提名 30」。
        ne = 50.0 if eng[code] <= 0 else 30.0
        nu = _pct_norm(_sen_arr, sen[code]) if s["senti_n"] else 50.0
        score = (Wg["blogger"] * nb + Wg["strategy"] * ns
                 + Wg["senti"] * nu + Wg["engine"] * ne)
        score = max(0.0, min(100.0, score + max(-10.0, min(10.0, s["lhb_b"]))))
        # ★ P2 量比换手惩罚（dy8）：放量追高（量比>2.5）线性扣分，最多 15 分
        _tr = _turn_ratio(code, today)
        _pen = 0.0
        if _tr is not None and _tr > TURN_CHASE_RATIO:
            _pen = min(TURN_CHASE_PENALTY_MAX, (_tr - TURN_CHASE_RATIO) * 10.0)
            score = max(0.0, score - _pen)
        tier = "S" if score >= 80 else ("A" if score >= 68 else ("B" if score >= 55 else "C"))
        # n_strat=0 的票要说明原因（审计发现：854 张卡里有 8 张零战法命中，
        # 基本是北交所/次新等 K 线不足的票 —— 静默显示"0 个"会被当成 bug）
        strat_txt = ("%d 个（%s）" % (len(s["strat"]), "/".join(sorted(s["fams"])[:4]))
                     if s["strat"] else
                     "0 个（本票未触发任何选股类战法：多为上市时间短 / K 线不足 "
                     "130 根，或当前无技术买点）")
        # 2026-10-30：博主荐股下线后 bloggers 恒空、bw 恒 0，卡片文案里不再出现博主字样
        # （省掉"0 位博主提及""博主 0%""博主分档：无提及"这类看着像故障的残句）。
        verdict = (
            "%s命中战法 %s｜主引擎 %d 路交叉%s｜权重：%s" % (
                ("%d 位博主提及（信誉加权声量 %.0f 分位）｜" % (len(s["bloggers"]), round(nb))
                 if s["bloggers"] else ""),
                strat_txt, len(s["engine"]),
                "｜龙虎榜印证：%s" % s["lhb"][0][:40] if s["lhb"] else "",
                ("数据学出 %s" % Wt["fit_date"]) if Wt["src"] == "learned" else "默认")
                + "｜门控权重 舆情%.0f%%/战法%.0f%%/引擎%.0f%%"
                  % (Wg["senti"]*100, Wg["strategy"]*100, Wg["engine"]*100)
                + ("｜量比 %.2f→扣 %.1f 分" % (_tr, _pen) if _pen > 0 else "")
        )
        con.execute(
            "INSERT OR REPLACE INTO res_stock_card(build_date,code,name,res_score,tier,n_blogger,"
            "blogger_w,bloggers,n_strat,strat_keys,strat_families,strat_weight,lhb_confirm,lhb_bonus,actual_t5,"
            "senti_net,senti_n,sector,sector_flow,engine_cross,is_watch,evidence_json,verdict)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (today, code, s["name"], round(score, 1), tier, len(s["bloggers"]),
             round(bw[code], 1), "、".join(sorted(s["bloggers"])), len(s["strat"]),
             "、".join(s["strat"]), "、".join(sorted(s["fams"])), sw[code],
             ("；".join(s["lhb"])[:400] if s["lhb"] else ""), round(s["lhb_b"], 1),
             # ★ 2026-10-21 dy9 修正：原来写 `_actual_t5.get(code) or 0.0`，
             # 把「没有真实收益」错写成 0.0 —— 实测 825 张卡里 308 张被填成 0，
             # 直接把分层均值（AVG）和避雷名单证据条全部污染。
             # 缺失必须落 NULL（SQL 语义＝未知），不能落 0（＝恰好持平）。
             (round(_actual_t5[code], 2) if code in _actual_t5 else None),
             round(s["senti"], 2), s["senti_n"], "", None,
             "、".join(sorted(set(s["engine"]))), 0,
             json.dumps(s["ev"][:8], ensure_ascii=False), verdict))
        n_card += 1
    con.commit()
    return {"cards": n_card, "weight_src": Wt["src"], "weight_fit": Wt["fit_date"]}


def refresh_cards(con: sqlite3.Connection, bars: dict | None = None) -> dict:
    """用**最新学出的权重**重算共振卡（evolve 学完后调用 → 学习当天就生效）。

    只重算卡片，不重跑信号抽取与战法回测：战法命中从已落库的 res_stock_card 取回，
    故成本极低（亚秒级），可以在学习结束后立刻反哺 —— 这是「自我提升」的闭环出口。

    ⚠️ 两个实测踩到的坑（2026-10-15）：
      ① **必须先 _set_bars**：`_latest_trade_date()` 依赖 `_BARS`，而本函数不像 build()
         那样会载入 K 线 —— 不设就退化成自然日（今天），导致新卡片的 build_date
         与 res_divergence 等表**对不上**、前端分歧预警读成 0 条。
      ② **必须同步重算分歧预警**：分歧源自卡片，卡片日期一变而分歧不跟着变就会脱节。
    """
    _set_bars(bars if bars is not None else load_bars())
    strat_by_code = {}
    try:
        for r in con.execute("SELECT code, strat_keys FROM res_stock_card"):
            keys = [k for k in str(r["strat_keys"] or "").split("、") if k]
            if keys:
                strat_by_code[r["code"]] = keys
    except Exception:  # noqa: BLE001
        pass
    name_of = {}
    try:
        name_of = {r["code"]: r["name"] for r in con.execute("SELECT code,name FROM res_stock")}
    except Exception:  # noqa: BLE001
        pass
    out = build_cards(con, None, strat_by_code, name_of)
    try:
        out.update(build_divergence(con))
    except Exception:  # noqa: BLE001
        pass
    return out


def build_strategy_archive(con: sqlite3.Connection, strat_by_code: dict) -> dict:
    """战法档案（支柱 B）：回测指标 + 当日命中。"""
    name_of, school_of = {}, {}
    for r in con.execute("SELECT strategy_key, name, school FROM strategy_sim"):
        name_of[r["strategy_key"]] = r["name"]
        school_of[r["strategy_key"]] = r["school"]
    for _s in (getattr(P, "STRATEGIES", None) or []):
        _k = _s.get("key")
        if _k:
            name_of.setdefault(_k, _s.get("name") or _k)
            school_of.setdefault(_k, _s.get("school") or "")
    rev: dict = {}
    for code, ks in strat_by_code.items():
        for k in ks:
            rev.setdefault(k, []).append(code)
    con.execute("DELETE FROM res_strategy")
    n = 0
    for r in con.execute("SELECT * FROM strategy_sim_window WHERE window_label='mid' ORDER BY rank"):
        k = r["strategy_key"]
        hit = rev.get(k, [])
        kb_n = con.execute("SELECT COUNT(*) n FROM kb_strategy_node WHERE strat_key=?", (k,)).fetchone()["n"]
        con.execute(
            "INSERT OR REPLACE INTO res_strategy(strategy_key,window_label,name,school,annual_return,"
            "total_return,win_rate,max_dd,beat_bench,trades,score,rank,rating,oos_return,oos_beat_bench,"
            "oos_validated,hit_n,hit_codes,kb_n,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (k, "mid", name_of.get(k) or r["strategy_key"], school_of.get(k) or "",
             r["annual_return"], r["total_return"], r["win_rate"], r["max_dd"], r["beat_bench"],
             r["trades"], r["score"], r["rank"], r["rating"], r["oos_return"], r["oos_beat_bench"],
             r["oos_validated"], len(hit), "、".join(hit[:20]), kb_n, _now()))
        n += 1
    con.commit()
    return {"strategies": n}


def build_kb_graph(con: sqlite3.Connection) -> dict:
    """战法知识图谱：从知识轨道源文件抽「战法类」词条，与战法回测互证。"""
    con.execute("DELETE FROM kb_strategy_node")
    rating_of = {r["strategy_key"]: (r["rating"] or "") for r in
                 con.execute("SELECT strategy_key, rating FROM strategy_sim_window WHERE window_label='mid'")}
    ret_of = {r["strategy_key"]: (r["annual_return"] or 0) for r in
              con.execute("SELECT strategy_key, annual_return FROM strategy_sim_window WHERE window_label='mid'")}
    n = 0
    for d in con.execute("SELECT doc_key,track,source_path FROM kb_doc"):
        sp = d["source_path"] or ""
        if not sp or not os.path.exists(sp):
            continue
        try:
            with open(sp, "r", encoding="utf-8") as f:
                text = f.read()
        except Exception:  # noqa: BLE001
            continue
        blocks = []
        cur_t, cur_b = None, []
        for line in text.splitlines():
            if line.startswith("#"):
                if cur_t:
                    blocks.append((cur_t, "\n".join(cur_b).strip()))
                cur_t = line.strip("# ").strip()
                cur_b = []
            elif cur_t:
                cur_b.append(line)
        if cur_t:
            blocks.append((cur_t, "\n".join(cur_b).strip()))
        for i, (t, b) in enumerate(blocks):
            if not b or len(b) < 30:
                continue
            if not t or len(t) < 4 or t in ("目录", "前言", "序言", "概述"):
                continue
            hit_kw, sk = [], None
            for kws, key in KW_TO_STRAT:
                if any(kw in t or kw in b[:600] for kw in kws):
                    hit_kw.append(kws[0])
                    if sk is None:
                        sk = key
            if not hit_kw:
                continue
            if sk and sk in rating_of:
                ann = ret_of.get(sk) or 0
                if rating_of[sk] in ("优秀", "良好") and ann > 10:
                    cons = "一致"
                    vd = "回测印证：%s 年化 %.2f%%，评级 %s —— 该说法与回测同向" % (
                        sk, ann, rating_of[sk])
                elif rating_of[sk] in ("较差",):
                    cons = "存疑"
                    vd = "回测警示：%s 年化 %.2f%%，评级 %s —— 该说法未被回测支持" % (
                        sk, ann, rating_of[sk])
                else:
                    cons = "待观察"
                    vd = "回测中性：%s 年化 %.2f%%，评级 %s" % (sk, ann, rating_of[sk])
            else:
                cons, vd = "无回测", "本系统未对该说法建立回测对照（不评价对错）"
            con.execute(
                "INSERT OR REPLACE INTO kb_strategy_node(nid,doc_key,track,title,body,strat_key,"
                "consistency,verdict,tags,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                ("%s#%d" % (d["doc_key"], i), d["doc_key"], d["track"], t[:80], b[:2000],
                 sk or "", cons, vd, "、".join(hit_kw[:6]), _now()))
            n += 1
    con.commit()
    return {"kb_nodes": n}


def build_divergence(con: sqlite3.Connection) -> dict:
    """分歧预警：三类内部矛盾。"""
    con.execute("DELETE FROM res_divergence")
    today = _latest_trade_date()
    n = 0
    # ① 博主看多 但 舆情看空
    rows = con.execute(
        "SELECT code, name, bloggers, senti_net, senti_n, n_blogger, n_strat FROM res_stock_card "
        "WHERE build_date=? AND n_blogger>=2 AND senti_n>=5 AND senti_net<0", (today,)).fetchall()
    for r in rows:
        con.execute("INSERT OR REPLACE INTO res_divergence(build_date,code,name,kind,severity,detail)"
                    " VALUES(?,?,?,?,?,?)",
                    (today, r["code"], r["name"], "博主看多·舆情看空", 3,
                     "%d 位博主（%s）看多，但近 10 日论坛情绪净空 %.1f（%d 条）"
                     % (r["n_blogger"], r["bloggers"], r["senti_net"], r["senti_n"])))
        n += 1
    # ② 博主看多 但 龙虎榜为负档形态（换手/振幅上榜）
    rows = con.execute(
        "SELECT code, name, lhb_confirm, lhb_bonus FROM res_stock_card "
        "WHERE build_date=? AND n_blogger>=1 AND lhb_bonus<0", (today,)).fetchall()
    for r in rows:
        con.execute("INSERT OR REPLACE INTO res_divergence(build_date,code,name,kind,severity,detail)"
                    " VALUES(?,?,?,?,?,?)",
                    (today, r["code"], r["name"], "博主看多·资金形态警示", 2,
                     "博主推荐但龙虎榜形态为负档（实证 T+1 为负）：%s" % (r["lhb_confirm"] or "")[:160]))
        n += 1
    # ③ 多路博主提及 但 零战法背书
    rows = con.execute(
        "SELECT code, name, n_blogger, bloggers FROM res_stock_card "
        "WHERE build_date=? AND n_blogger>=2 AND n_strat=0", (today,)).fetchall()
    for r in rows:
        con.execute("INSERT OR REPLACE INTO res_divergence(build_date,code,name,kind,severity,detail)"
                    " VALUES(?,?,?,?,?,?)",
                    (today, r["code"], r["name"], "多人推荐·无技术背书", 2,
                     "%d 位博主提及但未命中任何战法（%s）" % (r["n_blogger"], r["bloggers"])))
        n += 1
    con.commit()
    return {"divergence": n}


# ================================================================ 主流程
def build(con: sqlite3.Connection, force: bool = False) -> dict:
    with _LOCK:
        t0 = time.time()
        ensure_tables(con)
        try:
            con.execute("PRAGMA journal_mode=WAL")
        except Exception:  # noqa: BLE001
            pass
        _STATUS.update(stage="载入K线底座", detail="", at=_now())
        bars = load_bars()
        if not bars:
            return {"ok": False, "error": "本地 K 线缓存为空（decline_kcache / predict_kcache 都读不到）"}
        _set_bars(bars)
        idx, bench = build_indices(bars)

        _STATUS.update(stage="市场环境回填", detail="%d 个交易日" % len(bench), at=_now())
        states = state_history(bench)
        n_states = fill_state_log(con, states)

        _STATUS.update(stage="股票主数据归一", detail="", at=_now())
        n_stock = sync_stock_master(con)
        name_of = {r["code"]: r["name"] for r in con.execute("SELECT code,name FROM res_stock")}

        _STATUS.update(stage="抽取信号·引擎/龙虎榜/情绪", detail="", at=_now())
        # 2026-10-30：博主荐股这条源已下线（_pull_blogger 恒返回空，见其 docstring），
        # _blogger_credibility 也不再调用 —— 它的唯一消费者就是 _pull_blogger。
        # 保留 _blogger_credibility 函数本身不删，便于日后要恢复时一处改回。
        sigs = []
        sigs += _pull_engine(con)
        sigs += _pull_lhb(con)
        sigs += _pull_sentiment(con)
        for s in sigs:
            s.setdefault("name", "")
            if not s["name"]:
                s["name"] = name_of.get(s["code"], "")
            s["mkt_state"] = states.get(s["ts"], "")

        # ★ 标的范围闸门（2026-10-15 深度审计发现并修复 · 二次加固）：
        # 龙虎榜会带出**可转债**（如 113710 四方转债）、**B 股**（200056 *ST皇庭B）、
        # **北交所**（920xxx）—— 它们的涨跌幅限制与交易规则都与主板/创业板不同。
        # 上一版闸门只判「在不在 res_stock 里」，而 res_stock 本身含 3 只「未知板块」标的
        # （可转债等非股票），等于闸门漏了。改为**按代码规则判真 A 股**（不依赖 market 字段填充）：
        #   沪市主板 60 / 科创板 68 / 深市主板 00 / 创业板 30 / 北交所 92·43·83·87。
        # 北交所**保留**（它是合法 A 股、用户在 A 股语境下也看得到），仅剔除可转债 / B 股 / 其他。
        _A_SHARE_SQL = ("LENGTH(code)=6 AND (code LIKE '60%' OR code LIKE '00%' OR code LIKE '30%'"
                        " OR code LIKE '68%' OR code LIKE '92%' OR code LIKE '43%'"
                        " OR code LIKE '83%' OR code LIKE '87%')")
        valid = {r["code"] for r in con.execute(
            "SELECT code FROM res_stock WHERE " + _A_SHARE_SQL)}
        dropped: dict = {}
        keep = []
        for s in sigs:
            if s["code"] in valid:
                keep.append(s)
            else:
                dropped[s["src_type"]] = dropped.get(s["src_type"], 0) + 1
        n_dropped = len(sigs) - len(keep)
        sigs = keep

        # 候选票集：博主票 ∪ 关注池 ∪ 引擎票
        cand = {s["code"] for s in sigs} | {
            r["symbol"] for r in con.execute("SELECT symbol FROM watch_items")}
        cand = {c for c in cand if c}

        _STATUS.update(stage="战法命中计算", detail="%d 只候选票" % len(cand), at=_now())
        s_sig, strat_by_code = _pull_strategy(con, bars, cand)
        sigs += s_sig

        _STATUS.update(stage="强度归一 + 统一回填", detail="%d 条信号" % len(sigs), at=_now())
        normalize_strength(sigs)
        fill_returns(sigs, idx, bench)

        con.execute("DELETE FROM sig_unified")
        for s in sigs:
            sid = "%s|%s|%s|%s" % (s["src_type"], s["src_key"], s["code"], s["ts"])
            con.execute(
                "INSERT OR REPLACE INTO sig_unified(sid,ts,src_type,src_key,code,name,direction,"
                "strength,raw_score,horizon,evidence,ret_t1,ret_t3,ret_t5,ret_t10,bench_t1,bench_t5,"
                "ex_t1,ex_t5,fill_status,mkt_state,built_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (sid, s["ts"], s["src_type"], s["src_key"], s["code"], s["name"],
                 s["direction"], s.get("strength"), s.get("raw_score"), s.get("horizon"),
                 s.get("evidence"), s.get("ret_t1"), s.get("ret_t3"), s.get("ret_t5"),
                 s.get("ret_t10"), s.get("bench_t1"), s.get("bench_t5"),
                 s.get("ex_t1"), s.get("ex_t5"), s.get("fill_status"), s.get("mkt_state"), _now()))
        con.commit()

        _STATUS.update(stage="战法档案", detail="", at=_now())
        r_b = build_strategy_archive(con, strat_by_code)

        _STATUS.update(stage="战法知识图谱", detail="", at=_now())
        r_kb = build_kb_graph(con)

        _STATUS.update(stage="信源画像", detail="", at=_now())
        r_c = build_source_profile(con, states)

        _STATUS.update(stage="个股共振卡", detail="", at=_now())
        r_a = build_cards(con, bars, strat_by_code, name_of)

        _STATUS.update(stage="分歧预警", detail="", at=_now())
        r_d = build_divergence(con)

        _STATUS.update(stage="完成", detail="", at=_now())
        return {"ok": True, "engine_ver": ENGINE_VER, "bars_codes": len(bars),
                "signals": len(sigs), "candidate_codes": len(cand),
                "states_backfilled": n_states, "stocks": n_stock,
                "non_a_dropped": n_dropped, "non_a_detail": dropped,
                "elapsed": round(time.time() - t0, 1), **r_a, **r_b, **r_c, **r_kb, **r_d}


# ================================================================ 只读接口
def payload(con: sqlite3.Connection) -> dict:
    ensure_tables(con)
    row = con.execute("SELECT MAX(build_date) d FROM res_stock_card").fetchone()
    bd = (row and row["d"]) or None
    cards = [dict(r) for r in con.execute(
        "SELECT * FROM res_stock_card WHERE build_date=? ORDER BY res_score DESC LIMIT 120",
        (bd,))] if bd else []
    for c in cards:
        try:
            c["evidence"] = json.loads(c.pop("evidence_json") or "[]")
        except Exception:  # noqa: BLE001
            c["evidence"] = []
    strategies = [dict(r) for r in con.execute(
        "SELECT * FROM res_strategy WHERE window_label='mid' ORDER BY rank")]
    sources = [dict(r) for r in con.execute("SELECT * FROM res_source ORDER BY "
                                            "CASE WHEN score IS NULL THEN 1 ELSE 0 END, score DESC")]
    for s in sources:
        try:
            s["by_state"] = json.loads(s.pop("by_state_json") or "{}")
        except Exception:  # noqa: BLE001
            s["by_state"] = {}
    div = [dict(r) for r in con.execute(
        "SELECT * FROM res_divergence WHERE build_date=? ORDER BY severity DESC LIMIT 40", (bd,))] if bd else []
    # 知识图谱：**按一致性分档各取一批**，避免"一致"（158 条）把「存疑」全挤掉
    # （第一版实测：LIMIT 120 只看到清一色「一致」，读者会误以为回测全数印证）。
    kb = []
    for _cons in ("一致", "存疑", "待观察", "无回测"):
        kb += [dict(r) for r in con.execute(
            "SELECT * FROM kb_strategy_node WHERE consistency=? ORDER BY doc_key LIMIT 40", (_cons,))]
    ms = None
    r = con.execute("SELECT * FROM market_state_log ORDER BY trade_date DESC LIMIT 1").fetchone()
    if r:
        try:
            ms = {"state": r["state"], "trade_date": r["trade_date"],
                  "breadth": json.loads(r["breadth_json"] or "{}")}
        except Exception:  # noqa: BLE001
            ms = {"state": r["state"], "trade_date": r["trade_date"], "breadth": {}}
    nst = con.execute("SELECT COUNT(*) n FROM market_state_log").fetchone()["n"]
    nsig = con.execute("SELECT COUNT(*) n FROM sig_unified").fetchone()["n"]
    nfill = con.execute("SELECT COUNT(*) n FROM sig_unified WHERE fill_status='ok'").fetchone()["n"]
    # 2026-10-30：口径11 要报当前三项权重，payload 自己也读一次（build 里的 Wt 不在此作用域）
    Wt = _load_weights(con)
    n_mkt = con.execute("SELECT COUNT(*) n FROM res_stock").fetchone()["n"]
    stats = {
        "signals": nsig, "filled": nfill, "cards": len(cards), "strategies": len(strategies),
        "sources": len(sources), "divergence": len(div), "kb_nodes": len(kb),
        "state_days": nst,
    }
    caliber = [
        "口径1 · 超额基准＝%s（基准池＝本地 K 线覆盖的全市场个股，逐日取中位数），不是沪深300："
        "面板覆盖的票以中小盘题材股为主，等权基准更贴近其真实收益来源。"
        "股票主数据 %d 只（以 stock_names 归一，全库 code/symbol 五种写法已对齐）。" % (BENCH_LABEL, n_mkt),
        "口径2 · 龙虎榜**只作印证**（cross-check + 少量加分），不进评分主干——"
        "上榜是结果不是原因（因果倒置），故不做预判输入。",
        "口径3 · 跨源强度用**源内百分位归一**（同源内先排名再映射 0~100），不做量纲硬拼。",
        "口径4 · 样本不足（可回填 < %d 条）不给信誉分、不参与排序，如实标注。" % MIN_N_SHOW,
        "口径5 · 市场环境三态（结构/震荡/普跌）由全市场等权净值与中位涨幅离线判定，"
        "指数项用等权净值 20 日线代替上证 20 日线（本地无指数 K 线）。",
        "口径6 · 战法回测沿用语料库自带口径：等权单标的满仓、信号次日收盘成交、"
        "不计手续费与滑点、后复权，仅作策略相对强弱比较，不是可实现收益预测。",
        "口径7 · **标的范围**：本面板只处理 A 股（闸门＝stock_names 5588 只）。"
        "龙虎榜会带出可转债 / B 股 / 北交所标的，其涨跌幅限制与交易规则与 A 股不同，"
        "已在入库前统一剔除（剔除数量见接口 non_a_dropped 字段，不做静默丢弃）。",
        "口径8 · **可交易性**：收益回填按「信号日收盘买入、持有 N 个交易日」计算；"
        "而龙虎榜 / 舆情 / 战法命中都在盘后生成、实际最早只能**次日**买入 —— 故这是「信号有效性」的度量，"
        "不等于可实现收益，实盘还要再扣掉一个开盘跳空。",
        "口径9 · **数据源时效**：各源更新节奏不同（龙虎榜 T 日盘后、战法命中为当日快照、"
        "论坛舆情为当日抓取）。面板顶部「数据日」是**统一信号日**口径，"
        "不代表每个源都新鲜到该日。",
        "口径10 · **板块资金维度本期未接入**：本地板块资金源当前只有 2026-10-08 一天的数据，"
        "补采时上游（东财/新浪）双双不可达。卡片上 sector / sector_flow 留空，"
        "不用陈旧数据充数 —— 宁缺勿假；源恢复后再接入。",
        "口径11 · **博主荐股维度已于 2026-10-30 永久下线**：该源实证为纯噪声"
        "（15 位博主 / 898 样本，最高 |t|=1.17、有效独立窗口仅 10.4 个），"
        "原 34%% 权重按比例并入战法 / 舆情 / 主引擎三项（现为 %.0f%% / %.0f%% / %.0f%%）。"
        "历史 blogger_overlap 数据仍保留，只用于「剔除池」的负收益风控，不再参与本面板打分。"
        % (Wt["strategy"] * 100, Wt["senti"] * 100, Wt["engine"] * 100),
    ]
    return {"ok": True, "build_date": bd, "data_date": bd, "engine_ver": ENGINE_VER,
            "market_state": ms,
            "cards": cards, "strategies": strategies, "sources": sources,
            "divergence": div, "kb_nodes": kb, "stats": stats, "caliber": caliber,
            "weights": {"strategy": Wt["strategy"], "senti": Wt["senti"],
                        "engine": Wt["engine"], "blogger": 0.0,
                        "src": Wt["src"], "fit_date": Wt["fit_date"],
                        "blogger_off": True},
            "strategy_note": "「今日命中」= 该战法在**全部候选票**中的命中数；"
                             "卡片侧命中数会略小 —— 因为只有出了共振卡的票才进卡片统计。"
                             "两者口径不同，不是数据不一致。",
            "note": "三支柱＋自我提升：个股共振（票为中心）· 战法体系（战法为中心）· "
                    "信源画像（人为中心）· 自我提升（从汇聚到长出）。"
                    "生成由后端批量任务完成，展示层只读。"
                    "博主荐股源已于 2026-10-30 下线，本面板不再含任何博主维度。"}


def avoid_payload(con: sqlite3.Connection, limit: int = 80) -> dict:
    """避雷名单（2026-10-21 dy9，进哥拍板）——把「低分卡」从排序末尾升级为一级显式功能。

    ★ 为什么单独做这个面板（实证，不是拍脑袋）：
      40747 行面板、D+1 买入持 5 日、超额基准=全市场等权中位数：
        · C 级（低分）    实际 T+5 超额 **-4.51%**，比全样本 +1.93% 差 6.4 个百分点
        · S 级（最高分）  实际 -0.65%（有真实收益样本少）→ **头部是负 alpha**
      翻译：四维信号对「谁不能买」有统计显著性，对「该买谁」没有。
      故本面板只做**排除**，不做推荐，且必须带上真实回测数字，不做暗示性渲染。

    归因逐条可追溯（每条都标了它自己的历史 T+5 超额与 t 值），
    来源＝dy8 面板分层检验结果，不是经验判断。
    """
    ensure_tables(con)
    row = con.execute("SELECT MAX(build_date) d FROM res_stock_card").fetchone()
    bd = (row and row["d"]) or None
    if not bd:
        return {"ok": False, "error": "尚未生成共振卡，无避雷名单", "build_date": None,
                "items": [], "stats": {}}

    # ── 负面归因规则（阈值与 t 值均来自 dy8 面板分层，硬编码在此处以便追溯）──
    # (判定函数, 短标签, 说明, 该标签自身的历史 T+5 超额)
    def _has_lhb(c):
        try:
            return float(c["lhb_bonus"] or 0) < 0
        except Exception:  # noqa: BLE001
            return False

    def _has_engine(c):
        return bool((c["engine_cross"] or "").strip())

    def _weak_blogger(c):
        try:
            bw = float(c["blogger_w"] or 0)
        except Exception:  # noqa: BLE001
            return False
        return 0 < bw < 150.0     # 卡片口径：strength 0~100，1.5 阈值等值于 150

    def _bearish(c):
        try:
            return float(c["senti_net"] or 0) < 0
        except Exception:  # noqa: BLE001
            return False

    def _no_strat(c):
        try:
            return int(c["n_strat"] or 0) == 0
        except Exception:  # noqa: BLE001
            return False

    RULES = [
        (_has_lhb,      "龙虎榜上榜",   "上榜是结果不是原因；正/负向上榜历史 T+5 均显著为负（-1.59% / -1.86%）"),
        (_has_engine,   "引擎提名",     "被主引擎提名者历史 T+5 超额 -1.24%（t=-2.29）显著为负"),
        (_weak_blogger, "弱博主提及",   "被少量提及（声量 0~1.5）历史 T+5 超额 -0.57%（t=-4.41）显著为负"),
        (_bearish,      "舆情净看空",   "情绪净值为负，方向与买点相反"),
        (_no_strat,     "无战法命中",   "零战法支持，仅有情绪面，缺乏技术面印证"),
    ]

    rows = [dict(r) for r in con.execute(
        "SELECT * FROM res_stock_card WHERE build_date=? ORDER BY res_score ASC",
        (bd,))]
    # 避雷候选：tier=C 优先，按分数升序（最低分最该避）
    cands = [c for c in rows if (c.get("tier") or "") == "C"]
    if len(cands) < limit:
        cands = [c for c in rows if (c.get("tier") or "") in ("C", "B")]
    cands = cands[:limit]

    items = []
    for c in cands:
        reasons = []
        for fn, tag, why in RULES:
            try:
                if fn(c):
                    reasons.append({"tag": tag, "why": why})
            except Exception:  # noqa: BLE001
                continue
        items.append({
            "code": c.get("code"), "name": c.get("name"),
            "res_score": c.get("res_score"), "tier": c.get("tier"),
            "n_blogger": c.get("n_blogger"), "blogger_w": c.get("blogger_w"),
            "n_strat": c.get("n_strat"), "senti_net": c.get("senti_net"),
            "lhb_bonus": c.get("lhb_bonus"), "engine_cross": c.get("engine_cross") or "",
            "actual_t5": c.get("actual_t5"),
            "risk_n": len(reasons),
            "reasons": reasons,
        })
    # 风险条数多的排前面（同分数下更该避）
    items.sort(key=lambda x: (-x["risk_n"], x["res_score"] if x["res_score"] is not None else 999))

    # ── 真实回测证据（每个 tier 的实际 T+5 均值，前端原样展示，不做修饰）──
    tiers = {}
    for r in con.execute(
            """SELECT tier, COUNT(*) n,
                      SUM(CASE WHEN actual_t5 IS NOT NULL THEN 1 ELSE 0 END) nf,
                      AVG(actual_t5) avg_t5
               FROM res_stock_card WHERE build_date=? GROUP BY tier""", (bd,)):
        tiers[r["tier"] or "?"] = {"n": r["n"], "n_filled": r["nf"],
                                   "avg_t5": round(r["avg_t5"], 3) if r["avg_t5"] is not None else None}
    allrow = con.execute(
        """SELECT COUNT(*) n, SUM(CASE WHEN actual_t5 IS NOT NULL THEN 1 ELSE 0 END) nf,
                  AVG(actual_t5) a FROM res_stock_card WHERE build_date=?""", (bd,)).fetchone()
    stats = {
        "total": allrow["n"], "filled": allrow["nf"],
        "avg_t5": round(allrow["a"], 3) if allrow["a"] is not None else None,
        "by_tier": tiers,
        "avoid_n": len(items),
    }
    def _ftn(d):
        """取某档的 avg_t5 数值，缺失按 0 处理（仅用于做差）。"""
        v = (d or {}).get("avg_t5")
        return float(v) if v is not None else 0.0

    def _ft(d):
        """某档 avg_t5 的展示串；缺失显示 —，不显示 0 冒充有数据。"""
        v = (d or {}).get("avg_t5")
        return ("%+.2f" % v) if v is not None else "—"

    def _cn(d):
        """某档「有真实收益」的张数。"""
        return int((d or {}).get("n_filled") or 0)

    def _tn(d):
        """某档总张数。"""
        return int((d or {}).get("n") or 0)

    def _fv(v):
        """裸数值（如全样本 avg_t5）的展示串。"""
        try:
            return ("%+.2f" % float(v)) if v is not None else "—"
        except Exception:  # noqa: BLE001
            return "—"

    caliber = [
        "口径 · 本面板**只做排除，不做推荐**：四维信号对「谁不能买」有统计显著性，"
        "对「该买谁」没有（S 级头部实测负 alpha）。任何把这里反过来当买入清单的用法都是误用。",
        "口径 · 每条负面标签后面的百分比，是该标签在 40747 行面板上的**真实 T+5 超额**"
        "（D+1 收盘买入、持有 5 日、超额基准＝全市场等权中位数），不是估计值。",
        "口径 · 风险条数＝命中几条独立负面标签，**不是概率**。条数多＝多个角度看都该避。",
        "口径 · actual_t5 为该卡生成日之后 5 个交易日的真实超额；为 null 表示还没到期，"
        "不代表被剔除。",
        # ★ dy9 修正（2026-10-21）：上一版把 -4.51%/+1.93% 写死在文案里，那是 dy8 重建瞬间的
        # 快照；回填补齐到 100% 后真实值是「S %.2f%% / A %.2f%% / B %.2f%% / C %.2f%%，全样本 %.2f%%」。
        # 文案改为**运行时动态取值**，杜绝"数字漂了、说明没跟"的陈旧口径。
        "口径 · 本次实测分层（%s）：S %s%%（%d/%d 张有真实收益）· A %s%%（%d/%d）· "
        "B %s%%（%d/%d）· C %s%%（%d/%d）· 全样本 %s%%（%d/%d）。"
        "**样本量必须一起看**：S/A 里大量是刚进池的新票，还没满 5 个交易日、拿不到真实收益 —— "
        "越新鲜的高分票越无法验证，所以 S/A 的均值只代表「老票里高分的那批」，不能当作全体结论。"
        % (bd,
           _ft(tiers.get("S")), _cn(tiers.get("S")), _tn(tiers.get("S")),
           _ft(tiers.get("A")), _cn(tiers.get("A")), _tn(tiers.get("A")),
           _ft(tiers.get("B")), _cn(tiers.get("B")), _tn(tiers.get("B")),
           _ft(tiers.get("C")), _cn(tiers.get("C")), _tn(tiers.get("C")),
           _fv(stats.get("avg_t5")), stats.get("filled") or 0, stats.get("total") or 0),
    ]
    return {"ok": True, "build_date": bd, "items": items, "stats": stats,
            "caliber": caliber,
            "note": "避雷名单＝低分卡的显式归因。C 级实测 T+5 超额 %s%%（%d/%d 张有数据），"
                    "全样本 %s%%（%d/%d）。C 级与全样本差距其实只有 %.2f 个百分点 —— "
                    "真正有区分度的是 A 级(%s%%)与 C 级(%s%%)之间；"
                    "而避雷名单本身（最低分+多负面标签）实测 27 个有效样本里 25 个为负。"
                    % (_ft(tiers.get("C")), _cn(tiers.get("C")), _tn(tiers.get("C")),
                       _fv(stats.get("avg_t5")), stats.get("filled") or 0, stats.get("total") or 0,
                       abs(_ftn(tiers.get("C")) - _ftn({"avg_t5": stats.get("avg_t5")})),
                       _ft(tiers.get("A")), _ft(tiers.get("C")))}


def status_payload(con: sqlite3.Connection) -> dict:
    ensure_tables(con)
    return {"engine_ver": ENGINE_VER, "stage": _STATUS.get("stage"),
            "detail": _STATUS.get("detail"), "at": _STATUS.get("at"),
            "today": _today(),
            "runs": [{"build_date": r["build_date"], "cards": r["n"]} for r in con.execute(
                "SELECT build_date, COUNT(*) n FROM res_stock_card GROUP BY build_date "
                "ORDER BY build_date DESC LIMIT 8")]}


def maybe_build(con: sqlite3.Connection) -> dict:
    """定时任务入口：当天没跑过就跑一次（自带锁与异常隔离）。"""
    try:
        row = con.execute("SELECT COUNT(*) n FROM res_stock_card WHERE build_date=?",
                          (_today(),)).fetchone()
        if row and row["n"] > 0:
            return {"ok": True, "skipped": "今天已有共振卡"}
    except Exception:  # noqa: BLE001
        pass
    try:
        return build(con)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)[:300]}
