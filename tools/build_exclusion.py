# -*- coding: utf-8 -*-
"""构建"剔除池 / 干净宇宙" —— 选股负面清单的离线计算与落库。

剔除理由（与 viewer/config.EXCLUSION_REASONS 同源，白名单外一律不写）：
  st            名称含 ST / *ST / 退 / 警示 / 风险（退市/风险警示，数据太拉胯）。
  no_kline      名称字典里有、但 K 线库缺失（无法做技术/量化分析）。
  illiquid      60 日均成交额低于阈值（极弱流动性，实际不可交易）。      ← 2026-10-31 新增
  （blogger_noise 已于 2026-10-30 移除，理由见 compute() 内注释；不删除博主数据。）

实证依据（已核实）：
  blogger_perf 969 条：平均 T+5 收益 -3.33%，577 跌 / 250 涨（60% 跌）。
  80 只被多博主点名票：平均 T+5 -2.04%，53/80（66%）跌。
  illiquid：全市场 60 日均额 最小 22 万 / p1 869 万 / 中位 1.25 亿；
            阈值 1,000 万/日 命中 79 只（1.4%）。口径与分档见 viewer/config 的说明块。

用法：
  python tools/build_exclusion.py            # 计算并落库 + 打印覆盖率
  python tools/build_exclusion.py --dry-run  # 只打印，不写库
可调用的 main() 供 sched_job 直接调用（**必须不带 argv**）。

────────────────────────────────────────────────────────────────────────────
2026-10-31 整改（测评 P0-B）。原文件有四处缺陷，逐条对应如下：

① `a = ap.parse_args()` 读的是**父进程的** sys.argv。
   由 sched_job 以 `build_exclusion.main()` 调用时，argv 里带着 "build_exclusion" 这个词，
   而本脚本的参数表里没有它 ⇒ argparse 打印用法后抛 SystemExit(2)。
   而 sched_job 只 `except Exception`，SystemExit 继承 BaseException、捕不到 ⇒
   既没有 traceback、也没有 FAILED 行，日志里干干净净 ⇒ 自 10-26 上线起 **11 次执行 11 次
   退出码 2、0 次成功**，而这张表里却有 224 行。
   改法：`main(argv=None)`，argv 为 None 时按"空参数"解析；命令行入口显式传 sys.argv[1:]。
   这样"被当库调用"与"被当脚本跑"两条路不再共用一个 argv。

② `compute()` **没有 illiquid 分支**（白名单里有、代码里没有）。
   后果：表里 illiquid 恒为 0，而"干净宇宙"里留着 000544（60 日 ADV 22.1 万元）。
   改法：新增 illiquid 分支，阈值与窗口全部读 config，不写死在代码里。

③ 写库是 `DELETE FROM` 紧跟 `INSERT`，**两条语句不在同一事务**（默认 autocommit）。
   中间任何一次失败（磁盘满 / 被 kill / 库锁超时）都会留下一张空表，
   而消费侧读到空表 = 一只都不剔 = 与"没有剔除逻辑"完全同形。
   改法：`BEGIN IMMEDIATE` 包住 DELETE + INSERT + 批次登记，异常则 ROLLBACK。

④ 表里只有四列（code / reason / detail / updated_at）：没有写入者、没有入口、没有批次。
   于是"这 224 行是谁写的"在产物结构上不可归因；而 DELETE + 全量 INSERT 的写法
   让这张表**没有历史** —— 任何一次成功都会把之前所有批次抹掉。
   改法：新增 batch_id / pool_asof / writer 三列 + `exclusion_pool_batch`（批次元）
   与 `exclusion_pool_history`（append 写），并保留最近 N 个批次供多批次比较。
"""
import os
import re
import sys
import json
import sqlite3
import argparse
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP_DB = os.environ.get("VIEWER_DB") or os.path.join(ROOT, "viewer", "app.db")
KL_DB = os.path.join(ROOT, "data", "kline_daily.db")

_ST_RE = re.compile(r"ST|\*ST|退|警示|风险")

# 写库的入口标识。命令行跑 = cli；被调度器调用 = sched_job。
# 这个值会落进 exclusion_pool.writer 与批次表 —— 2026-10-31 之前这张表没有这一列，
# 于是"224 行是谁写的"根本无从回答。多入口并存时，产物必须能区分来源。
_DEFAULT_WRITER = "cli"


def _cfg(name, default):
    """读 viewer.config 的参数；读不到就用传入的默认值（不让本脚本因 config 失败而停）。"""
    try:
        from viewer import config as CFG
        return getattr(CFG, name, default)
    except Exception:  # noqa: BLE001
        return default


def _con(path):
    return sqlite3.connect(path, timeout=60)


def _adv60_map():
    """返回 (map{code: (adv_yuan, obs_n)}, window_from, window_to, threshold_yuan)。

    ADV = 窗口内 `close × volume` 的均值。**单位已核**：data/kline_daily.db 的 kline.v
    存的是**股**不是手（000001 于 20261030 为 186,424,507 股 × 11.64 元 = 21.7 亿元；
    若按"手"算会得 2,170 亿元，不成立。000544 复算得 22.1 万元，与测评报告中的 22 万一致）。
    这里不复用 stock_daily_snapshot.volume —— 那一列的量纲按板块混存（688/689 存股、
    其余存手，13,798 行"手" / 1,849 行"股"），直接乘会错 100 倍且方向不定。
    """
    th = _cfg("ILLIQUID_ADV60_MIN_YUAN", 10_000_000)
    win = int(_cfg("ILLIQUID_ADV_WINDOW_DAYS", 60))
    min_obs = int(_cfg("ILLIQUID_MIN_OBS", 40))
    if th is None:
        return {}, None, None, None
    if not os.path.exists(KL_DB):
        print("[warn] kline 库不存在，illiquid 跳过：", KL_DB)
        return {}, None, None, th
    kc = _con(KL_DB)
    try:
        ds = [r[0] for r in kc.execute(
            "SELECT DISTINCT d FROM kline ORDER BY d DESC LIMIT ?", (win,)).fetchall()]
        if not ds:
            print("[warn] kline 无数据，illiquid 跳过")
            return {}, None, None, th
        lo, hi = min(ds), max(ds)
        rows = kc.execute(
            "SELECT code, AVG(c * v) AS adv, COUNT(*) AS n FROM kline "
            "WHERE d >= ? AND c IS NOT NULL AND v IS NOT NULL "
            "GROUP BY code HAVING n >= ?", (lo, min_obs)).fetchall()
        m = {r[0]: (float(r[1] or 0.0), int(r[2] or 0)) for r in rows}
        return m, lo, hi, th
    finally:
        kc.close()


def compute():
    """返回 exclusion rows: list[(code, reason, detail)]。"""
    con = _con(APP_DB)
    con.row_factory = sqlite3.Row
    rows = []

    # --- 1) ST / 退市 / 风险警示 ---
    try:
        for code, name in con.execute("SELECT code, name FROM stock_names").fetchall():
            if name and _ST_RE.search(name):
                rows.append((code, "st", "名称含ST/退/警示/风险"))
    except Exception as e:
        print("[warn] stock_names 读取失败:", e)

    # --- 2) 博主噪声：已于 2026-10-30 移除 ---
    # 原逻辑把 blogger_overlap 里的票直接塞进剔除池（reason=blogger_noise，永久剔除）。
    # 那是「拿沉淀快照绑定个股」的最极端形态：推荐时点之后股价早已走完，
    # 有效期按进哥的估算是 20 天以内，而剔除是永久的。博主源自 2026-10-27 起也停更了。
    # 避坑需求改由 viewer/avoid.py 承担：当日量价 + 近 15 日利空公告，实时算、不永久标记。
    # 剔除池回归「客观硬垃圾」单一职责：st / no_kline / illiquid。

    con.close()

    # --- 3) 无 K 线 ---
    kline_codes = set()
    if os.path.exists(KL_DB):
        kc = _con(KL_DB)
        try:
            kline_codes = set(r[0] for r in kc.execute(
                "SELECT DISTINCT code FROM kline").fetchall())
            ac = _con(APP_DB)
            try:
                all_codes = set(r[0] for r in ac.execute(
                    "SELECT code FROM stock_names").fetchall())
                for code in (all_codes - kline_codes):
                    rows.append((code, "no_kline", "K线库缺失，无法分析"))
            finally:
                ac.close()
        except Exception as e:
            print("[warn] kline 读取失败:", e)
        finally:
            kc.close()
    else:
        print("[warn] kline 库不存在，no_kline / illiquid 均跳过：", KL_DB)

    # --- 4) 极弱流动性（2026-10-31 新增：这一条此前只存在于白名单和前端标签里）---
    adv_map, lo, hi, th = _adv60_map()
    if th is not None and adv_map:
        n_ill = 0
        for code, (adv, obs) in adv_map.items():
            if code not in kline_codes:
                continue                      # 已在 no_kline 里，不重复计理由
            if adv < float(th):
                rows.append((code, "illiquid",
                             "60日均额 %.1f万 < %.0f万（窗口 %s-%s，%d 个交易日）"
                             % (adv / 1e4, float(th) / 1e4, lo, hi, obs)))
                n_ill += 1
        print("[illiquid] 阈值 %.0f 万元/日，窗口 %s-%s，命中 %d 只"
              % (float(th) / 1e4, lo, hi, n_ill))

    return rows


def _migrate(con):
    """给既有表补列（ALTER TABLE ADD COLUMN 不可重复，故先查 PRAGMA）。"""
    have = set(r[1] for r in con.execute("PRAGMA table_info(exclusion_pool)").fetchall())
    for col, decl in (("batch_id", "TEXT"), ("pool_asof", "TEXT"),
                      ("writer", "TEXT"), ("adv60", "REAL")):
        if col not in have:
            con.execute("ALTER TABLE exclusion_pool ADD COLUMN %s %s" % (col, decl))


def main(argv=None):
    """argv=None ⇒ 按空参数解析（被当库调用时的正确姿势）。

    2026-10-31 整改（测评 P0-B ①）：原签名是 `main()` 且内部 `ap.parse_args()`，
    被 sched_job 调用时解析到的是父进程 argv（含 "build_exclusion" 一词）⇒ SystemExit(2)。
    现在把「是不是从命令行来的」显式区分：命令行入口传 sys.argv[1:]，其余一律空表。
    """
    ap = argparse.ArgumentParser(description="构建剔除池/干净宇宙")
    ap.add_argument("--dry-run", action="store_true", help="只打印不写库")
    ap.add_argument("--writer", default=_DEFAULT_WRITER,
                    help="写入者标识（cli / sched_job / manual）")
    a = ap.parse_args([] if argv is None else list(argv))
    if a.writer == _DEFAULT_WRITER and argv is None:
        # 被库调用时默认就是 sched_job；显式传 --writer 时以传入为准。
        a.writer = "sched_job"

    rows = compute()
    # 去重（同一 code 多理由取首个；compute() 的顺序即理由优先级 st > no_kline > illiquid）
    seen = {}
    for code, reason, detail in rows:
        seen.setdefault(code, (reason, detail))
    dedup = [(c, v[0], v[1]) for c, v in seen.items()]

    # 统计
    from collections import Counter
    cnt = Counter(r[1] for r in dedup)

    # 干净宇宙规模
    ac = _con(APP_DB)
    total = ac.execute("SELECT COUNT(*) FROM stock_names").fetchone()[0]
    ac.close()
    clean = total - len(dedup)

    print("=" * 60)
    print("剔除池构建结果")
    print("  全A 名称字典       : %d" % total)
    for reason in ("st", "no_kline", "illiquid", "blogger_noise"):
        print("  剔除 %-16s : %d" % (reason, cnt.get(reason, 0)))
    print("  剔除合计(去重)     : %d" % len(dedup))
    print("  干净宇宙(可入选)   : %d  (%.1f%% of A)"
          % (clean, 100 * clean / total if total else 0))
    print("=" * 60)

    if a.dry_run:
        print("[dry-run] 未写库")
        return 0

    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    batch_id = datetime.now().strftime("%Y%m%d_%H%M%S") + "_%05d" % len(dedup)

    con = _con(APP_DB)
    try:
        con.execute(
            "CREATE TABLE IF NOT EXISTS exclusion_pool ("
            "code TEXT PRIMARY KEY, reason TEXT NOT NULL, detail TEXT, updated_at TEXT)")
        _migrate(con)
        con.execute(
            "CREATE TABLE IF NOT EXISTS exclusion_pool_batch ("
            "batch_id TEXT PRIMARY KEY, pool_asof TEXT, writer TEXT, rows INTEGER, "
            "counts_json TEXT, total_a INTEGER, clean_universe INTEGER, created_at TEXT)")
        con.execute(
            "CREATE TABLE IF NOT EXISTS exclusion_pool_history ("
            "batch_id TEXT NOT NULL, code TEXT NOT NULL, reason TEXT NOT NULL, detail TEXT, "
            "PRIMARY KEY (batch_id, code))")

        # ③ 三件事必须在同一个事务里：清空 → 插入 → 登记批次。
        # 不包事务的话，DELETE 与 INSERT 之间任何一次失败都会留下一张空表，
        # 而空表 = 一只都不剔 = 与"没有剔除逻辑"完全同形，且没有任何告警。
        con.execute("BEGIN IMMEDIATE")
        try:
            con.execute("DELETE FROM exclusion_pool")
            con.executemany(
                "INSERT OR REPLACE INTO exclusion_pool"
                "(code,reason,detail,updated_at,batch_id,pool_asof,writer) "
                "VALUES(?,?,?,?,?,?,?)",
                [(c, r, d, ts, batch_id, ts, a.writer) for c, r, d in dedup])
            con.executemany(
                "INSERT OR REPLACE INTO exclusion_pool_history(batch_id,code,reason,detail) "
                "VALUES(?,?,?,?)",
                [(batch_id, c, r, d) for c, r, d in dedup])
            con.execute(
                "INSERT OR REPLACE INTO exclusion_pool_batch"
                "(batch_id,pool_asof,writer,rows,counts_json,total_a,clean_universe,created_at) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (batch_id, ts, a.writer, len(dedup),
                 json.dumps(dict(cnt), ensure_ascii=False), total, clean, ts))
            # 保留最近 N 个批次（默认 120 ≈ 4 个月日更）。剪枝也放在同一事务里，
            # 否则会出现"旧批次已删、新批次未写"的中间态。
            keep = int(_cfg("POOL_HISTORY_KEEP_BATCHES", 120))
            old = [r[0] for r in con.execute(
                "SELECT batch_id FROM exclusion_pool_batch ORDER BY pool_asof DESC "
                "LIMIT -1 OFFSET ?", (keep,)).fetchall()]
            for b in old:
                con.execute("DELETE FROM exclusion_pool_history WHERE batch_id=?", (b,))
                con.execute("DELETE FROM exclusion_pool_batch WHERE batch_id=?", (b,))
            con.execute("COMMIT")
        except Exception:
            con.execute("ROLLBACK")
            raise
    finally:
        con.close()

    print("[ok] exclusion_pool 已落库 %d 行  batch_id=%s writer=%s"
          % (len(dedup), batch_id, a.writer))
    if cnt.get("illiquid", 0) == 0:
        # 「从不触发」必须被当成待解释的现象，不是好消息。
        print("[warn] illiquid 命中 0 只 —— 若阈值非 None，这是一个需要解释的现象，"
              "不是好消息（先查 kline 窗口与 ILLIQUID_ADV60_MIN_YUAN）。")
    return 0


if __name__ == "__main__":
    # ④ 命令行入口显式传 argv，库调用走 main() / main([])。
    sys.exit(main(sys.argv[1:]))
