"""工具资格契约（tool_contract）：把「这个工具今天能不能用」变成机器可判定的问题。

为什么要这个模块（2026-11-06 审计结论 P0-01）
    改之前 `tool_registry` 只回答「这个工具叫什么、写了什么、跑过几次」，
    `apply_tool()` 唯一的闸门是 `status != 'retired'`。于是系统能算出 33 个工具的名单，
    却回答不了最该先回答的那句：**今天该不该开这把工具**。
    审计实测里 `oss_high_vol`（回测结论是「高波动 = 风险」）被当正向选股器跑出 48 只名单、
    接口返回 ok=true —— 这就是「能执行」与「会使用」的差距。

三条不可让渡的规则（改这个文件前先读）
    1. **资格先于计算**：不满足前置条件就不调用工具，而不是算完再解释为什么不该用。
    2. **角色先于名称**：判据是 `role` / `decision_use`，不是「突破」「超跌」这类名字。
       名字会骗人（`box_bottom` 的默认 pos_max 曾经是 1.0，能筛出 100% 箱体位置的票）。
    3. **无证据不等于正面证据，弃权是一等结果**：缺失数据必须 fail-closed 到
       ABSTAIN / UNKNOWN，绝不能因为「没报错」就当成通过。

契约数据的来源与权威顺序
    `viewer/data/tool_contract.v2.json` 是审计产出的目标契约（33 条，机器可读）。
    它描述的是**静态属性**（角色、用途、适用行情、持有期、前置条件、禁用边界）。
    「数据是否过期」这类**运行时状态不写进契约**：契约一旦写死 stale，等数据补齐了
    工具也永远起不来（审计附录里 `kronos_kline` 就写着 evidence_state=stale）。
    所以本模块把两者分开：
        静态证据 ← 契约 与 tool_registry.bt_verdict 归一化
        运行时新鲜度 ← evaluate() 每次按 ctx.data_quality 现算
"""
from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
from pathlib import Path

log = logging.getLogger(__name__)

CONTRACT_VERSION = "tool-contract.v2"
SPEC_PATH = Path(__file__).resolve().parent / "data" / "tool_contract.v2.json"

# ---------------------------------------------------------------- 枚举

ROLES = (
    "hard_guard", "risk_guard", "universe_selector", "context_detector",
    "pattern_signal", "ranking_scorer", "reverse_signal", "explanation_only",
    "retired",
)
DECISION_USES = (
    "exclude", "select", "rank", "watch", "describe", "reverse_exclude",
    "no_decision",
)
EVIDENCE_STATES = (
    "validated_positive", "conditional", "validated_negative", "unvalidated",
    "stale", "retired",
)

# 前端与报告用的中文名。放后端而不是前端：口径只有一份，前端只渲染，
# 免得同一件事在页面和接口里被叫成两个名字（这个仓库已经吃过一次亏）。
ROLE_CN = {
    "hard_guard": "硬闸门",
    "risk_guard": "风险排除",
    "universe_selector": "选择器",
    "context_detector": "上下文检测",
    "pattern_signal": "形态信号",
    "ranking_scorer": "排序器",
    "reverse_signal": "反向信号",
    "explanation_only": "解释性",
    "retired": "已淘汰",
}
DECISION_USE_CN = {
    "exclude": "排除",
    "select": "选股",
    "rank": "排序",
    "watch": "仅观察",
    "describe": "仅描述",
    "reverse_exclude": "反向排除",
    "no_decision": "不参与决策",
}
EVIDENCE_CN = {
    "validated_positive": "已验证正面",
    "conditional": "有条件采纳",
    "validated_negative": "已验证反向",
    "unvalidated": "未验证",
    "stale": "数据过期",
    "retired": "已淘汰",
}

# 市况词表归一化。审计 P0-05 指出的问题：`/api/signal/regime` 给的是
# `down/range/up`，而工具表 `bt_regime` 写的是「下跌/上涨/震荡/全部」——
# 两套词汇没有映射，就会「down 找不到下跌」，路由拼起来必然错。
REGIME_MAP = {
    "上涨": "up", "强势": "up", "多头": "up", "up": "up",
    "震荡": "range", "中性": "range", "盘整": "range", "range": "range",
    "下跌": "down", "弱势": "down", "空头": "down", "down": "down",
    "全部": "all", "所有": "all", "all": "all", "any": "all", "": "all",
}
VERDICT_MAP = {
    "B 否决·反向": "validated_negative",
    "B 否决": "validated_negative",
    "否决·反向": "validated_negative",
    "C 有条件采纳": "conditional",
    "有条件采纳": "conditional",
    "A 采纳": "validated_positive",       # 标签体系是 A/B/C 三档，此前只写了 "D 采纳"，
    "D 采纳": "validated_positive",       # 导致 "A 采纳" 被读成「未验证」—— 保留 D 兼容旧数据
    "采纳": "validated_positive",
    "": "unvalidated",
}

# 能做「正向决策产出」的角色。反向信号与解释性工具永远不在这个集合里 ——
# 这是「不把反向工具当买入依据」的唯一实现点，不要在前端另抄一份判断。
POSITIVE_ROLES = {
    "hard_guard", "risk_guard", "universe_selector", "context_detector",
    "pattern_signal", "ranking_scorer",
}

DDL = """
CREATE TABLE IF NOT EXISTS tool_contract (
    tool_key             TEXT PRIMARY KEY,
    contract_version     TEXT NOT NULL,
    role                 TEXT NOT NULL,
    decision_use_json    TEXT NOT NULL,
    horizons_json        TEXT NOT NULL,
    regimes_json         TEXT NOT NULL,
    precondition_json    TEXT NOT NULL,
    blocked_when_json    TEXT NOT NULL,
    missing_data_policy  TEXT NOT NULL,
    target_params_json   TEXT NOT NULL DEFAULT '{}',
    evidence_state       TEXT NOT NULL,
    evidence_note        TEXT NOT NULL DEFAULT '',
    valid_from           TEXT NOT NULL DEFAULT '',
    retired_at           TEXT NOT NULL DEFAULT '',
    spec_hash            TEXT NOT NULL DEFAULT ''
);
"""


# ---------------------------------------------------------------- 载入

def load_spec() -> dict:
    """读契约 JSON。读不到就返回空表并**大声记日志**，不静默降级成"所有工具都合规"。"""
    try:
        return json.loads(SPEC_PATH.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        log.exception("工具契约文件读取失败：%s", SPEC_PATH)
        return {"tools": {}}


def spec_hash() -> str:
    """契约正文指纹。落进每条记录，用于回答「这次判断是按哪版契约做的」。"""
    try:
        return hashlib.sha256(SPEC_PATH.read_bytes()).hexdigest()
    except Exception:  # noqa: BLE001
        return ""


def normalize_regime(raw) -> str:
    s = str(raw or "").strip()
    return REGIME_MAP.get(s, REGIME_MAP.get(s.lower(), "all"))


def normalize_verdict(raw) -> str:
    s = str(raw or "").strip()
    # 英文枚举直通：契约 JSON 的 `evidence_state` 本来就写英文，
    # 若有人把枚举名直接写进 `bt_verdict`（回测落地脚本早期就是这个行为），
    # 这里认得出来才不会变成「写进去了但读不出来」的假报工。
    if s in EVIDENCE_STATES:
        return s
    if s in VERDICT_MAP:
        return VERDICT_MAP[s]
    low = s.lower()
    for k, v in VERDICT_MAP.items():
        if k and low == k.lower():
            return v
    return "unvalidated"


# ---------------------------------------------------------------- 建表与同步

def ensure_table(con: sqlite3.Connection) -> None:
    con.executescript(DDL)
    con.commit()


def sync(con: sqlite3.Connection) -> dict:
    """把契约 JSON 灌进表里（幂等）。返回统计，供启动日志与 /api/router/context 自证。

    为什么每次启动都覆盖：契约是代码资产，不是用户数据。用户改的是 `tool_registry`
    的 status（要不要留在货架），不该被这里覆盖；但「角色/用途/禁用边界」必须与代码
    同版本，否则代码升级了契约还是旧的，判定会按上一版规则跑。
    """
    ensure_table(con)
    spec = load_spec()
    tools = spec.get("tools") or {}
    h = spec_hash()
    n = 0
    for key, t in tools.items():
        con.execute(
            "INSERT INTO tool_contract(tool_key,contract_version,role,decision_use_json,"
            "horizons_json,regimes_json,precondition_json,blocked_when_json,"
            "missing_data_policy,target_params_json,evidence_state,evidence_note,"
            "valid_from,retired_at,spec_hash) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(tool_key) DO UPDATE SET "
            "contract_version=excluded.contract_version, role=excluded.role, "
            "decision_use_json=excluded.decision_use_json, "
            "horizons_json=excluded.horizons_json, regimes_json=excluded.regimes_json, "
            "precondition_json=excluded.precondition_json, "
            "blocked_when_json=excluded.blocked_when_json, "
            "missing_data_policy=excluded.missing_data_policy, "
            "target_params_json=excluded.target_params_json, "
            "evidence_state=excluded.evidence_state, evidence_note=excluded.evidence_note, "
            "spec_hash=excluded.spec_hash",
            (
                key, CONTRACT_VERSION, str(t.get("role") or ""),
                json.dumps(t.get("decision_use") or [], ensure_ascii=False),
                json.dumps(t.get("horizons") or [], ensure_ascii=False),
                json.dumps(t.get("market_regimes") or [], ensure_ascii=False),
                json.dumps(t.get("preconditions") or [], ensure_ascii=False),
                json.dumps(t.get("blocked_when") or [], ensure_ascii=False),
                str(t.get("missing_data_policy") or ""),
                json.dumps(t.get("target_default_params") or {}, ensure_ascii=False),
                str(t.get("evidence_state") or "unvalidated"),
                str(t.get("evidence_note") or ""),
                "2026-11-06", "", h,
            ),
        )
        n += 1
    con.commit()
    return {"contract_version": CONTRACT_VERSION, "loaded": n, "spec_hash": h[:16]}


# ---------------------------------------------------------------- 读取

_COLS = ("tool_key,contract_version,role,decision_use_json,horizons_json,regimes_json,"
         "precondition_json,blocked_when_json,missing_data_policy,target_params_json,"
         "evidence_state,evidence_note,valid_from,retired_at,spec_hash")


def _dec(row) -> dict:
    d = dict(zip(_COLS.split(","), row))
    for k, src in (("decision_use", "decision_use_json"),
                   ("horizons", "horizons_json"),
                   ("regimes", "regimes_json"),
                   ("preconditions", "precondition_json"),
                   ("blocked_when", "blocked_when_json"),
                   ("target_params", "target_params_json")):
        try:
            d[k] = json.loads(d.get(src) or "[]")
        except Exception:  # noqa: BLE001
            d[k] = {} if k == "target_params" else []
        d.pop(src, None)
    d["role_cn"] = ROLE_CN.get(d.get("role") or "", d.get("role") or "")
    d["decision_use_cn"] = [DECISION_USE_CN.get(x, x) for x in (d.get("decision_use") or [])]
    d["evidence_cn"] = EVIDENCE_CN.get(d.get("evidence_state") or "", "")
    return d


def get(con: sqlite3.Connection, tool_key: str) -> dict | None:
    try:
        r = con.execute("SELECT " + _COLS + " FROM tool_contract WHERE tool_key=?",
                        (tool_key,)).fetchone()
    except sqlite3.OperationalError:
        return None
    return _dec(r) if r else None


def list_all(con: sqlite3.Connection) -> dict[str, dict]:
    try:
        rows = con.execute("SELECT " + _COLS + " FROM tool_contract").fetchall()
    except sqlite3.OperationalError:
        return {}
    return {r[0]: _dec(r) for r in rows}


# ---------------------------------------------------------------- 证据与新鲜度

def static_evidence(contract: dict | None, reg_row: dict | None) -> tuple[str, str]:
    """静态证据状态：优先取 `tool_registry.bt_verdict`（最新实测），契约兜底。

    `bt_verdict` 是回测机跑出来的既成结论，比契约文件里的快照新；契约里的
    `evidence_state` 只在 registry 没有裁决时使用。

    特例：契约写 `stale` 的一律不采信（原因见模块头）。`stale` 是运行时状态，
    被写进静态字段就等于给这个工具判了无期 —— 数据补上了它也永远起不来。
    """
    verdict = normalize_verdict((reg_row or {}).get("bt_verdict"))
    if verdict != "unvalidated":
        return verdict, "来自 tool_registry.bt_verdict"
    ev = str((contract or {}).get("evidence_state") or "unvalidated")
    if ev == "stale":
        return "unvalidated", "契约标注 stale，但运行时新鲜度需现算，此处不采信"
    return ev, str((contract or {}).get("evidence_note") or "")


def stale_reason(tool_key: str, ctx: dict) -> str:
    """运行时新鲜度。只对**有日期语义**的工具判，返回空串表示新鲜。

    判据来自审计 §4.2.A 的硬规则：
      日频行情/指标/快照滞后 > 1 个交易日 → 日频工具禁用
      季频股东数据 > 120 天 → 机构类工具禁用
      模型输出日期 ≠ 最新交易日 → 模型类工具禁用
    """
    q = (ctx or {}).get("data_quality") or {}
    trade_date = str((ctx or {}).get("trade_date") or "")

    def _age_days(d) -> int | None:
        d = str(d or "")[:10]
        if not d or not trade_date:
            return None
        try:
            from datetime import date
            y1, m1, d1 = (int(x) for x in trade_date[:10].split("-"))
            y2, m2, d2 = (int(x) for x in d.split("-"))
            return (date(y1, m1, d1) - date(y2, m2, d2)).days
        except Exception:  # noqa: BLE001
            return None

    # 模型类：输出日期必须等于最新交易日（审计：kronos 停在 10-25，日线已到 11-06）
    if tool_key == "kronos_kline":
        d = q.get("kronos_date") or ""
        if not d:
            return "Kronos 无输出记录，模型类工具不可用"
        age = _age_days(d)
        if age is None or age > 1:
            return "Kronos 输出停在 %s，比最新交易日 %s 落后，禁用" % (d, trade_date)
        return ""

    # 季频：股东名册
    if tool_key == "holder_org":
        d = q.get("holders_date") or ""
        age = _age_days(d)
        if age is not None and age > 120:
            return "股东名册截止 %s，已超 120 天，机构类工具禁用" % d
        return ""

    # 季频/低频：板块成员（行业中性、产业链相关）
    if tool_key in ("reso_ab", "avoid_c"):
        d = q.get("res_card_date") or ""
        if not d:
            return "共振卡无批次数据"
        age = _age_days(d)
        if age is not None and age > 3:
            return "共振卡批次停在 %s，已过期" % d
        return ""

    return ""


def data_quality(tool_key: str, ctx: dict, contract: dict | None) -> str:
    """缺数据策略：abstain / unknown / not_applicable 三档，绝不返回「通过」。"""
    return str((contract or {}).get("missing_data_policy") or "abstain")


# ---------------------------------------------------------------- 资格判定

# 契约 JSON 的 evidence_note 是英文的审计原文，直接抛到页面上会中英混排。
# 这里有实测数字的工具走中文模板（见 evaluate），没有数字的少数几条在这里补中文 ——
# 补的是**结论**，不是翻译：用户要看的是「这个形态到底有没有用」，不是原作者的措辞。
NOTE_CN = {
    "box_bottom": ("14.8 年条件化回测（约 103 万条箱底样本）：T+20 相对同池 -0.036pp（t=-0.14）、"
                   "T+60 -0.409pp（t=-0.58）、T+120 -0.594pp（t=-0.88）—— "
                   "箱底没有稳定的方向性超额，只输出「值得研究」，不构成买入依据"),
    "pick_quality": "长持有期工具：需要 10 年以上样本与工具级结果追踪后才能用于可执行决策",
    "holder_org": "机构持仓是季度数据，不能当日频信号用",
    "gate_admit": "硬否决覆盖范围有限，且缺数据当前会静默放行 —— 对硬闸门来说这是不安全的方向",
    "avoid_c": "默认模式是标注，不是硬排除",
    "pillar_dip": "当前前瞻样本偏弱，保留为诊断用途，等重测",
    "pillar_self": "当前前瞻样本偏弱，需要 challenger 验证",
}


def _has_cn(s: str) -> bool:
    return any("\u4e00" <= ch <= "\u9fff" for ch in s or "")


def _evidence_text(tool_key: str, contract: dict | None, role: str,
                   uses: list, reg: dict) -> str:
    """把证据状态说成一句人话。三种来源按可靠度排序：实测数字 > 中文结论 > 兜底。"""
    edge = reg.get("bt_edge")
    if edge is not None:
        if role in ("hard_guard", "risk_guard") or "reverse_exclude" in uses:
            # 排除器被标成「反向」是褒义：它当选股器是负的，正因为它该反着用
            return ("回测显示它是风险排除项（%s %s 日超额 %spp）：只减不加，"
                    "不得当作正向选股器使用"
                    % (reg.get("bt_regime") or "全部",
                       reg.get("bt_hold") or "-", edge))
        return ("已有反向回测结论（%s %s 日超额 %spp），不能作为正向选择器"
                % (reg.get("bt_regime") or "回测", reg.get("bt_hold") or "-", edge))
    note = NOTE_CN.get(tool_key) or str((contract or {}).get("evidence_note") or "")
    if note and _has_cn(note):
        return "研究结论：%s" % note
    return "该形态没有可用的正面证据（未验证或已被证伪）"


def evaluate(tool_key: str, contract: dict | None, reg_row: dict | None,
             ctx: dict) -> dict:
    """判定一个工具在给定上下文下的资格。**这是唯一判定入口，不要在别处重写。**

    返回：
        state        → select / rank / guard / watch / describe / blocked
        decision     → actionable / watch / no_decision / blocked
        reasons      → 被拒原因（人能读懂的中文，直接可展示）
        role / evidence / decision_use 等原样带出，供前端分组与解释

    判定顺序严格按审计 §4.2.C，**先硬后软**：任一条硬规则命中即判 blocked，
    不再看后面的条件 —— 否则「行情不符」会被「前置条件满足」冲淡成可用。
    """
    reg = reg_row or {}
    ct = contract or {}
    role = str(ct.get("role") or "")
    uses = list(ct.get("decision_use") or [])
    horizons = list(ct.get("horizons") or [])
    regimes = list(ct.get("regimes") or [])
    evidence, ev_note = static_evidence(ct, reg)
    reasons: list[str] = []
    hard = False
    # 被拒不等于「工具坏了」。分四类是为了让页面能分开摆放，也让用户知道该怎么处理：
    #   hard     → 别用了（淘汰 / 数据过期 / 证据反向 / 权限不够）
    #   regime   → 今天这个行情不该开（明天可能就好了）
    #   horizon  → 它不服务你这个持有期（换个周期就能用，工具本身没问题）
    #   unknown  → 没契约，不知道怎么用（要补配置，不是点一下就好）
    blocked_kind = ""

    regime = normalize_regime(ctx.get("market_regime"))
    horizon = ctx.get("horizon_days")

    # ① 没有契约 = 不知道怎么用 = 不许决策（fail-closed）
    if not ct:
        return {
            "tool_key": tool_key, "state": "blocked", "decision": "blocked",
            "blocked_kind": "unknown",
            "role": "", "role_cn": "未登记契约",
            "evidence": "unvalidated", "evidence_cn": "未验证",
            "decision_use": [], "decision_use_cn": [],
            "reasons": ["没有资格契约：未定义角色与适用条件，不得参与决策"],
            "registry_status": reg.get("status"), "advisory": int(reg.get("advisory") or 0),
        }

    # ② 已淘汰
    if role == "retired" or str(reg.get("status") or "") == "retired" or evidence == "retired":
        hard = True
        blocked_kind = "retired"
        reasons.append("已淘汰：%s" % (str(reg.get("retire_reason") or "")
                                    or str(ct.get("evidence_note") or "")[:80]))

    # ③ 数据过期（运行时）
    if not hard:
        st = stale_reason(tool_key, ctx)
        if st:
            hard = True
            blocked_kind = "stale"
            reasons.append(st)

    # ④ 已验证反向 + **正向用途** = 禁止（审计点名的三个「生命周期矛盾」在这里被挡住）
    #    注意 `positive_use` 这半个条件不能省：`box_bottom` 的契约也是
    #    validated_negative，但它的用途是 watch（箱底没有方向性 alpha，值得看≠能买）。
    #    省掉这个条件会把「箱底观察」和「反向信号当买入」当成同一件事一起禁掉 ——
    #    那就把审计 §4.5 想要的「输出观察」也一起杀了。
    #
    #    反向工具（pick_ma_bull 等）的 decision_use 是 no_decision，positive_use=False，
    #    所以不会被上面那条硬规则拦 —— 但它们仍然**不能**成为买入依据。两条路一起用：
    #    硬拦「拿去当选择器」的调用，原因写清证据；对只跑一遍看名单的调用，
    #    由 ⑧/else 判成 no_decision，同时把「为什么不该当买入」的原因挂出来，
    #    免得用户看到一句空的「没有可用用途」而不知道是回测已经否掉了它。
    positive_use = any(u in ("select", "rank") for u in uses)
    evidence_reason = ""
    if evidence == "validated_negative":
        evidence_reason = _evidence_text(tool_key, ct, role, uses, reg)
    if not hard and evidence_reason:
        reasons.append(evidence_reason)
        # 反向信号工具无条件判死：它的用途本来就写着 no_decision，
        # 继续往下走只会多出一条「周期错配」把真正的死因盖住。
        if positive_use or role == "reverse_signal":
            hard = True
            blocked_kind = "evidence"

    # ⑤/⑥ 行情与持有期：**排除器不受这两条限制**。
    #    「这只票有退市风险 / 波动极端」与「你打算拿几天」无关，拿得越久越该排除；
    #    拿持有期去卡硬风险防线，等于允许一只退市票在 120 日视角下混进候选池。
    #    审计 §4.2.D 把硬风险否决层的优先级放在选股得分之上，这里按那个顺序落。
    is_guard = (role in ("hard_guard", "risk_guard")
                or "exclude" in uses or "reverse_exclude" in uses)

    # ⑤ 行情不符
    if not hard and not is_guard and regimes and "all" not in regimes and regime not in regimes:
        hard = True
        blocked_kind = "regime"
        reasons.append("当前市况 %s 不在适用行情 %s 内"
                       % (regime, "/".join(regimes)))

    # ⑥ 持有期不符
    if not hard and not is_guard and horizons and "all" not in horizons \
            and horizon not in (None, ""):
        try:
            if int(horizon) not in [int(x) for x in horizons if str(x).isdigit()]:
                hard = True
                blocked_kind = "horizon"
                reasons.append("该工具只服务 %s 日持有期，当前目标 %s 日 —— 工具本身没问题，是周期错配"
                               % ("/".join(str(x) for x in horizons), horizon))
        except (TypeError, ValueError):
            pass

    # ⑦ shadow / advisory 的决策权限（注意：只限制"正向产出"，不限制"排除"）
    if not hard:
        if positive_use and str(reg.get("status") or "") == "shadow":
            hard = True
            blocked_kind = "authority"
            reasons.append("影子工具不得产出可执行名单（需样本外验证后才能上线）")
        elif positive_use and int(reg.get("advisory") or 0) == 1:
            hard = True
            blocked_kind = "authority"
            reasons.append("说明性标注工具不参与进出决策，只可观察形态")

    # ⑧ 角色本身不允许正向产出（反向信号 / 解释性）
    if not hard and role in ("reverse_signal", "explanation_only") and positive_use:
        hard = True
        blocked_kind = "authority"
        reasons.append("%s 不得作为正向选择器" % ROLE_CN.get(role, role))

    # ⑨ 箱体：位置前置条件（审计 §8.4 的 2/3/4 条验收在这里落地）
    #    「箱体池」与「箱底触发」必须分开：进了池不等于到了底。
    #    600519 不在池里 → 不触发；000731 位置 35% → 不是底；000008 位置 22% → 只观察。
    box_only_watch = ""
    if tool_key == "box_bottom" and not hard:
        code = str(ctx.get("stock_code") or "")
        box = ctx.get("box") or {}
        if code:
            pos = (box.get("positions") or {}).get(code)
            if not box.get("available"):
                hard = True
                blocked_kind = "stale"
                reasons.append("箱体数据缺失，无法判定是否在箱底")
            elif pos is None:
                hard = True
                blocked_kind = "regime"
                reasons.append("不在当前有效箱底名单（形态未过门槛或不在 750 日低位）")
            elif float(pos) > 33.3:
                hard = True
                blocked_kind = "regime"
                reasons.append("箱体位置 %.0f%% > 1/3，不是箱底" % float(pos))
            else:
                box_only_watch = "箱底条件成立（位置 %.0f%%），值得观察／研究，不构成买入依据" % float(pos)
        else:
            # 批量模式：输出的是一份"值得看"的池子，不是买入名单
            box_only_watch = "箱体工具在批量模式下只输出观察池，不构成买入依据"

    # ---- 定状态 ----
    if hard:
        state, decision = "blocked", "blocked"
    elif box_only_watch:
        # 箱底是"条件成立，值得研究"，不是"可以买"。这是审计 §4.5 的最终行为，
        # 也是那张 14.8 年条件化回测（T+20 -0.036pp，t=-0.14）唯一支持的结论。
        state, decision = "watch", "watch"
        reasons.append(box_only_watch)
    elif role == "hard_guard" or role == "risk_guard" or "reverse_exclude" in uses:
        state, decision = "guard", "actionable"      # 排除器是"可用"的，但它只减不加
    elif "select" in uses:
        state, decision = "select", "actionable"
    elif "rank" in uses:
        state, decision = "rank", "actionable"
    elif "watch" in uses:
        state, decision = "watch", "watch"
    elif "describe" in uses:
        state, decision = "describe", "watch"
    else:
        state, decision = "blocked", "no_decision"
        reasons.append("没有可用于当前上下文的决策用途")

    return {
        "tool_key": tool_key,
        "state": state,
        "decision": decision,
        "blocked_kind": blocked_kind,
        "role": role,
        "role_cn": ROLE_CN.get(role, role),
        "evidence": evidence,
        "evidence_cn": EVIDENCE_CN.get(evidence, evidence),
        "evidence_note": ev_note,
        "decision_use": uses,
        "decision_use_cn": [DECISION_USE_CN.get(u, u) for u in uses],
        "reasons": reasons,
        "registry_status": str(reg.get("status") or ""),
        "advisory": int(reg.get("advisory") or 0),
    }


# ---------------------------------------------------------------- 三轴分离（审计 T02）

# 审计 §5.2 的核心要求：不要把 `status=retired` 当成唯一手段。
# 一个工具身上其实有三个正交的问题，混在一个 status 字段里就必然要做取舍：
#   lifecycle      → 还留不留在货架上（用户要能看见、能点开、能看到淘汰原因）
#   decision_mode  → 允不允许参与决策（这个才是"能不能当买入依据"）
#   evidence_state → 有没有正面/反向/无证据（这个回答"凭什么"）
# 混在一起的后果审计已经写出来了：`pick_ma_bull` 有明确的 -3.683pp 反向结论，
# 但它 status=active，于是系统仍然让它在下跌行情里当正向筛选器跑。
LIFECYCLE_CN = {"kept_on_shelf": "在架上", "retired": "已下架"}
DECISION_MODE_CN = {
    "normal": "可决策",
    "watch_only": "仅观察",
    "shadow_only": "仅影子（不产出可执行）",
    "advisory_only": "仅说明（不参与进出）",
    "explain_only": "仅解释",
    "disabled_or_reverse_only": "已停用（只可反向/排除用）",
    "disabled": "已停用",
}


def lifecycle_of(reg_row: dict | None, contract: dict | None) -> dict:
    """算出三轴状态。纯函数，不查库 —— 便于测试与前端复现同一套口径。

    两个"能不能"必须分开，混成一个布尔值就会出现审计里那种自相矛盾：
        can_decide → 能不能参与决策（排除器也算，因为它确实在替你做减法）
        can_buy    → 能不能产出"可以买"的名单（只有正常的 select/rank 才行）

    `oss_high_vol` 就是必须区分的那一类：它是 shadow + advisory，按字面两条规则
    都该禁；但契约给它的用途是 `reverse_exclude`，审计 §5.1 也明确写着
    "风险排除，允许输出排除/风险标记"。所以它 can_decide=True、can_buy=False ——
    能用来剔票，永远不能用来选票。
    """
    reg = reg_row or {}
    ct = contract or {}
    status = str(reg.get("status") or "")
    advisory = int(reg.get("advisory") or 0)
    role = str(ct.get("role") or "")
    uses = list(ct.get("decision_use") or [])
    positive_use = any(u in ("select", "rank") for u in uses)
    is_guard = (role in ("hard_guard", "risk_guard")
                or "exclude" in uses or "reverse_exclude" in uses)

    lifecycle = "retired" if status == "retired" else "kept_on_shelf"

    if status == "retired" or role == "retired":
        mode = "disabled"
    elif role == "reverse_signal":
        mode = "disabled_or_reverse_only"
    elif role == "explanation_only":
        mode = "explain_only"
    elif status == "shadow" and not is_guard:
        mode = "shadow_only"
    elif advisory == 1 and not is_guard:
        mode = "advisory_only"
    elif is_guard or positive_use:
        mode = "normal"
    elif "watch" in uses or "describe" in uses:
        # 只观察的工具（箱底、波浪位置、机构持仓、质量龙头…）不是"禁用"，
        # 它确实在提供判断材料，只是这份材料不构成买入依据。
        mode = "watch_only"
    else:
        mode = "disabled"

    can_decide = mode in ("normal", "watch_only")
    can_buy = mode == "normal" and positive_use
    can_screen = lifecycle != "retired"
    return {
        "lifecycle": lifecycle,
        "lifecycle_cn": LIFECYCLE_CN.get(lifecycle, lifecycle),
        "decision_mode": mode,
        "decision_mode_cn": DECISION_MODE_CN.get(mode, mode),
        "can_decide": bool(can_decide),
        "can_buy": bool(can_buy),
        "can_screen": bool(can_screen),
    }


def allowed_as_screen(tool_key: str, contract: dict | None, reg_row: dict | None) -> tuple[bool, str]:
    """能不能被 `apply_tool()` 当筛选器执行（静态判定，不含行情/持有期）。

    与 `evaluate()` 的分工：`evaluate()` 回答「这次决策能不能用它」，
    这个函数回答「点下去跑一遍这个动作本身合不合法」。

    为什么反向/解释性工具仍然允许跑：用户确实要能看见「按这条规则能筛出谁」，
    只是那份名单不能当买入依据。真正禁止执行的是已淘汰、以及契约缺失的工具 ——
    这两类跑出来的数字没有任何解释力，只会被误读。
    """
    if contract is None:
        return False, "没有资格契约：未定义角色与适用条件，不得执行"
    role = str(contract.get("role") or "")
    if role == "retired" or str((reg_row or {}).get("status") or "") == "retired":
        return False, "该工具已淘汰，不能参与筛选"
    return True, ""


def decision_label(ev: str) -> str:
    return EVIDENCE_CN.get(ev, ev)


# ---------------------------------------------------------------- 运行时参数默认值

def effective_defaults(registry: dict, contract: dict | None) -> dict:
    """默认参数的权威顺序：契约 `target_default_params` > 登记表 `default_params`。

    用途是把审计发现的「名称与行为不一致」按契约纠正，
    典型就是 `box_bottom.pos_max: 1.0 → 0.333`（名字叫箱体谷底，却允许 100% 位置）。
    只在**契约显式给出**该键时覆盖，没给的键仍走登记表 —— 契约是补丁不是全量替换。
    """
    base = dict((registry or {}).get("default_params") or {})
    tgt = (contract or {}).get("target_params") or {}
    if isinstance(tgt, dict):
        for k, v in tgt.items():
            base[k] = v
    return base
