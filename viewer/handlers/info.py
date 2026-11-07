# -*- coding: utf-8 -*-
"""info 域 API 处理函数（从 viewer/server.py 物理抽取，2026-11-07 拆分重构）。

抽取规则：函数体逻辑零改动；依赖的 server 模块级符号经 `from viewer.server import (...)`
重定向进本地命名空间；标准库本地导入。server.py 末尾 re-export 保持命名空间兼容，
routes.py 与既有测试（S.api_*）无需改动。
"""
from __future__ import annotations

import datetime, logging, time
from viewer.server import (  # noqa: E402,F401
    DB,
    _jl,
    _num,
    _split_sectors,
    timedelta,
)

def api_info_brief(hours: int = 48) -> dict:
    """「消息与外围」顶部汇总结论（P0-D）：9 个子项 → 一屏「今日消息面 Top5 · 影响板块」。
    分来源取高信号项（政策 importance / 国际 intensity / 海外 heat / 公告 tone·类别 / 快讯 / 社区热点），
    再按优先级与影响面收敛成 5 条；并汇总最常被点到的板块。**每源标数据日**，陈旧源不冒充今日。"""
    log = logging.getLogger(__name__)
    out: dict = {"ok": True, "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                 "window_hours": hours, "items": [], "sectors": [], "counts": {},
                 "freshness": [], "stale_warn": "", "basis": ""}
    cutoff = (datetime.now() - timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M")
    cutoff_d = cutoff[:10]
    cand: list = []

    # 先探各源数据日：**陈旧源不许排进 Top5**（例如国际局势可能停在数日前），只能当背景。
    # 这是「语义门」纪律：不能把旧闻混进"今日消息面"里冒充新消息。
    def _src_latest(tbl: str, col: str) -> str:
        try:
            return str(DB.conn.execute("SELECT MAX(%s) FROM %s" % (col, tbl)).fetchone()[0] or "")[:16]
        except Exception:  # noqa: BLE001
            return ""
    geo_latest = _src_latest("geo_events", "event_date")
    geo_stale = bool(geo_latest) and geo_latest[:10] < (datetime.now() - timedelta(days=3)).strftime("%Y-%m-%d")

    def _add(src, tag, title, sectors, note, date, symbols, prio, link=""):
        if not title:
            return
        # link：这条消息的「完整研判在哪一页」。2026-10-31 去重：同一批国际事件在
        #   「国际局势」面板已有完整研判（decision），本页只负责「发生了什么」，
        #   不再复述那段结论（体检实测：3 条研判结论在两个面板逐字相同）。
        cand.append({"src": src, "tag": tag or "", "title": str(title)[:150],
                     "sectors": sectors or [], "note": (note or "")[:120],
                     "date": str(date or "")[:16], "symbols": symbols or [], "prio": prio,
                     "link": link or ""})

    # ① 政策 / 官方口径（importance 数字越小越重要）
    try:
        for r in DB.conn.execute(
                "SELECT pub_time, issuer, importance, title, sectors, transmission FROM policy_news "
                "WHERE pub_time>=? ORDER BY importance ASC, pub_time DESC LIMIT 12", (cutoff,)).fetchall():
            _add("政策", (r["issuer"] or "官方")[:20], r["title"], _split_sectors(r["sectors"]),
                 r["transmission"], r["pub_time"], [],
                 1 if (_num(r["importance"]) or 9) <= 1 else 2)
    except Exception as e:  # noqa: BLE001
        log.warning("info_brief 政策失败: %s", e)

    # ② 国际局势（intensity 越大越重要；注意该源当前可能停在更早的日期）
    #    2026-10-31 去重（进哥：「内容特别相似的应该合并」）：
    #      这里原本把 geo_events.decision（研判结论，60~120 字）当作 note 复述一遍，
    #      而「国际局势」面板的每张卡已经把同一段 decision 标成「研判结论」完整展示。
    #      同一段话在两个面板逐字出现，既占版面又让人以为是两条不同的判断。
    #      职责切开：本页答「今天有什么消息」（标题 + 板块 + 方向），
    #      「该怎么办」留在国际局势面板（link 指过去，点一下就到）。
    #    为什么 note 只留「利空…」半段：direction 是「利多A、B；利空C、D」，
    #      而 A、B 已经由 sectors_up 渲染成「影响板块」了 —— 同一批板块名在一个条目里
    #      出现两遍，还是冗余。留下利空面（它不在 sectors_up 里），信息不丢、重复归零。
    def _bear_side(direction) -> str:
        s = str(direction or "")
        i = s.find("利空")
        return s[i:].strip("；。 ") if i >= 0 else ""
    try:
        for r in DB.conn.execute(
                "SELECT title, intensity, direction, sectors_up, event_date, symbols_up "
                "FROM geo_events ORDER BY intensity DESC, event_date DESC LIMIT 6").fetchall():
            syms = _jl(r["symbols_up"]) if r["symbols_up"] else []
            _add("国际", "强度%s" % (r["intensity"] or "—"), r["title"],
                 _split_sectors(r["sectors_up"]), _bear_side(r["direction"]),
                 r["event_date"], [s for s in syms if isinstance(s, dict)][:4],
                 6 if geo_stale else (1 if (_num(r["intensity"]) or 0) >= 4 else 3),
                 link="geo_events")
    except Exception as e:  # noqa: BLE001
        log.warning("info_brief 国际失败: %s", e)

    # ③ 海外舆情（heat 热度）
    try:
        for r in DB.conn.execute(
                "SELECT publish_time, region, heat, title, sectors, sentiment FROM overseas_news "
                "WHERE publish_time>=? ORDER BY heat DESC LIMIT 8", (cutoff,)).fetchall():
            _add("海外", r["region"] or "", r["title"], _split_sectors(r["sectors"]),
                 (r["sentiment"] or ""), r["publish_time"], [],
                 2 if (_num(r["heat"]) or 0) >= 70 else 4)
    except Exception as e:  # noqa: BLE001
        log.warning("info_brief 海外失败: %s", e)

    # ④ 个股公告（tone 有明确多空 / 并购重组类优先）
    try:
        for r in DB.conn.execute(
                "SELECT notice_date, code, name, title, cat, tone FROM stock_ann "
                "WHERE notice_date>=? ORDER BY notice_date DESC LIMIT 80", (cutoff_d,)).fetchall():
            tone, cat = (r["tone"] or ""), (r["cat"] or "")
            prio = 2 if tone in ("good", "bad") else (3 if cat in ("并购重组", "业绩预告") else 5)
            _add("公告", "%s · %s" % (r["name"] or "", cat or "公告"), r["title"], [],
                 ("利好" if tone == "good" else ("利空" if tone == "bad" else "")),
                 r["notice_date"],
                 ([{"code": r["code"], "name": r["name"]}] if r["code"] else []), prio)
    except Exception as e:  # noqa: BLE001
        log.warning("info_brief 公告失败: %s", e)

    # ⑤ 全市场快讯（兜底，优先级最低）
    try:
        for r in DB.conn.execute(
                "SELECT publish_time, site, title FROM news WHERE publish_time>=? "
                "ORDER BY publish_time DESC LIMIT 20", (cutoff,)).fetchall():
            _add("快讯", r["site"] or "", r["title"], [], "", r["publish_time"], [], 5)
    except Exception as e:  # noqa: BLE001
        log.warning("info_brief 快讯失败: %s", e)

    # ⑥ 社区热点词（样本量）
    try:
        for r in DB.conn.execute(
                "SELECT topic, weight FROM topics ORDER BY weight DESC LIMIT 10").fetchall():
            _add("社区热点", "热度 %s" % round(_num(r["weight"]) or 0), r["topic"],
                 [r["topic"]], "帖子被高频提及的词", "", [], 6)
    except Exception as e:  # noqa: BLE001
        log.warning("info_brief 热点失败: %s", e)

    # 收敛成 5 条：按优先级、单来源最多 2 条（保证不全是同一源）
    picked: list = []
    per_src: dict = {}
    for c in sorted(cand, key=lambda x: (x["prio"], -len(x["sectors"]))):
        if len(picked) >= 5:
            break
        if per_src.get(c["src"], 0) >= 2:
            continue
        per_src[c["src"]] = per_src.get(c["src"], 0) + 1
        picked.append(c)
    out["items"] = picked

    # 影响板块云：统计全部候选里被点到的板块（比只看 5 条更能反映消息面重心）
    sec_cnt: dict = {}
    for c in cand:
        # 陈旧源（例如停在数日前的国际局势）不参与「影响板块」统计 ——
        # 否则一条旧闻的板块会顶到"今日重心"上，正是报告要根治的"时效断档"。
        if c["src"] == "国际" and geo_stale:
            continue
        for s in c["sectors"]:
            k = s.strip()
            if not k:
                continue
            e = sec_cnt.setdefault(k, {"sector": k, "n": 0, "srcs": set()})
            e["n"] += 1
            e["srcs"].add(c["src"])
    out["sectors"] = sorted(
        [{"sector": v["sector"], "n": v["n"], "srcs": sorted(v["srcs"])} for v in sec_cnt.values()],
        key=lambda x: -x["n"])[:10]

    # 各源数据日 + 陈旧提示（这是「语义门」：不能把 9-26 的国际局势当成今天的新消息）
    fresh: list = []
    for label, tbl, col in (("政策风向", "policy_news", "pub_time"),
                            ("国际局势", "geo_events", "event_date"),
                            ("海外舆情", "overseas_news", "publish_time"),
                            ("个股公告", "stock_ann", "notice_date"),
                            ("全市场快讯", "news", "publish_time"),
                            ("社区热点", "topics", "weight")):
        try:
            v = DB.conn.execute("SELECT MAX(%s) FROM %s" % (col, tbl)).fetchone()[0]
        except Exception:  # noqa: BLE001
            v = None
        if label == "社区热点":
            fresh.append({"label": label, "latest": "样本 %s 词条" % (len(DB.conn.execute(
                "SELECT 1 FROM topics LIMIT 30").fetchall()))})
            continue
        fresh.append({"label": label, "latest": str(v)[:16] if v else "—"})
    out["freshness"] = fresh
    stale = [f for f in fresh if f["label"] == "国际局势" and
             f["latest"] != "—" and f["latest"][:10] < (datetime.now() - timedelta(days=3)).strftime("%Y-%m-%d")]
    if stale:
        out["stale_warn"] = ("「国际局势」的最新数据停在 %s（不在本次 %s 小时窗口内）；"
                             "下面的外部消息以政策风向与海外舆情为准，国际条目仅供背景参考。"
                             % (stale[0]["latest"], hours))

    out["counts"] = {"candidates": len(cand), "policy": sum(1 for c in cand if c["src"] == "政策"),
                     "geo": sum(1 for c in cand if c["src"] == "国际"),
                     "overseas": sum(1 for c in cand if c["src"] == "海外"),
                     "ann": sum(1 for c in cand if c["src"] == "公告"),
                     "news": sum(1 for c in cand if c["src"] == "快讯"),
                     "topics": sum(1 for c in cand if c["src"] == "社区热点")}
    out["basis"] = ("口径：政策=官方/部委发布（importance 越小越重要）；国际=事件强度≥4；"
                    "海外=舆情热度≥70；公告=带多空 tone 或并购重组类；快讯/热点词兜底。"
                    "影响板块取自各源自带的 sectors 字段，未做二次推断。仅供方向参考，非投资建议。")
    return out
