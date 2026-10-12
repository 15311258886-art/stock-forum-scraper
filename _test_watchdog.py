"""建议6 / P2 自测：盯盘（后台任务）心跳与假活自愈。

覆盖：
   1) 心跳正常（距上次扫描 < 30 分钟）→ 什么都不做，不打标、不补扫
   2) 假活（距上次扫描 > 30 分钟且线程不在跑）→ running 置 false + 打 zombie_at + 自动补起一轮
   3) /api/watchdesk 回传 zombie / zombie_scan_age，前端状态牌能显示「已停止（xx 未扫描）」
   4) 自愈防抖：连续两轮间隔不足 15 分钟时只标记不重复补扫

跑法：.venv\\Scripts\\python.exe _test_watchdog.py
注意：本用例会临时改 meta.watchdesk_state，跑完自动复位。
"""
from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from viewer import server as SVR  # noqa: E402

KEY = SVR._DESK_STATE_KEY
ok = True


def set_state(**kw) -> None:
    SVR.DB.set_meta(KEY, json.dumps(kw, ensure_ascii=False))


def get_state() -> dict:
    return json.loads(SVR.DB.get_meta(KEY, "{}") or "{}")


def ago(seconds: int) -> str:
    return (datetime.now() - timedelta(seconds=seconds)).strftime("%Y-%m-%d %H:%M:%S")


def main() -> int:
    global ok
    backup = SVR.DB.get_meta(KEY) or "{}"
    try:
        # ---- 1) 心跳正常 ----
        set_state(running=False, last_scan_at=ago(60))
        SVR._WATCHDOG_LAST_HEAL["at"] = 0.0
        SVR._watchdesk_heartbeat()
        s1 = get_state()
        print(f"[case1] zombie_at={s1.get('zombie_at')!r} 期望=None（心跳正常不该打标）")
        if s1.get("zombie_at"):
            ok = False
            print("FAIL: 心跳正常却被判为假活")

        # ---- 2) 假活 → 置 running=false + 打标 + 自动补扫 ----
        SVR._watchdesk_RUNNING = None  # noqa: F841（仅确保属性存在，若不存在也无所谓）
        SVR._DESK_RUNNING["on"] = False
        set_state(running=True, started_at=ago(3600), last_scan_at=ago(4 * 86400))  # 4 天前，就是报告里那个状态
        t0 = time.time()
        SVR._watchdesk_heartbeat()
        s2 = get_state()
        print(f"[case2] running={s2.get('running')} zombie_at={s2.get('zombie_at')!r} "
              f"zombie_scan_age={s2.get('zombie_scan_age')}")
        if s2.get("running") is not False:
            ok = False
            print("FAIL: 假活时 running 未置 false")
        if not s2.get("zombie_at"):
            ok = False
            print("FAIL: 未打 zombie_at 标")
        if not (s2.get("zombie_scan_age") or 0) > SVR._WATCHDOG_STALE_SECONDS:
            ok = False
            print("FAIL: zombie_scan_age 口径不对")
        # 自动补扫：等线程起来一小会儿，确认 _DESK_RUNNING 被点亮（说明补扫线程已启动）
        waited = 0.0
        while waited < 6 and not SVR._DESK_RUNNING["on"]:
            time.sleep(0.2)
            waited += 0.2
        print(f"[case2] 自动补扫线程已启动={SVR._DESK_RUNNING['on']}（等待 {waited:.1f}s）")
        if not SVR._DESK_RUNNING["on"]:
            ok = False
            print("FAIL: 假活未自动补起扫描线程")

        # ---- 3) 接口回传 zombie 字段 ----
        d = SVR.api_watchdesk()
        st = (d.get("stats") or {})
        print(f"[case3] zombie={st.get('zombie')} zombie_scan_age={st.get('zombie_scan_age')} "
              f"stale_s={st.get('heartbeat_stale_seconds')}")
        if "zombie" not in st or "zombie_scan_age" not in st:
            ok = False
            print("FAIL: /api/watchdesk 未回传 zombie 字段")

        # ---- 4) 自愈防抖：距上次自愈 < 15 分钟 → 只标记不补扫 ----
        SVR._DESK_RUNNING["on"] = False
        SVR._watchdesk_heartbeat()          # 刚自愈过，这次只该打标
        print(f"[case4] 补扫未重复启动（_DESK_RUNNING.on={SVR._DESK_RUNNING['on']}）")
        if SVR._DESK_RUNNING["on"]:
            ok = False
            print("FAIL: 自愈防抖失效，重复补扫了")
    finally:
        SVR.DB.set_meta(KEY, backup)
        SVR._DESK_RUNNING["on"] = False

    print("=" * 56)
    print("ALL_PASS" if ok else "HAS_FAILURE")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
