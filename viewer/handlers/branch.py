# -*- coding: utf-8 -*-
"""branch 域 API 处理函数（从 viewer/server.py 物理抽取，2026-11-07 拆分重构）。

抽取规则：函数体逻辑零改动；依赖的 server 模块级符号经 `from viewer.server import (...)`
重定向进本地命名空间；标准库本地导入。server.py 末尾 re-export 保持命名空间兼容，
routes.py 与既有测试（S.api_*）无需改动。
"""
from __future__ import annotations

from viewer.server import (  # noqa: E402,F401
    DB,
    _branch_live_quotes,
    _chain_quotes,
    _jl,
    _num,
    _sector_reverse,
)

def api_branch_detail(sector: str, branch: str, date: str | None = None) -> dict:
    """分支透视：把看板上的分支数字展开成「都有谁 / 谁在买谁在说 / 大致判断」。

    口径（对用户必须诚实，故随包返回，前端照原样展示）：
      · members 来自 `sector_members(分支) ∩ 主线成分股`，东财 F10 单板块最多返回 300 只，
        概念越大越可能被截断 —— 重合数是「可见下限」，不是全市场真值。
      · rise_prob_3d 是**该营业部/机构席位的历史统计**（该席位过往买入后 3 日上涨占比），
        不是对这只票的预测，只作参照。
    · 博主/选手覆盖来自本地库 positions / trades / stock_mentions，与龙虎榜方向未必重叠；
      没有覆盖时明确返回空，不编。
    优先只读本地库与磁盘缓存（快照覆盖时零网络、弹层秒开）；仅当成员行情缺失时
    （全市场概念的成员不在快照内）才补一次批量行情（120s TTL），失败静默降级为 «—»。
    """
    row = None
    if date:
        row = DB.conn.execute(
            "SELECT * FROM sector_chain WHERE trade_date=? AND sector=?",
            (date, sector)).fetchone()
    if not row and sector:
        row = DB.conn.execute(
            "SELECT * FROM sector_chain WHERE sector=? ORDER BY trade_date DESC LIMIT 1",
            (sector,)).fetchone()

    no_mainline = False
    b = None
    if row:
        d = row["trade_date"]
        branches = _jl(row["branches_json"], []) or []
        b = next((x for x in branches if x.get("name") == branch), None)
    if not b:
        # 无主线（sector 留空/在 sector_chain 里找不到）或该主线本期无此分支 → 按全市场概念兜底：
        # 概念本身在磁盘缓存 sector_map_cache.json 有全市场归属，直接据此取成员。
        rev0 = _sector_reverse()
        if branch in rev0:
            if row:
                d = row["trade_date"]
            else:
                d = date or (DB.conn.execute(
                    "SELECT MAX(trade_date) AS d FROM lhb_stocks").fetchone()["d"])
            b = {"name": branch, "members": [{"code": c, "name": ""} for c in rev0[branch]]}
            src = "cache"
            no_mainline = True
        else:
            return {"available": False, "sector": sector, "branch": branch,
                    "date": (row["trade_date"] if row else None),
                    "message": ("还没有该方向的主线研判数据，请先运行 sector_chain.py。"
                                if not row else
                                "本期方向里没有这个分支（可能已轮换），刷新看板可看最新分支。")}

    members = b.get("members") or []
    if not no_mainline:
        src = "chain"
        if not members:
            members = [{"code": c, "name": ""} for c in _sector_reverse().get(branch, [])]
            src = "cache"
    codes = [m["code"] for m in members]

    # 全市场当日上榜池：用于算「市场共振」wide / 主线内上榜 reso 与共振名单
    lhb_pool = {}
    for r in DB.conn.execute(
            "SELECT security_code, security_name, change_rate, net_amt, reason "
            "FROM lhb_stocks WHERE trade_date=?", (d,)):
        net = _num(r["net_amt"]) or 0.0
        lhb_pool[r["security_code"]] = {
            "name": r["security_name"] or "",
            "pct": _num(r["change_rate"]),
            "net_yi": round(net / 1e8, 2),
            "reason": r["reason"],
        }
    lhb_all = set(lhb_pool.keys())
    rev = _sector_reverse()
    reso = len(set(codes) & lhb_all)
    wide_codes = sorted(set(rev.get(branch, [])) & lhb_all)
    wide = len(wide_codes)
    state = "已扩散" if wide >= 5 else ("局部共振" if wide >= 1 else "尚未启动")
    state_rule = ("全市场同概念当日上龙虎榜 ≥5 只＝已扩散；1–4 只＝局部共振；"
                  "0 只＝尚未启动（与「方向研判报告」口径一致）。")
    wide_list = []
    for c in wide_codes:
        x = lhb_pool[c]
        wide_list.append({
            "code": c, "name": x["name"], "pct": x["pct"], "net_yi": x["net_yi"],
            "reason": x["reason"], "on_mainline": (c in set(codes)),
        })
    wide_list.sort(key=lambda x: -(x["net_yi"] or 0))

    q = _chain_quotes()

    # ① 成员股：涨幅（本地行情快照）+ 是否上榜/净买/上榜原因
    stocks = []
    for m in members[:60]:
        c = m["code"]
        qv = q.get(c) or {}
        stocks.append({"code": c, "name": m.get("name") or qv.get("name") or "",
                       "pct": qv.get("pct"), "price": qv.get("price"),
                       "on_lhb": False, "net_yi": None, "reason": None, "lhb_chg": None})
    by_code = {s["code"]: s for s in stocks}
    if codes:
        ph = ",".join("?" * len(codes))
        for r in DB.conn.execute(
                f"""SELECT security_code, security_name, change_rate, net_amt, reason
                    FROM lhb_stocks WHERE trade_date=? AND security_code IN ({ph})""",
                (d, *codes)):
            s = by_code.get(r["security_code"])
            if not s:
                continue
            s["on_lhb"] = True
            s["net_yi"] = round((_num(r["net_amt"]) or 0.0) / 1e8, 2)
            s["reason"] = r["reason"]
            s["lhb_chg"] = _num(r["change_rate"])
            if not s["name"]:
                s["name"] = r["security_name"] or ""

    # 涨幅/名称兜底（2026-09-29）：行情快照 sector_chain_cache 只覆盖主线成分（~336 只），
    # 全市场概念（如「西部大开发」）的成员大多不在其中 → 涨幅与名称大面积空。
    # 两级兜底：① 龙虎榜自带当日涨跌幅（本地，零成本，上榜股即有）；
    #           ② 仍缺的走 _branch_live_quotes 实时批量行情（120s TTL，失败静默显示 «—»）。
    for s in stocks:
        if s["pct"] is None and s["lhb_chg"] is not None:
            s["pct"] = s["lhb_chg"]
    _miss = [s["code"] for s in stocks if s["pct"] is None or not s["name"]]
    if _miss:
        _live = _branch_live_quotes(_miss)
        for s in stocks:
            x = _live.get(s["code"]) or {}
            if s["pct"] is None and x.get("pct") is not None:
                s["pct"] = x["pct"]
                s["price"] = x.get("price")
            if not s["name"] and x.get("name"):
                s["name"] = x["name"]
    stocks.sort(key=lambda x: -(x["pct"] if x["pct"] is not None else -999))

    # ② 谁在买：成员股上的龙虎榜买方席位（净买 + 该席位历史 3 日上涨概率）
    buyers = []
    if codes:
        for r in DB.conn.execute(
                f"""SELECT security_code, operate_name, side, buy, net, rise_prob_3d
                    FROM lhb_seats WHERE trade_date=? AND security_code IN ({ph})
                    ORDER BY net DESC""", (d, *codes)):
            if (r["side"] or "") != "BUY":
                continue
            net = _num(r["net"]) or 0.0
            if net <= 0:
                continue
            s = by_code.get(r["security_code"]) or {}
            buyers.append({"stock": s.get("name") or r["security_code"],
                           "code": r["security_code"], "name": r["operate_name"] or "—",
                           "net_yi": round(net / 1e8, 2),
                           "buy_yi": round((_num(r["buy"]) or 0.0) / 1e8, 2),
                           "rise_prob_3d": _num(r["rise_prob_3d"])})
    buyers = buyers[:12]

    # ③ 谁在说：该概念（全市场范围）被博主/实盘选手关注、提及的，带原话与链接
    universe = sorted(set(codes) | set(_sector_reverse().get(branch, [])))
    advocates: list[dict] = []
    seen_adv: set = set()
    if universe:
        ph2 = ",".join("?" * len(universe))
        for who_col, tbl in (("player", "positions"), ("author", "trades")):
            for r in DB.conn.execute(
                    f"""SELECT {who_col} AS who, symbol, symbol_name, action, site,
                               post_url, raw_text
                        FROM {tbl} WHERE symbol IN ({ph2}) ORDER BY symbol LIMIT 60""",
                    universe):
                k = (r["who"], r["symbol"], r["action"])
                if k in seen_adv:
                    continue
                seen_adv.add(k)
                advocates.append({"who": r["who"] or "—", "symbol": r["symbol"],
                                  "name": r["symbol_name"] or "", "action": r["action"] or "",
                                  "site": r["site"] or "", "url": r["post_url"] or "",
                                  "text": r["raw_text"] or "", "kind": "持仓/操作",
                                  "on_mainline": r["symbol"] in by_code})
        for r in DB.conn.execute(
                f"""SELECT m.symbol, m.symbol_name, m.context, p.author AS who, p.site,
                           p.id AS pid
                    FROM stock_mentions m LEFT JOIN posts p ON p.id = m.post_id_ref
                    WHERE m.symbol IN ({ph2}) LIMIT 60""", universe):
            k = (r["who"], r["symbol"], "mention")
            if k in seen_adv:
                continue
            seen_adv.add(k)
            advocates.append({"who": r["who"] or "—", "symbol": r["symbol"],
                              "name": r["symbol_name"] or "", "action": "提及",
                              "site": r["site"] or "", "url": "", "text": r["context"] or "",
                              "kind": "讨论帖", "on_mainline": r["symbol"] in by_code})
    advocates = advocates[:24]

    # ④ 大致判断：全部来自上面几块的真实计数，不引入任何外部假设
    pcts = [s["pct"] for s in stocks if s["pct"] is not None]
    n_zt = sum(1 for p in pcts if p >= 9.8)
    n_up = sum(1 for p in pcts if p > 0)
    n_dn = sum(1 for p in pcts if p < 0)
    lhb_stocks = [s for s in stocks if s["on_lhb"]]
    net_sum = round(sum(s["net_yi"] or 0.0 for s in lhb_stocks), 2)
    probs = [x["rise_prob_3d"] for x in buyers if x["rise_prob_3d"] is not None]
    prob_avg = round(sum(probs) / len(probs), 1) if probs else None
    # 候选（还没怎么动的）：分支已启动、但它自己涨幅 <3%，属"同方向补涨"观察位
    cands = [s for s in stocks if (s["pct"] is not None and s["pct"] < 3)]
    if n_zt >= 2 or (n_zt >= 1 and len(lhb_stocks) >= 3):
        tone = "hot"
    elif n_up > n_dn or lhb_stocks:
        tone = "warm"
    else:
        tone = "cool"
    parts = [f"与「{sector}」重合 {b.get('overlap')} 只"]
    if n_zt:
        parts.append(f"其中 {n_zt} 只涨停")
    parts.append(f"{n_up} 涨 / {n_dn} 跌")
    if lhb_stocks:
        parts.append(f"{len(lhb_stocks)} 只上龙虎榜、合计净买 {net_sum} 亿")
    if prob_avg is not None:
        parts.append(f"买方席位历史 3 日上涨概率均值 {prob_avg}%")
    if not advocates:
        parts.append("该分支暂无博主/选手覆盖")
    judge = {"tone": tone, "text": "；".join(parts) + "。",
             "n_zt": n_zt, "n_up": n_up, "n_dn": n_dn, "n_lhb": len(lhb_stocks),
             "net_sum_yi": net_sum, "prob_avg": prob_avg,
             "n_candidates": len(cands),
             "candidates": [{"code": s["code"], "name": s["name"], "pct": s["pct"]}
                            for s in cands[:8]],
             "basis": [
                 "成员 = 该概念成分股 ∩ 主线成分股（东财 F10，单板块上限 300 只）",
                 "净买 / 上榜原因来自交易所龙虎榜",
                 "席位上涨概率 = 该营业部/机构的历史统计，非个股预测",
                 "博主覆盖来自本地库持仓与讨论帖，与龙虎榜方向未必重叠",
             ]}

    return {"available": True, "sector": sector, "branch": branch, "date": d,
            "sector_label": ("（全市场概念，无主线归属）" if no_mainline else sector),
            "overlap": (None if no_mainline else b.get("overlap")),
            "reso": reso, "wide": wide, "state": state, "state_rule": state_rule,
            "wide_list": wide_list,
            "members_source": src,
            "members_total": len(members), "stocks": stocks,
            "buyers": buyers, "advocates": advocates, "judge": judge,
            "note": "仅为方向参考，非买卖建议。"}
