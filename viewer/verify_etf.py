# -*- coding: utf-8 -*-
"""etf.py 交付护栏（2026-10-17）。

断言：
  1) 模块导入正常（etf / quant / metalearn 均可 import，无语法/循环导入错误）
  2) payload 的 left.type == "A" 且 right.type == "B"（左右分离的关键交付）
  3) 右栏 B 恰好 6 个维度：资金流/行情/跟踪指数 status==已接入(Sina 实时)，其余==待接入(东财不可用)；
     已接入维度带真实 value、待接入维度 value 为 None（绝不编造）
  4) 左栏 A：空宇宙→[] 候选 + 提示；已接入→候选为非空列表且结构完整、权重生效
  5) server.py 含三条新路由字符串：/api/etf /api/etf/status /api/etf/run
  6) etf.py 调用 metalearn.weights_from_ic（权重唯一真源），且未在 etf.py 内重抄权重公式
  7) etf.py 无编造数据（无硬编码 ETF 代码清单 / 无伪造数值）
"""
from __future__ import annotations

import os
import sqlite3
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

FAILURES = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        print("  PASS  %s" % name)
    else:
        print("  FAIL  %s  -> %s" % (name, detail))
        FAILURES.append(name)


# ---- 1) 模块导入 ------------------------------------------------------------
print("[1] 模块导入")
try:
    import metalearn  # noqa: F401
    import quant      # noqa: F401
    import etf        # noqa: F401
    check("import etf / quant / metalearn", True)
except Exception as e:  # noqa: BLE001
    check("import etf / quant / metalearn", False, str(e))
    # 后面都依赖导入，直接收尾
    print("\n失败 %d 项" % len(FAILURES))
    sys.exit(1)


# ---- 2) payload 的 left/right 分离 -----------------------------------------
print("[2] payload 左右分离")
con = sqlite3.connect(":memory:")
try:
    payload = etf.payload(con)
    check("payload.ok", bool(payload.get("ok")), str(payload))
    left = payload.get("left") or {}
    right = payload.get("right") or {}
    check("left.type == 'A'", left.get("type") == "A", "left=%r" % left)
    check("right.type == 'B'", right.get("type") == "B", "right=%r" % right)
    check("payload 含 left/right 两个独立对象", "left" in payload and "right" in payload)
except Exception as e:  # noqa: BLE001
    check("etf.payload 可调用", False, str(e))


# ---- 3) 右栏 B：6 维，部分已接入(Sina)/部分诚实待接入，绝不编造 -----------------
print("[3] 右栏 B · 6 维（已接入 + 待接入）")
try:
    right = (etf.payload(con).get("right") or {})
    dims = right.get("dims") or []
    check("右栏恰好 6 个维度", len(dims) == 6, "实际 %d 个" % len(dims))
    allowed = {"已接入(代理)", "已接入(指数名)", "已接入(估算)", "已接入", "待接入"}
    check("每个维度 status 合法(已接入/待接入)",
          all(d.get("status") in allowed for d in dims),
          "status=%r" % [d.get("status") for d in dims])
    check("至少 1 维已接入（Sina 实时数据已打通）",
          any(d.get("status", "").startswith("已接入") for d in dims))
    check("至少 1 维诚实待接入（东财不可用维度）",
          any(d.get("status") == "待接入" for d in dims))
    # 已接入维度必须有真实 value；待接入维度 value 必为 None（不编造）
    for d in dims:
        if d.get("status", "").startswith("已接入"):
            check("已接入维度[%s] 有真实 value" % d["dim"], d.get("value") is not None)
        else:
            check("待接入维度[%s] value 为 None（未编造）" % d["dim"], d.get("value") is None)
    keys_ok = all({"dim", "key", "value", "status", "note"} <= set(d.keys()) for d in dims)
    check("维度字段完整 {dim,key,value,status,note}", keys_ok)
except Exception as e:  # noqa: BLE001
    check("右栏 B 断言", False, str(e))


# ---- 4) 左栏 A：空宇宙诚实返回 / 已接入则候选结构完整 -----------------------
print("[4] 左栏 A · 候选池诚实返回")
try:
    left = (etf.payload(con).get("left") or {})
    cands = left.get("candidates") or []
    if left.get("universe_empty"):
        check("左栏空宇宙 candidates == []", cands == [], "cands=%r" % cands)
        check("note 含「ETF 日 K 待接入」提示", "ETF 日 K 待接入" in (left.get("note") or ""),
              "note=%r" % left.get("note"))
    else:
        check("左栏已接入：candidates 为非空列表", len(cands) > 0, "n=%d" % len(cands))
        ok_struct = all({"rank", "symbol", "score", "factor_scores"} <= set(c) for c in cands)
        check("候选结构完整 {rank,symbol,score,factor_scores}", ok_struct)
        check("权重已生效 weights_effective", bool(left.get("weights_effective")))
    # 底层函数也直接验证
    fc = etf.etf_factor_candidates(con)
    if fc.get("universe_empty"):
        check("etf_factor_candidates 空宇宙 candidates==[]",
              (fc.get("candidates") or []) == [], str(fc))
    else:
        check("etf_factor_candidates 已接入候选>0",
              len(fc.get("candidates") or []) > 0, str(fc.get("n_universe")))
    # 顾问维度函数（现返回 dict：dims/quotes/quotes_meta）
    adv = etf.etf_advisory()
    adv_dims = adv.get("dims") or []
    check("etf_advisory 返回 6 维（已接入+待接入混合）",
          len(adv_dims) == 6
          and any(d["status"].startswith("已接入") for d in adv_dims)
          and any(d["status"] == "待接入" for d in adv_dims),
          str([d["status"] for d in adv_dims]))
    check("etf_advisory 含 quotes 与 quotes_meta", "quotes" in adv and "quotes_meta" in adv)
except Exception as e:  # noqa: BLE001
    check("左栏 A 断言", False, str(e))


# ---- 5) server.py 含新路由字符串 -------------------------------------------
print("[5] server.py 新路由")
server_path = os.path.join(ROOT, "server.py")
server_src = ""
if os.path.exists(server_path):
    with open(server_path, encoding="utf-8") as f:
        server_src = f.read()
else:
    check("server.py 存在", False, "未找到 server.py")
for route in ("/api/etf", "/api/etf/status", "/api/etf/run"):
    check("server.py 含路由 %s" % route, route in server_src, "缺失")


# ---- 6) etf.py 调用 metalearn.weights_from_ic，未重抄公式 --------------------
print("[6] 权重唯一真源 = metalearn.weights_from_ic")
etf_path = os.path.join(ROOT, "etf.py")
etf_src = ""
if os.path.exists(etf_path):
    with open(etf_path, encoding="utf-8") as f:
        etf_src = f.read()
check("etf.py 调用 metalearn.weights_from_ic", "weights_from_ic" in etf_src)
check("etf.py 未重定义 weights_from_ic", "def weights_from_ic" not in etf_src)
# 重抄公式的典型特征：出现 IC 收缩/先验混合的**手写数学**；
# 注意：etf.py 仅 `from metalearn import (..., IC_SHRINK_K, W_PRIOR_MIX, ...)` 引入常量名属正常，不应判为重写。
# 真正重写会留下运算式（n/(n+K) / (1.0 - prior_mix) / 重新定义 W_FLOOR =）。
redef_hints = ["n / (n +", "(1.0 - prior_mix)", "W_FLOOR =", "def weights_from_ic"]
leaked = [h for h in redef_hints if h in etf_src]
check("etf.py 未手写权重收缩/先验混合数学", not leaked, "命中残留:%r" % leaked)


# ---- 7) etf.py 无编造数据 --------------------------------------------------
print("[7] etf.py 无编造数据（无硬编码 ETF 代码清单）")
# 若硬编码了具体 ETF 代码（6 位数字串）参与逻辑，应被抓出
import re
hardcoded = re.findall(r'["\']\d{6}["\']', etf_src)
# ETF_CODE_PREFIXES 里的前缀是 2~3 位，不算 6 位代码硬编码；这里只查 6 位完整代码
check("etf.py 无 6 位硬编码 ETF 代码", len(hardcoded) == 0,
      "命中:%r" % hardcoded)


# ---- 8) 行情快照真实可读 + etf.py 离线（不 import akshare） -----------------
print("[8] 行情快照真实 + etf.py 不依赖 akshare")
try:
    right = (etf.payload(con).get("right") or {})
    quotes = right.get("quotes") or {}
    check("右栏含实时行情快照 quotes", len(quotes) > 0, "quotes 数=%d" % len(quotes))
    sample = next(iter(quotes.values())) if quotes else {}
    check("quotes 字段含 name/price/pct/amount",
          all(k in sample for k in ("name", "price", "pct", "amount")), str(sample))
    big = [c for c, v in quotes.items() if (v.get("amount") or 0) > 1e8]
    check("存在成交额 > 1 亿 的真实 ETF（非全零编造）", len(big) > 0, "big=%d" % len(big))
    # etf.py 必须离线读缓存，不得在请求路径 import akshare（否则强依赖联网）
    # 注意：docstring 里提到 akshare 属正常说明，仅检查真正的 import 语句
    import re as _re
    has_ak_import = _re.search(r"^\s*(import|from)\s+akshare\b", etf_src, _re.M) is not None
    check("etf.py 不在请求路径 import akshare（离线读缓存）", not has_ak_import)
except Exception as e:  # noqa: BLE001
    check("行情快照断言", False, str(e))


# ---- 9) 集思录投顾指标已接入并正确传前端（溢价率/规模/费率/真实份额净申赎）----
print("[9] 集思录投顾指标（进哥 cookie 打通 jisilu.cn）已接入并传前端")
try:
    right = (etf.payload(con).get("right") or {})
    jl = right.get("jisilu") or {}
    jlm = right.get("jisilu_meta") or {}
    check("右栏 payload 含 jisilu 真实数据", len(jl) > 0, "jisilu 数=%d" % len(jl))
    check("jisilu_meta 计数与 jisilu 一致", jlm.get("n") == len(jl),
          "meta.n=%s jisilu=%d" % (jlm.get("n"), len(jl)))
    check("jisilu 来源标注 jisilu.cn/data/etf/etf_list",
          (jlm.get("source") or "").startswith("jisilu.cn"), str(jlm.get("source")))
    # 抽查：每个 jisilu 条目含 4 个必填真实字段，且溢价率为数值（非空）
    req = {"premium_rt", "scale_est", "fee", "amount_increase_rt", "index_nm", "name"}
    ok_struct = all(req <= set(v.keys()) for v in jl.values())
    check("jisilu 每条约含 溢价率/规模/费率/净申赎/跟踪指数 字段", ok_struct)
    nonnull_prem = sum(1 for v in jl.values() if v.get("premium_rt") is not None)
    check("溢价率(premium_rt) 真实非空占比高（集思录 nav_discount_rt）",
          nonnull_prem >= max(1, int(0.9 * len(jl))), "%d/%d 非空" % (nonnull_prem, len(jl)))
    # 抽样核对一只大 ETF 的数值合理性（用缓存直接读，避免依赖随机首个键）
    import json as _json
    cache = _json.load(open(os.path.join(ROOT, "..", "jisilu_etf_cache.json"), encoding="utf-8"))
    c510300 = cache.get("etfs", {}).get("510300")
    if c510300:
        check("抽样 510300(沪深300ETF) 规模/费率/溢价率均为真实数值",
              c510300.get("scale_est") is not None and c510300.get("fee") is not None
              and c510300.get("premium_rt") is not None, str(c510300))
except Exception as e:  # noqa: BLE001
    check("集思录接入断言", False, str(e))


print("\n========================================")
if FAILURES:
    print("结果：%d 项失败 -> %s" % (len(FAILURES), ", ".join(FAILURES)))
    sys.exit(1)
else:
    print("结果：全部通过 (0 failures)")
    sys.exit(0)
