# -*- coding: utf-8 -*-
"""执行换库：备份 -> 建 trades_v2(剔除机器号) -> 事务内 DELETE trades + INSERT -> QA 复验。
绝对红线：只读用 mode=ro；写生产库前先一致性备份；swap 包在事务里（INSERT 失败回滚）。
"""
import sys, time, json, sqlite3
from pathlib import Path
sys.path.insert(0, str(Path(r"F:\AI Agent部署知识库\2026-09-22-11-49-42\stock-forum-scraper\tools").resolve()))
import apply_trades_v2 as A

ROOT = Path(r"F:\AI Agent部署知识库\2026-09-22-11-49-42\stock-forum-scraper")
PROD = ROOT / "viewer" / "app.db"
BOT_AUTHOR = "远凡aa量化交易系统"   # 唯一真机器号（量化交易系统），换库前剔除

def main():
    # ---------- 0) 备份生产库（一致性副本） ----------
    ts = time.strftime("%Y%m%d-%H%M%S")
    bak = PROD.with_name(f"{PROD.name}.bak-swap-{ts}")
    A.make_consistent_copy(PROD, bak)
    print(f"[backup] 生产库已备份 -> {bak}")

    # ---------- 1) 读 staging jsonl，剔除机器号 ----------
    records = A.load_staging(Path(A.DEFAULT_STAGING))
    before = len(records)
    records = [r for r in records if (r.get("author") or "") != BOT_AUTHOR]
    print(f"[staging] 读取 {before} 行，剔除机器号 '{BOT_AUTHOR}' -> {len(records)} 行")

    # ---------- 2) 建 trades_v2 并导入（复用 apply 的 import 逻辑） ----------
    conn = A.open_rw(PROD)
    try:
        A.ensure_trades_v2(conn, drop=True)
        stats = A.import_staging(conn, records, fill_times=True)
        print(f"[import] {stats}")
        v2_rows = conn.execute("SELECT COUNT(*) FROM trades_v2").fetchone()[0]
        v2_authors = conn.execute("SELECT COUNT(DISTINCT author) FROM trades_v2").fetchone()[0]
        print(f"[import] trades_v2 当前: rows={v2_rows} authors={v2_authors}")

        # ---------- 3) 换库：事务内 DELETE trades + INSERT（显式 23 列） ----------
        tcols = [c[1] for c in conn.execute("PRAGMA table_info(trades)")]
        assert len(tcols) == 23, f"trades 列数异常: {len(tcols)}"
        col_list = ",".join(tcols)
        conn.execute("BEGIN")
        try:
            conn.execute("DELETE FROM trades")
            conn.execute(f"INSERT INTO trades ({col_list}) SELECT {col_list} FROM trades_v2")
            conn.execute("COMMIT")
        except Exception as e:
            conn.execute("ROLLBACK")
            raise RuntimeError(f"swap 失败已回滚: {e}")
        print(f"[swap] trades 已替换为 trades_v2（{v2_rows} 行；剔除机器号后实装 {v2_rows} 行）")
    finally:
        conn.close()

    # ---------- 4) QA 复验（只读） ----------
    rc = sqlite3.connect(f"file:{PROD}?mode=ro", uri=True); rc.row_factory = sqlite3.Row
    rep = {}
    rep["trades_rows"] = rc.execute("SELECT COUNT(*) n FROM trades").fetchone()["n"]
    rep["trades_authors"] = rc.execute("SELECT COUNT(DISTINCT author) n FROM trades").fetchone()["n"]
    rep["trades_stocks"] = rc.execute("SELECT COUNT(DISTINCT symbol) n FROM trades WHERE symbol IS NOT NULL AND symbol<>''").fetchone()["n"]
    rep["site"] = dict(A._dist(rc, "trades", "site"))
    rep["action"] = dict(A._dist(rc, "trades", "action"))
    rep["post_time_distinct"] = rc.execute("SELECT COUNT(DISTINCT post_time) n FROM trades").fetchone()["n"]
    rep["post_time_minmax"] = [r[0] for r in rc.execute("SELECT MIN(post_time),MAX(post_time) FROM trades").fetchone()]
    rep["null_symbol"] = rc.execute("SELECT COUNT(*) n FROM trades WHERE symbol IS NULL OR symbol=''").fetchone()["n"]
    rep["non6"] = rc.execute("SELECT COUNT(*) n FROM trades WHERE symbol IS NOT NULL AND symbol<>'' AND NOT (symbol GLOB '[0-9][0-9][0-9][0-9][0-9][0-9]' AND LENGTH(symbol)=6)").fetchone()["n"]
    rep["dirty_000001"] = rc.execute("SELECT symbol_name FROM trades WHERE symbol='000001' LIMIT 1").fetchone()["symbol_name"]
    rep["has_000688"] = rc.execute("SELECT COUNT(*) n FROM trades WHERE symbol='000688'").fetchone()["n"]
    rep["has_bot"] = rc.execute("SELECT COUNT(*) n FROM trades WHERE author=?",(BOT_AUTHOR,)).fetchone()["n"]
    # 集中度
    c = A._stock_concentration(rc, "trades", 5)
    rep["top1_pct"]=c["top1_pct"]; rep["top5_pct"]=c["top5_pct"]
    rep["top5"]=[(s,nm,n,p) for s,nm,n,p in c["top"][:5]]
    # join 健康度：trades.author -> players.name
    jr = rc.execute(
        "SELECT COUNT(DISTINCT t.author) a, "
        "SUM(CASE WHEN p.name IS NOT NULL THEN 1 ELSE 0 END) matched_rows, "
        "COUNT(*) total_rows FROM trades t LEFT JOIN players p ON p.name=t.author").fetchone()
    rep["join_authors_total"]=jr["a"]; rep["join_matched_rows"]=jr["matched_rows"]; rep["join_total_rows"]=jr["total_rows"]
    rep["join_unmatched_authors"]=jr["a"] - rc.execute(
        "SELECT COUNT(DISTINCT t.author) n FROM trades t WHERE t.author IN (SELECT name FROM players)").fetchone()["n"]
    rc.close()
    rep["backup"] = str(bak)
    print("\n========== QA 复验（换库后 trades） ==========")
    print(json.dumps(rep, ensure_ascii=False, indent=2))
    Path(ROOT / "tools" / f"_swap_qa_{ts}.json").write_text(json.dumps(rep, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[qa] 报告已存 tools/_swap_qa_{ts}.json")

if __name__ == "__main__":
    main()
