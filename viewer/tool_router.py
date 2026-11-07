"""受约束策略路由：先判资格，再决定用哪把工具，最后允许"什么都不做"。

解决的问题（2026-11-06 审计 P0-03 / P0-05）
    改之前页面的"建议下一刀"只在算一件事：**哪个工具能把池子砍到最接近目标数量**。
    它不看行情、不看持有期、不看工具角色、不看证据状态 —— 于是会出现
    「均线多头（下跌行情 −3.683pp）+ 超跌反弹 → 0 只」这种自相矛盾的链，
    而接口照样返回 ok=true。那不是策略，那是数量规划。

六层顺序（审计 §4.2，顺序本身是设计的一部分，不要重排）
    数据质量 → 市场状态 → 工具资格 → 硬风险否决 → 候选与排序 → 组合/解释/弃权

三条不可让渡的结果语义
    1. `ABSTAIN` 是**一等结果**，不是失败。没有合格工具时必须说"今天不做"，
       而不是从反向工具里硬凑一份名单把页面填满。
    2. 只有 `select` 角色能开仓候选；`rank` 只能给相对顺序；`guard` 只做减法。
       排序器单独出现在链里等于"用相对名次冒充买入理由"。
    3. 每个被拒的工具都必须带一句人话原因 —— 用户要能回答"为什么不用它"，
       否则下次他还会自己去点那个已经被证伪的工具。
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import sqlite3
import time
import uuid
from datetime import datetime
from pathlib import Path

log = logging.getLogger(__name__)

ROUTER_VERSION = "router-v1.0"

DDL = """
CREATE TABLE IF NOT EXISTS tool_decision_log (
    decision_id        TEXT PRIMARY KEY,
    as_of              TEXT NOT NULL,
    context_hash       TEXT NOT NULL,
    market_regime      TEXT NOT NULL DEFAULT '',
    objective          TEXT NOT NULL DEFAULT '',
    horizon_days       INTEGER NOT NULL DEFAULT 0,
    stock_code         TEXT NOT NULL DEFAULT '',
    router_version     TEXT NOT NULL DEFAULT '',
    contract_version   TEXT NOT NULL DEFAULT '',
    chain_json         TEXT NOT NULL DEFAULT '[]',
    candidate_ids_json TEXT NOT NULL DEFAULT '[]',
    rejected_json      TEXT NOT NULL DEFAULT '[]',
    eligible_json      TEXT NOT NULL DEFAULT '[]',
    final_count        INTEGER NOT NULL DEFAULT 0,
    status             TEXT NOT NULL DEFAULT '',
    abstain_reason     TEXT NOT NULL DEFAULT '',
    created_at         TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_tdl_asof ON tool_decision_log(as_of DESC, created_at DESC);

-- 到期打分（审计 T08）。表先建好，填充靠后续的 outcome 任务；
-- 没有这张表，"自迭代"就只能改参数，学不到"哪种行情该开哪条链"。
CREATE TABLE IF NOT EXISTS tool_outcome (
    decision_id     TEXT NOT NULL,
    code            TEXT NOT NULL,
    horizon_days    INTEGER NOT NULL,
    due_date        TEXT NOT NULL DEFAULT '',
    realized_date   TEXT NOT NULL DEFAULT '',
    gross_return    REAL,
    benchmark_return REAL,
    net_excess_return REAL,
    max_drawdown    REAL,
    hit             INTEGER,
    status          TEXT NOT NULL DEFAULT 'pending',
    scored_at       TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (decision_id, code, horizon_days)
);
"""


# ---------------------------------------------------------------- 基础取数

def _max_date(con: sqlite3.Connection, table: str, col: str) -> str:
    try:
        r = con.execute("SELECT MAX(%s) FROM %s" % (col, table)).fetchone()
        return str(r[0])[:10] if r and r[0] else ""
    except sqlite3.Error:
        return ""


def _latest_trade_date(con: sqlite3.Connection) -> str:
    """最新交易日以行情表为准，不信系统时间。

    为什么不用 datetime.now()：周末/节假日打开页面时，"今天"不是交易日，
    拿它当 as_of 会让所有数据看起来都"过期 1 天"，进而把全部工具误判成 stale。
    """
    for table, col in (("stock_daily_snapshot", "trade_date"),
                       ("stock_indicators", "date"),
                       ("stock_indicators_daily", "date")):
        d = _max_date(con, table, col)
        if d:
            return d
    return ""


def _box_context() -> dict:
    """读最近一次箱体计算的核心结果，解出「谁在箱底、位置多少」。

    为什么不重算：路由要毫秒级返回，箱体扫描是全市场 5000+ 只 × 750 日，
    重算一次几十秒。这里读的是筛台自己落盘的缓存（同一个事实源），
    缓存过期由 box_date 与 trade_date 的差值暴露给调用方，而不是偷偷用旧数据。
    """
    try:
        from viewer import toolbox_stack as TS
        cache_dir = Path(TS.CACHE_DIR)
    except Exception:  # noqa: BLE001
        return {"available": False, "trade_date": "", "positions": {}, "reason": "缓存目录不可用"}
    if not cache_dir.exists():
        return {"available": False, "trade_date": "", "positions": {},
                "reason": "箱体尚未计算过"}
    files = [p for p in cache_dir.glob("box_bottom_*.json")
             if not p.name.endswith(".drop.json")]
    if not files:
        return {"available": False, "trade_date": "", "positions": {},
                "reason": "箱体尚未计算过"}
    latest = max(files, key=lambda p: p.stat().st_mtime)
    try:
        data = json.loads(latest.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {"available": False, "trade_date": "", "positions": {},
                "reason": "箱体缓存不可读"}
    marks = data.get("marks") or {}
    positions: dict[str, float] = {}
    for code, mark in marks.items():
        m = re.search(r"位置\s*(\d+(?:\.\d+)?)%", str(mark))
        if m:
            positions[str(code)] = float(m.group(1))
    return {
        "available": bool(positions),
        "trade_date": str(data.get("trade_date") or "")[:10],
        "positions": positions,
        "count": len(positions),
        "cache_file": latest.name,
        "reason": "" if positions else "箱体缓存里没有可解析的位置",
    }


def build_context(con: sqlite3.Connection, as_of: str = "", horizon_days: int = 20,
                  objective: str = "research", stock_code: str = "") -> dict:
    """装配一次决策的完整上下文。**所有判据都从这里取，别在别处另拼一套。**"""
    from viewer import tool_contract as TC

    try:
        from viewer import regime as RG
        reg = RG.current(con) or {}
    except Exception:  # noqa: BLE001
        log.exception("市况读取失败（按 range 处理并标注）")
        reg = {}

    trade_date = str(reg.get("date") or "")[:10] or _latest_trade_date(con)
    box = _box_context()

    data_quality = {
        "trade_date": trade_date,
        "snapshot_date": _max_date(con, "stock_daily_snapshot", "trade_date"),
        "indicator_date": _max_date(con, "stock_indicators", "date"),
        "kronos_date": _max_date(con, "kronos_item", "run_date"),
        "holders_date": _max_date(con, "stock_holders", "end_date"),
        "res_card_date": _max_date(con, "res_stock_card", "build_date"),
        "box_date": box.get("trade_date") or "",
        "box_count": box.get("count") or 0,
    }
    stale = []
    if data_quality["snapshot_date"] and trade_date and \
            data_quality["snapshot_date"] < trade_date:
        stale.append("日快照停在 %s，落后于交易日 %s"
                     % (data_quality["snapshot_date"], trade_date))
    if data_quality["indicator_date"] and trade_date and \
            data_quality["indicator_date"] < trade_date:
        stale.append("技术指标停在 %s，落后于交易日 %s"
                     % (data_quality["indicator_date"], trade_date))

    ctx = {
        "as_of": as_of or trade_date,
        "trade_date": trade_date,
        "market_regime": TC.normalize_regime(reg.get("regime") or "range"),
        "market_regime_cn": reg.get("regime_cn") or "",
        "regime_ret20": reg.get("ret20"),
        "regime_breadth": reg.get("breadth"),
        "regime_note": reg.get("note") or "",
        "horizon_days": int(horizon_days or 20),
        "objective": objective or "research",
        "stock_code": str(stock_code or ""),
        "data_quality": data_quality,
        "stale": stale,
        "box": box,
    }
    ctx["context_hash"] = hashlib.sha256(
        json.dumps({k: ctx[k] for k in ("trade_date", "market_regime", "horizon_days",
                                        "objective", "stock_code")},
                   ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()[:16]
    return ctx


# ---------------------------------------------------------------- 判定

def judge_all(con: sqlite3.Connection, ctx: dict) -> list[dict]:
    """对 33 个工具逐个判资格。返回顺序与货架一致，便于前端稳定渲染。"""
    from viewer import toolbox as TB
    from viewer import tool_contract as TC

    try:
        tools = TB.list_tools(con)
    except Exception:  # noqa: BLE001
        log.exception("读取货架失败（路由按空货架返回）")
        return []
    cts = TC.list_all(con)
    out = []
    for t in tools:
        key = str(t.get("tool_key") or "")
        d = TC.evaluate(key, cts.get(key), t, ctx)
        d["name"] = t.get("name") or key
        d["kind"] = t.get("kind")
        d["pillar_cn"] = t.get("pillar_cn") or []
        d["can_buy"] = bool(t.get("can_buy"))
        d["decision_mode"] = t.get("decision_mode")
        d["decision_mode_cn"] = t.get("decision_mode_cn")
        out.append(d)
    return out


def plan(con: sqlite3.Connection, as_of: str = "", horizon_days: int = 20,
         objective: str = "research", stock_code: str = "",
         max_candidates: int = 30, risk_level: str = "normal",
         persist: bool = True) -> dict:
    """给出今日的受约束计划。

    返回结构对齐审计 §4.4 的约定：status / selected_chain / eligible / watch /
    blocked / reason 都在，前端只需要渲染，不需要再判断一次"哪个能用"。
    """
    ctx = build_context(con, as_of, horizon_days, objective, stock_code)
    judged = judge_all(con, ctx)

    eligible = [d for d in judged if d["decision"] == "actionable"]
    watch = [d for d in judged if d["decision"] == "watch"]
    blocked = [d for d in judged if d["decision"] in ("blocked", "no_decision")]

    # 只有 select 能开候选。rank 单独出现不算理由 —— 相对名次不能代替"为什么是它"。
    selectors = [d for d in eligible if d["state"] == "select"]
    rankers = [d for d in eligible if d["state"] == "rank"]
    guards = [d for d in eligible if d["state"] == "guard" and d["role"] == "hard_guard"]
    risk_guards = [d for d in eligible if d["state"] == "guard" and d["role"] == "risk_guard"]

    chain: list[dict] = []
    status = "ABSTAIN"
    reason = ""

    if selectors:
        sel = selectors[0]
        chain.append({"tool_key": sel["tool_key"], "name": sel["name"],
                      "role": sel["role"], "role_cn": sel["role_cn"],
                      "step": "select", "note": "生成候选池"})
        # 排序器只在有选择器时追加：单独一个排序器排出来的名单，
        # 和你随便给一份名单让它排，是同一件事 —— 没有"谁被选中"这个动作。
        for rk in rankers[:1]:
            chain.append({"tool_key": rk["tool_key"], "name": rk["name"],
                          "role": rk["role"], "role_cn": rk["role_cn"],
                          "step": "rank", "note": "给出相对顺序，不改变候选集合"})
        for gd in guards[:1]:
            chain.insert(0, {"tool_key": gd["tool_key"], "name": gd["name"],
                             "role": gd["role"], "role_cn": gd["role_cn"],
                             "step": "guard", "note": "先做硬否决，再选股"})

        # 证据门槛（审计 §6.1）：未验证的选择器只能进观察，不能 ACTIONABLE。
        ev = str(sel.get("evidence") or "")
        if ev in ("validated_positive", "conditional"):
            status = "ACTIONABLE"
            reason = "有合格选择器，且证据状态为「%s」" % sel.get("evidence_cn", ev)
        else:
            status = "WATCH"
            reason = ("有可用选择器，但证据未验证（%s）—— 只给观察，不给可执行结论"
                      % sel.get("evidence_cn", ev))
    elif any(d["state"] == "guard" for d in eligible):
        status = "ABSTAIN"
        reason = ("当前只有排除类工具可用，没有任何合格的正向选择器 —— "
                  "能剔票，但说不出该买什么，所以弃权")
    else:
        status = "ABSTAIN"
        reason = ("当前上下文（%s 行情 / %s 日持有期）下没有任何合格的正向工具 —— "
                  "不为填满页面而从反向或说明性工具里硬凑名单"
                  % (ctx["market_regime"], ctx["horizon_days"]))

    result = {
        "ok": True,
        "status": status,
        "reason": reason,
        "context": {
            "as_of": ctx["as_of"],
            "trade_date": ctx["trade_date"],
            "market_regime": ctx["market_regime"],
            "market_regime_cn": ctx["market_regime_cn"],
            "regime_ret20": ctx["regime_ret20"],
            "regime_breadth": ctx["regime_breadth"],
            "horizon_days": ctx["horizon_days"],
            "objective": ctx["objective"],
            "stock_code": ctx["stock_code"],
            "box_date": ctx["box"].get("trade_date"),
            "box_count": ctx["box"].get("count"),
            "stale": ctx["stale"],
        },
        "context_hash": ctx["context_hash"],
        "selected_chain": chain,
        "eligible_tools": [{"tool_key": d["tool_key"], "name": d["name"],
                            "role": d["role"], "role_cn": d["role_cn"],
                            "state": d["state"], "evidence": d["evidence"],
                            "evidence_cn": d["evidence_cn"]} for d in eligible],
        "watch_tools": [{"tool_key": d["tool_key"], "name": d["name"],
                         "role": d["role"], "role_cn": d["role_cn"],
                         "reasons": d["reasons"]} for d in watch],
        "blocked_tools": [{"tool_key": d["tool_key"], "name": d["name"],
                           "role": d["role"], "role_cn": d["role_cn"],
                           "blocked_kind": d["blocked_kind"],
                           "reason": "；".join(d["reasons"])} for d in blocked],
        "counts": {
            "total": len(judged),
            "actionable": len(eligible),
            "watch": len(watch),
            "blocked": len(blocked),
        },
        "box_position_pct": (ctx["box"]["positions"].get(ctx["stock_code"])
                             if ctx["box"].get("positions") else None),
        "risk_level": risk_level,
        "max_candidates": int(max_candidates or 30),
        "router_version": ROUTER_VERSION,
    }
    if persist:
        try:
            result["decision_id"] = save_decision(con, ctx, result)
        except Exception:  # noqa: BLE001
            log.exception("决策留痕失败（不影响本次结果）")
            result["decision_id"] = ""
    return result


# ---------------------------------------------------------------- 留痕

def ensure_tables(con: sqlite3.Connection) -> None:
    con.executescript(DDL)
    con.commit()


def save_decision(con: sqlite3.Connection, ctx: dict, result: dict) -> str:
    """把这次判断冻下来：谁被选中、谁被拒、为什么。

    审计 §3.9 / §6.3 的要求是"决策必须可追溯"。没有这条记录，
    事后只能看到"那天页面显示了 7 只"，永远回答不了"为什么是这 7 只"。
    """
    ensure_tables(con)
    did = "d" + datetime.now().strftime("%Y%m%d%H%M%S") + uuid.uuid4().hex[:6]
    rejected = [{"tool_key": b["tool_key"], "blocked_kind": b["blocked_kind"],
                 "reason": b["reason"]} for b in result.get("blocked_tools") or []]
    con.execute(
        "INSERT INTO tool_decision_log(decision_id,as_of,context_hash,market_regime,"
        "objective,horizon_days,stock_code,router_version,contract_version,chain_json,"
        "candidate_ids_json,rejected_json,eligible_json,final_count,status,"
        "abstain_reason,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (did, str(ctx.get("as_of") or ""), str(ctx.get("context_hash") or ""),
         str(ctx.get("market_regime") or ""), str(ctx.get("objective") or ""),
         int(ctx.get("horizon_days") or 0), str(ctx.get("stock_code") or ""),
         ROUTER_VERSION, "tool-contract.v2",
         json.dumps(result.get("selected_chain") or [], ensure_ascii=False),
         json.dumps([], ensure_ascii=False),
         json.dumps(rejected, ensure_ascii=False),
         json.dumps([e["tool_key"] for e in result.get("eligible_tools") or []],
                    ensure_ascii=False),
         int(result.get("counts", {}).get("actionable") or 0),
         str(result.get("status") or ""), str(result.get("reason") or ""),
         time.strftime("%Y-%m-%d %H:%M:%S")))
    con.commit()
    return did


def explain(con: sqlite3.Connection, decision_id: str) -> dict:
    """按 decision_id 取回一次判断的完整依据（审计 §4.4 的 /api/router/explain）。"""
    ensure_tables(con)
    r = con.execute(
        "SELECT decision_id,as_of,context_hash,market_regime,objective,horizon_days,"
        "stock_code,router_version,contract_version,chain_json,candidate_ids_json,"
        "rejected_json,eligible_json,final_count,status,abstain_reason,created_at "
        "FROM tool_decision_log WHERE decision_id=?", (decision_id,)).fetchone()
    if not r:
        return {"ok": False, "error": "没有这条决策记录：%s" % decision_id}
    keys = ("decision_id", "as_of", "context_hash", "market_regime", "objective",
            "horizon_days", "stock_code", "router_version", "contract_version",
            "chain_json", "candidate_ids_json", "rejected_json", "eligible_json",
            "final_count", "status", "abstain_reason", "created_at")
    d = dict(zip(keys, r))
    for k in ("chain_json", "candidate_ids_json", "rejected_json", "eligible_json"):
        try:
            d[k.replace("_json", "")] = json.loads(d.pop(k) or "[]")
        except Exception:  # noqa: BLE001
            d[k.replace("_json", "")] = []
    return {"ok": True, "decision": d}


def recent_decisions(con: sqlite3.Connection, limit: int = 20) -> list[dict]:
    ensure_tables(con)
    try:
        rows = con.execute(
            "SELECT decision_id,as_of,market_regime,horizon_days,status,final_count,"
            "abstain_reason,created_at FROM tool_decision_log "
            "ORDER BY created_at DESC LIMIT ?", (int(limit),)).fetchall()
    except sqlite3.Error:
        return []
    return [{"decision_id": r[0], "as_of": r[1], "market_regime": r[2],
             "horizon_days": r[3], "status": r[4], "final_count": r[5],
             "abstain_reason": r[6], "created_at": r[7]} for r in rows]


def policy_status() -> dict:
    """当前路由策略的自述：版本、契约版本、是否允许在线改权重。

    审计 §3.8 要求"线上不做在线自由改权重，只执行冻结、版本化、可回滚的 policy"。
    这里把那条纪律显式返回，前端与脚本都能读到，不用去翻文档。
    """
    from viewer import tool_contract as TC
    return {
        "ok": True,
        "router_version": ROUTER_VERSION,
        "contract_version": TC.CONTRACT_VERSION,
        "contract_spec_hash": TC.spec_hash()[:16],
        "online_weight_tuning": False,
        "policy_frozen": True,
        "abstain_is_valid": True,
        "rules": [
            "资格先于计算：不满足前置条件不调用工具",
            "角色先于名称：以 role / decision_use 为准",
            "shadow / advisory 不产出 ACTIONABLE",
            "反向信号不得当正向选择器",
            "无合格工具时输出 ABSTAIN，不从反向工具硬凑名单",
            "线上不改权重，只执行版本化 policy",
        ],
    }


def screen_context(con: sqlite3.Connection) -> dict:
    """筛道（`toolbox_stack.run_stack`）专用的轻量上下文。

    与 `build_context()` 的差别只有一处：**`horizon_days = None`**。
    原因是筛道这个动作没有「我打算拿几天」这个输入 —— 它就是「按这条规则筛一遍」。
    拿一个编出来的 20 日去卡持有期，会把「箱体谷底」这类 60/120 日工具在筛道上误杀，
    而那正是用户点「筛」时想做的是「看看筛出谁」，不是「今天买不买」。

    所以这里只带**行情**，让 ⑤（行情不符）/ ⑦（权限）/ ⑧（反向不得正向用）三条生效，
    ⑥（持有期）因为 `horizon` 为空自动跳过 —— 这不是放水，是把判定放在有信息的地方：
    持有期该在 `/api/router/plan` 判，那里才有用户选的周期。
    """
    from viewer import tool_contract as TC

    try:
        from viewer import regime as RG
        reg = RG.current(con) or {}
    except Exception:  # noqa: BLE001
        log.exception("市况读取失败（筛道按 range 兜底）")
        reg = {}
    try:
        box = _box_context()
    except Exception:  # noqa: BLE001
        log.exception("箱体上下文读取失败（筛道按无箱体处理）")
        box = {"available": False, "positions": {}, "count": 0}
    return {
        "as_of": str(reg.get("date") or "")[:10] or _latest_trade_date(con),
        "trade_date": str(reg.get("date") or "")[:10] or _latest_trade_date(con),
        "market_regime": TC.normalize_regime(reg.get("regime") or "range"),
        "market_regime_cn": reg.get("regime_cn") or "",
        "regime_ret20": reg.get("ret20"),
        "regime_breadth": reg.get("breadth"),
        "horizon_days": None,          # ★ 筛道没有持有期语义，见上
        "objective": "screen",
        "stock_code": "",
        "box": box,
    }


def screen_verdicts(con: sqlite3.Connection, keys) -> dict:
    """批量判一条筛道里每个工具的资格。返回 tool_key → evaluate() 结果。

    只判一次、共用一份上下文：33 条契约在一条 5 级筛道里被反复判会白读 5 遍库。
    """
    from viewer import tool_contract as TC

    keys = [str(k or "") for k in (keys or []) if k]
    if not keys:
        return {}
    ctx = screen_context(con)
    cts = TC.list_all(con)
    out = {}
    for k in dict.fromkeys(keys):
        try:
            out[k] = TC.evaluate(k, cts.get(k), None, ctx)
        except Exception:  # noqa: BLE001
            log.exception("筛道资格判定异常（%s 按不可用处理）", k)
            out[k] = {"tool_key": k, "decision": "blocked", "blocked_kind": "unknown",
                      "reasons": ["工具资格判定异常，拒绝执行"], "state": "blocked"}
    return out
