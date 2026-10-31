# -*- coding: utf-8 -*-
"""全市场 K 线沉淀库（kbase）。

定位：把"按需抓取的 JSON 缓存（predict_kcache.json，仅 285 只）"升级为
**全 A 股日 K 沉淀库**（SQLite），覆盖 stock_names 中 6 位 A 股代码（运行期计算，
当前约 5237 只）、从上市日至今。完备性以"末根日期 == 数据集最新交易日"为口径，
而非根数阈值——根数口径会把近年 IPO 的天然短历史误判成残缺（已验证：334 只 <800 根里
330 只末根已对齐最新交易日）。

设计要点（对应进哥"沉淀不是缓存 / 内存最小"的要求）：
- 落库到独立 SQLite 文件 viewer/kbase.db，与 app.db 解耦，避免膨胀主库。
- 绝不全量载入内存：调用方按需 per-code 读取（lazy）；本模块只做落库与单只查询。
- 对外通过 KBaseBars 惰性代理（collections.abc.Mapping + 可写）无缝替换旧 JSON dict，
  原 load_kcache()["bars"] 的所有访问方式（get / [] / items / 赋值）保持兼容。
- 抓取复用 strategy_sim.fetch_kline（Sina 日线，空代理 opener 绕过本机代理劫持）。
"""
from __future__ import annotations

import os
import sqlite3
import sys
import threading
import time
import json
from collections.abc import Mapping
from datetime import datetime, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.path.join(ROOT, "viewer", "kbase.db")
KBASE_ADJ = "hfq"  # 与 strategy_sim.ADJ 一致（后复权，价格连续）


class KBase:
    def __init__(self, path: str = DB):
        self.path = path
        self._lock = threading.Lock()
        self._dates_cache: dict = {}   # 2026-10-29：交易日历按 mtime 记忆化（见 distinct_dates）
        self._series_cache: dict = {}  # 2026-10-29：全池「日期+收盘」序列按 mtime 记忆化
        self.ensure()

    # ---- schema ----------------------------------------------------------
    def ensure(self) -> None:
        con = sqlite3.connect(self.path, timeout=30)
        # 2026-10-29（本次复查发现的**锁冲突总源头**）：kbase.db 一直是 journal_mode=delete，
        # 这意味着**任何写事务都会锁死整个库文件** —— 日更补齐一跑起来（一次 DELETE+INSERT
        # 十几万行），服务端的回测/预测读请求全部排队，超时后报
        # `sqlite3.OperationalError: database is locked`（日志里多域同时报锁就是这个）。
        # 切 WAL：读不阻塞写、写不阻塞读。库里并发模型就此从「串行排队」变成「读写并行」。
        # 若此刻正好有别的进程在写、切不动 WAL，静默降级（不影响功能，下次启动再切）。
        try:
            con.execute("PRAGMA journal_mode=WAL")
            con.execute("PRAGMA synchronous=NORMAL")
        except sqlite3.OperationalError:
            pass
        con.execute(
            """CREATE TABLE IF NOT EXISTS kline(
                   code TEXT NOT NULL,
                   date TEXT NOT NULL,
                   o REAL, h REAL, l REAL, c REAL, v REAL,
                   adj TEXT,
                   PRIMARY KEY(code, date))"""
        )
        con.execute("CREATE INDEX IF NOT EXISTS idx_kline_code ON kline(code)")
        con.execute("CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT)")
        con.execute("CREATE TABLE IF NOT EXISTS suspended(code TEXT PRIMARY KEY, since TEXT, note TEXT)")
        con.commit()
        con.close()

    # ---- 基础查询 --------------------------------------------------------
    def _con(self):
        # 2026-10-29：加 busy_timeout。此前只有 connect(timeout=30)，
        # 而 timeout 只作用于「获取锁」的等待，配合下面 ensure() 里切的 WAL 才是完整方案。
        c = sqlite3.connect(self.path, timeout=30)
        c.execute("PRAGMA busy_timeout=30000")
        return c

    def count(self) -> int:
        con = self._con()
        n = con.execute("SELECT COUNT(DISTINCT code) FROM kline").fetchone()[0]
        con.close()
        return n

    def row_count(self) -> int:
        con = self._con()
        n = con.execute("SELECT COUNT(*) FROM kline").fetchone()[0]
        con.close()
        return n

    def codes(self) -> list:
        con = self._con()
        cs = [r[0] for r in con.execute("SELECT DISTINCT code FROM kline ORDER BY code")]
        con.close()
        return cs

    def has(self, code: str) -> bool:
        con = self._con()
        r = con.execute("SELECT 1 FROM kline WHERE code=? LIMIT 1", (code,)).fetchone()
        con.close()
        return r is not None

    def last_date(self, code: str):
        con = self._con()
        r = con.execute("SELECT MAX(date) FROM kline WHERE code=?", (code,)).fetchone()
        con.close()
        return r[0] if r else None

    def latest_date(self) -> str:
        """数据集内最新的交易日期（作为完备性基准）。"""
        con = self._con()
        r = con.execute("SELECT MAX(date) FROM kline").fetchone()
        con.close()
        return r[0] if r and r[0] else None

    def distinct_dates(self) -> list:
        """升序返回全部**有效**交易日（单条 SQL，避免对全市场 bars 全量迭代）。

        2026-10-29：加 mtime 记忆化。kbase.db 有 230 万+ 行，`SELECT DISTINCT date`
        实测 2~4s；回测 `_context()` 每次重建都要它一次，叠加进首次请求耗时就变成
        「打开回测要等一分多钟」。交易日历一天只变一次（盘后日更），按文件 mtime 缓存即可。

        2026-10-29 二次修复（本次复查发现，**口径级 bug**）：
          `SELECT DISTINCT date` 的语义是「只要有一票有这天的 K 线就算交易日」。
          实测踩到：2026-10-28 全市场 5239 只里只有 600707 一只（单票详情刷新顺手写进去的
          一根 K 线，而当日全市场日更并未跑完）→ 日历凭空多出一天 → 回测 `_elapsed()`
          多算 1 个交易日 → T+10 被判成「已到期但没有 K 线」（nodata 27/27），
          基准 bench 也会用这一根票算全市场均值。表现为「周期明明走完了，却全部显示缺数据」。
          修法：对**最近 lookback 个日历日**做覆盖率校验，覆盖数低于近期峰值 ratio 的日期
          视为「未成形的半日」剔除。只校验近期，历史早期（1990 年代只有几十只票）不受影响——
          回测窗口只取最近 SERIES_TAIL 个交易日，碰不到远古区间。
        """
        try:
            mt = os.path.getmtime(self.path)
        except OSError:
            mt = None
        if mt is not None and self._dates_cache.get("mtime") == mt:
            return self._dates_cache["dates"]
        con = self._con()
        try:
            ds = [r[0] for r in con.execute("SELECT DISTINCT date FROM kline ORDER BY date")]
            ds = self._drop_unformed_dates(con, ds)
        finally:
            con.close()
        if mt is not None:
            self._dates_cache = {"mtime": mt, "dates": ds}
        return ds

    @staticmethod
    def _drop_unformed_dates(con, ds: list, lookback: int = 40,
                             ratio: float = 0.3) -> list:
        """剔除「覆盖数畸低」的近期日期（半日/单票脏数据）→ 保住交易日历口径一致。"""
        if not ds:
            return ds
        recent = ds[-lookback:]
        try:
            cnt = dict(con.execute(
                "SELECT date, COUNT(*) FROM kline WHERE date >= ? GROUP BY date",
                (recent[0],)))
        except sqlite3.Error:
            return ds
        peak = max(cnt.values()) if cnt else 0
        if peak <= 0:
            return ds
        floor = peak * ratio
        drop = {d for d in recent if cnt.get(d, 0) < floor}
        if not drop:
            return ds
        return [d for d in ds if d not in drop]

    def close_series_bulk(self, codes, adj: str = KBASE_ADJ, min_date: str = "") -> dict:
        """批量取「日期 + 收盘」序列 → {code: (dates: list[str], closes: list[float|None])}。

        2026-10-29 性能修复（回测面板「加载失败」的真凶）：
          · `bars_of()` 每次调用都**新开一个连接**，并把 o/h/l/v 一起构造成 dict；
          · 回测基准要对候选池 ~745 只票取全历史 → 745 次开连接 + 约 360 万条 dict，
            实测单次 /api/backtest 要 **73s**（远超前端 15s 预算 → 面板永远"加载失败"）；
          · 回测真正用到的只有 `d` 和 `c` 两个字段 → 这里一次批量查出来，
            日期字符串去重共享（同一交易日只保留一个 str 对象），耗时与内存都降到零头。
          · `min_date`：调用方只需「最近一段」时传入起始日，3.3M 根 K 线可压到几十万根
            （内存从 ~150MB 降到 ~15MB，冷启动从 70s 级降到亚秒级）。

        返回的 dates/closes 严格同序（按 date 升序），与 `bars_of()` 的索引口径完全一致，
        因此下游 `_window_ret(k, idx, w)` 的位置语义不变（口径零漂移）。
        """
        out: dict = {}
        codes = [c for c in (codes or []) if c]
        if not codes:
            return out
        pool: dict = {}          # 日期字符串去重池：同一交易日全池共用一个 str 对象
        con = self._con()
        try:
            CH = 400             # 每批 400 只，避开 SQLite 变量数上限
            for i in range(0, len(codes), CH):
                chunk = codes[i:i + CH]
                if min_date:
                    q = ("SELECT code,date,c FROM kline WHERE adj=? AND date>=? AND code IN (%s) "
                         "ORDER BY code,date" % ",".join("?" * len(chunk)))
                    rows = con.execute(q, [adj, min_date] + chunk)
                else:
                    q = ("SELECT code,date,c FROM kline WHERE adj=? AND code IN (%s) "
                         "ORDER BY code,date" % ",".join("?" * len(chunk)))
                    rows = con.execute(q, [adj] + chunk)
                for code, dt, c in rows:
                    dt = pool.setdefault(dt, dt)
                    e = out.get(code)
                    if e is None:
                        e = out[code] = ([], [])
                    e[0].append(dt)
                    e[1].append(None if c is None else float(c))
        except sqlite3.Error:
            import logging
            logging.exception("kbase.close_series_bulk 失败（回测将退化为逐只读取）")
        finally:
            con.close()
        return out

    def ohlcv_bulk(self, codes, adj: str = KBASE_ADJ, min_date: str = "") -> dict:
        """批量取 OHLCV → {code: [(date, o, h, l, c, v), ...]}（按日期升序）。

        2026-10-29 新增（量化/ETF 面板「永远算不出来」的真凶）：
          `_universe_bars()` 原来把全市场 5239 只票的**全历史**（约 2300 万根 K 线）逐只
          `bars_of()` 读出来、再各建一个 tuple —— 实测子进程内存冲到 **16.7 GB** 被系统杀掉，
          于是量化面板永远是"计算中"、ETF 左栏永远超时。
          这里一次 SQL 批量取（日期去重共享字符串），配合调用方 `min_date` 只取需要的窗口，
          内存从 16GB 级降到几百 MB，磁盘往返从 5239 次降到十几次。
        """
        out: dict = {}
        codes = [c for c in (codes or []) if c]
        if not codes:
            return out
        pool: dict = {}
        con = self._con()
        try:
            CH = 400
            for i in range(0, len(codes), CH):
                chunk = codes[i:i + CH]
                if min_date:
                    q = ("SELECT code,date,o,h,l,c,v FROM kline WHERE adj=? AND date>=? "
                         "AND code IN (%s) ORDER BY code,date" % ",".join("?" * len(chunk)))
                    rows = con.execute(q, [adj, min_date] + chunk)
                else:
                    q = ("SELECT code,date,o,h,l,c,v FROM kline WHERE adj=? "
                         "AND code IN (%s) ORDER BY code,date" % ",".join("?" * len(chunk)))
                    rows = con.execute(q, [adj] + chunk)
                for code, dt, o, h, l, c, v in rows:
                    dt = pool.setdefault(dt, dt)
                    e = out.get(code)
                    if e is None:
                        e = out[code] = []
                    e.append((dt,
                              None if o is None else float(o),
                              None if h is None else float(h),
                              None if l is None else float(l),
                              None if c is None else float(c),
                              None if v is None else float(v)))
        except sqlite3.Error:
            import logging
            logging.exception("kbase.ohlcv_bulk 失败（调用方应退回逐只读取）")
            out = {}
        finally:
            con.close()
        return out

    def completeness(self, codes: list) -> dict:
        """日期口径完备性报告：返回每类计数与明细。

        stale = 末根 != 最新交易日 的票；stale_exempt = 其中已登记进 suspended
        豁免名单（停牌/退市/源站停更）的票。stale_exempt == stale 即表示
        "陈旧票全部已确认源站停更，无数据缺口"。
        """
        latest = self.latest_date()
        con = self._con()
        have_last = {r[0]: r[1] for r in con.execute(
            "SELECT code, MAX(date) FROM kline GROUP BY code")}
        con.close()
        suspended = self._suspended_set()
        target = set(codes)
        present = {c: have_last.get(c) for c in target if c in have_last}
        missing = [c for c in target if c not in have_last]
        complete = [c for c in present if have_last[c] == latest]
        stale = [c for c in present if have_last[c] != latest]
        stale_exempt = [c for c in stale if c in suspended]
        return {
            "latest": latest,
            "target": len(target),
            "present": len(present),
            "missing": missing,
            "complete": complete,
            "stale": stale,
            "stale_exempt": stale_exempt,
        }

    def bars_of(self, code: str, adj: str = KBASE_ADJ) -> list:
        """返回 [{d,o,h,l,c,v}]，按日期升序。无数据返回 []。"""
        con = self._con()
        rows = con.execute(
            "SELECT date,o,h,l,c,v FROM kline WHERE code=? ORDER BY date", (code,)
        ).fetchall()
        con.close()
        return [
            {"d": r[0], "o": r[1], "h": r[2], "l": r[3], "c": r[4], "v": r[5]}
            for r in rows
        ]

    def set_bars(self, code: str, bars: list, adj: str = KBASE_ADJ) -> None:
        if not bars:
            return
        rows = [
            (code, str(b.get("d"))[:10], b.get("o"), b.get("h"),
             b.get("l"), b.get("c"), b.get("v"), adj)
            for b in bars
        ]
        with self._lock:
            con = self._con()
            con.execute("DELETE FROM kline WHERE code=?", (code,))
            con.executemany(
                "INSERT OR REPLACE INTO kline(code,date,o,h,l,c,v,adj) VALUES(?,?,?,?,?,?,?,?)",
                rows,
            )
            con.commit()
            con.close()

    # ---- meta（updated 标记等） -----------------------------------------
    def updated(self):
        con = self._con()
        r = con.execute("SELECT value FROM meta WHERE key='updated'").fetchone()
        con.close()
        return r[0] if r else None

    def set_updated(self, v) -> None:
        con = self._con()
        con.execute(
            "INSERT OR REPLACE INTO meta(key,value) VALUES('updated',?)",
            (str(v) if v else None,),
        )
        con.commit()
        con.close()

    # ---- 批量抓取全市场（日期口径完备性） --------------------------------
    def bulk_fetch_all(self, codes: list, datalen: int = 10000, workers: int = 6,
                       delay: float = 0.03, adj: str = KBASE_ADJ,
                       progress=None, ingest_existing: bool = False,
                       baseline: str = None) -> dict:
        """抓取 codes 日线并落库。返回 {fetched, ok, fail, skipped, suspended, baseline}。

        - 仅抓取"库内末根日期 != baseline"的票（**日期口径**，非根数阈值），已完整的票
          不重复请求；库内无记录的新增代码也会进入 todo。
        - baseline 默认取 latest_date()（数据集最新交易日）。末根应等于该日才算完整。
        - 某票重抓后末根仍 < baseline：判定为停牌/退市/源站停更，登记进 suspended 表，
          后续运行跳过，不再无限重试（已验证：20 只 stale 重抓末根均不变）。
        - ingest_existing=True：先合并旧 JSON 缓存再抓取。
        """
        sys.path.insert(0, ROOT)
        import strategy_sim as SS  # noqa: E402  抓取逻辑唯一真源

        if baseline is None:
            # 2026-10-29 修（**灾难级**）：原先用 `latest_date()` = MAX(date) 当基准。
            # MAX(date) 会被「单只票的脏数据」带偏——实测 2026-10-28 全市场 5219 只里
            # 只有 600707 一只有 K 线（单票详情刷新顺手写进去的），于是 baseline 变成 10-28，
            # 而其余 5216 只当天重抓回来末根都是 10-27 → 全部命中下面的「末根 != 基准」分支
            # → 4474 只正常交易中的票被批量登记进 suspended，此后再不抓取，
            # 全市场 85% 的 K 线永久冻结在 10-27（本次复查才发现，此前一直以为是源站问题）。
            #
            # 也不能直接用「覆盖率达标的最后一天」（那样永远不主动向前推进）。
            # 正解：取最新交易日，但**要求该日已有起码覆盖**（>= 全池 5%），
            # 单票脏数据（0.02%）会被跳过，而「日更跑了一半」（14%）则算数、会继续补齐。
            baseline = self._fetch_baseline()
        suspended = self._suspended_set()

        stats = {"fetched": 0, "ok": 0, "fail": 0, "skipped": 0, "suspended": 0}
        fail: list = []

        # 1) 合并旧缓存（已存在则跳过网络请求）
        if ingest_existing:
            stats["skipped"] += self._ingest_legacy()

        # 2) 需要网络抓取的票：末根 != baseline 且 不在 suspended 名单
        have = {}
        try:
            con = self._con()
            have = {r[0]: r[1] for r in con.execute(
                "SELECT code, MAX(date) FROM kline GROUP BY code").fetchall()}
            con.close()
        except Exception:  # noqa: BLE001
            pass
        todo = [c for c in codes if c not in suspended and have.get(c, "") != baseline]
        total = len(todo)

        def one(code):
            rows = []
            # 限流/偶发丢包时最多重试 3 次（每次间隔递增），确保拿到全量历史
            for attempt in range(3):
                try:
                    rows = SS.fetch_kline(code, datalen, adj)
                    if rows:
                        break
                except Exception:  # noqa: BLE001
                    rows = []
                if attempt < 2:
                    time.sleep(0.5 * (attempt + 1))
            return code, rows

        from concurrent.futures import ThreadPoolExecutor, as_completed
        done = 0
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futs = {ex.submit(one, c): c for c in todo}
            batch = []
            for fut in as_completed(futs):
                code, rows = fut.result()
                done += 1
                if rows:
                    last = str(rows[-1].get("d"))[:10]
                    if last != baseline:
                        # 2026-10-29 修：原先「末根 != 基准」就一律登记停牌，这是上一个
                        # 灾难的放大器（真停牌与「源站还没出这一天」被混为一谈）。
                        # 现在按**数据有没有前进**区分：
                        #   · last > before（重抓后末根前进了）→ 源站有更新，只是还没到基准日，
                        #     属于正常滞后，绝不登记，下次继续补；
                        #   · last <= before（重抓前后一模一样）→ 源站确实没给新数据，
                        #     才判定停牌/退市/源站停更，并**必须写明 note**（便于日后复核与清理）。
                        before = have.get(code, "")
                        if last < baseline and before and last <= before:
                            self._mark_suspended(
                                code, last, "源站停更(重抓末根未前进)")
                            stats["suspended"] += 1
                        elif last > baseline:
                            # 该票已有下一天的数据（个别票先出）→ 基准滞后，正常，绝不判停牌
                            stats["ahead"] = stats.get("ahead", 0) + 1
                        else:
                            stats["lagging"] = stats.get("lagging", 0) + 1
                    batch.append((code, rows))
                    stats["ok"] += 1
                else:
                    stats["fail"] += 1
                    fail.append(code)
                # 每 150 只批量落库一次，降低提交频率
                # 2026-10-29：降到 40。每次 _write_batch 是「逐只 DELETE 全历史 + 重插几千行」，
                # 150 只一批的事务要跑十几秒，期间 delete 模式下整库不可读（服务侧回测直接撞锁）。
                # 40 只一批把单次持锁压到 2~3 秒，配合 WAL 后读请求基本感觉不到。
                if len(batch) >= 40:
                    self._write_batch(batch, adj)
                    batch.clear()
                if progress and done % 100 == 0:
                    progress(done, total, stats)
            if batch:
                self._write_batch(batch, adj)
                batch.clear()

        self.set_updated(_today_cst())
        if progress:
            progress(total, total, stats)
        stats["baseline"] = baseline
        stats["fail_codes"] = fail[:50]
        return stats

    # ---- 停牌/退市豁免名单（suspended） --------------------------------
    def _ensure_suspended(self) -> None:
        con = self._con()
        con.execute(
            "CREATE TABLE IF NOT EXISTS suspended(code TEXT PRIMARY KEY, since TEXT, note TEXT)"
        )
        con.commit()
        con.close()

    def _suspended_set(self, max_age_days: int = 21) -> set:
        """停牌豁免名单（带保质期）。

        2026-10-29 修：原先「一旦登记就永久跳过」。叠加当天那个批量误判事故，
        4474 只正常票被永久冻结、再也不会重抓。停牌会复牌、源站停更也会恢复，
        所以豁免必须是**有期限的**：超过 max_age_days 自动失效，重新参加抓取判定；
        重抓后若确实仍无新数据，会带 note 重新登记（见 bulk_fetch_all 的判定）。
        """
        self._ensure_suspended()
        con = self._con()
        cutoff = (datetime.now() - timedelta(days=max_age_days)).strftime("%Y-%m-%d")
        s = set(r[0] for r in con.execute(
            "SELECT code FROM suspended WHERE since >= ?", (cutoff,)).fetchall())
        con.close()
        return s

    def _mark_suspended(self, code: str, since: str, note: str = "") -> None:
        self._ensure_suspended()
        con = self._con()
        con.execute(
            "INSERT OR REPLACE INTO suspended(code, since, note) VALUES(?,?,?)",
            (code, since, note),
        )
        con.commit()
        con.close()

    def clear_suspended(self, code: str = None) -> int:
        """清空停牌豁免名单。code=None 时清空全部，返回删除行数。"""
        self._ensure_suspended()
        con = self._con()
        if code:
            cur = con.execute("DELETE FROM suspended WHERE code=?", (code,))
        else:
            cur = con.execute("DELETE FROM suspended")
        n = cur.rowcount
        con.commit()
        con.close()
        return n

    # ---- 目标集合 & 增量更新 ------------------------------------------
    def target_codes(self) -> list:
        """从 app.db 取 6 位 A 股代码（与 build_kbase.load_codes 同口径）。"""
        db = os.path.join(ROOT, "viewer", "app.db")
        try:
            con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
            cs = [str(r[0]) for r in con.execute("SELECT code FROM stock_names")]
            con.close()
            return [c for c in cs if len(c) == 6 and c.isdigit() and c[0] in "03648"]
        except Exception:  # noqa: BLE001
            return []

    def _fetch_baseline(self, min_ratio: float = 0.05, floor: int = 20) -> str:
        """抓取基准日：数据里的最新交易日，但要求该日已有起码覆盖。

        为什么要这道门槛：基础版本用 MAX(date) 会被单票脏数据带偏（见 bulk_fetch_all 注释）；
        而纯「覆盖率过滤后的最后一天」又太保守 —— 日更跑到一半（14%）时它就退回到前一天，
        永远不往前推进。这里取「最新且有 >=5% 覆盖」的那一天：
          · 1 只脏数据（0.02%）→ 跳过，退到真正的市场末日；
          · 日更跑了一半（14%）  → 采信，继续把剩下的票补齐。
        """
        con = self._con()
        try:
            rows = con.execute(
                "SELECT date, COUNT(*) n FROM kline GROUP BY date ORDER BY date DESC LIMIT 5"
            ).fetchall()
            total = con.execute("SELECT COUNT(DISTINCT code) FROM kline").fetchone()[0] or 0
        except sqlite3.Error:
            return self.latest_date()
        finally:
            con.close()
        if not rows:
            return ""
        need = max(floor, int(total * min_ratio))
        for date, n in rows:
            if n >= need:
                return date
        return rows[0][0]

    def incremental_update(self, datalen: int = 10000, workers: int = 2,
                          progress=None, baseline: str = None) -> dict:
        """每日增量更新：仅补"末根 != baseline"的票 + 新增代码；停牌股跳过。"""
        codes = self.target_codes()
        return self.bulk_fetch_all(
            codes, datalen=datalen, workers=workers, progress=progress, baseline=baseline
        )

    def purge_test_codes(self, codes=("ZZTEST",)) -> int:
        """删除测试/垃圾代码（如 ZZTEST），返回删除行数。"""
        con = self._con()
        n = 0
        for c in codes:
            cur = con.execute("DELETE FROM kline WHERE code=?", (c,))
            n += cur.rowcount
        con.commit()
        con.close()
        return n

    def _write_batch(self, batch, adj):
        with self._lock:
            con = self._con()
            for code, rows in batch:
                con.execute("DELETE FROM kline WHERE code=?", (code,))
                con.executemany(
                    "INSERT OR REPLACE INTO kline(code,date,o,h,l,c,v,adj) VALUES(?,?,?,?,?,?,?,?)",
                    [(code, str(b.get("d"))[:10], b.get("o"), b.get("h"),
                      b.get("l"), b.get("c"), b.get("v"), adj) for b in rows],
                )
            con.commit()
            con.close()

    def _ingest_legacy(self) -> int:
        """把旧 JSON 缓存（若有）并入 kbase，返回并入的票数。"""
        n = 0
        # predictor 的缓存
        pk = os.path.join(ROOT, "viewer", "predict_kcache.json")
        n += self._ingest_json(pk, is_predictor=True)
        # strategy_sim 的缓存
        kc = os.path.join(ROOT, "kline_cache.json")
        n += self._ingest_json(kc, is_predictor=False)
        return n

    def _ingest_json(self, path: str, is_predictor: bool) -> int:
        if not os.path.exists(path):
            return 0
        try:
            import json
            d = json.load(open(path, encoding="utf-8"))
        except Exception:  # noqa: BLE001
            return 0
        if not isinstance(d, dict):
            return 0
        bars_map = d.get("bars") if is_predictor else d
        if not isinstance(bars_map, dict):
            return 0
        cnt = 0
        for code, v in bars_map.items():
            if self.has(code):
                continue
            rows = self._norm(v, is_predictor)
            if rows:
                self.set_bars(code, rows)
                cnt += 1
        return cnt

    @staticmethod
    def _norm(v, is_predictor: bool) -> list:
        if is_predictor:
            if not isinstance(v, list):
                return []
            return [b for b in v if isinstance(b, dict) and b.get("d")]
        # strategy_sim 结构：{"rows":[...], "adj":...}
        rows = v.get("rows") if isinstance(v, dict) else None
        if not isinstance(rows, list):
            return []
        return [b for b in rows if isinstance(b, dict) and b.get("d")]


class KBaseBars(Mapping):
    """load_kcache()["bars"] 的惰性代理：读从 kbase 取，写回 kbase 落库。

    完全兼容旧 JSON dict 的用法：.get(code) / [code] / items() / values() /
    len() / 赋值 bars[code]=v / setdefault。
    """

    def __init__(self, base: KBase):
        self._base = base

    def __getitem__(self, code):
        bars = self._base.bars_of(code)
        if not bars:
            raise KeyError(code)
        return bars

    def __iter__(self):
        return iter(self._base.codes())

    def __len__(self):
        return self._base.count()

    def get(self, code, default=None):
        bars = self._base.bars_of(code)
        return bars if bars else default

    def setdefault(self, code, default=None):
        if self._base.has(code):
            return self._base.bars_of(code)
        self._base.set_bars(code, default or [])
        return default

    def items(self):
        for code in self._base.codes():
            yield code, self._base.bars_of(code)

    def values(self):
        for code in self._base.codes():
            yield self._base.bars_of(code)

    def __setitem__(self, code, bars):
        self._base.set_bars(code, bars)

    def __repr__(self):
        return f"<KBaseBars codes={self._base.count()}>"


def _today_cst() -> str:
    try:
        from datetime import datetime, timedelta, timezone
        tz = timezone(timedelta(hours=8))
        return datetime.now(tz).strftime("%Y-%m-%d")
    except Exception:  # noqa: BLE001
        return __import__("datetime").datetime.now().strftime("%Y-%m-%d")


# 进程级单例（避免重复建连）
_INSTANCE = None


def get_base() -> KBase:
    global _INSTANCE
    if _INSTANCE is None:
        _INSTANCE = KBase()
    return _INSTANCE
