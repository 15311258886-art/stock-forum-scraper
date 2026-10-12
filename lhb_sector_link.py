# -*- coding: utf-8 -*-
"""龙虎榜「板块扩散」分析

核心问题：游资在 T 日集中买入某板块的股票后，该板块内【未被买入】的股票，
         在 T+1 / T+3 是否跟随上涨？

数据链路（均已实测可用）：
  个股→板块   : 东财 F10 RPT_F10_CORETHEME_BOARDTYPE (datacenter.eastmoney.com)
  板块→成分股 : 同一报表反查 filter=(BOARD_NAME="xxx")
  个股行情    : 新浪 CN_MarketData.getKLineData（全历史 / 不复权）
  基准        : 新浪上证指数 sh000001

输出：sector_link_result.json + 控制台结论
"""
import argparse
import json
import os
import ssl
import sqlite3
import sys
import time
import urllib.parse
import urllib.request
from collections import defaultdict

BASE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(BASE, "viewer", "app.db")
OUT = os.path.join(BASE, "sector_link_result.json")

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
import netctx  # noqa: E402  —— 统一出网 TLS 口径（整改 #27）
_CTX = netctx.https_context("datacenter.eastmoney.com")  # 整改 #27：默认不校验，按环境变量可开启
_OP = urllib.request.build_opener(urllib.request.ProxyHandler({}),
                                  urllib.request.HTTPSHandler(context=_CTX))

F10 = "https://datacenter.eastmoney.com/securities/api/data/v1/get"
F10_REF = "https://emweb.securities.eastmoney.com/"

# 剔除非题材板块（三类：指数/成分/交易属性、行为统计类、财务事件与风格类）
# 说明：行为统计类（如「最近多板」「东方财富热股」「百日新高」）天然把上榜股全兜进去，
#       拿它做「扩散」毫无意义，必须排除。
EXCLUDE_KW = (
    # ① 指数 / 成分 / 交易属性
    "指数", "综指", "成分", "标普", "标准普尔", "富时", "MSCI", "央视", "上证", "深证",
    "中证", "沪深", "沪股通", "深股通", "陆股通", "融资", "融券", "重仓",
    "基金", "QFII", "社保", "ETF", "LOF", "B股", "AH", "GDR", "转债",
    "茅指数", "宁组合", "红利",
    # ② 行为 / 统计类（含上榜股集合，扩散分析无意义）
    "连板", "多板", "涨停", "跌停", "打板", "炸板", "首板", "二板", "反包",
    "昨日", "今日", "最近", "新晋", "新高", "新低", "热股", "热炒", "热门",
    "人气", "活跃", "龙虎榜", "趋势股", "题材股", "指标股", "权重股",
    "大盘", "小盘", "中盘", "绩优", "亏损", "超跌", "反弹", "强势", "龙头",
    "妖股", "摘帽", "新股", "次新", "高送转",
    # ③ 财务事件 / 事件驱动 / 风格
    "预盈", "预增", "预减", "预亏", "首亏", "首盈", "扭亏", "增亏", "减亏",
    "中报", "年报", "季报", "业绩", "每股", "估值", "分红", "送转", "配股",
    "增发", "回购", "增持", "减持", "解禁", "质押", "商誉", "并购", "重组",
    "借壳", "股权", "举牌", "破净", "破发", "低价", "高价", "百元",
    "价值", "成长", "防御", "周期",
)

MAX_MEMBERS = 25      # 每个板块采样上限（控制请求量）
MAX_DENSITY = 0.25    # 上榜密度上限：板块内上榜股占比超此值 → 判定为统计类板块，跳过
TOP_PER_DAY = 4       # 每个交易日只取净买额 Top-N 的板块做扩散计算
GAP_F10 = 0.35        # F10 节流
GAP_SINA = 0.25       # 新浪节流


def _fetch(url, referer, retries=3, gap=0.8, timeout=18):
    last = None
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA, "Referer": referer})
            with _OP.open(req, timeout=timeout) as r:
                return r.read().decode("utf-8-sig", "ignore")
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(gap * (i + 1))
    print(f"    [fail] {type(last).__name__} {url[:80]}", flush=True)
    return None


def f10_rows(params):
    url = F10 + "?" + urllib.parse.urlencode(params)
    t = _fetch(url, F10_REF)
    if not t:
        return []
    try:
        return ((json.loads(t).get("result") or {}).get("data")) or []
    except Exception:  # noqa: BLE001
        return []


_SECTOR_CACHE_FILE = os.path.join(BASE, "sector_map_cache.json")
_SECTOR_CACHE = None


def _load_cache():
    global _SECTOR_CACHE
    if _SECTOR_CACHE is None:
        try:
            with open(_SECTOR_CACHE_FILE, encoding="utf-8") as f:
                _SECTOR_CACHE = json.load(f)
        except Exception:  # noqa: BLE001
            _SECTOR_CACHE = {}
    return _SECTOR_CACHE


def _save_cache():
    if _SECTOR_CACHE is None:
        return
    try:
        with open(_SECTOR_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(_SECTOR_CACHE, f, ensure_ascii=False)
    except Exception:  # noqa: BLE001
        pass


def stock_sectors(code):
    """一只股票所属题材板块名（已过滤）；结果落盘缓存，重跑零请求"""
    cache = _load_cache()
    if code in cache:
        return cache[code]
    suffix = "SH" if code[0] in "659" else "SZ"
    rows = f10_rows({"reportName": "RPT_F10_CORETHEME_BOARDTYPE", "columns": "ALL",
                     "filter": f'(SECUCODE="{code}.{suffix}")', "pageSize": "60",
                     "pageNumber": "1", "source": "HSF10", "client": "PC"})
    out = []
    for r in rows:
        nm = (r.get("BOARD_NAME") or "").strip()
        if not nm or len(nm) > 10:
            continue
        if any(k in nm for k in EXCLUDE_KW):
            continue
        out.append(nm)
    out = list(dict.fromkeys(out))
    cache[code] = out
    return out


def sector_members(bname):
    """板块 → 成分股 [(code, name)]"""
    rows = f10_rows({"reportName": "RPT_F10_CORETHEME_BOARDTYPE",
                     "columns": "SECURITY_CODE,SECURITY_NAME_ABBR",
                     "filter": f'(BOARD_NAME="{bname}")', "pageSize": "300",
                     "pageNumber": "1", "source": "HSF10", "client": "PC"})
    seen, out = set(), []
    for r in rows:
        c = r.get("SECURITY_CODE")
        if not c or c in seen:
            continue
        seen.add(c)
        out.append((c, r.get("SECURITY_NAME_ABBR") or ""))
    return out


_KL_CACHE = {}


def sina_kline(code, datalen=80):
    if code in _KL_CACHE:
        return _KL_CACHE[code]
    sym = ("sh" if code[0] in "659" else "sz") + code
    t = _fetch(f"https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/"
               f"CN_MarketData.getKLineData?symbol={sym}&scale=240&ma=no&datalen={datalen}",
               "https://finance.sina.com.cn/", retries=2, gap=0.5)
    out = []
    if t:
        try:
            for r in json.loads(t) or []:
                try:
                    out.append({"date": str(r["day"])[:10], "close": float(r["close"])})
                except Exception:  # noqa: BLE001
                    continue
        except Exception:  # noqa: BLE001
            pass
    time.sleep(GAP_SINA)
    _KL_CACHE[code] = out
    return out


def ret_after(kl, base_date, n):
    """基准日收盘 → 之后第 n 个交易日收盘的收益率%"""
    idx = next((i for i, k in enumerate(kl) if k["date"] >= base_date), None)
    if idx is None or idx + n >= len(kl):
        return None
    c0, c1 = kl[idx]["close"], kl[idx + n]["close"]
    if not c0:
        return None
    return round((c1 / c0 - 1) * 100, 2)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=0, help="只分析最近 N 个上榜日（0=全部）")
    ap.add_argument("--min-appear", type=int, default=2, help="板块上榜股数下限")
    args = ap.parse_args()

    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    dates = [r["trade_date"] for r in con.execute(
        "SELECT DISTINCT trade_date FROM lhb_stocks ORDER BY trade_date DESC")]
    if args.days:
        dates = dates[:args.days]
    dates = sorted(dates)
    # 同一股票一天可能因多个「上榜原因」出现多行、且 net_amt 不同；
    # 按 (日期, 代码) 归并为一行（取净买额绝对值最大者，避免重复计入）。
    merged: dict[tuple, dict] = {}
    for d in dates:
        for r in con.execute(
                "SELECT security_code, security_name, trade_date, net_amt "
                "FROM lhb_stocks WHERE trade_date=?", (d,)):
            k = (r["trade_date"], r["security_code"])
            v = {"security_code": r["security_code"], "security_name": r["security_name"],
                 "trade_date": r["trade_date"], "net_amt": r["net_amt"]}
            old = merged.get(k)
            if old is None or abs(v["net_amt"] or 0) > abs(old["net_amt"] or 0):
                merged[k] = v
    rows = list(merged.values())
    con.close()
    print(f"[样本] 上榜日 {len(dates)} 天（{dates[0]}~{dates[-1]}），归并后上榜股 {len(rows)} 条"
          f"（原始多行已按 日期+代码 归并）", flush=True)

    # ---- 1) 个股 → 板块 ----
    print("[1/4] 个股 → 所属题材板块（东财 F10）", flush=True)
    sec_map = {}
    for i, r in enumerate(rows, 1):
        s = stock_sectors(r["security_code"])
        sec_map[(r["trade_date"], r["security_code"])] = s
        if i % 40 == 0:
            print(f"      ..{i}/{len(rows)}", flush=True)
        time.sleep(GAP_F10)
    _save_cache()  # 落盘缓存：同日重跑零请求（此前漏调用，导致每次都全量重抓）
    print(f"      映射缓存已保存（{len(_SECTOR_CACHE or {})} 只股票）", flush=True)

    # ---- 2) 按 (日期, 板块) 聚合 ----
    agg = defaultdict(lambda: {"codes": {}, "net": 0.0})
    for r in rows:
        for s in sec_map.get((r["trade_date"], r["security_code"])) or []:
            k = (r["trade_date"], s)
            agg[k]["codes"][r["security_code"]] = r["security_name"]
            agg[k]["net"] += (r["net_amt"] or 0)

    focus = {k: v for k, v in agg.items() if len(v["codes"]) >= args.min_appear}
    print(f"[2/4] 上榜≥{args.min_appear}只的「板块·日」组合：{len(focus)} 个"
          f"（占全部聚合 {len(agg)}）", flush=True)
    top = sorted(focus.items(), key=lambda kv: -kv[1]["net"])[:12]
    for (d, s), v in top:
        nm = list(v["codes"].values())
        print(f"      {d} {s:<10} 上榜{len(v['codes'])}只 净买{v['net']/1e8:>7.2f}亿  "
              f"{'/'.join(nm[:5])}", flush=True)

    # ---- 3) 板块成分股 + 后续涨幅 ----
    print(f"[3/4] 计算扩散：板块内【未上榜】成分股 T+1/T+3 涨幅", flush=True)
    results, bench = [], {}
    by_day = defaultdict(list)
    for (d, s), v in focus.items():
        by_day[d].append(((d, s), v))
    candidates = []
    for d in sorted(by_day):
        candidates.extend(sorted(by_day[d], key=lambda kv: -kv[1]["net"])[:TOP_PER_DAY])
    print(f"      候选「板块·日」{len(candidates)} 个"
          f"（每交易日净买额 Top {TOP_PER_DAY}）", flush=True)
    for (d, s), v in candidates:
        members = sector_members(s)
        time.sleep(GAP_F10)
        if not members:
            continue
        onlist = set(v["codes"].keys())
        density = len(onlist) / float(max(1, len(members)))
        if density > MAX_DENSITY:
            print(f"      跳过「{s}」：上榜密度 {density:.0%}（统计类板块，非真题材）", flush=True)
            continue
        sample = [(c, n) for c, n in members if c not in onlist][:MAX_MEMBERS]
        r1s, r3s = [], []
        for c, _n in sample:
            kl = sina_kline(c)
            a, b = ret_after(kl, d, 1), ret_after(kl, d, 3)
            if a is not None:
                r1s.append(a)
            if b is not None:
                r3s.append(b)
        # 2026-09-25 修复：最新交易日（T+1 尚未收盘/未出数据）不再整段跳过，
        # 照常入库并把「榜后表现」留空标为待验证 —— 否则最新一天会从产品里消失。
        pending = not r1s
        if d not in bench:
            idx = sina_kline("000001")
            bench[d] = (ret_after(idx, d, 1), ret_after(idx, d, 3))
        results.append({
            "date": d, "sector": s, "onlist_n": len(v["codes"]),
            "onlist": [{"code": c, "name": n} for c, n in v["codes"].items()][:8],
            "onlist_names": "/".join(list(v["codes"].values())[:6]),
            "net_yi": round(v["net"] / 1e8, 2),
            "net_amt": round(v["net"], 2),
            "members_n": len(members), "sampled": len(r1s),
            "t1_avg": round(sum(r1s) / len(r1s), 2) if r1s else None,
            "t3_avg": round(sum(r3s) / len(r3s), 2) if r3s else None,
            "t1_win": round(sum(1 for x in r1s if x > 0) * 100.0 / len(r1s), 0) if r1s else None,
            "bench_t1": bench[d][0], "bench_t3": bench[d][1],
            "pending": pending,
        })

    # ---- 4) 汇总 ----
    print(f"[4/4] 汇总（有效样本 {len(results)} 个「板块·日」）", flush=True)
    if not results:
        print("!! 无有效样本", flush=True)
        return 1
    t1 = [x["t1_avg"] for x in results if x["t1_avg"] is not None]
    t3 = [x["t3_avg"] for x in results if x["t3_avg"] is not None]
    b1 = [x["bench_t1"] for x in results if x["bench_t1"] is not None]
    b3 = [x["bench_t3"] for x in results if x["bench_t3"] is not None]
    ex1 = [x["t1_avg"] - x["bench_t1"] for x in results if x["bench_t1"] is not None]
    ex3 = [x["t3_avg"] - x["bench_t3"] for x in results
           if x["t3_avg"] is not None and x["bench_t3"] is not None]
    summary = {
        "n": len(results),
        "t1_avg": round(sum(t1) / len(t1), 2) if t1 else None,
        "t3_avg": round(sum(t3) / len(t3), 2) if t3 else None,
        "bench_t1_avg": round(sum(b1) / len(b1), 2) if b1 else None,
        "bench_t3_avg": round(sum(b3) / len(b3), 2) if b3 else None,
        "excess_t1": round(sum(ex1) / len(ex1), 2) if ex1 else None,
        "excess_t3": round(sum(ex3) / len(ex3), 2) if ex3 else None,
        "t1_win_rate": (round(sum(1 for x in results if (x["t1_avg"] or 0) > 0) * 100.0 / len(t1), 0)
                        if t1 else None),
        "pending_n": sum(1 for x in results if x.get("pending")),
    }
    print()
    print("=" * 72)
    print(f"  样本数            : {summary['n']} 个「上榜板块·交易日」")
    print(f"  板块内未上榜股 T+1: 平均 {summary['t1_avg']}%   "
          f"（同期上证 {summary['bench_t1_avg']}%，超额 {summary['excess_t1']}%）")
    print(f"  板块内未上榜股 T+3: 平均 {summary['t3_avg']}%   "
          f"（同期上证 {summary['bench_t3_avg']}%，超额 {summary['excess_t3']}%）")
    print(f"  板块层面 T+1 上涨比例: {summary['t1_win_rate']}%")
    print("=" * 72)
    print()
    print("  明细（按超额 T+1 排序，前 12）：")
    for x in sorted(results, key=lambda y: -((y["t1_avg"] or 0) - (y["bench_t1"] or 0)))[:12]:
        ex = (x["t1_avg"] or 0) - (x["bench_t1"] or 0)
        # pending（最新交易日 T+1 尚未出数）时 t1_avg/t3_avg 都是 None，
        # 2026-09-25 之后改为照常入库，但打印这里没跟着改 → 整段崩溃在落库之前。修掉。
        t1 = x["t1_avg"] if x["t1_avg"] is not None else None
        t3 = x["t3_avg"]
        print(f"   {x['date']} {x['sector']:<10} 上榜{x['onlist_n']:>2}只 "
              f"净买{x['net_yi']:>7.2f}亿 | T+1 {('待验证' if t1 is None else '%6.2f' % t1)}% "
              f"T+3 {('待验证' if t3 is None else '%.2f' % t3):>6}% "
              f"| 超额 {ex:>6.2f}%  样本{x['sampled']}只")

    with open(OUT, "w", encoding="utf-8") as f:
        json.dump({"summary": summary, "detail": results,
                   "generated_at": time.strftime("%Y-%m-%d %H:%M:%S")},
                  f, ensure_ascii=False, indent=2)
    print(f"\n  结果已写入 {os.path.basename(OUT)}", flush=True)

    # ---- 5) 落库（查看器直接读，避免每次调用重算 8 分钟）----
    try:
        w = sqlite3.connect(DB, timeout=30)
        w.execute("""CREATE TABLE IF NOT EXISTS lhb_sector_focus (
            trade_date TEXT NOT NULL,
            sector     TEXT NOT NULL,
            net_amt    REAL,
            onlist_n   INTEGER,
            onlist_json TEXT,
            members_n  INTEGER,
            sampled    INTEGER,
            t1_avg     REAL, t3_avg REAL, t1_win REAL,
            bench_t1   REAL, bench_t3 REAL,
            updated_at TEXT,
            PRIMARY KEY (trade_date, sector))""")
        now = time.strftime("%Y-%m-%d %H:%M:%S")
        w.executemany(
            """INSERT OR REPLACE INTO lhb_sector_focus
               (trade_date, sector, net_amt, onlist_n, onlist_json, members_n, sampled,
                t1_avg, t3_avg, t1_win, bench_t1, bench_t3, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            [(x["date"], x["sector"], x["net_amt"], x["onlist_n"],
              json.dumps(x["onlist"], ensure_ascii=False), x["members_n"], x["sampled"],
              x["t1_avg"], x["t3_avg"], x["t1_win"], x["bench_t1"], x["bench_t3"], now)
             for x in results])
        w.execute("INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)",
                  ("lhb_sector_summary",
                   json.dumps({**summary, "generated_at": now}, ensure_ascii=False)))
        w.commit()
        w.close()
        print(f"  已落库 lhb_sector_focus：{len(results)} 行（供查看器「板块动向」读取）", flush=True)
    except Exception as e:  # noqa: BLE001 —— 落库失败不影响 JSON 产出
        print(f"  !! 落库失败：{type(e).__name__}: {e}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
