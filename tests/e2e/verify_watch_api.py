# -*- coding: utf-8 -*-
"""关注池改造（2026-10-14）API 级独立回归 —— QA 严过关

独立取证原则：期望值不来自被测接口自身，而来自：
  * 任务书（team-lead）给定的实况与预期；
  * QA 在进程内独立复算 bili_scan.signals_for_watch（只读 bars 缓存 + 只读库）得到的信号表。

阶段：
  A. 生产库只读快照（改动前基线）               -> _qa_watch_work/snap_prod_before.json
  B. 真实库只读断言（端口 8792）：/api/watch_list 信号 / 多组 / 分组计数
  C. 隔离副本写链路往返（端口 8793，VIEWER_DB）：add 追加组 / group_remove / reorder
     —— 项目 sanctioned 隔离机制（db.py 注释明示写接口测试应对副本跑）；
        用户桌面实例 8765 正在同一生产库上运行，QA 纪律：不对活动生产库执行写测试。
  D. 老库迁移幂等（端口 8794）：改造前快照 app.db.bak-watchpool-20261014 起服务，
     新代码应自动补建 watch_item_groups、数据不丢、重复请求不产生重复行。
  E. 生产库只读快照（改动后）与 A 对比           -> 证明回归全程对生产库零写入

用法：
  python verify_watch_api.py            # 全量（A~E）
  python verify_watch_api.py mkiso DIR  # 制作事务一致隔离副本（供 UI 回归脚本使用），打印 DB 路径
退出码：0 = 全部 PASS；1 = 存在 FAIL。
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

BASE = Path(r"F:\AI Agent部署知识库\2026-09-22-11-49-42\stock-forum-scraper")
PY = BASE / ".venv" / "Scripts" / "python.exe"
PROD_DB = BASE / "viewer" / "app.db"
BACKUP_DB = BASE / "viewer" / "app.db.bak-watchpool-20261014"
WORK = BASE / "_qa_watch_work"
RESULT_JSON = BASE / "_qa_watch_api_result.json"

PORT_REAL = 8792      # B：真实库只读
PORT_ISO = 8793       # C：隔离副本写往返
PORT_MIG = 8794       # D：老库迁移

# —— 期望值（来源：任务书实况 + QA 进程内独立复算 signals_for_watch，2026-10-14）——
EXPECT_SIGNAL_DATE = "2026-10-13"
EXPECT_GROUPS_N = {"3": 6, "4": 0, "5": 0, "6": 0, "7": 15}   # 长期潜伏6 / 短期0 / 极速下滑0 / 正热板块0 / 随便看看15
EXPECT_ITEMS_N = 21
EXPECT_STRAT = {           # symbol -> (期望命中数, 期望 sname 集合或 None)
    "603979": (4, {"RSI 中轴金叉", "Williams %R 超卖", "向下跳空回补", "站上年线（MA250）"}),
    "600869": (5, None), "301358": (3, None), "002756": (2, None), "002812": (2, None),
    "600188": (2, None), "600547": (2, None), "603929": (2, None), "002185": (1, None),
    "603288": (0, None), "002236": (0, None),
}
EXPECT_MIN_NONEMPTY = 9

RESULTS: list[dict] = []


def check(name: str, cond, detail: str = "") -> bool:
    ok = bool(cond)
    RESULTS.append({"name": name, "ok": ok, "detail": str(detail)})
    print(("  PASS  " if ok else "  FAIL  ") + name + (("  | " + str(detail)) if detail else ""))
    return ok


# ---------------------------------------------------------------- HTTP
def http_get(url: str, timeout: int = 150):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def http_post(url: str, payload: dict, timeout: int = 150):
    req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"),
                                 headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


# ---------------------------------------------------------------- 服务生命周期
def start_server(port: int, db: Path | None = None) -> subprocess.Popen:
    env = dict(os.environ)
    env["VIEWER_PORT"] = str(port)
    if db is not None:
        env["VIEWER_DB"] = str(db)
    for k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY", "all_proxy"):
        env.pop(k, None)
    p = subprocess.Popen([str(PY), "-m", "viewer.server"], cwd=str(BASE), env=env,
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                         text=True, encoding="utf-8", errors="replace")
    return p


def wait_ready(port: int, p: subprocess.Popen, timeout_s: int = 150) -> None:
    t0 = time.time()
    while time.time() - t0 < timeout_s:
        if p.poll() is not None:
            out = p.stdout.read() if p.stdout else ""
            raise RuntimeError(f"server(port={port}) exited early code={p.returncode}\n--- log tail ---\n{out[-3000:]}")
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/api/summary", timeout=5).read()
            return
        except Exception:
            time.sleep(0.5)
    raise RuntimeError(f"server(port={port}) not ready in {timeout_s}s")


def stop_server(p: subprocess.Popen) -> None:
    try:
        p.terminate()
        p.wait(timeout=10)
    except Exception:
        try:
            p.kill()
        except Exception:
            pass


# ---------------------------------------------------------------- DB 快照 / 副本
def ro_conn(db: Path) -> sqlite3.Connection:
    c = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    return c


def snapshot(db: Path) -> dict:
    """只读结构快照：分组 / 标的（结构字段）/ 关联组。不含价格等易变字段。"""
    c = ro_conn(db)
    try:
        return {
            "groups": [dict(r) for r in c.execute(
                "SELECT id,name,kind,sort_order,is_default FROM watch_groups ORDER BY id")],
            "items": [dict(r) for r in c.execute(
                "SELECT id,symbol,name,group_id,attention,sort_order FROM watch_items ORDER BY id")],
            "item_groups": [dict(r) for r in c.execute(
                "SELECT item_id,group_id,sort_order FROM watch_item_groups ORDER BY group_id,sort_order,item_id")],
        }
    finally:
        c.close()


def mkiso(dest_dir: Path) -> Path:
    """事务一致副本（sqlite backup API；用户实例在写生产库，禁裸拷三件套）。

    零删除纪律：目标已存在时换带序号的新文件名（不 unlink），
    避免触发沙箱 safe-delete 守卫、也不误删他人产物。
    """
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / "app.db"
    n = 1
    while dest.exists():
        n += 1
        dest = dest_dir / ("app-%d.db" % n)
    src = sqlite3.connect(f"file:{PROD_DB}?mode=ro", uri=True)
    try:
        dst = sqlite3.connect(str(dest))
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()
    return dest


def db_group_order(db: Path, gid: int) -> list:
    c = ro_conn(db)
    try:
        return [r[0] for r in c.execute(
            "SELECT item_id FROM watch_item_groups WHERE group_id=? ORDER BY sort_order, item_id", (gid,))]
    finally:
        c.close()


def find_item(d: dict, symbol: str):
    for it in (d.get("items") or []):
        if it.get("symbol") == symbol:
            return it
    return None


def group_of(d: dict, gid) -> dict | None:
    for g in (d.get("groups") or []):
        if str(g.get("id")) == str(gid):
            return g
    return None


# ---------------------------------------------------------------- 阶段 B：真实库只读
def phase_b() -> None:
    print("\n================ Phase B：真实库只读断言（port %d） ================" % PORT_REAL)
    p = start_server(PORT_REAL)          # 不设 VIEWER_DB → 生产库
    try:
        wait_ready(PORT_REAL, p)
        d = http_get(f"http://127.0.0.1:{PORT_REAL}/api/watch_list")
        check("B1.1 /api/watch_list ok=true", d.get("ok") is True, f"ok={d.get('ok')}")
        check("B1.2 signal_date == %s" % EXPECT_SIGNAL_DATE,
              d.get("signal_date") == EXPECT_SIGNAL_DATE, f"actual={d.get('signal_date')}")
        items = d.get("items") or []
        check("B1.3 标的数 == %d" % EXPECT_ITEMS_N, len(items) == EXPECT_ITEMS_N, f"actual={len(items)}")
        bad_struct = [it["symbol"] for it in items
                      if not isinstance(it.get("groups"), list) or it.get("n_groups") != len(it.get("groups") or [])]
        check("B1.4 每个 item 有 groups 数组且 n_groups==len(groups)", not bad_struct, f"bad={bad_struct}")
        gn = {str(g["id"]): g.get("n_items") for g in (d.get("groups") or [])}
        for gid, exp in EXPECT_GROUPS_N.items():
            check(f"B1.5 分组 id={gid} n_items == {exp}（DISTINCT 去重）", gn.get(gid) == exp, f"actual={gn.get(gid)}")
        nonempty = [it["symbol"] for it in items if (it.get("strat_hits") or [])]
        check(f"B1.6 strat_hits 非空只数 >= {EXPECT_MIN_NONEMPTY}", len(nonempty) >= EXPECT_MIN_NONEMPTY,
              f"actual={len(nonempty)} -> {nonempty}")
        for sym, (n, names) in EXPECT_STRAT.items():
            it = find_item(d, sym)
            if it is None:
                check(f"B1.7 {sym} 存在于池中", False, "not found")
                continue
            hits = it.get("strat_hits") or []
            if n == 0:
                check(f"B1.7 {sym}（{it.get('name')}）strat_hits 为空数组", hits == [], f"actual={hits}")
            else:
                got = {h.get("sname") for h in hits}
                check(f"B1.7 {sym}（{it.get('name')}）命中数 == {n}", len(hits) == n,
                      f"actual={len(hits)} {sorted(got)}")
                if names is not None:
                    check(f"B1.8 {sym} 命中名称集合与独立复算一致", got == names,
                          f"actual={sorted(got)}")
        with open(BASE / "_qa_watch_b1_evidence.json", "w", encoding="utf-8") as f:
            json.dump({"signal_date": d.get("signal_date"),
                       "strat": {it["symbol"]: [h.get("sname") for h in (it.get("strat_hits") or [])]
                                 for it in items},
                       "groups": [{"id": g["id"], "name": g["name"], "n_items": g["n_items"]}
                                  for g in (d.get("groups") or [])]},
                      f, ensure_ascii=False, indent=1)
        print("  证据落盘 -> _qa_watch_b1_evidence.json")
    finally:
        stop_server(p)


# ---------------------------------------------------------------- 阶段 C：隔离副本写往返
ORIG_G3_ORDER = [26, 25, 20, 18, 5, 3]     # 长期潜伏组初始顺序（生产库只读快照确认）


def phase_c() -> None:
    print("\n================ Phase C：隔离副本写链路往返（port %d，VIEWER_DB） ================" % PORT_ISO)
    iso_dir = WORK / "iso_write"
    db = mkiso(iso_dir)
    print("  ISO_DB =", db)
    snap0 = snapshot(db)
    p = start_server(PORT_ISO, db)
    try:
        wait_ready(PORT_ISO, p)
        root = f"http://127.0.0.1:{PORT_ISO}"

        # —— B2.1 同票追加分组：不再报错，existed + hint「已追加到分组」——
        r = http_post(root + "/api/watch_item_add", {"symbol": "603979", "groups": [4]})
        check("C2.1 add {603979, groups:[4]} ok=true", r.get("ok") is True, json.dumps(r, ensure_ascii=False))
        check("C2.2 existed=true（同票在池不报错）", r.get("existed") is True, f"existed={r.get('existed')}")
        check("C2.3 hint 含「已追加到分组」", "已追加到分组" in (r.get("hint") or ""), f"hint={r.get('hint')!r}")

        # —— B2.2 GET：n_groups=2、含组3+4、短期 tab n_items=1 ——
        d = http_get(root + "/api/watch_list")
        jc = find_item(d, "603979")
        check("C2.4 金诚信 n_groups == 2", jc and jc.get("n_groups") == 2,
              f"n_groups={jc.get('n_groups') if jc else None}")
        gids = sorted(str(g["id"]) for g in (jc.get("groups") or [])) if jc else []
        check("C2.5 金诚信 groups 含 id=3 与 id=4", gids == ["3", "4"], f"groups={gids}")
        check("C2.6 短期(4) n_items == 1", (group_of(d, 4) or {}).get("n_items") == 1,
              f"n_items={(group_of(d, 4) or {}).get('n_items')}")

        # —— B2.3 移出单组：left=1 ——
        r2 = http_post(root + "/api/watch_item_group_remove", {"id": 26, "group_id": 4})
        check("C2.7 group_remove left == 1", r2.get("ok") is True and r2.get("left") == 1,
              json.dumps(r2, ensure_ascii=False))

        # —— B2.4 GET：回到 n_groups=1、短期 n_items=0 ——
        d2 = http_get(root + "/api/watch_list")
        jc2 = find_item(d2, "603979")
        check("C2.8 金诚信 n_groups == 1（还原）", jc2 and jc2.get("n_groups") == 1,
              f"n_groups={jc2.get('n_groups') if jc2 else None}")
        check("C2.9 短期(4) n_items == 0（还原）", (group_of(d2, 4) or {}).get("n_items") == 0,
              f"n_items={(group_of(d2, 4) or {}).get('n_items')}")
        check("C2.10 标的总数仍为 21", len(d2.get("items") or []) == EXPECT_ITEMS_N,
              f"n={len(d2.get('items') or [])}")

        # —— B3.1 组内排序：提交换序，DB 落盘为提交序 ——
        perm = [25, 26, 20, 18, 5, 3]
        r3 = http_post(root + "/api/watch_item_reorder", {"group_id": 3, "ids": perm})
        check("C3.1 reorder ok=true", r3.get("ok") is True, json.dumps(r3, ensure_ascii=False))
        order = db_group_order(db, 3)
        check("C3.2 DB 中长期潜伏组内顺序 == 提交序", order == perm, f"db={order} expect={perm}")

        # —— B3.2 还原初始顺序 ——
        r4 = http_post(root + "/api/watch_item_reorder", {"group_id": 3, "ids": ORIG_G3_ORDER})
        order2 = db_group_order(db, 3)
        check("C3.3 还原后组内顺序 == 初始序", order2 == ORIG_G3_ORDER, f"db={order2}")
        check("C3.4 reorder moved 计数 == 6", r4.get("moved") == 6, f"moved={r4.get('moved')}")
    finally:
        stop_server(p)
    # —— C9（副本侧）：往返结束后副本状态应与初始快照完全一致 ——
    snap1 = snapshot(db)
    same = snap0 == snap1
    check("C9.1 写往返后副本结构快照 == 初始快照（往返干净可还原）", same,
          "" if same else json.dumps({"before": snap0["item_groups"], "after": snap1["item_groups"]},
                                     ensure_ascii=False)[:600])


# ---------------------------------------------------------------- 阶段 D：老库迁移幂等
def phase_d() -> None:
    print("\n================ Phase D：老库迁移幂等（port %d，改造前快照） ================" % PORT_MIG)
    mig_dir = WORK / "iso_migrate"
    mig_dir.mkdir(parents=True, exist_ok=True)
    db = mig_dir / "app.db"
    shutil.copyfile(BACKUP_DB, db)
    print("  MIG_DB =", db, "（改造前快照，无 watch_item_groups 表）")
    p = start_server(PORT_MIG, db)
    try:
        wait_ready(PORT_MIG, p)
        root = f"http://127.0.0.1:{PORT_MIG}"
        d = http_get(root + "/api/watch_list")
        check("D1 老库起服务 watch_list ok=true", d.get("ok") is True)
        items = d.get("items") or []
        check("D2 老库 21 只标的不丢", len(items) == EXPECT_ITEMS_N, f"n={len(items)}")
        no_grp = [it["symbol"] for it in items if not (it.get("groups") or [])]
        check("D3 迁移后每只标的都有关联组（自动补建）", not no_grp, f"缺组={no_grp}")
        gn = {str(g["id"]): g.get("n_items") for g in (d.get("groups") or [])}
        check("D4 分组计数 长期潜伏=6 / 随便看看=15", gn.get("3") == 6 and gn.get("7") == 15, f"{gn}")
        c = ro_conn(db)
        try:
            n_rows = c.execute("SELECT COUNT(*) FROM watch_item_groups").fetchone()[0]
            n_dup = c.execute(
                "SELECT COUNT(*) FROM (SELECT item_id,group_id FROM watch_item_groups"
                " GROUP BY item_id,group_id HAVING COUNT(*)>1)").fetchone()[0]
        finally:
            c.close()
        check("D5 关联表行数 == 21 且无重复 (item_id,group_id)", n_rows == 21 and n_dup == 0,
              f"rows={n_rows} dup={n_dup}")
        # 幂等：再打一次（ensure_schema 重复执行）不涨行、不丢数据
        d2 = http_get(root + "/api/watch_list")
        c = ro_conn(db)
        try:
            n_rows2 = c.execute("SELECT COUNT(*) FROM watch_item_groups").fetchone()[0]
            n_items2 = c.execute("SELECT COUNT(*) FROM watch_items").fetchone()[0]
        finally:
            c.close()
        check("D6 二次请求后行数/标的数不变（迁移幂等）",
              n_rows2 == 21 and n_items2 == 21 and len(d2.get("items") or []) == 21,
              f"rows={n_rows2} items={n_items2}")
    finally:
        stop_server(p)


# ---------------------------------------------------------------- 阶段 E：生产库零写入证明
def phase_e(snap_before: dict) -> None:
    print("\n================ Phase E：生产库前后快照对比（零写入证明） ================")
    snap_after = snapshot(PROD_DB)
    same = snap_before == snap_after
    check("E1 生产库结构快照 前后一致（回归全程零写入）", same,
          "" if same else json.dumps({"before": snap_before, "after": snap_after}, ensure_ascii=False)[:800])
    with open(BASE / "_qa_watch_work" / "snap_prod_after.json", "w", encoding="utf-8") as f:
        json.dump(snap_after, f, ensure_ascii=False, indent=1)


# ---------------------------------------------------------------- main
def main() -> int:
    WORK.mkdir(parents=True, exist_ok=True)
    if len(sys.argv) == 3 and sys.argv[1] == "mkiso":
        db = mkiso(Path(sys.argv[2]))
        print(db)
        return 0

    snap_before = snapshot(PROD_DB)
    with open(WORK / "snap_prod_before.json", "w", encoding="utf-8") as f:
        json.dump(snap_before, f, ensure_ascii=False, indent=1)
    print("生产库初始快照 -> _qa_watch_work/snap_prod_before.json"
          f"（items={len(snap_before['items'])}, item_groups={len(snap_before['item_groups'])}）")

    try:
        phase_b()
        phase_c()
        phase_d()
    finally:
        pass
    phase_e(snap_before)

    fails = [r for r in RESULTS if not r["ok"]]
    with open(RESULT_JSON, "w", encoding="utf-8") as f:
        json.dump({"total": len(RESULTS), "passed": len(RESULTS) - len(fails), "failed": len(fails),
                   "results": RESULTS}, f, ensure_ascii=False, indent=1)
    print("\n================ API 级回归结论 ================")
    print(f"Total={len(RESULTS)} Passed={len(RESULTS)-len(fails)} Failed={len(fails)}")
    for r in fails:
        print("  - FAIL " + r["name"] + ("  | " + r["detail"] if r["detail"] else ""))
    print("结果落盘 ->", RESULT_JSON.name)
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
