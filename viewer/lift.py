# -*- coding: utf-8 -*-
"""限售解禁风险 —— 把「股东马上能卖了」变成可提前看到的日期。

2026-10-30 进哥提的：
  「尤其这种上新的股票，时间不是特别长的，很多股东手上的股票已经临近释放期了。
   股东到了释放期就可以把股票变卖掉，他肯定卖，就容易出现砸盘。可以做个风险提示，
   比如再过三个月股东的这个股票就可以解锁可以变卖了。」

**他的直觉在数据上成立，但时点被他推早了两个多月。这是他必须知道的一点。**


实证（tools/probe_lift_signal.py + tools/probe_lift_timing.py）
  样本：2025-08-18 ~ 2026-10-21，283 个交易日，1588 个解禁事件（去重后）。
  口径：日收益 − 同日全市场均值（demean），按「相对解禁日的交易偏移」聚合。

  · 抛压集中在**解禁前最后 20 个交易日**，前 40 日反而还是正的：
        解禁前 60~41 日   +0.44%（t=+0.51）   ← 提前跑会踏空
        解禁前 40~21 日   −0.94%（t=−1.16）
        解禁前 20~11 日   −1.88%（t=−3.36）   ← 主要跌幅在这
        解禁前 10~1 日    −1.39%（t=−2.81）
        解禁后 1~5 日     +0.31%              ← 靴子落地后反弹
        解禁后 6~20 日    +0.75%
    累积曲线：−40 日 +0.76% → −25 日转负 → 解禁日 −1.68%（谷底）→ +5 日 −1.10%。
    所以「提前三个月提示」是错的：那时还在超额上涨，提前躲会踏空。

  · 不是所有解禁都砸盘，只看**首发原股东**（PE/VC、创始团队，成本≈0）：
        首发原股东   前20日 −2.80%（t=−4.66）   ← 真信号
        股权激励     前20日 −0.96%（t=−1.60）
        定增解禁     前20日 −0.55%（t=−0.63）   ← 机构还要做下一轮，动机弱
        其他         前20日 −0.56%（t=−0.66）
    进哥说的「上新时间不长」也在这里被验证：首发解禁本身就在上市 12 个月后，
    这批事件天然全是次新股。

  · 按占流通比有**剂量反应**（因果证据，不是相关）：
        <1%    −0.40%   >30%   −2.53%（t=−3.43）   比例越高跌越多
    所以「解禁市值大」不是指标 —— 市值≥20 亿那档只有 −0.81%（t=−0.89，不显著），
    大票有机构托底、且预期早被消化。**看比例，不看绝对金额。**

  · 最终口径（组合后才是最强的一档）：
        首发原股东 & 占流通≥3%    n=423   前20日 **−3.36%**  t=**−5.09**
    时间对半切：前半段 −2.12%(t=−2.47)、后半段 −4.37%(t=−3.61)，两段都显著负。

  · 安慰剂检验（必须做，否则分不清是解禁还是这批票本来就在跌）：
    随机抽同样数量的 (code, date)，解禁前 20 日累积超额 ≈ −0.01%，
    事件组 −1.46%。差值就是解禁本身的贡献。


**为什么表里存事件、不存风险分**
  `lift_event` 是**公司公告的事实**（哪天解禁多少股），跟 avoid.py 里「不沉淀」的原则
  不冲突 —— 沉淀的是事实，不是结论。风险等级、剩几个交易日、该不该躲，全部在调用时
  按当日交易日历现算。这样即使表停更一周，已入库的事件也不会给出错误的「还剩几天」。


**不做的事（避免重复踩坑）**
  · 不做「解禁后避坑」：解禁后 T+5 是 +0.46%（t=+2.04），正向。
  · 不按解禁市值排风险：不显著（见上）。
  · 不把「定增解禁」当风险：t=−0.63，是噪声。
  · 不提前 60 个交易日就亮红灯：前 40 日还是正超额，那时亮灯会让用户错过上涨。
    60 交易日内的批次只进 `watch`（纯信息，不参与任何风险分）。
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
import urllib.parse
import urllib.request
from bisect import bisect_left
from datetime import date as _date

DB_PATH = os.environ.get("VIEWER_DB") or os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "app.db")

API = "https://datacenter-web.eastmoney.com/api/data/v1/get"
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")

# ---- 判档阈值（全部来自上面的实证，改动前请重跑探针）----
TD_HIGH = 20          # 解禁前 20 个交易日：主要跌幅区间（−1.88%/t=−3.36 + −1.39%/t=−2.81）
TD_MID = 40           # 20~40 个交易日：跌幅不显著（−0.94%/t=−1.16），只作「准备」提示
TD_WATCH = 60         # 40~60 个交易日：纯信息，前 40 日累积还是正的，不参与风险分
RATIO_STRONG = 0.03   # 占流通 ≥3%：组合口径的最优切点（n=423, −3.36%, t=−5.09）
RATIO_MID = 0.10      # 占流通 ≥10%：不限类型也有 −2.19%(t=−4.01)
NEW_SHARE_TYPE = "首发原股东"
FETCH_AHEAD_DAYS = 400   # 往前拉 400 天（解禁计划通常提前 1 年公告）

_SCHEMA = """
CREATE TABLE IF NOT EXISTS lift_event (
    code        TEXT NOT NULL,
    name        TEXT,
    free_date   TEXT NOT NULL,      -- 解禁日 YYYY-MM-DD
    share_type  TEXT,               -- 原始解禁类型
    type_group  TEXT,               -- 粗分类：定增解禁 / 首发原股东 / 股权激励 / 其他
    n_holder    INTEGER,            -- 本批解禁股东数
    free_shares REAL,               -- 本次解禁股数（万股）
    lift_cap    REAL,               -- 解禁市值（万元）
    free_ratio  REAL,               -- 占流通股比例（小数，0.05 = 5%）
    total_ratio REAL,               -- 占总股本比例
    is_new      INTEGER,
    fetched_at  TEXT,
    PRIMARY KEY (code, free_date, share_type)
);
CREATE INDEX IF NOT EXISTS ix_lift_date ON lift_event(free_date);
CREATE INDEX IF NOT EXISTS ix_lift_code ON lift_event(code);
"""


def ensure_schema(con: sqlite3.Connection) -> None:
    con.executescript(_SCHEMA)
    con.commit()


def _type_group(t: str) -> str:
    t = t or ""
    if "定向增发" in t:
        return "定增解禁"
    if "股权激励" in t or "激励" in t:
        return "股权激励"
    if "首发" in t or "原股东" in t or "战略配售" in t:
        return "首发原股东"
    return "其他"


def _fetch(start: str, end: str, page_size: int = 500) -> list:
    out, page = [], 1
    while True:
        q = urllib.parse.urlencode({
            "reportName": "RPT_LIFT_STAGE", "columns": "ALL",
            "pageNumber": page, "pageSize": page_size,
            "sortColumns": "FREE_DATE,SECURITY_CODE", "sortTypes": "1,1",
            "filter": "(FREE_DATE>='%s')(FREE_DATE<='%s')" % (start, end),
            "source": "WEB", "client": "WEB",
        })
        req = urllib.request.Request(API + "?" + q, headers={
            "User-Agent": _UA, "Referer": "https://data.eastmoney.com/"})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                js = json.loads(r.read().decode("utf-8"))
        except Exception:  # noqa: BLE001 —— 单页失败不中断，已取到的照用
            break
        res = js.get("result") or {}
        data = res.get("data") or []
        if not data:
            break
        out.extend(data)
        if page >= (res.get("pages") or 1):
            break
        page += 1
        time.sleep(0.2)
    return out


def refresh(con: sqlite3.Connection | None = None, days_back: int = 7,
            days_ahead: int = FETCH_AHEAD_DAYS) -> dict:
    """增量刷新解禁事件。

    只拉 [今天−7, 今天+400]，不碰更早的历史（历史用于实证，重拉浪费时间）。
    往前留 7 天是为了吃掉「刚过期但仍在窗口内」的事件变更（延期/取消）。
    """
    own = con is None
    if own:
        con = sqlite3.connect(DB_PATH)
    ensure_schema(con)
    today = time.strftime("%Y-%m-%d")
    start = time.strftime("%Y-%m-%d", time.localtime(time.time() - days_back * 86400))
    end = time.strftime("%Y-%m-%d", time.localtime(time.time() + days_ahead * 86400))
    rows = _fetch(start, end)
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    n = 0
    for r in rows:
        code = r.get("SECURITY_CODE")
        fd = (r.get("FREE_DATE") or "")[:10]
        if not (code and fd):
            continue
        try:
            con.execute(
                "INSERT OR REPLACE INTO lift_event"
                " (code,name,free_date,share_type,type_group,n_holder,free_shares,"
                "  lift_cap,free_ratio,total_ratio,is_new,fetched_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (code, r.get("SECURITY_NAME_ABBR"), fd,
                 r.get("FREE_SHARES_TYPE"), _type_group(r.get("FREE_SHARES_TYPE")),
                 r.get("BATCH_HOLDER_NUM"), r.get("CURRENT_FREE_SHARES"),
                 r.get("LIFT_MARKET_CAP"), r.get("FREE_RATIO"),
                 r.get("TOTAL_RATIO") or r.get("TOTALSHARES_RATIO"),
                 1 if str(r.get("IS_NEW")) == "1" else 0, now))
            n += 1
        except sqlite3.Error:
            continue
    con.commit()
    if own:
        con.close()
    return {"ok": True, "fetched": len(rows), "saved": n,
            "range": [start, end], "as_of": now}


# 未来交易日没有数据源，只能外推。A 股休市 = 周末 + 法定节假日，
# 周末用 weekday() 判，节假日必须硬编码。这里是 2026-11 ~ 2027-12 的停市日，
# 依据国务院放假安排推算（春节/端午/中秋按农历折公历）。**每年 12 月要按官方
# 通知校准一次**——错 1~2 天不影响 20/40/60 的判档，但长假整段算错会差 5 天以上，
# 所以春节、国庆这种必须列全。
HOLIDAYS = frozenset((
    # 2026 年 11~12 月：无长假
    # 2027 年
    "2027-01-01", "2027-01-02", "2027-01-03",                       # 元旦
    "2027-02-05", "2027-02-06", "2027-02-07", "2027-02-08",
    "2027-02-09", "2027-02-10", "2027-02-11",                       # 春节
    "2027-04-03", "2027-04-04", "2027-04-05",                       # 清明
    "2027-05-01", "2027-05-02", "2027-05-03", "2027-05-04", "2027-05-05",   # 劳动节
    "2027-06-09", "2027-06-10", "2027-06-11",                       # 端午
    "2027-09-15", "2027-09-16", "2027-09-17",                       # 中秋
    "2027-10-01", "2027-10-02", "2027-10-03", "2027-10-04",
    "2027-10-05", "2027-10-06", "2027-10-07",                       # 国庆
))


def _calendar(con: sqlite3.Connection, as_of: str, ahead_days: int = 460) -> list:
    """交易日历 = 库里有 K 线的历史日 + 外推的未来日。

    为什么必须补未来：K 线表只到昨天（甚至前天），而解禁日在未来几十天之后。
    如果只拿 `date>=as_of` 查表，as_of 当天及之后全是空的，所有 td 都会算成 0 或直接
    查不到 —— 这是这个功能最容易踩的坑，不是「差不多够用」的问题。
    """
    hist = [r[0] for r in con.execute(
        "SELECT DISTINCT date FROM stock_indicators_daily WHERE date<=? ORDER BY date",
        (as_of,))]
    last = hist[-1] if hist else as_of
    fut = []
    d = _date.fromisoformat(max(last, as_of))
    end = _date.fromisoformat(as_of).toordinal() + ahead_days
    while d.toordinal() <= end:
        s = d.isoformat()
        if s > last and d.weekday() < 5 and s not in HOLIDAYS:
            fut.append(s)
        d = _date.fromordinal(d.toordinal() + 1)
    return hist + fut


def run(con: sqlite3.Connection | None = None) -> dict:
    """定时任务入口（tools/sched_job.py 调用）。"""
    return refresh(con)


def _level_of(tg: str, ratio: float, td: int) -> tuple:
    """(level, 实证依据)。td = 距解禁的交易日数，已解禁为负。

    **亮灯只给实证显著的窗口**（td<=20）。20~60 日一律 watch（灰字），
    因为那一段实测 −0.94%、t=−1.16，不显著 —— 亮灯就是噪声。
    这一条是本模块最容易被人「好心改坏」的地方：觉得「40 天后要解禁了，早点点灯
    更负责」，实际是让用户在一个还在超额上涨的阶段（前 40 日 +0.44%）提前离场。
    """
    if td < 0:
        return None, ""
    is_new = tg == NEW_SHARE_TYPE
    if is_new and ratio >= RATIO_STRONG and td <= TD_HIGH:
        return "high", "首发原股东解禁占流通%.1f%%，同类实证前20交易日跑输大盘3.36%%(t=-5.09)" % (ratio * 100)
    if ratio >= RATIO_MID and td <= TD_HIGH:
        return "mid", "解禁占流通%.1f%%，同类前20交易日跑输大盘2.19%%(t=-4.01)" % (ratio * 100)
    if ratio >= RATIO_STRONG and td <= TD_MID:
        return "watch", "%.0f个交易日后解禁占流通%.1f%%，将进入实证风险窗口（当前 20 日外，历史同期仍为超额上涨，暂不亮灯）" % (td, ratio * 100)
    if ratio >= RATIO_STRONG and td <= TD_WATCH:
        return "watch", "%.0f个交易日后解禁占流通%.1f%%（前40个交易日仍为超额上涨，暂不构成风险）" % (td, ratio * 100)
    return None, ""


def compute(con: sqlite3.Connection, codes=None, as_of: str | None = None) -> dict:
    """给定股票代码，返回未来的解禁风险。

    codes=None → 全市场。
    返回 {code: {"level","td","days","free_date","type","free_ratio","lift_cap","msg"}}
    只包含 level 非空（即 60 个交易日内有值得说的事件）的票。
    """
    as_of = as_of or time.strftime("%Y-%m-%d")
    cal = _calendar(con, as_of)
    if not cal:
        return {}
    base = bisect_left(cal, as_of)          # as_of 在日历中的位置（今天=第 0 个交易日）
    horizon = _date.fromordinal(
        _date.fromisoformat(as_of).toordinal() + 200).isoformat()
    sql = ("SELECT code,name,free_date,type_group,free_ratio,lift_cap,n_holder"
           " FROM lift_event WHERE free_date>=? AND free_date<=?")
    params = [as_of, horizon]
    if codes is not None:
        codes = [c for c in (codes or []) if c]
        if not codes:
            return {}
        sql += " AND code IN (%s)" % ",".join("?" * len(codes))
        params.extend(codes)
    rows = con.execute(sql, params).fetchall()

    # 按票聚合：一只票未来可能有多批，取「风险最高」的那批；同级取最近的
    order = {None: 0, "watch": 1, "mid": 2, "high": 3}
    best = {}
    for code, name, fd, tg, ratio, cap, nh in rows:
        ratio = ratio or 0.0
        i = bisect_left(cal, fd)
        if i >= len(cal):
            continue
        td = i - base                   # 还有几个交易日
        lv, why = _level_of(tg or "", ratio, td)
        if not lv:
            continue
        cur = best.get(code)
        if cur is None or order[lv] > order[cur["level"]] or (
                order[lv] == order[cur["level"]] and td < cur["td"]):
            try:
                nat = (_date.fromisoformat(fd) - _date.fromisoformat(as_of)).days
            except ValueError:
                nat = 0
            best[code] = {
                "level": lv, "td": td, "days": max(0, nat),
                "free_date": fd, "type": tg, "name": name,
                "free_ratio": round(ratio * 100, 2),
                "lift_cap": round((cap or 0) / 10000.0, 2),   # 万元 → 亿元
                "n_holder": nh, "msg": why,
            }
    return best


def summary(con: sqlite3.Connection, codes=None, as_of: str | None = None) -> dict:
    """给接口层用的统一外壳。"""
    items = compute(con, codes, as_of)
    cnt = {"high": 0, "mid": 0, "watch": 0}
    for v in items.values():
        cnt[v["level"]] = cnt.get(v["level"], 0) + 1
    return {"ok": True, "as_of": as_of or time.strftime("%Y-%m-%d"),
            "n": len(items), "count": cnt, "items": items,
            "caliber": ("解禁风险=首发原股东/占流通比例 × 距解禁交易日数；"
                        "实证：首发原股东&占流通≥3%解禁前20交易日跑输大盘3.36%(t=-5.09)。"
                        "只在前20个交易日亮红灯，前40日仍为超额上涨，故60日内仅作信息提示。")}


def _main() -> int:
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    print("刷新解禁事件 …")
    print(refresh(con))
    s = summary(con)
    print("\n全市场未来窗口内风险：%s" % s["count"])
    top = sorted(s["items"].items(), key=lambda kv: (kv[1]["td"], -kv[1]["free_ratio"]))[:12]
    print("\n%-8s %-8s %-11s %5s %8s %9s  %s"
          % ("代码", "名称", "解禁日", "剩余日", "占流通", "金额亿", "档"))
    for code, v in top:
        print("%-8s %-8s %-11s %5d %7.1f%% %9.1f  %s"
              % (code, (v["name"] or "")[:8], v["free_date"], v["td"],
                 v["free_ratio"], v["lift_cap"], v["level"]))
    con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
