"""用本机 Chromium（puppeteer-core）取雪球页面的「渲染后 HTML」。

为什么需要它：雪球首页/个股页被阿里云 WAF 的 JS 挑战页拦截，裸 requests 拿不到真实内容；
只有带 JS 引擎的真实浏览器能过 WAF（通过后会下发匿名 xq_a_token）。本模块把「启动浏览器 →
预热首页拿匿名 token → 打开目标 URL → 返回渲染后 HTML」封装成一次同步调用，供 ingest 在抓取
雪球时使用（普通站点仍走 RateLimitedSession，不受影响）。

失败处理：浏览器不可用 / 页面被 WAF 拦截时返回 status_code=403、text 为 WAF 页或空，
由上层 parser 的 _is_blocked 优雅判定为 blocked（不报错、不入库）。

2026-09-25 优化（针对「雪球打开过慢 / 代理没开时整轮卡死」）：
1. 常驻浏览器服务：一个 Node 长进程持有单个 Chromium 实例，多 URL 复用，省去每次新开/
   关闭浏览器的开销（原先 5 个 URL = 5 次启动，串行约 4~5 分钟）。可用环境变量
   XQ_PERSISTENT_BROWSER=0 关闭回到一次性子进程模式。
2. 代理存活预检：抓取前先 TCP 探测代理（默认 127.0.0.1:53269）。代理不可达时立即返回
   403（PROXY_UNREACHABLE），不再傻等 120s 超时导致整轮挂起。
3. 等待策略：目标页用 domcontentloaded + 显式等待帖子节点（article.timeline__item 等）
   出现即返回，替代原先的 networkidle2（等整页所有图片/广告）+ 固定 6s 空等。
"""
from __future__ import annotations

import atexit
import json
import logging
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from types import SimpleNamespace

logger = logging.getLogger(__name__)

# 2026-11-01 P1-5：雪球 cookie 改走 `viewer.secrets_dpapi`（DPAPI 密文优先，明文回退）。
# 为什么用 try/except 而不是裸 import：本模块在 `scraper/` 下，可能被
#   (a) 工程根入口以 `scraper.browser_fetch` 导入（此时工程根在 sys.path 上），
#   (b) 也可能被 `python scraper/browser_fetch.py` 直接跑（此时 sys.path[0] 是 `scraper/`，
#       **工程根不在**）。裸 `from viewer import ...` 在 (b) 下会 ImportError。
#   Cookie 是渲染抓取的硬依赖，**绝不能让加密改造的 import 失败把整条采集带崩** ——
#   导入失败就 `_secrets = None`，退回改造前的明文直读，行为完全不变。
try:
    _PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if _PROJECT_ROOT not in sys.path:
        sys.path.insert(0, _PROJECT_ROOT)
    from viewer import secrets_dpapi as _secrets
except Exception:  # noqa: BLE001
    _secrets = None

# —— 本机已验证可用路径（可用环境变量覆盖）——
NODE_EXE = os.environ.get("XQ_NODE_EXE") or (
    r"C:\Users\野比&大熊\.workbuddy\binaries\node\versions\22.22.2-3\node.exe"
)
CHROME_EXE = os.environ.get("XQ_CHROME_EXE") or (
    r"C:\Users\野比&大熊\AppData\Local\ms-playwright\chromium-1234\chrome-win64\chrome.exe"
)
NODE_PATH = os.environ.get("XQ_NODE_PATH") or (
    r"C:\Users\野比&大熊\.workbuddy\binaries\node\workspace\node_modules"
)
PROXY = os.environ.get("XQ_PROXY") or "http://127.0.0.1:53269"
# 代理不可达时降级为直连（本机可直连外网，无需代理；默认 True，探测失败置 False）
_USE_PROXY = os.environ.get("XQ_USE_PROXY", "1") != "0"

# 2026-10-25（进哥反馈「黑框又出来了」）：本模块的父进程是 pythonw（无控制台），
# 拉起 node 子进程时不加 CREATE_NO_WINDOW，Windows 会为它新建一个控制台窗口；
# 而 stdout/stderr 都已指向 DEVNULL，于是窗口里什么都不显示 —— 就是那个「全黑空框」。
# 常驻浏览器服务到 BS_MAX_AGE 重启时还会再弹一次，所以现象是「关了又出来」。
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)   # 非 Windows 平台无此常量 → 0

# 2026-10-05（报告 P2 / 建议5）代理降噪：
# 旧行为是每次抓取都 TCP 探一次代理，探测失败就 warning 一条，13:10-13:13 三分钟内刷了 20 条，
# 把真实错误全淹了。改成「连续 N 次失败自动停用 + 合并告警 + 指数退避重试」：
#   停用后不再每轮探测（省时间），只在退避窗口到点时悄悄探一次；
#   探测恢复自动重新启用；用户显式改配置（XQ_PROXY 变了）则立即复位。
_PROXY_AUTO_DISABLE_AFTER = int(os.environ.get("XQ_PROXY_FAIL_LIMIT", "2"))
_PROXY_BASE_BACKOFF = 600.0      # 首轮退避 10 分钟
_PROXY_MAX_BACKOFF = 3600.0      # 退避上限 1 小时
_PROXY_STATE = {
    "fail_streak": 0,
    "disabled": False,
    "next_retry_at": 0.0,
    "retry_count": 0,
    "disabled_at": 0.0,
    "notified": False,
    "proxy_env": PROXY,           # 用来识别「用户改了配置」并立即复位
}
_PROXY_LOCK = threading.Lock()

# 常驻浏览器服务端口（每次抓取在此端口上复用同一 Chromium 实例）
BS_PORT = int(os.environ.get("XQ_BROWSER_PORT", "8799"))
# 服务最长寿命（秒）：到点自动重启，避免 Chromium 长期运行的内存/会话泄漏
BS_MAX_AGE = float(os.environ.get("XQ_BROWSER_MAX_AGE", "1800"))
# 是否启用常驻浏览器（默认开；设为 0 回到一次性子进程模式）
USE_PERSISTENT = os.environ.get("XQ_PERSISTENT_BROWSER", "1") != "0"

# —— 一次性子进程脚本（兜底 / 关闭常驻时启用）——
_NODE_ONESHOT = r"""
const puppeteer = require('puppeteer-core');
(async () => {
  const url = process.argv[2];
  let browser;
  try {
    browser = await puppeteer.launch({
      executablePath: process.env.XQ_CHROME_EXE,
      headless: 'new',
      args: ['--no-sandbox','--disable-setuid-sandbox','--disable-dev-shm-usage',
             '--ignore-certificate-errors','--disable-blink-features=AutomationControlled','--window-size=1920,1080'].concat(process.env.XQ_PROXY ? ['--proxy-server=' + process.env.XQ_PROXY] : []),
    });
    const page = await browser.newPage();
    await page.setUserAgent('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36');
    try { await page.setViewport({ width: 1920, height: 1080 }); } catch (e) {}
    // 0) 注入雪球登录 cookie（若提供），否则维持匿名
    try {
      const cookies = (function(){ try { if (process.env.XQ_COOKIES_PATH) return JSON.parse(require('fs').readFileSync(process.env.XQ_COOKIES_PATH,'utf8')); } catch(e){} try { if (process.env.XQ_COOKIES_JSON) return JSON.parse(process.env.XQ_COOKIES_JSON); } catch(e){} return []; })();  // P2-23
      if (cookies && cookies.length) await page.setCookie(...cookies);
    } catch (e) { /* cookie 注入失败不致命 */ }
    // 1) 预热首页，过 WAF 拿匿名 token
    try {
      await page.goto('https://xueqiu.com/', { waitUntil: 'networkidle2', timeout: 25000 });
      await new Promise(r => setTimeout(r, 3000));
    } catch (e) { /* 预热失败不致命 */ }
    // 2) 打开目标页：domcontentloaded 比 networkidle2 快得多（不必等所有图片/广告）
    await page.goto(url, { waitUntil: 'domcontentloaded', timeout: 30000 });
    // 3) 显式等帖子节点出现即返回，避免固定 6s 空等
    try {
      await page.waitForSelector('article.timeline__item, .timeline, .status-content', { timeout: 12000 });
    } catch (e) { /* 没等到也不致命，照样取当前 HTML */ }
    const html = await page.content();
    process.stdout.write(html);
  } catch (e) {
    process.stderr.write('BROWSER_ERR ' + e.message);
    process.exit(2);
  } finally {
    if (browser) await browser.close();
  }
})();
"""

# —— 常驻浏览器服务脚本（HTTP 服务，复用单个 Chromium 实例）——
_NODE_SERVER = r"""
const http = require('http');
const puppeteer = require('puppeteer-core');
const PORT = process.env.XQ_BROWSER_PORT;
let browser = null;
let warmed = false;
async function getBrowser() {
  if (!browser) {
    browser = await puppeteer.launch({
      executablePath: process.env.XQ_CHROME_EXE,
      headless: 'new',
      args: ['--no-sandbox','--disable-setuid-sandbox','--disable-dev-shm-usage',
             '--ignore-certificate-errors','--disable-blink-features=AutomationControlled','--window-size=1920,1080'].concat(process.env.XQ_PROXY ? ['--proxy-server=' + process.env.XQ_PROXY] : []),
    });
  }
  return browser;
}
const server = http.createServer(async (req, res) => {
  if (req.url === '/health') { res.writeHead(200); res.end('OK'); return; }
  if (req.method !== 'POST') { res.writeHead(405); res.end(); return; }
  let body = '';
  req.on('data', c => body += c);
  req.on('end', async () => {
    let url;
    try { url = JSON.parse(body).url; } catch (e) { res.writeHead(400); res.end('BAD_JSON'); return; }
    let page;
    try {
      const br = await getBrowser();
      page = await br.newPage();
      try { await page.setUserAgent('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36'); } catch (e) {}
      try { await page.setViewport({ width: 1920, height: 1080 }); } catch (e) {}
      // 0) 注入雪球登录 cookie（若提供）
      try {
        const cookies = (function(){ try { if (process.env.XQ_COOKIES_PATH) return JSON.parse(require('fs').readFileSync(process.env.XQ_COOKIES_PATH,'utf8')); } catch(e){} try { if (process.env.XQ_COOKIES_JSON) return JSON.parse(process.env.XQ_COOKIES_JSON); } catch(e){} return []; })();  // P2-23
        if (cookies && cookies.length) await page.setCookie(...cookies);
      } catch (e) { /* 注入失败不致命 */ }
      // 首请求做一次预热（拿匿名 token），之后复用浏览器上下文里的 cookie
      if (!warmed) {
        try {
          await page.goto('https://xueqiu.com/', { waitUntil: 'networkidle2', timeout: 25000 });
          await new Promise(r => setTimeout(r, 3000));
        } catch (e) { /* 预热失败不致命 */ }
        warmed = true;
      }
      await page.goto(url, { waitUntil: 'domcontentloaded', timeout: 30000 });
      try {
        await page.waitForSelector('article.timeline__item, .timeline, .status-content', { timeout: 12000 });
      } catch (e) { /* 没等到也不致命 */ }
      const html = await page.content();
      res.writeHead(200, {'Content-Type': 'text/html; charset=utf-8'});
      res.end(html);
    } catch (e) {
      res.writeHead(500); res.end('BROWSER_ERR ' + e.message);
    } finally {
      if (page) { try { await page.close(); } catch (e) {} }
    }
  });
});
server.listen(PORT, '127.0.0.1', () => { console.error('XQ_BROWSER_SERVER_UP:' + PORT); });
"""

def _load_cookies() -> list:
    """读取雪球 cookie 文件（Netscape .txt 或 JSON .json），返回 puppeteer page.setCookie 可用的列表。

    查找顺序（2026-11-01 P1-5 起）：环境变量 XQ_COOKIE_FILE
      → 受管凭据 `cookies.txt`（**DPAPI 密文 `cookies.txt.dpapi` 优先，明文回退**）
      → 工程根 cookies.json → 相对路径 cookies.txt / cookies.json。
    第 3 档是改造前的老路径，刻意原样保留（cookie 也可能只存在 cookies.json）。
    2026-09-25：雪球接口要求登录态（search.json 返回 error_code 400016），
    匿名 token 已不够，必须注入登录 cookie 才能拿到数据。
    """
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # 工程根
    text = None
    path = None
    # ① 环境变量显式指定 → 尊重它，不参与"受管凭据"（用户已经明确给了路径，就按他的来）
    envf = os.environ.get("XQ_COOKIE_FILE")
    if envf and os.path.exists(envf):
        try:
            with open(envf, encoding="utf-8", errors="ignore") as _fh:
                text = _fh.read()
            path = envf
        except OSError:
            text = None
    # ② 受管凭据（P1-5）：`secrets_dpapi.load` 优先读 `<工程根>/cookies.txt.dpapi`，
    #    缺失/解不开时回退 `<工程根>/cookies.txt` 明文（并打一条**限频** WARNING 提醒迁移）。
    #    这一步刻意排在 `cookies.json` **之前** —— 沿用原查找顺序
    #    （cookies.txt 优先于 cookies.json），不改变"哪一份 cookie 生效"。
    if text is None and _secrets is not None:
        managed = _secrets.load("cookies.txt")
        if managed is not None:
            text = managed
            path = "cookies.txt（受管：DPAPI 密文优先，明文回退）"
    # ③ 原兜底路径**原样保留**：工程根 cookies.json / 相对路径 cookies.txt、cookies.json。
    #    为什么不能删：改造前 cookie 也可能只存在于 cookies.json，砍掉这一档等于砍功能。
    if text is None:
        for c in (os.path.join(root, "cookies.json"), "cookies.txt", "cookies.json"):
            if not c or not os.path.exists(c):
                continue
            try:
                with open(c, encoding="utf-8", errors="ignore") as _fh:
                    text = _fh.read()
                path = c
                break
            except OSError:
                continue
    if text is None:
        return []
    out = []
    stripped = text.strip()
    if stripped.startswith("{") or stripped.startswith("["):
        try:
            data = json.loads(stripped)
            arr = data if isinstance(data, list) else (data.get("cookies") or [])
            for c in arr:
                if isinstance(c, dict) and c.get("name") and c.get("value"):
                    out.append({
                        "name": str(c["name"]), "value": str(c["value"]),
                        "domain": c.get("domain") or ".xueqiu.com",
                        "path": c.get("path") or "/",
                    })
        except Exception:  # noqa: BLE001
            pass
    else:
        # Netscape HTTP Cookie File 格式：domain \t flag \t path \t secure \t expires \t name \t value
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split("\t")
            if len(parts) >= 7:
                domain, _flag, cpath, _secure, _exp, name, value = parts[:7]
                if name and value:
                    out.append({"name": name, "value": value,
                                "domain": domain or ".xueqiu.com",
                                "path": cpath or "/"})
    if out:
        logger.info("已加载雪球 cookie %d 条（来自 %s）", len(out), path)
    else:
        logger.warning("未找到可用的雪球 cookie 文件（%s），将以匿名访问", path)
    return out


_BS_LOCK = threading.Lock()
_bs_proc = None
_bs_started_at = 0.0


def _proxy_host_port() -> tuple[str, int]:
    """从 PROXY（如 http://user:pass@127.0.0.1:53269）解析出 host/port。"""
    p = PROXY.split("://", 1)[-1]
    if "@" in p:  # user:pass@host:port
        p = p.split("@", 1)[1]
    if ":" in p:
        h, pp = p.rsplit(":", 1)
        return h, int(pp)
    return p, 80


def _proxy_reachable(timeout: float = 3.0) -> bool:
    """原始 TCP 探测（不参与任何状态判断），用于 _proxy_check 内部与单测。"""
    h, p = _proxy_host_port()
    try:
        with socket.create_connection((h, p), timeout=timeout):
            return True
    except OSError:
        return False


def proxy_status() -> dict:
    """给上层/日志用的只读状态（是否停用、何时重试、退避时长）。"""
    with _PROXY_LOCK:
        left = max(0.0, _PROXY_STATE["next_retry_at"] - time.time())
        return {
            "proxy": _PROXY_STATE["proxy_env"],
            "disabled": bool(_PROXY_STATE["disabled"]),
            "fail_streak": int(_PROXY_STATE["fail_streak"]),
            "retry_count": int(_PROXY_STATE["retry_count"]),
            "disabled_at": _PROXY_STATE["disabled_at"] or None,
            "next_retry_in": int(left),
            "auto_disable_after": _PROXY_AUTO_DISABLE_AFTER,
        }


def _proxy_reset_locked() -> None:
    """代理恢复/配置变更后复位计数器（调用方须持锁）。"""
    _PROXY_STATE.update({"fail_streak": 0, "disabled": False, "next_retry_at": 0.0,
                         "retry_count": 0, "disabled_at": 0.0, "notified": False})
    globals()["_USE_PROXY"] = True


def _proxy_check(timeout: float = 3.0) -> bool:
    """带自动停用的代理探活（报告 建议5）。

    返回 True 表示本次可以走代理；False 表示应降级直连（此时 _USE_PROXY 已置 False）。
    关键点：**停用后不再每轮刷 warning**，只在退避窗口到点静默探一次。
    """
    global _USE_PROXY
    st = _PROXY_STATE
    now = time.time()

    # 用户改了 XQ_PROXY → 视为显式重新配置，立即复位（不沿用旧的停用状态）
    if st["proxy_env"] != PROXY:
        st["proxy_env"] = PROXY
        with _PROXY_LOCK:
            _proxy_reset_locked()
        logger.info("雪球代理配置变更为 %s，已复位探测状态", PROXY)

    with _PROXY_LOCK:
        if st["disabled"]:
            if now < st["next_retry_at"]:
                _USE_PROXY = False
                return False
            # 退避窗口到点：悄悄探一次（不打日志，避免噪声）
            if _proxy_reachable(timeout):
                _proxy_reset_locked()
                logger.info("雪球代理 %s 已恢复，重新启用代理模式", PROXY)
            else:
                st["retry_count"] += 1
                back = min(_PROXY_MAX_BACKOFF, _PROXY_BASE_BACKOFF * (2 ** min(st["fail_streak"], 4)))
                st["next_retry_at"] = now + back
                _USE_PROXY = False
                return False

        if _proxy_reachable(timeout):
            _proxy_reset_locked()
            return True

        # 探测失败：累加失败次数
        st["fail_streak"] += 1
        if st["fail_streak"] >= _PROXY_AUTO_DISABLE_AFTER:
            if not st["disabled"]:
                st["disabled"] = True
                st["disabled_at"] = now
                st["next_retry_at"] = now + _PROXY_BASE_BACKOFF
                _USE_PROXY = False
                # 全程只出这一条合并告警（之前是每轮一条，20 条刷屏）
                logger.warning(
                    "雪球代理 %s 连续 %d 次探测失败，已自动停用并降级为直连"
                    "（之后不再重复告警，最早 %d 分钟后重试第 %d 次）；"
                    "代理恢复后会自动重新启用，或清空环境变量 XQ_PROXY 立即直连。",
                    PROXY, st["fail_streak"], int(_PROXY_BASE_BACKOFF // 60), st["retry_count"] + 1)
            else:
                st["next_retry_at"] = now + min(_PROXY_MAX_BACKOFF,
                                                _PROXY_BASE_BACKOFF * (2 ** min(st["fail_streak"], 4)))
                _USE_PROXY = False
        else:
            # 还没到停用阈值：一条 info 就够，不上 warning
            logger.info("雪球代理 %s 探测失败（第 %d/%d 次），继续尝试",
                        PROXY, st["fail_streak"], _PROXY_AUTO_DISABLE_AFTER)
            _USE_PROXY = False
        return False


def _ensure_browser_server() -> bool:
    """启动（或复用）常驻浏览器服务。返回 True 表示服务可用。"""
    global _bs_proc, _bs_started_at
    with _BS_LOCK:
        now = time.time()
        if _bs_proc is not None and _bs_proc.poll() is None and (now - _bs_started_at) < BS_MAX_AGE:
            return True
        # 需要（重）启动
        if _bs_proc is not None:
            try:
                _bs_proc.kill()
                _bs_proc.wait(timeout=5)
            except Exception:  # noqa: BLE001
                pass
        fd, sp = tempfile.mkstemp(suffix=".cjs", prefix="_xq_bs_")
        os.close(fd)
        Path(sp).write_text(_NODE_SERVER, encoding="utf-8")
        env = dict(os.environ)
        env["XQ_CHROME_EXE"] = CHROME_EXE
        env["XQ_PROXY"] = PROXY if _USE_PROXY else ""
        env["NODE_PATH"] = NODE_PATH
        env["XQ_BROWSER_PORT"] = str(BS_PORT)
        _ck_fd, _ck_sp = tempfile.mkstemp(suffix=".json", prefix="_xq_ck_")
        os.close(_ck_fd)
        Path(_ck_sp).write_text(json.dumps(_load_cookies(), ensure_ascii=False), encoding="utf-8")  # P2-23：cookie 落临时文件，避免塞进 env（体积 / 进程列表泄漏）
        env["XQ_COOKIES_PATH"] = _ck_sp
        try:
            _bs_proc = subprocess.Popen(
                [NODE_EXE, sp], env=env,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                creationflags=_NO_WINDOW,   # 2026-10-25：不许弹控制台黑框
            )
        except Exception as e:  # noqa: BLE001
            logger.error("启动雪球浏览器服务失败: %s", e)
            return False
        _bs_started_at = now
        # 轮询 /health 直到就绪（最多 ~10s）
        up = False
        for _ in range(40):
            if _bs_proc.poll() is not None:
                logger.error("雪球浏览器服务启动即退出")
                return False
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{BS_PORT}/health", timeout=0.5):
                    up = True
                    break
            except Exception:  # noqa: BLE001
                time.sleep(0.25)
        # 服务已加载脚本，可删除临时文件
        try:
            Path(sp).unlink()
        except OSError:
            pass
        return up


def shutdown_browser_server() -> None:
    """进程退出时主动回收常驻 Chromium，避免成为孤儿进程。"""
    global _bs_proc
    with _BS_LOCK:
        if _bs_proc is not None:
            try:
                _bs_proc.kill()
            except Exception:  # noqa: BLE001
                pass
            _bs_proc = None


atexit.register(shutdown_browser_server)


def _browser_get_persistent(url: str, timeout: int) -> "SimpleNamespace | None":
    """经常驻浏览器服务取页面；任何失败返回 None（由调用方回退一次性子进程）。"""
    if not _ensure_browser_server():
        return None
    data = json.dumps({"url": url}).encode("utf-8")
    req = urllib.request.Request(
        f"http://127.0.0.1:{BS_PORT}/", data=data,
        headers={"Content-Type": "application/json"}, method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            html = resp.read().decode("utf-8", "replace")
        if html:
            return SimpleNamespace(text=html, status_code=200, url=url)
        return SimpleNamespace(text="EMPTY_RESPONSE", status_code=403, url=url)
    except urllib.error.HTTPError as e:
        logger.warning("雪球浏览器服务返回 %s", e.code)
        return None
    except Exception as e:  # noqa: BLE001
        logger.warning("雪球浏览器服务调用失败，回退一次性抓取: %s", e)
        return None


def _browser_get_oneshot(url: str, timeout: int) -> "SimpleNamespace":
    """兜底：每次新开浏览器抓一页（原始模式，已优化等待策略）。"""
    script_path = None
    try:
        fd, script_path = tempfile.mkstemp(suffix=".cjs", prefix="_xq_browser_")
        os.close(fd)
        Path(script_path).write_text(_NODE_ONESHOT, encoding="utf-8")
        env = dict(os.environ)
        env["XQ_CHROME_EXE"] = CHROME_EXE
        env["XQ_PROXY"] = PROXY if _USE_PROXY else ""
        env["NODE_PATH"] = NODE_PATH
        _ck_fd, _ck_sp = tempfile.mkstemp(suffix=".json", prefix="_xq_ck_")
        os.close(_ck_fd)
        Path(_ck_sp).write_text(json.dumps(_load_cookies(), ensure_ascii=False), encoding="utf-8")  # P2-23：cookie 落临时文件，避免塞进 env（体积 / 进程列表泄漏）
        env["XQ_COOKIES_PATH"] = _ck_sp
        proc = subprocess.run(
            [NODE_EXE, script_path, url],
            capture_output=True, timeout=timeout, env=env,
            creationflags=_NO_WINDOW,   # 2026-10-25：兜底路径同样不许弹黑框
        )
        html = proc.stdout.decode("utf-8", "replace")
        if proc.returncode == 0 and html:
            return SimpleNamespace(text=html, status_code=200, url=url)
        err = proc.stderr.decode("utf-8", "replace")
        return SimpleNamespace(text=(html or err or "BLOCKED"), status_code=403, url=url)
    except FileNotFoundError:
        return SimpleNamespace(text="NODE_NOT_FOUND", status_code=403, url=url)
    except subprocess.TimeoutExpired:
        return SimpleNamespace(text="BROWSER_TIMEOUT", status_code=403, url=url)
    except Exception as e:  # noqa: BLE001
        return SimpleNamespace(text=f"BROWSER_EXCEPTION:{e}", status_code=403, url=url)
    finally:
        if script_path and Path(script_path).exists():
            try:
                Path(script_path).unlink()
            except OSError:
                pass


def browser_get(url: str, timeout: int = 75) -> "SimpleNamespace":
    """返回类 requests.Response 的对象（.text / .status_code）。失败时 status_code=403。

    流程：先 TCP 探测代理 →（默认）常驻浏览器服务 → 失败回退一次性子进程。
    """
    host, port = _proxy_host_port()
    if not _proxy_check():
        # 不再每轮 warning（报告 建议5）；一次停用只会有 _proxy_check 里那一条合并告警。
        logger.info("雪球代理 %s:%s 不可用，本次降级为直连（浏览器不走代理）", host, port)
    if USE_PERSISTENT:
        res = _browser_get_persistent(url, timeout)
        if res is not None:
            return res
        logger.info("常驻浏览器不可用，回退一次性抓取: %s", url)
    return _browser_get_oneshot(url, timeout)


if __name__ == "__main__":
    import sys
    u = sys.argv[1] if len(sys.argv) > 1 else "https://xueqiu.com/k?q=600519"
    r = browser_get(u)
    print("status", r.status_code, "len", len(r.text))
    print(r.text[:300])
