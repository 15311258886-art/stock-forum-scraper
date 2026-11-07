# -*- coding: utf-8 -*-
"""signal 域 API 处理函数（从 viewer/server.py 物理抽取，2026-11-07 拆分重构）。

抽取规则：函数体逻辑零改动；依赖的 server 模块级符号经 `from viewer.server import (...)`
重定向进本地命名空间；标准库本地导入。server.py 末尾 re-export 保持命名空间兼容，
routes.py 与既有测试（S.api_*）无需改动。
"""
from __future__ import annotations

import logging
from viewer.server import (  # noqa: E402,F401
    DB,
    KPARAMS,
)

def api_signal_evidence(fwd: int = 5) -> dict:
    """信号战绩（2026-10-29）：大样本回测 + 全市场基线，给前端"贴标签"用。

    与 /api/signal/backtest 的分工：那个吐原始实证表（17 信号 × 3 窗口的裸数字，供核对）；
    本接口只给结论 —— 每条信号一行，带超额胜率、超额收益、裁决（有效/中性/反向/样本不足）。

    为什么必须带超额：全市场前向 5 日胜率本身只有 46.12%（n≈129 万，见 signal_baseline）。
    绝对胜率 48% 看着过半，实际跑输基线 2 个百分点 —— 只印胜率等于误导。
    """
    try:
        from viewer.kernel import evidence as EV
        return EV.build(DB.conn, fwd)
    except Exception:  # noqa: BLE001
        logging.exception("api_signal_evidence 失败")
        return {"ok": False, "err": "证据层不可用", "signals": [], "baseline": {}}
def api_signal_regime(fwd: int = 10) -> dict:
    """分市况的信号表现（2026-10-29）。

    与 /api/signal/evidence 的分工必须分清，否则会误读：
      evidence 回答「这条信号整体有没有用」（全样本混算）；
      regime   回答「在当前市况里，它历史上好还是差」（同状态内比较）。
    两者可以给出相反结论 —— 实测就是：全样本几乎无 alpha，但追涨类在弱势市
    有稳定的负超额（-7pt 量级），而这类负超额在混算里被逆势类的正超额抵消掉了。
    所以前端必须把「当前市况」放在最显眼处，别让人把整体结论当成当前结论。

    另外如实报告 **有效独立窗口数**（交易日数 / fwd）：相邻交易日的 fwd 日窗口
    重叠 fwd-1 天，n 笔样本远不等于 n 个独立观测。用它决定这张表该被当成
    「结论」还是「方向参考」。
    """
    try:
        from viewer import regime as RG
        con = DB.conn
        cur = RG.current(con)
        bmap = RG.load_baseline(con)

        try:
            _nd = con.execute("SELECT COUNT(DISTINCT trade_date) FROM market_regime").fetchone()[0] or 0
        except Exception:  # noqa: BLE001
            _nd = 0
        eff = round(_nd / float(fwd), 1) if fwd else 0.0
        grade = "可信" if eff >= 60 else ("仅供方向参考" if eff >= 20 else "不足以下结论")

        cols = [d[1] for d in con.execute("PRAGMA table_info(signal_regime)").fetchall()]
        if not cols:
            return {"ok": False, "err": "还没有分市况回测数据，先跑 "
                                        "`python -m viewer.signal_backtest --diagnose`",
                    "signals": [], "baseline": {}, "current": cur}
        has_stab = "stab" in cols
        sel = ("reason,direction,fwd,regime,n,winrate_net,avg_ret_net,sharpe_net,"
               "edge_wr,edge_ret" + (",stab" if has_stab else ",NULL"))
        rows = []
        for r in con.execute("SELECT " + sel + " FROM signal_regime WHERE fwd=?", (fwd,)):
            rows.append({
                "reason": r[0], "direction": r[1], "fwd": r[2], "regime": r[3],
                "n": r[4],
                "winrate_net": round(float(r[5]), 4) if r[5] is not None else None,
                "avg_ret_net": round(float(r[6]), 4) if r[6] is not None else None,
                "sharpe_net": round(float(r[7]), 3) if r[7] is not None else None,
                "edge_wr": round(float(r[8]), 4) if r[8] is not None else None,
                "edge_ret": round(float(r[9]), 5) if r[9] is not None else None,
                "stab": r[10],
            })

        base = {}
        for (f, reg), v in bmap.items():
            if int(f) != int(fwd):
                continue
            base[reg] = {"n": v.get("n"),
                         "winrate_net": round(float(v.get("winrate_net") or 0), 4),
                         "avg_ret_net": round(float(v.get("avg_ret_net") or 0), 4)}

        return {"ok": True, "fwd": fwd, "current": cur, "baseline": base,
                "signals": rows, "days": _nd, "eff_windows": eff, "grade": grade,
                "regimes": [{"key": k, "cn": RG.REGIME_CN[k]} for k in RG.REGIMES]}
    except Exception:  # noqa: BLE001
        logging.exception("api_signal_regime 失败")
        return {"ok": False, "err": "分市况数据不可用", "signals": [], "baseline": {}}
def api_signal_combo(fwd=None, limit=60):
    """/api/signal/combo —— 信号两两交集（决策③）。

    只返回 usable=1（样本 ≥ MIN_COMBO_N）的行，并**把被排除的条数一起说出来**：
    「有 N 个组合因样本不足未列入」本身就是结论 —— 它说明大多数看起来不错的组合
    其实只出现在几十个样本里，禁不起下一年的检验。

    verdict 的四分类是刻意设计的，因为「超额为正」有四种完全不同的成因：
      · 候选      —— 两个超额都为正且 q<0.05，组合本身可能真有增量
      · 赚的是市况 —— 相对基线为正、相对成分却不为正，说明组合并不比单用更好
      · 不显著    —— BH 校正后 q≥0.05，属于「检验了 216 次」的产物
      · 无效      —— 其余
    只报第一个，等于把后三类也包装成好消息。
    """
    out = {"ok": True, "rows": [], "excluded": 0, "total": 0, "note": "", "calc_version": ""}
    try:
        _cols = [d[1] for d in DB.conn.execute("PRAGMA table_info(signal_combo)").fetchall()]
        if not _cols:
            out["note"] = "交集表尚未生成（需先跑一次全量回测）"
            return out
        where, args = ["usable=1"], []
        if fwd:
            where.append("fwd=?")
            args.append(int(fwd))
        rows = DB.conn.execute(
            "SELECT reason_a,reason_b,direction,fwd,n,winrate_net,avg_ret_net,sharpe_net,"
            "edge_vs_base,edge_vs_parts,q_bh,part_a_ret_net,part_b_ret_net,calc_version "
            "FROM signal_combo WHERE " + " AND ".join(where) +
            " ORDER BY edge_vs_base DESC LIMIT ?", args + [int(limit)]).fetchall()
        out["excluded"] = int(DB.conn.execute(
            "SELECT COUNT(*) FROM signal_combo WHERE usable=0").fetchone()[0])
        for r in rows:
            evb, evp, q = r[8], r[9], r[10]
            if (evb or 0) > 0 and (evp is not None and evp <= 0):
                verdict = "赚的是市况"
            elif q is not None and q < 0.05 and (evb or 0) > 0 and (evp or 0) > 0:
                verdict = "候选"
            elif q is not None and q >= 0.05:
                verdict = "不显著"
            else:
                verdict = "无效"
            out["rows"].append({
                "a": r[0], "b": r[1], "direction": r[2], "fwd": r[3], "n": r[4],
                "winrate_net": r[5], "avg_ret_net": r[6], "sharpe_net": r[7],
                "edge_vs_base": evb, "edge_vs_parts": evp, "q_bh": q,
                "part_a": r[11], "part_b": r[12], "verdict": verdict,
            })
        out["total"] = len(out["rows"])
        if rows:
            out["calc_version"] = rows[0][13] or ""
        out["note"] = ("交集 = 同一天同一只票上、两个**同方向**信号同时触发。"
                       "「超额(基准)」相对全市场同期；「超额(成分)」相对这两个信号单独用的平均。"
                       "两个都为正才说明组合本身有价值；只有前者为正说明赚的是市况。"
                       "本表只列样本 ≥ %d 的组合，另有 %d 个因样本不足未列入。"
                       % (KPARAMS.MIN_COMBO_N, out["excluded"]))
    except Exception as e:  # noqa: BLE001
        logging.exception("api_signal_combo 失败")
        return {"ok": False, "error": str(e)}
    return out
def api_signal_backtest(limit=200):
    """P4：信号 alpha 回测实证结果（17 信号 × 3 前向窗口 = 51 行）。
    这是 viewer/config.SIGNAL_WEIGHTS 的实证来源；此前只落库、前端无入口查看，
    等于「数据有、展示未接」。本接口把原始实证表开放给前端（展示层渲染待补），
    并附带当前被消费的权重，便于核对「实证 → 权重」链路是否一致。"""
    out = {"ok": True, "rows": [], "summary": {}, "weights": {}}
    try:
        # 2026-10-29：口径改为**税后**。税前超额里有一整块是交易成本幻觉——
        # 实测 T+1 平均仅 -0.586%，双边 0.15% 足以把"微幅正超额"翻成负。
        # 老库（未跑新版回测）没有 *_net 列时自动退回税前，不报错。
        _cols = [d[1] for d in DB.conn.execute("PRAGMA table_info(signal_backtest)").fetchall()]
        _has_net = ("winrate_net" in _cols) and ("avg_ret_net" in _cols)
        _sel = ("reason,direction,fwd,n,winrate,avg_ret,sharpe,updated,fee,"
                "winrate_net,avg_ret_net,sharpe_net" if _has_net
                else "reason,direction,fwd,n,winrate,avg_ret,sharpe,updated,0,NULL,NULL,NULL")
        rows = DB.conn.execute(
            "SELECT " + _sel + " FROM signal_backtest ORDER BY sharpe DESC LIMIT ?", (limit,)).fetchall()
        # 同期基线：把绝对胜率/收益换算成超额。缺了这一步，48% 的胜率会被误读成"过半"，
        # 而全市场 5 日基线胜率本就是 46.12%（n≈129 万）—— 那是跑输。
        _base = {}
        _fee = 0.0
        _EV = None
        try:
            from viewer.kernel import evidence as _EV
            _bcols = [d[1] for d in DB.conn.execute("PRAGMA table_info(signal_baseline)").fetchall()]
            _bnet = ("winrate_net" in _bcols) and ("avg_ret_net" in _bcols)
            _bsel = ("fwd, winrate, avg_ret, fee, winrate_net, avg_ret_net" if _bnet
                     else "fwd, winrate, avg_ret, 0, NULL, NULL")
            for _b in DB.conn.execute("SELECT " + _bsel + " FROM signal_baseline"):
                _wr_n = float(_b[4]) if _b[4] is not None else float(_b[1] or 0)
                _ret_n = float(_b[5]) if _b[5] is not None else float(_b[2] or 0)
                _base[int(_b[0])] = (float(_b[1] or 0), float(_b[2] or 0), _wr_n, _ret_n)
                _fee = float(_b[3] or 0) or _fee
        except Exception:  # noqa: BLE001
            pass
        out["baseline"] = {str(k): {"winrate": round(v[0], 4), "avg_ret": round(v[1], 4),
                                    "winrate_net": round(v[2], 4), "avg_ret_net": round(v[3], 4)}
                           for k, v in _base.items()}
        out["fee"] = round(_fee, 6)
        out["net_of_fee"] = bool(_fee > 0)

        def _row(r):
            b = _base.get(int(r[2]), (0.0, 0.0, 0.0, 0.0))
            wr_net = float(r[9]) if r[9] is not None else float(r[4] or 0)
            ret_net = float(r[10]) if r[10] is not None else float(r[5] or 0)
            sh_net = float(r[11]) if r[11] is not None else float(r[6] or 0)
            ew = wr_net - b[2]
            er = ret_net - b[3]
            _n = int(r[3] or 0)
            v = _EV.judge(_n, ew, ret_net, sh_net) if _EV else ""
            return {"signal": r[0], "direction": r[1], "fwd": r[2], "n": r[3],
                    "winrate": r[4], "avg_ret": r[5], "sharpe": r[6], "updated": r[7],
                    "winrate_net": round(wr_net, 4), "avg_ret_net": round(ret_net, 4),
                    "sharpe_net": round(sh_net, 3),
                    "edge_wr": round(ew, 4), "edge_ret": round(er, 4),
                    "verdict": v, "verdict_label": (_EV.LABEL.get(v, v) if _EV else ""),
                    # 2026-10-31 决策①：本面板原来自己判 verdict、没有分层标签。
                    # 现在统一由 evidence.tier_info 给，别在这里手拼 tier 字段。
                    **(_EV.tier_info(_n) if _EV else {})}

        out["rows"] = [_row(r) for r in rows]
        if _EV:
            out["tiering"] = _EV.tier_legend()
        # 当前被消费的权重（config.SIGNAL_WEIGHTS），核对实证→权重链路
        try:
            import config as _cfg
            out["weights"] = {k: v for k, v in getattr(_cfg, "SIGNAL_WEIGHTS", {}).items()}
        except Exception:
            pass
        pos = sum(1 for x in out["rows"] if x["avg_ret_net"] > 0)
        neg = sum(1 for x in out["rows"] if x["avg_ret_net"] < 0)
        out["summary"] = {
            "total": len(rows),
            "positive_alpha": pos, "negative_alpha": neg,
            "best": (rows[0][0] if rows else None),
            "worst": (rows[-1][0] if rows else None),
        }
    except Exception as e:  # noqa: BLE001
        out["ok"] = False; out["error"] = str(e)
    return out
