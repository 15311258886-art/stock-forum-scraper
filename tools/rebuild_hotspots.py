"""离线重算「近期热点」（模块二）。

为什么要这个脚本
----------------
热点卡的数据链是：overseas_news（原始条目） → build_hotspots() 聚合 → overseas_hotspots。
当 lexicon 的映射表或 hotspots 的输出字段发生变化时，界面要立刻反映新结构，
但**重新联网抓取**既需要 VPN / API Key，又会清空并重灌新闻表——代价过大。

本脚本只用库中**已存在的** overseas_news 条目重算聚合结果，不联网、不抓取：
    overseas_news（只读）  →  build_hotspots  →  overseas_hotspots（覆盖写，派生数据）

安全性：只覆盖写 overseas_hotspots（纯派生缓存，随时可由 news 重建），
        绝不改动 overseas_news / posts / trades 等原始抓取数据。

用法：
    .\\.venv\\Scripts\\python.exe tools\\rebuild_hotspots.py
报告写入 tools/_qa_rebuild.txt（UTF-8），避免 Windows 控制台编码乱码。
"""
from __future__ import annotations

import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from overseas import hotspots as hs  # noqa: E402
from viewer.db import DB_PATH, Database  # noqa: E402

REPORT = ROOT / "tools" / "_qa_rebuild.txt"


def _line(out: list[str], s: str = "") -> None:
    out.append(s)


def _backup() -> Path:
    """用 SQLite 官方 backup API 做一致性快照。

    直接拷贝 app.db 文件是不安全的：本库开着 WAL，最新写入可能还在 app.db-wal 里，
    拷出来的 .db 会缺失这部分数据。backup() 由 SQLite 自己处理页级一致性。
    """
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    dst = ROOT / "viewer" / f"app.db.bak-{stamp}"
    src = sqlite3.connect(str(DB_PATH))
    try:
        out = sqlite3.connect(str(dst))
        try:
            src.backup(out)
        finally:
            out.close()
    finally:
        src.close()
    return dst


def main() -> int:
    out: list[str] = []
    bak = _backup()
    _line(out, f"[BACKUP] 一致性快照 → {bak.name}（{bak.stat().st_size} 字节）")
    db = Database()

    news = [dict(r) for r in db.conn.execute("SELECT * FROM overseas_news").fetchall()]
    before = [dict(r) for r in db.conn.execute("SELECT * FROM overseas_hotspots").fetchall()]
    _line(out, f"[IN]  overseas_news 条目 = {len(news)}")
    _line(out, f"[IN]  overseas_hotspots 重算前 = {len(before)}")

    if not news:
        _line(out, "[STOP] 库中无海外新闻条目，无法重算。")
        _line(out, "       请先在界面点「刷新海外舆情(联网)」或「载入示例」，再运行本脚本。")
        REPORT.write_text("\n".join(out), encoding="utf-8")
        db.close()
        return 2

    # 库中列是否已含新字段（由 db.py 的兼容迁移负责补齐）
    cols = [r["name"] for r in db.conn.execute("PRAGMA table_info(overseas_hotspots)").fetchall()]
    _line(out, f"[SCHEMA] overseas_hotspots 列 = {','.join(cols)}")
    need = ("sectors_up_detail", "sectors_down_detail", "decision")
    missing = [c for c in need if c not in cols]
    if missing:
        _line(out, f"[STOP] 缺少新列 {missing}，请先完成任务二（db.py 建表/迁移）再重跑。")
        REPORT.write_text("\n".join(out), encoding="utf-8")
        db.close()
        return 3

    try:
        hotspots = hs.build_hotspots(db, news)
    except Exception as exc:  # noqa: BLE001
        _line(out, f"[FAIL] build_hotspots 抛异常：{type(exc).__name__}: {exc}")
        import traceback
        _line(out, traceback.format_exc())
        REPORT.write_text("\n".join(out), encoding="utf-8")
        db.close()
        return 4

    _line(out, f"[OUT] 重算后热点 = {len(hotspots)} 张")
    _line(out, "")

    no_detail = []
    bad_json = []
    for h in hotspots:
        for k in ("sectors_up_detail", "sectors_down_detail"):
            v = h.get(k) or ""
            try:
                json.loads(v)
            except Exception:
                bad_json.append((h.get("key"), k))
        if not json.loads(h.get("sectors_up_detail") or "[]") and \
           not json.loads(h.get("sectors_down_detail") or "[]"):
            no_detail.append(h.get("key"))

    for i, h in enumerate(hotspots, 1):
        up = json.loads(h.get("sectors_up_detail") or "[]")
        dn = json.loads(h.get("sectors_down_detail") or "[]")
        n_up = sum(len(x.get("symbols") or []) for x in up)
        n_dn = sum(len(x.get("symbols") or []) for x in dn)
        _line(out, f"#{i} {str(h.get('title'))[:44]}")
        _line(out, f"    heat={h.get('heat')}  amp={h.get('amplification_factor')}  conf={h.get('confidence')}")
        _line(out, f"    领域: 上涨 {len(up)} 个/公司 {n_up} 家 ｜ 下跌 {len(dn)} 个/公司 {n_dn} 家")
        _line(out, f"    结论: {str(h.get('decision'))[:160]}")
        if i <= 3:
            _line(out, f"    明细样本: {json.dumps(up[:2], ensure_ascii=False)[:300]}")
        _line(out, "")

    _line(out, f"[QA] JSON 解析失败字段 = {bad_json or '无'}")
    _line(out, f"[QA] 无任何领域明细的热点 = {no_detail or '无'}")

    REPORT.write_text("\n".join(out), encoding="utf-8")
    db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
