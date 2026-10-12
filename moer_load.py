# -*- coding: utf-8 -*-
"""摩尔金融抓取 - 入库段。

读取 moer_crawler.cjs 产出的 moer_posts.json，写入 viewer/app.db：
  - players 表：每个发帖作者 upsert 为 site='moer' 的选手（channel='E' 组合跟随类）。
  - positions 表：按帖子识别的 (代码, 动作) 写入（去重）。
  - trades 表：同样写入一笔交易记录（保留动作/时间语义），与 positions 二选一以防重复，
    这里 positions 与 trades 都写但按各自 key 去重。

用法：.venv\Scripts\python.exe moer_load.py [--json moer_posts.json]
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


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", default="moer_posts.json")
    ap.add_argument("--channel", default="E", help="摩尔持仓渠道标签（默认 E=组合跟随）")
    args = ap.parse_args()

    jf = ROOT / args.json
    if not jf.exists():
        print("[错误] 未找到", jf, "— 请先运行 moer_crawler.cjs 抓取出 JSON")
        return 2
    data = json.loads(jf.read_text(encoding="utf-8"))
    posts = data.get("posts", [])
    if not posts:
        print("[提示] posts 为空，可能 URL 不对/被拦截；errors=", data.get("errors"))
        return 0

    try:
        from viewer.db import Database
    except Exception as e:  # noqa: BLE001
        print("[错误] 导入 viewer.db 失败：", e)
        return 2

    db = Database()
    n_player = n_pos = n_tr = 0
    for p in posts:
        author = (p.get("author") or "摩尔用户").strip() or "摩尔用户"
        try:
            db.save_player(site="moer", name=author, channel=args.channel,
                           rank_position=None, ranking_score=0, is_top_player=1,
                           is_certified=0, source_url=p.get("url") or "",
                           tier=args.channel, tier_weight=0.7, influence=70.0,
                           is_visible=1, platform="moer", followers=0)
            n_player += 1
        except Exception as e:  # noqa: BLE001
            print(f"  [选手写入失败] {author}: {e}")
        for c in p.get("codes", []):
            sym = str(c.get("symbol", "")).strip()
            if len(sym) != 6:
                continue
            action = (c.get("action") or "HOLD").upper()
            # positions 去重
            try:
                exists = db.conn.execute(
                    "SELECT COUNT(*) FROM positions WHERE player=? AND channel=? AND symbol=? AND action=?",
                    (author, args.channel, sym, action),
                ).fetchone()[0]
            except Exception:  # noqa: BLE001
                exists = 0
            if not exists:
                try:
                    db.save_position(site="moer", player=author, channel=args.channel, symbol=sym,
                                     symbol_name="", ratio=None, action=action,
                                     post_time="", post_url=p.get("url") or "",
                                     raw_text=(p.get("text") or "")[:400],
                                     access_level="free", access_hint="")
                    n_pos += 1
                except Exception as e:  # noqa: BLE001
                    print(f"  [持仓写入失败] {author}/{sym}: {e}")
            # trades 去重
            try:
                exists_t = db.conn.execute(
                    "SELECT COUNT(*) FROM trades WHERE author=? AND symbol=? AND action=?",
                    (author, sym, action),
                ).fetchone()[0]
            except Exception:  # noqa: BLE001
                exists_t = 0
            if not exists_t:
                try:
                    db.save_trade(site="moer", author=author, post_id=f"moer_{abs(hash(sym+action))}",
                                  post_url=p.get("url") or "", post_time="", action=action,
                                  symbol=sym, symbol_name="", price=None, quantity=None,
                                  raw_text=(p.get("text") or "")[:400],
                                  buy_time="", buy_price=None, current_price=None,
                                  sell_time="", sell_price=None, hold_since="",
                                  access_level="free", access_hint="")
                    n_tr += 1
                except Exception as e:  # noqa: BLE001
                    print(f"  [交易写入失败] {author}/{sym}: {e}")
    db.close()
    print(f"[完成] 摩尔入库：选手 {n_player} 人，持仓 {n_pos} 条，交易 {n_tr} 条 -> {db.path}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:  # noqa: BLE001
        traceback.print_exc()
        sys.exit(1)
