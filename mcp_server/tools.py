"""mcp_server/tools.py — 暴露给 LLM Agent 的只读查询工具集。

共 12 个工具，覆盖规划文档 A 方案四类核心（情绪/荐股/共振/大V）并扩展至
帖子、新闻公告、统一信号、基本面、每日精选、龙虎榜、信源信誉。
全部为 SELECT，零写入；返回结构均可被 json.dumps 序列化。
"""
from . import db

# ---------------------------------------------------------------------------
# 工具元数据：MCP tools/list 返回给客户端，决定 Agent 如何调用
# ---------------------------------------------------------------------------
TOOLS = [
    {
        "name": "resolve_stock",
        "description": "按代码/名称/公司关键字搜索 A 股，返回匹配股票列表（code/name/market）。"
                       "后续所有'个股类'工具都接受代码或名称输入，会先经此解析。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "股票代码(6位)或名称/公司关键字，如 600519、茅台、贵州茅台"},
                "limit": {"type": "integer", "description": "返回条数上限", "default": 20}
            },
            "required": ["query"]
        }
    },
    {
        "name": "query_stock_sentiment",
        "description": "查询某只股票的论坛整体情绪：近 N 天情绪分布、平均情绪分、每日趋势、近期帖子样例。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "symbol": {"type": "string", "description": "股票代码或名称，如 600519 或 茅台"},
                "days": {"type": "integer", "description": "回看天数", "default": 30}
            },
            "required": ["symbol"]
        }
    },
    {
        "name": "list_posts",
        "description": "检索论坛帖子（股吧/雪球/淘股吧等），可按个股、站点、情绪过滤。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "symbol": {"type": "string", "description": "可选，按股票代码或名称关联"},
                "site": {"type": "string", "description": "可选，站点过滤，如 eastmoney/ths/xueqiu/taoguba"},
                "sentiment": {"type": "string", "description": "可选，情绪过滤，如 positive/negative/neutral"},
                "days": {"type": "integer", "description": "回看天数", "default": 30},
                "limit": {"type": "integer", "description": "返回条数上限", "default": 30}
            }
        }
    },
    {
        "name": "list_recommendations",
        "description": "列出 B 档外部荐股（机构/大V 荐股，带回测 pnl）。可按板块、方向过滤。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "sector": {"type": "string", "description": "可选，板块/行业关键字"},
                "direction": {"type": "string", "description": "可选，方向：看多/看空/中性"},
                "limit": {"type": "integer", "description": "返回条数上限", "default": 30}
            }
        }
    },
    {
        "name": "get_resonance",
        "description": "查询某股票的多源共振：按源类型(博主/战法/席位/情绪/引擎)与方向聚合的信号概览 + 明细。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "symbol": {"type": "string", "description": "股票代码或名称"}
            },
            "required": ["symbol"]
        }
    },
    {
        "name": "top_influencers",
        "description": "大V/博主/关键人物排行榜：评分、层级、胜率、平均收益、最佳调用等。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "kind": {"type": "string", "description": "可选，person_kind 过滤，如 自然人大V/机构/媒体"},
                "n": {"type": "integer", "description": "Top N", "default": 20},
                "by": {"type": "string", "description": "排序字段：score(默认) 或 ret(平均收益)"}
            }
        }
    },
    {
        "name": "get_signals",
        "description": "统一信号查询（多源：博主/战法/席位/情绪/引擎），可按个股、源类型、方向过滤。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "symbol": {"type": "string", "description": "可选，股票代码或名称"},
                "src_type": {"type": "string", "description": "可选，如 blogger/strategy/lhb/sentiment/engine/bili"},
                "direction": {"type": "string", "description": "可选，多/空/中性"},
                "limit": {"type": "integer", "description": "返回条数上限", "default": 50}
            }
        }
    },
    {
        "name": "get_news",
        "description": "新闻/公告检索：kind=news 取财经新闻流；kind=ann 取个股公告(支持按股票过滤)。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "kind": {"type": "string", "description": "news 或 ann，默认 news"},
                "symbol": {"type": "string", "description": "可选，仅 ann 时按股票过滤"},
                "days": {"type": "integer", "description": "回看天数(仅 news 生效)", "default": 14},
                "limit": {"type": "integer", "description": "返回条数上限", "default": 30}
            }
        }
    },
    {
        "name": "get_fundamentals",
        "description": "查询个股基本面（ROE/毛利率/营收同比/净利同比等）。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "symbol": {"type": "string", "description": "股票代码或名称"}
            },
            "required": ["symbol"]
        }
    },
    {
        "name": "get_self_pick",
        "description": "查询每日精选/自选股票（评分、层级、方向、结论、回测收益）。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "pick_date": {"type": "string", "description": "可选 YYYY-MM-DD，默认取最新一天"},
                "kind": {"type": "string", "description": "可选，如 main，默认 main"},
                "limit": {"type": "integer", "description": "返回条数上限", "default": 50}
            }
        }
    },
    {
        "name": "get_lhb",
        "description": "查询龙虎榜（交易日期、个股、上榜原因、净买入、后续涨跌）。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "trade_date": {"type": "string", "description": "可选 YYYY-MM-DD"},
                "symbol": {"type": "string", "description": "可选，股票代码或名称"},
                "limit": {"type": "integer", "description": "返回条数上限", "default": 30}
            }
        }
    },
    {
        "name": "compare_signal_sources",
        "description": "信源信誉对比：各信号源(博主/战法/席位)的信誉分、层级、胜率、超额收益、选股力。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "src_type": {"type": "string", "description": "可选，如 blogger/strategy/lhb"},
                "limit": {"type": "integer", "description": "返回条数上限", "default": 30}
            }
        }
    },
    # ── 2026-10-29 新增：WorkBuddy 算力通道（分析取包 / 回填）────────────────────
    # 这一组是唯一带写能力的工具（submit_analysis）。写权限收敛在单点，审计面最小。
    {
        "name": "list_analysis_jobs",
        "description": "列出待分析的复盘任务（用户在查看器里点了「AI 复盘」并选择 WorkBuddy 时会入队）。"
                       "返回 queue_id / 复盘日期 / 状态。拿 queue_id 去调 get_analysis_brief 取包。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "description": "返回条数上限", "default": 5}
            }
        }
    },
    {
        "name": "get_analysis_brief",
        "description": "取某个待分析任务的完整信息包：system_prompt + user_prompt + 结构化 context"
                       "（市场横截面/主线/资金/政策/情绪/持仓/用户自己的知识库准则/上一份复盘）。"
                       "调用后任务状态变为 taken，查看器上会显示「WorkBuddy 已接单」。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "queue_id": {"type": "integer", "description": "list_analysis_jobs 返回的任务号"}
            },
            "required": ["queue_id"]
        }
    },
    {
        "name": "submit_analysis",
        "description": "把分析结论回填到查看器（唯一有写能力的工具）。output 必须是结构化对象，"
                       "字段见 get_analysis_brief 返回的 schema。回填后查看器的 AI 复盘面板会自动显示，"
                       "provider 显示为 WorkBuddy。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "queue_id": {"type": "integer", "description": "任务号"},
                "output": {
                    "type": "object",
                    "description": "结构化分析结论（verdict/confidence/market_read/mainlines/opportunities/"
                                   "risks/portfolio_advice/disagreements/vs_last/actions）"
                },
                "raw_text": {"type": "string", "description": "可选，原始分析文本，留档以便复核"},
                "elapsed_ms": {"type": "integer", "description": "可选，本次分析耗时（毫秒）"}
            },
            "required": ["queue_id", "output"]
        }
    },
]


# ---------------------------------------------------------------------------
# handlers
# ---------------------------------------------------------------------------
def _resolve(con, args):
    return {"ok": True, "data": db.resolve_stock(con, args["query"], args.get("limit", 20))}


def _sentiment(con, args):
    code, name = db.best_code(con, args["symbol"])
    if not code:
        return {"ok": False, "error": "无法解析股票: " + str(args["symbol"])}
    days = int(args.get("days", 30))
    cutoff = db.cutoff_date(days)
    cur = con.cursor()
    cur.execute(
        "SELECT AVG(p.sentiment_score) avg_score, COUNT(*) cnt "
        "FROM posts p JOIN stock_mentions sm ON p.id=sm.post_id_ref "
        "WHERE sm.symbol=? AND p.post_time>=?", (code, cutoff))
    row = cur.fetchone()
    cur.execute(
        "SELECT p.sentiment, COUNT(*) cnt FROM posts p "
        "JOIN stock_mentions sm ON p.id=sm.post_id_ref "
        "WHERE sm.symbol=? AND p.post_time>=? GROUP BY p.sentiment", (code, cutoff))
    dist = {r["sentiment"]: r["cnt"] for r in cur}
    cur.execute(
        "SELECT substr(p.post_time,1,10) d, COUNT(*) cnt, AVG(p.sentiment_score) avg_score "
        "FROM posts p JOIN stock_mentions sm ON p.id=sm.post_id_ref "
        "WHERE sm.symbol=? AND p.post_time>=? GROUP BY d ORDER BY d", (code, cutoff))
    trend = [dict(r) for r in cur]
    cur.execute(
        "SELECT p.site, p.author, p.post_time, p.sentiment, p.sentiment_score, p.post_url, "
        "substr(p.text,1,200) text FROM posts p "
        "JOIN stock_mentions sm ON p.id=sm.post_id_ref "
        "WHERE sm.symbol=? AND p.post_time>=? ORDER BY p.post_time DESC LIMIT 20",
        (code, cutoff))
    samples = [dict(r) for r in cur]
    return {
        "ok": True,
        "symbol": code, "name": name, "window_days": days,
        "avg_sentiment_score": round(row["avg_score"], 3) if row["avg_score"] is not None else None,
        "mention_count": row["cnt"],
        "sentiment_distribution": dist,
        "daily_trend": trend,
        "recent_samples": samples,
    }


def _list_posts(con, args):
    symbol = args.get("symbol")
    days = int(args.get("days", 30))
    cutoff = db.cutoff_date(days)
    limit = int(args.get("limit", 30))
    sql = ("SELECT p.site, p.author, p.post_time, p.sentiment, p.sentiment_score, "
           "p.post_url, substr(p.text,1,300) text "
           "FROM posts p ")
    params = []
    if symbol:
        code, _ = db.best_code(con, symbol)
        if not code:
            return {"ok": False, "error": "无法解析股票: " + str(symbol)}
        sql += "JOIN stock_mentions sm ON p.id=sm.post_id_ref WHERE sm.symbol=? AND p.post_time>=? "
        params += [code, cutoff]
    else:
        sql += "WHERE p.post_time>=? "
        params += [cutoff]
    site = args.get("site")
    if site:
        sql += "AND p.site=? " if symbol else "AND p.site=? "
        params.append(site)
    sent = args.get("sentiment")
    if sent:
        sql += "AND p.sentiment=? "
        params.append(sent)
    sql += "ORDER BY p.post_time DESC LIMIT ?"
    params.append(limit)
    cur = con.cursor()
    cur.execute(sql, params)
    return {"ok": True, "data": [dict(r) for r in cur]}


def _list_reco(con, args):
    sql = ("SELECT src_name, src_kind, src_tier, pub_date, symbol, symbol_name, sector, "
           "direction, strength, reason, url, pnl_t5, pnl_t10, pnl_status "
           "FROM extref_signals WHERE 1=1")
    params = []
    sector = args.get("sector")
    if sector:
        sql += " AND sector LIKE ?"; params.append("%" + sector + "%")
    direction = args.get("direction")
    if direction:
        sql += " AND direction=?"; params.append(direction)
    sql += " ORDER BY pub_date DESC LIMIT ?"
    params.append(int(args.get("limit", 30)))
    cur = con.cursor()
    cur.execute(sql, params)
    return {"ok": True, "data": [dict(r) for r in cur]}


def _resonance(con, args):
    code, name = db.best_code(con, args["symbol"])
    if not code:
        return {"ok": False, "error": "无法解析股票: " + str(args["symbol"])}
    cur = con.cursor()
    cur.execute(
        "SELECT src_type, direction, COUNT(*) n, AVG(strength) avg_strength "
        "FROM sig_unified WHERE code=? GROUP BY src_type, direction", (code,))
    overview = [dict(r) for r in cur]
    cur.execute(
        "SELECT src_type, src_key, ts, direction, strength, horizon, evidence, "
        "ret_t5, ex_t5, mkt_state FROM sig_unified WHERE code=? ORDER BY ts DESC LIMIT 50",
        (code,))
    details = [dict(r) for r in cur]
    return {"ok": True, "symbol": code, "name": name,
            "overview": overview, "details": details}


def _top_influencers(con, args):
    kind = args.get("kind")
    n = int(args.get("n", 20))
    by = args.get("by", "score")
    sql = ("SELECT name, score, score_tier, person_kind, win_rate_5d, win_rate_20d, "
           "avg_ret_5d, avg_ret_20d, best_call_symbol, best_call_ret, evidence_level, "
           "mention_count, post_count, active_days FROM person_scores WHERE in_ranking=1 ")
    params = []
    if kind:
        sql += "AND person_kind=? "; params.append(kind)
    order = "avg_ret_5d DESC" if by == "ret" else "score DESC"
    sql += "ORDER BY " + order + " LIMIT ?"
    params.append(n)
    cur = con.cursor()
    cur.execute(sql, params)
    return {"ok": True, "data": [dict(r) for r in cur]}


def _signals(con, args):
    sql = ("SELECT sid, ts, src_type, src_key, code, name, direction, strength, horizon, "
           "evidence, ret_t5, ex_t5, mkt_state FROM sig_unified WHERE 1=1")
    params = []
    symbol = args.get("symbol")
    if symbol:
        code, _ = db.best_code(con, symbol)
        if not code:
            return {"ok": False, "error": "无法解析股票: " + str(symbol)}
        sql += " AND code=?"; params.append(code)
    st = args.get("src_type")
    if st:
        sql += " AND src_type=?"; params.append(st)
    direction = args.get("direction")
    if direction:
        sql += " AND direction=?"; params.append(direction)
    sql += " ORDER BY ts DESC LIMIT ?"
    params.append(int(args.get("limit", 50)))
    cur = con.cursor()
    cur.execute(sql, params)
    return {"ok": True, "data": [dict(r) for r in cur]}


def _news(con, args):
    kind = (args.get("kind") or "news").lower()
    limit = int(args.get("limit", 30))
    cur = con.cursor()
    if kind == "ann":
        sql = ("SELECT code, name, title, url, notice_date, cat, tone FROM stock_ann ")
        params = []
        symbol = args.get("symbol")
        if symbol:
            code, _ = db.best_code(con, symbol)
            if not code:
                return {"ok": False, "error": "无法解析股票: " + str(symbol)}
            sql += "WHERE code=? "; params.append(code)
        sql += "ORDER BY notice_date DESC LIMIT ?"; params.append(limit)
        cur.execute(sql, params)
        return {"ok": True, "kind": "ann", "data": [dict(r) for r in cur]}
    # default: news
    cutoff = db.cutoff_date(int(args.get("days", 14)))
    cur.execute(
        "SELECT site, title, url, publish_time, summary, source FROM news "
        "WHERE publish_time>=? ORDER BY publish_time DESC LIMIT ?", (cutoff, limit))
    return {"ok": True, "kind": "news", "data": [dict(r) for r in cur]}


def _fundamentals(con, args):
    code, name = db.best_code(con, args["symbol"])
    if not code:
        return {"ok": False, "error": "无法解析股票: " + str(args["symbol"])}
    cur = con.cursor()
    cur.execute(
        "SELECT symbol, name, roe, roe_kf, dr, gm, rev_yoy, np_yoy, kf_np_yoy, "
        "report_date, source FROM fundamentals WHERE symbol=?", (code,))
    row = cur.fetchone()
    if not row:
        return {"ok": True, "symbol": code, "name": name, "found": False, "data": None}
    return {"ok": True, "symbol": code, "name": name, "found": True, "data": dict(row)}


def _self_pick(con, args):
    kind = args.get("kind", "main")
    limit = int(args.get("limit", 50))
    cur = con.cursor()
    pick_date = args.get("pick_date")
    if not pick_date:
        cur.execute("SELECT MAX(pick_date) FROM self_pick WHERE kind=?", (kind,))
        pick_date = cur.fetchone()[0]
    if not pick_date:
        return {"ok": False, "error": "无精选数据"}
    cur.execute(
        "SELECT code, name, score, tier, direction, confidence, horizon, senti_net, "
        "verdict, ret_t1, ex_t1, evidence_json FROM self_pick WHERE pick_date=? AND kind=? "
        "ORDER BY rank LIMIT ?", (pick_date, kind, limit))
    return {"ok": True, "pick_date": pick_date, "data": [dict(r) for r in cur]}


def _lhb(con, args):
    sql = ("SELECT trade_date, security_code, security_name, reason, close_price, "
           "change_rate, net_amt, d1_chg, d5_chg, d10_chg, d20_chg FROM lhb_stocks WHERE 1=1")
    params = []
    td = args.get("trade_date")
    if td:
        sql += " AND trade_date=?"; params.append(td)
    symbol = args.get("symbol")
    if symbol:
        code, _ = db.best_code(con, symbol)
        if not code:
            return {"ok": False, "error": "无法解析股票: " + str(symbol)}
        sql += " AND security_code=?"; params.append(code)
    sql += " ORDER BY trade_date DESC LIMIT ?"
    params.append(int(args.get("limit", 30)))
    cur = con.cursor()
    cur.execute(sql, params)
    return {"ok": True, "data": [dict(r) for r in cur]}


def _compare_sources(con, args):
    sql = ("SELECT src_type, src_key, label, score, tier, win_t5, avg_ex_t5, "
           "pick_skill, timing_skill, n_sig, note FROM res_source WHERE 1=1")
    params = []
    st = args.get("src_type")
    if st:
        sql += " AND src_type=?"; params.append(st)
    sql += " ORDER BY score DESC LIMIT ?"
    params.append(int(args.get("limit", 30)))
    cur = con.cursor()
    cur.execute(sql, params)
    return {"ok": True, "data": [dict(r) for r in cur]}


# ---------------------------------------------------------------------------
# WorkBuddy 算力通道 handlers（2026-10-29）
# ---------------------------------------------------------------------------
# 设计说明：mcp_server 的整体铁律是「只读」，但分析回填在语义上必须写库。
# 处理方式是**收敛而非放开**：写能力只出现在 _rw_connect() 一个函数里，
# 只被 submit_analysis 一个工具调用，其余 12 个工具仍走只读连接。
# 这样即便提示词被注入，能造成的最大影响也只是"多写一份复盘结论"，不会篡改原始语料。
def _ai_mod():
    """延迟 import viewer.ai_review（只依赖标准库；延迟可让 MCP 启动更轻）。"""
    import os
    import sys

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if root not in sys.path:
        sys.path.insert(0, root)
    from viewer import ai_review

    return ai_review


def _rw_connect():
    """打开可写连接（仅 submit_analysis 使用）。与只读连接共用同一个 app.db。"""
    import os
    import sqlite3

    path = os.environ.get("SFS_DB") or db.DEFAULT_DB
    con = sqlite3.connect(os.path.abspath(path), timeout=15)
    con.row_factory = sqlite3.Row
    try:
        con.execute("PRAGMA busy_timeout=15000")
    except sqlite3.Error:
        pass
    return con


def _analysis_jobs(con, args):
    A = _ai_mod()
    rw = _rw_connect()
    try:
        items = A.queue_pending(rw, limit=int(args.get("limit", 5)))
        st = A.queue_status(rw)
    finally:
        rw.close()
    return {
        "ok": True,
        "count": len(items),
        "items": items,
        "queue_counts": (st or {}).get("counts") or {},
        "hint": ("有待分析任务时：先用 get_analysis_brief(queue_id) 取信息包，"
                 "分析完成后用 submit_analysis(queue_id, output) 回填。"),
    }


def _analysis_brief(con, args):
    qid = int(args.get("queue_id") or 0)
    if not qid:
        return {"ok": False, "error": "缺少 queue_id"}
    A = _ai_mod()
    rw = _rw_connect()
    try:
        res = A.queue_brief(rw, qid)
    finally:
        rw.close()
    return res


def _analysis_submit(con, args):
    qid = int(args.get("queue_id") or 0)
    if not qid:
        return {"ok": False, "error": "缺少 queue_id"}
    output = args.get("output")
    if not output:
        return {"ok": False, "error": "缺少 output（结构化分析结论）"}
    A = _ai_mod()
    rw = _rw_connect()
    try:
        res = A.queue_submit(rw, qid, output,
                             raw_text=args.get("raw_text") or "",
                             elapsed_ms=int(args.get("elapsed_ms") or 0))
    finally:
        rw.close()
    return res


_HANDLERS = {
    "resolve_stock": _resolve,
    "query_stock_sentiment": _sentiment,
    "list_posts": _list_posts,
    "list_recommendations": _list_reco,
    "get_resonance": _resonance,
    "top_influencers": _top_influencers,
    "get_signals": _signals,
    "get_news": _news,
    "get_fundamentals": _fundamentals,
    "get_self_pick": _self_pick,
    "get_lhb": _lhb,
    "compare_signal_sources": _compare_sources,
    # 2026-10-29：WorkBuddy 算力通道
    "list_analysis_jobs": _analysis_jobs,
    "get_analysis_brief": _analysis_brief,
    "submit_analysis": _analysis_submit,
}


def call_tool(con, name, args):
    h = _HANDLERS.get(name)
    if not h:
        raise ValueError("未知工具: " + str(name))
    return h(con, args or {})
