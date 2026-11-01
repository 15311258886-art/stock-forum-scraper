"""每日 AI 复盘：真实调用大模型，对当日全量信息做判断。

## 本模块存在的理由（与规则引擎的分工）

项目里既有的「今日决策 / 个股诊断 / 消息面汇总」全部是**本地规则引擎**：
固定权重公式 + 阈值判断，输出可预测、可复现，但**没有推理过程**。
例如 api_today_decision 打分 = 40×(净流入比) + 30(涨停)/20(涨>5%) + 梯队数×1.2。

本模块走另一条路：把「当日信息包 + 用户自己沉淀的知识库准则」交给 LLM，
让它输出**带理由的判断**，并显式回答两个规则引擎永远答不了的问题：

1. `disagreements` —— 我和规则打分不一致的地方，为什么？
2. `vs_last` —— 上一份复盘的判断，今天兑现了没有？

只有这两项能回答，"AI 到底有没有真的想过"才不靠信仰。

## 为什么不卡全站

LLM 调用是**网络等待**，等待期间 GIL 是释放的。所以它不像本地重聚合那样
把单进程服务冻住 —— 后台线程跑即可，不需要像 lowbox 那样搬子进程。
（但仍然遵守「请求线程只读缓存，计算进后台」的范式。）

## 多模型

统一走 OpenAI 兼容的 /chat/completions：
- deepseek : api.deepseek.com
- hunyuan  : api.hunyuan.cloud.tencent.com
- qwen     : dashscope.aliyuncs.com/compatible-mode
Key 从 scraper.credentials 加密库读，键名见 PROVIDERS。
"""

from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import time
import urllib.error
import urllib.request

log = logging.getLogger(__name__)

# ---------------------------------------------------------------- providers

PROVIDERS: dict = {
    "deepseek": {
        "label": "DeepSeek",
        "url": "https://api.deepseek.com/chat/completions",
        "model": "deepseek-chat",
        "secret": "deepseek_api_key",
        "env": "DEEPSEEK_API_KEY",
        "console": "https://platform.deepseek.com/api_keys",
        "howto": "platform.deepseek.com → API keys → 创建",
    },
    "hunyuan": {
        "label": "混元",
        "url": "https://api.hunyuan.cloud.tencent.com/v1/chat/completions",
        "model": "hunyuan-turbos-latest",
        "secret": "hunyuan_api_key",
        "env": "HUNYUAN_API_KEY",
        "console": "https://console.cloud.tencent.com/hunyuan",
        "howto": "腾讯云控制台 → 大模型知识引擎/混元 → API Key",
    },
    "qwen": {
        "label": "通义千问",
        "url": "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions",
        "model": "qwen-plus",
        "secret": "dashscope_api_key",
        "env": "DASHSCOPE_API_KEY",
        "console": "https://bailian.console.aliyun.com",
        "howto": "阿里云百炼控制台 → API-KEY 管理 → 创建",
    },
    # 2026-10-29 新增：WorkBuddy 算力通道。
    # 它与上面三家有本质区别——**不在这里调模型**。本进程只把「信息包」放进队列
    # （ai_review_queue，state=pending），由 WorkBuddy 侧通过 MCP 工具取走、分析、回填。
    # 好处：① 分析能力不受本机 API Key 与网络限制；② 主进程零模型调用，不会被拖慢；
    # ③ 全程留痕（谁取的、什么时候回填、原文是什么），比"黑盒 API 调用"更可审计。
    "workbuddy": {
        "label": "WorkBuddy",
        "url": "",
        "model": "workbuddy-agent",
        "secret": "",
        "env": "",
    },
}
DEFAULT_PROVIDER = "deepseek"
# 单次调用超时：完整复盘 prompt 约 1~1.5 万字，模型生成 2000+ token，
# 实测 deepseek 通常 30~70s。给足 180s，超时按失败落库（可在页面重试）。
TIMEOUT = 180


def provider_key(name: str) -> str:
    """读某模型的 API Key：环境变量优先，其次加密库。

    workbuddy 例外：它不靠 API Key，而是由 WorkBuddy 主代理通过 MCP 取包分析，
    因此恒定返回 "local"（= 可用），前端据此把它渲染成可点选的选项。
    """
    p = PROVIDERS.get(name) or {}
    if not p:
        return ""
    if name == "workbuddy":
        return "local"
    ek = os.environ.get(p["env"] or "")
    if ek:
        return ek.strip()
    try:
        from scraper.credentials import load_secret

        return (load_secret(p["secret"]) or "").strip()
    except Exception:  # noqa: BLE001 —— 缺库/未配置都按"没 key"处理
        return ""


def available_providers() -> list:
    """只返回**已配 Key** 的模型，前端据此渲染切换按钮（不让人点了才发现没配）。"""
    out = []
    for k, p in PROVIDERS.items():
        ok = bool(provider_key(k))
        out.append({"key": k, "label": p["label"], "model": p["model"], "ready": ok,
                    "need_key": k != "workbuddy",
                    "console": p.get("console") or "",
                    "howto": p.get("howto") or ""})
    return out


def set_provider_key(name: str, key: str) -> dict:
    """把某个模型的 API Key 写进本机加密凭据库（Fernet，非明文）。

    为什么这件事必须由界面来做：Key 是**用户自己账号下的付费凭证**，
    我（程序）不可能凭空生成，也不可能替他去腾讯云/阿里云开通。
    但"没入口"和"没 Key"是两件事 —— 这个函数解决的是后者之外的那一半：
    让他能在一个框里贴进去，而不是翻代码改环境变量。
    """
    p = PROVIDERS.get(name)
    if not p:
        return {"ok": False, "err": "未知模型：%s" % name}
    if name == "workbuddy":
        return {"ok": False, "err": "WorkBuddy 通道走本地队列，不使用 API Key"}
    k = (key or "").strip().strip('"').strip("'")
    if not k:
        return {"ok": False, "err": "Key 是空的"}
    if len(k) < 16:
        return {"ok": False, "err": "Key 只有 %d 位，像是没复制全（正常 30 位以上）" % len(k)}
    if any(c.isspace() for c in k):
        return {"ok": False, "err": "Key 里含空格或换行，请重新复制"}
    try:
        from scraper.credentials import save_secret
        save_secret(p["secret"], k)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "err": "写入加密凭据库失败：%s: %s" % (type(e).__name__, e)}
    # 写完立刻验一次：不验证就报"已保存"，等于让他等到下次复盘失败才知道填错了。
    chk = test_provider(name)
    return {"ok": True, "saved": True, "label": p["label"],
            "verified": bool(chk.get("ok")), "test": chk}


def test_provider(name: str, timeout: int = 25) -> dict:
    """用 1 个 token 的最小请求验证 Key 是否真的可用（而不是只看格式像不像）。"""
    p = PROVIDERS.get(name)
    if not p:
        return {"ok": False, "err": "未知模型：%s" % name}
    if name == "workbuddy":
        return {"ok": True, "note": "本地队列通道，不需要网络鉴权"}
    key = provider_key(name)
    if not key:
        return {"ok": False, "err": "未配置 Key"}
    body = json.dumps({
        "model": p["model"],
        "messages": [{"role": "user", "content": "ping"}],
        "max_tokens": 1,
    }).encode("utf-8")
    req = urllib.request.Request(
        p["url"], data=body, method="POST",
        headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"},
    )
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            resp.read()
        return {"ok": True, "elapsed_ms": int((time.time() - t0) * 1000)}
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8", "ignore")[:200]
        except Exception:  # noqa: BLE001
            pass
        if e.code in (401, 403):
            return {"ok": False, "err": "Key 无效或无权限（HTTP %s）" % e.code}
        if e.code == 402:
            return {"ok": False, "err": "Key 有效但余额不足（HTTP 402）"}
        if e.code == 404:
            return {"ok": False, "err": "接口地址或模型名不对（HTTP 404）：%s" % detail}
        return {"ok": False, "err": "HTTP %s：%s" % (e.code, detail)}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "err": "%s: %s" % (type(e).__name__, str(e)[:160])}



# ---------------------------------------------------------------- schema

_SCHEMA = """
CREATE TABLE IF NOT EXISTS ai_review (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    review_date     TEXT NOT NULL,          -- 复盘针对的交易日（YYYY-MM-DD）
    provider        TEXT NOT NULL,          -- deepseek / hunyuan / qwen
    model           TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'ok',   -- ok / err
    context_json    TEXT,                   -- 当时喂进去的信息包（可复现、可审计）
    context_chars   INTEGER DEFAULT 0,
    output_json     TEXT,                   -- 结构化判断
    raw_text        TEXT,                   -- 模型原始输出（解析失败时留着排障）
    prompt_tokens   INTEGER DEFAULT 0,
    completion_tokens INTEGER DEFAULT 0,
    elapsed_ms      INTEGER DEFAULT 0,
    err             TEXT,
    created_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_ai_review_date ON ai_review(review_date, created_at DESC);
"""

_ensured = False


def ensure_schema(conn: sqlite3.Connection) -> None:
    global _ensured
    if _ensured:
        return
    try:
        conn.executescript(_SCHEMA)
        conn.commit()
        _ensured = True
    except Exception:  # noqa: BLE001
        log.exception("ai_review 建表失败")


# ---------------------------------------------------------------- 小工具


def _clip(s, n: int) -> str:
    """截断长文本（中文字符按 1 计）。保持单行，避免把 prompt 撑爆。"""
    if s is None:
        return ""
    t = re.sub(r"\s+", " ", str(s)).strip()
    return t if len(t) <= n else t[:n] + "…"


def _num(v, default=None):
    try:
        if v is None or v == "":
            return default
        return float(v)
    except (TypeError, ValueError):
        return default


def _q(conn, sql, args=()):
    """安全查询：任何异常返回空列表，绝不因为某张表缺失而整包失败。"""
    try:
        return conn.execute(sql, args).fetchall()
    except Exception as e:  # noqa: BLE001
        log.debug("ai_review 取数失败 %s: %s", sql[:60], e)
        return []


def _one(conn, sql, args=()):
    rows = _q(conn, sql, args)
    return rows[0][0] if rows else None


def latest_trade_date(conn) -> str:
    """最近一个「有主线板块数据」的交易日 —— 复盘针对它，而不是机械用今天。
    凌晨/周末打开时不至于复盘一个还没有数据的空日期。"""
    d = _one(conn, "SELECT MAX(trade_date) FROM sector_chain")
    if d:
        return str(d)
    d = _one(conn, "SELECT MAX(trade_date) FROM stock_daily_snapshot")
    if d:
        return str(d)
    return time.strftime("%Y-%m-%d")


# ---------------------------------------------------------------- 信息包

# 各分区字数配额（合计约 9000 字 ≈ 6000~7000 token，deepseek 完全吃得下）
BUDGET = {
    "market": 500,
    "mainlines": 900,
    "money": 700,
    "news": 1600,
    "sentiment": 600,
    "portfolio": 700,
    "doctrine": 2200,   # 用户自己的知识库准则 —— 这是"结合我产出的内容"的核心
    "evidence": 2600,   # 2026-10-31 新增：本地实证账本（含「已证伪禁区」），见 viewer/evidence.py
    "last": 900,        # 上一份复盘 + 系统回填的成绩单
    "data_health": 400,
}


def _regime_desc(bj: dict) -> str:
    """把 `breadth_json` 里的**实测值**拼成一句当日描述，不引用任何判定规则。

    为什么要这一步：`note` 列的含义随写入方漂移 —— resonance 写的是"今天为什么是这个
    状态"，pillars 写的是规则原文。规则原文对模型来说是**噪声甚至误导**（它会把
    "普跌 = 上涨占比<35%" 读成"今天上涨占比<35%"）。而实测值（up_ratio / median_pct）
    两个口径的语义完全一致，可以直接说。

    resonance 那批行只有 `{"bench_median_pct": null}` → 拼不出来，返回空串，
    由调用方决定是否退回该行的 note。
    """
    if not isinstance(bj, dict):
        return ""
    parts = []
    ur = bj.get("up_ratio")
    up_n, pool_n = bj.get("up_n"), bj.get("pool_n")
    if isinstance(ur, (int, float)) and not isinstance(ur, bool):
        seg = "上涨占比 %.1f%%" % (float(ur) * 100.0)
        if isinstance(up_n, (int, float)) and isinstance(pool_n, (int, float)) and pool_n:
            seg += "（%d / %d 只）" % (int(up_n), int(pool_n))
        parts.append(seg)
    mp = bj.get("median_pct")
    if isinstance(mp, (int, float)) and not isinstance(mp, bool):
        parts.append("涨幅中位数 %+.2f%%" % float(mp))
    idx = str(bj.get("index_note") or "").strip()
    if idx:
        parts.append(idx)
    return "，".join(parts)


def _sec_market(conn, date: str) -> dict:
    """市场环境。

    注意：本库**没有指数收盘价的历史序列**（kbase 里 5239 个代码全是 6 位个股，
    app.db.market_quotes 存的也是个股行情）。所以这里不假装有指数，改用全市场横截面
    描述市场 —— 等权涨幅 / 市值加权涨幅（近似指数）/ 成交额 / 涨跌家数，
    信息量其实比单看一个指数点位更大。真实缺失会写进 note，让模型知道。
    """
    out: dict = {"date": date}
    snap = _q(
        conn,
        "SELECT pct, amount, total_mv FROM stock_daily_snapshot "
        "WHERE trade_date=? AND pct IS NOT NULL",
        (date,),
    )
    used = date
    if not snap:
        alt = _one(conn, "SELECT MAX(trade_date) FROM stock_daily_snapshot")
        if alt:
            used = str(alt)
            snap = _q(
                conn,
                "SELECT pct, amount, total_mv FROM stock_daily_snapshot "
                "WHERE trade_date=? AND pct IS NOT NULL",
                (used,),
            )
    if snap:
        vals = [(_num(r["pct"], 0.0) or 0.0) for r in snap]
        mv = [(_num(r["total_mv"], 0.0) or 0.0) for r in snap]
        amt = sum((_num(r["amount"], 0.0) or 0.0) for r in snap)
        up = sum(1 for v in vals if v > 0)
        dn = sum(1 for v in vals if v < 0)
        wsum = sum(mv) or 1.0
        out["breadth"] = {
            "as_of": used,
            "total": len(vals),
            "up": up,
            "down": dn,
            "flat": len(vals) - up - dn,
            "limit_up": sum(1 for v in vals if v >= 9.8),
            "limit_down": sum(1 for v in vals if v <= -9.8),
            "up_gt5": sum(1 for v in vals if v >= 5),
            "dn_gt5": sum(1 for v in vals if v <= -5),
            "median_pct": round(sorted(vals)[len(vals) // 2], 2),
            "avg_pct": round(sum(vals) / len(vals), 2),
            "cap_weighted_pct": round(
                sum(v * w for v, w in zip(vals, mv)) / wsum, 2) if wsum > 1 else None,
            "amount_yi": round(amt / 1e8, 0),
        }
        out["note"] = "本库未接入指数收盘序列；市值加权涨幅为全市场市值加权，可近似指数涨跌。"
    # 2026-10-30 P1-a：这张表有两个写入方（pillars / resonance），`note` 的含义也分两种：
    #   resonance 写的是「今天为什么是这个状态」，pillars 写的是**判定规则原文**
    #   （"普跌 = 上涨占比<35% 或 中位涨幅≤-1.2%；…"）。把规则原文当"当前市况说明"喂给
    #   大模型，它会把规则当成今天的实况（"今天就是上涨占比<35%"）—— 这是错的。
    #   所以：能用 breadth_json 里的实测值（up_ratio / median_pct）就自己拼一句；
    #   只有 resonance 那一行才采用它的 note。
    try:
        from viewer import state_basis as _SB
        _last = _SB.latest(conn)
    except Exception:  # noqa: BLE001
        _last = None
    if _last:
        _bj = _SB.breadth_of(_last)
        _desc = _regime_desc(_bj)
        _basis = _last.get("basis") or "unknown"
        if not _desc and _basis == _SB.BASIS_RESONANCE:
            _desc = _clip(_last.get("note"), 120)
        out["regime"] = {
            "state": _last.get("state"),
            "as_of": _last.get("trade_date"),
            "basis": _basis,          # pillars / resonance —— "这条标签是谁判的"
            "note": _desc or "",
        }
    return out


def _sec_mainlines(conn, date: str, n: int = 6) -> list:
    """主线板块：净流入、龙头、梯队、次日/3日表现（验证昨天的判断）。"""
    rows = _q(
        conn,
        "SELECT sector, net_amt, onlist_n, leader_name, leader_code, leader_pct_now, "
        "tiers_json, branches_json, t1_avg, t3_avg FROM sector_chain "
        "WHERE trade_date=? ORDER BY net_amt DESC LIMIT ?",
        (date, n),
    )
    out = []
    for r in rows:
        tiers = _jl_local(r["tiers_json"])
        out.append({
            "sector": r["sector"],
            "net_yi": round((_num(r["net_amt"], 0) or 0) / 1e8, 2),
            "onlist": r["onlist_n"],
            "leader": r["leader_name"],
            "leader_pct": _num(r["leader_pct_now"]),
            "tiers": [
                "%s(%s%%)" % (t.get("name"), t.get("pct"))
                for t in tiers[:5]
                if isinstance(t, dict)
            ],
            "branches": [
                (b.get("name") or b.get("branch") or str(b)) if isinstance(b, dict) else str(b)
                for b in _jl_local(r["branches_json"])[:5]
            ],
            "t1_avg": _num(r["t1_avg"]),
            "t3_avg": _num(r["t3_avg"]),
        })
    return out


def _jl_local(s) -> list:
    if not s:
        return []
    if isinstance(s, list):
        return s
    try:
        v = json.loads(s)
        return v if isinstance(v, list) else []
    except Exception:  # noqa: BLE001
        return []


def _sec_money(conn, date: str) -> dict:
    """资金：龙虎榜（个股 + 净买卖）+ 板块资金流。"""
    out: dict = {}
    rows = _q(
        conn,
        "SELECT security_code, security_name, change_rate, net_amt, reason "
        "FROM lhb_stocks WHERE trade_date=? ORDER BY ABS(COALESCE(net_amt,0)) DESC LIMIT 12",
        (date,),
    )
    out["lhb"] = [
        {
            "code": r["security_code"],
            "name": r["security_name"],
            "pct": _num(r["change_rate"]),
            "net_wan": round((_num(r["net_amt"], 0) or 0) / 1e4, 0),
            "reason": _clip(r["reason"], 40),
        }
        for r in rows
    ]
    bf = _q(
        conn,
        "SELECT board_name, kind, main_net, net_pct FROM board_flow_daily "
        "WHERE trade_date=? ORDER BY main_net DESC LIMIT 10",
        (date,),
    )
    if bf:
        out["board_flow"] = [
            {
                "board": r["board_name"],
                "kind": r["kind"],
                "net_yi": round((_num(r["main_net"], 0) or 0) / 1e8, 2),
                "pct": _num(r["net_pct"]),
            }
            for r in bf
        ]
    return out


def _sec_news(conn, date: str) -> dict:
    """消息面。

    踩过的坑：`news` 表看着像新闻，实际 78% 是 eastmoney 股吧帖、其余是 ths 论坛言论
    （"明天开盘就要跑""家人们炒股是持久战"），不是新闻。所以：
    - 政策/要闻 → policy_news（有 issuer / importance，质量最高）
    - 公司层面 → stock_ann（只要 bad/good，neutral 是例行公告没有信息量）
    - 外围 → overseas_news + overseas_hotspots（带 sectors / transmission）
    - 论坛言论 → 归到 sentiment 区，且标明"股吧言论"
    """
    out: dict = {}
    pol = _q(
        conn,
        "SELECT title, issuer, importance, sectors, pub_time FROM policy_news "
        "WHERE substr(COALESCE(pub_time,''),1,10) <= ? "
        "ORDER BY importance DESC, pub_time DESC LIMIT 12",
        (date,),
    )
    # 同一条消息常被拆成多条入库（央行逆回购拆成 3 条、国务院表态拆成 3 条），按标题前 24 字去重
    pol_out, seen_t = [], set()
    for r in pol:
        t = _clip(r["title"], 100)
        k = re.sub(r"[【】\s]", "", t)[:24]
        if k in seen_t:
            continue
        seen_t.add(k)
        pol_out.append({
            "title": t,
            "issuer": r["issuer"],
            "importance": r["importance"],
            "sectors": _clip(r["sectors"], 40),
        })
        if len(pol_out) >= 9:
            break
    out["policy"] = pol_out
    ann = _q(
        conn,
        "SELECT name, title, tone, cat FROM stock_ann "
        "WHERE notice_date <= ? AND tone IN ('bad','good') "
        "ORDER BY notice_date DESC LIMIT 10",
        (date,),
    )
    out["ann"] = [
        {"name": r["name"], "title": _clip(r["title"], 70), "tone": r["tone"], "cat": r["cat"]}
        for r in ann
    ]
    ovn = _q(
        conn,
        "SELECT title, region, sentiment, sectors, publish_time FROM overseas_news "
        "ORDER BY COALESCE(heat,0) DESC, publish_time DESC LIMIT 8",
    )
    out["overseas_news"] = [
        {
            "title": _clip(r["title"], 80),
            "region": r["region"],
            "sentiment": r["sentiment"],
            "sectors": _clip(r["sectors"], 40),
        }
        for r in ovn
    ]
    ovh = _q(
        conn,
        "SELECT title, direction, domestic_sectors, transmission, heat FROM overseas_hotspots "
        "ORDER BY COALESCE(heat,0) DESC LIMIT 5",
    )
    out["overseas_hot"] = [
        {
            "title": _clip(r["title"], 80),
            "dir": r["direction"],
            "sectors": _clip(r["domestic_sectors"], 50),
            "transmission": _clip(r["transmission"], 80),
        }
        for r in ovh
    ]
    return out


def _sec_attention(conn, date: str) -> dict:
    """外部人气榜（extref_signals）。

    ⚠️ 2026-10-31 大幅瘦身：原函数名 _sec_sentiment，还带「讨论热度榜 / 发帖最多 /
    帖子情绪分布 / 言论样本」四块，全部来自 posts + stock_mentions。论坛抓取已于
    2026-10-30 全停，这四块从此只会逐日变陈、永远不会变新 —— 而模型拿到它们
    仍会当真（"近期散户在热议 X"），等于用死数据生成活结论。
    故整块删除，只保留仍在日更的 extref_signals（外部人气/榜单）。
    要恢复看 git 历史 826986d 及之前版本。
    """
    out: dict = {}
    ext = _q(
        conn,
        "SELECT symbol_name, direction, strength, reason FROM extref_signals "
        "WHERE pub_date <= ? ORDER BY pub_date DESC LIMIT 8",
        (date,),
    )
    out["extref"] = [
        {
            "name": r["symbol_name"],
            "dir": r["direction"],
            "strength": r["strength"],
            "why": _clip(r["reason"], 50),
        }
        for r in ext
    ]
    return out


def _sec_portfolio(conn, date: str) -> dict:
    """我的自选 —— 判断必须落到"我该怎么办"，而不是泛泛谈大盘。

    ⚠️ 2026-10-31 删除「跟踪的持仓动作」一段：它读 positions 表，而那张表最后一行是
    **2021-06-20** 的集思录「实盘汇总贴」晒盘（271 行，手工记账、非交割单）。
    也就是说 AI 复盘一直在向模型声称"你在跟踪这些持仓"，用的是五年前别人晒的仓位。
    这不是数据陈旧的问题，是把"别人的旧仓位"当"我的仓位"喂给模型 —— 会直接污染
    「该不该割 / 该不该加」的判断。持仓真相只有 positions 之外的自选池（watch_*）
    与自选荐股（self_pick），后者仍在日更，故保留。
    """
    out: dict = {}
    sp = _q(
        conn,
        "SELECT code, name, score, tier, direction, verdict FROM self_pick "
        "WHERE pick_date=? ORDER BY score DESC LIMIT 12",
        (date,),
    ) or _q(
        conn,
        "SELECT code, name, score, tier, direction, verdict FROM self_pick "
        "ORDER BY pick_date DESC LIMIT 12",
    )
    out["self_pick"] = [
        {
            "code": r["code"],
            "name": r["name"],
            "score": _num(r["score"]),
            "tier": r["tier"],
            "dir": r["direction"],
            "verdict": _clip(r["verdict"], 40),
        }
        for r in sp
    ]
    return out


def _extract_doctrine(doc: dict) -> dict:
    """从知识库文档里提取「可用来判断的准则」。

    库里混着三套解析结构（同一张 kb_doc 表，doc_key 不同结构不同）：
      A. zdx_stock      → conclusions[] / pitfalls[] / schools[].common_method
      B. bili_*         → sections[].{title, items[], paragraphs[]}
      C. value_basics   → groups[].{title, intro[], terms[].{name, one_line}}
    三套都要吃得下 —— 否则「结合我自己的体系」这句话就是空的（只有 1 份文档生效）。
    """
    cons: list = []
    pits: list = []
    schools: list = []

    # ---- A 型
    for blk in (doc.get("conclusions") or [])[:2]:
        for it in (blk.get("items") or [])[:4]:
            cons.append(_clip(it, 120))
    for blk in (doc.get("pitfalls") or [])[:2]:
        for it in (blk.get("items") or [])[:3]:
            pits.append(_clip(it, 100))
    for sc in (doc.get("schools") or [])[:2]:
        if sc.get("common_method"):
            schools.append("%s：%s" % (sc.get("name"), _clip(sc["common_method"], 120)))

    # ---- B 型（B 站体系）：取每节标题 + 前两条要点
    for sec in (doc.get("sections") or [])[:6]:
        title = sec.get("title") or ""
        items = sec.get("items") or []
        lead = items[0] if items else ((sec.get("paragraphs") or [None])[0])
        if title and lead:
            cons.append("%s —— %s" % (_clip(title, 30), _clip(lead, 100)))
        for it in items[1:3]:
            cons.append(_clip(it, 100))

    # ---- C 型（基础常识）：概念名 + 一句话定义
    for g in (doc.get("groups") or [])[:5]:
        terms = g.get("terms") or []
        for tm in terms[:4]:
            if tm.get("name") and tm.get("one_line"):
                cons.append("%s：%s" % (_clip(tm["name"], 14), _clip(tm["one_line"], 90)))
        intro = g.get("intro") or []
        if not terms and intro:
            cons.append("%s —— %s" % (_clip(g.get("title"), 26), _clip(intro[0], 100)))

    # 去重（同一句在 items/paragraphs 里重复出现很常见）
    def _uniq(seq):
        seen, out = set(), []
        for s in seq:
            k = s[:28]
            if k and k not in seen:
                seen.add(k)
                out.append(s)
        return out

    return {"conclusions": _uniq(cons)[:6], "pitfalls": _uniq(pits)[:3], "schools": _uniq(schools)[:2]}


def _sec_doctrine(conn) -> dict:
    """用户自己沉淀的准则 —— "结合我这边产出的内容"。

    来源两张表：
    - kb_doc      ：知识库文档（抖音博主体系 v5.0、价值投资基础、土斯体系…），
                    只取准则/误区/流派方法，不塞正文
    - evo_knowledge：数据自生长规则（历史回测出来的结论，带超额收益数字）
    """
    out: dict = {"docs": [], "rules": []}
    rows = _q(
        conn,
        "SELECT doc_key, track, title, version, payload FROM kb_doc WHERE status='ok'",
    )
    for r in rows:
        item = {"track": r["track"], "title": r["title"], "version": r["version"]}
        try:
            doc = (json.loads(r["payload"]) or {}).get("doc") or {}
        except Exception:  # noqa: BLE001
            out["docs"].append(item)
            continue
        item.update(_extract_doctrine(doc))
        out["docs"].append(item)

    rul = _q(
        conn,
        "SELECT title, body, evidence FROM evo_knowledge "
        "WHERE COALESCE(status,'') != 'disabled' ORDER BY kid LIMIT 12",
    )
    seen = set()
    for r in rul:
        when = _clip(r["title"], 90)
        # title 常常自带「数据自生长规则：」前缀，且与 body 首句重复，剥掉再判重
        when = re.sub(r"^(数据自生长规则|被回测推翻的说法|信源退步预警)[:：]\s*", "", when)
        key = when[:26]
        if key in seen:
            continue
        seen.add(key)
        out["rules"].append({"when": when, "then": _clip(r["body"], 150)})
    return out


def _sec_evidence(conn) -> dict:
    """本地实证账本 —— 让模型知道「证据到哪为止」以及「哪些说法已被我们自己推翻」。

    2026-10-31 新增。起因：体检发现模型根本不知道我们做过实证，于是会自然引用
    「超买要回调」（实测 +0.69%）、「减持必跌」（严格口径 t=−0.13）这类**已被证伪**
    的常识。给模型更多数据不会解决这个问题，给它**边界和禁区**才会。

    数据源是 viewer/evidence.py（唯一账本），不在这里重复维护数字。
    """
    try:
        import evidence as EV
        return {"asof": EV.ASOF, "text": EV.prompt_block(max_chars=BUDGET["evidence"])}
    except Exception:  # noqa: BLE001 —— 账本缺失不该阻断复盘
        log.exception("[ai_review] 实证账本读取失败")
        return {}


def _sec_last(conn, date: str, provider: str) -> dict:
    """上一份复盘 + **系统回填的成绩单**。

    关键改动（2026-10-31）：旧实现只把上一份的结论文字塞进去，然后要求模型
    「判断它兑现没有」—— 模型手里没有价格数据，只能凭印象自评，这是链路里
    最容易自欺的一环。现在由 viewer/review_score.py 按可执行口径回填真实的
    T+5/T+10 超额与命中率，作为**事实**给它。模型可以解释偏差，但不能编造成绩。
    """
    out: dict = {}
    rows = _q(
        conn,
        "SELECT review_date, output_json FROM ai_review "
        "WHERE status='ok' AND review_date < ? AND output_json IS NOT NULL "
        "ORDER BY review_date DESC, id DESC LIMIT 1",
        (date,),
    )
    if rows:
        try:
            o = json.loads(rows[0]["output_json"])
        except Exception:  # noqa: BLE001
            o = {}
        if o:
            out = {
                "date": rows[0]["review_date"],
                "verdict": o.get("verdict"),
                "mainlines": [m.get("sector") for m in (o.get("mainlines") or [])][:4],
                "actions": o.get("actions") or [],
                "risks": [r.get("label") for r in (o.get("risks") or [])][:4],
            }
    # 成绩单无论有没有上一份都要给 —— 它是"事实"，不是"上次的回忆"
    try:
        import review_score as RS
        out["scoreboard"] = RS.scoreboard_text(conn)
    except Exception:  # noqa: BLE001
        log.exception("[ai_review] 成绩单生成失败")
        out["scoreboard"] = ""
    return out


def _sec_health(conn, date: str) -> list:
    """数据新鲜度 —— 让模型知道哪块数据是旧的，不许拿旧数据冒充今日。"""
    checks = [
        ("主线板块", "sector_chain", "trade_date"),
        ("龙虎榜", "lhb_stocks", "trade_date"),
        ("全市场快照", "stock_daily_snapshot", "trade_date"),
        ("新闻", "news", "publish_time"),
        ("政策", "policy_news", "pub_time"),
        ("公告", "stock_ann", "notice_date"),
        ("自选", "self_pick", "pick_date"),
    ]
    out = []
    for label, table, col in checks:
        d = _one(conn, "SELECT MAX(%s) FROM %s" % (col, table))
        ds = str(d)[:10] if d else ""
        out.append({
            "name": label,
            "latest": ds,
            "stale": bool(ds and ds < date),
        })
    return out


def _sec_harvest(conn) -> dict:
    """局面（2026-10-31）：盘面结构 + 今日陷阱名单 + 实证边界。

    为什么单列一节而不是并进"市场环境"：市场环境讲的是指数涨跌，
    这一节讲的是**筹码与收割结构**（涨停/炸板/连板/开板派发），
    两者是不同维度，混在一起模型会把"跌"和"该跑"当成一回事。
    取数失败一律降级为空，绝不让它拖垮整份复盘。
    """
    try:
        import harvest as HARVEST
        p = HARVEST.payload(conn)
        if not p.get("ok"):
            return {}
        p["text"] = HARVEST.text_block(conn).replace(
            "## 七、局面（结构层，全部来自日线实测）", "### 盘面结构（日线实测，不是推断）")
        return p
    except Exception:  # noqa: BLE001
        logging.exception("_sec_harvest 失败")
        return {}


def _sec_oss(conn) -> dict:
    """开源情报（2026-10-30）：开源社区流行、但已被我们数据推翻的规则。

    为什么必须进上下文：模型最容易被「均线多头」「突破放量」「超买回调」这类
    流行说法带走，而这些说法在本系统严格口径下是稳定负超额（长样本 t≈−10）。
    不告诉它，它就会用一套已被证伪的语言写复盘，还写得很有说服力 —— 这正是
    evidence.py 建账本要解决的同一个问题。
    """
    try:
        import oss_mine as OSS
        txt = OSS.text_block(conn)
        if not txt:
            return {}
        p = OSS.payload(conn)
        return {"text": txt, "stats": p.get("stats") or {},
                "span": p.get("data_span") or {}}
    except Exception:  # noqa: BLE001
        logging.exception("_sec_oss 失败")
        return {}


def _sec_selection(conn) -> dict:
    """选股方向（2026-10-30）：短线形态证伪 + 低波池。

    与 _sec_oss 同类但更硬：这里不只推翻流行说法，还给出了「用户直觉本身
    被数据推翻」的证据 —— 「长期横盘蓄势待爆发」在 2013-2026 实测 2 年 −0.6%、
    胜率 35%，是所有底部形态里最差的一组；真正有效的是「一直低波」。
    不写进上下文，模型会用「底部蓄势、等待突破」这套语言写出一篇看似专业的废话。
    """
    try:
        import selection as SEL
        txt = SEL.text_block(conn)
        if not txt:
            return {}
        p = SEL.payload(conn)
        return {"text": txt, "stats": p.get("stats") or {},
                "asof": p.get("asof")}
    except Exception:  # noqa: BLE001
        logging.exception("_sec_selection 失败")
        return {}


def build_context(conn, date: str, provider: str = DEFAULT_PROVIDER) -> dict:
    """汇聚当日信息包。每个分区独立取数，单区失败不影响整体。"""
    # 先回填「上次推荐的票现在什么结果」——**必须在取上下文之前做**，
    # 否则喂给模型的是昨天还没更新的成绩，它会照着旧数据"认账"。
    # 实测这一步 0.1~2s（只扫参与计分的那几天），可忽略；失败也不影响复盘。
    _backfill_quiet(conn)

    ctx = {
        "review_date": date,
        "market": _sec_market(conn, date),
        "mainlines": _sec_mainlines(conn, date),
        "money": _sec_money(conn, date),
        "news": _sec_news(conn, date),
        "attention": _sec_attention(conn, date),
        "portfolio": _sec_portfolio(conn, date),
        "doctrine": _sec_doctrine(conn),
        "evidence": _sec_evidence(conn),
        "last": _sec_last(conn, date, provider),
        "data_health": _sec_health(conn, date),
        "harvest": _sec_harvest(conn),
        "oss": _sec_oss(conn),
        "selection": _sec_selection(conn),
        "picks_ctx": "",
    }
    try:
        import review_score as RS
        ctx["picks_ctx"] = RS.picks_context(conn, date)
    except Exception:  # noqa: BLE001
        log.exception("[ai_review] 上次荐股对账单生成失败")
    return ctx


_last_backfill = 0.0


def _backfill_quiet(conn) -> None:
    """回填 + 限流（5 分钟）。失败只记日志，绝不让它拖垮复盘。"""
    global _last_backfill
    now = time.time()
    if now - _last_backfill < 300:
        return
    _last_backfill = now
    try:
        import review_score as RS
        RS.score_pending(conn)
    except Exception:  # noqa: BLE001
        log.exception("[ai_review] 复盘前回填失败（继续用已有数字）")



# ---------------------------------------------------------------- prompt

SYSTEM_PROMPT = """你是一位为个人投资者服务的 A 股交易员。**不是研究员，是交易员。**

## 你的产出是什么

**一张能直接照着下单的票单，加一份认账的复盘。**

用户不需要你讲方向、讲逻辑、讲研报。他自己会判断；他要你替他做完「筛票 → 打分 → 给价位」这件事，
把结论端上来。一份没有具体代码的复盘对他**零价值**——那是他自己也能干的事，不需要花钱问你。

硬性要求：

1. **必须给票。** picks 1~6 只，每只必须有 6 位代码、推荐分、买入区间、止损价、目标价。
   **1 只是完全合法的答案，好过 3 只凑数。** 宁可只推 1 只真正想买的，也不要为了凑数量塞一只
   你自己都不信的——系统会逐条机检，价位为 0、写「占位」、买入区倒挂的条目会被标红、不入账、
   也不计入你的准确率，等于白写还留下污点。
   代码不许空，不许拿「某板块龙头」「低波池候选」「XX 概念」这类**占位词**充数——
   那不是票，那是把选择权又推回给他。若当日确实一只都选不出，picks 写空数组，
   并在 verdict 里明说「今天没有值得买的票」。**空仓是合法结论，凑数是错的。**
2. **分数是唯一排序依据。** score 0~100，含义写死：
   「未来 5 个交易日，该股跑赢全市场等权平均的概率（%）的估计」。
   50 = 与大盘持平，60 = 略强，70 = 明显值得买，80 以上 = 强烈。
   **score 必须等于 score_breakdown 四项之和**（trend/money/position/catalyst 各 0~25）。
   不许先拍一个总分再倒推分项。评不到 60 分的票不要放进 picks。
3. **给价位，不给「观察一下」。** entry_low~entry_high 是今天/明天真能挂单的区间；
   stop 是认错价（一般 −5%~−8%）；target 是止盈价。三个价都要能落在当日收盘价附近可执行的范围里。
4. **一句话逻辑。** thesis ≤30 字，why_now ≤40 字。写不下 = 你没想清楚。
   不要「受益于行业景气度提升」这种谁都能写的话，也不要复述研报。
5. **必须认账。** picks_review 段逐条回答上一份推的票：当时的依据今天还成立吗、哪一条先失效了、
   下次遇到同样结构改什么。系统会把真实收盘价结果给你，**以它为准**，不许自评命中率，
   不许用「市场环境变化」带过——必须指到可核对的字段或事件。
6. **不许编。** 信息包里没有的代码/数字/事件一律不得出现，缺失就写「数据缺失」。
7. **实证边界是硬约束**：第八段标 refuted / watch 的说法一律不得作为买入理由；
   标 weak 的可以提示，但不能单独构成买入理由。若你的票只能靠其中某条撑着，就把它从 picks 里去掉。
8. 结论仅供研究参考，不给绝对化承诺。
9. 必须提出 1~3 条**可被回测检验**的假说（hypotheses）。条件必须能用现成字段表达，
   写清预期方向；系统会拿它去回测并进入下一轮上下文。提错了比不提更有价值。
10. **局内人推演（insider 段，必答但只写 3 句）**：假设你是这个市场里资金量最大、信息最早的一方，
   要在接下来 5 个交易日内挣钱，你会怎么布这个局？
   ① `where_to_lure` 最想让散户在哪个位置接盘（挂涨停/炸板/连板/陷阱名单的数据）；
   ② `squeeze` 先砸哪类、托哪类（结合板块阶段）；③ `reverse` 据此我今天该避免什么。
   撑不住的地方必须写「这一步是猜测」，不许写成脱离数据的阴谋论，也不许点名任何个股会被操纵。

只输出 JSON，不要 markdown 代码块，不要任何解释性前后缀。

**字段重要度 = 从上到下。** picks 是这份东西的主体，其余都是为了支撑它。

JSON 结构：
{
  "verdict": "一句话结论，30字内，要含方向与仓位（例：普跌不接反弹，只留两成仓做强势股）",
  "confidence": 0-100,

  "picks": [
    {
      "symbol": "6位代码，必填，不许空",
      "name": "股票名称",
      "score": 78,
      "score_breakdown": {"trend": 20, "money": 22, "position": 18, "catalyst": 18},
      "action": "买 / 等回踩 / 观察",
      "entry_low": 11.20, "entry_high": 11.55,
      "stop": 10.62, "target": 13.10,
      "horizon": 5,
      "thesis": "30字内：为什么是它",
      "why_now": "40字内：为什么是今天买，不是昨天也不是明天",
      "evidence": ["可核对的事实，带数字（例：主力净流入1.2亿，占成交额18%）", "…", "…"],
      "invalidate": "30字内：什么信号出现就立刻认错"
    }
  ],

  "picks_review": [
    {"symbol": "上一份推的代码", "name": "名称",
     "still_valid": true,
     "first_break": "哪一条依据先失效（指到字段或事件）；没失效就写「未失效」",
     "fix": "20字内：下次遇到同样结构你要改什么"}
  ],

  "market_read": {"state": "普涨/结构/震荡/普跌 之一", "logic": "80字内，必须引用信息包数字"},
  "mainlines": [
    {"sector": "板块名", "logic": "40字内", "stage": "启动/加速/高潮/退潮"}
  ],
  "insider": {
    "where_to_lure": "80字内，必须挂涨停/炸板/连板/陷阱名单的数据",
    "squeeze": "80字内，先砸哪类托哪类",
    "reverse": "60字内，据此我今天该避免什么",
    "data_backed": true
  },
  "risks": [{"label": "风险名", "detail": "40字内", "level": "高/中/低"}],
  "portfolio_advice": [{"name": "股票名", "advice": "加/减/持/清/观察", "reason": "40字内"}],
  "disagreements": [
    {"point": "分歧点", "rule_view": "规则引擎会怎么说", "my_view": "你怎么看", "why": "为什么"}
  ],
  "vs_last": {"available": true, "hit": ["兑现了的"], "miss": ["没兑现的及原因"]},
  "actions": ["今天该做的具体动作，最多3条，每条20字内"],
  "hypotheses": [
    {"statement": "一句话结论（20字内）",
     "predicate": "可回测条件，形如「字段 运算符 数值」，可用 and 连接两个；运算符限 > >= < <= ==",
     "horizon": 5,
     "expect": "negative 或 positive（预期 T+horizon 相对全市场的超额方向）",
     "rationale": "为什么这么猜，60字内"}
  ]
}

picks 的硬约束（会机检，不合格的整份作废）：
  · 1~6 条（实在没有就空数组 + verdict 说明），**按 score 从高到低排列**
  · score = trend + money + position + catalyst，四项都在 0~25，总分 0~100
  · entry_low < entry_high，stop < entry_low，target > entry_high，**四个价都必须 > 0**
  · 每只的 evidence 至少 2 条且必须有数字
  · 不许出现「占位」「待补」「示例」这类字样 —— 出现即判该条无效

hypotheses 的 predicate 只能用下列字段（其它一律视为无效）：
  ret1       当日涨幅%          ret2    两日累计涨幅%      ret5    前5日涨幅%
  mom20      近20日涨幅%
  volume_ratio 量比（本项目口径=当日量/含当日5日均量，上限≈5，不要按通达信量比理解）
  turnover   龙虎榜当日换手率%   lhb_hit 近10日上过龙虎榜=1
  lift_days  距解禁还有几个交易日（无解禁事件=-1）
  ann_neg    近20日有股东减持公告=1
  consec_up  截至当日连续上涨天数
  rsi6 / macd_hist / bias6 / ma20_dev
  macd_cross_down  当日 MACD 柱由正转负=1（事件型，与「柱为负」的状态型不同）
  ma20_break       当日收盘跌破 MA20=1（事件型，与「已低于 MA20」不同）
注意：事件型字段才是「刚发生」，状态型字段覆盖近半股票，用错会让结论完全不同。
"""


# 输出契约（给 WorkBuddy 通道用）：与 SYSTEM_PROMPT 里要求的结构保持一致。
# 单独抽出来是为了让 MCP 侧无需解析提示词文本就能拿到「回填必须长什么样」。
OUTPUT_SCHEMA_HINT = {
    "verdict": "一句话结论，30字内，含方向与仓位",
    "confidence": "0-100 整数",
    # 2026-11-02：主体从「机会（可空）」改成「票单（必须有代码+分数+价位）」。
    # 老字段 opportunities 仍可读（历史数据兼容），但新产出必须走 picks。
    "picks": [{
        "symbol": "6位代码，必填", "name": "名称",
        "score": "0-100，未来5日跑赢全市场的概率估计",
        "score_breakdown": {"trend": "0-25", "money": "0-25",
                            "position": "0-25", "catalyst": "0-25"},
        "action": "买/等回踩/观察",
        "entry_low": "数值", "entry_high": "数值",
        "stop": "数值", "target": "数值", "horizon": "持有交易日，整数",
        "thesis": "30字内", "why_now": "40字内",
        "evidence": ["带数字的可核对事实，至少2条"],
        "invalidate": "30字内",
    }],
    "picks_review": [{"symbol": "", "name": "", "still_valid": True,
                      "first_break": "哪条依据先失效", "fix": "20字内"}],
    "market_read": {"state": "普涨/结构/震荡/普跌", "logic": "80字内"},
    "mainlines": [{"sector": "板块名", "logic": "40字内", "stage": "启动/加速/高潮/退潮"}],
    "insider": {"where_to_lure": "80字内", "squeeze": "80字内",
                "reverse": "60字内", "data_backed": True},
    "risks": [{"label": "风险名", "detail": "40字内", "level": "高/中/低"}],
    "portfolio_advice": [{"name": "股票名", "advice": "加/减/持/清/观察", "reason": "40字内"}],
    "disagreements": [{"point": "分歧点", "rule_view": "规则引擎会怎么说",
                       "my_view": "你怎么看", "why": "为什么"}],
    "vs_last": {"available": True, "hit": ["兑现了的"], "miss": ["没兑现的及原因"]},
    "actions": ["最多3条，每条20字内"],
    "hypotheses": [{"statement": "20字内", "predicate": "字段 运算符 数值",
                    "horizon": 5, "expect": "negative/positive", "rationale": "60字内"}],
}


def validate_picks(out: dict) -> dict:
    """机检 picks 是否可执行。**不通过不算失败，但页面必须显示出来。**

    为什么要在代码里查而不是只在提示词里写：
    提示词是"请求"，不是"约束"。模型有相当概率返回 score=85 但 breakdown 加起来 62，
    或者 stop 高于 entry_low（止损设在买入价上方 = 一买就触发）。
    这些错在文字里看不出来，落到实盘就是钱。所以逐条查，把问题摆到页面上。

    返回 {"ok": n, "bad": [{"symbol","issue"}], "clean": bool}
    """
    issues: list = []
    picks = out.get("picks") if isinstance(out, dict) else None
    if not isinstance(picks, list):
        return {"ok": 0, "bad": [{"symbol": "-", "issue": "没有 picks 段"}], "clean": False}
    good = 0
    for i, p in enumerate(picks, 1):
        if not isinstance(p, dict):
            issues.append({"symbol": "#%d" % i, "issue": "不是对象"})
            continue
        sym = str(p.get("symbol") or "").strip()
        tag = sym or "#%d" % i
        bad = []
        if not _re_sym.match(sym):
            bad.append("代码不是 6 位数字（%r）" % sym)
        try:
            sc = int(round(float(p.get("score"))))
        except Exception:  # noqa: BLE001
            sc = -1
        if not (0 <= sc <= 100):
            bad.append("推荐分缺失或越界")
        bd = p.get("score_breakdown") or {}
        if isinstance(bd, dict) and bd:
            try:
                tot = sum(float(bd.get(k) or 0) for k in ("trend", "money", "position", "catalyst"))
                if abs(tot - sc) > 1.5:
                    bad.append("四项分解合计 %.0f ≠ 总分 %d" % (tot, sc))
            except Exception:  # noqa: BLE001
                bad.append("score_breakdown 不是数字")
        else:
            bad.append("没有 score_breakdown，分数无法追溯")
        def _n(k):
            try:
                v = p.get(k)
                return float(v) if v not in (None, "") else None
            except Exception:  # noqa: BLE001
                return None
        lo, hi, st, tg = _n("entry_low"), _n("entry_high"), _n("stop"), _n("target")
        if None in (lo, hi, st, tg):
            bad.append("买入区间/止损/目标缺项")
        elif min(lo, hi, st, tg) <= 0:
            # 实测（2026-11-02 首跑）：模型为了凑够「3 只」写了一只
            # entry/stop/target 全 0、thesis 写「占位」的票。这种条目一旦入账，
            # 就会以"永不触发的无效推荐"混进准确率分母 —— 必须在入口拦掉。
            bad.append("价位为 0 或负数（占位条目）")
        else:
            if not (lo < hi):
                bad.append("买入区间倒挂（low ≥ high）")
            if not (st < lo):
                bad.append("止损价不在买入区间下方")
            if not (tg > hi):
                bad.append("目标价不在买入区间上方")
        ev = p.get("evidence")
        if not isinstance(ev, list) or len([x for x in ev if str(x).strip()]) < 2:
            bad.append("证据少于 2 条")
        _txt = " ".join(str(p.get(k) or "") for k in ("thesis", "why_now", "invalidate"))
        _txt += " " + " ".join(str(x) for x in (ev if isinstance(ev, list) else []))
        if re.search(r"占位|待补|示例|TODO|待填", _txt, re.I):
            bad.append("含「占位/待补」这类未完成字样")
        if bad:
            issues.append({"symbol": tag, "issue": "；".join(bad)})
        else:
            good += 1
    return {"ok": good, "bad": issues, "clean": not issues and good > 0}


_re_sym = re.compile(r"^\d{6}$")



def _render_user(ctx: dict) -> str:
    """把信息包渲染成给模型读的中文文本（不是塞原始 JSON —— 中文叙述更省 token 也更准）。"""
    L: list = []
    L.append("【复盘日期】%s" % ctx.get("review_date"))

    m = ctx.get("market") or {}
    L.append("\n## 一、市场环境")
    b = m.get("breadth") or {}
    if b:
        L.append("全市场横截面（%s）：共%d只，涨%d 跌%d 平%d；涨停%d 跌停%d；涨超5%%的%d只，跌超5%%的%d只"
                 % (b.get("as_of") or "-", b.get("total", 0), b.get("up", 0), b.get("down", 0),
                    b.get("flat", 0), b.get("limit_up", 0), b.get("limit_down", 0),
                    b.get("up_gt5", 0), b.get("dn_gt5", 0)))
        L.append("等权平均涨幅 %+.2f%%，市值加权涨幅(近似指数) %+.2f%%，中位数 %+.2f%%，全市场成交额约 %.0f 亿"
                 % (b.get("avg_pct") or 0, b.get("cap_weighted_pct") or 0,
                    b.get("median_pct") or 0, b.get("amount_yi") or 0))
    if m.get("note"):
        L.append("（口径说明：%s）" % m["note"])
    if m.get("regime"):
        L.append("系统判定的市场状态：%s（%s）" % (m["regime"].get("state"), m["regime"].get("note") or ""))

    hv = ctx.get("harvest") or {}
    if hv.get("text"):
        L.append("\n" + hv["text"])

    os_ = ctx.get("oss") or {}
    if os_.get("text"):
        L.append("\n" + os_["text"])

    se_ = ctx.get("selection") or {}
    if se_.get("text"):
        L.append("\n" + se_["text"])

    L.append("\n## 二、主线板块（按当日主力净流入排序）")
    for i, s in enumerate(ctx.get("mainlines") or [], 1):
        L.append("%d. %s：净流入%.2f亿，榜上%d只，龙头%s(%+.2f%%)，梯队%s%s"
                 % (i, s.get("sector"), s.get("net_yi") or 0, s.get("onlist") or 0,
                    s.get("leader") or "-", s.get("leader_pct") or 0,
                    "、".join(s.get("tiers") or []) or "无",
                    ("，分支：" + "、".join(s["branches"])) if s.get("branches") else ""))
        if s.get("t1_avg") is not None or s.get("t3_avg") is not None:
            L.append("   该板块历史：T+1平均%+.2f%%，T+3平均%+.2f%%"
                     % (s.get("t1_avg") or 0, s.get("t3_avg") or 0))

    mo = ctx.get("money") or {}
    L.append("\n## 三、资金")
    if mo.get("lhb"):
        L.append("龙虎榜净买卖前列：" + "；".join(
            "%s(%s) 净%+.0f万 [%s]" % (x["name"], x["code"], x["net_wan"] or 0, x["reason"])
            for x in mo["lhb"][:8]))
    if mo.get("board_flow"):
        L.append("板块资金流前列：" + "；".join(
            "%s 净%.2f亿(%+.2f%%)" % (x["board"], x["net_yi"] or 0, x["pct"] or 0)
            for x in mo["board_flow"][:8]))

    nw = ctx.get("news") or {}
    L.append("\n## 四、消息面")
    if nw.get("policy"):
        L.append("政策与要闻（按重要性）：")
        for x in nw["policy"][:10]:
            L.append("  - [%s|重要度%s] %s%s" % (x.get("issuer") or "-", x.get("importance") or "-",
                                              x["title"],
                                              ("（涉及：" + x["sectors"] + "）") if x.get("sectors") else ""))
    if nw.get("ann"):
        L.append("公司公告（仅利好/利空，已剔除例行公告）：" + "；".join(
            "%s %s[%s]" % (x["name"], x["title"], x.get("tone") or "")
            for x in nw["ann"][:8]))
    if nw.get("overseas_hot"):
        L.append("外围热点（带国内传导）：")
        for x in nw["overseas_hot"][:4]:
            L.append("  - %s%s%s" % (x["title"],
                                     ("（国内：" + x["sectors"] + "）") if x.get("sectors") else "",
                                     ("｜传导：" + x["transmission"]) if x.get("transmission") else ""))
    elif nw.get("overseas_news"):
        L.append("海外资讯：" + "｜".join(
            "%s%s" % (x["title"], ("[" + (x["sectors"] or "") + "]") if x.get("sectors") else "")
            for x in nw["overseas_news"][:5]))

    se = ctx.get("attention") or {}
    L.append("\n## 五、外部人气榜（投顾/榜单看多信号，非新闻、非本系统结论）")
    if se.get("extref"):
        L.append("外部人气榜：" + "；".join(
            "%s[%s/%s]" % (x["name"], x.get("dir") or "", x.get("strength") or "")
            for x in se["extref"][:6]))
    else:
        L.append("（本期无外部人气数据）")

    pf = ctx.get("portfolio") or {}
    L.append("\n## 六、系统自选（判断要落到这些票上）")
    if pf.get("self_pick"):
        L.append("系统自选：" + "、".join(
            "%s(%s分/%s)" % (x["name"], x.get("score"), x.get("tier") or "")
            for x in pf["self_pick"][:10]))
    else:
        L.append("（无自选数据）")
    # 2026-10-31 硬提示：模型过去会把"论坛讨论热度"当散户情绪，而源已停；这里明写一句，
    # 防止它从训练记忆里自行补一个"市场情绪"出来。
    L.append("（注：本项目自 2026-10-30 起不再采集论坛/股吧言论，任何"
             "「散户怎么看」「讨论热度」的说法都没有数据支撑，不得编造。）")

    dc = ctx.get("doctrine") or {}
    L.append("\n## 七、我自己的交易体系（你判断时必须对齐这套准则）")
    for d in (dc.get("docs") or []):
        L.append("### 《%s》%s%s" % (d.get("title"), d.get("version") or "",
                                    ("·" + d["track"]) if d.get("track") else ""))
        for c in (d.get("conclusions") or [])[:5]:
            L.append("  · " + c)
        for p in (d.get("pitfalls") or [])[:2]:
            L.append("  × 要避开：" + p)
        for s in (d.get("schools") or [])[:1]:
            L.append("  ▸ 流派：" + s)
    if dc.get("rules"):
        L.append("### 数据自生长规则（历史回测得出的有效组合，含超额收益）")
        for r in dc["rules"][:8]:
            L.append("  · 当 %s → %s" % (r.get("when"), r.get("then")))

    ev = ctx.get("evidence") or {}
    L.append("\n## 八、本系统的实证边界（硬约束，不是参考）")
    if ev.get("text"):
        L.append(ev["text"])
        L.append("↑ 标 refuted / watch 的照法**一律不得作为判断依据**。"
                 "若你的结论只能靠其中某条支撑，就如实说「该方向缺证据」。")
    else:
        L.append("（实证账本不可用；本轮凡涉及量化依据，必须标「未经验证」）")

    last = ctx.get("last") or {}
    L.append("\n## 九、上次判断与你的真实成绩单")
    if last.get("date"):
        L.append("上一份复盘日期：%s" % last.get("date"))
        L.append("当时结论：%s" % (last.get("verdict") or ""))
        L.append("当时看好的主线：%s" % "、".join(last.get("mainlines") or []))
        L.append("当时提示的风险：%s" % "、".join(last.get("risks") or []))
        L.append("当时给的行动：%s" % "；".join(last.get("actions") or []))
    else:
        L.append("（无历史复盘，首份）")
    if last.get("scoreboard"):
        L.append("")
        L.append(last["scoreboard"])
        L.append("↑ 以上是系统按收盘价回填的真实结果。vs_last 段必须以它为准；"
                 "不许写「已兑现」而成绩单显示为错，也不许在样本不足时声称自己准确。")
    # 逐票对账单（2026-11-02）：上面是"汇总成绩"，这里把上次每一只票摊开。
    # 目的不是羞辱它，是让 picks_review 有具体对象可归因 —— 没有这一段的模型
    # 只能写"上次判断基本兑现"这种自我评价，写了等于没写。
    if ctx.get("picks_ctx"):
        L.append("")
        L.append(ctx["picks_ctx"])

    h = ctx.get("data_health") or []
    if h:
        stale = [x for x in h if x.get("stale")]
        L.append("\n## 十、数据新鲜度")
        L.append("；".join("%s最新%s%s" % (x["name"], x.get("latest") or "无",
                                          "(旧)" if x.get("stale") else "") for x in h))
        if stale:
            L.append("注意：以下数据不是复盘日当天的，涉及它们的判断要降低置信度："
                     + "、".join(x["name"] for x in stale))

    text = "\n".join(L)
    return text


# ---------------------------------------------------------------- LLM 调用


def call_llm(provider: str, system: str, user: str) -> dict:
    """调 OpenAI 兼容接口。返回 {ok, text, usage, elapsed_ms, err}。"""
    p = PROVIDERS.get(provider)
    if not p:
        return {"ok": False, "err": "未知模型：%s" % provider}
    key = provider_key(provider)
    if not key:
        return {"ok": False, "err": "未配置 %s 的 API Key（设置面板填入，或设环境变量 %s）"
                                    % (p["label"], p["env"])}
    body = json.dumps({
        "model": p["model"],
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": 0.4,
        "max_tokens": 3000,
        "response_format": {"type": "json_object"},
    }).encode("utf-8")
    req = urllib.request.Request(
        p["url"], data=body, method="POST",
        headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"},
    )
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            raw = resp.read().decode("utf-8", "ignore")
        j = json.loads(raw)
        text = (j.get("choices") or [{}])[0].get("message", {}).get("content") or ""
        usage = j.get("usage") or {}
        return {
            "ok": True,
            "text": text,
            "usage": {
                "prompt_tokens": usage.get("prompt_tokens") or 0,
                "completion_tokens": usage.get("completion_tokens") or 0,
            },
            "elapsed_ms": int((time.time() - t0) * 1000),
        }
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8", "ignore")[:300]
        except Exception:  # noqa: BLE001
            pass
        return {"ok": False, "elapsed_ms": int((time.time() - t0) * 1000),
                "err": "HTTP %s：%s" % (e.code, _clip(detail, 240))}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "elapsed_ms": int((time.time() - t0) * 1000),
                "err": "%s: %s" % (type(e).__name__, _clip(str(e), 200))}


_JSON_RE = re.compile(r"\{[\s\S]*\}")


def parse_output(text: str) -> dict:
    """模型输出可能是裸 JSON、也可能裹了 ```json 或前后废话 —— 都容错。"""
    if not text:
        return {}
    t = text.strip()
    if t.startswith("```"):
        t = re.sub(r"^```[a-zA-Z]*\s*", "", t)
        t = re.sub(r"\s*```$", "", t)
    try:
        return json.loads(t)
    except Exception:  # noqa: BLE001
        pass
    m = _JSON_RE.search(t)
    if m:
        try:
            return json.loads(m.group(0))
        except Exception:  # noqa: BLE001
            return {}
    return {}


# ---------------------------------------------------------------- 主流程


def run(conn, date: str = "", provider: str = DEFAULT_PROVIDER) -> dict:
    """跑一次完整复盘并落库。**同步阻塞**，调用方负责放到后台线程。"""
    ensure_schema(conn)
    date = date or latest_trade_date(conn)
    provider = provider or DEFAULT_PROVIDER
    model = (PROVIDERS.get(provider) or {}).get("model") or provider

    ctx = build_context(conn, date, provider)
    user_prompt = _render_user(ctx)

    res = call_llm(provider, SYSTEM_PROMPT, user_prompt)
    out_json = parse_output(res.get("text") or "")
    ok = bool(res.get("ok")) and bool(out_json)

    err = res.get("err") or ""
    if res.get("ok") and not out_json:
        err = "模型返回无法解析为 JSON（原文已存 raw_text）"

    row = {
        "review_date": date,
        "provider": provider,
        "model": model,
        "status": "ok" if ok else "err",
        "context_json": json.dumps(ctx, ensure_ascii=False),
        "context_chars": len(user_prompt),
        "output_json": json.dumps(out_json, ensure_ascii=False) if out_json else None,
        "raw_text": res.get("text") or None,
        "prompt_tokens": (res.get("usage") or {}).get("prompt_tokens", 0),
        "completion_tokens": (res.get("usage") or {}).get("completion_tokens", 0),
        "elapsed_ms": res.get("elapsed_ms") or 0,
        "err": err or None,
        "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    try:
        conn.execute(
            "INSERT INTO ai_review (review_date, provider, model, status, context_json, "
            "context_chars, output_json, raw_text, prompt_tokens, completion_tokens, "
            "elapsed_ms, err, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (row["review_date"], row["provider"], row["model"], row["status"],
             row["context_json"], row["context_chars"], row["output_json"],
             row["raw_text"], row["prompt_tokens"], row["completion_tokens"],
             row["elapsed_ms"], row["err"], row["created_at"]),
        )
        conn.commit()
    except Exception:  # noqa: BLE001
        log.exception("ai_review 落库失败")
        try:
            conn.rollback()
        except Exception:  # noqa: BLE001
            pass

    _register_calls(conn, date, provider, out_json, res.get("elapsed_ms") or 0)

    return {
        "ok": ok,
        "review_date": date,
        "provider": provider,
        "model": model,
        "err": err,
        "elapsed_ms": row["elapsed_ms"],
        "tokens": {"prompt": row["prompt_tokens"], "completion": row["completion_tokens"]},
        "context_chars": row["context_chars"],
        "data": out_json,
    }


def _register_calls(conn, date: str, provider: str, out_json, elapsed_ms: int = 0) -> dict:
    """落库后登记「可计分单元」与「可回测假说」（2026-10-31）。

    这两步是整个回路的下半段：复盘写完不算完，得让它**能被事后算账**。
    任何一步失败都只记日志，绝不影响复盘本身的保存。
    """
    info = {"calls": 0, "hypotheses": 0}
    if not isinstance(out_json, dict) or not out_json:
        return info
    try:
        rid = int(conn.execute(
            "SELECT MAX(id) FROM ai_review WHERE review_date=? AND provider=?",
            (date, provider)).fetchone()[0])
    except Exception:  # noqa: BLE001
        rid = 0
    try:
        import review_score as RS
        info["calls"] = RS.record_from_review(conn, rid, date, provider, out_json)
    except Exception:  # noqa: BLE001
        log.exception("[ai_review] 计分单元登记失败 date=%s", date)
    try:
        import hypo as HP
        info["hypotheses"] = HP.record_from_review(conn, rid, date, provider,
                                                   out_json, elapsed_ms)
    except Exception:  # noqa: BLE001
        log.exception("[ai_review] 假说登记失败 date=%s", date)
    return info


def digest_of(row) -> dict:
    """把 DB 行转成前端可直接渲染的结构（不含 context/raw，避免响应过大）。"""
    if row is None:
        return {}
    d = dict(row)
    try:
        out = json.loads(d.get("output_json") or "{}")
    except Exception:  # noqa: BLE001
        out = {}
    return {
        "ok": d.get("status") == "ok",
        "id": d.get("id"),
        "review_date": d.get("review_date"),
        "provider": d.get("provider"),
        "model": d.get("model"),
        "created_at": d.get("created_at"),
        "elapsed_ms": d.get("elapsed_ms"),
        "context_chars": d.get("context_chars"),
        "tokens": {
            "prompt": d.get("prompt_tokens") or 0,
            "completion": d.get("completion_tokens") or 0,
        },
        "err": d.get("err"),
        "data": out,
        # 每次读都现算（纯字符串运算，代价可忽略）：票单是否可执行，
        # 必须在页面上看得见，而不是埋在日志里等出了事再回查。
        "picks_check": validate_picks(out),
    }


def context_brief(ctx_json: str) -> dict:
    """从落库的信息包里数出「这次 AI 到底读了多少东西」。

    为什么要有：进哥的核心质疑是「它真的思考了，还是在装」。光说"用了大模型"没用，
    得能指着屏幕说：它读了 N 份你自己的知识库、M 条政策、K 只股票的龙虎榜。
    统计得很轻（一次 json.loads），只在缓存命中/详情时调用，不进请求热路径之外的循环。
    """
    out = {"docs": 0, "rules": 0, "sections": 0}
    if not ctx_json:
        return out
    try:
        ctx = json.loads(ctx_json)
    except Exception:  # noqa: BLE001
        return out
    dc = ctx.get("doctrine") or {}
    out["docs"] = len(dc.get("docs") or [])
    out["rules"] = len(dc.get("rules") or [])
    # 有内容的分区数（不是空 dict/list 才算）
    n = 0
    for k in ("market", "mainlines", "money", "news", "attention", "portfolio", "doctrine", "last"):
        v = ctx.get(k)
        if isinstance(v, (dict, list)) and len(v) > 0:
            n += 1
    out["sections"] = n
    out["policy"] = len(((ctx.get("news") or {}).get("policy")) or [])
    out["mainlines"] = len((ctx.get("mainlines")) or [])
    out["lhb"] = len((((ctx.get("money") or {}).get("lhb"))) or [])
    out["hot_stocks"] = 0   # 2026-10-31 起废弃（论坛停抓，讨论热度无源），保留键名仅为兼容旧前端
    return out


def latest(conn, date: str = "", provider: str = "") -> dict:
    """取最近一份成功复盘；指定 provider 时只取该模型。"""
    ensure_schema(conn)
    sql = ("SELECT * FROM ai_review WHERE status='ok'")
    args: list = []
    if date:
        sql += " AND review_date=?"
        args.append(date)
    if provider:
        sql += " AND provider=?"
        args.append(provider)
    sql += " ORDER BY review_date DESC, id DESC LIMIT 1"
    rows = _q(conn, sql, tuple(args))
    if not rows:
        return {}
    d = digest_of(rows[0])
    d["context_stats"] = context_brief(rows[0]["context_json"])
    return d


def last_error(conn, date: str = "", provider: str = "") -> dict:
    """最近一次失败记录 —— 页面要能说清"为什么没出来"，而不是转圈。"""
    sql = "SELECT * FROM ai_review WHERE status='err'"
    args: list = []
    if date:
        sql += " AND review_date=?"
        args.append(date)
    if provider:
        sql += " AND provider=?"
        args.append(provider)
    sql += " ORDER BY id DESC LIMIT 1"
    rows = _q(conn, sql, tuple(args))
    return digest_of(rows[0]) if rows else {}


def history(conn, limit: int = 30) -> list:
    ensure_schema(conn)
    rows = _q(
        conn,
        "SELECT review_date, provider, model, status, elapsed_ms, err, output_json, created_at "
        "FROM ai_review ORDER BY id DESC LIMIT ?",
        (int(limit or 30),),
    )
    out = []
    for r in rows:
        try:
            o = json.loads(r["output_json"] or "{}")
        except Exception:  # noqa: BLE001
            o = {}
        out.append({
            "review_date": r["review_date"],
            "provider": r["provider"],
            "model": r["model"],
            "status": r["status"],
            "elapsed_ms": r["elapsed_ms"],
            "err": r["err"],
            "created_at": r["created_at"],
            "verdict": o.get("verdict") or "",
            "confidence": o.get("confidence"),
        })
    return out


def context_preview(conn, review_id: int) -> dict:
    """审计用：某次复盘到底喂了什么进去（用户有权看到 AI 读了哪些数据）。"""
    rows = _q(conn, "SELECT context_json, err FROM ai_review WHERE id=?", (review_id,))
    if not rows:
        return {}
    try:
        return json.loads(rows[0]["context_json"] or "{}")
    except Exception:  # noqa: BLE001
        return {}


# ============================================================================
# WorkBuddy 算力通道：分析队列（2026-10-29 新增）
# ============================================================================
"""为什么要有队列，而不是让本进程去调 WorkBuddy：

本进程是 HTTP 服务，职责是"把数据摆出来"，不该成为任何外部智能的同步依赖。
所以把两者解耦成「生产者 / 消费者」：
    本进程（生产者）  —— 取数、拼信息包、写进 ai_review_queue（state=pending），立即返回。
    WorkBuddy（消费者）—— 通过 MCP 取走 brief，用它自己的推理能力分析，submit 回填。

这样做的三个实际好处（不是设计洁癖）：
  1. 本进程零模型调用 → 不会出现"点了 AI 复盘，后端整体卡住"（这正是本模块原先的隐患，
     虽然 LLM 调用释放 GIL，但拼信息包要读十几张表，撞上写锁一样会抖）。
  2. 分析能力不绑定本机 API Key：没配 key 也能用，换模型不用改代码。
  3. 可审计：谁取的（taken_at）、什么时候回填（done_at）、原文是什么（raw_text）都留痕。
"""

_QUEUE_SCHEMA = """
CREATE TABLE IF NOT EXISTS ai_review_queue (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    review_date   TEXT NOT NULL,
    provider      TEXT NOT NULL DEFAULT 'workbuddy',
    state         TEXT NOT NULL DEFAULT 'pending',  -- pending / taken / done / error
    brief_chars   INTEGER DEFAULT 0,
    created_at    TEXT NOT NULL,
    taken_at      TEXT,
    done_at       TEXT,
    err           TEXT
);
CREATE INDEX IF NOT EXISTS ix_ai_queue_state ON ai_review_queue(state, id DESC);
CREATE INDEX IF NOT EXISTS ix_ai_queue_date  ON ai_review_queue(review_date, id DESC);
"""

_queue_ensured = False


def _conn_path(conn) -> str:
    """取连接指向的库文件路径（用于后台线程另开连接）。取不到返回空串。"""
    try:
        for r in conn.execute("PRAGMA database_list"):
            if r[1] == "main":
                return str(r[2] or "")
    except Exception:  # noqa: BLE001
        pass
    return ""


def _brief_dir() -> str:
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")


def _brief_path(qid: int) -> str:
    return os.path.join(_brief_dir(), "ai_brief_%s.json" % int(qid))


def _build_brief_payload(conn, date: str, provider: str) -> dict:
    ctx = build_context(conn, date, provider)
    prompt = _render_user(ctx)
    return {
        "review_date": date,
        "provider": provider,
        "system_prompt": SYSTEM_PROMPT,
        "user_prompt": prompt,
        "context": ctx,
        "schema": OUTPUT_SCHEMA_HINT,
    }


def _spawn_brief_builder(qid: int, date: str, provider: str, db_path: str) -> None:
    """后台拼包并落盘（2026-10-29）。

    为什么必须异步：实测 build_context 要读市场横截面/主线/资金/政策/情绪/持仓/知识库
    十余张表，单次 **16~18 秒**。放在请求线程里，用户点「开始分析」就要干等 16 秒，
    再叠加当时机器负载，很容易又撞上前端超时 —— 那就又回到"点不动"的老问题。
    改为：入队只写一行（毫秒级），拼包交给后台；WorkBuddy 取包时直接读落盘文件。
    """
    if not db_path:
        return

    def _run() -> None:
        con = None
        try:
            con = sqlite3.connect(db_path, timeout=30)
            con.row_factory = sqlite3.Row
            con.execute("PRAGMA busy_timeout=30000")
            payload = _build_brief_payload(con, date, provider)
            text = json.dumps(payload, ensure_ascii=False)
            os.makedirs(_brief_dir(), exist_ok=True)
            fp = _brief_path(qid)
            tmp = fp + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(text)
            os.replace(tmp, fp)          # 原子替换：绝不留下半截文件
            try:
                con.execute("UPDATE ai_review_queue SET brief_chars=? WHERE id=?", (len(text), int(qid)))
                con.commit()
            except Exception:  # noqa: BLE001
                pass
            log.info("[ai_review] 信息包已备好 queue_id=%s %d 字", qid, len(text))
        except Exception:  # noqa: BLE001
            log.exception("[ai_review] 后台拼包失败 queue_id=%s（取包时会现拼兜底）", qid)
        finally:
            try:
                if con is not None:
                    con.close()
            except Exception:  # noqa: BLE001
                pass

    try:
        import threading

        threading.Thread(target=_run, name="ai-brief-%s" % qid, daemon=True).start()
    except Exception:  # noqa: BLE001
        log.exception("[ai_review] 拼包线程启动失败 queue_id=%s", qid)


def ensure_queue(conn) -> None:
    """建队列表（幂等；与 ai_review 主表的 _ensured 各自独立，避免互相牵连）。"""
    global _queue_ensured
    if _queue_ensured:
        return
    try:
        conn.executescript(_QUEUE_SCHEMA)
        conn.commit()
        _queue_ensured = True
    except Exception:  # noqa: BLE001
        log.exception("ai_review_queue 建表失败")


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def queue_ensure(conn, date: str, provider: str = "workbuddy") -> dict:
    """确保「同日同 provider」有一个待处理任务。**立即返回，不做任何重活。**

    已有 pending/taken → 直接返回它（不重复入队，避免用户连点三次产生三份任务）。
    否则新建一条 pending，并把「拼信息包」扔到后台线程（16~18s 的活不能占请求线程）。
    """
    ensure_queue(conn)
    try:
        row = conn.execute(
            "SELECT id, state, created_at, brief_chars FROM ai_review_queue "
            "WHERE review_date=? AND provider=? AND state IN ('pending','taken') "
            "ORDER BY id DESC LIMIT 1", (date, provider)).fetchone()
    except Exception:  # noqa: BLE001
        log.exception("ai_review_queue 查询失败")
        row = None
    if row:
        return {"queue_id": row[0], "state": row[1], "created_at": row[2],
                "brief_chars": row[3] or 0, "reused": True}

    try:
        cur = conn.execute(
            "INSERT INTO ai_review_queue (review_date, provider, state, brief_chars, created_at) "
            "VALUES (?,?,?,?,?)", (date, provider, "pending", 0, _now()))
        conn.commit()
        qid = int(cur.lastrowid)
    except Exception as e:  # noqa: BLE001
        log.exception("ai_review_queue 入队失败")
        try:
            conn.rollback()
        except Exception:  # noqa: BLE001
            pass
        return {"queue_id": 0, "state": "error", "err": "入队失败：%s" % str(e)[:150]}

    db_path = _conn_path(conn)
    _spawn_brief_builder(qid, date, provider, db_path)
    log.info("[ai_review] 已入队 queue_id=%s date=%s provider=%s（信息包后台准备中）",
             qid, date, provider)
    return {"queue_id": qid, "state": "pending", "created_at": _now(),
            "brief_chars": 0, "building": True}


def queue_pending(conn, limit: int = 5) -> list:
    """列出待分析任务（给 MCP / WorkBuddy 用）。只返回元信息，不含 brief 正文。"""
    ensure_queue(conn)
    try:
        rows = conn.execute(
            "SELECT id, review_date, provider, state, brief_chars, created_at, taken_at "
            "FROM ai_review_queue WHERE state IN ('pending','taken') "
            "ORDER BY id DESC LIMIT ?", (int(limit),)).fetchall()
    except Exception:  # noqa: BLE001
        return []
    out = []
    for r in rows:
        out.append({
            "queue_id": r[0], "review_date": r[1], "provider": r[2], "state": r[3],
            "brief_chars": r[4], "created_at": r[5], "taken_at": r[6],
        })
    return out


def queue_brief(conn, qid: int) -> dict:
    """取某任务的完整分析包（含 system_prompt / user_prompt / context）。

    取走即标记 taken —— 这样前端能显示"WorkBuddy 已接单"，人也能看出来它在动。
    注意：重算不出 brief（brief 不入库，队列表只存长度），因此这里现取一次 context。
    历史任务需要复现时用 ai_review.context_json（那份是随结果一起落库的）。
    """
    ensure_queue(conn)
    try:
        row = conn.execute(
            "SELECT id, review_date, provider, state, created_at FROM ai_review_queue WHERE id=?",
            (int(qid),)).fetchone()
    except Exception:  # noqa: BLE001
        return {"ok": False, "err": "查询失败"}
    if not row:
        return {"ok": False, "err": "任务不存在：%s" % qid}
    if row[3] == "done":
        return {"ok": False, "err": "该任务已完成，请用 /api/ai_review/detail 读结果"}

    date = row[1]
    payload = None
    fp = _brief_path(qid)
    # 优先读后台已经备好的包（秒级）；没有才现拼（兜底，慢但可用）
    try:
        if os.path.exists(fp):
            with open(fp, "r", encoding="utf-8") as f:
                payload = json.load(f)
    except Exception:  # noqa: BLE001
        log.exception("读取落盘信息包失败 queue_id=%s，转现拼", qid)
        payload = None

    if not payload:
        try:
            payload = _build_brief_payload(conn, date, row[2])
        except Exception as e:  # noqa: BLE001
            log.exception("取分析包失败")
            return {"ok": False, "err": "取包失败：%s" % str(e)[:150]}

    try:
        conn.execute("UPDATE ai_review_queue SET state='taken', taken_at=? WHERE id=? AND state='pending'",
                     (_now(), int(qid)))
        conn.commit()
    except Exception:  # noqa: BLE001
        pass

    return {
        "ok": True,
        "queue_id": int(qid),
        "review_date": date,
        "provider": row[2],
        "system_prompt": payload.get("system_prompt") or SYSTEM_PROMPT,
        "user_prompt": payload.get("user_prompt") or "",
        "context": payload.get("context") or {},
        "schema": payload.get("schema") or OUTPUT_SCHEMA_HINT,
        "cached_brief": bool(os.path.exists(fp)),
    }


def queue_submit(conn, qid: int, output, raw_text: str = "", elapsed_ms: int = 0) -> dict:
    """回填分析结论：写进 ai_review 主表（前端读的就是它），并把队列任务标 done。

    output 可以是 dict，也可以是模型原始输出字符串（会走 parse_output 解析）。
    前端不需要知道 WorkBuddy 这条通道的存在——它只读 /api/ai_review。
    """
    ensure_queue(conn)
    try:
        row = conn.execute(
            "SELECT review_date, provider, state FROM ai_review_queue WHERE id=?",
            (int(qid),)).fetchone()
    except Exception:  # noqa: BLE001
        return {"ok": False, "err": "查询失败"}
    if not row:
        return {"ok": False, "err": "任务不存在：%s" % qid}
    date, provider = row[0], row[1]

    if isinstance(output, str):
        out_json = parse_output(output)
        if not raw_text:
            raw_text = output
    else:
        out_json = output if isinstance(output, dict) else {}

    if not out_json:
        try:
            conn.execute("UPDATE ai_review_queue SET state='error', err=?, done_at=? WHERE id=?",
                         ("回填内容无法解析为 JSON", _now(), int(qid)))
            conn.commit()
        except Exception:  # noqa: BLE001
            pass
        return {"ok": False, "err": "回填内容无法解析为 JSON（需要结构化对象）"}

    model = (PROVIDERS.get(provider) or {}).get("model") or provider
    try:
        conn.execute(
            "INSERT INTO ai_review (review_date, provider, model, status, context_json, "
            "context_chars, output_json, raw_text, prompt_tokens, completion_tokens, "
            "elapsed_ms, err, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (date, provider, model, "ok", json.dumps({"source": "workbuddy_mcp"}, ensure_ascii=False),
             0, json.dumps(out_json, ensure_ascii=False), raw_text or None,
             0, 0, int(elapsed_ms or 0), None, _now()))
        rid = int(conn.execute("SELECT MAX(id) FROM ai_review").fetchone()[0])
        conn.execute("UPDATE ai_review_queue SET state='done', done_at=?, err=NULL WHERE id=?",
                     (_now(), int(qid)))
        conn.commit()
    except Exception as e:  # noqa: BLE001
        log.exception("回填落库失败")
        try:
            conn.rollback()
        except Exception:  # noqa: BLE001
            pass
        return {"ok": False, "err": "落库失败：%s" % str(e)[:150]}

    # WorkBuddy 通道也必须登记计分单元/假说 —— 否则「用大脑做的复盘」反而成了唯一
    # 无法被算账的那条路径，那就本末倒置了。
    info = _register_calls(conn, date, provider, out_json, elapsed_ms)

    log.info("[ai_review] WorkBuddy 回填完成 queue_id=%s review_id=%s date=%s calls=%s hyps=%s",
             qid, rid, date, info.get("calls"), info.get("hypotheses"))
    return {"ok": True, "queue_id": int(qid), "review_id": rid, "review_date": date,
            "scored_units": info.get("calls"), "hypotheses": info.get("hypotheses")}


def queue_status(conn, qid: int = 0) -> dict:
    """队列状态（诊断用）：不传 qid 就返回全量统计 + 最近若干条。"""
    ensure_queue(conn)
    try:
        if qid:
            r = conn.execute("SELECT id, review_date, provider, state, created_at, taken_at, done_at, err "
                             "FROM ai_review_queue WHERE id=?", (int(qid),)).fetchone()
            if not r:
                return {"ok": False, "err": "任务不存在"}
            return {"ok": True, "item": {
                "queue_id": r[0], "review_date": r[1], "provider": r[2], "state": r[3],
                "created_at": r[4], "taken_at": r[5], "done_at": r[6], "err": r[7]}}
        rows = conn.execute("SELECT state, COUNT(*) FROM ai_review_queue GROUP BY state").fetchall()
        counts = {r[0]: r[1] for r in rows}
        recent = conn.execute("SELECT id, review_date, provider, state, created_at, done_at "
                              "FROM ai_review_queue ORDER BY id DESC LIMIT 10").fetchall()
        return {"ok": True, "counts": counts,
                "recent": [{"queue_id": r[0], "review_date": r[1], "provider": r[2],
                            "state": r[3], "created_at": r[4], "done_at": r[5]} for r in recent]}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "err": str(e)[:150]}
