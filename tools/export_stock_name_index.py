"""导出「股票名 -> 代码」全量索引（scraper/stock_names_full.json），供采集解析器**离线校验标的**。

背景（2026-09-29 王总反馈）：
    淘股吧正文里「第三次出发」被解析成 SELL 第三次、「超短线低吸全仓怼单标起」被解析成
    BUY 全仓怼单标起——因为解析器只用正则抓 2~6 个汉字当股票名，**从不校验这个词是不是股票**。
    脏名入库后既没有代码（点不开详情），又污染「选手实盘」面板。

    修复策略：解析器改为**词典驱动**——先在正文里匹配真实股票名（最左最长匹配），
    命中才产出交易记录，并用词典里的代码补齐 symbol（名字→代码反查），
    彻底消灭"文本碎片当股票名"。

用法：
    python tools/export_stock_name_index.py          # 导出（内置表 + stock_names 缓存 + 龙虎榜简称）
每次 `stock_names` 缓存表有新增（sync / 联网补全）后重跑一次即可，纯本地、秒级。
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from viewer.stock_names import BUILTIN  # noqa: E402
import os

DB_PATH = Path(os.environ.get("VIEWER_DB") or (ROOT / "viewer" / "app.db"))
OUT_PATH = ROOT / "scraper" / "stock_names_full.json"
EXTRA_PATH = ROOT / "scraper" / "stock_name_map.json"

_FULLWIDTH = str.maketrans(
    "ＡＢＣＤＥＦＧＨＩＪＫＬＭＮＯＰＱＲＳＴＵＶＷＸＹＺ"
    "ａｂｃｄｅｆｇｈｉｊｋｌｍｎｏｐｑｒｓｔｕｖｗｘｙｚ",
    "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    "abcdefghijklmnopqrstuvwxyz",
)


def _norm(name: str) -> str:
    """统一形态：全角字母 → 半角、去空白、去首尾标点。匹配双方都过一遍，避免"万科Ａ"匹配不上。"""
    s = (name or "").translate(_FULLWIDTH).strip()
    return "".join(ch for ch in s if ch.strip())


def main() -> int:
    idx: dict[str, str] = {}

    def put(name: str, code: str) -> None:
        nm = _norm(name)
        code = (code or "").strip()
        if len(nm) >= 2 and code.isdigit() and len(code) == 6:
            idx.setdefault(nm, code)

    # ① 内置表（人工维护的高频名）
    for code, (nm, _co) in BUILTIN.items():
        put(nm, code)

    # ② 手工补充表（历史遗留，可能含指数等非个股）
    if EXTRA_PATH.exists():
        try:
            for nm, code in json.loads(EXTRA_PATH.read_text(encoding="utf-8")).items():
                put(nm, str(code))
        except Exception as e:  # noqa: BLE001
            print(f"[warn] 读取 {EXTRA_PATH.name} 失败：{e}")

    # ③ 库内权威来源：stock_names 缓存表 + 龙虎榜证券简称（交易所口径）
    if DB_PATH.exists():
        con = sqlite3.connect(str(DB_PATH))
        con.row_factory = sqlite3.Row
        queries = (
            "SELECT code, name FROM stock_names WHERE name IS NOT NULL AND name<>''",
            "SELECT DISTINCT security_code AS code, security_name AS name FROM lhb_stocks "
            "WHERE security_name IS NOT NULL AND security_name<>''",
            "SELECT DISTINCT security_code AS code, security_name AS name FROM lhb_inst "
            "WHERE security_name IS NOT NULL AND security_name<>''",
        )
        for q in queries:
            try:
                for r in con.execute(q).fetchall():
                    put(r["name"], r["code"])
            except Exception:  # noqa: BLE001 老库缺表静默跳过
                continue
        con.close()
    else:
        print(f"[warn] 未找到 {DB_PATH}，仅导出内置表")

    OUT_PATH.write_text(
        json.dumps(idx, ensure_ascii=False, indent=0, sort_keys=True), encoding="utf-8"
    )
    # 名称长度分布，便于判断词典是否够用
    lens: dict[int, int] = {}
    for n in idx:
        lens[len(n)] = lens.get(len(n), 0) + 1
    print(f"已导出 {len(idx)} 个股票名 → {OUT_PATH}")
    print("名称长度分布：" + "、".join(f"{k}字×{v}" for k, v in sorted(lens.items())))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
