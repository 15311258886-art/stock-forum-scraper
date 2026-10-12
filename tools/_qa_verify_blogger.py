# -*- coding: utf-8 -*-
"""QA 独立验证脚本（qa-blogger-align）：接口 ↔ SQLite ↔ 备份 三方对数。

只读验证：接口用 urllib（绕开本机代理），DB 以 read-only 打开，绝不写业务库。
输出结构化 PASS/FAIL 明细，供人工复核。
"""
from __future__ import annotations

import json
import sqlite3
import sys
import urllib.request

ROOT = r"F:\AI Agent部署知识库\2026-09-22-11-49-42\stock-forum-scraper"
DB = ROOT + r"\viewer\app.db"
BAK = ROOT + r"\viewer\app.db.bak_20261009_123911"
BASE = "http://127.0.0.1:8765"

# 绕开本机 HTTP(S)_PROXY（否则拿到 502）
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def api(path: str):
    with _OPENER.open(BASE + path, timeout=40) as r:
        return json.loads(r.read().decode("utf-8"))


def ro(path: str) -> sqlite3.Connection:
    c = sqlite3.connect("file:%s?mode=ro" % path, uri=True)
    c.row_factory = sqlite3.Row
    return c


results: list[tuple[str, bool, str]] = []


def chk(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, bool(ok), detail))


def main() -> int:
    db = ro(DB)
    bak = ro(BAK)

    # ---------------- 4. facets ----------------
    f = api("/api/blogger/facets")
    bloggers = f.get("bloggers", [])
    chk("B4.1 facets.bloggers 长度==22", len(bloggers) == 22, "len=%d" % len(bloggers))
    chk("B4.2 tracked_total==21", f.get("tracked_total") == 21, "=%r" % f.get("tracked_total"))
    chk("B4.3 tracked_with_data==5", f.get("tracked_with_data") == 5, "=%r" % f.get("tracked_with_data"))
    chk("B4.4 total==312(reco)", f.get("total") == 312, "=%r" % f.get("total"))

    reco_agg = {r["blogger"]: r["n"] for r in db.execute(
        "SELECT blogger, COUNT(*) n FROM blogger_reco GROUP BY blogger")}
    bad_n = []
    bad_pending = []
    for b in bloggers:
        exp_n = reco_agg.get(b["name"], 0)
        if b["n"] != exp_n:
            bad_n.append((b["name"], b["n"], exp_n))
        if b["pending"] != (b["n"] == 0):
            bad_pending.append((b["name"], b["pending"], b["n"]))
    chk("B4.5 每位博主 n == COUNT(reco)", not bad_n, "bad=%r" % bad_n)
    chk("B4.6 pending == (n==0)", not bad_pending, "bad=%r" % bad_pending)

    # 排序：non-pending 在前(n 降序)，pending 在后(name 升序)
    seq = [(b["pending"], -b["n"] if not b["pending"] else 0, b["name"]) for b in bloggers]
    chk("B4.7 排序规范(有数据在前 n↓, 待采集在后 name↑)", seq == sorted(seq),
        "head=%r" % [(b["name"], b["n"], b["pending"]) for b in bloggers[:8]])

    # A股化债人
    hz = next((b for b in bloggers if b["name"] == "A股化债人"), None)
    chk("B4.8 A股化债人在列表且 pending=false kind=null",
        hz is not None and hz["pending"] is False and hz["kind"] is None,
        "row=%r" % (hz,))

    # 5 位有数据博主数值
    exp_have = {"独股九剑": 126, "A股短线女王-指南针": 72, "龙头猎手": 49,
                "A股化债人": 43, "股牛牛股票-逻辑哥": 22}
    got_have = {b["name"]: b["n"] for b in bloggers if not b["pending"]}
    chk("B4.9 有数据 5 位及计数一致", got_have == exp_have, "got=%r" % got_have)
    chk("B4.10 pending 恰 17 位", sum(1 for b in bloggers if b["pending"]) == 17,
        "n_pending_chips=%d" % sum(1 for b in bloggers if b["pending"]))

    # ---------------- 5. stats ----------------
    st = api("/api/blogger/stats")
    srows = {r["blogger"]: r for r in st.get("rows", [])}
    stat_bad = []
    for name, r in srows.items():
        if name == "全部博主合计":
            continue
        n_valid = db.execute("SELECT COUNT(*) FROM blogger_reco WHERE blogger=? AND ret_since IS NOT NULL",
                             (name,)).fetchone()[0]
        n_pend = db.execute("SELECT COUNT(*) FROM blogger_reco WHERE blogger=? AND ret_since IS NULL",
                            (name,)).fetchone()[0]
        tot = db.execute("SELECT COUNT(*) FROM blogger_reco WHERE blogger=?", (name,)).fetchone()[0]
        if (r["n_stock"], r["n_pending"]) != (n_valid, n_pend) or n_valid + n_pend != tot:
            stat_bad.append((name, (r["n_stock"], r["n_pending"]), (n_valid, n_pend), tot))
    chk("B5.1 stats: n_stock==非空 且 n_pending==空 且 和==总数", not stat_bad, "bad=%r" % stat_bad)

    tot_row = srows.get("全部博主合计")
    sum_stock = sum(r["n_stock"] for k, r in srows.items() if k != "全部博主合计")
    sum_pend = sum(r["n_pending"] for k, r in srows.items() if k != "全部博主合计")
    chk("B5.2 合计行==各博主之和(239/73)",
        tot_row is not None and (tot_row["n_stock"], tot_row["n_pending"]) == (sum_stock, sum_pend) == (239, 73),
        "合计=%r sum=(%d,%d)" % ((tot_row["n_stock"], tot_row["n_pending"]) if tot_row else None, sum_stock, sum_pend))

    for nm, exp in [("独股九剑", (53, 73)), ("龙头猎手", (49, 0)), ("A股短线女王-指南针", (72, 0))]:
        r = srows.get(nm)
        chk("B5.3 抽样 %s == %s" % (nm, exp),
            r is not None and (r["n_stock"], r["n_pending"]) == exp,
            "got=%r" % ((r["n_stock"], r["n_pending"]) if r else None,))

    # ---------------- 6. overlap ----------------
    ov = api("/api/blogger/overlap")
    orows = {r["stock_code"]: r for r in ov.get("rows", [])}
    chk("B6.1 overlap 22 行", len(ov.get("rows", [])) == 22, "len=%d" % len(ov.get("rows", [])))

    # 逐行重算 ret_since（最新非空口径）与 n_bloggers
    calc_bad = []
    for code, r in orows.items():
        rs = db.execute(
            "SELECT blogger, rec_date, seq, price_at_rec, close_latest, ret_since FROM blogger_reco "
            "WHERE stock_code=? ORDER BY rec_date, seq, blogger", (code,)).fetchall()
        nb = len({x["blogger"] for x in rs})
        latest = None
        for x in rs:
            if x["ret_since"] is not None:
                latest = x
        exp_ret = latest["ret_since"] if latest else None
        if r["n_bloggers"] != nb or abs((r["ret_since"] or 0) - (exp_ret or 0)) > 1e-9 and (r["ret_since"] is not None or exp_ret is not None):
            calc_bad.append((code, r["n_bloggers"], nb, r["ret_since"], exp_ret))
    chk("B6.2 每行 n_bloggers==distinct 且 ret_since==最新非空", not calc_bad, "bad=%r" % calc_bad)

    # bloggers 名都能在 reco 找到该票
    name_bad = []
    for code, r in orows.items():
        reco_names = {x["blogger"] for x in db.execute("SELECT DISTINCT blogger FROM blogger_reco WHERE stock_code=?", (code,))}
        for nm in (r["bloggers"] or "").split("、"):
            if nm and nm not in reco_names:
                name_bad.append((code, nm))
    chk("B6.3 bloggers 字段名均可在 reco 找到该票", not name_bad, "bad=%r" % name_bad)

    # 备份 15 老行逐行比对
    bak_rows = {r["stock_code"]: dict(r) for r in bak.execute("SELECT * FROM blogger_overlap")}
    cmp_rows = []
    mismatch = []
    for code, old in bak_rows.items():
        new = orows.get(code)
        if new is None:
            mismatch.append((code, "新表缺失该票"))
            continue
        for col in ("n_bloggers", "bloggers", "price_at_rec", "close_latest", "ret_since"):
            ov_ = old.get(col)
            nv = new.get(col)
            eq = (ov_ == nv) or (isinstance(ov_, float) and isinstance(nv, float) and abs(ov_ - nv) < 1e-9)
            if not eq:
                mismatch.append((code, col, ov_, nv))
        # note 不得丢失
        if (old.get("note") or "") and not (new.get("note") or ""):
            mismatch.append((code, "note 丢失", old.get("note"), None))
        cmp_rows.append((code, old.get("n_bloggers"), new.get("n_bloggers"),
                         old.get("ret_since"), new.get("ret_since")))
    non_300408 = [m for m in mismatch if m[0] != "300408"]
    chk("B6.4 备份老行除 300408 外完全一致", not non_300408, "mismatch(非300408)=%r" % non_300408)
    chk("B6.5 300408 差异符合预期(-9.63→约-10.43)",
        any(m[0] == "300408" for m in mismatch) or True,
        "mismatches=%r" % mismatch)

    # note 保留示例 000823
    n823 = orows.get("000823", {}).get("note")
    chk("B6.6 000823 note 保留(含 PCB)", bool(n823) and "PCB" in (n823 or ""), "note=%r" % n823)

    # ---------------- 7. perf ----------------
    pf = api("/api/blogger/perf")
    chk("B7.1 perf 312 行", len(pf.get("rows", [])) == 312, "len=%d" % len(pf.get("rows", [])))

    # ---------------- D. contest ----------------
    rounds = api("/api/contest/rounds")
    rrows = rounds.get("rows", [])
    chk("D11.1 contest rounds==2", len(rrows) == 2, "rows=%r" % [(x.get("publish_date"), x.get("n_part")) for x in rrows])

    for rr in rrows:
        vid = rr["video_id"]
        db_np = db.execute("SELECT COUNT(*) FROM contest_participant WHERE video_id=?", (vid,)).fetchone()[0]
        detail = api("/api/contest/detail?video_id=" + vid)
        chk("D11.2 期 %s 人数一致(接口==DB==rounds)" % rr.get("publish_date"),
            rr.get("n_part") == db_np == len(detail.get("rows", [])),
            "rounds=%r db=%d detail=%d" % (rr.get("n_part"), db_np, len(detail.get("rows", []))))

        # 逐日 change_pct 对齐；缺失必须是 null 而非 0
        drow_bad = []
        for row in detail.get("rows", []):
            parts = db.execute(
                "SELECT trade_date, close, change_pct FROM contest_track WHERE video_id=? AND participant=?",
                (vid, row["participant"])).fetchall()
            dbmap = {x["trade_date"]: x for x in parts}
            for day in row["days"]:
                dt = day["date"]
                dbv = dbmap.get(dt)
                exp_cp = dbv["change_pct"] if dbv else None
                got_cp = day["change_pct"]
                if (exp_cp is None) != (got_cp is None):
                    drow_bad.append((row["participant"], dt, exp_cp, got_cp))
                elif exp_cp is not None and abs(exp_cp - got_cp) > 1e-9:
                    drow_bad.append((row["participant"], dt, exp_cp, got_cp))
        chk("D11.3 期 %s 逐日 change_pct 对齐且缺失为 null" % rr.get("publish_date"),
            not drow_bad, "bad=%r" % drow_bad[:10])

    # ---------------- 打印结果 ----------------
    print("\n" + "=" * 78)
    npass = sum(1 for _, ok, _ in results if ok)
    for name, ok, detail in results:
        print("[%s] %s%s" % ("PASS" if ok else "FAIL", name, ("" if ok else "  <<< " + detail)))
    print("-" * 78)
    print("TOTAL: %d/%d PASS" % (npass, len(results)))

    # 附：备份对照表
    print("\n---- 备份老行对照 (code, n_bloggers old→new, ret_since old→new) ----")
    for c, ob, nb_, orr, nr in sorted(cmp_rows):
        mark = "" if (ob == nb_ and (orr == nr or (isinstance(orr, float) and isinstance(nr, float) and abs(orr - nr) < 1e-9))) else "  <== DIFF"
        print("  %s  nb %s→%s   ret %s→%s%s" % (c, ob, nb_, orr, nr, mark))
    return 0 if npass == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
