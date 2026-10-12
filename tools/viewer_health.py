"""查看器健康检查：轮询 8765，验证 Request G 的两个端点 + 回归确认旧端点仍在。"""
import json
import time
import urllib.request

PORT = 8765
BASE = f"http://127.0.0.1:{PORT}"


def get(path, timeout=4):
    with urllib.request.urlopen(BASE + path, timeout=timeout) as r:
        return r.status, json.loads(r.read().decode("utf-8"))


ok = False
for _ in range(80):
    try:
        st, d = get("/api/refresh_status", timeout=1)
        ok = True
        break
    except Exception:
        time.sleep(0.25)

if not ok:
    print("FAILED: /api/refresh_status 不可达")
    raise SystemExit(1)

print("GET /api/refresh_status ->", st)
print("  整轮 status   :", d.get("status"))
print("  整轮 last_ok  :", d.get("last_ok_at"))
print("  stale_seconds :", d.get("stale_seconds"), " interval:", d.get("interval_seconds"))
print("  tracked       :", d.get("tracked_sources"))
print("  sources       :", [(x["source_key"], x["display_name"], x["last_ok_at"]) for x in d.get("sources", [])])

for ep in ("/api/stocks", "/api/operating", "/api/persons", "/api/players", "/api/consensus?min_holders=2",
           "/api/hotspots", "/api/summary"):
    try:
        s, dd = get(ep, timeout=8)
        n = len(dd) if isinstance(dd, list) else "obj"
        extra = ""
        if ep == "/api/stocks" and isinstance(dd, list) and dd:
            extra = f" sources[0]={dd[0].get('sources')}"
        print(f"GET {ep} -> {s} ({n}){extra}")
    except Exception as e:
        print(f"GET {ep} -> ERR {e}")

# 静态资源可读 + 关键标记
for path, needles in (
    ("/static/index.html", ["refreshBar", "btnRefreshNow", "rsCountdown"]),
    ("/static/app.js", ["refresh_status", "td-fresh", "freshLegend"]),
):
    try:
        with urllib.request.urlopen(BASE + path, timeout=5) as r:
            body = r.read().decode("utf-8", "ignore")
        miss = [n for n in needles if n not in body]
        print(f"GET {path} -> {r.status}  缺标记:{miss or '无'}")
    except Exception as e:
        print(f"GET {path} -> ERR {e}")

# 回归：载入示例入口应已移除
try:
    with urllib.request.urlopen(BASE + "/static/index.html", timeout=5) as r:
        html = r.read().decode("utf-8", "ignore")
    print("index.html 仍含 btnDemo ?", "btnDemo" in html, " 含 载入示例 ?", "载入示例" in html)
except Exception as e:
    print("index 检查失败", e)
