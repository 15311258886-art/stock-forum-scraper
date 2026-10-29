# -*- coding: utf-8 -*-
"""实证账本 —— 「我们到底知道什么」的唯一清单（2026-10-31 建立）。

## 为什么要有这个文件

这轮体检暴露了一个结构性问题：我们做了大量实证，但结论散落在三个互不相通的地方——

  ① `avoid.py` / `lift.py` 的**代码注释**里（人读得到，程序读不到）
  ② `docs/*.md` 的**报告**里（人读得到，模型读不到）
  ③ `evo_knowledge` 表里（但内容还停在「博主声量=0/1」时代，与现行实证脱节）

后果很具体：**给大模型做复盘时，它完全不知道哪些指标已被证伪**，于是会一本正经地
引用「超买要回调」（实测 +0.69%）、「减持必跌」（严格口径 t=−0.13）这类已经被我们自己
推翻的说法。一个不知道「自己哪条经验已被证伪」的分析师，越能说越危险。

所以这里做一件事：把每条实证结论登记成一条 **claim**，带样本量、超额、t 值、
状态（active / weak / refuted / watch）和可复现的探针脚本。它是：

  · `avoid.py` 权重的来源（避免权重与实证脱钩）
  · `ai_review` 上下文的一段（让模型知道证据边界与禁区）
  · 前端「我们凭什么这么说」的依据

## 状态的四个值（不要自造）

  active    |t|≥2 且方向明确 → 可作为规则使用，给出权重
  weak      |t|<2 但方向一致、样本够 → 只做提示，不给分（写进上下文时必须标注「强度不足」）
  refuted   实测方向与直觉相反，或口径修正后不成立 → **禁止在任何判断里使用**
  watch     样本太少（<30 个独立交易日）或证据互相矛盾 → 只观察，不进规则

## 铁律

**任何 claim 的 t 值都必须能由 `probe` 字段指向的脚本复现。** 不能复现的数字不许写进来。
这不是形式主义：本次体检中「股东减持 t=−5.66」就是因为无法复现（口径错用了公告当日
收盘价）而被推翻，从 3 分降到 2 分。

## 2026-10-31 统一重标定（本账本最重要的一次方法论修正）

在此之前，数字来自多个各写各的 SQL 的探针脚本，**口径不统一**。最严重的偏差是
「当日收盘买入 vs 次日收盘买入」—— 龙虎榜、公告、跌停这些信号都是**盘后**才知道的，
用当日收盘价算收益等于用了未来价格，会把强度系统性算高（个别规则被高估一倍以上）。

现在所有数字统一由 `viewer/hypo.py` 一个引擎产出：
  · 严格可执行口径：d 盘后知情 → **d+1 收盘建仓** → d+1+H 收盘平仓
  · 每条 claim 的 `probe` 字段写明可复现的 predicate，任何人跑一遍就能核对

**并新增一条门槛原则：显著 ≠ 可用。**
|t|≥2 只说明「不是噪声」；能否进规则还要看**幅度**。双边成本约 0.15%~0.25%，
幅度 <0.3% 的信号即使 t=−2.7 也没有操作价值。按这条标准，本次删掉了三条
t 值看似合格但幅度不值一提的规则 —— 它们此前在界面上占着权重，实际只是噪音。

## 口径约定（全项目统一，改这里等于改全部）

  · 超额 = 个股收益 − 同日全市场等权均值（demean），不扣任何"风格因子"
  · 可执行口径：事件日 d 盘后知情 → **d+1 收盘**建仓 → d+1+H 收盘平仓
  · t 值按**日聚合后再检验**（同一天几千个样本不能当独立样本，否则 t 虚高几十倍）
"""

from __future__ import annotations

# 账本最后更新日。新增/修改 claim 时必须同步。
ASOF = "2026-10-31"

# ---------------------------------------------------------------- claim 定义
#
# 字段说明：
#   key     稳定标识，供 avoid.py 等取权重用
#   group   avoid（避坑） / lift（解禁） / forward（正向） / forum（论坛信号）
#   name    展示名
#   rule    判定条件的自然语言写法（必须可落到现成字段）
#   horizon 前向天数（交易日）
#   n       样本数（事件数，不是记录数）
#   days    覆盖的独立交易日数 —— **这个数比 n 更能说明可信度**，t 值按日聚合后，
#           自由度实际由它决定
#   excess  T+horizon 平均超额 %
#   t       日聚合后的 t 值
#   win     胜率 %（方向为负时指"下跌占比"）
#   status  active / weak / refuted / watch
#   weight  仅 avoid 用；0 表示不给分
#   probe   可复现脚本（相对仓库根）
#   note    关键限定条件、容易被误用的地方

CLAIMS: tuple = (
    # ---------------- 龙虎榜：目前最强的一类信号 ----------------
    {
        "key": "lhb_turnover", "group": "avoid", "name": "龙虎榜高换手",
        "rule": "近10个交易日内上过龙虎榜，且上榜当日换手率 ≥ 15%",
        "horizon": 5, "n": 1873, "days": 19,
        "excess": -6.12, "t": -9.05, "win": 19.3, "status": "active", "weight": 6,
        "probe": "viewer/hypo.py（predicate: lhb_hit == 1 and turnover >= 15）",
        "note": "2026-10-31 统一重标定：原分 15~20% / ≥20% 两档，重算后两档强度几乎相同"
                "（−6.12% t=−9.05 vs −6.43% t=−9.02），故合并为一档、门槛降到 15% —— "
                "同一个信号拆成两行只会让权重分配失真。<15% 仍不命中（−1.35%，t=−1.84）。"
                "机制上它等价于「极端放量的极端情形」，是本系统里因果链最清楚的一条。"
                "T+10 仍在恶化（−9.92%，t=−9.34，胜率 12.7%），不是一日情绪。",
    },
    # -------- 量价类：2026-10-31 统一引擎重算，「放量下跌」被推翻 --------
    {
        "key": "vol_extreme", "group": "avoid", "name": "极端放量",
        "rule": "当日量比 ≥3（本项目量比 = 当日量/含当日5日均量，上限≈5，对应真实量比约 5~7 倍）",
        "horizon": 5, "n": 784, "days": 138,
        "excess": -1.81, "t": -2.59, "win": 38.4, "status": "active", "weight": 2,
        "probe": "viewer/hypo.py（predicate: volume_ratio >= 3）",
        "note": "**不分涨跌**。「放量上涨」同样 −0.97%（t=−3.81）——起作用的是极端放量本身"
                "（注意力峰值），不是涨跌方向。这条与龙虎榜是同一个机制的两个刻度："
                "龙虎榜是「极端放量 + 上了榜」，这里是「纯看量」。"
                "**长样本（2016 起 2590 个交易日）按日均值只剩 −0.84%（t=−5.59）**——"
                "方向仍在、显著，但幅度比短样本小一半，权重从 4 降到 2。",
    },
    {
        "key": "vol_heavy", "group": "avoid", "name": "放量",
        "rule": "当日量比 2~3",
        "horizon": 5, "n": 12171, "days": 144,
        "excess": -0.89, "t": -4.13, "win": 41.1, "status": "active", "weight": 1,
        "probe": "viewer/hypo.py（predicate: volume_ratio >= 2）",
        "note": "与「极端放量」互斥分档（2~3 给 1 分，≥3 给 2 分），不叠加 —— 同一天不该"
                "因为同一个现象拿两次分。**长样本（2621 个交易日）按日均值只有 −0.18%"
                "（t=−2.55）**：显著但幅度可忽略，权重从 2 降到 1。",
    },
    {
        "key": "crash_big", "group": "avoid", "name": "大跌",
        "rule": "当日跌幅 ≥7%",
        "horizon": 5, "n": 17649, "days": 143,
        "excess": -1.15, "t": -2.99, "win": 42.4, "status": "active", "weight": 5,
        "probe": "viewer/hypo.py（predicate: ret1 <= -7）",
        "note": "比「跌 5%~7%」更强（−1.15% vs −0.80%），故分成两档而非一条阈值。"
                "**长样本（2569 个交易日）按日均值 −1.48%、t=−11.71，比短样本更强**，"
                "权重 4→5。但要记住它的双面性：逐样本均值是 **+0.89%** —— 少数暴跌后"
                "暴力反弹的日子赚得极多，把逐笔平均拉成了正数。按规则反复做的期望看按日均值"
                "（亏），单笔押注的体验看逐样本（可能赚、方差巨大）。",
    },
    {
        "key": "crash_mid", "group": "avoid", "name": "当日下跌",
        "rule": "当日跌幅 5%~7%",
        "horizon": 5, "n": 42783, "days": 143,
        "excess": -0.80, "t": -2.56, "win": 43.9, "status": "active", "weight": 2,
        "probe": "viewer/hypo.py（predicate: ret1 <= -5）",
        "note": "与「大跌」互斥（≥7% 归上一档）。",
    },
    {
        "key": "macd_dead", "group": "avoid", "name": "MACD死叉",
        "rule": "MACD 柱当日由正转负（事件，不是「柱为负」的状态）",
        "horizon": 5, "n": 29344, "days": 143,
        "excess": -0.51, "t": -2.01, "win": 43.8, "status": "weak", "weight": 0,
        "probe": "viewer/hypo.py（predicate: macd_cross_down == 1）",
        "note": "t 刚过线、幅度中等，只作辅助。注意它是**事件型**字段：用「柱为负」这种"
                "状态型定义会覆盖近半股票，结论完全不同。"
                "**长样本（2617 个交易日）按日均值仅 −0.08%、t=−1.09 → 已不显著**，"
                "状态降为 weak、权重归零：只展示，不再参与避坑打分。",
    },
    {
        "key": "vol_down", "group": "avoid", "name": "放量下跌",
        "rule": "当日量比 ≥1.8 且跌幅 ≥3%",
        "horizon": 5, "n": 2691, "days": 143,
        "excess": 0.01, "t": -1.73, "win": 46.9, "status": "active", "weight": 1,
        "probe": "viewer/hypo.py（predicate: volume_ratio >= 1.8 and ret1 <= -3）",
        "note": "**这条翻过两次案，是「短样本会把真效应判死」的标本：**\n"
                "  ① 首版 4 分，宽松口径 −0.65%（t=−3.93）；\n"
                "  ② 2026-10-31 严格口径在 288 日样本上只有 +0.01% → 判 refuted、删除；\n"
                "  ③ 因子表回补到 2016 后重算：**按日均值 −0.56%，t=−5.36** —— 效应是真的，"
                "只是被 2025-08 起那一段特殊行情抹平了。\n"
                "恢复为 active，但权重只给 1：它和「放量」「大跌」是同一个现象的不同刻度，"
                "放进 avoid 分档会重复计分，因此只作提示、不进分。\n"
                "口径警告：逐样本均值 +0.12%（符号相反），说明少数反弹日贡献了全部正收益。",
    },
    {
        "key": "shrink_down", "group": "avoid", "name": "缩量下跌",
        "rule": "当日量比 ≤0.6 且收跌",
        "horizon": 5, "n": 18638, "days": 143,
        "excess": -0.24, "t": -1.97, "win": 45.4, "status": "active", "weight": 1,
        "probe": "viewer/hypo.py（predicate: volume_ratio <= 0.6 and ret1 < 0）",
        "note": "幅度 −0.24% 在短样本上被判无意义（扣双边成本约 0.2% 后基本归零）；"
                "长样本按日均值 −0.21%、t=−3.03，**方向稳定但幅度确实小**，只给 1 分提示。",
    },
    {
        "key": "two_day_drop", "group": "avoid", "name": "两日累跌",
        "rule": "两日累计跌幅 ≥6%",
        "horizon": 5, "n": 62108, "days": 142,
        "excess": -0.36, "t": -1.75, "win": 45.4, "status": "active", "weight": 1,
        "probe": "viewer/hypo.py（predicate: ret2 <= -6）",
        "note": "短样本 −0.36%（t=−1.75）被判不够；长样本按日均值 −0.45%、t=−5.08，"
                "**显著且方向稳定**。仍只给 1 分：覆盖 13% 的股票，权重高了会把整张表的"
                "区分度稀释掉（这是它当初被删的真正理由，不是效应不存在）。",
    },
    # ---------------- 公告类：口径修正后强度大幅下调 ----------------
    {
        "key": "ann_reduce", "group": "avoid", "name": "股东减持",
        "rule": "近 20 个自然日内有「股东减持」类公告（已剔除承诺不减持/终止减持）",
        "horizon": 5, "n": 1253, "days": 129,
        "excess": -1.83, "t": -4.62, "win": 35.8, "status": "active", "weight": 3,
        "probe": "viewer/hypo.py（predicate: ann_neg == 1）",
        "note": "**这条经历了两次翻案，是「样本覆盖度决定结论」的最好例子：**\n"
                "  ① 最初 n=69 → 报 t=−5.66（小样本虚高）；\n"
                "  ② 口径修正（公告盘后才知，次日才能买）后 n=115 → t=−0.13，被判「强度不足」降权；\n"
                "  ③ 全市场公告抓取上线（`viewer/ann_feed.py`）后 n=1253 / 129 个交易日\n"
                "     → T+5 −1.83%、t=−4.62，权重回到 3。\n"
                "真正的原因一直不是「减持没用」，而是**只看了 3% 的股票**。",
    },
    # ---------------- 解禁 ----------------
    {
        "key": "lift_first_holder", "group": "lift", "name": "首发原股东解禁临近",
        "rule": "未来 20 个交易日内解禁，且解禁方为首发原股东，且解禁量占流通股 ≥3%",
        "horizon": 20, "n": 423, "days": 90,
        "excess": -3.36, "t": -5.09, "win": 42.0, "status": "active", "weight": 0,
        "probe": "tools/probe_lift_timing.py",
        "note": "**抛压在解禁前 20 个交易日，不是解禁后**（解禁后 T+5 反而 +0.31%）。"
                "前 60~41 日仍 +0.44%，前 40~21 日 −0.94%（t=−1.16 不显著），"
                "说明只有最后一个月才该提示。定增解禁、股权激励解禁均不显著，"
                "**只认首发原股东**。看占流通比例，不看解禁金额。",
    },
    # ---------------- 论坛类：已停抓，留档防人重新捡起 ----------------
    {
        "key": "forum_hold", "group": "forum", "name": "论坛实盘选手 HOLD",
        "rule": "淘股吧/雪球实盘选手发帖声明「持仓不动」",
        "horizon": 5, "n": 124, "days": 25,
        "excess": -1.96, "t": -1.79, "win": 45.0, "status": "refuted", "weight": 0,
        "probe": "tools/probe_hold_net.py",
        "note": "原始口径 −3.08%（t=−2.57）看起来能用，但用「同日、前 5 日涨幅相差 ≤2pp」的"
                "普通股票做配对后只剩 −1.96%（t=−1.79，不显著）。即：那 3% 里 1.1% 只是"
                "「前期涨得差不多的股票也会跌」。且一天只筛出 4 只标的（覆盖 0.07%）。"
                "**机制**：选手是在涨完之后才发帖，发帖前 5 日已有 +6.4%~+10.9% 超额。",
    },
    {
        "key": "forum_bullish", "group": "forum", "name": "淘股吧看多帖",
        "rule": "淘股吧主帖情绪判为「看多」",
        "horizon": 5, "n": 210, "days": 22,
        "excess": -1.41, "t": -2.02, "win": 46.0, "status": "watch", "weight": 0,
        "probe": "tools/probe_crowd_signal.py",
        "note": "t 值刚过线但源已停更（Cookie 2026-10-24 失效），无法继续验证。留档：若将来重启"
                "社区采集，这是唯一值得先复测的切面。",
    },
    # ---------------- 系统自身的荐股 ----------------
    {
        "key": "selfpick_a", "group": "forward", "name": "每日荐股 A 档",
        "rule": "self_pick 表中 tier='A' 的标的",
        "horizon": 5, "n": 127, "days": 6,
        "excess": -3.00, "t": -2.05, "win": 46.0, "status": "watch", "weight": 0,
        "probe": "（在库计算：self_pick.ex_t5）",
        "note": "⚠️ **只有 6 个独立交易日**（2026-10-14~10-21），自由度 5，这个 t 值本身不可靠。"
                "更硬的证据来自独立样本：A 档前 5 日平均涨幅 +4.84%（市场基准≈0），"
                "即它在追高；而全市场「前 5 日涨 5-15%」的股票 T+5 超额仅 −0.25%（t=−2.41），"
                "A 档在同档里是 −6.36%，**比随机选还差 6 个百分点**。"
                "评分体系里没有量价/趋势维度，追高来自候选池特征，不是打分错误。"
                "结论：不改逻辑（样本不足会过拟合），等积累 ≥60 个交易日再评估。",
    },
)

# ---------------------------------------------------------------- 明确禁区
#
# 这些说法曾被写进代码或口口相传，但实测方向相反 / 不成立。
# **单独列出来，是因为「不要做什么」比「要做什么」更容易被遗忘。**
# 模型上下文里必须出现这一段 —— 否则它会自然地引用这些"常识"。

REFUTED_NOTES: tuple = (
    ("放量下跌就该躲（曾经最大的误判，2026-10-31 二次翻案）",
     "宽松口径（当日收盘买入）−0.65%（t=−3.93）看似有效，改可执行口径后在 288 日短样本上"
     "只剩 +0.01% → 一度判死、删掉 20% 权重。**因子表回补到 2016 后重算：按日均值 −0.56%、"
     "t=−5.36，效应是真的**，只是短样本那一段（2025-08 起）恰好把它抹平了。教训不是「放量下跌"
     "没用」，而是**不能用一个 14 个月的窗口给规则判死刑**。现为 active、权重 1（与放量/大跌"
     "同源，不进分档以免重复计分）。另一层：逐样本均值 +0.12%，少数反弹日贡献了全部正收益。", "viewer/hypo.py"),
    ("超买要回调（RSI6>85）", "严格口径 **−0.10%（t=−2.72）**：方向与直觉一致，但幅度只有 0.1%，"
     "扣掉成本毫无意义；宽松口径 +0.17% 是当日涨幅造成的假象。注意措辞：**这不是「方向相反」，"
     "而是「方向对但幅度太小」** —— 前者该删除，后者该忽略，两者都不能当依据。",
     "viewer/hypo.py"),
    ("偏离均线过多要回归（BIAS6>10）", "严格口径 −0.10%（t=−2.87），同 RSI6：方向对、幅度无意义。"
     "宽松口径与严格口径符号相反，这正是「可执行性」造成的差异。", "viewer/hypo.py"),
    ("跌破 MA20 该走", "事件口径（当日由 ≥MA20 转为 <MA20）−0.51%（t=−1.52，不显著）；"
     "用「偏离 MA20 超过 −10%」近似则为 −0.06%（t=−0.47）。两种定义都测不出可用效应。",
     "viewer/hypo.py"),
    ("利空公告一律避开（tone=bad 全量）", "实测仅 −0.27%，被流程性公告（担保、制度修订）稀释。"
     "按类别拆开后，只有减持/人事勉强有效，而监管处罚 **+1.47%**、风险提示 **+0.82%**、"
     "诉讼 **+0.42%**、资产冻结 **+0.72%** 全是**正向**（利空出尽）。",
     ".probe/probe_ann_caliber.py"),
    ("高管人事变动是利空", "严格口径 T+5 −0.85%（t=−0.74）、T+20 −3.39%（t=−1.57），全周期不显著。"
     "已从 avoid 规则中删除。", ".probe/probe_ann_caliber.py"),
    ("博主推荐过的票要回避", "旧「博主反向警示」拿 6 月推荐绑 11 月的票，荐股保鲜期实测不到 20 天；"
     "且源已停更，属结构性失效，不是参数问题。", "docs/避坑指标改版_20261030.md"),
    ("跟着实盘选手持仓不动", "见 forum_hold：配对后不显著。", "tools/probe_hold_net.py"),
    ("实盘选手老手更准", "三分类（按历史战绩）后无单调关系；「被套才死扛」假说也被否（r=+0.206，"
     "方向与假说相反）。", "tools/probe_crowd_signal.py"),
)

# ---------------------------------------------------------------- 长样本复核合并
#
# 2026-10-31 补做的一件事：上面每条 claim 的 n/days/excess/t 都是**这张因子表当时
# 能覆盖的 288 个交易日（2025-08-18 起）**算出来的——单一牛市段落。t 值再大也只证明
# "这一年有效"。我把因子历史回补到 2016-01-04（2627 个交易日、10.4 万条/日）后，
# 用 tools/long_sample_audit.py 把 10 条可落谓词的 claim 在同一严格口径下重算了一遍。
#
# 结论分三档（详见 docs/长样本复核_20261031.md）：
#   consistent  长样本同向且更强/持平 → 可继续用
#   weakened    幅度打折过半或失去显著性 → 必须降权，否则等于凭短样本重量
#   no_sample   数据本身只有这么长（龙虎榜 20 天、减持公告 130 天）→ 不是统计问题
#
# 这里不把数字抄死，而是从 data/long_sample_audit.json 合并进来：改口径只需重跑工具，
# 账本自动跟着变，不会出现"文档一套数、代码一套数"。
_LONG_JSON = "data/long_sample_audit.json"


def _merge_long_sample() -> None:
    import json
    import os
    p = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), _LONG_JSON)
    if not os.path.exists(p):
        return
    try:
        with open(p, "r", encoding="utf-8") as f:
            doc = json.load(f)
    except Exception:  # noqa: BLE001
        return
    m = {r["key"]: r for r in doc.get("rows") or []}
    for c in CLAIMS:
        r = m.get(c["key"])
        if not r:
            continue
        lg = r.get("long") or {}
        c["long"] = {
            "start": doc.get("start"),
            "n": lg.get("n"), "days": lg.get("days"),
            "excess_day": lg.get("excess_day"), "excess_sample": lg.get("excess"),
            "t": lg.get("t"), "conflict": bool(lg.get("conflict")),
            "verdict": r.get("verdict"),
        }
        # 口径冲突必须留在 note 里：逐样本均值为正、按日均值为负，说明这条效应
        # "大多数时候亏、少数反弹日赚得极多"，只看一个均值会得出相反的结论。
        if lg.get("conflict") and "口径冲突" not in (c.get("note") or ""):
            c["note"] = (c.get("note") or "") + (
                "【长样本】%s 起 %s 个交易日重算：按日均值 %+.2f%%，逐样本均值 %+.2f%% "
                "（符号相反）——效应是「多数日子小幅跑输 + 极少数反弹日大幅跑赢」，"
                "按规则反复做，期望看按日均值。" % (
                    doc.get("start"), lg.get("days"), lg.get("excess_day") or 0,
                    lg.get("excess") or 0))


_merge_long_sample()

# ---------------------------------------------------------------- 查询接口


def active(group: str = "") -> list:
    """可当规则使用的（含 weak —— 它们能提示但不给分）。"""
    return [c for c in CLAIMS
            if c["status"] in ("active", "weak") and (not group or c["group"] == group)]


def refuted() -> list:
    return [c for c in CLAIMS if c["status"] == "refuted"]


def get(key: str):
    for c in CLAIMS:
        if c["key"] == key:
            return c
    return None


def summary() -> dict:
    """给前端/接口用的汇总。"""
    from collections import Counter

    cnt = Counter(c["status"] for c in CLAIMS)
    return {
        "asof": ASOF,
        "n_claims": len(CLAIMS),
        "by_status": dict(cnt),
        "claims": [dict(c) for c in CLAIMS],
        "refuted_notes": [{"claim": a, "evidence": b, "probe": c} for a, b, c in REFUTED_NOTES],
    }


def prompt_block(max_chars: int = 3000) -> str:
    """给大模型读的证据清单（紧凑中文，不是 JSON —— 省 token 且模型更不容易误读）。

    刻意分成「可以用的」「强度不足的」「已证伪的」三段。第三段最重要：
    它是给模型的**负面清单**，比正面规则更能防止胡说。
    """
    L = ["【本系统的实证边界】以下全部由本地回测得出，超额 = T+H 相对同日全市场等权。",
         "标 weak 的强度不足，只能当提示、不能当依据；标 refuted 的一律不许使用。",
         "「长样本」= 因子历史回补到 2016-01-04 后的重算结果，以**按日均值**为准（每天等权，"
         "抗单日聚集）；「逐样本」均值会被少数反弹日拉偏，两者符号相反时只认按日均值。", ""]

    def fmt(c):
        lg = c.get("long") or {}
        tail = ""
        if lg.get("t") is not None:
            tail = "｜长样本(%s 起 %s 日) 按日均值 %+.2f%%，t=%+.2f%s" % (
                lg.get("start"), lg.get("days") or 0, lg.get("excess_day") or 0.0,
                lg.get("t") or 0.0,
                "（逐样本 %+.2f%% 符号相反，别用）" % (lg.get("excess_sample") or 0.0)
                if lg.get("conflict") else "")
        return ("· %s：%s → T+%d 超额 %+.2f%%，t=%+.2f，胜率 %.0f%%（短样本 n=%d，%d 个交易日）%s"
                % (c["name"], c["rule"], c["horizon"], c["excess"], c["t"], c["win"],
                   c["n"], c["days"], tail))

    a = [c for c in CLAIMS if c["status"] == "active"]
    w = [c for c in CLAIMS if c["status"] == "weak"]
    L.append("■ 可作依据（active，%d 条）" % len(a))
    L.extend(fmt(c) for c in a)
    if w:
        L.append("")
        L.append("■ 仅供参考（weak，强度不足，不许单独据此下结论，%d 条）" % len(w))
        L.extend(fmt(c) for c in w)
    L.append("")
    L.append("■ 已证伪 —— 禁止使用（refuted/watch）")
    for c in CLAIMS:
        if c["status"] in ("refuted", "watch"):
            L.append("· %s：%s（实测 %+.2f%%，t=%+.2f，%s）"
                     % (c["name"], c["rule"], c["excess"], c["t"],
                        "已推翻" if c["status"] == "refuted" else "证据不足/源已停"))
    L.append("")
    L.append("■ 反直觉的禁区（这些是「常识」，但本市场实测相反）")
    for a_, b_, _ in REFUTED_NOTES:
        L.append("· %s —— %s" % (a_, b_))

    s = "\n".join(L)
    if len(s) > max_chars:
        s = s[:max_chars] + "\n…（清单已截断）"
    return s


if __name__ == "__main__":
    # 自检：python viewer/evidence.py
    print(prompt_block())
    print("\n" + "=" * 70)
    s = summary()
    print("账本 %s：共 %d 条，状态分布 %s" % (s["asof"], s["n_claims"], s["by_status"]))
    # 显著性以**长样本**为准（有长样本结果的用长样本 t，没有的才退回短样本 t）——
    # 短样本 t 只是历史记录，用它对 active 做体检会把被长样本救回来的条目误判成违规。
    def _t_of(c):
        lg = c.get("long") or {}
        return lg.get("t") if lg.get("t") is not None else c["t"]

    bad = [c["key"] for c in CLAIMS if c["status"] == "active" and abs(_t_of(c)) < 2]
    print("自检 active 但 |t|<2 的条目：", [c["key"] for c in bad] or "无（通过）")
    bad2 = [c for c in CLAIMS if c["weight"] and c["status"] in ("refuted", "watch")]
    print("自检 refuted 却给了权重：", [c["key"] for c in bad2] or "无（通过）")
