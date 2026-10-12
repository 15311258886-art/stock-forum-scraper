"""API 桥接：Node E2E 脚本为每次 API 请求 spawn 本脚本，本脚本调用真实 screener_extra 同名函数。
这样无需起独立 server.py（沙箱内起服被安全策略/路径卡住），又能用真实函数 + 真实 DeepSeek Key 验证。"""
import sys
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "viewer"))
import screener_extra as se  # noqa: E402


def handle(route, method, query, body):
    if route == "/api/screener/valuation":
        codes = [c.strip() for c in query.get("codes", "").split(",") if c.strip()]
        data = se.valuation_pct(codes)
        if codes and all((d.get("ok") is False) for d in data.values()):
            return {"ok": False,
                    "error": "全部标的均未获取到真实估值指标（请检查 akshare 安装与网络）",
                    "data": data}
        return {"ok": True, "data": data}
    if route == "/api/screener/backtest":
        codes = [c.strip() for c in query.get("codes", "").split(",") if c.strip()]
        return se.backtest(codes, query.get("start", "2024-01-01"), query.get("end", "2025-01-01"))
    if route == "/api/screener/themes":
        data = se.themes_map()
        if not data:
            return {"ok": False, "error": "未能获取真实概念板块数据（请检查 akshare 安装与网络）"}
        return {"ok": True, "data": data}
    if route == "/api/valuation_cache/status":
        return se.valuation_cache_status()
    if route == "/api/nl_screener":
        return se.nl_screener(body.get("text", ""))
    if route == "/api/settings/deepseek_key" and method == "GET":
        return {"configured": bool(se.get_deepseek_key())}
    if route == "/api/settings/deepseek_key" and method == "POST":
        key = (body.get("key") or "").strip()
        if not key:
            return {"ok": False, "error": "key 为空"}
        se.set_deepseek_key(key)
        return {"ok": True}
    return {"ok": True, "data": []}


if __name__ == "__main__":
    import io
    try:
        req = json.loads(sys.stdin.read() or "{}")
        # baostock 的 login()/logout() 会向 stdout 打印 "login success!" 等，
        # 会污染本脚本要回写的 JSON。先重定向 stdout 吞掉这些噪声，再只输出 JSON。
        _buf = io.StringIO()
        _real = sys.stdout
        sys.stdout = _buf
        try:
            out = handle(req.get("route", ""), req.get("method", "GET"), req.get("query", {}), req.get("body", {}))
        finally:
            sys.stdout = _real
    except Exception as e:  # noqa: BLE001
        out = {"ok": False, "error": repr(e)}
    sys.stdout.write(json.dumps(out, ensure_ascii=False))
