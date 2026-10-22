"""原生桌面窗口版启动器（替代「浏览器打开网页」的使用方式）—— 单实例版。

做法：复用现有本地 HTTP 服务（viewer/server.py）作为后端，
再用 pywebview 把同一套 HTML/JS 界面嵌入一个原生窗口，
从而表现为「桌面软件」，去掉浏览器地址栏/标签等干扰，双击即开。

依赖：pywebview（Windows 下使用系统 Edge WebView2 运行时，Win10/11 自带）。
若运行环境缺 WebView2，自动回退到「用默认浏览器打开」。

用法：python viewer/desktop_app.py [--allow-multi]

2026-09-22 修正（解决「一打开程序就不断多出窗口」）：
  旧版每次双击都会无条件 `webview.create_window()`；即使 8765 已被别的实例占用
  （SO_REUSEADDR 下 bind 可能"看似成功"或在守护线程里静默失败），窗口照样会开出来，
  于是窗口越开越多、关掉一个还有别的，且多个后端抢同一端口、请求随机命中旧代码。
  现在按顺序做三件事：
  1. **单实例锁**：用命名互斥体（Local\\…，纯 ctypes 标准库）判断是否已有桌面实例；
     已有则直接退出并提示去任务栏找，不再新开窗口。
  2. **端口复用**：起服务前先探测 127.0.0.1:8765；已被占用则直接复用，不重复 bind。
  3. **退出兜底**：窗口关闭后 os._exit(0)，杜绝残留进程（守护线程随进程结束）。
  调试需要同时开多个窗口时加 --allow-multi。

2026-09-23 修正（解决「msedgewebview2 has stopped working / Error launching CrashSender.exe」）：
  该弹框是内嵌 Edge WebView2 **渲染进程崩溃后、崩溃上报进程又启动失败**所致，
  属窗口显示层故障，不影响本地服务与数据。对策见下方 WEBVIEW_STORAGE / _harden_webview_env：
  ① 关闭崩溃上报器与错误弹窗；② 固定 WebView2 用户数据目录（不再每次新建临时目录）；
  ③ 后端未就绪时不再开窗（避免白屏与二次崩溃）。

2026-10-01 修正（去掉双击启动时的控制台黑框）：
  现象：双击桌面 .lnk 除了查看器窗口，还会先弹一个控制台黑框（截图里的
  「scheduler thread running / 抓取调度」那一屏），运行期间一直挂在桌面最前。
  原因：.lnk 的 Target 用的是 .venv\\Scripts\\python.exe（控制台版 Python），
  而调度/抓取日志全往 stderr 打，就全糊在这个黑框里。
  对策：不改 .lnk（手改 .lnk 会破坏 LinkTracker 校验、下次双击会弹
  「快捷方式已更改」确认框，更烦），改为在 main() 最开头 *_hide_startup_console()*：
  把 stderr 重定向到 NUL → 隐藏控制台窗口（user32.ShowWindow/SW_HIDE）
  → kernel32.FreeConsole() 脱离控制台。窗口在出现的瞬间即消失，不掉日志，
  因为日志本来就由 _setup_logging() 落盘到 viewer/logs/desktop-YYYYMMDD.log。
  排障要临时看控制台：设环境变量 VIEWER_KEEP_CONSOLE=1 再启动即可。
"""
from __future__ import annotations

import ctypes
import json
import logging
from logging.handlers import RotatingFileHandler
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

PORT = int(os.environ.get("VIEWER_PORT", "8765"))
BASE = f"http://127.0.0.1:{PORT}"
MUTEX_NAME = f"Local\\stock_forum_viewer_{PORT}"

# ---------------------------------------------------------------------------
# WebView2 稳定性加固（2026-09-23）
#   现象：窗口弹出「msedgewebview2 has stopped working — Error launching
#         CrashSender.exe」，随即窗口白屏。
#   定性：这是**内嵌 Edge WebView2 渲染进程崩溃之后，其崩溃上报进程
#         （CrashSender / crashpad handler）又启动失败**而弹出的通知框。
#         它属"窗口显示层"故障，**不涉及本地 HTTP 服务与数据**。
#   诱因（本机实测佐证）：① 反复/并发启动时每次新建一个
#         %TEMP%\tmp*\EBWebView 临时用户数据目录（本机当晚残留 7 个，18:45–23:29），
#         目录竞态/锁冲突；② 运行时刚更新（本机同时存在 153.0.4234.32 与 .48 两个版本）；
#         ③ 杀毒/EDR 拦截崩溃上报进程。
#   对策（两层，都在本文件内完成）：
#     a) 关闭崩溃上报器与错误弹窗：即使渲染进程真崩，也只静默重载，不再弹这个框；
#     b) 固定用户数据目录（storage_path + private_mode=False），不再每次新建临时目录，
#        既消除目录堆积，也避开"运行时更新期目录竞态"。
# ---------------------------------------------------------------------------
WEBVIEW_STORAGE = os.path.join(
    os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"),
    "股票讨论实时查看器", "WebView2")
_WEBVIEW2_EXTRA_ARGS = "--disable-crash-reporter --noerrdialogs"


def _hide_startup_console() -> None:
    """双击桌面 .lnk 启动时会挂一个控制台窗口（就是那个黑框）。

    做法：把已打开的控制台窗口隐藏（SW_HIDE）并脱离控制台（FreeConsole），
    全程在窗口出现的瞬间完成、不闪、不留黑框。

    两个必须注意的点：
      1. FreeConsole 之后 stdout/stderr 即失效，任何 print() 都会抛异常，
         因此**先**把它们重定向到 NUL 再 FreeConsole；
      2. 日志已由 _setup_logging() 落盘到 viewer/logs/desktop-*.log，
         脱离控制台不影响排障。
    调试时想保留控制台：设环境变量 VIEWER_KEEP_CONSOLE=1 即可跳过。
    """
    if os.name != "nt":
        return
    if os.environ.get("VIEWER_KEEP_CONSOLE"):
        return
    try:
        devnull = open(os.devnull, "w")          # Windows 下 os.devnull == "NUL"
        sys.stdout, sys.stderr = devnull, devnull
        k32 = ctypes.windll.kernel32
        u32 = ctypes.windll.user32
        k32.GetConsoleWindow.restype = ctypes.c_void_p   # HWND 是指针宽，必须显式声明
        hwnd = k32.GetConsoleWindow()
        if hwnd:
            u32.ShowWindow(ctypes.c_void_p(hwnd), 0)     # SW_HIDE（ShowWindow 在 user32，不在 kernel32）
            k32.FreeConsole()
    except Exception:                             # noqa: BLE001 —— 脱控制台失败也不阻断启动
        pass


def _setup_logging() -> None:
    """桌面入口诊断日志落盘 viewer/logs/desktop-YYYYMMDD.log（UTF-8，滚动 5MB×5）。

    修复（2026-09-28 体检报告 P1）：双击 .lnk 启动无可见控制台，原 print() 全部丢弃，
    出问题无法排查。改为强制 UTF-8 文件落盘；同时保留控制台输出（若有）。
    """
    try:
        log_dir = os.path.join(ROOT, "viewer", "logs")
        os.makedirs(log_dir, exist_ok=True)
        log_path = os.path.join(log_dir, "desktop-%s.log" % time.strftime("%Y%m%d"))
        log_path_abs = os.path.abspath(log_path)
        fmt = logging.Formatter("%(asctime)s %(levelname)s: %(message)s", "%Y-%m-%d %H:%M:%S")
        root = logging.getLogger()
        if any(isinstance(h, RotatingFileHandler) and getattr(h, "baseFilename", "") == log_path_abs
               for h in root.handlers):
            return
        fh = RotatingFileHandler(log_path, encoding="utf-8", maxBytes=5 * 1024 * 1024, backupCount=5)
        fh.setFormatter(fmt)
        fh.setLevel(logging.INFO)
        root.addHandler(fh)
        try:
            is_tty = sys.stderr is not None and os.isatty(sys.stderr.fileno())
        except Exception:
            is_tty = False
        has_console = any(isinstance(h, logging.StreamHandler) and not isinstance(h, RotatingFileHandler)
                          for h in root.handlers)
        if is_tty and not has_console:
            sh = logging.StreamHandler(sys.stderr)
            sh.setFormatter(fmt)
            sh.setLevel(logging.INFO)
            root.addHandler(sh)
        if root.level == logging.NOTSET or root.level > logging.INFO:
            root.setLevel(logging.INFO)
    except Exception as e:  # noqa: BLE001
        print(f"[desktop_app] 日志初始化失败（不影响启动）：{e}", file=sys.stderr)


def _harden_webview_env() -> None:
    """在 import webview / 创建 WebView2 环境**之前**注入附加启动参数。

    必须在 WebView2 环境创建前生效；幂等（已含则不重复追加），并保留调用方
    可能已设置的其它参数。
    """
    cur = (os.environ.get("WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS") or "").strip()
    if _WEBVIEW2_EXTRA_ARGS not in cur:
        os.environ["WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS"] = (cur + " " + _WEBVIEW2_EXTRA_ARGS).strip()


def _port_in_use(host: str = "127.0.0.1", port: int = PORT) -> bool:
    """探测端口是否已有服务在监听（有则复用，不再重复 bind）。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.6)
        return s.connect_ex((host, port)) == 0


def _disk_build_id() -> str:
    """磁盘上的后端代码指纹（与 server 侧同一实现，见 viewer/build_id.py）。"""
    try:
        from viewer import build_id as _bid
        return _bid.of_disk(ROOT)
    except Exception:  # noqa: BLE001 —— 指纹算不出来不阻断启动
        return ""


def _running_backend_build() -> dict:
    """探测 8765 上正在监听的后端，返回其 /api/version；拿不到返回 {}。"""
    try:
        with urllib.request.urlopen(BASE + "/api/version", timeout=2) as r:
            d = json.loads(r.read().decode("utf-8", "replace"))
        return d if isinstance(d, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def _wait_backend_refresh(old_build, tries: int = 100, interval: float = 0.3) -> bool:
    """等后端换成新代码（/api/version 的 build_id 变掉）。最多 tries×interval 秒。"""
    for _ in range(tries):
        cur = _running_backend_build()
        if cur.get("build_id") and cur.get("build_id") != old_build:
            return True
        time.sleep(interval)
    return False


def _acquire_single_instance() -> bool:
    """True=本进程是唯一实例；False=已有实例在运行。非 Windows 环境一律放行。

    2026-10-05（优化报告 建议1 / P1）：旧实现用 ``ctypes.windll.kernel32``，
    默认 ``use_last_error=False``，此时 ``GetLastError`` 取到的是**上一次无关
    Win32 调用**残留的错误码，==183 的判定既可能假阳性也可能漏判，表现就是
    「双击启动能并存 1 个 server + 2 个 desktop 窗口」。

    两处修正：
    1) ``use_last_error=True`` + 用 ``ctypes.get_last_error()`` 读错误码，
       并只取 ``CreateMutexW`` 自己失败那一次的值（WA 前先 SetLastError(0)）；
    2) 互斥体只是**进程级**判定，端口/服务还活着才算真有实例 —— 互斥体判 False
       时再补一次端口持有者检测，端口已有人监听才认定为「已有实例」。
    """
    if os.name != "nt":
        return True
    # 本进程已持有过互斥体 = 本进程就是那个唯一实例（同进程重复调用直接放行）。
    # 少了这一句，第二次调用会因 "ERROR_ALREADY_EXISTS" 落去查端口，
    # 而端口一旦被（自己起的）服务占着就被误判成「已有别的实例」而拒绝启动。
    if globals().get("_MUTEX_HANDLE"):
        return True
    try:
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.SetLastError(0)                   # 只对 CreateMutexW 这次调用负责
        handle = k32.CreateMutexW(None, False, MUTEX_NAME)
        if not handle:
            logging.debug("[desktop_app] 创建互斥体失败，回退端口持有者检测。")
            return not _port_in_use()
        if ctypes.get_last_error() == 183:    # ERROR_ALREADY_EXISTS
            # 互斥体层面已有实例 —— 但服务可能早被其它手段拉起/端口已被复用，
            # 以端口持有者为准：端口没人监听说明前端根本连不上，这次照常放行。
            if _port_in_use():
                logging.info("[desktop_app] 检测到已有查看器实例在运行（%s 已被占用），"
                             "本次不再新开窗口（请在任务栏切换；并行调试请加 --allow-multi）。", PORT)
                return False
            logging.warning("[desktop_app] 互斥体已存在但端口 %s 无人监听（疑似残留/孤儿互斥体），"
                            "本次按唯一实例启动。", PORT)
        globals()["_MUTEX_HANDLE"] = handle   # 持有到进程结束，进程退出即自动释放
        return True
    except Exception:                         # noqa: BLE001 —— 任何异常都不阻断启动
        return True


def _start_server():
    """【旧路径，仅作兜底】在本进程内以 daemon 线程起服务。

    2026-10-06 王总反馈「点了刷新也没反应」——根因就在这条路径上：
    服务跑在**本进程的守护线程**里，而主进程在 WebView2 窗口关闭/崩溃时
    ``os._exit(0)`` 兜底退出，守护线程随之销毁，窗口还在、服务已没，
    页面点「刷新」自然永远没反应。故默认改走 ``_spawn_server_process()``
    拉**独立进程**，本函数只保留在「独立进程拉不起来」时的最后兜底。
    """
    import viewer.server as SVR
    SVR.main()


def _server_log_path() -> str:
    log_dir = os.path.join(ROOT, "viewer", "logs")
    os.makedirs(log_dir, exist_ok=True)
    return os.path.join(log_dir, "server-standalone.log")


def _spawn_server_process() -> None:
    """detached 拉起一个**独立进程**跑 viewer.server（脱离本进程，窗口关了服务还在）。

    与旧做法（本进程 daemon 线程）的区别：进程边界 = 生命周期边界，
    窗口崩溃 / 关闭只带走窗口进程，后端服务继续监听 8765，页面刷新即恢复。
    server.py 自带单实例锁（``_acquire_server_instance``），重复拉起会自行退出，不会抢端口。
    """
    fh = open(_server_log_path(), "ab", buffering=0)   # 不随 with 关闭，参看 server.py 同名注释
    creation = 0x00000008 | 0x00000200 if os.name == "nt" else 0   # DETACHED_PROCESS | NEW_PROCESS_GROUP
    subprocess.Popen(
        [sys.executable, "-u", "-m", "viewer.server"],
        cwd=ROOT, stdout=fh, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
        creationflags=creation, close_fds=True,
    )


def _wait_port(host: str = "127.0.0.1", port: int = PORT, tries: int = 60, interval: float = 0.3) -> bool:
    """轮询等待端口就绪；最多 tries × interval 秒。"""
    for _ in range(tries):
        if _port_in_use(host, port):
            return True
        time.sleep(interval)
    return _port_in_use(host, port)


def _js_restart_service():
    """供前端 JS 调用（pywebview expose）：**服务已断但窗口还在**时一键把后端拉回来。

    这是「点了刷新没用」的正面解法：服务进程没了，前端 POST 任何 /api 都发不出去，
    所以重启入口必须走窗口侧（pywebview 桥），而不是 HTTP。
    浏览器打开（非桌面窗口）的场景拿不到这个桥，前端会降级为「请双击桌面快捷方式」提示。
    """
    try:
        if _port_in_use():
            return {"ok": True, "already": True, "message": "本地服务已在运行，无需重启。"}
        _spawn_server_process()
        if _wait_port():
            return {"ok": True, "already": False, "message": "本地服务已重启，正在重连…"}
        return {"ok": False, "message": "已发起重启，但 18 秒内未就绪，请稍候再点一次或重启程序。"}
    except Exception as e:  # noqa: BLE001 —— JS 侧只拿到字符串，任何异常都要带出原因
        return {"ok": False, "message": "拉起本地服务失败：%s" % e}


def main():
    _hide_startup_console()      # 先去黑框，再初始化日志（日志改落盘，不受影响）
    _setup_logging()
    allow_multi = "--allow-multi" in sys.argv

    if not allow_multi and not _acquire_single_instance():
        logging.info("[desktop_app] 已有查看器窗口在运行，本次不再新开窗口（请在任务栏切换；"
                     "如需并行调试加 --allow-multi）。")
        return

    server_ready = _port_in_use()
    if server_ready:
        # 2026-10-16：端口有人听 ≠ 跑的是最新代码。原来在这里无条件「复用」，于是改了
        # .py / 静态资源后双击图标重启，后端仍是旧进程、旧代码，新接口直接 404
        # （进哥 10-16 反馈的「量化信号」404 就是这么来的——旧进程 02:17 起，
        #  量化路由 09:42 才写进 server.py，它当然没有）。
        # 现在改为：比对「运行中后端的 build_id」与「磁盘 build_id」，不一致就接管重启
        # （新后端进程会终止旧后端并抢占端口，见 server._takeover_stale_server）。
        _running = _running_backend_build()
        _disk = _disk_build_id()
        if _disk and _running and _running.get("build_id") != _disk:
            logging.warning("[desktop_app] 8765 上的后端是旧代码（运行 build=%s ≠ 磁盘 build=%s），"
                            "接管重启中…", _running.get("build_id"), _disk)
            try:
                _spawn_server_process()          # 新进程负责终止旧后端并接管端口
                if _wait_backend_refresh(_running.get("build_id")):
                    logging.info("[desktop_app] 后端已切换到最新代码（build=%s）。", _disk)
                else:
                    logging.warning("[desktop_app] 后端接管超时，暂以现有后端继续"
                                    "（页面可能仍是旧代码，稍后重开一次即可）。")
            except Exception as e:  # noqa: BLE001 —— 接管失败不影响开窗
                logging.warning("[desktop_app] 接管旧后端失败（%s），沿用现有后端。", e)
        else:
            logging.info("[desktop_app] 端口 %s 已有服务在监听，直接复用（未重复启动后端）。", PORT)
    else:
        # 2026-10-06：默认拉**独立进程**跑后端，不再用本进程 daemon 线程
        # （旧做法下窗口一崩/一关，服务随 os._exit(0) 一起没了，页面就成死页）。
        try:
            _spawn_server_process()
            server_ready = _wait_port()
        except Exception as e:  # noqa: BLE001 —— 拉不起来再退到线程兜底，不阻断开窗
            logging.warning("[desktop_app] 独立进程拉起后端失败（%s），回退本进程线程。", e)
        if not server_ready:
            threading.Thread(target=_start_server, daemon=True).start()
            for _ in range(50):
                try:
                    urllib.request.urlopen(BASE + "/api/summary", timeout=2)
                    server_ready = True
                    break
                except Exception:  # noqa: BLE001
                    time.sleep(0.2)

    # 2026-09-23：后端没起来就**不要再开窗口** —— 旧版对着一个死地址开窗只会白屏，
    # 还平白让 WebView2 多崩一次。这里给出可执行的诊断后直接退出。
    if not server_ready:
        logging.error("[desktop_app] 本地服务未在预期时间内就绪（%s）。"
                      "请先确认 8765 未被残留实例占用，或直接运行："
                      ".venv\\Scripts\\python.exe -m viewer.server", BASE)
        return

    try:
        _harden_webview_env()          # 必须在 import webview / 创建 WebView2 环境之前
        import webview
        _wnd = webview.create_window(
            "股票讨论实时查看器",
            BASE + "/",
            width=1280, height=860,
            min_size=(900, 600),
            text_select=True,
        )
        # 暴露给页面 JS：服务进程阵亡时，前端没有可用的 HTTP 通道发重启请求，
        # 只能借这条 pywebview 桥让窗口侧把后端重新拉起来（见 _js_restart_service 注释）。
        try:
            _wnd.expose(_js_restart_service)
        except Exception as e:  # noqa: BLE001 —— expose 失败不影响开窗，只是少一个自愈按钮
            logging.warning("[desktop_app] 暴露重启接口给前端失败（不影响使用）：%s", e)
        # private_mode=False + storage_path：固定 WebView2 用户数据目录，
        # 不再每次启动都新建 %TEMP%\tmp*\EBWebView（消除目录堆积与运行时更新期的竞态）。
        try:
            os.makedirs(WEBVIEW_STORAGE, exist_ok=True)
        except OSError:
            pass
        webview.start(private_mode=False, storage_path=WEBVIEW_STORAGE)
    except Exception as e:  # noqa: BLE001
        # 无 WebView2 运行时等：回退到浏览器
        logging.error("[desktop_app] 原生窗口不可用（%s），回退到浏览器。", e)
        import webbrowser
        webbrowser.open(BASE + "/")
    finally:
        sys.stdout.flush()
        sys.stderr.flush()
        os._exit(0)     # 兜底：窗口关闭即退出进程，避免残留（守护线程随进程结束）


if __name__ == "__main__":
    main()
