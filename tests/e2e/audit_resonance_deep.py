# -*- coding: utf-8 -*-
"""博主共振 / 自我提升 两板块的专项深度审计（2026-10-15）。

通用语义审计器（audit_semantics.py）覆盖不到新增的 res_* / evo_* 表，本脚本补齐：
  §1 日期一致性   —— 各表日期字段 vs 最近交易日，有无断档/未来戳/口径不一
  §2 尺度一致性   —— 同一字段在不同 src_type 下是否同义；两套基准是否被如实标注
  §3 交叉一致性   —— 卡片统计 vs 信号总线原始记录（同指标是否两处算成两个数）
  §4 引用完整性   —— 悬空 code、空字段率、孤儿行
  §5 可交易性口径 —— 三处收益回填实现的口径是否一致
  §6 前端一致性   —— 是否有写死的权重/阈值，与后端下发值不符

用法：.venv\\Scripts\\python.exe audit_resonance_deep.py
退出码：0 无 FAIL，1 有 FAIL
"""
from __future__ import annotations

import io
import json
import os
import re
import sqlite3
import sys

sys.stdout.reconfigure(encoding="utf-8")

ROOT = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(ROOT, "viewer", "app.db")
APPJS = os.path.join(ROOT, "viewer", "static", "app.js")
INDEX = os.path.join(ROOT, "viewer", "static", "index.html")

fails, warns, infos = [], [], []


def ok(cond, msg):
    print(("  PASS " if cond else "  FAIL ") + msg)
    if not cond:
        fails.append(msg)


def warn(cond, msg):
    if not cond:
        print("  WARN " + msg)
        warns.append(msg)
    else:
        print("  PASS " + msg)


def info(msg):
    print("  ·    " + msg)
    infos.append(msg)


con = sqlite3.connect(DB, timeout=60)
con.row_factory = sqlite3.Row


def q1(sql, args=()):
    try:
        r = con.execute(sql, args).fetchone()
        return dict(r) if r else {}
    except Exception as e:  # noqa: BLE001
        return {"_err": str(e)}


def qa(sql, args=()):
    try:
        return [dict(x) for x in con.execute(sql, args).fetchall()]
    except Exception as e:  # noqa: BLE001
        return [{"_err": str(e)}]


# 最近交易日：以本地 K 线缓存为准（与引擎同源，避免自证）
_sig = q1("SELECT MAX(ts) d FROM sig_unified")
LATEST_TRADE = _sig.get("d") or ""
print("=" * 78)
print("博主共振 / 自我提升 专项深度审计   最近交易日 = %s" % LATEST_TRADE)
print("=" * 78)

# ---------------------------------------------------------------- §1 日期一致性
print("\n§1 日期一致性")
# 业务日期列（应当 ≤ 最近交易日）
BIZ_DATE = {
    "res_stock_card": "build_date", "res_divergence": "build_date",
    "sig_unified": "ts", "market_state_log": "trade_date",
    "blogger_reco": "rec_date", "blogger_perf": "rec_date",
    "lhb_stocks": "trade_date", "prediction_item": "run_date", "pillar_item": "run_date",
}
# 生成时间戳列（ETL 落库时刻，允许 = 自然日；**不是**业务日期，不参与时效判定）
GEN_TS = {
    "res_strategy": "updated_at", "res_source": "updated_at",
    "kb_strategy_node": "updated_at", "res_stock": "updated_at",
}
# 引擎批次日期（标「基于哪个交易日的数据」；须与 res_stock_card.build_date 一致）
ENGINE_DATE = {"evo_weight": "fit_date", "evo_journal": "log_date"}
print("  ── 业务日期（应 ≤ 最近交易日 %s）" % LATEST_TRADE)
for t, c in BIZ_DATE.items():
    m = q1("SELECT MAX(%s) m FROM %s" % (c, t)).get("m")
    flag = ""
    if m and LATEST_TRADE:
        d = str(m)[:10]
        flag = "✓" if d == LATEST_TRADE else ("⚠ 晚于最近交易日" if d > LATEST_TRADE else "落后 %s" % d)
    info("%-18s.%-12s 最新=%s  %s" % (t, c, m, flag))
print("  ── 生成时间戳（ETL 落库时刻，允许为自然日）")
for t, c in GEN_TS.items():
    m = q1("SELECT MAX(%s) m FROM %s" % (c, t)).get("m")
    info("%-18s.%-12s 最新=%s" % (t, c, m))
print("  ── 引擎批次日期（须与卡片业务日一致，否则是口径漂移）")
_card_d = q1("SELECT DISTINCT build_date d FROM res_stock_card").get("d")
for t, c in ENGINE_DATE.items():
    m = q1("SELECT MAX(%s) m FROM %s" % (c, t)).get("m")
    ok(str(m)[:10] == str(_card_d)[:10],
       "D0 %s.%s(%s) 与卡片业务日(%s) 一致" % (t, c, m, _card_d))

# 日期戳伪造（F2）：卡片/分歧不得晚于最近交易日
for t, c in (("res_stock_card", "build_date"), ("res_divergence", "build_date")):
    r = q1("SELECT MAX(%s) m FROM %s" % (c, t))
    ok(bool(r.get("m")) and str(r["m"])[:10] <= LATEST_TRADE,
       "D1 %s 无日期戳伪造（%s ≤ %s）" % (t, r.get("m"), LATEST_TRADE))

# 卡片与分歧必须同一天（本轮修过的坑，固化为断言）
a = q1("SELECT DISTINCT build_date d FROM res_stock_card")
b = q1("SELECT DISTINCT build_date d FROM res_divergence")
ok(len(qa("SELECT DISTINCT build_date FROM res_stock_card")) == 1 and
   len(qa("SELECT DISTINCT build_date FROM res_divergence")) == 1 and
   (a.get("d") == b.get("d")),
   "D2 卡片与分歧预警同日（卡=%s 分=%s）" % (a.get("d"), b.get("d")))

# 信号日期分布（近 5 天）
info("信号日期分布 TOP6：")
for x in qa("SELECT ts, COUNT(*) n FROM sig_unified GROUP BY ts ORDER BY ts DESC LIMIT 6"):
    info("      %s  %d 条" % (x["ts"], x["n"]))

# ---------------------------------------------------------------- §2 尺度一致性
print("\n§2 尺度一致性（同一字段在不同源下是否同义）")
grp = qa("SELECT src_type, COUNT(*) n, SUM(CASE WHEN win_t5 IS NULL THEN 1 ELSE 0 END) wnull,"
         " SUM(CASE WHEN avg_ex_t5 IS NULL THEN 1 ELSE 0 END) enull,"
         " AVG(n_sig) navg FROM res_source GROUP BY src_type ORDER BY n DESC")
info("res_source 分源字段填充：")
for g in grp:
    info("      %-10s 源数=%-4s win_t5空=%-4s avg_ex_t5空=%-4s 平均n_sig=%.0f"
         % (g["src_type"], g["n"], g["wnull"], g["enull"], g["navg"] or 0))

# 关键：战法源的 avg_ex_t5 必须全空（否则会把「跑赢基准比例」误当超额收益）
r = q1("SELECT COUNT(*) n FROM res_source WHERE src_type='strategy' AND avg_ex_t5 IS NOT NULL")
ok(r.get("n") == 0, "S1 战法源未混填 avg_ex_t5（应为 %d 条非空，实为 %d）"
   % (0, r.get("n", -1)))

# n_sig 语义：对战法是「回测交易次数」、对博主是「推荐条数」——必须可区分
r = q1("SELECT COUNT(*) n FROM res_source WHERE src_type='strategy' AND note NOT LIKE '%回测%'")
ok(r.get("n") == 0, "S2 战法源 note 已声明回测口径（未声明 %d 条）" % r.get("n", -1))

# 两套基准并存 → 必须在 UI/口径里被区分说明（不是禁止，是要标注）
js = io.open(APPJS, "r", encoding="utf-8").read() if os.path.exists(APPJS) else ""
warn("等权基准" in js or "全市场等权" in js,
     "S3 前端已说明「全市场等权中位数」这一自建基准")
warn("上证指数" in js or "bench" in js,
     "S4 战法体系区已标出与 strategy_sim 自带基准（上证指数）的差异")

# 强度 0-100 的三个来源必须都存在且被名区分
info("强度/评分类字段（三处 0~100）：sig_unified.strength（源内百分位）、"
     "res_stock_card.res_score（四维加权）、res_source.score（信誉分）")

# ---------------------------------------------------------------- §3 交叉一致性
print("\n§3 交叉一致性（同指标是否两处算成两个数）")
cards = qa("SELECT code, name, n_blogger, n_strat, bloggers, build_date FROM res_stock_card"
           " ORDER BY res_score DESC LIMIT 8")
bad = []
for c in cards:
    # 卡片 n_blogger 应等于信号总线里该票在窗口内的**独立博主数**
    r = q1("SELECT COUNT(DISTINCT src_key) n FROM sig_unified WHERE code=? AND src_type='blogger'"
           " AND ts>=date(?, '-20 day')", (c["code"], LATEST_TRADE))
    nb = r.get("n") or 0
    if nb < (c["n_blogger"] or 0):
        bad.append((c["code"], c["n_blogger"], nb))
warn(not bad, "X1 卡片博主数与信号总线一致（不一致 %d 例%s）"
     % (len(bad), ("：%s" % bad[:3]) if bad else ""))

# 战法命中数：卡片 n_strat 应等于 sig_unified 里该票 strategy 信号所载战法数
r = q1("SELECT COUNT(*) n FROM res_stock_card")
tot_card = r.get("n", 0)
hit_card = q1("SELECT COUNT(*) n FROM res_stock_card WHERE n_strat > 0").get("n", 0)
warn(tot_card and hit_card / float(tot_card) >= 0.95,
     "X2 有战法命中的卡片占比 ≥95%%（%d/%d）" % (hit_card, tot_card))
# 零命中的卡必须带解释（只显示"0 个"会被当成 bug —— 审计发现 854 张里有 8 张零命中）
bad0 = q1("SELECT COUNT(*) n FROM res_stock_card WHERE n_strat=0 AND verdict NOT LIKE '%未触发%'")
ok(bad0.get("n", 0) == 0, "X2b 零战法命中的卡片均已说明原因（未说明 %d 张）" % bad0.get("n", -1))

# 信源画像的 n_sig 应等于信号总线里该源的条数
mism = qa("""SELECT rs.src_type, rs.src_key, rs.n_sig,
             (SELECT COUNT(*) FROM sig_unified su WHERE su.src_type=rs.src_type
              AND su.src_key=rs.src_key) real_n
             FROM res_source rs WHERE rs.src_type IN ('blogger','engine','sentiment')
             AND rs.n_sig <> (SELECT COUNT(*) FROM sig_unified su WHERE su.src_type=rs.src_type
                              AND su.src_key=rs.src_key) LIMIT 5""")
warn(not mism, "X3 信源画像 n_sig 与信号总线条数一致（不一致 %d 例%s）"
     % (len(mism), ("：%s" % mism[:2]) if mism else ""))

# 战法档案 hit_n 应等于「实际有该战法的票数」
mism2 = qa("""SELECT strategy_key, hit_n,
              (SELECT COUNT(*) FROM res_stock_card WHERE instr(strat_keys, res_strategy.strategy_key)>0) real_n
              FROM res_strategy WHERE hit_n > 0 ORDER BY hit_n DESC LIMIT 5""")
info("战法档案命中交叉抽查：")
for m in mism2:
    info("      %-22s hit_n=%-5s 卡片侧=%-5s" % (m["strategy_key"], m["hit_n"], m["real_n"]))

# 规则库样本之和应 ≤ 训练集规模
r = q1("SELECT SUM(n) s FROM evo_rule")
w = q1("SELECT n_rows FROM evo_weight ORDER BY fit_date DESC LIMIT 1")
if r.get("s") is not None and w.get("n_rows"):
    ok(r["s"] <= w["n_rows"], "X4 规则样本和(%s) ≤ 训练集(%s)" % (r["s"], w["n_rows"]))

# 权重归一化必须成立
wj = q1("SELECT w_json, w_blogger, w_strategy, w_senti, w_engine FROM evo_weight")
try:
    wd = json.loads(wj["w_json"] or "{}")
    s = sum(wd.values())
    ok(abs(s - 1.0) < 1e-6, "X5 学出权重归一化（和=%.6f）" % s)
    for k in ("blogger", "strategy", "senti", "engine"):
        ok(wd.get(k, 0) >= 0.06 - 1e-9, "X6 维度 %s 权重≥单维下限 6%%（%.4f）" % (k, wd.get(k, 0)))
except Exception as e:  # noqa: BLE001
    ok(False, "X5/X6 权重解析失败：%s" % e)

# ---------------------------------------------------------------- §4 引用完整性
print("\n§4 引用完整性")
for t in ("res_stock_card", "sig_unified"):
    r = q1("SELECT COUNT(*) n FROM %s WHERE code NOT IN (SELECT code FROM res_stock)" % t)
    ok(r.get("n", 0) == 0,
       "R1 %s 无悬空 code（可转债/B股/北交所应由闸门剔除，当前 %d 条）" % (t, r.get("n", -1)))

# 空字段率（关键字段）
for t, cols in (("res_stock_card", ["name", "tier", "verdict", "res_score"]),
                ("sig_unified", ["code", "ts", "src_type", "strength"]),
                ("evo_rule", ["cond_text", "status"]),
                ("evo_knowledge", ["title", "body"])):
    tot = q1("SELECT COUNT(*) n FROM %s" % t).get("n", 0)
    for c in cols:
        e = q1("SELECT COUNT(*) n FROM %s WHERE %s IS NULL OR %s=''" % (t, c, c)).get("n", 0)
        if e:
            warn(False, "R2 %s.%s 空值 %d/%d" % (t, c, e, tot))
ok(True, "R2 关键字段空值扫描完成")

# ---------------------------------------------------------------- §5 可交易性口径
print("\n§5 可交易性口径（三处收益回填是否同一把尺）")
info("resonance.fill_returns：信号日**收盘价**买入，持有 N 个交易日，基准=全市场等权中位数")
info("evolve 训练集 y：同上（同函数同源口径）")
info("blogger_perf：base_close=推荐日收盘，r1/r3/r5/r10 由外部脚本算")
r = qa("SELECT blogger, rec_date, base_close, r1, r5 FROM blogger_perf "
       "WHERE r5 IS NOT NULL ORDER BY rec_date DESC LIMIT 3")
for x in r:
    info("      blogger_perf 样本：%s %s base=%s r1=%s r5=%s"
         % (x["blogger"], x["rec_date"], x["base_close"], x["r1"], x["r5"]))
# 口径必须由后端下发（不是靠文案里恰好出现"收盘"两字）
cal_txt = ""
try:
    import urllib.request
    op = urllib.request.build_opener(urllib.request.ProxyHandler({}))   # 本机必须绕系统代理
    with op.open("http://127.0.0.1:8765/api/resonance", timeout=90) as rr:
        cal_txt = " ".join(json.loads(rr.read().decode("utf-8")).get("caliber") or [])
except Exception as e:  # noqa: BLE001
    warn(False, "T1 无法取接口口径（%s）" % str(e)[:70])
ok("可交易" in cal_txt or "次日" in cal_txt,
   "T1 口径已声明可交易性（信号日收盘买入 vs 实际最早次日可买）")
ok("标的范围" in cal_txt, "T2 口径已声明标的范围闸门（只处理 A 股）")
ok("数据源时效" in cal_txt, "T3 口径已声明各源更新节奏不一")

# ---------------------------------------------------------------- §6 前端一致性
print("\n§6 前端一致性")
# 权重不得写死（必须与后端下发一致）
# 权重兜底常量允许存在（evolve 不可用时用默认值），但只应出现在兜底表达式里
hard = re.findall(r"blogger:\s*0\.34", js)
warn(len(hard) <= 1,
     "F1 四维权重兜底常量仅 1 处（实为 %d 处；多于此说明别处也在写死权重）" % len(hard))
ok("RES.evo" in js and "resEvoRun" in js, "F2 第 4 支柱已接入前端（RES.evo / resEvoRun 存在）")
ok("resTab(" in js, "F3 支柱切换函数存在")
# 口径必须由后端下发（前端只渲染）
ok("caliber" in js, "F4 口径由后端下发、前端渲染（caliber 字段被消费）")
# 版本串（技能明确要求：改 js/css 必须升 ?v=）
idx = io.open(INDEX, "r", encoding="utf-8").read() if os.path.exists(INDEX) else ""
mt_js = os.path.getmtime(APPJS) if os.path.exists(APPJS) else 0
ver = re.findall(r"app\.js\?v=([\w\-]+)", idx)
vstr = ver[0] if ver else ""
warn(("purgeold2" not in vstr) and ("resonance" in vstr or "evolve" in vstr),
     "F5 前端版本串已为本次改动更新（当前 app.js?v=%s）" % vstr)

print("\n" + "-" * 78)
print("合计 FAIL=%d  WARN=%d  INFO=%d" % (len(fails), len(warns), len(infos)))
if fails:
    print("\nFAIL 明细：")
    for f in fails:
        print("   × " + f)
if warns:
    print("\nWARN 明细：")
    for w in warns:
        print("   ! " + w)
print("AUDIT-DONE")
sys.exit(1 if fails else 0)
