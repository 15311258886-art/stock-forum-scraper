# -*- coding: utf-8 -*-
"""低位池（lowbox）/ 低波池 行业上限的纯逻辑模块。

把"选股结果里同一行业占比不能过高"的约束抽成无副作用的纯函数，
让生产代码（viewer/server.py 的 api_screener_lowbox）和回测脚本
（tools/backtest_lowvol_industry.py）共用同一套上限逻辑，
保证"回测结论 → 生产阈值"的口径完全一致。

设计纪律：
  · 本模块不 import 任何业务模块 / 不读配置，全部逻辑由入参驱动（便于单测）。
  · 行业维度取 证监会一级行业 = industry_csrc 的"-"前段；缺失归 "未知" 组，
    同样受上限约束（缺失不能成为逃避限制的后门）。
  · 两个阈值（按只数上限 / 按池子占比上限）任一生效即叠加取更紧者；
    都置 0 / None = 不限制，等价于原 [:limit] 的截断行为。
"""
from typing import Dict, List, Optional, Tuple


def industry_level1(industry_csrc) -> str:
    """证监会一级行业：取 industry_csrc（如 '制造业-食品制造业'）的 '-' 前段。

    缺失 / 空 → '未知'。没有 '-' 的单段（如 '制造业'）原样返回。
    """
    s = (industry_csrc or "").strip()
    if not s:
        return "未知"
    if "-" in s:
        return s.split("-", 1)[0]
    return s


def fetch_industry_map(con, codes: List[str]) -> Dict[str, str]:
    """批量查 stock_org.industry_csrc → {code: 一级行业}。

    分片查避免一次性拼过长的 IN 列表；查不到的 code 不写入（调用方用 .get(code,'未知') 兜底）。
    """
    out: Dict[str, str] = {}
    codes = [c for c in (codes or []) if c]
    if not codes:
        return out
    for i in range(0, len(codes), 500):
        chunk = codes[i:i + 500]
        ph = ",".join("?" * len(chunk))
        try:
            for code, cs in con.execute(
                "SELECT code, industry_csrc FROM stock_org WHERE code IN (%s)" % ph, chunk
            ):
                out[code] = industry_level1(cs)
        except Exception:  # noqa: BLE001
            # 查询失败（如表不存在）时退回空 map，由调用方按"未知"处理
            pass
    return out


def apply_industry_cap(
    rows: List[dict],
    limit: int,
    max_per_industry: Optional[int] = None,
    max_industry_pct: Optional[float] = None,
    industry_key: str = "industry1",
) -> Tuple[List[dict], Dict[str, int]]:
    """在已排好序（通常按综合分降序）的候选行上施加行业上限，返回截断后的池子。

    参数：
      rows            候选行（list of dict），调用方需保证已按优先级排好序；
                     每行须含 industry_key 字段（如 'industry1'）。
      limit           目标池子大小（如 30）。
      max_per_industry  单行业最多保留只数（0/None = 不限制）。
      max_industry_pct  单行业最多占池子比例（0/None = 不限制）；
                        生效时折算为 ceil/round(limit*pct) 只。
      industry_key   行内行业字段名。

    返回：
      (capped_rows, counts) —— capped_rows 长度 ≤ limit；counts 为各行业的实际入选只数。

    规则：
      · 两个阈值都生效时取更紧者（min）。
      · 贪心遍历已排序行：某行业已达上限则该行跳过（保留更高优先级的同行业票）。
      · 因上限导致无法填满 limit 时，返回少于 limit 的池子（这正是分散化的代价，由调用方披露）。
      · '未知' 组与其他行业一视同仁，同样受上限约束。
    """
    if (not max_per_industry or max_per_industry <= 0) and (not max_industry_pct or max_industry_pct <= 0):
        # 无上限：等价于原 [:limit]
        return list(rows[:limit]), {}

    eff_cap: Optional[int] = None
    if max_industry_pct and max_industry_pct > 0:
        eff_cap = max(1, int(round(limit * float(max_industry_pct))))
    if max_per_industry and max_per_industry > 0:
        eff_cap = min(eff_cap, int(max_per_industry)) if eff_cap else int(max_per_industry)

    counts: Dict[str, int] = {}
    out: List[dict] = []
    for r in rows:
        if len(out) >= limit:
            break
        ind = (r.get(industry_key) or "未知") if isinstance(r, dict) else "未知"
        c = counts.get(ind, 0)
        if eff_cap is not None and c >= eff_cap:
            continue
        counts[ind] = c + 1
        out.append(r)
    return out, counts


def concentration(counts: Dict[str, int]) -> Tuple[float, float, int, int]:
    """由 counts（各行业入选只数）算集中度：第一大行业占比、前三大累计占比。

    返回 (top1_share, top3_share, n_industries, pool_size)。
    pool_size=0 时占比返回 0.0。
    """
    total = sum(counts.values())
    if total <= 0:
        return 0.0, 0.0, 0, 0
    ordered = sorted(counts.values(), reverse=True)
    top1 = ordered[0] / total if ordered else 0.0
    top3 = sum(ordered[:3]) / total
    return float(top1), float(top3), len(ordered), total
