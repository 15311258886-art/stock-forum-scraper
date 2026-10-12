"""结束占用指定端口（默认 8765）的监听进程，供重启查看器前清场。

注意：中文 Windows 的 netstat / taskkill 输出是 GBK。**不要给 subprocess 传
text=True**——那样解码发生在子进程读取线程里，解不动会抛 UnicodeDecodeError
（表现为 "Exception in thread Thread-N (_readerthread)"），而且拿不到任何输出。
统一先取 bytes，再按候选编码表自己解。
"""
import subprocess
import sys


def _decode(raw: bytes) -> str:
    """中文 Windows 输出兼容解码：utf-8 优先，退回 gbk/cp936，最后 latin-1 兜底。"""
    for enc in ("utf-8", "gbk", "cp936"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("latin-1", "ignore")


PORT = sys.argv[1] if len(sys.argv) > 1 else "8765"
raw = subprocess.run(["netstat", "-ano", "-p", "TCP"], capture_output=True).stdout or b""
out = _decode(raw)
pids = set()
for line in out.splitlines():
    if f":{PORT}" in line and "LISTENING" in line.upper():
        parts = line.split()
        if parts:
            pids.add(parts[-1])
print("listening PIDs on", PORT, "->", sorted(pids) or "none")
for p in sorted(pids):
    r = subprocess.run(["taskkill", "/PID", p, "/F"], capture_output=True)
    msg = _decode((r.stdout or b"") + (r.stderr or b"")).strip().replace("\r\n", " ")
    print("taskkill", p, "->", msg[:160])
