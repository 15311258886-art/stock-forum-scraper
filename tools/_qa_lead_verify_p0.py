# -*- coding: utf-8 -*-
"""团队长独立复核：路径穿越是否真的成立（只读 GET，绕过代理）。"""
import urllib.request

OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))
BASE = "http://127.0.0.1:8765"
out = []

CASES = [
    "/static/../server.py",
    "/static/../../viewer/app.db",
    "/static/../../tools/_cookies_latest.json",
    "/static/../../.browser-profile/Default/Network/Cookies",
    "/static/%2e%2e%2fserver.py",
    "/api/summary",
]
out.append(f"{'path':<58}{'HTTP':<7}{'bytes':<11}前 24 字节")
for p in CASES:
    try:
        with OPENER.open(BASE + p, timeout=15) as r:
            body = r.read()
        head = body[:24]
        # 只保留可打印 ASCII，避免二进制内容把输出文件判成二进制
        prev = "".join(chr(b) if 32 <= b < 127 else "." for b in head)
        out.append(f"{p:<58}{r.status:<7}{len(body):<11}{prev}")
    except Exception as e:
        out.append(f"{p:<58}FAIL   {e!r}")

txt = "\n".join(out)
print(txt)
with open("tools/_qa_lead_verify_p0.txt", "w", encoding="utf-8") as f:
    f.write(txt + "\n")
