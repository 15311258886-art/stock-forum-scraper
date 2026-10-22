#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""巴菲特选股策略周报 → 结构化 index.json 导入器。

用法：
    python import_report.py                 # 扫描 reports/*.html，登记所有未入库的期次
    python import_report.py reports/xxx.html # 仅登记指定一期

每期研报是「自包含 HTML」（含 ECharts CDN），本脚本只解析其中的
「全部入围标的指标表」（代码/名称/分档/ROE/毛利率/净利率/资产负债率/PE/PB/
股息率/总市值/最新价/日涨跌），产出可查询的结构化数据，落进同目录 index.json。

index.json 结构：
{
  "updated_at": "2026-09-29",
  "reports": [
    {
      "period": "2026-09-29",
      "date":   "2026-09-29",
      "title":  "巴菲特价值投资选股策略 · A股周度筛选研报（2026-09-29）",
      "file":   "reports/2026-09-29.html",
      "summary":"10 只入围（核心 3 / 观察 3 / 剔除 4）",
      "counts": {"core":3,"watch":3,"out":4,"total":10},
      "picks":  [
        {"code":"600938","name":"中国海油","tier":"core",
         "roe":"16.16%","gross":"51.63%","net":"31.95%","debt":"28.60%",
         "pe":"11.44","pb":"1.85","div":"3.44%","mv":"15832.23",
         "price":"33.31","chg":"-0.57%"}
      ]
    }
  ]
}
"""
import json
import os
import re
import sys
from datetime import date

HERE = os.path.dirname(os.path.abspath(__file__))
REPORTS_DIR = os.path.join(HERE, "reports")
INDEX_PATH = os.path.join(HERE, "index.json")

TIER_MAP = {"核心": "core", "观察": "watch", "剔除": "out"}


def _strip_tags(s: str) -> str:
    return re.sub(r"<[^>]+>", "", s or "").strip()


def _find_period(text: str, fallback_name: str) -> str:
    """优先从文件名/正文取 YYYY-MM-DD；否则用今天。"""
    m = re.search(r"(\d{4}-\d{2}-\d{2})", fallback_name)
    if m:
        return m.group(1)
    m = re.search(r"(\d{4}-\d{2}-\d{2})", text)
    if m:
        return m.group(1)
    return date.today().isoformat()


def parse_report(html_path: str) -> dict | None:
    with open(html_path, "r", encoding="utf-8", errors="replace") as f:
        html = f.read()

    # 标题：<title> 或 <h1>
    title = ""
    m = re.search(r"<title>(.*?)</title>", html, re.S)
    if m:
        title = _strip_tags(m.group(1))
    if not title:
        m = re.search(r"<h1[^>]*>(.*?)</h1>", html, re.S)
        if m:
            title = _strip_tags(m.group(1))

    period = _find_period(html, os.path.basename(html_path))
    # 规范化文件名 → reports/<period>.html（保持 URL 干净）
    norm_name = period + ".html"
    norm_path = os.path.join(REPORTS_DIR, norm_name)
    if os.path.abspath(norm_path) != os.path.abspath(html_path):
        import shutil
        shutil.copyfile(html_path, norm_path)

    # 抓取所有「含指标行的数据行」，按代码去重（同码多表时保留指标最全的一行）
    # 兼容六种模板（跨度 2026-07 至 2026-09 的版式演化）：
    #   A 新（09-29+）　：tds[0]=代码, tds[1]=名称, tds[2]=分档文本, 后接 10 项指标
    #   B 09-22 完整表　：tds[0]=代码(.SH后缀), tds[1]=名称, tds[2]=<span class="pill">分档</span>, 后接 10 项指标
    #   C 旧（09-15）　　：tds[0]=<span class="tag">分档</span>, tds[1]=名称, tds[2]=代码(.SH后缀), 后接 10 项指标
    #   D 09-22 精简表　：6 列（代码/名称/ROE三口径/分档 pill），仅作兜底（prio=1）
    #   E 08-29 表　　　：12 列（分档/名称/代码.SZ/ROE/毛利率/净利率/负债率/PE/PB/股息率/市值/收盘价），9 项指标（无涨跌）
    #   F 07-11 表　　　：14 列（等级SABC/代码/名称/行业/市值/ROE/毛利率/净利率/PE/PB/股息率/负债率/CAGR×2），
    #                     早期"确信度分级"体系 → 映射 S/A=核心、B/C=观察
    cands: dict = {}  # code -> (prio, pick)

    def _span_text(cell: str) -> str:
        # 兼容 tag / pill / badge 三种徽标类名；一格多徽标时只取第一个（分档徽标通常在最前，
        # 后续的“新进 / 回归 / 出池”等状态徽标不应参与分档映射）
        m = re.search(r'class="(?:tag|pill|badge)[^"]*"[^>]*>(.*?)</span>', cell, re.S)
        return _strip_tags(m.group(1)) if m else ""

    for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", html, re.S):
        tds = re.findall(r"<td[^>]*>(.*?)</td>", tr, re.S)
        if len(tds) < 6:
            continue

        def tx(i: int) -> str:
            return _strip_tags(tds[i])

        code = name = tier_text = ""
        nums: list = []
        prio = 0
        # F 早期（07-11）：14 列，tds[1] 是带市场前缀的代码
        if len(tds) >= 14 and re.fullmatch(r"(?:sh|sz|SH|SZ)?\d{6}", tx(1)):
            grade = tx(0).upper()
            code = re.sub(r"\D", "", tx(1))
            name = tx(2)
            tier_text = {"S": "核心", "A": "核心", "B": "观察", "C": "观察"}.get(grade, "观察")
            nums = [tx(5), tx(6), tx(7), tx(11), tx(8), tx(9), tx(10),
                    tx(4).replace("亿", ""), "—", "—"]
            prio = 2
        # E 早期（08-29）：12 列，tds[2] 是带 .SH/.SZ 后缀的代码
        elif len(tds) >= 11 and re.fullmatch(r"\d{6}\.(?:SH|SZ)", tx(2)):
            tier_text = tx(0)
            name = tx(1)
            code = tx(2).split(".")[0]
            nums = [tx(3), tx(4), tx(5), tx(6), tx(7), tx(8), tx(9),
                    tx(10).replace("亿", ""), tx(11), "—"]
            prio = 2
        elif 'class="tag' in tds[0] and len(tds) >= 13:  # C
            tier_text, name, code = _span_text(tds[0]), _strip_tags(tds[1]), _strip_tags(tds[2]).split(".")[0]
            nums = [_strip_tags(x) for x in tds[3:13]]
        elif len(tds) == 6 and 'class="pill' in tds[5]:  # D
            code, name, tier_text = _strip_tags(tds[0]).split(".")[0], _strip_tags(tds[1]), _span_text(tds[5])
            nums = [_strip_tags(tds[4]), "—", "—", "—", "—", "—", "—", "—", "—", "—"]
            prio = 1
        elif len(tds) >= 13:  # A / B
            code = _strip_tags(tds[0]).split(".")[0]
            name = _strip_tags(tds[1])
            tier_text = _span_text(tds[2]) or _strip_tags(tds[2])
            nums = [_strip_tags(x) for x in tds[3:13]]
        else:
            continue
        if not re.fullmatch(r"\d{6}", code) or len(nums) != 10:
            continue
        pick = {
            "code": code, "name": name, "tier": TIER_MAP.get(tier_text, "out"),
            "roe": nums[0], "gross": nums[1], "net": nums[2], "debt": nums[3],
            "pe": nums[4], "pb": nums[5], "div": nums[6],
            "mv": nums[7], "price": nums[8], "chg": nums[9],
        }
        old = cands.get(code)
        def _holes(p):
            return sum(1 for v in p.values() if v == "—")
        if old is None or (prio < old[0]) or (prio == old[0] and _holes(pick) < _holes(old[1])):
            cands[code] = (prio, pick)
    picks = [cands[c][1] for c in cands]

    if not picks:
        print(f"  ! 跳过 {html_path}：未解析到指标行（可能模板不匹配）")
        return None

    counts = {
        "core": sum(1 for p in picks if p["tier"] == "core"),
        "watch": sum(1 for p in picks if p["tier"] == "watch"),
        "out": sum(1 for p in picks if p["tier"] == "out"),
        "total": len(picks),
    }
    summary = f"{counts['total']} 只入围（核心 {counts['core']} / 观察 {counts['watch']} / 剔除 {counts['out']}）"

    return {
        "period": period, "date": period, "title": title,
        "file": "reports/" + norm_name, "summary": summary,
        "counts": counts, "picks": picks,
    }


def main():
    os.makedirs(REPORTS_DIR, exist_ok=True)
    targets = sys.argv[1:] or [os.path.join(REPORTS_DIR, f) for f in os.listdir(REPORTS_DIR) if f.endswith(".html")]

    existing = {}
    if os.path.exists(INDEX_PATH):
        try:
            existing = {r["period"]: r for r in json.load(open(INDEX_PATH, encoding="utf-8")).get("reports", [])}
        except Exception:
            existing = {}

    added = 0
    for path in targets:
        if not os.path.isfile(path):
            print(f"  ! 不存在：{path}")
            continue
        rec = parse_report(path)
        if not rec:
            continue
        existing[rec["period"]] = rec
        added += 1
        print(f"  + 登记 {rec['period']}：{rec['counts']['total']} 只（核心{rec['counts']['core']}/观察{rec['counts']['watch']}/剔除{rec['counts']['out']}）")

    reports = sorted(existing.values(), key=lambda r: r["period"], reverse=True)
    out = {"updated_at": date.today().isoformat(), "reports": reports}
    with open(INDEX_PATH, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print(f"\n完成：共 {len(reports)} 期，新增/更新 {added} 期 → {INDEX_PATH}")


if __name__ == "__main__":
    main()
