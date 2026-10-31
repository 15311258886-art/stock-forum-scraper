# -*- coding: utf-8 -*-
"""出网抓取结果核验器（纯本地，不联网）。

用途：本机跑完 `fetch_a_shares_tencent.py` / `fetch_plate_members.py` 后，
    用本脚本核验「数据是否真的补全」——而不是凭脚本打印的 "OK" 自我安慰。

检查项（对应此前缺口 C3 / C4）：
  1. stock_daily_snapshot 今日快照行数 + 覆盖率（vs **security_master** 权威股票数）
  2. a_shares_universe 计数（遗留表，仅作对照：它漏收整块北交所）
  3. 估值字段 pe_ttm / pb / total_mv 非空比例（治 C4 估值空洞）
  4. plate_members 各板块成员计数 + 总板块数（治 C3 官方池）
  5. stock_names 是否已被快照刷新（source=eastmoney_snapshot / tencent_snapshot）
  6. 非股票是否混进了快照（security_master.kind != stock 的行）—— **INFO 级**

退出码语义（**这是本脚本的契约，CI / 定时任务按此判断，不要凭打印内容猜**）：

  0  = 达标。覆盖率 ≥ ``COVERAGE_MIN``（99.0%）且无其它硬缺口。
       仍会逐个列出缺失代码（如果数量在 ``LIST_LIMIT`` 以内全列），
       但"缺的那几只"已被判定为**源本身不给行情**（退市/停牌/未覆盖段），
       不是我们抓取漏了 —— 所以不算失败。
  1  = 有真实缺口。覆盖率 < 99.0%，或估值非空率 < 80%，或 plate_members 为空，
       或快照为空。**缺的 code 必须逐个列出**（只给百分比等于没给信息），
       并附「为什么缺」的取证（见下）。
  2  = 读库 / 取码失败：库文件不存在、快照表缺失、security_master 缺失或查询异常。
       这是"我没法判断"，与"数据有缺口"是两件事，必须区分开。

「缺行情」与「源不覆盖」怎么区分：
  `fetch_a_shares_tencent.py --merge` 每次落库都会写
  ``data/snapshots/_fetch_attempt_<date>.json``，里面逐只记了 requested / missing
  及原因（``no_response`` = 源没回这只；``no_quote`` = 回了但无成交价，多为停牌；
  ``no_trade`` = 回了但当日零成交，抓取端拒绝落库）。
  历史日另有 ``data/snapshots/backfill_missing_<date>.json``，记的是
  "这一天根本补不了、为什么"（缺 code 清单也在里面），对应原因 ``no_source``。
  本脚本读它们做归因；没有取证文件就只能标 ``no_evidence``，**不猜**。

`--date` 与 `--recent`：
  · 默认核验快照里最新一天，并附一张「近期各交易日覆盖率」表 —— 不看这张表，
    任何"近 N 日对比"都会把"某天起才补上的板块"读成"板块突然多出几百只"。
  · `--date 2026-10-29` 可单独体检历史日，但历史日**通常返回 1**：
    实时源没有历史行情，那些日子补不了（见 backfill_missing_*.json），
    这是**预期的缺口**，不是回归。退出码只对"最新一天"负责。

运行：python tools/verify_fetch.py
      python tools/verify_fetch.py --date 2026-10-29 --recent 0
"""
import json
import os
import sqlite3
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from viewer import security_master as SM  # noqa: E402

DB_PATH = os.environ.get("VIEWER_DB") or os.path.join(ROOT, "viewer", "app.db")
SNAP_DIR = os.path.join(ROOT, "data", "snapshots")

EXIT_OK = 0
EXIT_GAP = 1
EXIT_ERR = 2

COVERAGE_MIN = 99.0     # 覆盖率达标线（%）：全 A 5586 只里允许缺 55 只以内
VALUATION_MIN = 80.0    # 估值字段非空率下限（%）
LIST_LIMIT = 80         # 单个缺口清单最多逐条打印多少只（超出只给数量，避免刷屏）

# 取证文件里的原因 -> 人话。键与 fetch_a_shares_tencent.py 的 missing 取值一一对应。
_REASON_ZH = {
    "no_response": "源未返回该代码（源不覆盖该代码段 / 已退市无行情）",
    "no_quote": "源有返回但无成交价（停牌等）",
    "no_trade": "源只给陈旧最后价、当日零成交（停牌/退市），抓取端拒绝落库",
    "no_source": "该日无源可补（实时接口无历史行情；见 backfill_missing_<date>.json）",
    "parsed_but_not_stored": "源有返回但未落库（落库环节被过滤，需排查）",
    "not_requested": "最近一次抓取未请求该代码（历史缺口；重跑对应板块即可）",
    "no_evidence": "无取证文件，无法归因（先跑一次 fetch_a_shares_tencent.py）",
}

# 这些原因 = "源本来就没有当日行情"，不算抓取漏抓，只记 INFO。
_SOURCE_SIDE = ("no_response", "no_quote", "no_trade", "no_source")


def _q(con, sql, args=()):
    """容错查询：表缺失时返回 'ERR:...' 字符串而不是抛异常（本脚本要跑完所有小节）。"""
    try:
        return con.execute(sql, args).fetchall()
    except Exception as e:  # noqa: BLE001
        return "ERR:" + str(e)[:60]


def _table_exists(con, name: str) -> bool:
    try:
        return con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                           (name,)).fetchone() is not None
    except Exception:  # noqa: BLE001
        return False


def _load_attempt_report(trade_date: str):
    """读本次抓取的取证文件。返回 dict 或 None（不存在/损坏/过期都算 None）。

    过期（date 字段对不上）必须当不存在处理：拿昨天的请求记录去解释今天的缺口，
    会得出"源不覆盖"的错误结论。
    """
    path = os.path.join(SNAP_DIR, "_fetch_attempt_%s.json" % trade_date)
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            rep = json.load(f)
    except Exception:  # noqa: BLE001
        return None
    if not isinstance(rep, dict) or rep.get("date") != trade_date:
        return None
    return rep


def _load_backfill_note(trade_date: str):
    """读"该日补不了"的取证文件 `backfill_missing_<date>.json`，没有则 None。

    与 `_fetch_attempt_*.json` 的区别：那个记的是"这次抓了谁、谁没回"，
    这个记的是"这一天根本补不了，为什么"。历史日的缺口只能用它解释 ——
    拿今天的抓取记录去解释昨天的缺口，会得出"源不覆盖"的假结论。
    """
    path = os.path.join(SNAP_DIR, "backfill_missing_%s.json" % trade_date)
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            note = json.load(f)
    except Exception:  # noqa: BLE001
        return None
    if not isinstance(note, dict) or note.get("date") != trade_date:
        return None
    return note


def _reason_for_date(trade_date: str, codes) -> str:
    """某一日缺失代码的归因（用于近期表的一句话标注，不做逐只展开）。"""
    if _load_backfill_note(trade_date):
        return "no_source"
    rep = _load_attempt_report(trade_date)
    if not rep:
        return "no_evidence"
    reasons = _attribute(rep, list(codes))
    uniq = sorted(set(reasons.values()))
    return "/".join(uniq) if uniq else "no_evidence"


def _board_counts(con, master_codes, trade_date: str):
    """返回 (got, {(板块): [覆盖数, 总数]}) —— 跨日对比必须按这个粒度看。"""
    got = {c for (c,) in con.execute(
        "SELECT code FROM stock_daily_snapshot WHERE trade_date=?", (trade_date,))}
    rows_by_code = {r["code"]: r for r in SM.load(con)}
    by_board: dict = {}
    for code in master_codes:
        b = (rows_by_code.get(code) or {}).get("board") or "?"
        by_board.setdefault(b, [0, 0])
        by_board[b][1] += 1
        if code in got:
            by_board[b][0] += 1
    return got, by_board


def _attribute(rep, missing_codes):
    """给每个缺失代码归因：{code: reason_key}。

    归因规则（宁缺勿猜）：
      · 取证文件里 missing 明确记了原因 → 用它的原因；
      · 在 requested_codes 里却没进 missing、也没落库 → 落库环节被过滤（真 bug）；
      · 不在 requested_codes 里 → 这次压根没请求（历史缺口）；
      · 没有取证文件 → no_evidence。
    """
    out = {}
    if not rep:
        return {c: "no_evidence" for c in missing_codes}
    req = rep.get("requested_codes")
    req_set = set(req) if isinstance(req, list) and req else None
    miss = rep.get("missing") or {}
    for c in missing_codes:
        if c in miss:
            out[c] = miss[c] if miss[c] in _SOURCE_SIDE else "no_response"
        elif req_set is not None and c in req_set:
            out[c] = "parsed_but_not_stored"
        elif req_set is not None:
            out[c] = "not_requested"
        else:
            out[c] = "no_evidence"
    return out


def _print_code_list(codes, rows_by_code, reasons, suspects, hint=""):
    """逐个列出缺失代码：代码 / 名称 / 板块 / 归因 / 是否退市嫌疑。"""
    shown = codes[:LIST_LIMIT]
    for c in shown:
        r = rows_by_code.get(c) or {}
        why = reasons.get(c, "no_evidence")
        flags = []
        if c in suspects:
            flags.append("名称带退市标记")
        print("      - %-8s %-12s %-6s 原因=%s%s"
              % (c, (r.get("name") or "?")[:12], (r.get("board") or "?")[:6],
                 why, ("  [" + "/".join(flags) + "]") if flags else ""))
    if len(codes) > LIST_LIMIT:
        print("      …… 其余 %d 只同类（%s）"
              % (len(codes) - LIST_LIMIT,
                 hint or "完整清单见 data/snapshots/ 下该日的取证文件"))


def main(argv=None):
    """argv=None 时按命令行参数解析（`sys.argv[1:]`）。

    **测试请显式传 `[]`**：直接 `main()` 会吃进 pytest 自己的参数并报 unrecognized arguments。
    """
    import argparse
    ap = argparse.ArgumentParser(description="出网抓取结果核验（纯本地）")
    ap.add_argument("--date", default=None,
                    help="核验指定交易日（默认=快照里最新一天）")
    ap.add_argument("--recent", type=int, default=5,
                    help="近期各日覆盖率表显示几天（默认 5；0=不显示）")
    args = ap.parse_args(argv)

    if not os.path.exists(DB_PATH):
        print("[verify] 找不到 %s" % DB_PATH)
        return EXIT_ERR
    con = sqlite3.connect(DB_PATH, timeout=30)
    problems = []      # 硬缺口 -> exit 1
    infos = []         # 允许存在 -> 不影响退出码，但必须写清为什么
    print("=" * 60)
    print("出网抓取结果核验  @ %s" % DB_PATH)
    print("=" * 60)

    if not _table_exists(con, "stock_daily_snapshot"):
        print("\n[!] stock_daily_snapshot 表不存在 —— 无法核验任何数据。")
        print("    修复：python tools/fetch_a_shares_tencent.py --merge")
        con.close()
        print("=" * 60)
        print("核验结果：✗ 读库失败（缺快照表），退出码 2")
        return EXIT_ERR

    # ---------- 1. 快照 + 覆盖率（分母 = 权威主表的股票数） ----------
    latest_td = con.execute("SELECT MAX(trade_date) FROM stock_daily_snapshot").fetchone()[0]
    td = args.date or latest_td
    if args.date and td != latest_td:
        print("\n[!] --date %s 不是快照里最新的一天（最新=%s）。" % (td, latest_td))
        print("    历史日只做覆盖率体检，退出码仍只对最新一天负责 —— 别拿历史日冒充当日验收。")
    snap_n = con.execute("SELECT COUNT(*) FROM stock_daily_snapshot WHERE trade_date=?",
                         (td,)).fetchone()[0]
    legacy_uni_n = (con.execute("SELECT COUNT(*) FROM a_shares_universe").fetchone()[0]
                    if _table_exists(con, "a_shares_universe") else -1)
    try:
        master_codes = SM.stock_codes(con)          # 含北交所，非股票已剔除
        master_n = len(master_codes)
    except SM.SecurityMasterMissing as e:
        print("\n[!] security_master 缺失：%s" % e)
        print("    没有权威宇宙就没有覆盖率分母 —— 不猜分母，直接报读码失败。")
        con.close()
        print("=" * 60)
        print("核验结果：✗ 读库失败（缺 security_master），退出码 2")
        return EXIT_ERR
    except Exception as e:  # noqa: BLE001
        print("\n[!] 读取 security_master 失败：%s" % e)
        con.close()
        print("=" * 60)
        print("核验结果：✗ 读库失败，退出码 2")
        return EXIT_ERR

    print("\n[1] 日度快照 (stock_daily_snapshot)")
    print("    核验交易日 trade_date = %s%s" % (td, "（最新）" if td == latest_td else "（历史）"))
    print("    该日快照行数 = %d" % snap_n)
    print("    权威股票数 security_master = %d（含北交所 %d）"
          % (master_n, len(SM.stock_codes(con, boards=SM.BOARD_BSE)) if master_n else 0))
    print("    遗留表 a_shares_universe = %d（仅对照：它漏收整块北交所，不可当分母）"
          % legacy_uni_n)

    cov = 0.0
    stock_rows_n = 0
    # 主表全量（5.5k 行）读一次就够，1/1b/6 三节都要用板块名
    rows_by_code = {r["code"]: r for r in SM.load(con)}
    if not td:
        problems.append("快照表为空：fetch_a_shares_tencent.py 未跑或未落库")
    else:
        got, by_board = _board_counts(con, master_codes, td)
        # 覆盖率只认"权威宇宙 ∩ 快照"：快照里的可转债/ETF 不能拿来凑分子，
        # 否则 111024 这类行会把覆盖率虚抬成 >100%，缺口再次隐身。
        covered = [c for c in master_codes if c in got]
        missing = [c for c in master_codes if c not in got]
        cov = round(100.0 * len(covered) / master_n, 2) if master_n else 0.0
        stock_rows_n = len(covered)
        print("    覆盖率(权威股票 ∩ 快照 / 权威股票) = %d/%d = %.2f%%（达标线 ≥ %.1f%%）"
              % (len(covered), master_n, cov, COVERAGE_MIN))

        print("    分板块明细：")
        for b in sorted(by_board, key=lambda x: by_board[x][0] - by_board[x][1]):
            have, total = by_board[b]
            print("      %-8s %4d/%4d%s" % (b, have, total,
                                            "" if have == total else "  ← 缺 %d" % (total - have)))

        rep = _load_attempt_report(td)
        note = _load_backfill_note(td)
        if note is not None:
            print("    不可补取证 data/snapshots/backfill_missing_%s.json：%s"
                  % (td, note.get("reason_zh") or note.get("reason")))
        if rep is not None:
            print("    取证文件 data/snapshots/_fetch_attempt_%s.json：请求 %d 只 / 解析 %d 只 / "
                  "落库 %d 只 / 失败批 %d  （%s）"
                  % (td, rep.get("requested", -1), rep.get("parsed", -1),
                     rep.get("inserted", -1), rep.get("failed_batches", -1),
                     "板块=%s" % (rep.get("boards") or "全市场")))
        else:
            print("    取证文件 data/snapshots/_fetch_attempt_%s.json：不存在或已过期" % td)

        if missing:
            if note is not None:
                # 有"不可补"取证：该日全部缺口都是源侧限制，不该被读成抓取漏抓
                reasons = {c: "no_source" for c in missing}
            else:
                reasons = _attribute(rep, missing)
            suspects = {r["code"] for r in SM.delist_suspects(con)}
            groups = {}
            for c in missing:
                groups.setdefault(reasons.get(c, "no_evidence"), []).append(c)
            print("    缺失 %d 只（逐个列出；原因取自取证文件，不猜）：" % len(missing))
            hint = ("完整代码清单见 data/snapshots/backfill_missing_%s.json" % td
                    if note is not None else "")
            for why in sorted(groups, key=lambda k: -len(groups[k])):
                print("      ├ %s：%d 只 —— %s"
                      % (why, len(groups[why]), _REASON_ZH.get(why, why)))
                _print_code_list(groups[why], rows_by_code, reasons, suspects, hint)
            # 归因决定这条算不算"真实缺口"：
            #   · not_requested / parsed_but_not_stored = 我们自己的问题 -> 硬缺口
            #   · no_response / no_quote = 源不给，记 INFO（并在报告里留痕）
            #   · no_evidence = 没取证，不算缺口也不算通过，归 INFO 但提示补取证
            own_fault = [c for c in missing
                         if reasons.get(c) in ("not_requested", "parsed_but_not_stored")]
            if own_fault:
                problems.append("缺失 %d 只属于抓取端自身问题（未请求/未落库），"
                                "代码：%s" % (len(own_fault), ",".join(own_fault[:20])))
            if cov < COVERAGE_MIN:
                problems.append("覆盖率 %.2f%% < %.1f%%：缺 %d 只（分母=权威主表；明细见上）"
                                % (cov, COVERAGE_MIN, len(missing)))
        if cov >= COVERAGE_MIN:
            print("    覆盖率判定：✓ 达标")

    # ---------- 1b. 近期各交易日覆盖率（跨日对比的唯一正确视角） ----------
    if args.recent and args.recent > 0 and master_n:
        dates = [r[0] for r in con.execute(
            "SELECT DISTINCT trade_date FROM stock_daily_snapshot "
            "ORDER BY trade_date DESC LIMIT ?", (int(args.recent),))]
        board_of_code = {c: ((rows_by_code.get(c) or {}).get("board") or "?")
                         for c in master_codes}
        print("\n[1b] 近期各交易日覆盖率（%d 天）" % len(dates))
        print("    单列这张表的原因：不按日看，任何「近 N 日对比」都会把"
              "「某天起才补上的板块」读成「这个板块突然多出几百只」。")
        print("    %-11s %-6s %-10s %-8s %-10s %s"
              % ("交易日", "快照行", "股票覆盖", "覆盖率", "北交所", "缺口与归因"))
        for d in dates:
            got_d, bb = _board_counts(con, master_codes, d)
            cov_d = len([c for c in master_codes if c in got_d])
            pct_d = round(100.0 * cov_d / master_n, 2) if master_n else 0.0
            bse_d = bb.get(SM.BOARD_BSE, [0, 0])
            n_rows = con.execute("SELECT COUNT(*) FROM stock_daily_snapshot "
                                 "WHERE trade_date=?", (d,)).fetchone()[0]
            gaps = []
            for b in sorted(bb, key=lambda x: bb[x][0] - bb[x][1]):
                have, total = bb[b]
                if have == total:
                    continue
                miss_b = [c for c in master_codes
                          if c not in got_d and board_of_code[c] == b]
                gaps.append("%s -%d（%s）" % (b, len(miss_b), _reason_for_date(d, miss_b)))
            print("    %-11s %-6d %-10s %-8s %-10s %s"
                  % (d, n_rows, "%d/%d" % (cov_d, master_n), "%.2f%%" % pct_d,
                     "%d/%d" % (bse_d[0], bse_d[1]),
                     "；".join(gaps) if gaps else "—（无缺口）"))
        if any(_load_backfill_note(d) for d in dates):
            print("    注：标 no_source 的日子是「源本来就没有那一天的行情」，**不是抓取漏抓**，")
            print("        详见 data/snapshots/backfill_missing_<date>.json；做同比/环比时请跳过或单独标注。")

    # ---------- 2. 估值字段非空比例（治 C4） ----------
    # 分母用"快照里的真股票行"（与 master kind='stock' 交集），
    # 亏损股 PE 为负属正常有效值，只判 IS NOT NULL，不判正负。
    print("\n[2] 估值基本面（治 C4 估值空洞，分母=快照里的股票行）")
    denom = stock_rows_n or snap_n
    if not td:
        print("    （跳过：无快照）")
    elif not _table_exists(con, SM.TABLE):
        print("    （跳过：security_master 缺失，分母不可信）")
    else:
        for col in ("pe_ttm", "pb", "total_mv"):
            row = _q(con, "SELECT COUNT(*) FROM stock_daily_snapshot s "
                          "JOIN %s m ON m.code = s.code "
                          "WHERE s.trade_date = ? AND m.kind = ? AND s.%s IS NOT NULL"
                     % (SM.TABLE, col), (td, SM.KIND_STOCK))
            if isinstance(row, str):
                print("    %-10s 查询失败：%s" % (col, row))
                continue
            n = row[0][0]
            ratio = round(100.0 * n / denom, 2) if denom else 0.0
            print("    %-10s 非空 = %d / %d (%.2f%%)" % (col, n, denom, ratio))
            if denom and ratio < VALUATION_MIN:
                problems.append("估值字段 %s 非空率 %.2f%% < %.0f%%（C4 仍空洞）"
                                % (col, ratio, VALUATION_MIN))

    # ---------- 3. 官方板块成分（治 C3） ----------
    print("\n[3] 官方板块成分 (plate_members, 治 C3)")
    pm_rows = _q(con, "SELECT board, COUNT(*) FROM plate_members GROUP BY board ORDER BY board")
    if isinstance(pm_rows, str):
        print("    表缺失或异常:", pm_rows)
        problems.append("plate_members 表缺失（fetch_plate_members.py 未跑）")
        pm_total = 0
    else:
        pm_total = sum(r[1] for r in pm_rows)
        print("    板块数 = %d，成员行数合计 = %d" % (len(pm_rows), pm_total))
        for b, c in pm_rows:
            flag = "" if c >= 10 else "  ⚠偏少"
            print("      %-8s %d%s" % (b, c, flag))
        if pm_total == 0:
            problems.append("plate_members 为 0 行（fetch_plate_members.py 未跑或全未匹配）")

    # ---------- 4. stock_names 是否被快照刷新 ----------
    print("\n[4] 名称字典刷新 (stock_names)")
    nm_total = con.execute("SELECT COUNT(*) FROM stock_names").fetchone()[0]
    nm_src = con.execute("SELECT COUNT(*) FROM stock_names "
                         "WHERE source IN ('eastmoney_snapshot','tencent_snapshot')"
                         ).fetchone()[0]
    print("    总条目 = %d，已由快照刷新(source=eastmoney/tencent_snapshot) = %d"
          % (nm_total, nm_src))
    if nm_src == 0:
        problems.append("stock_names 未由快照刷新（名称可能陈旧）")

    # ---------- 5. 漏斗官方模式可用性推断 ----------
    print("\n[5] 漏斗 official 模式可用性")
    if pm_total == 0:
        print("    ⚠ plate_members 为空 → 漏斗 mode=official 仍会回退博主挖名毒池（C3 未治）")
        problems.append("漏斗 official 模式仍回退（plate_members 空）")
    else:
        print("    ✓ plate_members 有数据 → 漏斗 official 模式可用官方池，不再回退毒池")

    # ---------- 6. 非股票污染（INFO 级：历史行不许删，只能标清楚） ----------
    print("\n[6] 快照里的非股票（security_master.kind != stock）")
    if not master_n:
        print("    （跳过：主表缺失）")
    elif not td:
        print("    （跳过：无快照）")
    else:
        bad = _q(con, "SELECT s.code, m.name, m.kind FROM stock_daily_snapshot s "
                      "JOIN %s m ON m.code = s.code "
                      "WHERE s.trade_date = ? AND m.kind <> ? "
                      "ORDER BY s.code" % SM.TABLE, (td, SM.KIND_STOCK))
        orphan = _q(con, "SELECT s.code FROM stock_daily_snapshot s "
                         "LEFT JOIN %s m ON m.code = s.code "
                         "WHERE s.trade_date = ? AND m.code IS NULL "
                         "ORDER BY s.code" % SM.TABLE, (td,))
        if isinstance(bad, str) or isinstance(orphan, str):
            print("    查询失败：%s" % (bad if isinstance(bad, str) else orphan))
        else:
            if bad:
                for c, n, k in bad:
                    print("    ⚠ %-8s %-20s kind=%s" % (c, n, k))
                infos.append("快照含 %d 行非股票（可转债/ETF）：这些是本轮之前的历史行，"
                             "现场约定只读/只新增，不删。消费方必须用 "
                             "security_master.stock_codes() 取码，不要直接扫快照表。" % len(bad))
            else:
                print("    ✓ 无（抓取端已只落 kind='stock'）")
            if orphan:
                print("    另有 %d 行不在权威主表（%s）：主表不认识的代码，"
                      "统计时会被 stock_codes() 自动排除。"
                      % (len(orphan), ", ".join(c for (c,) in orphan[:10])))
                infos.append("快照有 %d 行代码不在 security_master（可能已摘牌或非 A 股段），"
                             "不删但需随主表更新归位。" % len(orphan))

    # ---------- 结论 ----------
    con.close()
    print("\n" + "=" * 60)
    if infos:
        print("允许存在的差异（INFO，不影响退出码）：")
        for i in infos:
            print("  · %s" % i)
    if problems:
        print("核验结果：⚠ 仍有 %d 项硬缺口" % len(problems))
        for p in problems:
            print("  - %s" % p)
        print("=" * 60)
        print("退出码 %d（真实缺口）" % EXIT_GAP)
        return EXIT_GAP
    print("核验结果：✓ 达标（覆盖率 %.2f%% ≥ %.1f%%，无其它硬缺口）" % (cov, COVERAGE_MIN))
    print("=" * 60)
    print("退出码 %d（达标）" % EXIT_OK)
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
