# -*- coding: utf-8 -*-
"""extref 自动回测器：用日 K（前复权）给「窗口已走完」的信号回填 t1/t5/t10 收益。

口径（硬性，改了会让胜率榜失真）：
  · 基准 = 信号发布日**当天收盘**；发布日非交易日时，取其后第一个交易日收盘。
  · tN = (第 N 个交易日收盘 / 基准收盘 − 1) × 100（单位 %）。
  · 用**前复权**价（qfq），避免除权/送股跳空污染收益。
  · 窗口未走完的信号**保持 pending 不回填**；取不到行情的记 `na`，**绝不写 0**
    （0 会被读成「零收益」，与「未测」混淆——用户明确要求）。
  · **部分回填（2026-09-30 新增）**：T+1 一到期就先写入 `pnl_t1`，状态仍保持 `pending`，
    等 5 日窗口走完再整体转 `done`。理由——此前必须等 T+5 才落任何数字，用户点开信号
    长期只见「—」。部分值不污染胜率榜（榜只认 `done`）。

源与兜底（2026-09-30 实测）：
  · 腾讯 `web.ifzq.gtimg.cn`（前复权，主源）—— 直连可用；
  · 新浪 `money.finance.sina.com.cn`（备用源）—— 直连可用，与腾讯最近交易日价格一致；
  · 东财 `push2his`（RemoteDisconnected，被掐）、网易 126（DNS 失败）—— 均不可用，不接。
  单源单次抖动会把好信号永久判 `na`（`pending_signals` 只捞 `pending`），故改为**多源兜底 +
  `reopen_stale_na` 定期自愈**，双保险。

隔离纪律：只 import extref.store，不 import viewer 业务模块；连接由调用方传入。
"""
from __future__ import annotations

import json
import re
import time
import urllib.request
from datetime import datetime, timedelta

from extref import store as st

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))
TX_KLINE = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
SINA_KLINE = ("https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/"
              "CN_MarketData.getKLineData")


def _prefix(code: str) -> str:
    if code.startswith(("60", "68", "90")):
        return "sh"
    if code.startswith(("4", "8")):
        return "bj"
    return "sz"


# 旧名保留（外部可能引用）
_tx_prefix = _prefix


def _http_text(url: str, referer: str, timeout: int = 12) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": _UA, "Referer": referer})
    with _OPENER.open(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def _fetch_tencent(code: str, bars: int) -> list[tuple[str, float]]:
    """腾讯日 K（前复权）。失败/无数据返回 []。"""
    sym = _prefix(code) + code
    url = "%s?param=%s,day,,,%d,qfq" % (TX_KLINE, sym, bars)
    d = json.loads(_http_text(url, "https://gu.qq.com/"))
    node = (d.get("data") or {}).get(sym) or {}
    arr = node.get("qfqday") or node.get("day") or []
    out = []
    for row in arr:
        try:
            out.append((str(row[0])[:10], float(row[2])))   # [date, open, close, high, low, vol]
        except Exception:
            continue
    return out


def _fetch_sina(code: str, bars: int) -> list[tuple[str, float]]:
    """新浪日 K（备用源）。失败/无数据返回 []。

    该接口返回 JSON 数组（键名可能不带引号），元素形如
    {"day":"2026-09-29","open":"..","high":"..","low":"..","close":"..","volume":".."}。
    """
    sym = _prefix(code) + code
    url = "%s?symbol=%s&scale=240&ma=no&datalen=%d" % (SINA_KLINE, sym, bars)
    txt = _http_text(url, "https://finance.sina.com.cn/").strip()
    if not txt.startswith("["):
        return []
    txt = re.sub(r'([{,])\s*([A-Za-z_]\w*)\s*:', r'\1"\2":', txt)   # 兼容无引号键名
    arr = json.loads(txt)
    out = []
    for row in arr:
        try:
            out.append((str(row["day"])[:10], float(row["close"])))
        except Exception:
            continue
    out.sort(key=lambda x: x[0])
    return out


def fetch_daily(code: str, bars: int = 400, retry: int = 2) -> list[tuple[str, float]]:
    """多源取该股日 K 收盘序列，升序 [(date, close)]；全部源都失败才返回 []。

    2026-09-28 加固：带重试（默认 2 次，共 3 轮）。
    2026-09-30 加固：每轮内先试腾讯、再试新浪——本函数返回 [] 时上层会落 `na`，
    而 `pending_signals` 只捞 pending，**一条信号一旦被误标 na 就再也不会回填**。
    单源抗不住抖动，故加备用源；
    """
    for attempt in range(max(1, retry + 1)):
        for fn in (_fetch_tencent, _fetch_sina):
            try:
                out = fn(code, bars)
            except Exception:
                out = []
            if out:
                return out
        if attempt < retry:
            time.sleep(0.6 * (attempt + 1))
    return []


def reopen_stale_na(conn, older_than_days: int = 2, dry_run: bool = False) -> int:
    """把「na 且已搁置超过 N 天」的行重开为 pending，纳入下一轮回测。返回重开条数。

    **na 不是终态**（2026-09-30 新增）。两个真实教训：
      ① 发布当天回测时，当天 K 线尚未生成 → 旧逻辑判 `na` → 永久冻结（09-27/09-28 共 41 条被冤判）；
      ② 单次网络失败 → 判 `na` → 同样永久冻结。
    定期重开让误判自愈，也顺带覆盖「当时停牌、现在复牌」的票。传 older_than_days=0 即全部重开。
    """
    cutoff = (datetime.now() - timedelta(days=older_than_days)).strftime("%Y-%m-%d %H:%M:%S")
    n = conn.execute(
        "SELECT COUNT(*) FROM extref_signals WHERE pnl_status='na' AND symbol<>'' "
        "AND IFNULL(updated_at,'') < ?", (cutoff,)).fetchone()[0]
    if n and not dry_run:
        conn.execute(
            "UPDATE extref_signals SET pnl_status='pending', updated_at=? "
            "WHERE pnl_status='na' AND symbol<>'' AND IFNULL(updated_at,'') < ?",
            (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), cutoff))
        conn.commit()
    return n


def compute_pnl(closes: list[tuple[str, float]], pub_date: str):
    """按基准日计算 t1/t5/t10。返回 (t1, t5, t10, base_date) 或 None（无基准）。"""
    idx = next((i for i, (dt, _) in enumerate(closes) if dt >= pub_date), None)
    if idx is None:
        return None
    base = closes[idx][1]
    if not base:
        return None

    def at(n):
        j = idx + n
        return round((closes[j][1] / base - 1) * 100, 2) if j < len(closes) else None

    return at(1), at(5), at(10), closes[idx][0]


def backtest_all(conn, limit: int = 500, dry_run: bool = False) -> dict:
    """给所有 pending 信号尝试回填。返回 {pending, done, na, skipped, detail}。

    · done   —— 5 日窗口已走完，已回填 t1/t5(/t10)。
    · partial—— 5 日窗口未走完但 T+1 已到，已先回填 pnl_t1（状态仍 pending）。
    · skipped—— 窗口未走完（发布日过近或无后续交易日），保持 pending。
    · na      —— 所有日 K 源都取不到行情（退市/代码无效/长期停牌），记 na 不写 0。
    """
    # 先做自愈：把搁置超过 N 天的 na 重开为 pending，纳入本轮（na 不是终态，见函数注释）
    reopened = reopen_stale_na(conn, older_than_days=2, dry_run=dry_run)
    rows = st.pending_signals(conn, limit=limit)
    today = datetime.now().strftime("%Y-%m-%d")
    done = na = skipped = partial = 0
    detail = []
    cache: dict[str, list] = {}
    for r in rows:
        code = (r.get("symbol") or "").strip()
        pub = (r.get("pub_date") or "").strip()
        if not code or not pub or pub >= today:
            skipped += 1
            continue
        if code not in cache:
            cache[code] = fetch_daily(code)
        closes = cache[code]
        res = compute_pnl(closes, pub) if closes else None
        if not res:
            # 2026-09-28 加固：区分「行情还没走到发布日」与「真的取不到行情」。
            # 前者（新股 / 长期停牌 / 行情延迟）只是窗口还没到，必须保持 pending 继续等——
            # 记成 na 会把它**永久踢出回填队列**（pending_signals 只捞 pending）；后者才记 na。
            if closes and closes[-1][0] < pub:
                skipped += 1
                detail.append({"sid": r["sid"], "symbol": code, "status": "wait_bar",
                               "pub": pub, "last_bar": closes[-1][0]})
                continue
            if not dry_run:
                st.backfill_pnl(conn, r["sid"], None, None, None, status="na")
            na += 1
            detail.append({"sid": r["sid"], "symbol": code, "status": "na"})
            continue
        t1, t5, t10, base_date = res
        if t5 is None:                       # 5 日窗口还没走完
            # 部分回填：T+1 已到期就先落地，别让用户只看到「—」。
            # 状态仍留 pending（未走完），胜率榜只认 done，故部分值不会污染榜单。
            if t1 is not None:
                if not dry_run:
                    st.backfill_pnl(conn, r["sid"], t1, None, None, status="pending")
                partial += 1
                detail.append({"sid": r["sid"], "symbol": code, "name": r.get("symbol_name"),
                               "status": "partial", "t1": t1, "base": base_date})
            else:
                skipped += 1
                detail.append({"sid": r["sid"], "symbol": code, "status": "wait_bar",
                               "pub": pub, "last_bar": closes[-1][0]})
            continue
        if not dry_run:
            st.backfill_pnl(conn, r["sid"], t1, t5, t10, status="done")
        done += 1
        detail.append({"sid": r["sid"], "symbol": code, "name": r.get("symbol_name"),
                       "t1": t1, "t5": t5, "t10": t10, "base": base_date})
    return {"pending": len(rows), "done": done, "na": na, "partial": partial,
            "skipped": skipped, "reopened": reopened, "detail": detail}


if __name__ == "__main__":
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from viewer.db import Database  # noqa: E402

    args = set(sys.argv[1:])
    c = Database().conn
    st.ensure_schema(c)
    if "--reopen-na" in args:          # 一次性修复：把全部 na 重开再回测
        n = reopen_stale_na(c, older_than_days=0)
        print("重开 na → pending:", n, "条")
    if "--dry" in args:
        print(json.dumps(backtest_all(c, dry_run=True), ensure_ascii=False, indent=1))
    else:
        print(json.dumps(backtest_all(c), ensure_ascii=False, indent=1))
