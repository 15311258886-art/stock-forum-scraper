"""安全专项只读探测（临时 QA 脚本，不属于产品代码）。

只做两类无害操作：
1. 原始 socket 发送 GET（绕过任何客户端 URL 规范化，用于验证路径穿越）。
2. urllib（显式禁用 proxy）发送 GET，检查响应头与错误处理。

**不调用任何写接口**，不修改 DB / 源码 / 服务。
结果写入 tools/_qa_security_probe.out（UTF-8）。
"""
from __future__ import annotations

import hashlib
import json
import socket
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

HOST, PORT = "127.0.0.1", 8765
ROOT = Path(__file__).resolve().parent.parent
OUT = Path(__file__).resolve().parent / "_qa_security_probe.out"
lines: list[str] = []


def log(*a):
    lines.append(" ".join(str(x) for x in a))


# ---------------------------------------------------------------- 原始 socket
def raw_get(target: str, host_header: str = f"127.0.0.1:{PORT}", extra: str = "") -> dict:
    """发送原始请求行，不做任何规范化；返回状态行/头/正文前若干字节。"""
    req = (f"GET {target} HTTP/1.1\r\nHost: {host_header}\r\n"
           f"User-Agent: qa-sec-probe\r\nAccept: */*\r\n{extra}Connection: close\r\n\r\n")
    try:
        with socket.create_connection((HOST, PORT), timeout=6) as s:
            s.sendall(req.encode("latin-1", "replace"))
            buf = b""
            while True:
                chunk = s.recv(65536)
                if not chunk:
                    break
                buf += chunk
                if len(buf) > 400_000:
                    break
    except Exception as e:  # noqa: BLE001
        return {"target": target, "error": f"{type(e).__name__}: {e}", "raw": b""}
    head, _, body = buf.partition(b"\r\n\r\n")
    hl = head.decode("latin-1", "replace").split("\r\n")
    status = hl[0] if hl else "<no response>"
    headers = {}
    for h in hl[1:]:
        if ":" in h:
            k, v = h.split(":", 1)
            headers[k.strip().lower()] = v.strip()
    return {"target": target, "status": status, "headers": headers,
            "blen": len(body), "body_head": body[:160], "raw": buf}


def probe_traversal():
    log("=" * 78)
    log("[A] 路径穿越：原始 socket GET（不规范化 URL）")
    log("=" * 78)
    payloads = [
        "/static/app.js",                          # 基线：应 200
        "/static/__nope__.js",                     # 基线：应 404
        "/static/../server.py",                    # 经典 ../
        "/static/../desktop_app.py",
        "/static/../app.db",
        "/static/../../stock-forum-scraper/viewer/server.py",
        "/static/subdir/../../../viewer/server.py",
        "/static/..\\server.py",                    # Windows 反斜杠
        "/static/..%2fserver.py",                  # 编码斜杠
        "/static/%2e%2e%2fserver.py",              # 编码 ../
        "/static/..%5cserver.py",                  # 编码反斜杠
        "/static//../server.py",
        "/static/....//server.py",
        "/static/..;/server.py",
    ]
    for t in payloads:
        r = raw_get(t)
        if r.get("error"):
            log(f"  {t:52s} -> {r['error']}")
            continue
        ct = r["headers"].get("content-type", "-")
        cl = r["headers"].get("content-length", "-")
        log(f"  {t:52s} -> {r['status']}  type={ct}  len={cl}  body={r['blen']}B")
        if r["status"].startswith("HTTP/1.1 200") and r["blen"]:
            head = r["body_head"]
            if t.endswith("app.db") or "app.db" in t:
                log(f"      body[0:64] hex = {head[:64].hex()}")
            else:
                txt = head.decode("utf-8", "replace").replace("\n", " ")[:120]
                log(f"      body[0:120] = {txt!r}")
    return payloads


# ------------------------------------------------------------------- HTTP 头
def http_get(path: str, headers: dict | None = None) -> dict:
    op = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # 绕过 http_proxy
    req = urllib.request.Request(f"http://{HOST}:{PORT}{path}", headers=headers or {})
    try:
        with op.open(req, timeout=8) as r:
            body = r.read(4000)
            return {"path": path, "code": r.status, "headers": dict(r.headers), "blen": len(body),
                    "body": body}
    except urllib.error.HTTPError as e:
        b = e.read(2000)
        return {"path": path, "code": e.code, "headers": dict(e.headers), "blen": len(b), "body": b}
    except Exception as e:  # noqa: BLE001
        return {"path": path, "error": f"{type(e).__name__}: {e}"}


def probe_headers_and_errors():
    log("")
    log("=" * 78)
    log("[B] 响应头 / CORS / 错误处理（urllib 禁用 proxy）")
    log("=" * 78)
    cases = [
        "/api/summary",
        "/api/overseas_hotspots",
        "/static/app.js",
        "/static/__nope__.js",
        "/api/__nope__",
    ]
    for p in cases:
        r = http_get(p)
        if r.get("error"):
            log(f"  {p:34s} -> {r['error']}")
            continue
        h = {k.lower(): v for k, v in r["headers"].items()}
        log(f"  {p:34s} -> {r['code']}  ACAO={h.get('access-control-allow-origin', '（缺失）')}"
            f"  CT={h.get('content-type', '-')}  X-CTO={h.get('x-content-type-options', '（缺失）')}"
            f"  CSP={h.get('content-security-policy', '（缺失）')}")

    log("")
    log("  -- 带 Origin 头的跨源 GET（浏览器跨站读取场景）--")
    for p in ("/api/summary", "/static/app.js", "/"):
        r = http_get(p, {"Origin": "https://evil.example"})
        if r.get("error"):
            log(f"  {p:16s} -> {r['error']}")
            continue
        h = {k.lower(): v for k, v in r["headers"].items()}
        log(f"  {p:16s} -> {r['code']}  ACAO={h.get('access-control-allow-origin', '（缺失）')}")

    log("")
    log("  -- 伪造 Host 头（DNS rebinding 面：Host 是否被校验）--")
    for hv in ("evil.example", "attacker.local", ""):
        r = raw_get("/api/summary", host_header=hv or "127.0.0.1")
        log(f"  Host={hv or '(空)':16s} -> {r.get('status', r.get('error'))}")

    log("")
    log("  -- 参数非法时的错误处理（预期 500/断连，不应泄露 traceback）--")
    for p in ("/api/posts?limit=abc", "/api/consensus?min_holders=x", "/api/detail?id=xyz"):
        r = http_get(p)
        if r.get("error"):
            log(f"  {p:34s} -> {r['error']}")
            continue
        body = r["body"].decode("utf-8", "replace").replace("\n", " ")[:160]
        log(f"  {p:34s} -> {r['code']}  body={body!r}")
        if "Traceback" in body or "File \"" in body or "\\" in body:
            log("      !! 正文疑似包含 traceback / 本地路径")


def probe_write_endpoints_shape_only():
    """仅记录写接口的“缺失防御措施”，不实际发起写请求。"""
    log("")
    log("=" * 78)
    log("[C] 写接口防御措施（代码级核对，未发起任何写请求）")
    log("=" * 78)
    log("  端点            | Content-Type 校验 | Origin/Referer 校验 | CSRF token | 自定义头")
    rows = [
        ("POST /api/login_check", "无（json.loads 失败即 payload={}）", "无", "无", "无"),
        ("POST /api/fetch", "无", "无", "无", "无"),
        ("POST /api/demo", "无", "无", "无", "无"),
        ("POST /api/overseas", "无", "无", "无", "无"),
        ("POST /api/sync_names", "无", "无", "无", "无"),
    ]
    for r in rows:
        log("  " + " | ".join(x.ljust(12) for x in r))
    log("  说明：server.py:637-687 只按 route 分派，未读取 Origin/Referer/Host，"
        "也未要求自定义头或 token；请求体解析失败被静默降级为 {}（641-643 行）。")


def main():
    try:
        probe_traversal()
        probe_headers_and_errors()
        probe_write_endpoints_shape_only()
    finally:
        OUT.write_text("\n".join(lines) + "\n", encoding="utf-8")
        print("written:", OUT)


if __name__ == "__main__":
    main()
