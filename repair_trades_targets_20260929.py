"""修复 trades 表的「标的」脏数据（2026-09-29 王总反馈：标的显示文本碎片、点不开）。

背景：
    旧淘股吧解析器用「正则抓 2~6 汉字」猜股票名，把正文碎片写进了 trades.symbol_name：
    SELL「第三次」／SELL「累计」／BUY「全仓怼单标起」／SELL「但是」… 这些行既没有 6 位代码
    （前端"名称+代码"点不开详情），又污染「选手实盘」面板。共 65 条真实淘股吧行中，32 条无代码、
    39 条名字不是真股票名。

本脚本按**新词典驱动解析器**重算每条帖子的标的，做法（按 post 分组）：
    1) 已有代码的行保留（代码锚定最可信），只把被截断的名字换成词典规范名（"金健"→金健米业）；
    2) 无代码行：若本次重算能对上动作 → 补代码+规范名；否则名字在词典里 → 补代码；
       都不是 → **判定为旧解析器的臆造行，删除**（前端不再出现"点开没内容"的假标的）；
    3) 重算出来但库里没有的真交易 → 补插（同一帖子里原来只记了 1 条、实际有 3 笔的情况）。

用法：
    python repair_trades_targets_20260929.py            # 演练（只打印，不改库）
    python repair_trades_targets_20260929.py --apply    # 执行（先自动备份 app.db）
"""
from __future__ import annotations

import shutil
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scraper import name_match as nm  # noqa: E402
from scraper.parsers.taoguba import TaogubaParser  # noqa: E402

DB = ROOT / "viewer" / "app.db"


def main() -> int:
    apply = "--apply" in sys.argv
    con = sqlite3.connect(str(DB))
    con.row_factory = sqlite3.Row
    parser = TaogubaParser({})

    rows = con.execute(
        "SELECT * FROM trades WHERE site='taoguba' AND post_url NOT LIKE '%example.local%' "
        "ORDER BY id"
    ).fetchall()
    groups: dict[str, list[sqlite3.Row]] = {}
    for r in rows:
        groups.setdefault(r["post_id"] or r["post_url"] or str(r["id"]), []).append(r)

    stats = {"name_fixed": 0, "linked": 0, "code_filled": 0, "deleted": 0, "added": 0}
    plan: list[str] = []

    for _pid, grp in groups.items():
        raw = next((g["raw_text"] for g in grp if g["raw_text"]), "") or ""
        cands = parser._extract_trades(raw)
        # 已被库中行"代表"的 (动作, 代码)：既包括本就有代码的行，也包括本次补码成功的行。
        # 最终只补插这里没出现过的重算结果，避免"补码 + 补录"同一条重复入库。
        represented: set[tuple[str, str]] = set()
        for g in grp:
            if (g["symbol"] or "").strip():
                represented.add((g["action"] or "", (g["symbol"] or "").strip()))

        for r in grp:
            sym, nm_old = (r["symbol"] or "").strip(), (r["symbol_name"] or "").strip()
            canonical = nm.code_name(sym) if sym else None
            if sym:
                if canonical and nm_old != canonical:
                    plan.append(f"改名 #{r['id']} {r['author']} {r['action']} "
                                f"{nm_old!r}→{canonical!r}（{sym}）")
                    stats["name_fixed"] += 1
                    if apply:
                        try:
                            con.execute("UPDATE trades SET symbol_name=? WHERE id=?", (canonical, r["id"]))
                        except sqlite3.IntegrityError:
                            pass
                continue

            # 无代码：先看能否对上重算结果（同一动作）
            hit = next((c for c in cands if c["action"] == r["action"]), None)
            if hit:
                plan.append(f"补码 #{r['id']} {r['author']} {r['action']} "
                            f"{nm_old!r}→{hit['symbol_name']!r}({hit['symbol']})")
                stats["linked"] += 1
                represented.add((hit["action"], hit["symbol"]))
                if apply:
                    try:
                        con.execute("UPDATE trades SET symbol=?, symbol_name=? WHERE id=?",
                                    (hit["symbol"], hit["symbol_name"], r["id"]))
                    except sqlite3.IntegrityError:
                        con.execute("DELETE FROM trades WHERE id=?", (r["id"],))
                        stats["deleted"] += 1
                continue

            if nm_old and nm.is_known(nm_old):
                code = nm.index()[nm.normalize(nm_old)]
                plan.append(f"补码(名) #{r['id']} {r['author']} {r['action']} {nm_old!r}({code})")
                stats["code_filled"] += 1
                represented.add((r["action"] or "", code))
                if apply:
                    try:
                        con.execute("UPDATE trades SET symbol=? WHERE id=?", (code, r["id"]))
                    except sqlite3.IntegrityError:
                        pass
                continue

            plan.append(f"删除 #{r['id']} {r['author']} {r['action']} {nm_old!r}"
                        f" ← 旧解析器臆造（原文无此标的）：{raw[:34]}…")
            stats["deleted"] += 1
            if apply:
                con.execute("DELETE FROM trades WHERE id=?", (r["id"],))

        # 重算出的真交易，库里还没有的补插
        base = grp[0]
        for c in cands:
            key = (c["action"], c["symbol"])
            if key in represented:
                continue
            if apply:
                cur = con.execute(
                    "INSERT OR IGNORE INTO trades (site, author, post_id, post_url, post_time, "
                    "action, symbol, symbol_name, price, quantity, raw_text, buy_time, buy_price, "
                    "sell_time, sell_price, access_level) "
                    "VALUES ('taoguba',?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (base["author"], base["post_id"], base["post_url"], base["post_time"],
                     c["action"], c["symbol"], c["symbol_name"], c["price"], c["quantity"], raw,
                     "", None, "", None, base["access_level"] or "free"),
                )
                if not cur.rowcount:
                    continue
                    # INSERT OR IGNORE 被唯一索引挡下（同帖同标的同动作已存在）→ 不计入
            plan.append(f"补录 + {base['author']} {c['action']} "
                        f"{c['symbol_name']}({c['symbol']}) ← {raw[:30]}…")
            stats["added"] += 1
            represented.add(key)

    print(f"===== 修复计划（{len(plan)} 项）=====")
    for line in plan:
        print("  " + line)
    print("\n===== 汇总 =====")
    print(f"  改名（截断名→词典规范名）: {stats['name_fixed']}")
    print(f"  补代码（对上重算结果）    : {stats['linked']}")
    print(f"  补代码（按名字反查）      : {stats['code_filled']}")
    print(f"  删除臆造行                : {stats['deleted']}")
    print(f"  补录漏记的真交易          : {stats['added']}")

    if not apply:
        print("\n[演练模式] 未改动数据库。加 --apply 执行（会自动备份）。")
        con.close()
        return 0

    bak = DB.with_name(f"app.db.bak_targetfix_{datetime.now():%Y%m%d-%H%M%S}")
    con.commit()
    con.close()
    shutil.copy2(DB, bak)
    print(f"\n已执行。备份：{bak.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
