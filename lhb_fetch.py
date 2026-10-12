# -*- coding: utf-8 -*-
"""龙虎榜抓取（东财免鉴权接口）→ viewer/app.db。

【2026-09-25 v2 扩容说明】
用户反馈「游资龙虎榜点开没内容」，审计后发现两类问题：
  ① 展示层：运行中的查看器是旧进程，没有 /api/lhb 路由（已定位，非代码问题）；
  ② 数据层：原脚本只抓「个股榜单 + 席位明细」两个接口，**漏抓三块**，本次补齐：
     a. lhb_stocks 的上榜后表现（D1/D2/D5/D10/D20/D30 涨幅）——原接口一直返回这些字段但没落库；
     b. **营业部/游资视角**（RPT_OPERATEDEPT_TRADE_DETAILS）——支持「由营业部反查它在买什么」，
        原来只能「由股查席位」，游资追踪的核心视角缺失；
     c. **机构席位统计**（RPT_ORGANIZATION_TRADE_DETAILS）——买/卖次数、净买额、占成交比、后续表现。

【接口实测要点】
- datacenter-web.eastmoney.com 的 filter **不支持**范围写法 (A>='x')(A<='y')（HTTP 400），
  但**支持**等值 (TRADE_DATE='2026-09-24')。因此本脚本按日枚举。
- 个股：RPT_DAILYBILLBOARD_DETAILSNEW      （含 D1/D2/D5/D10/D20/D30_CLOSE_ADJCHRATE）
- 席位：RPT_BILLBOARD_DAILYDETAILSBUY / SELL
- 营业部：RPT_OPERATEDEPT_TRADE_DETAILS    （含 D1/D3/D5/D10/D20/D30_CLOSE_ADJCHRATE）
- 机构：RPT_ORGANIZATION_TRADE_DETAILS     （BUY_TIMES/SELL_TIMES/NET_BUY_AMT/RATIO + D1/D3/D5/D10）
- 均需 ut=7eea3edcaed734bea9cbfc24409ed989；缺 ut 也可，但带上更稳。
- 实测不存在的报表名：RPT_RATEDEPT_STA、RPT_BILLBOARD_TRADEALL（返回 code 9501 报表配置不存在）。

【设计约束】
- **幂等**：按 trade_date 先删后插，可反复运行、可被 auto_update.py 调度。
- **不猜字段**：所有列名均来自 columns=ALL 的实dump，映射见各 fetch_* 函数。
- 上榜后表现（D1/D3/D5）在**最新交易日通常为 None**（走势还没走完），属正常，非故障。

用法：
  .venv\\Scripts\\python.exe lhb_fetch.py            # 最近 10 个自然日
  .venv\\Scripts\\python.exe lhb_fetch.py --days 5
退出码：0 成功（含"无新数据"），1 失败。
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sqlite3
import ssl
import sys
import time
import urllib.request

BASE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(BASE, "viewer", "app.db")
_EM = "https://datacenter-web.eastmoney.com/api/data/v1/get"
_UT = "7eea3edcaed734bea9cbfc24409ed989"
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

_ctx = ssl.create_default_context()
_ctx.check_hostname = False
_ctx.verify_mode = ssl.CERT_NONE
_opener = urllib.request.build_opener(
    urllib.request.ProxyHandler({}),          # 直连，不吃系统代理
    urllib.request.HTTPSHandler(context=_ctx),
)


def _get_json(url: str, timeout: int = 15, retries: int = 2):
    last = None
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": _UA, "Referer": "https://data.eastmoney.com/", "Accept": "*/*"})
            with _opener.open(req, timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8-sig", "ignore"))
        except Exception as e:  # noqa: BLE001
            last = e
            if attempt < retries:
                time.sleep(0.6 * (attempt + 1))
    raise last  # type: ignore[misc]


def _api(report: str, day: str, size: int = 500, sort: str = "SECURITY_CODE",
         sort_type: int = 1) -> str:
    q = (f"reportName={report}&columns=ALL&pageNumber=1&pageSize={size}"
         f"&filter=(TRADE_DATE='{day}')&sortColumns={sort}&sortTypes={sort_type}"
         f"&source=WEB&client=WEB&ut={_UT}")
    return f"{_EM}?{q}"


def _rows(url: str) -> list[dict]:
    j = _get_json(url)
    return (j.get("result") or {}).get("data") or []


# ---------------------------------------------------------------- 建表/补列
_STOCK_EXTRA_COLS = (
    "d1_chg", "d2_chg", "d5_chg", "d10_chg", "d20_chg", "d30_chg",
    "buy_seat", "sell_seat", "sum_buy_amt", "sum_sell_amt", "net_bs_amt",
    "deal_amount_ratio",
)

_DDL_DEPTS = """
CREATE TABLE IF NOT EXISTS lhb_depts (
    trade_date     TEXT NOT NULL,
    operate_code   TEXT NOT NULL DEFAULT '',
    operate_name   TEXT NOT NULL DEFAULT '',
    org_name       TEXT NOT NULL DEFAULT '',
    security_code  TEXT NOT NULL DEFAULT '',
    security_name  TEXT NOT NULL DEFAULT '',
    act_buy        REAL,
    act_sell       REAL,
    net_amt        REAL,
    reason         TEXT NOT NULL DEFAULT '',
    d1_chg         REAL,
    d3_chg         REAL,
    d5_chg         REAL,
    d10_chg        REAL,
    d20_chg        REAL,
    d30_chg        REAL,
    PRIMARY KEY (trade_date, operate_code, security_code, reason)
)"""

_DDL_INST = """
CREATE TABLE IF NOT EXISTS lhb_inst (
    trade_date     TEXT NOT NULL,
    security_code  TEXT NOT NULL DEFAULT '',
    security_name  TEXT NOT NULL DEFAULT '',
    buy_times      INTEGER,
    sell_times     INTEGER,
    buy_amt        REAL,
    sell_amt       REAL,
    net_buy_amt    REAL,
    ratio          REAL,
    reason         TEXT NOT NULL DEFAULT '',
    d1_chg         REAL,
    d3_chg         REAL,
    d5_chg         REAL,
    d10_chg        REAL,
    PRIMARY KEY (trade_date, security_code, reason)
)"""


def ensure_schema(con: sqlite3.Connection) -> list[str]:
    """幂等补齐列/表。返回本次实际新增的动作描述（供日志）。"""
    acted = []
    cols = {r[1] for r in con.execute("PRAGMA table_info(lhb_stocks)")}
    for c in _STOCK_EXTRA_COLS:
        if c not in cols:
            con.execute(f"ALTER TABLE lhb_stocks ADD COLUMN {c} REAL")
            acted.append(f"lhb_stocks.+{c}")
    for name, ddl in (("lhb_depts", _DDL_DEPTS), ("lhb_inst", _DDL_INST)):
        exists = con.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
        ).fetchone()
        if not exists:
            con.execute(ddl)
            acted.append(f"create {name}")
    if acted:
        con.commit()
    return acted


# ---------------------------------------------------------------- 抓取
def fetch_stocks(day: str) -> list[dict]:
    """个股榜单（含上榜后表现 D1/D2/D5/D10/D20/D30）。"""
    out = []
    for r in _rows(_api("RPT_DAILYBILLBOARD_DETAILSNEW", day)):
        code = r.get("SECURITY_CODE")
        if not code:
            continue
        out.append({
            "trade_date": (r.get("TRADE_DATE") or "")[:10],
            "security_code": code,
            "security_name": r.get("SECURITY_NAME_ABBR") or "",
            "market": r.get("MARKET") or "",
            "close_price": r.get("CLOSE_PRICE"),
            "change_rate": r.get("CHANGE_RATE"),
            "turnoverrate": r.get("TURNOVERRATE"),
            "buy_amt": r.get("BILLBOARD_BUY_AMT"),
            "sell_amt": r.get("BILLBOARD_SELL_AMT"),
            "net_amt": r.get("BILLBOARD_NET_AMT"),
            "deal_amt": r.get("BILLBOARD_DEAL_AMT"),
            "deal_net_ratio": r.get("DEAL_NET_RATIO"),
            "free_market_cap": r.get("FREE_MARKET_CAP"),
            "reason": r.get("EXPLANATION") or "",
            "explain": r.get("EXPLAIN") or "",
            # 2026-09-25 新增：上榜后表现（跟不跟得看这几个数）
            "d1_chg": r.get("D1_CLOSE_ADJCHRATE"),
            "d2_chg": r.get("D2_CLOSE_ADJCHRATE"),
            "d5_chg": r.get("D5_CLOSE_ADJCHRATE"),
            "d10_chg": r.get("D10_CLOSE_ADJCHRATE"),
            "d20_chg": r.get("D20_CLOSE_ADJCHRATE"),
            "d30_chg": r.get("D30_CLOSE_ADJCHRATE"),
            "buy_seat": r.get("BUY_SEAT") or "",
            "sell_seat": r.get("SELL_SEAT") or "",
            "sum_buy_amt": r.get("SUM_BUY_AMT"),
            "sum_sell_amt": r.get("SUM_SELL_AMT"),
            "net_bs_amt": r.get("NET_BS_AMT"),
            "deal_amount_ratio": r.get("DEAL_AMOUNT_RATIO"),
        })
    return out


def fetch_seats(day: str) -> list[dict]:
    """筹码席位明细：买入前5 / 卖出前5（含该席位3日上涨概率）。"""
    out = []
    for report, side in (("RPT_BILLBOARD_DAILYDETAILSBUY", "BUY"),
                         ("RPT_BILLBOARD_DAILYDETAILSSELL", "SELL")):
        for r in _rows(_api(report, day, size=2000)):
            code = r.get("SECURITY_CODE")
            if not code:
                continue
            out.append({
                "trade_date": (r.get("TRADE_DATE") or "")[:10],
                "security_code": code,
                "operate_code": str(r.get("OPERATEDEPT_CODE") or ""),
                "operate_name": r.get("OPERATEDEPT_NAME") or "",
                "side": side,
                "buy": r.get("BUY"),
                "sell": r.get("SELL"),
                "net": r.get("NET"),
                "rise_prob_3d": r.get("RISE_PROBABILITY_3DAY"),
            })
    return out


def fetch_depts(day: str) -> list[dict]:
    """营业部视角：某营业部当日上榜了哪些股、买卖额、上榜原因、上榜后表现。

    与 fetch_seats 的区别（两者不可互替）：
      - seats 是「以股票为轴」——某股的前5买卖席位；
      - depts 是「以营业部为轴」——某游资席位在扫哪些股，且带 D1/D3/D5 战绩。
    """
    out = []
    for r in _rows(_api("RPT_OPERATEDEPT_TRADE_DETAILS", day, size=2000,
                        sort="NET_AMT", sort_type=-1)):
        code = r.get("SECURITY_CODE")
        if not code:
            continue
        out.append({
            "trade_date": (r.get("TRADE_DATE") or "")[:10],
            "operate_code": str(r.get("OPERATEDEPT_CODE") or ""),
            "operate_name": r.get("OPERATEDEPT_NAME") or "",
            "org_name": r.get("ORG_NAME_ABBR") or "",
            "security_code": code,
            "security_name": r.get("SECURITY_NAME_ABBR") or "",
            "act_buy": r.get("ACT_BUY"),
            "act_sell": r.get("ACT_SELL"),
            "net_amt": r.get("NET_AMT"),
            "reason": r.get("EXPLANATION") or "",
            "d1_chg": r.get("D1_CLOSE_ADJCHRATE"),
            "d3_chg": r.get("D3_CLOSE_ADJCHRATE"),
            "d5_chg": r.get("D5_CLOSE_ADJCHRATE"),
            "d10_chg": r.get("D10_CLOSE_ADJCHRATE"),
            "d20_chg": r.get("D20_CLOSE_ADJCHRATE"),
            "d30_chg": r.get("D30_CLOSE_ADJCHRATE"),
        })
    return out


def fetch_inst(day: str) -> list[dict]:
    """机构席位统计：机构当日在哪些股上买/卖了几次、净买额、占成交比、后续表现。"""
    out = []
    for r in _rows(_api("RPT_ORGANIZATION_TRADE_DETAILS", day, size=1000,
                        sort="NET_BUY_AMT", sort_type=-1)):
        code = r.get("SECURITY_CODE")
        if not code:
            continue
        out.append({
            "trade_date": (r.get("TRADE_DATE") or "")[:10],
            "security_code": code,
            "security_name": r.get("SECURITY_NAME_ABBR") or "",
            "buy_times": r.get("BUY_TIMES"),
            "sell_times": r.get("SELL_TIMES"),
            "buy_amt": r.get("BUY_AMT"),
            "sell_amt": r.get("SELL_AMT"),
            "net_buy_amt": r.get("NET_BUY_AMT"),
            "ratio": r.get("RATIO"),
            "reason": r.get("EXPLANATION") or "",
            "d1_chg": r.get("D1_CLOSE_ADJCHRATE"),
            "d3_chg": r.get("D3_CLOSE_ADJCHRATE"),
            "d5_chg": r.get("D5_CLOSE_ADJCHRATE"),
            "d10_chg": r.get("D10_CLOSE_ADJCHRATE"),
        })
    return out


# ---------------------------------------------------------------- 写入
def _insert(con: sqlite3.Connection, table: str, rows: list[dict], ignore: bool = False) -> int:
    """写入并返回**实际生效**行数（OR IGNORE 时按 total_changes 差值算）。"""
    if not rows:
        return 0
    before = con.total_changes
    cols = list(rows[0].keys())
    ph = ",".join("?" for _ in cols)
    verb = "INSERT OR IGNORE" if ignore else "INSERT"
    sql = f"{verb} INTO {table} ({','.join(cols)}) VALUES ({ph})"
    con.executemany(sql, [[r[c] for c in cols] for r in rows])
    return con.total_changes - before


def main() -> int:
    ap = argparse.ArgumentParser(description="龙虎榜抓取（东财，v2 四维度）")
    ap.add_argument("--days", type=int, default=10, help="向前枚举的自然日数（默认 10）")
    ap.add_argument("--db", default=DB)
    args = ap.parse_args()

    today = dt.date.today()
    days = [(today - dt.timedelta(days=i)).isoformat() for i in range(args.days)]
    print(f"[lhb] 目标日期范围: {days[-1]} ~ {days[0]}（共 {len(days)} 天）", flush=True)

    con = sqlite3.connect(args.db, timeout=30)
    acted = ensure_schema(con)
    print(f"[lhb] 结构检查：{('新增/补齐 ' + ', '.join(acted)) if acted else '已是最新，无需变更'}", flush=True)

    tot = {"stocks": 0, "seats": 0, "depts": 0, "inst": 0}
    hit_days = []
    mx = None
    try:
        for d in days:
            try:
                stocks = fetch_stocks(d)
            except Exception as e:  # noqa: BLE001
                print(f"  {d}: 个股接口失败 {type(e).__name__}", flush=True)
                continue
            if not stocks:
                continue

            parts = {}
            for key, fn in (("seats", fetch_seats), ("depts", fetch_depts), ("inst", fetch_inst)):
                try:
                    parts[key] = fn(d)
                except Exception as e:  # noqa: BLE001
                    print(f"  {d}: {key} 接口失败 {type(e).__name__}: {str(e)[:60]}", flush=True)
                    parts[key] = []

            # 幂等：先删当日，再插
            for tbl in ("lhb_stocks", "lhb_seats", "lhb_depts", "lhb_inst"):
                con.execute(f"DELETE FROM {tbl} WHERE trade_date=?", (d,))
            n_s = _insert(con, "lhb_stocks", stocks)
            n_t = _insert(con, "lhb_seats", parts["seats"], ignore=True)
            n_d = _insert(con, "lhb_depts", parts["depts"], ignore=True)
            n_i = _insert(con, "lhb_inst", parts["inst"], ignore=True)
            con.commit()

            tot["stocks"] += n_s; tot["seats"] += n_t
            tot["depts"] += n_d; tot["inst"] += n_i
            hit_days.append(f"{d}(股{n_s}/席{n_t}/部{n_d}/机{n_i})")
            print(f"  {d}: 个股 {n_s} · 席位 {n_t} · 营业部 {n_d} · 机构 {n_i}", flush=True)

        mx = con.execute("SELECT MAX(trade_date) FROM lhb_stocks").fetchone()[0]
    finally:
        con.close()

    print(f"[lhb] 完成：写入 {json.dumps(tot, ensure_ascii=False)}；命中交易日 {len(hit_days)} 个",
          flush=True)
    for h in hit_days:
        print(f"      {h}", flush=True)
    print(f"[lhb] 库内最新交易日 = {mx}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
