"""一体化「开窗 + 盯梢 + 自动存 Cookie + 验证」脚本（淘股吧 / tgb.cn）。

背景：淘股吧已迁至 tgb.cn，登录方式为微信扫码/手机验证码/MFA（无密码表单），
登录态由 SSO 域 sso.tgb.cn 决定。本脚本：
  1. 用独立 profile 启动一个有头 Edge，打开 tgb.cn 首页；
  2. 后台轮询 CDP 的 Network.getAllCookies，检测「是否已登录」；
  3. 一旦检测到登录态 cookie（sso.tgb.cn 域出现会话 cookie，或 tgb.cn 域出现登录标志），
     立即抓取全部相关 cookie 并调用 credentials.save_cookies 加密存库；
  4. 存库后打印结果，退出。

判定「已登录」的可靠信号（不依赖猜测 cookie 名）：
  - 通过 CDP 访问受限页 https://www.tgb.cn/user/getMoreListAction，
    若返回的 URL 仍是 /user/getMoreListAction（未 302 到 sso.tgb.cn 登录页）即判已登录。
  这是与 sites.yaml verify.url 完全一致的判据，最可靠。
"""
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.request

EDGE = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
PORT = 9333
URL = "https://www.tgb.cn"

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 直连 127.0.0.1，绕过系统代理（用户常开 VPN，代理会拦截 localhost）
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def cdp_json(path: str):
    with _OPENER.open(f"http://127.0.0.1:{PORT}{path}", timeout=5) as r:
        return json.loads(r.read().decode("utf-8"))


def cdp_alive() -> bool:
    """探测 CDP 端口是否仍被浏览器占用（窗口被关时这里会抛连接拒绝）。"""
    try:
        cdp_json("/json/version")
        return True
    except Exception:
        return False


def wait_cdp(seconds: int = 40) -> bool:
    for _ in range(seconds):
        try:
            cdp_json("/json/version")
            return True
        except Exception:
            time.sleep(1)
    return False


def get_page_ws() -> str | None:
    """返回第一个 page 标签的 WebSocket 地址（窗口被关时返回 None，不抛异常）。"""
    try:
        tabs = cdp_json("/json/list")
    except Exception:
        return None
    for t in tabs:
        if t.get("type") == "page" and t.get("webSocketDebuggerUrl"):
            return t["webSocketDebuggerUrl"]
    return None


NODE_PATH = r"C:/Users/野比&大熊/.workbuddy/binaries/node/versions/22.22.2-3/node.exe"


def _run_cdp(js_body: str, timeout: int = 30) -> str:
    """通过 Node 22 内置 WebSocket 直连 CDP，执行一段浏览器上下文里的 JS，
    返回 stdout 原文（脚本内部用 console.log 输出结果）。"""
    if not os.path.exists(NODE_PATH):
        return ""
    ws = get_page_ws()
    if not ws:
        return ""
    node_script = (
        "const ws = new WebSocket(" + json.dumps(ws) + ");\n"
        "let id = 0; const pend = new Map();\n"
        "ws.onmessage = (e) => { const m = JSON.parse(e.data);"
        " if (m.id && pend.has(m.id)) { pend.get(m.id)(m); pend.delete(m.id); } };\n"
        "const send = (method, params={}) => new Promise(r => { const i=++id; pend.set(i,r);"
        " ws.send(JSON.stringify({id:i, method, params})); });\n"
        "ws.onopen = async () => {\n"
        + js_body +
        "\n};\n"
        "setTimeout(() => process.exit(1), " + str(timeout * 1000) + ");\n"
    )
    p = subprocess.run(
        [NODE_PATH, "-e", node_script], capture_output=True, text=True, timeout=timeout + 10
    )
    return p.stdout.strip()


def is_logged_in() -> bool:
    """用浏览器自己的会话判断是否**真正**登录成功。

    判据（双保险，都满足才算登录）：
      1. loginStatus cookie 不再是「qrcode」——扫码这一步必须已完成；
      2. sso.tgb.cn 域出现**非 acw_tc 的会话 cookie**（真正的登录态 token 落在 SSO 域）。
    之前误把「页面 302 跳转」当成已登录，导致在扫码完成前就抓了 cookie，
    抓到的是 loginStatus=qrcode 的半成品——必须改掉。
    """
    js = (
        "const r = await send('Network.getAllCookies');\n"
        "const cs = (r.result && r.result.cookies) || [];\n"
        "const lv = cs.filter(c => c.name === 'loginStatus').map(c => c.value)[0] || '';\n"
        "const sso = cs.filter(c => (c.domain||'').includes('sso.tgb.cn') "
        "&& c.name !== 'acw_tc');\n"
        "console.log(JSON.stringify({lv, ssoN: sso.length, ssoNames: sso.map(c=>c.name)}));\n"
        "ws.close(); process.exit(0);"
    )
    out = _run_cdp(js)
    if not out:
        return False
    try:
        d = json.loads(out)
    except Exception:
        return False
    lv = d.get("lv") or ""
    # 1) 扫码态必须已结束（不再是 qrcode）
    if lv == "qrcode" or lv == "":
        return False
    # 2) SSO 域必须出现真实会话 cookie
    if (d.get("ssoN") or 0) < 1:
        return False
    return True


def fetch_cookies_via_cdp() -> list[dict]:
    """通过 CDP 的 Network.getAllCookies 抓取全部 cookie（含 httpOnly）。"""
    js = (
        "const r = await send('Network.getAllCookies');\n"
        "console.log(JSON.stringify((r.result && r.result.cookies) || []));\n"
        "ws.close(); process.exit(0);"
    )
    out = _run_cdp(js)
    try:
        return json.loads(out)
    except Exception:
        return []


def main() -> int:
    # 2026-09-24：先看 9333 端口是否已有 Edge 启动（被 launch_user_edge_with_debug.py 接管过的用户 Edge）。
    # 若是，直接连过去；用户登录后 cookie 落在**用户自己的 profile**，不丢登录态、也不会创建临时空 profile。
    cdp_already = False
    try:
        if cdp_json("/json/version"):
            cdp_already = True
            print("[接管] 9333 端口已有 Edge（应来自 launch_user_edge_with_debug.py）。直接接管，不另开窗口。")
    except Exception:
        cdp_already = False

    if not cdp_already:
        if not os.path.exists(EDGE):
            print(f"[错误] 找不到 Edge：{EDGE}")
            return 1
        # 1) 启动受控窗口（保留旧路径作为兜底）
        profile = tempfile.mkdtemp(prefix="taoguba-login-")
        subprocess.Popen(
            [EDGE, f"--remote-debugging-port={PORT}", f"--user-data-dir={profile}",
             "--no-first-run", "--no-default-browser-check", URL]
        )
        print(f"[启动] Edge 窗口已打开（profile={profile}）")
        print("[提示] 请在弹出的窗口里登录淘股吧（微信扫码 / 手机验证码）。")

        if not wait_cdp():
            print("[错误] CDP 未就绪，窗口可能没打开。")
            return 2
        print("[CDP] 就绪，开始盯梢…")

    # 2) 轮询检测登录（最多等 15 分钟）
    from scraper.credentials import save_cookies  # noqa: E402

    deadline = time.time() + 15 * 60
    while time.time() < deadline:
        # 窗口被关 → CDP 端口失去监听。此时不该崩溃，而是明确告知并让用户重开。
        if not cdp_alive():
            print("[退出] 登录窗口已关闭（CDP 端口无响应）。请重新运行本脚本再登录。")
            return 5
        if is_logged_in():
            print("[检测] 已登录，正在抓取 cookie…")
            cookies = fetch_cookies_via_cdp()
            if not cookies:
                print("[错误] CDP 抓取 cookie 失败（可能窗口被关）。")
                return 3
            # 只保留 tgb.cn / taoguba 相关域的 cookie（含 sso 域）
            keep = [c for c in cookies if any(
                d in (c.get("domain") or "") for d in ("tgb.cn", "taoguba")
            )]
            if not keep:
                keep = cookies
            n = save_cookies("taoguba", keep)
            print(f"[成功] 已保存 {n} 条 cookie 到 taoguba 凭据库。")
            domains = sorted({c.get("domain") for c in keep})
            print(f"[域] {domains}")
            print("DONE")
            return 0
        time.sleep(3)

    print("[超时] 15 分钟内未检测到登录，请重试。")
    return 4


if __name__ == "__main__":
    sys.exit(main())
