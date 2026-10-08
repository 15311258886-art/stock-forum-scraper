# -*- coding: utf-8 -*-
"""阶段3 合理性闸门（运行时防护，独立模块，零第三方依赖）。

作用：对回测 / 预测产出的核心指标做「经验合理区间」校验，作为运行时最后一道
软性防护（只防护、不替代计算）。设计原则（详见 docs/stage1-3_design.md §3.4 / Q6）：

- 绝不修改任何计算公式（predictor / screener_extra 仅最小侵入调用 run_gates）。
- 阈值集中在常量区（BOUNDS），王总可按 Q6 经验区间调整。
- 测试模式（ctx["test_mode"]=True）下，越界 → 抛 GateViolation（用于单测 / CI 失败）。
- 生产模式（默认）仅告警 + 写 reports/gate_warnings.json，绝不阻断业务、绝不静默吞。
- 缺指标（None）一律 skip，绝不假 PASS。

红线：本模块不引入任何假数据；所有判断基于被测系统已算出的真实指标值。
"""
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# ───────────────────────── 阈值常量区（Q6 经验区间，可集中调整） ─────────────────────────
# total_return / annual_return（年化总收益）：基于 A 股实盘经验与独立验证数据调整。
# 独立验证 B7 回测：total_return≈0.50（50%），sharpe≈2.0；年化/总收益超过 200% 或
# sharpe>3 在 A 股持续策略中极为罕见，通常是过拟合或计算错误，故收紧上限。
TOTAL_RETURN_BOUNDS = (-0.95, 2.0)
SHARPE_BOUNDS = (-2.0, 3.0)
# max_drawdown：必须是 [负, 0]（0 表示无回撤；正数意味着把收益误标为回撤）
MAX_DRAWDOWN_BOUNDS = (-1.0, 0.0)
# 单日收益（含涨跌停 ±10%/±20%，取主板口径 ±11% 留 1% 余量）
DAILY_RETURN_BOUNDS = (-0.11, 0.11)

REPORTS_DIR = Path(__file__).resolve().parent.parent / "reports"
GATE_WARNINGS_PATH = REPORTS_DIR / "gate_warnings.json"


class GateViolation(Exception):
    """测试模式下闸门越界抛出的异常（生产模式不抛）。"""


@dataclass
class GateResult:
    name: str
    ok: bool
    level: str            # "ok" | "warn" | "error" | "skip"
    observed: Optional[float]
    bounds: tuple
    message: str


def _check(name: str, value, bounds: tuple, ctx: dict) -> GateResult:
    """单指标区间校验：None → skip；越界 → error（测试模式抛 GateViolation）。"""
    if value is None:
        return GateResult(name, True, "skip", None, bounds,
                          "skip: 指标未提供（None），不假 PASS")
    lo, hi = bounds
    if lo <= float(value) <= hi:
        return GateResult(name, True, "ok", float(value), bounds,
                          "在区间 [%s, %s] 内" % (lo, hi))
    msg = "%s=%s 超出合理区间 [%s, %s]" % (name, value, lo, hi)
    if ctx.get("test_mode"):
        raise GateViolation(msg)
    return GateResult(name, False, "error", float(value), bounds, msg)


def check_total_return(value, ctx: dict) -> GateResult:
    """年化总收益区间校验。"""
    return _check("total_return", value, TOTAL_RETURN_BOUNDS, ctx)


def check_sharpe(value, ctx: dict) -> GateResult:
    """夏普比率区间校验。"""
    return _check("sharpe", value, SHARPE_BOUNDS, ctx)


def check_drawdown(value, ctx: dict) -> GateResult:
    """最大回撤区间校验：必须落在 [-1.0, 0]（负数=亏损幅度）。"""
    return _check("max_drawdown", value, MAX_DRAWDOWN_BOUNDS, ctx)


def check_daily_return(value, ctx: dict) -> GateResult:
    """单日收益校验：value 可为单值或列表；列表逐笔校验，任一越界即 error。

    单日收益含涨跌停（主板 ±10% / 创业板·科创板 ±20%），主板口径 ±11% 留 1% 余量。
    """
    if value is None:
        return GateResult("daily_return", True, "skip", None, DAILY_RETURN_BOUNDS,
                          "skip: 未提供单日收益序列")
    lo, hi = DAILY_RETURN_BOUNDS
    items = value if isinstance(value, (list, tuple)) else [value]
    violations = []
    for i, v in enumerate(items):
        if v is None:
            continue
        if not (lo <= float(v) <= hi):
            violations.append((i, float(v)))
            if ctx.get("test_mode"):
                raise GateViolation(
                    "daily_return[%d]=%s 超出合理区间 [%s, %s]" % (i, v, lo, hi))
    if violations:
        return GateResult("daily_return", False, "error", None, DAILY_RETURN_BOUNDS,
                          "单日收益越界 %d 笔，示例: %s" % (
                              len(violations),
                              ", ".join("%s=%s" % (i, v) for i, v in violations[:5])))
    return GateResult("daily_return", True, "ok", None, DAILY_RETURN_BOUNDS,
                      "全部 %d 笔单日收益在区间内" % len(items))


def run_gates(metrics: dict, ctx: dict) -> list:
    """对指标字典跑全部闸门，返回 GateResult 列表。

    metrics 可含：total_return / annual_return / sharpe / max_drawdown / daily_returns(列表)。
    缺字段 → 对应闸门 skip（绝不假 PASS）。
    生产模式（非 test_mode）：把 error/warn 落地为 reports/gate_warnings.json（不阻断业务）。
    """
    ctx = ctx or {}
    results = [
        check_total_return(metrics.get("total_return"), ctx),
        check_sharpe(metrics.get("sharpe"), ctx),
        check_drawdown(metrics.get("max_drawdown"), ctx),
        check_daily_return(metrics.get("daily_returns"), ctx),
    ]
    if not ctx.get("test_mode"):
        _record_warnings(results, ctx)
    return results


def _record_warnings(results: list, ctx: dict) -> None:
    """生产模式：将 error/warn 级结果写入 reports/gate_warnings.json（追加，不阻断业务）。"""
    triggered = [r for r in results if (not r.ok) or (r.level in ("warn", "error"))]
    if not triggered:
        return
    try:
        REPORTS_DIR.mkdir(parents=True, exist_ok=True)
        prev = []
        if GATE_WARNINGS_PATH.exists():
            try:
                prev = json.loads(GATE_WARNINGS_PATH.read_text(encoding="utf-8")) or []
                if not isinstance(prev, list):
                    prev = []
            except Exception:  # noqa: BLE001
                prev = []
        entry = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "module": ctx.get("module"),
            "function": ctx.get("function"),
            "warnings": [asdict(r) for r in triggered],
        }
        prev.append(entry)
        # 仅保留最近 50 条，避免无限增长
        GATE_WARNINGS_PATH.write_text(
            json.dumps(prev[-50:], ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:  # noqa: BLE001
        # 绝不让报告写入失败影响业务
        pass


if __name__ == "__main__":
    # 自测演示（非测试套件）：以一组样例指标展示闸门行为
    demo = {"total_return": 1.2, "sharpe": 0.9, "max_drawdown": -0.2,
            "daily_returns": [0.01, -0.02, 0.005]}
    for r in run_gates(demo, {"test_mode": False, "module": "demo", "function": "demo"}):
        print(r)
    print("gate module self-check ok")
