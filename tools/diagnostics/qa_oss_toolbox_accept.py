#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P0 验收：11 个开源规则工具进筛台可用（接口级，只读）。

对应任务 #2 的七条验收点：
  ① 工具墙出现 33 张卡、11 条 oss 全部 active、每张卡该有的字段都有
  ② 每条点击后能出「本次 5264 → N」，且 note 的 N 与 steps.out 一致
  ③ 命中数与独立重算对得上 —— **这是本次验收的核心**
  ④ 参数可调，且改后重算方向合理（门槛收紧 → 命中变少）
  ⑤ 能叠加进筛道求交（交集单调不增）
  ⑥ 现有工具行为不变（33 个全部可单跑；retired 的必须报错而不是静默跑）
  ⑦ 契约/导航不挂 —— 交给 pytest（本脚本只报「跑没跑」，判定看退出码）

③ 为什么必须独立重算，而不是直接拿 `oss_rule.n` 对：`oss_rule.n` 是**394 个交易日的
样本总数**、而且是不剔 ST/次新的老口径；拿它和「某一天筛出多少只」比，必然对不上，
对不上还会被误判成 bug。所以这里做两件事：
  (a) 用 kbase 后复权现算 2026-11-03 的命中级数，与工具的 `out` 逐条比 —— 同一天、
      同一批票、同一套判据，差异必须接近 0，否则就是「产品筛出来的票和回测口径不是一批」；
  (b) 顺带把 `oss_rule.n / days` 换算成日均，做个量级对照（只作参考，不作判据）。

用法： .venv/Scripts/python.exe tools/qa_oss_toolbox_accept.py
"""

from __future__ import annotations

import json
import sqlite3
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(ROOT))

import numpy as np                                          # noqa: E402

import backtest_oss as B                                    # noqa: E402

BASE = "http://127.0.0.1:8765"
APP_DB = ROOT / "viewer" / "app.db"
LOGS = ROOT / "viewer" / "logs"

# ④ 参数敏感性用例：(工具, 参数, 从严值, 从宽值, 期望方向)
PARAM_CASES = (
    ("oss_near_high", "max_gap", 10, 25, "宽门槛命中更多"),
    ("oss_burst", "min_ret", 6, 2, "低门槛命中更多"),
    ("oss_shrink_vol", "max_vr", 0.5, 1.0, "高门槛命中更多"),
    ("oss_rsi_overbought", "min_rsi", 90, 70, "低门槛命中更多"),
)

# ⑤ 求交用例
STACK_CASES = (
    (("oss_ma_align", "oss_rsi_overbought"), []),
    (("oss_burst_vol", "oss_high_vol"), []),
)


def _get(path: str, timeout: int = 120) -> dict:
    with urllib.request.urlopen(BASE + path, timeout=timeout) as r:
        return json.loads(r.read().decode())


def stack(spec: list[dict], tries: int = 25) -> dict:
    """跑一次筛道，遇到 pending 就等（后台单线程队列在算）。"""
    q = urllib.parse.quote(json.dumps(spec, ensure_ascii=False))
    last: dict = {}
    for _ in range(tries):
        last = _get("/api/toolbox/stack?tools=" + q)
        if not last.get("pending"):
            return last
        time.sleep(1.5)
    return last


def one(tool: str, params: dict | None = None) -> dict:
    spec = [{"tool": tool}]
    if params:
        spec[0]["params"] = params
    return stack(spec)


# ---------------------------------------------------------------- ③ 独立重算
def oracle_counts(day: str) -> dict[str, int]:
    """用 kbase 后复权现算 `day` 当天的 11 条命中只数（产品默认参数、不剔 ST/次新）。

    与产品口径刻意对齐的部分：
      · 不做 ST / 次新剔除（产品 default_params 的 skip_st=0）
      · 只要求「当天有行情」，和 _build_oss_snapshot 一样
      · 判据逐条照 toolbox._p_* 的默认参数（与 backtest_oss.rule_hits 同一份实现，
        该实现已由 tools/backtest_oss_selfcheck.py 的 [B] 项与 toolbox 逐字版比对过 440 组）
    """
    con = sqlite3.connect("file:%s?mode=ro" % B.KBASE.as_posix(), uri=True)
    d0 = "2024-01-01"          # 251 个交易日足够，任何一条判据都能算全
    rows = con.execute(
        "SELECT code,date,c,h,l,v FROM kline WHERE date>=? AND date<=? AND c>0 "
        "ORDER BY code,date", (d0, day)).fetchall()
    con.close()
    per: dict[str, list] = {}
    for code, d, c, h, l, v in rows:
        per.setdefault(str(code), []).append((str(d), float(c), float(h), float(l), float(v)))
    out = {r: 0 for r in B.ALL_RULES}
    for code, ser in per.items():
        if ser[-1][0] != day:          # 当天没行情（停牌）
            continue
        c = np.array([x[1] for x in ser])
        h = np.array([x[2] for x in ser])
        l = np.array([x[3] for x in ser])
        v = np.array([x[4] for x in ser])
        feat = B.stock_features(c, h, l, v)
        for r in B.ALL_RULES:
            if bool(B.rule_hits(feat, r)[-1]):
                out[r] += 1
    return out


def oss_rule_daily() -> dict[str, tuple]:
    """oss_rule 里的「样本数 / 交易日数」→ 日均命中（老口径，仅作量级参考）。"""
    con = sqlite3.connect("file:%s?mode=ro" % APP_DB.as_posix(), uri=True)
    try:
        rows = con.execute(
            "SELECT key,n,days,excess,t FROM oss_rule WHERE key LIKE 'oss\\_%' ESCAPE '\\'"
        ).fetchall()
        return {str(r[0]): (r[1] or 0, r[2] or 0, r[3], r[4]) for r in rows}
    except sqlite3.Error:
        return {}
    finally:
        con.close()


def main() -> int:
    t0 = time.time()
    problems: list[str] = []
    report: dict = {"generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                    "checks": {}, "tools": []}

    # ---------- ① 卡片 ----------
    d = _get("/api/toolbox")
    tools = d.get("tools") or []
    oss = [t for t in tools if str(t.get("tool_key", "")).startswith("oss_")]
    active_oss = [t for t in oss if t.get("status") == "active"]
    universe = d.get("universe")
    print("[1] 工具 %d 张卡（oss %d 条 / active %d 条），宇宙 %s"
          % (len(tools), len(oss), len(active_oss), universe))
    if len(tools) != 33:
        problems.append("卡片数 %d，应为 33" % len(tools))
    if len(oss) != 11:
        problems.append("oss 卡片 %d 条，应为 11" % len(oss))
    if len(active_oss) != 11:
        problems.append("oss active %d 条，应为 11" % len(active_oss))
    need = ("name", "why", "what", "caveat", "params_schema", "default_params")
    for t in oss:
        miss = [k for k in need if not t.get(k)]
        if miss:
            problems.append("%s 卡片缺字段：%s" % (t.get("tool_key"), ",".join(miss)))
        if not t.get("caveat"):
            problems.append("%s 没有 caveat（跑输的必须写在卡片上）" % t.get("tool_key"))
    report["checks"]["cards"] = {"total": len(tools), "oss": len(oss),
                                 "oss_active": len(active_oss), "universe": universe}

    # ---------- ② 逐条跑 ----------
    print("\n[2] 逐条单跑（默认参数）")
    prod: dict[str, int] = {}
    rows_out = []
    for t in oss:
        key = t["tool_key"]
        r = one(key)
        steps = r.get("steps") or []
        if not r.get("ok") or not steps:
            problems.append("%s 跑不起来：%s" % (key, str(r.get("errors") or r)[:160]))
            continue
        st = steps[0]
        note = str(st.get("note") or "")
        out = int(st.get("out") or 0)
        prod[key] = out
        note_ok = ("→ %d 只" % out) in note
        date_ok = "2026-11-03" in note or str(r.get("trade_date")) in note
        if not note_ok:
            problems.append("%s 的 note 与 out 不一致：note=%r out=%s" % (key, note, out))
        if not date_ok:
            problems.append("%s 的 note 里没有数据日：%r" % (key, note))
        rows_out.append({"tool": key, "in": st.get("in"), "out": out,
                         "ms": st.get("ms"), "source": st.get("source"),
                         "note": note, "error": st.get("error") or ""})
        print("    %-20s %5s → %-5d %6sms  %s" % (key, st.get("in"), out,
                                                  st.get("ms"), st.get("source")))

    # ---------- ③ 独立重算对照 ----------
    print("\n[3] 与后复权现算对照（%s）" % (rows_out[0]["note"].split("数据日")[-1].split("：")[0].strip()
                                            if rows_out else "?"))
    day = "2026-11-03"
    orc = oracle_counts(day)
    old = oss_rule_daily()
    print("    %-20s %8s %8s %8s %10s" % ("工具", "产品", "现算", "差", "老口径日均"))
    diff_rows = []
    for key in [r["tool"] for r in rows_out]:
        p = prod.get(key, 0)
        o = orc.get(key, 0)
        n_days = old.get(key)
        od = ("%.1f" % (n_days[0] / n_days[1])) if (n_days and n_days[1]) else "-"
        d_abs = p - o
        rel = abs(d_abs) / max(1, o)
        print("    %-20s %8d %8d %+8d %10s" % (key, p, o, d_abs, od))
        diff_rows.append({"tool": key, "product": p, "oracle": o,
                          "diff": d_abs, "rel": round(rel, 4),
                          "old_daily_hits": od})
        if abs(d_abs) > 2 and rel > 0.02:
            problems.append("%s 产品 %d 只 / 现算 %d 只，差 %+d（相对 %.1f%%）"
                            % (key, p, o, d_abs, rel * 100))
    report["checks"]["oracle"] = {"date": day, "rows": diff_rows}

    # ---------- ④ 参数敏感 ----------
    print("\n[4] 参数可调（改门槛后必须朝预期方向变）")
    for tool, pname, tight, loose, expect in PARAM_CASES:
        rt = one(tool, {pname: tight})
        rl = one(tool, {pname: loose})
        ot = (rt.get("steps") or [{}])[0].get("out")
        ol = (rl.get("steps") or [{}])[0].get("out")
        ok = ot is not None and ol is not None and int(ol) >= int(ot)
        print("    %-20s %s=%s → %-5s | %s=%s → %-5s  %s"
              % (tool, pname, tight, ot, pname, loose, ol, "OK" if ok else "★方向错"))
        if not ok:
            problems.append("%s 参数 %s 从 %s 放宽到 %s，命中没变多（%s → %s）"
                            % (tool, pname, tight, loose, ot, ol))
        report["checks"].setdefault("params", []).append(
            {"tool": tool, "param": pname, "tight": tight, "out_tight": ot,
             "loose": loose, "out_loose": ol, "ok": bool(ok)})

    # ---------- ⑤ 求交 ----------
    print("\n[5] 筛道求交（交集必须单调不增）")
    for keys, _ in STACK_CASES:
        parts = []
        for k in keys:
            r = one(k)
            parts.append(int((r.get("steps") or [{}])[0].get("out") or 0))
        r = stack([{"tool": k} for k in keys])
        final = int(r.get("final") or 0)
        ok = final <= min(parts)
        print("    %s → 各级 %s，最终 %d  %s"
              % (" ∩ ".join(keys), parts, final, "OK" if ok else "★交集比最小的一级还多"))
        if not ok:
            problems.append("求交 %s 最终 %d > min(%s)" % (keys, final, parts))
        report["checks"].setdefault("stack", []).append(
            {"steps": list(keys), "parts": parts, "final": final, "ok": bool(ok)})

    # ---------- ⑥ 全部工具可跑 + retired 必须报错 ----------
    print("\n[6] 33 个工具逐个可跑；retired 必须明确报错")
    bad = []
    for t in tools:
        r = one(t["tool_key"])
        st = (r.get("steps") or [{}])[0]
        err = st.get("error") or ""
        if t.get("status") == "retired":
            if not err:
                bad.append("%s 已 retired 但没报错" % t["tool_key"])
        elif not r.get("ok") or err:
            bad.append("%s 失败：%s" % (t["tool_key"], err[:80]))
    print("    失败/异常 %d 个" % len(bad))
    problems.extend(bad)
    report["checks"]["all_tools"] = {"checked": len(tools), "bad": bad}

    # ---------- 落盘 ----------
    report["problems"] = problems
    report["elapsed_s"] = round(time.time() - t0, 1)
    LOGS.mkdir(parents=True, exist_ok=True)
    out = LOGS / ("qa_oss_toolbox_accept_%s.json" % time.strftime("%Y%m%d"))
    out.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")

    print("\n" + "=" * 66)
    if problems:
        print("验收不通过：%d 项" % len(problems))
        for p in problems:
            print("  ✗", p)
    else:
        print("验收通过：①②③④⑤⑥ 全过（⑦ 见 pytest）")
    print("报告：%s（%.1fs）" % (out, report["elapsed_s"]))
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
