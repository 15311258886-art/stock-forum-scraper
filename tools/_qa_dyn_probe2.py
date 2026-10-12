# -*- coding: utf-8 -*-
"""动态功能测试 第二轮（只读）：真实样本接口字段对账 + 交叉一致性。"""
from __future__ import annotations

import json
import socket
from datetime import datetime
from pathlib import Path

HOST, PORT = "127.0.0.1", 8765
OUT = Path(__file__).resolve().parent / "_qa_dyn_out2.txt"
LOG: list[str] = []


def raw_get(path: str, timeout: float = 30.0) -> dict:
    req = (f"GET {path} HTTP/1.1\r\nHost: {HOST}:{PORT}\r\n"
           f"Connection: close\r\nUser-Agent: qa-dyn-probe2\r\n\r\n")
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
        return {"error": f"{type(e).__name__}: {e}", "body": b"", "headers": {}, "status": None}
    if not buf:
        return {"error": "EMPTY_REPLY", "body": b"", "headers": {}, "status": None}
    sep = buf.find(b"\r\n\r\n")
    if sep < 0:
        return {"error": "NO_HEADER_END", "body": buf, "headers": {}, "status": None}
    head, body = buf[:sep].decode("iso-8859-1"), buf[sep + 4:]
    lines = head.split("\r\n")
    hdrs = {}
    for ln in lines[1:]:
        if ":" in ln:
            k, v = ln.split(":", 1)
            hdrs[k.strip().lower()] = v.strip()
    try:
        status = int(lines[0].split(" ")[1])
    except Exception:  # noqa: BLE001
        status = None
    return {"status": status, "headers": hdrs, "body": body, "error": None,
            "status_line": lines[0]}


def jget(path: str):
    r = raw_get(path)
    try:
        return json.loads(r["body"].decode("utf-8")), r
    except Exception:  # noqa: BLE001
        return None, r


def main() -> None:
    posts, _ = jget("/api/posts?limit=3")
    op, _ = jget("/api/operating")
    pid = posts[0]["id"] if posts else 1
    author = posts[0]["author"] if posts else ""
    tid = op[0]["id"] if op else 1

    LOG.append("### A. 真实样本接口字段（/api/detail · /api/person · /api/player）")
    for label, path in [
        ("detail-post", f"/api/detail?kind=post&id={pid}"),
        ("detail-post-full", f"/api/detail?kind=post&id={pid}&full=1"),
        ("detail-trade", f"/api/detail?kind=trade&id={tid}"),
        ("person", "/api/person?name=" + author),
        ("player", "/api/player?name=大阪柯小李"),
    ]:
        obj, r = jget(path)
        if obj is None:
            LOG.append(f"[{label}] {path} -> 无 JSON（{r['error'] or r['status']}）")
            continue
        if isinstance(obj, dict):
            keys = sorted(obj.keys())
            LOG.append(f"[{label}] {path} status={r['status']} bytes={len(r['body'])} keys={keys}")
            for k, v in obj.items():
                if isinstance(v, dict):
                    LOG.append(f"    {k}: dict keys={sorted(v.keys())}")
                elif isinstance(v, list):
                    inner = sorted(v[0].keys()) if v and isinstance(v[0], dict) else (
                        f"<{type(v[0]).__name__}>" if v else "[]")
                    LOG.append(f"    {k}: list n={len(v)} item_keys={inner}")
                else:
                    sv = json.dumps(v, ensure_ascii=False)
                    LOG.append(f"    {k}: {type(v).__name__} = {sv[:160]}")
        else:
            LOG.append(f"[{label}] {path} status={r['status']} list n={len(obj)}")

    LOG.append("")
    LOG.append("### B. min_holders 是否真的生效")
    for mh in (1, 2, 3, 5, 8, 20, 100):
        obj, _ = jget(f"/api/consensus?min_holders={mh}")
        if isinstance(obj, list):
            hs = [x.get("holders") for x in obj]
            LOG.append(f"  min_holders={mh} -> n={len(obj)} holders范围={min(hs) if hs else '-'}..{max(hs) if hs else '-'}")

    LOG.append("")
    LOG.append("### C. 重复同名参数（取第一个值）")
    for path in ("/api/posts?limit=abc&limit=5", "/api/posts?limit=5&limit=abc",
                 "/api/consensus?min_holders=1&min_holders=abc"):
        r = raw_get(path)
        LOG.append(f"  {path} -> status={r['status']} err={r['error']} bytes={len(r['body'])}")

    LOG.append("")
    LOG.append("### D. 404 / 错误页是否泄露实现细节")
    r = raw_get("/api/nonexistent")
    LOG.append(f"  status_line={r['status_line']!r}")
    LOG.append(f"  headers={json.dumps({k: v for k, v in r['headers'].items()}, ensure_ascii=False)}")

    LOG.append("")
    LOG.append("### E. hold_since / hold_days 语义一致性（今日 2026-09-22）")
    today = datetime(2026, 9, 22)
    stocks, _ = jget("/api/stocks")
    for sym in [x["symbol"] for x in (stocks or [])][:4]:
        d, _ = jget(f"/api/stock?symbol={sym}")
        if not d:
            continue
        LOG.append(f"[{sym}] {d.get('symbol_name')}")
        for p in d.get("positions") or []:
            since = (p.get("hold_since") or "").strip()
            expect = None
            if since:
                try:
                    expect = (today - datetime.strptime(since[:10], "%Y-%m-%d")).days
                except Exception:  # noqa: BLE001
                    expect = None
            LOG.append(f"    {p.get('player')} action={p.get('action')} hold_since={since!r} "
                       f"hold_days={p.get('hold_days')} buy_time={p.get('buy_time')!r} "
                       f"sell_time={p.get('sell_time')!r} (since->今日应={expect})")

    LOG.append("")
    LOG.append("### F. 全量交叉一致性：/api/positions vs /api/consensus vs /api/stock")
    positions, _ = jget("/api/positions")
    cons1, _ = jget("/api/consensus?min_holders=1")
    bad = []
    for c in cons1 or []:
        sym = c["symbol"]
        rows = [p for p in positions or [] if p["symbol"] == sym]
        players = {p.get("player") for p in rows if p.get("player")}
        if c.get("holders") != len(players):
            bad.append((sym, c.get("holders"), len(players), len(rows)))
    LOG.append(f"  consensus.holders != distinct(positions.player) 的标的：{bad if bad else '无'}")

    stocks, _ = jget("/api/stocks")
    for s in stocks or []:
        d, _ = jget(f"/api/stock?symbol={s['symbol']}")
        if not d:
            continue
        n_pos = len([p for p in (positions or []) if p["symbol"] == s["symbol"]])
        if d.get("position_count") != n_pos:
            LOG.append(f"  !! {s['symbol']} /api/stock.position_count={d.get('position_count')} "
                       f"!= /api/positions 条数={n_pos}")

    LOG.append("")
    LOG.append("### G. /api/sentiment 每条 map 的 bull+bear+neutral 是否等于 mentions")
    sent, _ = jget("/api/sentiment")
    for m in (sent or {}).get("map", []):
        tot = (m.get("bull", 0) + m.get("bear", 0) + m.get("neutral", 0))
        if tot != m.get("mentions"):
            LOG.append(f"  !! {m['symbol']} 三项和={tot} mentions={m.get('mentions')}")
    LOG.append(f"  map 条数={len((sent or {}).get('map', []))} "
               f"name_covered={sent.get('name_covered') if sent else None}/"
               f"{sent.get('name_total') if sent else None}")

    LOG.append("")
    LOG.append("### H. 海外舆情字段抽样（symbols / source_mix / daily / news）")
    ov, _ = jget("/api/overseas_sentiment")
    if ov:
        for k in ("symbols", "source_mix", "daily", "news"):
            arr = ov.get(k) or []
            LOG.append(f"  {k}: n={len(arr)} item_keys={sorted(arr[0].keys()) if arr and isinstance(arr[0], dict) else arr[:3]}")
        hs, _ = jget("/api/overseas_hotspots")
        LOG.append(f"  hotspots: n={len(hs or [])} 每条 decision 非空="
                   f"{sum(1 for h in hs if (h.get('decision') or '').strip())}/{len(hs or [])} "
                   f"sectors_up_detail 非空={sum(1 for h in hs if h.get('sectors_up_detail'))}/{len(hs or [])} "
                   f"sectors_down_detail 非空={sum(1 for h in hs if h.get('sectors_down_detail'))}/{len(hs or [])}")
        # 标题重复检测
        titles = {}
        for h in hs or []:
            titles.setdefault(h.get("title"), []).append(h.get("key"))
        dup = {t: k for t, k in titles.items() if len(k) > 1}
        LOG.append(f"  重复标题：{json.dumps(dup, ensure_ascii=False)}")

    LOG.append("")
    LOG.append("### I. 前端未引用的后端 GET 接口（死接口）")
    LOG.append("  见下方 grep 结论：/api/positions、/api/stock_names、/api/login_status "
               "在 viewer/static/*.js|html 中均无引用（已在报告正文核对）")

    OUT.write_text("\n".join(LOG), encoding="utf-8")
    print("WROTE", OUT)


if __name__ == "__main__":
    main()
