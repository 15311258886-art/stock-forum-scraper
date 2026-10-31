# -*- coding: utf-8 -*-
"""供应链瓶颈猎手引擎（2026-10-24 进哥拍板）。

把 GitHub 上 Bottleneck Hunter 的「产业链瓶颈选股」方法论落成本机数据驱动的真实工具。
- 纯标准库实现，**无网络依赖**（估值用 KB 内写死的真实快照，界面显式标注 as_of）。
- 绝不编造市值/估值：所有数字来自 bottleneck_kb.json 的 curated 快照。
- 评级与估值红黄绿灯全部由确定性规则计算，沙箱/本机结果一致，便于回测。

对外入口：get_bottleneck(theme=None, top=None)
  ok=true:  返回 {theme_key, theme_name, summary, as_of, disclaimer, layers,
                   bottlenecks:[...], ranking:[...], themes:[...]}
  ok=false: {ok:False, error:"...", themes:[...]}（主题未找到 / 知识库缺失）
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
KB_PATH = ROOT / "data" / "bottleneck_kb.json"

# 6 维判据固定顺序（与 KB 一致）
DIMS = ["concentration", "expansion", "substitutability", "utilization", "demand", "qualification"]
DIM_LABEL = {
    "concentration": "供给集中度",
    "expansion": "扩产周期",
    "substitutability": "替代难度",
    "utilization": "产能利用率",
    "demand": "需求增速",
    "qualification": "客户验证",
}
RATING_RANK = {"S": 0, "A": 1, "B": 2, "C": 3}
SIGNAL_BASE = {"S": 4, "A": 3, "B": 2, "C": 1}


def _load_kb():
    if not KB_PATH.exists():
        return None
    try:
        return json.loads(KB_PATH.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None


def _match_theme(kb, theme):
    if not theme:
        return kb["themes"][0]
    t = str(theme).strip()
    # 1) key 精确
    for th in kb["themes"]:
        if th["key"].lower() == t.lower():
            return th
    # 2) key 包含 / name 包含（模糊）
    for th in kb["themes"]:
        if t.lower() in th["key"].lower() or t in th["name"]:
            return th
    # 3) 首字符中文包含
    for th in kb["themes"]:
        if th["name"].startswith(t) or t in th["name"]:
            return th
    return None


def _rating_of(scores: dict) -> str:
    reds = sum(1 for d in DIMS if scores.get(d))
    if reds >= 4:
        return "S"
    if reds == 3:
        return "A"
    if reds >= 1:
        return "B"
    return "C"


def _valuation(c):
    """对单个候选股计算估值红黄绿灯 + 信号强度。c: KB candidate dict。"""
    cap = c.get("market_cap")
    ps = c.get("ps")
    pe = c.get("pe")
    rev_yoy = c.get("rev_yoy")
    rev_est = c.get("rev_est")
    tam = c.get("tam_est")

    # 市值 / TAM
    cap_vs_tam = round(cap / tam, 3) if (cap and tam) else None

    # 红黄绿灯判定（阈值说明：公司总市值含其他业务，故仅当市值显著超过单环节 TAM 时才视为透支）
    red_reasons = []
    if cap_vs_tam is not None and cap_vs_tam > 0.60:
        red_reasons.append(f"市值已达该环节 TAM 的 {cap_vs_tam*100:.0f}%（>60%，预期已较充分）")
    if ps is not None and ps > 30 and (rev_yoy is None or rev_yoy < 100):
        red_reasons.append(f"PS {ps:.0f}x 但营收增速 {rev_yoy or '?'}% < 100%（估值透支）")
    if pe is not None and pe > 80 and (rev_yoy is None or rev_yoy < 50):
        red_reasons.append(f"PE {pe:.0f}x 且增速偏低，安全边际不足")

    yellow_reasons = []
    if not red_reasons:
        if cap_vs_tam is not None and cap_vs_tam > 0.35:
            yellow_reasons.append(f"市值占该环节 TAM 的 {cap_vs_tam*100:.0f}%（偏高）")
        if ps is not None and ps > 15:
            yellow_reasons.append(f"PS {ps:.0f}x 不便宜")
        if pe is not None and pe > 50:
            yellow_reasons.append(f"PE {pe:.0f}x 偏贵")

    if red_reasons:
        light = "red"
    elif yellow_reasons:
        light = "yellow"
    else:
        light = "green"

    # 信号强度：基础分 - 红灯数，下限 1
    base = None  # 由调用方按评级给
    return {
        "cap_vs_tam": cap_vs_tam,
        "light": light,
        "red_reasons": red_reasons,
        "yellow_reasons": yellow_reasons,
    }


def get_bottleneck(theme=None, top=None):
    """主入口。返回 dict（含 ok 字段）。"""
    kb = _load_kb()
    themes_list = [{"key": t["key"], "name": t["name"]} for t in (kb or {}).get("themes", [])]

    if kb is None:
        return {"ok": False, "error": "知识库 bottleneck_kb.json 缺失", "themes": themes_list}

    th = _match_theme(kb, theme)
    if th is None:
        return {
            "ok": False,
            "error": f"未找到主题「{theme}」，可选：{', '.join(t['name'] for t in kb['themes'])}",
            "themes": themes_list,
        }

    # ── 环节 → 评级 + 瓶颈卡片 ──
    bottlenecks = []
    for sg in th["segments"]:
        scores = sg["scores"]
        rating = _rating_of(scores)
        reds = sum(1 for d in DIMS if scores.get(d))
        bottlenecks.append({
            "name": sg["name"],
            "layer": sg["layer"],
            "rating": rating,
            "red_count": reds,
            "scores": scores,
            "score_notes": sg.get("score_notes", {}),
            "thesis": sg.get("thesis", ""),
            "pro": sg.get("pro", ""),
            "con": sg.get("con", ""),
            "candidates": [c["code"] for c in sg.get("candidates", [])],
        })
    # 按评级 S>A>B，再按红标数降序
    bottlenecks.sort(key=lambda b: (RATING_RANK.get(b["rating"], 9), -b["red_count"]))

    # ── 候选股 → 排名 + 一页纸 ──
    ranking_map = {}
    for sg in th["segments"]:
        rating = _rating_of(sg["scores"])
        for c in sg.get("candidates", []):
            val = _valuation(c)
            signal = max(1, SIGNAL_BASE.get(rating, 1) - len(val["red_reasons"]))
            # 安全边际判定（简化 10 年@25x PE 思想：当前 PE 过高即视为安全边际不足）
            margin_note = "估值合理，安全边际尚可"
            if (c.get("pe") and c["pe"] > 60) or (c.get("ps") and c["ps"] > 20 and (c.get("rev_yoy") or 0) < 60):
                margin_note = "估值偏贵：10 年 @25xPE 退出测算安全边际不足"
            one_pager = {
                "code": c["code"],
                "name": c["name"],
                "segment": sg["name"],
                "rating": rating,
                "signal": signal,
                "light": val["light"],
                "ps": c.get("ps"),
                "pe": c.get("pe"),
                "market_cap": c.get("market_cap"),
                "rev_yoy": c.get("rev_yoy"),
                "cap_vs_tam": val["cap_vs_tam"],
                "red_reasons": val["red_reasons"],
                "yellow_reasons": val["yellow_reasons"],
                "margin_note": margin_note,
                "why_bottleneck": sg.get("thesis", ""),
                "why_this": c.get("note", ""),
                "pro": sg.get("pro", ""),
                "con": sg.get("con", ""),
            }
            # 同代码跨环节去重，保留信号更高的一条
            prev = ranking_map.get(c["code"])
            if prev is None or signal > prev["signal"]:
                ranking_map[c["code"]] = one_pager

    ranking = sorted(ranking_map.values(),
                     key=lambda r: (-r["signal"], RATING_RANK.get(r["rating"], 9), -(r["market_cap"] or 0)))

    if top:
        try:
            top = max(1, int(top))
            ranking = ranking[:top]
        except Exception:  # noqa: BLE001
            pass

    return {
        "ok": True,
        "theme_key": th["key"],
        "theme_name": th["name"],
        "summary": th.get("summary", ""),
        "as_of": kb.get("as_of", ""),
        "disclaimer": kb.get("disclaimer", ""),
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "layers": th.get("layers", []),
        "bottlenecks": bottlenecks,
        "ranking": ranking,
        "themes": themes_list,
    }


# ── 便捷：仅取主题列表（供前端选择器；路由可单独暴露）──
def list_themes():
    kb = _load_kb()
    if not kb:
        return []
    return [{"key": t["key"], "name": t["name"]} for t in kb["themes"]]


if __name__ == "__main__":
    import sys
    arg = sys.argv[1] if len(sys.argv) > 1 else None
    res = get_bottleneck(arg)
    print(json.dumps(res, ensure_ascii=False, indent=2)[:1500])
