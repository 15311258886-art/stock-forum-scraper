"""建议5 / P2 自测：雪球代理自动停用 + 告警合并 + 指数退避；建议4 配置体检。

跑法：.venv\\Scripts\\python.exe _test_proxy_noise.py
注意：相连不存在的代理（127.0.0.1:53269 默认就没起）后会进入停用态，
      用例结束会复位，不影响真实抓取（真抓时本来也是直连）。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from scraper import browser_fetch as BF  # noqa: E402
from viewer import server as SVR  # noqa: E402

ok = True


def main() -> int:
    global ok
    # ---- 1) 连续失败 N 次后自动停用 ----
    BF._PROXY_STATE.update({"fail_streak": 0, "disabled": False, "next_retry_at": 0.0,
                            "retry_count": 0, "disabled_at": 0.0, "notified": False})
    BF._USE_PROXY = True
    limit = BF._PROXY_AUTO_DISABLE_AFTER
    r1 = BF._proxy_check(timeout=0.6)
    r2 = BF._proxy_check(timeout=0.6)
    st = BF._PROXY_STATE
    print(f"[case1] 第1次={r1} 第2次={r2} 阈值={limit} fail_streak={st['fail_streak']} "
          f"disabled={st['disabled']}")
    if r1 or r2:
        ok = False
        print("FAIL: 不可达的代理竟然被判为可用")
    if st["fail_streak"] != limit:
        ok = False
        print(f"FAIL: fail_streak 应累加到 {limit}")
    if not st["disabled"]:
        ok = False
        print("FAIL: 连续失败未达到阈值时没自动停用")

    # ---- 2) 停用后：退避窗口已在未来，且重复调用不再累加失败/不再重新探测 ----
    ps = BF.proxy_status()
    print(f"[case2] proxy_status={ps}")
    if ps.get("disabled") is not True:
        ok = False
        print("FAIL: proxy_status 未反映「已停用」")
    if ps.get("next_retry_in", 0) <= 0:
        ok = False
        print("FAIL: 退避窗口不在未来（没有退避）")
    if ps.get("auto_disable_after") != limit:
        ok = False
        print("FAIL: auto_disable_after 未回传")
    win = ps["next_retry_in"]
    s_before = dict(st)
    for _ in range(5):
        BF._proxy_check(timeout=0.6)
    print(f"[case2] 再调 5 次：fail_streak {s_before['fail_streak']}->{st['fail_streak']} "
          f"retry_count={st['retry_count']}（应都不变）")
    if st["fail_streak"] != s_before["fail_streak"] or st["retry_count"] != s_before["retry_count"]:
        ok = False
        print("FAIL: 停用后仍在累加探测次数（等于每轮都在真探测 = 旧行为没改）")
    if st["next_retry_in"] if "next_retry_in" in st else None:
        pass
    BF._PROXY_STATE["next_retry_at"] = 0.0
    print(f"[case2] 退避窗口 ≈ {win}s")

    # ---- 3) 退避窗口没到 → 直接 False（连探测都不做，省时间）----
    BF._PROXY_STATE["next_retry_at"] = 9e15    # 未来很远，绝不该触发真探测
    r = BF._proxy_check(timeout=0.6)
    print(f"[case3] 退避未到时 _proxy_check={r}（应为 False 且不打 warning）")
    if r:
        ok = False
        print("FAIL: 退避窗口内不该再走代理")

    # ---- 4) 复位（配置变更/恢复）----
    BF._proxy_reset_locked()
    print(f"[case4] 复位后 disabled={BF._PROXY_STATE['disabled']} _USE_PROXY={BF._USE_PROXY}")
    if BF._PROXY_STATE["disabled"] or not BF._USE_PROXY:
        ok = False
        print("FAIL: 代理状态未能复位")

    # ---- 5) 建议4：配置体检字段 ----
    cfg = SVR._player_cfg()
    print(f"[case5] player_cfg={cfg}")
    if "players_enabled" not in cfg or "positions_enabled" not in cfg:
        ok = False
        print("FAIL: _player_cfg 字段缺失")
    if not isinstance(cfg.get("players_sites"), list):
        ok = False
        print("FAIL: players_sites 应为列表")

    print("=" * 56)
    print("ALL_PASS" if ok else "HAS_FAILURE")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
