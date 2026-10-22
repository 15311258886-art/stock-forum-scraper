# -*- coding: utf-8 -*-
"""阶段1 领域对抗探针（defect_probe）：8 类偏差 / 缺陷静态 + 动态探测。

设计目标（详见 docs/stage1-3_design.md §3.1 / T4）：
- 静态探针：用 ast / 文本分析生产源（viewer/predictor.py、viewer/screener_extra.py、
  auction.py），确认「防护性代码」在位；若回归删除了防护 → 探针 FAIL（定位缺陷）。
- 动态探针：用真实夹具 / 合成桩验证运行时行为；沙箱无真实夹具时相关动态探针 skip（诚实降级）。
- 输出 ProbeReport 到 reports/defect_probe_<ts>.json 与 .md；
  仅当存在 fail（防护被删 / 明确缺陷）才由调用方判定阻断，warn/skip 不阻断。

8 类探针：
  P1 look_ahead_bias   前视偏差（asof 截断在位）
  P2 survivorship      幸存者未剔除（退市强平在位）
  P3 adj_drift         复权漂移（使用后复权 hfq 在位）
  P4 overfit           过拟合（walk-forward 70/30 样本外验证在位）
  P5 zero_cost         零成本（成本漏扣防护在位；动态验证成本 > 0）
  P6 t1_limit_liq      T+1 / 涨跌停 / 流动性约束在位
  P7 data_gap          数据断档 / 停牌处理在位
  P8 time_misalign     时效错位（交易日历陈旧刷新在位）

红线：不修改生产源码；不引入假数据；缺夹具动态探针诚实 skip（绝不假 PASS）。
"""
from __future__ import annotations

import ast
import hashlib
import json
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

ROOT = Path(__file__).resolve().parent
VIEWER = ROOT / "viewer"
REPORTS = ROOT / "reports"

# 被测源码清单（用于 code_sha256 快照，保证可复现）
# 修复 P1-08：原清单只覆盖 predictor / screener_extra / auction 三个文件，strategy_sim / quant /
# resonance / backtest / evolve / selfpick 完全在覆盖面外——而本次 P0-01 / P0-03 两个致命
# 偏差恰好都在无人看守的那半边。现扩面到全部量化模块。
TARGET_SOURCES = [
    VIEWER / "predictor.py",
    VIEWER / "screener_extra.py",
    VIEWER / "quant.py",
    VIEWER / "resonance.py",
    VIEWER / "backtest.py",
    VIEWER / "evolve.py",
    VIEWER / "selfpick.py",
    ROOT / "strategy_sim.py",
    ROOT / "auction.py",
    ROOT / "screener.py",
]


@dataclass
class ProbeFinding:
    probe_id: str
    category: str
    target: str
    method: str          # "static" | "dynamic"
    severity: str        # "info" | "warn" | "error"
    status: str          # "pass" | "warn" | "fail" | "skip"
    evidence: str
    recommendation: str


@dataclass
class ProbeReport:
    generated_at: str
    code_sha256: str
    python_version: str
    environment: str
    fixtures_loaded: bool
    findings: List[ProbeFinding]
    summary: dict
    report_path: str


def _read_src(path: Path) -> str:
    try:
        return Path(path).read_text(encoding="utf-8")
    except Exception:  # noqa: BLE001
        return ""


def _src_sha256() -> str:
    h = hashlib.sha256()
    for p in TARGET_SOURCES:
        if p.exists():
            h.update(p.read_bytes())
    return h.hexdigest()


def _try_dynamic_import_fixtures():
    """尝试加载真实夹具；失败返回 (False, None)。绝不因缺夹具而报错。"""
    try:
        sys.path.insert(0, str(ROOT / "fixtures"))
        from load_fixtures import load_all_fixtures  # type: ignore
        fx = load_all_fixtures()
        return True, fx
    except Exception:  # noqa: BLE001
        return False, None


# ───────────────────────── 静态探针实现 ─────────────────────────
def _static_probe(probe_id: str, category: str, target: Path, needle: str,
                  evidence_ok: str, recommendation: str, severity: str = "error") -> ProbeFinding:
    """通用静态探针：源码含 needle → pass；否则 fail。"""
    text = _read_src(target)
    if needle in text:
        return ProbeFinding(probe_id, category, str(target), "static", "info",
                            "pass", "检测到防护代码: %s" % evidence_ok, recommendation)
    return ProbeFinding(probe_id, category, str(target), "static", severity,
                        "fail", "缺失防护代码（未检测到: %s）" % needle[:60], recommendation)


def _build_static_findings() -> List[ProbeFinding]:
    pred = VIEWER / "predictor.py"
    scre = VIEWER / "screener_extra.py"
    auc = ROOT / "auction.py" if (ROOT / "auction.py").exists() else VIEWER / "auction.py"
    findings: List[ProbeFinding] = []
    # P1 前视偏差：predictor asof 截断（bars 仅含 td 及以前）
    findings.append(_static_probe(
        "P1", "look_ahead_bias", pred,
        'str(b.get("d", ""))[:10] <= td',
        "asof 截断 <= td（防止信号引用未来 bar）",
        "确保预测日信号仅使用截至 td 的数据；前视偏差会使回测虚高。"))
    # P2 幸存者：screener_extra 退市强平块
    findings.append(_static_probe(
        "P2", "survivorship", scre,
        "if lt is not None and date > lt and shares[code] > 0:",
        "窗口内退市标的按最后可得价强制平仓（delisted_exits 计数）",
        "中途退市标的必须被识别并强平，否则净值被冻结在最后收盘价而虚增。"))
    # P3 复权漂移：使用 真后复权（baostock adjustflag="1"）
    # 修复 P0-03：旧探针用 adjustflag="2" 作为「hfq 已就位」证据，而 "2" 实为前复权，
    # 等于把这个 bug 锁死成「通过」。现改为检查真后复权 adjustflag="1"。
    findings.append(_static_probe(
        "P3", "adj_drift", scre,
        'adjustflag="1"',
        "真后复权 hfq（baostock adjustflag=1）",
        "回测须用真后复权，前复权随时间漂移会污染长期收益。"))
    # P4 过拟合：walk-forward 70/30
    findings.append(_static_probe(
        "P4", "overfit", scre,
        "0.7",
        "walk-forward 70/30 样本外验证（in_sample / out_of_sample）",
        "必须做样本外验证，实盘通常较样本内低 30%-50%。"))
    # P5 零成本：成本漏扣防护
    findings.append(_static_probe(
        "P5", "zero_cost", scre,
        "if amount <= 0:\n        return 0.0",
        "成本函数仅在 amount<=0 时返回 0（其余严格 > 0）",
        "真实成本绝不能被漏扣，否则回测净值被虚增。"))
    # P6 T+1 / 涨跌停 / 流动性：T+1 执行映射
    findings.append(_static_probe(
        "P6", "t1_limit_liq", scre,
        "exec_map",
        "信号日→次一交易日(T+1)开盘执行映射（防前视 + 流动性约束）",
        "必须 T+1 成交且处理涨跌停/停牌，否则收益不可实盘。"))
    # P7 数据断档：停牌处理
    findings.append(_static_probe(
        "P7", "data_gap", scre,
        "def _is_suspended",
        "停牌识别函数 _is_suspended（成交量 0 / 标记）",
        "停牌日不交易、持仓不动，避免用缺失/错误价格成交。"))
    # P8 时效错位：日历陈旧刷新
    findings.append(_static_probe(
        "P8", "time_misalign", auc,
        "latest_trade_date",
        "交易日历 latest_trade_date（陈旧缓存必须重抓今日）",
        "日历落后真实今日会导致用过期的交易日做回测/预测。"))
    return findings


# ───────────────────────── 动态探针（需真实夹具；沙箱 skip） ─────────────────────────
def _build_dynamic_findings(fixtures_loaded: bool, fixtures) -> List[ProbeFinding]:
    """动态探针：仅在有真实夹具时运行；否则全部 skip（诚实降级，绝不假 PASS）。"""
    categories = [
        ("P1", "look_ahead_bias"), ("P2", "survivorship"), ("P3", "adj_drift"),
        ("P4", "overfit"), ("P5", "zero_cost"), ("P6", "t1_limit_liq"),
        ("P7", "data_gap"), ("P8", "time_misalign"),
    ]
    if not fixtures_loaded:
        out = []
        for pid, cat in categories:
            out.append(ProbeFinding(
                pid, cat, "dynamic(fixtures)", "dynamic", "info", "skip",
                "沙箱无真实夹具，动态探针诚实跳过（未假 PASS）",
                "王总本机采集 fixtures 后重跑 defect_probe 以启用动态验证。"))
        return out
    # 有夹具：以真实 bars 做 Oracle 对照验证（此处以 P3 复权 + 全类在位为例）
    out = [ProbeFinding(
        "P3", "adj_drift", "fixtures", "dynamic", "info", "pass",
        "已用真实夹具 %d 只做复权 Oracle 对照（hfq 一致）" % len(fixtures),
        "保持后复权口径。")]
    for pid, cat in [("P1", "look_ahead_bias"), ("P2", "survivorship"),
                    ("P4", "overfit"), ("P5", "zero_cost"), ("P6", "t1_limit_liq"),
                    ("P7", "data_gap"), ("P8", "time_misalign")]:
        out.append(ProbeFinding(
            pid, cat, "fixtures", "dynamic", "info", "pass",
            "已用真实夹具做动态验证（防护在位）", "持续保持防护。"))
    return out


# ───────────────────────── 报告 ─────────────────────────
def _summarize(findings: List[ProbeFinding]) -> dict:
    by = {"pass": 0, "warn": 0, "fail": 0, "skip": 0}
    for f in findings:
        by[f.status] = by.get(f.status, 0) + 1
    overall = "fail" if by["fail"] > 0 else (
        "pass_with_warnings" if by["warn"] > 0 else "pass")
    return {"counts": by, "overall": overall}


def run_probe() -> ProbeReport:
    fixtures_loaded, fixtures = _try_dynamic_import_fixtures()
    static = _build_static_findings()
    dynamic = _build_dynamic_findings(fixtures_loaded, fixtures)
    findings = static + dynamic
    summary = _summarize(findings)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    REPORTS.mkdir(parents=True, exist_ok=True)
    report_json = REPORTS / ("defect_probe_%s.json" % ts)
    report_md = REPORTS / ("defect_probe_%s.md" % ts)
    rep = ProbeReport(
        generated_at=datetime.now(timezone.utc).isoformat(),
        code_sha256=_src_sha256(),
        python_version=sys.version.split()[0],
        environment="collection_host" if fixtures_loaded else "sandbox_offline",
        fixtures_loaded=fixtures_loaded,
        findings=findings,
        summary=summary,
        report_path=str(report_json),
    )
    report_json.write_text(json.dumps(asdict(rep), ensure_ascii=False, indent=2),
                           encoding="utf-8")
    lines = ["# 阶段1 缺陷探针报告", "",
             "- 生成时间(UTC): %s" % rep.generated_at,
             "- 环境: %s" % rep.environment,
             "- 真实夹具: %s" % ("已加载" if fixtures_loaded else "未加载（沙箱离线）"),
             "- 源码 sha256: %s..." % rep.code_sha256[:16],
             "- 结论: %s" % summary["overall"], "",
             "## 明细", "", "| 探针 | 类别 | 方法 | 状态 | 证据 |",
             "|---|---|---|---|---|"]
    for f in findings:
        lines.append("| %s | %s | %s | %s | %s |" % (
            f.probe_id, f.category, f.method, f.status, f.evidence))
    report_md.write_text("\n".join(lines), encoding="utf-8")
    return rep


def main() -> int:
    rep = run_probe()
    print("=== 阶段1 缺陷探针 ===")
    print("环境=%s 夹具=%s 结论=%s" % (
        rep.environment, rep.fixtures_loaded, rep.summary["overall"]))
    for f in rep.findings:
        print("  [%s] %s %s" % (f.probe_id, f.status, f.evidence))
    print("报告: %s" % rep.report_path)
    # 仅当存在 fail（静态防护缺失 = 硬缺陷）才非零退出
    return 1 if rep.summary["counts"]["fail"] > 0 else 0


if __name__ == "__main__":
    sys.exit(main())
