# -*- coding: utf-8 -*-
"""现成策略模板（P5，2026-10-26）。

吸收 InStock(myhhub/stock) 的选股思路，落地为基于【本地已算好的因子 + 全宇宙扫描】的现成模板。
每个模板是一条对 stock_indicators（P1 因子快照，已剔除干净宇宙垃圾）的筛选 SQL，秒级覆盖全市场。
优先级参考 P4 回测实证 edge：均值回归/超卖类（RSI6/CCI/WR 超卖）已被证实有正 alpha，排在最前。

模板（均在干净宇宙 5243 只内筛选）：
  oversold_rebound   —— 超卖反弹（均值回归，P4 正 alpha 最强）
  ma_bull_array      —— 均线多头排列（close>ma20>ma60>ma200 且 macd>0）
  supertrend_long    —— Supertrend 多头（趋势跟踪，P4 显示弱 alpha，标注谨慎）
  vol_breakout       —— 放量站上均线上（量价配合）
  quality_leaders    —— 质量龙头（来自 universe_scan 质量分 + A/B 档）

落库 stock_strategy_picks(template,code,name,score,reasons,updated) + 控制台摘要。
用法：.venv/Scripts/python.exe -m viewer.strategy_templates
"""
import os
import sys
import time
import json
import sqlite3

try:
    from viewer.config import signal_weight  # P4 实证信号权重（SIGNAL_WEIGHTS）
except Exception:  # noqa: BLE001
    def signal_weight(name):  # 兜底：config 不可用时返回未回测
        return 0.0, False, "未回测"

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

# 每个模板：key / 名称 / 说明 / 风险标注 / SQL（从 stock_indicators 选 code,name；quality_leaders 需 join）
# alpha_signals：该模板所依赖的「技术指标信号」，对应 viewer/config.SIGNAL_WEIGHTS 的键；
#   用于在 P4 全量回测实证下，给模板标注净 alpha 方向（正=优先 / 负=降权谨慎）。
TEMPLATES = [
    {
        "key": "oversold_rebound", "name": "超卖反弹（均值回归）",
        "desc": "RSI6<30 且 WR6<-80 且 CCI<-100：严重超卖后的均值回归，P4 回测正向 alpha 最强。",
        "risk": "低-中（反弹失败可能继续下探，需结合大盘环境）",
        "sql": ("SELECT code, name, (30-rsi6)+(-80-wr6)+(-100-cci) AS score FROM stock_indicators "
                "WHERE rsi6 < 30 AND wr6 < -80 AND cci < -100 AND close > 0"),
        "reasons": ["RSI6<30", "WR6<-80", "CCI<-100"],
        "alpha_signals": ["RSI6超卖", "跌破BOLL下轨", "KDJ超卖金叉", "CCI超卖"],
    },
    {
        "key": "ma_bull_array", "name": "均线多头排列",
        "desc": "close>ma20>ma60>ma200 且 macd>0：中长期多头趋势，趋势跟随。",
        "risk": "中（高位追涨回撤风险）",
        "sql": ("SELECT code, name, (close/ma200-1)*100 AS score FROM stock_indicators "
                "WHERE close > ma20 AND ma20 > ma60 AND ma60 > ma200 AND macd > 0 AND ma200 > 0"),
        "reasons": ["close>ma20>ma60>ma200", "MACD>0"],
        "alpha_signals": ["MACD零上金叉区"],
    },
    {
        "key": "supertrend_long", "name": "Supertrend 多头（趋势跟踪·弱）",
        "desc": "close>Supertrend 且 macd>0。⚠ P4 回测显示该类趋势跟随 alpha 偏弱，谨慎使用。",
        "risk": "中-高（趋势反转即止损）",
        "sql": ("SELECT code, name, (close-supertrend)/supertrend*100 AS score FROM stock_indicators "
                "WHERE close > supertrend AND macd > 0 AND supertrend > 0"),
        "reasons": ["close>Supertrend", "MACD>0"],
        "alpha_signals": ["股价在Supertrend上"],
    },
    {
        "key": "vol_breakout", "name": "放量站上均线",
        "desc": "volume_ratio>2 且 close>ma20：量价配合的短线突破。",
        "risk": "中（放量可能为出货）",
        "sql": ("SELECT code, name, volume_ratio AS score FROM stock_indicators "
                "WHERE volume_ratio > 2 AND close > ma20 AND volume_ratio > 0"),
        "reasons": ["量比>2", "close>ma20"],
        "alpha_signals": ["放量"],
    },
    {
        "key": "quality_leaders", "name": "质量龙头",
        "desc": "来自全宇宙扫描的质量分>0.6 且档位 A/B：低波动、RSI 健康、偏离小的稳健标的。",
        "risk": "低（偏防御，弹性小）",
        "sql": ("SELECT s.code, s.name, u.quality AS score FROM stock_indicators s "
                "JOIN stock_universe_scan u ON u.code=s.code "
                "WHERE u.quality > 0.6 AND u.tier IN ('A','B')"),
        "reasons": ["质量分>0.6", "档位A/B"],
        "alpha_signals": [],  # 不依赖技术信号，靠 universe_scan 质量分，净 alpha 中性
    },
]


def alpha_evidence(tpl):
    """基于 viewer/config.SIGNAL_WEIGHTS 给模板标注 P4 实证净 alpha 方向与依据。"""
    keys = tpl.get("alpha_signals") or []
    ev = []
    net = 0.0
    for k in keys:
        w, inv, note = signal_weight(k)
        ev.append({"signal": k, "weight": w, "invert": inv, "note": note})
        net += w
    if not keys:
        verdict = "中性（不依赖技术信号，靠质量分稳健筛选）"
    elif net > 0:
        verdict = "正alpha（P4实证该方向可信，优先考虑）"
    elif net < 0:
        verdict = "负alpha（P4实证该方向在本环境失效，降权/谨慎）"
    else:
        verdict = "中性"
    return {"verdict": verdict, "net_weight": round(net, 2), "signals": ev}


# 模块加载即预计算（TEMPLATES 自带 alpha，server 直接取用，无需重复计算）
for _t in TEMPLATES:
    _t["alpha"] = alpha_evidence(_t)


def ensure_tables(con: sqlite3.Connection):
    con.execute("""CREATE TABLE IF NOT EXISTS stock_strategy_picks (
        template TEXT NOT NULL, code TEXT NOT NULL, name TEXT,
        score REAL, reasons TEXT, updated TEXT,
        PRIMARY KEY (template, code))""")
    con.commit()


def run(con=None, asof: str = None):
    own = con is None
    if con is None:
        con = sqlite3.connect(os.environ.get("VIEWER_DB") or os.path.join(ROOT, "viewer", "app.db"))
    ensure_tables(con)
    t0 = time.time()
    updated = asof or ""
    total = 0
    summary = []
    for tpl in TEMPLATES:
        try:
            rows = con.execute(tpl["sql"]).fetchall()
        except Exception as e:  # noqa: BLE001
            print("[strategy_templates] %s 查询失败: %s" % (tpl["key"], e), flush=True)
            rows = []
        reasons = json.dumps(tpl["reasons"], ensure_ascii=False)
        recs = [(tpl["key"], code, name, round(float(score) if score is not None else 0.0, 4), reasons, updated)
                for code, name, score in rows]
        # 先清旧再插新（保证模板结果最新）
        con.execute("DELETE FROM stock_strategy_picks WHERE template=?", (tpl["key"],))
        if recs:
            con.executemany(
                "INSERT OR REPLACE INTO stock_strategy_picks"
                "(template,code,name,score,reasons,updated) VALUES (?,?,?,?,?,?)", recs)
        total += len(recs)
        summary.append((tpl["key"], tpl["name"], len(recs), tpl["risk"]))
        print("[strategy_templates] %-18s %-4d 只  %s" % (tpl["key"], len(rows), tpl["name"]), flush=True)
    con.commit()
    if own:
        con.close()
    print("[strategy_templates] 共 %d 条命中，用时 %.1fs" % (total, time.time() - t0), flush=True)
    return {"ok": True, "total": total, "secs": round(time.time() - t0, 1), "summary": summary}


if __name__ == "__main__":
    run()
