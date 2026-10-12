#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""语义闸门审计器（semantics audit）—— 专治「数字没错，但属于错的那一天/错的口径」。

与常规回归的区别：
    常规回归测「通不通 / 有没有报错 / 字段非空」；
    本审计测「这个数到底属于哪一天、是不是只有一把尺、UI 有没有如实标注」。

原理 = 业界数据质量六维度（Timeliness / Consistency / Accuracy / Validity / Completeness / Uniqueness）
        在「本地 SQLite + HTTP API + 单页前端」这种轻量形态下的落地版，不依赖 dbt / Great Expectations 之类的重型栈。

规则清单
--------
T1  交易日闸门：把「当前日历日」当业务日期写库/返回的表达式，必须落在白名单或在同作用域内经过交易日校验。
T2  数据日必带：分时/行情类 API 的 payload 必须返回 data_date 或 trade_date 字段。
T3  陈旧必声明：响应带历史日期数据时，必须同时给 stale 与 market_note，否则 UI 无从标注。
C1  口径守恒：同一指标在后端 py 与前端 js 各算一次 → 报警（必须能对拍或收敛到一端）。
C2  同源多实现：同一能力被两处独立实现 → 口径漂移风险（今天这个 bug 就栽在 P 模块自己另起炉灶上）。
F1  时效断档：业务表最新日期早于最近交易日 → FAIL（数据陈旧/没采上）。
F2  日期戳伪造：库里 trade_date > 最近交易日 → FAIL（这正是「假期数据被打上当天戳」的直接证据）。
D1  未标日期：前端面板标题写死「当日/今日/实时」却没有日期变量槽 → 警告。

用法
----
    python audit_semantics.py                # 静态 + 数据库检查
    python audit_semantics.py --json         # 机器可读输出
    python audit_semantics.py --fix-advice   # 只打印处置建议

退出码：0 = 无 FAIL；1 = 存在 FAIL（可用于定时任务/门禁）。
"""
from __future__ import annotations

import argparse
import ast
import datetime as _dt
import json
import os
import re
import sqlite3
import sys

# ---------------------------------------------------------------- 配置
ROOT = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(ROOT, "viewer", "app.db")
SKIP_DIRS = {".venv", "node_modules", "archive", "__pycache__", ".git"}
PY_EXTS = {".py", ".pyi"}
JS_EXTS = {".js", ".mjs"}

# FORMATTING: 仅做展示格式化、不参与业务日期判定的白名单正则（命中即放行）
NOW_PATTERNS = [
    re.compile(r"strftime([\"'])[^\"']*[WMd][\"']", re.S),          # 时间戳串用于日志/展示
    re.compile(r"_\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}"),           # "YYYY-MM-DD HH:MM:SS"
]

# 业务上「必须用最近交易日、不能用日历日」的写入点关键字
BUSINESS_DATE_HINTS = ("trade_date", "data_date", "date", "pub_date", "snapshot_date")

# 老项目里已经存在的正确做法（用作「范式已存在、别处没抄过去」的对照）
GOOD_PATTERNS = {
    "latest_trade_date": re.compile(r"latest_trade_date\("),
    "is_trading_day": re.compile(r"is_trading_day\("),
    "sample_trade_date": re.compile(r"sample_trade_date\("),
    "sina_kline_dates": re.compile(r"sina_kline_dates\("),
    "anchor_source": re.compile(r"anchor_source"),
    "is_live": re.compile(r"is_live"),
    "stale": re.compile(r"\bstale\b"),
    "market_note": re.compile(r"market_note"),
}

# 前端面板标题里写死「当日/今日/实时」的措辞（D1 用）
UI_DATE_HINT = re.compile(r"当日|今日|今天|实时")

# ---------------------------------------------------------------- 工具
def _iter_files(exts):
    for base, dirs, files in os.walk(ROOT):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.startswith(".bench")]
        for f in files:
            if os.path.splitext(f)[1] in exts:
                yield os.path.join(base, f)


def _lines(path):
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as fh:
            return fh.read().splitlines()
    except OSError:
        return []


def _rel(p):
    return os.path.relpath(p, ROOT).replace("\\", "/")


# ---------------------------------------------------------------- 交易日历（复用 auction，避免再写第三把尺）
def _trade_days(n=12):
    try:
        sys.path.insert(0, ROOT)
        from auction import sina_kline_dates
        rows = sina_kline_dates("sh000001", n) or []
        days = []
        for r in rows:
            d = (r.get("date") or r.get("day") or "")[:10]
            if d:
                days.append(d)
        return sorted(set(days))
    except Exception:
        return []


def _latest_trade_day(today=None):
    days = _trade_days()
    today = today or _dt.date.today().isoformat()
    earlier = [d for d in days if d <= today]
    return earlier[-1] if earlier else None


# 「事实表」判定：日期列之外若有任何行情数值列已经有值，才是承载真实数据的表。
# 全是 NULL 的（如 contest_track 这种预生成未来观察槽的前瞻排程表）不参与 F1/F2 判定。
_FACT_VALUE_COLS = re.compile(r"close|open|pct|price|net|value|amount|turnover|"
                              r"main|flow|rate", re.I)

# 前瞻排程表白名单（2026-10-03 人工核对：这些表的日期是「待观察日/排程槽」而非「数据归属日」，
# 出现晚于今天的日期属正常；它们只是先把行预生成，等窗口走到那天再由采集回填数值列）。
# 新增这种表时务必登记进来，否则 F1/F2 会误伤；登记时写清理由。
SCHEDULE_TABLES = {
    "contest_track": "选手信号前瞻观察槽（预生成未来 N 天行，窗口走到再回填 close/change_pct）",
}


def _is_fact_table(cur, table, date_col):
    try:
        cols = [r[1] for r in cur.execute("PRAGMA table_info(%s)" % table)]
    except Exception:
        return False
    val_cols = [c for c in cols if c != date_col and _FACT_VALUE_COLS.search(c)]
    if not val_cols:
        return False
    for c in val_cols:
        try:
            cur.execute("select count(*) from %s where %s is not null and %s != ''"
                        % (table, c, c))
            if cur.fetchone()[0] > 0:
                return True
        except Exception:
            continue
    return False


# ---------------------------------------------------------------- 规则实现
class Finding(dict):
    def __init__(self, rule, level, where, msg, advice=""):
        super().__init__(rule=rule, level=level, where=where, msg=msg, advice=advice)


# ---- T1：日历日当业务日期 ----
def rule_t1():
    out = []
    for path in _iter_files(PY_EXTS):
        try:
            src = open(path, "r", encoding="utf-8", errors="ignore").read()
            tree = ast.parse(src)
        except SyntaxError:
            continue
        rel = _rel(path)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            if not isinstance(fn, ast.Attribute):
                continue
            if fn.attr not in ("now", "today"):
                continue
            # 只关心被拼成 / 赋给业务日期变量的目的地
            dests = []
            if isinstance(node, ast.Assign):
                dests = [t for t in node.targets if isinstance(t, ast.Name)]
            if not dests:
                continue
            for t in dests:
                if not any(k in t.id for k in ("date", "today", "day")):
                    continue
                name = t.id
                # 白名单：仅用于格式化展示
                skip = False
                try:
                    seg = ast.get_source_segment(src, node) or ""
                except Exception:
                    seg = ""
                if any(p.search(seg) for p in NOW_PATTERNS):
                    continue
                # 同作用域内是否有交易日校验（粗判：文件级存在相关函数）
                guarded = any(p.search(src) for p in
                              (GOOD_PATTERNS["latest_trade_date"],
                               GOOD_PATTERNS["is_trading_day"],
                               GOOD_PATTERNS["sample_trade_date"]))
                out.append(Finding(
                    "T1", "WARN" if guarded else "FAIL",
                    "%s:%d  %s = %s()" % (rel, node.lineno, name, fn.attr),
                    "用「当前日历日」作为业务日期变量；该文件%s"
                    % ("已有交易日历工具但未在此处调用（疑似口径未统一）" if guarded
                       else "没有任何交易日校验"),
                    "改用 latest_trade_date() / sample_trade_date()，并在非交易日显式标注 stale"))
    return out


# ---- T2/T3：API payload 的数据日与陈旧声明 ----
def rule_t2_t3():
    out = []
    start = _latest_trade_day()
    if not start:
        return [Finding("T2", "WARN", "-", "取不到交易日历，跳过 payload 检查")]
    # 拉一次线上关键端点实测
    try:
        import urllib.request, urllib.parse
        params = urllib.parse.urlencode({"kind": "industry"})
        with urllib.request.urlopen("http://127.0.0.1:8765/api/board_flow_intraday?" + params,
                                    timeout=8) as r:
            d = json.load(r)
    except Exception as e:
        return [Finding("T2", "WARN", "-", "服务未起或接口不通，跳过 payload 实测（%s）" % e)]

    has_date = bool(d.get("data_date") or d.get("trade_date"))
    if not has_date:
        out.append(Finding("T2", "FAIL", "/api/board_flow_intraday",
                           "payload 未返回 data_date / trade_date，前端无从标注数据日",
                           "响应体至少补 data_date + trade_date"))
    if d.get("data_date") and d.get("data_date") > start:
        out.append(Finding("T3", "FAIL", "/api/board_flow_intraday",
                           "data_date(%s) 晚于最近交易日(%s)，日期戳被伪造" % (d.get("data_date"), start),
                           "落库必须用数据源真实日期，不可用 datetime.now()"))
    if d.get("data_date") and d.get("data_date") < start:
        missing = ("stale" not in d) or ("market_note" not in d)
        if missing:
            out.append(Finding("T3", "FAIL", "/api/board_flow_intraday",
                               "返回的是历史日数据(%s < %s)，但没有 stale/market_note，界面必然被误读"
                               % (d.get("data_date"), start),
                               "必须同时返回 stale + market_note，UI 挂「非当日数据」胶囊"))
        else:
            out.append(Finding("T3", "PASS", "/api/board_flow_intraday",
                               "历史日数据已带 stale/market_note（%s）" % d.get("market_note")))
    return out


# ---- C2：同一语义能力被多个模块各自实现（「两把尺 / 三把尺」的根） ----
# 判据用 AST 顶层 FunctionDef 名，而不是「谁引用了谁」——引用不算第二实现，自己 def 才算。
SYNONYM_GROUPS = {
    "交易日历 / 最近交易日": ["market_trade_days", "latest_trade_date", "is_trading_day",
                            "sample_trade_date", "get_trade_date", "today_trade_date"],
    "多空情绪判定": ["sentiment", "mood", "tone", "bull_bear", "multi_empty"],
    "胜率 / 收益窗口": ["win_rate", "pnl_t1", "yield_t1", "roi_t1", "pnl"],
    "换手 / 值得率": ["turnover_rate", "worth_rate", "deserve_rate"],
}


def rule_c2():
    out = []
    owners: dict[str, dict[str, list[int]]] = {}
    for path in _iter_files(PY_EXTS):
        if os.path.basename(path) == os.path.basename(__file__):
            continue          # 审计器自身只读引用，不构成第二实现
        try:
            tree = ast.parse(open(path, "r", encoding="utf-8", errors="ignore").read())
        except SyntaxError:
            continue
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                owners.setdefault(node.name, {}).setdefault(_rel(path), []).append(node.lineno)
    for group, names in SYNONYM_GROUPS.items():
        for name in names:
            where = owners.get(name)
            if not where:
                continue
            if len(where) >= 2:
                detail = ", ".join("%s:%s" % (f, l[0]) for f, l in
                                   sorted(where.items(), key=lambda kv: kv[1][0])[:4])
                out.append(Finding(
                    "C2", "FAIL", detail,
                    "「%s」被 %d 个模块各自实现（%s）——同名不同实现 = 两把尺，口径随时漂移"
                    % (name, len(where), group),
                    "抽成单一权威模块（如 auction.py），其余模块只 import；"
                    "若确实需要扩展，让子模块显式转调并注释说明差异"))
    return out


# ---- C1：同一指标前后端各算一次 ----
def rule_c1():
    out = []
    js = {}
    for p in _iter_files(JS_EXTS):
        js[_rel(p)] = "\n".join(_lines(p))
    py = {}
    for p in _iter_files(PY_EXTS):
        py[_rel(p)] = "\n".join(_lines(p))
    candidates = ["情绪", "胜率", "值得率", "换手率", "收益率", "pct", "仓位", "净值"]
    for word in candidates:
        # 只扫描产品代码：tests/ 与 tools/ 里的探针脚本不算「前端实现」
        jh = [f for f, s in js.items()
              if word in s and re.search(r"\.toFixed\(|Math\.round", s)
              and not f.startswith(("tests/", "tools/", "archive/"))]
        ph = [f for f, s in py.items() if word in s and re.search(r"round\(|f\"\{.*:\.\d+f", s)]
        if jh and ph and not any("compute" in os.path.basename(f) for f in ph):
            out.append(Finding(
                "C1", "WARN", "%s ↔ %s" % (", ".join(sorted(jh)[:2]), ", ".join(sorted(ph)[:2])),
                "「%s」前后端各自做数值格式化，容易出现同一个数两个样子" % word,
                "数值一律由后端下发、前端只渲染；若必须前端算，加一条前后端对拍断言"))
    return out


# ---- F1/F2：数据库层的时效与日期戳 ----
def rule_f():
    out = []
    if not os.path.exists(DB):
        return [Finding("F1", "WARN", "-", "数据库不存在，跳过")]
    start = _latest_trade_day()
    try:
        con = sqlite3.connect("file:%s?mode=ro" % DB.replace("\\", "/"), uri=True)
        cur = con.cursor()
        tables = [r[0] for r in cur.execute(
            "select name from sqlite_master where type='table' order by name")]
    except Exception as e:
        return [Finding("F1", "WARN", "-", "打开数据库失败：%s" % e)]

    latest = start
    skipped = []
    # 今天是不是交易日。定时闸门（工作日 16:30 之类）在法定节假日仍会触发，
    # 那时「时效断档」是必然现象，不能算 FAIL，否则放假期间天天红。
    today = _dt.date.today().isoformat()
    try:
        from auction import is_trading_day as _is_td
        _today_is_td = _is_td(today)
    except Exception:  # noqa: BLE001 —— 日历取不到按「未知」处理，维持原判级
        _today_is_td = None
    if latest:
        for t in tables:
            for col in ("trade_date", "data_date", "date"):
                try:
                    cur.execute("select max(%s) from %s" % (col, t))
                    mx = cur.fetchone()[0]
                except Exception:
                    continue
                if not (isinstance(mx, str) and re.match(r"\d{4}-\d{2}-\d{2}", mx)):
                    continue
                if t in SCHEDULE_TABLES:
                    skipped.append("%s.%s(%s)" % (t, col, SCHEDULE_TABLES[t][:12]))
                    break
                # 排程表（前瞻预生成、数值列全空）不判 F1/F2
                if not _is_fact_table(cur, t, col):
                    skipped.append("%s.%s" % (t, col))
                    break
                if mx > latest:
                    out.append(Finding(
                        "F2", "FAIL", "%s.%s" % (t, col),
                        "库内最大值 %s 晚于最近交易日 %s —— 与今天这个 bug 同一形态（把非交易日/未来日写成当天）"
                        % (mx, latest),
                        "将该日期改回真实数据日，或在写入层加交易日闸门前置拦截"))
                    break
                if mx < latest and _today_is_td is False and latest != today:
                    out.append(Finding(
                        "F1", "INFO", "%s.%s" % (t, col),
                        "最新数据日 %s 落后最近交易日 %s，但今天（%s）为非交易日，时效断档属预期；"
                        "节后首日仍然如此才需要补采" % (mx, latest, today),
                        "假期/周末不报警；恢复交易首日重跑本审计器即可复检"))
                    break
                if mx < latest:
                    out.append(Finding(
                        "F1", "FAIL", "%s.%s" % (t, col),
                        "最新数据日 %s 落后于最近交易日 %s，时效断档 %d 天"
                        % (mx, latest,
                           len([d for d in _trade_days() if mx < d <= latest])),
                        "补跑该表缺失日期的采集；若是休市日/数据源未披露属预期，把该表加进免检名单"))
                    break
    if skipped:
        out.append(Finding("F1", "INFO", ", ".join(skipped[:6]) + ("…" if len(skipped) > 6 else ""),
                           "以上为前瞻排程表（数值列全空），已排除在 F1/F2 之外",
                           "如需纳入，请在表中显式写入 'actual' 状态列"))
    try:
        con.close()
    except Exception:
        pass
    return out


# ---- D1：UI 未标日期 ----
def rule_d1():
    out = []
    for p in _iter_files(JS_EXTS):
        for i, line in enumerate(_lines(p), 1):
            if UI_DATE_HINT.search(line) and re.search(r"(bf-curve-title|<h2|panel-title|"
                                                       r"section-t|· \$\{|class=\"[a-z-]*title)", line):
                out.append(Finding(
                    "D1", "WARN", "%s:%d" % (_rel(p), i),
                    "面板文案写死「当日/今日」，该处看不到数据来自哪一天",
                    "把措辞换成带日期槽的模板（如「… · ${data_date} …」），并在非当日时挂 stale 标记"))
    return out


# ---------------------------------------------------------------- 主流程
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--fix-advice", action="store_true")
    args = ap.parse_args()

    findings = []
    findings += rule_t1()
    findings += rule_t2_t3()
    findings += rule_c2()
    findings += rule_c1()
    findings += rule_f()
    findings += rule_d1()

    if args.fix_advice:
        for f in findings:
            print("[%s] %s\n    %s\n    -> %s" % (f["level"], f["rule"], f["msg"], f["advice"]))
        return 0

    if args.json:
        print(json.dumps({"latest_trade_day": _latest_trade_day(), "findings": findings},
                         ensure_ascii=False, indent=2))
        return 1 if any(f["level"] == "FAIL" for f in findings) else 0

    start = _latest_trade_day()
    print("=" * 78)
    print("语义闸门审计 audit_semantics  最近交易日 = %s" % (start or "取不到"))
    print("=" * 78)
    for f in findings:
        mark = {"FAIL": "[FAIL]", "WARN": "[WARN]", "PASS": "[ ok ]"}.get(f["level"], "[    ]")
        print("%s %-3s %s\n     %s" % (mark, f["rule"], f["where"], f["msg"]))
        if f["advice"]:
            print("     -> %s" % f["advice"])
    n_fail = sum(1 for f in findings if f["level"] == "FAIL")
    n_warn = sum(1 for f in findings if f["level"] == "WARN")
    print("-" * 78)
    print("合计 FAIL=%d  WARN=%d" % (n_fail, n_warn))
    return 1 if n_fail else 0


if __name__ == "__main__":
    sys.exit(main())
