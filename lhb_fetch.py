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

# 2026-10-30 D33：东财「该日没有数据」是 code=9201（success=False），必须与真失败区分开。
EMPTY_OK_CODE = 9201

import netctx  # noqa: E402  —— 统一出网 TLS 口径（整改 #27）
_ctx = netctx.https_context("datacenter-web.eastmoney.com")  # 整改 #27：默认不校验，按环境变量可开启
_opener = urllib.request.build_opener(
    urllib.request.ProxyHandler({}),          # 直连，不吃系统代理
    urllib.request.HTTPSHandler(context=_ctx),
)


class LhbApiError(RuntimeError):
    """东财接口层拒绝（报表改名 / 限流 / 结构变更）。

    与「该交易日确实没有记录」（code=9201，合法空结果）**必须分开**：
    前者要重试/告警，后者是正常的日子。只在 `_rows(..., strict=True)` 里抛。
    """


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


def _rows(url: str, strict: bool = False) -> list[dict]:
    """取 data 数组。

    2026-10-30 D33：`strict=True` 时把「接口层拒绝」与「该日确实没有数据」分开 —— 这两件事
    在返回体里长得**几乎一样**（都是 `result: null`、rows 0），但后果完全不同：
      · code=**9201**「返回数据为空」 = 该交易日确实没有这类记录（合法空结果，**不是失败**）
      · code=**9501**「报表配置不存在」 = 东财改了报表名/结构（**是真失败**，且正是「明细表
        悄悄缺一天、主榜却正常」的典型成因）
    实测（2026-10-30）：非交易日 2026-10-25 → `success=False, code=9201, message='返回数据为空'`；
    报表名打错 → `success=False, code=9501, message='报表配置不存在,...'`；
    正常有数据 → `success=True, code=0, result={...}`。
    所以判据必须是 **code**，不是「抓到几行」。
    """
    j = _get_json(url)
    if strict:
        code = j.get("code") if isinstance(j, dict) else None
        if code not in (0, EMPTY_OK_CODE):
            raise LhbApiError("接口拒绝 code=%s message=%s"
                              % (code, str((j or {}).get("message"))[:80]))
        # code=0 却拿不到 result 字典 → 结构变了，同样按失败处理（保守：宁可真失败可重试，
        # 也不要静默写 0 行）
        if code == 0 and not isinstance((j or {}).get("result"), dict):
            raise LhbApiError("返回体结构异常：code=0 但 result 不是字典")
    return ((j or {}).get("result") or {}).get("data") or []


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


def fetch_seats(day: str, strict: bool = False) -> list[dict]:
    """筹码席位明细：买入前5 / 卖出前5（含该席位3日上涨概率）。

    `strict=True` 时接口层拒绝（code∉{0,9201}）会抛 `LhbApiError`，供 main() 判失败；
    默认 False 保持原行为（供运维脚本按需宽松调用）。
    """
    out = []
    for report, side in (("RPT_BILLBOARD_DAILYDETAILSBUY", "BUY"),
                         ("RPT_BILLBOARD_DAILYDETAILSSELL", "SELL")):
        for r in _rows(_api(report, day, size=2000), strict=strict):
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


def fetch_depts(day: str, strict: bool = False) -> list[dict]:
    """营业部视角：某营业部当日上榜了哪些股、买卖额、上榜原因、上榜后表现。

    与 fetch_seats 的区别（两者不可互替）：
      - seats 是「以股票为轴」——某股的前5买卖席位；
      - depts 是「以营业部为轴」——某游资席位在扫哪些股，且带 D1/D3/D5 战绩。
    `strict` 语义见 fetch_seats。
    """
    out = []
    for r in _rows(_api("RPT_OPERATEDEPT_TRADE_DETAILS", day, size=2000,
                        sort="NET_AMT", sort_type=-1), strict=strict):
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


def fetch_inst(day: str, strict: bool = False) -> list[dict]:
    """机构席位统计：机构当日在哪些股上买/卖了几次、净买额、占成交比、后续表现。

    `strict` 语义见 fetch_seats。注意「该日无机构上榜」是**合法空结果**（code=9201），
    strict 下不会抛异常，仍返回 [] —— 空结果不等于失败。
    """
    out = []
    for r in _rows(_api("RPT_ORGANIZATION_TRADE_DETAILS", day, size=1000,
                        sort="NET_BUY_AMT", sort_type=-1), strict=strict):
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
    # 2026-10-30 D33：明细接口失败必须让本进程**非 0 退出**，否则 lhb_daily 会判 ok=true
    # 并记为"今天跑过了"，当天再也不重试 → 那张表永久缺一天（2026-10-29 就是这么丢的）。
    # 判据：只认「接口层异常」（网络/解析/code∉{0,9201}），**不认「抓回 0 行」**——
    # 该日确实没有机构上榜是合法结果，不能当失败（否则天天误报）。
    detail_failed = []          # [(day, table, err)]
    stocks_failed = []          # 主榜整体失败的日期（本轮只告警，不改 rc，见下方说明）
    try:
        for d in days:
            try:
                stocks = fetch_stocks(d)
            except Exception as e:  # noqa: BLE001
                print(f"  {d}: 个股接口失败 {type(e).__name__}: {str(e)[:60]}", flush=True)
                stocks_failed.append(d)
                continue
            if not stocks:
                continue

            parts = {}
            failed_today = set()
            for key, fn in (("seats", fetch_seats), ("depts", fetch_depts), ("inst", fetch_inst)):
                try:
                    parts[key] = fn(d, strict=True)
                except Exception as e:  # noqa: BLE001
                    print(f"  {d}: {key} 接口失败 {type(e).__name__}: {str(e)[:80]}"
                          f" ← 视为失败（不写空数据）", flush=True)
                    parts[key] = []
                    failed_today.add(key)
                    detail_failed.append((d, key, f"{type(e).__name__}: {str(e)[:80]}"))
                else:
                    if not parts[key]:
                        # 合法空结果：接口正常、该日确实没有这类记录
                        print(f"  {d}: {key} 抓回 0 行（接口正常，该日无记录；不算失败）",
                              flush=True)

            # 幂等：先删当日，再插。
            # 2026-10-30 D33：**只重建抓成功的表** —— 抓失败还照删，等于用空数据覆盖好数据
            # （--days 8 每轮都会重写最近 8 天，一次抖动就能抹掉 8 天明细）。
            con.execute("DELETE FROM lhb_stocks WHERE trade_date=?", (d,))
            n_s = _insert(con, "lhb_stocks", stocks)
            n = {"seats": 0, "depts": 0, "inst": 0}
            for tbl, key in (("lhb_seats", "seats"), ("lhb_depts", "depts"), ("lhb_inst", "inst")):
                if key in failed_today:
                    continue                    # 保留库里原有明细，绝不清空
                con.execute(f"DELETE FROM {tbl} WHERE trade_date=?", (d,))
                n[key] = _insert(con, tbl, parts[key], ignore=True)
            con.commit()

            tot["stocks"] += n_s; tot["seats"] += n["seats"]
            tot["depts"] += n["depts"]; tot["inst"] += n["inst"]
            hit_days.append(f"{d}(股{n_s}/席{n['seats']}/部{n['depts']}/机{n['inst']})")
            print(f"  {d}: 个股 {n_s} · 席位 {n['seats']} · 营业部 {n['depts']} · 机构 {n['inst']}",
                  flush=True)

        mx = con.execute("SELECT MAX(trade_date) FROM lhb_stocks").fetchone()[0]
    finally:
        con.close()

    print(f"[lhb] 完成：写入 {json.dumps(tot, ensure_ascii=False)}；命中交易日 {len(hit_days)} 个",
          flush=True)
    for h in hit_days:
        print(f"      {h}", flush=True)
    print(f"[lhb] 库内最新交易日 = {mx}", flush=True)

    if stocks_failed:
        # 说明：主榜失败本轮只告警、不改退出码。理由：--days 8 里任一天的主榜抖动都会让整轮
        # 变红，进而挡住 last_date 记账、白烧 MAX_TRIES。主榜另有 tools/backfill_lhb.py 兜底，
        # 且它就是"回测唯一需要的表"，值得单独决策（见 DECISIONS.md D33 遗留）。
        print(f"[lhb] ⚠️ 主榜接口失败 {len(stocks_failed)} 天（{'、'.join(stocks_failed)}）"
              f"—— 本轮不判失败，仅告警；如需补齐请用 tools/backfill_lhb.py", flush=True)
    if detail_failed:
        print(f"[lhb] ❌ 明细接口失败 {len(detail_failed)} 处 → 退出码 1（让 lhb_daily 重试）：",
              flush=True)
        for d, t, err in detail_failed:
            print(f"      {d} / {t}: {err}", flush=True)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
