# -*- coding: utf-8 -*-
"""B站博主共识结论（2026-10-12，王总：市场情绪面板不能只甩视频标题堆，
要先给结论——① 博主集中聊的板块方向 ② 博主点名最多的个股；标题堆下沉为证据）。

口径（全部本地、不联网、不推测）：
- 个股识别：全量 A 股名称词典（scraper/name_match + stock_names_full.json ≈5,600 名），
  只认完整名称，不猜简称、不猜代码；
- 情绪方向：viewer/analytics.score_sentiment（与帖子情绪速览/入库 sentiment 同一把尺，
  含否定式处理），仅代表标题口头倾向，不构成涨跌预测；
- 板块方向两路合成：① 被点名个股按 stock_org 一级行业聚合（覆盖活跃池 ≈321 只）；
  ② 标题直提行业/题材词（内置词表，覆盖"聊板块不点个股"的视频）。
"""
from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path

from scraper import name_match
from viewer import analytics

_ROOT = Path(__file__).resolve().parent
_DB = _ROOT / "viewer" / "app.db"

_CACHE: dict = {}
_CACHE_TTL = 300  # 秒
_LOCK = threading.Lock()

# 行业/题材词 → 板块名。不用裸词 "AI"（research_insight 的教训：裸短词会误归类）。
_TOPIC_WORDS: list[tuple[str, str]] = [
    ("人工智能", "人工智能"), ("大模型", "人工智能"), ("AIGC", "人工智能"),
    ("DeepSeek", "人工智能"), ("OpenAI", "人工智能"), ("AI+", "人工智能"),
    ("机器人", "机器人"), ("人形机器人", "机器人"), ("具身智能", "机器人"),
    ("算力", "算力·半导体"), ("芯片", "算力·半导体"), ("半导体", "算力·半导体"),
    ("光模块", "算力·半导体"), ("CPO", "算力·半导体"), ("光刻", "算力·半导体"),
    ("存储", "算力·半导体"), ("数据中心", "算力·半导体"), ("英伟达", "算力·半导体"),
    ("消费电子", "消费电子"), ("华为", "消费电子"), ("苹果", "消费电子"),
    ("折叠屏", "消费电子"), ("AI眼镜", "消费电子"), ("手机", "消费电子"),
    ("房地产", "房地产"), ("地产", "房地产"), ("楼市", "房地产"),
    ("银行", "银行"), ("券商", "证券"), ("证券", "证券"), ("保险", "保险"),
    ("黄金", "黄金·有色"), ("有色", "黄金·有色"), ("稀土", "黄金·有色"),
    ("白银", "黄金·有色"), ("工业金属", "黄金·有色"),
    ("医药", "医药"), ("创新药", "医药"), ("中药", "医药"), ("医疗", "医药"),
    ("疫苗", "医药"), ("CXO", "医药"),
    ("白酒", "食品饮料"), ("食品", "食品饮料"), ("啤酒", "食品饮料"), ("乳制品", "食品饮料"),
    ("汽车", "汽车"), ("智能驾驶", "汽车"), ("无人驾驶", "汽车"), ("车企", "汽车"),
    ("军工", "军工"), ("国防", "军工"), ("航天", "军工"), ("大飞机", "军工"),
    ("固态电池", "锂电·新能源"), ("锂电", "锂电·新能源"), ("锂电池", "锂电·新能源"),
    ("钠电池", "锂电·新能源"), ("电解液", "锂电·新能源"), ("储能", "锂电·新能源"),
    ("光伏", "光伏·风电"), ("风电", "光伏·风电"),
    ("煤炭", "煤炭"), ("钢铁", "钢铁"),
    ("电力", "电力"), ("电网", "电力"), ("绿电", "电力"), ("核电", "电力"),
    ("游戏", "传媒·游戏"), ("影视", "传媒·游戏"), ("传媒", "传媒·游戏"), ("短剧", "传媒·游戏"),
    ("信创", "计算机"), ("操作系统", "计算机"), ("网络安全", "计算机"), ("软件", "计算机"),
    ("物流", "交通运输"), ("快递", "交通运输"), ("航运", "交通运输"), ("港口", "交通运输"),
    ("航空", "交通运输"), ("高铁", "交通运输"),
    ("旅游", "社服·消费"), ("酒店", "社服·消费"), ("免税", "社服·消费"), ("电商", "社服·消费"),
    ("零售", "社服·消费"), ("消费", "社服·消费"),
    ("猪肉", "农业"), ("农业", "农业"), ("种业", "农业"), ("养殖", "农业"),
    ("化工", "化工"), ("氟化工", "化工"), ("磷化工", "化工"),
    ("基建", "建筑·基建"), ("一带一路", "建筑·基建"),
]
# 行业词按长度降序匹配，防"机器人"被"机器"截胡之类（模糊匹配铁律：长词优先）
_TOPIC_WORDS.sort(key=lambda x: -len(x[0]))
_GENERIC_INDUSTRY = {"其他", "综合", "其他软件服务"}


def _conn() -> sqlite3.Connection:
    con = sqlite3.connect("file:%s?mode=ro" % _DB.as_posix().replace("\\", "/"), uri=True)
    con.row_factory = sqlite3.Row
    return con


def _industry_map(con: sqlite3.Connection) -> dict[str, str]:
    """code → 一级行业（stock_org.industry 第一段）。"""
    out: dict[str, str] = {}
    try:
        for r in con.execute(
            "SELECT code, industry FROM stock_org WHERE industry IS NOT NULL AND industry != ''"
        ):
            code = str(r["code"] or "").strip().zfill(6)
            ind = str(r["industry"]).split("-")[0].strip()
            if code.isdigit() and ind:
                out[code] = ind
    except Exception:  # noqa: BLE001
        pass
    return out


def _tone(bull: int, bear: int) -> tuple[str, str]:
    """(档位key, 中文判词)。多空都无人表态=中性；悬殊=偏多/偏空；否则=分歧。"""
    if bull >= 2 and bull >= bear * 2:
        return "bull", "偏多"
    if bear >= 2 and bear >= bull * 2:
        return "bear", "偏空"
    if bull > 0 and bear > 0:
        return "div", "分歧"
    if bull > 0:
        return "bull", "偏多"
    if bear > 0:
        return "bear", "偏空"
    return "neu", "中性"


def _sample(title: str, blogger: str, vdate: str, url: str) -> dict:
    t = (title or "").strip()
    if len(t) > 46:
        t = t[:46] + "…"
    return {"title": t, "blogger": blogger, "date": vdate, "url": url}


def digest(cat: str = "sentiment", days: int = 30) -> dict:
    key = (cat, int(days))
    with _LOCK:
        hit = _CACHE.get(key)
        if hit and time.time() - hit[0] < _CACHE_TTL:
            return hit[1]
    days = max(3, min(int(days), 365))
    con = _conn()
    try:
        rows = con.execute(
            "SELECT blogger, title, vdate, url FROM bili_pile "
            "WHERE cat = ? AND vdate >= date('now', 'localtime', ?) AND vdate <= date('now', 'localtime') "
            "ORDER BY vdate DESC",
            (cat, "-%d day" % days),
        ).fetchall()
        ind_map = _industry_map(con)
    finally:
        con.close()

    stocks: dict[str, dict] = {}
    sectors: dict[str, dict] = {}
    stock_rows = 0      # 有点名个股的视频条数
    sector_rows = 0     # 命中板块话题的视频条数
    for r in rows:
        title = r["title"] or ""
        label = analytics.score_sentiment(title)[0]
        bull = 1 if label == "看多" else 0
        bear = 1 if label == "看空" else 0

        # ---- 个股识别（每条视频同名只计 1 次，问的是"多少条视频提到"）----
        codes: list[tuple[str, str]] = []
        try:
            seen: set[str] = set()
            for nm, code, _s, _e in name_match.scan_names(title):
                if code in seen:
                    continue
                seen.add(code)
                codes.append((nm, code))
        except Exception:  # noqa: BLE001
            codes = []
        hit_sec: set[str] = set()
        if codes:
            stock_rows += 1
            for nm, code in codes:
                st = stocks.get(code)
                if not st:
                    st = stocks[code] = {"code": code, "name": nm, "n": 0,
                                         "bull": 0, "bear": 0, "bloggers": set(), "samples": []}
                st["n"] += 1
                st["bull"] += bull
                st["bear"] += bear
                st["bloggers"].add(r["blogger"])
                if len(st["samples"]) < 2:
                    st["samples"].append(_sample(title, r["blogger"], r["vdate"], r["url"]))
                ind = ind_map.get(code)
                if ind:
                    hit_sec.add(ind)

        # ---- 标题直提行业/题材词（聊板块不点个股的视频也计入方向）----
        if title:
            for w, sec in _TOPIC_WORDS:
                if w in title:
                    hit_sec.add(sec)
        if hit_sec:
            sector_rows += 1
            for sec in hit_sec:
                sc = sectors.get(sec)
                if not sc:
                    sc = sectors[sec] = {"sector": sec, "n": 0, "bull": 0, "bear": 0,
                                         "from_stock": 0, "samples": []}
                sc["n"] += 1
                sc["bull"] += bull
                sc["bear"] += bear
                if codes:
                    sc["from_stock"] += 1
                if len(sc["samples"]) < 2:
                    sc["samples"].append(_sample(title, r["blogger"], r["vdate"], r["url"]))

    sec_list = sorted(sectors.values(), key=lambda x: (-x["n"], -(x["bull"] + x["bear"])))[:8]
    stk_list = sorted(stocks.values(), key=lambda x: (-x["n"], -x["bull"]))[:10]
    top_w = max((s["n"] for s in sec_list), default=1)
    for s in sec_list:
        s["tone"], s["tone_label"] = _tone(s["bull"], s["bear"])
        s["w"] = round(100.0 * s["n"] / max(1, top_w))
        s["bloggers_n"] = len({sp["blogger"] for sp in s["samples"]})
    top_s = max((s["n"] for s in stk_list), default=1)
    for s in stk_list:
        s["tone"], s["tone_label"] = _tone(s["bull"], s["bear"])
        s["w"] = round(100.0 * s["n"] / max(1, top_s))
        s["bloggers"] = sorted(s["bloggers"])
        s["bloggers_n"] = len(s["bloggers"])

    # ---- 一句话结论（全部由真实计数拼装，无样本不编数）----
    total = len(rows)
    if total == 0:
        verdict = "近 %d 天没有该分堆的视频数据，给不出方向结论。" % days
    elif not sec_list and not stk_list:
        verdict = ("近 %d 天共 %d 条视频，标题里既没点名个股也没提明确板块，"
                   "博主都在聊大盘与技巧，暂时给不出方向结论。" % (days, total))
    else:
        parts = []
        if sec_list:
            parts.append("博主集中聊的板块方向：" + "、".join(
                "%s(%d条·%s)" % (s["sector"], s["n"], s["tone_label"]) for s in sec_list[:3]))
        if stk_list:
            parts.append("点名最多的个股：" + "、".join(
                "%s(%d次·%s)" % (s["name"], s["n"], s["tone_label"]) for s in stk_list[:3]))
        verdict = "近 %d 天 %d 条视频：%s。" % (days, total, "；".join(parts))

    basis = [
        "个股识别：按 A 股名称词典（约 5,600 只）匹配标题里的完整公司名，不猜简称；同一条视频同名只计 1 次。",
        "情绪方向：与帖子情绪速览/入库 sentiment 是同一把尺（统一词表，含否定式处理），"
        "「偏多/偏空」指博主口头倾向的人数对比，不构成涨跌预测。",
        "板块方向两路合成：① 被点名个股按其所属一级行业归堆（行业资料覆盖活跃池约 321 只）；"
        "② 标题直接提到行业/题材词（如「固态电池」「军工」）。",
        "讨论热度 ≠ 荐股：热度高只说明关注多。买卖决策请结合「每日预测」（板块方向→股东结构→技术买点）自己把关。",
    ]

    payload = {
        "ok": True,
        "cat": cat,
        "days": days,
        "total": total,
        "stock_mention_rows": stock_rows,
        "sector_hit_rows": sector_rows,
        "sectors": sec_list,
        "stocks": stk_list,
        "verdict": verdict,
        "basis": basis,
        "generated_at": time.strftime("%Y-%m-%d %H:%M"),
    }
    with _LOCK:
        _CACHE[key] = (time.time(), payload)
    return payload


if __name__ == "__main__":
    import json
    print(json.dumps(digest("sentiment", 30), ensure_ascii=False, indent=1)[:3000])
