# -*- coding: utf-8 -*-
"""douyin 域 API 处理函数（从 viewer/server.py 物理抽取，2026-11-07 拆分重构）。

抽取规则：函数体逻辑零改动；依赖的 server 模块级符号经 `from viewer.server import (...)`
重定向进本地命名空间；标准库本地导入。server.py 末尾 re-export 保持命名空间兼容，
routes.py 与既有测试（S.api_*）无需改动。
"""
from __future__ import annotations

import json
from viewer.server import (  # noqa: E402,F401
    DB,
    KPARAMS,
    _DOUYIN_BRIEF_LEN,
    _DOUYIN_LABEL,
    _DOUYIN_SORTS,
    _STANCE_DESC,
    _TRACK_DESC,
    _douyin_meta,
    _douyin_ready,
)

def api_douyin_facets(domain: str = "tech") -> dict:
    """某个域的面板头部数据：数据截至 / 规模 / 博主目录 / 分类分布 / 日期跨度。

    `bloggers` 以**视频表实际作者**为准并集档案表：豆包产出里存在「有视频、无档案」的
    作者（实测 sentiment 的 BRTV天下财经、tech 的 斌哥Bg），若只按档案表出目录，
    这些人会在筛选 chip 里凭空消失、他们的视频再也筛不出来。
    """
    domain = domain if domain in _DOUYIN_LABEL else "tech"
    out = {"ok": True, "domain": domain, "label": _DOUYIN_LABEL[domain],
           "src_updated": None, "n_blogger": 0, "n_video": 0,
           "bloggers": [], "categories": [], "date_range": [None, None],
           "no_date": 0, "with_summary": 0, "note": ""}
    if not _douyin_ready():
        out["ok"] = False
        out["note"] = "抖音语料表尚未建立：请先执行 python tools/import_douyin_corpus.py"
        return out

    m = _douyin_meta(domain)
    out["src_updated"] = m.get("src_updated")
    out["imported_at"] = m.get("imported_at")

    prof = {}
    for r in DB.conn.execute(
            "SELECT name, douyin_id, followers, intro, tags_json, n_video"
            " FROM douyin_corpus_blogger WHERE domain = ?", (domain,)):
        prof[r["name"]] = dict(r)

    agg = {}
    for r in DB.conn.execute(
            "SELECT author, COUNT(*) AS n, MAX(publish_date) AS last_date"
            " FROM douyin_corpus_video WHERE domain = ? GROUP BY author", (domain,)):
        agg[r["author"]] = {"n": r["n"], "last_date": r["last_date"]}

    bloggers = []
    for name in set(agg) | set(prof):
        p = prof.get(name) or {}
        a = agg.get(name) or {}
        tags = []
        if p.get("tags_json"):
            try:
                tags = json.loads(p["tags_json"]) or []
            except Exception:  # noqa: BLE001
                tags = []
        bloggers.append({
            "name": name,
            "n": a.get("n", 0),
            "last_date": a.get("last_date"),
            "pending": a.get("n", 0) == 0,
            "douyin_id": p.get("douyin_id"),
            "followers": p.get("followers"),
            "intro": p.get("intro"),
            "tags": tags,
        })
    bloggers.sort(key=lambda b: (b["n"] == 0, -b["n"], b["name"]))
    out["bloggers"] = bloggers

    out["categories"] = [
        {"name": r["cat"] or "未分类", "n": r["n"]}
        for r in DB.conn.execute(
            "SELECT COALESCE(NULLIF(category,''),'未分类') AS cat, COUNT(*) AS n"
            " FROM douyin_corpus_video WHERE domain = ? GROUP BY cat ORDER BY n DESC", (domain,))]
    out["categories"] = [c for c in out["categories"] if c["name"] != "未分类"] + \
                        [c for c in out["categories"] if c["name"] == "未分类"]

    r = DB.conn.execute(
        "SELECT COUNT(*) AS n, MIN(publish_date) AS dmin, MAX(publish_date) AS dmax,"
        " SUM(CASE WHEN publish_date IS NULL THEN 1 ELSE 0 END) AS nod,"
        " SUM(CASE WHEN summary IS NOT NULL AND summary <> '' THEN 1 ELSE 0 END) AS ws"
        " FROM douyin_corpus_video WHERE domain = ?", (domain,)).fetchone()
    if r:
        out["n_video"] = r["n"] or 0
        out["date_range"] = [r["dmin"], r["dmax"]]
        out["no_date"] = r["nod"] or 0
        out["with_summary"] = r["ws"] or 0
    out["n_blogger"] = len([b for b in bloggers if b["n"] > 0])
    return out
def api_douyin_list(domain: str = "tech", q: str | None = None, blogger: str | None = None,
                    category: str | None = None, topic: str | None = None,
                    stance: str | None = None, offset: int = 0, limit: int = 30,
                    sort: str = "date") -> dict:
    """视频语料分页列表（后端分页 —— tech 域 2701 条绝不能一次性塞给浏览器）。

    列表**不返回完整摘要**（单条最长数千字，2701 条全量可达数 MB），只回
    `summary_brief`（前 180 字）与 `has_summary`；点开单条再走 /api/douyin/detail 取全文。
    """
    domain = domain if domain in _DOUYIN_LABEL else "tech"
    out = {"ok": True, "domain": domain, "label": _DOUYIN_LABEL[domain],
           "total": 0, "offset": 0, "limit": limit, "has_more": False, "rows": [], "note": ""}
    if not _douyin_ready():
        out["ok"] = False
        out["note"] = "抖音语料表尚未建立：请先执行 python tools/import_douyin_corpus.py"
        return out

    where, args = ["domain = ?"], [domain]
    if blogger:
        where.append("author = ?")
        args.append(blogger)
    if category:
        if category in ("__none__", "未分类"):
            where.append("(category IS NULL OR category = '')")
        else:
            where.append("category = ?")
            args.append(category)
    if topic:
        where.append("topic = ?")
        args.append(topic)
    if stance:
        where.append("stance = ?")
        args.append(stance)
    if q:
        kw = "%" + q.strip() + "%"
        where.append("(title LIKE ? OR summary LIKE ? OR author LIKE ?)")
        args += [kw, kw, kw]
    wsql = " WHERE " + " AND ".join(where)

    tot = DB.conn.execute("SELECT COUNT(*) AS n FROM douyin_corpus_video" + wsql, args).fetchone()
    total = tot["n"] if tot else 0
    lim = max(1, min(int(limit or 30), 200))
    off = max(0, int(offset or 0))
    order = _DOUYIN_SORTS.get(sort, _DOUYIN_SORTS["date"])
    rows = DB.conn.execute(
        "SELECT video_id, title, publish_date, author, video_url, category, summary,"
        " source_of_summary FROM douyin_corpus_video" + wsql +
        " ORDER BY " + order + " LIMIT ? OFFSET ?", args + [lim, off]).fetchall()

    items = []
    for r in rows:
        s = r["summary"] or ""
        items.append({
            "video_id": r["video_id"],
            "title": r["title"] or "",
            "publish_date": r["publish_date"],
            "author": r["author"],
            "video_url": r["video_url"],
            "category": r["category"],
            "summary_brief": (s[:_DOUYIN_BRIEF_LEN] + "…") if len(s) > _DOUYIN_BRIEF_LEN else s,
            "summary_len": len(s),
            "has_summary": bool(s),
            "source_of_summary": r["source_of_summary"],
        })
    out.update({"total": total, "offset": off, "limit": lim, "rows": items,
                "has_more": off + len(items) < total})
    return out
def api_douyin_detail(video_id: str) -> dict:
    """单条视频完整摘要（列表只给 brief，点开才取全文）。"""
    out = {"ok": False, "video_id": video_id, "row": None, "note": ""}
    if not video_id:
        out["note"] = "缺少 video_id"
        return out
    if not _douyin_ready():
        out["note"] = "抖音语料表尚未建立"
        return out
    r = DB.conn.execute(
        "SELECT video_id, domain, title, publish_date, author, video_url, category,"
        " summary, source_of_summary, src_updated FROM douyin_corpus_video WHERE video_id = ?",
        (video_id,)).fetchone()
    if not r:
        out["note"] = "未找到该视频"
        return out
    out["ok"] = True
    out["row"] = dict(r)
    out["label"] = _DOUYIN_LABEL.get(r["domain"], r["domain"])
    return out
def api_douyin_overview() -> dict:
    """两域概览（供前端首屏/面板头部一次拿全，避免两次请求）。"""
    out = {"ok": _douyin_ready(), "domains": {}}
    for d in ("sentiment", "tech"):
        f = api_douyin_facets(d)
        out["domains"][d] = {
            "label": f["label"], "src_updated": f.get("src_updated"),
            "n_blogger": f.get("n_blogger", 0), "n_video": f.get("n_video", 0),
            "no_date": f.get("no_date", 0), "with_summary": f.get("with_summary", 0),
            "date_range": f.get("date_range", [None, None]),
        }
    if not out["ok"]:
        out["note"] = "抖音语料表尚未建立：请先执行 python tools/import_douyin_corpus.py"
    return out
def api_douyin_topics() -> dict:
    """技术域 → 9 条学习方法论轨道聚合（视频数/代表博主/样例）。"""
    out = {"ok": _douyin_ready(), "tracks": []}
    if not out["ok"]:
        out["note"] = "抖音语料表尚未建立：请先执行 python tools/import_douyin_corpus.py"
        return out
    agg = {}
    for r in DB.conn.execute(
        "SELECT topic, author, title FROM douyin_corpus_video WHERE domain='tech' AND topic IS NOT NULL"):
        t, a, ti = r[0], r[1], r[2] or ""
        d = agg.setdefault(t, {"n": 0, "bloggers": {}, "titles": []})
        d["n"] += 1
        d["bloggers"][a] = d["bloggers"].get(a, 0) + 1
        if len(d["titles"]) < 4:
            d["titles"].append(ti[:40])
    tracks = []
    for t, d in sorted(agg.items(), key=lambda x: -x[1]["n"]):
        top = sorted(d["bloggers"].items(), key=lambda x: -x[1])[:5]
        tracks.append({"track": t, "desc": _TRACK_DESC.get(t, ""), "n": d["n"],
                       "top_bloggers": [{"name": b, "n": n} for b, n in top],
                       "samples": d["titles"]})
    out["tracks"] = tracks
    return out
def api_douyin_stances() -> dict:
    """情绪域 → 5 类观点类型聚合（数/样例）。"""
    out = {"ok": _douyin_ready(), "stances": []}
    if not out["ok"]:
        out["note"] = "抖音语料表尚未建立：请先执行 python tools/import_douyin_corpus.py"
        return out
    agg = {}
    for r in DB.conn.execute(
        "SELECT stance, author, title FROM douyin_corpus_video WHERE domain='sentiment' AND stance IS NOT NULL"):
        st, a, ti = r[0], r[1], r[2] or ""
        d = agg.setdefault(st, {"n": 0, "items": []})
        d["n"] += 1
        if len(d["items"]) < 6:
            d["items"].append({"blogger": a, "title": ti[:40]})
    out["stances"] = [{"stance": st, "desc": _STANCE_DESC.get(st, ""), "n": d["n"], "samples": d["items"]}
                      for st, d in sorted(agg.items(), key=lambda x: -x[1]["n"])]
    return out
def api_douyin_bloggers() -> dict:
    """博主分域画像：技术派 / 观点派两套群像，各含 tags + 主题(观点)分布。"""
    out = {"ok": _douyin_ready(), "tech": [], "sentiment": []}
    if not out["ok"]:
        out["note"] = "抖音语料表尚未建立：请先执行 python tools/import_douyin_corpus.py"
        return out
    for dom, key, dist_col in (("tech", "tech", "topic"), ("sentiment", "sentiment", "stance")):
        bloggers = {}
        for r in DB.conn.execute(
            "SELECT name, douyin_id, followers, intro, tags_json, n_video"
            " FROM douyin_corpus_blogger WHERE domain=?", (dom,)):
            name = r[0]
            bloggers[name] = {"name": name, "douyin_id": r[1], "followers": r[2], "intro": r[3] or "",
                              "tags": json.loads(r[4]) if r[4] else [], "n_video": r[5] or 0, "dist": {}}
        for r in DB.conn.execute(
            f"SELECT author, {dist_col}, COUNT(*) FROM douyin_corpus_video"
            f" WHERE domain=? AND {dist_col} IS NOT NULL GROUP BY author, {dist_col}", (dom,)):
            if r[0] in bloggers:
                bloggers[r[0]]["dist"][r[1]] = r[2]
        out[key] = sorted(bloggers.values(), key=lambda x: -x["n_video"])
    return out
def api_douyin_plate(plate: str = "") -> dict:
    """板块 → 个股结论（2026-10-25 重构：进哥要「结论」不要「罗列」）。

    在原有「方法轨道 / 舆情观点 / 博主」计数之上，增加**个股层**：从本板块语料
    (标题+摘要)抽取点名个股，关联 res_stock_card(合成研究卡) / sig_unified(六源信号方向) /
    豆包 stance，逐股给出「看涨/看跌/中性 + 力度 + 理由」，
    并在板块层给一句话结论。方法轨道/舆情分布退居「依据」，由前端折叠展示。
    """
    import time
    out = {"ok": bool(plate), "plate": plate, "tech": None, "sentiment": None,
           "bloggers": [], "stocks": [], "summary": "",
           "n_stocks": 0, "n_bull": 0, "n_bear": 0, "n_neutral": 0}
    if not plate:
        out["note"] = "缺少 plate 参数"
        return out
    now = time.time()
    hit = _plate_concl_cache.get(plate)
    if hit and now - hit[0] < 300:
        d = dict(hit[1])
        d["cached"] = True
        return d

    vids = [r[0] for r in DB.conn.execute(
        "SELECT video_id FROM douyin_corpus_plate WHERE plate=?", (plate,)).fetchall()]
    if not vids:
        out["note"] = "板块「%s」暂无关联语料" % plate
        return out
    ph = ",".join("?" * len(vids))

    # ---------- 依据层：方法轨道 / 舆情观点 / 博主 ----------
    tech = {"n": 0, "tracks": {}, "bloggers": {}, "samples": []}
    sent = {"n": 0, "stances": {}, "bloggers": {}, "samples": []}
    pv = []
    for r in DB.conn.execute(
            "SELECT domain, author, title, topic, stance, summary FROM douyin_corpus_video"
            " WHERE video_id IN (%s)" % ph, vids):
        dom, a, ti, tp, st, sm = r[0], r[1], (r[2] or ""), r[3], r[4], (r[5] or "")
        pv.append((dom, a, ti, st, sm))
        if dom == "tech":
            tech["n"] += 1
            tech["tracks"][tp] = tech["tracks"].get(tp, 0) + 1
            tech["bloggers"][a] = tech["bloggers"].get(a, 0) + 1
            if len(tech["samples"]) < 6:
                tech["samples"].append({"blogger": a, "title": ti[:40]})
        else:
            sent["n"] += 1
            sent["stances"][st] = sent["stances"].get(st, 0) + 1
            sent["bloggers"][a] = sent["bloggers"].get(a, 0) + 1
            if len(sent["samples"]) < 6:
                sent["samples"].append({"blogger": a, "title": ti[:40]})
    tech["tracks"] = sorted(tech["tracks"].items(), key=lambda x: -x[1])
    sent["stances"] = sorted([(k, v) for k, v in sent["stances"].items() if k],
                             key=lambda x: -x[1])
    out["tech"] = tech
    out["sentiment"] = sent
    out["bloggers"] = sorted(set(list(tech["bloggers"].keys()) + list(sent["bloggers"].keys())))

    # ---------- 个股层：从本板块语料抽点名个股 ----------
    name2code = {}
    for r in DB.conn.execute(
            "SELECT code, name FROM res_stock WHERE name IS NOT NULL AND length(name)>=3"):
        name2code.setdefault(r[1], r[0])
    code2name = {}
    for nm, cd in name2code.items():
        code2name.setdefault(cd, nm)
    names = sorted(name2code.keys(), key=len, reverse=True)
    hits = {}
    for dom, a, ti, st, sm in pv:
        text = (ti or "") + " " + (sm or "")
        found = set(nm for nm in names if nm in text)
        found = set(n for n in found if not any(n != m and n in m for m in found))
        for nm in found:
            cd = name2code[nm]
            h = hits.setdefault(cd, {"n": 0, "stances": set(), "authors": set(), "samples": []})
            h["n"] += 1
            if st:
                h["stances"].add(st)
            if a:
                h["authors"].add(a)
            if len(h["samples"]) < 2 and sm:
                h["samples"].append({"author": a or "", "text": sm[:80]})

    card = {}
    for r in DB.conn.execute("SELECT code, tier, res_score, verdict FROM res_stock_card"):
        card[r[0]] = (r[1], r[2], r[3] or "")
    # 2026-10-30：博主共振已下线（实证反向 + 源停更），此处不再读 blogger_overlap。
    sig = {}
    for r in DB.conn.execute(
            "SELECT code, direction, COUNT(*) FROM sig_unified GROUP BY code, direction"):
        sig.setdefault(r[0], {})[r[1]] = r[2]

    stocks = []
    for cd, h in hits.items():
        nm = code2name.get(cd) or cd
        t, rs, cv = card.get(cd, (None, None, ""))
        s = sig.get(cd, {})
        n_mul = s.get("多", 0)
        n_short = s.get("空", 0)
        tier_w = {"S": 4, "A": 3, "B": 2, "C": 1}.get(t, 0)
        sts = sorted(h["stances"])
        bull_st = [x for x in sts if x in ("看多", "板块机会")]
        bear_st = [x for x in sts if x in ("看空", "风险提示")]
        conf = 0.0
        risk = 0.0
        reasons = []
        if n_mul:
            conf += min(n_mul, KPARAMS.CARD_SRC_CAP) * KPARAMS.CARD_SCORE_W["multi"]
            reasons.append("六源信号 %d 路偏多" % n_mul)
        if n_short:
            risk += min(n_short, KPARAMS.CARD_SRC_CAP) * KPARAMS.CARD_SCORE_W["short"]
            reasons.append("六源信号 %d 路偏空" % n_short)
        if tier_w:
            conf += tier_w * KPARAMS.CARD_SCORE_W["tier"]
            reasons.append("合成卡 %s 档(%s)" % (t, rs))
        if bull_st:
            conf += KPARAMS.CARD_SCORE_W["bull_tag"]
            reasons.append("豆包标注「%s」" % "/".join(bull_st))
        if bear_st:
            risk += KPARAMS.CARD_SCORE_W["bear_tag"]
            reasons.append("豆包标注「%s」" % "/".join(bear_st))
        conf += min(h["n"], KPARAMS.CARD_SAMPLE_CAP) * KPARAMS.CARD_SCORE_W["sample"]
        if n_mul and n_short:
            risk += KPARAMS.CARD_SCORE_W["both_dir"]
            reasons.append("多空信号并存，方向不纯")
        if not t and not s:
            reasons.append("仅豆包提及，尚无合成层/信号印证")
        conf = min(conf, KPARAMS.CARD_SCORE_CAP)
        risk = min(risk, KPARAMS.CARD_SCORE_CAP)
        net = conf - risk
        if net >= KPARAMS.CARD_DIR_NEUTRAL_NET and n_mul >= n_short:
            d = "看涨"
        elif net <= -KPARAMS.CARD_DIR_NEUTRAL_NET or (n_short > n_mul and risk >= 14):
            d = "看跌"
        else:
            d = "中性"
        strength = ("强" if abs(net) >= KPARAMS.CARD_STRONG_NET
                    else ("中" if abs(net) >= KPARAMS.CARD_MID_NET else "弱"))
        score = int(round(50 + net * 0.5))
        score = max(0, min(100, score))
        stocks.append({
            "code": cd, "name": nm, "dir": d, "strength": strength,
            "score": score, "conf": round(conf, 1), "risk": round(risk, 1),
            "tier": t or "", "card_score": rs, "card_note": (cv or "")[:90],
            "douyin_n": h["n"],
            "sig_mul": n_mul, "sig_short": n_short,
            "stances": sts, "bloggers": sorted(h["authors"])[:5],
            "reasons": reasons, "samples": h["samples"],
            # 2026-10-31 整改（测评 P1「结论卡证据强度」项）：强度标签必须自带来历。
            # 之前这三档只是一个 abs(net) 的切分，界面上"强"看起来像结论，
            # 实际是 6 个拍定系数的差。现在把"它是怎么来的""系数没有来源"直接放在卡里。
            "strength_basis": KPARAMS.CARD_STRENGTH_BASIS,
            "weights_are_arbitrary": KPARAMS.CARD_WEIGHTS_ARE_ARBITRARY,
            "net": round(net, 2),
        })

    bulls = sorted([x for x in stocks if x["dir"] == "看涨"], key=lambda x: -x["score"])
    bears = sorted([x for x in stocks if x["dir"] == "看跌"], key=lambda x: -x["risk"])
    neuts = [x for x in stocks if x["dir"] == "中性"]
    n_bull, n_bear, n_neu = len(bulls), len(bears), len(neuts)

    if stocks:
        parts = []
        if bulls:
            top = "、".join("%s(%s)" % (x["name"], x["code"]) for x in bulls[:3])
            parts.append("看涨 %d 只，最值得关注 %s" % (n_bull, top))
        else:
            parts.append("本轮没有触发看涨的票")
        if bears:
            tb = "、".join("%s(%s)" % (x["name"], x["code"]) for x in bears[:3])
            parts.append("看跌/风险 %d 只，需回避 %s" % (n_bear, tb))
        summary = "「%s」板块：%d 条语料点名 %d 只个股，%s。" % (
            plate, len(vids), len(stocks), "；".join(parts))
    else:
        summary = "「%s」板块：%d 条语料暂未抽出明确点名个股。" % (plate, len(vids))

    out.update({
        "ok": True, "stocks": stocks,
        "bulls": bulls, "bears": bears, "neuts": neuts,
        "n_stocks": len(stocks), "n_bull": n_bull, "n_bear": n_bear, "n_neutral": n_neu,
        "summary": summary,
        "updated_at": __import__("datetime").datetime.now().strftime("%Y-%m-%d %H:%M"),
    })
    _plate_concl_cache[plate] = (now, dict(out))
    return out
def api_douyin_resonance(limit: int = 40) -> dict:
    """豆包语料 x 主力合成层 共振引擎（打通孤岛的化学反应）。

    从 douyin_corpus_video 的标题+摘要抽取点名个股 -> 关联 res_stock_card（合成研究卡）
    / sig_unified（六源统一信号）-> 排序产出高可信共振标的；
    并把「豆包在讲、主力合成层还没覆盖」的票作为自进化盲区(gaps)回吐，供 evo 层学习。
    结果缓存 300s（豆包语料按日更新），首次调用约 10~20s 抽取，之后命中缓存。
    """
    import time
    from datetime import datetime as _dt
    now = time.time()
    global _resonance_cache
    if _resonance_cache.get("payload") and now - _resonance_cache["ts"] < 300:
        p = dict(_resonance_cache["payload"]); p["cached"] = True
        return p

    rows = DB.conn.execute(
        "SELECT code, name FROM res_stock WHERE name IS NOT NULL AND length(name)>=3").fetchall()
    name2code, code2name = {}, {}
    for code, name in rows:
        name2code.setdefault(name, code)
        code2name.setdefault(code, name)
    names = sorted(name2code.keys(), key=len, reverse=True)

    def extract(text):
        found = set()
        for nm in names:
            if nm in text:
                found.add(nm)
        return {n for n in found if not any(n != m and n in m for m in found)}

    hits = {}
    for r in DB.conn.execute(
            "SELECT video_id, domain, title, summary, author, stance, topic FROM douyin_corpus_video"):
        vid, dom, title, summary, author, stance, topic = r[0], r[1], r[2], r[3], r[4], r[5], r[6]
        text = (title or "") + " " + (summary or "")
        for nm in extract(text):
            code = name2code[nm]
            h = hits.setdefault(code, {"n": 0, "domains": set(), "authors": set(),
                                       "stances": set(), "samples": []})
            h["n"] += 1
            h["domains"].add(dom)
            if author:
                h["authors"].add(author)
            if stance:
                h["stances"].add(stance)
            if len(h["samples"]) < 2 and summary:
                h["samples"].append({"author": author, "text": (summary or "")[:70]})

    card = {r[0]: (r[1], r[2]) for r in DB.conn.execute(
        "SELECT code, tier, res_score FROM res_stock_card")}
    # 2026-10-30：博主共振下线，overlap 不再参与打分（原 sc 里 +o、verdict 判 code in overlap）
    sig = {}
    for r in DB.conn.execute(
            "SELECT code, direction, COUNT(*) FROM sig_unified GROUP BY code, direction"):
        sig.setdefault(r[0], {})[r[1]] = sig.get(r[0], {}).get(r[1], 0) + r[2]

    out_rows, gaps = [], []
    for code, h in hits.items():
        # 2026-11-01（工单②）：原来缺省值是 `("-", None)` —— "-" 是个**假值哨兵**，
        # 前端拼 `'合成' + tier + '/' + String(score)` 时就渲染成字面量 `合成-/null`。
        # 按三态诚实原则拆开：
        #   · 票**不在** res_stock_card  → 这个维度本来就没有（has_card=False，tier/score 皆 None）
        #   · 票在卡里但 res_score 为空  → 算了没算出（score=None，tier 有值）
        # 前端据此分别写「合成层未覆盖」和「—」，不再出现 "null"。
        has_card = code in card
        t, rs = card.get(code, (None, None))
        s = sig.get(code, {})
        s_n = sum(s.values())
        sc = h["n"] + (5 if code in card else 0) + min(s_n, 5)
        if t in ("A", "S") and h["n"] >= 2:
            verdict = "强共振"
        elif code in card or s_n:
            verdict = "共振"
        else:
            verdict = "豆包独立关注"
            gaps.append({"code": code, "name": code2name.get(code), "douyin_n": h["n"]})
        out_rows.append({
            "code": code, "name": code2name.get(code),
            "douyin_n": h["n"], "domains": sorted(h["domains"]),
            "bloggers": sorted(h["authors"])[:6], "stances": sorted(h["stances"]),
            "card_tier": t, "card_score": rs, "has_card": has_card,
            "sig_n": s_n, "sig_dir": s, "verdict": verdict, "score": sc,
            "samples": h["samples"],
        })

    out_rows.sort(key=lambda x: -x["score"])
    out_rows = out_rows[:max(1, min(int(limit or 40), 60))]
    gaps.sort(key=lambda x: -x["douyin_n"])
    distinct = len(hits)
    resonance_n = sum(1 for c in hits if c in card or c in sig)
    payload = {
        "ok": True,
        "updated_at": _dt.now().strftime("%Y-%m-%d %H:%M"),
        "distinct_stocks": distinct,
        "resonance_n": resonance_n,
        "rows": out_rows,
        "gaps": gaps[:10],
    }
    _resonance_cache = {"ts": now, "payload": payload}
    return payload
