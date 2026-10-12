# -*- coding: utf-8 -*-
"""股票论坛查看器 —— 数据自动更新编排脚本（本机定时任务的执行体）。

做八件事：
  1) 跑全站回填（复用 run_backfill_local.py：eastmoney/sina/ths/taoguba/xueqiu + 选手池重建）
  2) 跑龙虎榜抓取（lhb_fetch.py，东财免鉴权接口；2026-09-25 重建并纳入）
  3) 跑权威股东抓取（holders_fetch.py，东财 F10 十大流通股东 + 户数）
  4) 对股东数据重打性质标签（holders_tag.py，隔离实控人/家族，保留真牛散）
  5) 跑板块扩散分析（lhb_sector_link.py，龙虎榜→题材板块→T+1/T+3 验证）
  6) 跑主线研判链（sector_chain.py，方向→龙头→梯队→分支；2026-10-09 纳入，
     此前漏在编排之外，导致 sector_chain 表只能在服务内手动跑）
  7) 对 app.db 做 WAL checkpoint(TRUNCATE)，避免 app.db-wal 无限膨胀
  8) 把本次结果写成 auto_update_status.json（供巡检/汇报读取），并追加一行到 auto_update.log

用法：
  .venv\\Scripts\\python.exe auto_update.py            # 跑全部站点
  .venv\\Scripts\\python.exe auto_update.py --sites xueqiu   # 只跑雪球（调试）

设计要点（2026-09-25）：
  - 雪球自「浏览器指纹修复」后**不再需要 cookie**（已验证：移走 cookies.txt 仍能抓到
    news/trades），故本脚本不依赖 cookies.txt；若文件存在则自动被 browser_fetch 读取，无害。
  - 全程只调用本工程已验证的链路，不新增依赖。
  - 任何异常都会写进 status 文件，确保巡检可发现失败。
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import subprocess
import sys
import time
from datetime import datetime

BASE = os.path.dirname(os.path.abspath(__file__))
PY = os.path.join(BASE, ".venv", "Scripts", "python.exe")
DB = os.path.join(BASE, "viewer", "app.db")
STATUS = os.path.join(BASE, "auto_update_status.json")
LOG = os.path.join(BASE, "auto_update.log")
TABLES = ["players", "positions", "trades", "news", "posts", "lhb_stocks", "lhb_seats",
          "stock_names", "lhb_depts", "lhb_inst", "stock_holders", "stock_holder_num",
          "lhb_sector_focus"]


def _counts() -> dict:
    out: dict = {}
    try:
        con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=20)
        cur = con.cursor()
        for t in TABLES:
            try:
                cur.execute(f"SELECT COUNT(*) FROM {t}")
                out[t] = cur.fetchone()[0]
            except Exception:  # noqa: BLE001
                pass
        con.close()
    except Exception as e:  # noqa: BLE001
        out["_err"] = str(e)[:200]
    return out


def _wal_checkpoint():
    """WAL 回收，并**如实**返回结果。

    修正（2026-09-25）：PRAGMA wal_checkpoint(TRUNCATE) 会返回元组
    (busy, log_pages, checkpointed_pages)，只有 busy=0 才代表真正截断成功。
    此前实现只 execute 而不读返回值、恒返回 True —— 属「假成功」：
    查看器开着（持有 DB 连接）时 WAL 实际无法截断，却被报告成成功。
    """
    try:
        con = sqlite3.connect(DB, timeout=30)
        row = con.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        con.close()
        if row and int(row[0]) != 0:
            return (f"busy：有连接占用（查看器在运行），WAL 未截断 "
                    f"log={row[1]} ckpt={row[2]}")
        return True
    except Exception as e:  # noqa: BLE001
        return str(e)[:200]


def main() -> int:
    ap = argparse.ArgumentParser(description="股票论坛查看器 数据自动更新")
    ap.add_argument("--sites", default="", help="逗号分隔站点；留空=全部")
    args = ap.parse_args()

    started = datetime.now()
    print(f"[auto_update] 开始 {started:%Y-%m-%d %H:%M:%S}  sites={args.sites or 'ALL'}", flush=True)
    before = _counts()

    cmd = [PY, os.path.join(BASE, "run_backfill_local.py")]
    if args.sites:
        cmd += ["--sites", args.sites]

    run = {"returncode": None, "tail": ""}
    try:
        r = subprocess.run(
            cmd, cwd=BASE, capture_output=True, text=True,
            encoding="utf-8", errors="ignore", timeout=3600,
        )
        run["returncode"] = r.returncode
        run["tail"] = "\n".join((r.stdout or "").splitlines()[-25:])
    except Exception as e:  # noqa: BLE001
        run["error"] = f"{type(e).__name__}: {e}"[:300]

    # ---- 龙虎榜（独立链路，2026-09-25 重建并纳入本编排）----
    # 原 lhb_fetch.py 丢失导致龙虎榜长期停更；现每次随全量更新一并抓取（幂等）。
    lhb = {"returncode": None, "tail": ""}
    try:
        r2 = subprocess.run(
            [PY, os.path.join(BASE, "lhb_fetch.py"), "--days", "6"],
            cwd=BASE, capture_output=True, text=True,
            encoding="utf-8", errors="ignore", timeout=900,
        )
        lhb["returncode"] = r2.returncode
        lhb["tail"] = "\n".join((r2.stdout or "").splitlines()[-8:])
    except Exception as e:  # noqa: BLE001
        lhb["error"] = f"{type(e).__name__}: {e}"[:300]

    # ---- 权威股东：抓取(F10) + 风险隔离打标 ----
    # holders_fetch 抓十大流通股东/十大股东/户数；holders_tag 给每条打 holder_kind，
    # 把「实控人/家族」与「二级市场牛散」分开，避免污染牛散/游资数据（王总明确要求）。
    holders = {"returncode": None, "tail": ""}
    try:
        r3 = subprocess.run(
            [PY, os.path.join(BASE, "holders_fetch.py"), "--limit", "600"],
            cwd=BASE, capture_output=True, text=True,
            encoding="utf-8", errors="ignore", timeout=1800,
        )
        holders["returncode"] = r3.returncode
        holders["tail"] = "\n".join((r3.stdout or "").splitlines()[-8:])
    except Exception as e:  # noqa: BLE001
        holders["error"] = f"{type(e).__name__}: {e}"[:300]
    holders["tag_rc"] = None
    try:
        r4 = subprocess.run(
            [PY, os.path.join(BASE, "holders_tag.py")],
            cwd=BASE, capture_output=True, text=True,
            encoding="utf-8", errors="ignore", timeout=600,
        )
        holders["tag_rc"] = r4.returncode
        holders["tag_tail"] = "\n".join((r4.stdout or "").splitlines()[-6:])
    except Exception as e:  # noqa: BLE001
        holders["tag_error"] = f"{type(e).__name__}: {e}"[:300]

    # ---- 板块扩散（龙虎榜 → 题材板块 → T+1/T+3 验证）----
    # 缺 T+1 行的最新交易日也照常入库（标 pending），避免断档。
    sector = {"returncode": None, "tail": ""}
    try:
        r5 = subprocess.run(
            [PY, os.path.join(BASE, "lhb_sector_link.py"), "--days", "7"],
            cwd=BASE, capture_output=True, text=True,
            encoding="utf-8", errors="ignore", timeout=1800,
        )
        sector["returncode"] = r5.returncode
        sector["tail"] = "\n".join((r5.stdout or "").splitlines()[-8:])
    except Exception as e:  # noqa: BLE001
        sector["error"] = f"{type(e).__name__}: {e}"[:300]

    # ---- 主线研判链（方向→龙头→梯队→分支）----
    # 2026-10-12 修复：下方 status["sector_chain"]=chain 引用了从未赋值的 chain，
    # 一执行到状态落盘就 NameError、整个日更静默断档（状态文件的 ended 永远停在旧日期）。
    # 此处补上真正跑 sector_chain.py 的子进程，与其它步骤同口径（超时 1800s、失败不阻断后续）。
    chain = {"returncode": None, "tail": ""}
    try:
        r6 = subprocess.run(
            [PY, os.path.join(BASE, "sector_chain.py")],
            cwd=BASE, capture_output=True, text=True,
            encoding="utf-8", errors="ignore", timeout=1800,
        )
        chain["returncode"] = r6.returncode
        chain["tail"] = "\n".join((r6.stdout or "").splitlines()[-8:])
    except Exception as e:  # noqa: BLE001
        chain["error"] = f"{type(e).__name__}: {e}"[:300]

    # ---- 本地库备份（规格书 D1：每日 1 份留 7 天 + 每周 1 份留 4 周 + 备份前完整性校验）----
    # 用 sqlite3 在线备份 API（服务在跑、有 WAL 时文件拷贝会拿到不一致副本）。
    # 非致命：备份失败只记状态，不阻断日更（数据抓取优先）。
    backup = {"ok": None, "msg": ""}
    try:
        from tools.db_backup import daily_backup  # noqa: E402  —— 延迟导入，避免启动期耦合
        b_ok, b_msg, _ = daily_backup()
        backup["ok"] = b_ok
        backup["msg"] = b_msg[:300]
    except Exception as e:  # noqa: BLE001
        backup["ok"] = False
        backup["msg"] = f"{type(e).__name__}: {e}"[:300]

    time.sleep(2)  # 等 WAL 落盘
    ck = _wal_checkpoint()
    after = _counts()
    delta = {
        k: (after[k] - before.get(k, 0))
        for k in after
        if isinstance(after.get(k), int) and isinstance(before.get(k), int)
    }

    ended = datetime.now()
    status = {
        "started": started.strftime("%Y-%m-%d %H:%M:%S"),
        "ended": ended.strftime("%Y-%m-%d %H:%M:%S"),
        "seconds": int((ended - started).total_seconds()),
        "ok": run.get("returncode") == 0,
        "returncode": run.get("returncode"),
        "error": run.get("error"),
        "wal_checkpoint": ck,
        "before": before,
        "after": after,
        "delta": delta,
        "lhb": lhb,
        "sector_chain": chain,
        "backup": backup,
        "log_tail": run.get("tail", ""),
        "note": "雪球无需 cookie（指纹修复后匿名可抓）；龙虎榜已纳入本编排。查看器需关窗重开才能看到新数据。",
    }
    try:
        with open(STATUS, "w", encoding="utf-8") as f:
            json.dump(status, f, ensure_ascii=False, indent=2)
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(f"\n=== {started:%Y-%m-%d %H:%M:%S}~{ended:%H:%M:%S} ok={status['ok']} "
                    f"sec={status['seconds']} delta={delta} wal={ck} ===\n")
    except Exception as e:  # noqa: BLE001
        print(f"[auto_update] 写状态文件失败：{e}", flush=True)

    print("[auto_update] 完成 " + json.dumps(
        {k: status[k] for k in ("started", "ended", "seconds", "ok", "delta")},
        ensure_ascii=False), flush=True)
    print(f"[auto_update] 龙虎榜 returncode={lhb.get('returncode')}", flush=True)
    return 0 if status["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
