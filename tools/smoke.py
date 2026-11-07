#!/usr/bin/env python
# tools/smoke.py —— 冒烟测试 / CI 健康检查（C 阶段，2026-10-27）
#
# 目标：挡住「改 A 漏 B」类低级错误——这是我们之前反复翻车的地方：
#   - import 路径错（viewer.xxx 漏前缀）
#   - JOBS 字典漏注册（函数写了跑不到）
#   - score 误填 / 字段错
#   - 剔除池被污染（误塞 blogger_noise 等越界理由）
#   - 品牌红线泄露（致锐宇/Zhiruiyu/ZRY）
#
# 用法：python tools/smoke.py
#   exit 0 = 全部通过；exit 1 = 存在失败项（可用于 CI / 提交前门禁）
import os
import re
import sys
import glob
import json
import py_compile
import sqlite3

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "viewer"))

OK = []
FAIL = []


def check(name, cond, detail=""):
    if cond:
        OK.append(name)
        print("  [PASS] %s%s" % (name, ("  -> " + detail) if detail else ""))
    else:
        FAIL.append(name)
        print("  [FAIL] %s%s" % (name, ("  -> " + detail) if detail else ""))


print("=" * 60)
print("SMOKE: 编译 / 配置 / DB 健康 / 干净宇宙 / 红线")
print("=" * 60)

# ---- 1. 编译所有模块 ----
print("\n[1] 编译 viewer/*.py + tools/*.py")
py_files = glob.glob(os.path.join(ROOT, "viewer", "*.py")) + \
           glob.glob(os.path.join(ROOT, "tools", "*.py"))
compile_fail = []
for f in py_files:
    try:
        py_compile.compile(f, doraise=True)
    except Exception as e:  # noqa: BLE001
        compile_fail.append((os.path.basename(f), str(e)))
check("全部模块语法编译通过", not compile_fail,
      "" if not compile_fail else ("失败示例: " + str(compile_fail[:3])))

# ---- 2. 配置常量一致性 ----
print("\n[2] viewer/config.py 常量一致性")
import viewer.config as cfg  # noqa: E402
check("PILLARS_POOL_LIMIT>0", cfg.PILLARS_POOL_LIMIT > 0, "=%d" % cfg.PILLARS_POOL_LIMIT)
check("KLINE_LOOKBACK_DAYS>=200", cfg.KLINE_LOOKBACK_DAYS >= 200, "=%d" % cfg.KLINE_LOOKBACK_DAYS)
check("WINDOW>0", cfg.WINDOW > 0, "=%d" % cfg.WINDOW)
wsum = round(cfg.W_MOMENTUM + cfg.W_VOLUME + cfg.W_QUALITY, 6)
check("三轴权重合计=1.0", abs(wsum - 1.0) < 1e-6, "=%s" % wsum)
check("避坑标记已换源（不再依赖博主流效窗口）", not hasattr(cfg, "BLOGGER_RISK_WINDOW_DAYS"),
      "BLOGGER_RISK_WINDOW_DAYS 应已删除，改由 viewer/avoid.py 实时算")
check("SIGNAL_WEIGHTS 非空", len(cfg.SIGNAL_WEIGHTS) > 0, "=%d 信号" % len(cfg.SIGNAL_WEIGHTS))
check("剔除理由白名单不含 blogger_noise", "blogger_noise" not in cfg.EXCLUSION_REASONS,
      str(cfg.EXCLUSION_REASONS))

# ---- 3. 数据库健康 ----
print("\n[3] 数据库健康（viewer/app.db）")
con = os.environ.get("VIEWER_DB") or sqlite3.connect(os.path.join(ROOT, "viewer", "app.db"))


def cnt(t):
    try:
        return con.execute("SELECT COUNT(*) FROM %s" % t).fetchone()[0]
    except Exception:  # noqa: BLE001
        return -1


check("stock_indicators >=5000", cnt("stock_indicators") >= 5000, "=%d" % cnt("stock_indicators"))
check("stock_universe_scan >=5000", cnt("stock_universe_scan") >= 5000,
      "=%d" % cnt("stock_universe_scan"))
check("stock_strategy_picks >=500", cnt("stock_strategy_picks") >= 500,
      "=%d" % cnt("stock_strategy_picks"))
check("signal_backtest >=17(回测已落地)", cnt("signal_backtest") >= 17,
      "=%d" % cnt("signal_backtest"))

# ---- 4. 干净宇宙 / 剔除池断言 ----
print("\n[4] 干净宇宙强制断言（防再次污染）")
import viewer.universe_filter as uf  # noqa: E402
viol = uf.assert_clean_pool(con=con, strict=False)
check("剔除池无非白名单理由(assert_clean_pool)", len(viol) == 0, "违例=%s" % viol)
excl_reasons = set(r[0] for r in con.execute("SELECT DISTINCT reason FROM exclusion_pool"))
bad_reason = excl_reasons - set(cfg.EXCLUSION_REASONS)
check("剔除池理由全在白名单", len(bad_reason) == 0, "越界=%s" % bad_reason)
st_in_ind = con.execute(
    "SELECT COUNT(*) FROM stock_indicators WHERE name LIKE '%ST%' OR name LIKE '%退%'"
).fetchone()[0]
check("因子表无 ST/退 残留", st_in_ind == 0, "=%d" % st_in_ind)

# ── 2026-10-31 新增（测评 P0-B ⑤ / P1 第 2 条）：剔除池健康 + 各理由空转检测 ──
# 为什么这几条要进 smoke：本轮发现 illiquid 在**白名单里、在前端标签里、在准入函数里**，
# 唯独不在 compute() 里 —— 表里 224 条、illiquid = 0。而"从不触发"比"失败"更危险：
# 失败的断言会产生证据，空转的断言只产生安心。所以它必须被机器盯着，不能靠人想起来。
health = uf.pool_health(force=True)
check("剔除池可用(表在且非空)", bool(health["ok"]), "health=%s" % (health.get("error") or "ok"))
check("剔除池未超龄(<%d天)" % health.get("max_staleness_days", 7),
      not health["stale"],
      "asof=%s age=%s天" % (health.get("pool_asof"), health.get("age_days")))
check("剔除池有批次/写入者可归因",
      bool(health.get("batch_id")) and bool(health.get("writer")),
      "batch=%s writer=%s" % (health.get("batch_id"), health.get("writer")))

# 各理由「连续多少个批次为 0」——只有 history 表能回答，而 history 表是 2026-10-31 才建的。
# 判据：连续 90 批为 0 ⇒ FAIL（约三个月日更）。目前批次少，故该判据当前只在有数据时生效。
for _reason in cfg.EXCLUSION_REASONS:
    _streak = 0
    try:
        _rows = con.execute(
            "SELECT counts_json FROM exclusion_pool_batch ORDER BY pool_asof DESC LIMIT 400"
        ).fetchall()
    except Exception:  # noqa: BLE001
        _rows = []
    for (_cj,) in _rows:
        try:
            _cnt = json.loads(_cj or "{}")
        except Exception:  # noqa: BLE001
            _cnt = {}
        if int(_cnt.get(_reason, 0) or 0) == 0:
            _streak += 1
        else:
            break
    check("理由 %s 非空转(连续空批<90)" % _reason, _streak < 90,
          "当前批次数 %d / by_reason=%s" % (len(_rows), health.get("by_reason")))
con.close()

# ---- 5. 策略模板 alpha 实证背书 ----
print("\n[5] 策略模板带 P4 alpha 背书")
import viewer.strategy_templates as stmod  # noqa: E402
bad_alpha = [t["key"] for t in stmod.TEMPLATES if "net_weight" not in t.get("alpha", {})]
check("所有模板带 net_weight 背书", len(bad_alpha) == 0, "缺=%s" % bad_alpha)

# ---- 6. 红线扫描（品牌露出） ----
# 仅扫描「可能拼字符串泄露的业务代码」，排除治理/元文件：
#   - tools/smoke.py 自身（注释说明红线词）
#   - ARCHITECTURE/DECISIONS/CAPABILITIES.md（治理文档说明红线规则）
# 这些文件提到红线词是「定义规则」，不是「泄露红线」，否则任何说明都会误报。
print("\n[6] 红线扫描（致锐宇 / Zhiruiyu / ZRY · 业务代码）")
EXCLUDE_RED = {"tools/smoke.py", "ARCHITECTURE.md", "DECISIONS.md", "CAPABILITIES.md"}
hits = []
for ext in ("*.py", "*.md"):
    for f in glob.glob(os.path.join(ROOT, "**", ext), recursive=True):
        rel = os.path.relpath(f, ROOT).replace("\\", "/")
        if rel in EXCLUDE_RED or rel.lower().startswith("readme"):
            continue
        try:
            txt = open(f, encoding="utf-8", errors="ignore").read()
        except Exception:  # noqa: BLE001
            continue
        for kw in ("致锐宇", "Zhiruiyu", "ZRY"):
            if kw in txt:
                hits.append((rel, kw))
check("红线零命中(业务代码)", len(hits) == 0, "" if not hits else "命中=%s" % hits[:5])

# ---- 7. 调度注册一致性（JOBS 写了却跑不到 = 静默死任务） ----
# 2026-11-05 自审新增。本文件开头就把「JOBS 字典漏注册（函数写了跑不到）」列为要挡的
# 低级错误，但**从来没有对应的检查** —— 于是它真的发生了：
#   · `plate_members`（ARCHITECTURE §五 明确要跑，填官方板块成员）
#   · `signal_backtest`（ARCHITECTURE §六 明确"已补注册"）
#   两个都只登在 `tools/sched_job.py::JOBS` 里，而 `viewer/server.py::_sched_tasks()`
#   压根没列它们 → 子进程永远等不到调用方，`job_runs` 里零记录，也没人发现。
#
# 根因是**双注册表**：JOBS 说"这个任务有实现"，_sched_tasks 说"它什么时候跑"。
# 两处必须同时登记，而文档口径一直写的是"JOBS 是唯一注册表"—— 文档与实现不符。
# 这条检查把两处拉齐：JOBS 里每个键都必须在 _sched_tasks() 出现过，
# 唯一豁免是被 `viewer/config.DISABLED_SCHED` 显式停用（停用是一份有理由的清单）。
print("\n[7] 调度注册一致性（JOBS ↔ server._sched_tasks）")
_sj = os.path.join(ROOT, "tools", "sched_job.py")
_sv = os.path.join(ROOT, "viewer", "server.py")
jobs_keys, task_keys = set(), set()
try:
    _txt = open(_sj, encoding="utf-8", errors="ignore").read()
    _i = _txt.find("JOBS = {")
    _j = _txt.find("\n}", _i)
    jobs_keys = set(re.findall(r'^\s{4}"([a-z0-9_]+)"\s*:', _txt[_i:_j], re.M))
except Exception as _e:  # noqa: BLE001
    check("能读出 JOBS 注册表", False, str(_e))
try:
    _txt2 = open(_sv, encoding="utf-8", errors="ignore").read()
    # 三种登记写法都算「有入口」，缺一不可：
    #   ① `_sched_tasks()` 的 `("名字", 可调用)` 行；② `_daily_job("名字", (H, M))` 的日更；
    #   ③ 任何位置的 `_spawn_offproc("名字")`（含在别的 _maybe 函数里间接派发的）。
    _k = _txt2.find("def _sched_tasks()")
    _m = _txt2.find("\ndef ", _k + 10)
    task_keys = set(re.findall(r'\(\s*"([a-z0-9_]+)"\s*,', _txt2[_k:_m]))
    task_keys |= set(re.findall(r'_daily_job\(\s*"([a-z0-9_]+)"', _txt2))
    task_keys |= set(re.findall(r'_spawn_offproc\(\s*"([a-z0-9_]+)"', _txt2))
    # ④ 重量级面板：`_HEAVY_CACHE` 的 `"job": "名字"`（由 _heavy_daily_refresh 按 TTL 派发）
    task_keys |= set(re.findall(r'"job"\s*:\s*"([a-z0-9_]+)"', _txt2))
except Exception as _e:  # noqa: BLE001
    check("能读出 server._sched_tasks", False, str(_e))
check("JOBS 注册表非空", len(jobs_keys) >= 25, "=%d 个任务" % len(jobs_keys))
check("_sched_tasks 清单非空", len(task_keys) >= 25, "=%d 个任务" % len(task_keys))
_off = set(getattr(cfg, "DISABLED_SCHED", frozenset()))
_manual = set(getattr(cfg, "MANUAL_ONLY_SCHED", frozenset()))
_orphan = sorted(jobs_keys - task_keys - _off - _manual)
check("JOBS 里每个任务都有调度入口（或已显式声明停用/仅手动）", len(_orphan) == 0,
      "写了但永远跑不到=%s（DISABLED_SCHED=%s MANUAL_ONLY_SCHED=%s）"
      % (_orphan, sorted(_off), sorted(_manual)))
# 反向检查：清单里登记的豁免名必须在 JOBS 里真实存在，防止"清单腐烂"
_stale = sorted((_off | _manual) - jobs_keys)
check("停用/手动清单里的名字都真实存在于 JOBS", len(_stale) == 0, "幽灵条目=%s" % _stale)

# ---- 汇总 ----
print("\n" + "=" * 60)
print("汇总: PASS=%d  FAIL=%d" % (len(OK), len(FAIL)))
if FAIL:
    print("失败项: %s" % FAIL)
    print("SMOKE_FAILED")
    sys.exit(1)
print("SMOKE_OK")
sys.exit(0)
