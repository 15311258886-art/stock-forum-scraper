#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""股东透视数据抓取（2026-09-25 新增）

目的：给「实盘操作 / 龙虎榜」里的股票补上**法定披露口径且带报告期**的持有人数据，
解决两个问题：
  1. 论坛源（淘股吧/雪球）拿不到"什么时候买的" → 改用权威报告期（季度）替代；
  2. 跨平台"昵称对昵称"印证不成立（实测 0 匹配）→ 改用"事件对权威披露"印证。

抓取内容（东方财富 datacenter 免鉴权接口）：
  A. 十大流通股东  RPT_F10_EH_FREEHOLDERS  —— 含自然人（牛散）、机构、持股比例、报告期
  B. 十大股东      RPT_F10_EH_HOLDERS      —— 总股本口径
  C. 股东户数      RPT_F10_EH_HOLDERNUM    —— 筹码集中度趋势（户数减少=集中）

入库表：
  stock_holders     (symbol, end_date, holder_rank) 唯一
  stock_holder_num  (symbol, end_date) 唯一

用法：
  python holders_fetch.py                 # 抓 positions + lhb_stocks 里出现的全部代码
  python holders_fetch.py --symbols 600519,000001
  python holders_fetch.py --limit 20      # 只抓前 N 只（试跑）
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import ssl
import sys
import time
import urllib.parse
import urllib.request

BASE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(BASE, "viewer", "app.db")
if BASE not in sys.path:
    sys.path.insert(0, BASE)
# 2026-11-01：交易所/板块口径不再在本文件里抄一份段表，统一走 security_master。
from viewer import security_master as SM  # noqa: E402
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
UT = "fa5fd1943c7b386f172d6893dbfba10b"
GAP = 0.35  # 请求间隔（避免被限流）

import netctx  # noqa: E402  —— 统一出网 TLS 口径（整改 #27）
_CTX = netctx.https_context("datacenter.eastmoney.com")  # 整改 #27：默认不校验，按环境变量可开启
_OPENER = urllib.request.build_opener(
    urllib.request.ProxyHandler({}), urllib.request.HTTPSHandler(context=_CTX))


def _num(v):
    if v is None or v == "" or v == "-":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _secucode(code: str) -> str:
    """东财 datacenter 的 secucode 后缀（.SH / .SZ / .BJ）—— 交易所归属委托主表。

    2026-11-01：这里原本是**第三份**代码段前缀表（``code[0] in "659"`` → .SH，
    ``code[:2] in ("43","83","87","88","92")`` 或 ``code[0] in "48"`` → .BJ）。
    和腾讯行情那个「920 段被拼成 sh、返回空、不报错」的 bug 同一类：
    写死段表，新代码段一启用就悄悄错，而错了的表现是**接口返回空**，
    看起来像"这只票没有股东数据"。现在唯一来源是 `security_master.exchange_of()`。

    EXCHANGE_OTHER（既非股票段也非债券/ETF 段，如老三板 400xxx）按深市兜底 ——
    这类代码不在 security_master 宇宙里，本脚本也不会去抓（见 collect_codes）。
    """
    s = str(code).strip().zfill(6)
    ex = SM.exchange_of(s)
    if ex == SM.EXCHANGE_SSE:
        return s + ".SH"
    if ex == SM.EXCHANGE_BSE:
        return s + ".BJ"
    return s + ".SZ"


def _get(report: str, flt: str, sort_columns: str, sort_types: str,
         size: int = 20, retries: int = 2):
    """调东财 datacenter（HSF10 通道），带重试。

    注意：sortTypes 必须与 sortColumns 一一对应且只能是 1/-1。
    2026-09-25 修正：此前把 "END_DATE,HOLDER_RANK" 拆成 sortColumns=END_DATE /
    sortTypes=HOLDER_RANK（非法值），导致接口静默返回空 —— 这也是「十大股东 0 行」的真因。
    """
    params = urllib.parse.urlencode({
        "reportName": report,
        "columns": "ALL",
        "filter": flt,
        "sortColumns": sort_columns,
        "sortTypes": sort_types,
        "pageSize": str(size),
        "pageNumber": "1",
        "source": "HSF10",
        "client": "PC",
    })
    url = "https://datacenter.eastmoney.com/securities/api/data/v1/get?" + params
    last = None
    for i in range(retries + 1):
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": UA,
                              "Referer": "https://emweb.securities.eastmoney.com/"})
            with _OPENER.open(req, timeout=20) as r:
                j = json.loads(r.read().decode("utf-8-sig", "ignore"))
            return (((j or {}).get("result") or {}).get("data") or [])
        except Exception as e:  # noqa: BLE001
            last = e
            if i < retries:
                time.sleep(0.6 * (i + 1))
    print(f"      [warn] {report} 失败: {type(last).__name__}", flush=True)
    return []


def _ensure_tables(con: sqlite3.Connection) -> None:
    con.executescript("""
    CREATE TABLE IF NOT EXISTS stock_holders (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        symbol TEXT NOT NULL,
        symbol_name TEXT DEFAULT '',
        end_date TEXT NOT NULL,          -- 报告期（法定披露口径）
        notice_date TEXT DEFAULT '',     -- 公告日
        holder_rank INTEGER,
        holder_name TEXT,
        holder_type TEXT DEFAULT '',     -- 机构 / 自然人 / 其他
        is_org INTEGER DEFAULT 0,
        hold_num REAL,
        hold_ratio REAL,                 -- 占总股本 %
        free_ratio REAL,                 -- 占流通股 %
        hold_change TEXT DEFAULT '',     -- 增加/减少/不变/新进
        change_ratio REAL,
        scope TEXT DEFAULT 'free',       -- free=十大流通股东, total=十大股东
        disclose_at TEXT DEFAULT '',       -- P1-09 披露时点（盘后/次交易日口径）
        UNIQUE(symbol, end_date, holder_rank, scope)
    );
    CREATE INDEX IF NOT EXISTS idx_sh_symbol ON stock_holders(symbol);
    CREATE INDEX IF NOT EXISTS idx_sh_natural ON stock_holders(holder_type);

    CREATE TABLE IF NOT EXISTS stock_holder_num (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        symbol TEXT NOT NULL,
        symbol_name TEXT DEFAULT '',
        end_date TEXT NOT NULL,
        notice_date TEXT DEFAULT '',
        holder_total_num INTEGER,
        total_num_ratio REAL,            -- 较上期 %
        avg_hold_amt REAL,
        price REAL,
        hold_focus TEXT DEFAULT '',      -- 非常集中/较集中/分散…
        UNIQUE(symbol, end_date)
    );
    CREATE INDEX IF NOT EXISTS idx_shn_symbol ON stock_holder_num(symbol);
    """)


def _classify(name: str, is_org) -> str:
    """判断持有人类型：机构 / 自然人（牛散）/ 其他。"""
    if str(is_org) == "1":
        return "机构"
    nm = (name or "").strip()
    if not nm:
        return "其他"
    # 机构特征词
    for kw in ("公司", "基金", "证券", "银行", "保险", "信托", "投资", "资产",
               "有限合伙", "社保", "年金", "大学", "研究院", "中心", "集团",
               "股东", "计划", "组合", "账户", "委员会", "部", "所", "会"):
        if kw in nm:
            return "机构"
    # 中文姓名（2-4 字纯中文，且不含机构特征词）→ 视为自然人
    if 1 < len(nm) <= 4 and all("\u4e00" <= c <= "\u9fff" for c in nm):
        return "自然人"
    return "其他"


def fetch_one(con: sqlite3.Connection, code: str) -> tuple[int, int]:
    """抓一只股票，返回 (股东行数, 户数行数)。"""
    suc = _secucode(code)
    ymd = time.strftime("%Y-%m-%d %H:%M:%S")

    rows: list[tuple] = []
    # ---- A. 十大流通股东（最新 3 个报告期） ----
    free = _get("RPT_F10_EH_FREEHOLDERS", f'(SECUCODE="{suc}")',
                "END_DATE,HOLDER_RANK", "-1,1", size=30)
    for r in free:
        ed = str(r.get("END_DATE") or "")[:10]
        if not ed:
            continue
        nm = r.get("HOLDER_NAME") or ""
        rows.append((
            code, r.get("SECURITY_NAME_ABBR") or "", ed,
            str(r.get("NOTICE_DATE") or "")[:10],
            r.get("HOLDER_RANK"), nm, _classify(nm, r.get("IS_HOLDORG")),
            1 if str(r.get("IS_HOLDORG")) == "1" else 0,
            _num(r.get("HOLD_NUM")), _num(r.get("HOLD_RATIO")),
            _num(r.get("FREE_HOLDNUM_RATIO")),
            r.get("HOLD_NUM_CHANGE") or "", _num(r.get("CHANGE_RATIO")), "free",
        ))

    # ---- B. 十大股东（总股本口径） ----
    tot = _get("RPT_F10_EH_HOLDERS", f'(SECUCODE="{suc}")',
               "END_DATE,HOLDER_RANK", "-1,1", size=30)
    for r in tot:
        ed = str(r.get("END_DATE") or "")[:10]
        if not ed:
            continue
        nm = r.get("HOLDER_NAME") or ""
        rows.append((
            code, r.get("SECURITY_NAME_ABBR") or "", ed, "",
            r.get("HOLDER_RANK"), nm, _classify(nm, r.get("IS_HOLDORG")),
            1 if str(r.get("IS_HOLDORG")) == "1" else 0,
            _num(r.get("HOLD_NUM")), _num(r.get("HOLD_NUM_RATIO")),
            None, r.get("HOLD_NUM_CHANGE") or "",
            _num(r.get("CHANGE_RATIO")), "total",
        ))

    n_h = 0
    if rows:
        cur = con.executemany(
            """INSERT OR REPLACE INTO stock_holders
               (symbol, symbol_name, end_date, notice_date, holder_rank, holder_name,
                holder_type, is_org, hold_num, hold_ratio, free_ratio,
                hold_change, change_ratio, scope)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", rows)
        n_h = cur.rowcount if cur.rowcount and cur.rowcount > 0 else len(rows)

    # ---- C. 股东户数（筹码集中度） ----
    # 注：股东户数报表无 HOLDER_RANK 列，排序只能按 END_DATE
    nums = _get("RPT_F10_EH_HOLDERNUM", f'(SECUCODE="{suc}")', "END_DATE", "-1", size=12)
    nrows = []
    for r in nums:
        ed = str(r.get("END_DATE") or "")[:10]
        if not ed:
            continue
        nrows.append((
            code, r.get("SECURITY_NAME_ABBR") or "", ed,
            str(r.get("NOTICE_DATE") or "")[:10],
            r.get("HOLDER_TOTAL_NUM"), _num(r.get("TOTAL_NUM_RATIO")),
            _num(r.get("AVG_HOLD_AMT")), _num(r.get("PRICE")),
            r.get("HOLD_FOCUS") or "",
        ))
    n_n = 0
    if nrows:
        cur = con.executemany(
            """INSERT OR REPLACE INTO stock_holder_num
               (symbol, symbol_name, end_date, notice_date, holder_total_num,
                total_num_ratio, avg_hold_amt, price, hold_focus)
               VALUES (?,?,?,?,?,?,?,?,?)""", nrows)
        n_n = cur.rowcount if cur.rowcount and cur.rowcount > 0 else len(nrows)

    return (n_h, n_n)


def collect_codes(con: sqlite3.Connection) -> list[str]:
    """待抓代码（2026-10-30 重写）。

    BUG-20261030：原实现只取 positions ∪ lhb_stocks。positions 清理后为 0 行、
    lhb_stocks 只剩 703 只历史龙虎榜票 —— 股东数据永久停在 568 只，全市场
    4400+ 只永远没有股东户数/十大股东。这与 strategy_sim.universe() 是同一类
    bug：主路径依赖了已废弃的表，兜底又太窄。

    新口径：**先补干净宇宙里还没有股东户数记录的票**（覆盖优先），
    再用原口径兜底（保证老票的增量更新不被丢掉）。
    """
    codes: list[str] = []
    # ① 覆盖优先：干净宇宙里缺 stock_holder_num 记录的
    try:
        from viewer.universe_filter import all_ashares, get_exclusion_pool
        ex, _, _ = get_exclusion_pool()
        # 2026-11-01：原实现是 `if c[0] not in "036": continue` —— 又一份代码段白名单。
        # 剔北交所是**产品选择**（先覆盖沪深主战场，北交所股东数据后续再补），
        # 不是"北交所没有股东数据"，更不该靠首字符猜：920 段 2023 年才启用，
        # `c[0] in "036"` 恰好把它挡住，看着对、理由错，换段就漏。
        # 口径唯一来源 = security_master.stock_codes(include_bse=False)。
        allowed = set(SM.stock_codes(con, include_bse=False))
        have = {str(r[0]).strip() for r in
                con.execute("SELECT DISTINCT symbol FROM stock_holder_num")}
        for c in all_ashares():
            c = str(c or "").strip()
            if len(c) != 6 or not c.isdigit():
                continue
            if c not in allowed:
                continue
            if c in ex or c in have:
                continue
            codes.append(c)
    except Exception:  # noqa: BLE001 —— universe_filter/主表不可用时退回旧口径（②）
        pass
    # ② 兜底：原口径（持仓 + 龙虎榜），保证老票继续增量更新
    for sql in ("SELECT DISTINCT symbol FROM positions WHERE symbol IS NOT NULL AND TRIM(symbol)<>''",
                "SELECT DISTINCT security_code FROM lhb_stocks WHERE security_code IS NOT NULL"):
        try:
            for (c,) in con.execute(sql):
                c = str(c).strip()
                if len(c) == 6 and c.isdigit():
                    codes.append(c)
        except sqlite3.Error:
            pass
    return list(dict.fromkeys(codes))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", default="", help="逗号分隔的 6 位代码；留空=自动取库内持仓+龙虎榜")
    ap.add_argument("--limit", type=int, default=0, help="只抓前 N 只（试跑）")
    # 2026-10-30：全宇宙 4400+ 只按 GAP=0.35 串行要 2.6 小时，一次跑完不现实。
    # 默认每轮最多 800 只（约 25 分钟），每天推进一点，约一周补齐；要全量跑用 --max-per-run 0。
    ap.add_argument("--max-per-run", type=int, default=800,
                    help="单轮最多抓多少只（0=不限，全量约 2.6 小时）")
    args = ap.parse_args()

    if not os.path.exists(DB):
        print(f"[err] 数据库不存在: {DB}")
        return 1

    con = sqlite3.connect(DB, timeout=30)
    _ensure_tables(con)
    con.commit()

    if args.symbols.strip():
        codes = [c.strip().zfill(6) for c in args.symbols.split(",") if c.strip()]
    else:
        codes = collect_codes(con)
    if args.limit:
        codes = codes[:args.limit]
    if args.max_per_run and len(codes) > args.max_per_run:
        print(f"[holders] 单轮上限 {args.max_per_run} 只，本轮抓前 {args.max_per_run} 只，"
              f"剩余 {len(codes) - args.max_per_run} 只留待下一轮", flush=True)
        codes = codes[:args.max_per_run]

    print(f"[holders] 待抓 {len(codes)} 只（预计 ~{len(codes)*(GAP*3+0.8):.0f}s）", flush=True)

    tot_h = tot_n = 0
    okc = natural = 0
    for i, code in enumerate(codes, 1):
        nh, nn = fetch_one(con, code)
        tot_h += nh
        tot_n += nn
        if nh or nn:
            okc += 1
        if i % 25 == 0 or i == len(codes):
            con.commit()
            print(f"   ..{i}/{len(codes)}  股东行 {tot_h}  户数行 {tot_n}", flush=True)
        time.sleep(GAP)

    con.commit()
    natural = con.execute(
        "SELECT COUNT(DISTINCT symbol) FROM stock_holders WHERE holder_type='自然人'"
    ).fetchone()[0]
    n_nat_rows = con.execute(
        "SELECT COUNT(*) FROM stock_holders WHERE holder_type='自然人'").fetchone()[0]
    print()
    print(f"[holders] 完成：成功 {okc}/{len(codes)} 只"
          f"｜股东行 {tot_h}｜户数行 {tot_n}｜含自然人股东的股票 {natural} 只（自然人条目 {n_nat_rows}）",
          flush=True)
    con.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
