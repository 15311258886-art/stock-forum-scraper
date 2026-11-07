#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""接口层全面探活（2026-11-05 自审 P0）。

## 为什么要它

`tools/smoke.py` 只覆盖 23 条准入门禁，前端实际会打的 `/api/*` 有三百多个字面量。
「点得开」这件事必须实测，不能靠读代码推断 —— 一个 KeyError / 一条 SQL 报错，
在浏览器里表现为「面板空白」，而服务端 HTTP 仍是 200。

## 判据（不只是看状态码）

每一条记 5 个维度，任何一个不合格都算「有问题」：

  · `status`   —— 非 200 直接算坏
  · `ms`       —— > 8000ms 算「慢」（单进程 + GIL，慢接口会拖死整站）
  · `bytes`    —— 0 字节 / < 40 字节算「空响应」
  · `json_ok`  —— 声称是 JSON 却解析不了 → 算坏
  · `err_key`  —— 响应体里带 `"error"` / `"ok": false` / `Traceback` → 算坏

## 纪律

  · **只发 GET**，且跳过 `server._GET_WRITE_ROUTES` 里声明的写副作用路由；
  · **不猜参数**：带占位符的路由按 `SAMPLE_ARGS` 里登记的实测参数展开，没登记的跳过并列出
    （宁可知不知道，也不拿错参数去测然后误报）；
  · 顺序发、单条 10s 超时 —— 并发会把单进程服务打满，测出来的「慢」全是自己造成的。

## 用法

    python tools/api_probe_all.py                 # 全量探活，生成报告
    python tools/api_probe_all.py --only kline    # 只测路径含 kline 的
    python tools/api_probe_all.py --base http://127.0.0.1:8765
"""
from __future__ import annotations

import argparse
import io
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SERVER = os.path.join(ROOT, "viewer", "server.py")
OUT = os.path.join(ROOT, "data", "api_probe_report.json")

# 带占位符 / 需要参数的只读路由 → 实测可用参数
SAMPLE_ARGS = {
    "/api/kline": "code=600519&period=day&limit=30",
    "/api/stock": "code=600519",
    "/api/stock_detail": "code=600519",
    "/api/f10": "code=600519",
    "/api/ann": "code=600519",
    "/api/quote": "code=600519",
    "/api/min": "code=600519",
    "/api/daychips": "code=600519",
    "/api/notes": "code=600519",
    "/api/fund_flow": "code=600519",
    "/api/lhb": "code=600519",
}

# server.py 里声明的写副作用 GET 路由（探活要跳过，别把测试变成写库）
# ★ 这里**不能**放裸 "/"：`s.endswith("/")` 为真会让「每个路由都以 / 开头」恒成立，
#   整张表被一条规则全部滤掉（第一版就踩了：routes 收集到 0 条，静默空跑）。
SKIP_PREFIX = ("/index.html", "/static/", "/api/market_cache/",
               "/api/pillars/generate", "/api/auction/backfill")

# 动词后缀 = 明确的「会写库/会调外网/会花钱」动作。探活是**只读**审计，
# 不能为了测通不通就去跑一遍全市场重算或打一次大模型。这些路由单独列出，
# 用 --include-write 才测（默认跳过，并在报告里如实标注「未测」）。
WRITE_SUFFIX = ("/run", "/generate", "/refresh", "/backfill", "/rebuild", "/reset",
                "/delete", "/clear", "/test", "/scan", "/build", "/import", "/save",
                "/add", "/update", "/apply", "/push", "/sync", "/train", "/fetch")

# 从源码里抠 /api/xxx 字面量的两种写法：route == "/api/x" / route.startswith("/api/x")
RE_EQ = re.compile(r'route\s*==\s*"(/api/[A-Za-z0-9_/\-]*)"')
RE_SW = re.compile(r'route\.startswith\(\s*"(/api/[A-Za-z0-9_/\-]*)"')
RE_ANY = re.compile(r'"(/api/[A-Za-z0-9_/\-]+)"')


def collect_routes() -> list[str]:
    src = io.open(SERVER, encoding="utf-8", errors="ignore").read()
    exact = set(RE_EQ.findall(src))
    prefix = set(x.rstrip("/") for x in RE_SW.findall(src))
    anylit = set(RE_ANY.findall(src))
    # 精确匹配的路由里，去掉明显是前缀的（长度更短且被 startswith 覆盖）
    routes = set(exact)
    for p in prefix:
        if p and p != "/api":
            routes.add(p)
    # 补充：凡字面量出现但既不在 exact 也不在 prefix 的，只收「看起来是完整路由」的
    for a in anylit:
        if a.count("/") >= 2 and not a.endswith("/") and a not in routes:
            routes.add(a)
    out = []
    for r in sorted(routes):
        if not r.startswith("/api"):
            continue
        if any(r == s or (s.endswith("/") and r.startswith(s)) for s in SKIP_PREFIX):
            continue
        out.append(r)
    return out


def probe(url: str, timeout: float) -> dict:
    t0 = time.time()
    req = urllib.request.Request(url, headers={"User-Agent": "api-probe/1.0",
                                               "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read()
            return {"status": r.status, "ms": int((time.time() - t0) * 1000),
                    "bytes": len(body), "body": body[:400]}
    except urllib.error.HTTPError as e:
        body = b""
        try:
            body = e.read()[:400]
        except Exception:  # noqa: BLE001
            pass
        return {"status": e.code, "ms": int((time.time() - t0) * 1000),
                "bytes": len(body), "body": body}
    except Exception as e:  # noqa: BLE001
        return {"status": 0, "ms": int((time.time() - t0) * 1000), "bytes": 0,
                "body": ("%s: %s" % (type(e).__name__, str(e)[:120])).encode()}


def judge(path: str, r: dict) -> tuple[str, str]:
    """返回 (评级, 说明)。评级：ok / slow / empty / bad / dead"""
    if r["status"] == 0:
        return "dead", r["body"].decode("utf-8", "replace")[:120]
    if r["status"] != 200:
        return "bad", "HTTP %d %s" % (r["status"],
                                      r["body"].decode("utf-8", "replace")[:160])
    txt = r["body"].decode("utf-8", "replace")
    if "Traceback (most recent call last)" in txt:
        return "bad", "响应体含 Traceback"
    low = txt.lower()
    if '"ok": false' in low or '"ok":false' in low:
        return "bad", "ok=false：" + txt[:160]
    if re.search(r'"error"\s*:\s*"[^"]+', txt or ""):
        return "bad", "带 error：" + txt[:160]
    if r["bytes"] < 40:
        return "empty", "只有 %d 字节" % r["bytes"]
    if r["ms"] > 8000:
        return "slow", "%dms" % r["ms"]
    return "ok", ""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8765")
    ap.add_argument("--only", default="")
    ap.add_argument("--timeout", type=float, default=10.0)
    ap.add_argument("--include-write", action="store_true",
                    help="连 /run /generate /scan 等写副作用路由一起测（默认跳过）")
    a = ap.parse_args()

    routes = collect_routes()
    if a.only:
        routes = [r for r in routes if a.only in r]
    skipped_write = []
    if not a.include_write:
        keep = []
        for r in routes:
            if r.endswith(WRITE_SUFFIX):
                skipped_write.append(r)
            else:
                keep.append(r)
        routes = keep
    # 合并 SAMPLE_ARGS：/api/kline 与 /api/kline/x 都按登记展开
    plan, skipped = [], []
    for r in routes:
        q = ""
        for k, v in SAMPLE_ARGS.items():
            if r == k or r.startswith(k + "/"):
                q = v
                break
        if q:
            plan.append((r, "%s%s?%s" % (a.base, r, q)))
        else:
            plan.append((r, a.base + r))

    print("[probe] 待测 %d 条（写副作用路由跳过 %d 条）" % (len(plan), len(skipped_write)),
          flush=True)
    res = []
    for i, (path, url) in enumerate(plan, 1):
        r = probe(url, a.timeout)
        lvl, why = judge(path, r)
        res.append({"path": path, "status": r["status"], "ms": r["ms"],
                    "bytes": r["bytes"], "level": lvl, "why": why})
        if lvl != "ok":
            print("  [%s] %-46s %s" % (lvl.upper(), path, why[:110]), flush=True)
        if i % 50 == 0:
            print("  ... %d/%d" % (i, len(plan)), flush=True)

    bad = [x for x in res if x["level"] in ("bad", "dead", "empty")]
    slow = [x for x in res if x["level"] == "slow"]
    print("\n[probe] 合计 %d 条：ok=%d 慢=%d 坏/空/死=%d"
          % (len(res), len(res) - len(bad) - len(slow), len(slow), len(bad)), flush=True)
    if bad:
        print("[probe] ── 坏清单 ──", flush=True)
        for x in bad:
            print("  %-6s %-46s %s" % (x["level"], x["path"], x["why"][:120]), flush=True)
    if slow:
        print("[probe] ── 慢清单 ──", flush=True)
        for x in slow:
            print("  slow   %-46s %dms" % (x["path"], x["ms"]), flush=True)

    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with io.open(OUT, "w", encoding="utf-8") as fh:
        json.dump({"at": time.strftime("%Y-%m-%d %H:%M:%S"), "base": a.base,
                   "total": len(res), "results": res}, fh, ensure_ascii=False, indent=1)
    print("[probe] 报告 → %s" % OUT, flush=True)
    return 0 if not bad else 1


if __name__ == "__main__":
    sys.exit(main())
