# -*- coding: utf-8 -*-
"""lhb 域 API 处理函数（从 viewer/server.py 物理抽取，2026-11-07 拆分重构）。

抽取规则：函数体逻辑零改动；依赖的 server 模块级符号经 `from viewer.server import (...)`
重定向进本地命名空间；标准库本地导入。server.py 末尾 re-export 保持命名空间兼容，
routes.py 与既有测试（S.api_*）无需改动。
"""
from __future__ import annotations

import json, logging, threading
from viewer.server import (  # noqa: E402,F401
    DB,
    _LHBW_CONSTS,
    _lhb_depts_top,
    _lhb_inst_top,
    _lhb_recent_dates,
    _lhbw_init,
    _lhbw_num,
    _lhbw_pct,
    _lhbw_row_dict,
    _sector_outlook,
)

def api_lhb(date: str | None = None, window_days: int = 5) -> dict:
    """游资龙虎榜（4 维度）。

    数据来源：东方财富 datacenter-web 免鉴权接口（交易所盘后披露，T 日 16:30 后可取）。
    date 传 'auto'/空 = 返回库里最新交易日。window_days = 席位榜/机构榜的滚动窗口（交易日数）。

    返回：
      stocks    当日个股榜单（含 d1/d2/d5/d10/d20/d30 上榜后表现）
      seats     当日买卖前 5 席位明细
      depts_top 游资席位榜（以营业部为轴，滚动 window_days 日）
      inst_top  机构席位榜（以个股为轴，滚动 window_days 日）
    """
    dates = [r[0] for r in DB.conn.execute(
        "SELECT DISTINCT trade_date FROM lhb_stocks ORDER BY trade_date DESC LIMIT 15").fetchall()]
    if not dates:
        return {"dates": [], "date": "", "stocks": [], "seats": [], "empty": True}
    d = date if date in dates else dates[0]
    stocks = [dict(r) for r in DB.conn.execute(
        """SELECT trade_date, security_code, security_name, market, close_price,
                  change_rate, turnoverrate, buy_amt, sell_amt, net_amt,
                  deal_amt, deal_net_ratio, reason, explain,
                  d1_chg, d2_chg, d5_chg, d10_chg, deal_amount_ratio, net_bs_amt
           FROM lhb_stocks WHERE trade_date = ?
           ORDER BY net_amt DESC""", (d,)).fetchall()]
    seats = [dict(r) for r in DB.conn.execute(
        """SELECT trade_date, security_code, operate_code, operate_name, side,
                  buy, sell, net, rise_prob_3d
           FROM lhb_seats WHERE trade_date = ?
           ORDER BY security_code, side, net DESC""", (d,)).fetchall()]
    # 统计该日机构席位净买（供前端汇总条）
    inst = DB.conn.execute(
        """SELECT SUM(CASE WHEN side='BUY' THEN net ELSE 0 END) AS buy_net,
                  SUM(CASE WHEN side='SELL' THEN net ELSE 0 END) AS sell_net
           FROM lhb_seats WHERE trade_date = ? AND (operate_name LIKE '%机构专用%'
              OR operate_name LIKE '%基金%' OR operate_name LIKE '%社保%'
              OR operate_name LIKE '%QFII%' OR operate_name LIKE '%保险%')""", (d,)).fetchone()
    return {"dates": dates, "date": d, "stocks": stocks, "seats": seats,
            "depts_top": _lhb_depts_top(d, window_days),
            "inst_top": _lhb_inst_top(d, window_days),
            "window_days": window_days,
            "window_dates": _lhb_recent_dates(d, window_days, "lhb_depts"),
            "inst_buy_net": (inst["buy_net"] if inst else None) or 0,
            "inst_sell_net": (inst["sell_net"] if inst else None) or 0}
def api_lhb_sectors(date: str | None = None) -> dict:
    """龙虎榜「板块动向」——资金在哪些板块出现多点共振，及其扩散验证。

    与 lhb_stocks（个股榜）是两个独立口径：
      - 个股榜回答「哪只票被买」
      - 板块动向回答「资金是不是成片扫某个方向」
    板块聚集度（上榜股数多 + 净买额大）才有前瞻意义；单只票的上榜不构成方向。

    数据由 lhb_sector_link.py 离线计算后落库（lhb_sector_focus），本接口只读，不重算。
    """
    try:
        rows = [dict(r) for r in DB.conn.execute(
            """SELECT trade_date, sector, net_amt, onlist_n, onlist_json, members_n, sampled,
                      t1_avg, t3_avg, t1_win, bench_t1, bench_t3, updated_at
               FROM lhb_sector_focus ORDER BY trade_date DESC, net_amt DESC""").fetchall()]
    except Exception:  # noqa: BLE001 —— 表不存在时降级为空（首次尚未跑过分析脚本）
        return {"dates": [], "date": "", "rows": [], "summary": {}, "empty": True}
    if not rows:
        return {"dates": [], "date": "", "rows": [], "summary": {}, "empty": True}

    dates = sorted({r["trade_date"] for r in rows}, reverse=True)
    d = date if date in dates else dates[0]
    for r in rows:
        try:
            r["onlist"] = json.loads(r.pop("onlist_json") or "[]")
        except Exception:  # noqa: BLE001
            r["onlist"] = []
        if r.get("t1_avg") is not None and r.get("bench_t1") is not None:
            r["excess_t1"] = round(r["t1_avg"] - r["bench_t1"], 2)
        if r.get("t3_avg") is not None and r.get("bench_t3") is not None:
            r["excess_t3"] = round(r["t3_avg"] - r["bench_t3"], 2)
    cur = [r for r in rows if r["trade_date"] == d]
    summary = {}
    try:
        mv = DB.conn.execute("SELECT value FROM meta WHERE key='lhb_sector_summary'").fetchone()
        if mv and mv[0]:
            summary = json.loads(mv[0])
    except Exception:  # noqa: BLE001
        summary = {}
    out = {"dates": dates, "date": d, "rows": cur, "summary": summary,
           "basis": "板块内【未上榜】成分股在上榜日之后的 T+1/T+3 平均涨幅，对比同期上证",
           "updated_at": rows[0].get("updated_at") or ""}
    # 方向研判报告：三段（为什么涨 / 牵涉哪些板块 / 接下来看什么）供前端在表格下方分节渲染。
    out["outlook"] = _sector_outlook(d)
    return out
def api_lhb_weather(days: int = 120) -> dict:
    """龙虎榜资金风向（2026-10-30 D35 阶段二）：**只读 `lhb_weather_daily`，绝不现算**。

    为什么必须只读表：全历史 `compute()` 实测 110 秒（要扫 1382 万行日线 + 4084 个上榜日
    的滚动分位）。放在请求线程里跑，就是 D32 那个「单接口 45 秒超时、全站被 GIL 冻住」的
    同一个坑。计算只在 `python -m viewer.lhb_weather --save` 时做一次，请求侧永远只读。

    产品口径（这是**描述性**指标，不是信号）：
      · 返回体里刻意没有 buy/sell/direction/score 之外的任何"该怎么做"字段；
      · 结构与集中度只有 27/25 天样本（`sample_days_*`），远低于 `MIN_SAMPLE_FOR_STATE`，
        所以它们的 `pct` 恒为 None、并带 `badge` 说明"仅供参考、不参与判定" ——
        **不许拿 27 天算出一个"250 日分位"来充数**，那是编数据；
      · `disclaimer` 原样回前端，面板底部必须显示。

    参数 `days`：曲线长度（20~400，默认 120）。分位窗口固定 250，与 `lhb_weather.HIST` 一致，
    所以实际取 `max(days, 251)` 行：前 250 行只用于算今日分位，不进 `series`。
    """
    try:
        _lhbw_init()
        from viewer import lhb_weather as LW
        hist, min_hist, min_sample = _LHBW_CONSTS
        days = max(20, min(int(days or 120), 400))
        n_fetch = max(days, hist + 1)

        con = DB.conn

        def _fetch(sql, args=()):
            """执行查询并返回 [(cols, vals), ...]，不碰 `DB.conn` 的 row_factory。"""
            cur = con.execute(sql, args)
            cols = [d[0] for d in (cur.description or [])]
            return cols, cur.fetchall()

        exists = con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
            (LW.TABLE,)).fetchone()
        if not exists:
            return {"ok": False, "not_ready": True, "days": days, "series": [], "dims": [],
                    "error": "资金风向尚未落库",
                    "note": "需要先跑一次：python -m viewer.lhb_weather --save"
                            "（只读表，不在请求里现算）"}

        cols, fetched = _fetch(
            "SELECT * FROM %s ORDER BY trade_date DESC LIMIT ?" % LW.TABLE, (n_fetch,))
        if not fetched:
            return {"ok": False, "not_ready": True, "days": days, "series": [], "dims": [],
                    "error": "资金风向表是空的",
                    "note": "需要先跑一次：python -m viewer.lhb_weather --save"}
        rows = [_lhbw_row_dict(cols, v) for v in reversed(fetched)]   # → 时间升序

        # 「今日」= 最后一行**有分值**的日期，不是表里最后一行。
        #   实测 2026-10-30 就是反例：当天已有 48 只上榜，但全市场成交额（kline_daily.db）
        #   和分母（stock_indicators_daily）都还没到，于是 score=NULL、denom_src=fallback。
        #   如果直接取 rows[-1] 当"今天"，前端会显示 score=—、档位=未知 的空温度计，
        #   看上去像功能坏了。取最后一个有分值的日期，再把"表里更新的那行还没算完"如实报出来。
        idx_today = -1
        for i in range(len(rows) - 1, -1, -1):
            if _lhbw_num(rows[i].get("score")) is not None:
                idx_today = i
                break
        if idx_today < 0:
            return {"ok": False, "not_ready": True, "days": days, "series": [], "dims": [],
                    "error": "资金风向表里还没有可定档的日期",
                    "note": "多半是历史太短（阈值需 %d 个交易日）；等下一次 --save" % min_hist}
        today = rows[idx_today]
        latest = rows[-1]
        pending = None
        if str(latest.get("trade_date") or "") != str(today.get("trade_date") or ""):
            pending = {
                "trade_date": str(latest.get("trade_date") or ""),
                "n_listed": _lhbw_num(latest.get("n_listed")),
                "denom_src": str(latest.get("denom_src") or ""),
                "reason": "该日全市场成交额或分母尚未到齐，暂不计分（不计入曲线）",
            }

        # ── 今日分位：只用今日之前的 250 行（窗口不含当日）──────────
        prior = [_lhbw_num(r.get("score")) for r in rows[:idx_today]][-hist:]
        score = _lhbw_num(today.get("score"))
        score_pct = _lhbw_pct(prior, score)

        def _fmt_pct(x, nd=2, signed=True):
            """比值 → 百分数（×100）。用于 list_ratio / net_amt_ratio / *_share / d1_win_20。"""
            v = _lhbw_num(x)
            if v is None:
                return "—"
            return ("%+.*f%%" if signed else "%.*f%%") % (nd, v * 100.0)

        def _fmt_pp(x, nd=2, signed=True):
            """**已经是百分数**的值，只补小数点。

            实测坑：`lhb_stocks.d1_chg` 存的就是百分数（9.25925926 = +9.26%，
            全表 min/max = −92.78 / +177.74），所以 `d1_mean_20` 也是百分数。
            拿它再过一遍 ×100 会印出「−123.47%」这种不存在的涨幅（本项目第一版就踩了）。
            """
            v = _lhbw_num(x)
            if v is None:
                return "—"
            return ("%+.*f" if signed else "%.*f") % (nd, v) + "%"

        def _fmt_yi(x):
            v = _lhbw_num(x)
            return "—" if v is None else "%+.2f 亿" % (v / 1e8)

        n_listed = _lhbw_num(today.get("n_listed"))
        denom = _lhbw_num(today.get("denom"))
        dims = [
            {
                "key": "participation", "label": "参与度", "in_state": True,
                "rows": [
                    ["上榜占全市场", _fmt_pct(today.get("list_ratio"), 2, False),
                     ("%d / %d 只" % (int(n_listed), int(denom))) if n_listed and denom else "—"],
                    ["净买入占成交额", _fmt_pct(today.get("net_amt_ratio"), 3, False),
                     "净买入 " + _fmt_yi(today.get("net_amt"))],
                ],
                "pct": _lhbw_num(today.get("part_pct")), "pct_label": "参与度 · 近一年分位",
                "badge": None, "sample_days": None,
            },
            {
                "key": "effect", "label": "赚钱效应", "in_state": True,
                "rows": [
                    ["上榜股次日均涨", _fmt_pp(today.get("d1_mean_20")),
                     "近 20 个上榜日均值，已滞后 1 个交易日"],
                    ["次日上涨占比", _fmt_pct(today.get("d1_win_20"), 1, False), ""],
                ],
                "pct": _lhbw_num(today.get("effect_pct")), "pct_label": "赚钱效应 · 近一年分位",
                "badge": None, "sample_days": None,
            },
            {
                "key": "structure", "label": "结构", "in_state": False,
                "rows": [
                    ["机构净买入占比", _fmt_pct(today.get("inst_share"), 1),
                     "机构专用 %s ｜ 游资 %s" % (_fmt_yi(today.get("inst_net")),
                                                _fmt_yi(today.get("hot_net")))],
                ],
                "pct": None, "pct_label": None,
                "badge": "样本 %s 天，仅供参考，不参与判定"
                         % int(_lhbw_num(today.get("sample_days_structure")) or 0),
                "sample_days": int(_lhbw_num(today.get("sample_days_structure")) or 0),
            },
            {
                "key": "concentration", "label": "集中度", "in_state": False,
                "rows": [
                    ["前三席位集中度", _fmt_pct(today.get("top3_share"), 1, False),
                     ("HHI %.3f" % _lhbw_num(today.get("hhi"))) if _lhbw_num(today.get("hhi")) is not None
                     else "HHI —"],
                ],
                "pct": None, "pct_label": None,
                "badge": "样本 %s 天，仅供参考，不参与判定"
                         % int(_lhbw_num(today.get("sample_days_sector")) or 0),
                "sample_days": int(_lhbw_num(today.get("sample_days_sector")) or 0),
            },
        ]

        series = []
        for d in rows[max(0, idx_today - days + 1):idx_today + 1]:
            series.append({"trade_date": str(d.get("trade_date") or ""),
                           "score": _lhbw_num(d.get("score")),
                           "state": str(d.get("state") or "unknown")})

        state = str(today.get("state") or "unknown")
        return {
            "ok": True,
            "asof": str(today.get("trade_date") or ""),
            "days": days,
            "hist": hist,
            "today": {
                "trade_date": str(today.get("trade_date") or ""),
                "score": score,
                "score_pct": score_pct,
                "state": state,
                "state_cn": {"hot": "偏热", "neutral": "中性", "cold": "偏冷"}.get(state, "未知"),
                "n_listed": (int(round(n_listed)) if n_listed is not None else None),
                "denom": (int(round(denom)) if denom is not None else None),
                "denom_src": str(today.get("denom_src") or ""),
                "q33": (round(_lhbw_num(today.get("q33")), 2)
                        if _lhbw_num(today.get("q33")) is not None else None),
                "q67": (round(_lhbw_num(today.get("q67")), 2)
                        if _lhbw_num(today.get("q67")) is not None else None),
                "updated_at": str(today.get("updated_at") or ""),
            },
            "dims": dims,
            "series": series,
            "pending": pending,
            "sample_days": {
                "structure": dims[2]["sample_days"],
                "sector": dims[3]["sample_days"],
            },
            "min_sample_for_state": min_sample,
            # 面板底部必须原样显示（口径边界，不是客套话）
            "disclaimer": "本指标描述资金活跃度，不构成个股买卖依据。",
            "note": "口径：上榜家数/全市场有成交家数 = 参与度；上榜股次日涨跌幅（滞后 1 个交易日）"
                    "= 赚钱效应。两者各取近 250 个交易日的分位、等权合成 score，再按滚动 250 日"
                    " 33/67 分位切三档。结构与集中度样本不足 %d 天，只展示不参与判定。"
                    % min_sample,
        }
    except Exception as e:  # noqa: BLE001
        logging.exception("api_lhb_weather 失败")
        return {"ok": False, "days": days, "series": [], "dims": [],
                "error": "资金风向读取失败：%s" % str(e)[:160]}
def api_lhb_backfill(payload: dict | None = None) -> dict:
    """POST /api/lhb/backfill：后台回填龙虎榜 3 年历史（需联网；端点+任务骨架）。

    实际回填依赖东财 lhb 接口联网抓取；本端点只接收请求并启动后台任务，
    返回「已启动」。联网验证见 P1-29 / 体检报告 P1。
    """
    payload = payload or {}
    try:
        def _job():
            try:
                import lhb_daily as _lhb
                if hasattr(_lhb, "backfill_3y"):
                    _lhb.backfill_3y()
                elif hasattr(_lhb, "backfill"):
                    _lhb.backfill()
                else:
                    logging.warning("lhb_daily 无 backfill 入口，回填未执行（需确认接口）")
            except Exception:  # noqa: BLE001
                logging.exception("龙虎榜回填任务失败（需联网）")
        threading.Thread(target=_job, name="lhb-backfill", daemon=True).start()
        return {"ok": True, "started": True,
                "note": "龙虎榜 3 年历史回填已启动（后台；需联网，失败请查看日志）"}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)[:200]}
