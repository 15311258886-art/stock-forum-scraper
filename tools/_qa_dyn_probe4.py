# -*- coding: utf-8 -*-
"""动态功能测试 第四轮（只读）：排序/空值/错误语义 细节。"""
from __future__ import annotations

import json
import socket
from pathlib import Path

HOST, PORT = "127.0.0.1", 8765
OUT = Path(__file__).resolve().parent / "_qa_dyn_out4.txt"
LOG: list[str] = []


def jget(path: str):
    req = (f"GET {path} HTTP/1.1\r\nHost: {HOST}:{PORT}\r\n"
           f"Connection: close\r\nUser-Agent: qa-dyn-probe4\r\n\r\n")
    try:
        s = socket.create_connection((HOST, PORT), timeout=30)
        s.settimeout(30)
        s.sendall(req.encode("utf-8", "surrogateescape"))
        buf = b""
        while True:
            d = s.recv(65536)
            if not d:
                break
            buf += d
        s.close()
    except Exception as e:  # noqa: BLE001
        return None, {"error": f"{type(e).__name__}: {e}", "body": b"", "status": None}
    if not buf:
        return None, {"error": "EMPTY_REPLY", "body": b"", "status": None}
    sep = buf.find(b"\r\n\r\n")
    if sep < 0:
        return None, {"error": "NO_HEADER_END", "body": buf, "status": None}
    head, body = buf[:sep].decode("iso-8859-1"), buf[sep + 4:]
    try:
        status = int(head.split(" ")[1])
    except Exception:  # noqa: BLE001
        status = None
    try:
        return json.loads(body.decode("utf-8")), {"status": status, "body": body, "error": None}
    except Exception:  # noqa: BLE001
        return None, {"status": status, "body": body, "error": "NOT_JSON"}


def main() -> None:
    LOG.append("### A. /api/players 排序与空值")
    pl, _ = jget("/api/players")
    rp = [p.get("rank_position") for p in pl or []]
    LOG.append(f"  条数={len(pl or [])} rank_position 为 None 的={sum(1 for x in rp if x is None)}")
    LOG.append(f"  前 5 个 rank_position={rp[:5]}（SQLite 中 NULL 排最前）")
    tiers = {}
    for p in pl or []:
        tiers[p.get("tier")] = tiers.get(p.get("tier"), 0) + 1
    LOG.append(f"  tier 分布={json.dumps(tiers, ensure_ascii=False)}")
    chs = {}
    for p in pl or []:
        chs[p.get("channel")] = chs.get(p.get("channel"), 0) + 1
    LOG.append(f"  channel 分布={json.dumps(chs, ensure_ascii=False)}")
    LOG.append(f"  is_visible=0 的={sum(1 for p in pl or [] if p.get('is_visible') != 1)} "
               f"influence 为 None/0 的={sum(1 for p in pl or [] if not p.get('influence'))}")
    top = [p for p in pl or [] if p.get("is_top_player")]
    LOG.append(f"  is_top_player=1 的={len(top)}")

    LOG.append("")
    LOG.append("### B. /api/positions 是否有重复 (site,player,symbol)")
    pos, _ = jget("/api/positions")
    seen = {}
    for p in pos or []:
        k = (p.get("site"), p.get("player"), p.get("symbol"))
        seen[k] = seen.get(k, 0) + 1
    dup = {str(k): v for k, v in seen.items() if v > 1}
    LOG.append(f"  条数={len(pos or [])} 重复组合数={len(dup)} 示例={list(dup.items())[:5]}")
    LOG.append(f"  action 分布=" + json.dumps(
        {a: sum(1 for p in pos or [] if p.get("action") == a)
         for a in sorted({p.get("action") for p in pos or []})}, ensure_ascii=False))

    LOG.append("")
    LOG.append("### C. 「HTTP 200 + {error:...}」错误语义清单")
    errs = []
    for path in ("/api/stock", "/api/stock?symbol=", "/api/stock?symbol=___nope",
                 "/api/person?name=", "/api/player?name=", "/api/player?name=__x__",
                 "/api/detail", "/api/detail?kind=bogus&id=1",
                 "/api/detail?kind=post&id=-1", "/api/detail?kind=post&id=999999999"):
        obj, r = jget(path)
        if isinstance(obj, dict) and obj.get("error"):
            errs.append((path, r["status"], obj["error"]))
    for p, s, e in errs:
        LOG.append(f"  {p} -> HTTP {s} body={{'error': {e!r}}}")

    LOG.append("")
    LOG.append("### D. /api/summary.sites 与站点配置")
    sm, _ = jget("/api/summary")
    if sm:
        LOG.append(f"  sites={sm.get('sites')} last_ingest={sm.get('last_ingest')!r} "
                   f"login_status={json.dumps(sm.get('login_status'), ensure_ascii=False)}")

    LOG.append("")
    LOG.append("### E. 海外热点 heat 是否严格降序 + 字段类型")
    hs, _ = jget("/api/overseas_hotspots")
    heats = [h.get("heat") for h in hs or []]
    LOG.append(f"  n={len(heats)} 降序={heats == sorted(heats, reverse=True)} 首尾={heats[:3]}..{heats[-1:]}")
    types = {}
    for h in hs or []:
        for k in ("decision", "overview", "transmission", "amplification", "cross_check",
                  "sectors_up_detail", "sectors_down_detail", "variables"):
            types.setdefault(k, set()).add(type(h.get(k)).__name__)
    LOG.append(f"  字段类型={ {k: sorted(v) for k, v in types.items()} }")
    bad = [h.get("id") for h in hs or []
           if not isinstance(h.get("sectors_up_detail"), list)
           or not isinstance(h.get("sectors_down_detail"), list)
           or not isinstance(h.get("decision"), str)]
    LOG.append(f"  decision/明细 类型不符合预期的 id：{bad if bad else '无'}")

    OUT.write_text("\n".join(LOG), encoding="utf-8")
    print("WROTE", OUT)


if __name__ == "__main__":
    main()
