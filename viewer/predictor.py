# -*- coding: utf-8 -*-
"""每日预测引擎（pred-v1.0，2026-09-28 新增）

定位：把库里已有的六路信号（技术面 / 论坛情绪 / 龙虎榜资金 / 选手跟随 / 外部参考 / 筹码）
汇合成一份**每日短线推荐**（10 核心 + 10 观察），并在 T+1 / T+3 / T+5 用真实收盘价
自动复盘，按各信号的实际命中率**自适应调整权重** —— 形成一个可追溯、可验证的自我进化闭环。

设计口径（重要，界面上会一并标注）：
- 预测基准价：预测日（T）收盘价；复盘收益 = 目标日收盘 / T 日收盘 - 1（不复权口径）。
- 命中判定：该票同期收益 > **全候选池同期等权平均收益**（即"跑赢本池"），衡量的是选股超额能力，
  而不是绝对涨跌 —— 熊市里能选到跌得少的，同样算命中。
- 权重学习：贝叶斯收缩。样本少时靠近中性先验（0.5 命中率），样本多时才相信实盘表现，
  避免 3 个样本的高胜率把权重带飞（防过拟合）。
- 历史不可回补：posts / positions / lhb_* 等表只存当前状态，无历史快照 → 只有技术面可回放，
  其余信号必须从启用日起逐日实盘累积。
- K 线来源复用 strategy_sim.fetch_kline（新浪日线，空代理 opener，绕过本机代理劫持）。

对外接口（server.py 调用）：
    ensure_tables(con)          建表（幂等）
    generate(con, run_date=None, force=False)  生成当日预测 → dict
    review(con, force=False)     复盘已到期窗口 → dict
    rescore(con)                 按实盘命中率重算权重 → dict
    today_payload(con)           前端「今日推荐」载荷
    scores_payload(con)          前端「策略记分板」载荷
    track_payload(con)           前端「复盘轨迹」载荷
    backtest(days)               技术面历史回放（离线，命令行用）
"""
from __future__ import annotations

import concurrent.futures
import json
import math
import os
import sqlite3
import sys
import threading
import time
import traceback
from collections import Counter, defaultdict, deque
from datetime import datetime, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import strategy_sim as SS  # noqa: E402
import kbase as _kbase  # noqa: E402  全市场 K 线沉淀库（2026-10-25 接入，替换 285 只 JSON 缓存）

try:
    from strategy_data import STRATEGIES  # noqa: E402
except Exception:  # noqa: BLE001
    STRATEGIES = []

ENGINE_VER = "pred-v2.0"
DB_PATH = os.environ.get("VIEWER_DB") or os.path.join(ROOT, "viewer", "app.db")
KCACHE = os.path.join(ROOT, "predict_kcache.json")

# ---- 可调参数 -------------------------------------------------------------
# 2026-10-29：核心/观察池由「固定只数」改为「按当日全池分数分位带」自适应（见 _band_counts）。
# CORE_N / WATCH_N 仅作为上限兼容旧引用，不再驱动切片。
CORE_N = 10                 # [已弃用] 核心池只数，改用 CORE_MAX/CORE_MIN + 分位带
WATCH_N = 10                # [已弃用] 观察池只数，改用 WATCH_MAX + 分位带
CORE_MAX = 8                # 核心池上限（证据区间 4~7 的上沿再留 1 只余量，见 _band_counts）
CORE_MIN = 3                # 核心池下限（候选池很小时保底，不再硬凑到 10）
WATCH_MAX = 10              # 观察池上限
CORE_PCT = 0.010            # 核心池分位带：当日全池 score 的前 1%
WATCH_PCT = 0.030           # 观察池分位带：前 3%（扣掉核心池后）
# 候选池上限（= strategy_sim.universe 的 limit）。
# 2026-10-26 进哥关切「每日预测是否只扫一小撮票」：原 160 是流动性前 160，
# 会系统性漏掉流动性靠后的政策受益小票。改为环境变量可配、默认 500（覆盖更全），
# 并由 pillars.generate 进一步并入「今日热板块」官方成分股，双保险扩池。
# P2（2026-10-26）：默认上限提到 2000，且 universe() 已与干净宇宙求差 —— 重的四支柱分析覆盖
# 流动性头部 2000 只干净票；其余干净宇宙由 viewer/universe_scan 廉价全量扫描覆盖。可用
# 环境变量 PILLARS_POOL_LIMIT 进一步调大（如 5268 即全宇宙重分析）。
from viewer.config import PILLARS_POOL_LIMIT as POOL_LIMIT  # 集中配置（环境变量覆盖见 viewer/config.py）
# 2026-10-31 整改（测评 P0-D）：阈值唯一来源。eff 的 40、judge 的 500、名单的 20
# 全部收进这一个模块 —— 原来它们分散在三处，互相不知道对方存在。
from viewer.kernel import params as _PARAMS
BARS = 660                  # 每只票取多少根日线（够「近两年 2×244」回放 + MA120 / 年线）
MIN_BARS = 130              # 少于此根数的票不参与技术面判定
WINDOWS = (1, 3, 5, 10)     # 复盘窗口（交易日）：1 日 / 3 日 / 5 日 / 10 日（2026-09-29 应王总加 T+1）
MAX_SERIES = 30             # 逐日曲线最多保留多少个交易日（历史沉淀）
PRIOR_HIT = 0.5             # 贝叶斯收缩先验命中率
PRIOR_N = 20                # 先验等效样本量
W_MIN, W_MAX = 0.02, 0.34   # 单信号权重上下限（防止一路信号独大；不再用 0.06 硬兜底）
# 2026-10-29：十二路里只有技术面做过全市场因子回测（signal_backtest，单信号样本可达 76 万）。
# 有实证支撑的维度沿用 W_MAX；其余维度虽经实盘结算，但样本仅几十到几百、标准误 ±3~5pt，
# 不足以证明有效，上限收紧 —— 没被验证过的信号不该主导排序。
VERIFIED_DIMS = {"tech"}
W_MAX_UNVERIFIED = 0.25
ADJUST_LOG_EPS = 0.02       # 权重变动超过此值才记日志（防刷屏）
# 覆盖度校正（2026-10-01 新增）：命中票数少的信号说明它对候选池没代表性，
# 一律给等量权重等于把噪声当信号。故权重再乘一个「覆盖置信度」因子。
COVER_K = 30.0              # 覆盖置信度标度：命中约 30 只 → conf≈0.63；100 只 → conf≈0.96
COVER_FLOOR = 0.45          # 覆盖置信度下限保留比例（极低覆盖不至于被归零）

# ---- 动量冷却（王总 2026-10-11：选出的票系统性跑输池子，根因是「追热反转」）----
# 预测日前 5 个交易日累计涨幅越高 → 短线均值回归风险越大。实证：前 5 日已涨的票
# T+3 命中仅 16%、收益 -4.35%；前 5 日未涨的票 T+3 命中 33%、收益 -0.36%。
# 故对「热票」按超额涨幅施加冷却系数（乘到综合分上），不硬性剔除，保留少量多样性。
COOL_THR = 0.00    # 前 5 日涨幅超过此阈值开始降温（0 = 涨过的就降权）
COOL_SLOPE = 0.15  # 每超 15 个百分点涨幅，冷却系数额外 -1.0
COOL_MAX = 0.85    # 最多把综合分砍到原来的 15%（不归零，极端热票仍留一线）

# ---- 三层选股框架（2026-10-11 王总定：板块方向 → 股东结构 → 技术买点）----
# 第①层：先定「钱往哪去」——聚合 政策/海外/板块资金/情绪/跟随 五源板块热度（各源内部
#         percentile 归一后再加权），取前 TOP_BOARDS 个板块为资金方向。命中热板块 = 方向对。
# 第②层：在热板块内用「股东结构」选股——股东户数百分位分档（非常集中→发散性）+ 一级行业内
#         股东户数最小的前 30%（筹码集中易控盘）。这层不重新打分，只做加成与硬 gate。
# 第③层：技术面买点沿用现有 tech 信号 + 动量冷却。三层是「自顶向下」过滤，而非平铺加分。
TOP_BOARDS = 6              # 热度榜取前几板块作为方向（核心硬 gate 候选）
BOARD_BONUS = 0.15          # 命中热板块的软加权加成（方向对）
CONC_BONUS = {"非常集中": 0.22, "集中": 0.15, "相对集中": 0.08, "分散": 0.0, "发散性": -0.05}
HOLDER30_BONUS = 0.12       # 一级行业内股东户数最小的前 30% → 筹码集中易控盘，加成
COVER_BLENDED = 0.45        # conf 对权重的混合起点（conf=0 时仍保留 45% 先验）
# ---- 主参考窗口（王总 2026-10-06 定）---------------------------------------
# 原话：「回测以近两年（一年半~两年）的涨跌为主要参考，太早的那些参考性不大。」
# 含义：所有**用于调参 / 算权重 / 覆盖度**的统计，只吃这个窗口内的样本；
# 窗口外的样本仍然留库可查（历史沉淀、逐日曲线不受影响），只是不再参与「自我进化」。
# 注意：这是**调参口径**，不是指标口径 —— 六路/十二路信号的定义一个字不改。
LOOKBACK_YEARS = 2.0        # 主参考窗口长度（年）
MIN_LOOKBACK_YEARS = 1.5    # 下限：一年半（王总口述区间）
LOOKBACK_DAYS = 10          # 情绪/资金/跟随类信号的回看自然日
SENT_POS = 0.15             # 情绪分 > 此值算看多
SENT_NEG = -0.15            # 情绪分 < 此值算看空

def window_start(today=None) -> str:
    """主参考窗口起点（YYYY-MM-DD）：今天回退 LOOKBACK_YEARS 年。

    只用于「哪些样本算数」的判定，不参与任何指标计算。取不到日期时返回空串
    （= 不过滤），宁可不算窗口，也不许把整批样本误判过期。
    """
    try:
        import datetime as _dt
        d = today or _dt.date.today()
        return (d - _dt.timedelta(days=int(round(LOOKBACK_YEARS * 365.0)))).isoformat()
    except Exception:  # noqa: BLE001 —— 日期算不出来就当作无窗口
        return ""


def window_caliber() -> dict:
    """回传给前端的口径说明：主参考窗口多长、从哪天起、当前样本落在窗口内的有几条。"""
    ws = window_start()
    return {"years": LOOKBACK_YEARS, "min_years": MIN_LOOKBACK_YEARS,
            "start": ws, "label": ("权重只按 %s 起的近 %.1f 年样本统计"
                                   % (ws, LOOKBACK_YEARS)) if ws else ""}


_LOCK = threading.Lock()    # 生成/复盘串行化（scheduler 与手动点击可能同时来）
_STATUS: dict = {"running": False, "stage": "", "started_at": None, "last_error": None}

# ---- 十路信号元数据（顺序即前端展示顺序）----------------------------------
# ⚠️ 2026-10-31：由十二路缩为十路。删掉的两路是「论坛情绪」「选手跟随」，
#   原因不是效果不好，而是**输入源已经不存在**：论坛抓取 2026-10-30 全停
#   （见 viewer/config.INGEST_SOURCES 与 docs/论坛大V信号实证_20261030.md），
#   这两路读的 posts / stock_mentions / trades / person_scores 不再有新行。
#   留着它们的实际后果是：滚动 10 日窗口继续把 10-29 之前的旧帖当"领先信号"喂给模型，
#   越往后越是拿陈货当新信号 —— 所以宁可直接摘掉，也不让它以"衰减"的方式假装还活着。
#   sig_sentiment / sig_follow 两个函数体暂留（不删代码便于日后源恢复时一处改回），
#   但已无任何调用点。
# prior = 先验权重系数（2026-10-01 按王总要求重构：覆盖度校正 + 维度扩到十二路）
#   取值来自三件事：① 数据在库里的实际覆盖率 ② 信号本身的"可信度"（是否可被证伪）
#   ③ 是否容易被刷分（游资上榜、外部信源都属易刷，故给低 prior）
SIGNALS = [
    {"key": "tech", "name": "技术面", "icon": "◈", "kind": "signal", "prior": 1.45,
     "desc": "86 条公开策略当日触发（按流派归并），池内命中策略数最多的优先。"},
    {"key": "holders", "name": "机构结构", "icon": "◎", "kind": "signal", "prior": 0.45,
     "desc": "前十大股东里机构（is_org）家数与持股变动、股东户数环比下降（机构结构趋优）。"
             "⚠ 2026-10-11 实测：带此信号的票 T+1 平均跑输池子 168bp，先验由 1.05 降至 0.45。"},
    {"key": "ann", "name": "公告催化", "icon": "❐", "kind": "signal", "prior": 1.00,
     "desc": "近 10 日公告按类目分好坏：股权激励/回购增持/并购重组/产品突破 加分，"
             "减持/质押/退市风险/监管处罚 减分。"},
    {"key": "desk", "name": "盯盘结论", "icon": "⬢", "kind": "signal", "prior": 0.95,
     "desc": "盯盘模块当日结论：机会窗口加分、风险主导减分（含改判触发点）。"},
    {"key": "inst", "name": "机构席位", "icon": "◆", "kind": "signal", "prior": 0.50,
     "desc": "龙虎榜「机构专用」席位净买入（lhb_inst），按上榜原因分档加权；"
             "落点为 T+3~T+5（T+1 反向、涨占比 43.1%），故只作中期加分。"
             "⚠ 2026-10-11 实测：带此信号整体负贡献，先验由 0.85 降至 0.50。"},
    {"key": "policy", "name": "政策指向", "icon": "⚑", "kind": "signal", "prior": 1.00,
     "desc": "中央级政策按 sectors 映射到个股，按重要度加权（内部资料）。"
             "⚠ 2026-10-11 实测：带此信号 T+1 平均跑赢池子 191bp，先验由 0.80 升至 1.00。"},
    {"key": "board", "name": "板块资金", "icon": "⁑", "kind": "signal", "prior": 0.70,
     "desc": "所属行业板块当日主力净额与涨跌幅（板块级映射，稀疏）。"},
    {"key": "overseas", "name": "海外联动", "icon": "⊕", "kind": "signal", "prior": 0.60,
     "desc": "地缘事件直接点名的个股 + 海外舆情传导板块（稀疏但指向明确）。"},
    {"key": "extref", "name": "外部参考", "icon": "◇", "kind": "signal", "prior": 0.42,
     "desc": "外部信源（投顾/榜单/共识）看多信号，按信源层级加权；覆盖率极低故权重低。"},
    {"key": "lhb", "name": "游资上榜", "icon": "◈", "kind": "signal", "prior": 0.30,
     "desc": "龙虎榜上榜异动（lhb_stocks），按「上榜原因 + 席位属性」分档；"
             "实证 T+1 均值为负（涨幅类 +1.09%、换手 -1.10%、振幅 -2.39%、拉萨系 -2.20%），"
             "故仅作风险标记而非看多条。"},
]
SIGNAL_KEYS = [s["key"] for s in SIGNALS]
SIGNAL_META = {s["key"]: s for s in SIGNALS}
SIGNAL_PRIOR = {s["key"]: float(s.get("prior", 1.0)) for s in SIGNALS}

# 2026-10-31 新增（测评 P1「core 档没有解释」项）：tier 的**唯一定义出口**。
# 之前 tier="core" 只在这一个文件里被赋值一次，却从没有任何地方说明它是什么，
# 于是前端把它渲染成「核心」，用户读成「最值得买」。它其实只是"综合分排前 N 名"。
# 这个字典随 /api/predict 与 run 记录一起返回，界面上"core 是什么意思"只能从这里取。
TIER_DEFINITION = {
    "core": "综合分排序的前 core_k 名。这是**截断位次**，不是质量结论 —— "
            "core 与 watch 的收益差在本系统内从未被检验过，没有证据支持"
            "「core 更值得买」。",
    "watch": "入选名单中 core 之后的部分。同样不是「次优」，只是排名其后。",
    "note": "两个档位的差别仅在于排名位置；把 core 读成「核心持仓」是本系统历史上"
            "最容易被误读的一处标签。若要让它承载质量含义，必须先做"
            "「core vs watch 收益差」的检验（含多重比较校正），否则标签不该这么写。",
}


# ---- 龙虎榜校准表（L1 方法论校准层）---------------------------------------------
# 出处分两类：
#   A) 方法论 —— wb-finance-skill/references/ 下的方法论避坑条目；
#   B) 实证   —— 用库内 lhb_* 自带的 d1_chg/d5_chg 字段反推「上榜后真实股价反应」
#                （2026-10-01 实测，近 30 日样本；纯 SQL，无需 pandas）。
# 之所以要这张表：旧版 sig_lhb / sig_inst 不区分「因什么上榜」和「什么席位买的」，
# 把涨幅类、换手率类、振幅类、拉萨系一视同仁算成净买额 → 拿噪声当信号。
LHB_CALIB = {
    # 上榜触发原因 → 方向系数。实证 T+1 次日均值：涨幅类 +1.09% / 跌幅类 -0.46%
    # / 换手率类 -1.10% / 振幅类 -2.39%。故涨幅类可作正向，换手、振幅类应视为风险。
    "reason": {
        "up":       {"kw": ("涨幅", "涨幅偏离"), "f": 1.00,
                     "note": "涨幅类上榜 T+1 +1.09%"},
        "down":     {"kw": ("跌幅",),            "f": 0.25,
                     "note": "跌幅类上榜 T+1 -0.46%"},
        "turnover": {"kw": ("换手率", "换手达到"), "f": -0.60,
                     "note": "换手率上榜 T+1 -1.10%（高换手=分歧，非利好）"},
        "amplitude": {"kw": ("振幅",),           "f": -0.80,
                     "note": "振幅类上榜 T+1 -2.39%（最差）"},
    },
    # 席位属性 → 方向系数。实证（lhb_depts，近 30 日席位次日均值）：
    # 沪股通专用 +0.28% / 拉萨系(东财) -2.20% / 其它营业部 -0.23%。
    # 拉萨系印证了 leader-game.md「量化席位信号意义弱」，实盘比方法论更狠——是负的。
    "seat": {
        "north": {"kw": ("股通专用",), "f": 1.00,
                  "note": "沪股通/陆股通专用 席位次日 +0.28%"},
        "inst":  {"kw": ("机构专用",), "f": 0.80,
                  "note": "机构专用席位"},
        "lhasa": {"kw": ("拉萨", "东财", "东方财富"), "f": -0.60,
                  "note": "拉萨系/东财通道 席位次日 -2.20%"},
        "other": {"kw": (), "f": 0.00,
                  "note": "其它营业部 席位次日 -0.23%（不加不减）"},
    },
    # 机构专用席位（lhb_inst）的落点。实证 T+1 上涨占比仅 43.1%（<50%），
    # 且净买入>0 组 T+1 均值 -0.55% 而净买入<=0 组 +1.06% —— 短线是反向指标；
    # 但 T+3 +3.19% / T+5 +4.05% 是正向。本系统权重主轴是 T+5，故本路保留有效，
    # 但必须标注「落点是 T+3~T+5，不是 T+1」，避免拿次日噪声当利好。
    "inst_hold_days": (3, 5),
}
# 上榜次数极长尾（近 10 日有 9 次上榜的票）样本极小、且次日均值仍为负，
# 故同一票单次上榜只计一次强度，防止「刷榜」放大权重（实证：上榜1~3次 T+1 均为负）。
LHB_MAX_COUNT_PER_STOCK = 1

# ---- 表 ------------------------------------------------------------------
DDL = """
CREATE TABLE IF NOT EXISTS prediction_run (
  run_date     TEXT PRIMARY KEY,   -- 预测日（YYYY-MM-DD，即信号判定日）
  created_at   TEXT,
  engine_ver   TEXT,
  pool_n       INTEGER,            -- 候选池只数
  signal_n     INTEGER,            -- 有效信号数
  core_n       INTEGER,
  watch_n      INTEGER,
  market_view  TEXT,               -- 当日研判（一句话）
  style_view   TEXT,               -- 风格/主线判断
  breadth_json TEXT,               -- 各信号当日出票统计
  bench_json   TEXT,               -- 各窗口基准（候选池同期等权平均）
  status       TEXT,               -- ok | partial
  note         TEXT
);

CREATE TABLE IF NOT EXISTS prediction_item (
  run_date     TEXT NOT NULL,
  symbol       TEXT NOT NULL,
  name         TEXT,
  tier         TEXT,               -- core | watch
  rank         INTEGER,
  score        REAL,
  hit_signals  TEXT,               -- 命中信号明细 json
  reason       TEXT,
  entry_close  REAL,               -- T 日收盘（基准价）
  next_open    REAL,               -- T+1 开盘（供真实可成交性参考）
  ret_t1 REAL, ret_t3 REAL, ret_t5 REAL,
  bench_t1 REAL, bench_t3 REAL, bench_t5 REAL,
  hit_t1 INTEGER, hit_t3 INTEGER, hit_t5 INTEGER,
  caveat       TEXT,               -- 已到期的窗口全部为空 → '未到期'；有异常 → 说明
  reviewed_at  TEXT,
  mom5         REAL,               -- 预测日前 5 日累计涨幅（动量冷却用）
  cool         REAL,               -- 动量冷却系数
  board        TEXT,               -- 第①层：命中最高热度板块词（资金方向）
  conc         TEXT,               -- 第②层：股东户数百分位档位（非常集中..发散性）
  holder30     INTEGER,            -- 第②层：是否一级行业内股东户数前 30%
  PRIMARY KEY (run_date, symbol)
);
CREATE INDEX IF NOT EXISTS idx_pi_date ON prediction_item(run_date);
CREATE INDEX IF NOT EXISTS idx_pi_sym  ON prediction_item(symbol);

CREATE TABLE IF NOT EXISTS predictor_score (
  signal_key   TEXT PRIMARY KEY,
  kind         TEXT,               -- signal | school | strategy
  name         TEXT,
  school       TEXT,
  n_t1 INTEGER DEFAULT 0, n_hit_t1 INTEGER DEFAULT 0, sum_ret_t1 REAL DEFAULT 0,
  n_t3 INTEGER DEFAULT 0, n_hit_t3 INTEGER DEFAULT 0, sum_ret_t3 REAL DEFAULT 0,
  n_t5 INTEGER DEFAULT 0, n_hit_t5 INTEGER DEFAULT 0, sum_ret_t5 REAL DEFAULT 0,
  weight       REAL,
  weight_prev  REAL,
  updated_at   TEXT
);

CREATE TABLE IF NOT EXISTS predictor_adjust_log (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  log_date      TEXT,
  signal_key    TEXT,
  kind          TEXT,
  weight_before REAL,
  weight_after  REAL,
  reason        TEXT,
  created_at    TEXT
);
CREATE INDEX IF NOT EXISTS idx_pal_date ON predictor_adjust_log(log_date);

-- 近两年逐日回放（王总 2026-10-06 定：主参考窗口＝近两年）
CREATE TABLE IF NOT EXISTS predictor_replay (
  id           INTEGER PRIMARY KEY,
  window_label TEXT,            -- y2（近两年）… 与 strategy_sim_window 口径对齐
  days         INTEGER,         -- 回放交易日数
  pool         INTEGER,         -- 候选池只数
  start_date   TEXT,
  end_date     TEXT,
    scores_json  TEXT,            -- {key:{t3:{n,hit_rate,avg_ret}, t5:…, t10:…}}
    computed_at  TEXT
  );

-- 隔离机制核心表：不同外部知识源/轨道理论独立存储、独立验证、独立开关。
-- 与 predictor_score / prediction_* 零 JOIN（无任何外键/关联查询）。
CREATE TABLE IF NOT EXISTS knowledge_source (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  source_key    TEXT    NOT NULL,   -- 来源主键：某博主ID / 媒体名 / 内部轨道 doc_key（全局唯一语义）
  category      TEXT    NOT NULL,   -- technical_emotion | macro_strategy | internal_track（两类外部 + 内部轨道）
  track         TEXT,               -- 所属轨道，对齐 kb_sources.yaml 的 group/track
  name          TEXT,               -- 展示名（如「博主A·放量突破战法」）
  enabled       INTEGER DEFAULT 1,  -- 独立开关：1=启用 0=停用；停用仅置 0，绝不级联影响其它来源
  trust_score   REAL    DEFAULT 0.0,-- 该来源可信度评分（0~1）。验证结果【只写本列】，默认不回写 predictor_score.weight
  signal_key    TEXT,               -- 被信号化的目标信号（可空，如映射到已有 'extref'）
  promoted      INTEGER DEFAULT 0,  -- 是否已「提升权到信号权重」：仅当 =1（用户显式确认）才允许后续提权流程（P2）
  content_json  TEXT,               -- 论点/结论/方法 JSON 占位（P2 抓取后填充）
  updated_at    TEXT,
  created_at    TEXT
);
-- 隔离语义索引：同一来源在同一 category+track 下唯一；category / enabled 高频筛选
CREATE UNIQUE INDEX IF NOT EXISTS idx_ks_src ON knowledge_source(source_key, category, track);
CREATE INDEX IF NOT EXISTS idx_ks_cat     ON knowledge_source(category);
CREATE INDEX IF NOT EXISTS idx_ks_enabled ON knowledge_source(enabled);

-- 人工笔记表（P1）：与 learn_tracks / KB 轨道零耦合（独立表）；本期仅建表 + 占位 UI。
CREATE TABLE IF NOT EXISTS growth_notes (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  section     TEXT,               -- 归属区标记（'A' | 'D' | 'B' | 自由），仅用于轻量归类
  content     TEXT,               -- 笔记正文
  created_at  TEXT,
  updated_at  TEXT
);
CREATE INDEX IF NOT EXISTS idx_gn_sec ON growth_notes(section);
"""

_SCORE_COLS = ("kind", "name", "school",
               "n_t1", "n_hit_t1", "sum_ret_t1",
               "n_t3", "n_hit_t3", "sum_ret_t3",
               "n_t5", "n_hit_t5", "sum_ret_t5")


# ---------------------------------------------------------------- 基础设施
def connect() -> sqlite3.Connection:
    con = sqlite3.connect(DB_PATH, timeout=30)
    con.row_factory = sqlite3.Row
    try:
        con.execute("PRAGMA busy_timeout=30000")
    except Exception:  # noqa: BLE001
        pass
    return con


def ensure_tables(con: sqlite3.Connection) -> None:
    con.executescript(DDL)
    con.commit()
    _migrate(con)
    _drop_stale_signal_keys(con)
    # 新维度 / 新信号的记账行也要自愈：只删不补会让 /api/prediction/scores 只返回旧那几路
    # （2026-10-01 实测：删完 fund 后接口只剩 5 路，新 12 路的行要等下一次 generate 才出现）。
    try:
        _ensure_score_rows(con, [])
    except Exception:  # noqa: BLE001
        pass


def _migrate(con: sqlite3.Connection) -> None:
    """幂等加列迁移（SQLite 不支持 IF NOT EXISTS 的 ADD COLUMN）。"""
    cols_item = {
        "ret_t10": "REAL", "bench_t10": "REAL", "hit_t10": "INTEGER",
        "series_json": "TEXT", "mom5": "REAL", "cool": "REAL",
        "board": "TEXT", "conc": "TEXT", "holder30": "INTEGER",
        # 2026-10-12 整改 #16：T+1 开盘口径（并列对照，不动原 T 收盘口径列）
        "ret2_t1": "REAL", "ret2_t3": "REAL", "ret2_t5": "REAL", "ret2_t10": "REAL",
        "hit2_t1": "INTEGER", "hit2_t3": "INTEGER", "hit2_t5": "INTEGER", "hit2_t10": "INTEGER",
    }
    for col in (1, 3, 5, 10):
        pass  # 窗口列已随 DDL（t3/t5 旧，t10 在下面补）
    existing = {r["name"] for r in con.execute("PRAGMA table_info(prediction_item)").fetchall()}
    for col, typ in cols_item.items():
        if col not in existing:
            try:
                con.execute("ALTER TABLE prediction_item ADD COLUMN %s %s" % (col, typ))
            except Exception as e:  # noqa: BLE001 —— P2-02：精确吞「列已存在」，其余异常照常抛出
                if "duplicate column" in str(e).lower() or "already exists" in str(e).lower():
                    pass
                else:
                    raise
    existing = {r["name"] for r in con.execute("PRAGMA table_info(predictor_score)").fetchall()}
    for col in ("n_t10", "n_hit_t10", "sum_ret_t10"):
        if col not in existing:
            try:
                con.execute("ALTER TABLE predictor_score ADD COLUMN %s REAL" % col)
            except Exception as e:  # noqa: BLE001 —— P2-02：精确吞「列已存在」，其余异常照常抛出
                if "duplicate column" in str(e).lower() or "already exists" in str(e).lower():
                    pass
                else:
                    raise
    con.commit()


def _today() -> str:
    # P2-06: 单点替换 naive datetime.now() → 时区感知的 CST 时间，避免跨时区错乱
    try:
        from timeutil import today_cst
        return today_cst()
    except Exception:  # noqa: BLE001 —— timeutil 不可用时回退 naive（保持旧行为）
        return datetime.now().strftime("%Y-%m-%d")


def _latest_trade_date_anchor() -> str:
    """预测 / K 线刷新锚点：动态计算「真实最近【已收盘】交易日」，绝不依赖缓存旧日期或裸 today()。

    硬保证（用户原话：每天预测和抓取的数据都该是实时和当天的，拿当天去预测，
    不是昨天的、也不是上个月的）：
      - 收盘后（>=15:00）：锚点 = auction.latest_trade_date()（含今日，若今日是交易日且已收盘）。
      - 盘中 / 盘前（<15:00）：锚点 = auction.latest_trade_date(昨日)（严格早于今日，
        避免用半根 K 线预测今日 —— 今日这根 K 线收盘前会变）。
      - 取不到时退回 _today()（极端兜底，绝不用缓存里的旧日期）。
    auction.latest_trade_date 已叠加 2026 确定性交易日历兜底（国庆 10/01–10/07 等），
    即使 Sina K 线源未刷新也能正确识别刚过去的交易日。
    """
    from auction import latest_trade_date as _au_ltd
    now = datetime.now()
    if now.hour >= 15:
        td = _au_ltd()
    else:
        y = (now - timedelta(days=1)).strftime("%Y-%m-%d")
        td = _au_ltd(y) or _au_ltd()
    return td or _today()


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _f(x, d=None):
    try:
        v = float(x)
        if math.isnan(v) or math.isinf(v):
            return d
        return v
    except (TypeError, ValueError):
        return d


def _pct_le(sorted_vals: list, x) -> float:
    """x 在已排序列表 sorted_vals 中的百分位（0~1，含等号 = 小于等于 x 的占比）。

    P1-16 语义修正：原实现用严格小于（`<`），返回的是「小于 x 的占比」，与函数名
    pct_le（percentile ≤）不符；现改为「≤ x 的占比」，与命名及下游「归一化到 0~1」口径一致。
    """
    if not sorted_vals:
        return 0.5
    lo, hi = 0, len(sorted_vals)
    while lo < hi:
        mid = (lo + hi) // 2
        if sorted_vals[mid] <= x:
            lo = mid + 1
        else:
            hi = mid
    return lo / len(sorted_vals)


def status_snapshot() -> dict:
    return dict(_STATUS)


# ---------------------------------------------------------------- K 线缓存
def load_kcache() -> dict:
    """返回 {updated, bars}。

    2026-10-25 重构：原 predict_kcache.json（仅 285 只按需缓存）已升级为
    viewer/kbase.db（全 A 股 ~5588 只日 K 沉淀，SQLite，lazy 读取）。
    bars 现为 KBaseBars 惰性代理，对外用法与原 JSON dict 完全兼容
    （.get / [] / items / values / len / 赋值），所有消费方无需改动。
    """
    base = _kbase.get_base()
    return {"updated": base.updated(), "bars": _kbase.KBaseBars(base)}


def save_kcache(d: dict) -> None:
    """仅持久化 updated 标记到 kbase meta。

    bars 已通过 KBaseBars 实时落库（SQLite），不再整体 dump 到 JSON——
    避免全市场 5588 只一次性载入内存（内存最小）。
    """
    try:
        base = _kbase.get_base()
        if d and d.get("updated") is not None:
            base.set_updated(d["updated"])
    except Exception as e:  # noqa: BLE001
        print("[predictor] kcache meta 写入失败:", str(e)[:100], flush=True)


_LB_CACHE: dict = {"key": None, "data": {}}


def _local_bars(codes: list, want: int = BARS) -> dict:
    """从本地日线全库（data/kline_daily.db，1707 万行、后复权）读最近 want 根 K 线。

    2026-10-30 新增（BUG-20261030 修复配套）。原 fetch_bars 对每只票都走新浪
    网络（datalen=660）；候选池被锁在 610 只时勉强够用，候选池修正为全市场
    5000+ 只后网络抓取必然限流。本地库与新浪 hfq 逐字段一致（000001/600519/
    300750 三票核对，且本地多当天一根），故历史统一走本地，网络只补缺的根数。
    """
    out: dict = {}
    want_codes = set(codes or [])
    if not want_codes:
        return out
    db = getattr(SS, "KLINE_DB", None)
    if not db or not os.path.exists(db):
        return out
    try:
        con = sqlite3.connect("file:%s?mode=ro" % db, uri=True)
        try:
            last = con.execute("SELECT MAX(d) FROM kline").fetchone()[0]
            if not last:
                return out
            ck = (str(last), frozenset(want_codes), int(want))
            if _LB_CACHE.get("key") == ck and _LB_CACHE.get("data"):
                return _LB_CACHE["data"]
            start = con.execute(
                "SELECT MIN(d) FROM (SELECT DISTINCT d FROM kline "
                "WHERE d<=? ORDER BY d DESC LIMIT ?)",
                (last, int(want))).fetchone()[0]
            cur = con.execute(
                "SELECT code,d,o,h,l,c,v FROM kline "
                "WHERE d>=? AND d<=? AND c>0 ORDER BY code,d", (start, last))
            cc, buf = None, []
            for code, d, o, h, l, c, v in cur:
                code = str(code)
                if code not in want_codes:
                    continue
                if code != cc:
                    if buf:
                        out[cc] = buf
                    cc, buf = code, []
                ds = str(d)
                buf.append({"d": "%s-%s-%s" % (ds[:4], ds[4:6], ds[6:8]),
                            "o": float(o or 0), "h": float(h or 0),
                            "l": float(l or 0), "c": float(c or 0),
                            "v": float(v or 0)})
            if buf:
                out[cc] = buf
        finally:
            con.close()
    except Exception as e:  # noqa: BLE001
        print("[predictor] 本地 K 线读取失败（降级走网络）：%s" % str(e)[:120], flush=True)
        return {}
    _LB_CACHE["key"] = (str(last), frozenset(want_codes), int(want))
    _LB_CACHE["data"] = out
    return out


def fetch_bars(codes: list, kc: dict, force: bool = False,
               progress=None) -> dict:
    """按需抓取 K 线：本地日线全库打底，网络只补本地缺的那几根。

    返回 {code: [bar, ...]}；bar = {d,o,h,l,c,v}。
    """
    bars = kc.setdefault("bars", {})
    today = _today()
    # 2026-10-30：新鲜度判断改锚点为「最近已收盘交易日」。
    # 原用裸 today()：非交易日早上（如周六/节假日/开盘前）全池都会被判成
    # "缺当天" → 5000 只全量重抓，既慢又撞限流。锚点由交易日历给出，天然正确。
    try:
        anchor = _latest_trade_date_anchor() or today
    except Exception:  # noqa: BLE001
        anchor = today
    out, need = {}, []
    local = _local_bars(list(codes))
    for c in codes:
        # 本地优先：本地命中就完全不碰 kc。
        # ⚠️ 不能写成 bars.get(c) first —— kc 是 kbase 包装，get() 是逐只查库，
        # 5000 只 × 查库会把整轮预测拖到分钟级甚至超时（2026-10-30 实测）。
        lv = local.get(c)
        v = lv if lv else (bars.get(c) or [])
        # ⚠️ 不写回 kc：__setitem__ 会落库（set_bars），只读打开的 kbase.db 直接抛错。
        if v and str(v[-1].get("d", ""))[:10] >= anchor:
            out[c] = v
        elif (not force and v and kc.get("updated") == today
              and datetime.now().hour < 15):
            # ⚠️ "今天抓过就跳过"只允许发生在**盘中**（hour<15）。
            # 旧版无时段限制：盘中任何一次触发（重启/手动/预览）抓到的日线只有到
            # 昨日的完整 bar，却把 updated 标成今天 → 15:05 收盘任务复用盘中旧
            # K 线，交易日众数停在昨日，当日预测永不生成（2026-09-29/30 连续两天复现）。
            # 收盘后（>=15 点）必须看到 v[-1].d >= today（第一条短路）才算新鲜，否则重抓。
            out[c] = v
        else:
            need.append(c)
    if need:
        lock = threading.Lock()
        done = [0]
        ok_cnt = [0]

        def one(code):
            try:
                base = local.get(code) or bars.get(code) or []
                # 本地已有足够历史 → 只补缺的几根。2026-10-30：原全量 660 根
                # × 5000 只必然被新浪限流；本地已覆盖 99%+ 历史。
                # 只补 2 根（anchor 当天 + 前一日），把单请求体积压到最小。
                want = BARS if len(base) < 130 else 2
                v2 = SS.fetch_kline(code, want)
                if v2 and base:
                    byd = {str(b.get("d", ""))[:10]: b for b in base}
                    for b in v2:
                        byd[str(b.get("d", ""))[:10]] = b
                    v = [byd[k] for k in sorted(byd)][-BARS:]
                else:
                    v = v2
            except Exception:  # noqa: BLE001
                v = []
            with lock:
                done[0] += 1
                if v:
                    ok_cnt[0] += 1
                if progress and done[0] % 20 == 0:
                    progress(done[0], len(need))
            return code, v

        # 并发 8 → 16（2026-10-30）：候选池从 610 扩到 5000+ 后，缺当天数据的日子
        # 要补的票数涨了 8 倍。实测 200 只：8 并发 1.6s / 16 并发 0.74s，16 是拐点，
        # 再往上收益递减且更容易触发新浪限流，故取 16。
        with __import__("concurrent.futures").futures.ThreadPoolExecutor(max_workers=16) as ex:
            for code, v in ex.map(one, need):
                if v:
                    # kbase 可能以只读方式打开（set_bars 会 DELETE+INSERT）→ 落库失败
                    # 不得阻断预测本身，本轮数据已在 out 里。
                    try:
                        bars[code] = v
                    except Exception:  # noqa: BLE001
                        pass
                    out[code] = v
        # ⚠️ 只有"确实抓到了数据"才把 updated 标成今天。
        # 旧版无条件写 updated=today → 全部抓失败的那天（如新浪限流）会被当成
        # "今天已经抓过了"，此后每次都复用旧 K 线，预测日永远停在旧交易日。
        if ok_cnt[0] >= max(1, len(need) // 2):
            kc["updated"] = today
            save_kcache(kc)
        else:
            print("[predictor] fetch_bars: 本轮抓取成功率过低（%d/%d），不更新缓存标记，下次重试"
                  % (ok_cnt[0], len(need)), flush=True)
        # 覆盖度留痕（2026-10-30）：本地命中 / 需网络补 / 补成功，供审计与前端判断
        # 「这一天到底是全市场数据还是残缺数据」。写 meta 失败不影响主流程。
        _cov = {"local_hit": len(codes) - len(need), "net_need": len(need),
                "net_ok": ok_cnt[0], "anchor": str(anchor)}
        try:
            con = sqlite3.connect(DB_PATH, timeout=15)
            con.executemany(
                "INSERT INTO meta(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                [("predictor_bars_" + k, str(v)) for k, v in _cov.items()])
            con.commit()
            con.close()
        except Exception:  # noqa: BLE001
            pass
        print("[predictor] fetch_bars 覆盖：本地 %d / 网络补 %d（成功 %d）· 锚点 %s"
              % (_cov["local_hit"], _cov["net_need"], _cov["net_ok"], _cov["anchor"]),
              flush=True)
    return out


def _trade_date_of(bars_map: dict) -> str | None:
    """候选池 K 线的最后一根日期（取众数，避免个别停牌票拖后腿）。"""
    cnt = Counter()
    for v in bars_map.values():
        if v:
            cnt[str(v[-1].get("d", ""))[:10]] += 1
    return cnt.most_common(1)[0][0] if cnt else None


# ---------------------------------------------------------------- 信号：技术面
# ---- 策略门控（王总 2026-10-06 定：按「近两年」回测取舍技术面策略）------------------
# 口径说明：六路 / 十二路信号的定义一个字不改，只改「哪些策略能进技术面信号」。
# 判定依据全部来自 strategy_sim_window 表的主参考窗口（STRAT_GATE_WINDOW = 近两年回测结果）。
STRAT_GATE_ENABLED = True
STRAT_GATE_WINDOW = "mid"          # 与 strategy_sim.MAIN_WINDOW_LABEL 对齐
STRAT_GATE_MIN_TRADES = 30         # 近两年交易次数下限：低于此视为样本不足 → 剔除
STRAT_GATE_MIN_TOTAL_RET = 0.0     # 近两年总收益下限（百分数）：为负即近两年跑不动 → 剔除
STRAT_GATE_DROP_RATING = "较差"    # 近两年评级达到该档及以下的 → 剔除
STRAT_GATE_STATE = {"kept": 0, "dropped": 0, "drop": []}   # 供日志 / 接口读取


def _sim_window_rows() -> dict:
    """读 strategy_sim_window 主参考窗口的各策略回测结果 → {strategy_key: Row}。

    读不到（无回测记录 / 表不存在 / 窗口为空）时返回空 dict，调用方按「全量策略」降级运行，
    绝不因为回测数据缺失而误杀策略。
    """
    if not STRAT_GATE_ENABLED:
        return {}
    try:
        if not os.path.exists(DB_PATH):
            return {}
        con = sqlite3.connect("file:%s?mode=ro" % DB_PATH.replace("\\", "/"), uri=True)
        con.row_factory = sqlite3.Row
        rows = con.execute("SELECT * FROM strategy_sim_window WHERE window_label=?",
                           (STRAT_GATE_WINDOW,)).fetchall()
        con.close()
        return {r["strategy_key"]: r for r in rows}
    except Exception as exc:  # noqa: BLE001
        print("[predictor] 读策略回测窗口失败，本轮按全量策略运行：%s" % exc, flush=True)
        return {}


def _gate_strategies(strat_list: list) -> list:
    """按近两年回测结果过滤策略：跑不动 / 样本不足 / 评级差的，从技术面信号里剔掉。"""
    sim = _sim_window_rows()
    STRAT_GATE_STATE.update({"kept": 0, "dropped": 0, "drop": []})
    if not sim:
        print("[predictor] 近两年回测窗口为空，本轮不放门控（全量策略）", flush=True)
        return strat_list
    keep, dropped = [], []
    for s in strat_list:
        r = sim.get(s.get("key"))
        if r is None:
            keep.append(s)          # 没跑过回测的不擅自剔除
            continue
        why = []
        tr, ret = r["trades"], r["total_return"]
        rating = (r["rating"] or "")
        if tr is not None and tr < STRAT_GATE_MIN_TRADES:
            why.append("近两年仅 %s 次交易(< %d)" % (tr, STRAT_GATE_MIN_TRADES))
        if ret is not None and ret < STRAT_GATE_MIN_TOTAL_RET:
            why.append("近两年总收益 %.2f%%(<0)" % ret)
        if rating == STRAT_GATE_DROP_RATING:
            why.append("近两年评级「较差」")
        if why:
            dropped.append((s.get("key"), "; ".join(why)))
        else:
            keep.append(s)
    STRAT_GATE_STATE["kept"] = len(keep)
    STRAT_GATE_STATE["dropped"] = len(dropped)
    STRAT_GATE_STATE["drop"] = dropped
    if dropped:
        print("[predictor] 近两年窗口策略门控：保留 %d 条，剔除 %d 条" %
              (len(keep), len(dropped)), flush=True)
        for k, w in dropped:
            print("   - %-26s %s" % (k, w), flush=True)
    return keep


def _load_strategies() -> list:
    out = []
    for s in STRATEGIES or []:
        if not s.get("backtestable"):
            continue
        if s.get("scope_limited"):
            continue
        if s.get("needs"):            # 需要指数/宽度等额外上下文的，本轮跳过（择时类）
            continue
        out.append(s)
    return _gate_strategies(out)


def sig_tech(bars_map: dict, strat_list: list, progress=None) -> dict:
    """技术面：每票跑全部策略，取「最后一根 K 线触发买入」的。

    返回 {code: {"strength": hit_n, "schools": [...], "strategies": [key...]}}
    """
    res: dict = {}
    n = len(bars_map)
    for idx, (code, k) in enumerate(bars_map.items()):
        if len(k) < MIN_BARS:
            continue
        hits, schools = [], set()
        for s in strat_list:
            key = s.get("key")
            try:
                buy, _sell = SS.build_signals(key, k, code=code, name="")
            except Exception:  # noqa: BLE001
                continue
            if buy and buy[-1]:
                hits.append(key)
                schools.add(s.get("school") or "其它")
        if hits:
            res[code] = {"strength": len(hits), "strategies": hits,
                         "schools": sorted(schools)}
        if progress and (idx + 1) % 20 == 0:
            progress(idx + 1, n)
    return res


def sig_tech_replay(k: list, strat_list: list) -> dict:
    """历史回放用：一次算完整序列，返回 {strategy_key: [bool,...]}（长度 = len(k)）。

    避免按日重算 —— build_signals 本就返回整段序列，直接复用。
    """
    out: dict = {}
    for s in strat_list:
        key = s.get("key")
        try:
            buy, _sell = SS.build_signals(key, k)
        except Exception:  # noqa: BLE001
            continue
        out[key] = buy
    return out


# ---------------------------------------------------------------- 信号：其它五路
def sig_sentiment(con: sqlite3.Connection, asof: str | None = None) -> dict:
    """论坛情绪：近 LOOKBACK_DAYS 天，某票被提及次数 + 关联帖平均情绪分。

    strength = 提及次数 × (1 + 情绪分)，情绪为负则打折。

    asof：数据隔离边界（P1-01）。给定 asof 时，回看窗口以 asof 为锚点
    （since = asof - LOOKBACK_DAYS 天），避免用到未来帖；为 None 时退化为「今天」锚点。
    """
    if asof is None:
        since = (datetime.now() - timedelta(days=LOOKBACK_DAYS)).strftime("%Y-%m-%d")
    else:
        try:
            since = (datetime.strptime(asof[:10], "%Y-%m-%d")
                     - timedelta(days=LOOKBACK_DAYS)).strftime("%Y-%m-%d")
        except Exception:  # noqa: BLE001
            since = (datetime.now() - timedelta(days=LOOKBACK_DAYS)).strftime("%Y-%m-%d")
    rows = con.execute(
        "SELECT m.symbol, m.symbol_name, p.sentiment, p.sentiment_score "
        "FROM stock_mentions m LEFT JOIN posts p ON p.id = m.post_id_ref "
        "WHERE p.post_time >= ? AND m.symbol IS NOT NULL AND m.symbol != ''",
        (since,)).fetchall()
    agg: dict = {}
    for r in rows:
        sym = str(r["symbol"]).strip()
        if len(sym) != 6 or not sym.isdigit():
            continue
        a = agg.setdefault(sym, {"n": 0, "sum": 0.0, "cnt": 0, "name": None})
        a["n"] += 1
        a["name"] = a["name"] or (r["symbol_name"] or None)
        sc = _f(r["sentiment_score"])
        if sc is not None:
            a["sum"] += sc
            a["cnt"] += 1
    out: dict = {}
    for sym, a in agg.items():
        avg = (a["sum"] / a["cnt"]) if a["cnt"] else 0.0
        # sentiment_score 实测约 -14~3，直接按该口径归一（不再假设 0~100）
        tone = 1.0 + max(-1.0, min(1.0, avg / 4.0))
        strength = a["n"] * tone
        if strength <= 0:
            continue
        ev = ["近%d日提及 %d 次" % (LOOKBACK_DAYS, a["n"])]
        if a["cnt"]:
            ev.append("情绪%+.2f" % avg)
        out[sym] = {"strength": strength, "evidence": ev, "name": a["name"]}
    return out


def _lhb_reason_tier(reason: str) -> tuple[str, float]:
    """龙虎榜「上榜原因」→ (档位 key, 方向系数)。查不到原因时按中性 0.3。

    依据见 LHB_CALIB["reason"]（库内 d1_chg 后验 + leader-game.md）。
    """
    r = str(reason or "")
    for k, v in LHB_CALIB["reason"].items():
        for kw in v["kw"]:
            if kw in r:
                return k, v["f"]
    return "unknown", 0.30


def _lhb_seat_factor(names: str) -> tuple[str, float]:
    """席位名 → (档位 key, 方向系数)。names 可含多个逗号分隔席位（lhb_depts.org_name）。

    依据见 LHB_CALIB["seat"]。拉萨系/东财通道实证次日均值 -2.20%，是明确负向。
    """
    n = str(names or "")
    for k, v in LHB_CALIB["seat"].items():
        if not v["kw"]:
            continue
        for kw in v["kw"]:
            if kw in n:
                return k, v["f"]
    return "other", LHB_CALIB["seat"]["other"]["f"]


def sig_inst(con: sqlite3.Connection, asof: str | None = None) -> dict:
    """龙虎榜「机构专用」席位（lhb_inst）：近 LOOKBACK_DAYS 天机构净买入。

    为什么单独成一路：2026-10-01 王总指出「龙虎榜很多查不到资金、且很多是模拟的、参考性不强」。
    实测口径 —— 近 10 日 lhb_stocks 有 487 行、net_amt 缺失仅 0.4%，所以"查不到"不是数据缺失，
    而是 `if net <= 0: continue` 把净卖出的全丢了；而"参考性不强"是可以量化的：
    近 10 日 lhb_depts 有 3123 条营业部席位行，拉萨系/方新侠/章盟主这类席位高度同质，
    同一批游资反复上榜，"上榜次数"基本等于噪声。
    但龙虎榜里有一块刷不了 —— **机构专用席位**（lhb_inst，近 10 日 259 行、池内覆盖 64 票），
    它必须真金白银下单。故拆成两路：本函数（机构，硬钱）+ sig_lhb（游资，噪声级）。

    asof：数据隔离边界，只统计 trade_date <= asof。
    """
    since = (datetime.now() - timedelta(days=LOOKBACK_DAYS)).strftime("%Y-%m-%d")
    if asof:
        try:
            since = (datetime.strptime(asof, "%Y-%m-%d")
                     - timedelta(days=LOOKBACK_DAYS)).strftime("%Y-%m-%d")
        except ValueError:
            asof = None
    sql = ("SELECT security_code, security_name, trade_date, net_buy_amt, ratio, buy_times, "
           "reason FROM lhb_inst WHERE trade_date >= ?")
    args = [since]
    if asof:
        sql += " AND trade_date <= ?"
        args.append(asof)
    try:
        rows = con.execute(sql, args).fetchall()
    except Exception:  # noqa: BLE001
        return {}
    agg: dict = {}
    for r in rows:
        sym = str(r["security_code"] or "").strip()
        if len(sym) != 6 or not sym.isdigit():
            continue
        net = _f(r["net_buy_amt"], 0.0) or 0.0
        if net <= 0:
            continue
        ratio = _f(r["ratio"], 0.0) or 0.0
        a = agg.setdefault(sym, {"net": 0.0, "n": 0, "name": None, "ratio": 0.0,
                                 "graded": {}, "raw": 0.0})
        tier, f = _lhb_reason_tier(r["reason"])
        a["graded"][tier] = a["graded"].get(tier, 0.0) + net
        a["raw"] += net
        a["net"] += net
        a["n"] += 1
        a["ratio"] = max(a["ratio"], abs(ratio))
        a["name"] = a["name"] or (r["security_name"] or None)
    out: dict = {}
    for sym, a in agg.items():
        # 按上榜原因分档加权：涨幅类可作正向，换手/振幅类是风险（实证 T+1 为负）。
        # leader-game.md：「假机构席位容易误导」「量化席位信号意义弱」——
        # 但机构专用是真硬钱，故只做口径校正而非整体否决。
        w_net = 0.0
        for tier, net_t in a["graded"].items():
            f = LHB_CALIB["reason"].get(tier, {}).get("f", 0.30)
            w_net += (net_t / 1e8) * f
        # ratio（机构买入占该股成交比）越大越硬 —— 机构真金砸盘 vs 顺手买一点，含金量差很多
        strength = w_net * (1.0 + min(a["ratio"], 60.0) / 100.0)
        good = [k for k in a["graded"] if LHB_CALIB["reason"].get(k, {}).get("f", 0) > 0]
        ev = ["近%d日机构专用席位%d次" % (LOOKBACK_DAYS, a["n"]),
              "原始净买 %+.2f 亿" % (a["raw"] / 1e8),
              "加权后 %+.2f 亿" % w_net,
              "占成交 %.1f%%" % a["ratio"],
                                 "榜单类型：" + ("/".join(good) if good else "无正向类型"),
                                 "落点 T+3~T+5（T+1 涨占比仅 43.1%、净买>0 组 T+1 -0.55% "
                                 "而净买<=0 组 +1.06%，短线为反向）"]
        out[sym] = {"strength": strength, "evidence": ev, "name": a["name"]}
    return out


def sig_lhb(con: sqlite3.Connection, asof: str | None = None) -> dict:
    """龙虎榜营业部 / 游资席位：近 LOOKBACK_DAYS 天上榜净买入（lhb_stocks 口径）。

    权重刻意压到噪声级（prior=0.30）：席位同质化、同一批游资反复上榜、"上榜次数"不是有效强度。
    只保留给"异动识别"用，不承担主要打分。
    """
    since = (datetime.now() - timedelta(days=LOOKBACK_DAYS)).strftime("%Y-%m-%d")
    if asof:
        try:
            since = (datetime.strptime(asof, "%Y-%m-%d")
                     - timedelta(days=LOOKBACK_DAYS)).strftime("%Y-%m-%d")
        except ValueError:
            asof = None
    sql = ("SELECT security_code, security_name, trade_date, net_amt, reason "
           "FROM lhb_stocks WHERE trade_date >= ?")
    args = [since]
    if asof:
        sql += " AND trade_date <= ?"
        args.append(asof)
    try:
        rows = con.execute(sql, args).fetchall()
    except Exception:  # noqa: BLE001
        return {}
    # 席位属性：lhb_stocks 只有 buy_seat/sell_seat（数字），席位名在 lhb_depts。
    # 取每票最近一次上榜时的席位明细做分层。
    seat_map: dict = {}
    try:
        seat_sql = (
            "SELECT d.security_code, d.org_name FROM lhb_depts d JOIN ("
            "  SELECT security_code, MAX(trade_date) md FROM lhb_depts "
            "  WHERE trade_date >= ? GROUP BY security_code"
            ") t ON d.security_code = t.security_code AND d.trade_date = t.md"
        )
        seat_args = [since]
        if asof:
            seat_sql += " AND d.trade_date <= ?"
            seat_args.append(asof)
        for r in con.execute(seat_sql, seat_args):
            seat_map[str(r[0] or "").strip()] = str(r[1] or "")
    except Exception:  # noqa: BLE001
        seat_map = {}

    agg: dict = {}
    for r in rows:
        sym = str(r["security_code"] or "").strip()
        if len(sym) != 6 or not sym.isdigit():
            continue
        net = _f(r["net_amt"], 0.0) or 0.0
        if net <= 0:
            continue
        tier, f_reason = _lhb_reason_tier(r["reason"])
        seat_key, f_seat = _lhb_seat_factor(seat_map.get(sym, ""))
        # 单次上榜只计一次强度（取该票各次里加权后最高的那次）：
        # 实证近 10 日同票上榜 2~9 次的次日均值几乎全为负（1次 -1.72% … 7次 -3.56%），
        # 所谓"上榜次数"基本是噪声，按次数累加等于给刷榜票加权。
        a = agg.setdefault(sym, {"n": 0, "name": None, "raw": 0.0, "best": 0.0,
                                 "tier": tier, "seat": seat_key})
        val = (net / 1e8) * f_reason * (1.0 + f_seat)
        if val > a["best"]:
            a["best"] = val
            a["tier"], a["seat"] = tier, seat_key
        a["raw"] += net
        a["n"] += 1
        a["name"] = a["name"] or (r["security_name"] or None)
    out: dict = {}
    for sym, a in agg.items():
        out[sym] = {"strength": a["best"],
                    "evidence": ["近%d日上榜%d次（取加权最高的一次）" % (LOOKBACK_DAYS, a["n"]),
                                 "原始净买 %+.2f 亿" % (a["raw"] / 1e8),
                                 "榜单类型：" + a["tier"] + "（" +
                                 LHB_CALIB["reason"].get(a["tier"], {}).get("note", "未知"),
                                 "席位：" + a["seat"] + "（" +
                                 LHB_CALIB["seat"].get(a["seat"], {}).get("note", "未知"),
                                 "→ 该档 T+1 实证为负，仅作异动/风险标记，不作看多依据"],
                    "name": a["name"]}
    return out


def _industry_index(con: sqlite3.Connection) -> dict:
    """板块词 → 个股集合。stock_org.industry 是「一级-二级-三级」三段式，
    拆开建反查，政策/海外/板块资金的板块词才能落到个股上。"""
    idx: dict = {}
    try:
        for r in con.execute(
                "SELECT code, industry FROM stock_org "
                "WHERE industry IS NOT NULL AND industry != ''"):
            code = str(r["code"] or "").strip().zfill(6)
            if not code.isdigit():
                continue
            for part in str(r["industry"]).split("-"):
                part = part.strip()
                if part:
                    idx.setdefault(part, set()).add(code)
    except Exception:  # noqa: BLE001
        pass
    return idx


def _expand_sector_words(idx: dict, words: list) -> set:
    """板块词 → 个股（双向包含匹配：'传媒' 命中 '文化传媒-影视动漫-影视'，反之亦然）。"""
    hit: set = set()
    for w in words:
        w = str(w).strip()
        if not w:
            continue
        base = w.split("(")[0].rstrip("、").strip()
        for part, codes in idx.items():
            if base in part or part in base:
                hit |= codes
    return hit


def _hot_sectors(con: sqlite3.Connection, asof: str | None = None) -> tuple:
    """第①层 板块方向：聚合多源板块热度 → 板块热度榜（2026-10-11 王总定）。

    来源（均按「板块词→个股」反查，映射到候选池口径）：
      - policy_news.sectors（政策指向，importance×level 加权）
      - overseas_hotspots.domestic_sectors（海外传导板块，heat 加权）
      - board_flow_samples(kind='industry', main_net>0)（板块资金主力净额）
      - 论坛情绪 sentiment / 选手跟随 follow（per-symbol 强度按板块反聚合）
    各源原始值量级差异巨大（main_net 可达千亿、importance 仅 1~5），故每个源内部先
    percentile 归一到 0~1，再按源权重求和，得到跨源可比的板块热度。
    返回 (hot_boards, sym_board)：
      - hot_boards: 热度降序的 [(word, score), ...]（全量，调用方取前 N）
      - sym_board:  {sym: (word, score)} 个股命中的最高热度板块
    """
    idx = _industry_index(con)
    sym_words: dict = {}
    for w, cs in idx.items():
        for c in cs:
            sym_words.setdefault(c, set()).add(w)
    pol, ovs, bf = {}, {}, {}
    # P1-01: 板块热度三源均按 asof 截断 + 5 日窗口（[asof-5d, asof]），杜绝前视偏差。
    # asof 为 None 时退化为全量（保持旧行为）。各源日期列：
    #   policy_news.pub_time / overseas_hotspots.first_seen(回退 updated_at) / board_flow_samples.trade_date
    if asof is not None:
        pol_win = (" WHERE date(pub_time) <= date(?) "
                   "AND date(pub_time) >= date(?, '-5 day')")
        ovs_win = (" WHERE date(COALESCE(first_seen, updated_at)) <= date(?) "
                   "AND date(COALESCE(first_seen, updated_at)) >= date(?, '-5 day')")
        bf_win = (" AND date(trade_date) <= date(?) "
                  "AND date(trade_date) >= date(?, '-5 day')")
        pol_p, ovs_p, bf_p = (asof, asof), (asof, asof), (asof, asof)
    else:
        pol_win, ovs_win, bf_win = "", "", ""
        pol_p = ovs_p = bf_p = ()
    for r in con.execute(
            "SELECT sectors, importance, level FROM policy_news" + pol_win, pol_p):
        try:
            ws = json.loads(r["sectors"] or "[]")
        except Exception:  # noqa: BLE001
            ws = []
        if not isinstance(ws, list):
            continue
        imp = float(r["importance"] or 1.0)
        lvl = 1.5 if str(r["level"] or "") == "中央" else 1.0
        for w in ws:
            pol[str(w)] = pol.get(str(w), 0.0) + imp * lvl
    for r in con.execute(
            "SELECT domestic_sectors, heat FROM overseas_hotspots" + ovs_win, ovs_p):
        try:
            ws = json.loads(r["domestic_sectors"] or "[]") if r["domestic_sectors"] else []
        except Exception:  # noqa: BLE001
            ws = []
        if isinstance(ws, list):
            for w in ws:
                ovs[str(w)] = ovs.get(str(w), 0.0) + float(r["heat"] or 1.0)
    for r in con.execute(
            "SELECT board_name, main_net FROM board_flow_samples "
            "WHERE kind='industry' AND main_net>0" + bf_win, bf_p):
        bn = str(r["board_name"])
        bf[bn] = bf.get(bn, 0.0) + float(r["main_net"] or 0)
    # 2026-10-31：sen / fol 两源（论坛提及情绪、选手成交）随论坛停抓一并置空。
    #   只留 pol（政策）/ ovs（海外传导）/ bf（板块主力资金）三个**仍在日更**的源。
    def _norm(contrib):
        if not contrib:
            return {}
        vals = sorted(contrib.values())
        return {w: _pct_le(vals, v) for w, v in contrib.items()}

    npol, novs, nbf = _norm(pol), _norm(ovs), _norm(bf)
    # 政策/板块资金权重高（直接资金/政策信号）
    src_w = {"pol": 1.0, "ovs": 0.8, "bf": 1.0}
    heat: dict = {}
    for src_name, nmap in (("pol", npol), ("ovs", novs), ("bf", nbf)):
        for w, v in nmap.items():
            heat[w] = heat.get(w, 0.0) + v * src_w[src_name]
    hot_boards = sorted(heat.items(), key=lambda x: -x[1])
    sym_board: dict = {}
    for sym, ws in sym_words.items():
        best = None
        for w in ws:
            if w in heat and (best is None or heat[w] > best[1]):
                best = (w, heat[w])
        if best:
            sym_board[sym] = best
    return hot_boards, sym_board


def _share_conc(con: sqlite3.Connection, codes: list) -> tuple:
    """第②层 股东结构量化（2026-10-11 王总定）：股东分布分级 + 板块内前 30% 户数最小。

    返回 (conc_map, holder30_set)：
      - conc_map: {sym: (tier, pct_rank)} 池内按股东户数百分位分 5 档
          （非常集中=最小20% … 发散性=最大20%）。户数少=筹码集中=易控盘，
          贴合王总「股东数相对较小」原话——非前十大持股占比（A股普遍>60%无区分度）。
      - holder30_set: 一级行业内股东户数最小的前 30%（板块内筹码最集中）。
    """
    pool = set(str(c).strip().zfill(6) for c in codes if str(c).strip().isdigit())
    hn: dict = {}
    for r in con.execute(
            "SELECT symbol, end_date, holder_total_num FROM stock_holder_num "
            "WHERE symbol IS NOT NULL AND holder_total_num IS NOT NULL"):
        sym = str(r["symbol"] or "").strip().zfill(6)
        if sym not in pool:
            continue
        hn.setdefault(sym, []).append((str(r["end_date"] or ""), float(r["holder_total_num"])))
    hn_latest: dict = {}
    for sym, lst in hn.items():
        lst.sort()
        hn_latest[sym] = lst[-1][1]
    have = [c for c in pool if c in hn_latest]
    vals = sorted(hn_latest[c] for c in have)
    # 池内百分位分档切点
    def _qt(p):
        if not vals:
            return 0
        i = int(p * (len(vals) - 1))
        return vals[i]
    cut = [_qt(0.2), _qt(0.4), _qt(0.6), _qt(0.8)]
    TIERS = ("非常集中", "集中", "相对集中", "分散", "发散性")

    def _tier(n):
        if n <= cut[0]:
            return 0
        if n <= cut[1]:
            return 1
        if n <= cut[2]:
            return 2
        if n <= cut[3]:
            return 3
        return 4
    conc_map = {c: (TIERS[_tier(hn_latest[c])], round(_pct_le(vals, hn_latest[c]), 3))
                for c in have}
    # 板块内（一级行业）前 30%
    sec1: dict = {}
    for r in con.execute("SELECT code, industry FROM stock_org WHERE industry IS NOT NULL AND industry != ''"):
        c = str(r["code"] or "").strip().zfill(6)
        if c in pool:
            sec1[c] = str(r["industry"]).split("-")[0].strip()
    by_sec: dict = {}
    for c in pool:
        by_sec.setdefault(sec1.get(c, "未知"), []).append(c)
    holder30: set = set()
    for _s, lst in by_sec.items():
        lst2 = [c for c in lst if c in hn_latest]
        if not lst2:
            continue
        lst2.sort(key=lambda x: hn_latest[x])
        k = max(1, int(len(lst2) * 0.30))
        holder30 |= set(lst2[:k])
    return conc_map, holder30


def sig_ann(con: sqlite3.Connection, asof: str | None = None) -> dict:
    """公告催化：近 N 日公告按类目分好坏（stock_ann 已有 cat/tone 分类）。

    这是最硬的一类信号 —— 公告是白纸黑字、可证伪，不像情绪和席位那样可刷。
    """
    GOOD = ("股权激励", "回购增持", "并购重组", "产品/资质突破", "股东回报", "业绩预增")
    BAD = ("股东减持", "质押担保", "退市风险", "监管处罚", "减值计提", "业绩预降", "诉讼仲裁")
    since = (datetime.now() - timedelta(days=LOOKBACK_DAYS * 2)).strftime("%Y-%m-%d")
    sql = "SELECT code, name, notice_date, cat, tone FROM stock_ann WHERE notice_date >= ?"
    args = [since]
    if asof:
        sql += " AND notice_date <= ?"
        args.append(asof)
    try:
        rows = con.execute(sql, args).fetchall()
    except Exception:  # noqa: BLE001
        return {}
    agg: dict = {}
    for r in rows:
        sym = str(r["code"] or "").strip().zfill(6)
        if len(sym) != 6 or not sym.isdigit():
            continue
        cat = str(r["cat"] or "")
        # 2026-10-01 口径自查：判据改用入库时已定好的 tone（分类规则唯一真源 = newsclass/watchdesk）。
        # 改前这里按「类目名子串 + 两张硬编码白名单」猜好坏，是同一批标签的第三份副本，
        # 命名与上游不同步（"业绩预增" vs "业绩向好"、"回购增持" vs "股东回报"），改个词就静默失效。
        # tone 缺失的老数据回落旧匹配，避免历史信号整体归零。
        tone = str(r["tone"] or "")
        a = agg.setdefault(sym, {"good": 0, "bad": 0, "name": None, "cats": []})
        if tone == "good" or (not tone and any(g in cat for g in GOOD)):
            a["good"] += 1
            a["cats"].append(cat)
        elif tone == "bad" or (not tone and any(b in cat for b in BAD)):
            a["bad"] += 1
            a["cats"].append(cat)
        a["name"] = a["name"] or (r["name"] or None)
    out: dict = {}
    for sym, a in agg.items():
        # 坏消息只扣 0.6 分：短线里"坏消息落地"有时反而是买点，不宜全额抵消
        strength = a["good"] - 0.6 * a["bad"]
        if strength <= 0:
            continue
        ev = ["近%d日利好公告 %d 条" % (LOOKBACK_DAYS * 2, a["good"])]
        if a["bad"]:
            ev.append("利空 %d 条" % a["bad"])
        out[sym] = {"strength": strength, "evidence": ev, "name": a["name"]}
    return out


def desk_snapshot_date(con: sqlite3.Connection) -> str:
    """`stock_desk` 这份快照的日期（max(scan_at) 的前 10 位）。取不到返回 ""。

    单独抽成函数是因为「这路能不能用」要靠它判：`_generate_inner` 拿它和预测日比，
    `sig_desk` 内部也用它。**只读，不写库。**
    """
    try:
        r = con.execute("SELECT MAX(scan_at) AS m FROM stock_desk").fetchone()
    except Exception:  # noqa: BLE001
        return ""
    return str((r["m"] if r is not None else "") or "")[:10]


def sig_desk(con: sqlite3.Connection, asof: str | None = None) -> dict:
    """盯盘结论：watchdesk 模块当日产出（stock_desk：stance / risk_score / opp_score）。

    双向信号 —— 风险主导（risk_score 高）应当负贡献。盯盘结论是内部研判产物，
    与政策、公告同属"内部资料"一路。

    **快照语义（2026-10-30 收紧为「不许冒充历史」，见 DECISIONS.md D31）**：
      `stock_desk` 每次扫描**全量重写**、只留当前一期（227 行、无历史版本），无法按日回溯。
      改前是"照常参与 + 如实标注"——回放 2017 年时却拿 2026-10-30 的盯盘结论打分，
      这是纯前视，标注不能免责。
      现在：`asof` 早于快照日（`desk_snapshot_date(con) > asof`）→ 返回 `{}`，
      即**该路不可用**，沿用项目既有缺失语义（空 dict = 这一路没数据，同 sig_extref /
      sig_board 查不到行时一样），不新造"不可用"标记；原因由调用方写进 run note。

      判据为什么是 `>` 而不是 `!=`：
        a) `asof >= 快照日` 意味着快照**不来自未来**，不是前视，照常用（陈旧是另一个问题，
           现状未做新鲜度过滤，本轮不扩大改动面）；
        b) 快照内混有两个扫描日（2026-10-14 4 行 / 2026-10-30 223 行），若用 `!=`
           要求"快照日必须等于 asof"，实盘当天也会被判不可用 —— 那是拿修复前视去砸实盘，
           D31 要求的"实盘零影响"就破了。

    **⚠ `asof` 形参在这里不代表"支持回放"：本路的历史回放能力为零。**
      任何早于当期快照日的历史日，本函数一律返回 `{}`（不可用）。它保留 `asof` 形参只为
      让 9 路信号签名一致、并让"这一路在当前 asof 下到底能不能用"有个统一入口；
      以后若有人想给历史日补盯盘结论，必须先让 `stock_desk` 变成**带历史版本**的表
      （每次扫描追加而不是全量重写），否则不要试图在这里加日期过滤蒙混过关。
    """
    cut = str(asof or "")[:10]
    if cut:
        snap = desk_snapshot_date(con)
        if snap and snap > cut:
            return {}                   # 快照来自未来 → 不许冒充当日结论
    sql = "SELECT code, name, stance, risk_score, opp_score, scan_at FROM stock_desk"
    args: list = []
    try:
        rows = con.execute(sql, args).fetchall()
    except Exception:  # noqa: BLE001
        return {}
    out: dict = {}
    for r in rows:
        sym = str(r["code"] or "").strip().zfill(6)
        if len(sym) != 6 or not sym.isdigit():
            continue
        opp = _f(r["opp_score"], 0.0) or 0.0
        risk = _f(r["risk_score"], 0.0) or 0.0
        strength = opp - risk
        if strength <= 0:
            continue
        st = str(r["stance"] or "")
        ev = ["盯盘：%s" % st]
        if opp:
            ev.append("机会分 %g" % opp)
        if risk:
            ev.append("风险分 %g" % risk)
        out[sym] = {"strength": strength, "evidence": ev, "name": r["name"] or None}
    return out


def sig_policy(con: sqlite3.Connection, asof: str | None = None) -> dict:
    """政策指向（内部资料）：中央级政策按 sectors 映射到个股，按 importance 加权。

    实测：policy_news 共 73 条，其中不少 sectors 为空；能落到候选池的板块词约占 22%
    （172 个板块词里 38 个命中池内票）。属天然稀疏信号 —— 靠 _cover_conf() 自动压权重，
    命中即加分、不命中不加分，不会稀释大盘。
    """
    out: dict = {}
    idx = _industry_index(con)
    sql2 = "SELECT key, sectors, importance, level, title, pub_time FROM policy_news"
    args2: list = []
    if asof:
        sql2 += " WHERE (pub_time IS NULL OR pub_time = '' OR pub_time <= ?)"
        args2.append(asof)
    try:
        rows = con.execute(sql2 + " ORDER BY id", args2).fetchall()
    except Exception:  # noqa: BLE001
        return out
    agg: dict = {}
    for r in rows:
        try:
            words = json.loads(r["sectors"] or "[]")
        except Exception:  # noqa: BLE001
            words = []
        if not isinstance(words, list) or not words:
            continue
        hit = _expand_sector_words(idx, [str(w) for w in words])
        if not hit:
            continue
        imp = _f(r["importance"], 1.0) or 1.0
        lvl = 1.5 if str(r["level"] or "") == "中央" else 1.0
        wgt = imp * lvl
        for code in hit:
            a = agg.setdefault(code, {"s": 0.0, "n": 0, "title": None, "name": None})
            a["s"] += wgt
            a["n"] += 1
            a["title"] = a["title"] or (str(r["title"] or "")[:24] or None)
    for code, a in agg.items():
        out[code] = {"strength": a["s"],
                     "evidence": ["政策命中 %d 条" % a["n"]] + ([a["title"]] if a["title"] else []),
                     "name": a["name"]}
    return out


def sig_board(con: sqlite3.Connection, asof: str | None = None) -> dict:
    """板块资金：所属行业板块当日主力净额（board_flow_samples，kind='industry'）。"""
    idx = _industry_index(con)
    sql = ("SELECT board_name, trade_date, main_net, pct FROM board_flow_samples "
           "WHERE kind='industry'")
    args: list = []
    if asof:
        sql += " AND trade_date <= ?"
        args.append(asof)
    try:
        # 注意：board_flow_samples **没有 id 列**（建表时未建），ORDER BY id 会直接抛
        # OperationalError，异常被子句吞掉后整路信号变空 —— 2026-10-01 实测踩过。
        # 这里按 trade_date/ts 排序取最近交易日，并放宽到最近 3 天聚合，避免单日板块缺失。
        rows = con.execute(sql + " ORDER BY trade_date DESC, ts DESC", args).fetchall()
    except Exception as e:  # noqa: BLE001
        return {}
    if not rows:
        return {}
    last_day = str(rows[0]["trade_date"] or "")
    # 先只取最近一个交易日（同一板块跨日相加会让净额虚高出「1189 亿」这种离谱值）；
    # 若该日一条都没匹配上，再放宽到最近 3 天。
    out: dict = {}
    for days_back in (0, 2):
        cut = last_day
        if days_back:
            try:
                cut = (datetime.strptime(last_day, "%Y-%m-%d")
                       - timedelta(days=3)).strftime("%Y-%m-%d")
            except ValueError:
                cut = last_day
        agg: dict = {}
        for r in rows:
            d = str(r["trade_date"] or "")
            if d != last_day and (not cut or d < cut):
                continue
            codes = _expand_sector_words(idx, [str(r["board_name"] or "")])
            if not codes:
                continue
            net = _f(r["main_net"], 0.0) or 0.0
            if net <= 0:
                continue
            pct = _f(r["pct"], 0.0) or 0.0
            for code in codes:
                a = agg.setdefault(code, {"net": 0.0, "pct": 0.0})
                a["net"] += net
                a["pct"] = max(a["pct"], pct)
        if agg:
            # main_net 原始单位不明（实测换算成"亿"会得出千亿级离谱值），
            # 故只给相对强度，不标绝对金额单位，避免误导。
            for code, a in agg.items():
                out[code] = {"strength": a["net"],
                             "evidence": ["所属板块资金强度 %.2f" % a["net"],
                                          "板块涨 %.2f%%" % a["pct"]],
                             "name": None}
            break
    return out


def sig_overseas(con: sqlite3.Connection, asof: str | None = None) -> dict:
    """海外联动：地缘事件直点个股（geo_events.symbols_up，最准）+ 海外舆情传导板块。

    实测 geo_events 63 只直点符号里只有 1 只在池内 —— 天然稀疏，命中即是有价值信号。
    """
    idx = _industry_index(con)
    out: dict = {}
    try:
        rows = con.execute("SELECT symbols_up, symbols_down, sectors_up, sectors_down, "
                           "intensity, direction, event_date, title FROM geo_events")
    except Exception:  # noqa: BLE001
        rows = []
    for r in rows:
        if asof:
            ed = str(r["event_date"] or "")[:10]
            if ed and ed > asof:
                continue
        try:
            ups = _json_loads(r["symbols_up"])
            downs = _json_loads(r["symbols_down"])
        except Exception:  # noqa: BLE001
            ups, downs = [], []
        codes = set()
        for it in ups:
            if isinstance(it, dict) and it.get("code"):
                codes.add(str(it["code"]).strip().zfill(6))
        for it in downs:
            if isinstance(it, dict) and it.get("code"):
                codes.add(str(it["code"]).strip().zfill(6))
        sectors = [str(x) for x in _json_loads(r["sectors_up"])]
        sectors = [s for s in sectors if s]
        if not codes and sectors:
            codes = _expand_sector_words(idx, sectors)
        if not codes:
            continue
        inten = _f(r["intensity"], 2.0) or 2.0
        for code in codes:
            a = out.setdefault(code, {"s": 0.0, "n": 0, "title": None})
            a["s"] += min(inten, 5.0) / 2.0
            a["n"] += 1
            a["title"] = a["title"] or (str(r["title"] or "")[:24] or None)
    # 海外舆情传导（overseas_hotspots.domestic_sectors）
    try:
        for r in con.execute("SELECT domestic_sectors, heat, confidence, title FROM overseas_hotspots"):
            sectors = [x.strip() for x in str(r["domestic_sectors"] or "").split(",") if x.strip()]
            codes = _expand_sector_words(idx, sectors)
            if not codes:
                continue
            heat = _f(r["heat"], 0.0) or 0.0
            conf = _f(r["confidence"], 0.6) or 0.6
            w = (1.0 + min(heat, 500.0) / 200.0) * (0.5 + conf)
            title = str(r["title"] or "")[:24]
            for code in codes:
                a = out.setdefault(code, {"s": 0.0, "n": 0, "title": None})
                a["s"] += w
                a["n"] += 1
                a["title"] = a["title"] or title or None
    except Exception:  # noqa: BLE001
        pass
    # overseas_news.sectors（部分行有板块）
    try:
        for r in con.execute("SELECT sectors, sentiment_score, heat, title FROM overseas_news"):
            raw = str(r["sectors"] or "")
            if not raw or raw == "[]":
                continue
            try:
                words = json.loads(raw)
            except Exception:  # noqa: BLE001
                words = []
            codes = _expand_sector_words(idx, [str(w) for w in words]) if isinstance(words, list) else set()
            if not codes:
                continue
            sent = _f(r["sentiment_score"], 0.0) or 0.0
            if sent < 0:
                continue
            heat = _f(r["heat"], 0.0) or 0.0
            w = (1.0 + min(heat, 100.0) / 60.0) * (0.7 + min(sent, 3.0) / 6.0)
            title = str(r["title"] or "")[:24]
            for code in codes:
                a = out.setdefault(code, {"s": 0.0, "n": 0, "title": None})
                a["s"] += w
                a["n"] += 1
                a["title"] = a["title"] or title or None
    except Exception:  # noqa: BLE001
        pass
    for code, a in out.items():
        out[code] = {"strength": a["s"],
                     "evidence": ["海外/地缘 %d 条指向" % a["n"]] + ([a["title"]] if a["title"] else []),
                     "name": None}
    return out


def _json_loads(x) -> list:
    if x is None:
        return []
    if isinstance(x, (list, tuple)):
        return list(x)
    try:
        v = json.loads(x)
        return v if isinstance(v, list) else []
    except Exception:  # noqa: BLE001
        return []


def sig_follow(con: sqlite3.Connection, asof: str | None = None) -> dict:
    """实盘选手跟随：改用 **trades 实时成交**，按选手本人的历史胜率加权。

    2026-10-01 重要修正：原实现读 positions 表，而 positions 最新 post_time = 2021-06-20
    —— 也就是说界面上那批"选手跟随"其实是五年前的快照，根本不是跟随。
    trades 表有当天（如 2026-10-01 12:31）的真实成交，且带 action（BUY/SELL/HOLD），
    才是活的信号。权重按 person_scores 的真实胜率给，不再用拍脑袋的 influence/tier_weight。

    asof：数据隔离边界（按 post_time 字符串比较，格式 YYYY-MM-DD HH:MM:SS）。
    """
    # 1) 选手历史胜率 → 0~1（无胜率记录者取中性 0.5）
    win: dict = {}
    try:
        for r in con.execute(
                "SELECT name, win_rate_5d, win_rate_20d, best_call_ret FROM person_scores"):
            nm = str(r["name"] or "").strip()
            if not nm:
                continue
            w20 = _f(r["win_rate_20d"])
            w5 = _f(r["win_rate_5d"])
            base = 0.5
            if w20 is not None:
                base = min(1.0, max(0.0, w20))
            elif w5 is not None:
                base = min(1.0, max(0.0, w5))
            win[nm] = base
    except Exception:  # noqa: BLE001
        pass

    sql = ("SELECT symbol, symbol_name, author, action, post_time FROM trades "
           "WHERE symbol IS NOT NULL AND symbol != ''")
    args: list = []
    if asof:
        # P1-10: 数据隔离边界 = [asof-10d, asof]，既避免用到未来帖，又把信号收窄到近 10 日窗口
        sql += " AND post_time <= ? AND post_time >= date(?, '-10 day')"
        args.append(asof)
        args.append(asof)
    try:
        rows = con.execute(sql + " ORDER BY id", args).fetchall()
    except Exception:  # noqa: BLE001
        return {}

    # P1-10: 新近度锚点随 asof 走（asof 为 None 时用今天），与上方 asof 隔离边界保持一致口径
    if asof:
        anchor = asof[:10]
    else:
        anchor = datetime.now().strftime("%Y-%m-%d")
    anchor_dt = datetime.strptime(anchor, "%Y-%m-%d") if anchor else None
    out: dict = {}
    for r in rows:
        sym = str(r["symbol"] or "").strip().zfill(6)
        if len(sym) != 6 or not sym.isdigit():
            continue
        act = str(r["action"] or "").upper()
        if act.startswith("SELL") or any(w in act for w in ("卖", "减仓", "清仓")):
            continue          # 跟随买入方，卖出的不参与
        who = str(r["author"] or "").strip()
        if not who:
            continue
        # 2) 新近度衰减：当天 1.3 / 3 天内 1.1 / 10 天内 1.0 / 更早 0.6
        ts = str(r["post_time"] or "")
        day = ts[:10]
        if anchor_dt is None:
            rec = 1.0
        elif day >= anchor:
            rec = 1.3
        elif day >= (anchor_dt - timedelta(days=3)).strftime("%Y-%m-%d"):
            rec = 1.1
        elif day >= (anchor_dt - timedelta(days=10)).strftime("%Y-%m-%d"):
            rec = 1.0
        else:
            rec = 0.6
        # 3) 胜率权重：0.55（全败）~ 1.45（全胜）
        w = rec * (0.45 + 1.0 * win.get(who, 0.5))
        a = out.setdefault(sym, {"w": 0.0, "who": [], "win": 0.0, "name": None})
        a["w"] += w
        a["win"] += win.get(who, 0.5)
        if who and who not in a["who"]:
            a["who"].append(who)
        a["name"] = a["name"] or (r["symbol_name"] or None)
    for sym, a in out.items():
        n = len(a["who"]) or 1
        a["strength"] = a["w"]
        a["evidence"] = ["%d 位选手近%dd有买入" % (n, LOOKBACK_DAYS),
                         "含：%s" % "、".join(a["who"][:3]),
                         "选手均胜率 %.0f%%" % (a["win"] / n * 100)]
    return out


def sig_extref(con: sqlite3.Connection, asof: str | None = None) -> dict:
    """外部参考：外部信源看多信号，按信源 tier 加权。

    口径说明：extref_signals 为人工沉淀、低频更新（看多信号集中在 2026-09-07/08，
    远早于 10 日回看窗口），故不按"近 N 日"过滤，只取 direction 含「多」且排除「中性」的行。
    asof：数据隔离边界 —— 只取 pub_date <= asof 的信号（pub_date 为信源发布日，可信）；
    pub_date 为空的旧行按"早已发布"处理照常参与。
    """
    sql = "SELECT symbol, symbol_name, src_name, src_tier, direction, pub_date " \
          "FROM extref_signals WHERE symbol IS NOT NULL AND symbol != ''"
    args: list = []
    if asof:
        sql += " AND (pub_date IS NULL OR pub_date = '' OR pub_date <= ?)"
        args.append(asof)
    rows = con.execute(sql, args).fetchall()
    tw = {"A": 1.0, "B": 0.6, "C": 0.3}
    out: dict = {}
    for r in rows:
        sym = str(r["symbol"] or "").strip()
        if len(sym) != 6 or not sym.isdigit():
            continue
        d = str(r["direction"] or "")
        if d and not any(w in d for w in ("多", "买", "推荐", "看好", "增")):
            continue
        a = out.setdefault(sym, {"w": 0.0, "src": [], "name": None})
        a["w"] += tw.get(str(r["src_tier"] or "B").upper(), 0.6)
        if r["src_name"] and r["src_name"] not in a["src"]:
            a["src"].append(r["src_name"])
        a["name"] = a["name"] or (r["symbol_name"] or None)
    for sym, a in out.items():
        a["strength"] = a["w"]
        a["evidence"] = ["%d 个信源看多" % len(a["src"]),
                         "含：%s" % "、".join(a["src"][:3])]
    return out


def sig_holders(con: sqlite3.Connection, asof: str | None = None) -> dict:
    """机构结构（原名「筹码集中」，2026-10-01 升级）：

    原只算「股东户数环比下降」，信息量太薄。升级为两件事合成：
      ① stock_holders（26128 行，**池内 160/160 全覆盖**）：最新一期前十大里的机构家数、
         机构合计持股占比（hold_ratio）、以及 hold_change 含"增持"的家数 —— 这是最硬的
         内部资料，比股东户数总数可读得多；
      ② stock_holder_num：股东户数环比下降（筹码趋集中）。

    **时点口径（2026-10-30 修前视偏差，见 DECISIONS.md D31）**：
      `asof` 非空  → 只认 `end_date <= asof` 的报告期，再在剩下的期里取最新一期（时点取数）；
      `asof` 为空  → 与改前**完全一致**（全表最新一期），实盘"当场"路径走这一支。

    改前 `asof` 形参存在但函数体完全没用它：回放 2017 年时取的是 2026 年最新一期，
    等于"拿十年后的十大股东名单预测今天"（前视偏差）。

    ⚠ **这是近似 PIT，不是严格 PIT，别当严格 PIT 用**：
      本库 `disclose_at` 只有 2026-01-14 之后的行有值（15.4 万 / 30.8 万），2026 年前基本
      为空，无法按"公告可得日"过滤，只能把**报告期截止日 `end_date`** 当作已知时点。
      真实世界财报滞后 1~4 个月才公告，故 asof 落在「报告期截止日之后、公告日之前」时，
      本函数仍会用到当时市场还不知道的数据 —— 残留的二阶前视，量级远小于改前
      （改前连报告期都不看，一律取最新一期）。待 `disclose_at` 回补到足够覆盖后，
      把条件换成 `COALESCE(disclose_at, end_date) <= asof` 即可收敛为严格 PIT。
    """
    out: dict = {}
    cut = str(asof or "")[:10]        # td 形如 'YYYY-MM-DD'；容忍入参带时间部分
    # ① 前十大股东
    #  重要：同一股东会有 scope='free'/'total' 两行、且跨多个 end_date 重复出现，
    #  早期版本直接累加导致「机构合计持股 30334%」这种离谱值。这里按
    #  (symbol, end_date, holder_rank) 去重后再取最新一期末，且机构占比只累加该期前十大。
    #  **ORDER BY 不是可选的（2026-10-30 实测踩到）**：下面的循环靠 `d > a["date"]` 才重置
    #  累加器，若旧报告期的行排在新报告期之后，老期的前十大会被**累加进最新期** ——
    #  实测 000001 得到「前十大含机构 30 家 / 合计持股 195.6%」（前十大只有 10 个名额，
    #  30 家在物理上不可能）。而 SQLite 无 ORDER BY 时的行序由查询计划决定，加一个
    #  WHERE 条件就会变（本次仅因加了 asof 过滤，4911 只票的读数就变了）——
    #  即"同一份数据、不同行序 → 不同结论"。故显式排序，让「最新一期胜出」可复现。
    #
    #  **`scope` 必须进 tie-break（第二层的同一歧义）**：同一 (symbol, end_date, holder_rank)
    #  会有 scope='free'（十大流通股东）与 'total'（十大股东）两行，而去重键 (sym,d,rank)
    #  只保留**先出现的那一行**。实测这两行 `is_org` 不一致的有 **15,569 组**、
    #  `hold_ratio` 差 >0.01 的有 **38,431 组** —— 谁在前，org_n/ratio 就不是同一个数
    #  （QA 实测：只把 tie-break 从 free 换成 total，org_n 变 800 只、ratio 变 4702 只）。
    #  现状"恰好 free 在前"是因为 UNIQUE(symbol,end_date,holder_rank,scope) 索引里
    #  `'free' < 'total'` —— **这是运气，不是设计**；改插入序或换查询计划，800 只票的
    #  org_n 会静默 ±1，没人发现。故把 scope 显式写进 ORDER BY 钉死。
    #
    #  为什么钉 **free 优先**、且为什么不用 `AND scope='free'` 过滤：
    #    · 语义上 "前十大（流通）股东里的机构" 是这一路的本意（hold_ratio 也是该 scope 的
    #      比例）；`total` 是总股本口径，切换会让 ratio 语义漂移到另一个口径 —— 那是**另一个决策**，
    #      不该藏在修 bug 里顺手做（真实影响 4,702 只票）。
    #    · 不能用 WHERE 过滤：有 **9,690 组只有 total 行**（另有 9,236 组只有 free），
    #      过滤会把这些组静默丢掉。故"free 优先、缺失回落 total"。
    #    · 副作用要写清楚：因为有 9,690 组回落 total，这个 ratio **不是严格单一口径**。
    #      这是既有数据现实（历史就是这么算的），本轮只保证"选谁是可复现的"，
    #      不假装它已经是纯流通口径。
    try:
        sql = ("SELECT symbol, end_date, holder_rank, holder_name, is_org, holder_type, "
               "hold_ratio, hold_change FROM stock_holders "
               "WHERE symbol IS NOT NULL AND symbol != ''")
        args: list = []
        if cut:
            sql += " AND end_date <= ?"
            args.append(cut)
        rows = con.execute(
            sql + " ORDER BY symbol, end_date, holder_rank,"
                  " CASE scope WHEN 'free' THEN 0 ELSE 1 END", args).fetchall()
    except Exception:  # noqa: BLE001
        rows = []
    last: dict = {}
    seen: set = set()
    for r in rows:
        sym = str(r["symbol"] or "").strip().zfill(6)
        if len(sym) != 6 or not sym.isdigit():
            continue
        d = str(r["end_date"] or "")
        rank = _f(r["holder_rank"], 999) or 999.0
        key = (sym, d, rank)
        if key in seen:
            continue                     # free/total 双 scope 去重
        seen.add(key)
        a = last.setdefault(sym, {"date": "", "org_n": 0, "ratio": 0.0,
                                  "add_n": 0, "cut_n": 0, "name": None})
        if d > a["date"]:
            a.clear()
            a.update({"date": d, "org_n": 0, "ratio": 0.0, "add_n": 0, "cut_n": 0, "name": None})
        if rank > 10:
            continue                     # 只看前十大
        is_org = _f(r["is_org"], 0) or str(r["holder_type"] or "") in ("机构", "基金", "券商", "保险")
        if is_org:
            a["org_n"] += 1
            a["ratio"] += _f(r["hold_ratio"], 0.0) or 0.0
        chg = str(r["hold_change"] or "")
        if any(w in chg for w in ("增持", "加仓")):
            a["add_n"] += 1
        elif any(w in chg for w in ("减持", "减仓")):
            a["cut_n"] += 1
    # ② 股东户数环比
    #  stock_holder_num **有 end_date 字段**（1994-12-31 ~ 2026-10-26），与 ① 同口径按 asof 过滤。
    #  不过滤会让「户数环比」这一半仍然前视 —— 只修 ① 等于前视从大门赶进小门。
    try:
        hsql = ("SELECT symbol, end_date, holder_total_num FROM stock_holder_num "
                "WHERE symbol IS NOT NULL AND holder_total_num IS NOT NULL")
        hargs: list = []
        if cut:
            hsql += " AND end_date <= ?"
            hargs.append(cut)
        hrows = con.execute(hsql + " ORDER BY symbol, end_date", hargs).fetchall()
    except Exception:  # noqa: BLE001
        hrows = []
    lastn: dict = {}
    for r in hrows:
        sym = str(r["symbol"] or "").strip().zfill(6)
        if len(sym) != 6 or not sym.isdigit():
            continue
        n = _f(r["holder_total_num"])
        if n is None or n <= 0:
            continue
        lastn.setdefault(sym, []).append((str(r["end_date"] or ""), n))
    for sym, a in last.items():
        ratio = a["ratio"]                  # hold_ratio 本身就是百分数（27.18 = 27.18%）
        strength = ratio + 2.0 * a["add_n"] - 1.5 * a["cut_n"]
        ev = ["%s 前十大含机构 %d 家" % (a["date"] or "最新期", a["org_n"]),
              "机构合计持股 %.1f%%" % ratio]
        if a["add_n"]:
            ev.append("其中 %d 家增持" % a["add_n"])
        if a["cut_n"]:
            ev.append("%d 家减持" % a["cut_n"])
        lst = lastn.get(sym) or []
        if len(lst) >= 2:
            lst.sort()
            (_d0, n0), (d1, n1) = lst[-2], lst[-1]
            if n0 > 0:
                chg = (n1 - n0) / n0
                if chg < -0.005:
                    strength += -chg * 100
                    ev.append("股东户数较上期 %+.1f%%" % (chg * 100))
        if ratio < 1.0 and not a["add_n"] and not a["cut_n"] and len(lst) < 2:
            continue                        # 既无机构也无增减持、户数也没变 → 不占名额
        out[sym] = {"strength": strength, "evidence": ev, "name": a.get("name")}
    return out


# ---------------------------------------------------------------- 权重
def _default_weights() -> dict:
    """初始权重 = 先验系数归一化（不再等权；等权会让 1.3% 覆盖的外部参考和 98% 覆盖的技术面同权）。"""
    tot = sum(SIGNAL_PRIOR.get(k, 1.0) for k in SIGNAL_KEYS) or 1.0
    return {k: SIGNAL_PRIOR.get(k, 1.0) / tot for k in SIGNAL_KEYS}


def _drop_stale_signal_keys(con: sqlite3.Connection) -> None:
    """删掉上一代维度遗留的 signal 行（六路的 fund「龙虎榜资金」等）。只加不减会出事：

    残留一行就让 load_weights 的「口径一致性校验」（got != set(SIGNAL_KEYS)）永远不通过，
    库里存的 rescore 权重永远读不回来，每轮打分都退回先验 —— 2026-10-01 实测踩到。
    放在 ensure_tables 里自愈，这样重启服务（不重新生成）也能收敛到当前维度。
    """
    try:
        stale = [str(r["signal_key"]) for r in con.execute(
            "SELECT signal_key FROM predictor_score WHERE kind='signal'")]
    except Exception:  # noqa: BLE001
        return
    stale = [k for k in stale if k not in SIGNAL_KEYS]
    if stale:
        con.executemany("DELETE FROM predictor_score WHERE signal_key=?",
                        [(k,) for k in stale])
        con.commit()


def _cover_conf(hit_n: int) -> float:
    """覆盖置信度：命中票数越少 → 越接近 0（说明该信号对池子没代表性）。"""
    return 1.0 - math.exp(-(max(0, int(hit_n or 0)) / COVER_K))


def load_weights(con: sqlite3.Connection) -> dict:
    """读取当前生效权重。

    2026-10-01 修复：维度从六路扩到十二路后，predictor_score 里仍残留旧六路等权 1/6，
    与新七路的先验值混用 → 归一化后每路只剩 8.3%，十二路信息被稀释成"伪等权"。
    故这里做**口径一致性校验**：库里存的 signal key 集合必须与当前 SIGNALS 完全一致，
    否则（维度变更期）一律返回先验归一权重，绝不混用两个口径的权重。
    """
    w = _default_weights()
    try:
        rows = list(con.execute(
            "SELECT signal_key, weight FROM predictor_score WHERE kind='signal'"))
    except Exception:  # noqa: BLE001
        return w
    got = {str(r["signal_key"]) for r in rows if r["weight"] is not None}
    if not got or got != set(SIGNAL_KEYS):
        return w
    for r in rows:
        if r["signal_key"] in w and r["weight"] is not None:
            w[r["signal_key"]] = float(r["weight"])
    return w


def _shrunk_hit(n: int, hit: int, prior_hit: float = PRIOR_HIT,
                prior_n: float = PRIOR_N) -> float:
    """Beta-Binomial 贝叶斯收缩命中率（P1-17）。

    逐路用 SIGNAL_PRIOR[k] 作 Beta 先验（caller 传入 prior_hit=_ph），比旧版「全局固定
    PRIOR_HIT/PRIOR_N」更统一——每条信号有自己校准过的先验，冷启动也更稳。
    后验均值 = (hit + α) / (n + α + β)，其中 α = prior_hit·prior_n，β = (1-prior_hit)·prior_n。
    默认参数与旧版等价（prior_hit=PRIOR_HIT, prior_n=PRIOR_N），不破坏无参调用方。
    """
    a = prior_hit * prior_n
    b = (1.0 - prior_hit) * prior_n
    denom = n + a + b
    return (hit + a) / denom if denom > 0 else prior_hit


def _history_coverage(con: sqlite3.Connection) -> dict:
    """从历史 prediction_run.breadth_json 取各信号的历史最大命中票数（覆盖度）。

    为什么必须算覆盖度：2026-10-01 王总指出「外部参考 2 只、论坛情绪 9 只却和技术面 154 只
    拿一样权重，等于拿噪声当信号」。覆盖度校正就是治这个 —— 命中票数少的信号，
    先自己把权重往下压，再谈实盘胜率。
    """
    # 版本隔离：覆盖度只能取自**同一代引擎**的 run。v1.0 那几天的 breadth 里根本没有
    # 新维度的键，混进来会让它们覆盖度算 0 → 被压到 COVER_FLOOR，反过来把旧几路抬高
    # （2026-10-01 实测：v2.0 首日 rescore 后 tech 23% / follow 8.7% 反超 inst 6.6%，
    # 只因 inst 在 v1.0 里压根不存在）。跨版本比对口径本身就是错的。
    cov: dict = {}
    # 覆盖度同样只认主参考窗口内的 run（与 _aggregate 同一个尺子，否则两处又会变成两把尺）
    _ws = window_start()
    _f = " AND run_date >= ?" if _ws else ""
    try:
        rows_ver = con.execute(
            "SELECT breadth_json FROM prediction_run "
            "WHERE engine_ver=? AND breadth_json IS NOT NULL AND breadth_json != ''"
            + _f, ((ENGINE_VER,) + ((_ws,) if _ws else ()))).fetchall()
        rows = rows_ver if rows_ver else con.execute(
            "SELECT breadth_json FROM prediction_run "
            "WHERE breadth_json IS NOT NULL AND breadth_json != ''" + _f,
            ((_ws,) if _ws else ())).fetchall()
        for r in rows:
            try:
                b = json.loads(r["breadth_json"] or "{}")
            except Exception:  # noqa: BLE001
                continue
            if not isinstance(b, dict):
                continue
            for k, v in b.items():
                if k.startswith("_") or k not in SIGNAL_KEYS:
                    continue          # _w/_prior 等快照字段不是命中票数，勿污染覆盖度
                if isinstance(v, (int, float)) and v > 0:
                    cov[k] = max(cov.get(k, 0), int(v))
    except Exception:  # noqa: BLE001
        pass
    return cov


# ---- 近两年逐日回放 → 技术面权重校准（王总 2026-10-06）--------------------------------
# 实盘复盘样本攒得太慢（一天只出几十条，且要等窗口到期），技术面却唯一能拿历史 K 线
# 逐日回放 —— 其余十一路没有历史快照，回测不了。
# 所以在「技术面实盘样本 < REPLAY_MIN_SAMPLE」时，技术面改用近两年逐日回放的命中率定权重。
# 口径与实盘逐字对齐：命中 = 跑赢候选池同期等权平均（与 prediction_item.hit_* 同一套算法）。
REPLAY_MIN_SAMPLE = 200
REPLAY_TECH_SIGNAL = "tech"


def _replay_stats(con: sqlite3.Connection) -> dict:
    """读 predictor_replay（近两年逐日回放），汇总各窗口的 (命中数, 样本数)。"""
    out = {}
    try:
        r = con.execute("SELECT scores_json FROM predictor_replay WHERE id=1").fetchone()
        if not r or not r["scores_json"]:
            return out
        d = json.loads(r["scores_json"]) or {}
        for w in ("t1", "t3", "t5", "t10"):
            n = h = 0
            for k, v in d.items():
                if k.startswith("school:") or not isinstance(v, dict):
                    continue                       # 流派级不重复计数，只吃策略级
                o = v.get(w) or {}
                nn = o.get("n") or 0
                if not nn:
                    continue
                hr = o.get("hit_rate")
                n += nn
                h += nn if hr is None else int(round(nn * hr))
            out[w] = (n, h)
    except Exception:  # noqa: BLE001
        pass
    return out


def _excess_ret(n1: int, sr1: float, n5: int, sr5: float):
    """加权平均超额收益（T+1 权重 0.4 / T+5 权重 0.6）。样本不足返回 None。

    2026-10-29 重写：入参 sr* 现在已经是「ret − 同期池子 bench」的累积
    （见 `_aggregate`），所以这里**不再减任何基线**。

    旧版的做法是 `sr/n - base`，base 取自 signal_baseline，有两个硬伤：
      · 该表只有 fwd=5/10/20，`base1` 永远取默认值 0.0；
      · 它的样本期是全市场 250 个交易日，而账本样本是最近 18 个交易日，
        两个时段的市场环境完全不同（前者 T+5 基线 -0.05%，后者 -2.28%）。
    减出来的数既不是绝对收益、也不是超额，是混合体 —— 实测 ann 因此出现
    「命中率 60% 但超额 -2.37%」的矛盾读数。
    """
    parts = []
    if n1 >= 20:
        parts.append((sr1 / n1, 0.4))
    if n5 >= 10:
        parts.append((sr5 / n5, 0.6))
    if not parts:
        return None
    tw = sum(w for _, w in parts)
    return sum(v * w for v, w in parts) / tw


def _ret_multiplier(n1: int, sr1: float, n5: int, sr5: float) -> float:
    """命中率之外的第二个维度：平均**超额**收益（2026-10-29 修正口径）。

    为什么必须有这一维：`_shrunk_hit` 只看"有没有跑赢池子"。一个 47% 跑赢、
    但跑赢时小赚、跑输时大亏的信号，命中率看着还行，加进排序却持续失血。
    实测（18 个交易日账本）就是这个形态：12 路信号 T+5 胜率全部 37~47%，
    但超额从 +2.86%（inst）到 -1.98%（overseas）横跨 4.8 个百分点。

    返回值 0.55 ~ 1.15。正负不对称：赚钱最多加 15%，亏钱最多减 45%
    ——亏损不可逆，惩罚理应重于奖励。

    斜率实测定：入参 ex 已是真超额（小数），实测跨度 ±0.03。取 12/15 倍后，
    inst(+2.86%) 触上限、overseas(-1.98%) 落到 0.70，中间各档能拉开；
    旧版吃的是「绝对收益减≈0 的伪基线」，量级大到十一路里有九路直接触底，
    区分度归零 —— 这也是它名义生效、实际没在区分任何东西的原因。
    """
    ex = _excess_ret(n1, sr1, n5, sr5)
    if ex is None:
        return 1.0                      # 样本不足 → 不动，交给 hit_score 决定
    z = ex * 12.0 if ex >= 0 else ex * 15.0
    return 1.0 + max(-0.45, min(0.15, z))


def _compute_weights(con: sqlite3.Connection, prior_mode: str = "prod") -> tuple:
    """按「先验系数 × 实盘命中率 × 覆盖置信度」算十二路权重，返回 (new_w, detail)。只算不写库。

    `prior_mode`（2026-10-31 决策②）：
      · "prod"      —— 生产口径，先验随样本量让位（默认，行为与改动前一致）
      · "data_only" —— 影子口径，**完全不用先验**（prior_eff 恒为 1.0），
                       其余环节（命中率 / 覆盖 / 超额系数 / 证据因子 / 限幅 / 归一化）完全相同。
    做成同一个函数的两个模式，而不是复制一份影子函数：复制出来的第二份迟早会与
    第一份走岔，而这两份本来就是要对比的 —— 一旦口径不同，对比就没有意义。

    先验系数此前漏乘（2026-10-01 实测踩到）：rescore 只乘 hit_score×cov_blend，
    导致每次跑完都按"覆盖度"重新洗牌——游资上榜（prior 0.30，全十二路最低）
    会因为"覆盖 52 只 > 外部参考 2 只"被抬到权重第三，与先验设计正好相反。
    补上 prior 后，冷启动（无历史样本 → hit_score=1.0）时权重自然退化为"先验 × 覆盖置信度"。
    """
    cover = _history_coverage(con)
    rp = _replay_stats(con)          # 技术面实盘样本不足时兜底用（近两年回放）
    rows = con.execute("SELECT * FROM predictor_score WHERE kind='signal'").fetchall()
    # 2026-10-29：不再读 signal_baseline。账本里的 sum_ret_* 已改为真超额
    # （ret − 同期池子 bench，见 _aggregate），再减一次外部基线会重复扣除，
    # 而且那份基线是全市场 250 日口径、样本期与账本对不上。
    raw, detail = {}, {}
    for r in rows:
        k = r["signal_key"]
        if k not in SIGNAL_KEYS:
            continue
        src = "实盘复盘"
        # n1 始终来自实盘复盘（T+1 已到期样本最多，是主轴之一）；t3/t5/t10 在「技术面实盘不足」时
        # 用近两年逐日回放兜底，其它信号一律实盘。
        n1, h1 = int(r["n_t1"] or 0), int(r["n_hit_t1"] or 0)
        if k == REPLAY_TECH_SIGNAL and rp and \
                (int(r["n_t3"] or 0) + int(r["n_t5"] or 0) + int(r["n_t10"] or 0)) < REPLAY_MIN_SAMPLE:
            n3, h3 = rp.get("t3", (0, 0))
            n5, h5 = rp.get("t5", (0, 0))
            n10, h10 = rp.get("t10", (0, 0))
            src = "近两年逐日回放（%s 起）" % (rp.get("from") or "")
        else:
            n3, h3 = int(r["n_t3"] or 0), int(r["n_hit_t3"] or 0)
            n5, h5 = int(r["n_t5"] or 0), int(r["n_hit_t5"] or 0)
            n10, h10 = int(r["n_t10"] or 0), int(r["n_hit_t10"] or 0)
        # P1-17: 逐路用 SIGNAL_PRIOR[k] 作 Beta-Binomial 先验均值，冷启动更稳且统一框架
        _ph = SIGNAL_PRIOR.get(k, PRIOR_HIT)
        s1 = _shrunk_hit(n1, h1, prior_hit=_ph)
        s3 = _shrunk_hit(n3, h3, prior_hit=_ph)
        s5 = _shrunk_hit(n5, h5, prior_hit=_ph)
        s10 = _shrunk_hit(n10, h10, prior_hit=_ph)
        # 权重主轴：T+1（0.28）+ T+3（0.34）+ T+5（0.28）+ T+10（0.10）。
        # 旧版只吃 T+5（0.6），而 T+5 样本长期为 0（窗口未到期）→ 主轴恒为中性 0.5，
        # 自学习等于空转（2026-10-11 王总定：把已到期的 T+1/T+3 纳入主轴）。
        score = 0.28 * s1 + 0.34 * s3 + 0.28 * s5 + 0.10 * s10
        # 2026-10-31 整改（测评 P0-D）：样本量置信度改走参数注册表。
        # 原来这里写的是常数 40，而 judge 的门槛是 500，两处互不知情 ——
        # n=499 时 judge 答"样本不足、无权裁决"，eff 已给 99.9996%。
        # 现在 eff 的唯一实现在 viewer/kernel/params.py，两处阈值同源，
        # 矛盾带宽由 params.invariant_gap() 直接算出并进日志/接口。
        # 2026-10-31 决策①：展示口径与生效口径拆开。
        #   eff   —— 样本量本身有多足（面板上那句"置信度"用它）
        #   eff_w —— 再乘权重门：<40 归零、40–500 线性升、≥500 恒等 1
        # 原来只有 eff 一个数，于是 n=200 时权重按 0.992 满功率生效，
        # 而 evidence.judge 同时说"样本不足、无权裁决"—— 同一个 n，两个相反的断言。
        _n_obs = n1 + n3 + n5 + n10
        eff = _PARAMS.eff_of(_n_obs)          # 样本量置信度 0→1（含 T+1）
        eff_w = _PARAMS.eff_weight(_n_obs)    # 生效置信度（过裁决门后）
        hit_score = 1.0 + eff_w * (score - 0.5) * 2.0            # 中性=1.0
        cov_n = cover.get(k, 0)
        cov_conf = _cover_conf(cov_n)                            # 覆盖置信度 0→1
        # 2026-10-29：覆盖度再乘 eff —— 覆盖广只说明「触发得多」，不说明「触发得准」。
        # 旧版让 extref（实盘样本 n=2）靠 cov_n 拿到 3.75% 权重，等于给噪声分配仓位。
        # 2026-10-31：改乘 eff_w（过门槛后的生效置信度），理由同上一条 —— 覆盖度修正
        # 也是个"准不准"的判断，样本不过门就不该修。
        cov_blend = COVER_FLOOR + (1.0 - COVER_FLOOR) * cov_conf * eff_w
        prior = SIGNAL_PRIOR.get(k, 1.0)
        # 2026-10-29：先验随样本量让位。prior 恒为乘数时，样本再多也压不过它 ——
        # 实测 tech 有 1032 条实盘样本、超额 -0.88%，却因 prior=1.45 长期占权重第一；
        # inst 超额 +2.86% 反而排第五。现在先验只在样本少时生效，数据多了退回 1.0。
        # 2026-10-31：同样改用 eff_w。先验"让位"本来是个信任转移，
        # 让给一个没过裁决门的样本量等于把控制权交给噪声。
        if prior_mode == "data_only":
            # 影子口径：先验彻底不参与，等价于"如果当初没有我手工排的那份顺序，
            # 数据会把这十二路排成什么样"。其余因子照旧，保证与生产口径可比。
            prior_eff = 1.0
        else:
            prior_eff = 1.0 + (prior - 1.0) * (1.0 - eff_w)
        # 收益维度修正：命中率高但超额为负的信号在这里被压下来（详见 _ret_multiplier）
        _sr1, _sr5 = _f(r["sum_ret_t1"], 0.0), _f(r["sum_ret_t5"], 0.0)
        ex_ret = _excess_ret(n1, _sr1, n5, _sr5)
        rmul = _ret_multiplier(n1, _sr1, n5, _sr5)
        raw[k] = prior_eff * hit_score * cov_blend * rmul
        detail[k] = {"prior": prior, "prior_eff": round(prior_eff, 3),
                     "hit_t1": s1, "hit_t3": s3, "hit_t5": s5, "hit_t10": s10,
                     "n1": n1, "n3": n3, "n5": n5, "n10": n10, "conf": eff,
                     "n_obs": _n_obs, "conf_w": round(eff_w, 4),
                     "tier": _PARAMS.tier_of(_n_obs),
                     "tier_label": _PARAMS.TIER_LABEL[_PARAMS.tier_of(_n_obs)],
                     "cov_n": cov_n, "cov_conf": cov_conf, "cov_blend": cov_blend,
                     "ret_mul": round(rmul, 3),
                     "excess_ret": (round(ex_ret, 5) if ex_ret is not None else None),
                     "avg_ret_t1": round(_sr1 / n1, 4) if n1 else None,
                     "avg_ret_t5": round(_sr5 / n5, 4) if n5 else None,
                     "src": src}
    if not raw:
        raw = {k: SIGNAL_PRIOR.get(k, 1.0) for k in SIGNAL_KEYS}

    # ---- 大样本证据修正（2026-10-29）----------------------------------------
    # 用全市场因子回测的结论修正技术面权重：历史上整体跑输基线 → 降权；有超额 → 升权。
    # 其他十一路没有大样本依据，不参与修正，改为在下面收紧权重上限。
    ev_factor = 1.0
    try:
        from viewer.kernel import evidence as _ev
        ev_factor = _ev.tech_factor(con)
    except Exception:  # noqa: BLE001  证据层缺失（未跑回测 / 导入失败）不该阻断权重计算
        ev_factor = 1.0
    if abs(ev_factor - 1.0) > 1e-6 and "tech" in raw:
        raw["tech"] *= ev_factor
        detail.setdefault("tech", {})["evidence_factor"] = round(ev_factor, 3)

    # 归一化 → 限幅 → **再归一化**。
    #   第一道限幅加在 raw 上会失效：raw 的量级是 prior×hit×cov（≈0.15~1.35），
    #   十二路全都 > W_MAX，一夹就全等于 W_MAX，归一化后又变回"伪等权"，
    #   覆盖率差异（技术面 154 只 vs 外部参考 2 只）被抹平 —— 2026-10-01 实测踩到。
    #   故限幅放到归一化之后：夹的是最终权重百分比，语义才是"单路不超过 34% / 不低于 2%"。
    tot = sum(raw.values()) or 1.0
    norm_w = {k: v / tot for k, v in raw.items()}
    clipped = {k: max(W_MIN, min(W_MAX if k in VERIFIED_DIMS else W_MAX_UNVERIFIED, v))
               for k, v in norm_w.items()}
    tot2 = sum(clipped.values()) or 1.0
    new_w = {k: v / tot2 for k, v in clipped.items()}
    return new_w, detail


_SHADOW_SWITCH_RULE = ("切换条件（两条同时满足）：① 该路影子权重连续 20 个交易日高于生产；"
                       "② 该路对应信号税后超额为正。只满足①不切 —— "
                       "那可能只是把噪声排得更靠前。")

_SHADOW_DDL = """
CREATE TABLE IF NOT EXISTS signal_weight_shadow (
    asof       TEXT NOT NULL,
    signal_key TEXT NOT NULL,
    w_prod     REAL,
    w_data     REAL,
    diff_pp    REAL,
    n_obs      INTEGER,
    tier       TEXT,
    created_at TEXT,
    PRIMARY KEY (asof, signal_key)
)"""


def _save_shadow(con, prod_w, data_w, detail) -> int:
    """落当日影子权重（每个信号一行）。**不参与任何计算**。

    主键是 (asof, signal_key)：同一天重跑 rescore 会覆盖而不是累加 ——
    否则一天跑三次就会让这条信号在"连续 20 个交易日"的判据里被算成 3 天。
    """
    con.execute(_SHADOW_DDL)
    asof, ts, n = _today(), _now(), 0
    for k in SIGNAL_KEYS:
        if k not in prod_w or k not in data_w:
            continue
        d = detail.get(k) or {}
        con.execute(
            "INSERT INTO signal_weight_shadow"
            " (asof,signal_key,w_prod,w_data,diff_pp,n_obs,tier,created_at)"
            " VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(asof,signal_key) DO UPDATE SET"
            " w_prod=excluded.w_prod, w_data=excluded.w_data, diff_pp=excluded.diff_pp,"
            " n_obs=excluded.n_obs, tier=excluded.tier, created_at=excluded.created_at",
            (asof, k, prod_w[k], data_w[k], (data_w[k] - prod_w[k]) * 100.0,
             int(d.get("n_obs") or 0), str(d.get("tier") or ""), ts))
        n += 1
    con.commit()
    return n


def load_shadow(con, days: int = 20) -> dict:
    """最近 N 个交易日的影子 vs 生产权重，按信号汇总。

    判据写在返回里：**某一路影子权重连续高于生产、且该路税后超额为正**，
    才构成"可以切"的证据。只给差值不给判据，等于让人凭感觉切。
    """
    try:
        rows = con.execute(
            "SELECT asof, signal_key, w_prod, w_data, diff_pp, n_obs, tier"
            " FROM signal_weight_shadow ORDER BY asof DESC LIMIT ?", (int(days) * 20,)).fetchall()
    except sqlite3.Error:
        return {"ok": True, "n_days": 0, "rows": [],
                "switch_rule": _SHADOW_SWITCH_RULE,
                "note": "影子表尚未建立（还没跑过 rescore）"}
    dayset = sorted({r[0] for r in rows}, reverse=True)[:int(days)]
    keep = [r for r in rows if r[0] in set(dayset)]
    by = {}
    for asof, k, wp, wd, diff, nobs, tier in keep:
        b = by.setdefault(k, {"signal_key": k, "n_days": 0, "w_prod_last": None,
                              "w_data_last": None, "diff_pp_last": None,
                              "n_obs": nobs, "tier": tier, "data_ahead_days": 0})
        b["n_days"] += 1
        if b["w_prod_last"] is None:
            b["w_prod_last"] = round(float(wp or 0), 4)
            b["w_data_last"] = round(float(wd or 0), 4)
            b["diff_pp_last"] = round(float(diff or 0), 2)
        if (wd or 0) > (wp or 0):
            b["data_ahead_days"] += 1
    out = sorted(by.values(), key=lambda x: -(x["diff_pp_last"] or 0))
    return {
        "ok": True, "n_days": len(dayset), "asof": dayset[0] if dayset else None,
        "rows": out,
        "switch_rule": _SHADOW_SWITCH_RULE,
        "note": ("影子权重 = 只用数据（不用先验），归一化口径与生产一致。"
                 "它不参与任何评分、排序、组合；这里只记录差异，不改任何权重。"),
    }


def rescore(con: sqlite3.Connection, log: bool = True) -> dict:
    """按「实盘命中率 × 覆盖置信度 × 先验系数」重算十二路信号权重，变动写日志。

    口径（2026-10-11 修正）：
      - 实盘表现主轴：T+1（0.28）+ T+3（0.34）+ T+5（0.28）+ T+10（0.10），贝叶斯收缩防过拟合；
        旧版只吃 T+5（0.6），而 T+5 样本长期为 0 → 主轴恒为中性，自学习空转。现已把已到期的
        T+1/T+3 纳入主轴，让实盘真正驱动权重（带 holders/inst/follow 的信号因跑输被自动降权）。
      - 覆盖置信度：命中票数越少越不信任（外部参考 2 只 → 大幅降权；技术面 154 只 → 不降）；
      - 先验系数：按 2026-10-11 实证方向重校准（holders 1.05→0.45、inst 0.85→0.50、follow 0.52→0.40、
        tech 1.35→1.45、policy 0.80→1.00），作为冷启动与稳态的基线；
      - 三者相乘后限幅（W_MIN/W_MAX）+ 归一化。
    """
    new_w, detail = _compute_weights(con)

    old = load_weights(con)
    changes = []
    ts = _now()
    for k in SIGNAL_KEYS:
        before, after = old.get(k), new_w[k]
        if log and abs(after - before) >= ADJUST_LOG_EPS:
            d = detail.get(k, {})
            if not (d.get("n5") or d.get("n3") or d.get("n10")):
                # 无实盘样本：不打"命中率 50%"这种假数字，直接说明权重由先验 + 覆盖度决定
                reason = "无实盘复盘样本，权重由先验 × 覆盖度决定"
            else:
                base = ("T+1 命中率 %.1f%%（样本 %d）、T+3 %.1f%%（样本 %d）、T+5 %.1f%%、T+10 %.1f%%"
                        % (d.get("hit_t1", 0.5) * 100, d.get("n1", 0),
                           d.get("hit_t3", 0.5) * 100, d.get("n3", 0),
                           d.get("hit_t5", 0.5) * 100, d.get("hit_t10", 0.5) * 100))
                # 命中率来源自述：实盘复盘 vs 近两年逐日回放（王总 2026-10-06 定：以近两年为主参考）
                reason = ("近两年回放 " + base) if "回放" in str(d.get("src") or "") else base
            if d.get("cov_n"):
                reason += "；覆盖 %d 只（置信度 %.2f）" % (d["cov_n"], d.get("cov_conf", 0))
            # 2026-10-29：把「超额」和「先验衰减」写进理由。此前只写命中率，
            # 而命中率与超额会背离（实测 ann 命中 60% 但超额 -0.99%），
            # 只看日志根本不知道为什么降权，事后无法复核。
            if d.get("excess_ret") is not None:
                reason += "；超额 %+.2f%%（收益系数 %.3f）" % (
                    d["excess_ret"] * 100, d.get("ret_mul", 1.0))
            if abs((d.get("prior_eff") or 1.0) - (d.get("prior") or 1.0)) >= 0.05:
                reason += "；先验 %.2f→%.2f（样本已足，先验让位）" % (
                    d.get("prior", 1.0), d.get("prior_eff", 1.0))
            # 口径自述：让人一眼知道这批权重是拿哪段时间的涨跌算出来的（呼应窗口调整）
            _wsd = window_start()
            reason = ("[近 %.1f 年窗口%s] " % (
                LOOKBACK_YEARS, ("自 %s 起" % _wsd) if _wsd else "")) + reason
            reason += " → 权重 %+.1f%%" % ((after - before) * 100)
            changes.append((_today(), k, "signal", before, after, reason, ts))
        meta = SIGNAL_META.get(k, {})
        con.execute(
            "INSERT INTO predictor_score (signal_key, kind, name, school, weight, weight_prev, updated_at) "
            "VALUES (?,?,?,?,?,?,?) ON CONFLICT(signal_key) DO UPDATE SET "
            "weight=excluded.weight, weight_prev=predictor_score.weight, updated_at=excluded.updated_at",
            (k, "signal", meta.get("name", k), None, after, before, ts))
    # P1-14：rescore 幂等化——同一 engine_ver + 同一交易日（_today）已写过调权日志则跳过重复写入，
    # 避免重跑/重试把同一批权重变化记成多条日志（权重 UPSERT 与 walk-forward 仍每次执行，不受影响）。
    _sig_key = "rescore_done_%s_%s" % (ENGINE_VER, _today())
    _already = False
    try:
        _already = con.execute("SELECT 1 FROM meta WHERE key=?", (_sig_key,)).fetchone() is not None
    except Exception:  # noqa: BLE001  meta 不可用时退化为每轮都记（旧行为）
        _already = False
    if changes:
        if not _already:
            con.executemany(
                "INSERT INTO predictor_adjust_log (log_date,signal_key,kind,weight_before,weight_after,reason,created_at) "
                "VALUES (?,?,?,?,?,?,?)", changes)
            try:
                con.execute("INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)", (_sig_key, "1"))
            except Exception:  # noqa: BLE001
                pass
    con.commit()
    # A3-3 整改：每次调权后用滚动窗口重跑最近 250 交易日样本外命中率（walk-forward 报告），
    # 供「把权重调乱 → 报告显示命中率下降」的验收路径直接可见。
    try:
        wf = _walk_forward_track(con)
    except Exception:  # noqa: BLE001
        wf = {"ok": False, "error": "walk-forward 重跑失败"}
    # adjusted 的语义是「写了几条调权日志」（受 2% 阈值 + 当日幂等约束），不等于「改了几路权重」。
    # 2026-10-29 实测被误读：技术面权重 0.1689→0.1499（-1.9%，未达 2% 阈值）明明变了，
    # 接口却报 adjusted=0。故另给 changed（真实变动路数，阈值 1e-4），供界面与日志直接引用。
    _changed = sum(1 for k in SIGNAL_KEYS if abs(new_w.get(k, 0) - old.get(k, 0)) >= 1e-4)
    # 决策②：影子权重同期计算并落表。**只记录不生效** —— 上面的 new_w 一个字节都没被它影响。
    # 为什么不在这里切：全量回测 9 个信号只有 1 个税后为正，此时让数据主导权重，
    # 学出来的是一组负期望排序。先攒 20 个交易日，看影子权重是否真的更稳。
    shadow = None
    if getattr(_PARAMS, "SHADOW_WEIGHTS_ENABLED", False):
        try:
            data_w, _d2 = _compute_weights(con, prior_mode="data_only")
            shadow = {"ok": True, "asof": _today(),
                      "rows": _save_shadow(con, new_w, data_w, detail)}
        except Exception as e:  # noqa: BLE001  影子失败绝不能影响生产权重
            shadow = {"ok": False, "error": str(e)[:160]}
    return {"weights": new_w, "adjusted": len(changes), "changed": _changed,
            "detail": detail, "walk_forward": wf, "shadow": shadow}


def _band_counts(vals: list) -> tuple:
    """按当日全池分数分位带定 core/watch 只数（2026-10-29 替代固定 10/10）。

    背景：原来 CORE_N 写死 10，等于每天必须凑满 10 只。用 `prediction_item` 里 14 个
    交易日、267 条已到期样本做「取前 k 只」的 T+5 超额曲线：

        k     均值      中位      逐日胜率
        1    -2.98%   -4.55%   16.7%
        3    +3.40%   -0.25%   42.9%
        5    +0.74%   +1.98%   57.1%   ← 中位/胜率双见顶
        7    +0.17%   +0.95%   57.1%
        10   -0.72%   +0.04%   50.0%

    均值被右尾支配（k=2 的 +4.43% 几乎全来自单只暴涨），所以判据用**中位数与胜率**——
    两个都不受右尾影响的指标一致指向 k∈[4,7]，k>=8 归零。原固定 10 只正好落在最差档。

    设计取舍（刻意保守）：
    - 取证据区间的**上沿**（8）而不是最优点（5）。14 个交易日 ≈ 2.8 个独立窗口，
      取 argmax 就是曲线拟合；取区间上沿能拿到大部分改善，又不在单点上押注。
    - 用「前 N%」而不是绝对只数，这样候选池从 157 只涨到 735 只时，选择性不漂移。
    - 不引入「相邻分数断层」判据：实测当日头部落差 0.1119、σ=0.2400，任何合理的
      断层阈值要么从不触发、要么在 k=2 处切断——而 k=2 的中位数是 -0.62%，是负的。
      判据不加分却多一个参数，故弃用。
    """
    n = len([v for v in vals if v is not None])
    if n <= 0:
        return 0, 0
    if n <= CORE_MIN:
        return n, 0
    core_k = int(round(n * CORE_PCT))
    core_k = max(CORE_MIN, min(CORE_MAX, core_k))
    core_k = min(core_k, n)
    watch_k = int(round(n * WATCH_PCT)) - core_k
    watch_k = max(0, min(WATCH_MAX, watch_k))
    watch_k = min(watch_k, max(0, n - core_k))
    return core_k, watch_k


def _ensure_score_rows(con: sqlite3.Connection, items: list) -> None:
    """确保记账行存在：十二路信号 + 九流派 + 全部策略（后两类仅记录，不参与合成权重）。"""
    _drop_stale_signal_keys(con)
    rows = []
    for k in SIGNAL_KEYS:
        m = SIGNAL_META[k]
        rows.append((k, "signal", m["name"], None))
    schools = sorted({(s.get("school") or "其它") for s in (STRATEGIES or [])})
    for sc in schools:
        rows.append(("school:" + sc, "school", sc, sc))
    for s in (STRATEGIES or []):
        if not s.get("backtestable") or s.get("scope_limited") or s.get("needs"):
            continue
        rows.append((s.get("key"), "strategy", s.get("name") or s.get("key"), s.get("school")))
    for key, kind, name, school in rows:
        try:
            con.execute(
                "INSERT OR IGNORE INTO predictor_score (signal_key,kind,name,school,weight,weight_prev,updated_at) "
                "VALUES (?,?,?,?,NULL,NULL,?)", (key, kind, name, school, _now()))
        except Exception:  # noqa: BLE001
            pass
    con.commit()


# ---------------------------------------------------------------- 合成与生成
def _pool_regime(bars_map: dict) -> dict:
    """候选池价格状态（2026-10-15 修复 market_view 普跌失灵）。

    背景：宽度=多信号共振占比，度量的是"信号共识"而非"价格趋势"。2026-09-28~10-14
    连续 8 天宽度≥35% 说"偏强可积极选股"，同期池等权两根 -2% 级阴线——普跌里信号
    共振照样扩散（超跌票集体触发技术面），宽度门形同虚设。此处用池内 K 线直接算
    价格证据，与四支柱 A 柱的"普跌"判定同源（价格维度），供 _market_view 联动降级。

    返回 {"chg1": 当日池等权涨跌%, "down_ratio": 下跌家数占比%,
          "chg5": 近5日池等权涨跌%, "n": 参与统计只数}；数据不足返回 {}。"""
    chg1_list, chg5_list = [], []
    for k in bars_map.values():
        try:
            c1, c2 = _f(k[-1].get("c")), _f(k[-2].get("c"))
            if c1 and c2:
                chg1_list.append((c1 / c2 - 1) * 100)
            if len(k) >= 6:
                c6 = _f(k[-6].get("c"))
                if c1 and c6:
                    chg5_list.append((c1 / c6 - 1) * 100)
        except Exception:  # noqa: BLE001
            continue
    n = len(chg1_list)
    if n < 10:  # 样本太少不构成行情判断
        return {}
    out = {
        "chg1": sum(chg1_list) / n,
        "down_ratio": sum(1 for x in chg1_list if x < 0) / n * 100,
        "n": n,
    }
    if chg5_list:
        out["chg5"] = sum(chg5_list) / len(chg5_list)
    return out


def _market_view(sig_stats: dict, senti: dict, weights: dict, top_sectors: list,
                 regime: dict | None = None) -> tuple:
    """由信号统计合成当日研判（纯数据描述 + 强弱结论，不引入外部判断）。

    宽度口径用「多信号共振只数占比」而非技术面触发率 —— 后者因 86 条策略近乎全触发
    （99%）会变成恒为偏强的伪信号；前者才反映"有多少票被多路独立信号共同看多"。

    2026-10-15 普跌联动修复：宽度是信号共识度，不是价格趋势。池等权当日普跌
    （下跌家数≥60% 或当日跌逾 1.5%）时，无论宽度多高，结论强制降为防守——
    宽度与价格背离时以价格为准，并如实写出背离，供人工复核。"""
    conv_n = sig_stats.get("conv_n", 0)
    pool_n = sig_stats.get("_pool", 1) or 1
    breadth = conv_n / pool_n * 100 if pool_n else 0
    if breadth >= 35:
        tone, style = "偏强", "多信号共振扩散，可积极选股"
    elif breadth >= 20:
        tone, style = "中性偏强", "结构性行情，资金聚焦少数板块"
    elif breadth >= 10:
        tone, style = "中性偏弱", "缩量分化，信号集中于强势股"
    else:
        tone, style = "偏弱", "信号稀少，宜降低仓位等待"
    parts = ["多信号共振 %d/%d 只（%.0f%%）" % (conv_n, pool_n, breadth)]
    rg = regime or {}
    chg1, down_ratio = rg.get("chg1"), rg.get("down_ratio")
    if chg1 is not None and down_ratio is not None:
        parts.append("池等权当日 %+.2f%%、下跌 %d/%d 只" % (chg1, round(down_ratio / 100 * (rg.get("n") or 0)), rg.get("n") or 0))
        if "chg5" in rg:
            parts.append("近5日 %+.2f%%" % rg["chg5"])
        if down_ratio >= 60 or chg1 <= -1.5:
            tone, style = "防守（价格普跌修正宽度）", "池内普跌，信号共振与价格背离，宜降仓防守、慎追高拥挤票"
    if senti.get("net") is not None:
        parts.append("论坛情绪净多 %+d" % senti["net"])
    if sig_stats.get("inst_n"):
        parts.append("机构席位净买 %+.2f 亿" % sig_stats["inst_n"])
    view = "市场宽度%s → %s。依据：%s。" % (tone, style, "；".join(parts))
    sect = ("主线板块：%s" % "、".join(top_sectors[:3])) if top_sectors else "主线板块未形成明显聚焦"
    return view, sect


def _sector_of(con: sqlite3.Connection, syms: list) -> dict:
    """个股 → 板块（复用 board_flow 的映射思路：优先 stock_org，退回无）。"""
    out = {}
    if not syms:
        return out
    try:
        q = ",".join("?" * len(syms))
        for r in con.execute(
                "SELECT code, MAX(industry) AS industry FROM stock_org "
                "WHERE code IN (%s) GROUP BY code" % q, syms):
            if r["industry"]:
                out[str(r["code"])] = r["industry"]
    except Exception:  # noqa: BLE001
        pass
    return out


def generate(con: sqlite3.Connection, run_date: str | None = None,
             force: bool = False, progress=None) -> dict:
    """生成当日预测。

    run_date：预测日（默认 = 候选池 K 线的最后一根交易日）。已有同日记录且 force=False → 直接返回。
    """
    with _LOCK:
        if _STATUS["running"]:
            return {"ok": False, "error": "已有生成任务在跑，请稍候"}
        _STATUS.update(running=True, stage="准备", started_at=_now(), last_error=None)
        try:
            return _generate_inner(con, run_date, force, progress)
        except Exception as e:  # noqa: BLE001
            _STATUS["last_error"] = str(e)[:300]
            raise
        finally:
            _STATUS.update(running=False, stage="")


def _generate_inner(con, run_date, force, progress) -> dict:
    t0 = time.time()
    ensure_tables(con)
    _ensure_score_rows(con, None)

    # 1) 候选池 + K 线
    _STATUS["stage"] = "取候选池"
    codes, names, _boards = SS.universe(POOL_LIMIT)
    if not codes:
        return {"ok": False, "error": "候选池为空（positions / lhb_stocks 无数据）"}
    kc = load_kcache()
    bars_map = fetch_bars(codes, kc, force=force,
                          progress=(lambda a, b: progress and progress("抓 K 线", a, b)))
    bars_map = {c: v for c, v in bars_map.items() if len(v) >= MIN_BARS}
    td = run_date or _trade_date_of(bars_map)
    if not td:
        return {"ok": False, "error": "无法确定交易日"}

    if not force:
        row = con.execute("SELECT run_date FROM prediction_run WHERE run_date=?", (td,)).fetchone()
        if row:
            return {"ok": True, "run_date": td, "cached": True}

    # 1.5) 数据隔离（asof 截断）：预测日为 td，则一切信号只允许看见 td 及以前的数据。
    #   - K 线：截断到 td 收盘（补跑历史日时尤其关键——K 线缓存里可能有 td 之后的真实走势，
    #     不截断就等于"拿着后面的答案做前面的预测"）。
    #   - 龙虎榜 / 外部参考：sig_fund / sig_extref 内按 trade_date / pub_date <= td 过滤。
    #   - 股东结构（holders）：2026-10-30 起按 end_date <= td 取报告期（近似 PIT，见 D31）。
    #   - 盯盘结论（desk）：stock_desk 无历史版本，快照日 > td 时直接判该路不可用（见 D31）。
    #   - 其余（政策/板块/海外）：库内时间戳不可靠（post_time 停在 2021 等），属"快照语义"，
    #     无法精确回溯 —— 在 run note 中如实标注，不假装隔离。
    _STATUS["stage"] = "数据隔离截断"
    bars_map = {c: [b for b in v if str(b.get("d", ""))[:10] <= td]
                for c, v in bars_map.items()}
    bars_map = {c: v for c, v in bars_map.items() if len(v) >= MIN_BARS}
    if not bars_map:
        return {"ok": False, "error": "截断到 %s 后候选池 K 线为空" % td}
    # A3-1（2026-10-12）：截断后立刻加一道**可断言**的时点校验 —— 万一日后有人动上面两行，
    # 这里会直接抛 LookaheadError，而不是静默地"拿后面的答案做前面的预测"。
    for _c, _bars in bars_map.items():
        if _bars:
            assert_point_in_time(td, _bars[-1].get("d"))

    # 2) 十路信号（2026-10-31：论坛情绪 / 选手跟随两路已随论坛停抓摘除，见 SIGNALS 注释）
    _STATUS["stage"] = "算技术面"
    strat_list = _load_strategies()
    tech = sig_tech(bars_map, strat_list,
                    progress=(lambda a, b: progress and progress("算技术面", a, b)))
    _STATUS["stage"] = "汇总其它信号"
    inst_raw = sig_inst(con, asof=td)        # 龙虎榜「机构专用」席位（硬钱）
    lhb_raw = sig_lhb(con, asof=td)          # 龙虎榜游资/营业部席位（噪声级）
    extref_raw = sig_extref(con, asof=td)
    holders_raw = sig_holders(con, asof=td)   # 2026-10-30：按 td 取报告期（原漏传 asof → 前视，D31）
    ann_raw = sig_ann(con, asof=td)
    desk_raw = sig_desk(con, asof=td)
    # desk 不可用时要说明原因（run note 是项目既定的"如实标注"位置，见上面 1.5 段注释）
    desk_note = ""
    if not desk_raw:
        _ds = desk_snapshot_date(con)
        if _ds and _ds > td:
            desk_note = "｜盯盘结论不可用：stock_desk 快照日 %s 晚于预测日 %s（该表无历史版本）" % (_ds, td)
    policy_raw = sig_policy(con, asof=td)
    board_raw = sig_board(con, asof=td)
    overseas_raw = sig_overseas(con, asof=td)
    raw_signals = {
        "tech": tech, "holders": holders_raw, "ann": ann_raw, "desk": desk_raw,
        "inst": inst_raw, "policy": policy_raw, "board": board_raw,
        "overseas": overseas_raw, "extref": extref_raw, "lhb": lhb_raw,
    }
    # 只保留候选池内的票参与合成（池外票无 K 线，无法复盘校验，也不计入宽度统计）。
    # 否则 follow/holders 等全表信号会把池外票混入打分与「多信号共振」宽度统计。
    pool_set = set(bars_map.keys())
    for k in list(raw_signals):
        raw_signals[k] = {s: v for s, v in raw_signals[k].items() if s in pool_set}

    # 情绪净多（用于研判文案）—— 2026-10-31 起恒为 0：论坛源已停，不再读 posts。
    net_senti = 0

    # 3) 强度归一化 + 加权合成
    _STATUS["stage"] = "合成打分"
    weights = load_weights(con)
    # 各信号内部先按百分位归一（同一信号内可比），再乘权重
    norm = {}
    for k, m in raw_signals.items():
        if not m:
            norm[k] = {}
            continue
        vals = sorted(_f(v.get("strength"), 0.0) or 0.0 for v in m.values())
        norm[k] = {sym: _pct_le(vals, _f(v.get("strength"), 0.0) or 0.0)
                   for sym, v in m.items()}
    scores: dict = defaultdict(float)
    hits_by_sym: dict = defaultdict(list)
    for k in SIGNAL_KEYS:
        w = weights.get(k, 0.0)
        for sym, p in norm.get(k, {}).items():
            if p <= 0:
                continue
            scores[sym] += w * p
            src = raw_signals[k].get(sym) or {}
            hit = {
                "key": k, "name": SIGNAL_META[k]["name"], "weight": round(w, 4),
                "pct": round(p, 3), "strength": round(_f(src.get("strength"), 0.0) or 0.0, 3),
                "evidence": src.get("evidence") or [],
            }
            if k == "tech":      # 带上逐策略/流派明细，供流派记分与"哪种方法更准"对比
                hit["strategies"] = src.get("strategies") or []
                hit["schools"] = src.get("schools") or []
            hits_by_sym[sym].append(hit)

    # 4) 排序取核心/观察（同分时技术面命中数多者优先）
    # 多信号共振加成：一只票被越多路独立信号命中，判断越稳（加权后再叠加固定加成，
    # 直接落地用户"每个部分都多多少少参与一下、综合对比"的要求）。
    for sym in list(scores):
        nsig = len(hits_by_sym.get(sym, []))
        if nsig >= 2:
            scores[sym] += 0.10 * (nsig - 1)

    # ---- 动量冷却（2026-10-11 王总需求：抑制「追热反转」）----
    # 预测日前 5 个交易日累计涨幅越高，短线均值回归风险越大。实证显示前 5 日已涨的票
    # T+3 命中仅 16%、收益 -4.35%，而前 5 日未涨的票 T+3 命中 33%、收益 -0.36%。
    # 对「热票」按超额涨幅施加冷却系数（乘到综合分上），不硬性剔除，保留少量多样性。
    _bi = {c: _bars_index(v) for c, v in bars_map.items()}
    mom5_map, cool_map = {}, {}
    for csym, k in bars_map.items():
        bi = _bi.get(csym, {})
        idx = bi.get(td)
        m = None
        if idx is not None and idx >= 5:
            c0 = _f(k[idx - 5].get("c"))
            c1 = _f(k[idx].get("c"))
            if c0:
                m = c1 / c0 - 1.0
        mom5_map[csym] = m
        cool_map[csym] = (1.0 if (m is None or m <= COOL_THR)
                          else max(1.0 - COOL_MAX, 1.0 - (m - COOL_THR) / COOL_SLOPE))
    for csym in list(scores):
        cf = cool_map.get(csym, 1.0)
        if cf < 1.0:
            scores[csym] *= cf

    # ---- 第①/②层：板块方向 + 股东结构（2026-10-11 王总三层框架）----
    # 预测日 td 的板块热度聚合 → 取前 TOP_BOARDS 为资金方向；热板块内再用股东结构加成。
    # 局限：policy_news / overseas / sentiment / follow 时间戳不可靠（快照语义），无法精确回溯，
    # 历史日补跑时板块热度是"当前快照"而非"当日真实"——此局限在 run note 已标注。
    hot_boards, sym_board = _hot_sectors(con, td)
    hot_words = {w for w, _ in hot_boards[:TOP_BOARDS]}
    conc_map, holder30_set = _share_conc(con, codes)
    for csym in list(scores):
        sb = sym_board.get(csym)
        extra = 0.0
        if sb and sb[0] in hot_words:
            extra += BOARD_BONUS
        conc = conc_map.get(csym)
        if conc:
            extra += CONC_BONUS.get(conc[0], 0.0)
        if csym in holder30_set:
            extra += HOLDER30_BONUS
        if extra:
            scores[csym] += extra

    # 核心池硬 gate：优先从热板块票里选（方向对）；不足用冷板块补满，避免饿死。
    # 观察池开放全市场，保证覆盖面（前 6 板块仅覆盖约 15/160 只，全靠核心 gate 会饿死）。
    ranked = sorted(scores.items(),
                    key=lambda kv: (-kv[1], -len(hits_by_sym[kv[0]]),
                                    -int(norm.get("tech", {}).get(kv[0], 0) * 100)))
    hot_ranked = [s for s, _ in ranked if sym_board.get(s) and sym_board[s][0] in hot_words]
    cold_ranked = [s for s, _ in ranked if not (sym_board.get(s) and sym_board[s][0] in hot_words)]
    # 2026-10-29：只数由分位带自适应决定（core 3~8、watch ≤10），不再固定 10/10。
    core_k, watch_k = _band_counts([sc for _, sc in ranked])
    core_syms = hot_ranked[:core_k]
    if len(core_syms) < core_k:
        core_syms += cold_ranked[:core_k - len(core_syms)]
    remain = [s for s in (hot_ranked[core_k:] + cold_ranked) if s not in set(core_syms)]
    watch_syms = remain[:watch_k]
    picked = [(s, scores[s]) for s in core_syms + watch_syms]
    if not picked:
        return {"ok": False, "error": "没有股票被任何信号命中，未生成预测"}

    # 5) 落库
    _STATUS["stage"] = "写库"
    pool_n = len(bars_map)
    sig_stats = {k: len(v or {}) for k, v in raw_signals.items()}
    sig_stats["_pool"] = pool_n
    sig_stats["conv_n"] = sum(1 for v in hits_by_sym.values() if len(v) >= 2)
    sig_stats["inst_n"] = sum((_f(v.get("strength"), 0) or 0) for v in inst_raw.values())
    sig_stats["lhb_n"] = sum((_f(v.get("strength"), 0) or 0) for v in lhb_raw.values())
    # 权重快照：前端信号条要同时显示「命中数」和「权重%」，两个口径必须对齐
    sig_stats["_w"] = {k: round(float(weights.get(k, 0.0)), 4) for k in SIGNAL_KEYS}
    sig_stats["_prior"] = {k: SIGNAL_PRIOR.get(k, 1.0) for k in SIGNAL_KEYS}
    # tier 语义随运行记录一起落库/出接口：界面上「core 是什么意思」只能从这里取，
    # 不允许前端自己再编一句「核心持仓」。
    sig_stats["_tier"] = {"core_k": len(core_syms), "definition": TIER_DEFINITION}
    # 池价格状态（普跌联动，2026-10-15）：与宽度同屏落库，market_view 与反向证据共用
    regime = _pool_regime(bars_map)
    if regime:
        sig_stats["_regime"] = {k: round(v, 4) for k, v in regime.items()}
    sec_map = _sector_of(con, [s for s, _ in picked])
    top_sectors = [s for s, _ in Counter(sec_map.values()).most_common(5)]
    view, style = _market_view(sig_stats, {"net": net_senti}, weights, top_sectors, regime)

    con.execute("DELETE FROM prediction_item WHERE run_date=?", (td,))
    con.execute("DELETE FROM prediction_run WHERE run_date=?", (td,))
    items = []
    for i, (sym, sc) in enumerate(picked):
        # 2026-10-31 整改（测评 P1「core 档没有解释」项）：tier 只是**截断位次**。
        # 原实现 core = 综合分排序前 core_k 名，但界面上把它写成「核心」——
        # 一个位次词被读成了质量结论（"值得重仓 / 更可能涨"）。
        # 真实情况是：core 与 watch 的收益差**从未被检验过**，本系统没有这个证据。
        # 所以不改下游用法（fund_masters / selfpick 都在用 tier），
        # 只把定义钉在唯一出口上，随接口一起返回，见 TIER_DEFINITION。
        tier = "core" if i < len(core_syms) else "watch"
        k = bars_map.get(sym) or []
        entry = _f(k[-1].get("c")) if k else None
        nxt = _f(k[-1].get("c")) if k else None      # 次日开盘需 T+1 才有，复盘时回填
        hs = sorted(hits_by_sym[sym], key=lambda h: -h["weight"])
        reason = " + ".join("%s(%.0f%%)" % (h["name"], h["weight"] * 100) for h in hs[:3])
        ev = []
        for h in hs[:3]:
            ev.extend(h["evidence"][:1])
        if ev:
            reason += "｜" + "；".join(ev[:3])
        nm = names.get(sym) or sym
        m5 = mom5_map.get(sym)
        coolf = cool_map.get(sym, 1.0)
        if coolf < 1.0:
            reason += "｜动量冷却×%.2f（前5日%s%.1f%%）" % (
                coolf, "涨" if (m5 or 0) >= 0 else "跌", (m5 or 0) * 100)
        # 第①/②层标注：板块方向 + 股东结构（三层框架可见化）
        sb0 = sym_board.get(sym)
        bd0 = sb0[0] if sb0 else ""
        if bd0:
            bk = next((i + 1 for i, (w, _) in enumerate(hot_boards) if w == bd0), None)
            reason += "｜板块:%s" % bd0 + ("(热度第%d)" % bk if bk else "")
        conc0 = conc_map.get(sym)
        conc0_tier = conc0[0] if conc0 else "未知"
        if conc0:
            reason += "｜股东:%s" % conc0_tier + ("·板块前30%" if sym in holder30_set else "")
        h30i = 1 if sym in holder30_set else 0
        caveat = "未到期"
        if "ST" in str(nm).upper() or "退" in str(nm):
            caveat = "⚠ ST/退市风险警示，短线波动限制 5%"
        elif entry is None:
            caveat = "⚠ 暂无完整K线，复盘收益待补"
        items.append((td, sym, nm, tier, i + 1, round(sc, 4),
                      json.dumps(hs, ensure_ascii=False), reason, entry, nxt,
                      None, None, None, None, None, None, None, None, None,
                      caveat, None,
                      round(m5, 4) if m5 is not None else None,
                      round(coolf, 4), bd0, conc0_tier, h30i))
    con.executemany(
        "INSERT OR REPLACE INTO prediction_item (run_date,symbol,name,tier,rank,score,"
        "hit_signals,reason,entry_close,next_open,ret_t1,ret_t3,ret_t5,"
        "bench_t1,bench_t3,bench_t5,hit_t1,hit_t3,hit_t5,caveat,reviewed_at,mom5,cool,"
        "board,conc,holder30) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", items)
    _wstr = "、".join("%s %.0f%%" % (SIGNAL_META[k]["name"], weights.get(k, 0) * 100)
                      for k in SIGNAL_KEYS)
    _bstr = "、".join("%s" % w for w, _ in hot_boards[:TOP_BOARDS])
    _note = "候选池 %d 只；K线截至 %s（asof隔离）；权重：%s；方向(前%d热板块)：%s" % (
        pool_n, td, _wstr, TOP_BOARDS, _bstr) + desk_note
    con.execute(
        "INSERT OR REPLACE INTO prediction_run (run_date,created_at,engine_ver,pool_n,signal_n,"
        "core_n,watch_n,market_view,style_view,breadth_json,bench_json,status,note) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (td, _now(), ENGINE_VER, pool_n, len(SIGNAL_KEYS),
         len(core_syms), len(watch_syms),
         view, style, json.dumps(sig_stats, ensure_ascii=False), None, "ok", _note))
    con.commit()
    try:
        res = rescore(con, log=True)     # 每次生成后刷新权重（把最新复盘结果纳入下一次判断）
        # 口径对齐：前端「N路信号贡献」读 breadth_json._w，「N路信号权重」读 predictor_score，
        # 二者必须同源，否则同一次生成里两张表数字对不上（2026-10-01 实测踩到）。
        w_after = (res or {}).get("weights") or {}
        if w_after:
            row = con.execute("SELECT breadth_json FROM prediction_run "
                              "WHERE run_date=?", (td,)).fetchone()
            if row:
                try:
                    b = json.loads(row["breadth_json"] or "{}") or {}
                    b["_w"] = {k: round(float(v), 4) for k, v in w_after.items()}
                    con.execute("UPDATE prediction_run SET breadth_json=? WHERE run_date=?",
                                (json.dumps(b, ensure_ascii=False), td))
                    con.commit()
                except Exception:  # noqa: BLE001
                    pass
    except Exception:  # noqa: BLE001
        pass
    return {"ok": True, "run_date": td, "core": len(core_syms),
            "watch": len(watch_syms), "pool_n": pool_n,
            "elapsed": round(time.time() - t0, 2),
            "signals": sig_stats, "view": view, "style": style}


# ---------------------------------------------------------------- 复盘
def _bars_index(k: list) -> dict:
    return {str(b.get("d", ""))[:10]: i for i, b in enumerate(k)}


def review(con: sqlite3.Connection, force: bool = False, progress=None) -> dict:
    """对所有已生成预测做到期窗口复盘：T+1/T+3/T+5 收益 + 是否跑赢候选池平均。"""
    ensure_tables(con)
    runs = [r["run_date"] for r in con.execute(
        "SELECT run_date FROM prediction_run ORDER BY run_date")]
    if not runs:
        return {"ok": True, "reviewed": 0, "note": "尚无预测记录"}
    kc = load_kcache()
    codes, _names, _b = SS.universe(POOL_LIMIT)
    bars_map = fetch_bars(codes, kc, force=False,
                          progress=(lambda a, b: progress and progress("抓 K 线", a, b)))
    pool_curves = {c: _bars_index(v) for c, v in bars_map.items() if len(v) >= 30}

    done_items = 0
    done_runs = 0
    for rd in runs:
        items = con.execute("SELECT * FROM prediction_item WHERE run_date=?", (rd,)).fetchall()
        if not items:
            continue
        # 基准：候选池全部票在 T 起第 w 个交易日的同期平均收益
        bench = {}
        for w in WINDOWS:
            rets = []
            for c, idx in pool_curves.items():
                r = _window_ret(bars_map[c], idx.get(rd), w)
                if r is not None:
                    rets.append(r)
            bench[w] = (sum(rets) / len(rets)) if rets else None
        upd, pending = [], 0
        for it in items:
            sym = it["symbol"]
            k = bars_map.get(sym) or []
            idx = pool_curves.get(sym, {}).get(rd)
            entry = _f(it["entry_close"])
            if entry is None and idx is not None:
                entry = _f(k[idx].get("c"))
            row = {"hit": {}, "hit2": {}, "ret": {}, "ret2": {}, "caveat": [], "series": []}
            if idx is None or entry in (None, 0):
                pending += 1
                # ⚠️ 与下方 UPDATE 的 17 个占位符严格对齐：4 ret + 4 bench + 4 hit
                #    + series + caveat + reviewed_at + run_date + symbol = 17。
                #    旧版多给一个 None（18 值）→ 只要有一只票缺K线，executemany 整体
                #    报 "Incorrect number of bindings (17 vs 18)"，当天复盘/权重更新全失效。
                upd.append((None, None, None, None, None, None, None, None, None,
                            None, None, None, None,
                            None, None, None, None, None, None, None, None,
                            "无K线数据", None, rd, sym))
                continue
            if idx + 1 < len(k):
                row["next_open"] = _f(k[idx + 1].get("o"))
            # P1-03：统一「可否成交」判定（board_rules.tradeable）。信号日涨停→无法买入、停牌→无成交，
            # 这种 entry 的收益不可兑现，混入复盘会虚抬命中率；直接标灰跳过，不污染统计。
            try:
                from board_rules import tradeable as _tradeable
                if not _tradeable(k[idx], "buy", sym, (it.get("name") or ""), rd):
                    pending += 1
                    upd.append((None, None, None, None, None, None, None, None, None,
                                None, None, None, None,
                                None, None, None, None,
                                None, None, None, None,
                                "涨停/停牌不可买入，收益不可兑现", None, rd, sym))
                    continue
            except Exception:  # noqa: BLE001  board_rules 不可用时不阻断复盘
                pass
            # P1-04：净收益闸门——命中判定用「扣交易成本后的净收益」对比基准（佣金/印花/滑点来回约 0.3%），
            # 避免把刚够覆盖成本的微弱超额当成真命中。成本用 screener_extra._trade_cost 折算成收益率口径。
            try:
                from screener_extra import _trade_cost as _tc, INIT_CAPITAL as _N0
                _N = _N0
                _cost_rate = (_tc(_N, True, sym, 1.0) + _tc(_N, False, sym, 1.0)) / _N
            except Exception:  # noqa: BLE001
                _cost_rate = 0.003
            for w in WINDOWS:
                r = _window_ret(k, idx, w)
                if r is None:
                    continue          # 未到期
                b = bench.get(w)
                row["ret"][w] = r
                row["hit"][w] = (1 if (b is not None and r > b) else 0)
                # 整改 #16 + P1-02：并列算 T+1 开盘口径（含双边滑点），P1-04：命中判定改用净收益。
                r2 = _window_ret2(k, idx, w)
                if r2 is not None:
                    row["ret2"][w] = r2
                    r2_net = r2 - _cost_rate
                    row["hit2"][w] = (1 if (b is not None and r2_net > b) else 0)
            # 逐日曲线（沉淀）：自预测日次日起，最多 MAX_SERIES 个交易日
            for j in range(idx + 1, min(len(k), idx + 1 + MAX_SERIES)):
                c = _f(k[j].get("c"))
                if c is None:
                    continue
                pc = _f(k[j - 1].get("c")) if j > 0 else entry
                row["series"].append({
                    "d": str(k[j].get("d", ""))[:10],
                    "c": round(c, 2),
                    "chg": round(c / pc - 1, 4) if pc else None,
                    "ret": round(c / entry - 1, 4),   # 相对预测日收盘的累计收益
                })
            if not row["ret"]:
                pending += 1
            # 除权粗检：首根交易日跌幅 < -11%（非跌停口径）→ 疑似除权，标注
            if idx + 1 < len(k):
                r1 = _window_ret(k, idx, 3)
                if r1 is not None and r1 < -0.115:
                    row["caveat"].append("首个交易日跌幅异常（疑似除权/停牌）")
            if row["hit"] and len(row["hit"]) == len(WINDOWS):
                done_items += 1
            upd.append((row["ret"].get(1), row["ret"].get(3), row["ret"].get(5), row["ret"].get(10),
                        bench.get(1), bench.get(3), bench.get(5), bench.get(10),
                        row["hit"].get(1), row["hit"].get(3), row["hit"].get(5), row["hit"].get(10),
                        row["ret2"].get(1), row["ret2"].get(3), row["ret2"].get(5), row["ret2"].get(10),
                        row["hit2"].get(1), row["hit2"].get(3), row["hit2"].get(5), row["hit2"].get(10),
                        json.dumps(row["series"], ensure_ascii=False),
                        ("未到期" if pending and not row["ret"] else
                         ("；".join(row["caveat"]) if row["caveat"] else None)),
                        _now(), rd, sym))
        con.executemany(
            "UPDATE prediction_item SET ret_t1=?,ret_t3=?,ret_t5=?,ret_t10=?,"
            "bench_t1=?,bench_t3=?,bench_t5=?,bench_t10=?,"
            "hit_t1=?,hit_t3=?,hit_t5=?,hit_t10=?,"
            "ret2_t1=?,ret2_t3=?,ret2_t5=?,ret2_t10=?,"
            "hit2_t1=?,hit2_t3=?,hit2_t5=?,hit2_t10=?,"
            "series_json=COALESCE(?,series_json),"
            "caveat=COALESCE(?,caveat),reviewed_at=? "
            "WHERE run_date=? AND symbol=?", upd)
        con.execute("UPDATE prediction_run SET bench_json=? WHERE run_date=?",
                    (json.dumps({"t%d" % w: bench.get(w) for w in WINDOWS},
                                ensure_ascii=False), rd))
        done_runs += 1
        if progress:
            progress("复盘", done_runs, len(runs))

    # 按信号/流派/策略聚合命中率
    _aggregate(con)
    con.commit()
    res = rescore(con, log=True)
    # ── 阶段3 合理性闸门（最小侵入·仅告警·不阻断·不改计算）──
    # 对已复盘的实现单日收益(ret_t1)做日内合理性校验（口径对齐 daily_return 闸门）
    _apply_reasonableness_gate_predictor(con)
    return {"ok": True, "reviewed": done_items, "runs": done_runs,
            "weights": res.get("weights"), "adjusted": res.get("adjusted")}


def _apply_reasonableness_gate_predictor(con) -> None:
    """对已复盘实现收益做日内合理性校验（仅告警，不改计算、不阻断）。

    - 闸门模块缺失或异常时静默跳过（生产零依赖、零污染）。
    - 仅取 1 交易日收益（ret_t1）做 daily_return 闸门，不重新计算任何公式。
    - 生产模式（test_mode=False）下越界只写告警，不抛异常、不影响返回。
    """
    try:
        try:
            from reasonableness_gate import run_gates  # 同目录平级导入
        except ImportError:
            from viewer.reasonableness_gate import run_gates  # 包内导入兜底
    except Exception:  # noqa: BLE001
        return
    try:
        rows = con.execute(
            "SELECT ret_t1 FROM prediction_item WHERE ret_t1 IS NOT NULL").fetchall()
        realized = [r["ret_t1"] for r in rows if r["ret_t1"] is not None]
        if not realized:
            return
        ctx = {"module": "viewer/predictor.py", "function": "review", "test_mode": False}
        run_gates({"daily_returns": realized}, ctx)
    except Exception:  # noqa: BLE001
        # 生产模式闸门不应抛；任何异常都不影响业务返回
        pass


def _window_ret(k: list, idx: int | None, w: int) -> float | None:
    """从 idx 起第 w 个交易日的收益（相对 idx 收盘）。未到期返回 None。"""
    if idx is None:
        return None
    j = idx + w
    if j >= len(k) or idx < 0:
        return None
    c0 = _f(k[idx].get("c"))
    c1 = _f(k[j].get("c"))
    if not c0 or c1 is None:
        return None
    return c1 / c0 - 1.0


def _window_ret2(k: list, idx: int | None, w: int) -> float | None:
    """T+1 开盘 → 第 w 个交易日收盘 的收益（2026-10-12 整改 #16）。

    与原 `_window_ret` 的区别**只在入场价**：
      · 原口径：T 日收盘（相当于"看到信号的当天收盘价就能买到"，偏乐观）；
      · 本口径：T+1 开盘（次日开盘才买得进，更贴近真实可成交性）。
    出场日与原口径一致（同为 idx+w 收盘），便于两口径并列对照。
    T+1 无开盘价（尚未产生 / 停牌）时退化为原口径，不返回 None，避免整条丢失。
    """
    if idx is None or idx < 0:
        return None
    if idx + 1 >= len(k):
        return None
    o1 = _f(k[idx + 1].get("o"))
    if not o1:
        return _window_ret(k, idx, w)
    j = idx + w
    if j >= len(k):
        return None
    c1 = _f(k[j].get("c"))
    if c1 is None:
        return None
    # P1-02：T+1 开盘买入付买入滑点、T+w 收盘卖出付卖出滑点，净收益口径扣双边滑点
    # （与 screener_extra.SLIPPAGE_RATE=0.001 对齐）。这是「rescore 主轴切 T+1 开盘+滑点」的收益侧落地。
    _SLIP = 0.001
    return (c1 * (1 - _SLIP)) / (o1 * (1 + _SLIP)) - 1.0


def _aggregate(con: sqlite3.Connection) -> dict:
    """把每一条已复盘记录回填到信号/流派/策略三个层级的记分表。"""
    _ensure_score_rows(con, None)
    con.execute("UPDATE predictor_score SET n_t1=0,n_hit_t1=0,sum_ret_t1=0,"
                "n_t3=0,n_hit_t3=0,sum_ret_t3=0,"
                "n_t5=0,n_hit_t5=0,sum_ret_t5=0,n_t10=0,n_hit_t10=0,sum_ret_t10=0 "
                "WHERE kind IN ('signal','school','strategy')")
    # 主参考窗口（王总 2026-10-06）：早于 window_start 的复盘样本不再参与权重统计 ——
    # 太早的样本对当前策略没参考价值，混进来只会把近期特征冲淡。样本本身不删，仍留库可查。
    _ws = window_start()
    # 2026-10-29：把 bench_* 一并取出。此前只 SELECT 了 ret_*，导致下面只能累积
    # 「绝对收益」。而 prediction_item 每行早就存了同期池子基准 bench_t*，等于把
    # 手边的真超额浪费掉，后面 _excess_ret 只能拿 signal_baseline（全市场 250 日）
    # 去减，样本期完全对不上 —— 实测 ann 出现「命中率 60% 但超额 -2.37%」这种
    # 自相矛盾的读数，根因就在这里（base1 还因为基线表只有 5/10/20 而默认成 0）。
    _SQL = ("SELECT run_date,symbol,ret_t1,ret_t3,ret_t5,ret_t10,"
            "bench_t1,bench_t3,bench_t5,bench_t10,"
            "hit_t1,hit_t3,hit_t5,hit_t10,hit_signals FROM prediction_item")
    rows = con.execute(_SQL + (" WHERE run_date >= ?" if _ws else ""),
                       ((_ws,) if _ws else ())).fetchall()
    # 用 K 线重算「每只票在其预测日触发了哪些策略」代价太高 → 只对技术面用存储的命中明细，
    # 其余信号用 item 上的 hit_signals 中的 key 归属。流派/策略层面依赖 hit_signals 里的
    # strategy 列表（生成时已随 tech 信号写入，格式见下）。
    agg: dict = defaultdict(lambda: {"n1": 0, "h1": 0, "s1": 0.0,
                                     "n3": 0, "h3": 0, "s3": 0.0,
                                     "n5": 0, "h5": 0, "s5": 0.0,
                                     "n10": 0, "h10": 0, "s10": 0.0})
    for r in rows:
        try:
            hs = json.loads(r["hit_signals"] or "[]")
        except Exception:  # noqa: BLE001
            hs = []
        keys = []
        for h in hs:
            keys.append(h.get("key"))
            for st in (h.get("strategies") or []):
                keys.append(st)
            for sc in (h.get("schools") or []):
                keys.append("school:" + sc)
        for kk in set(x for x in keys if x):
            a = agg[kk]
            for w, (nf, hf, sf) in ((1, ("n1", "h1", "s1")), (3, ("n3", "h3", "s3")),
                                    (5, ("n5", "h5", "s5")), (10, ("n10", "h10", "s10"))):
                ret = r["ret_t%d" % w]
                hit = r["hit_t%d" % w]
                if ret is None or hit is None:
                    continue
                a[nf] += 1
                a[hf] += int(hit)
                # sum_ret_* 自 2026-10-29 起存的是**超额**（ret − 同期池子 bench），
                # 不是绝对收益。bench 缺失（极早期行）时才退化用绝对收益，并在下方
                # 的 detail 里能看出该路基线质量。改语义是安全的：本函数每次全量清零
                # 重算，库里不会留下旧口径的累加值。
                ben = r["bench_t%d" % w]
                a[sf] += (float(ret) - float(ben)) if ben is not None else float(ret)
    _aggregate_window = {"start": _ws or "全部历史", "items": len(rows),
                         "years": LOOKBACK_YEARS}
    for kk, a in agg.items():
        con.execute(
            "UPDATE predictor_score SET n_t1=?,n_hit_t1=?,sum_ret_t1=?,"
            "n_t3=?,n_hit_t3=?,sum_ret_t3=?,"
            "n_t5=?,n_hit_t5=?,sum_ret_t5=?,n_t10=?,n_hit_t10=?,sum_ret_t10=?,updated_at=? "
            "WHERE signal_key=?", (a["n1"], a["h1"], round(a["s1"], 4),
                                   a["n3"], a["h3"], round(a["s3"], 4),
                                   a["n5"], a["h5"], round(a["s5"], 4),
                                   a["n10"], a["h10"], round(a["s10"], 4), _now(), kk))
    con.commit()
    return dict(agg)


# ---------------------------------------------------------------- 前端载荷
# ---- 样本外验证（A3-2 整改，2026-10-13）------------------------------------
# 规格：交叉验证按时间切分（80% 训练 / 20% 最新样本外），**样本外命中率必须单独展示**，
# 禁止只给全样本命中率（全样本含训练集，数字会虚高）。本段把 prediction_run 的全部
# 预测交易日按时间序切成「训练段 / 样本外段」，只统计样本外段在 T+1/T+3/T+5/T+10
# 的实盘命中，与训练段、全样本分开返回，绝不混合成一个数。
OOS_TEST_FRACTION = 0.20         # 最新 20% 交易日作为样本外测试段
OOS_TRAIN_FRACTION = 0.80        # 前 80% 交易日作为训练段
OOS_MIN_RUNS = 3                 # 样本外至少要有这么多天，才单独报命中率


def _binomial_ci(k, n, z=1.96):
    """二项比例 95% 置信区间（Wald 近似）。返回 (low, high) 或 None（n=0）。

    P1-06：命中率配套 95% CI——样本少时点估计方差大、区间宽，前端据此标「不可信」，
    而不是只给一个看似精确的点估计（避免小样本命中率被当成铁律）。
    """
    if not n:
        return None
    p = k / n
    se = math.sqrt(p * (1 - p) / n)
    lo = max(0.0, p - z * se)
    hi = min(1.0, p + z * se)
    return (round(lo, 4), round(hi, 4))


def _out_of_sample_stats(con: sqlite3.Connection) -> dict:
    """预测引擎样本外命中率（按时间切分，A3-2）。

    口径：
      - 拿 prediction_run 的 run_date（预测交易日序）按 80% 训练 / 20% 样本外切分；
      - 样本外段的命中率只统计 prediction_item.hit_*（跑赢候选池同期等权平均），
        未到期窗口为 NULL 自然不计入样本 —— 缺失不是 0，更不是命中；
      - 返回 train(in_sample) / out_of_sample(test) / compare 三段，前端单独展示样本外命中率，
        避免「全样本（含训练集）数字虚高」的自欺。
    """
    wrap = {"ok": True,
            "split": "80% 训练 / 20% 样本外（按预测交易日序）",
            "split_note": "样本外命中率单独展示（非全样本）：全样本含训练集，数字会虚高",
            "in_sample": None, "out_of_sample": None, "oos_hit": None,
            "compare": []}
    try:
        runs = [r["run_date"] for r in con.execute(
            "SELECT run_date FROM prediction_run ORDER BY run_date").fetchall()]
        if len(runs) < OOS_MIN_RUNS + 1:
            wrap["ok"] = False
            wrap["note"] = "预测样本不足，无法按时间切分样本外（至少需要 %d 个预测交易日）" % (OOS_MIN_RUNS + 1)
            return wrap
        n = len(runs)
        k = n - max(OOS_MIN_RUNS, int(round(n * OOS_TEST_FRACTION)))
        if k < 1:
            k = 1
        train_dates, test_dates = runs[:k], runs[k:]

        def _seg(dates) -> dict:
            acc = {w: [0, 0] for w in (1, 3, 5, 10)}     # [样本数, 命中数]
            if not dates:
                return {}
            q = ",".join("?" * len(dates))
            rows = con.execute(
                "SELECT hit_t1,hit_t3,hit_t5,hit_t10 FROM prediction_item "
                "WHERE run_date IN (%s)" % q, dates).fetchall()
            for r in rows:
                for w in (1, 3, 5, 10):
                    h = r["hit_t%d" % w]
                    if h is not None:            # 未到期 = None = 不计数，不是「没命中」
                        acc[w][0] += 1
                        acc[w][1] += int(h)
            return {("t%d" % w): {"n": acc[w][0], "hit": acc[w][1],
                                  "hit_rate": round(acc[w][1] / acc[w][0], 4) if acc[w][0] else None,
                                  "ci95": _binomial_ci(acc[w][1], acc[w][0])}
                    for w in (1, 3, 5, 10)}

        train = _seg(train_dates)
        test = _seg(test_dates)
        wrap["train_start"] = train_dates[0]
        wrap["train_end"] = train_dates[-1]
        wrap["test_start"] = test_dates[0]
        wrap["test_end"] = test_dates[-1]
        wrap["train_n"] = len(train_dates)
        wrap["test_n"] = len(test_dates)
        wrap["in_sample"] = train
        wrap["out_of_sample"] = test
        # oos_hit：样本外（测试段）各窗口命中率，供前端/复盘面板单独展示
        wrap["oos_hit"] = {("t%d" % w): (test.get("t%d" % w) or {}).get("hit_rate")
                           for w in (1, 3, 5, 10)}
        # 样本内 vs 样本外对比（可读行）
        for w in (1, 3, 5, 10):
            tr = (train.get("t%d" % w) or {})
            te = (test.get("t%d" % w) or {})
            wrap["compare"].append({
                "window": "T+%d" % w,
                "train_n": tr.get("n", 0), "train_rate": tr.get("hit_rate"), "train_ci": tr.get("ci95"),
                "test_n": te.get("n", 0), "test_rate": te.get("hit_rate"), "test_ci": te.get("ci95"),
            })
        return wrap
    except Exception:  # noqa: BLE001 —— 统计失败不阻塞预测接口
        wrap["ok"] = False
        wrap["error"] = "样本外切分统计失败：%s" % traceback.format_exc(limit=1)
        return wrap


# ---- walk-forward 滚动报告（A3-3 整改，2026-10-13）--------------------------
# 规格：engine 每次调整权重后，必须用滚动窗口重跑最近 250 个交易日的样本外命中率，
# 生成「命中率轨迹图」，并与 predictor_adjust_log 的每次调整原因一一对齐展示。
# 这里实现「滚动窗口重跑最近 250 交易日样本外命中率」：
#   - 对每个预测交易日 d，只用「截至 d 之前（含）」的到期样本（T+1 主轴）算滚动窗口命中率；
#   - 每一步都不使用 d 之后的数据 → 轨迹点天然点内时点（与 A3-1 时间穿越防护同口径）；
#   - 调权记录（predictor_adjust_log）原样返回，前端把「某次调权 → 之后命中率变化」对齐看。
WF_WINDOW = 250                 # 滚动窗口：最近 250 个交易日
WF_MIN_WINDOW = 20              # 窗口内至少这么多到期样本才产出轨迹点
WF_TRACK_WINDOWS = (1, 3, 5)    # 轨迹主轴窗口（T+1 先到期，样本最多）


def _walk_forward_track(con: sqlite3.Connection) -> dict:
    """walk-forward 滚动报告：最近 250 个交易日样本外命中率轨迹 + 调权对齐。

    返回：
      - points: [{run_date, window, n, oos_hit_rate, ...}] 滚动窗口样本外命中率轨迹；
      - aligns: predictor_adjust_log 的每次调整（时间 + 信号 + 原因 + 调整前后权重），
        供前端与轨迹一一对齐（某次调权之后命中率是否改善）。
    """
    wrap = {"ok": True,
            "method": "walk-forward：滚动窗口重跑最近 250 个交易日样本外命中率",
            "window": WF_WINDOW, "points": [], "aligns": [],
            "note": "命中率轨迹：横轴=预测日，纵轴=截至该日滚动窗口的样本外命中率；"
                    "每次调权（predictor_adjust_log）在轨迹上对齐标注，观察调权后命中率变化。"}
    try:
        runs = [r["run_date"] for r in con.execute(
            "SELECT run_date FROM prediction_run ORDER BY run_date").fetchall()]
        if not runs:
            wrap["ok"] = False
            return wrap
        # 一次取全量到期样本，按 run_date 分组，避免 N+1 查询
        all_rows = con.execute(
            "SELECT run_date, hit_t1, hit_t3, hit_t5 FROM prediction_item "
            "WHERE run_date IN (SELECT run_date FROM prediction_run)").fetchall()
        by_day: dict = {}
        for r in all_rows:
            bag = by_day.setdefault(r["run_date"], {w: [0, 0] for w in WF_TRACK_WINDOWS})
            for w in WF_TRACK_WINDOWS:
                h = r["hit_t%d" % w]
                if h is not None:          # 未到期 = None = 不计数，不是「没命中」
                    bag[w][0] += 1
                    bag[w][1] += int(h)
        # 双端队列维护「最近 WF_WINDOW 个预测交易日」的滚动累计
        win: deque = deque()
        acc = {w: [0, 0] for w in WF_TRACK_WINDOWS}
        for d in runs:
            day = by_day.get(d, {w: [0, 0] for w in WF_TRACK_WINDOWS})
            win.append(day)
            for w in WF_TRACK_WINDOWS:
                acc[w][0] += day[w][0]
                acc[w][1] += day[w][1]
            if len(win) > WF_WINDOW:
                out = win.popleft()
                for w in WF_TRACK_WINDOWS:
                    acc[w][0] -= out[w][0]
                    acc[w][1] -= out[w][1]
            # T+1 为主轴（到期样本最多），其它窗口作补充
            n1 = acc[1][0]
            if n1 >= WF_MIN_WINDOW:
                point = {"run_date": d, "window": min(len(win), WF_WINDOW),
                         "n": n1, "oos_hit_rate": round(acc[1][1] / n1, 4)}
                for w in (3, 5):
                    nw = acc[w][0]
                    point["t%d_n" % w] = nw
                    point["t%d_oos_hit_rate" % w] = round(acc[w][1] / nw, 4) if nw else None
                wrap["points"].append(point)
        # 调权对齐：predictor_adjust_log 每次调整（时间序）
        for r in con.execute(
                "SELECT log_date, signal_key, kind, weight_before, weight_after, reason "
                "FROM predictor_adjust_log ORDER BY log_date ASC, id ASC").fetchall():
            wrap["aligns"].append({
                "log_date": r["log_date"] or "", "signal_key": r["signal_key"] or "",
                "kind": r["kind"] or "", "weight_after": r["weight_after"],
                "weight_before": r["weight_before"], "reason": r["reason"] or "",
            })
        return wrap
    except Exception:  # noqa: BLE001 —— 报告失败不阻塞预测接口
        wrap["ok"] = False
        wrap["error"] = "walk-forward 滚动报告计算失败"
        return wrap


def _score_row(r) -> dict:
    def wr(n, h):
        n, h = int(n or 0), int(h or 0)
        return {"n": n, "hit": h, "rate": round(h / n, 4) if n else None}

    def ex(n, s):
        """平均**超额**收益（2026-10-29 起 sum_ret_* 已是 ret − 同期池子 bench）。

        字段名保留 avg_ret_* 是为了不破坏既有调用方，但语义已从「平均收益」
        改为「平均超额」。新增 avg_ex_* 别名，新代码请用它，避免与绝对收益混淆。
        """
        return round((s or 0) / n, 4) if n else None
    return {
        "key": r["signal_key"], "kind": r["kind"], "name": r["name"], "school": r["school"],
        "t1": wr(r["n_t1"], r["n_hit_t1"]),
        "t3": wr(r["n_t3"], r["n_hit_t3"]), "t5": wr(r["n_t5"], r["n_hit_t5"]),
        "t10": wr(r["n_t10"], r["n_hit_t10"]),
        "avg_ret_t1": ex(r["n_t1"], r["sum_ret_t1"]),
        "avg_ret_t3": ex(r["n_t3"], r["sum_ret_t3"]),
        "avg_ret_t5": ex(r["n_t5"], r["sum_ret_t5"]),
        "avg_ret_t10": ex(r["n_t10"], r["sum_ret_t10"]),
        "avg_ex_t1": ex(r["n_t1"], r["sum_ret_t1"]),
        "avg_ex_t5": ex(r["n_t5"], r["sum_ret_t5"]),
        "weight": r["weight"], "weight_prev": r["weight_prev"],
    }


# 2026-10-12 整改 A3-1：时点纪律守卫（防"时间穿越"）。
# 规格书要求「代码层面在特征构造入口统一加时点校验（特征日期 ≤ 预测日），并写单元测试
# 证明把未来数据喂进去会被拒绝」。本项目多数信号早已带 asof 边界（如 sig_inst 只统计
# trade_date <= asof），但那只是"约定"；这里给一个**可断言、可单测**的统一入口。
class LookaheadError(AssertionError):
    """使用了晚于预测日的数据（前视偏差）。"""


def assert_point_in_time(asof: str | None, *candidate_dates) -> None:
    """任何入模特征的日期都不得晚于预测日 asof；违反即抛 LookaheadError。

    asof 为 None → 表示"不设边界"（全量重建/历史回放场景），跳过检查。
    日期一律按 YYYY-MM-DD 字符串比较（ISO 序即时间序）。
    """
    if not asof:
        return
    a = str(asof)[:10]
    for d in candidate_dates:
        if d is None or d == "":
            continue
        s = str(d)[:10]
        if s > a:
            raise LookaheadError(
                "前视偏差：特征日期 %s 晚于预测日 %s —— 拒绝使用未来数据" % (s, a))


# 2026-10-12 整改 #21：今日推荐 payload 固定附带的风险声明（合规：非投资建议）。
DISCLAIMER_TEXT = "以上为公开讨论情绪与历史统计的信号，非投资建议；回测不代表未来。"


def today_payload(con: sqlite3.Connection | None = None) -> dict:
    own = con is None
    con = con or connect()
    try:
        ensure_tables(con)
        rd = con.execute("SELECT MAX(run_date) AS d FROM prediction_run").fetchone()
        rid = rd["d"] if rd else None
        if not rid:
            return {"ok": True, "has_data": False, "signals": SIGNALS,
                    "engine_ver": ENGINE_VER, "status": status_snapshot()}
        run = con.execute("SELECT * FROM prediction_run WHERE run_date=?", (rid,)).fetchone()
        items = con.execute("SELECT * FROM prediction_item WHERE run_date=? ORDER BY rank",
                            (rid,)).fetchall()
        out_items = []
        for it in items:
            try:
                hs = json.loads(it["hit_signals"] or "[]")
            except Exception:  # noqa: BLE001
                hs = []
            try:
                series = json.loads(it["series_json"] or "[]")
            except Exception:  # noqa: BLE001
                series = []
            out_items.append({
                "symbol": it["symbol"], "name": it["name"], "tier": it["tier"],
                "rank": it["rank"], "score": it["score"], "signals": hs,
                "reason": it["reason"], "entry_close": it["entry_close"],
                "next_open": it["next_open"],
                "board": it["board"], "conc": it["conc"], "holder30": it["holder30"],
                "ret": {"t1": it["ret_t1"], "t3": it["ret_t3"], "t5": it["ret_t5"], "t10": it["ret_t10"]},
                "bench": {"t1": it["bench_t1"], "t3": it["bench_t3"], "t5": it["bench_t5"], "t10": it["bench_t10"]},
                "hit": {"t1": it["hit_t1"], "t3": it["hit_t3"], "t5": it["hit_t5"], "t10": it["hit_t10"]},
                # 整改 #16：T+1 开盘入场口径（并列对照）
                "ret2": {"t1": it["ret2_t1"], "t3": it["ret2_t3"], "t5": it["ret2_t5"], "t10": it["ret2_t10"]},
                "hit2": {"t1": it["hit2_t1"], "t3": it["hit2_t3"], "t5": it["hit2_t5"], "t10": it["hit2_t10"]},
                "series": series,
                "caveat": it["caveat"],
            })
        # 整改 #16：两种入场口径的命中率对照（让"T收盘口径偏乐观"这件事可见）
        def _rate(key: str, w: int):
            vals = [x[key][w] for x in out_items if x[key].get(w) is not None]
            return round(sum(vals) / len(vals) * 100, 1) if vals else None
        hit_compare = {
            "close_entry": {w: _rate("hit", w) for w in (1, 3, 5, 10)},
            "open_entry": {w: _rate("hit2", w) for w in (1, 3, 5, 10)},
        }
        # 最近一次权重调整理由（用于「我在自我修正」的可视化）
        adj = [dict(r) for r in con.execute(
            "SELECT * FROM predictor_adjust_log ORDER BY id DESC LIMIT 8").fetchall()]
        stale = _pred_stale(rid)
        out = {
            "ok": True, "has_data": True, "run_date": rid,
            "created_at": run["created_at"], "engine_ver": run["engine_ver"],
            "pool_n": run["pool_n"], "core_n": run["core_n"], "watch_n": run["watch_n"],
            "market_view": run["market_view"], "style_view": run["style_view"],
            "breadth": json.loads(run["breadth_json"] or "{}"),
            "bench": json.loads(run["bench_json"] or "null"),
            "status": run["status"], "note": run["note"],
            "items": out_items, "adjust_log": adj,
            "signals": SIGNALS, "windows": list(WINDOWS),
            "engine_status": status_snapshot(),
            "disclaimer": DISCLAIMER_TEXT,   # 整改 #21
            # 整改 #16：入场口径对照（close=T日收盘口径 / open=T+1开盘口径）
            "hit_compare": hit_compare,
            "entry_basis_note": ("hit/ret 为 T 日收盘入场口径；hit2/ret2 为 T+1 开盘入场口径"
                                 "（更贴近真实可成交性，命中率通常更低，属「变诚实」而非「变差」）。"),
            # A3-5（2026-10-13）：反向证据列表 —— 谁在反对这个结论，机会与风险同屏
            "against": _against_evidence(con, run),
        }
        if stale:
            out["stale"] = True
            out["last_trade_date"] = stale["last_trade_date"]
            out["lag_days"] = stale["lag_days"]
            out["stale_reason"] = stale["reason"]
        else:
            out["stale"] = False
            out["stale_reason"] = ""
        return out
    finally:
        if own:
            con.close()


# ===== A3-5 反向证据列表（2026-10-13）：谁在反对这个结论 =====
_AGAINST_MAX = 6


def _against_evidence(con: sqlite3.Connection, run) -> list:
    """针对当日研判结论生成「反向证据列表」—— 只使用落库的真实数据，绝不无中生有。

    语义：市场研判结论说明「谁在推进」，反向证据则回答「谁在反对这个结论、谁至少没配合」，
    机会与风险同屏展示，避免只看到看多一面（偏食）。每条都写明「谁 in 反对、反对什么、
    数据源自哪」；确实没有反向证据时返回 []，由前端如实显示「暂无反向证据」，不硬凑。
    注意：这不是对结论的否定判断，而是结论成立所需的反向条件清单，用于让用户自己权衡。

    反向证据来源（全部真实可查，与 generate 阶段同口径）：
      ① 市场宽度不足：多信号共振只数占比 < 35% → 大多数候选票没被多路独立信号证实；
      ② 论坛情绪对立：近 LOOKBACK_DAYS 天情绪净多 < 0 → 散户情绪站在结论对面；
      ③ 资金在撤：lhb_sector_focus 最近交易日净流出板块 → 钱在撤，若它是主线即直接反证；
      ④ 自我修正史：predictor_adjust_log 最近被调低权重的信号 → 历史复盘跑输，反对押注它。
    """
    out: list = []
    # ① 市场宽度反向（breadth_json = generate 期 sig_stats 快照，conv_n/_pool 同口径）
    try:
        bd = json.loads(run["breadth_json"] or "{}")
        conv_n = int(bd.get("conv_n") or 0)
        pool_n = int(bd.get("_pool") or 1) or 1
        breadth = conv_n / pool_n * 100 if pool_n else 0
        tone = ("偏强" if breadth >= 35
                else "中性偏强" if breadth >= 20
                else "中性偏弱" if breadth >= 10 else "偏弱")
        if breadth < 35:
            out.append({
                "who": "市场宽度（多信号共振只数占比）",
                "what": "仅 %.0f%%（%d/%d）候选票被多路独立信号共同证实，宽度%s，反对把它当成全面普涨"
                        % (breadth, conv_n, pool_n, tone),
                "source": "breadth_json@" + str(run["run_date"]),
            })
    except Exception:  # noqa: BLE001
        pass
    # ①b 候选池价格普跌（2026-10-15 新增：宽度=信号共识≠价格趋势，普跌日宽度可能虚高）
    try:
        bd = json.loads(run["breadth_json"] or "{}")
        rg = bd.get("_regime") or {}
        dr, c1 = _f(rg.get("down_ratio"), 0.0) or 0.0, _f(rg.get("chg1"), 0.0) or 0.0
        if dr >= 60 or c1 <= -1.5:
            out.append({
                "who": "候选池价格（等权）",
                "what": "当日池等权 %+.2f%%、下跌家数占比 %.0f%%（%d 只样本），价格普跌，"
                        "反对把「多信号共振宽度偏强」解读为可积极选股"
                        % (c1, dr, int(rg.get("n") or 0)),
                "source": "breadth_json._regime@" + str(run["run_date"]),
            })
    except Exception:  # noqa: BLE001
        pass
    # ② 论坛情绪对立 —— 2026-10-31 已删除：论坛源停抓，posts 不再更新。
    #   留着这段的后果不是"少一条反证"，而是**长期用 10-29 之前的旧情绪当今天的反证**，
    #   那比没有反证更糟（会让人以为模型看过多空分歧，其实看的是历史）。
    #   原实现见 git 历史 826986d 及之前版本。
    # ③ 资金在撤：lhb_sector_focus 最近交易日的净流出板块（钱在撤 = 反对追多）
    try:
        srow = con.execute("SELECT MAX(trade_date) AS d FROM lhb_sector_focus").fetchone()
        sdate = srow["d"] if srow else None
        if sdate:
            for r in con.execute(
                    "SELECT sector, net_amt FROM lhb_sector_focus WHERE trade_date=? "
                    "AND net_amt<0 ORDER BY net_amt ASC LIMIT 2", (sdate,)).fetchall():
                out.append({
                    "who": "板块资金流向",
                    "what": "%s 资金净流出 %s 亿（钱在撤，若恰好是看多主线则是最直接的反证）"
                            % (r["sector"], abs(round((_f(r["net_amt"]) or 0.0) / 1e8, 2))),
                    "source": "lhb_sector_focus@" + str(sdate),
                })
                if len(out) >= _AGAINST_MAX:
                    return out[:_AGAINST_MAX]
    except Exception:  # noqa: BLE001
        pass
    # ④ 自我修正史：最近被调低权重的信号（历史复盘跑输 → 它在反对继续押注该信号）
    try:
        for a in con.execute(
                "SELECT * FROM predictor_adjust_log "
                "WHERE weight_after IS NOT NULL AND weight_before IS NOT NULL "
                "AND weight_after < weight_before ORDER BY id DESC LIMIT 3").fetchall():
            nm = SIGNAL_META.get(a["signal_key"], {}).get("name") or a["signal_key"]
            out.append({
                "who": "引擎自我修正（%s）" % nm,
                "what": "该信号权重已从 %.0f%% 调低至 %.0f%%（历史复盘跑输，反对继续重仓押注）"
                        % ((a["weight_before"] or 0) * 100, (a["weight_after"] or 0) * 100),
                "source": "predictor_adjust_log@" + str(a["log_date"]),
            })
            if len(out) >= _AGAINST_MAX:
                break
    except Exception:  # noqa: BLE001
        pass
    return out[:_AGAINST_MAX]


def _db_latest_data_date() -> str | None:
    """数据库里实际「落库过数据」的最新日期 = MAX(prediction_run.run_date, auction_snapshot.date)。

    用于「最近有数据的交易日」等 UI 字段的口径：只报真实落库过的日期，
    绝不用日历里「理论上存在但还没产生数据」的交易日（如今天 02:25 时的 10-09）。
    forward-backfill + 同天对账已保证 DB 日期不滞后，故该值即「真实最近有数据的交易日」。
    """
    try:
        con = sqlite3.connect("file:%s?mode=ro" % DB_PATH.replace("\\", "/"), uri=True)
        con.row_factory = sqlite3.Row
        try:
            d1 = con.execute("SELECT MAX(run_date) AS m FROM prediction_run").fetchone()["m"]
            d2 = con.execute("SELECT MAX(date) AS m FROM auction_snapshot").fetchone()["m"]
        finally:
            con.close()
        cands = [x for x in (d1, d2) if x]
        return max(cands) if cands else None
    except Exception:  # noqa: BLE001
        return None


def _pred_stale(run_date: str | None) -> dict | None:
    """预测是否滞后于最近交易日 → 前端标题打「数据截至 MM-DD」角标 + 一句人话说明。

    触发条件：最近交易日 > 最新一份预测的 run_date（也就是「今天开盘了，预测还没补上」）。
    取不到最近交易日（日历/网络不可用）就静默返回 None —— **宁可不标，也不许标错**：
    标错的代价是用户按错日期下判断，比什么都不说更糟（2026-10-05 时效断档的教训）。
    """
    if not run_date:
        return None
    try:
        import datetime as _dt
        from emapi import latest_valuation_date
        from auction import latest_trade_date as _au_ltd
        ltd = latest_valuation_date()
        real_today = _au_ltd()           # 权威交易日历（已修跨日失效）
        # 取「估值披露日」与「真实交易日」里更晚的那个作参照：
        # 东财估值表(RPT_VALUEANALYSIS_DET)可能晚于交易日才披露，不能因为它没披露就漏标陈旧。
        ref = max([x for x in (str(ltd), str(real_today)) if x], default=None)
        # 【最新交易日口径 · 点4】「最近有数据的交易日」必须来自数据库实际落库的日期：
        # 把 ref 上限钳到 DB 里真实有数据的最新日期，避免把「日历存在但尚无数据」的交易日
        # （如今天 02:25 时的 10-09）误报成"最近有数据的交易日"。forward-backfill + 同天对账
        # 已保证 DB 日期不滞后，故钳制后 ref 即"真实最近有数据的交易日"。
        data_latest = _db_latest_data_date()
        if data_latest and (ref is None or ref > data_latest):
            ref = data_latest
        if not ref or ref <= str(run_date):
            return None
        d_pred = _dt.date.fromisoformat(str(run_date)[:10])
        d_ref = _dt.date.fromisoformat(str(ref)[:10])
        lag = max(0, (d_ref - d_pred).days)
        mmdd = str(run_date)[5:10]
        return {
            "last_trade_date": str(ref),
            "lag_days": lag,
            "reason": ("数据截至 %s：最近交易日 %s 的预测还没生成，下面是最近一次运行（%s）的结果，"
                       "生成/调度跑完这一日就补齐" % (mmdd, str(ref)[5:10], mmdd)),
        }
    except Exception:  # noqa: BLE001 —— 日历取不到就当不陈旧，绝不抛到接口外
        return None


def scores_payload(con: sqlite3.Connection | None = None) -> dict:
    own = con is None
    con = con or connect()
    try:
        ensure_tables(con)
        rows = con.execute("SELECT * FROM predictor_score").fetchall()
        sig, sch, stg = [], [], []
        for r in rows:
            d = _score_row(r)
            if r["kind"] == "signal":
                sig.append(d)
            elif r["kind"] == "school":
                sch.append(d)
            else:
                stg.append(d)
        order = {k: i for i, k in enumerate(SIGNAL_KEYS)}
        sig.sort(key=lambda x: order.get(x["key"], 99))
        sch.sort(key=lambda x: (-(x["t5"]["rate"] if x["t5"]["rate"] is not None else -1),
                                -(x["t5"]["n"] or 0)))
        stg.sort(key=lambda x: (-(x["t5"]["rate"] if x["t5"]["rate"] is not None else -1),
                                -(x["t5"]["n"] or 0)))
        return {"ok": True, "signals": sig, "schools": sch, "strategies": stg,
                "windows": list(WINDOWS),
                # A3-2 样本外验证：样本外命中率单独展示（非全样本，含训练集会虚高）
                "out_of_sample": _out_of_sample_stats(con)}
    finally:
        if own:
            con.close()


def track_payload(con: sqlite3.Connection | None = None) -> dict:
    own = con is None
    con = con or connect()
    try:
        ensure_tables(con)
        runs = con.execute(
            "SELECT run_date, core_n, watch_n, pool_n, market_view FROM prediction_run "
            "ORDER BY run_date").fetchall()
        out = []
        cum = {"t1": [0, 0], "t3": [0, 0], "t5": [0, 0], "t10": [0, 0]}   # [样本, 命中]
        for r in runs:
            rd = r["run_date"]
            items = con.execute(
                "SELECT tier, ret_t1, ret_t3, ret_t5, ret_t10, hit_t1, hit_t3, hit_t5, hit_t10 "
                "FROM prediction_item WHERE run_date=?", (rd,)).fetchall()
            day = {"run_date": rd, "core_n": r["core_n"], "watch_n": r["watch_n"],
                   "pool_n": r["pool_n"], "market_view": r["market_view"],
                   "core": {}, "watch": {}, "all": {}}
            for w in (1, 3, 5, 10):
                for tier in ("core", "watch", "all"):
                    sel = [x for x in items if tier == "all" or x["tier"] == tier]
                    rets = [_f(x["ret_t%d" % w]) for x in sel]
                    rets = [x for x in rets if x is not None]
                    hits = [x["hit_t%d" % w] for x in sel if x["hit_t%d" % w] is not None]
                    day[tier]["t%d" % w] = {
                        "n": len(rets),
                        "avg_ret": round(sum(rets) / len(rets), 4) if rets else None,
                        "hit_rate": round(sum(hits) / len(hits), 4) if hits else None,
                    }
                    if tier == "all":
                        cum["t%d" % w][0] += len(rets)
                        cum["t%d" % w][1] += sum(hits)
            out.append(day)
        summary = {}
        for w in (1, 3, 5, 10):
            n, h = cum["t%d" % w]
            summary["t%d" % w] = {"n": n, "hit": h,
                                  "hit_rate": round(h / n, 4) if n else None}
        return {"ok": True, "days": out, "summary": summary,
                "runs": len(out), "windows": list(WINDOWS),
                # A3-2 样本外验证：复盘轨迹也单独给样本外命中率，禁止只拿全样本充数
                "out_of_sample": _out_of_sample_stats(con),
                # A3-3 walk-forward：滚动窗口重跑最近 250 交易日样本外命中率轨迹（含调权对齐）
                "walk_forward": _walk_forward_track(con)}
    finally:
        if own:
            con.close()


def growth_adjust_log_payload(con: sqlite3.Connection | None = None) -> dict:
    """成长面板（growth）A/D 区共用的只读载荷：权重演化时序 + 最近调整。

    纯读 predictor_adjust_log + predictor_score，幂等、不写库。
    隔离纪律：本函数绝不触碰 knowledge_source / growth_notes（那是本期预留的隔离表，零 JOIN）。
    """
    own = con is None
    con = con or connect()
    try:
        ensure_tables(con)

        def _f(x, d=None):
            try:
                return float(x) if x is not None else d
            except (TypeError, ValueError):
                return d

        # 当前各 signal 权重（来自 predictor_score，kind='signal'）
        score_rows = con.execute(
            "SELECT signal_key, weight FROM predictor_score WHERE kind='signal'").fetchall()
        current_weights: dict = {}
        has_real_weight = False
        for r in score_rows:
            w = r["weight"]
            val = 0.0
            if w is not None:
                try:
                    val = float(w)
                    has_real_weight = True
                except (TypeError, ValueError):
                    val = 0.0
            current_weights[r["signal_key"]] = val

        # 权重演化时序：按 signal_key 分组，log_date 升序，取每次调整后的 weight_after
        adj_rows = con.execute(
            "SELECT id, log_date, signal_key, kind, weight_before, weight_after, reason "
            "FROM predictor_adjust_log ORDER BY log_date ASC, id ASC").fetchall()
        weights_series: dict = {}
        for r in adj_rows:
            key = r["signal_key"]
            w = r["weight_after"]
            if w is None:
                continue
            try:
                wv = float(w)
            except (TypeError, ValueError):
                continue
            weights_series.setdefault(key, []).append(
                {"log_date": r["log_date"] or "", "weight": wv})

        # 最近 N=8 条调整（按 id 倒序），供 D 区「最近调整」
        recent = con.execute(
            "SELECT id, log_date, signal_key, kind, weight_before, weight_after, reason "
            "FROM predictor_adjust_log ORDER BY id DESC LIMIT 8").fetchall()
        recent_adjusts = []
        for r in recent:
            recent_adjusts.append({
                "id": r["id"],
                "log_date": r["log_date"] or "",
                "signal_key": r["signal_key"] or "",
                "kind": r["kind"] or "",
                "weight_before": _f(r["weight_before"]),
                "weight_after": _f(r["weight_after"]),
                "reason": r["reason"] or "",
            })

        # has_data：有调整记录、或任一信号已学到真实权重（NULL 权重不算）→ 视为「有成长证据」
        has_data = bool(adj_rows) or has_real_weight
        return {
            "ok": True,
            "has_data": has_data,
            "signals": SIGNALS,  # 12 维，前端据此【动态】决定画几条权重线，绝不硬编码 6
            "weights_series": weights_series,
            "current_weights": current_weights,
            "recent_adjusts": recent_adjusts,
            "caliber": window_caliber(),
        }
    finally:
        if own:
            con.close()


# ---------------------------------------------------------------- 历史查询（日期筛选 + 区间沉淀）
def _day_windows(con, rd: str) -> dict:
    """某一天各窗口的命中率 / 平均收益（用于 dates 清单与 history 汇总）。"""
    out = {}
    items = con.execute(
        "SELECT tier, ret_t1, ret_t3, ret_t5, ret_t10, hit_t1, hit_t3, hit_t5, hit_t10 "
        "FROM prediction_item WHERE run_date=?", (rd,)).fetchall()
    for w in (1, 3, 5, 10):
        rets, hits = [], []
        for x in items:
            r = _f(x["ret_t%d" % w])
            h = x["hit_t%d" % w]
            if r is not None:
                rets.append(r)
            if h is not None:
                hits.append(int(h))
        out["t%d" % w] = {
            "n": len(rets),
            "avg_ret": round(sum(rets) / len(rets), 4) if rets else None,
            "hit_rate": round(sum(hits) / len(hits), 4) if hits else None,
        }
    return out


def dates_payload(con: sqlite3.Connection | None = None) -> dict:
    """可选预测日清单（最早=首份预测日）+ 每日三窗口摘要，供前端日期选择器。"""
    own = con is None
    con = con or connect()
    try:
        ensure_tables(con)
        runs = con.execute(
            "SELECT run_date, core_n, watch_n, pool_n, market_view, created_at, "
            "status, note FROM prediction_run ORDER BY run_date").fetchall()
        dates = []
        for r in runs:
            rd = r["run_date"]
            dates.append({
                "run_date": rd, "core_n": r["core_n"], "watch_n": r["watch_n"],
                "pool_n": r["pool_n"], "market_view": r["market_view"],
                "created_at": r["created_at"], "status": r["status"], "note": r["note"],
                "windows": _day_windows(con, rd),
            })
        first = dates[0]["run_date"] if dates else None
        last = dates[-1]["run_date"] if dates else None
        return {"ok": True, "dates": dates, "first": first, "last": last,
                "count": len(dates), "windows": list(WINDOWS)}
    finally:
        if own:
            con.close()


def history_payload(con: sqlite3.Connection | None = None, run_date: str | None = None,
                    range_days: int = 0, window: int = 5) -> dict:
    """按日期回看（mode=day）或按区间汇总（mode=range）。

    - day：某一天 10 核心 + 10 观察的完整载荷（研判/信号/逐日曲线/复盘）。
    - range：最近 N 天（0=全部）所有预测日的汇总表现 + 逐日沉淀清单。
    两种模式都把每个窗口（3/5/10）的收益与命中随行下发，前端切换窗口不重复请求。
    """
    own = con is None
    con = con or connect()
    try:
        ensure_tables(con)
        all_runs = [r["run_date"] for r in con.execute(
            "SELECT run_date FROM prediction_run ORDER BY run_date").fetchall()]
        if not all_runs:
            return {"ok": True, "has_data": False, "windows": list(WINDOWS),
                    "signals": SIGNALS, "status": status_snapshot()}
        # 决定本次包含哪些 run_date
        if range_days and range_days > 0:
            inc = all_runs[-range_days:]
            mode = "range"
        else:
            rd = run_date or all_runs[-1]
            if rd not in all_runs:
                rd = all_runs[-1]
            inc = [rd]
            mode = "day"

        # 汇总各窗口
        cum = {w: [0, 0, 0.0] for w in WINDOWS}   # [样本, 命中, 累计收益]
        day_summ = []
        flat = []
        for rd in inc:
            items = con.execute(
                "SELECT * FROM prediction_item WHERE run_date=? ORDER BY rank", (rd,)).fetchall()
            dw = _day_windows(con, rd)
            day_summ.append({"run_date": rd, "windows": dw,
                             "core_n": len([x for x in items if x["tier"] == "core"]),
                             "watch_n": len([x for x in items if x["tier"] == "watch"])})
            for it in items:
                sigs = []
                try:
                    sigs = json.loads(it["hit_signals"] or "[]")
                except Exception:  # noqa: BLE001
                    pass
                try:
                    series = json.loads(it["series_json"] or "[]")
                except Exception:  # noqa: BLE001
                    series = []
                rec = {
                    "run_date": rd, "symbol": it["symbol"], "name": it["name"],
                    "tier": it["tier"], "rank": it["rank"], "score": it["score"],
                    "signals": sigs, "reason": it["reason"],
                    "entry_close": it["entry_close"], "next_open": it["next_open"],
                    "ret": {"t1": it["ret_t1"], "t3": it["ret_t3"], "t5": it["ret_t5"], "t10": it["ret_t10"]},
                    "bench": {"t1": it["bench_t1"], "t3": it["bench_t3"], "t5": it["bench_t5"], "t10": it["bench_t10"]},
                    "hit": {"t1": it["hit_t1"], "t3": it["hit_t3"], "t5": it["hit_t5"], "t10": it["hit_t10"]},
                    "series": series, "caveat": it["caveat"],
                }
                flat.append(rec)
                for w in WINDOWS:
                    r = _f(it["ret_t%d" % w])
                    h = it["hit_t%d" % w]
                    if r is not None:
                        cum[w][0] += 1
                        cum[w][2] += float(r)
                    if h is not None:
                        cum[w][1] += int(h)
        summary = {}
        for w in WINDOWS:
            n, h, s = cum[w]
            summary["t%d" % w] = {"n": n, "hit": h,
                                  "hit_rate": round(h / n, 4) if n else None,
                                  "avg_ret": round(s / n, 4) if n else None}

        out = {"ok": True, "has_data": True, "mode": mode, "window": window,
               "windows": list(WINDOWS), "run_dates": inc, "summary": summary,
               "days": day_summ, "items": flat if mode == "range" else None,
               "signals": SIGNALS, "status": status_snapshot()}

        if mode == "day":
            rd = inc[0]
            run = con.execute("SELECT * FROM prediction_run WHERE run_date=?", (rd,)).fetchall()
            run = run[0] if run else None
            items = con.execute(
                "SELECT * FROM prediction_item WHERE run_date=? ORDER BY rank", (rd,)).fetchall()
            out_items = []
            for it in items:
                try:
                    hs = json.loads(it["hit_signals"] or "[]")
                except Exception:  # noqa: BLE001
                    hs = []
                try:
                    series = json.loads(it["series_json"] or "[]")
                except Exception:  # noqa: BLE001
                    series = []
                out_items.append({
                    "symbol": it["symbol"], "name": it["name"], "tier": it["tier"],
                    "rank": it["rank"], "score": it["score"], "signals": hs,
                    "reason": it["reason"], "entry_close": it["entry_close"],
                    "next_open": it["next_open"],
                    "ret": {"t1": it["ret_t1"], "t3": it["ret_t3"], "t5": it["ret_t5"], "t10": it["ret_t10"]},
                    "bench": {"t1": it["bench_t1"], "t3": it["bench_t3"], "t5": it["bench_t5"], "t10": it["bench_t10"]},
                    "hit": {"t1": it["hit_t1"], "t3": it["hit_t3"], "t5": it["hit_t5"], "t10": it["hit_t10"]},
                    "series": series, "caveat": it["caveat"],
                })
            adj = [dict(r) for r in con.execute(
                "SELECT * FROM predictor_adjust_log ORDER BY id DESC LIMIT 8").fetchall()]
            out.update({
                "run_date": rd,
                "created_at": run["created_at"] if run else None,
                "engine_ver": run["engine_ver"] if run else ENGINE_VER,
                "pool_n": run["pool_n"] if run else 0,
                "core_n": run["core_n"] if run else 0,
                "watch_n": run["watch_n"] if run else 0,
                "market_view": run["market_view"] if run else "",
                "style_view": run["style_view"] if run else "",
                "breadth": json.loads(run["breadth_json"] or "{}") if run else {},
                "bench": json.loads(run["bench_json"] or "null") if run else None,
                "status": run["status"] if run else None,
                "note": run["note"] if run else None,
                "items": out_items,
                "adjust_log": adj,
            })
        return out
    finally:
        if own:
            con.close()


def latest_summary(con: sqlite3.Connection | None = None) -> dict:
    """给「今日看板」用的一行摘要。"""
    own = con is None
    con = con or connect()
    try:
        ensure_tables(con)
        rd = con.execute("SELECT MAX(run_date) AS d FROM prediction_run").fetchone()
        rid = rd["d"] if rd else None
        if not rid:
            return {"ok": True, "has_data": False}
        r = con.execute("SELECT * FROM prediction_run WHERE run_date=?", (rid,)).fetchone()
        n = con.execute("SELECT COUNT(*) AS c FROM prediction_item WHERE run_date=?",
                        (rid,)).fetchone()["c"]
        sc = con.execute(
            "SELECT COUNT(*) AS n, SUM(hit_t1) AS h FROM prediction_item "
            "WHERE hit_t1 IS NOT NULL").fetchone()
        return {"ok": True, "has_data": True, "run_date": rid, "n": n,
                "market_view": r["market_view"],
                "hit_rate_t1": (round(sc["h"] / sc["n"], 4)
                                if sc["n"] and sc["h"] is not None else None),
                "n_reviewed": sc["n"] or 0}
    finally:
        if own:
            con.close()


# ---------------------------------------------------------------- 历史回放
def backtest(days: int = 60, pool_limit: int = POOL_LIMIT) -> dict:
    """技术面历史回放（离线命令行用）。

    对过去 days 个交易日，逐日构造「截至该日」的 K 线切片，用 build_signals 取当日触发，
    统计各流派 / 各策略的 T+3 / T+5 / T+10 命中率（命中 = 跑赢候选池同期等权平均）。

    只回放技术面 —— 情绪 / 资金 / 跟随 / 外部信号的表里没有历史快照，无法回测。
    """
    con = connect()
    try:
        codes, _names, _b = SS.universe(pool_limit)
        kc = load_kcache()
        bars_map = fetch_bars(codes, kc, force=False)
        # 回放天数自适应：按候选池 K 线长度的**中位数**算可支撑天数。
        # 不用最短值 —— 池子里偶有上市不足百日的新股（len=1），会把可回放天数压成 0。
        _L = sorted(len(v) for v in bars_map.values())
        avail = max(0, (_L[len(_L) // 2] if _L else 0) - MIN_BARS - 5)
        if days > avail:
            print("[predictor] 回放天数 %d 超出 K 线可支撑长度 %d，自动收到 %d（≈%.1f 年）"
                  % (days, avail, avail, avail / 244.0), flush=True)
            days = avail
        bars_map = {c: v for c, v in bars_map.items() if len(v) >= MIN_BARS + days + 10}
        strat_list = _load_strategies()
        if not bars_map:
            return {"ok": False, "error": "K 线不足，无法回放"}
        ref = max(bars_map.values(), key=len)
        n_end = len(ref)
        # 预计算每票的策略触发序列
        tech_seq = {c: sig_tech_replay(v, strat_list) for c, v in bars_map.items()}
        res: dict = defaultdict(lambda: {"n1": 0, "h1": 0, "n3": 0, "h3": 0, "n5": 0, "h5": 0,
                                         "n10": 0, "h10": 0, "s10": 0.0,
                                         "s1": 0.0, "s3": 0.0, "s5": 0.0})
        start = max(MIN_BARS, n_end - days - 5)
        for idx in range(start, n_end - 1):
            d = str(ref[idx].get("d", ""))[:10]
            # 当日基准：全池同期等权平均
            bench = {}
            for w in WINDOWS:
                rr = [_window_ret(v, idx, w) for v in bars_map.values()]
                rr = [x for x in rr if x is not None]
                bench[w] = (sum(rr) / len(rr)) if rr else None
            for c, v in bars_map.items():
                if idx >= len(v):
                    continue
                seq = tech_seq.get(c) or {}
                keys = []
                for s in strat_list:
                    k = s.get("key")
                    b = seq.get(k)
                    if b and idx < len(b) and b[idx]:
                        keys.append(k)
                        keys.append("school:" + (s.get("school") or "其它"))
                if not keys:
                    continue
                for kk in set(keys):
                    a = res[kk]
                    for w, (nf, hf, sf) in ((3, ("n3", "h3", "s3")), (5, ("n5", "h5", "s5")),
                                            (10, ("n10", "h10", "s10"))):
                        r = _window_ret(v, idx, w)
                        if r is None or bench[w] is None:
                            continue
                        a[nf] += 1
                        a[hf] += 1 if r > bench[w] else 0
                        a[sf] += r
        out = {}
        for kk, a in res.items():
            out[kk] = {
                "t3": {"n": a["n3"], "hit_rate": round(a["h3"] / a["n3"], 4) if a["n3"] else None,
                       "avg_ret": round(a["s3"] / a["n3"], 4) if a["n3"] else None},
                "t5": {"n": a["n5"], "hit_rate": round(a["h5"] / a["n5"], 4) if a["n5"] else None,
                       "avg_ret": round(a["s5"] / a["n5"], 4) if a["n5"] else None},
                "t10": {"n": a["n10"], "hit_rate": round(a["h10"] / a["n10"], 4) if a["n10"] else None,
                        "avg_ret": round(a["s10"] / a["n10"], 4) if a["n10"] else None},
            }
        _d0 = str(ref[start].get("d", ""))[:10]
        _d1 = str(ref[n_end - 2].get("d", ""))[:10]
        try:
            # 只留一条「近两年逐日回放」快照（id=1），权重校准读它就够
            con.execute("DELETE FROM predictor_replay WHERE id=1")
            con.execute(
                "INSERT INTO predictor_replay (id,window_label,days,pool,start_date,end_date,"
                "scores_json,computed_at) VALUES (1,?,?,?,?,?,?,?)",
                ("y2", days, len(bars_map), _d0, _d1,
                 json.dumps(out, ensure_ascii=False), _now()))
            con.commit()
            print("[predictor] 近两年逐日回放已落库：%s ~ %s，%d 个交易日 / %d 只票"
                  % (_d0, _d1, days, len(bars_map)), flush=True)
        except Exception as exc:  # noqa: BLE001
            print("[predictor] 回放结果落库失败（不影响本次统计）：%s" % exc, flush=True)
        return {"ok": True, "days": days, "pool": len(bars_map), "start": _d0,
                "end": _d1, "scores": out, "saved": True}
    finally:
        con.close()


# ---------------------------------------------------------------- 调度钩子
_DAILY_DONE: dict = {"date": None, "last": None, "fail": 0}

# 缺日补跑（catch-up）状态：前端轮询 /api/prediction/status 可见进度
_CATCHUP: dict = {"running": False, "stage": "", "total": 0, "done": 0,
                  "dates": [], "current": None, "error": None, "finished_at": None}
_CATCH_MAX = 10          # 单次补跑最多补几个交易日（防首跑回溯几年）
_CATCH_LOCK = threading.Lock()


def _catchup_status() -> dict:
    return dict(_CATCHUP)


def _trade_calendar(bars_map: dict) -> list:
    """交易日历：取候选池众数票的日期序列（K 线源即交易所日历，不自造）。

    只留 <= 今天的部分，升序。
    """
    ref = max(bars_map.values(), key=len) if bars_map else []
    days = sorted({str(b.get("d", ""))[:10] for b in ref
                   if b.get("d") and str(b["d"])[:10] <= _today()})
    return days


def catch_up(con: sqlite3.Connection, progress=None, force: bool = False) -> dict:
    """缺日补跑：把「最后一次预测日」到「最近已收盘交易日」之间漏掉的每个交易日补齐。

    核心是数据隔离的逐日链条（预测与验证严格分开）：
      对每个缺失交易日 d（按时间升序）：
        1) generate(run_date=d)：asof 截断——K 线只用到 d 收盘、龙虎榜/外部参考只取
           d 及以前发布的数据，**预测时看不见 d 之后的任何真实走势**；
        2) review()：用 d 之后已经走出来的**实际** K 线复盘历史预测（T+3/T+5/T+10），
           这一步是"验证"，只更新 ret/bench/hit 等后验字段，不改预测内容本身；
        3) generate 内部 rescore()：把刚验证完的命中率纳入权重，供下一天的预测使用。
      即：用 d 的数据预测 d+1 → 用 d+1 实际走势验证 → 权重微调 → 预测 d+2。逐日滚动。
    """
    with _CATCH_LOCK:
        if _CATCHUP["running"]:
            return {"ok": False, "error": "补跑已在进行中", "status": _catchup_status()}
        _CATCHUP.update(running=True, stage="准备", total=0, done=0, dates=[],
                        current=None, error=None, finished_at=None)
    try:
        ensure_tables(con)
        # 1) 交易日历（顺带把 K 线刷新到最新）
        #    【最新交易日锚点 · 硬保证】每日预测 / K 线刷新一律以"动态计算的最新交易日"为绝对上界，
        #    不用裸 today()，也不信 K 线缓存里可能滞后的旧日期（如国庆后 K 线停在节前 09-30）。
        #    anchor = _latest_trade_date_anchor()（收盘后含今日，盘中回退到昨日之前），
        #    永不依赖缓存默认值或 host 时钟的裸 today()。
        _CATCHUP["stage"] = "刷新K线"
        codes, _names, _b = SS.universe(POOL_LIMIT)
        if not codes:
            raise RuntimeError("候选池为空")

        # 锚点 = 真实最近【已收盘】交易日（收盘后含今日，盘中回退到昨日之前）。
        real_td = _latest_trade_date_anchor()
        # 同天对账：若"真实最新交易日"的预测尚未生成 → 强制刷新 K 线到最新，把预测补齐到当天。
        # 即使错过 9:15–9:35 现场窗口也没关系：只要当天预测还缺，收盘后这一轮就会 force 补上。
        row0 = con.execute("SELECT MAX(run_date) AS m FROM prediction_run").fetchone()
        last_run0 = row0["m"] if row0 else None
        force_kline = bool(force) or not (last_run0 and last_run0 >= real_td)

        kc = load_kcache()
        bars_map = fetch_bars(codes, kc, force=force_kline)
        bars_map = {c: v for c, v in bars_map.items() if len(v) >= MIN_BARS}
        cal = _trade_calendar(bars_map)
        if not cal:
            raise RuntimeError("交易日历为空（K 线不可用）")
        # target_end 一律以"真实最新交易日"为绝对上界——绝不超过它，也绝不用早于它的缓存/默认值。
        target_end = cal[-1]
        pushed_today = False
        if real_td > target_end:
            # K 线日历落后真实交易日（源尚未刷新，如长假后首日上午）→ 以真实交易日兜底目标日，
            # 让当日预测真正被尝试生成；generate 内部 asof 截断保证不偷看未来。
            # 若当日完整数据尚未采集，下面会给该次运行打 caveat，不编当日数据。
            target_end = real_td
            pushed_today = True
            if real_td not in cal:
                cal = cal + [real_td]
        elif real_td < target_end:
            # K 线日历领先于真实交易日（异常：缓存混入未来/非交易日期）→ 截断到真实交易日，
            # 绝不用未来日期或休市日数据做预测。
            target_end = real_td
        else:
            target_end = cal[-1]
        if not target_end:
            raise RuntimeError("无已收盘交易日")
        # 2) 缺失日 = (last_run, target_end] 内的交易日
        row = con.execute("SELECT MAX(run_date) AS m FROM prediction_run").fetchone()
        last_run = row["m"] if row else None
        if last_run:
            missing = [d for d in cal if d > last_run and d <= target_end]
        else:
            missing = [d for d in cal if d <= target_end][-_CATCH_MAX:]   # 首跑只补最近 10 天
        missing = missing[-_CATCH_MAX:]
        # 兜底目标日（真实今天）可能不在 K 线日历里（源尚未刷新），确保它进入补跑队列，
        # 否则上面把 target_end 推到今天也只是空转——这正是 10-08 当晚补跑失效的卡点。
        if pushed_today and target_end not in missing and (not last_run or target_end > last_run):
            missing.append(target_end)
            missing.sort()
        with _CATCH_LOCK:
            _CATCHUP.update(total=len(missing), dates=list(missing), done=0)
        results = []
        for i, d in enumerate(missing):
            with _CATCH_LOCK:
                _CATCHUP.update(current=d, stage="生成 %s 预测（asof 隔离）" % d, done=i)
            if progress:
                progress("补跑", i, len(missing))
            # 2a) 生成 d 日预测（隔离：看不见 d 之后）
            g = generate(con, run_date=d, force=False)
            if not g.get("ok"):
                # 已存在（cached）也算过；真失败则记录并继续下一天
                results.append({"date": d, "generate": g})
                continue
            # 兜底生成的「真实今天」预测：今日完整数据可能尚未采集，
            # 如实打 caveat，不让人误判为一份完整成熟的当日预测。
            if pushed_today and d == target_end:
                try:
                    latest_bar = max([(b[-1].get("d") or "")[:10]
                                      for b in bars_map.values() if b], default="—") or "—"
                    cave = "⚠ 今日完整数据尚未采集，本预测基于截至 %s 的最新可用数据生成" % latest_bar
                    con.execute("UPDATE prediction_run SET note = note || ? WHERE run_date=?",
                                ("｜" + cave, d))
                    con.execute("UPDATE prediction_item SET caveat = ? WHERE run_date=?", (cave, d))
                    con.commit()
                except Exception:  # noqa: BLE001
                    pass
            # 2b) 复盘验证：用实际走势验证全部历史预测（含刚生成的 d 之前的）
            r = review(con)
            results.append({"date": d, "generate": g, "review": r})
        with _CATCH_LOCK:
            _CATCHUP.update(running=False, stage="完成", done=len(missing),
                            error=None, finished_at=_now())
        return {"ok": True, "target_end": target_end, "last_run": last_run,
                "caught": [r.get("date") for r in results], "results": results}
    except Exception as e:  # noqa: BLE001
        with _CATCH_LOCK:
            _CATCHUP.update(running=False, stage="失败", error=str(e)[:300],
                            finished_at=_now())
        return {"ok": False, "error": str(e)[:300], "status": _catchup_status()}


def maybe_daily(con: sqlite3.Connection) -> dict:
    """调度线程每轮调用（仿 board_flow.maybe_sample 的挂法）：交易日收盘后跑一次「复盘 + 生成」。

    - 只在 15:05 之后、非周末运行；同一天成功后只跑一次。
    - 先复盘（已到期窗口 + 权重更新）→ 再走 catch_up 补齐缺失交易日（含今天）。
    - ⚠️ 失败不再标记 done（旧版 bug：抓 K 线失败也标 done → 当天永远不再生成）；
      连续失败 3 次才停手（防限流死循环打爆数据源），第二天自动恢复。
    """
    now = datetime.now()
    if now.weekday() >= 5:
        return {"skip": "weekend"}
    if (now.hour, now.minute) < (15, 5):
        return {"skip": "pre_close"}
    today = now.strftime("%Y-%m-%d")
    if _DAILY_DONE.get("date") == today:
        return {"skip": "done"}
    out: dict = {"date": today, "attempt": (_DAILY_DONE.get("fail") or 0) + 1}
    try:
        out["review"] = review(con)
    except Exception as e:  # noqa: BLE001
        out["review_error"] = str(e)[:200]
    ok = True
    try:
        # 同天对账在 catch_up 内部完成：以"动态计算的最新交易日"为锚，若当天预测缺失则强制刷新 K 线补齐。
        out["catch_up"] = catch_up(con)
        if not out["catch_up"].get("ok"):
            ok = False
    except Exception as e:  # noqa: BLE001
        out["catch_up_error"] = str(e)[:200]
        ok = False
    if ok:
        _DAILY_DONE["date"] = today
        _DAILY_DONE["last"] = out
        _DAILY_DONE["fail"] = 0
    else:
        _DAILY_DONE["fail"] = (_DAILY_DONE.get("fail") or 0) + 1
        if _DAILY_DONE["fail"] >= 3:
            _DAILY_DONE["date"] = today   # 连续 3 次失败，当天不再自动重试
            _DAILY_DONE["last"] = out
    return out


def run_now(con: sqlite3.Connection, force: bool = True) -> dict:
    """手动触发（前端按钮 / API）：复盘 + 生成，忽略"已生成"跳过逻辑。"""
    out: dict = {}
    try:
        out["review"] = review(con, force=force)
    except Exception as e:  # noqa: BLE001
        out["review_error"] = str(e)[:300]
    try:
        out["generate"] = generate(con, force=force)
    except Exception as e:  # noqa: BLE001
        out["generate_error"] = str(e)[:300]
    return out


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="每日预测引擎")
    ap.add_argument("--generate", action="store_true", help="生成当日预测")
    ap.add_argument("--review", action="store_true", help="复盘已到期窗口")
    ap.add_argument("--backtest", type=int, default=0, help="技术面历史回放天数")
    ap.add_argument("--force", action="store_true", help="忽略缓存强制重算")
    args = ap.parse_args()
    c = connect()
    try:
        ensure_tables(c)
        if args.generate:
            print(json.dumps(generate(c, force=args.force), ensure_ascii=False, indent=2)[:2000])
        if args.review:
            print(json.dumps(review(c), ensure_ascii=False, indent=2)[:2000])
        if args.backtest:
            print(json.dumps(backtest(args.backtest), ensure_ascii=False, indent=2)[:4000])
    finally:
        c.close()
