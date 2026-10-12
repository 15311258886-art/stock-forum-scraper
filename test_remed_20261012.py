# -*- coding: utf-8 -*-
"""remed 20261012 回归测试 —— 对应《整改执行清单》本批已采纳条目。

设计：server.py 在 import 期就会建 Database()（有副作用），故此处不整体 import，
改用 ast 从真实源码里取出目标函数单独 exec（被测的仍是线上那份源码），
其余用静态断言锁定「已修好的形态」，防止日后被改回去。

运行：
    .venv\\Scripts\\python.exe -m pytest test_remed_20261012.py -q
    （亦可直接 `.venv\\Scripts\\python.exe test_remed_20261012.py`）
"""
from __future__ import annotations

import ast
import io
import json
import math
import os
import re
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
SERVER_PY = os.path.join(ROOT, "viewer", "server.py")
APP_JS = os.path.join(ROOT, "viewer", "static", "app.js")


def _read(path: str) -> str:
    return io.open(path, encoding="utf-8").read()


def _load_server_func(name: str, extra_ns: dict | None = None):
    """从 viewer/server.py 源码里取出名为 name 的顶层函数并 exec，返回该函数对象。"""
    src = _read(SERVER_PY)
    tree = ast.parse(src)
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            seg = ast.get_source_segment(src, node)
            ns = {"os": os, "math": math, "json": json, "re": re}
            if extra_ns:
                ns.update(extra_ns)
            exec(seg, ns)
            return ns[name]
    raise AssertionError(f"viewer/server.py 中找不到顶层函数 {name}")


# ---------------- #12 normalize：CJK 紧贴代码可提取 ----------------
def test_normalize_extracts_cjk_adjacent_code():
    sys.path.insert(0, ROOT)
    from viewer.stock_names import normalize
    assert normalize("宁德时代300750") == "300750"
    assert normalize("贵州茅台600519关注") == "600519"
    assert normalize("sh600519") == "600519"
    assert normalize("600519") == "600519"
    return True


def test_normalize_still_rejects_long_digit_runs():
    sys.path.insert(0, ROOT)
    from viewer.stock_names import normalize
    # 7 位以上数字串不应被误取为 6 位代码
    assert normalize("1234567") == "1234567"
    return True


# ---------------- #08 _json_safe：NaN/Inf 必须被清成 null ----------------
def test_json_safe_strips_nan_and_inf():
    f = _load_server_func("_json_safe")
    bad = {"a": float("nan"), "b": [1.0, float("inf"), float("-inf")],
           "c": {"d": float("nan")}, "e": "ok"}
    safe = f(bad)
    assert safe["a"] is None and safe["c"]["d"] is None
    assert safe["b"][0] == 1.0 and safe["b"][1] is None and safe["b"][2] is None
    assert safe["e"] == "ok"
    # 产物必须是合法 JSON（前端 JSON.parse 才不炸）
    out = json.dumps(safe, ensure_ascii=False, allow_nan=False, default=str)
    json.loads(out)
    return True


def test_send_json_uses_allow_nan_false():
    src = _read(SERVER_PY)
    i = src.find("def _send_json")
    assert i > 0, "找不到 _send_json"
    seg = src[i:i + 700]
    assert "allow_nan=False" in seg, "_send_json 未启用 allow_nan=False"
    assert "_json_safe(" in seg, "_send_json 未走 _json_safe 清洗"
    return True


# ---------------- #06 _pid_alive：信号安全探活 ----------------
def test_pid_alive_semantics():
    f = _load_server_func("_pid_alive")
    assert f(os.getpid()) is True          # 自己一定活着
    assert f(0) is False                   # 非法 PID
    assert f(999999) is False              # 几乎不可能存在的 PID
    return True


def test_no_signal_zero_probe_in_server():
    src = _read(SERVER_PY)
    # 排除 _pid_alive 自身：它在 POSIX 分支上有意保留 os.kill(pid, 0)（类 Unix 上安全）。
    tree = ast.parse(src)
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == "_pid_alive":
            src = src.replace(ast.get_source_segment(src, node), "")
            break
    # 除该函数外，不许再出现「os.kill(pid, 0) 探活」
    # （原 _acquire_server_instance 里那处，Windows 上会误发 CTRL_C_EVENT）
    assert not re.search(r"os\.kill\(\s*[A-Za-z_][\w]*\s*,\s*0\s*\)", src), \
        "server.py 仍存在 os.kill(pid, 0) 探活（Windows 上会误发 CTRL_C_EVENT）"
    return True


# ---------------- #07 GET 回环 Host 守卫 ----------------
def test_do_get_has_loopback_host_guard():
    src = _read(SERVER_PY)
    i = src.find("def _do_GET")
    assert i > 0
    seg = src[i:i + 800]
    assert "_host_ok" in seg, "_do_GET 未做回环 Host 校验"
    return True


def test_guard_write_requires_origin_for_dangerous():
    src = _read(SERVER_PY)
    i = src.find("def _guard_write")
    assert i > 0
    seg = src[i:i + 1600]
    assert "/api/edge/restart" in seg and "/api/login/open" in seg, \
        "_guard_write 未对危险端点强制 Origin"
    return True


# ---------------- #23 前端 keydown 监听一次性守卫 ----------------
def test_bf_keydown_binding_guarded():
    js = _read(APP_JS)
    assert "_bfKeysBound" in js, "app.js 缺少 _bfKeysBound 一次性守卫"
    i = js.find("function bindBoardFlowControls")
    assert i > 0
    seg = js[i:i + 4000]
    assert "_bfKeysBound" in seg, "bindBoardFlowControls 未使用一次性守卫"
    return True


if __name__ == "__main__":
    tests = [
        test_normalize_extracts_cjk_adjacent_code,
        test_normalize_still_rejects_long_digit_runs,
        test_json_safe_strips_nan_and_inf,
        test_send_json_uses_allow_nan_false,
        test_pid_alive_semantics,
        test_no_signal_zero_probe_in_server,
        test_do_get_has_loopback_host_guard,
        test_guard_write_requires_origin_for_dangerous,
        test_bf_keydown_binding_guarded,
    ]
    fails = []
    for t in tests:
        try:
            ok = t()
            print(("PASS " if ok else "FAIL ") + t.__name__)
            if not ok:
                fails.append(t.__name__)
        except AssertionError as e:
            print("FAIL " + t.__name__ + " :: " + str(e))
            fails.append(t.__name__)
        except Exception as e:  # noqa: BLE001
            print("ERROR " + t.__name__ + " :: " + repr(e))
            fails.append(t.__name__ + "(error)")
    print("\n==== %d/%d PASS ====" % (len(tests) - len(fails), len(tests)))
    sys.exit(1 if fails else 0)
