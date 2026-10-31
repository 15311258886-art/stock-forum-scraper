# -*- coding: utf-8 -*-
"""`viewer/desktop_app.py` 的可诊断性守卫（2026-11-01 工单②）。

背景（为什么值得一个专门的测试文件）：
`restart_viewer.py` 拉起 desktop_app 后，进程 4~5 秒内**静默消失** —— 日志空、无报错。
后来查明是父会话退出时整棵进程树被作业对象回收（不是它自己自杀），但**这不能成为结论**：
旧代码在 `_setup_logging()` 之前一行都不写，所以"它到底死在哪一步"这个问题当时根本没法回答。
进哥双击桌面图标走的就是这条路 —— 出问题时无从下手才是真缺陷。

因此这里把「必须留下痕迹」钉成源码契约，而不是靠"我记得加过日志"：
  1. 日志是**固定文件名** `viewer/logs/desktop_app.log`；
  2. `main()` 里**先建日志，再藏控制台**（反过来时，藏控制台那步一死就一个字都没有）；
  3. 四条出口全接上：sys.excepthook / threading.excepthook / atexit / signal 三种；
  4. 每条提前 return 都必须先写 `_EXIT["reason"]`，`main()` 里不许有裸 `os._exit`；
  5. 窗口「进/出」各有阶段行，退出原因看得见；
  6. **不许加重试、不许加常驻兜底** —— 要的是"死了能说出为什么"，不是"看起来还活着"。

第 6 条同样是被要求的：悄悄加重试会让"启动失败"变成"启动很慢然后还是失败"，
把一次性可诊断的故障变成间歇性的、日志里看不出所以然的故障。
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

SRC_PATH = ROOT / "viewer" / "desktop_app.py"
SRC = SRC_PATH.read_text(encoding="utf-8")
TREE = ast.parse(SRC)

_NESTED = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)


def _func(name: str) -> ast.FunctionDef:
    for node in TREE.body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError("desktop_app.py 里找不到函数 %s（改名了？守卫要跟着改）" % name)


def _walk_own(node: ast.AST):
    """遍历 node 内部，但**不进嵌套函数/类** —— 我们只看这个函数自己的代码。"""
    stack = list(ast.iter_child_nodes(node))
    while stack:
        cur = stack.pop()
        yield cur
        if isinstance(cur, _NESTED):
            continue
        stack.extend(ast.iter_child_nodes(cur))


def _parents() -> dict:
    pmap = {}
    for node in ast.walk(TREE):
        for child in ast.iter_child_nodes(node):
            pmap[child] = node
    return pmap


def _assigns_exit_reason(stmt: ast.stmt) -> bool:
    """这条语句是不是 `_EXIT["reason"] = ...`。"""
    if not isinstance(stmt, ast.Assign):
        return False
    for tgt in stmt.targets:
        if (isinstance(tgt, ast.Subscript) and isinstance(tgt.value, ast.Name)
                and tgt.value.id == "_EXIT"
                and isinstance(tgt.slice, ast.Constant) and tgt.slice.value == "reason"):
            return True
    return False


def test_desktop_app_logs_every_exit_path():
    """核心守卫：日志位置固定、建日志早于藏控制台、四条出口齐、退出有原因。

    这条测试的全部意义在于**它会在实现退化时变红**。为此每条断言都对着 AST/真实常量，
    而不是"文件里包含某个字符串"——后者写起来轻松，却对"日志还在、但早就不在 main 里了"
    这种退化毫无反应。
    """
    from viewer import desktop_app as da

    # ① 固定文件名（旧口径是 desktop-YYYYMMDD.log，排障时要在十几个文件里找"刚才那次"）
    assert da.LOG_PATH.replace("\\", "/").endswith("viewer/logs/desktop_app.log"), (
        "诊断日志必须是固定文件名 viewer/logs/desktop_app.log，实际 = %r" % (da.LOG_PATH,))
    for stale in ("desktop-%s.log", "desktop-%Y%m%d", "strftime"):
        assert stale not in SRC, "还留着按日期切分的旧日志命名（%r）" % (stale,)

    # ② main() 里先建日志，再藏控制台 —— 顺序反了，"藏控制台"那步一死就一个字都没有
    main = _func("main")
    calls = [n for n in _walk_own(main) if isinstance(n, ast.Call)]
    setup_lines = [c.lineno for c in calls
                   if isinstance(c.func, ast.Name) and c.func.id == "_setup_logging"]
    hide_lines = [c.lineno for c in calls
                  if isinstance(c.func, ast.Name) and c.func.id == "_hide_startup_console"]
    assert setup_lines, "main() 里没有调用 _setup_logging()"
    assert hide_lines, "main() 里没有调用 _hide_startup_console()"
    assert min(setup_lines) < min(hide_lines), (
        "_setup_logging() 必须在 _hide_startup_console() **之前**（%s vs %s）："
        "反过来的话，藏控制台那一步把进程弄死时日志根本没建，"
        "现象就是这次查了两轮的「启动失败且一个字都没有」"
        % (min(setup_lines), min(hide_lines)))

    # ③ 四条出口全接上（缺一条就有一种"无声消失"无法归因）
    hooks = _func("_install_crash_hooks")
    assert any(isinstance(n, ast.Assign) and isinstance(n.targets[0], ast.Attribute)
               and n.targets[0].attr == "excepthook"
               and isinstance(n.targets[0].value, ast.Name) and n.targets[0].value.id == "sys"
               for n in _walk_own(hooks)), "没接 sys.excepthook（主线程未捕获异常会无声）"
    assert any(isinstance(n, ast.Assign) and isinstance(n.targets[0], ast.Attribute)
               and n.targets[0].attr == "excepthook"
               and isinstance(n.targets[0].value, ast.Name) and n.targets[0].value.id == "threading"
               for n in _walk_own(hooks)), "没接 threading.excepthook（后台线程异常会无声）"
    assert any(isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
               and n.func.attr == "register"
               and isinstance(n.func.value, ast.Name) and n.func.value.id == "atexit"
               for n in _walk_own(hooks)), "没接 atexit（正常退出没有收尾行）"
    sig_names = {n.value for n in _walk_own(hooks)
                 if isinstance(n, ast.Constant) and isinstance(n.value, str)}
    assert {"SIGINT", "SIGTERM", "SIGBREAK"} <= sig_names, (
        "信号钩子没覆盖 SIGINT/SIGTERM/SIGBREAK 三种，实际只注册了 %s" % (sorted(sig_names),))
    assert any(isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
               and n.func.attr == "signal"
               and isinstance(n.func.value, ast.Name) and n.func.value.id == "signal"
               for n in _walk_own(hooks)), "没有真的调用 signal.signal 去注册处理函数"
    assert "_install_crash_hooks()" in SRC and any(
        isinstance(c, ast.Call) and isinstance(c.func, ast.Name) and c.func.id == "_install_crash_hooks"
        for c in calls), "main() 里没有调用 _install_crash_hooks()，钩子装了也没人装"

    # ④ 每条提前 return 必须先写明原因；main 里不许有裸 os._exit
    pmap = _parents()
    returns = [n for n in _walk_own(main) if isinstance(n, ast.Return)]
    assert returns, "main() 里一条 return 都没有？结构变了，守卫要重新对着看"
    for ret in returns:
        block = pmap[ret].body
        idx = block.index(ret)
        assert any(_assigns_exit_reason(s) for s in block[:idx]), (
            "第 %d 行的 return 之前没有写 _EXIT[\"reason\"] —— "
            "这条路径退出时日志只会说 reason=unknown，等于没写" % ret.lineno)
    assert not [n for n in _walk_own(main)
                if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                and n.func.attr == "_exit"
                and isinstance(n.func.value, ast.Name) and n.func.value.id == "os"], (
        "main() 里有裸 os._exit —— 它会跳过所有日志刷新，退出原因必然丢失；"
        "请走 _hard_exit(code, reason)")

    hard = _func("_hard_exit")
    args = [a.arg for a in hard.args.args]
    assert args[:2] == ["code", "reason"], "_hard_exit 的签名必须带 reason，实际 %r" % (args,)
    hard_src = ast.get_source_segment(SRC, hard) or ""
    assert "reason=%s" in hard_src, (
        "_hard_exit 落盘的那行必须把 reason 打出来，否则退出原因还是丢的")

    # ⑤ 窗口进/出各有阶段行（"卡在 webview.start 里"与"进都没进去"是两种完全不同的故障）
    stages = {n.args[0].value for n in _walk_own(main)
              if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "_stage"
              and n.args and isinstance(n.args[0], ast.Constant)}
    for need in ("boot", "console_hidden", "port_probe", "window_created",
                 "webview_start_enter", "webview_start_return"):
        assert need in stages, "缺少阶段标记 %r（现存 %s）" % (need, sorted(stages))

    # ⑥ 窗口退出原因看得见
    assert 'window_closed' in SRC, "窗口正常关闭时没有标记 reason=window_closed"


def test_desktop_app_does_not_retry_the_window():
    """不许给建窗/启动加重试或常驻兜底（工单明确要求）。

    理由：加重试会把"启动失败"变成"启动很慢，然后还是失败"，把一次性可诊断的故障
    变成间歇性的；加常驻兜底则会让进程在窗口已经不可用时继续"看起来活着"，
    两种都让日志失去意义。webview.start 只能被调用一次，且不能在循环里。
    """
    main = _func("main")
    starts = [n for n in _walk_own(main)
              if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
              and n.func.attr == "start"
              and isinstance(n.func.value, ast.Name) and n.func.value.id == "webview"]
    assert len(starts) == 1, "webview.start 被调用 %d 次（必须是 1 次）" % len(starts)

    pmap = _parents()
    node = starts[0]
    while node in pmap:
        node = pmap[node]
        assert not isinstance(node, (ast.For, ast.While, ast.AsyncFor)), (
            "webview.start 被放进了循环 —— 那就是「失败就重试」的写法，会给用户一个"
            "「启动很慢」的假象，而真正的原因被重试淹掉")
    # 也不许用「进程活着但窗口没了就重开」这类常驻兜底
    for bad in ("while True", "for _ in range(3)"):
        assert bad not in SRC, "出现可疑的重试/兜底写法：%r" % (bad,)


def test_desktop_app_banner_covers_the_three_things_we_always_ask():
    """启动横幅必须一次答完排障最常问的三个问题：哪个解释器、哪个 cwd、webview 在不在。"""
    env = _func("_log_env")
    text = ast.get_source_segment(SRC, env) or ""
    for need in ("sys.executable", "os.getcwd()", "find_spec(\"webview\")", "sys.argv"):
        assert need in text, "_log_env 没记录 %s（排障时每次都要现场问一遍）" % need
