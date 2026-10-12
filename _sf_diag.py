import sqlite3, urllib.request, urllib.error

db = r"F:\AI Agent部署知识库\2026-09-22-11-49-42\stock-forum-scraper\viewer\app.db"

# 1) 主库完整性
try:
    c = sqlite3.connect(db)
    r = c.execute("PRAGMA integrity_check").fetchone()
    print("INTEGRITY_CHECK:", r[0])
    n = c.execute("SELECT count(*) FROM sqlite_master WHERE type='table'").fetchone()[0]
    print("TABLE_COUNT:", n)
    c.close()
except Exception as e:
    print("INTEGRITY_ERROR:", type(e).__name__, e)

# 2) 实际 HTTP 响应（首页 + 一个轻量 API）
for path in ("/", "/api/status", "/api/health"):
    try:
        req = urllib.request.Request("http://127.0.0.1:8765" + path)
        resp = urllib.request.urlopen(req, timeout=8)
        body = resp.read(300)
        print("HTTP", path, "->", resp.status, repr(body[:80]))
    except urllib.error.HTTPError as e:
        print("HTTP", path, "->", e.code)
    except Exception as e:
        print("HTTP", path, "-> ERR", type(e).__name__, str(e)[:80])
