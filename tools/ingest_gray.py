# -*- coding: utf-8 -*-
"""论坛源灰度探针（2026-10-31 决策③，进哥拍板走 C）。

## 为什么不是"把 INGEST_SOURCES 打开"

打开的后果不是"数据开始更新"，而是"交易时段每 10 分钟一轮、共约 42 轮/天"，
而淘股吧的 Cookie 早在 2026-10-24 就被判 login_failed。真开的结果是：
**每 10 分钟抓一次登录跳转页，解析出 0 条，日志刷满，主库被空跑覆盖一次 meta**。
更坏的情况是触发风控（封 IP/封号），那时连"以后能不能抓"都变成未知。

所以灰度第一步必须是**不写库的探针**：抓 → 解析 → 出报告 → 丢掉。
先回答三个问题，再谈要不要接主库：
  ① 源还活不活（HTTP 状态 / 页面字节 / 是否登录页）
  ② 解析还准不准（解析出几条，字段完整率多少）
  ③ 和现有库重不重复（若接主库，会新增还是原地踏步）

## 明确不做的事

· 不写 app.db（一个字节都不写，包括 login_status 与 raw_pages）；
· 不改 config.INGEST_SOURCES（生产调度状态完全不受影响）；
· 不自动重试、不并发、不绕 robots —— 探针只有一次机会，抓不到就是抓不到，
  那本身就是结论。

## 用法

    python tools/ingest_gray.py                       # 默认 taoguba / trades / 2 页
    python tools/ingest_gray.py --site eastmoney --mode posts --pages 3
    python tools/ingest_gray.py --report              # 看历次探针的趋势

报告落在 `data/ingest_gray/`，一天一个文件，跑三次就能看出趋势。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
os.chdir(ROOT)

OUT_DIR = os.path.join(ROOT, "data", "ingest_gray")

# 判定阈值：不写死数字会变成"看着差不多就算通过"
MIN_PARSED_OK = 5        # 一页至少解析出 5 条才算解析正常
MIN_BYTES_OK = 3000      # 低于这个字节数基本是登录页/风控页/空壳


def _cfg(site):
    from scraper.config import get_site, load_config
    data = load_config()
    c = get_site(data, site)
    c["_key"] = site
    return c


def _session(cfg, site):
    """建 session 并尝试登录。返回 (sess, login_state, note)。"""
    from scraper.session import RateLimitedSession
    sess = RateLimitedSession(
        user_agent=cfg.get("user_agent", ""),
        rate_limit_per_min=cfg.get("rate_limit_per_min", 15),
        timeout=cfg.get("timeout", 15),
        respect_robots=bool(cfg.get("respect_robots", True)),
        extra_headers=cfg.get("extra_headers") or None,
    )
    if not cfg.get("requires_login"):
        return sess, "public", "该站点无需登录"
    try:
        from scraper.auth import ensure_login
        method = ensure_login(sess, cfg)
        return sess, "ok", "登录方式：%s" % method
    except Exception as e:  # noqa: BLE001
        return sess, "login_failed", "登录失败：%s" % str(e)[:160]


def _sniff_login_page(html: str) -> bool:
    """粗判是不是登录页/风控页。命中即说明"抓到了，但抓的是墙"。"""
    if not html:
        return True
    keys = ("请登录", "登录后查看", "验证码", "安全验证", "访问验证", "captcha", "login")
    low = html[:6000].lower()
    return any(k.lower() in low for k in keys)


def probe(site="taoguba", mode="trades", pages=2):
    from viewer.ingest import PARSER_REGISTRY
    cfg = _cfg(site)
    if site not in PARSER_REGISTRY:
        return {"ok": False, "error": "unsupported_site", "site": site,
                "known": sorted(PARSER_REGISTRY)}

    targets = (cfg.get("targets") or {}).get(mode) or []
    if not targets and mode == "posts":
        targets = (cfg.get("targets") or {}).get("trades") or []
    if not targets:
        return {"ok": False, "error": "no_target_url", "site": site, "mode": mode,
                "note": "站点配置里没有这个 mode 的目标页，先确认 config 里的 targets 段"}

    sess, login_state, login_note = _session(cfg, site)
    parser = PARSER_REGISTRY[site](cfg)
    parse_fn = {"trades": "parse_trades", "posts": "parse_posts", "news": "parse_news"}.get(mode)
    if not parse_fn or not hasattr(parser, parse_fn):
        return {"ok": False, "error": "no_parser", "site": site, "mode": mode}

    url_reports, samples = [], []
    for url in targets[:max(1, int(pages))]:
        t0 = time.time()
        rec = {"url": url, "http": None, "bytes": 0, "parsed": 0,
               "login_page": False, "elapsed_ms": 0, "error": None, "fields": {}}
        try:
            resp = sess.get(url)
            rec["http"] = getattr(resp, "status_code", None)
            html = getattr(resp, "text", "") or ""
            rec["bytes"] = len(html.encode("utf-8", "ignore"))
            rec["login_page"] = _sniff_login_page(html)
            if rec["http"] and resp.status_code < 400 and not rec["login_page"]:
                recs = list(getattr(parser, parse_fn)(html, url) or [])
                rec["parsed"] = len(recs)
                if recs:
                    d = recs[0].to_dict() if hasattr(recs[0], "to_dict") else dict(recs[0])
                    rec["fields"] = {k: (v is not None and v != "") for k, v in d.items()}
                    samples.append({k: (str(v)[:60] if v is not None else None)
                                    for k, v in list(d.items())[:8]})
        except Exception as e:  # noqa: BLE001
            rec["error"] = str(e)[:200]
        rec["elapsed_ms"] = int((time.time() - t0) * 1000)
        url_reports.append(rec)

    parsed_total = sum(r["parsed"] for r in url_reports)
    bad_http = [r for r in url_reports if r["error"] or (r["http"] or 0) >= 400]
    login_pages = [r for r in url_reports if r["login_page"]]

    if login_state == "login_failed":
        verdict, why = "login_required", "登录态不可用（%s）——先解决凭据，否则抓到的都是墙" % login_note
    elif bad_http or login_pages:
        verdict, why = "blocked", "请求失败或被拦（HTTP 错误/登录页/风控页）"
    elif parsed_total == 0:
        verdict, why = "parse_empty", "抓到了页面但解析出 0 条 —— 页面结构可能变了"
    elif parsed_total < MIN_PARSED_OK:
        verdict, why = "parse_thin", "解析条数偏少（%d < %d），不足以支撑接主库" % (parsed_total, MIN_PARSED_OK)
    else:
        verdict, why = "ok", "解析正常（%d 条），可以进入下一步：接旁路库观察 3 天" % parsed_total

    return {
        "ok": True,
        "site": site, "mode": mode,
        "checked_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "login_state": login_state, "login_note": login_note,
        "urls": url_reports, "samples": samples,
        "parsed_total": parsed_total,
        "verdict": verdict, "verdict_why": why,
        "wrote_db": False,
        "note": ("本探针**没有写入 app.db**，也没有改动 config.INGEST_SOURCES；"
                 "生产抓取状态与探针结果无关。"),
    }


def _save(rep):
    os.makedirs(OUT_DIR, exist_ok=True)
    name = "%s_%s_%s.json" % (datetime.now().strftime("%Y%m%d_%H%M%S"), rep["site"], rep["mode"])
    p = os.path.join(OUT_DIR, name)
    with open(p, "w", encoding="utf-8") as f:
        json.dump(rep, f, ensure_ascii=False, indent=1)
    return p


def report(limit=12):
    """历次探针趋势：同一个源跑三次就能看出"是偶发还是真的死了"。"""
    if not os.path.isdir(OUT_DIR):
        return {"ok": False, "error": "no_probe_yet", "dir": OUT_DIR}
    files = sorted(os.listdir(OUT_DIR))[-limit:]
    rows = []
    for fn in files:
        try:
            with open(os.path.join(OUT_DIR, fn), encoding="utf-8") as f:
                r = json.load(f)
            rows.append({"at": r.get("checked_at"), "site": r.get("site"), "mode": r.get("mode"),
                         "verdict": r.get("verdict"), "parsed": r.get("parsed_total"),
                         "login": r.get("login_state")})
        except Exception:  # noqa: BLE001
            continue
    return {"ok": True, "n": len(rows), "rows": rows,
            "dir": OUT_DIR}


def main():
    ap = argparse.ArgumentParser(description="论坛源灰度探针（不写库）")
    ap.add_argument("--site", default="taoguba")
    ap.add_argument("--mode", default="trades", choices=["trades", "posts", "news"])
    ap.add_argument("--pages", type=int, default=2)
    ap.add_argument("--report", action="store_true", help="只看历次探针趋势")
    a = ap.parse_args()

    if a.report:
        r = report()
        if not r.get("ok"):
            print("还没有探针记录。先跑一次：python tools/ingest_gray.py")
            return 1
        print("%-21s %-10s %-6s %-14s %-7s %s" % ("时间", "源", "模式", "判定", "解析", "登录态"))
        for x in r["rows"]:
            print("%-21s %-10s %-6s %-14s %-7s %s" % (x["at"], x["site"], x["mode"],
                                                      x["verdict"], x["parsed"], x["login"]))
        return 0

    rep = probe(a.site, a.mode, a.pages)
    if not rep.get("ok"):
        print(json.dumps(rep, ensure_ascii=False, indent=1))
        return 2
    p = _save(rep)
    print("=" * 72)
    print("灰度探针  %s  %s/%s" % (rep["checked_at"], rep["site"], rep["mode"]))
    print("=" * 72)
    print("登录态：%s（%s）" % (rep["login_state"], rep["login_note"]))
    for r in rep["urls"]:
        print("  · HTTP %s  %6d 字节  %3d 条  %5dms  %s%s"
              % (r["http"], r["bytes"], r["parsed"], r["elapsed_ms"],
                 "登录页/风控页 " if r["login_page"] else "", r["error"] or ""))
    print("-" * 72)
    print("判定：%s —— %s" % (rep["verdict"], rep["verdict_why"]))
    print("报告：%s" % p)
    print("注：%s" % rep["note"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
