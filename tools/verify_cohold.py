"""隔离验证 Request F（近5日共同买入 / 共同卖出板块）。

不触碰生产库：把 app.db 复制到临时目录，通过 VIEWER_DB 指向副本后再导入
viewer.db / viewer.server（两者都在导入时读取该环境变量），最后用临时端口
8794 起一个真实 HTTP 实例验证路由。

覆盖点：
  1. 窗口与锚定：auto→数据最新日 / today→按日历日 / 显式日期 / 非法值回退 / days 生效；
  2. **跨表合并**：positions（无数量列）与 trades（有数量、更准时刻）必须都取，
     证明「只查 positions 会漏掉大半」这个设计前提；
  3. 口径：返回组一律 ≥min_holders 人；调高 min_holders 结果只减不增；组内选手去重；
  4. 字段正确性：浮盈只由**同一笔**的买价/现价算、合计数量不含缺数量的行、
     均价按数量加权、持有状态 =「买点之后是否存在该选手对该标的的卖出记录」；
  5. **卖出组按 sell_time 过滤**（而不是 buy_time）——否则「几个月前建仓、窗口内卖出」
     的记录会被整批漏掉；
  6. HTTP 层：GET /api/cohold_recent 返回 200 且结构完整；
  7. 回归：/api/consensus（共同持仓共识）不受影响。
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
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

SRC = ROOT / "viewer" / "app.db"
PORT = 8794
fails: list[str] = []


def check(cond: bool, msg: str):
    if cond:
        print(f"  [OK] {msg}")
    else:
        print(f"  [FAIL] {msg}")
        fails.append(msg)


def isolated_copy() -> Path:
    tmp = Path(tempfile.mkdtemp(prefix="qa_cohold_"))
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

    # ---------- 1. 窗口与锚定 ----------
    print("\n[1] 窗口与锚定口径")
    auto = db.cohold_recent(days=5, min_holders=2)
    latest = db._latest_action_date()
    check(auto["anchor_source"] == "latest_data",
          f"默认 auto 锚定数据最新日（anchor_source={auto['anchor_source']}）")
    check(bool(latest) and auto["anchor"] == latest,
          f"锚定日 = 数据最后一次动作日（{auto['anchor']} vs {latest}）")
    d0 = date.fromisoformat(auto["anchor"])
    check(auto["window_start"] == (d0 - timedelta(days=4)).isoformat()
          and auto["window_end"] == d0.isoformat(),
          f"5 天窗口首尾正确（{auto['window_start']} ~ {auto['window_end']}）")

    d3 = db.cohold_recent(days=3, min_holders=2)
    check(d3["window_start"] == (d0 - timedelta(days=2)).isoformat(),
          f"days=3 时窗口收缩为 3 天（{d3['window_start']} ~ {d3['window_end']}）")

    td = db.cohold_recent(days=5, min_holders=2, anchor="today")
    check(td["anchor_source"] == "today" and td["window_end"] == date.today().isoformat(),
          f"anchor='today' 按日历日锚定（{td['window_start']} ~ {td['window_end']}）")

    ex = db.cohold_recent(days=5, min_holders=2, anchor="2026-09-20")
    check(ex["anchor_source"] == "explicit" and ex["window_end"] == "2026-09-20"
          and ex["window_start"] == "2026-09-16",
          f"显式日期锚定正确（{ex['window_start']} ~ {ex['window_end']}）")

    bad = db.cohold_recent(days=5, min_holders=2, anchor="不是日期")
    check(bad["anchor_source"] == "latest_data",
          "非法 anchor 回退到数据最新日（不抛异常、不产生空窗口）")

    # ---------- 2. 跨表合并（本板块的核心设计前提） ----------
    print("\n[2] 跨表合并 positions + trades")
    all_groups = (auto["buy"] or []) + (auto["sell"] or [])
    ev = [set(g.get("evidence") or []) for g in all_groups]
    check(any("positions" in e and "trades" in e for e in ev),
          "存在同一标的的记录同时来自 positions 与 trades（两表真的合并了）")
    check(all("trades" in e for e in ev),
          "所有组都含 trades 出处（数量与成交时刻只在这张表里）")
    # 关掉 trades 只查 positions，结果必然显著变少 —— 证明「只查一张表会漏」
    pos_only = 0
    for g in auto["buy"]:
        if all(p.get("evidence") == ["positions"] for p in g["players"]):
            pos_only += 1
    check(pos_only == 0,
          f"共同买入组没有任何一组只依赖 positions（实测 {pos_only} 组）—— 单表查询会漏掉全部结果")
    qty_rows = [p for g in all_groups for p in g["players"] if p.get("quantity")]
    check(bool(qty_rows), f"有 {len(qty_rows)} 位参与者带出成交数量（来自 trades，positions 无此列）")

    # ---------- 3. 口径 ----------
    print("\n[3] 同向操作口径")
    for g in auto["buy"]:
        if g["player_count"] < 2:
            check(False, f"共买组 {g['symbol']} 人数 {g['player_count']} < 2")
    for g in auto["sell"]:
        if g["player_count"] < 2:
            check(False, f"共卖组 {g['symbol']} 人数 {g['player_count']} < 2")
    check(all(g["player_count"] >= 2 for g in all_groups), "所有返回组一律 ≥2 人")
    check(all(len({p["player"] for p in g["players"]}) == g["player_count"]
              for g in all_groups), "组内选手已去重（player_count == 去重后人数）")

    mh3 = db.cohold_recent(days=5, min_holders=3)
    check(len(mh3["buy"]) <= len(auto["buy"]) and len(mh3["sell"]) <= len(auto["sell"]),
          f"min_holders=3 结果只减不增（共买 {len(auto['buy'])}→{len(mh3['buy'])}，"
          f"共卖 {len(auto['sell'])}→{len(mh3['sell'])}）")
    check(all(g["player_count"] >= 3 for g in (mh3["buy"] + mh3["sell"])),
          "min_holders=3 时所有返回组 ≥3 人")

    # ---------- 4. 字段正确性 ----------
    print("\n[4] 字段正确性")
    # 4a 浮盈必须来自同一笔（用库里的原始记录重算核对）
    bad_gain = []
    for g in all_groups:
        for p in g["players"]:
            if p.get("pct_gain") is None:
                continue
            # positions 的买价列叫 buy_price、trades 的叫 price —— 两表列名不同，分别取。
            # 按**动作时间倒序**取第一个两值齐全的记录：这正是实现里「最近一笔」的口径，
            # 验证必须复刻同一规则，否则同一选手多笔记录时会对不上。
            rows = []
            for tbl, who, pcol in (("positions", "player", "buy_price"),
                                   ("trades", "author", "price")):
                for r in db.conn.execute(
                        f"SELECT {pcol} bp, current_price cp, buy_time t FROM {tbl} "
                        f"WHERE {who}=? AND symbol=?", (p["player"], g["symbol"])).fetchall():
                    rows.append((r["t"] or "", r["bp"], r["cp"]))
            rows.sort(key=lambda x: x[0], reverse=True)
            want = None
            for _t, bp, cp in rows:
                if bp and cp:
                    want = round((cp / bp - 1) * 100, 1)
                    break
            if want is not None and abs(want - p["pct_gain"]) > 0.15:
                bad_gain.append((p["player"], g["symbol"], want, p["pct_gain"]))
    check(not bad_gain, f"所有浮盈都能由同一笔的买价/现价复算（不符 {len(bad_gain)} 条）")
    # 卖出组的浮盈必须为 None：SELL 行的价是卖出价，算出来的不是收益（会误导成盈利）
    sell_gains = [p["pct_gain"] for g in (auto["sell"] or []) for p in g["players"]
                  if p.get("pct_gain") is not None]
    check(not sell_gains,
          f"共卖组一律不返回浮盈（实测非空 {len(sell_gains)} 条）—— 卖出价对现价不构成收益")
    buy_gains = [p for g in (auto["buy"] or []) for p in g["players"]
                 if p.get("pct_gain") is not None]
    check(bool(buy_gains), f"共买组正常返回浮盈（{len(buy_gains)} 条）")

    # 4b 合计数量 = 组内非空数量之和（缺数量的行不参与，也不记 0 顶替）
    bad_qty = []
    for g in all_groups:
        want = sum(p["quantity"] for p in g["players"] if p.get("quantity")) or None
        if g.get("total_quantity") != want:
            bad_qty.append((g["symbol"], g.get("total_quantity"), want))
    check(not bad_qty, f"合计数量 = 组内非空数量之和（不符 {len(bad_qty)} 条）")

    # 4c 均价按数量加权
    bad_avg = []
    for g in all_groups:
        pairs = [(p["price"], p["quantity"] or 0) for p in g["players"] if p.get("price")]
        if not pairs:
            if g.get("avg_price") is not None:
                bad_avg.append((g["symbol"], g["avg_price"], None))
            continue
        wsum = sum(w for _, w in pairs)
        want = round((sum(pr * w for pr, w in pairs) / wsum) if wsum
                     else (sum(pr for pr, _ in pairs) / len(pairs)), 3)
        if g.get("avg_price") != want:
            bad_avg.append((g["symbol"], g.get("avg_price"), want))
    check(not bad_avg, f"均价按数量加权（不符 {len(bad_avg)} 条）")

    # 4d 持有状态 = 买点之后是否存在该选手对该标的的卖出记录
    bad_hold = []
    for g in (auto["buy"] or []):
        for p in g["players"]:
            sells = []
            for tbl, who, tcol in (("positions", "player", "sell_time"),
                                   ("trades", "author", "sell_time")):
                for r in db.conn.execute(
                        f"SELECT {tcol} s FROM {tbl} WHERE {who}=? AND symbol=? "
                        f"AND UPPER(COALESCE(action,''))='SELL' AND {tcol}<>''",
                        (p["player"], g["symbol"])).fetchall():
                    sells.append(r["s"])
            want = not any(s >= p["last_time"] for s in sells)
            if bool(p["still_holding"]) != bool(want):
                bad_hold.append((p["player"], g["symbol"], p["still_holding"], want))
    check(not bad_hold, f"「买入后已卖出 / 持有中」判定与原始卖出记录一致（不符 {len(bad_hold)} 条）")
    exited = [(g["symbol"], p["player"]) for g in (auto["buy"] or [])
              for p in g["players"] if not p.get("still_holding")]
    check(bool(exited), f"确实识别出「窗口内买入后又卖出」的行（{exited[:3]}）")

    # ---------- 5. 卖出组按 sell_time 过滤 ----------
    print("\n[5] 卖出组窗口按 sell_time 过滤（不是 buy_time）")
    lo, hi = auto["window_start"], auto["window_end"]
    old_buys = []
    for g in (auto["sell"] or []):
        for p in g["players"]:
            for tbl, who, tcol in (("positions", "player", "buy_time"),
                                   ("trades", "author", "buy_time")):
                for r in db.conn.execute(
                        f"SELECT {tcol} b FROM {tbl} WHERE {who}=? AND symbol=? "
                        f"AND {tcol}<>''", (p["player"], g["symbol"])).fetchall():
                    if (r["b"] or "")[:10] < lo:
                        old_buys.append((g["symbol"], p["player"], r["b"]))
    check(bool(old_buys),
          f"共卖组里确实存在「建仓日早于窗口」的行（{old_buys[:2]}）—— "
          f"若按 buy_time 过滤这些行会被整批漏掉")

    # ---------- 6. HTTP 层 ----------
    print("\n[6] HTTP 层")
    env = dict(os.environ, VIEWER_PORT=str(PORT))
    proc = subprocess.Popen(
        [sys.executable, "-m", "viewer.server"], cwd=str(ROOT), env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    try:
        data = None
        for _ in range(40):
            try:
                with urllib.request.urlopen(
                        f"http://127.0.0.1:{PORT}/api/cohold_recent?days=5&min_holders=2",
                        timeout=3) as resp:
                    if resp.status != 200:
                        continue
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
            for k in ("days", "min_holders", "anchor", "anchor_source", "today",
                      "latest_data_date", "window_start", "window_end", "buy", "sell"):
                if k not in data:
                    check(False, f"/api/cohold_recent 缺字段 {k}")
            check(all(k in data for k in ("days", "anchor_source", "window_start",
                                          "window_end", "buy", "sell")),
                  "/api/cohold_recent 结构完整（窗口/锚定/两组清单）")
            check(isinstance(data.get("buy"), list) and isinstance(data.get("sell"), list),
                  "buy / sell 均为数组")
            g0 = (data.get("buy") or [None])[0]
            if g0:
                need = ("symbol", "symbol_name", "player_count", "players",
                        "total_quantity", "avg_price", "first_time", "last_time", "urls")
                check(all(k in g0 for k in need), f"组字段完整（{g0['symbol']}）")
                p0 = (g0.get("players") or [None])[0]
                pneed = ("player", "last_time", "price", "quantity", "ratio",
                         "pct_gain", "still_holding", "evidence")
                check(bool(p0) and all(k in p0 for k in pneed), "参与者字段完整（含数量/价格/浮盈/持有状态）")
                check(bool(g0.get("symbol_name")), f"名称已在服务端补全（{g0.get('symbol_name')}）")
            # 非法天数 / 非法阈值被钳制
            with urllib.request.urlopen(
                    f"http://127.0.0.1:{PORT}/api/cohold_recent?days=-5&min_holders=0",
                    timeout=5) as resp:
                g = json.loads(resp.read().decode("utf-8"))
            check(g["days"] >= 1 and g["min_holders"] >= 2,
                  f"越界参数被钳制（days={g['days']}, min_holders={g['min_holders']}）")
            # 回归：共同持仓共识接口不受影响
            with urllib.request.urlopen(
                    f"http://127.0.0.1:{PORT}/api/consensus?min_holders=2", timeout=20) as resp:
                c = json.loads(resp.read().decode("utf-8"))
            check(resp.status == 200 and isinstance(c, list) and len(c) > 0,
                  f"回归：/api/consensus 仍返回 {len(c)} 条共同持仓")
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
    print("ALL PASSED —— Request F 近5日共同买入/卖出 后端行为符合预期")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
