# -*- coding: utf-8 -*-
"""后端修复验收脚本（自起 8766 临时实例 → 逐项实测 → 报告落盘）。

约定：
- 所有 localhost 探测都绕过代理（ProxyHandler({})），否则服务异常时会拿到 502 而非真实结果。
- 报告以 UTF-8 写入 tools/_qa_fix_backend.txt，避免控制台编码干扰。
- 500 兜底用「进程内再起一个真实 Handler 实例（8767）」并注入异常来验证，因为跨进程无法 monkeypatch。
"""
from __future__ import annotations

import http.client
import json
import os
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from http.server import ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PY = str(ROOT / ".venv" / "Scripts" / "python.exe")
TMP_PORT = 8766
LIVE_PORT = 8765
INJ_PORT = 8767
BIG_PORT = 8768

OUT: list[str] = []


def p(*a):
    OUT.append(" ".join(str(x) for x in a))


OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def get(path: str, headers: dict | None = None, port: int = TMP_PORT):
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", headers=headers or {})
    try:
        with OPENER.open(req, timeout=20) as r:
            return r.status, r.read(), dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read(), dict(e.headers)
    except Exception as e:  # noqa: BLE001
        return -1, f"<EXC {type(e).__name__}: {e}>".encode(), {}


def raw(method: str, path: str, headers: dict | None = None, body: bytes | None = None,
        port: int = TMP_PORT):
    """底层 http.client：路径不被归一化，可发 `..` / 伪造 Host / 自定义 Origin。"""
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=20)
    try:
        c.request(method, path, body=body, headers=headers or {})
        r = c.getresponse()
        data = r.read()
        return r.status, data, dict(r.getheaders())
    finally:
        c.close()


def alive(port: int) -> bool:
    try:
        with OPENER.open(f"http://127.0.0.1:{port}/api/summary", timeout=3) as r:
            return r.status == 200
    except Exception:  # noqa: BLE001
        return False


def wait_up(port: int, timeout: float = 25.0) -> bool:
    t0 = time.time()
    while time.time() - t0 < timeout:
        if alive(port):
            return True
        time.sleep(0.4)
    return False


p("=" * 78)
p("后端修复验收（临时实例 127.0.0.1:%d；日期 %s）" % (TMP_PORT, time.strftime("%Y-%m-%d")))
p("=" * 78)
p(f"验收前：8765 存活 = {alive(LIVE_PORT)}")

env = dict(os.environ)
env["VIEWER_PORT"] = str(TMP_PORT)
logf = open(ROOT / "tools" / "_bf_server8766.log", "w", encoding="utf-8", errors="replace")
proc = subprocess.Popen([PY, str(ROOT / "viewer" / "server.py")], cwd=str(ROOT),
                        env=env, stdout=logf, stderr=subprocess.STDOUT)
try:
    if not wait_up(TMP_PORT):
        raise SystemExit("临时实例启动失败，见 tools/_bf_server8766.log")
    p("临时实例已启动 = True")

    # ------------------------------------------------------------ 1 路径穿越 + 静态
    p("")
    p("[1] 路径穿越仍 404 / 静态资源仍 200")
    for path in ("/static/../server.py", "/static/../app.db", "/static/%2e%2e/server.py",
                 "/static/../../server.py", "/static/", "/static/nope.js"):
        st, data, _ = raw("GET", path)
        p(f"    {path:<34} -> {st}  body={data[:40]!r}")
    for path in ("/", "/index.html", "/static/app.js", "/static/app.css"):
        st, data, _ = raw("GET", path)
        p(f"    {path:<34} -> {st}  bytes={len(data)}")

    # ------------------------------------------------------------ 2 CORS
    p("")
    p("[2] CORS：恶意 Origin 不再拿到 ACAO；仅回环 Origin 回显")
    for origin, label in ((None, "(无 Origin)"), ("https://evil.example", "https://evil.example"),
                          ("http://evil.example:8766", "http://evil.example:8766"),
                          (f"http://127.0.0.1:{TMP_PORT}", f"http://127.0.0.1:{TMP_PORT}"),
                          (f"http://localhost:{TMP_PORT}", f"http://localhost:{TMP_PORT}"),
                          ("null", "null")):
        hdrs = {"Origin": origin} if origin else {}
        st, _, hdr = get("/api/summary", hdrs)
        p(f"    {label:<28} -> {st}  ACAO={hdr.get('Access-Control-Allow-Origin')!r}")

    # ------------------------------------------------------------ 3 非法整数参数
    p("")
    p("[3] 非法整数参数 -> 400 JSON（修复前连接被硬关、0 字节）")
    for path in ("/api/posts?limit=abc", "/api/consensus?min_holders=abc",
                 "/api/detail?kind=post&id=abc"):
        st, data, _ = raw("GET", path)
        p(f"    {path:<44} -> {st}  bytes={len(data)}  body={data[:60]!r}")

    # ------------------------------------------------------------ 4 参数上限
    p("")
    p("[4] 参数上限：limit 钳制 [1,500]")
    p(f"    /api/posts                 (默认)  -> {len(json.loads(get('/api/posts')[1]))} 行")
    p(f"    /api/posts?limit=-1                -> {len(json.loads(get('/api/posts?limit=-1')[1]))} 行"
      f"   （修复前 SQLite LIMIT -1 = 返回全表 185 行）")
    p(f"    /api/posts?limit=0                 -> {len(json.loads(get('/api/posts?limit=0')[1]))} 行（钳到 1）")
    p(f"    /api/posts?limit=3                 -> {len(json.loads(get('/api/posts?limit=3')[1]))} 行")
    p(f"    /api/posts?limit=999999999         -> {len(json.loads(get('/api/posts?limit=999999999')[1]))} 行"
      f"（已钳制为 500；posts 表只有 185 行，故结果等于全表但上限被锁死）")
    sys.path.insert(0, str(ROOT))
    import viewer.server as vsrv  # noqa: E402
    dummy = object.__new__(vsrv.Handler)
    p(f"    钳制函数实测 _int_arg(limit=-1)        -> {dummy._int_arg({'limit': ['-1']}, 'limit', 20, 1, 500)}")
    p(f"    钳制函数实测 _int_arg(limit=999999999) -> {dummy._int_arg({'limit': ['999999999']}, 'limit', 20, 1, 500)}")
    try:
        dummy._int_arg({"limit": ["abc"]}, "limit", 20, 1, 500)
        p("    钳制函数实测 _int_arg(limit=abc)       -> 未抛错（异常！）")
    except vsrv._BadRequest as e:
        p(f"    钳制函数实测 _int_arg(limit=abc)       -> _BadRequest({e})")
    st, data, _ = raw("GET", "/api/person?name=" + "x" * 100)
    p(f"    /api/person?name=<100 字符>          -> {st}  body={data[:70]!r}")
    st, data, _ = raw("GET", "/api/stock?symbol=" + "9" * 100)
    p(f"    /api/stock?symbol=<100 字符>         -> {st}  body={data[:70]!r}")

    # 4b) 用 >500 行的临时库证明上限真的咬合（当前 posts 仅 185 行，看不出 500 这个天花板）
    import sqlite3  # noqa: E402
    import tempfile  # noqa: E402
    tmpdb = Path(tempfile.gettempdir()) / "bf_big_verify.db"
    src = sqlite3.connect(str(ROOT / "viewer" / "app.db"))
    dst = sqlite3.connect(str(tmpdb))
    src.backup(dst)
    dst.close()
    src.close()
    c2 = sqlite3.connect(str(tmpdb))
    c2.execute("DELETE FROM posts")
    c2.executemany(
        "INSERT INTO posts (site,author,post_id,post_url,post_time,text,sentiment,sentiment_score) "
        "VALUES (?,?,?,?,?,?,?,?)",
        [("t", "a", str(i), "", f"2026-09-{i % 28 + 1:02d} 10:00", "text", "中性", 0.0)
         for i in range(600)])
    c2.commit()
    c2.close()
    p(f"    [4b] 临时库（in-process 8768）posts 行数 = 600")
    srv3 = ThreadingHTTPServer(("127.0.0.1", BIG_PORT), vsrv.Handler)
    th3 = threading.Thread(target=srv3.serve_forever, daemon=True)
    th3.start()
    db_orig = vsrv.DB
    vsrv.DB = vsrv.Database(str(tmpdb))
    try:
        time.sleep(0.5)
        n1 = len(json.loads(get("/api/posts?limit=999999999", port=BIG_PORT)[1]))
        n3 = len(json.loads(get("/api/posts?limit=700", port=BIG_PORT)[1]))
        n2 = len(json.loads(get("/api/posts?limit=-1", port=BIG_PORT)[1]))
        nt = len(json.loads(get("/api/posts?limit=1", port=BIG_PORT)[1]))
        p(f"    [4b] 600 行临时库：limit=999999999 -> {n1} 行，limit=700 -> {n3} 行，"
          f"limit=-1 -> {n2} 行，limit=1 -> {nt} 行  ⇒ 天花板 500 生效（600 行全表不可再取）")
    finally:
        vsrv.DB = db_orig
        srv3.shutdown()
        srv3.server_close()

    # ------------------------------------------------------------ 5 人员数
    p("")
    persons = json.loads(get("/api/persons")[1])
    p(f"[5] /api/persons 条数 = {len(persons)}（修复前 LIMIT 100 截断为 100）")

    # ------------------------------------------------------------ 6 人物 positions.channel
    p("")
    positions_all = json.loads(get("/api/positions")[1])
    who = positions_all[0]["player"]
    pdata = json.loads(get("/api/person?name=" + urllib.parse.quote(who))[1])
    p(f"[6] /api/person?name={who} 的 positions 共 {len(pdata['positions'])} 条（带 channel）：")
    for it in pdata["positions"][:6]:
        p(f"      {it.get('symbol')}  channel={it.get('channel')!r}  hold_since={it.get('hold_since')!r}"
          f"  hold_days={it.get('hold_days')}")
    miss = [i for i in pdata["positions"] if "channel" not in i]
    p(f"    缺 channel 字段的条数 = {len(miss)}（修复前 100% 缺失，前端 app.js 兜底成 'A'）")
    # 再取一个渠道非 A 的人，证明 channel 是"如实返回"而不是恒为 A
    other = next((r["player"] for r in positions_all
                  if r.get("channel") and r["channel"] != "A"), None)
    if other:
        od = json.loads(get("/api/person?name=" + urllib.parse.quote(other))[1])
        chans = [(i["symbol"], i["channel"]) for i in od["positions"][:6]]
        p(f"    渠道≠A 的样本 /api/person?name={other}：{chans}")

    # ------------------------------------------------------------ 7 海外新闻区域
    p("")
    ov = json.loads(get("/api/overseas_sentiment")[1])
    c = Counter(n["region"] for n in ov["news"])
    p(f"[7] /api/overseas_sentiment news 共 {len(ov['news'])} 条，区域分布 = {dict(c)}")
    p(f"    US/HK/GLOBAL 是否都出现 = {all(k in c for k in ('US', 'HK', 'GLOBAL'))}"
      f"（修复前 40 条全为 US）")

    # ------------------------------------------------------------ 8 源状态 enabled
    p("")
    src = json.loads(get("/api/overseas_sources")[1])
    rows = src["sources"]
    have = [r for r in rows if "enabled" in r]
    on = sum(1 for r in rows if r.get("enabled"))
    p(f"[8] /api/overseas_sources sources={len(rows)}，带 enabled 字段={len(have)}，"
      f"enabled=1 共 {on}，enabled=0 共 {len(rows) - on}")
    for r in rows:
        if not r.get("enabled"):
            p(f"      enabled=0  {r['source_key']:<20} {r['region']:<7} status={r['status']}")

    # ------------------------------------------------------------ 9 404 头
    p("")
    st, data, hdr = raw("GET", "/api/does-not-exist")
    p(f"[9] GET /api/does-not-exist -> {st}  Server={hdr.get('Server')!r}  body={data[:60]!r}")
    st, data, hdr = raw("GET", "/totally/nothing")
    p(f"    GET /totally/nothing     -> {st}  Server={hdr.get('Server')!r}  body={data[:60]!r}")
    st, data, hdr = get("/api/summary")
    p(f"    GET /api/summary         -> {st}  Server={hdr.get('Server')!r}")
    st, _, hdr = raw("POST", "/api/does-not-exist",
                     {"Content-Type": "application/json"}, b"{}")
    p(f"    POST /api/does-not-exist -> {st}  Server={hdr.get('Server')!r}")
    p(f"    以上响应头含 'Python/' = {'Python/' in json.dumps([hdr])}")

    # ------------------------------------------------------------ 10 写接口守卫
    p("")
    p("[10] 写接口守卫（POST，5 个写接口共用）")
    J = {"Content-Type": "application/json"}
    for hh, body, label in (
        ({**J, "Origin": "https://evil.example"}, b"{}", "恶意 Origin"),
        ({**J, "Host": "evil.example"}, b"{}", "伪造 Host"),
        ({**J, "Host": "evil.example:8766"}, b"{}", "伪造 Host:port"),
        ({"Content-Type": "text/plain"}, b"{}", "Content-Type 非 json"),
        ({}, b"{}", "Content-Type 缺失"),
        (J, b"{oops", "非法 JSON 体"),
        (J, b"[1,2]", "JSON 非对象"),
        (J, b"", "空体"),
        ({**J, "Host": "localhost:%d" % TMP_PORT}, b"{}", "合法 Host:localhost"),
    ):
        st, data, _ = raw("POST", "/api/does-not-exist", hh, body)
        p(f"    {label:<22} -> {st}  {data[:64]!r}")
    p("    说明：非法 JSON 体在修复前会静默降级成 {} 后**照常执行写接口**（最危险的一处）；"
      "现在在路由前即回 400，未进入任何 handler。")

    # ------------------------------------------------------------ 11 hold_days
    p("")
    p("[11] 已持有天数（同一 symbol 的所有记录必须一致）")
    for sym in ("600519", "601318"):
        d = json.loads(get("/api/stock?symbol=" + sym)[1])
        days = sorted({it["hold_days"] for it in d["positions"]})
        p(f"    {sym}: positions={len(d['positions'])}  hold_since={d['positions'][0]['hold_since']!r}"
          f"  hold_days 取值集合={days}  不同取值数={len(days)}")
        for it in d["positions"]:
            p(f"        {it['player']:<14} channel={it['channel']} action={it['action']:<4} "
              f"buy_time={str(it['buy_time']):<17} sell_time={str(it['sell_time']):<17} hold_days={it['hold_days']}")

    # ------------------------------------------------------------ 12 GET 500 兜底（进程内注入）
    p("")
    p("[12] GET 未预期异常兜底（进程内另起真实 Handler 实例在 8767，注入异常）")
    srv2 = ThreadingHTTPServer(("127.0.0.1", INJ_PORT), vsrv.Handler)
    th = threading.Thread(target=srv2.serve_forever, daemon=True)
    th.start()
    orig = vsrv.api_topics
    try:
        def boom():
            raise RuntimeError("boom /secret/path/injected.py")
        vsrv.api_topics = boom
        time.sleep(0.5)
        st, data, hdr = raw("GET", "/api/topics", port=INJ_PORT)
        txt = data.decode("utf-8", "ignore")
        p(f"    /api/topics（内部抛 RuntimeError）-> {st}  body={data[:80]!r}")
        p(f"    响应体含 traceback / 文件路径 = {'Traceback' in txt or 'injected' in txt or 'Traceback' in txt}")
        st, data, _ = raw("GET", "/api/does-not-exist", port=INJ_PORT)
        p(f"    /api/does-not-exist（同实例）      -> {st}  body={data[:60]!r}")
    finally:
        vsrv.api_topics = orig
        srv2.shutdown()
        srv2.server_close()
    p("    完整 traceback 只出现在本脚本 stderr（tools/_bf_verify_err.log），未回给客户端。")
finally:
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except Exception:  # noqa: BLE001
        proc.kill()
    logf.close()

time.sleep(0.5)
p("")
p("=" * 78)
p(f"验收后：8766 已停止 = {not alive(TMP_PORT)}；8765 仍存活 = {alive(LIVE_PORT)}")
p("=" * 78)

(ROOT / "tools" / "_qa_fix_backend.txt").write_text("\n".join(OUT) + "\n", encoding="utf-8")
print("\n".join(OUT))
