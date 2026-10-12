# -*- coding: utf-8 -*-
"""研报结论聚合（research / 机构评级面板）：把全市场研报压成「结论 + 排序」。

背景（2026-10-01 王总反馈）：
  原来的「研报 / 机构评级」面板只把东财研报一条条罗列出来（标题/机构/评级/目标价），
  看的人得自己从几百份研报里汇总「到底哪只票能看」——信息有了，结论没有。

本模块做的事：
  0. 先说清楚一个必须让用户知道的偏差：东财研报里 ~92% 都是「买入/增持」，
     因为券商极少公开发负面报告。所以本表**只能横向比强弱、不能把「看多」当利好**。
     打分里刻意降权「一致看多」，加重「覆盖广度（几家机构真金白银覆盖）」与
     「目标价空间」，并对「只有 1 家券商的单点研报」直接封顶到 B 级。
  1. 抓近 N 天全市场券商研报（东财 report_list，分页），按个股聚合。
     注意东财新版研报多数不再硬填目标价，改用「预测当年 PE / EPS」替代估值。
  2. 拉实时现价，算一致目标价的中位数与隐含空间（中位数抗单点极端值）。
  3. 六因子打分（一致看多 20 / 上涨空间 25 / 覆盖广度 12 / 新鲜度 13 / 预测估值 8 / 主线热度 10）。
  4. 分档 S/A/B/C/D + 持仓属性（短线进攻 / 中线埋伏 / 长线配置 / 回避）+ 每只票一句人话结论。

对外：
  · insight(days=60) -> dict（带文件缓存，TTL 3 小时，研报非实时数据）
  · build(days)      -> dict（真跑：抓网 + 计算）
"""
from __future__ import annotations

import datetime
import json
import os
import statistics
import threading
import time

import emapi
import research

_HERE = os.path.dirname(os.path.abspath(__file__))
CACHE_PATH = os.path.join(_HERE, "viewer", "research_insight.json")
TTL_SEC = 3 * 3600
_LOCK = threading.Lock()

# ---------------------------------------------------------------- 主题词典
# (主题名, 强关键词(命中即归类), 弱关键词(需累计命中 >=2 才算可靠归类))
# 教训（2026-10-01）：第一版把裸词 "AI" 放进强关键词，结果「东航物流/科锐国际/飞龙股份」
# 全被误判成 AI 算力主线。凡是两个字母的泛词一律不进强关键词。
THEMES = [
    ("AI算力/半导体", ["算力", "智算", "人工智能", "大模型", "先进制程", "晶圆", "封测",
                    "存储芯片", "DRAM", "GPU", "光模块", "AI服务器", "AI眼镜", "AI应用"],
     ["芯片", "半导体", "服务器", "PCB", "电子", "终端"]),
    ("机器人/高端装备", ["人形机器人", "机器人", "机械臂", "减速器", "丝杠", "灵巧手",
                     "工业母机", "高端装备"],
     ["机床", "刀具", "精密", "自动化"]),
    ("医药/创新药", ["创新药", "创新药企", "医疗器械", "CXO"],
     ["医药", "生物", "疫苗", "临床", "药企", "集采", "中药"]),
    ("军工/国防", ["军工", "国防", "导弹", "军民融合", "航天"],
     ["航空", "船舶", "雷达", "兵装", "装备"]),
    ("资源/周期", ["动力煤", "焦煤", "焦企", "铁矿", "铜矿", "铝土", "黄金", "稀土",
                "钢铁", "锂矿", "磷矿", "资源品"],
     ["煤炭", "焦炭", "铜", "铝", "锰", "锌", "锂", "磷", "有色", "能源"]),
    ("电力/公用事业", ["电力", "核电", "绿电", "电网", "火电", "发电"],
     ["用电", "电力体制改革", "公用"]),
    ("新能源/储能", ["储能", "逆变器", "钠离子", "海风"],
     ["光伏", "风电", "锂电", "组件", "硅料", "负极", "正极", "电解液", "新能源"]),
    ("化工/新材料", ["新材料", "钛白", "玻纤", "民爆", "化纤", "氯碱"],
     ["化工", "农药", "磷", "氟", "乙烯", "农化"]),
    ("汽车/智驾", ["智能驾驶", "自动驾驶", "新能源车", "汽车零部件"],
     ["汽车", "整车", "智驾", "座舱", "轮胎", "车规"]),
    ("消费/食品饮料", ["白酒", "食品饮料", "免税", "餐饮"],
     ["零售", "乳业", "调味", "啤酒", "零食", "消费复苏", "潮玩"]),
    ("金融/非银", ["券商", "保险", "财富管理", "金融IT"],
     ["银行", "非银", "信托"]),
    ("地产/基建", ["房地产", "基建", "水泥", "竣工"],
     ["地产", "房建", "城投", "建材"]),
    ("出口/出海", ["出海", "全球化", "跨境", "性价比出海"],
     ["出口", "海外", "关税"]),
    ("消费电子", ["折叠屏", "AI眼镜", "智能终端"],
     ["消费电子", "手机", "XR", "穿戴"]),
    ("物流/交运", ["快递", "物流", "航运", "港口", "货代"],
     ["航空运输", "高铁"]),
]

_RANK_W = {"buy": 1.0, "overweight": 0.55, "neutral": 0.0, "underweight": -0.55, "sell": -1.0}


def _today() -> datetime.date:
    return datetime.date.today()


def _theme_of(titles: list) -> list:
    """返回 (主题名, 是否可靠命中)。可靠 = 命中强关键词，或命中 >=2 个不同弱关键词。"""
    out = []
    for name, strong, weak in THEMES:
        strong_hit = False
        weak_hit = set()
        for t in titles:
            if any(k in t for k in strong):
                strong_hit = True
            else:
                for k in weak:
                    if k in t:
                        weak_hit.add(k)
        if strong_hit or len(weak_hit) >= 2:
            out.append((name, bool(strong_hit)))
    return out


def _fetch_reports(days: int, max_pages: int = 6, page_size: int = 100):
    """抓近 days 天全市场券商研报（去重）。返回 (rows, hits)。"""
    begin = (_today() - datetime.timedelta(days=days)).strftime("%Y-%m-%d")
    rows, seen, hits = [], set(), 0
    for p in range(1, max_pages + 1):
        try:
            r = emapi.report_list(page_size=page_size, page_no=p, begin_time=begin)
        except Exception:
            break
        if not r.get("ok"):
            break
        hits = r.get("hits") or hits
        raw = r.get("rows") or []
        if not raw:
            break
        for it in raw:
            key = (it.get("title") or "", it.get("orgName") or it.get("orgSName") or "",
                   str(it.get("publishDate") or ""))
            if key in seen:
                continue
            seen.add(key)
            rows.append(research._row(it))
        if len(raw) < page_size:
            break
    return rows, hits


def _quotes(codes: list) -> dict:
    """批量现价：东财 ulist（快）→ 腾讯 qt.gtimg（东财挂了时的兜底）。"""
    out = {}
    codes = [c for c in codes if c and c.isdigit()]
    if not codes:
        return out
    try:
        sids = ",".join(emapi.secid(c) for c in codes)
        for q in emapi.ulist(sids, fields="f2,f3,f12,f14"):
            if q.get("code") and q.get("price") is not None:
                out[q["code"]] = {"price": q["price"], "pct": q.get("pct"), "name": q.get("name")}
    except Exception:
        pass
    if not out:
        try:
            for i in range(0, len(codes), 40):
                batch = codes[i:i + 40]
                syms = [("sh" if c[:1] == "6" else "sz") + c for c in batch]
                for q in emapi.tencent_quote_batch(syms) or []:
                    if q.get("code") and q.get("price") is not None:
                        out[q["code"]] = {"price": q["price"], "pct": q.get("pct"), "name": q.get("name")}
        except Exception:
            pass
    return out


def _grade_of(score: float, bull: float, n_orgs: int) -> tuple:
    """分档。硬约束：只有 1 家券商的单点研报，最高只能 B（证据不足）。

    判词口径（2026-10-01）：每个出口都把「为什么回避」写进标签里。
    原来三处 D 档一律返回「回避」，跟 screener 技术面的「偏弱回避」、盯盘风险标签
    撞同一个词，读者分不清是谁在说回避、按什么判的。现在按判据区分：
      回避·机构偏空（一致评级净值为负）／回避·单点证据弱（只有 1 家券商且分低）／
      回避·研报评分低（六因子综合分 <33）。
    这一整套是「研报维度」结论，跟技术面换手形态分档是两把尺。
    """
    if bull < -0.05:
        return "D", "回避·机构偏空"
    if n_orgs <= 1:  # 单点证据封顶
        if score >= 58:
            return "B", "中线埋伏（单点）"
        if score >= 45:
            return "C", "观察（单点）"
        return "D", "回避·单点证据弱"
    if score >= 66 and bull >= 0.2:
        return "S", "核心机会"
    if score >= 52:
        return "A", "短线进攻"
    if score >= 44:
        return "B", "中线埋伏"
    if score >= 33:
        return "C", "仅观察"
    return "D", "回避·研报评分低"


def _holdings(age_days, upside, n_orgs, pe_mean, theme_n, n_depth) -> list:
    h = []
    if age_days <= 12 and upside >= 15:
        h.append("短线进攻")
    if n_orgs >= 2 and (30 >= age_days >= 12 or theme_n >= 12):
        h.append("中线埋伏")
    if n_orgs >= 3 and pe_mean is not None and 0 < pe_mean <= 40 and n_depth >= 1:
        h.append("长线配置")
    if n_orgs >= 2 and upside >= 35:
        h.append("目标价弹性")
    if not h:
        h.append("观望")
    return h


def _verdict(grade, upside, n_orgs, target_stale) -> str:
    v = {
        "S": "综合最优：多家机构覆盖 + 空间/题材都在线，可优先进自选、分批建仓",
        "A": "短线催化：研报新鲜、题材热，适合 2~6 周波段，注意别追高开",
        "B": "中线埋伏：逻辑成立但催化未到，回调分批，等下一份研报/财报确认",
        "C": "仅观察：机构在跟但不够强，评分升到 B 以上再动手",
        "D": "回避（研报口径）：机构分歧或评级偏空，不追高、不逆势接",
    }.get(grade, "观望")
    if n_orgs <= 1:
        v += "（只有 1 家券商给覆盖，单点证据，仅作线索）"
    if target_stale:
        v += "（目标价发布较久、可能未随股价下修，空间别当真）"
    if grade in ("S", "A") and upside >= 25:
        v += "（一致目标价隐含空间已超 25%，追high 要控制仓位）"
    return v + "。"


def _one_liner(name, code, n_orgs, dist, price, target, upside, pe_mean, age_days,
               theme, grade_label) -> str:
    parts = []
    if n_orgs:
        parts.append(f"{n_orgs} 家券商覆盖（{dist.get('buy', 0)} 买入 / "
                     f"{dist.get('overweight', 0)} 增持）")
    if target and price:
        parts.append(f"一致目标价 {target:.2f} vs 现价 {price:.2f}，隐含 {upside:+.1f}%")
    elif target:
        parts.append(f"一致目标价 {target:.2f}")
    if pe_mean is not None:
        parts.append(f"预测 PE {pe_mean:g} 倍")   # 与列表列的 pe_mean 保持一致，别一处取整一处带小数
    if theme:
        parts.append(f"主线「{theme}」")
    parts.append(f"最新研报 {age_days} 天前")
    return f"{name or code} {code}（{grade_label}）：" + "，".join(parts) + "。"


def build(days: int = 60, max_pages: int = 6) -> dict:
    t0 = time.time()
    rows, hits = _fetch_reports(days, max_pages=max_pages)
    if not rows:
        return {"ok": False, "reason": "未取到研报数据（东财 report_list 异常或窗口内无研报）"}

    # ---- 按个股聚合 ----
    agg = {}
    theme_cnt = {}
    theme_conf = {}
    for r in rows:
        code = (r.get("stock_code") or "").strip()
        if not code.isdigit():
            continue
        a = agg.setdefault(code, {"name": r.get("stock") or "", "reports": [], "themes": [], "conf": []})
        a["reports"].append(r)
        for name, reliable in _theme_of([r.get("title") or ""]):
            a["themes"].append(name)
            theme_cnt[name] = theme_cnt.get(name, 0) + 1
            if reliable:
                theme_conf[name] = True
    if not agg:
        return {"ok": False, "reason": "研报均未带股票代码，无法聚合出个股结论"}

    quotes = _quotes(list(agg.keys()))
    picks = []
    for code, a in agg.items():
        rs = a["reports"]
        n = len(rs)
        dist = {"buy": 0, "overweight": 0, "neutral": 0, "underweight": 0, "sell": 0}
        orgs, targets, pes, dates, titles = set(), [], [], [], []
        n_depth = 0
        for r in rs:
            k = r.get("rating_code")
            if k in dist:
                dist[k] += 1
            if k:
                orgs.add(r.get("org") or "?")
            if r.get("has_target") and r.get("target"):
                targets.append(r["target"])
            if r.get("pe") is not None and r.get("pe") > 0:
                pes.append(r["pe"])
            if r.get("date"):
                dates.append(r["date"])
            titles.append(r.get("title") or "")
            if "深度" in (r.get("title") or ""):
                n_depth += 1
        if not dates:
            continue
        latest = max(dates)
        try:
            age_days = (_today() - datetime.date.fromisoformat(latest)).days
        except Exception:
            age_days = 999
        bull = sum(_RANK_W.get(r.get("rating_code"), 0) for r in rs) / float(n or 1)
        # 中位数抗单点极端目标价（第一版用均值，德业股份 142 把空间吹到 +81%）
        target = round(statistics.median(targets), 2) if targets else None
        pe_mean = round(sum(pes) / len(pes), 1) if pes else None
        q = quotes.get(code) or {}
        price, pct = q.get("price"), q.get("pct")
        target_stale = bool(targets) and age_days > 30
        if price is None:
            upside = None
        elif target:
            upside = round((target - price) / price * 100, 1)
        else:
            upside = None
        # 目标价发布超 30 天未更新 → 空间打折（股价可能早就走完了）
        uc = upside if upside is not None else 0.0

        # 六因子
        f_consensus = (bull + 1.0) / 2.0 * 20 * (1.0 if targets else 0.85)
        if upside is None:
            f_space = 12.0
        elif upside < 0:
            f_space = max(0.0, 5.0 + upside / 4.0)
        elif upside < 10:
            f_space = 6.0 + uc * 0.6
        elif upside < 20:
            f_space = 12 + (uc - 10) * 0.7
        elif upside < 30:
            f_space = 19 + (uc - 20) * 0.3
        else:
            f_space = 22 + min(3.0, (uc - 30) * 0.15)
        if target_stale and f_space > 6:
            f_space = round(f_space * 0.85, 1)
        norg = len(orgs)
        f_breadth = {1: 4.0, 2: 7.0, 3: 9.5}.get(min(norg, 3), 12.0)
        f_fresh = 13.0 if age_days <= 7 else 11.0 if age_days <= 14 else \
            8.0 if age_days <= 30 else 5.0 if age_days <= days else 3.0
        if pe_mean is None:
            f_val = 4.0
        else:
            f_val = 8.0 if pe_mean <= 20 else 6.5 if pe_mean <= 30 else \
                5.0 if pe_mean <= 45 else 3.0 if pe_mean <= 70 else 2.0

        # 主线：取最可靠（强词命中优先）的那条；都没可靠命中 → 标“未归类”，只给中性热度分
        theme, conf = "", False
        for t in sorted(set(a["themes"]), key=lambda x: -theme_cnt.get(x, 0)):
            if theme_conf.get(t):
                theme, conf = t, True
                break
        if not theme:
            theme = sorted(set(a["themes"]), key=lambda x: -theme_cnt.get(x, 0))[0] if a["themes"] else ""
        theme_n = theme_cnt.get(theme, 0)
        f_theme = min(theme_n, 40) / 40.0 * 10 if conf else 5.0

        score = round(f_consensus + f_space + f_breadth + f_fresh + f_val + f_theme, 1)
        grade, grade_label = _grade_of(score, bull, len(orgs))

        picks.append({
            "code": code, "name": a["name"] or (q.get("name") or ""),
            "price": price, "pct": pct,
            "score": score, "grade": grade, "grade_label": grade_label,
            "holdings": _holdings(age_days, uc, len(orgs), pe_mean, theme_n, n_depth),
            "n_reports": n, "n_orgs": len(orgs), "orgs": sorted(orgs),
            "dist": dist, "bull": round(bull, 3),
            "target": target, "n_target": len(targets),
            "upside": upside, "pe_mean": pe_mean,
            "latest_date": latest, "age_days": age_days,
            "themes": sorted(set(a["themes"])), "theme": theme, "theme_reliable": conf,
            "target_stale": target_stale, "n_depth": n_depth,
            "verdict": _verdict(grade, uc, len(orgs), target_stale),
            "one_liner": _one_liner(a["name"] or q.get("name") or "", code, len(orgs), dist,
                                    price, target, uc, pe_mean, age_days, theme, grade_label),
            "latest_title": (rs[-1].get("title") or "")[:60],
            "factors": {
                "一致看多": round(f_consensus, 1), "上涨空间": round(f_space, 1),
                "覆盖广度": round(f_breadth, 1), "研报新鲜度": round(f_fresh, 1),
                "预测估值": round(f_val, 1), "主线热度": round(f_theme, 1),
            },
        })

    picks.sort(key=lambda x: (-x["score"], -x["n_orgs"], x["code"]))
    for i, p in enumerate(picks, 1):
        p["rank"] = i
    # 同一分档内重新按名次分组（S 里 1..n）
    for g in ("S", "A", "B", "C", "D"):
        k = 0
        for p in picks:
            if p["grade"] == g:
                k += 1
                p["rank_in_grade"] = k

    themes_sorted = sorted(theme_cnt.items(), key=lambda kv: -kv[1])[:6]
    themes = [{"theme": t, "n": c, "reliable": bool(theme_conf.get(t))} for t, c in themes_sorted]
    theme_pick = {}
    for p in picks:  # 已按分排序
        for t in p["themes"]:
            if t not in theme_pick:
                theme_pick[t] = p["code"]
            if len(theme_pick) >= len(themes_sorted):
                break

    n_bull = sum(1 for p in picks if p["bull"] > 0.2)
    gs = {}
    for p in picks:
        gs[p["grade"]] = gs.get(p["grade"], 0) + 1
    total = len(picks)
    top3 = [p for p in picks if p["grade"] in ("S", "A")][:3]
    n_avoid = gs.get("D", 0)

    main_line = "、".join(x["theme"] for x in themes[:3]) if themes else "—"
    summary_text = (
        f"近 {days} 天 {len(rows)} 份券商研报，覆盖 {total} 只个股。"
        f"先说清一个口径：券商研报天然偏多（{round(n_bull / max(total, 1) * 100)}% 的票都拿到买入/增持），"
        f"负面报告极少公开发，所以这张表比的是**相对强弱**，不是「看多=会涨」。"
        f"按六因子（一致看多 20 / 目标价空间 25 / 覆盖广度 12 / 研报新鲜度 13 / 预测估值 8 / 主线热度 10）"
        f"排序：{gs.get('S', 0)} 只核心机会、{gs.get('A', 0)} 只短线进攻、{gs.get('B', 0)} 只中线埋伏、"
        f"{n_avoid} 只回避。本期研报最集中的方向是「{main_line}」，优先在主线里挑。"
    )
    if top3:
        summary_text += " 目前最靠前的三个："
        summary_text += "；".join(
            f"{p['rank']}. {p['name'] or p['code']}（{p['code']} {p['grade']} 级 {p['score']} 分）"
            for p in top3) + "。"
    if n_avoid:
        summary_text += f" 另有 {n_avoid} 只机构偏空或证据不足，列在「回避名单」。"

    # 只给前 50 条非回避 + 前 15 条回避，避免前端一次性渲染 500 行
    nond = [p for p in picks if p["grade"] != "D"]
    avoid = [p for p in picks if p["grade"] == "D"]
    front = [dict(p) for p in (nond[:50] + avoid[:15])]

    return {
        "ok": True,
        "generated": datetime.datetime.now().strftime("%Y-%m-%d %H:%M"),
        "window_days": days,
        "total_reports": len(rows),
        "hits": hits,
        "n_stocks": total,
        "themes": themes,
        "theme_top_code": theme_pick,
        "dist_grade": gs,
        "summary_text": summary_text,
        "picks": front,
        "n_front": len(front),
        "cost_sec": round(time.time() - t0, 1),
    }


def insight(days: int = 60, force: bool = False) -> dict:
    """带缓存读取；缓存过期则重新计算（3 小时 TTL，研报非实时数据）。"""
    with _LOCK:
        if not force:
            try:
                if time.time() - os.path.getmtime(CACHE_PATH) < TTL_SEC:
                    with open(CACHE_PATH, "r", encoding="utf-8") as f:
                        d = json.load(f)
                    if d.get("ok"):
                        d["cached"] = True
                        return d
            except Exception:
                pass
        try:
            d = build(days=days)
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "reason": f"聚合失败：{e}"}
        try:
            os.makedirs(os.path.dirname(CACHE_PATH), exist_ok=True)
            with open(CACHE_PATH, "w", encoding="utf-8") as f:
                json.dump(d, f, ensure_ascii=False)
        except Exception:
            pass
        d["cached"] = False
        return d
