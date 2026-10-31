#!/usr/bin/env python
# tools/pipeline.py —— 主工作流编排（C 阶段，2026-10-27）
#
# 把「采集 → 因子 → 评估 → 回测(季度) → 选股」收敛成一条主线。
# 这是对外唯一的主工作流入口：加功能不再并列一堆散入口，而是挂到这条线对应阶段。
#
# 用法：
#   python tools/pipeline.py            # 打印完整工作流 + 出网命令清单（不执行）
#   python tools/pipeline.py --local     # 跑本地可算阶段（评估 + 选股）
#   python tools/pipeline.py --with-indicators   # 本地 + 因子全量 backfill(~20min)
#   python tools/pipeline.py --backtest  # 本地 + 信号回测全量(季度,~30min)
#   python tools/pipeline.py --full      # 本地全量（因子+评估+选股+回测）
#
# 出网阶段（采集）必须在进哥本机 venv 跑（沙箱无网），pipeline 只打印命令不自动执行。
import os
import sys
import time
import argparse
import subprocess

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = sys.executable

# 出网阶段（须本机执行，沙箱不可行）
NET_STAGES = [
    ("采集·全A报价估值(C4估值空洞)", [PY, "tools/fetch_a_shares_snapshot.py"],
     "填报价/PE/PB/市值，治 C4 估值真空"),
    ("采集·官方板块成员(C3/C4官方池)", [PY, "tools/fetch_plate_members.py"],
     "填 plate_members，否则 funnel official 回退博主挖名池"),
    ("采集·K线增量", [PY, "tools/sched_job.py", "kline_daily"],
     "出网补 K 线，须本机"),
]

# 本地阶段
INDICATORS = ("因子·技术指标(全量backfill~20min)", [PY, "-m", "viewer.indicators", "--mode", "backfill"],
              "本地纯算幂等；smoke 已验证健康，默认跳过")
UNIVERSE_SCAN = ("评估·全宇宙扫描", [PY, "-m", "viewer.universe_scan"], "本地 ~1s")
STRATEGY = ("选股·策略模板", [PY, "-m", "viewer.strategy_templates"], "本地 ~1s")
BACKTEST = ("回测·信号alpha(季度全量~30min)", [PY, "-m", "viewer.signal_backtest"],
            "季度重算；P4 实证驱动 SIGNAL_WEIGHTS")


def run_stage(name, args, note=""):
    print("\n>>> %s%s" % (name, ("  [%s]" % note) if note else ""))
    print("    $ %s" % " ".join(args))
    t0 = time.time()
    r = subprocess.run(args, cwd=ROOT)
    secs = time.time() - t0
    if r.returncode == 0:
        print("    [OK] 用时 %.1fs" % secs)
    else:
        print("    [FAIL] returncode=%s 用时 %.1fs" % (r.returncode, secs))
    return r.returncode


def main():
    ap = argparse.ArgumentParser(description="主工作流编排：采集→因子→评估→回测→选股")
    ap.add_argument("--local", action="store_true", help="跑本地可算阶段(评估+选股)")
    ap.add_argument("--with-indicators", action="store_true", help="本地阶段包含因子全量 backfill")
    ap.add_argument("--backtest", action="store_true", help="额外跑信号回测全量(季度)")
    ap.add_argument("--full", action="store_true", help="本地全量(因子+评估+选股+回测)")
    args = ap.parse_args()

    print("=" * 62)
    print("主工作流：采集(net) → 因子 → 评估 → 回测(季度) → 选股")
    print("=" * 62)

    print("\n[出网阶段 · 须进哥本机 venv 执行，pipeline 不自动跑]")
    for name, a, note in NET_STAGES:
        print("  - %s\n      $ %s\n        (%s)" % (name, " ".join(a), note))

    if not (args.local or args.with_indicators or args.backtest or args.full):
        print("\n[本地阶段 · 本命令仅打印计划]")
        print("  --local          评估 + 选股（默认，秒级）")
        print("  --with-indicators  + 因子全量 backfill")
        print("  --backtest        + 信号回测全量(季度)")
        print("  --full            含以上全部")
        return

    stages = []
    if args.with_indicators or args.full:
        stages.append(INDICATORS)
    stages.append(UNIVERSE_SCAN)
    stages.append(STRATEGY)
    if args.backtest or args.full:
        stages.append(BACKTEST)

    print("\n[执行本地阶段] 共 %d 项" % len(stages))
    fails = 0
    for name, a, note in stages:
        rc = run_stage(name, a, note)
        if rc != 0:
            fails += 1
    print("\n本地阶段完成：失败 %d 项" % fails)
    if fails:
        sys.exit(1)
    print("PIPELINE_LOCAL_OK")


if __name__ == "__main__":
    main()
