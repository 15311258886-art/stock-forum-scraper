"""修正版：板块热度(归一化) + 股东户数百分位5档 + 分布可行性。"""
import sys, os, sqlite3, json
sys.path.insert(0, "viewer")
import predictor
con = predictor.connect()
con.row_factory = sqlite3.Row

_codes, _names, _b = predictor.SS.universe(predictor.POOL_LIMIT)
codes = [str(c).strip().zfill(6) for c in _codes if str(c).strip().isdigit()]
pool = set(codes)
print("POOL:", len(pool))
idx = predictor._industry_index(con)
sym_words = {}
for w, cs in idx.items():
    for c in cs:
        sym_words.setdefault(c, set()).add(w)

def pct_norm(contrib):
    if not contrib: return {}
    vals = sorted(contrib.values())
    return {w: predictor._pct_le(vals, v) for w, v in contrib.items()}

# 各源原始贡献
pol, ovs, bf, sen, fol = {}, {}, {}, {}, {}
for r in con.execute("SELECT sectors, importance, level FROM policy_news"):
    try: ws = json.loads(r["sectors"] or "[]")
    except Exception: ws = []
    if not isinstance(ws, list): continue
    imp = float(r["importance"] or 1.0); lvl = 1.5 if str(r["level"] or "") == "中央" else 1.0
    for w in ws: pol[str(w)] = pol.get(str(w), 0.0) + imp * lvl
for r in con.execute("SELECT domestic_sectors, heat FROM overseas_hotspots"):
    try: ws = json.loads(r["domestic_sectors"] or "[]") if r["domestic_sectors"] else []
    except Exception: ws = []
    if isinstance(ws, list):
        for w in ws: ovs[str(w)] = ovs.get(str(w), 0.0) + float(r["heat"] or 1.0)
for r in con.execute("SELECT board_name, main_net FROM board_flow_samples WHERE kind='industry' AND main_net>0"):
    bf[str(r["board_name"])] = bf.get(str(r["board_name"]), 0.0) + float(r["main_net"] or 0)
sent = predictor.sig_sentiment(con)
for sym, v in sent.items():
    for w in sym_words.get(sym, set()): sen[w] = sen.get(w, 0.0) + v["strength"] * 0.1
try:
    follow = predictor.sig_follow(con)
    for sym, v in follow.items():
        for w in sym_words.get(sym, set()): fol[w] = fol.get(w, 0.0) + v["strength"] * 0.1
except Exception as e:
    print("follow err:", e)

# 归一化后跨源求和
npol, novs, nbf, nsen, nfol = pct_norm(pol), pct_norm(ovs), pct_norm(bf), pct_norm(sen), pct_norm(fol)
heat = {}
for src in (npol, novs, nbf, nsen, nfol):
    for w, v in src.items():
        # 板块资金/政策权重高（直接资金/政策信号），情绪/跟随次之
        wgt = {"policy":1.0,"overseas":0.8,"board":1.0,"sentiment":0.5,"follow":0.5}[
            "policy" if src is npol else "overseas" if src is novs else "board" if src is nbf
            else "sentiment" if src is nsen else "follow"]
        heat[w] = heat.get(w, 0.0) + v * wgt
board_scores = {w: (sc, len(idx.get(w, set()) & pool)) for w, sc in heat.items() if idx.get(w, set()) & pool}
top = sorted(board_scores.items(), key=lambda x: -x[1][0])[:10]
print("\n=== 归一化板块热度 TOP10（词, 热度, 池内票）===")
for w, (sc, n) in top:
    print("  %-12s heat=%5.2f 池内=%d" % (w, sc, n))
for N in (5, 6, 8):
    keep = set()
    for w, _, in top[:N]: keep |= (idx.get(w, set()) & pool)
    print("硬GATE 前%d板块 → 候选池保留 %d/%d" % (N, len(keep), len(pool)))

# 个股 -> 一级行业
sec1 = {}
for r in con.execute("SELECT code, industry FROM stock_org WHERE industry IS NOT NULL AND industry!=''"):
    c = str(r["code"] or "").strip().zfill(6)
    if c in pool: sec1[c] = str(r["industry"]).split("-")[0].strip()
from collections import defaultdict, Counter
by_sec = defaultdict(list)
for c in pool: by_sec[sec1.get(c, "未知")].append(c)

# 股东户数最新期
hn = {}
for r in con.execute("SELECT symbol, end_date, holder_total_num FROM stock_holder_num WHERE symbol IS NOT NULL AND holder_total_num IS NOT NULL"):
    sym = str(r["symbol"]).strip().zfill(6)
    if sym not in pool: continue
    hn.setdefault(sym, []).append((str(r["end_date"] or ""), float(r["holder_total_num"])))
hn_latest = {}
for sym, lst in hn.items():
    lst.sort(); hn_latest[sym] = lst[-1][1]
print("\n有股东户数数据的票: %d/%d" % (len(hn_latest), len(pool)))

# 池内股东户数百分位5档
have = [c for c in pool if c in hn_latest]
vals = sorted(hn_latest[c] for c in have)
def qtile(p):
    if not vals: return 0
    i = int(p * (len(vals) - 1)); return vals[i]
cut = [qtile(0.2), qtile(0.4), qtile(0.6), qtile(0.8)]
def conc_tier(n):
    if n <= cut[0]: return "非常集中"
    if n <= cut[1]: return "集中"
    if n <= cut[2]: return "相对集中"
    if n <= cut[3]: return "分散"
    return "发散性"
tier = {c: conc_tier(hn_latest[c]) for c in have}
print("=== 股东户数百分位5档（全池）===")
for t in ("非常集中", "集中", "相对集中", "分散", "发散性"):
    print("  %-6s %d" % (t, Counter(tier.values()).get(t, 0)))

# 板块内前30%（一级行业，户数最小）
top30 = set()
for s, lst in by_sec.items():
    lst2 = [c for c in lst if c in hn_latest]
    if not lst2: continue
    lst2.sort(key=lambda x: hn_latest[x])
    k = max(1, int(len(lst2) * 0.30))
    top30 |= set(lst2[:k])
print("\n板块内(一级)股东户数前30% 覆盖 %d 只" % len(top30))
print("前30% 且 集中度档<=相对集中: %d" % len(top30 & {c for c, v in tier.items() if v in ("非常集中","集中","相对集中")}))

# 三层交集（硬gate前6 且 股东前30%或集中度优）
gate6 = set()
for w, _, in top[:6]: gate6 |= (idx.get(w, set()) & pool)
strong = gate6 & (top30 | {c for c, v in tier.items() if v in ("非常集中","集中","相对集中")})
print("\n=== 三层交集 ===")
print("硬gate前6板块: %d 只" % len(gate6))
print("其中 股东强: %d 只" % len(strong))
print("仅硬gate前6(不卡股东): %d 只 → 核心池够10? %s" % (len(gate6), "YES" if len(gate6) >= 10 else "NO"))
con.close()
