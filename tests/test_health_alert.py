# -*- coding: utf-8 -*-
"""巡检弹窗与告警适用性的守卫测试（2026-11-02）。

背景：桌面每隔 6 小时弹一个「Windows Script Host 脚本错误：未结束的字符串常量
（行1 字符13）」，一次两个。根因是本文件里的 popup_alert 把含换行的消息直接
拼进了 VBScript 的 MsgBox 字面量 —— VBScript 的字符串字面量**不允许跨行**。

这里钉三件"错了也不抛异常、只是默默变成噪音"的事：
  1. 生成出来的 VBS 必须是"每行字面量都闭合"的，且消息正文**不得**出现在代码里；
  2. 集中度指标在小样本下必须判"不可用"，而不是拿百分比硬套阈值造告警；
  3. "全停了"与"某个源坏了"不得同时弹 —— 同一件事只许吵一次。
"""
from __future__ import annotations

import io
import os
import sys
import tempfile

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from viewer import check_fetcher_health as F  # noqa: E402


# ── 工具 ──────────────────────────────────────────────────────────────────────

def _quotes_balanced(line: str) -> bool:
    """按 VBScript 口径数引号："" 是转义，不算开合。"""
    i, n = 0, 0
    while i < len(line):
        if line[i] == '"':
            if i + 1 < len(line) and line[i + 1] == '"':
                i += 2
                continue
            n += 1
        i += 1
    return n % 2 == 0


@pytest.fixture()
def sandbox(monkeypatch, tmp_path):
    """把临时目录、弹窗进程都换成可观测的假实现。"""
    spawned: list = []
    monkeypatch.setattr(F.tempfile, "gettempdir", lambda: str(tmp_path))
    monkeypatch.setattr(F.subprocess, "Popen",
                        lambda args, **kw: spawned.append(args))
    return tmp_path, spawned


# ── ① VBS 生成：这次的 bug 本身 ────────────────────────────────────────────────

def test_message_never_lands_in_vbs_code(sandbox):
    """消息正文不得进代码 —— 换行/引号/反斜杠都在正文里，一进字面量就可能炸。"""
    tmp, spawned = sandbox
    msg = '多行\n测试 "引号" 与反斜杠 \\ 中文：37.5%'
    F.popup_alert("WARN", msg)
    vbs = [p for p in os.listdir(tmp) if p.endswith(".vbs")][0]
    code = io.open(tmp / vbs, encoding="utf-16").read()
    assert "37.5%" not in code, "消息正文被拼进了脚本，换行就会触发 800A0409"
    assert "多行" not in code
    # 正文走独立文件，且完整保留
    msgs = [p for p in os.listdir(tmp) if p.endswith(".msg")]
    assert len(msgs) == 1
    body = io.open(tmp / msgs[0], encoding="utf-16", newline="").read()
    assert body.count("测试") == 1 and "37.5%" in body
    assert "\n" in body and body.startswith("WARN")


def test_every_line_of_vbs_has_closed_literals(sandbox):
    """逐行校验字面量闭合 —— 直接对应「未结束的字符串常量」这个报错。"""
    tmp, spawned = sandbox
    F.popup_alert("CRITICAL", "随便写点什么\n第二行")
    vbs = [p for p in os.listdir(tmp) if p.endswith(".vbs")][0]
    code = io.open(tmp / vbs, encoding="utf-16").read()
    for i, line in enumerate(code.splitlines(), 1):
        assert _quotes_balanced(line), f"第 {i} 行引号未闭合：{line!r}"


def test_vbs_is_utf16_with_bom_and_runs_silently(sandbox):
    """UTF-16(带 BOM) 才不乱码；//B 才保证即使出错也不会再糊一个错误框。"""
    tmp, spawned = sandbox
    F.popup_alert("WARN", "中文测试")
    vbs = [p for p in os.listdir(tmp) if p.endswith(".vbs")][0]
    assert open(tmp / vbs, "rb").read(2) == b"\xff\xfe"
    assert spawned and spawned[0][:2] == ["wscript.exe", "//B"], spawned


def test_popup_failure_never_raises(monkeypatch):
    """弹窗是"加装"，坏了不许影响告警落盘。"""
    monkeypatch.setattr(F.tempfile, "gettempdir",
                        lambda: (_ for _ in ()).throw(OSError("boom")))
    F.popup_alert("WARN", "x")   # 不抛即通过


# ── ② 集中度：小样本下指标无定义 ──────────────────────────────────────────────

def test_concentration_unusable_on_tiny_sample():
    """实测现场：24 条、4 个标的 -> Top5 必然 100%，这不是异常，是数学。"""
    ok, why = F.conc_usable({"total": 24, "n_symbols": 4})
    assert not ok and "样本量" in why


def test_concentration_unusable_when_symbols_le_five():
    """标的数 ≤5 时 Top5 定义上恒等于 100%，永不可能是"健康"的。"""
    ok, why = F.conc_usable({"total": 5000, "n_symbols": 5})
    assert not ok and "无定义" in why


def test_concentration_usable_on_healthy_sample():
    ok, why = F.conc_usable({"total": 5000, "n_symbols": 60})
    assert ok and why == ""


def test_tiny_sample_produces_no_concentration_alert(monkeypatch, tmp_path):
    """回归：旧版每 6 小时弹两条（CRITICAL+WARN），全是误报。"""
    monkeypatch.setattr(F, "ALERT_LOG", str(tmp_path / "a.log"))
    monkeypatch.setattr(F, "STATE_F", str(tmp_path / "s.json"))
    monkeypatch.setattr(F, "_get", lambda path, **kw: (200, {
        "errors_today": 0, "in_window": False, "sources": [],
    }))
    monkeypatch.setattr(F, "get_trades_stats", lambda: {
        "total": 24, "top1": 37.5, "top5": 100.0, "n_symbols": 4,
        "max_dt": None, "recent_authors": 3, "all_authors": 3,
    })
    calls: list = []
    monkeypatch.setattr(F, "popup_alert", lambda lv, m: calls.append((lv, m)))
    F.main()
    assert calls == [], [c[0] for c in calls]


# ── ③ 同一件事只吵一次 ────────────────────────────────────────────────────────

def _stub_api(monkeypatch, *, sched_sources, in_window=True, rf_sources=None):
    """造两个接口的返回值：自动调度器 + 手动刷新状态。"""
    sched = {"errors_today": 0, "in_window": in_window, "enabled": True,
             "sources": sched_sources, "last_run_at": "2026-10-31 22:23:00",
             "skip_reason": "" if sched_sources else "sources_empty"}
    rf = {"last_ok_at": "2026-10-31 22:23:00", "sources": rf_sources or []}

    def _fake_get(path, **kw):
        return (200, sched) if "scheduler" in path else (200, rf)

    monkeypatch.setattr(F, "_get", _fake_get)


def test_no_stale_alert_when_scheduler_has_no_sources(monkeypatch, tmp_path):
    """回归（本次实测现场）：自动调度器 sources_empty，等于根本没有自动任务。

    此时数据"36h 没更新"是因为没人点按钮，不是故障。旧版会每 2 小时弹一次，
    永远弹下去 —— 拿用户没点按钮当线上故障。
    """
    monkeypatch.setattr(F, "ALERT_LOG", str(tmp_path / "a.log"))
    monkeypatch.setattr(F, "STATE_F", str(tmp_path / "s.json"))
    _stub_api(monkeypatch, sched_sources=[], rf_sources=[
        {"source_key": "value_basics", "last_ok_at": "2026-10-31 22:23:00",
         "status": "ok", "error": ""},
        {"source_key": "taoguba", "last_ok_at": "2026-10-28 09:00:00",
         "status": "error", "error": "需要登录"},
    ])
    monkeypatch.setattr(F, "get_trades_stats", lambda: None)
    calls: list = []
    monkeypatch.setattr(F, "popup_alert", lambda lv, m: calls.append((lv, m)))
    F.main()
    assert calls == [], [c[0] for c in calls]


def test_all_stale_suppresses_single_source_error(monkeypatch, tmp_path):
    """全部信源都停了，就别再补一条"taoguba 登录过期"，那是同一件事的碎片。"""
    monkeypatch.setattr(F, "ALERT_LOG", str(tmp_path / "a.log"))
    monkeypatch.setattr(F, "STATE_F", str(tmp_path / "s.json"))
    _stub_api(monkeypatch, sched_sources=[{"source_key": "x"}], rf_sources=[
        {"source_key": "value_basics", "last_ok_at": "2026-10-31 22:23:00",
         "status": "ok", "error": ""},
        {"source_key": "taoguba", "last_ok_at": "2026-10-28 09:00:00",
         "status": "error", "error": "需要登录"},
    ])
    monkeypatch.setattr(F, "get_trades_stats", lambda: None)
    calls: list = []
    monkeypatch.setattr(F, "popup_alert", lambda lv, m: calls.append((lv, m)))
    F.main()
    assert len(calls) == 1, [(c[0], c[1][:30]) for c in calls]
    assert "没有成功更新" in calls[0][1] and "需要登录" not in calls[0][1]


def test_partial_failure_alone_does_report_named_source(monkeypatch, tmp_path):
    """反面：数据还在动、只是某个源坏了 —— 要报，且必须点名。"""
    monkeypatch.setattr(F, "ALERT_LOG", str(tmp_path / "a.log"))
    monkeypatch.setattr(F, "STATE_F", str(tmp_path / "s.json"))
    _stub_api(monkeypatch, sched_sources=[{"source_key": "x"}], rf_sources=[
        {"source_key": "value_basics", "last_ok_at": "2026-11-02 09:05:00",
         "status": "ok", "error": ""},
        {"source_key": "taoguba", "last_ok_at": "2026-10-28 09:00:00",
         "status": "error", "error": "需要登录"},
    ])
    monkeypatch.setattr(F, "get_trades_stats", lambda: None)
    calls: list = []
    monkeypatch.setattr(F, "popup_alert", lambda lv, m: calls.append((lv, m)))
    F.main()
    assert len(calls) == 1
    assert "taoguba" in calls[0][1]
