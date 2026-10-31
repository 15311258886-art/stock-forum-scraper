"""线缆字节实测工具 —— 用于验收 DEF-2026-016 的关闭条件。

为什么不用 curl / 不用 Content-Length 求和：
  * curl 的 `%{size_download}` 只数正文，不含响应头；
  * Content-Length 之和既不含头，也不反映 304 的真实成本；
  * 浏览器的 `Network.encodedDataLength` 才是「网卡上过去了多少字节」的定义。
本工具用裸 socket 发请求并统计收到的全部字节（状态行 + 响应头 + 正文），
定义上与 `Network.encodedDataLength` 对齐，且不依赖浏览器调试端口，CI 可跑。

用法：
    .venv/Scripts/python.exe tools/measure_wire.py 8799

输出分两个口径，因为「首屏 JS ≤ 450 KB」这条验收对口径很敏感：
  * 正文口径（仅 Content-Length 覆盖的字节）
  * 含头口径（真实线缆字节，含每个响应的 ~300 B 协议开销）
两者差 10 个响应 × ~300 B ≈ 3 KB，恰好是能不能卡在 460,800 B 里的量级。
"""

from __future__ import annotations

import re
import socket
import sys
from pathlib import Path

HOST = "127.0.0.1"
PORT_DEFAULT = 8799
REPO_ROOT = Path(__file__).resolve().parent.parent
INDEX_HTML = REPO_ROOT / "viewer" / "static" / "index.html"
TIMEOUT_S = 30.0
RECV_CHUNK = 65536
KB = 1024
FIRST_SCREEN_JS_LIMIT = 450 * KB
REVISIT_LIMIT = 5 * KB


def load_first_screen_assets() -> list[str]:
    """从 index.html 抽出首屏资源路径（跳过 data: 内联与外部 URL）。"""
    html = INDEX_HTML.read_text(encoding="utf-8")
    paths: list[str] = ["/"]
    seen: set[str] = {"/"}
    pattern = r'<(?:script|link)[^>]*(?:src|href)="([^"]+)"'
    for match in re.finditer(pattern, html):
        url = match.group(1)
        if url.startswith(("data:", "http://", "https://", "//")):
            continue
        url = "/" + url.lstrip("/")
        if url not in seen:
            seen.add(url)
            paths.append(url)
    return paths


def is_javascript(path: str) -> bool:
    """判断资源是否 JS（去掉 query string 后看扩展名）。"""
    clean = path.split("?", 1)[0]
    return clean.endswith(".js")


def raw_request(port: int, path: str, extra_headers: dict[str, str] | None = None) -> dict:
    """发一次 GET，返回状态码、响应头、线缆字节数、正文字节数。"""
    lines = [
        f"GET {path} HTTP/1.1",
        f"Host: {HOST}:{port}",
        "Accept-Encoding: gzip",
        "Connection: close",
        "User-Agent: wire-meter/1.0",
    ]
    for key, value in (extra_headers or {}).items():
        lines.append(f"{key}: {value}")
    payload = ("\r\n".join(lines) + "\r\n\r\n").encode("latin-1")

    sock = socket.create_connection((HOST, port), timeout=TIMEOUT_S)
    try:
        sock.sendall(payload)
        buffers: list[bytes] = []
        while True:
            chunk = sock.recv(RECV_CHUNK)
            if not chunk:
                break
            buffers.append(chunk)
    finally:
        sock.close()

    raw = b"".join(buffers)
    head, _, body = raw.partition(b"\r\n\r\n")
    head_text = head.decode("latin-1")
    status = int(head_text.split(" ", 2)[1])
    headers: dict[str, str] = {}
    for line in head_text.split("\r\n")[1:]:
        if ":" in line:
            name, value = line.split(":", 1)
            headers[name.strip().lower()] = value.strip()
    return {
        "path": path,
        "status": status,
        "headers": headers,
        "wire": len(raw),
        "body": len(body),
    }


def summarize(label: str, rows: list[dict]) -> tuple[int, int]:
    """打印一组行的小计，返回 (正文合计, 含头合计)。"""
    body_sum = sum(row["body"] for row in rows)
    wire_sum = sum(row["wire"] for row in rows)
    print(f"  {label}（{len(rows)} 个）：正文 {body_sum:,} B，含头 {wire_sum:,} B = {wire_sum / KB:.2f} KB")
    return body_sum, wire_sum


def main(argv: list[str]) -> int:
    port = int(argv[1]) if len(argv) > 1 else PORT_DEFAULT
    assets = load_first_screen_assets()
    print(f"首屏资源数（含文档）：{len(assets)}   目标端口：{HOST}:{port}")

    print("\n--- 首访（冷启动，全量传输） ---")
    visits = [raw_request(port, path) for path in assets]
    for row in visits:
        encoding = row["headers"].get("content-encoding", "-")
        print(f"  {row['status']} {row['wire']:>9,} B  ce={encoding:<5} {row['path']}")
    all_body, all_wire = summarize("全页", visits)
    js_visits = [row for row in visits if is_javascript(row["path"])]
    js_body, js_wire = summarize("JS 子集", js_visits)
    print(f"  阈值 {FIRST_SCREEN_JS_LIMIT:,} B（450 KB×1024）"
          f" → 正文余量 {FIRST_SCREEN_JS_LIMIT - js_body:+,} B，"
          f"含头余量 {FIRST_SCREEN_JS_LIMIT - js_wire:+,} B")

    print("\n--- 二访（带 If-None-Match 复验） ---")
    revisits = []
    for row in visits:
        etag = row["headers"].get("etag")
        if not etag:
            continue
        again = raw_request(port, row["path"], {"If-None-Match": etag})
        revisits.append(again)
        print(f"  {again['status']} {again['wire']:>9,} B  body={again['body']:<7} {again['path']}")
    hit_304 = sum(1 for row in revisits if row["status"] == 304)
    revisit_body, revisit_wire = summarize("全页", revisits)
    js_revisits = [row for row in revisits if is_javascript(row["path"])]
    _, js_revisit_wire = summarize("JS 子集", js_revisits)
    print(f"  304 命中 {hit_304}/{len(revisits)}，全页降幅 "
          f"{100.0 * (1 - revisit_wire / all_wire) if all_wire else 0:.1f}%")
    print(f"  阈值 {REVISIT_LIMIT:,} B（5 KB×1024）"
          f" → 全页余量 {REVISIT_LIMIT - revisit_wire:+,} B，"
          f"JS 余量 {REVISIT_LIMIT - js_revisit_wire:+,} B")
    print(f"\n  （全页首访正文 {all_body:,} B 含头 {all_wire:,} B；二访正文 {revisit_body:,} B）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
