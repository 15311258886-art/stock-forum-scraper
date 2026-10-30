# -*- coding: utf-8 -*-
"""雪球组合持仓(cube)回填 - 入库段。

读取 xueqiu_cube_crawler.cjs 产出的 xueqiu_cubes.json，写入 viewer/app.db：
  - players 表：每个雪球组合作者 upsert 为 site='xueqiu' 的选手（channel='E'，层级=E 组合跟随）。
  - positions 表：每个持仓明细 insert（按 player+channel+symbol+action 去重，避免重复）。
  - ratio：雪球 weight 已是百分比(如 12.3)；若 <=1.5 视为小数比例再 ×100。

用法：.venv\Scripts\python.exe xueqiu_cube_load.py [--json xueqiu_cubes.json]
"""
from __future__ import annotations

import argparse
import json
import sys
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

logger_ok = print


def _norm_ratio(w) -> float:
    try:
        w = float(w)
    except (TypeError, ValueError):
        return 0.0
    if w <= 1.5:          # 小数比例 -> 百分比
        w *= 100.0
    return round(w, 4)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", default="xueqiu_cubes.json")
    ap.add_argument("--channel", default="E", help="雪球组合持仓渠道标签（默认 E=组合跟随）")
    args = ap.parse_args()

    jf = ROOT / args.json
    if not jf.exists():
        print("[错误] 未找到", jf, "— 请先运行 xueqiu_cube_crawler.cjs 抓取出 JSON")
        return 2
    data = json.loads(jf.read_text(encoding="utf-8"))
    players = data.get("players", {})
    if not players:
        print("[提示] players 为空，可能抓取被 WAF/未登录拦截；errors=", data.get("errors"))
        return 0

    try:
        from viewer.db import Database
    except Exception as e:  # noqa: BLE001
        print("[错误] 导入 viewer.db 失败：", e)
        return 2

    db = Database()
    n_player = 0
    n_pos = 0
    for uid, p in players.items():
        name = (p.get("name") or uid).strip()
        cubes = p.get("cubes", [])
        if not cubes:
            print(f"  [跳过] {name}({uid}) 无 cube 持仓")
            continue
        # upsert 选手
        try:
            db.save_player(site="xueqiu", name=name, channel=args.channel,
                           rank_position=None, ranking_score=0, is_top_player=1,
                           is_certified=0, source_url=f"https://xueqiu.com/{uid}",
                           tier=args.channel, tier_weight=0.8, influence=80.0,
                           is_visible=1, platform="xueqiu", followers=0)
            n_player += 1
        except Exception as e:  # noqa: BLE001
            print(f"  [选手写入失败] {name}: {e}")
        # 持仓去重插入
        for cube in cubes:
            for h in cube.get("holdings", []):
                sym = str(h.get("symbol", "")).strip()
                if not sym or len(sym) != 6:
                    continue
                # 去重：同选手同渠道同代码同动作已存在则跳过
                try:
                    exists = db.conn.execute(
                        "SELECT COUNT(*) FROM positions WHERE player=? AND channel=? AND symbol=? AND action=?",
                        (name, args.channel, sym, "HOLD"),
                    ).fetchone()[0]
                except Exception:  # noqa: BLE001
                    exists = 0
                if exists:
                    continue
                try:
                    db.save_position(
                        site="xueqiu", player=name, channel=args.channel, symbol=sym,
                        symbol_name=h.get("name") or "", ratio=_norm_ratio(h.get("weight")),
                        action="HOLD", post_time="", post_url=f"https://xueqiu.com/{uid}",
                        raw_text=f"cube:{cube.get('cube_name','')}",
                        access_level="free", access_hint="",
                    )
                    n_pos += 1
                except Exception as e:  # noqa: BLE001
                    print(f"  [持仓写入失败] {name}/{sym}: {e}")
    db.close()
    print(f"[完成] 雪球组合持仓入库：选手 {n_player} 人，持仓 {n_pos} 条 -> {db.path}")
    return 0


if __name__ == "__main__":
    # 2026-10-30 退役闸（同 xueqiu_person.py / viewer/config.XUEQIU_ENABLED）
    try:
        from viewer.config import XUEQIU_ENABLED
    except Exception:  # noqa: BLE001
        XUEQIU_ENABLED = False
        print("[退役] 读不到 viewer/config.XUEQIU_ENABLED，按停用处理")
    if not XUEQIU_ENABLED:
        print("[退役] 雪球组合入库已停用（config.XUEQIU_ENABLED=False），不入库。")
        sys.exit(0)
    try:
        sys.exit(main())
    except Exception:  # noqa: BLE001
        traceback.print_exc()
        sys.exit(1)
