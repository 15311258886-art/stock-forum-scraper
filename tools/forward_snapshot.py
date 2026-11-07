# -*- coding: utf-8 -*-
"""前向记账 · 第一棒：落盘当天所有可验证判断 —— 2026-11-05

## 为什么需要这一步

决策台（`pillar_item`）与三条已采纳判据都是**快照型判断**：读的是当天的舆情、
资金流、当天形态。它们无法用历史数据重建（改了任何一处上游，历史就对不上），
所以验证它们的唯一办法是**前向记账** —— 今天把「它说了什么」原样写下来，
到期再用真实收盘价回看。这支脚本负责「写下来」这一步。

## 记什么

1. **决策台记录**：`viewer/app.db` 的 `pillar_item`
   （真实字段是 `run_date / pillar / symbol / name / rank / score / reason / detail_json`，
   `run_date` 是 `YYYY-MM-DD` 文本，不是整数 —— 落库时转成 `snap_date` 整数）。
   `src` 记成 `pillar:<主线名>`。

   ⚠️ 决策台**不是收盘即出票**：实测 `pillar_run.created_at` 是 16:02 / 18:36 / 次日
   00:13。只在 15:35 记一次会永远漏掉当天的决策台记录，而 `pillar_item` 只写当天、
   漏了就补不回来。所以决策台默认**回看 3 个交易日**（`--pillar-lookback`），
   账本按各自的 `snap_date` 落，进场价仍由 `forward_score` 按「该日次日」算，口径不变。
2. **三条已采纳判据的当日触发名单**（只算快照日）：
   · `oss_near_high`          距 250 日最高收盘 ≥ -25%
   · `oss_reversal20`         20 日累计跌幅 ≥ 20%
   · `pb_vol_price_diverge`   创 20 日新高且量比 < 0.8（排除型）
   `src` 直接用 `tools/cond_engine.py::TOOLS` 的键，**判据函数一行不改、直接调用**
   （`cond_engine` 已经把 `viewer/playbook.py` 的判据并进 `TOOLS`，
   指标全部来自 `viewer/indicators.compute_indicators`）。指标与判据的公式不在本文件里。

## 落在哪

`viewer/app.db` 的新表 `forward_book`（`CREATE TABLE IF NOT EXISTS`，幂等）：

    id, snap_date, src, code, name, entry_date, entry_px, skip_reason,
    signal_json, created_at        唯一索引 (snap_date, src, code)

重复跑同一天不会产生重复行（`ON CONFLICT ... DO UPDATE`，且**不覆盖**
`entry_date / entry_px / skip_reason` —— 那三列是回填脚本的领地，重跑快照不该抹掉）。

## 安全退出（退出码 0）

  · 今天不是交易日（周末/节假日）→ 打印原因后退出
  · 今天的 K 线还没补完（覆盖率 < 90%）→ 打印原因后退出，避免记下半张盘

## 用法

  python tools/forward_snapshot.py                 # 记今天
  python tools/forward_snapshot.py --date 20261105 # 补记某一天
  python tools/forward_snapshot.py --force         # 无视覆盖率闸门
  python tools/forward_snapshot.py --limit 300     # 只扫前 300 只（调试）
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:                                                          # 直接跑：tools/ 在 sys.path[0]
    import forward_lib as FL
except ImportError:                                           # `python -m tools.forward_snapshot`
    from tools import forward_lib as FL                       # noqa: E402
from viewer.indicators import compute_indicators              # noqa: E402

# 判据键 → (人读的名字, 角色)。角色只用于报告措辞：排除型判据「负超额」才是好事。
CRITERIA: tuple[tuple[str, str, str], ...] = (
    ("oss_near_high", "距 250 日最高收盘 ≥ -25%", "选股"),
    ("oss_reversal20", "20 日累计跌幅 ≥ 20%", "选股"),
    ("pb_vol_price_diverge", "创 20 日新高且量比 < 0.8", "排除"),
)


def _load_tools() -> dict:
    """取 `cond_engine.TOOLS`（判据唯一真源）。

    导入失败就如实报错退出 —— 不要在本地另抄一份判据，那会让账本和回测对不上。
    """
    from tools.cond_engine import TOOLS       # noqa: E402
    return TOOLS


def _build_X(code: str, rows: list[tuple]) -> dict | None:
    """按 `tools/cond_engine.py::main` 的同一套口径构好判据输入 `X`。

    口径逐行照抄该文件（同样的列名、同样的字段集合），差别只有两点：
      ① 只取「到 snap_date 为止的最后 TRAIL_BARS 根」，不读全历史（单日只需末根）；
      ② 返回 None 表示这只票根数不够/算崩，调用方跳过。
    指标一律来自 `compute_indicators`，本函数不定义任何指标。
    """
    if len(rows) < FL.MIN_BARS_CRIT:
        return None
    df = pd.DataFrame(rows, columns=["d", "open", "close", "high", "low", "volume"])
    df["date"] = pd.to_datetime(df["d"].astype(str), format="%Y%m%d")
    df["code"] = code
    try:
        ind = compute_indicators(df)
    except Exception:      # noqa: BLE001 —— 单只票算崩不该拖垮整轮
        return None

    c = df["close"].to_numpy(np.float64)
    v = df["volume"].to_numpy(np.float64)
    prev = np.concatenate(([np.nan], c[:-1]))
    with np.errstate(divide="ignore", invalid="ignore"):
        ret1 = (c / prev - 1.0) * 100.0
        mom20 = (c / np.concatenate((np.full(20, np.nan), c[:-20])) - 1.0) * 100.0
        high250 = pd.Series(c).rolling(251).max().to_numpy(np.float64)
        dist_high250 = (c / high250 - 1.0) * 100.0
        ma20 = ind["ma20"].to_numpy(np.float64)
        ma20_dev = (c / ma20 - 1.0) * 100.0
        atr_pct = ind["atr"].to_numpy(np.float64) / c * 100.0

    X = dict(close=c, volume=v, ret1=ret1, mom20=mom20,
             dist_high250=dist_high250, ma20_dev=ma20_dev, atr_pct=atr_pct,
             ma5=ind["ma5"].to_numpy(np.float64),
             ma20=ma20,
             ma60=ind["ma60"].to_numpy(np.float64),
             ma200=ind["ma200"].to_numpy(np.float64),
             macd=ind["macd"].to_numpy(np.float64),
             rsi6=ind["rsi6"].to_numpy(np.float64),
             wr6=ind["wr6"].to_numpy(np.float64),
             cci=ind["cci"].to_numpy(np.float64),
             volume_ratio=ind["volume_ratio"].to_numpy(np.float64))
    # 判据库需要的字段（ma10/macds/kdjk/kdjd/boll*/high/low）由 playbook 自己补，
    # 与 cond_engine 完全一致的调用方式。
    from viewer.playbook import enrich_X as _pb_enrich
    _pb_enrich(X, df, ind)
    X["_limit_thr"] = FL.limit_thr(code)
    return X


def _metrics(X: dict) -> dict:
    """记下判据用到的那几个量（只读取，不再算任何公式）。"""
    def last(key: str) -> float | None:
        arr = X.get(key)
        if arr is None or len(arr) == 0:
            return None
        try:
            x = float(arr[-1])
        except Exception:      # noqa: BLE001
            return None
        return round(x, 4) if np.isfinite(x) else None
    return {"close": last("close"), "dist_high250": last("dist_high250"),
            "mom20": last("mom20"), "volume_ratio": last("volume_ratio"),
            "ret1": last("ret1")}


def _upsert(con: sqlite3.Connection, snap: int, src: str, code: str,
            name: str, signal: dict, created: str) -> None:
    """写一条账本记录（幂等）。

    `ON CONFLICT ... DO UPDATE` 只更新 `name / signal_json`，**不动**
    `entry_date / entry_px / skip_reason` —— 那三列归 `forward_score.py` 回填，
    重跑当天的快照不该把它们抹回 NULL（否则每跑一次快照就要重新回填一遍）。
    """
    con.execute(
        "INSERT INTO forward_book (snap_date, src, code, name, signal_json, created_at) "
        "VALUES (?,?,?,?,?,?) "
        "ON CONFLICT(snap_date, src, code) DO UPDATE SET "
        "  name=excluded.name, signal_json=excluded.signal_json",
        (int(snap), src, str(code), name or "", json.dumps(signal, ensure_ascii=False),
         created))


def _exists(con: sqlite3.Connection, snap: int, src: str, code: str) -> bool:
    row = con.execute(
        "SELECT 1 FROM forward_book WHERE snap_date=? AND src=? AND code=?",
        (int(snap), src, str(code))).fetchone()
    return row is not None


def collect_pillar(con_app: sqlite3.Connection, snap: int,
                   names: dict[str, str]) -> list[dict]:
    """决策台某一天的记录 → 账本行。表结构按真实字段读，不猜。

    真实结构（2026-11-05 核对 `PRAGMA table_info(pillar_item)`）：
        run_date TEXT 'YYYY-MM-DD' / pillar TEXT / symbol TEXT / name TEXT /
        rank INTEGER / score REAL / reason TEXT / detail_json TEXT
        PRIMARY KEY (run_date, pillar, symbol)
    注意是 `run_date`（文本，带横线）和 `symbol`，不是 `date` / `code`。
    """
    iso = "%d-%02d-%02d" % (snap // 10000, snap // 100 % 100, snap % 100)
    rows = con_app.execute(
        "SELECT run_date, pillar, symbol, name, rank, score, reason, detail_json "
        "FROM pillar_item WHERE run_date=? ORDER BY pillar, rank", (iso,)).fetchall()
    out: list[dict] = []
    for r in rows:
        detail = None
        if r["detail_json"]:
            try:
                detail = json.loads(r["detail_json"])
            except Exception:      # noqa: BLE001 —— 原文保留，不让脏 JSON 拖垮记账
                detail = {"_raw": r["detail_json"][:500]}
        out.append({
            "snap": snap,
            "src": FL.PILLAR_PREFIX + str(r["pillar"]),
            "code": str(r["symbol"]),
            "name": str(r["name"] or names.get(str(r["symbol"]), "")),
            "signal": {"kind": "pillar", "pillar": r["pillar"],
                       "rank": r["rank"], "score": r["score"],
                       "reason": r["reason"], "detail": detail,
                       "run_date": r["run_date"]},
        })
    return out


def collect_criteria(con_kline: sqlite3.Connection, snap: int, names: dict[str, str],
                     limit: int = 0) -> tuple[list[dict], dict[str, int]]:
    """三条已采纳判据的当日触发名单。

    返回 (账本行, {判据键: 触发数})。扫描口径：snap_date 当天有 bar、是真 A 股、
    且至少有 MIN_BARS_CRIT 根历史的票（次新股给不出 250 日新高，算出来的
    NaN 会把「判不了」伪装成「没触发」，所以直接不认，与 toolbox 的说明一致）。
    """
    tools = _load_tools()
    missing = [k for k, _n, _r in CRITERIA if k not in tools]
    if missing:
        raise SystemExit("cond_engine.TOOLS 里找不到判据 %s —— 判据被改名/删除了？"
                         % ", ".join(missing))

    codes = [str(r[0]) for r in con_kline.execute(
        "SELECT DISTINCT code FROM kline WHERE d=?", (int(snap),))]
    codes = [c for c in codes if FL.is_ashare(c)]
    codes.sort()
    if limit:
        codes = codes[:limit]

    out: list[dict] = []
    hits = {k: 0 for k, _n, _r in CRITERIA}
    t0 = time.time()
    for i, code in enumerate(codes):
        rows = FL.load_bars(con_kline, code, snap)
        X = _build_X(code, rows)
        if X is None:
            continue
        for key, _label, _role in CRITERIA:
            try:
                mask = tools[key]["fn"](X)
            except Exception:      # noqa: BLE001 —— 判据内部异常不该中断整轮
                continue
            if len(mask) == 0 or not bool(mask[-1]):
                continue
            hits[key] += 1
            out.append({
                "src": key,
                "code": code,
                "name": names.get(code, ""),
                "signal": {"kind": "criterion", "tool": key,
                           "name": tools[key].get("note", ""),
                           "role": _role,
                           "metrics": _metrics(X)},
            })
        if (i + 1) % 1000 == 0:
            print("  扫描 %d/%d 只（%.0fs）" % (i + 1, len(codes), time.time() - t0))
    print("  扫描完成 %d 只，用时 %.0fs" % (len(codes), time.time() - t0))
    return out, hits


def _persist(con_app: sqlite3.Connection, rows: list[dict],
             created: str, retries: int = 4, delay: float = 20.0) -> tuple[dict, dict]:
    """把当天所有记录写进账本（一个事务）。返回 (新增数, 已存在数)。

    **为什么要重试**：`viewer/app.db` 同时被查看器服务端的日更任务写
    （`indicators_daily` 实测一轮几十分钟），本轮第一次记账（2026-11-05）
    就撞到 `database is locked`。扫描要 2 分多钟，因为写锁失败重扫一遍太浪费，
    所以把「写」收成一小段并在这里重试；事务失败必须先 rollback 再重来。

    每条记录自带 `snap`（决策台回看多天时，snap_date 各不相同）。
    """
    last: Exception | None = None
    for attempt in range(max(1, retries)):
        new_cnt = {"pillar": 0, "crit": 0}
        upd_cnt = {"pillar": 0, "crit": 0}
        try:
            for row in rows:
                kind = "pillar" if row["src"].startswith(FL.PILLAR_PREFIX) else "crit"
                seen = _exists(con_app, int(row["snap"]), row["src"], row["code"])
                _upsert(con_app, int(row["snap"]), row["src"], row["code"], row["name"],
                        row["signal"], created)
                (upd_cnt if seen else new_cnt)[kind] += 1
            con_app.commit()
            return new_cnt, upd_cnt
        except sqlite3.OperationalError as e:
            last = e
            try:
                con_app.rollback()
            except Exception:      # noqa: BLE001
                pass
            locked = "locked" in str(e).lower() or "busy" in str(e).lower()
            if locked and attempt + 1 < max(1, retries):
                print("  [app.db] 被占用（%s），%.0fs 后重试写账（第 %d/%d 次）"
                      % (e, delay, attempt + 1, retries))
                time.sleep(delay)
                continue
            raise
    raise last if last else RuntimeError("写账失败")


def main() -> int:
    FL.setup_stdout()
    ap = argparse.ArgumentParser(description="前向记账 · 落盘当日判断")
    ap.add_argument("--date", type=int, default=0, help="信号日 YYYYMMDD，默认今天")
    ap.add_argument("--force", action="store_true", help="跳过 K 线覆盖率闸门")
    ap.add_argument("--limit", type=int, default=0, help="只扫前 N 只（调试）")
    ap.add_argument("--pillar-lookback", type=int, default=3,
                    help="决策台回看几个交易日（默认 3；决策台常 16:00 后才落库，甚至拖到次日）")
    args = ap.parse_args()

    snap = int(args.date) if args.date else FL.today_int()
    print("[forward_snapshot] 开始 %s ｜ 信号日 %d" % (FL.now_str(), snap))

    try:
        con_kline = FL.connect_kline()
    except sqlite3.Error as e:
        print("!! %s" % e)
        print("   本次不记账（不是成功，是没数据可记）；等日更写完再跑一次即可。")
        return 0

    try:
        cnt_today = FL.day_bar_count(con_kline, snap)
        if cnt_today == 0:
            print("[forward_snapshot] %d 在 data/kline_daily.db 里没有行情"
                  "（周末/节假日，或当日 K 线还没更新）。本次不记账，正常退出。" % snap)
            return 0
        prev = con_kline.execute("SELECT MAX(d) FROM kline WHERE d<?", (snap,)).fetchone()
        prev_d = int(prev[0]) if prev and prev[0] is not None else 0
        cnt_prev = FL.day_bar_count(con_kline, prev_d) if prev_d else 0
        if cnt_prev and cnt_today < cnt_prev * FL.COVERAGE_MIN:
            msg = ("[forward_snapshot] 当日 K 线覆盖不足：%d 只有 %d 只，上一交易日 %d 有 %d 只"
                   "（阈值 %.0f%%）。多半是日更还没补完 —— 本次不记账，稍后重跑。"
                   % (snap, cnt_today, prev_d, cnt_prev, FL.COVERAGE_MIN * 100))
            if not args.force:
                print(msg + "（确要记账请加 --force）")
                return 0
            print(msg + "（--force：仍然记账）")
        print("  当日 %d 只 / 上一交易日(%d) %d 只" % (cnt_today, prev_d, cnt_prev))

        con_app = FL.connect_app()
        try:
            FL.ensure_schema(con_app)
            names = FL.code_name_map(con_app)
            created = FL.now_str()

            # ---- 决策台：回看若干天 ----------------------------------------
            # 为什么要回看：`pillar_run.created_at` 实测是 16:02 / 18:36 / 次日 00:13，
            # 决策台**收盘后几小时才落库**（服务端调度 16:00 起跑，有时拖到深夜）。
            # 只在 15:35 记一次快照会永远漏掉当天的决策台记录，而且漏了就补不回来
            # （`pillar_item` 只写当天）。所以这里默认回看 3 个交易日，把「迟到落库」
            # 和「机器当天空闲」两种情况一起兜住 —— 账本按各自的 snap_date 落，
            # 进场价照样由 forward_score 从「该 snap_date 的次日」算，口径不变。
            lookback = max(1, int(args.pillar_lookback))
            cal = FL.trading_dates(con_kline)
            hist = [d for d in cal if d <= snap][-lookback:]
            pillar_rows: list[dict] = []
            for d in hist:
                got = collect_pillar(con_app, d, names)
                pillar_rows.extend(got)
                print("  决策台 %d：%d 条" % (d, len(got)))
            if not pillar_rows:
                print("  （回看的 %d 个交易日里决策台都没有记录 —— 只记判据。"
                      "决策台出票依赖当日数据完整度，没有记录也是事实，不补造。）"
                      % len(hist))

            # ---- 三条已采纳判据：只算快照日 --------------------------------
            crit_rows, hits = collect_criteria(con_kline, snap, names, args.limit)
            for key, label, role in CRITERIA:
                print("  判据 %-22s %-28s %s：%d 只"
                      % (key, label, role, hits[key]))
            for row in crit_rows:
                row["snap"] = snap

            new_cnt, upd_cnt = _persist(con_app, pillar_rows + crit_rows, created)

            total = con_app.execute(
                "SELECT COUNT(*) FROM forward_book WHERE snap_date>=?",
                (min(hist) if hist else snap,)).fetchone()[0]
            print("\n== 落盘完成 ==")
            print("  决策台：新增 %d / 已存在 %d" % (new_cnt["pillar"], upd_cnt["pillar"]))
            print("  判据  ：新增 %d / 已存在 %d" % (new_cnt["crit"], upd_cnt["crit"]))
            print("  回看窗口 %d~%d 内账本共 %d 条" % (min(hist) if hist else snap, snap,
                                                     int(total)))

            by_src = con_app.execute(
                "SELECT snap_date, src, COUNT(*) n FROM forward_book WHERE snap_date>=? "
                "GROUP BY snap_date, src ORDER BY snap_date DESC, n DESC",
                (min(hist) if hist else snap,)).fetchall()
            print("  分来源：")
            for r in by_src:
                print("    %d  %-24s %3d" % (int(r["snap_date"]), r["src"], int(r["n"])))
        finally:
            con_app.close()
    except sqlite3.OperationalError as e:
        # 扫描中途库被日更抢走锁（WAL checkpoint 等）→ 本次不落半分账，下次再来。
        print("!! 扫描过程中数据库不可用：%s" % e)
        print("   本次未记账（事务未提交）。稍后重跑。")
        return 0
    finally:
        con_kline.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
