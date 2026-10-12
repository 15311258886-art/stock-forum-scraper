# -*- coding: utf-8 -*-
"""往 viewer/server.py 串行插入「板块资金流日期区间」三处接线（import / API / 路由）。

串行 io.open 替换，不用 Edit（board_flow.py 之类大文件并行 Edit 会静默丢写）。
跑法：.venv/Scripts/python.exe _patch_server_range.py
"""
import io
import os
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
PATH = os.path.join(ROOT, "viewer", "server.py")

API_SRC = '''

# ---------------------------------------------------------------- 板块资金流：日期区间
_FLOW_RANGE_CACHE: dict = {}
_FLOW_RANGE_TTL = 60          # 秒；区间聚合要读库 + 可能后台补抓，60s 足够挡住反复刷新


def _flow_range_cached(key: str, data: dict) -> dict:
    now = time.time()
    hit = _FLOW_RANGE_CACHE.get(key)
    if hit and now - hit[0] < _FLOW_RANGE_TTL and not data.get("force"):
        return hit[1]
    _FLOW_RANGE_CACHE[key] = (now, data)
    return data


def api_board_flow_range(start: str = "", end: str = "", kind: str = "industry",
                         top: int = 16, force: int = 0) -> dict:
    """板块资金流「日期区间」聚合（2026-10-05，王总：累计流入要能指定起止日期）。

    与「单日分时曲线」是两套数据形态：这里是**日线历史**（每个交易日一个点），
    那里是**当日分钟累计**。两个 mode 在前端并存，互不串味。
    """
    try:
        board_flow_range.ensure_schema(DB.conn)
        res = board_flow_range.range_flow(
            DB.conn, start=start, end=end, kind=kind, top=top, force=bool(force))
        res["force"] = bool(force)
        return _flow_range_cached("range:%s:%s:%s:%d:%d" % (start, end, kind, top, force), res)
    except Exception as exc:  # noqa: BLE001
        logger.exception("板块资金流区间聚合异常")
        return {"ok": False, "error": "区间聚合失败：%s" % exc, "series": [], "pool": []}


def api_board_flow_dates(kind: str = "industry") -> dict:
    """区间日期框的可选范围（交易日历 ∪ 库内已有日线日期）。"""
    try:
        board_flow_range.ensure_schema(DB.conn)
        return board_flow_range.date_options(DB.conn, kind)
    except Exception as exc:  # noqa: BLE001
        logger.exception("板块资金流日期清单异常")
        return {"ok": False, "error": str(exc), "days": []}

'''

ROUTE_SRC = '''        if route == "/api/board_flow_range":
            q = urllib.parse.parse_qs(parsed.query)
            return self._send_json(api_board_flow_range(
                start=self._str_arg(q, "start", "", maxlen=16),
                end=self._str_arg(q, "end", "", maxlen=16),
                kind=self._str_arg(q, "kind", "industry", maxlen=16),
                top=self._int_arg(q, "top", 16, 4, 24),
                force=self._int_arg(q, "force", 0, 0, 1)))
        if route == "/api/board_flow_dates":
            q = urllib.parse.parse_qs(parsed.query)
            return self._send_json(api_board_flow_dates(
                kind=self._str_arg(q, "kind", "industry", maxlen=16)))
'''

BOOT_SRC = 'board_flow.ensure_schema(DB.conn)    # 板块资金流采样表（幂等；供调度线程 maybe_sample 写入）\n'

# ---- ① import ----
with io.open(PATH, "r", encoding="utf-8") as f:
    src = f.read()

if "board_flow_range" not in src:
    anchor = "import board_flow  # noqa: E402"
    if anchor not in src:
        print("FATAL: 找不到 `import board_flow` 锚点")
        sys.exit(1)
    src = src.replace(anchor, anchor + "import board_flow_range\n", 1)
    print("[1] import board_flow_range 已插入")

# ---- ② API 函数 ----
if "def api_board_flow_range" not in src:
    anchor = "def api_board_flow_status() -> dict:"
    if anchor not in src:
        print("FATAL: 找不到 api_board_flow_status 锚点")
        sys.exit(1)
    src = src.replace(anchor, API_SRC.strip("\n") + "\n\n\n" + anchor, 1)
    print("[2] API 函数已插入")
else:
    print("[2] API 函数已存在（跳过）")

# ---- ③ 路由 ----
if 'route == "/api/board_flow_range"' not in src:
    anchor = '        if route == "/api/board_flow_status":'
    if anchor not in src:
        print("FATAL: 找不到 /api/board_flow_status 路由锚点")
        sys.exit(1)
    src = src.replace(anchor, ROUTE_SRC + anchor, 1)
    print("[3] 路由已插入")
else:
    print("[3] 路由已存在（跳过）")

# ---- ④ 启动段建表 ----
if "board_flow_range.ensure_schema" not in src:
    if BOOT_SRC not in src:
        print("FATAL: 找不到启动段 make-schema 行")
        sys.exit(1)
    src = src.replace(BOOT_SRC, BOOT_SRC +
                      "board_flow_range.ensure_schema(DB.conn)  # 板块资金流日线区间表（幂等）\n", 1)
    print("[4] 启动建表已插入")
else:
    print("[4] 启动建表已存在（跳过）")

with io.open(PATH, "w", encoding="utf-8") as f:
    f.write(src)
print("written:", PATH, len(src), "chars")

# ---- 复核（不信回执，只信 grep）----
with io.open(PATH, "r", encoding="utf-8") as f:
    back = f.read()
checks = {
    "import board_flow_range": "import board_flow_range" in back,
    "def api_board_flow_range": "def api_board_flow_range" in back,
    "def api_board_flow_dates": "def api_board_flow_dates" in back,
    '/api/board_flow_range': 'route == "/api/board_flow_range"' in back,
    '/api/board_flow_dates': 'route == "/api/board_flow_dates"' in back,
    "boot ensure_schema": "board_flow_range.ensure_schema(DB.conn)" in back,
}
print("RECHECK:", checks)
sys.exit(0 if all(checks.values()) else 2)
