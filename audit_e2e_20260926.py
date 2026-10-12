# -*- coding: utf-8 -*-
"""端到端接口冒烟 + 数据关联校验（2026-09-26）
只读：不改任何数据。输出到 stdout，同时写 audit_e2e_out.txt。
"""
import json, time, urllib.request, urllib.parse, sys, io

B = "http://127.0.0.1:8765"
OUT = []


def p(*a):
    s = " ".join(str(x) for x in a)
    print(s)
    OUT.append(s)


def g(path, timeout=30):
    t0 = time.time()
    try:
        r = urllib.request.urlopen(B + path, timeout=timeout)
        raw = r.read().decode("utf-8", "ignore")
        dt = time.time() - t0
        try:
            data = json.loads(raw)
        except Exception:
            data = None
        return r.status, data, raw, dt
    except Exception as e:
        return "ERR", None, str(e), time.time() - t0


GETS = [
    "/api/summary", "/api/stocks", "/api/operating", "/api/persons",
    "/api/sentiment", "/api/topics", "/api/hotspots", "/api/refresh_status",
    "/api/scheduler/status", "/api/kb/list", "/api/raw_pages",
    "/api/players", "/api/overseas_sentiment", "/api/overseas_hotspots",
    "/api/overseas_sources", "/api/positions", "/api/lhb", "/api/lhb_sectors",
    "/api/niusan", "/api/star_moves", "/api/dashboard", "/api/consensus",
    "/api/cohold_recent", "/api/posts", "/api/login_status",
]

p("=" * 78)
p("PART 1 · 全接口冒烟")
p("=" * 78)
results = {}
for path in GETS:
    st, data, raw, dt = g(path)
    n = ""
    if isinstance(data, list):
        n = f"list[{len(data)}]"
    elif isinstance(data, dict):
        ks = list(data.keys())
        n = f"keys={ks[:8]}"
    results[path] = (st, data, raw, dt)
    flag = "OK " if st == 200 else "!!!"
    p(f"{flag} {path:26s} {str(st):4s} {dt:6.2f}s len={len(raw):7d} {n[:110]}")

p("")
p("=" * 78)
p("PART 2 · 数据关联校验（外键穿透）")
p("=" * 78)


def sym_ok(sym):
    if not sym:
        return False
    st, d, _, _ = g("/api/stock?symbol=" + urllib.parse.quote(str(sym)), 20)
    if st != 200:
        return False
    if not isinstance(d, dict):
        return False
    # 至少要有名字或K线/行情痕迹
    return bool(d.get("name") or d.get("symbol_name") or d.get("quote") or d.get("kline") or d.get("rows"))


def collect_syms(path, extractor, limit=8):
    st, d, _, _ = g(path)
    if st != 200 or d is None:
        return [], f"接口失败 {st}"
    try:
        syms = extractor(d)
    except Exception as e:
        return [], f"提取失败 {e}"
    syms = [s for s in syms if s][:limit]
    bad = [s for s in syms if not sym_ok(s)]
    return syms, bad


checks = []

# /api/stocks
syms, bad = collect_syms("/api/stocks", lambda d: [x.get("symbol") for x in d])
checks.append(("实盘个股榜 /api/stocks → /api/stock", syms, bad))

# /api/consensus
syms, bad = collect_syms("/api/consensus", lambda d: [x.get("symbol") for x in d])
checks.append(("共同持仓共识 /api/consensus → /api/stock", syms, bad))

# /api/lhb
def _lhb(d):
    out = []
    for k in ("top_stocks", "stocks", "rows"):
        for x in (d.get(k) or []):
            out.append(x.get("symbol") or x.get("code"))
    return out
syms, bad = collect_syms("/api/lhb", _lhb)
checks.append(("游资龙虎榜 /api/lhb → /api/stock", syms, bad))

# /api/lhb_sectors
def _lhbs(d):
    out = []
    for r in (d.get("rows") or [])[:3]:
        for o in (r.get("onlist") or [])[:3]:
            out.append(o.get("code"))
    return out
syms, bad = collect_syms("/api/lhb_sectors", _lhbs)
checks.append(("板块动向 /api/lhb_sectors → /api/stock", syms, bad))

# /api/dashboard mainlines
def _dash(d):
    out = []
    ml = d.get("mainlines") or {}
    for r in (ml.get("rows") or [])[:5]:
        out.append(r.get("leader_code"))
        for t in (r.get("tiers") or [])[:2]:
            out.append(t.get("code"))
        for t in (r.get("forward") or [])[:2]:
            out.append(t.get("code"))
    con = d.get("consensus") or {}
    for c in (con.get("rows") or [])[:3]:
        out.append(c.get("symbol"))
    return out
syms, bad = collect_syms("/api/dashboard", _dash, limit=12)
checks.append(("今日看板 /api/dashboard → /api/stock", syms, bad))

# /api/star_moves
def _sm(d):
    out = []
    for r in (d.get("rows") or [])[:1]:
        for g2 in ("new", "add", "cut", "exit"):
            for x in (r.get(g2) or [])[:3]:
                out.append(x.get("symbol"))
    return out
syms, bad = collect_syms("/api/star_moves", _sm)
checks.append(("牛散动向 /api/star_moves → /api/stock", syms, bad))

# /api/cohold_recent
def _ch(d):
    out = []
    for k in ("buy", "sell", "groups"):
        v = d.get(k)
        if isinstance(v, list):
            for x in v[:5]:
                if isinstance(x, dict):
                    out.append(x.get("symbol"))
                    for it in (x.get("items") or [])[:2]:
                        out.append(it.get("symbol"))
    return out
syms, bad = collect_syms("/api/cohold_recent", _ch)
checks.append(("近5日共同买入 /api/cohold_recent → /api/stock", syms, bad))

# /api/niusan
def _ns(d):
    out = []
    for r in (d.get("rows") or [])[:5]:
        out.append(r.get("symbol"))
        for h in (r.get("holders") or [])[:2]:
            out.append(h.get("symbol"))
    return out
syms, bad = collect_syms("/api/niusan", _ns)
checks.append(("牛散持仓 /api/niusan → /api/stock", syms, bad))

for label, syms, bad in checks:
    tag = "OK " if not bad else "!!!"
    p(f"{tag} {label}")
    p(f"      抽查 {len(syms)} 只: {syms}")
    if bad:
        p(f"      ❌ 无法解析的代码: {bad}")

p("")
p("=" * 78)
p("PART 3 · 数据新鲜度 / 一致性")
p("=" * 78)

st, summ, _, _ = g("/api/summary")
cnt = (summ or {}).get("counts", {})
p("summary.counts:", json.dumps(cnt, ensure_ascii=False))

for label, path, key in [
    ("龙虎榜最新日", "/api/lhb", "date"),
    ("板块动向最新日", "/api/lhb_sectors", "date"),
    ("今日看板生成于", "/api/dashboard", "generated_at"),
]:
    st, d, _, _ = g(path)
    if isinstance(d, dict):
        p(f"{label}: {d.get(key)}")

st, d, _, _ = g("/api/star_moves")
if isinstance(d, dict):
    rows = d.get("rows") or []
    p("牛散动向报告期:", [r.get("date") for r in rows][:6])

st, d, _, _ = g("/api/login_status")
if isinstance(d, list):
    p("登录态:", ", ".join(f"{x.get('site')}={x.get('status')}" for x in d))

p("")
p("=" * 78)
p("PART 4 · 详情/K线穿透（抽样 2 只）")
p("=" * 78)
st, stk, _, _ = g("/api/stocks")
sample = []
if isinstance(stk, list) and stk:
    sample = [x.get("symbol") for x in stk[:2]]
for s in sample:
    st, d, raw, dt = g(f"/api/kline?symbol={s}&period=day", 40)
    k = len((d or {}).get("rows") or (d or {}).get("kline") or []) if isinstance(d, dict) else 0
    p(f"K线 {s}: status={st} {dt:.2f}s rows={k} keys={list((d or {}).keys())[:8] if isinstance(d, dict) else d}")
    st, d, raw, dt = g(f"/api/stock?symbol={s}", 20)
    p(f"详情 {s}: status={st} keys={list((d or {}).keys())[:12] if isinstance(d, dict) else d}")
    st, d, raw, dt = g(f"/api/quote?symbol={s}", 20)
    p(f"行情 {s}: status={st} {json.dumps(d, ensure_ascii=False)[:200] if isinstance(d, dict) else d}")

p("")
p("=" * 78)
p("PART 5 · 知识库（跟我学习）")
p("=" * 78)
st, d, _, _ = g("/api/kb/list")
p("kb/list:", json.dumps(d, ensure_ascii=False)[:1500])
for src in (d or {}).get("docs", []) or (d or {}).get("sources", []) or []:
    k = src.get("doc_key") or src.get("key")
    st, dd, _, _ = g("/api/kb/doc?doc_key=" + urllib.parse.quote(str(k)))
    nodes = len((dd or {}).get("nodes") or []) if isinstance(dd, dict) else 0
    p(f"  kb/doc {k}: status={st} nodes={nodes} title={(dd or {}).get('title') if isinstance(dd,dict) else ''}")

with io.open("audit_e2e_out.txt", "w", encoding="utf-8") as f:
    f.write("\n".join(OUT))
p("")
p("[written] audit_e2e_out.txt")
