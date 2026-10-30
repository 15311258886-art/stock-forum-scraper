# -*- coding: utf-8 -*-
"""ann_feed —— 全市场公告抓取（2026-10-30 建，进哥全权委托）

**为什么必须建它**
  `stock_ann` 在此之前只有一个写入方：`watchdesk.scan()`，而它只遍历**盯盘池**
  （淘股吧重仓池 + 自选，约 200 只）。后果是全库日均只有 **40.5 条**，
  而 A 股每天实际公告上千条 —— **覆盖率约 3%**。

  这个缺陷污染了所有公告类指标。`avoid.py` 里的「股东减持」就是活证据：
  在 113 条样本上、用宽松口径能算出 t=-5.66，换成可执行口径立刻掉到 t=-0.13。
  样本本身不是随机抽样（只覆盖 200 只票），t 值再漂亮也没有外部效度。

**抓什么**
  东财全市场公告流：`np-anotice-stock/api/security/ann`，**不传 stock_list 即全市场**。
  100 条/页，按 sort_date 倒序，翻到「超出回看窗口」为止。
  单条关键字段：`title` / `notice_date` / `art_code` / `codes[0].stock_code|short_name`。

**存什么（这是刻意的取舍）**
  **只存命中规则表的公告**（即 `_classify` 判定 cat != '其他事务' 的）。
  '其他事务' 占 80% 以上（季报全文、董事会决议、章程修订、股东会法律意见书…），
  对任何指标都没有用。全存等于把「3% 的覆盖率」换成「100% 的噪声」，
  既拖慢查询又误导后来人「我们公告很全」。
  命中规则的约 20%，每天 300~600 条 —— 这才是能用的样本。

**与 watchdesk 的关系**
  分类器直接复用 `watchdesk._classify`（同一套正则），保证同一标题在两条链路里
  cat/tone 完全一致 —— 否则同一张表里同一标题会有两种标签。
  ann_feed 只负责「抓更多」，不改分类口径、不碰 stock_desk 研判、不重算任何结论。

对外：
  refresh(con, days=3, max_pages=120) → 最近 N 日日更（语义不变）
  refresh_range(con, begin_date, end_date, max_pages=1000, historical=True)
      → 指定闭区间的完整分页抓取，供月度历史回补复用
  CLI: python viewer/ann_feed.py [days]
"""
from __future__ import annotations

import os
import re
import sqlite3
import sys
import time
import urllib.parse
from datetime import date, datetime, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(ROOT, "viewer", "app.db")
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import emapi                       # noqa: E402
from watchdesk import _classify    # noqa: E402

# ann_type=A 的流里仍会混入可转债/基金（实测「118066 统联转债」），必须按代码形态过滤，
# 否则转债公告会进 stock_ann，还被当成正股的风险信号。
_A_SHARE = re.compile(r"^(60|68|00|30|43|83|87|88|92)\d{4}$")
# 注：北交所仅 43/83/87/88/92 开头；81xxxx 是「特定对象转让」等非正股凭证，不算。

PAGE_SIZE = 100
DEFAULT_DAYS = 3                   # 回看窗口：覆盖周末与长假，重复抓由去重兜住
MAX_PAGES = 120                    # 单轮上限 12000 条：三季报截止日单日就超 4000 条，40 页不够
SLEEP = 0.12                       # 页间间隔，避开东财限流
NOTICE_URL = "https://data.eastmoney.com/notices/detail/%s/%s.html"


def _fetch_page(page: int, begin_time: str = "", end_time: str = "",
                timeout: int = 15):
    """取一页全市场公告。返回 (rows, total_hits)，失败返回 (None, 0)。

    begin_time/end_time 为闭区间；历史回补必须显式传日期，日更保持不传。
    """
    params = {"sr": -1, "page_size": PAGE_SIZE, "page_index": page,
              "ann_type": "A", "client_source": "web", "f_node": 0, "s_node": 0,
              "_": int(time.time() * 1000)}
    if begin_time:
        params["begin_time"] = str(begin_time)[:10]
    if end_time:
        params["end_time"] = str(end_time)[:10]
    url = ("https://np-anotice-stock.eastmoney.com/api/security/ann?"
           + urllib.parse.urlencode(params))
    j = emapi.get_json(url, referer="https://data.eastmoney.com/notices/",
                       timeout=timeout, retries=1)
    if not j:
        return None, 0
    d = j.get("data") or {}
    return (d.get("list") or []), int(d.get("total_hits") or 0)


def _ann_key(code: str, url: str, notice_date: str, title: str) -> tuple:
    """幂等键：优先 (code,url)；无 url 的历史/异常行用日期+标题兜底。"""
    if url:
        return ("url", code, url)
    return ("text", code, notice_date, title)


def refresh_range(con, begin_date: str, end_date: str, max_pages: int = 1000,
                  historical: bool = True, log=None):
    """抓取指定闭区间的全市场公告，按页提交并可验证 total_hits 完整性。

    返回契约：
      {ok, complete, begin, end, total_hits, pages, seen, kept, added,
       duplicates, oldest, newest, err, secs, cat_added}
    `seen` 是实际读取行数，不是 pages*PAGE_SIZE。只有 seen>=total_hits 才 complete。
    """
    t0 = time.time()
    try:
        begin = datetime.fromisoformat(str(begin_date)[:10]).date().isoformat()
        end = datetime.fromisoformat(str(end_date)[:10]).date().isoformat()
    except (TypeError, ValueError):
        return {"ok": False, "complete": False, "begin": str(begin_date),
                "end": str(end_date), "total_hits": 0, "pages": 0, "seen": 0,
                "kept": 0, "added": 0, "duplicates": 0, "oldest": "", "newest": "",
                "err": "日期必须是 YYYY-MM-DD", "secs": 0.0, "cat_added": {}}
    if begin > end:
        return {"ok": False, "complete": False, "begin": begin, "end": end,
                "total_hits": 0, "pages": 0, "seen": 0, "kept": 0, "added": 0,
                "duplicates": 0, "oldest": "", "newest": "",
                "err": "begin_date 晚于 end_date", "secs": 0.0, "cat_added": {}}

    existing_keys = set()
    for r in con.execute(
            "SELECT code,url,notice_date,title FROM stock_ann "
            "WHERE notice_date>=? AND notice_date<=?", (begin, end)):
        existing_keys.add(_ann_key(r[0] or "", r[1] or "", r[2] or "", r[3] or ""))

    now = time.strftime("%Y-%m-%d %H:%M:%S")
    pages = seen = kept = added = duplicates = 0
    total_hits = 0
    oldest = newest = ""
    err = None
    complete = False
    cat_added = {}
    max_pages = max(1, int(max_pages or 1))
    is_new = 0 if historical else 1

    for page in range(1, max_pages + 1):
        rows, page_total = _fetch_page(page, begin, end)
        if rows is None:
            err = "第 %d 页抓取失败" % page
            break
        if page_total > total_hits:
            total_hits = page_total
        if not rows:
            # 空页只能是「确实无数据」或异常返回；有 total_hits 却还没拿齐即不完整。
            if page == 1 and total_hits == 0:
                pages += 1
                complete = True
            elif total_hits and seen < total_hits:
                err = "空页截断：已读 %d / total_hits %d" % (seen, total_hits)
            break
        pages += 1
        seen += len(rows)
        page_batch = []
        stop = False
        for it in rows:
            nd = str(it.get("notice_date") or "")[:10]
            if not nd:
                continue
            if nd < oldest or not oldest:
                oldest = nd
            if nd > newest or not newest:
                newest = nd
            if nd < begin:
                stop = True
                continue
            if nd > end:
                continue
            codes = it.get("codes") or []
            # codes[0] 不一定是正股：转债公告会把「118066 统联转债」排在前面，
            # 必须取第一个形态是 A 股的代码，否则整条公告挂到转债代码下、正股永远查不到。
            code = name = ""
            for c0 in codes:
                _c = str((c0 or {}).get("stock_code") or "")
                if _A_SHARE.match(_c):
                    code = _c
                    name = str((c0 or {}).get("short_name") or "")
                    break
            title = (it.get("title") or "").strip()
            art = it.get("art_code") or ""
            if not code or not title:
                continue
            cat, tone = _classify(title)
            if cat == "其他事务":          # 见模块 docstring：不存噪声
                continue
            kept += 1
            url = (NOTICE_URL % (code, art)) if art else ""
            key = _ann_key(code, url, nd, title)
            if key in existing_keys:
                duplicates += 1
                continue
            existing_keys.add(key)
            page_batch.append((code, name, title, url, nd, cat, tone, is_new, now,
                               (nd + " 15:00:00") if nd else ""))
            cat_added[cat] = cat_added.get(cat, 0) + 1
        if page_batch:
            con.executemany(
                "INSERT INTO stock_ann(code,name,title,url,notice_date,cat,tone,is_new,"
                "first_seen_at,disclose_at) VALUES(?,?,?,?,?,?,?,?,?,?)", page_batch)
            added += len(page_batch)
        con.commit()                       # 每页提交：中断后重跑由幂等键兜住
        if log and (page == 1 or page % 10 == 0):
            try:
                log("  page=%d seen=%d/%s added=%d"
                    % (page, seen, total_hits or "?", added))
            except Exception:  # noqa: BLE001
                pass
        if stop:
            complete = bool(total_hits and seen >= total_hits)
            if not complete:
                err = "API 返回早于 begin 的行，已读 %d / total_hits %s" % (seen, total_hits)
            break
        if total_hits and seen >= total_hits:
            complete = True
            break
        if page >= max_pages:
            err = "页数上限截断：已读 %d / total_hits %s" % (seen, total_hits)
            break
        time.sleep(SLEEP)

    if not err and total_hits and seen < total_hits:
        err = "已读 %d / total_hits %d，分页未拿全" % (seen, total_hits)
    ok = bool(complete and err is None)
    res = {"ok": ok, "complete": complete, "begin": begin, "end": end,
           "total_hits": total_hits, "pages": pages, "seen": seen, "kept": kept,
           "added": added, "duplicates": duplicates, "oldest": oldest,
           "newest": newest, "err": err, "secs": round(time.time() - t0, 1),
           "cat_added": cat_added}
    if log:
        try:
            log("ann_feed %s~%s：%d 页 / seen=%d / total_hits=%d / 命中 %d / 新增 %d / "
                "重复 %d / complete=%s / %.1fs%s"
                % (begin, end, pages, seen, total_hits, kept, added, duplicates,
                   complete, res["secs"], (" / " + err) if err else ""))
        except Exception:  # noqa: BLE001
            pass
    return res


def refresh(con, days: int = DEFAULT_DAYS, max_pages: int = MAX_PAGES, log=None):
    """日更：抓最近 days 天全市场公告；行为与旧版一致，分类/去重/存储不变。"""
    cutoff = (date.today() - timedelta(days=int(days or DEFAULT_DAYS))).isoformat()
    end = date.today().isoformat()
    return refresh_range(con, cutoff, end, max_pages=max_pages,
                         historical=False, log=log)


def _main():
    days = int(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_DAYS
    con = sqlite3.connect(DB_PATH)
    before = con.execute("SELECT COUNT(*) FROM stock_ann").fetchone()[0]
    r = refresh(con, days=days, log=print)
    after = con.execute("SELECT COUNT(*) FROM stock_ann").fetchone()[0]
    print("库内 %d → %d 条" % (before, after))
    print("分页 %d（名义 %d 条）· 命中规则 %d · 新增 %d · %s~%s · %.1fs"
          % (r["pages"], r["seen"], r["kept"], r["added"], r["oldest"] or "-",
             r["newest"] or "-", r["secs"]))
    print("\n近 3 日各类别入库量：")
    for row in con.execute(
            "SELECT cat, COUNT(*) FROM stock_ann WHERE notice_date >= date('now','-3 day')"
            " GROUP BY cat ORDER BY 2 DESC"):
        print("  %-14s %4d" % row)
    return 0 if r["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(_main())
