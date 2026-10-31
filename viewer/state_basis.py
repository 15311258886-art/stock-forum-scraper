# -*- coding: utf-8 -*-
"""`market_state_log.basis` —— 市场状态标签的**来源溯源层**（2026-10-30 P1-a）。

## 为什么需要这一层

`market_state_log.state` 有**两个互不兼容的写入方**，同一列两种定义：

| 写入方 | 判定规则 | breadth_json |
|---|---|---|
| `viewer/pillars.py::market_state()` | 普跌 = 上涨占比<35% **或** 中位≤−1.2%；震荡 = 上涨占比<55% **或** 指数破 MA20 | 有 `up_ratio` / `up_n` / `median_pct` / `index_note` |
| `viewer/resonance.py::state_history()` | 普跌 = 中位≤−1.2%；震荡 = 中位<−0.45% **或** 等权净值破 20 日线 | 只有 `{"bench_median_pct": null}`，**结构上算不出 up_ratio** |

所以要有一个字段回答一句话：**"今天这条标签是谁判的"**。

> 注意：`state` 标签**不含持有期**。两个分类器都是纯同日口径（MA20 指**过去** 20 日）。
> 持有期错配在别处（`evo_weight.by_state_json` 是用单一 `y` 口径学的权重），
> 别把持有期塞进 `basis` —— 那会让下游误以为标签本身分 T+1/T+5 两套。

## 设计约束

1. **读路径必须容忍列不存在**：加列之前 `SELECT basis` 会抛 `no such column`，
   而本项目的习惯是 `except Exception: pass` —— 那就等于又造一个静默失效。
   所以读走 `basis_expr()`：列在就用 `COALESCE(basis, 推导值)`，列不在就只用推导值。
2. **写路径自迁移**：`ensure_column()` 幂等（ALTER + 一次性回填）。它只在**写入方**
   （日更任务、已在写域里持有连接）调用，不在请求线程里 ALTER。
3. **回填是确定性的，不是猜**：`breadth_json` 里含 `up_ratio` ⇔ pillars 口径
   （resonance 那份 JSON 是 `{"bench_median_pct": null}`，永远不含这个键）。
"""

from __future__ import annotations

import json
import sqlite3

BASIS_PILLARS = "pillars"
BASIS_RESONANCE = "resonance"
COL = "basis"
TABLE = "market_state_log"

# 回填/推导的唯一判据：breadth_json 是否含 "up_ratio"
_DERIVE_SQL = ("CASE WHEN breadth_json LIKE '%\"up_ratio\"%' THEN 'pillars' "
               "ELSE 'resonance' END")


def has_column(con: sqlite3.Connection) -> bool:
    """`market_state_log` 有没有 `basis` 列（只读，不建）。"""
    try:
        return COL in {r[1] for r in con.execute("PRAGMA table_info(%s)" % TABLE)}
    except Exception:  # noqa: BLE001
        return False


def derive_basis(breadth_json) -> str:
    """从 `breadth_json` 推导来源（与 `_DERIVE_SQL` 同判据，Python 侧版本）。"""
    try:
        obj = json.loads(breadth_json or "{}")
    except Exception:  # noqa: BLE001
        return BASIS_RESONANCE
    return BASIS_PILLARS if isinstance(obj, dict) and "up_ratio" in obj else BASIS_RESONANCE


def basis_expr(con: sqlite3.Connection) -> str:
    """可安全嵌进 SELECT 的来源表达式（列缺失时退化为推导，绝不报错）。"""
    if has_column(con):
        return "COALESCE(%s, %s)" % (COL, _DERIVE_SQL)
    return _DERIVE_SQL


def ensure_column(con: sqlite3.Connection) -> bool:
    """幂等迁移：加 `basis` 列 + 回填历史行。返回 True 表示这次真的做了迁移。

    只在写入方调用（那一刻连接本来就在写域里，与日更任务同事务上下文），
    1396 行的 UPDATE 是毫秒级。**不要在请求线程里调用本函数。**
    """
    if has_column(con):
        return False
    con.execute("ALTER TABLE %s ADD COLUMN %s TEXT" % (TABLE, COL))
    con.execute("UPDATE %s SET %s = %s WHERE %s IS NULL" % (TABLE, COL, _DERIVE_SQL, COL))
    con.commit()
    return True


def rows(con: sqlite3.Connection, limit: int | None = None) -> list:
    """整表或最近 N 行，含推导后的 `basis`。返回 list[dict]。

    列不存在时不报错（走 `basis_expr` 推导），所以调用方可以先把代码上线、后做迁移。
    """
    sql = ("SELECT trade_date, state, %s AS basis, breadth_json, note, created_at "
           "FROM %s ORDER BY trade_date ASC" % (basis_expr(con), TABLE))
    out = []
    for r in con.execute(sql):
        try:
            out.append(dict(r))
        except Exception:  # noqa: BLE001 —— 非 Row 工厂时退回按位置取
            out.append({"trade_date": r[0], "state": r[1], "basis": r[2],
                        "breadth_json": r[3], "note": r[4], "created_at": r[5]})
    if limit is not None and limit > 0:
        out = out[-int(limit):]
    return out


def latest(con: sqlite3.Connection) -> dict | None:
    """最新一行（按 trade_date，不是 rowid —— `market_state_log` 没有 id 列）。"""
    got = rows(con)
    return got[-1] if got else None


def breadth_of(row: dict | None) -> dict:
    """取 `breadth_json` 并解析成 dict（解析失败回空 dict）。"""
    if not row:
        return {}
    try:
        obj = json.loads(row.get("breadth_json") or "{}")
    except Exception:  # noqa: BLE001
        return {}
    return obj if isinstance(obj, dict) else {}
