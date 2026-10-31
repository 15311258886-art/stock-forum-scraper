"""隔离验证 Request G（「最后更新」列 / 刷新状态记录）。

不触碰生产库：把 app.db 复制到临时目录，通过 VIEWER_DB 指向副本后再导入
viewer.db / viewer.server（两者都在导入时读取该环境变量），最后用临时端口
8793 起一个真实 HTTP 实例验证路由。

覆盖点：
  1. refresh_status 表随库初始化自动建立（兼容旧库，无需手工迁移）；
  2. record_refresh("ok") 写入 last_ok_at / status / item_count；
  3. record_refresh("error") **不改动 last_ok_at**（只更新 attempt/status/error）——
     这是「最后更新」列可信的前提；
  4. 再次 ok 才前移 last_ok_at；
  5. api_refresh_status 结构完整（sources 不含 __all__、含阈值与 tracked_sources）；
  6. /api/stocks 返回 sources 字段（供股票卡片判定时效）；
  7. HTTP 层：GET /api/refresh_status 返回 200 且结构正确。
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

SRC = Path(os.environ.get("VIEWER_DB") or (ROOT / "viewer" / "app.db"))
PORT = 8793
fails: list[str] = []


def check(cond: bool, msg: str):
    if cond:
        print(f"  [OK] {msg}")
    else:
        print(f"  [FAIL] {msg}")
        fails.append(msg)


def isolated_copy() -> Path:
    tmp = Path(tempfile.mkdtemp(prefix="qa_refresh_"))
    dst = tmp / "app.db"
    shutil.copy2(SRC, dst)
    # WAL 模式下必须连同 -wal / -shm 一起复制，否则副本可能缺最近事务
    for suf in ("-wal", "-shm"):
        p = Path(str(SRC) + suf)
        if p.exists():
            shutil.copy2(p, Path(str(dst) + suf))
    return dst


def main() -> int:
    if not SRC.exists():
        print("!! 找不到源库", SRC)
        return 2
    dst = isolated_copy()
    os.environ["VIEWER_DB"] = str(dst)      # 必须在导入 viewer.* 之前设置
    print(f"隔离库：{dst}")

    from viewer.db import Database
    from viewer import server

    db = Database()

    print("\n[1] refresh_status 表随初始化建立")
    tables = {r["name"] for r in db.conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    check("refresh_status" in tables, "refresh_status 表存在（旧库自动补建）")

    print("\n[1b] 首次启用回填（仅限有真实出处的来源）")
    raw_sites = {r["site"] for r in db.conn.execute(
        "SELECT DISTINCT site FROM raw_pages WHERE site IS NOT NULL AND site<>''")}
    seeded = {r["source_key"] for r in db.get_refresh_status()}
    check(bool(seeded), f"回填后有记录：{sorted(seeded)}")
    # 2026-09-23 更新：刷新状态的来源不再只有抓取站点——知识库来源（doc_key）有自己的
    # 真实出处（本地 md 文件的吸取记录），其 last_ok_at 由 kb_reload 写入，同样不是编造。
    # 故不变量修正为「每个来源都能追到真实出处」，而不是「必须来自 raw_pages」。
    kb_keys = set(server._kb_keys())
    check(seeded - {"__all__"} <= (raw_sites | kb_keys),
          "刷新状态的来源均有真实出处（raw_pages 抓取记录 或 知识库吸取记录），无凭空编造")
    check(bool(seeded & raw_sites), "至少回填了一个真实抓取来源")
    # 回填后清空，后续步骤从空表开始逐项验证写语义
    db.conn.execute("DELETE FROM refresh_status")
    db.conn.commit()
    check(len(db.get_refresh_status()) == 0, "已清空，进入写语义验证")

    print("\n[2] 成功刷新写入 last_ok_at")
    db.record_refresh("taoguba", "ok", 12, "淘股吧")
    s = {r["source_key"]: r for r in db.get_refresh_status()}
    check(bool(s.get("taoguba", {}).get("last_ok_at")), "ok → last_ok_at 有值")
    check(s["taoguba"]["status"] == "ok", "ok → status=ok")
    check(s["taoguba"]["item_count"] == 12, "ok → item_count=12")
    ok1 = s["taoguba"]["last_ok_at"]

    print("\n[3] 失败刷新不动 last_ok_at（关键语义）")
    time.sleep(1.1)  # 拉开秒级时间差，确保「前移」可被检出
    db.record_refresh("taoguba", "error", 0, "淘股吧", error="boom")
    s = {r["source_key"]: r for r in db.get_refresh_status()}
    check(s["taoguba"]["last_ok_at"] == ok1,
          "error → last_ok_at 未被改动（仍是上次成功时刻）")
    check(s["taoguba"]["status"] == "error", "error → status=error")
    check(s["taoguba"]["error"] == "boom", "error → error 文本已记录")
    check(s["taoguba"]["last_attempt_at"] != ok1,
          "error → last_attempt_at 已更新（区别于 last_ok_at）")

    print("\n[4] 再次成功后 last_ok_at 才前移")
    db.record_refresh("taoguba", "ok", 7, "淘股吧")
    s = {r["source_key"]: r for r in db.get_refresh_status()}
    check(s["taoguba"]["last_ok_at"] != ok1, "ok → last_ok_at 前移")
    check(s["taoguba"]["status"] == "ok", "ok → status 复位为 ok")
    check(s["taoguba"]["error"] == "", "ok → error 已清空")

    print("\n[5] __all__ 整轮汇总 + api_refresh_status 结构")
    db.record_refresh("__all__", "ok", 1, "全部来源")
    r = server.api_refresh_status()
    check(r["status"] == "ok", "整轮 status=ok")
    check(bool(r["last_ok_at"]), "整轮 last_ok_at 有值")
    check(isinstance(r["sources"], list) and len(r["sources"]) >= 1, "sources 非空")
    check(all(x["source_key"] != "__all__" for x in r["sources"]),
          "sources 中不含 __all__（汇总单列）")
    check(r["interval_seconds"] == server.REFRESH_INTERVAL_SECONDS,
          f"interval_seconds={r['interval_seconds']}")
    check(r["stale_seconds"] == server.REFRESH_STALE_SECONDS,
          f"stale_seconds={r['stale_seconds']}（2×周期）")
    check(r["stale_seconds"] == 2 * r["interval_seconds"], "stale 阈值 = 2× 刷新周期")
    check(isinstance(r["tracked_sources"], list), "tracked_sources 为列表")
    src_tg = next((x for x in r["sources"] if x["source_key"] == "taoguba"), None)
    check(src_tg is not None and src_tg["display_name"] == "淘股吧",
          "来源带中文显示名（tooltip 用）")

    print("\n[6] /api/stocks 返回 sources 字段")
    st = server.api_stocks()
    check(bool(st), "api_stocks 有数据")
    check(all("sources" in x and isinstance(x["sources"], list) for x in st),
          f"api_stocks 全部 {len(st)} 行均含 sources 列表")

    # ---------- HTTP 层 ----------
    print("\n[7] HTTP：GET /api/refresh_status")
    env = dict(os.environ, VIEWER_DB=str(dst), VIEWER_PORT=str(PORT))
    proc = subprocess.Popen(
        [sys.executable, "-m", "viewer.server"],
        cwd=str(ROOT), env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )
    try:
        data = None
        for _ in range(80):
            try:
                with urllib.request.urlopen(
                        f"http://127.0.0.1:{PORT}/api/refresh_status", timeout=1) as resp:
                    check(resp.status == 200, f"HTTP 200（实际 {resp.status}）")
                    data = json.loads(resp.read().decode("utf-8"))
                    break
            except Exception:
                if proc.poll() is not None:
                    break
                time.sleep(0.25)
        if data is None:
            out = b""
            try:
                out = proc.stdout.read() if proc.stdout else b""
            except Exception:
                pass
            check(False, "HTTP 端点可达（服务未启动）" + (" —— " + out.decode("utf-8", "ignore")[:400]))
        else:
            check(isinstance(data.get("sources"), list), "/api/refresh_status 返回 sources")
            check("stale_seconds" in data, "返回 stale_seconds 阈值")
            check(isinstance(data.get("tracked_sources"), list), "返回 tracked_sources")
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=8)
        except Exception:
            proc.kill()

    print("\n" + "=" * 56)
    if fails:
        print(f"FAILED: {len(fails)} 项未通过")
        for f in fails:
            print("  -", f)
        return 1
    print("ALL PASSED —— Request G 刷新状态/最后更新列 后端行为符合预期")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
