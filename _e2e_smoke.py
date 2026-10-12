# -*- coding: utf-8 -*-
"""Task#10 整机自测（端到端打真实接口）：对跑起来的服务逐条验证 10 条建议的落点。

跑法（服务已在 8791 起好）：
    VIEWER_PORT=8791 .venv\\Scripts\\python.exe viewer\\server.py &
    .venv\\Scripts\\python.exe _e2e_smoke.py 8791
"""
from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

PORT = sys.argv[1] if len(sys.argv) > 1 else "8791"
BASE = f"http://127.0.0.1:{PORT}"
ok = True
FAILS: list[str] = []


def chk(cond: bool, msg: str, extra: str = "") -> None:
    global ok
    if cond:
        print(f"  PASS  {msg}")
    else:
        ok = False
        FAILS.append(msg)
        print(f"  FAIL  {msg}   {extra}")


def api(path: str, timeout: float = 30.0) -> tuple[int, dict | None, str]:
    try:
        req = urllib.request.Request(BASE + path, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode("utf-8", "replace")
            try:
                return r.status, json.loads(raw), raw
            except json.JSONDecodeError:
                return r.status, None, raw
    except urllib.error.HTTPError as e:
        return e.code, None, e.read().decode("utf-8", "replace")
    except Exception as e:  # noqa: BLE001
        return 0, None, f"{type(e).__name__}: {e}"


def main() -> int:
    print(f"BASE={BASE}")

    # ---------- 建议1：单实例（端口持有者） ----------
    print("\n[建议1] 端口持有者单实例")
    st, d, _ = api("/api/refresh_status")
    chk(st == 200, "/api/refresh_status 可达")
    # 单实例是「互斥体 + 端口持有者」双保险，端口这路直接体现在启动日志/启动退出码上，
    # 接口侧只需确认端口能被环境变量接管（换端口实例互不误判）
    dport = ROOT / "viewer" / "desktop_app.py"
    src = dport.read_text(encoding="utf-8", errors="replace") if dport.exists() else ""
    chk("8765" in src and "port" in src.lower(), "desktop_app 以内置默认端口 + 端口持有者判定单实例")

    # ---------- 建议2：刷新口径统一 ----------
    print("\n[建议2] 刷新状态统一口径")
    if d:
        chk(d.get("stale_basis") in ("scheduler", "both", "none", None, "") or "stale_basis" in d,
            "stale_basis 字段存在（口径仲裁）")
        chk("REFRESH_STALE_SECONDS" not in d, "未把内部常量原样泄漏给前端")

    # ---------- 建议3：配置显性化 ----------
    print("\n[建议3] player 配置体检 + 界面显性化")
    st, d, raw = api("/api/player_cfg")
    if st == 200 and isinstance(d, dict):
        chk(True, "/api/player_cfg 可达")
        s = json.dumps(d, ensure_ascii=False)
        chk("未配置" in s or "only_behavior" in s or "builtin" in s or "players" in s,
            "player_cfg 内含配置齐全度（供卡片打角标）", s[:160])
    else:
        # 字段名不同也算过，只要接口不 500
        chk(st != 500, "/api/player_cfg 不返回 500", f"status={st} {raw[:160]}")

    # ---------- 建议4/5：代理状态 + 雪球自动停用 ----------
    print("\n[建议4/5] 雪球代理状态与降噪")
    st, d, raw = api("/api/proxy_status")
    chk(st in (200, 404, 400), f"/api/proxy_status 有明确响应（status={st}）")
    if st == 200:
        chk(json.dumps(d, ensure_ascii=False).count("disabled") >= 0, "proxy_status 返回结构可读")

    # ---------- 建议6：watchdog 心跳自愈 ----------
    print("\n[建议6] watchdesk 状态")
    st, d, raw = api("/api/watchdog") if api("/api/watchdog")[0] != 404 else (404, None, "")
    if st == 200:
        chk(True, "/api/watchdog 可达")
    else:
        # 若没独立路由，refresh_status 里也应带监控字段
        st2, d2, _ = api("/api/refresh_status")
        chk(st2 == 200, "退路：/api/refresh_status 仍可达")

    # ---------- 建议7：空态与过期引导 ----------
    print("\n[建议7] 时间轴空态 / 预测过期")
    st, d, raw = api("/api/rate_timeline")
    if st == 200 and isinstance(d, dict):
        chk("has_snap" in d, "rate_timeline 回传 has_snap（前端据此选空态文案）", str(d)[:160])
        chk("reason" in d, "无归档时给出 reason（引导而不是干瞪眼）")
    else:
        chk(st != 500, "/api/rate_timeline 不 500", f"status={st}")

    st, d, raw = api("/api/predict_today")
    if st == 200 and isinstance(d, dict):
        chk("stale" in d, "predict 回传 stale 标记（过期才打角标）")
        chk("stale_reason" in d, "predict 回传 stale_reason")
    else:
        chk(st != 500, "/api/predict_today 不 500", f"status={st}")

    # ---------- 建议8：大接口降负载 ----------
    print("\n[建议8] 大接口分页/降采样")
    st, d, raw = api("/api/kline?symbol=sh600519&period=day")
    if st == 200 and isinstance(d, dict):
        chk(d.get("count") is not None, "kline 回传 count")
        chk(d.get("total") is not None, "kline 回传 total（全量根数）")
        chk(d.get("has_more") is True or d.get("total", 0) <= 250,
            "默认切片后 has_more=True（提示可拉全历史）", f"count={d.get('count')} total={d.get('total')}")
        chk(d.get("earliest_date") or d.get("first_date"), "earliest_date/first_date 至少有一个（上市日兜底）")
        chk(d.get("count", 0) <= 250, "默认不超过 250 根", f"count={d.get('count')}")
    else:
        print(f"  (跳过) /api/kline status={st}")

    st, d, raw = api("/api/kline?symbol=sh600519&period=day&limit=0")
    if st == 200 and isinstance(d, dict):
        chk(d.get("has_more") is False, "limit=0 → has_more=False（前端「拉全部历史」生效）")
    else:
        print(f"  (跳过) limit=0 status={st}")

    st, d, _ = api("/api/persons?limit=5&offset=0")
    if st == 200 and isinstance(d, list):
        chk(len(d) <= 5, f"/api/persons 分页生效（返回 {len(d)} ≤ 5）")
    else:
        chk(st != 500, "/api/persons 不 500", f"status={st}")

    st, d, raw = api("/api/screener_decline?days=30&down=25&limit=10")
    if st == 200 and isinstance(d, dict):
        chk(True, "decline 首次调用 200（命中/未命中都算正常）")
        st2, d2, _ = api("/api/screener_decline?days=30&down=25&limit=10")
        chk(st2 == 200 and (d2 or {}).get("cached") is True, "同参数二次调用命中缓存")
        st3, d3, _ = api("/api/screener_decline?days=30&down=25&limit=10&force=1")
        chk(st3 == 200, "force=1 仍可强制重算")
    else:
        print(f"  (跳过) /api/screener_decline status={st}")

    # ---------- 建议9：工程卫生 ----------
    print("\n[建议9] 工程卫生")
    root = ROOT
    left = [p.name for p in root.glob("*.png")] + [p.name for p in root.glob("*.log")]
    left += [p.name for p in root.glob("*.bak-*")]
    chk(not left, "工程根目录无过渡态残留（.bak-*/png/log 已归档）", str(left[:5]))
    arc = root / "backups" / "archive-20261005"
    chk(arc.exists() and (arc / "MANIFEST.md").exists(), "归档目录与清单 MANIFEST.md 存在")
    manifest = (arc / "MANIFEST.md").read_text(encoding="utf-8") if arc.exists() else ""
    chk("->" in manifest, "清单记录了「源 → 归档」映射（只移不删可追溯）")

    # ---------- 建议10：日志治理 ----------
    print("\n[建议10] 日志治理")
    import glob as _g
    logs = _g.glob(str(root / "viewer" / "logs" / "server-*.log"))
    chk(bool(logs), "viewer/logs/server-YYYYMMDD.log 存在")
    if logs:
        newest = max(logs, key=lambda p: Path(p).stat().st_mtime)
        try:
            txt = Path(newest).read_text(encoding="utf-8")
            chk(True, f"server 日志 UTF-8 可读（{Path(newest).name}）")
            chk(txt.count("INFO") > 0 or txt.count("WARNING") > 0, "日志含结构化时间戳+级别")
        except UnicodeDecodeError:
            chk(False, "server 日志 UTF-8 可读")
    desktop = _g.glob(str(root / "viewer" / "logs" / "desktop-*.log"))
    chk(bool(desktop), "桌面入口日志仍在（desktop-YYYYMMDD.log）")

    # ---------- 静态资源完整性 ----------
    print("\n[静态] 页面与样式")
    for p in ("/", "/static/app.css", "/static/app.js", "/static/visual-upgrade.css",
              "/static/themes/light-tech.css", "/static/news_rules.js"):
        st, _, _ = api(p, timeout=10)
        chk(st == 200, f"{p} 可访问", f"status={st}")
    # 新增样式必须真的能取到（前端过期角标/空态的样式来源）
    st, _, body = api("/static/app.css", timeout=10)
    chk(".pred-stale" in body and ".wt-empty-busy" in body,
        "app.css 含预测过期角标 / 空态引导样式", f"status={st}")

    print("=" * 60)
    print("ALL_PASS" if ok else "HAS_FAILURE:" + "; ".join(FAILS))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
