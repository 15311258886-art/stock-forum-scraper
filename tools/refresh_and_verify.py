"""刷新海外舆情（真实联网数据）并核验来源 URL 是否为真实域名。

用途：B 路线的执行与自证——清掉 overseas 的示例数据，改为联网抓取，
并统计落库 URL 中「真实域名」与「示例占位域名」各占多少。
"""
from __future__ import annotations

import json
import sqlite3
import time
import urllib.request
from pathlib import Path
import os

BASE = "http://127.0.0.1:8765"
DB = Path(os.environ.get("VIEWER_DB") or (Path(__file__).resolve().parent.parent / "viewer" / "app.db"))
PLACEHOLDER = ("example.local", "example.com", "example.org", "example.net", "localhost")


def wait_ready(timeout=30):
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            with urllib.request.urlopen(f"{BASE}/api/summary", timeout=3) as r:
                if r.status == 200:
                    return True
        except Exception:
            time.sleep(0.6)
    return False


def post(path, payload, timeout=180):
    body = json.dumps(payload).encode()
    req = urllib.request.Request(f"{BASE}{path}", data=body,
                                 headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, json.loads(r.read().decode("utf-8", "ignore"))


def count_urls():
    c = sqlite3.connect(str(DB))
    c.row_factory = sqlite3.Row
    out = {}
    for tbl in ("news", "overseas_news"):
        try:
            rows = [r[0] for r in c.execute(f"SELECT url FROM {tbl}")]
        except Exception as e:  # noqa: BLE001
            out[tbl] = {"error": str(e)}
            continue
        real = [u for u in rows if u and u.startswith("http") and not any(p in u for p in PLACEHOLDER)]
        ph = [u for u in rows if u and any(p in u for p in PLACEHOLDER)]
        out[tbl] = {"total": len(rows), "real": len(real), "placeholder": ph and len(ph) or 0,
                    "sample_real": real[:4], "sample_ph": (ph or [])[:2]}
    return out


if __name__ == "__main__":
    if not wait_ready():
        raise SystemExit("服务未就绪（8765）")
    print("服务就绪，开始联网抓取海外舆情 …")
    t0 = time.time()
    st, res = post("/api/overseas", {"live": True, "reset": True})
    print(f"HTTP {st}  耗时 {time.time() - t0:.1f}s")
    print("返回摘要：", json.dumps(res.get("result", res), ensure_ascii=False)[:400])
    print()
    print("=== 落库 URL 核验 ===")
    print(json.dumps(count_urls(), ensure_ascii=False, indent=2))
