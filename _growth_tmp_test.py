# 临时校验脚本：直接验证 growth_adjust_log_payload 真实逻辑（不依赖完整 server）
import sys, sqlite3
sys.path.insert(0, r'F:/AI Agent部署知识库/2026-09-22-11-49-42/stock-forum-scraper')
import viewer.predictor as P

con = sqlite3.connect(":memory:")
con.row_factory = sqlite3.Row
P.ensure_tables(con)  # 建全部表（含 knowledge_source / growth_notes）

# 1) 空库结构校验
d = P.growth_adjust_log_payload(con)
assert d["ok"] is True
assert isinstance(d["signals"], list) and len(d["signals"]) == 12, "signals 应为 12 维"
assert isinstance(d["weights_series"], dict)
assert isinstance(d["current_weights"], dict) and len(d["current_weights"]) == 12
assert isinstance(d["recent_adjusts"], list) and d["recent_adjusts"] == []
assert d["has_data"] is False, "全空库 has_data 应为 False（仅 NULL 权重的记账行不算）"
assert isinstance(d["caliber"]["label"], str)
print("PASS[空库]: signals=12, has_data=%s, current_weights keys=%d" % (d["has_data"], len(d["current_weights"])))

# 2) 注入一条调整记录 + 一个真实权重，验证 has_data / 动态维度
con.execute("INSERT INTO predictor_score (signal_key,kind,name,weight,weight_prev,updated_at) VALUES ('tech','signal','技术面',0.13,NULL,'2026-10-01')")
con.execute("INSERT INTO predictor_adjust_log (log_date,signal_key,kind,weight_before,weight_after,reason) VALUES ('2026-09-01','tech','signal',0.08,0.10,'贝叶斯收缩收敛')")
con.execute("INSERT INTO predictor_adjust_log (log_date,signal_key,kind,weight_before,weight_after,reason) VALUES ('2026-10-01','tech','signal',0.10,0.13,'覆盖度上升')")
d2 = P.growth_adjust_log_payload(con)
assert d2["has_data"] is True
assert "tech" in d2["weights_series"] and len(d2["weights_series"]["tech"]) == 2, "tech 应有 2 个演化点"
assert d2["current_weights"]["tech"] == 0.13
assert len(d2["recent_adjusts"]) == 2 and d2["recent_adjusts"][0]["signal_key"] == "tech"
print("PASS[有数据]: has_data=%s, tech 演化点=%d, 最近调整 top=%s" % (d2["has_data"], len(d2["weights_series"]["tech"]), d2["recent_adjusts"][0]["signal_key"]))

# 3) 隔离表存在且无外键 / 无 JOIN 依赖
tabs = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
for t in ("knowledge_source", "growth_notes"):
    assert t in tabs, "缺失隔离表: " + t
for t in ("knowledge_source", "growth_notes"):
    sql = (con.execute("SELECT sql FROM sqlite_master WHERE name=?", (t,)).fetchone()[0] or "")
    assert "REFERENCES" not in sql.upper(), t + " 含外键（违反零 JOIN）"
for r in con.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name LIKE 'predictor_%'"):
    assert "knowledge_source" not in (r[0] or "") and "growth_notes" not in (r[0] or ""), "predictor 表引用隔离表"
print("PASS[隔离]: knowledge_source / growth_notes 建表成功且无外键依赖")

print("\nALL PASS")
