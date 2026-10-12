# -*- coding: utf-8 -*-
# qa/verify_growth_backend.py
# 复盘成长面板 · 后端契约 + 空安全 + 隔离机制 + 12维动态 验证（真实执行 predictor.growth_adjust_log_payload）
# 用法：<venv>/python.exe qa/verify_growth_backend.py
import sys, os, re, sqlite3, tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VIEWER = os.path.join(ROOT, "viewer")
for p in (ROOT, VIEWER):
    if p not in sys.path:
        sys.path.insert(0, p)

import predictor  # noqa: E402

# 用临时空库，绝不动真实 app.db
TMP = tempfile.mktemp(suffix=".db")
predictor.DB_PATH = TMP

fails = []
passed = 0


def check(cond, msg):
    global passed
    print(("  PASS: " if cond else "  FAIL: ") + msg)
    if cond:
        passed += 1
    else:
        fails.append(msg)


con = sqlite3.connect(TMP)
con.row_factory = sqlite3.Row
predictor.ensure_tables(con)  # 建全表（含隔离表 knowledge_source / growth_notes，此时为空）

print("[场景1] 空库（knowledge_source / growth_notes 均为空）")
d = predictor.growth_adjust_log_payload(con)
check(d.get("ok") is True, "ok == True")
check(isinstance(d.get("signals"), list) and len(d["signals"]) == 12,
      "signals == 12 维（前端据此动态渲染，不硬编码）")
check(d.get("has_data") is False, "has_data == False（无调整记录且权重为 NULL → 视为无成长证据）")
check(d.get("weights_series") == {}, "weights_series == {}")
check(d.get("recent_adjusts") == [], "recent_adjusts == []")
cw = d.get("current_weights") or {}
check(isinstance(cw, dict) and len(cw) == 12, "current_weights 含 12 个信号键（NULL 权重归 0.0）")
check(all(v == 0.0 for v in cw.values()), "current_weights 全体为 0.0（NULL 不计入真实权重）")
cal = d.get("caliber") or {}
check(isinstance(cal.get("label"), str) and len(cal["label"]) > 0, "caliber.label 为非空字符串")

# 隔离机制：函数不得写入预留隔离表
ks = con.execute("SELECT COUNT(*) c FROM knowledge_source").fetchone()["c"]
gn = con.execute("SELECT COUNT(*) c FROM growth_notes").fetchone()["c"]
check(ks == 0 and gn == 0, "隔离：growth_adjust_log_payload 未写入 knowledge_source / growth_notes")

# 隔离机制（静态）：DDL 无外键 REFERENCES；函数体内无任何 JOIN
src = open(os.path.join(ROOT, "viewer", "predictor.py"), encoding="utf-8").read()
m = re.search(r"CREATE TABLE IF NOT EXISTS knowledge_source \(.*?\);", src, re.S)
check(bool(m) and "REFERENCES" not in m.group(0).upper(), "隔离：knowledge_source DDL 无 REFERENCES 外键")
fm = re.search(r"def growth_adjust_log_payload\([\s\S]*?\n(?=\S)", src)
# 仅判定真正的 SQL 连接（" JOIN " 前后有空格）；注释「零 JOIN」不算
check(bool(fm) and not re.search(r"\sJOIN\s", fm.group(0), re.I),
      "隔离：growth_adjust_log_payload 函数体内无任何 SQL JOIN（注释『零 JOIN』不误判）")

print("[场景2] 注入真实成长数据后")
con.execute("UPDATE predictor_score SET weight=0.12 WHERE signal_key='tech'")
con.execute("UPDATE predictor_score SET weight=0.08 WHERE signal_key='sentiment'")
con.execute("INSERT INTO predictor_adjust_log (log_date,signal_key,kind,weight_before,weight_after,reason) "
            "VALUES ('2026-09-01','tech','signal',0.05,0.08,'覆盖度上升')")
con.execute("INSERT INTO predictor_adjust_log (log_date,signal_key,kind,weight_before,weight_after,reason) "
            "VALUES ('2026-10-01','tech','signal',0.08,0.12,'贝叶斯收敛')")
con.execute("INSERT INTO predictor_adjust_log (log_date,signal_key,kind,weight_before,weight_after,reason) "
            "VALUES ('2026-09-05','sentiment','signal',0.04,0.08,'情绪回暖')")
con.commit()

d2 = predictor.growth_adjust_log_payload(con)
check(d2.get("has_data") is True, "有数据：has_data == True")
check(len(d2["signals"]) == 12, "有数据：signals 仍为 12 维（维度不因数据量变化而漂移）")
ws = d2.get("weights_series") or {}
check("tech" in ws and len(ws["tech"]) == 2, "tech 权重轨迹 2 点")
check(abs(ws["tech"][0]["weight"] - 0.08) < 1e-9 and abs(ws["tech"][1]["weight"] - 0.12) < 1e-9,
      "tech 轨迹按 log_date 升序（0.08 → 0.12）")
check("sentiment" in ws and len(ws["sentiment"]) == 1, "sentiment 轨迹 1 点")
cw2 = d2.get("current_weights") or {}
check(abs(cw2.get("tech", -1) - 0.12) < 1e-9 and abs(cw2.get("sentiment", -1) - 0.08) < 1e-9,
      "current_weights 反映真实权重（tech=0.12, sentiment=0.08）")
check(len(d2.get("recent_adjusts") or []) == 3, "recent_adjusts 含 3 条（按 id 倒序）")
# 动态维数：仅 2 个信号有数据 → 仅 2 条权重线（非硬编码 6 条）
drawable = [k for k, v in ws.items() if len(v) >= 1]
check(len(drawable) == 2, "动态维数：仅 2 个信号有轨迹 → 仅 2 条线（绝不硬编码 6/12）")
# recent_adjusts 结构字段齐全
ra0 = (d2.get("recent_adjusts") or [{}])[0]
check({"id", "log_date", "signal_key", "weight_before", "weight_after", "reason"} <= set(ra0.keys()),
      "recent_adjusts 元素字段齐全（id/log_date/signal_key/weight_before/weight_after/reason）")

con.close()
try:
    os.remove(TMP)
except OSError:
    pass

print()
if fails:
    print("❌ 失败 %d 项：%s" % (len(fails), "; ".join(fails)))
    sys.exit(1)
print("✅ verify_growth_backend 全部通过（共 %d 项断言）" % passed)
