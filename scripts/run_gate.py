# -*- coding: utf-8 -*-
"""本地等价门禁：串联 阶段0 变异自检闸 + 阶段1 缺陷探针 + pytest 正确性测试。

等价于 .github/workflows/selfcheck-gate.yml。任意一步非零退出 → 整体非零（阻断提交 / 合并）。
用法：.venv/Scripts/python.exe scripts/run_gate.py
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STEPS = []


def _run(name: str, cmd: list) -> bool:
    print("\n=== 本地门禁: %s ===" % name)
    r = subprocess.run(cmd, cwd=str(ROOT))
    STEPS.append((name, r.returncode))
    return r.returncode == 0


def main() -> int:
    ok = True
    ok &= _run("阶段0 变异自检闸", [sys.executable, "stage0_selfcheck_gate.py"])
    ok &= _run("阶段1 缺陷探针", [sys.executable, "defect_probe.py"])
    ok &= _run("正确性 pytest",
               [sys.executable, "-m", "pytest",
                "test_reasonableness_gate.py",
                "test_defect_probe.py",
                "test_fixtures_oracle.py",
                "test_stage0_correctness.py", "-q"])
    print("\n=== 本地门禁结论 ===")
    for n, rc in STEPS:
        print("  %s -> exit %d" % (n, rc))
    if ok:
        print("ALL GATES PASSED")
        return 0
    print("GATE FAILED")
    return 1


if __name__ == "__main__":
    sys.exit(main())
