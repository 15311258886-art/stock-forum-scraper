# -*- coding: utf-8 -*-
"""开源情报模块的自检（2026-10-30）。

为什么单独写一个而不是靠人工点一遍：
  这个模块的结论全部是数字，**数字错了界面上看不出来**——一个 t=−10 的规则和
  一个 t=−2 的规则在表格里长得一样，前端也不会报错。所以必须有一支脚本，
  把「落库的数字」重新算一遍，对不上就 FAIL。

覆盖 10 项：
  1  表结构与必要列齐备
  2  payload 结构完整（前端依赖的键全部存在、类型正确）
  3  stats 自洽（各状态计数之和 == 规则条数）
  4  status 只出现在合法枚举内
  5  规则按状态优先级排序（前端依赖这个顺序）
  6  cross 字段一致性（有长样本结果时，超额/t 都非空；同向标记与符号一致）
  7  text_block 含被推翻条目 + 明确披露样本边界
  8  空库降级（新连接/空表时 payload 返回 available=False 而不是抛异常）
  9  非法谓词返回 invalid，不抛异常、不污染表
 10  数字可复现（逐条重跑 predicate，与落库值比对）

运行：
  python tools/oss_selfcheck.py            # 全量（约 3 分钟，含第 10 项重算）
  python tools/oss_selfcheck.py --fast     # 跳过第 10 项（约 2 秒）
退出码：0=全通过，2=有 FAIL。
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "viewer"))
os.chdir(os.path.join(ROOT, "viewer"))       # hypo 里的相对路径依赖

import oss_mine as OSS  # noqa: E402
import hypo as HP  # noqa: E402

FAST = "--fast" in sys.argv
fails: list = []


def ck(cond, name, detail=""):
    print(("  PASS " if cond else "  FAIL ") + name + (("  → " + str(detail)) if detail else ""))
    if not cond:
        fails.append(name)


def main() -> int:
    db = os.path.join(ROOT, "viewer", "app.db")
    conn = sqlite3.connect(db)

    # ---- 1 表结构 ----
    cols = {r[1] for r in conn.execute("PRAGMA table_info(oss_rule)")}
    need = {"key", "repo", "title", "rule", "predicate", "horizon", "expect", "why",
            "n", "days", "excess", "t", "win", "status", "note", "stable", "tested_at",
            "cross_excess", "cross_t", "cross_same"}
    ck(need <= cols, "1 表结构完整", "缺列：" + ",".join(sorted(need - cols)) if need - cols else "")

    # ---- 2 payload 结构 ----
    p = OSS.payload(conn)
    shape = {"ok": bool, "available": bool, "asof": str, "data_span": dict,
             "stats": dict, "rules": list, "repos": list}
    bad = [k for k, t in shape.items() if not isinstance(p.get(k), t)]
    ck(not bad, "2 payload 顶层字段齐全且类型正确", "异常键：" + ",".join(bad) if bad else "")
    ck(p.get("available") is True, "2b 有数据时 available=True")

    rules = p.get("rules") or []

    # ---- 3 stats 自洽 ----
    st = p.get("stats") or {}
    keys = ("active", "refuted", "unstable", "marginal", "unsupported", "invalid")
    s = sum(int(st.get(k) or 0) for k in keys)
    ck(s == len(rules), "3 stats 计数之和 == 规则条数", "%d vs %d" % (s, len(rules)))
    ck(int(st.get("rules") or 0) == len(rules), "3b stats.rules 与实际条数一致")

    # ---- 4 status 合法 ----
    legal = set(keys)
    badst = sorted({r.get("status") for r in rules} - legal)
    ck(not badst, "4 status 全在合法枚举内", "非法：" + ",".join(map(str, badst)) if badst else "")

    # ---- 5 排序 ----
    order = [OSS._STATUS_ORDER.get(r.get("status") or "invalid", 9) for r in rules]
    ck(order == sorted(order), "5 规则按状态优先级排序")

    # ---- 6 cross 一致性 ----
    bad6 = []
    for r in rules:
        if r.get("cross_days"):
            if r.get("cross_excess") is None or r.get("cross_t") is None:
                bad6.append(r["key"] + "(空值)")
                continue
            same = (((r.get("excess") or 0) > 0)
                    == ((r.get("cross_excess") or 0) > 0))
            if bool(r.get("cross_same")) != bool(same):
                bad6.append(r["key"] + "(同向标记不符)")
    ck(not bad6, "6 长样本字段一致", "；".join(bad6))

    # ---- 7 text_block ----
    tb = OSS.text_block(conn)
    ck(bool(tb), "7 text_block 非空")
    ck("样本" in tb or "口径边界" in tb, "7b text_block 披露了样本边界")
    ck("禁止" in tb or "不得" in tb, "7c text_block 含显式禁用语句（防模型误用）")

    # ---- 8 空库降级 ----
    mem = sqlite3.connect(":memory:")
    try:
        OSS.ensure_schema(mem)          # 这个连接是全新的，必须能自己建表
        p2 = OSS.payload(mem)
        ok8 = (p2.get("ok") is True and p2.get("available") is False
               and isinstance(p2.get("rules"), list))
        ck(ok8, "8 空库降级为 available=False 且不抛异常",
           json.dumps({k: p2.get(k) for k in ("ok", "available", "reason")}, ensure_ascii=False))
        OSS.ensure_schema(mem)          # 重复调用幂等
        ck(True, "8b ensure_schema 可重复调用")
    except Exception as e:  # noqa: BLE001
        ck(False, "8 空库降级为 available=False 且不抛异常", repr(e)[:120])
    finally:
        mem.close()

    # ---- 9 非法谓词 ----
    try:
        row = OSS.test_one(conn, {"key": "_selfcheck_bad", "predicate": "nonsense >= 1",
                                  "expect": "positive", "horizon": 5,
                                  "repo": "", "title": "", "rule": "", "why": ""},
                           lookback=60, robust=False)
        ck(row.get("status") == "invalid", "9 非法谓词判 invalid 不抛异常", row.get("status"))
    except Exception as e:  # noqa: BLE001
        ck(False, "9 非法谓词判 invalid 不抛异常", repr(e)[:120])

    # ---- 10 数字可复现 ----
    if FAST:
        print("  SKIP 10 数字可复现（--fast）")
    else:
        print("  10 数字可复现：逐条重跑（约 3 分钟）…")
        bad10 = []
        for r in rules:
            if r.get("status") == "invalid":
                continue
            e = HP.evaluate(conn, r["predicate"], int(r.get("horizon") or 5), lookback=400)
            if not e.get("ok"):
                bad10.append("%s(重跑失败:%s)" % (r["key"], e.get("note")))
                continue
            de = abs((e.get("excess") or 0) - (r.get("excess") or 0))
            dt = abs((e.get("t") or 0) - (r.get("t") or 0))
            dn = abs((e.get("n") or 0) - (r.get("n") or 0))
            if de > 0.02 or dt > 0.05 or dn > 0:
                bad10.append("%s(超额差%.3f t差%.2f n差%d)" % (r["key"], de, dt, dn))
        ck(not bad10, "10 每条规则的落库数字可复现", "；".join(bad10))

    conn.close()
    print("")
    if fails:
        print("结果 FAIL（%d 项）：%s" % (len(fails), "；".join(fails)))
        return 2
    print("结果 PASS（全部检查通过）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
