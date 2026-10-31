# -*- coding: utf-8 -*-
"""静态文本资源 gzip 回归（2026-11-01，P2「首屏 JS 传输量」）。

## 为什么要这个文件

`_send_file` 里 2026-10-17 就写过 gzip，注释还写着「app.js 973KB → 压缩到约 1/4」。
**但它一次都没生效过**：判定写的是

    _is_text = ctype.startswith("text/") or ctype in ("application/javascript", "application/json")

而本服务下发的 ctype **恒带** `; charset=utf-8`（见 `_do_GET` 的 /static/ 分支），
于是 `ctype in ("application/javascript", ...)` **永远为假** —— 1.21MB 的 app.js
一直原样下发；`text/css` / `text/html` 因为走 `startswith("text/")` 才侥幸压上了。
实测：`curl -H 'Accept-Encoding: gzip' /static/app.js` → 1,214,382 字节，无 `Content-Encoding`。

这就是**"看起来有、实际没有"**的一类缺陷中最贵的那种：代码在、注释在、review 也过，
只有真去量字节才发现。而它恰好决定了验收能否达标 —— 所以这里用真 HTTP 服务钉死。

## 本文件的判据

1. 带 `Accept-Encoding: gzip` → 有 `Content-Encoding: gzip`，且
   `Content-Length` == 实际收到的字节数 == 解压后长度自洽（长度写错会让浏览器卡到超时）；
2. 不带 `Accept-Encoding` → **没有** `Content-Encoding`，长度等于原始大小；
3. `Vary: Accept-Encoding` 两种情况**都要有**（响应体随请求头变化，Vary 必须如实声明，
   否则中间缓存会把压缩版发给不支持的客户端）；
4. < 1KB 的不压（gzip 头 ~18 字节，压了白费协商）；
5. 二进制（png）不压 —— 而且是用**魔数**判的：路由把未知扩展名一律当 `text/html`，
   只靠 MIME 判会把图片当文本压；
6. 同一文件二次请求**不再压**（进程内缓存，键含 mtime_ns）；
7. 内容一改就重压（缓存自动失效）且旧版本被淘汰（否则改一次 app.js 就多占 0.27MB）。

全部在 tmp 目录里造受控文件（`STATIC_DIR` 打桩），**不碰仓库里的真实静态资源**。
"""

from __future__ import annotations

import gzip as _gzip_mod
import hashlib
import http.client
import logging
import sys
import threading
import urllib.parse
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _detach_prod_log_handlers() -> list:
    """摘掉根 logger 的 FileHandler（导入 `viewer.server` 会往生产日志写）。"""
    root = logging.getLogger()
    removed = [h for h in list(root.handlers) if isinstance(h, logging.FileHandler)]
    for h in removed:
        root.removeHandler(h)
    return removed


from viewer import server as S  # noqa: E402

_DETACHED = _detach_prod_log_handlers()

# 够大、且**高度可压**的文本（重复行 → gzip 一定能显著变小）
BIG_JS = "// filler\n" + ("var topicCard = 1; // 话题卡\n" * 800)
KIND_PNG = b"\x89PNG\r\n\x1a\n" + bytes(range(256)) * 24   # 带 PNG 魔数且几乎不可压


def _head(url: str, accept_encoding: str | None = None) -> tuple:
    """发 GET，返回 (status, headers, raw_body)。

    urllib **不会**自动解压 —— 拿到的 body 就是线缆上的字节，正是要量的东西。
    """
    headers = {"Accept-Encoding": accept_encoding} if accept_encoding else {}
    req = urllib.request.Request(url, headers=headers, method="GET")
    with urllib.request.urlopen(req, timeout=15) as r:
        return r.status, {k.lower(): v for k, v in r.headers.items()}, r.read()


def _digest(b: bytes) -> str:
    """比内容用摘要，不用 `==`。

    ⚠️ 不是为了快，是为了**失败时可读**：直接 `assert 大字符串 == 大字符串` 时，
    pytest 的断言重写会为两个 40KB 字符串构造 diff，实测直接把 difflib 打进
    RecursionError，单条用例耗时从 0.1s 涨到 28s（本文件第一版就踩了）。
    摘要不相等时人看得出来"内容不同"，也顺手把 28s 变回 0.1s。
    """
    return hashlib.sha256(b).hexdigest()


@pytest.fixture()
def static_root(monkeypatch, tmp_path):
    """受控静态目录：大 JS / 小 JS / png / css 各一份。"""
    # newline="\n"：Windows 上 write_text 默认把 \n 转成 \r\n，
    # 会让"文件字节"与"我写的字符串"对不上（第一版就是这么假红的）。
    (tmp_path / "big.js").write_text(BIG_JS, encoding="utf-8", newline="\n")
    (tmp_path / "small.js").write_text("var a = 1;\n", encoding="utf-8", newline="\n")
    (tmp_path / "pic.png").write_bytes(KIND_PNG)
    (tmp_path / "style.css").write_text("body{margin:0}\n" * 200, encoding="utf-8", newline="\n")
    # `/` 走的是 `STATIC_DIR/index.html`（HTML 才带 CSP），所以夹具里也要有一份
    (tmp_path / "index.html").write_text(
        "<!doctype html><html><head><title>t</title></head><body>" + "x" * 2000 + "</body></html>",
        encoding="utf-8", newline="\n")

    monkeypatch.setattr(S, "STATIC_DIR", tmp_path)
    S._gzip_cache_clear()
    yield tmp_path
    S._gzip_cache_clear()


@pytest.fixture()
def base_url(monkeypatch):
    """真起一个临时端口的服务（与浏览器走同一条 GET 管线）。"""
    monkeypatch.setattr(S.Handler, "log_message", lambda *a, **k: None, raising=False)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), S.Handler)
    httpd.daemon_threads = True
    th = threading.Thread(target=httpd.serve_forever, name="gz-probe", daemon=True)
    th.start()
    try:
        yield "http://127.0.0.1:%d" % httpd.server_address[1]
    finally:
        httpd.shutdown()
        httpd.server_close()
        th.join(timeout=5)


# ---------------------------------------------------------------- 0. 回归本体：charset 后缀
def test_media_type_strips_charset_suffix():
    """`application/javascript; charset=utf-8` 必须被判为可压 —— 旧实现就是这里失效的。"""
    assert S._media_type("application/javascript; charset=utf-8") == "application/javascript"
    assert S._is_compressible("application/javascript; charset=utf-8", b"x" * 4096) is True
    assert S._is_compressible("application/json; charset=utf-8", b"x" * 4096) is True
    assert S._is_compressible("text/css; charset=utf-8", b"x" * 4096) is True
    assert S._is_compressible("image/svg+xml", b"<svg>" + b"x" * 4096) is True


# ---------------------------------------------------------------- 1. 压缩 + 长度自洽
def test_gzip_when_accepted_and_length_is_consistent(base_url, static_root):
    st, h, body = _head(base_url + "/static/big.js", "gzip")
    assert st == 200
    assert h.get("content-encoding") == "gzip", "带 Accept-Encoding: gzip 却拿到未压缩内容"
    assert int(h["content-length"]) == len(body), (
        "Content-Length 与实际收到的字节数不一致：%s vs %d —— "
        "长度写错会让浏览器一直等到超时" % (h["content-length"], len(body)))
    decoded = _gzip_mod.decompress(body)
    raw = (static_root / "big.js").read_bytes()
    assert _digest(decoded) == _digest(raw), "解压后内容与原文件不一致（压缩流写坏了）"
    assert len(decoded) == len(raw) == (static_root / "big.js").stat().st_size


# ---------------------------------------------------------------- 2. 不支持 gzip 的客户端
def test_client_without_gzip_gets_original(base_url, static_root):
    st, h, body = _head(base_url + "/static/big.js", None)
    assert st == 200
    assert "content-encoding" not in h, "客户端没声明 gzip，却被发了压缩内容"
    raw = (static_root / "big.js").read_bytes()
    assert int(h["content-length"]) == len(raw) == len(body)
    assert body == raw


# ---------------------------------------------------------------- 3. Vary 两种都要有
def test_vary_is_sent_in_both_cases(base_url, static_root):
    """`Vary` 只在压缩时发是错的：未压缩那一份也会被中间缓存当成"对所有客户端有效"。"""
    _, h_gz, _ = _head(base_url + "/static/big.js", "gzip")
    _, h_raw, _ = _head(base_url + "/static/big.js", None)
    for name, h in (("压缩", h_gz), ("未压缩", h_raw)):
        assert "vary" in h, "%s 响应缺 Vary" % name
        assert "accept-encoding" in h["vary"].lower(), "%s 响应的 Vary 不含 Accept-Encoding" % name


# ---------------------------------------------------------------- 4. 小文件不压
def test_small_file_is_not_compressed(base_url, static_root):
    _, h, body = _head(base_url + "/static/small.js", "gzip")
    assert "content-encoding" not in h, "<1KB 的文件不该压（gzip 头开销大于收益）"
    assert body == (static_root / "small.js").read_bytes()


# ---------------------------------------------------------------- 5. 二进制不压（魔数判定）
def test_binary_png_is_not_compressed(base_url, static_root):
    """`/static/pic.png` 会被路由当成 text/html（只认 .css/.js），所以只靠 MIME 会误压。"""
    _, h, body = _head(base_url + "/static/pic.png", "gzip")
    assert "content-encoding" not in h, "png 被当成文本压了 —— 魔数兜底失效"
    assert body == KIND_PNG
    assert S._is_compressible("text/html; charset=utf-8", KIND_PNG) is False


# ---------------------------------------------------------------- 6/7. 缓存命中与失效
class _CountingGzip:
    """打桩 `S.gzip`：数 `compress` 被调用几次。"""

    def __init__(self):
        self.calls = 0

    def compress(self, data, level=6):
        self.calls += 1
        return _gzip_mod.compress(data, level)


def test_second_request_hits_cache_without_recompressing(base_url, static_root, monkeypatch):
    counter = _CountingGzip()
    monkeypatch.setattr(S, "gzip", counter)

    for _ in range(3):
        st, h, body = _head(base_url + "/static/big.js", "gzip")
        assert st == 200 and h.get("content-encoding") == "gzip"
        assert _digest(_gzip_mod.decompress(body)) == _digest(
            (static_root / "big.js").read_bytes())

    assert counter.calls == 1, (
        "同一文件请求 3 次应只压 1 次，实际压了 %d 次（缓存没生效 → 每次首屏白付几十毫秒）"
        % counter.calls)


def test_cache_invalidates_when_content_changes(base_url, static_root, monkeypatch):
    counter = _CountingGzip()
    monkeypatch.setattr(S, "gzip", counter)

    _, _, body1 = _head(base_url + "/static/big.js", "gzip")
    assert _digest(_gzip_mod.decompress(body1)) == _digest(
        (static_root / "big.js").read_bytes())

    # 改内容（长度也变），mtime_ns 必变 → 缓存键失效 → 必须重压
    new_text = BIG_JS + "// v2 追加内容\n" * 50
    (static_root / "big.js").write_text(new_text, encoding="utf-8", newline="\n")

    _, h2, body2 = _head(base_url + "/static/big.js", "gzip")
    assert counter.calls == 2, "内容变了却没重压 → 会把旧内容一直发给用户"
    assert _digest(_gzip_mod.decompress(body2)) == _digest(new_text.encode("utf-8"))
    assert int(h2["content-length"]) == len(body2)

    # 旧版本必须被淘汰，否则改一次 app.js 就多占 0.27MB
    keys_for_file = [k for k in S._GZIP_CACHE if k[0].endswith("big.js")]
    assert len(keys_for_file) == 1, "同一文件在缓存里留了 %d 份（应按 mtime 只留最新一份）" % len(keys_for_file)


def test_cache_size_is_capped(monkeypatch, tmp_path):
    """硬顶必须真的生效（否则长期运行会被静态资源挤爆内存）。"""
    monkeypatch.setattr(S, "_GZIP_CACHE_MAX_BYTES", 12 * 1024)
    S._gzip_cache_clear()
    try:
        for i in range(6):
            p = tmp_path / ("cap%d.js" % i)
            p.write_text(("var n%d = 1;\n" % i) * 4000, encoding="utf-8")
            assert S._gzip_cached(p, p.read_bytes()) is not None
        assert S._GZIP_CACHE_BYTES <= 12 * 1024, (
            "缓存超硬顶：%d > %d" % (S._GZIP_CACHE_BYTES, 12 * 1024))
        assert len(S._GZIP_CACHE) >= 1, "淘汰过头，把刚放进去的也清了"
    finally:
        S._gzip_cache_clear()


# ---------------------------------------------------------------- 8. HEAD 与 GET 同头
def test_head_reports_compressed_length_without_body(base_url, static_root):
    req = urllib.request.Request(base_url + "/static/big.js",
                                 headers={"Accept-Encoding": "gzip"}, method="HEAD")
    with urllib.request.urlopen(req, timeout=15) as r:
        h = {k.lower(): v for k, v in r.headers.items()}
        body = r.read()
    assert r.status == 200
    assert h.get("content-encoding") == "gzip"
    raw = (static_root / "big.js").read_bytes()
    assert int(h["content-length"]) == len(_gzip_mod.compress(raw, 6)), (
        "HEAD 的 Content-Length 必须等于 GET 会发出的压缩后长度")
    assert body == b"", "HEAD 不允许带 body"


# ---------------------------------------------------------------- 9. ETag / 304
# 为什么单独写一个 `http.client` 取数器：`urllib.request.urlopen` 把 304 当**异常**抛
# （HTTPError），而 304 恰好是这一段要验的**正常结果**。用它验不了。
def _raw_get(url: str, headers: dict | None = None):
    u = urllib.parse.urlsplit(url)
    target = u.path + (("?" + u.query) if u.query else "")
    conn = http.client.HTTPConnection(u.hostname, u.port, timeout=15)
    try:
        conn.request("GET", target, headers=headers or {})
        r = conn.getresponse()
        body = r.read()
        return r.status, {k.lower(): v for k, v in r.getheaders()}, body
    finally:
        conn.close()


def test_etag_is_sent_on_200(base_url, static_root):
    """200 必须带 ETag —— 否则浏览器永远学不会发 If-None-Match，304 永远用不上。"""
    st, h, _ = _raw_get(base_url + "/static/big.js", headers={"Accept-Encoding": "gzip"})
    assert st == 200
    assert h.get("etag"), "200 响应没有 ETag"


def test_second_request_with_if_none_match_gets_304(base_url, static_root):
    """核心：同一文件第二次带 If-None-Match → 304、零正文、保留 Vary。

    这条是「no-cache ≠ 必须重传正文」的落地证明：`no-cache` 只要求回源校验，
    校验结果"没变"时**不该再发 400KB 正文**。
    """
    st1, h1, body1 = _raw_get(base_url + "/static/big.js", headers={"Accept-Encoding": "gzip"})
    assert st1 == 200 and h1.get("etag")
    etag = h1["etag"]

    st2, h2, body2 = _raw_get(base_url + "/static/big.js",
                              headers={"Accept-Encoding": "gzip", "If-None-Match": etag})
    assert st2 == 304, "带 If-None-Match 且文件没变，应回 304，实际 %d" % st2
    assert body2 == b"", "304 不允许带正文（实测收到 %d 字节）" % len(body2)
    assert h2.get("content-length") == "0"
    assert h2.get("etag") == etag, "304 应回同一个 ETag"
    assert "accept-encoding" in (h2.get("vary") or "").lower(), "304 必须保留 Vary"
    assert "content-encoding" not in h2, "304 不携带实体，不该标 Content-Encoding"


def test_304_keeps_security_headers(base_url, static_root):
    """304 也要带安全头。

    为什么：有些中间层/客户端以 304 自身的响应头为准，缺了就等于把一份已缓存的页面
    变成「无防护页面」—— P1-5 加的点击劫持防护会凭空消失，而页面看起来完全正常。
    """
    st1, h1, _ = _raw_get(base_url + "/")
    etag = h1["etag"]
    st2, h2, body2 = _raw_get(base_url + "/", headers={"If-None-Match": etag})
    assert st2 == 304 and body2 == b""
    assert h2.get("x-frame-options", "").upper() == "SAMEORIGIN", "304 丢了 X-Frame-Options"
    assert "frame-ancestors" in (h2.get("content-security-policy") or ""), "304 丢了 CSP"
    assert "accept-encoding" in (h2.get("vary") or "").lower(), "304 丢了 Vary"


def test_304_works_for_identity_variant_too(base_url, static_root):
    """不压缩的那一份也要能 304 —— 它的 ETag 与 gzip 那份**不同**（representation 不同）。"""
    st1, h1, _ = _raw_get(base_url + "/static/big.js")
    etag_identity = h1["etag"]
    st_gz, h_gz, _ = _raw_get(base_url + "/static/big.js", headers={"Accept-Encoding": "gzip"})
    assert h_gz["etag"] != etag_identity, (
        "gzip 与 identity 共用同一 ETag：共享缓存按 Vary 分桶会把两份搞混")

    st2, h2, body2 = _raw_get(base_url + "/static/big.js",
                              headers={"If-None-Match": etag_identity})
    assert st2 == 304 and body2 == b""


def test_etag_changes_when_file_changes(base_url, static_root):
    """改内容（mtime_ns 必变）后旧 ETag 不得再命中 —— 否则会把旧内容一直发给用户。"""
    _, h1, _ = _raw_get(base_url + "/static/big.js", headers={"Accept-Encoding": "gzip"})
    old_etag = h1["etag"]

    (static_root / "big.js").write_text(BIG_JS + "// v2\n" * 20, encoding="utf-8", newline="\n")

    st2, h2, body2 = _raw_get(base_url + "/static/big.js",
                              headers={"Accept-Encoding": "gzip", "If-None-Match": old_etag})
    assert st2 == 200, "文件变了却回 304 —— 浏览器会永远用旧内容"
    assert h2["etag"] != old_etag
    assert _digest(_gzip_mod.decompress(body2)) == _digest((static_root / "big.js").read_bytes())


def test_304_is_decided_before_compression(base_url, static_root, monkeypatch):
    """304 判断必须在压缩之前 —— 否则命中 304 时白烧 30ms CPU 再把结果丢掉。"""
    _, h1, _ = _raw_get(base_url + "/static/big.js", headers={"Accept-Encoding": "gzip"})
    etag = h1["etag"]

    counter = _CountingGzip()
    monkeypatch.setattr(S, "gzip", counter)
    S._gzip_cache_clear()          # 保证缓存里没有现成的，任何压缩都会被计到

    st2, _, body2 = _raw_get(base_url + "/static/big.js",
                             headers={"Accept-Encoding": "gzip", "If-None-Match": etag})
    assert st2 == 304 and body2 == b""
    assert counter.calls == 0, "命中 304 却还是压了一次（%d 次）" % counter.calls
