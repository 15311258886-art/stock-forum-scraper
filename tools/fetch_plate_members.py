# -*- coding: utf-8 -*-
"""一次性抓取「方法共振漏斗」17 个板块的东财官方全量成分股，落库 plate_members。

为什么需要它（回答进哥第二个疑问）：
  漏斗"舆情池"= 豆包视频文本挖名的注意力池（非交易所官方全量，会漏掉没被博主点名的票）。
  本脚本改用东方财富概念/行业板块的**官方全量成分股**，结果更全、更权威。

运行环境：
  ⚠ 必须在「有出网能力」的机器上跑（本项目沙箱无出网）。在本机 venv 下：
      .venv/Scripts/python.exe tools/fetch_plate_members.py
  或指定板块：
      .venv/Scripts/python.exe tools/fetch_plate_members.py --plate 半导体
  重跑安全：同板块先 DELETE 再 INSERT（幂等）。

落库：viewer/app.db 的 plate_members(code,name,board,board_code,updated_at)
      其中 board = 漏斗板块名（与前端 mode=official 的入参一致），board_code = 东财 BK 代码。
"""
from __future__ import annotations
import argparse
import json
import sqlite3
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path
import os

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = Path(os.environ.get("VIEWER_DB") or (ROOT / "viewer" / "app.db"))

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")
HEADERS = {"User-Agent": UA, "Referer": "https://quote.eastmoney.com/"}

# ── 漏斗板块 → 东财板块名关键词（在概念 t:2 / 行业 t:1 中按名匹配，取首个命中）──
# 如需微调某个板块对应的东财板块，改这里即可。
PLATE_KEYWORDS = {
    "AI":       ["人工智能", "AI"],
    "半导体":    ["半导体"],
    "机器人":    ["机器人"],
    "黄金":      ["黄金", "贵金属"],
    "券商":      ["证券", "券商"],
    "银行":      ["银行"],
    "光伏":      ["光伏"],
    "消费":      ["消费电子", "食品饮料"],
    "地产":      ["房地产开发", "房地产"],
    "算力":      ["算力", "算力租赁", "东数西算"],
    "锂电":      ["锂电池", "锂电"],
    "白酒":      ["白酒"],
    "军工":      ["国防军工", "军工"],
    "软件":      ["软件开发", "计算机应用"],
    "汽车":      ["汽车整车", "汽车零部件"],
    "芯片":      ["芯片"],
    "游戏":      ["手机游戏", "游戏"],
}


def _http_json(url: str, timeout: int = 20) -> dict:
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _diff_rows(data) -> list[tuple[str, str]]:
    """把东财 clist 的 `diff` 拆成 [(code, name), ...]，两种形态都吃。

    ★ 2026-11-06 审计 P1-01（plate_members 连续失败 14 次，健康接口一直 degraded）：
      东财的 `diff` 有时是数组 `[["000001", 0, "平安银行"], ...]`，
      有时是对象 `{"0": {"f12": "000001", "f14": "平安银行"}, ...}`。
      旧代码只按数组写：`for row in data.get("diff") or []` + `row[0]`。
      一旦返回对象，`for row in dict` 迭代的是**键名**（"0"、"1"…），
      于是 `row[0]` 取到字符串的第一个字符 "0"，再当字典下标用 → `KeyError: 0`。
      这就是那条错误的全部来源。两种形态都解析，且对缺失字段 fail-soft（跳过，不整批炸）。
    """
    raw = data.get("diff") if isinstance(data, dict) else data
    if isinstance(raw, dict):
        raw = list(raw.values())
    if not isinstance(raw, (list, tuple)):
        return []
    out: list[tuple[str, str]] = []
    for row in raw:
        if isinstance(row, dict):
            code, name = row.get("f12"), row.get("f14")
        elif isinstance(row, (list, tuple)):
            code = row[0] if len(row) > 0 else None
            name = row[2] if len(row) > 2 else None
        else:
            continue
        if code:
            out.append((str(code), str(name or "")))
    return out


def _raw_count(data) -> int:
    """这一页实际给了多少条（分页终止判据用**原始条数**，不用解析后条数）。"""
    raw = data.get("diff") if isinstance(data, dict) else data
    if isinstance(raw, dict):
        return len(raw)
    if isinstance(raw, (list, tuple)):
        return len(raw)
    return 0


def _board_list() -> dict:
    """返回 {东财板块名: 板块代码}。

    ★ 2026-11-06 实测发现（审计报告未覆盖）：
      东财 `clist` 接口**单页硬上限 100 条**，`pz` 传 2000 也只回 100。
      原实现只请求第 1 页，而 `m:90+t:2` 的 total 是 496 ——
      也就是说**板块列表从一开始就只拿到了 1/5**（实测 131 个里有 100 个来自行业、
      31 个来自地域，概念板块一个都没有）。这才是"芯片"这类热门板块解析不到的真正原因：
      不是匹配算法不行，是那份名单里压根没有它。
      修 KeyError 只让程序不崩，补分页才让数据变全。
    """
    out: dict[str, str] = {}
    for fs in ("m:90+t:2", "m:90+t:1", "m:90+t:3"):   # 行业 / 地域 / 概念
        pn = 1
        while pn <= 12:                                # 12 × 100 足够覆盖（实测最大 total 531）
            url = ("https://push2.eastmoney.com/api/qt/clist/get?pn=%d&pz=100&po=1"
                   "&np=1&fltt=2&invt=2&fs=%s&fields=f12,f13,f14" % (pn, fs))
            try:
                data = _http_json(url).get("data") or {}
            except Exception as e:  # noqa: BLE001
                print(f"  [warn] 板块列表拉取失败({fs} pn={pn}): {e}")
                break
            rows = _diff_rows(data)
            for code, name in rows:
                # setdefault：行业名优先于概念名（先拉 t:2）。两者同名时，
                # 用行业码取成分股更稳 —— 概念板块的成分股口径经常被调整。
                if name and code:
                    out.setdefault(name, code)
            total = int(data.get("total") or 0)
            if _raw_count(data) < 100:
                break
            if total and pn * 100 >= total:
                break
            pn += 1
            time.sleep(0.15)
    return out


def resolve_board(keyword: str, boards: dict):
    """在 boards(名→码) 中按关键词解析板块代码：优先精确，其次包含。"""
    # 精确
    if keyword in boards:
        return boards[keyword], keyword
    # 包含（取名字最短的，避免"锂电池"命中"锂电池ETF"之类）
    hits = [n for n in boards if keyword in n]
    if hits:
        n = min(hits, key=len)
        return boards[n], n
    return None, None


def fetch_members(board_code: str) -> list:
    """分页拉取某东财板块全部成分股，返回 [(code, name), ...]。

    ★ 2026-11-06 实测发现（审计报告未覆盖）：原来的 `pz=10000` 是个**假分页**。
      接口单页硬上限 100，`len(diff) < pz` 第一次就成立（100 < 10000），
      于是循环立刻退出 —— 每个板块最多只拿 100 只成分股。
      修法是把 pz 设成接口真实上限 100，并用 `total`/原始条数判断是否还有下一页。
      这不是"少一点数据"：一个 400 只成分股的板块只入库 100 只，
      后面所有"板块内个股"的分析都会静默地只看到四分之一，而且不报错。
    """
    out = []
    pn = 1
    pz = 100          # 接口硬上限；传更大也只会返回 100
    while pn <= 40:   # 40 × 100 = 4000，足够任何板块
        fs = "b:" + board_code
        url = ("https://push2.eastmoney.com/api/qt/clist/get?pn=%d&pz=%d&po=1"
               "&np=1&fltt=2&invt=2&fs=%s&fields=f12,f13,f14" % (pn, pz, fs))
        data = _http_json(url).get("data") or {}
        rows = _diff_rows(data)
        for code, name in rows:
            if code:
                out.append((str(code).zfill(6), name))
        total = int(data.get("total") or 0)
        if _raw_count(data) < pz:
            break
        if total and pn * pz >= total:
            break
        pn += 1
        time.sleep(0.2)
    # 去重（同 code 可能跨页重复）
    seen = {}
    for c, n in out:
        seen.setdefault(c, n)
    return list(seen.items())


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--plate", help="只抓某个板块（默认全部）")
    ap.add_argument("--db", default=str(DB_PATH), help="app.db 路径")
    args = ap.parse_args()

    db = Path(args.db)
    if not db.exists():
        print(f"[error] 找不到数据库：{db}", file=sys.stderr)
        return 2

    plates = [args.plate] if args.plate else list(PLATE_KEYWORDS.keys())
    for p in plates:
        if p not in PLATE_KEYWORDS:
            print(f"[error] 未知板块：{p}（可选：{', '.join(PLATE_KEYWORDS)}）", file=sys.stderr)
            return 2

    print("==> 拉取东财板块列表（概念+行业）…")
    boards = _board_list()
    print(f"    共解析到 {len(boards)} 个东财板块")

    con = sqlite3.connect(str(db), timeout=30)
    con.execute("""CREATE TABLE IF NOT EXISTS plate_members (
        code TEXT, name TEXT, board TEXT, board_code TEXT, updated_at TEXT)""")
    con.commit()

    total_rows = 0
    for plate in plates:
        kws = PLATE_KEYWORDS[plate]
        members = []
        used_boards = []
        for kw in kws:
            bcode, bname = resolve_board(kw, boards)
            if not bcode:
                print(f"  [skip] 板块「{plate}」关键词「{kw}」未匹配到东财板块")
                continue
            ms = fetch_members(bcode)
            print(f"  [ok] 「{plate}」← 东财「{bname}」({bcode}) 成员 {len(ms)} 只")
            members += ms
            used_boards.append(f"{bname}:{bcode}")
            time.sleep(0.3)
        if not members:
            print(f"  [warn] 板块「{plate}」无成员，跳过落库（前端将继续回退舆情池）")
            continue
        # 去重（多关键词可能重叠）
        uniq = {}
        for c, n in members:
            uniq.setdefault(c, n)
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        con.execute("DELETE FROM plate_members WHERE board=?", (plate,))
        con.executemany(
            "INSERT INTO plate_members(code,name,board,board_code,updated_at) VALUES(?,?,?,?,?)",
            [(c, n, plate, "|".join(used_boards), stamp) for c, n in uniq.items()])
        con.commit()
        print(f"  => 板块「{plate}」落库 {len(uniq)} 只（来自 {len(used_boards)} 个东财板块）")
        total_rows += len(uniq)

    print(f"\n==> 完成。plate_members 新增/更新 {total_rows} 行。")
    print("    前端漏斗切到「官方池」即可看到交易所全量成分股。")
    con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
