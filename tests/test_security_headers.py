# -*- coding: utf-8 -*-
"""P1-5 安全头回归：点击劫持防护必须**走响应头**，令牌 Cookie 必须 HttpOnly/Strict。

## 为什么需要这个文件（今天的 bug 形状）

`viewer/static/index.html` 的 `<meta http-equiv="Content-Security-Policy">` 里一直写着
`frame-ancestors 'none'`。**按 CSP 规范，这条指令在 <meta> 里被浏览器忽略**
（Chrome 控制台原文：*The Content Security Policy directive 'frame-ancestors' is ignored
when delivered via a <meta> element*）。于是它是一条**看起来有、实际没有**的防线 ——
比不写更危险，因为读代码的人会以为已经防住了。

这类缺陷的特点是**改坏了也不报错**：头写错、写漏、或者被后来的人"顺手删掉"，
页面照样打得开，只有真的被别的站点套 iframe 时才发现没防住。
所以这里用最直接的方式钉死：起一个真 HTTP 服务，**读真实响应头**。

## 一个反直觉的口径：是 'self' / SAMEORIGIN，不是 'none' / DENY

本应用自己会内嵌**同源** iframe（`viewer/static/app.js:14674`
`<iframe src="/static/fund_masters/index.html">`，基金经理策略面板）。
`frame-ancestors 'none'` 与 `X-Frame-Options: DENY` 连**同源**嵌套一起禁，
后果是该面板静默白屏。威胁模型是"外部站点把本页面套进 iframe 做点击劫持"，
`'self'` 已完整覆盖（外部站点的 origin 不是本服务，一样被拒）。

所以本文件里有两条**方向相反**的护栏，缺一不可：
  · 头必须存在（否则等于没防）；
  · 头不许被收紧成 'none' / DENY（否则基金经理面板白屏）。
"""

from __future__ import annotations

import logging
import sys
import threading
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _detach_prod_log_handlers() -> list:
    """摘掉根 logger 的 FileHandler（导入 viewer.server 会挂一个写生产日志的）。"""
    root = logging.getLogger()
    removed = [h for h in list(root.handlers) if isinstance(h, logging.FileHandler)]
    for h in removed:
        root.removeHandler(h)
    return removed


from viewer import server as S  # noqa: E402

_DETACHED = _detach_prod_log_handlers()


@pytest.fixture()
def base_url(monkeypatch):
    """起一个**真** HTTP 服务（临时端口），返回 `http://127.0.0.1:<port>`。

    为什么不直接调 `Handler._send_file`：那只能证明"我们调了 send_header"，
    证明不了"浏览器真的会收到这两条头"。请求/响应是这一段唯一可信的验收面。
    只绑 127.0.0.1 的临时端口（port=0 由系统分配），不碰生产 8765。
    """
    monkeypatch.setattr(S.Handler, "log_message", lambda *a, **k: None, raising=False)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), S.Handler)
    httpd.daemon_threads = True
    th = threading.Thread(target=httpd.serve_forever, name="hdr-probe", daemon=True)
    th.start()
    try:
        yield "http://127.0.0.1:%d" % httpd.server_address[1]
    finally:
        httpd.shutdown()
        httpd.server_close()
        th.join(timeout=5)


def _head(url: str) -> dict:
    """发 GET，返回 (status, headers, body)。用 GET 而非 HEAD：

    HEAD 在 BaseHTTPRequestHandler 里由 `do_HEAD` 处理，而本项目的 `do_HEAD`
    与 `do_GET` 是两套代码；用 GET 验的才是**浏览器真实走的**那条路。
    """
    req = urllib.request.Request(url, method="GET")
    with urllib.request.urlopen(req, timeout=10) as r:
        return r.status, {k.lower(): v for k, v in r.headers.items()}, r.read()


# ---------------------------------------------------------------- 1. 头必须存在
def test_index_html_carries_frame_headers(base_url):
    """`/` 必须带 CSP frame-ancestors 与 X-Frame-Options —— 这是真正生效的那一处。"""
    st, h, body = _head(base_url + "/")
    assert st == 200
    assert b"<!DOCTYPE html" in body[:200], "首页返回的不是 index.html"
    assert h.get("x-frame-options", "").upper() == "SAMEORIGIN", (
        "缺 X-Frame-Options（老浏览器兜底）或取值不对：%r" % (h.get("x-frame-options"),))
    csp = h.get("content-security-policy", "")
    assert "frame-ancestors" in csp, (
        "响应头里没有 frame-ancestors —— meta 里那条是**无效**的，等于完全没防点击劫持")
    assert "'self'" in csp, "frame-ancestors 必须是 'self'（见模块 docstring）"


def test_index_html_does_not_copy_whole_meta_csp(base_url):
    """护栏：只许补 frame-ancestors 一条，不许把 meta 那一整串搬到响应头。

    为什么：响应头与 meta 同时存在时浏览器取**交集**。搬过去一旦漏了
    `'unsafe-inline'`（本应用内联脚本/onclick 极多），整站静默白屏。
    """
    _st, h, _b = _head(base_url + "/")
    csp = h.get("content-security-policy", "")
    assert "default-src" not in csp, (
        "响应头里出现了 default-src —— 有人把 meta 的整串 CSP 搬过来了，"
        "会与 meta 取交集，漏项即白屏")
    assert "script-src" not in csp


def test_api_json_gets_no_csp(base_url):
    """边界：API 的 JSON 响应不加 CSP（加了没有防护意义，只会误导排查）。"""
    st, h, _b = _head(base_url + "/api/version")
    assert st == 200
    assert "content-security-policy" not in h, "JSON 响应不该带 CSP"


def test_static_js_gets_xfo_but_not_csp(base_url):
    """边界：静态 JS 发 X-Frame-Options（无害）但不发 CSP（无意义）。"""
    st, h, _b = _head(base_url + "/static/app.js")
    assert st == 200
    assert h.get("x-frame-options", "").upper() == "SAMEORIGIN"
    assert "content-security-policy" not in h


# ---------------------------------------------------------------- 2. 不许收紧成 none/DENY
def test_nested_same_origin_page_allows_self_framing(base_url):
    """护栏（方向相反）：被本页同源内嵌的那个页面，也必须允许 'self' 嵌套。

    改了这条 = 基金经理策略面板变空白（2026-11-01 实测过：'none' 下 iframe
    直接 Refused to frame，面板一片白，控制台只有一行提示）。
    """
    st, h, _b = _head(base_url + "/static/fund_masters/index.html")
    assert st == 200
    csp = h.get("content-security-policy", "")
    assert "frame-ancestors 'none'" not in csp, (
        "frame-ancestors 'none' 会连**同源**嵌套一起禁掉 → "
        "/static/fund_masters/index.html 在基金经理面板里白屏")
    assert h.get("x-frame-options", "").upper() != "DENY", (
        "X-Frame-Options: DENY 同样会禁掉同源嵌套 → 基金经理面板白屏")


# ---------------------------------------------------------------- 3. Cookie 属性
def test_token_cookie_is_httponly_and_strict(base_url, monkeypatch):
    """令牌 Cookie 必须 `HttpOnly; SameSite=Strict`。

    取证（加 HttpOnly 的前提）：全仓 `grep -rn "local_token|document.cookie"
    viewer/static/` **0 命中** —— 前端从不读它，只有后端 `_token_ok()` 校验。
    若将来前端要读，本用例不会拦你，但请先把读取点改成后端注入
    （`<meta name="token">` 或首屏 JSON），再删掉 HttpOnly —— 顺序反了就是"功能被改坏"。
    """
    monkeypatch.setattr(S, "_LOCAL_TOKEN", "probe-token-xyz")
    _st, h, _b = _head(base_url + "/")
    ck = h.get("set-cookie", "")
    assert "local_token=probe-token-xyz" in ck, "启用令牌后首页应下发 local_token：%r" % (ck,)
    low = ck.lower()
    assert "httponly" in low, "缺 HttpOnly —— XSS 能直接读走写接口令牌"
    assert "samesite=strict" in low, "SameSite 必须是 Strict（本地单页应用无跨站需求）"
    assert "samesite=lax" not in low
    assert "path=/" in low


def test_no_cookie_when_token_disabled(base_url, monkeypatch):
    """未启用令牌时不许凭空下发 Cookie（保持既有行为）。"""
    monkeypatch.setattr(S, "_LOCAL_TOKEN", "")
    _st, h, _b = _head(base_url + "/")
    assert "set-cookie" not in h


# ---------------------------------------------------------------- 5. HEAD（curl -I 的前提）
def test_head_returns_same_security_headers(base_url):
    """HEAD 必须与 GET 同头，且不带 body。

    此前本服务对 HEAD 一律回 **501**（`BaseHTTPRequestHandler` 无默认实现），
    于是 `curl -I` 只看得到一句 Unsupported method，看不到任何安全头 ——
    "改了但验不到"，是这一轮最容易漏掉的一环。补 `do_HEAD` 后这条才能成立。
    """
    req = urllib.request.Request(base_url + "/", method="HEAD")
    with urllib.request.urlopen(req, timeout=10) as r:
        assert r.status == 200, "HEAD 仍不通（应为 200，不是 501）"
        h = {k.lower(): v for k, v in r.headers.items()}
        assert h.get("content-security-policy", "") == "frame-ancestors 'self'"
        assert h.get("x-frame-options", "").upper() == "SAMEORIGIN"
        assert int(h.get("content-length", "0")) > 1000, "HEAD 应保留 GET 的 Content-Length"
        assert r.read() == b"", "HEAD 不允许带 body"


# ---------------------------------------------------------------- 6. meta 那条要留着
def test_meta_keeps_frame_ancestors_with_explaining_comment():
    """meta 里那条 `frame-ancestors` **保留**，且旁边必须有"它在 meta 中无效"的注释。

    为什么不删：删掉会让下一个读代码的人以为"防点击劫持没人管"；
    留着 + 注释能让人顺着找到真正生效的响应头（`Handler._send_file`）。
    """
    html = (ROOT / "viewer" / "static" / "index.html").read_text(encoding="utf-8")
    assert "frame-ancestors 'none'" in html, (
        "meta 里的 frame-ancestors 被删了 —— 请保留并在旁边写明它无效，"
        "真正生效的是响应头")
    assert "is ignored" in html or "被忽略" in html or "不认" in html, (
        "meta 旁边缺少「这条在 meta 中无效」的说明注释 —— 下一个人会以为它生效")
