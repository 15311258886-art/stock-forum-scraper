# -*- coding: utf-8 -*-
"""由 AI 在外网经 westock 连接器抓取的 17 板块成分股 → 落库。

输入：.workbuddy 下 westock data_sector(constituent) 的落盘 JSON（tool-results/*.txt）。
      每个文件形如 {"content":"{...}"}（chatcmpl 封装）或裸 {"ok":true,...}（mcp-westock 原生）。
      内层 data 下为 comp_<板块码>: {stocks:[{SectorCode,code,name}]}。
输出：viewer/plate_members_staging.db 的 plate_members(code,name,board,board_code,updated_at)
      board = 漏斗板块名（与前端 mode=official 入参一致），board_code = 腾讯/聚源板块码 用 | 连接。

用法：.venv/Scripts/python.exe tools/load_plate_members_westock.py
"""
from __future__ import annotations
import json
import re
import sqlite3
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STAGING = ROOT / "viewer" / "plate_members_staging.db"
TOOL_RESULTS = Path(
    r"C:/Users/野比&大熊/.workbuddy/projects/"
    r"f-AI Agent部署知识库-WorkBuddy-2026-10-28-00-05-11/"
    r"37f70eeb-bf1d-4b6a-9b53-435c15a3b4e2/tool-results"
)

# ── 腾讯/聚源 板块码 → 漏斗板块名 ──
SECTOR_TO_BOARD = {
    "sw1_pt01801780": "银行",
    "sw2_pt01801053": "黄金",
    "sw2_pt01801081": "半导体",
    "sw2_pt01801125": "白酒",
    "sw2_pt01801193": "券商",
    "sw2_pt01801764": "游戏",
    "sw1_pt01801740": "军工",
    "sw2_pt01801104": "软件",
    "sw2_pt01801735": "光伏",
    "sw2_pt01801181": "地产",
    "sw2_pt01801737": "锂电",
    "indus_pt02GN2006": "算力",
    "indus_pt02003800": "AI",
    "indus_pt02003640": "机器人",
    "indus_pt02003891": "芯片",
    "sw1_pt01801120": "消费",   # 食品饮料
    "sw2_pt01801085": "消费",   # 消费电子
    "sw2_pt01801093": "汽车",   # 汽车零部件
    "sw2_pt01801095": "汽车",   # 乘用车
}

# 反向：漏斗板块 → 用到的所有源板块码（用于 board_code 字段）
BOARD_SOURCES = {}
for _code, _board in SECTOR_TO_BOARD.items():
    BOARD_SOURCES.setdefault(_board, []).append(_code)


def _norm_code(raw: str) -> str:
    """sh601818 / sz000035 / bj920367 → 6 位。"""
    if not raw:
        return ""
    s = re.sub(r"^[a-zA-Z]+", "", raw.strip())  # 去掉交易所前缀
    return s[-6:] if len(s) >= 6 else s


def _load_json_file(path: Path):
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return None
    text = text.strip()
    if not text:
        return None
    try:
        obj = json.loads(text)
    except Exception:
        return None
    # chatcmpl 封装：{"content":"{...}"}
    if isinstance(obj, dict) and isinstance(obj.get("content"), str):
        try:
            obj = json.loads(obj["content"])
        except Exception:
            return None
    return obj


def main() -> int:
    if not TOOL_RESULTS.exists():
        print(f"[error] 找不到 tool-results 目录：{TOOL_RESULTS}")
        return 2

    # board -> {code: name}，board -> set(source_codes)
    boards: dict[str, dict[str, str]] = {}
    used_codes: dict[str, set[str]] = {}
    seen_comp_keys: set[str] = set()
    unmapped: set[str] = set()

    files = sorted(TOOL_RESULTS.glob("*.txt"))
    print(f"==> 扫描 {len(files)} 个 tool-results 文件…")
    for fp in files:
        obj = _load_json_file(fp)
        if not obj:
            continue
        data = (obj.get("data") or {}) if isinstance(obj, dict) else {}
        if not isinstance(data, dict):
            continue
        for key, val in data.items():
            if not key.startswith("comp_"):
                continue
            seen_comp_keys.add(key)
            sector = key[len("comp_"):]
            board = SECTOR_TO_BOARD.get(sector)
            if not board:
                unmapped.add(sector)
                continue
            stocks = (val or {}).get("stocks") or []
            bmap = boards.setdefault(board, {})
            uc = used_codes.setdefault(board, set())
            uc.add(sector)
            for st in stocks:
                code = _norm_code(st.get("code", ""))
                name = (st.get("name") or "").strip()
                if not code:
                    continue
                bmap.setdefault(code, name)  # 同一 board 内去重，保留首个名

    print(f"    命中 comp_ 键 {len(seen_comp_keys)} 个；漏斗板块 {len(boards)} 个")
    if unmapped:
        print(f"    [warn] 未映射的板块码：{sorted(unmapped)}")

    # 落库
    con = sqlite3.connect(str(STAGING), timeout=30)
    con.execute("""CREATE TABLE IF NOT EXISTS plate_members (
        code TEXT, name TEXT, board TEXT, board_code TEXT, updated_at TEXT)""")
    con.execute("DELETE FROM plate_members")  # 整表重建（本表仅暂存用）
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    total = 0
    rows = []
    for board, cmap in sorted(boards.items()):
        bcode = "|".join(sorted(used_codes[board]))
        for code, name in cmap.items():
            rows.append((code, name, board, bcode, stamp))
            total += 1
    con.executemany(
        "INSERT INTO plate_members(code,name,board,board_code,updated_at) VALUES(?,?,?,?,?)",
        rows)
    con.commit()
    con.close()

    print(f"\n==> 落库完成：staging={STAGING}")
    print(f"    总行数 {total}，板块数 {len(boards)}")
    for board in sorted(boards):
        print(f"    {board:>4} : {len(boards[board]):>4} 只  (源 {BOARD_SOURCES[board]})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
