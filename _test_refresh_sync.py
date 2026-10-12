"""建议3 / P1 自测：刷新状态与调度口径统一。

验证三件事：
   1) 整轮抓取只要「至少一个来源成功」就把 __all__ 记 ok（last_ok_at 前移）
   2) 全部来源失败才记 error，且不动 last_ok_at
   3) /api/refresh_status 回传调度口径字段（scheduler_last_run_at / stale_basis）

跑法：.venv\\Scripts\\python.exe _test_refresh_sync.py
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from viewer import server as SVR  # noqa: E402

ok = True


def row(key: str = "__all__") -> dict:
    for r in SVR.DB.get_refresh_status():
        if r["source_key"] == key:
            return r
    return {}


def main() -> int:
    global ok
    before = row()
    print(f"[before] __all__ last_ok_at={before.get('last_ok_at')!r} status={before.get('status')!r}")

    # --- 1) 部分成功（errors=1 / 3 个源）→ 应记 ok 且 last_ok_at 前移 ---
    SVR._sched_record_all(errors=1, total_sources=3, total_items=777)
    r1 = row()
    print(f"[case1 ] last_ok_at={r1['last_ok_at']} status={r1['status']} items={r1['item_count']}")
    if r1["status"] != "ok":
        ok = False
        print("FAIL: 部分来源成功时 __all__ 未记为 ok")
    if r1["item_count"] != 777:
        ok = False
        print("FAIL: item_count 未透传")

    # --- 2) 全失败 → 记 error，但 last_ok_at 应等于 case1 的新值（不被抹掉/前移） ---
    SVR._sched_record_all(errors=3, total_sources=3, total_items=0)
    r2 = row()
    print(f"[case2 ] last_ok_at={r2['last_ok_at']} status={r2['status']}")
    if r2["status"] != "error":
        ok = False
        print("FAIL: 全部失败时未记为 error")
    if r2["last_ok_at"] != r1["last_ok_at"]:
        ok = False
        print("FAIL: 全失败时 last_ok_at 被改动了（应保持上次成功时刻）")

    # --- 3) 接口口径字段 ---
    d = SVR.api_refresh_status()
    keys = ("scheduler_last_run_at", "scheduler_enabled", "stale_basis", "stale_seconds")
    missing = [k for k in keys if k not in d]
    print(f"[case3 ] missing={missing}  stale_seconds={d.get('stale_seconds')}  basis={d.get('stale_basis')!r}")
    if missing:
        ok = False
        print("FAIL: /api/refresh_status 缺调度口径字段")
    if d.get("stale_seconds") != SVR.REFRESH_INTERVAL_SECONDS * 2:
        ok = False
        print("FAIL: stale_seconds 不是刷新周期的 2 倍（阈值与周期脱钩）")

    print("=" * 56)
    print("ALL_PASS" if ok else "HAS_FAILURE")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
