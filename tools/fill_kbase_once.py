# -*- coding: utf-8 -*-
"""一次性补齐 viewer/kbase.db 到最新交易日（按末根日期判定，幂等可重跑）。

背景（2026-10-29 复查）：`bulk_fetch_all` 曾因 baseline 被单票脏数据带偏，
把 4474 只正常票误登记为「停牌」而永久跳过，导致全市场 85% 的 K 线冻结在 10-27。
误登记已清理、判定逻辑已修，这里跑一轮把落后的票补齐。

用法：python tools/fill_kbase_once.py [workers]
"""
from __future__ import annotations

import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "viewer"))

from viewer import kbase as kb  # noqa: E402


def main() -> int:
    workers = int(sys.argv[1]) if len(sys.argv) > 1 else 8
    b = kb.get_base()
    base_line = b._fetch_baseline()
    print("[fill] baseline=%s workers=%d target=%d" % (
        base_line, workers, len(b.target_codes())), flush=True)
    t0 = time.time()

    def prog(done, total, st):
        if done % 400 == 0:
            print("[fill] %d/%d ok=%d fail=%d susp=%d lag=%d %.0fs" % (
                done, total, st.get("ok", 0), st.get("fail", 0),
                st.get("suspended", 0), st.get("lagging", 0), time.time() - t0), flush=True)

    res = b.bulk_fetch_all(b.target_codes(), datalen=10000, workers=workers, progress=prog)
    print("[fill] 完成 %.0fs %s" % (time.time() - t0,
                                    {k: v for k, v in res.items() if k != "fail_codes"}), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
