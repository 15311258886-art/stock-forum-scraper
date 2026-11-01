"""改版前前端回放服务（只读，纯诊断用）。

用途：进哥要找回改版前顺手的功能，需要看到真实的旧界面。
做法：把 git 历史里某一版的前端（viewer/static）原样拿出来，
     配一个极简代理 —— 静态文件读旧版，/api/* 转发给正在跑的 8765。

为什么这么做，而不是"checkout 旧代码再起一个后端"：
  · 旧后端会带调度器，可能写生产库；本进程只读静态文件 + 转发 GET，零写风险。
  · 后端只有一份（新的），旧前端调的接口大多仍兼容，够用来"认功能"。

启动：
  .venv/Scripts/python.exe tools/replay_old_ui.py --root _oldui/viewer/static --port 8766
"""

from __future__ import annotations

import argparse
import http.server
import os
import socketserver
import sys
import urllib.error
import urllib.request

BACKEND = os.environ.get("REPLAY_BACKEND", "http://127.0.0.1:8765")
ROOT = "."

# 转发时不带过去的头（逐跳头 + 由本进程重算的）
_DROP_REQ = {"host", "connection", "keep-alive", "proxy-authenticate",
             "proxy-authorization", "te", "trailers", "transfer-encoding", "upgrade"}
_DROP_RESP = {"connection", "keep-alive", "transfer-encoding", "content-encoding",
              "content-length", "set-cookie"}


class Handler(http.server.SimpleHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def __init__(self, *a, **kw):
        super().__init__(*a, directory=ROOT, **kw)

    # ---------- 转发 ----------
    def _proxy(self, method: str) -> None:
        url = BACKEND + self.path
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else None
        req = urllib.request.Request(url, data=body, method=method)
        for k, v in self.headers.items():
            if k.lower() not in _DROP_REQ:
                req.add_header(k, v)
        try:
            # 显式禁用环境代理：这台机器上走代理会得到 502，会被误判成"后端挂了"
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with opener.open(req, timeout=120) as r:
                data = r.read()
                self.send_response(r.status)
                for k, v in r.headers.items():
                    if k.lower() not in _DROP_RESP:
                        self.send_header(k, v)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
        except urllib.error.HTTPError as e:
            data = e.read()
            self.send_response(e.code)
            for k, v in e.headers.items():
                if k.lower() not in _DROP_RESP:
                    self.send_header(k, v)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        except Exception as e:  # 后端不可达时明确说，不要静默空响应
            msg = ('{"error":"replay proxy failed: %s"}' % e).encode()
            self.send_response(502)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(msg)))
            self.end_headers()
            self.wfile.write(msg)

    # ---------- 路由 ----------
    def translate_path(self, path: str) -> str:
        """旧 index.html 里写的是 `src="static/app.js"`，实际文件在 ROOT 根下。
        在这里统一去掉 /static 前缀 —— 比改 self.path 稳（self.path 还会被父类
        用于日志与目录列表，改它容易两头不一致）。"""
        p = (path or "/").split("?")[0].split("#")[0]
        if p in ("", "/"):
            p = "/index.html"
        elif p.startswith("/static/"):
            p = p[len("/static"):]
        return super().translate_path(p)

    def _route(self, method: str) -> None:
        path = self.path.split("?")[0]
        if path.startswith("/api/"):
            self._proxy(method)
            return
        if method != "GET":
            self._proxy(method)          # 其它写接口一律原样转给后端
            return
        super().do_GET()

    def do_GET(self):  # noqa: N802
        self._route("GET")

    def do_HEAD(self):  # noqa: N802
        path = self.path.split("?")[0]
        if path.startswith("/api/"):
            self._proxy("HEAD")
            return
        super().do_HEAD()

    def do_POST(self):  # noqa: N802
        self._route("POST")

    def end_headers(self):
        # 回放页不该被缓存，改了 _oldui 立刻生效
        if not self.path.startswith("/api/"):
            self.send_header("Cache-Control", "no-store")
        # 旧页面要嵌 iframe（fund_masters），别被 SAMEORIGIN 之外的策略拦
        super().end_headers()

    def log_message(self, fmt, *args):
        if os.environ.get("REPLAY_VERBOSE"):
            sys.stderr.write("[replay] " + fmt % args + "\n")


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def main() -> int:
    global ROOT, BACKEND
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default="_oldui/viewer/static")
    ap.add_argument("--port", type=int, default=8766)
    ap.add_argument("--backend", default=BACKEND)
    a = ap.parse_args()
    ROOT = os.path.abspath(a.root)
    BACKEND = a.backend.rstrip("/")
    if not os.path.isdir(ROOT):
        print("根目录不存在:", ROOT)
        return 2
    with Server(("127.0.0.1", a.port), Handler) as httpd:
        print(f"[replay] 旧前端 {ROOT}  ->  http://127.0.0.1:{a.port}/  (API -> {BACKEND})")
        sys.stdout.flush()
        httpd.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
