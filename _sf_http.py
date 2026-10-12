import urllib.request

# 验证当前监听 8765 的查看器实例实际能返回首页（绕过系统代理直连 localhost）
for path in ("/",):
    try:
        req = urllib.request.Request("http://127.0.0.1:8765" + path)
        resp = urllib.request.urlopen(req, timeout=10)
        body = resp.read(200)
        print("HTTP", path, "->", resp.status, repr(body[:80]))
    except Exception as e:
        print("HTTP", path, "-> ERR", type(e).__name__, str(e)[:120])

# 顺带验证一个轻量业务接口是否可用（/api/extref_sources 仅举例，失败不致命）
for path in ("/api/extref_sources", "/api/board"):
    try:
        req = urllib.request.Request("http://127.0.0.1:8765" + path)
        resp = urllib.request.urlopen(req, timeout=10)
        print("HTTP", path, "->", resp.status, "len", len(resp.read(500)))
    except Exception as e:
        print("HTTP", path, "-> ERR", type(e).__name__, str(e)[:80])
