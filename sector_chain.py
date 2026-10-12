#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""sector_chain.py —— 「主线研判」预计算

把龙虎榜「板块聚焦」升级为「方向→龙头→梯队→分支」的判断链，预计算落库
（sector_chain 表），供查看器秒读。不在请求时算，避免拖慢界面。

对每个(交易日, 板块)：
  leader   龙头     —— 上榜股里净买最大者（含涨幅、净买）
  tiers    梯队     —— 同板块其余成分股，按涨幅排序，标「已启动 / 待启动」
  branches 分支/下游 —— 该板块成分股共同出现的其他非指数概念板块（共现度）
  pending  上榜后表现 —— 复用 lhb_sector_focus 的 T+1/T+3 均值

依赖：lhb_sector_link（F10 板块归属/成分、行情缓存）、hq.sinajs.cn（批量行情）
缓存：sector_map_cache.json（板块归属）、sector_chain_cache.json（成分行情）
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import ssl
import sys
import time
import urllib.parse
import urllib.request
from collections import Counter, defaultdict

BASE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(BASE, "viewer", "app.db")
sys.path.insert(0, BASE)

import lhb_sector_link as L  # noqa: E402  （复用 F10 与行情缓存）

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
import netctx  # noqa: E402  —— 统一出网 TLS 口径（整改 #27）
_CTX = netctx.https_context("hq.sinajs.cn")  # 整改 #27：默认不校验，按环境变量可开启
_OPENER = urllib.request.build_opener(
    urllib.request.ProxyHandler({}), urllib.request.HTTPSHandler(context=_CTX))

QUOTE_CACHE_FILE = os.path.join(BASE, "sector_chain_cache.json")
GAP = 0.18

# 指数/风格/交易属性/行为类板块：不是「产业方向」，不参与分支推导
EXCLUDE_BRANCH = (
    "指数", "综指", "成分", "标普", "富时", "MSCI", "央视", "上证", "深证", "中证",
    "沪深", "HS300", "深成", "深证100", "上证180", "上证50", "大盘", "小盘", "权重股",
    "沪股通", "深股通", "融资", "融券", "重仓", "基金", "QFII", "社保", "券商",
    "预盈", "预增", "预减", "扭亏", "高送转", "次新", "ST", "B股", "转债", "AH股",
    "GDR", "破净", "低价", "百元股", "举牌", "股权转让", "ETF", "LOF",
    "连板", "涨停", "跌停", "打板", "炸板", "首板", "反板", "昨日", "活跃", "人气",
    "龙虎榜", "新晋", "摘帽", "预披露", "同花顺", "东财", "东方财富", "风格",
    "龙头", "热股", "融资融券", "地区", "板块", "特区", "长三角", "长江三角",
    "西部大开发", "乡村振兴", "国企改革", "央企", "央国企", "茅指数", "宁组合",
    "红利", "价值", "成长", "新股", "独角兽", "中报", "年报", "季报", "预",
    "百强", "品牌", "超级品牌", "热榜", "多板", "新高",
)

MAX_MEMBERS = 90      # 单个板块最多取多少成分股做梯队（控制行情请求量）
BRANCH_SAMPLE = 30    # 取多少成分股做「分支共现」统计（只用于发现候选分支）
BRANCH_CAND = 8       # 候选分支取前几个（再按全量交集重算、排序、截断）
BRANCH_SHOW = 6       # 落库保留几个分支
TOP_SECTORS = 8       # 落库前 N 个板块
TIER_SHOW = 8         # 梯队展示条数

_QCACHE: dict[str, dict] = {}


# ---------------------------------------------------------------- 行情（新浪批量）
def _qcache_load() -> None:
    global _QCACHE
    if os.path.exists(QUOTE_CACHE_FILE):
        try:
            _QCACHE = json.load(open(QUOTE_CACHE_FILE, encoding="utf-8"))
        except Exception:  # noqa: BLE001
            _QCACHE = {}


def _qcache_save() -> None:
    try:
        json.dump(_QCACHE, open(QUOTE_CACHE_FILE, "w", encoding="utf-8"),
                  ensure_ascii=False)
    except Exception:  # noqa: BLE001
        pass


def _sina_code(code: str) -> str:
    c = str(code).strip()
    if c[:1] in ("6", "5", "9"):
        return "sh" + c
    if c[:1] == "4" or c[:2] == "83" or c[:2] == "87" or c[:2] == "92":
        return "bj" + c
    return "sz" + c


def batch_quotes(codes: list[str], retries: int = 2) -> dict[str, dict]:
    """批量取行情：返回 {code: {name, price, pct, prev_close, amount, volume, date}}。"""
    todo = [c for c in dict.fromkeys(codes) if c and c not in _QCACHE]
    for i in range(0, len(todo), 80):
        chunk = todo[i:i + 80]
        url = "https://hq.sinajs.cn/list=" + ",".join(_sina_code(c) for c in chunk)
        req = urllib.request.Request(url, headers={
            "User-Agent": UA, "Referer": "https://finance.sina.com.cn/"})
        for attempt in range(retries + 1):
            try:
                with _OPENER.open(req, timeout=15) as resp:
                    txt = resp.read().decode("gbk", "ignore")
                break
            except Exception:  # noqa: BLE001
                if attempt == retries:
                    txt = ""
                else:
                    time.sleep(0.6 * (attempt + 1))
        for line in txt.splitlines():
            m = re.match(r'var hq_str_([a-z]{2})(\d{6})="(.*)";', line.strip())
            if not m:
                continue
            code, body = m.group(2), m.group(3)
            f = body.split(",")
            if len(f) < 32 or not f[0]:
                _QCACHE[code] = {"name": "", "price": None, "pct": None}
                continue
            try:
                prev = float(f[2])
                price = float(f[3])
                pct = round((price - prev) / prev * 100, 2) if prev else None
            except (ValueError, IndexError):
                price, prev, pct = None, None, None
            _QCACHE[code] = {
                "name": f[0], "price": price, "pct": pct, "prev_close": prev,
                "open": _f(f, 1), "high": _f(f, 4), "low": _f(f, 5),
                "volume": _f(f, 8), "amount": _f(f, 9),
                "date": (f[30] if len(f) > 30 else ""),
            }
        time.sleep(0.12)
    _qcache_save()
    return {c: _QCACHE.get(c, {}) for c in codes}


def _f(arr: list[str], i: int):
    try:
        return float(arr[i])
    except (ValueError, IndexError):
        return None


# ---------------------------------------------------------------- 分支共现
def branch_boards(codes: list[str], exclude: str) -> list[dict]:
    """从成分股的其他板块里找共现度高的「分支/下游」方向。"""
    cnt: Counter = Counter()
    for c in codes[:BRANCH_SAMPLE]:
        try:
            boards = L.stock_sectors(c)   # 已过滤过一轮
        except Exception:  # noqa: BLE001
            boards = []
        for b in boards:
            if b == exclude:
                continue
            if any(k in b for k in EXCLUDE_BRANCH):
                continue
            cnt[b] += 1
        time.sleep(GAP)
    out = []
    for name, n in cnt.most_common(BRANCH_CAND):
        if n < 2:
            continue
        out.append({"name": name, "overlap": n})
    return out


def branch_overlap(main_codes: list[str], branch: str, cap: int = 60) -> dict | None:
    """分支概念 ∩ 主线成分股（**全量**口径）→ 真实重合只数 + 成员名单。

    为什么分两步：逐只查 F10 拿"某只股票属于哪些板块"很贵（主线 178 只 × 每个板块），
    所以共现统计只用来「发现候选分支」；候选一旦确定，直接取**该分支的完整成分表**
    与主线全量成分求交，重合数与被点开的名单都准确。

    旧口径（2026-09-26 前）只对主线成分股的**前 30 只**做共现，于是看板上「人工智能 7」
    的真实含义是"前 30 只里有 7 只"——用户点开却对不上名单。此处修正。
    """
    try:
        mem = L.sector_members(branch)
    except Exception:  # noqa: BLE001
        return None
    if not mem:
        return None
    nm = {c: n for c, n in mem}
    hit = [c for c in dict.fromkeys(main_codes) if c in nm]
    if not hit:
        return None
    return {"name": branch, "overlap": len(hit),
            "members": [{"code": c, "name": nm.get(c) or ""} for c in hit[:cap]]}


# ---------------------------------------------------------------- 主流程
def build(days: int, min_appear: int, top_n: int) -> int:
    con = sqlite3.connect(DB, timeout=30)
    con.row_factory = sqlite3.Row

    dates = [r[0] for r in con.execute(
        "SELECT DISTINCT trade_date FROM lhb_stocks ORDER BY trade_date DESC LIMIT ?",
        (days,))]
    if not dates:
        print("[chain] 无龙虎榜数据", flush=True)
        return 1
    dates = sorted(dates)
    print(f"[chain] 交易日 {dates[0]} ~ {dates[-1]}（{len(dates)} 天）", flush=True)

    # 取每个(日,板块) 的上榜股（一次去重：同股多原因只算一次，净买取最大）
    rows = con.execute(
        """SELECT trade_date, security_code, security_name, MAX(net_amt) net,
                  MAX(change_rate) chg, MAX(close_price) close
           FROM lhb_stocks WHERE trade_date IN (%s)
           GROUP BY trade_date, security_code"""
        % ",".join("?" * len(dates)), dates).fetchall()
    onlist: dict[str, dict] = {}
    for r in rows:
        onlist[(r["trade_date"], r["security_code"])] = dict(r)
    print(f"[chain] 去重上榜股 {len(rows)} 条", flush=True)

    focus = con.execute(
        """SELECT trade_date, sector, net_amt, onlist_n, t1_avg, t3_avg
           FROM lhb_sector_focus WHERE trade_date IN (%s)
           ORDER BY trade_date DESC, net_amt DESC"""
        % ",".join("?" * len(dates)), dates).fetchall()

    # 只对最近 1 个交易日的 Top-N 板块做重活（历史板块已有 t1/t3，不必重复）
    latest = dates[-1]
    cand = [dict(r) for r in focus if r["trade_date"] == latest
            and (r["onlist_n"] or 0) >= min_appear][:top_n]
    if not cand:
        cand = [dict(r) for r in focus if (r["onlist_n"] or 0) >= min_appear][:top_n]
        latest = cand[0]["trade_date"] if cand else latest

    print(f"[chain] 待算板块 {len(cand)} 个（基准日 {latest}）", flush=True)

    _qcache_load()
    out_rows = []
    for k, item in enumerate(cand, 1):
        sector = item["sector"]
        d = item["trade_date"]
        # 当日该板块上榜股
        codes_on = [c for (dd, c) in onlist if dd == d
                    and sector in (L.stock_sectors(c) or [])]
        if not codes_on:
            # 兜底：用成分股反查当天上榜
            members_all = L.sector_members(sector)
            mset = {c for c, _ in members_all}
            codes_on = [c for (dd, c) in onlist if dd == d and c in mset]
        names_on = {c: onlist[(d, c)]["security_name"] for c in codes_on if (d, c) in onlist}

        if not codes_on:
            print(f"  ..{k}/{len(cand)} {sector} 无上榜股，跳过", flush=True)
            continue

        # 龙头 = 上榜股里净买最大
        lead_code = max(codes_on, key=lambda c: (onlist[(d, c)]["net"] or 0))
        lead = onlist[(d, lead_code)]

        members = L.sector_members(sector)
        time.sleep(GAP)
        if not members:
            continue
        mset = {c: n for c, n in members}
        others = [c for c in mset if c not in names_on][:MAX_MEMBERS]
        q = batch_quotes([lead_code] + others)

        # 梯队：同板块其余成分股，按涨幅排序
        tiers = []
        for c in others:
            v = q.get(c) or {}
            if v.get("pct") is None:
                continue
            tiers.append({"code": c, "name": v.get("name") or mset.get(c, ""),
                          "pct": v["pct"], "price": v.get("price")})
        tiers.sort(key=lambda x: -(x["pct"] or 0))

        # 分支/下游：① 样本共现「发现」候选（控请求量）；② 候选按全量交集「定重合数 + 名单」
        # ⚠️ 变量名勿与上面的 `d`(trade_date) / `cand`(板块候选) 撞车，否则写库时 trade_date 变 dict。
        main_codes = list(mset.keys())
        cands = branch_boards(main_codes[:BRANCH_SAMPLE], sector)
        branches = []
        for bc in cands:
            bd = branch_overlap(main_codes, bc["name"])
            if not bd:
                continue
            # 同义/上位板块过滤：overlap 占主线成分七成以上者（如「机械设备 → 通用设备 140/178」）
            # 并非延伸方向，只是换个名字的同一群人，留在横条上比数字本身更误导。
            if bd["overlap"] >= 0.7 * len(main_codes):
                continue
            branches.append(bd)
        branches.sort(key=lambda x: -x["overlap"])
        branches = branches[:BRANCH_SHOW]

        out_rows.append({
            "trade_date": d, "sector": sector,
            "net_amt": item["net_amt"], "onlist_n": item["onlist_n"],
            "leader_code": lead_code,
            "leader_name": lead["security_name"] or mset.get(lead_code, ""),
            "leader_net": lead["net"], "leader_chg": lead["chg"],
            "leader_pct_now": (q.get(lead_code) or {}).get("pct"),
            "onlist_json": json.dumps(
                [{"code": c, "name": names_on.get(c, "")} for c in codes_on[:10]],
                ensure_ascii=False),
            "tiers_json": json.dumps(tiers[:TIER_SHOW], ensure_ascii=False),
            "branches_json": json.dumps(branches, ensure_ascii=False),
            "t1_avg": item["t1_avg"], "t3_avg": item["t3_avg"],
        })
        up = sum(1 for t in tiers if (t["pct"] or 0) > 0)
        print(f"  ..{k}/{len(cand)} {sector}: 上榜{len(codes_on)}只 龙头={lead['security_name']} "
              f"梯队{len(tiers)}只(涨{up}) 分支{len(branches)}个", flush=True)

    if not out_rows:
        print("[chain] 无结果", flush=True)
        con.close()
        return 1

    cur = con.cursor()
    cur.execute("""CREATE TABLE IF NOT EXISTS sector_chain (
        trade_date TEXT, sector TEXT, net_amt REAL, onlist_n INTEGER,
        leader_code TEXT, leader_name TEXT, leader_net REAL, leader_chg REAL,
        leader_pct_now REAL, onlist_json TEXT, tiers_json TEXT, branches_json TEXT,
        t1_avg REAL, t3_avg REAL, updated_at TEXT,
        PRIMARY KEY (trade_date, sector))""")
    for r in out_rows:
        r = dict(r)
        r["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        cur.execute("""INSERT OR REPLACE INTO sector_chain
            (trade_date, sector, net_amt, onlist_n, leader_code, leader_name,
             leader_net, leader_chg, leader_pct_now, onlist_json, tiers_json,
             branches_json, t1_avg, t3_avg, updated_at)
            VALUES (:trade_date,:sector,:net_amt,:onlist_n,:leader_code,:leader_name,
                    :leader_net,:leader_chg,:leader_pct_now,:onlist_json,:tiers_json,
                    :branches_json,:t1_avg,:t3_avg,:updated_at)""", r)
    cur.execute("INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)",
                ("sector_chain_updated", time.strftime("%Y-%m-%d %H:%M:%S")))
    con.commit()
    con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    con.close()
    print(f"[chain] 落库 {len(out_rows)} 条 -> sector_chain", flush=True)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--min-appear", type=int, default=2)
    ap.add_argument("--top", type=int, default=TOP_SECTORS)
    a = ap.parse_args()
    return build(a.days, a.min_appear, a.top)


if __name__ == "__main__":
    raise SystemExit(main())
