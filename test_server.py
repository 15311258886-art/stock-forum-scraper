"""临时端到端测试服务器（仅用于本沙箱验证，不进交付）：
- 提供真实前端文件（viewer/index.html + viewer/static/*）
- B4/B7/B8/B6/设置 路由与 server.py 完全一致，直接调用 screener_extra 同名函数
- /api/screener 返回 mock 行（供 UI 渲染；puppeteer 端到端脚本也会拦截注入，双重保险）
这样能在沙箱内真实点击每个按钮、真实打 DeepSeek，验证 UI 接线与后端链路。
"""
import os
import sys
import json
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent
VIEWER = ROOT / "viewer"
STATIC = VIEWER / "static"
sys.path.insert(0, str(VIEWER))
import screener_extra as se  # noqa: E402

CT = {
    ".html": "text/html; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".svg": "image/svg+xml",
    ".ico": "image/x-icon",
    ".json": "application/json; charset=utf-8",
}


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, data, code=200, ct="application/json; charset=utf-8"):
        if isinstance(data, (dict, list)):
            body = json.dumps(data, ensure_ascii=False).encode("utf-8")
            ct = "application/json; charset=utf-8"
        else:
            body = data if isinstance(data, bytes) else str(data).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ct)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, obj, code=200):
        self._send(obj, code)

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        route = parsed.path
        if route in ("/", "/index.html"):
            return self._send((VIEWER / "index.html").read_bytes(), 200, "text/html; charset=utf-8")
        if route.startswith("/static/"):
            rel = route[len("/static/"):]
            p = (STATIC / rel).resolve()
            if (STATIC in p.parents or p == STATIC) and p.exists():
                ext = p.suffix.lower()
                return self._send(p.read_bytes(), 200, CT.get(ext, "application/octet-stream"))
            return self._send_json({"error": "not found"}, 404)
        if route == "/api/screener":
            return self._send_json({"ok": True, "desc": "mock", "rows": [
                {"code": "600000", "name": "浦发银行", "industry": "银行", "pe": 12, "pb": 1.1, "ps": 2, "peg": 1, "pcf": 8, "cap": 50, "pct": 1.2, "holder_num": 5, "worth": 75, "roe": 15, "gm": 30, "dr": 50, "dy": 2},
                {"code": "000001", "name": "平安银行", "industry": "保险", "pe": 9, "pb": 0.9, "ps": 1.5, "peg": 0.8, "pcf": 6, "cap": 40, "pct": -0.5, "holder_num": 4, "worth": 80, "roe": 18, "gm": 35, "dr": 45, "dy": 3.5},
            ]})
        if route == "/api/screener/valuation":
            q = urllib.parse.parse_qs(parsed.query)
            codes = [c.strip() for c in (q.get("codes", [""])[0].split(",")) if c.strip()]
            return self._send_json({"ok": True, "data": se.valuation_pct(codes)})
        if route == "/api/screener/backtest":
            q = urllib.parse.parse_qs(parsed.query)
            codes = [c.strip() for c in (q.get("codes", [""])[0].split(",")) if c.strip()]
            start = q.get("start", ["2024-01-01"])[0]
            end = q.get("end", ["2025-01-01"])[0]
            return self._send_json(se.backtest(codes, start, end))
        if route == "/api/screener/themes":
            return self._send_json({"ok": True, "data": se.themes_map()})
        if route == "/api/valuation_cache/status":
            return self._send_json(se.valuation_cache_status())
        if route == "/api/settings/deepseek_key":
            return self._send_json({"configured": bool(se.get_deepseek_key())})
        self._send_json({"error": "not found"}, 404)

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0) or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            payload = json.loads(raw.decode("utf-8")) if raw else {}
        except Exception:
            payload = {}
        parsed = urllib.parse.urlparse(self.path)
        route = parsed.path
        if route == "/api/nl_screener":
            text = payload.get("text") or ""
            return self._send_json(se.nl_screener(text))
        if route == "/api/settings/deepseek_key":
            key = (payload.get("key") or "").strip()
            if not key:
                return self._send_json({"ok": False, "error": "key 为空"}, 400)
            se.set_deepseek_key(key)
            return self._send_json({"ok": True})
        self._send_json({"error": "not found"}, 404)


if __name__ == "__main__":
    PORT = int(os.environ.get("VIEWER_PORT", "8821"))
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), H)
    print(f"TEST_SERVER_UP port={PORT}", flush=True)
    srv.serve_forever()
