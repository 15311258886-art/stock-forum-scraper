# -*- coding: utf-8 -*-
"""动态功能测试 第三轮（只读）：百分号编码参数 + 跨接口口径差异定位。"""
from __future__ import annotations

import json
import socket
from pathlib import Path
from urllib.parse import quote

HOST, PORT = "127.0.0.1", 8765
OUT = Path(__file__).resolve().parent / "_qa_dyn_out3.txt"
LOG: list[str] = []


def raw_get(path: str, timeout: float = 30.0) -> dict:
    req = (f"GET {path} HTTP/1.1\r\nHost: {HOST}:{PORT}\r\n"
           f"Connection: close\r\nUser-Agent: qa-dyn-probe3\r\n\r\n")
    try:
        s = socket.create_connection((HOST, PORT), timeout=timeout)
        s.settimeout(timeout)
        s.sendall(req.encode("utf-8", "surrogateescape"))
        buf = b""
        while True:
            d = s.recv(65536)
            if not d:
                break
            buf += d
        s.close()
    except Exception as e:  # noqa: BLE001
        return {"error": f"{type(e).__name__}: {e}", "body": b"", "status": None}
    if not buf:
        return {"error": "EMPTY_REPLY", "body": b"", "status": None}
    sep = buf.find(b"\r\n\r\n")
    if sep < 0:
        return {"error": "NO_HEADER_END", "body": buf, "status": None}
    head, body = buf[:sep].decode("iso-8859-1"), buf[sep + 4:]
    status = None
    try:
        status = int(head.split(" ")[1])
    except Exception:  # noqa: BLE001
        pass
    return {"status": status, "body": body, "error": None}


def jget(path: str):
    r = raw_get(path)
    try:
        return json.loads(r["body"].decode("utf-8")), r
    except Exception:  # noqa: BLE001
        return None, r


def main() -> None:
    LOG.append("### A. 百分号编码的中文参数（浏览器/前端 encodeURIComponent 的真实行为）")
    for label, path in [
        ("person-短线作手老王", "/api/person?name=" + quote("短线作手老王")),
        ("person-趋势猎人", "/api/person?name=" + quote("趋势猎人")),
        ("player-大阪柯小李", "/api/player?name=" + quote("大阪柯小李")),
        ("player-打板客小李", "/api/player?name=" + quote("打板客小李")),
    ]:
        obj, r = jget(path)
        if obj is None:
            LOG.append(f"[{label}] {path} -> 无 JSON（{r['error'] or r['status']}）")
            continue
        if isinstance(obj, dict) and obj.get("error"):
            LOG.append(f"[{label}] {path} -> {{'error': {obj['error']!r}}}（该样本不存在）")
            continue
        LOG.append(f"[{label}] status={r['status']} bytes={len(r['body'])}")
        for k, v in obj.items():
            if isinstance(v, dict):
                LOG.append(f"    {k}: dict keys={sorted(v.keys())}")
            elif isinstance(v, list):
                inner = sorted(v[0].keys()) if v and isinstance(v[0], dict) else (
                    f"<{type(v[0]).__name__}>" if v else "[]")
                LOG.append(f"    {k}: list n={len(v)} item_keys={inner}")
            else:
                LOG.append(f"    {k}: {type(v).__name__} = {json.dumps(v, ensure_ascii=False)[:120]}")

    LOG.append("")
    LOG.append("### B. 交叉口径差异定位（000333：consensus 9 人 vs positions 去重 10 人）")
    stocks, _ = jget("/api/stocks")
    positions, _ = jget("/api/positions")
    cons1, _ = jget("/api/consensus?min_holders=1")
    cons_map = {c["symbol"]: c for c in cons1 or []}
    for s in stocks or []:
        sym = s["symbol"]
        d, _ = jget(f"/api/stock?symbol={sym}")
        rows = [p for p in positions or [] if p["symbol"] == sym]
        raw_players = {p.get("player") for p in rows if p.get("player")}
        c = cons_map.get(sym)
        if (c or {}).get("holders") != len(raw_players):
            LOG.append(f"  {sym}: /api/stock.holders={d.get('holders') if d else None} "
                       f"/api/positions 去重={len(raw_players)} "
                       f"/api/consensus.holders={(c or {}).get('holders')} "
                       f"consensus 明细players={len((c or {}).get('players') or [])} "
                       f"其中 cleared=True 的={sum(1 for p in ((c or {}).get('players') or []) if p.get('cleared'))}")

    LOG.append("")
    LOG.append("### C. /api/stocks 24 只中，哪些不在 /api/consensus?min_holders=1 内")
    miss = [s["symbol"] for s in (stocks or []) if s["symbol"] not in cons_map]
    LOG.append(f"  缺失：{miss}")
    for sym in miss:
        rows = [p for p in positions or [] if p["symbol"] == sym]
        LOG.append(f"    {sym}: /api/positions 条数={len(rows)} "
                   f"players={[p.get('player') for p in rows]} "
                   f"actions={[p.get('action') for p in rows]} ratio={[p.get('ratio') for p in rows]}")

    LOG.append("")
    LOG.append("### D. /api/stock 的 views / view_summary 与 mention_count 是否自洽（全量 24 只）")
    for s in stocks or []:
        d, _ = jget(f"/api/stock?symbol={s['symbol']}")
        if not d:
            continue
        vs = d.get("views") or {}
        n = len(vs.get("bull", [])) + len(vs.get("bear", [])) + len(vs.get("neutral", []))
        if n != d.get("mention_count") or n != len(d.get("mentions") or []):
            LOG.append(f"  !! {s['symbol']} views 合计={n} mention_count={d.get('mention_count')} "
                       f"len(mentions)={len(d.get('mentions') or [])}")
    LOG.append("  （无 !! 行即全部自洽）")

    LOG.append("")
    LOG.append("### E. /api/detail 支持 kind 与真实 id")
    for kind in ("post", "trade", "news"):
        for rid in (1, 2, 3, 4143, 15952):
            obj, r = jget(f"/api/detail?kind={kind}&id={rid}")
            if obj and not obj.get("error"):
                LOG.append(f"  kind={kind} id={rid} -> OK keys={sorted(obj.keys())}")
    LOG.append("  news 表有 3 行但 id=1..3 均 not found（示例库 news.id 非 1 起）")

    LOG.append("")
    LOG.append("### F. 服务仍在监听（异常探针后存活确认）+ 空参默认值")
    canary, r = jget("/api/summary")
    LOG.append(f"  canary /api/summary status={r['status']} ok={bool(canary)}")
    for path in ("/api/consensus?min_holders=%20", "/api/consensus?min_holders=%20%20",
                 "/api/posts?limit=0", "/api/players?tier=&channel="):
        rr = raw_get(path)
        LOG.append(f"  {path} -> status={rr['status']} err={rr['error']} bytes={len(rr['body'])}")

    OUT.write_text("\n".join(LOG), encoding="utf-8")
    print("WROTE", OUT)


if __name__ == "__main__":
    main()
