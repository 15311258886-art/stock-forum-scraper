"""帖子情绪速览：把「最新帖子」从原帖列表改造成「结论 + 情绪」。

王总 2026-10-02 的原话：
    「你把这些帖子给我看，我不想看没有用……总结完之后，分析分析他们的情绪在哪儿。
     看某一只股票或者某一个板块，他们是看涨啊还是看跌呀。这种你直接给我输出结论，
     我要这种帖子链接没有什么意义。」

所以本模块只吐**结论**，不吐帖子列表：
    - 每只被点名的股票：看涨 / 看跌 / 分歧 / 中立 + 票数 + 净分 + 一句人话结论 + 代表原话
    - 每个行业板块：同上，按所属行业把个股再聚一层
    - 顶部一句总论：最近 24 小时情绪往哪边倒

口径纪律（与本项目其余模块一致）：
    1. 只统计**时间窗内**的帖子，窗内条数按 post_time 过滤（字符串字典序 = 时间序）；
    2. 单字「涨 / 跌」**不入词表**——「生意模式思考：对标【喜诗糖果】（定价权…」这种
       中性帖里常有"涨/跌"字样，用 analytics.score_sentiment 的老词表会把它误判成看空；
       这里只收双字以上的确定性词，宁可判「中性」也不乱贴标签；
    3. 「跌不动 / 不跌 / 不怕跌」这类否定式先抹掉再打分，否则同一句话能两头都算数；
    4. 样本不足（有效观点 < 2 条）时**显式降级**为「仅个别人提到」，不给强结论；
    5. 只取帖子正文做统计，不联网、不猜，缺数据就写清缺在哪。
"""
from __future__ import annotations

import copy
import logging
import re
import sqlite3
import sys
import threading
import time
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# ---------------------------------------------------------------- 情绪词表（唯一真源 = viewer/analytics.py）
# 2026-10-02 统一口径：词表与打分统一放在 analytics，本模块只做「聚合 → 结论」。
# 引用而非 copies：以后调词表改 analytics 一处，入库 / 情绪信号 / 本速览 三处同时生效。
from viewer import analytics as _an  # noqa: E402

POS_W = _an.POS_W      # dict，权重 1.0 强 / 0.5 弱
NEG_W = _an.NEG_W
INTENSIFIERS = _an.INTENSIFIERS
_sent_score = _an._sent_score

DB_PATH = ROOT / "viewer" / "app.db"
_CACHE_NAME: dict | None = None
_CACHE_IND: dict | None = None

# ---------------------------------------------------------------- 进程级结果缓存（带 TTL）
# 2026-10-13 性能修复：server.py 在每次 HTTP 请求里同步调用 build()，而 build() 是个
# CPU 重活（开独立 sqlite 连接、逐帖打分/抽代码/聚合，单次 6~8 秒）。单进程多线程的
# http.server 下，多个请求 + 后台调度叠加就把请求线程堵到整服务假死。
# 修复：在 build 外层套一层内存缓存，TTL 120 秒；命中即秒回深拷贝，未命中才重算。
# 服务是多线程的，必须用锁保证「查缓存 → 是否过期 → 重算 → 写回」这段的原子性，
# 否则可能多个线程同时穿透重算，反而放大卡顿（惊群）。
_BUILD_CACHE_TTL = 120.0  # 秒（两分钟）
# key = (hours, max_posts)；value = (计算时刻 timestamp, 结果对象)
_BUILD_CACHE: dict[tuple[int, int], tuple[float, dict]] = {}
_BUILD_CACHE_LOCK = threading.Lock()


def _score(text: str) -> float:
    """帖子正文 → 多空净分（>0 看多，<0 看空，0 中性）。

    2026-10-02：打分下沉到 `viewer.analytics._sent_score`，本模块只保留**聚合 + 结论**这一层。
    原先词表在本模块各存一份，与 ingest 入库用的老词表实测 17.4% 结论相反
    （「宁德时代跌不动…」老尺子判看空、新尺子判中性/看多）——同一只票两个地方唱反调。
    统一后：词条改一处，全站（入库 / 情绪信号 / 情绪速览）一起变。"""
    if not text:
        return 0.0
    return round(_sent_score(text), 2)


def _label(score: float) -> str:
    """净分 → 看多/看空/中性。判方向的门槛也在 analytics，别在两边各写一遍。"""
    return _an.sentiment_from_score(score)


def _reasons(texts: list[str], direction: str) -> list[str]:
    """从该标的的有方向帖子里抽「大家在说的理由」词（与方向同向、出现最多的前 3 个）。"""
    table = POS_W if direction == "看多" else NEG_W
    cnt: dict[str, int] = defaultdict(int)
    for t in texts:
        seen = set()
        for w in table:
            if w in t and w not in seen:
                seen.add(w)
                cnt[w] += 1
    return [w for w, _ in sorted(cnt.items(), key=lambda kv: (-kv[1], -len(kv[0])))][:3]


def _verdict(pos: int, neg: int, neu: int) -> tuple[str, str]:
    """(票数, 方向) → (结论, 置信度)。样本不够就老老实实说不够。"""
    n = pos + neg
    if n == 0:
        return "中立（没明确观点）", f"{neu} 帖只是顺口提到"
    if n < 2:
        who = "看多" if pos > neg else "看空"
        return (f"{who}（{n} 人明确表态）" if n == 1 else f"{who}（{n} 人表态）"), \
               f"样本太少，另有 {neu} 帖中立"
    ratio = max(pos, neg) / max(min(pos, neg), 1)
    if ratio >= 2.0:
        return ("看涨" if pos > neg else "看跌"), "较一致"
    if ratio >= 1.5:
        return ("偏多" if pos > neg else "偏空"), "有分歧"
    return "分歧（多空都有人）", "两边对打"


def _key_of(verdict: str) -> str:
    """结论文字 → 前端配色档位（bull/bear/neu 三档）。

    前后端共用同一份映射：_mini 与板块汇总两处都要用，早先各写一遍，
    改口径时容易只改一处导致「徽章颜色和结论文字对不上」。"""
    if verdict.startswith(("看涨", "偏多", "看多")):
        return "bull"
    if verdict.startswith(("看跌", "偏空", "看空")):
        return "bear"
    return "diverge"


def _mini(items: list[dict], hours: int) -> list[dict]:
    """把一个聚类（个股或行业）汇总成前端直接能渲染的结论条目。"""
    out = []
    for it in items:
        v, conf = _verdict(it["pos"], it["neg"], it["neu"])
        if v == "分歧（多空都有人）":
            conf = "两边对打"
        _bull = [i["text"] for i in it.get("items", []) if i.get("label") == "看多"]
        _bear = [i["text"] for i in it.get("items", []) if i.get("label") == "看空"]
        _why = _reasons(_bull, "看多") if it["pos"] >= it["neg"] else _reasons(_bear, "看空")
        out.append({
            "key": it["key"], "name": it["name"], "board": it["board"],
            "posts": it["posts"], "pos": it["pos"], "neg": it["neg"], "neu": it["neu"],
            "net": round(it["net"], 2),             "verdict": v,
            "verdict_key": _key_of(v),
            "confidence": conf,
            "reasons": _why,
            "summary": it["summary"],
            "quotes": it["quotes"][:2],
        })
    out.sort(key=lambda x: (-x["posts"], -abs(x["net"])))
    return out


def _name_cache() -> dict[str, str]:
    """代码 → 常用名。**每个查询各自容错**：
    2026-10-02 踩坑：把三条 SELECT 包在一个 try 里，其中 lhb_stocks 表字段对不上抛错，
    结果整个缓存被清成空表，页面上一律显示 "300750""600519" 这种裸代码。
    宁可某一路缺，也不能让一条 SQL 把整张表带塌。"""
    global _CACHE_NAME
    if _CACHE_NAME is not None:
        return _CACHE_NAME
    m: dict[str, str] = {}

    def _q(sql: str) -> None:
        try:
            con = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
            for r in con.execute(sql):
                k = (r[0] or "").strip()
                v = (r[1] or "").strip()
                if k and v:
                    m.setdefault(k, v)
            con.close()
        except Exception:  # noqa: BLE001 —— 表不存在/字段变化时静默跳过
            pass

    # ① 抓取时存下的提及名（最贴近论坛里大家用的叫法）② 名称缓存表 ③ 龙虎榜简称
    _q("SELECT symbol, symbol_name FROM stock_mentions WHERE symbol_name IS NOT NULL AND symbol_name<>''")
    _q("SELECT code, name FROM stock_names WHERE name IS NOT NULL AND name<>''")
    try:
        _q("SELECT security_code, security_name FROM lhb_stocks "
           "WHERE security_name IS NOT NULL AND security_name<>''")
    except Exception:  # noqa: BLE001
        pass
    _CACHE_NAME = m
    return m


def _fallback_name(code: str) -> str:
    return _name_cache().get(code, "")


def _industry_map() -> dict[str, str]:
    """code → 所属行业（取 stock_org.industry 的一级，如「汽车 - 乘用车 - …」→「汽车」）。"""
    global _CACHE_IND
    if _CACHE_IND is not None:
        return _CACHE_IND
    _CACHE_IND = {}
    try:
        con = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
        for r in con.execute("SELECT code, industry FROM stock_org "
                             "WHERE industry IS NOT NULL AND industry<>''"):
            code = (r[0] or "").strip()
            parts = [p for p in str(r[1]).split("-") if p.strip()]
            if code and parts:
                _CACHE_IND.setdefault(code, parts[0].strip())
        con.close()
    except Exception:  # noqa: BLE001
        _CACHE_IND = {}
    return _CACHE_IND


def _build_core(hours: int = 24, max_posts: int = 800) -> dict:
    """最近 hours 小时内的帖子 → 情绪结论（真实计算路径，不含缓存）。

    原 build() 全部逻辑下沉到这里；缓存壳见下方 build() 包装函数。
    本函数内部逻辑、try/except 降级、返回字段结构一律不变。"""
    now = datetime.now()
    cutoff = (now - timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M")
    out: dict = {
        "generated_at": now.strftime("%Y-%m-%d %H:%M"),
        "hours": hours,
        "ok": True,
    }

    con = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        rows = con.execute(
            "SELECT id, site, author, post_time, text FROM posts "
            "ORDER BY post_time DESC LIMIT ?", (max_posts,)).fetchall()
    finally:
        con.close()

    posts_in = []
    for r in rows:
        pt = (r["post_time"] or "").strip()
        if pt and pt < cutoff:      # 字典序即时间序；空时间串不参与（先留不算）
            continue
        if not pt:
            continue
        txt = (r["text"] or "").strip()
        if not txt:
            continue
        posts_in.append({"id": r["id"], "site": r["site"] or "", "author": r["author"] or "匿名",
                         "post_time": pt, "text": txt})

    out["scope"] = {"posts_total": len(posts_in), "posts_tagged": 0, "window_hours": hours,
                    "from": cutoff, "to": now.strftime("%Y-%m-%d %H:%M")}

    # ---------- 逐帖打标 + 抽标的 ----------
    stocks: dict[str, dict] = {}
    # 泛谈（没点名个股）单独一桶：它的 pos/neg/neu **只统计无个股指向的帖**，
    # 不能拿全库计数来凑，否则「另有 24 帖泛谈，看多 3」这种自相矛盾的账会印在页面上。
    general = {"posts": 0, "pos": 0, "neg": 0, "neu": 0, "net": 0.0, "samples": []}
    posts_with_stock = 0

    try:
        from viewer import analytics
        extract = analytics.extract_codes
    except Exception:  # noqa: BLE001
        extract = lambda t: []  # noqa: E731

    name_re = None
    try:
        from viewer import analytics as _an
        name_re = _an._name_re()
        idx = _an._name_index()
    except Exception:  # noqa: BLE001
        idx = {}

    ind_map = _industry_map()

    for p in posts_in:
        sc = _score(p["text"])
        lab = _label(sc)
        p["score"], p["label"] = sc, lab

        codes = []
        try:
            codes = [c for c in (extract(p["text"]) or [])]
        except Exception:  # noqa: BLE001
            codes = []
        if not codes and name_re is not None:
            for nm in set(name_re.findall(p["text"] or "")):
                c = idx.get(nm)
                if c:
                    codes.append(c)
        codes = list(dict.fromkeys(codes))
        if not codes:
            # 无个股指向 → 才计入泛谈桶的情绪
            general["posts"] += 1
            if lab == "看多":
                general["pos"] += 1
                general["net"] += sc
            elif lab == "看空":
                general["neg"] += 1
                general["net"] += sc
            else:
                general["neu"] += 1
            if len(general["samples"]) < 12:
                general["samples"].append({"author": p["author"], "text": p["text"][:60],
                                           "label": lab})
            continue
        posts_with_stock += 1

        for c in codes:
            d = stocks.setdefault(c, {"key": c, "name": _fallback_name(c), "board": ind_map.get(c, ""),
                                      "posts": 0, "pos": 0, "neg": 0, "neu": 0, "net": 0.0,
                                      "items": [], "samples": []})
            d["posts"] += 1
            d["items"].append({**p})
            if lab == "看多":
                d["pos"] += 1
                d["net"] += sc
            elif lab == "看空":
                d["neg"] += 1
                d["net"] += sc
            else:
                d["neu"] += 1
                if len(d["samples"]) < 3:
                    d["samples"].append({"author": p["author"], "text": p["text"][:60], "label": lab})

    # ---------- 个股结论 ----------
    stock_items = []
    for c, d in stocks.items():
        v, conf = _verdict(d["pos"], d["neg"], d["neu"])
        d["_verdict"] = v
        d["_conf"] = conf
        if d["pos"] == 0 and d["neg"] == 0:
            conf = "只是被顺口提到"
        bull_txts = [i["text"] for i in d["items"] if i["label"] == "看多"]
        bear_txts = [i["text"] for i in d["items"] if i["label"] == "看空"]
        reasons = _reasons(bull_txts, "看多") if d["pos"] >= d["neg"] else _reasons(bear_txts, "看空")
        # 代表原话：取该标的里分值最高 / 最低各一条（最典型的多空声音）
        ordered = sorted(d["items"], key=lambda x: -abs(x["score"]))
        quotes = [{"author": i["author"], "time": i["post_time"][:16], "label": i["label"],
                   "text": i["text"][:46]} for i in ordered[:2]]
        d["summary"] = (f"近{hours}小时有 {d['posts']} 帖点名"
                        + (f"{d['name'] or d['key']}（{d['key']}）" if d["name"] else f"{d['key']}")
                        + (f"，属{d['board']}，" if d["board"] else "，")
                        + f"看多 {d['pos']}、看空 {d['neg']}、中立 {d['neu']} → 结论「{v}」"
                          + (f"（{conf}）" if conf != "—" else ""))
        if reasons:
            d["summary"] += f"；在说的理由：{'、'.join(reasons)}"
        stock_items.append({**d, "quotes": quotes})

    out["stocks"] = _mini(stock_items, hours)

    # ---------- 行业（板块）结论 ----------
    boards: dict[str, dict] = {}
    for it in stock_items:
        b = it["board"] or "其他"
        bd = boards.setdefault(b, {"key": b, "name": b, "board": b, "posts": 0, "pos": 0, "neg": 0,
                                   "neu": 0, "net": 0.0, "items": [], "samples": []})
        bd["posts"] += it["posts"]
        bd["pos"] += it["pos"]
        bd["neg"] += it["neg"]
        bd["neu"] += it["neu"]
        bd["net"] += it["net"]
        bd["items"].append({"name": it.get("name"), "key": it.get("key"), "posts": it["posts"],
                            "pos": it["pos"], "neg": it["neg"], "net": it.get("net", 0),
                            "verdict": it.get("_verdict", "")})
    board_items = []
    for b, bd in boards.items():
        v, conf = _verdict(bd["pos"], bd["neg"], bd["neu"])
        bd["summary"] = (f"「{b}」近{hours}小时被 {bd['posts']} 帖提到"
                         + f"，看多 {bd['pos']}、看空 {bd['neg']}、中立 {bd['neu']} → 结论「{v}」（{conf}）")
        if bd["items"]:
            top = sorted(bd["items"], key=lambda x: -x["posts"])[:3]
            bd["summary"] += "；被点名最多：" + "、".join(
                f"{x.get('name') or x['key']}({x['posts']}帖{'看多' if x['pos'] > x['neg'] else '看空' if x['neg'] > x['pos'] else '中立'})"
                for x in top)
        board_items.append({**bd, "quotes": []})
    for b in board_items:
        _v, _c = _verdict(b["pos"], b["neg"], b["neu"])
        b["verdict"] = _v
        b["verdict_key"] = _key_of(_v)
    board_items.sort(key=lambda x: -x["posts"])
    # 只要真正成规模的板块：>=2 帖，或该板块内部有多只票被点名
    out["boards"] = [b for b in board_items if b["posts"] >= 2 or len(b["items"]) >= 2]

    # ---------- 大盘情绪 + 一句总论 ----------
    total_dir = general["pos"] + general["neg"]
    if total_dir == 0:
        mood = "看不出方向"
    else:
        rp = general["pos"] / total_dir
        if rp >= 0.75:
            mood = "明显偏多"
        elif rp >= 0.6:
            mood = "偏多"
        elif rp > 0.42:
            mood = "多空分歧"
        elif rp > 0.25:
            mood = "偏空"
        else:
            mood = "明显偏空"

    n_bull = sum(1 for s in out["stocks"] if s["verdict_key"] == "bull")
    n_bear = sum(1 for s in out["stocks"] if s["verdict_key"] == "bear")
    n_div = sum(1 for s in out["stocks"] if s["verdict_key"] == "diverge")
    top = out["stocks"][0] if out["stocks"] else None

    verdict_text = (f"最近 {hours} 小时共 {len(posts_in)} 帖，其中 {posts_with_stock} 帖点名了个股"
                    f"（一帖常提多只，所以下面票数会比帖数多）；"
                    f"按语气算，看多 {general['pos']}、看空 {general['neg']}、"
                    f"中立 {general['neu']}（中立多是闲聊/转载，不算情绪）。整体情绪：{mood}。")
    if general["pos"] + general["neg"] < 5:
        verdict_text += " 明牌的多空观点不多，别把这个比例当强信号——它更多说明「大家在聊，但没敢下注」。"
    # 文案里不塞 HTML（前端统一转义），mood 单独给一个字段由前端加粗
    if top:
        verdict_text += (f"被讨论最多的是 {top['name'] or top['key']}（{top['posts']} 帖，{top['verdict']}，"
                         f"净分 {top['net']:+}）。")
    if out["boards"]:
        b = out["boards"][0]
        verdict_text += f"板块上，「{b['name']}」最热（{b['posts']} 帖，{b['verdict']}）。"
    if n_bear or n_div:
        verdict_text += f"点名的 {len(out['stocks'])} 只票里：{n_bull} 只偏多/{n_bear} 只偏空/{n_div} 只分歧。"
    if not out["stocks"]:
        verdict_text += "⚠️ 窗内没有帖子点名个股，只有泛淡行情的闲聊——不代表个股情绪。"

    out["verdict"] = mood
    out["verdict_mood_class"] = ("bull" if mood.startswith(("偏多", "明显偏多"))
                                 else "bear" if mood.startswith(("偏空", "明显偏空")) else "diverge")
    out["verdict_text"] = verdict_text
    out["counts"] = {"posts_with_stock": posts_with_stock,
                     "stocks": len(out["stocks"]), "bull": n_bull, "bear": n_bear, "diverge": n_div,
                     "bull_posts": general["pos"], "bear_posts": general["neg"],
                     "neutral_posts": general["neu"]}
    out["scope"]["posts_tagged"] = posts_with_stock
    out["general"] = {"posts": general["posts"], "label": "无个股指向的泛谈",
                      "summary": (f"另有 {general['posts']} 帖只谈大盘/行情/操作、没点名具体票，"
                                  f"这批同样算情绪：看多 {general['pos']}、看空 {general['neg']}、中立 {general['neu']}。"),
                      "samples": general["samples"]}
    out["basis"] = (f"口径：取最近 {hours} 小时 posts 表正文；"
                    f"与「入库 sentiment / 每日预测·论坛情绪信号」是**同一把尺子**"
                    f"（词表与打分都在 viewer/analytics.py，改一处全站一起变）；"
                    f"加权打分（强词 ±1、弱词 ±0.5、含强度词 ×1.5），阈值 ±0.5 判方向；"
                    f"只认双字以上词，先剔除「跌不动/不跌/跌透了」这类否定式再打分；"
                    f"未联网、不推测，样本不足会显式标注。")
    return out


_REBUILDING = set()          # 正在后台重建的 key（防同一 key 起多个线程）
_REBUILD_LOCK = threading.Lock()


def _spawn_rebuild(key, hours: int, max_posts: int) -> None:
    """后台线程重算（过期时用，让请求线程立刻返回旧值）。失败只记日志，不影响任何返回。"""
    with _REBUILD_LOCK:
        if key in _REBUILDING:
            return
        _REBUILDING.add(key)

    def _run():
        try:
            res = _build_core(hours, max_posts)
            with _BUILD_CACHE_LOCK:
                _BUILD_CACHE[key] = (time.time(), res)
        except Exception:  # noqa: BLE001
            logging.getLogger(__name__).exception("postdigest 后台重算失败（继续用旧值）")
        finally:
            with _REBUILD_LOCK:
                _REBUILDING.discard(key)

    try:
        threading.Thread(target=_run, name="postdigest-rebuild", daemon=True).start()
    except Exception:  # noqa: BLE001
        with _REBUILD_LOCK:
            _REBUILDING.discard(key)


def build(hours: int = 24, max_posts: int = 800) -> dict:
    """最近 hours 小时内的帖子 → 情绪结论（带 120 秒 TTL 的进程级缓存）。

    公开入口，server.py 的 api_stocks() / api_today_decision() 都只调它。
    两层保护：
      1. 命中且未过期 → 直接返回缓存结果的深拷贝，毫秒级返回（原接口本应秒回）；
      2. 未命中或过期 → 照常走 _build_core() 重算，写回缓存后再返回深拷贝。
    始终返回深拷贝：多线程拿到的都是独立副本，任何调用方修改返回对象都不会
    污染缓存里那份，避免多线程相互踩踏。缓存期内 generated_at 可能是至多 2 分钟前
    的时刻（粗粒度情绪速览，误差可接受，不做特殊处理）。
    """
    key = (hours, max_posts)
    now_ts = time.time()
    entry = _BUILD_CACHE.get(key)
    # ── 2026-10-29 SWR（stale-while-revalidate）：过期**不阻塞请求** ────────────
    # 原逻辑过期后在请求线程里现算 6~8s，而 /api/stocks、/api/summary、/api/today_decision
    # 都要它 —— 每 2 分钟就有一个"倒霉的请求"替全站背这 6~8 秒，用户就是在这时候点开
    # 面板看到转圈的。现在：只要曾经算过，就**立刻返回旧值**，同时在后台线程刷新一份，
    # 下一个请求自然拿到新值。情绪速览本来就是 72 小时窗口的粗粒度结论，晚 1 分钟无差别。
    if entry is not None:
        stored_ts, result = entry
        if (now_ts - stored_ts) < _BUILD_CACHE_TTL:
            return copy.deepcopy(result)
        _spawn_rebuild(key, hours, max_posts)   # 过期：先给旧值，后台补新值
        return copy.deepcopy(result)
    with _BUILD_CACHE_LOCK:  # 冷启动（从没算过）：锁内算一次，杜绝并发同时重算（惊群）
        entry = _BUILD_CACHE.get(key)
        if entry is not None:
            return copy.deepcopy(entry[1])
        result = _build_core(hours, max_posts)
        _BUILD_CACHE[key] = (now_ts, result)
        # 写回的也是深拷贝给调用方；缓存里留原件，下次命中再拷贝
        return copy.deepcopy(result)