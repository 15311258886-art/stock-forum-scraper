# -*- coding: utf-8 -*-
"""成本分档标定位（2026-10-31 决策⑤，进哥拍板走 C）。

## 这个模块**不做**什么

不改 `params.COST_TIERS_BP`。一个数字都不写回。它只做三件事：
存成交回报 → 按流动性档统计执行偏差 → 给出"建议档位 + 样本量"。
改不改由人定，因为"用几笔成交改掉全站成本模型"这件事，不该由脚本自己决定。

## 为什么现在先用拍定值

10／20／35／60 bp 是拍的。拍定值能做两件正确的事：给上限、让结论偏保守。
保守方向的错误是**低估收益**，不会把亏看成赚 —— 这是可以带着走的不确定性。
而"准确"需要一样东西：他自己的成交回报，现在没有。

## 口径限制（必须一起读，否则这个数会被当成冲击成本引用）

本模块算的是**执行偏差 = 成交价相对当日典型价的偏离**：

    典型价 = (最高 + 最低 + 收盘) / 3        （K 线库只有 OHLC，没有成交额列）
    成本 bp = −(买卖方向调整后的偏离)        正数 = 吃亏

它包含但不等于冲击成本：里面还有**择时**（你主动追价、挂低等待）和
**方向本身的必然偏离**（买通常成交在偏高价、卖在偏低）。要分离这三者需要
分时或逐笔数据，本工具没有。所以字段名一律叫 `exec_cost_bp`，不叫 impact。

宁可少说，不要用名字冒充精度。

## K 线库只有 OHLC 的后果

典型价是代理基准，不是真实 VWAP。真实 VWAP 需要成交额／成交量，
本库的 `v` 是成交量（股），乘 `c` 只能近似当日成交额，除权日会偏低。
这两点都写进 `caveats` 一起返回，不允许它们沉在文档里。
"""
from __future__ import annotations

import csv
import os
import sqlite3

from viewer.kernel import params as _P

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_KLINE = os.path.join(ROOT, "data", "kline_daily.db")

DDL = """
CREATE TABLE IF NOT EXISTS trade_fill (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    d             INTEGER NOT NULL,
    code          TEXT    NOT NULL,
    side          TEXT    NOT NULL,
    qty           REAL    NOT NULL,
    price         REAL    NOT NULL,
    bench         REAL,
    adv           REAL,
    amount        REAL,
    participation REAL,
    exec_cost_bp  REAL,
    src           TEXT,
    created_at    TEXT DEFAULT (datetime('now','localtime')),
    UNIQUE(d, code, side, qty, price)
);
CREATE INDEX IF NOT EXISTS idx_trade_fill_code_d ON trade_fill(code, d);
"""

# CSV 列名兼容（手输/券商导出格式不一，宁可多认几种，也不要让人先改表头）
COLMAP = {
    "d": ("d", "date", "trade_date", "日期", "成交日期", "发生日期"),
    "code": ("code", "证券代码", "代码", "股票代码", "symbol"),
    "side": ("side", "买卖", "方向", "操作", "operation", "bs"),
    "qty": ("qty", "quantity", "数量", "成交数量", "股数"),
    "price": ("price", "成交价", "成交均价", "成交价格", "均价"),
}
SIDE_ALIAS = {"buy": "buy", "b": "buy", "买": "buy", "买入": "buy", "证券买入": "buy",
              "sell": "sell", "s": "sell", "卖": "sell", "卖出": "sell", "证券卖出": "sell"}


def ensure_table(con) -> None:
    """建表。可重复调用；用 IF NOT EXISTS，不做破坏性迁移。"""
    cur = con.cursor()
    for stmt in [s.strip() for s in DDL.split(";") if s.strip()]:
        cur.execute(stmt)
    con.commit()


def _norm_date(v) -> int:
    s = str(v or "").strip().replace("-", "").replace("/", "").replace(".", "")
    return int(s) if s.isdigit() and len(s) == 8 else 0


def _pick(row: dict, key: str):
    low = {str(k).strip().lower(): v for k, v in row.items()}
    for alias in COLMAP[key]:
        if alias.lower() in low:
            return low[alias.lower()]
    return None


def _kline_lookup(codes, dates):
    """{(code, d): (bench, adv)}。bench=(H+L+C)/3，adv≈C×V（近似，除权日偏低）。"""
    out = {}
    if not os.path.exists(_KLINE) or not codes:
        return out
    con = sqlite3.connect("file:%s?mode=ro" % _KLINE.replace("\\", "/"), uri=True, timeout=30)
    try:
        qs = ",".join("?" * len(codes))
        for code, d, h, l, c, v in con.execute(
                "SELECT code, d, h, l, c, v FROM kline WHERE code IN (%s)" % qs, list(codes)):
            if int(d) not in dates:
                continue
            c = float(c or 0)
            if c <= 0:
                continue
            h, l = float(h or c), float(l or c)
            out[(str(code), int(d))] = ((h + l + c) / 3.0, c * float(v or 0))
    finally:
        con.close()
    return out


def import_csv(con, path, src=None) -> dict:
    """导入成交明细 CSV。返回 {ok, inserted, skipped, errors}。

    幂等：同一笔（日期/代码/方向/数量/价格 全同）重复导入不会重复计数。
    算不出基准价（当日无 K 线、停牌、代码不在库）的笔**照样入库**，
    只是 exec_cost_bp 留空 —— 丢掉它们等于让样本偏向"好算的那些"。
    """
    ensure_table(con)
    if not os.path.exists(path):
        return {"ok": False, "error": "file_not_found", "path": path}
    recs, errors = [], []
    with open(path, encoding="utf-8-sig", newline="") as f:
        for i, row in enumerate(csv.DictReader(f), start=2):
            try:
                d = _norm_date(_pick(row, "d"))
                code = str(_pick(row, "code") or "").strip().zfill(6)
                side = SIDE_ALIAS.get(str(_pick(row, "side") or "").strip().lower())
                qty = float(str(_pick(row, "qty") or 0).replace(",", ""))
                price = float(str(_pick(row, "price") or 0).replace(",", ""))
            except (TypeError, ValueError):
                errors.append({"line": i, "why": "字段解析失败"})
                continue
            if not d or not code.strip("0") or side is None or qty <= 0 or price <= 0:
                errors.append({"line": i, "why": "缺日期/代码/买卖方向/数量/价格，或数值非法"})
                continue
            recs.append((d, code, side, qty, price))

    if not recs:
        return {"ok": False, "error": "no_valid_rows", "errors": errors[:20]}

    kl = _kline_lookup({r[1] for r in recs}, {r[0] for r in recs})
    ins = skip = 0
    for d, code, side, qty, price in recs:
        bench, adv = kl.get((code, d), (None, None))
        amount = qty * price
        part = (amount / adv) if adv else None
        cost = None
        if bench and bench > 0:
            raw = (price / bench - 1.0) * 10000.0        # 买入价高于典型价 = 正数
            cost = raw if side == "buy" else -raw        # 卖出价高于典型价 = 有利 = 负数
        try:
            con.execute(
                "INSERT INTO trade_fill (d, code, side, qty, price, bench, adv, amount,"
                " participation, exec_cost_bp, src) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (d, code, side, qty, price, bench, adv, amount, part, cost,
                 src or os.path.basename(path)))
            ins += 1
        except sqlite3.IntegrityError:
            skip += 1                                    # 重复笔
    con.commit()
    return {"ok": True, "inserted": ins, "skipped_dup": skip,
            "errors": errors[:20], "n_errors": len(errors)}


def _tier_of_adv(adv):
    for floor, bp in _P.COST_TIERS_BP:
        if adv is not None and adv >= floor:
            return floor, bp
    return None, None


def _pct(xs, p):
    xs = sorted(xs)
    if not xs:
        return None
    if len(xs) == 1:
        return round(xs[0], 2)
    k = (len(xs) - 1) * p / 100.0
    lo, hi = int(k), min(int(k) + 1, len(xs) - 1)
    return round(xs[lo] + (xs[hi] - xs[lo]) * (k - lo), 2)


def stats(con) -> dict:
    """按流动性档统计执行偏差，并给出建议值（不自动应用）。"""
    ensure_table(con)
    rows = con.execute(
        "SELECT code, d, side, amount, adv, exec_cost_bp FROM trade_fill").fetchall()
    buckets = {}
    n_total = n_priced = 0
    for code, d, side, amount, adv, cost in rows:
        n_total += 1
        floor, nominal = _tier_of_adv(adv)
        key = floor if floor is not None else 0
        b = buckets.setdefault(key, {"adv_floor": floor, "nominal_bp": nominal,
                                     "n": 0, "costs": []})
        b["n"] += 1
        if cost is not None:
            b["costs"].append(float(cost))
            n_priced += 1

    tiers = []
    for key in sorted(buckets, reverse=True):
        b = buckets[key]
        cs = b["costs"]
        tiers.append({
            "adv_floor": b["adv_floor"],
            "nominal_bp": b["nominal_bp"],
            "n_trades": b["n"],
            "n_priced": len(cs),
            "median_bp": _pct(cs, 50),
            "p75_bp": _pct(cs, _P.COST_CALIB_SUGGEST_PCT),
            # 建议值取 p75 向上取整到 5 的倍数 —— 成本估低比估高危险
            "suggested_bp": (None if len(cs) < _P.COST_CALIB_MIN_PER_TIER
                             else int((_pct(cs, _P.COST_CALIB_SUGGEST_PCT) + 4.99) // 5 * 5)),
            "ready": len(cs) >= _P.COST_CALIB_MIN_PER_TIER,
        })

    ready = n_priced >= _P.COST_CALIB_MIN_TRADES
    return {
        "ok": True,
        "n_trades": n_total,
        "n_priced": n_priced,
        "ready": ready,
        "need_more": max(0, _P.COST_CALIB_MIN_TRADES - n_priced),
        "tiers": tiers,
        "current": [{"adv_floor": f, "bp": bp} for f, bp in _P.COST_TIERS_BP],
        "note": ("这只是**建议**，不会自动改写 params.COST_TIERS_BP。"
                 "改了要连带重跑受成本影响的结论，所以必须由人拍。"
                 if ready else
                 "样本还不够（已标定 %d 笔 / 需要 %d 笔）—— 现在给出的分档统计只是排列噪声，不构成建议。"
                 % (n_priced, _P.COST_CALIB_MIN_TRADES)),
        "caveats": [
            "口径是「成交价相对当日典型价 (H+L+C)/3 的偏离」，不等于冲击成本；"
            "里面还含择时和买卖方向的必然偏离。",
            "K 线库只有 OHLC，没有成交额：ADV 是 C×V 近似，除权日会偏低；"
            "真实 VWAP 需要分时或逐笔数据。",
            "样本来自你自己的成交，天然偏向你实际买的票（流动性偏好已在样本里）；"
            "用它校准的是「你会碰到的票」的成本，不是全市场。",
        ],
    }


def describe() -> dict:
    return {"kline_db": os.path.exists(_KLINE),
            "min_trades": _P.COST_CALIB_MIN_TRADES,
            "min_per_tier": _P.COST_CALIB_MIN_PER_TIER,
            "suggest_pct": _P.COST_CALIB_SUGGEST_PCT,
            "current_tiers": [list(x) for x in _P.COST_TIERS_BP],
            "writes_params": False}


if __name__ == "__main__":
    import json
    import sys
    p = os.path.join(ROOT, "viewer", "app.db")
    con = sqlite3.connect(p, timeout=20)
    if len(sys.argv) > 1 and sys.argv[1] == "import":
        print(json.dumps(import_csv(con, sys.argv[2]), ensure_ascii=False, indent=1))
    else:
        print(json.dumps(stats(con), ensure_ascii=False, indent=1))
    con.close()
