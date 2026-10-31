# -*- coding: utf-8 -*-
"""本地数据仓（自有存储）—— 「算一次、抓一次，之后只从自己的盘上取」。

为什么要有这一层（进哥 2026-10-29 的要求原话）：
    「别光光从自己机器本身自己跑……我抓取的信息咱们先存在网盘上，然后你自己再从咱们
      自己的网盘去调取，它不会有调取的次数等等的这个封号或者是什么样的情况做限制。」

落地方式（不引入外部依赖，就是本机磁盘 + SQLite）：
    · K 线/行情/语料等**原始数据**早已落在自有库：viewer/kbase.db（全 A 日 K）、
      viewer/app.db（业务表）、viewer/data/*.json（各面板缓存）→ 外网只在「日更」时抓一次；
    · 本模块补上**最后一环**：所有重读接口的「最终响应」也落一份快照，
      于是任何面板在实时计算失败时都能立刻拿到最近一次的完整数据（不空白、不转圈到死）。

目录结构：
    viewer/data/warehouse/snap/<route_slug>__<md5>.json
        └ {"at": "2026-10-29 02:31:05", "path": "/api/backtest", "query": "source=pillars",
           "data": {…接口原样响应…}}

三条纪律：
    1. 写盘原子（tmp + os.replace），绝不出现半截 JSON；
    2. 同一 key 有最小写间隔（默认 120s），避免每请求都碰盘；
    3. 读到的快照一定带 `_snapshot_at`，前端必须显式标注「这是旧数据」——
       宁可让用户知道数据旧，也不能让他误以为是最新行情。
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WH_DIR = os.path.join(ROOT, "viewer", "data", "warehouse")
SNAP_DIR = os.path.join(WH_DIR, "snap")

_LOCK = threading.Lock()
_LAST_WRITE: dict = {}
_MIN_GAP = 120          # 同一 key 最快每 120s 写一次


def _key(path: str, query: str) -> str:
    return hashlib.md5(("%s?%s" % (path, query or "")).encode("utf-8")).hexdigest()[:16]


def _slug(path: str) -> str:
    s = "".join(ch if (ch.isalnum() or ch in "-_") else "_" for ch in (path or ""))
    return (s.strip("_") or "root")[:60]


def snap_file(path: str, query: str = "") -> str:
    return os.path.join(SNAP_DIR, "%s__%s.json" % (_slug(path), _key(path, query)))


def put_snapshot(path: str, query: str, obj, min_gap: int | None = None) -> bool:
    """把某路由的一次成功响应落盘。返回是否真的写了。"""
    if not isinstance(obj, dict) or obj.get("ok") is False or obj.get("computing"):
        return False           # 失败/占位包绝不当成「完整快照」存（否则下次满屏假数据）
    k = _slug(path) + "|" + _key(path, query)
    gap = _MIN_GAP if min_gap is None else min_gap
    now = time.time()
    with _LOCK:
        if now - _LAST_WRITE.get(k, 0) < gap:
            return False
        _LAST_WRITE[k] = now
    try:
        os.makedirs(SNAP_DIR, exist_ok=True)
        fp = snap_file(path, query)
        tmp = fp + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"at": time.strftime("%Y-%m-%d %H:%M:%S"),
                       "path": path, "query": query or "", "data": obj},
                      f, ensure_ascii=False, default=str)
        os.replace(tmp, fp)
        return True
    except Exception:  # noqa: BLE001  快照只做兜底，写失败绝不影响正常响应
        import logging
        logging.exception("本地数据仓写快照失败（忽略）")
        return False


def put_snapshot_bytes(path: str, query: str, body: bytes) -> bool:
    """同 put_snapshot，但直接接收已序列化好的响应体（避免再 dumps 一次几百 KB）。

    只在 _send_json 出口调用：响应体已经是 `_json_safe` 之后的最终 JSON，
    直接拼进信封即可，等于「落盘零额外序列化」。
    """
    if not body or body[:1] not in (b"{", b"["):
        return False
    k = _slug(path) + "|" + _key(path, query)
    now = time.time()
    with _LOCK:
        if now - _LAST_WRITE.get(k, 0) < _MIN_GAP:
            return False
        _LAST_WRITE[k] = now
    try:
        os.makedirs(SNAP_DIR, exist_ok=True)
        head = json.dumps({"at": time.strftime("%Y-%m-%d %H:%M:%S"),
                           "path": path, "query": query or ""}, ensure_ascii=False)
        fp = snap_file(path, query)
        tmp = fp + ".tmp"
        with open(tmp, "wb") as f:
            f.write(head[:-1].encode("utf-8"))     # 去掉结尾的 }
            f.write(b',"data":')
            f.write(body)
            f.write(b"}")
        os.replace(tmp, fp)
        return True
    except Exception:  # noqa: BLE001
        import logging
        logging.exception("本地数据仓写快照失败（忽略）")
        return False


def get_snapshot(path: str, query: str = "") -> tuple:
    """读回快照 → (data, at_str)；没有则 (None, None)。"""
    try:
        fp = snap_file(path, query)
        if not os.path.exists(fp):
            return None, None
        with open(fp, "r", encoding="utf-8") as f:
            obj = json.load(f)
        if not isinstance(obj, dict):
            return None, None
        data = obj.get("data")
        return (data, obj.get("at")) if isinstance(data, dict) else (None, None)
    except Exception:  # noqa: BLE001
        return None, None


def snapshot_response(path: str, query: str = "", allowed=None) -> dict:
    """给 /api/snapshot 用的标准回包：命中返回原样数据 + 降级标记。"""
    if not path or not path.startswith("/api/"):
        return {"ok": False, "_no_snapshot": True, "error": "path 必须是 /api/ 开头的路由"}
    if allowed is not None and path not in allowed:
        return {"ok": False, "_no_snapshot": True, "error": "该路由未开放快照兜底"}
    data, at = get_snapshot(path, query)
    if not isinstance(data, dict):
        return {"ok": False, "_no_snapshot": True,
                "error": "本地数据仓还没有这个面板的快照（需先成功加载过一次）"}
    return {"ok": True, "_snapshot": True, "_degraded": True,
            "_at": at, "path": path, "data": data}


def stats() -> dict:
    """家底：快照条数 / 总体积 / 最旧·最新时间（供健康检查与「自有存储」可视化）。"""
    n = 0
    size = 0
    oldest = newest = None
    try:
        for fn in os.listdir(SNAP_DIR):
            if not fn.endswith(".json"):
                continue
            fp = os.path.join(SNAP_DIR, fn)
            st = os.stat(fp)
            n += 1
            size += st.st_size
            ts = st.st_mtime
            oldest = ts if oldest is None or ts < oldest else oldest
            newest = ts if newest is None or ts > newest else newest
    except OSError:
        pass
    fmt = lambda t: time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(t)) if t else None  # noqa: E731
    return {"ok": True, "dir": SNAP_DIR, "count": n, "bytes": size,
            "oldest": fmt(oldest), "newest": fmt(newest)}
