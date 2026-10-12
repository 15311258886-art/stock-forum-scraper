# -*- coding: utf-8 -*-
"""探测生产实例（未重启的 8765）能否把新字段送达前端。
目的：证明「刷新即生效、不重启也不白屏」这一结论成立。
只读，不写任何数据。"""
import json
import sys
import urllib.request

BASE = "http://127.0.0.1:8765"
out = []

# 重要：本会话环境里 http_proxy / https_proxy 指向本机代理（127.0.0.1:55035），
# urllib 默认会把 localhost 请求也交给代理 → 服务未起时收到 502（而非 ConnectionRefused），
# 会把"服务没起来"误读成"服务返回错误"。故一律显式禁用代理。
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def get(path):
    with _OPENER.open(BASE + path, timeout=20) as r:
        return r.status, r.read()


# 1) 静态 JS 是否已是磁盘上的新版
try:
    st, body = get("/static/app.js")
    js = body.decode("utf-8", "replace")
    out.append(f"[static/app.js] HTTP {st} LEN={len(js)}")
    for k in ("ov-hs-dec-tag", "hotspotForecast", "ov-hs-dom-row", "sectors_up_detail"):
        out.append(f"    contains {k} = {k in js}")
    out.append(f"    contains ov-hs-ev(证据区,应为 False) = {'ov-hs-ev' in js}")
except Exception as e:
    out.append(f"[static/app.js] FAIL {e!r}")

# 2) 热点 API：新字段是否在
try:
    st, body = get("/api/overseas_hotspots")
    raw = body.decode("utf-8", "replace")
    out.append(f"[api/overseas_hotspots] HTTP {st} LEN={len(raw)}")
    d = json.loads(raw)
    items = d.get("items") if isinstance(d, dict) else d
    items = items or []
    out.append(f"    hotspots={len(items)}")
    keys = set()
    for it in items:
        keys |= set(it.keys())
    for k in ("decision", "sectors_up_detail", "sectors_down_detail"):
        out.append(f"    field {k} present = {k in keys}")

    def as_list(v):
        if isinstance(v, list):
            return v
        if isinstance(v, str) and v.strip():
            try:
                return json.loads(v) or []
            except Exception:
                return []
        return []

    dec_ok = sum(1 for it in items if str(it.get("decision") or "").strip())
    detail_ok = sum(1 for it in items
                    if as_list(it.get("sectors_up_detail")) or as_list(it.get("sectors_down_detail")))
    # 传输形态：字符串（旧服务）还是数组（新服务）
    form = set()
    for it in items:
        v = it.get("sectors_up_detail")
        form.add("array" if isinstance(v, list) else ("string" if isinstance(v, str) else "none"))
    out.append(f"    decision 非空 {dec_ok}/{len(items)}")
    out.append(f"    领域明细非空 {detail_ok}/{len(items)}")
    out.append(f"    sectors_up_detail 传输形态 = {sorted(form)}  (string=服务未重启, array=服务已重启)")
    if items:
        s = items[0]
        out.append(f"    样例 decision = {str(s.get('decision'))[:110]}")
        out.append(f"    样例 up_detail = {str(s.get('sectors_up_detail'))[:150]}")
except Exception as e:
    out.append(f"[api/overseas_hotspots] FAIL {e!r}")

# 3) 海外新闻 / 情绪接口仍可用（未被我改坏）
for p in ("/api/overseas_sentiment", "/api/overseas_sources", "/api/overseas"):
    try:
        st, body = get(p)
        out.append(f"[{p}] HTTP {st} LEN={len(body)}")
    except Exception as e:
        out.append(f"[{p}] FAIL {e!r}")

print("\n".join(out))
sys.stdout.flush()
with open("tools/_qa_live_probe.txt", "w", encoding="utf-8") as f:
    f.write("\n".join(out) + "\n")
