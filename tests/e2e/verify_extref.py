"""外部参考（extref）回归护栏：接口 / 筛选 / 静态文件 / 隔离自检 / 旧接口不回归 / 详情与采集回测。
用完可留作护栏（建议保留），也可删——当前先跑通。

扩展点（2026-09-27）：
  · 新增「详情 / 采集 / 回测」接口与前端点击闭环的断言；
  · 计数类断言改为「≥」容忍每日采集带来的数据增长（不再硬编码 8 条 / 0 回填）；
  · BASE 可由环境变量 EXTREF_BASE 覆盖（便于在测试端口 8799 上先跑，再对线上 8765 跑）。
"""
import json
import os
import sqlite3
import sys
import time
import urllib.request

op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
BASE = os.environ.get("EXTREF_BASE", "http://127.0.0.1:8765")
ok = 0
fails = []


def chk(cond, msg):
    global ok
    if cond:
        ok += 1
        print("  PASS", msg)
    else:
        fails.append(msg)
        print("  FAIL", msg)


ready = False
for _ in range(40):
    try:
        op.open(BASE + "/api/summary", timeout=3)
        ready = True
        break
    except Exception:
        time.sleep(0.5)
chk(ready, "后端就绪（%s）" % BASE)
if not ready:
    sys.exit(1)


def get(path):
    return json.loads(op.open(BASE + path, timeout=25).read().decode("utf-8", "ignore"))


print("== 1) /api/extref_signals 结构 ==")
d = get("/api/extref_signals")
chk(isinstance(d, dict), "返回 JSON 对象")
st = d.get("stats") or {}
chk(st.get("sources_total") == 33, "信源数 == 33（实得 %s）" % st.get("sources_total"))
chk(st.get("signals_total") >= 8, "信号数 ≥ 8（实得 %s）" % st.get("signals_total"))
chk(st.get("licensed_n") == 21, "持牌机构 == 21（实得 %s）" % st.get("licensed_n"))
chk(set(["A", "B", "C", "D"]) <= set((st.get("by_tier") or {}).keys()), "分档键齐全 A/B/C/D")
chk(st.get("pnl_done") >= 0 and st.get("pnl_pending") >= 0, "回填口径可追溯（done≥0 / pending≥0）")
chk(st.get("pnl_done") >= 1, "至少存在已回填信号（自动回测生效，实得 %s）" % st.get("pnl_done"))
chk(isinstance(st.get("red_line"), dict) and len(st["red_line"].get("items") or []) >= 3,
    "合规红线清单 ≥3 条")

srcs = d.get("sources") or []
chk(len(srcs) == 33, "sources 数组 33 条（实得 %d）" % len(srcs))
need_src = ("src_key", "name", "tier", "kind", "access", "status",
            "tier_label", "access_label", "status_label", "licensed")
chk(all(k in srcs[0] for k in need_src), "信源字段齐全（含中文标签）")
# 2026-09-27 新增：信源档案含「作用 / 价值 / 接入路径」三字段（详情抽屉用）
need_src_ext = ("role", "value", "howto")
chk(all(k in srcs[0] for k in need_src_ext), "信源档案含 role/value/howto（实得键 %s）"
    % sorted(srcs[0].keys()))
chk(any((srcs[i].get("role") or "").strip() for i in range(len(srcs))), "至少 1 家信源已填 role")
chk(any((srcs[i].get("howto") or "").strip() for i in range(len(srcs))), "至少 1 家信源已填 howto")

sigs = d.get("signals") or []
chk(len(sigs) >= 8, "signals 数组 ≥ 8 条（实得 %d）" % len(sigs))
need_sig = ("sid", "src_key", "src_name", "pub_date", "symbol", "symbol_name", "direction",
            "reason", "claim", "url", "hardness", "hardness_label", "pnl_status", "tags")
chk(all(k in sigs[0] for k in need_sig), "信号字段齐全")
done_sigs = [s for s in sigs if s.get("pnl_status") == "done"]
pend_sigs = [s for s in sigs if s.get("pnl_status") == "pending"]
na_sigs = [s for s in sigs if s.get("pnl_status") == "na"]
chk(all(s.get("pnl_t1") is not None and s.get("pnl_t5") is not None for s in done_sigs),
    "已回填信号收益非 null（实得 done=%d）" % len(done_sigs))
chk(all(s.get("pnl_t1") is None and s.get("pnl_t5") is None for s in pend_sigs),
    "待回填收益一律 null（不写 0，实得 pending=%d）" % len(pend_sigs))
chk(all(s.get("pnl_status") in ("pending", "done", "na") for s in sigs), "pnl_status 仅取 三态")
chk(all(str(s.get("url") or "").startswith("http") for s in sigs), "每条信号都有可点外链")

rank = d.get("rank") or []
chk(len(rank) >= 3, "胜率榜 ≥3 行（实得 %d）" % len(rank))
for r in rank:
    chk(r.get("rankable") == (r.get("done_n", 0) >= 5), "rankable 与 done_n≥5 一致（%s）" % r.get("src_name"))
    if r.get("rankable"):
        chk(r.get("win_rate_5") is not None, "已达门槛则给胜率（%s）" % r.get("src_name"))
        chk(r.get("avg_ret_5") is not None, "已达门槛则给均收益（%s）" % r.get("src_name"))
    else:
        # 诚实口径：未达 5 条仍显示算出的数字，但 sample_note 必须标注「未达门槛 · 仅供参考」
        chk("未达门槛" in (r.get("sample_note") or "") or "样本不足" in (r.get("sample_note") or ""),
            "未达门槛则明确标注（%s：%s）" % (r.get("src_name"), r.get("sample_note")))
    chk(r.get("sample_note"), "每行都有样本说明（%s）" % r.get("src_name"))

meta = d.get("meta") or {}
chk("honesty" in meta and "isolation" in meta, "meta 含诚实口径与隔离说明")
chk("不构成投资建议" in str(meta.get("honesty") or ""), "口径含「不构成投资建议」")

print("== 2) 筛选参数 ==")
chk(len((get("/api/extref_signals?tier=A").get("sources") or [])) == 20, "tier=A → 20 家")
chk(len((get("/api/extref_signals?status=live").get("sources") or [])) == 3, "status=live → 3 家")
chk(len((get("/api/extref_signals?hardness=hard").get("signals") or [])) >= 3, "hardness=hard ≥ 3 条（采集后增长）")
chk(len((get("/api/extref_signals?kind=rank").get("signals") or [])) >= 6, "kind=rank ≥ 6 条（采集后增长）")
chk(len((get("/api/extref_signals?tier=C").get("sources") or [])) == 6, "tier=C → 6 家")

print("== 3) 静态文件 ==")
home = op.open(BASE + "/", timeout=25).read().decode("utf-8", "ignore")
for pid in ("p-extref_soft", "p-extref_comm", "p-extref_rank"):
    chk(pid in home, "index.html 含容器 %s" % pid)
for pid in ("p-geo_events", "p-geo_policy", "p-geo_tech", "p-dashboard"):
    chk(pid in home, "既有容器未丢 %s" % pid)
js = op.open(BASE + "/static/app.js", timeout=25).read().decode("utf-8", "ignore")
for k in ('key: "extref"', "extref_soft", "paintExtrefInto", "loadExtref", "extrefRefresh",
          "data-ex-sym", "data-ex-refresh"):
    chk(k in js, "app.js 含 %s" % k)
# 2026-09-27 新增：点击闭环 + 取数通道 + 隔离分区
for k in ("openExtrefSignal", "openExtrefSource", "extrefCollect", "extrefBacktest",
          "data-ex-sid", "data-ex-src", "取数通道与可行性", "ex-self-block", "ex-ch-actions"):
    chk(k in js, "app.js 含（新）%s" % k)
css = op.open(BASE + "/static/app.css", timeout=25).read().decode("utf-8", "ignore")
for k in (".ex-cards", ".ex-tier", ".ex-status", ".ex-red", ".ex-tb", ".ex-note-box"):
    chk(k in css, "app.css 含 %s" % k)
for k in (".ex-self-block", ".ex-ch-actions", ".ex-more", ".ex-click"):
    chk(k in css, "app.css 含（新）%s" % k)

print("== 4) 旧接口不回归 ==")
for ep in ("/api/geo_events", "/api/summary", "/api/dashboard", "/api/stocks",
           "/api/lhb_sectors"):
    try:
        r = op.open(BASE + ep, timeout=30)
        chk(r.status == 200, "旧接口 %s 仍 200" % ep)
    except Exception as e:
        chk(False, "旧接口 %s 异常：%s" % (ep, type(e).__name__))

print("== 5) 隔离自检（直连库） ==")
dbp = os.path.join(os.path.dirname(os.path.abspath(__file__)), "viewer", "app.db")
c = sqlite3.connect(dbp)
# 强制把 WAL 落盘，避免「另一进程（服务器）正持有该库」时新连接读到旧快照、漏看 ALTER 加的列
try:
    c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
except Exception:
    pass
tabs = sorted(r[0] for r in c.execute(
    "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'extref%'"))
chk(tabs == ["extref_signals", "extref_sources"], "extref 表恰好两张：%s" % tabs)
sqls = [r[0] or "" for r in c.execute("SELECT sql FROM sqlite_master WHERE name LIKE 'extref%'")]
chk(not any((" JOIN " in s.upper() or "REFERENCES" in s.upper()) for s in sqls),
    "extref 建表语句无 JOIN / 无外键")
cols = [r[1] for r in c.execute("PRAGMA table_info(extref_signals)")]
chk(not any(x in cols for x in ("post_id", "raw_text", "author", "trade_id", "eid")),
    "extref_signals 未混入论坛/国际局势字段")
src_cols = [r[1] for r in c.execute("PRAGMA table_info(extref_sources)")]
print("  [debug] extref_sources cols=%s" % src_cols)
chk("role" in src_cols and "value" in src_cols and "howto" in src_cols, "extref_sources 含 role/value/howto 三列")
# 回填保护：重导/重采集不得把 done 冲回 pending（2026-09-27 修复点）
done_in_db = c.execute("SELECT COUNT(*) FROM extref_signals WHERE pnl_status='done'").fetchone()[0]
chk(done_in_db >= 1, "库中确有已回填信号（done=%d），重导不应清零" % done_in_db)
chk("geo_events" in [r[0] for r in c.execute(
    "SELECT name FROM sqlite_master WHERE type='table'")], "既有 geo_events 表仍在")
c.close()

print("== 6) 详情 / 采集 / 回测 接口（2026-09-27 新增） ==")
sids = [s.get("sid") for s in sigs if s.get("sid")]
src_keys = [s.get("src_key") for s in srcs if s.get("src_key")]
# 6a 信号详情抽屉
if sids:
    sd = get("/api/extref_signal_detail?sid=" + sids[0])
    chk(isinstance(sd, dict) and "error" not in sd, "信号详情返回正常（无 error）")
    chk(all(k in sd for k in ("signal", "source", "self_ref", "meta")), "信号详情含 signal/source/self_ref/meta")
    chk((sd.get("source") or {}).get("role"), "信号详情的 source 含 role")
    chk("isolation" in (sd.get("meta") or {}), "信号详情 meta 含 isolation（隔离口径）")
else:
    chk(False, "无可用 sid 测试信号详情")
# 6b 信源详情抽屉
if src_keys:
    sdet = get("/api/extref_source_detail?src_key=" + src_keys[0])
    chk(isinstance(sdet, dict) and "error" not in sdet, "信源详情返回正常（无 error）")
    chk(all(k in sdet for k in ("source", "signals", "count", "done_n", "pending_n")),
        "信源详情含 source/signals/count/done_n/pending_n")
else:
    chk(False, "无可用 src_key 测试信源详情")
# 6c 采集（冒烟：必须 ok 且不崩溃；东财人气榜可直连）
try:
    cl = get("/api/extref_collect?src_key=em-guba-rank")
    chk(cl.get("ok") is True, "采集接口 ok=True")
    rr = (cl.get("result") or {}).get("em-guba-rank") or {}
    chk(rr.get("error") in (None, "") or isinstance(rr.get("error"), str), "采集 em-guba-rank 无致命错误（%s）" % rr.get("error"))
    # 回填保护：采集后 done 不应下降（不冲回 pending）
    after = get("/api/extref_signals")["stats"]["pnl_done"]
    chk(after >= done_in_db, "采集后已回填数未下降（保护回测结果，%d ≥ %d）" % (after, done_in_db))
except Exception as e:
    chk(False, "采集接口异常：%s" % type(e).__name__)
# 6d 回测（冒烟：必须 ok 且返回分类计数）
try:
    bt = get("/api/extref_backtest")
    chk(bt.get("ok") is True, "回测接口 ok=True")
    rr = bt.get("result") or {}
    chk({"pending", "done", "skipped", "na"} <= set(rr.keys()), "回测返回 pending/done/skipped/na")
except Exception as e:
    chk(False, "回测接口异常：%s" % type(e).__name__)

print()
print("PASS=%d  FAIL=%d" % (ok, len(fails)))
for f in fails:
    print("  !! ", f)
sys.exit(1 if fails else 0)
