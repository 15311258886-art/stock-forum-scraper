# -*- coding: utf-8 -*-
"""参数注册表 —— 阈值与样本量口径的**唯一来源**（2026-10-31 新增，测评 P0-D）。

## 为什么要有这个文件

本轮评审发现同一套代码里有两个互不相容的样本充足性门槛：

    judge()  要求 n ≥ 500 才允许裁决（否则 insufficient，"我没资格判断"）
    eff       用常数 40（`1 - exp(-n/40)`，n=40 就到 63% 权重）

复现最锋利的一行是 **n = 499**：
    judge 答"样本不足、无权裁决"，eff 已给出 99.9996% ——
    **系统在同一次输出里同时说"我没资格裁决"和"我有 99.9996% 的把握"。**

(40, 500) 这 460 的区间，全部是"高置信度但禁止裁决"。
这把问题从"各处都有硬编码"升级为"**两个参数互相打脸、而没有任何一处代码或检查发现**"
—— 缺的是机制，不是细心。

## 为什么不能靠"把 40 改成 500"收口

那样只是把矛盾藏起来：改完会出现 30／40／500 三个阈值继续分叉，
而分叉的根源是"每个模块自己写自己的字面量"。所以：

  1. 三个阈值（eff_scale / eff_floor / judge_min_n）引用**同一个来源**（本文件）；
  2. 不变式写成**可机判的断言**：任意 n 都须满足「可裁决 ⟺ eff ≥ θ」，
     θ 显式声明，不隐式等于某个巧合；
  3. 任何模块再出现新的相关字面量，即判未修（见 tools/audit_params.py）。

## 当前状态：矛盾已按「样本量三层制」处置（2026-10-31 进哥拍板走 C）

原本只有两条路，两条都要付代价（重算全部历史 / 放行不够的样本），所以都没走。
实际走的是第三条：**不合并两个门槛，也不动任何一个数字，而是把 40–500 这段
明确定义成"观测中"** —— 它给方向，但不给权重、不进组合、不参与"强/中"判定。

    n <  OBSERVE_MIN_N(40)   观测     不判方向，一个说法都不给
    40 ≤ n < JUDGE_MIN_N(500) 观测中   给方向，usable=False，**权重门=线性 0→1**
    n ≥  JUDGE_MIN_N(500)    可裁决   完整裁决，权重门=1

两条关键性质（这是它优于 A/B 的全部理由）：

  1. **n ≥ 500 的信号权重与改动前逐位相同** —— 因为 `weight_gate(n≥500)=1`，
     `eff_weight = eff_of`，历史评分不需要重算，仍然可比；
  2. **40–500 不再"偷偷拿满权重"** —— 原来 eff(200)=0.992，权重大开，
     而 judge 同时说"不许裁决"。现在这段按线性门压到 0~1 之间，n=40 时归零。

`weight_gate` 用线性而非阶跃，是刻意的：n=499 与 n=500 之间若有断崖，
权重会在一根样本上抖半个档位，实测比"分叉"更难查。
"""

import math

# ── 阈值：全部集中在这里，别处不许出现新的字面量 ──────────────────────────────
EFF_SCALE = 40.0          # eff = 1 - exp(-n / EFF_SCALE)；n=EFF_SCALE 时 eff≈0.632
EFF_FLOOR = 0.0           # eff 下限（不做负数截断之外的处理）
JUDGE_MIN_N = 500         # 单信号最小可信样本（原 viewer/kernel/evidence.MIN_N）
LIST_N = 20               # 每日名单只数上限（原散落在 server 的 rank<=20 与 universe_scan 的 top20）

# ── 样本量三层制（2026-10-31 决策①，进哥拍板走 C）────────────────────────────
# judge 要求 n≥500 才允许裁决，eff 用尺度 40（n=40 就到 0.63）。选 A（把 eff
# 尺度改成 500）会让所有历史评分作废；选 B（把门槛降到 120）会让样本不够的信号
# 被判"有效"。两条都付代价，所以走第三条：**不合并门槛，把 40–500 显式定义成
# "观测中"** —— 给方向，但不给权重、不进组合、不参与"强/中"判定。
OBSERVE_MIN_N = 40        # 开始给方向的最低样本。
                          # **不要**用 EFF_SCALE 推导它：两者同值纯属巧合，
                          # 一旦互相派生就又是一处隐式耦合（也就是本轮要治的病）。
# 信号交集（两两同时触发）的最小可引用样本。
# 为什么不复用 JUDGE_MIN_N(500)：交集样本**天然**远小于单信号（两个条件的交集），
# 用 500 会让几乎所有组合直接判"样本不足"，整个检验等于白做；
# 用 OBSERVE_MIN_N(40) 又会把几十个样本的组合显示成能用。
# 200 是拍的（实测缺口），所以落库时带 usable 标记，前端必须显示 n 本身。
MIN_COMBO_N = 200

# 回测口径的双边综合成本（含税、含笼统滑点）。原来只在 signal_backtest.DEFAULT_FEE
# 写了一份，现在提到这里，因为 `cost_basis` 要把它和「执行口径」并列展示 ——
# 两份各写一个 0.0015，早晚会出现"界面上两个回测成本不一样"。
BACKTEST_FEE_ROUNDTRIP = 0.0015   # 佣金双边 5bp + 卖出印花税 5bp + 滑点 5bp

TIER_OBSERVE = "observe"
TIER_PROVISIONAL = "provisional"
TIER_DECIDABLE = "decidable"
TIER_LABEL = {TIER_OBSERVE: "观测", TIER_PROVISIONAL: "观测中", TIER_DECIDABLE: "可裁决"}

# ── K 线长度门槛（数据可用性，**不是**统计显著性）──────────────────────────────
# 语义与上面三行不同，别混用：上面问的是"这个结论有多少样本支撑"，
# 这里问的是"这只票的序列够不够算 20 日均线 / 够不够做 500 日前后的对齐"。
# 之所以也搬进来，是因为它们原本是 selection.py 里散落的 130 / 1020 两个字面量，
# 与样本量门槛长得几乎一样（都是 `if n < 数字`），属于同一类"第二来源"风险。
# 用 MIN_BARS_ 前缀区分，避免后来者把"序列长度不够"误读成"样本不足以裁决"。
MIN_BARS_SHORT_SIGNAL = 130    # 短周期研究：≥130 根日线（≈半年）才纳入滚动统计
MIN_BARS_LONG_ALIGN = 1020     # 中长期：需前 500 + 后 500 对齐，取 1020 留余量

# 一致性阈值：θ = eff(JUDGE_MIN_N)。由参数**导出**，不写死。
# 写成导出值而不是常数，是为了让"改参数后断言静默失效"这件事不可能发生。
def eff_of(n) -> float:
    """样本量置信度 0→1。唯一实现，predictor 与任何新模块都必须走这里。"""
    n = max(0.0, float(n or 0))
    return max(EFF_FLOOR, 1.0 - math.exp(-n / EFF_SCALE))


def tier_of(n) -> str:
    """样本量所属层：observe / provisional / decidable。

    这是全局唯一的分层实现 —— judge（裁决）与 predictor（权重）都必须调它。
    本轮矛盾的根因就是两边各自判断"样本够不够"，而口径不同。
    """
    try:
        x = float(n or 0)
    except (TypeError, ValueError):
        return TIER_OBSERVE
    if x != x or x < OBSERVE_MIN_N:      # NaN 也归到观测层，不留缝
        return TIER_OBSERVE
    if x < JUDGE_MIN_N:
        return TIER_PROVISIONAL
    return TIER_DECIDABLE


def weight_gate(n) -> float:
    """权重门 0→1。

      n <  40  → 0（一个说法都不给，权重必须归零，不能"给一点点"）
      40–500   → 线性 (n-40)/(500-40)
      n ≥ 500  → 1（**恒等**，所以 n≥500 的信号权重与改动前逐位相同）

    线性而非阶跃：n=499 与 n=500 之间若断崖，权重会在一根样本上抖半个档位。
    """
    t = tier_of(n)
    if t == TIER_OBSERVE:
        return 0.0
    if t == TIER_DECIDABLE:
        return 1.0
    span = float(JUDGE_MIN_N - OBSERVE_MIN_N)
    return max(0.0, min(1.0, (float(n) - OBSERVE_MIN_N) / span))


def eff_weight(n) -> float:
    """**给权重计算用**的置信度 = eff_of(n) × weight_gate(n)。

    与 eff_of 分开是为了让"展示"与"生效"用两个不同的数：
      · 展示（面板上那句"置信度 99.2%"）用 eff_of —— 它描述样本量本身；
      · 生效（乘进 hit_score / cov_blend）用 eff_weight —— 它还要过裁决门槛。
    原来两者是同一个数，于是系统一边说"没资格裁决"、一边乘着 99.2% 的权重。
    """
    return max(EFF_FLOOR, eff_of(n) * weight_gate(n))


def judge_min_eff() -> float:
    """θ：eff 达到多少才算"样本够了"。由 JUDGE_MIN_N 与 EFF_SCALE 导出。"""
    return eff_of(JUDGE_MIN_N)


def invariant_ok(n: int) -> bool:
    """不变式：任意 n 都须满足「judge 可裁决 ⟺ eff ≥ θ」。

    judge 可裁决 ⟺ n ≥ JUDGE_MIN_N；eff ≥ θ ⟺ eff(n) ≥ eff(JUDGE_MIN_N) ⟺ n ≥ JUDGE_MIN_N
    （因为 eff 对 n 严格单调）。所以这条不变式在**当前参数下恒成立** ——
    它本来就是构造出来的，不是测出来的。**这恰恰是问题所在**：
    两个数一个 40 一个 500，却能被同一套参数导出成一个自洽的 θ。
    真正暴露矛盾的不是这条恒等式，而是下面的 invariant_gap()。
    """
    return (n >= JUDGE_MIN_N) == (eff_of(n) >= judge_min_eff())


def invariant_gap() -> dict:
    """矛盾带宽：eff 已经"很确信"、而 judge 仍说"没资格"的样本量区间。

    判据取 eff ≥ 0.95（"很确信"的通俗阈值）到 JUDGE_MIN_N 之间的带宽。
    当前值：(EFF_SCALE=40, JUDGE_MIN_N=500) ⇒ eff(120)=0.950 ⇒ 带宽 = 500-120 = 380。
    实测报告里写的 460 用的是 eff≥0.99（n≈184）那一档口径；
    两种口径都对，只是"很确信"的取法不同 —— 所以这里把口径写出来，不写一个裸数。

    带宽 > 0 ⇒ 系统在同一个 n 上同时表达"高置信"与"无资格"。这是矛盾，
    不是精度问题：它会让"权重按置信度缩放"和"结论按样本量屏蔽"两套逻辑
    在同一份输出里彼此否定。
    """
    conf = 0.95
    # eff(n) = 1 - exp(-n/s) ≥ conf  ⇒  n ≥ -s·ln(1-conf)
    n_conf = -EFF_SCALE * math.log(1.0 - conf)
    return {"eff_conf_level": conf, "n_at_conf": round(n_conf, 1),
            "judge_min_n": JUDGE_MIN_N,
            "band": max(0.0, round(JUDGE_MIN_N - n_conf, 1)),
            "consistent": JUDGE_MIN_N <= n_conf + 1e-9,
            # 2026-10-31 决策①：band 照实报（它是事实），但矛盾已被三层制**处置**：
            # 这段区间不再产出"高置信 + 无权裁决"的自相矛盾输出，
            # 而是产出一条明确标注"观测中、不进组合"的记录。
            "resolved_by": "tier",
            "resolved": True,
            "tiering": {"observe_below": OBSERVE_MIN_N,
                        "provisional": [OBSERVE_MIN_N, JUDGE_MIN_N],
                        "decidable_at": JUDGE_MIN_N,
                        "provisional_usable": False,
                        "note": ("40–500 给方向但不给权重、不进组合；"
                                 "n≥500 权重门恒为 1，历史评分无需重算")}}


# ── 执行可行性 / 容量（2026-10-31 新增，测评「可执行性未判定」项）───────────────
# 报告的容量式：AUM ≤ CAPACITY_PARTICIPATION × N × min(ADV)
# 其中 N 是**你真实会买的只数**，不是常数；必须取 min(ADV) 而非平均 ADV
# （等权时约束对全部持仓同时成立，一只票拖后腿就决定门槛）。
# 0.001444 是报告从冲击成本模型反解出的参与率上限（单票下单额 / 当日成交额），
# 量级 0.14%。它是**点估计**，不是区间上界 —— 所以下面所有容量输出都写成区间。
CAPACITY_PARTICIPATION = 0.001444

# 成本分档（按 60 日 ADV，单位元）。**这四个数是我拍的，不是实测** ——
# 报告只说了"成本按流动性分档"这条修法，没给档位值；A 股的实际冲击成本
# 需要用自己的成交回报标定。所以：档位集中在此，改这里一处即可全链生效，
# 且 `executability.describe()` 会把这句"是拍的"一起返回给前端，
# 不允许它变成一个看起来像实测的漂亮数字。
COST_TIERS_BP = (
    (500_000_000, 10),   # ADV ≥ 5 亿 → 10bp
    (100_000_000, 20),   # 1–5 亿 → 20bp
    (50_000_000, 35),    # 5,000 万–1 亿 → 35bp
    (20_000_000, 60),    # 2,000 万–5,000 万 → 60bp
)
LIQUIDITY_REJECT_ADV = 20_000_000   # ADV < 2,000 万：直接判不可执行（不入档，不是高档）
COST_FLOOR_BP = 10                  # 报告里 Δ = E − 10bp 用的成本 fence，口径对齐用

# ── 成本校准位（2026-10-31 决策⑤，进哥拍板走 C）──────────────────────────────
# 现在先用拍定档位出结论（保守方向只会低估收益，不会把亏看成赚），
# 同时开一个口子接他自己的成交回报；攒够样本再回头校准。
# 校准**不自动改 COST_TIERS_BP** —— 只产出建议值 + 样本量，改不改由人定。
# 三个门槛的理由：
#   · 30 笔是"分档统计开始有意义"的下限，低于这个数的分档只是把噪声排列了一下；
#   · 每档 8 笔：一档里不到 8 笔，中位数会被单笔异常值完全决定；
#   · 取 p75 而不是中位：成本估低比估高危险 —— 估高只是少做几笔，估低是以为能赚。
COST_CALIB_MIN_TRADES = 30
COST_CALIB_MIN_PER_TIER = 8
COST_CALIB_SUGGEST_PCT = 75
ADV_WINDOW_DAYS_EXEC = 60           # 执行侧 ADV 窗口（与剔除池 illiquid 口径一致）
ADV_MIN_OBS_EXEC = 40               # 窗口内最少观测数，不足即判"ADV 不可估计"

# ── 持有期档位（2026-10-31 决策④，进哥拍板走 C）──────────────────────────────
# 原来的 hold_days 是"仅回显、不参与计算"。那样三档跑出来只有数字变、
# 结论不变，等于没跑。持有期真正影响的是**成本摊薄**：
#     年轮换次数 = 244 / hold_days
#     年化成本   = 年轮换 × 往返成本（2 × 单边）
# 3 日持有的年化成本是 20 日的 6.7 倍 —— 这才是短线打法真正的门槛。
# 它**不**影响容量：容量约束是"单票下单额 / 当日成交额"，与持有多久无关。
# 这一点必须写死在这里，否则很容易被误以为"拿久一点就能多装钱"。
TRADING_DAYS_PER_YEAR = 244
HOLD_PROFILES = {"short": 3, "swing": 10, "long": 20}
HOLD_PROFILE_LABEL = {"short": "短线 · 3 个交易日",
                      "swing": "波段 · 10 个交易日",
                      "long": "中线 · 20 个交易日"}


def cost_bp_of_adv(adv) -> float | None:
    """按 60 日 ADV（元）给单边冲击成本档位（bp）。ADV 不可估计 → None。

    返回 None 与返回某个大数必须分开：None 是"不知道"，大数是"很贵"。
    """
    if adv is None:
        return None
    try:
        a = float(adv)
    except (TypeError, ValueError):
        return None
    for floor, bp in COST_TIERS_BP:
        if a >= floor:
            return float(bp)
    return None


def aum_cap_point(n_hold, min_adv) -> float | None:
    """容量上界点估计：CAPACITY_PARTICIPATION × N × min(ADV)。

    注意三点，缺一条这个数就会被误读成"策略容量"：
      · 它是**点估计**，参数本身是点估计，所以它没有置信区间；
      · 下界是 0 且可达（容量 ∝ Δ²，Δ = E − 成本，E 取到区间下沿时 Δ≤0）；
      · min(ADV) 来自"你以为会买的那个池"，池一换，这个数就跟换。
    """
    try:
        n = int(n_hold)
        a = float(min_adv)
    except (TypeError, ValueError):
        return None
    if n <= 0 or a <= 0:
        return None
    return CAPACITY_PARTICIPATION * n * a

# ── 结论卡评分系数（2026-10-31 新增，测评 P1「结论卡证据强度」项）─────────────
# 报告判据原文：**结论层只允许出现原始参数，不允许出现压缩系数。**
# 原实现里一张结论卡的"强/中/弱"由 6 个魔数相减得出：
#     多路偏多 ×9 ／ 偏空 ×14 ／ 合成卡档位 ×7 ／ 豆包看多 ×12 ／ 看空 ×16 ／ 样本 ×3
# 这些数没有任何来源，而它们的差 `net = conf - risk` 一旦越过 45 就印一个「强」。
# 于是"强"这个字背后既没有样本量、也没有检验，只有系数和阈值。
#
# 本轮的处置**不是**去调这 6 个数（调了也只是换一组魔数），而是：
#   ① 把系数搬到唯一来源，改一处全链生效；
#   ② 强制结论卡输出 `strength_basis`，把"强"字的来历写在它旁边；
#   ③ 明写 `CARD_WEIGHTS_ARE_ARBITRARY = True` —— 让下游/前端知道这一层不可当证据引用。
CARD_WEIGHTS_ARE_ARBITRARY = True
CARD_SCORE_W = {
    "multi": 9.0,        # 六源中偏多一路
    "short": 14.0,       # 六源中偏空一路
    "tier": 7.0,         # 合成卡档位加权
    "bull_tag": 12.0,    # 豆包看多标注
    "bear_tag": 16.0,    # 豆包看空标注
    "sample": 3.0,       # 每个帖子样本（上限 4 个）
    "both_dir": 8.0,     # 多空并存惩罚
}
CARD_SAMPLE_CAP = 4          # 样本加权最多算几个帖子
CARD_SRC_CAP = 5             # 单侧最多算几路信号
CARD_SCORE_CAP = 100.0       # conf / risk 上限
CARD_STRONG_NET = 45.0       # |net| ≥ 此值印「强」
CARD_MID_NET = 25.0          # |net| ≥ 此值印「中」
CARD_DIR_NEUTRAL_NET = 15.0  # |net| 低于此值且多空接近 → 「中性」

CARD_STRENGTH_BASIS = (
    "「强/中/弱」由 conf−risk 的差值按固定阈值切分，conf/risk 是 6 个**拍定系数**的加权和。"
    "它不是显著性结论，也不含样本量与检验 —— 系数本身无来源，见 params.CARD_SCORE_W。"
    "引用时必须连同这句一起引用。"
)



# ── 权重学习开关（2026-10-31 新增，测评 P0-D ④）──────────────────────────────
# 报告建议：删 coverage、冻结命中率乘子与 rmul，只留 eff 与先验。
# 理由成立 —— 有效样本上限约 5 个（n_eff = n/fwd），在这个量级上调权重就是放大噪声。
# 但"只留 eff 与先验"有两种读法，产物差别极大，**所以没有默认切换**：
#   · "prior"：raw = 先验（SIGNAL_PRIOR 的手工排序）—— 稳定、可解释，但名单近乎不随数据变；
#   · "equal" ：raw = 1.0 —— 十二路等权，等于放弃区分。
# 两种都不该由我替进哥选，故默认保持现状（True），开关保留一行可切。
# 翻转方式：把下一行改成 False，并选定 FREEZE_SEMANTICS，然后**重算全部历史评分**
# （否则新旧评级不可比）。
WEIGHT_LEARNING_ENABLED = True
FREEZE_SEMANTICS = "prior"       # "prior" | "equal"，仅在 WEIGHT_LEARNING_ENABLED=False 时生效

# ── 影子权重（2026-10-31 决策②，进哥拍板走 C）────────────────────────────────
# 生产用先验（稳、可解释），同时后台再算一份"完全不用先验、让数据说话"的权重，
# **只记录不生效**。攒够样本再决定要不要切 —— 这样既不锁死，也不拿现网做实验。
#
# 为什么现在不直接开数据权重：全量回测 9 个信号里只有 KDJ 超卖金叉税后 +0.46%，
# 其余 8 个是 −0.10% ~ −0.65%。在信号本身没有正超额的时候让数据学权重，
# 学出来的是一组负期望的权重 —— 越学越糟。先证明某一路有正超额，再谈加权。
SHADOW_WEIGHTS_ENABLED = True
SHADOW_WEIGHTS_NOTE = ("影子权重 = 只用实盘命中率/覆盖/超额，**不用先验**，归一化口径与生产一致。"
                       "它每日落一行记录，不参与任何评分、排序、组合。"
                       "切换条件：某一路影子权重连续 20 个交易日高于生产权重，且该路税后超额为正。")


def describe() -> dict:
    """给 API / 日志用的自述。改参数后这里会自动跟着变，不需要同步第二处文档。"""
    g = invariant_gap()
    return {"EFF_SCALE": EFF_SCALE, "EFF_FLOOR": EFF_FLOOR,
            "JUDGE_MIN_N": JUDGE_MIN_N, "LIST_N": LIST_N,
            "OBSERVE_MIN_N": OBSERVE_MIN_N,
            "TIER_LABEL": dict(TIER_LABEL),
            "MIN_BARS_SHORT_SIGNAL": MIN_BARS_SHORT_SIGNAL,
            "MIN_BARS_LONG_ALIGN": MIN_BARS_LONG_ALIGN,
            "CAPACITY_PARTICIPATION": CAPACITY_PARTICIPATION,
            "COST_TIERS_BP": list(COST_TIERS_BP),
            "LIQUIDITY_REJECT_ADV": LIQUIDITY_REJECT_ADV,
            "COST_FLOOR_BP": COST_FLOOR_BP,
            "ADV_WINDOW_DAYS_EXEC": ADV_WINDOW_DAYS_EXEC,
            "ADV_MIN_OBS_EXEC": ADV_MIN_OBS_EXEC,
            "CARD_SCORE_W": CARD_SCORE_W,
            "CARD_WEIGHTS_ARE_ARBITRARY": CARD_WEIGHTS_ARE_ARBITRARY,
            "CARD_STRENGTH_BASIS": CARD_STRENGTH_BASIS,
            "WEIGHT_LEARNING_ENABLED": WEIGHT_LEARNING_ENABLED,
            "FREEZE_SEMANTICS": FREEZE_SEMANTICS,
            "SHADOW_WEIGHTS_ENABLED": SHADOW_WEIGHTS_ENABLED,
            "SHADOW_WEIGHTS_NOTE": SHADOW_WEIGHTS_NOTE,
            "theta": round(judge_min_eff(), 6),
            "invariant_gap": g,
            "note": ("参数一致（eff 饱和点与裁决门槛同量级）" if g["consistent"] else
                     "矛盾带宽 %s（eff 在 n≈%s 已达 %.0f%%，而 judge 要到 n=%d 才裁决）"
                     "—— 已按三层制处置，两道门分开："
                     "① 硬门（进不进组合/排序）：<%d 不判方向；%d–%d 标「观测中」，"
                     "usable=False，一律不进组合、不参与强/中判定；"
                     "② 软门（权重乘子）：%d–%d 按 weight_gate 线性衰减，n=%d 归零、"
                     "趋近 %d 时趋于 1。n≥%d 时 weight_gate 恒为 1，"
                     "权重与改动前逐位相同，历史评分仍可比。"
                     % (g["band"], g["n_at_conf"], g["eff_conf_level"] * 100, g["judge_min_n"],
                        OBSERVE_MIN_N, OBSERVE_MIN_N, JUDGE_MIN_N,
                        OBSERVE_MIN_N, JUDGE_MIN_N, OBSERVE_MIN_N, JUDGE_MIN_N,
                        JUDGE_MIN_N))}
