# -*- coding: utf-8 -*-
"""同花顺源「方法共振漏斗」17 板块官方成分股采集 → 落库 plate_members。

为什么有这个脚本（C3 缺口的沙箱可用替代源）：
  - 原 fetch_plate_members.py 用东方财富 push2，但在「云沙箱」出口 IP 被东财整体限制
    （push2 RST + datacenter "报表配置不存在"），只能在用户本机跑。
  - 腾讯 appstuff 主题接口已下线（code 11 No dispatch info found），新浪 JSON API 已废
    （Service not valid）。
  - 同花顺 q.10jqka.com.cn 在沙箱可通，且概念/行业板覆盖与漏斗 17 板块高度吻合，故作为
    沙箱/本机均可跑的替代采集源。

数据口径说明（重要）：
  - board 字段 = 漏斗板块名（与前端 mode=official 入参、pillars.py 热词子串匹配一致），
    取值固定为：AI / 半导体 / 机器人 / 黄金 / 券商 / 银行 / 光伏 / 消费 / 地产 /
    算力 / 锂电 / 白酒 / 军工 / 软件 / 汽车 / 芯片 / 游戏。
  - board_code 字段 = 来源前缀 + 同花顺板块码（如 "ths:gn:302035" / "ths:thshy:881121"），
    仅作溯源，不参与匹配。
  - 数据来源等级：同花顺为「交易所级行业/概念分类」的转载，可信度次于东财官方直连，
    但远高于纯舆情池。前端 plate_members 仍标记为 official 口径（权威分类源）。

⚠ 反爬须知：同花顺对匿名分页抓取限流极严（约 5 次请求后即间歇性返回空/401）。
  本脚本已内置 session(cookie) + 失败重试 + 礼貌延迟，但【不建议在云沙箱里整跑 17 板块】
  （会触发硬限流且拖慢整库）。推荐在【用户本机 venv】下运行，或仍用东财版权威脚本：
      .venv/Scripts/python.exe tools/fetch_plate_members.py
  若只在本机轻量补某几个板块，用 --plate 指定即可。

落库：viewer/app.db 的 plate_members(code,name,board,board_code,updated_at)（幂等：同 board 先删后插）。
"""
from __future__ import annotations
import argparse
import re
import sqlite3
import sys
import time
from datetime import datetime
from http.cookiejar import CookieJar
from pathlib import Path
from urllib.request import Request, build_opener, HTTPCookieProcessor, URLError
from urllib.error import HTTPError

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "viewer" / "app.db"

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")
BASE = "https://q.10jqka.com.cn"

# 漏斗板块名 → 同花顺板块 [(类型, 板块码), ...]
#   类型: "gn"=概念(concept), "thshy"=行业(industry)
# 选取原则：有干净行业板优先用行业（半导体/券商/银行/光伏/消费/地产/白酒/军工/软件/汽车/游戏），
#          无行业板或行业过宽则用核心概念（AI/机器人/黄金/算力/锂电/芯片）。
PLATE_BOARDS = {
    "AI":     [("gn", "302035")],                     # 人工智能
    "半导体":   [("thshy", "881121")],                 # 半导体(行业)
    "机器人":   [("gn", "300816"), ("gn", "309119")],  # 机器人概念 + 人形机器人
    "黄金":     [("gn", "300248")],                     # 黄金概念
    "券商":     [("thshy", "881157")],                 # 证券(行业)
    "银行":     [("thshy", "881155")],                 # 银行(行业)
    "光伏":     [("thshy", "881279")],                 # 光伏设备(行业)
    "消费":     [("thshy", "881124")],                 # 消费电子(行业)
    "地产":     [("thshy", "881153")],                 # 房地产(行业)
    "算力":     [("gn", "308828"), ("gn", "309068")],  # 东数西算(算力) + 算力租赁
    "锂电":     [("gn", "300733")],                     # 锂电池概念
    "白酒":     [("thshy", "881273")],                 # 白酒(行业)
    "军工":     [("thshy", "881166"), ("thshy", "881276")],  # 军工装备 + 军工电子
    "软件":     [("thshy", "881272")],                 # 软件开发(行业)
    "汽车":     [("thshy", "881125"), ("thshy", "881126")],  # 汽车整车 + 汽车零部件
    "芯片":     [("gn", "301085")],                     # 芯片概念
    "游戏":     [("thshy", "881275")],                 # 游戏(行业)
}

_PAIR_RE = re.compile(
    r'stockpage\.10jqka\.com\.cn/(\d{6})/"[^>]*>\d{6}</a>'
    r'.*?stockpage\.10jqka\.com\.cn/\1"[^>]*>([^<]+)</a>', re.S)


def _make_opener():
    cj = CookieJar()
    return build_opener(HTTPCookieProcessor(cj))


def _http_get(op, url: str, ref: str, retries: int = 3, timeout: int = 20) -> str:
    last = None
    for attempt in range(1, retries + 1):
        try:
            req = Request(url, headers={
                "User-Agent": UA, "Referer": ref,
                "Accept": "text/html,*/*;q=0.8",
            })
            raw = op.open(req, timeout=timeout).read()
            enc = "gbk"  # 同花顺全站 gbk
            return raw.decode(enc, "ignore")
        except HTTPError as e:
            last = e
            if e.code in (401, 403, 429):
                # 反爬限流：退避后重试
                time.sleep(3 * attempt)
                continue
            raise
        except URLError as e:
            last = e
            time.sleep(2 * attempt)
            continue
    raise last or RuntimeError("unknown http error")


def _extract_pairs(html: str) -> list:
    out = []
    for code, name in _PAIR_RE.findall(html):
        out.append((code, name.strip()))
    return out


def fetch_board_members(op, kind: str, code: str, delay: float,
                       max_pages: int = 300) -> list:
    """翻页拉取某同花顺板块全部成分股。返回 [(code, name), ...]。"""
    # 先取详情主页（建立 session/可能的 cookie，并拿到总页数提示）
    home = f"{BASE}/{kind}/detail/code/{code}/"
    _http_get(op, home, ref=f"{BASE}/{kind}/")
    out: list = []
    seen = set()
    p = 1
    empty_streak = 0
    while p <= max_pages:
        url = (f"{home}field/199112/order/desc/page/{p}/ajax/1/")
        try:
            html = _http_get(op, url, ref=home)
        except Exception as e:  # noqa: BLE001
            print(f"    [warn] 板块 {kind}:{code} 第{p}页失败: {e}")
            time.sleep(5)
            empty_streak += 1
            if empty_streak >= 3:
                break
            continue
        pairs = _extract_pairs(html)
        if not pairs:
            empty_streak += 1
            if empty_streak >= 3:
                break
            time.sleep(2)  # 限流瞬时空，稍等重试本页
            continue
        empty_streak = 0
        for c, n in pairs:
            if c not in seen:
                seen.add(c)
                out.append((c, n))
        if len(pairs) < 10:
            break  # 末页（不足一页）
        p += 1
        if delay:
            time.sleep(delay)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="同花顺源板块成分股采集")
    ap.add_argument("--plate", help="只抓某个板块（默认全部 17 个）")
    ap.add_argument("--db", default=str(DB_PATH), help="app.db 路径")
    ap.add_argument("--delay", type=float, default=0.6,
                    help="翻页间礼貌延迟(秒)，默认 0.6；本机可调更低")
    ap.add_argument("--max-pages", type=int, default=300,
                    help="单板块最大翻页数保护，默认 300")
    args = ap.parse_args()

    db = Path(args.db)
    if not db.exists():
        print(f"[error] 找不到数据库：{db}", file=sys.stderr)
        return 2

    plates = [args.plate] if args.plate else list(PLATE_BOARDS.keys())
    for p in plates:
        if p not in PLATE_BOARDS:
            print(f"[error] 未知板块：{p}（可选：{', '.join(PLATE_BOARDS)}）",
                  file=sys.stderr)
            return 2

    op = _make_opener()
    con = sqlite3.connect(str(db), timeout=30)
    con.execute("""CREATE TABLE IF NOT EXISTS plate_members (
        code TEXT, name TEXT, board TEXT, board_code TEXT, updated_at TEXT)""")
    con.commit()

    total_rows = 0
    for plate in plates:
        members = []
        used = []
        for kind, code in PLATE_BOARDS[plate]:
            ms = fetch_board_members(op, kind, code, args.delay, args.max_pages)
            print(f"  [ok] 「{plate}」← 同花顺 {kind}:{code} 成员 {len(ms)} 只")
            members += ms
            used.append(f"{kind}:{code}")
            time.sleep(args.delay)
        if not members:
            print(f"  [warn] 板块「{plate}」无成员，跳过落库")
            continue
        uniq = {}
        for c, n in members:
            uniq.setdefault(c, n)
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        con.execute("DELETE FROM plate_members WHERE board=?", (plate,))
        con.executemany(
            "INSERT INTO plate_members(code,name,board,board_code,updated_at)"
            " VALUES(?,?,?,?,?)",
            [(c, n, plate, "|".join(used), stamp) for c, n in uniq.items()])
        con.commit()
        print(f"  => 板块「{plate}」落库 {len(uniq)} 只（来自 {len(used)} 个同花顺板块）")
        total_rows += len(uniq)

    print(f"\n==> 完成。plate_members 新增/更新 {total_rows} 行。")
    con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
