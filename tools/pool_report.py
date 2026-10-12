# -*- coding: utf-8 -*-
"""人物池 / 入选选手 现状报告（可重复运行，用于月度迭代前后对账）。

三层口径（2026-09-23 与王总确认的约定）：
    候选池 candidate   —— 从多渠道（直播/网站/论坛/资讯）搜集的原始人名，目标 ~500
    人物池 persons     —— 候选筛过一轮后的「关注人物」，目标 200
    入选选手 players   —— 有实盘/持仓证据、进入共识计算的选手，目标 150~160

本脚本只读，不改任何数据。
"""
import io
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)

TARGET = {"candidate": 500, "persons": 200, "players": (150, 160), "top_players": (150, 160)}


def table_info(db, tbl):
    try:
        return [r["name"] for r in db.conn.execute(f"PRAGMA table_info({tbl})")]
    except Exception:
        return []


def count(db, tbl, where="", args=()):
    try:
        sql = f"SELECT COUNT(*) c FROM {tbl}" + (" WHERE " + where if where else "")
        return db.conn.execute(sql, args).fetchone()["c"]
    except Exception as e:
        return f"ERR:{e}"


def group(db, tbl, col, where="", limit=40):
    try:
        sql = f"SELECT {col} k, COUNT(*) n FROM {tbl}" + (" WHERE " + where if where else "") + \
              f" GROUP BY {col} ORDER BY n DESC LIMIT {limit}"
        return [(r["k"], r["n"]) for r in db.conn.execute(sql)]
    except Exception as e:
        return [("ERR", str(e))]


def main():
    from viewer.db import Database
    db = Database()

    print("=" * 76)
    print("A. 三层池子的实际规模")
    print("=" * 76)
    n_persons = count(db, "persons")
    n_players = count(db, "players")
    n_cand = count(db, "candidates")
    try:
        n_pool = count(db, "player_pool")
    except Exception:
        n_pool = "n/a"
    print(f"  persons       人物池     = {n_persons}   （目标 {TARGET['persons']}）")
    print(f"  players       入选选手   = {n_players}   （目标 {TARGET['players'][0]}~{TARGET['players'][1]}）")
    print(f"  candidates    候选池     = {n_cand}   （目标 {TARGET['candidate']}）")
    print(f"  player_pool   候选表     = {n_pool}")
    tables = [r["name"] for r in db.conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
    print("  全部表:", ", ".join(tables))

    print()
    print("=" * 76)
    print("B. 人物池 persons 构成")
    print("=" * 76)
    print("  列:", table_info(db, "persons"))
    for col in ("site", "platform", "role", "kind", "source", "status"):
        if col in table_info(db, "persons"):
            print(f"  按 {col}: {group(db, 'persons', col)}")

    print()
    print("=" * 76)
    print("C. 入选选手 players 构成")
    print("=" * 76)
    print("  列:", table_info(db, "players"))
    for col in ("tier", "channel", "platform", "site", "is_visible", "is_top_player"):
        if col in table_info(db, "players"):
            print(f"  按 {col}: {group(db, 'players', col)}")
    if "is_top_player" in table_info(db, "players"):
        print(f"  is_top_player=1 计数: {count(db, 'players', 'is_top_player=1')}")
    if "is_visible" in table_info(db, "players"):
        print(f"  is_visible=1 计数  : {count(db, 'players', 'is_visible=1')}")

    print()
    print("=" * 76)
    print("D. 重叠度（同一名字是否两处都有）")
    print("=" * 76)
    try:
        both = db.conn.execute(
            "SELECT COUNT(*) c FROM actors WHERE 1=0").fetchone()
    except Exception:
        both = None
    try:
        rows = db.conn.execute("""
            SELECT COUNT(*) c FROM players p
            WHERE EXISTS (SELECT 1 FROM persons s WHERE s.name = p.name)""").fetchone()
        print("  players 中同时出现在 persons 的:", rows["c"])
    except Exception as e:
        print("  比对失败:", e)

    print()
    print("=" * 76)
    print("E. 配置里的候选来源声明（config/sites.yaml）")
    print("=" * 76)
    p = "config/sites.yaml"
    if os.path.exists(p):
        s = io.open(p, encoding="utf-8").read()
        print(s)
    else:
        print("  未找到", p)

    print()
    print("=" * 76)
    print("F. 缺口")
    print("=" * 76)
    def gap(cur, tgt):
        if isinstance(cur, int) and isinstance(tgt, int):
            return f"差 {tgt - cur:+d}"
        return "n/a"
    print(f"  人物池   {n_persons} → {TARGET['persons']}   {gap(n_persons, TARGET['persons'])}")
    if isinstance(n_players, int):
        lo, hi = TARGET["players"]
        state = "已达区间" if lo <= n_players <= hi else (f"低于下限 {lo-n_players}" if n_players < lo else f"高于上限 {n_players-hi}")
        print(f"  入选选手 {n_players} → {lo}~{hi}   {state}")
    print(f"  候选池   {n_cand} → {TARGET['candidate']}   {gap(n_cand, TARGET['candidate'])}")

    print()
    print("=" * 76)
    print("G. 数据库库龄（是否需要每月刷新）")
    print("=" * 76)
    for k in ("demo", "last_ingest", "last_fetch_at", "pool_updated_at", "persons_updated_at"):
        v = db.get_meta(k)
        if v is not None:
            print(f"  meta[{k}] = {str(v)[:200]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
