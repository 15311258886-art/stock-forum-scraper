# -*- coding: utf-8 -*-
"""集中配置（A 阶段治理：把散落在各业务模块的常量抽到单一事实源）。

设计纪律：
  · 本文件是「纯常量 + 派生计算」，不 import 项目内任何其它模块（避免循环依赖）。
  · 环境变量优先于本文件默认值（如 PILLARS_POOL_LIMIT）。
  · 改系统范围 / 权重 = 改这里，而不是改各业务模块。
  · 每次调整常量，同步更新项目根 DECISIONS.md 对应条目。
"""
import os

# ---- 评估宇宙 / 候选池 ----
# 四支柱 / 重分析候选池上限：流动性头部 N 只干净票（环境变量可调到 5268 = 全宇宙重分析）
PILLARS_POOL_LIMIT = int(os.getenv("PILLARS_POOL_LIMIT", "2000"))

# ---- K 线 / 因子层 ----
WINDOW = 260                  # 因子历史落库深度（交易日）：stock_indicators_daily 仅保留最近 WINDOW 日给查看器
KLINE_LOOKBACK_DAYS = 420     # K 线拉取回看（日历日），需 ≥200 交易日，保证 MA200 等长周期因子在最新快照有效

# ---- 全宇宙扫描三轴权重（P2）----
W_MOMENTUM = 0.45
W_VOLUME = 0.25
W_QUALITY = 0.30

# ---- 调度时间窗（交易日触发）----
SCHED_TRADE_END_HOUR = 15     # 15:30 后触发 K线增量 / 因子 / 全宇宙扫描 / 策略模板
SCHED_TRADE_END_MINUTE = 30
SCHED_EXCLUSION_HOUR = 20     # 20:00 后触发剔除池重建（依赖快照 + 抖音刷新）

# ---- 剔除池（干净宇宙）原因标签 ----
# 注意（2026-10-26 修正，进哥指出"博主反向信号有时效，不能永久剔除"）：
#   博主信号「不再」作为剔除理由！博主荐股实证为反向，但这是「带时效的警示」而非
#   剥夺标的宇宙资格的硬条件。
#   （2026-10-30 补充：该警示本身也已下线 —— 它拿全期快照绑定个股，6 月的推荐到 11 月
#     仍在生效，而源自 2026-10-27 起停更。避坑改为 viewer/avoid.py 的当日量价实证，
#     完全不读博主数据。）
#   剔除池只保留客观、无时效争议的硬垃圾：st / no_kline / illiquid。
#   曾经存在的 blogger_noise（80 只永久剔除）已移除并释放回干净宇宙。
EXCLUSION_REASONS = ("st", "no_kline", "illiquid")

# ---- 避坑标记（2026-10-30 替代原「博主反向信号时效窗口」）----
# 原 BLOGGER_RISK_WINDOW_DAYS 已随博主维度一并删除，原因有两条：
#   ① 它只约束 avg_t5，n_bloggers 仍取自 blogger_overlap 这个**全期沉淀快照**，
#      窗口内外混用一套口径，修窗口等于只补一半；
#   ② 博主源于 2026-10-27 停更，该表此后再无写入 —— 给一个死源补时效参数没有意义。
# 现改为 viewer/avoid.py：每次调用从当日 K 线 + 近 15 日利空公告实时算，不落库、不设窗口，
# 结构上就不可能陈旧。阈值（LEVEL_HIGH / LEVEL_MID / ANN_WINDOW_DAYS）写在 avoid.py 里，
# 与权重一起归实证管（tools/probe_avoid_signal.py），改动前先重跑实证，别凭感觉调。

# ---- 信号权重表（P4 全量回测实证，治 C5）----
# 语义：weight > 0 → 该方向信号可信、正向加权；weight == 0 → 暂不使用；
#       weight < 0 → 该方向在本环境实证反向（invert=True），应降权/反用警惕。
# 数值为「初步相对权重」，需定期（季度）用 signal_backtest 重算，不写死胜率。
# 来源：signal_backtest 全量（5243 只 / 回看 250 日 / 前向 5,10,20）。
SIGNAL_WEIGHTS = {
    # 均值回归 / 超卖类 —— 正 alpha（强）
    "RSI6超卖":         {"dir": "buy",  "weight": 1.0,  "invert": False, "note": "fwd5 win52.7%/sharpe1.19，全环境最强正alpha"},
    "跌破BOLL下轨":      {"dir": "buy",  "weight": 0.9,  "invert": False, "note": "fwd5 win53.0%/sharpe1.11"},
    "KDJ超卖金叉":       {"dir": "buy",  "weight": 0.6,  "invert": False, "note": "fwd20 win48.2%/sharpe0.85"},
    "CCI超卖":          {"dir": "buy",  "weight": 0.3,  "invert": False, "note": "基本持平，弱正"},
    "WR6超卖":          {"dir": "buy",  "weight": 0.0,  "invert": False, "note": "基本持平偏负，暂不使用"},
    # 趋势跟随类 —— 负 alpha（降权 / 反用）
    "MACD零上金叉区":    {"dir": "buy",  "weight": -0.4, "invert": True,  "note": "负alpha，趋势跟随在本环境失效"},
    "股价在Supertrend上":  {"dir": "buy",  "weight": -0.6, "invert": True,  "note": "全窗口负alpha，最弱"},
    "放量":             {"dir": "buy",  "weight": -0.8, "invert": True,  "note": "fwd20 win37.1%/sharpe-1.20，最差，反向警惕出货"},
    # 做空侧（卖出信号）正 alpha
    "股价在Supertrend下":  {"dir": "sell", "weight": 0.8,  "invert": False, "note": "fwd20 win48.3%/sharpe0.92"},
    "PDI":             {"dir": "sell", "weight": 0.6,  "invert": False, "note": "fwd20 win47.4%/sharpe0.77"},
    "MACD零下死叉区":    {"dir": "sell", "weight": 0.2,  "invert": False, "note": "弱正"},
    # 中性 / 暂不加权
    "突破BOLL上轨":       {"dir": "sell", "weight": 0.0,  "invert": False, "note": "负alpha，暂不使用"},
    "RSI6超买":         {"dir": "sell", "weight": 0.0,  "invert": False, "note": "弱，暂不使用"},
    "KDJ超买死叉":       {"dir": "sell", "weight": 0.0,  "invert": False, "note": "弱，暂不使用"},
    "WR6超买":          {"dir": "sell", "weight": 0.0,  "invert": False, "note": "弱正，暂不使用"},
    "CCI超买":          {"dir": "sell", "weight": 0.0,  "invert": False, "note": "弱负，暂不使用"},
}


def signal_weight(name: str):
    """返回 (weight, invert, note)；未知信号返回 (0.0, False, '未回测')。"""
    w = SIGNAL_WEIGHTS.get(name)
    if not w:
        return 0.0, False, "未回测"
    return w["weight"], w["invert"], w["note"]


# ---- 采集链路开关（2026-10-30 全链路体检后收敛，进哥要求「抓了没用的全停」）----
# 判定依据不是感觉，是可核对的三个事实（见 docs/链路体检_20261030.md）：
#   ① 顺着**调用链**查到真实消费方（不能只 grep 表名 —— 通过函数间接读表会漏掉）；
#   ② 数据还在不在更新（停更超过 3 天 = 源已死，留着只是每天空跑）；
#   ③ 若产出信号，有没有经过实证（t 值 / 前向收益）。
# 停用 ≠ 删代码：只改这里一行即可恢复，历史数据与表结构全部保留。
#
# ⚠️ 2026-10-30 教训（写死在这里，防止下次再用 grep 判死）：
#   第一版凭「表名 grep 为零」停了 4 项，复核推翻 3 项：
#     · auction_snapshot —— 表名确实零引用，但它被 auction.summary() 读，而 summary 供
#       「今日总览」第一屏 + /api/auction。停了看板竞价就永远显示旧数据。
#     · extref_signals   —— 被 ai_review.py 与 predictor.py 直接 SQL 读（AI 复盘的证据源）。
#     · evo_knowledge    —— 被 ai_review.py 读（复盘引用的知识条目）。
#   → 结论：判死必须顺调用链，grep 只能用来**初筛**，不能作为停用依据。
#
# 唯一确认「只进不出」的一项：
#   intraday_snap —— 1,731 行、8 个交易日，注释写「为 14:30 盘中重建 vs 盘后重建攒回测样本」，
#                   但全项目只有它自己读（统计写了多少行），没有任何分析/面板消费。
#                   停了 = 放弃那条回测设想；数据与表保留，改回这里即恢复。
# 2026-10-31 更新：intraday_snap 已**物理删除**（任务、注册表、表、超时配置全部移除），
# 不再需要靠本清单"停用"。本清单现在只保留给「先停不删」的灰度用：
# 把任务名加进来即可跳过调度，从 config 里删掉即恢复，不改任何任务实现。
# 2026-10-30：抖音舆情语料停用（进哥拍板「停」）。
#   停的理由不是"没用"，而是**源已经死了，任务在空转**：
#     ① `douyin_corpus_video` 最后写入停在 **2026-10-20 14:29**，10 天零新增；
#     ② 每天 09:30 照常派子进程、照常回报「成功完成」——成功的是"跑完了"，不是"拿到了"；
#     ③ 它喂着 30+ 个接口（integrate / 舆情 / 板块提及），源一死，这些面板都在展示 10-20 的旧闻。
#   注意与 intraday_snap 的区别：这次是**先停、不删**。表、脚本、接口全部保留，
#   面板改为显示"停更于 2026-10-20"，从本清单删掉名字即恢复调度。
DISABLED_SCHED = frozenset({"douyin_corpus"})

# 雪球人轴抓取（xueqiu_person.py / xueqiu_cube_*.py|.cjs）：2026-10-30 停止使用。
#   它从未进过自动调度（一直是手动跑的辅助脚本），所以停用 = 在入口拦一道，
#   防止以后误跑把「大V操作」这类已被证伪的信号重新灌回系统。
#   置 False 后三个入口都会在开头直接退出，不抓、不入库。
#   依据同论坛链路的实证：docs/论坛大V信号实证_20261030.md —— 「跟人操作」的价值主张不成立。
XUEQIU_ENABLED = False

# 论坛爬虫源：交易时段每 10 分钟一轮（一天约 42 轮）。置空 = 完全停抓。
# 2026-10-30 夜：进哥全权委托，已置空。依据是实证而不是感觉：
#   ① 七个信号切面里六个是噪声（BUY +0.28% t=0.36；淘股吧「看多」−1.41% 但 t 仅 −2.02）
#   ② 唯一像样的 HOLD，做「同日、前 5 日涨幅相差 ≤2pp」的配对后从 −3.08%(t=−2.57)
#      掉到 −1.96%(t=−1.79)，**不显著**；且一天只筛出 4 只票（56 只标的 / 25 个交易日）
#   ③ 核心机制是「涨完才晒」：三类动作发帖前 5 日超额 +6%~+11%，发帖后归零
#   ④ 淘股吧 Cookie 早已失效（login_failed，2026-10-24 判定），抓到的本身就是残缺数据
# 实证见 docs/论坛大V信号实证_20261030.md + tools/probe_hold_net.py。
#
# 影响面（明确，不是无所谓）：**「他人操作」一级类的 6 个面板不再有新数据**，历史全部保留。
# 这是产品取舍：那些面板原本的价值主张是「跟着大V做」，而实证说明这个价值主张不成立。
# 改回这一行即恢复抓取，代码与表结构一行未删。
INGEST_SOURCES = []
