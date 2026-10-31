"""mcp_server/server.py — 零依赖 stdio JSON-RPC (MCP) 服务器。

仅用 Python 标准库，符合 MCP 2025-03-26 stdio 传输规范
（newline-delimited JSON：每行一个 JSON 对象，无内嵌换行）。
不引入任何第三方包，避免沙箱装包失败，也不污染项目依赖。
"""
import json
import sys
from . import db
from . import tools as toolmod

PROTOCOL_VERSION = "2025-03-26"


def _setup_streams():
    """强制 stdio 全程 UTF-8（根因修复）。

    Windows 上 Python 默认按 locale 编码，stdin/stdout 常为 GBK/cp936。
    响应中含私用区字符(如 U+E63C)或 emoji 时，json.dumps(ensure_ascii=False)
    写出会抛 UnicodeEncodeError，导致 run() 主循环未捕获异常、进程退出，
    MCP 客户端收到 'Connection closed'。显式 reconfigure 为 UTF-8 可根治。
    """
    for s in (sys.stdin, sys.stdout, sys.stderr):
        if hasattr(s, "reconfigure"):
            try:
                s.reconfigure(encoding="utf-8")
            except Exception:
                pass

def _send(obj):
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _message_stream():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except Exception:
            continue
        if isinstance(msg, dict):
            yield msg


def handle(msg, con):
    method = msg.get("method")
    mid = msg.get("id")
    params = msg.get("params") or {}
    is_dict = isinstance(params, dict)

    # 1) 握手：返回协议版本、能力、服务端信息
    if method == "initialize":
        client_ver = params.get("protocolVersion") if is_dict else None
        return {
            "jsonrpc": "2.0", "id": mid,
            "result": {
                "protocolVersion": client_ver or PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "stock-forum-mcp", "version": "1.0.0"},
            },
        }

    # 通知类（无 id，如 notifications/initialized）不回复
    if mid is None:
        return None

    # 2) 心跳
    if method == "ping":
        return {"jsonrpc": "2.0", "id": mid, "result": {}}

    # 3) 工具清单
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": mid, "result": {"tools": toolmod.TOOLS}}

    # 4) 调用工具
    if method == "tools/call":
        name = params.get("name") if is_dict else None
        args = (params.get("arguments") if is_dict else None) or {}
        try:
            data = toolmod.call_tool(con, name, args)
            text = json.dumps(data, ensure_ascii=False, indent=2)
            return {"jsonrpc": "2.0", "id": mid,
                    "result": {"content": [{"type": "text", "text": text}], "isError": False}}
        except Exception as e:
            err = json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False)
            return {"jsonrpc": "2.0", "id": mid,
                    "result": {"content": [{"type": "text", "text": err}], "isError": True}}

    # 5) 资源/提示类探测：返回空列表，避免严格客户端断开（我们只暴露 tools）
    if method in ("resources/list", "prompts/list", "resourceTemplates/list"):
        key = method.split("/")[0] + "s"
        return {"jsonrpc": "2.0", "id": mid, "result": {key: []}}

    # 其余方法：标准 method not found
    return {"jsonrpc": "2.0", "id": mid,
            "error": {"code": -32601, "message": "Method not found: " + str(method)}}


def run(db_path=None):
    _setup_streams()
    sys.stderr.write("stock-forum-mcp: read-only server starting\n")
    sys.stderr.flush()
    con = db.connect(db_path)
    try:
        for msg in _message_stream():
            resp = handle(msg, con)
            if resp is not None:
                _send(resp)
    except KeyboardInterrupt:
        pass
    finally:
        con.close()
