# -*- coding: utf-8 -*-
"""浏览器模式启动器（绕过 WebView2 原生窗口崩溃）。

双击桌面 .lnk 时由**用户会话**调用（非 agent 会话），因此拉起的纯后端
进程独立于 WorkBuddy，关机前持续运行。

流程（2026-11-01 重写，治「假成功」）：
  ① 拿单实例锁（ROOT/.launcher.lock）—— 连点快捷方式不再拉起一串启动器、开一串标签页；
  ② 8765 上已有**本查看器**且健康 → 直接复用，不重复拉起；
  ③ 否则 DETACHED 拉起纯后端（不依赖 WebView2）；
  ④ 轮询 /api/version 直到拿到「本产品」的 JSON —— 不是只看端口有没有人在听；
  ⑤ 健康才打开浏览器；任何一步失败都弹 Windows 原生错误框（含日志绝对路径）并返回非 0。

2026-09-28：原 desktop_app 原生窗口因本机 WebView2 运行时存在 153/154 双版本
冲突、渲染进程启动即崩，导致「双击没反应」。改为浏览器模式作为稳定入口。
2026-11-01（P1 假成功）：旧版在等 20 秒后**无条件** webbrowser.open，服务没起来也照样
把人带到打不开的页面；且只探测「端口有没有人在监听」、不校验 /api/version，8765 被别的
程序占着会被误判成"已启动"；还没有单实例锁、没有任何可诊断状态（pythonw 无控制台，
用户和运维都无从下手）。本次改动逐条对症，详见各函数注释。

退出码约定：
  0  成功（服务健康且浏览器已打开，或已有启动器在跑）
  1  启动器自身出了未预料的异常（兜底分支，正常不该出现）
  2  端口被非本产品占用
  3  服务在超时内未通过健康校验
  4  拉起子进程失败
  5  版本不符且用户选择不继续（默认不问，见 _confirm_continue）

本文件三个已定稿的产品口径（2026-11-01 与产品确认，改之前先读这段，别再重新纠结）：

  ① 代码指纹不一致 → **只 WARN 不拦**，默认照样打开浏览器；要询问得显式设
     VIEWER_LAUNCHER_ASK_ON_MISMATCH=1。
     理由：代码指纹按**文件内容**算，KEY_FILES 里全是每天在改的代码文件，改一个字节就变，
     一不一致就拦，启动器会变成日常骚扰；真正"你看到的是旧代码"的提示交给
     前端每 60 秒一次的 /api/version 自查条（页面顶部那个"点一下刷新"）。
     启动器只负责把事实写进 launcher_status.json 的 warn 字段，供事后追查。

  ② 慢服务重试 1s → 2s → 3s 的意图是"我们自己被 GIL 卡住时别把自家服务判成外来程序"
     （server.py 记过 /api/version 被拖到 2.4~9.8 秒的实测），
     **不是**"多等一会儿碰碰运气"。所以超时是递增重试、拿到就立刻走，不做固定长等待。

  ③ webbrowser.open 失败仍返回 0，但会弹一个框把 URL 给出来。
     理由：用户能手动复制就等于没失败；为这件事新增一个退出码，只会让所有调用方
     多一个分支去处理本来就已解决的问题。

"""
from __future__ import annotations

import atexit
import ctypes
import importlib.util
import json
import os
import socket
import subprocess
import sys
import time
import urllib.request
import webbrowser

ROOT = os.path.dirname(os.path.abspath(__file__))
PORT = 8765

# 退出码（与模块头部约定一一对应，别在别处硬编码数字）
EXIT_OK = 0
EXIT_UNEXPECTED = 1
EXIT_PORT_FOREIGN = 2
EXIT_HEALTH_TIMEOUT = 3
EXIT_SPAWN_FAIL = 4
EXIT_VERSION_MISMATCH = 5

# 等后端起来的上限与节奏：沿用旧版的 20s，但**不再**在超时后照样开浏览器
SERVICE_WAIT_SEC = 20.0
POLL_INTERVAL = 0.5
HEALTH_TIMEOUT = 1.0
# 「端口有人听但没回 JSON」时的追加探测超时（秒）。见 _probe_health_slow 的说明
_SLOW_PROBE_TIMEOUTS = (2.0, 3.0)

# 单实例锁文件。锁的是首字节；holder 信息写在 offset 1 之后
# （被锁的字节连"读"都会被系统拒绝，见 _read_lock_info 的注释）
_LOCK_NAME = ".launcher.lock"
_LOCK_SENTINEL = b"#"
_LOCK_INFO_OFFSET = 1

# 流程起点。每次 main() 重置，用于状态文件里的 elapsed_sec
_T0 = time.time()


# ══════════════════════════════════════════════════════════════════════════
# 路径（全部按 ROOT 现算，不缓存：测试与运维都要能把 ROOT 指到别处）
# ══════════════════════════════════════════════════════════════════════════
def _pyv() -> str:
    """项目自带解释器路径（拉起后端只用它，不用 PATH 里的 python）。"""
    return os.path.join(ROOT, ".venv", "Scripts", "python.exe")


def _log_path() -> str:
    """后端 stdout/stderr 落盘位置。pythonw 下没有控制台，这个文件就是唯一现场。"""
    return os.path.join(ROOT, "server_bg.log")


def _status_path() -> str:
    return os.path.join(ROOT, "viewer", "data", "launcher_status.json")


def _lock_path() -> str:
    return os.path.join(ROOT, _LOCK_NAME)


def _page_url() -> str:
    return "http://127.0.0.1:%d/" % PORT


# ══════════════════════════════════════════════════════════════════════════
# 输出：stderr 与原生消息框
# ══════════════════════════════════════════════════════════════════════════
def _say(msg: str) -> None:
    """往 stderr 写一行。pythonw 下 sys.stderr 是 None，必须判空。"""
    try:
        if sys.stderr is not None:
            sys.stderr.write(str(msg) + "\n")
            sys.stderr.flush()
    except Exception:  # noqa: BLE001 —— 输出失败不能反过来影响启动
        pass


def _notify(title: str, body: str, icon: int = 0x00000010) -> None:
    """弹消息框；无 GUI 环境降级为 stderr（**不**吞掉问题本身）。

    为什么用 ctypes 调 user32 而不用 tkinter：本项目 WebView2 / 桌面端历史上有崩溃教训，
    GUI 栈能少依赖就少依赖；user32.MessageBoxW 最轻，且 pythonw（无控制台）下依然弹得出来，
    这正是「双击没反应」最需要的可见性。
    icon: 0x10=MB_ICONERROR，0x40=MB_ICONINFORMATION。
    """
    _say("[启动器] %s\n%s" % (title, body))
    if os.name != "nt":
        # 非 Windows（CI / Linux 开发机）：stderr 已输出，没有消息框可用
        return
    try:
        ctypes.windll.user32.MessageBoxW(None, body, title, int(icon))
    except Exception:  # noqa: BLE001
        # 会话 0 / 无 user32（服务账号、自动化会话）：降级，绝不静默
        _say("[启动器] 无法显示消息框（无 GUI 环境），请以上面 stderr 输出为准。")


def _fatal(title: str, msg: str, detail_path: str | None = None, **kv) -> None:
    """失败必须可见：先写状态文件（stage=fail），再拼日志绝对路径，最后弹原生错误框。

    参数
      title        失败标题，直接当消息框标题（同时也是 state.error 的前缀）
      msg          给人看的一句话（说清失败阶段 + 可能原因）
      detail_path  额外要指给用户的文件；不传时默认带 server_bg.log + 最近的 sched 日志
      kv           透传给 _stage 的现场信息（port_owner / pid / build_id …）
    """
    logs = _log_candidates()
    if detail_path and detail_path not in logs:
        logs.append(detail_path)
    body_lines = [msg, "", "相关日志（绝对路径）："]
    body_lines += ["  · " + p for p in logs]
    body_lines += ["", "下一步：用记事本打开上面第一个日志，翻到最后 50 行看报错。"]
    _stage("fail", error=("%s｜%s" % (title, msg))[:600], **kv)
    _notify(title, "\n".join(body_lines))


def _log_candidates() -> list[str]:
    """排障要看的日志：本次后端 stdout/stderr + 最近改动的一个 sched-*.log。

    为什么还要 sched 日志：启动期最常炸的是调度/预热子进程（取数、建表），
    它们的异常只落在 viewer/logs/sched-*.log 里，server_bg.log 可能一行都没有。
    """
    out = [_log_path()]
    log_dir = os.path.join(ROOT, "viewer", "logs")
    try:
        names = [n for n in os.listdir(log_dir)
                 if n.startswith("sched-") and n.endswith(".log")]
    except OSError:
        names = []
    newest, newest_mtime = None, -1.0
    for name in names:
        path = os.path.join(log_dir, name)
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            continue
        if mtime > newest_mtime:
            newest, newest_mtime = path, mtime
    if newest:
        out.append(newest)
    return out


# ══════════════════════════════════════════════════════════════════════════
# 可诊断状态文件
# ══════════════════════════════════════════════════════════════════════════
def _stage(name: str, **kv) -> None:
    """把「当前走到哪一步」覆盖写入 viewer/data/launcher_status.json。

    为什么要有：pythonw 没有控制台，出问题后用户/运维没有任何线索。
    落一个 schema 稳定的小文件，事后一眼看到「上次跑到哪一步、PID、端口归属、指纹、耗时」。
    写失败一律忽略 —— 诊断文件不能反过来成为启动失败的原因。
    """
    payload = {
        "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
        "stage": name,            # spawn|wait|health|ok|fail
        "pid": None,              # 后端（或本启动器）PID，视阶段而定
        "port": PORT,
        "build_id": None,         # 对端 /api/version 报的代码指纹
        "build_match": None,      # 对端指纹是否等于磁盘指纹
        "port_owner": None,       # viewer|foreign|none
        "error": None,            # 失败原因（截断，供快速扫读）
        "warn": None,             # 不致命但要人知道的事（如"服务是旧版本"）
        "log": _log_path(),
        "elapsed_sec": round(time.time() - _T0, 3),
    }
    payload.update(kv)
    path = _status_path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)      # 原子替换：避免别人读到写了一半的 JSON
    except OSError:
        pass


def _read_status() -> dict:
    try:
        with open(_status_path(), "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _status_summary() -> str:
    """把上一次的状态压成一行，给「已有实例在跑」时打印用。"""
    data = _read_status()
    if not data:
        return "（还没有状态文件）"
    return ("stage=%s ts=%s pid=%s port_owner=%s build_id=%s "
            "elapsed=%ss warn=%s error=%s"
            % (data.get("stage"), data.get("ts"), data.get("pid"),
               data.get("port_owner"), data.get("build_id"),
               data.get("elapsed_sec"), data.get("warn"), data.get("error")))


# ══════════════════════════════════════════════════════════════════════════
# 健康握手（替代「端口有没有人在听」）
# ══════════════════════════════════════════════════════════════════════════
def _port_open() -> bool:
    """8765 上是否有进程在监听。只作前置快速探测，**不足以**判断是不是我们的服务。"""
    s = socket.socket()
    s.settimeout(0.5)
    try:
        return s.connect_ex(("127.0.0.1", PORT)) == 0
    finally:
        s.close()


def _probe_health(timeout: float = HEALTH_TIMEOUT) -> dict | None:
    """GET /api/version，返回解析出的 JSON dict；拿不到（连不上/超时/不是 JSON）返回 None。

    本函数**只负责"有没有 HTTP JSON"**，不做产品判定 —— 判定见 _looks_like_viewer()。
    这样拆开的好处：测试可以用 monkeypatch 直接喂各种形态的响应，
    而生产代码里"是不是本产品"的判断只有一处。
    """
    if not _port_open():
        return None
    try:
        with urllib.request.urlopen(_page_url() + "api/version", timeout=timeout) as resp:
            raw = resp.read()
    except Exception:  # noqa: BLE001 —— 连不上/超时/HTTP 错误一律当"还没有服务"
        return None
    try:
        data = json.loads(raw.decode("utf-8", "replace"))
    except (UnicodeDecodeError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _looks_like_viewer(info: dict) -> bool:
    """这个 /api/version 响应是不是本查看器的。

    依据（已核对实现与真实响应，不猜）：
      viewer/server.py 第 670-701 行 api_version() 的返回键恰好是
      version / served_at / pid / build_id / started_at / sched_task ——
      **没有** product、app 这类单一产品标识字段。
      2026-11-01 本机实测（服务在跑时 GET http://127.0.0.1:8765/api/version）：
      {"version": "20261031-layout3", "served_at": "2026-11-01 02:40:57", "pid": 7452,
       "build_id": "33f80a102b28", "started_at": "2026-11-01 00:11:54", "sched_task": "-"}

    因此用「三个字段同时具备且类型正确」作为产品指纹：
      · build_id 非空字符串（build_id.py::of_disk 的 12 位十六进制指纹）
      · pid 是整数（注意排除 bool —— 它是 int 的子类）
      · served_at 非空字符串
    只要求 build_id 非空、不校验长度/格式：万一将来指纹算法换（变长/换进制），
    启动器不能反过来把自己的服务判成"外人"。
    """
    build_id = info.get("build_id")
    if not isinstance(build_id, str) or not build_id.strip():
        return False
    pid = info.get("pid")
    if not isinstance(pid, int) or isinstance(pid, bool):
        return False
    served_at = info.get("served_at")
    if not isinstance(served_at, str) or not served_at.strip():
        return False
    return True


def _local_build_id() -> str:
    """现算磁盘上的代码指纹（viewer/build_id.py::of_disk）；算不出来返回空串。

    为什么不 `import viewer.build_id`：那会先执行 viewer/__init__.py。虽然当前那个文件
    没有副作用，但历史上"import 即建表/起线程"的坑刚填过（P0-05）。build_id.py 是零依赖
    纯函数模块，按文件路径直接加载最稳也最快。
    成本：2026-11-01 起 of_disk 改为**逐文件内容 sha1**（原来只 stat 的 (mtime, size) 有漏报洞，
    见该文件头注释），KEY_FILES 合计约 2.5MB，本函数会读一遍 ≈10~30ms —— 只在启动时算一次。
    of_disk 自己就是"任何文件缺失都记 missing、不抛异常"的契约，这里再兜一层：
    指纹算不出来绝不能成为启动失败的原因。
    """
    try:
        path = os.path.join(ROOT, "viewer", "build_id.py")
        spec = importlib.util.spec_from_file_location("_launcher_build_id", path)
        if spec is None or spec.loader is None:
            return ""
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return str(mod.of_disk(ROOT) or "")
    except Exception:  # noqa: BLE001
        return ""


def _probe_health_slow() -> dict | None:
    """给「端口有人在听但一秒内没回 JSON」的情况多几次、更长超时的重试。

    为什么需要：viewer/server.py 第 41 行的注释记着实测数据 —— 单线程 GIL 被调度线程
    占住时，连 /api/version 这种纯读缓存的请求都可能被拖到 2.4~9.8 秒。若只探 1 秒就
    下结论，本查看器只是"慢"就会被误判成"别的程序占了端口"，反而制造新的假失败。
    宁可多等几秒，也不要给用户一个错误的结论。
    """
    for timeout in _SLOW_PROBE_TIMEOUTS:
        info = _probe_health(timeout)
        if info is not None:
            return info
        time.sleep(POLL_INTERVAL)
    return None


def _wait_health(deadline: float) -> tuple[dict | None, dict | None]:
    """轮询到拿到「本查看器」的 /api/version，或到 deadline 为止。

    返回 (ours, foreign)：
      · ours 非空    → 健康，可用；
      · foreign 非空 → 端口被非本产品占着（例如我们拉起失败、端口被别人抢走了）；
      · 都为空       → 超时，HTTP 根本没通。
    顺序上先判 foreign 再判超时：如果端口被别人抢了，早退比干等 20 秒有用得多。
    """
    while time.time() < deadline:
        info = _probe_health(HEALTH_TIMEOUT)
        if info is not None:
            if _looks_like_viewer(info):
                return info, None
            return None, info
        time.sleep(POLL_INTERVAL)
    return None, None


# ══════════════════════════════════════════════════════════════════════════
# 单实例锁（跨平台、零第三方依赖）
# ══════════════════════════════════════════════════════════════════════════
def _open_lock_file():
    """打开（必要时创建）锁文件，且**不截断**已有内容。

    为什么不用 open(path, "w+b")：文件已存在时它会先清空，后来者会把 holder 信息擦掉。
    为什么不用 "a+b"：追加模式下写入永远落在末尾，没法把 holder 信息写到固定偏移。
    """
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0)
    fd = os.open(_lock_path(), flags, 0o600)
    return os.fdopen(fd, "r+b")


def _try_lock(fh) -> bool:
    """非阻塞拿锁：拿到 True，被别人占着 False。

    Windows 用 msvcrt.locking 锁首字节，POSIX 用 flock。
    实测（本机 Windows 11 / Python 3.13.14）三条要注意的事实：
      · 同一进程用两个句柄锁同一字节**也会失败**（EACCES），所以连点快捷方式
        即使在同一个会话里叠起来也不会双双放行；
      · 跨进程同样失败（EACCES）；
      · 被锁的字节连**读**都会 PermissionError —— 所以 holder 信息必须写在 offset 1 之后。
    """
    try:
        if os.fstat(fh.fileno()).st_size == 0:
            # 只有空文件才写哨兵字节：非空说明别人已经在用，此时碰 0 号字节会 EACCES
            fh.seek(0)
            fh.write(_LOCK_SENTINEL)
            fh.flush()
        fh.seek(0)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError:
        return False


def _write_lock_info(fh) -> None:
    """把「谁拿着锁」写进锁文件 offset 1 之后，供后来者读到并如实报告。"""
    try:
        info = {
            "pid": os.getpid(),
            "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
            "port": PORT,
            "script": os.path.abspath(__file__),
        }
        fh.seek(_LOCK_INFO_OFFSET)
        fh.truncate(_LOCK_INFO_OFFSET)
        fh.write(json.dumps(info, ensure_ascii=False).encode("utf-8"))
        fh.flush()
    except OSError:
        pass


def _read_lock_info() -> dict:
    """读 holder 信息。**必须**从 offset 1 开始读：0 号字节被占锁进程锁着，读它会 EACCES。"""
    try:
        with open(_lock_path(), "rb") as f:
            f.seek(_LOCK_INFO_OFFSET)
            raw = f.read()
        data = json.loads(raw.decode("utf-8", "replace"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _acquire_lock() -> tuple[object, bool]:
    """拿单实例锁，返回 (句柄, 是否拿到)；没拿到时句柄为 None。

    锁文件本身不可用（只读目录/权限异常）时**放行**：宁可暂时没有单实例保护，
    也不能因为这个附加功能把用户挡在启动之外。
    """
    try:
        fh = _open_lock_file()
    except OSError as e:
        _say("[启动器] 锁文件不可用（%s），本次无单实例保护。" % e)
        return None, True
    if _try_lock(fh):
        _write_lock_info(fh)
        return fh, True
    try:
        fh.close()
    except OSError:
        pass
    return None, False


def _release_lock(fh) -> None:
    """释放锁并关句柄。幂等：atexit 与 finally 都会调它。"""
    if fh is None:
        return
    try:
        if not getattr(fh, "closed", False):
            try:
                if os.name == "nt":
                    import msvcrt
                    fh.seek(0)
                    msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
    finally:
        try:
            fh.close()
        except OSError:
            pass


# ══════════════════════════════════════════════════════════════════════════
# 拉起后端
# ══════════════════════════════════════════════════════════════════════════
def _spawn_server():
    """DETACHED + NEW_PROCESS_GROUP 拉起纯后端，返回 Popen。

    这两个 creationflags 必须保留：让后端脱离 .lnk 启动的那棵进程树，双击后的启动器
    退出也不带走它。Windows 之外传非 0 会 ValueError，所以按平台算。
    """
    log = _log_path()
    parent = os.path.dirname(log)
    if parent:
        os.makedirs(parent, exist_ok=True)
    # "w" 每次拉起覆盖旧日志：一次启动一份现场，避免历史噪音混进来（复用旧服务时不会走到这里）
    fh = open(log, "w", encoding="utf-8")
    try:
        return subprocess.Popen(
            [_pyv(), "-m", "viewer.server"],
            cwd=ROOT,
            creationflags=(0x00000008 | 0x00000200) if os.name == "nt" else 0,
            stdout=fh,
            stderr=subprocess.STDOUT,
        )
    finally:
        # 父进程这份句柄用完即关（子进程已继承），否则每个启动器都漏一个文件句柄
        fh.close()


# ══════════════════════════════════════════════════════════════════════════
# 失败分支（统一出口，保证退出码 + 状态文件 + 可见提示三者一致）
# ══════════════════════════════════════════════════════════════════════════
def _fail_foreign(info: dict | None) -> int:
    """端口 8765 上回的不是本查看器 —— 最常见的"别人的程序占了端口"。"""
    detail = ""
    if info:
        try:
            detail = "\n\n对端返回的内容：%s" % json.dumps(info, ensure_ascii=False)[:300]
        except (TypeError, ValueError):
            detail = "\n\n对端返回的内容：%r" % (info,)
        why = "返回的不是本查看器"
    else:
        # 拿不到 JSON：是别人的程序，也可能是本查看器卡住了 —— 两种都如实说，不装懂
        why = ("它连 /api/version 都没有返回本查看器的 JSON（等了 %s 秒）"
               % int(sum(_SLOW_PROBE_TIMEOUTS) + HEALTH_TIMEOUT))
    msg = ("端口 %d 上有一个程序在监听，但%s，因此没有打开浏览器。%s\n\n"
           "下一步：关掉占用该端口的程序后重新双击图标；"
           "或先执行「netstat -ano | findstr :%d」看是谁占着（最后一列是 PID）。"
           % (PORT, why, detail, PORT))
    _fatal("查看器无法启动：端口 %d 被其它程序占用" % PORT, msg, port_owner="foreign")
    return EXIT_PORT_FOREIGN


def _fail_spawn(err: BaseException) -> int:
    """拉起子进程就失败了（解释器不见了 / 目录不可写）。"""
    msg = ("拉起后端进程失败：%s\n\n"
           "常见原因：.venv 被删除或移动、磁盘/目录不可写。\n"
           "下一步：确认解释器存在：%s" % (err, _pyv()))
    _fatal("查看器无法启动：拉起后端失败", msg, port_owner="none")
    return EXIT_SPAWN_FAIL


def _fail_timeout(pid) -> int:
    """后端进程起来了，但超时内没通过 /api/version 健康校验。"""
    msg = ("后端进程已拉起（pid=%s），但 %d 秒内 /api/version 仍没有返回可识别的响应，"
           "因此**没有**打开浏览器。\n\n"
           "服务卡在启动阶段：依赖缺失、数据库被占用、端口绑定失败都会这样，"
           "原因只写在日志里。"
           % (pid if pid else "未知", int(SERVICE_WAIT_SEC)))
    _fatal("查看器启动超时", msg, pid=pid, port_owner="none")
    return EXIT_HEALTH_TIMEOUT


def _confirm_continue(warn: str) -> bool:
    """版本不符时是否继续打开浏览器。

    默认**不问**：前端自己每 60 秒比对一次 /api/version，不一致会在页面顶部弹
    "点一下刷新"的提示条（见 viewer/server.py::api_version 的文档）；而代码指纹只要
    改动一个字节就变，每次都拦一下用户属于过度打扰。
    需要人工确认的场景（打包分发、给不懂技术的人用）可设环境变量
    VIEWER_LAUNCHER_ASK_ON_MISMATCH=1 打开 Yes/No 询问。
    """
    if os.environ.get("VIEWER_LAUNCHER_ASK_ON_MISMATCH", "") != "1" or os.name != "nt":
        return True
    try:
        ret = ctypes.windll.user32.MessageBoxW(
            None, warn + "\n\n是否仍然打开查看器？", "查看器是旧版本",
            0x00000004 | 0x00000010)   # MB_YESNO | MB_ICONERROR
        return ret == 6                   # IDYES
    except Exception:  # noqa: BLE001
        return True


# ══════════════════════════════════════════════════════════════════════════
# 主流程
# ══════════════════════════════════════════════════════════════════════════
def _launch() -> int:
    """已持有单实例锁之后的完整启动流程。返回退出码。"""
    _stage("spawn", port_owner=None, note="启动器已加锁，开始探测 8765")

    # ① 端口上已有「本查看器」→ 复用；有别人 → 直接报错，不打开浏览器
    info = _probe_health(HEALTH_TIMEOUT)
    if info is not None and not _looks_like_viewer(info):
        return _fail_foreign(info)
    if info is None and _port_open():
        # 有人在监听，但 /api/version 一秒内给不出 JSON。先别下结论：本查看器被 GIL 卡住时
        # 也会这样（见 _probe_health_slow）。多给几次机会再判。
        info = _probe_health_slow()
        if info is not None and not _looks_like_viewer(info):
            return _fail_foreign(info)
        if info is None:
            return _fail_foreign(None)

    spawned = None
    if info is None:
        _stage("spawn", port_owner="none", note="8765 无服务，拉起后端")
        try:
            spawned = _spawn_server()
        except OSError as e:
            return _fail_spawn(e)
        _stage("spawn", pid=spawned.pid, port_owner="none",
               note="后端已拉起，开始等待健康校验")
        _stage("wait", pid=spawned.pid, port_owner="none",
               note="最多等 %ss /api/version" % int(SERVICE_WAIT_SEC))
        info, foreign = _wait_health(_T0 + SERVICE_WAIT_SEC)
        if info is None:
            if foreign is not None:
                # 我们拉起的后端没占上端口，端口被别的程序拿走了
                return _fail_foreign(foreign)
            return _fail_timeout(spawned.pid)
        pid = info.get("pid") or spawned.pid
    else:
        pid = info.get("pid")
        _stage("health", pid=pid, port_owner="viewer",
               build_id=info.get("build_id"), note="8765 上已有查看器后端，复用")

    # ② 健康校验通过：核对代码指纹
    remote_build = str(info.get("build_id") or "")
    local_build = _local_build_id()
    # 磁盘指纹算不出来时记 None（未知），**不**记 False —— 不能把"算不出来"说成"不一致"，
    # 那会让状态文件天天报旧版本、把真正的旧版本淹掉。
    build_match = (local_build == remote_build) if local_build else None
    warn = None
    if build_match is False:
        # 语义是"服务端跑的是旧代码"，**不是**启动失败：接口可能少了新路由，
        # 但页面现在是能用的。给定论 + 明确动作，比判死有用。
        warn = ("服务端正在跑旧版本代码（运行中 build=%s，磁盘 build=%s），"
                "新改的功能要重启查看器才生效" % (remote_build, local_build))
    _stage("health", pid=pid, port_owner="viewer", build_id=remote_build,
           build_match=build_match, warn=warn,
           note="已通过 /api/version 健康校验（version=%s）" % info.get("version"))

    if warn and not _confirm_continue(warn):
        _stage("fail", pid=pid, port_owner="viewer", build_id=remote_build,
               build_match=build_match, warn=warn, error="版本不符，用户选择不重启")
        return EXIT_VERSION_MISMATCH

    # ③ 到这里才算成功：打开浏览器（旧版是"等 20 秒就开"，这正是假成功的来源）
    opened = False
    try:
        opened = bool(webbrowser.open(_page_url()))
    except Exception:  # noqa: BLE001
        opened = False
    _stage("ok", pid=pid, port_owner="viewer", build_id=remote_build,
           build_match=build_match, warn=warn,
           note=("已打开浏览器 %s" % _page_url()) if opened
                else "服务健康，但没能自动打开浏览器")
    if not opened:
        # 服务是好的，只是没法自动开浏览器：给用户一个能点的地址，别让人以为又失败了
        _notify("查看器已就绪", "服务已通过健康校验，但没能自动打开浏览器。\n\n"
                               "请手动访问：%s" % _page_url(), icon=0x00000040)
    return EXIT_OK


def main() -> int:
    """启动器入口；返回退出码（见模块头部约定）。"""
    global _T0
    _T0 = time.time()

    lock_fh, held = _acquire_lock()
    if not held:
        holder = _read_lock_info()
        # 用户第二次双击时，他要的是"前台那个"，不是"再来一个标签页"
        _say("[启动器] 已有启动器在运行（pid=%s，%s 启动），本次不重复打开浏览器。"
             % (holder.get("pid", "?"), holder.get("ts", "?")))
        _say("[启动器] 上一次的启动状态：%s" % _status_summary())
        # 故意不写状态文件：此刻的"权威状态"属于正在跑的那个启动器，
        # 后来者覆盖它会把真正的失败现场擦掉。
        return EXIT_OK

    atexit.register(_release_lock, lock_fh)   # 兜底：异常退出也要放锁
    try:
        return _launch()
    except Exception as e:  # noqa: BLE001
        # 兜底：pythonw 下未捕获异常 = 进程静默消失、双击"没反应"，和旧的假成功一样不可用。
        # traceback 落进状态文件（消息框只放得下一句话），再看日志就知道该改哪。
        import traceback
        tb = traceback.format_exc()
        _fatal("查看器启动失败：未预料的错误",
               "%s: %s\n\n启动器内部出了未预料的异常（不是服务端的问题）。"
               % (type(e).__name__, e),
               detail_path=_status_path(), traceback=tb)
        return EXIT_UNEXPECTED
    finally:
        _release_lock(lock_fh)


if __name__ == "__main__":
    sys.exit(main())
