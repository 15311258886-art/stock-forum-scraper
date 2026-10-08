# -*- coding: utf-8 -*-
"""defect_probe 自测：验证静态探针在干净代码上 pass、动态探针在缺夹具时 skip。

运行：
  .venv/Scripts/python.exe test_defect_probe.py
  .venv/Scripts/python.exe -m pytest test_defect_probe.py -q
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import defect_probe as dp  # noqa: E402


def test_static_probes_pass_on_clean_code():
    findings = dp._build_static_findings()
    pids = {f.probe_id for f in findings}
    assert pids == {"P1", "P2", "P3", "P4", "P5", "P6", "P7", "P8"}, pids
    fails = [f for f in findings if f.status == "fail"]
    assert not fails, "静态探针不应 fail: %s" % [(f.probe_id, f.evidence) for f in fails]


def test_dynamic_probes_skip_without_fixtures():
    findings = dp._build_dynamic_findings(False, None)
    assert len(findings) == 8
    assert all(f.status == "skip" for f in findings)


def test_summary_counts_consistent():
    findings = dp._build_static_findings() + dp._build_dynamic_findings(False, None)
    s = dp._summarize(findings)
    total = sum(s["counts"].values())
    assert total == len(findings)


def test_run_probe_produces_report():
    rep = dp.run_probe()
    assert rep.environment in ("sandbox_offline", "collection_host")
    assert rep.summary["overall"] in ("pass", "pass_with_warnings", "fail")
    # 沙箱无夹具 → 静态防护在位，不应有 fail
    assert rep.summary["counts"]["fail"] == 0
    assert Path(rep.report_path).exists()


if __name__ == "__main__":
    for name in list(globals()):
        if name.startswith("test_") and callable(globals()[name]):
            globals()[name]()
            print("PASS", name)
    print("ALL DEFECT_PROBE TESTS PASSED")
