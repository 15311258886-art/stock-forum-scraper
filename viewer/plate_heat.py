"""板块热度榜 —— 把「荐股」降级成「注意力」。

2026-10-29 进哥拍板：
  博主/论坛的荐股（推什么票、看多看空、后续涨跌）从此**不再作为信号、也不再展示**；
  只保留「注意力」这一个维度 —— 哪个板块被聊得多，就是哪个板块热。
  本模块只回答「热不热」，**不回答「涨不涨」**，也绝不输出任何方向判断。
  这是刻意的产品约束，不是遗漏：一旦混进方向，就又会退化成荐股。

三个源，各有硬缺陷，故必须分开标注、不许揉成一个数字糊弄人：

  ① 论坛提及  stock_mentions ⋈ posts ⋈ plate_members
     缺陷：stock_mentions.post_id_ref 有 88% 指向**已被清理的帖子**（孤儿提及），
     能 join 上且有时间的只有 ~9.6 千帖（2026-08 起）。故窗口一律用
     「最近 5 个有数据交易日」而**不是单日** —— 实测单日 173~1087 帖，波动 6 倍，
     单日榜会被噪音完全主导。

  ② 博主视频提及  douyin_corpus_plate ⋈ douyin_corpus_video
     缺陷：最晚只到 2026-10-11（豆包日更断档），故只作辅助分，并在榜面标出截止日；
     滞后超 7 天、或有量板块不足 3 个时，该源**整源停用**、权重全额还给论坛 ——
     min-max 下「只有一个板块有数据」会让它白拿满分，那是算法水出来的假热度。
     刻意**只取 plate 标签、不取 stance**：stance 是「板块机会/风险」＝方向，按进哥要求不用。

  ③ 板块内广度 = 本窗口被提及的不同股票数 / 该板块成员数
     缺陷：plate_members 只覆盖 3131 只、17 个粗板块；未覆盖的股票（提及股票中约 40%）
     不计入任何板块，故广度天然偏低，只作 0.20 的辅权重。

热度分 = 三源各自 log1p 后 min-max 到 0~1，加权 ×100。
用 min-max 而不是 z-score：z 会出负值，榜面上根本读不出「热度」二字。
分数是**自定义口径**，不是交易所或任何机构的官方指标 —— 榜面必须写明。
"""

from __future__ import annotations

import math
import os
import sqlite3
import time

# 与 blogger_score / regime 同款：模块自己算库路径，调度器（tools/sched_job.py）直接 run()
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.environ.get("VIEWER_DB") or os.path.join(ROOT, "viewer", "app.db")

# ---------------------------------------------------------------------------
# 板块名归一：别名 → 标准板块
# 只做**语义明确**的合并（芯片≡半导体、新能源汽车≡汽车）；
# 拿不准的一律保留原样 —— 宁可多出一个板块，也不要把两个不相干行业搅一起，
# 那是凭空制造假热度，比不合并更糟。
# 尤其：豆包语料里混着「贵州茅台/比亚迪/东方财富」这类**个股名当板块名**，
# 必须显式映射掉，否则榜单上会冒出「贵州茅台板块热度第一」这种荒谬结论。
# ---------------------------------------------------------------------------
ALIAS = {
    # 半导体链
    "芯片": "半导体", "半导体": "半导体", "光刻": "半导体", "面板": "半导体",
    # AI / 算力链
    "AI": "AI", "人工智能": "AI",
    "算力": "算力", "大数据": "算力", "CPO": "算力", "光模块": "算力",
    "PCB": "算力", "液冷": "算力",
    # 汽车链
    "汽车": "汽车", "新能源汽车": "汽车", "零部件": "汽车", "特斯拉": "汽车",
    "减速器": "汽车", "重卡": "汽车", "比亚迪": "汽车",
    # 锂电 / 储能
    "锂电": "锂电", "储能": "锂电", "宁德时代": "锂电",
    # 消费
    "消费": "消费", "食品饮料": "消费", "零售": "消费", "家电": "消费",
    "旅游": "消费", "酒店": "消费", "教育": "消费",
    "白酒": "白酒", "贵州茅台": "白酒",
    # 金融
    "券商": "券商", "东方财富": "券商",
    "银行": "银行", "招商银行": "银行",
    "保险": "保险",
    # 其他明确项
    "机器人": "机器人",
    "军工": "军工", "航天": "军工", "卫星": "军工",
    "软件": "软件", "信创": "软件",
    "传媒": "传媒", "影视": "传媒",
    "游戏": "游戏",
    "医药": "医药", "创新药": "医药",
    "地产": "地产", "物业": "地产", "建材": "地产", "水泥": "地产",
    "黄金": "黄金",
    "有色": "有色", "铜": "有色", "铝": "有色", "稀土": "有色",
    "煤炭": "煤炭",
    "石油": "石油", "天然气": "石油", "化工": "化工", "化肥": "化工",
    "电力": "电力", "电网": "电力", "水电": "电力", "风电": "电力",
    "核电": "电力", "氢能": "电力",
    "光伏": "光伏",
    "通信": "通信", "5G": "通信", "6G": "通信", "华为": "通信",
    "钢铁": "钢铁",
    "农业": "农业", "猪肉": "农业",
}

# 权重：论坛为主（唯一有当日时效的源），视频为辅，广度作修正项
W_FORUM = 0.55
W_VIDEO = 0.25
W_BREADTH = 0.20

# 视频源滞后超过这么多天 → 降权到 0.10，差额并入论坛
VIDEO_STALE_DAYS = 7
VIDEO_STALE_W = 0.10

WINDOW = 5          # 滚动窗口：最近 N 个有数据交易日
TOP_STOCKS = 3      # 每个板块展示的被提及最多股票数（只给次数，不给方向）
MIN_CMP = 3.0       # 环比门槛：板块**讨论占比**低于 3% 视为样本太薄，不出环比

DDL = """
CREATE TABLE IF NOT EXISTS plate_heat (
  asof_date   TEXT NOT NULL,   -- 榜单口径日（窗口末）
  plate       TEXT NOT NULL,   -- 标准板块名
  n_posts     INTEGER,         -- 窗口内论坛提及帖数
  n_stocks    INTEGER,         -- 窗口内板块被提及的不同股票数
  members     INTEGER,         -- 该板块成员总数（plate_members 归一后）
  breadth     REAL,            -- n_stocks / members，0~1
  n_videos    INTEGER,         -- 视频源提及数（另一窗口，见 video_asof）
  share       REAL,            -- 本窗口板块帖数占全站帖数（%，中位日）——热度的真实输入
  heat        REAL,            -- 综合热度 0~100（自定义口径）
  rank_no     INTEGER,
  prev_posts  INTEGER,         -- 上一同长窗口帖数
  chg_pp      REAL,            -- 占比变化（百分点），NULL=两边占比不足不出数
  src         TEXT,            -- forum / forum+video / video
  PRIMARY KEY (asof_date, plate)
);
CREATE TABLE IF NOT EXISTS plate_heat_top (
  asof_date TEXT NOT NULL,
  plate     TEXT NOT NULL,
  code      TEXT NOT NULL,
  name      TEXT,
  n_mentions INTEGER,
  PRIMARY KEY (asof_date, plate, code)
);
CREATE TABLE IF NOT EXISTS plate_heat_meta (
  k TEXT PRIMARY KEY,
  v TEXT
);
"""


def _norm(plate: str) -> str | None:
    """板块名归一。返回 None 表示该名不该进榜（空值等垃圾）。"""
    p = (plate or "").strip()
    if not p:
        return None
    return ALIAS.get(p, p)


def _minmax(vals: list[float]) -> list[float]:
    """min-max 到 0~1。全同值时全给 0.5 —— 不是 0，因为「都热得一样」不等于「都不热」。"""
    if not vals:
        return []
    lo, hi = min(vals), max(vals)
    if hi <= lo:
        return [0.5] * len(vals)
    return [(v - lo) / (hi - lo) for v in vals]


def _recent_days(con: sqlite3.Connection, n: int) -> list[str]:
    """最近 n 个**有提及数据**的日期（降序）。用有数据的日子而非自然日，
    避免周末/节假日空档把窗口撑成 10 天。"""
    rows = con.execute(
        "SELECT DISTINCT substr(p.post_time,1,10) d"
        "  FROM stock_mentions m JOIN posts p ON p.id = m.post_id_ref"
        " WHERE p.post_time IS NOT NULL AND p.post_time <> ''"
        " ORDER BY d DESC LIMIT ?", (n,)).fetchall()
    return [r[0] for r in rows if r[0]]


def _members(con: sqlite3.Connection) -> tuple[dict, dict]:
    """标准板块 → 成员数 / 成员代码集合。同一股票在多源板块里重复的按 code 去重。"""
    cnt: dict[str, set] = {}
    for r in con.execute("SELECT code, board FROM plate_members"):
        b = _norm(r["board"])
        if not b:
            continue
        cnt.setdefault(b, set()).add(str(r["code"]).zfill(6))
    return {k: len(v) for k, v in cnt.items()}, cnt


def _forum(con: sqlite3.Connection, days: list[str]) -> tuple[dict, dict]:
    """论坛窗口聚合 → ({plate: {posts, stocks, daily:[...]}}, {(plate,code): (name, n)})

    daily 是**每天**该板块的去重帖数，专门用来算环比中位数。
    为什么不用总量比：实测单日 173~1087 帖（抓取节奏不均），10-21 一天爆到 1087，
    总量比会算出全榜 -60%~-80% 的假降温。中位数对单日爆量不敏感，才敢拿来比。
    """
    if not days:
        return {}, {}
    marks = ",".join("?" * len(days))
    # 按日 × 板块 的去重帖数
    per_day: dict[str, dict[str, int]] = {}
    sql = (
        "SELECT substr(p.post_time,1,10) d, pm.board, COUNT(DISTINCT p.id) n"
        "  FROM stock_mentions m"
        "  JOIN posts p ON p.id = m.post_id_ref"
        "  JOIN plate_members pm ON pm.code = m.symbol"
        " WHERE p.post_time IS NOT NULL AND p.post_time <> ''"
        "   AND substr(p.post_time,1,10) IN (%s)"
        " GROUP BY d, pm.board" % marks)
    for r in con.execute(sql, days):
        b = _norm(r["board"])
        if b:
            per_day.setdefault(b, {})[r["d"]] = r["n"]

    # 板块内被提及的不同股票数 + 每只票的提及次数（代表股用）
    sql2 = (
        "SELECT pm.board, m.symbol, m.symbol_name, COUNT(DISTINCT p.id) n"
        "  FROM stock_mentions m"
        "  JOIN posts p ON p.id = m.post_id_ref"
        "  JOIN plate_members pm ON pm.code = m.symbol"
        " WHERE p.post_time IS NOT NULL AND p.post_time <> ''"
        "   AND substr(p.post_time,1,10) IN (%s)"
        " GROUP BY pm.board, m.symbol" % marks)
    detail: dict[tuple, tuple] = {}
    stocks: dict[str, int] = {}
    for r in con.execute(sql2, days):
        b = _norm(r["board"])
        if not b:
            continue
        detail[(b, str(r["symbol"]).zfill(6))] = (r["symbol_name"], r["n"])
        stocks[b] = stocks.get(b, 0) + 1

    agg: dict[str, dict] = {}
    for b, dd in per_day.items():
        agg[b] = {"posts": sum(dd.values()), "stocks": stocks.get(b, 0),
                  "daily": [dd.get(d, 0) for d in days]}
    return agg, detail


def _daily_total(con: sqlite3.Connection, days: list[str]) -> dict[str, int]:
    """每个窗口日的**全站**去重帖数（分母）。用于把板块绝对帖数换成占比，
    从而剔除「今天整体抓得少」这种与市场无关的波动。"""
    if not days:
        return {}
    marks = ",".join("?" * len(days))
    out: dict[str, int] = {}
    for r in con.execute(
            "SELECT substr(p.post_time,1,10) d, COUNT(DISTINCT p.id) n"
            "  FROM stock_mentions m JOIN posts p ON p.id = m.post_id_ref"
            " WHERE p.post_time IS NOT NULL AND p.post_time <> ''"
            "   AND substr(p.post_time,1,10) IN (%s) GROUP BY d" % marks, days):
        out[r["d"]] = r["n"] or 0
    return out


def _shares(daily: list[int], days: list[str], tot: dict[str, int]) -> list[float]:
    """每日板块帖数 → 每日占比（%）。分母是该日全站帖数，不是窗口总量。"""
    out: list[float] = []
    for d, n in zip(days, daily or []):
        t = tot.get(d) or 0
        out.append((n / t * 100.0) if t else 0.0)
    return out


def _median(vals: list[int]) -> float:
    v = sorted(vals)
    if not v:
        return 0.0
    n = len(v)
    return float(v[n // 2]) if n % 2 else (v[n // 2 - 1] + v[n // 2]) / 2.0


def _video(con: sqlite3.Connection) -> tuple[dict, str]:
    """视频源：取最新一天往前 7 天内（含）的全部提及，按标准板块计数。
    返回 ({plate: n}, 该源的数据截止日)。表不存在时返回 ({}, "")。"""
    try:
        r = con.execute("SELECT MAX(publish_date) mx FROM douyin_corpus_video"
                        " WHERE publish_date IS NOT NULL AND publish_date <> ''").fetchone()
    except sqlite3.Error:
        return {}, ""
    asof = (r["mx"] if r else "") or ""
    if not asof:
        return {}, ""
    d = asof.rsplit("-", 1)[0] + "-01"      # 同月即可（视频量小，不按日卡太死）
    out: dict[str, int] = {}
    try:
        for x in con.execute(
                "SELECT pl.plate, COUNT(*) n"
                "  FROM douyin_corpus_plate pl"
                "  JOIN douyin_corpus_video v ON v.video_id = pl.video_id"
                " WHERE v.publish_date >= ?", (d,)):
            b = _norm(x["plate"])
            if b:
                out[b] = out.get(b, 0) + x["n"]
    except sqlite3.Error:
        return {}, asof
    return out, asof


def _days_between(a: str, b: str) -> int:
    """两个 YYYY-MM-DD 相差天数。解析失败返回极大值（当作陈旧）。"""
    try:
        ta = time.strptime(a[:10], "%Y-%m-%d")
        tb = time.strptime(b[:10], "%Y-%m-%d")
        return abs(int((time.mktime(tb) - time.mktime(ta)) / 86400))
    except Exception:  # noqa: BLE001
        return 9999


def _migrate(con: sqlite3.Connection) -> None:
    """旧表结构（没有 share/chg_pp 列）直接 DROP 重建。

    这三张表是**可重算的派生产物**，不含任何原始数据，删掉下一次 build 就长回来，
    不值得为它写 ALTER 迁移 —— 而放着一个字段对不上的旧表，只会在接口里炸出
    「no such column」这种一眼看不出根因的错。
    """
    try:
        cols = {r[1] for r in con.execute("PRAGMA table_info(plate_heat)")}
    except sqlite3.Error:
        return
    if cols and "share" not in cols:
        con.executescript("DROP TABLE IF EXISTS plate_heat;"
                          " DROP TABLE IF EXISTS plate_heat_top;")
        con.commit()


def build(con: sqlite3.Connection) -> dict:
    """重算并落库。返回摘要（给接口与调度日志看）。"""
    _migrate(con)
    con.executescript(DDL)
    days = _recent_days(con, WINDOW)
    if not days:
        return {"ok": False, "error": "无可用的论坛提及日期（stock_mentions ⋈ posts 为空）"}
    asof = days[0]

    # 上一窗口：紧邻再往前 WINDOW 个有数据日
    prev_days: list[str] = []
    if len(days) >= WINDOW:
        prev_days = [r[0] for r in con.execute(
            "SELECT DISTINCT substr(p.post_time,1,10) d"
            "  FROM stock_mentions m JOIN posts p ON p.id = m.post_id_ref"
            " WHERE p.post_time IS NOT NULL AND p.post_time <> ''"
            "   AND substr(p.post_time,1,10) < ?"
            " ORDER BY d DESC LIMIT ?", (days[-1], WINDOW)).fetchall() if r[0]]

    agg, detail = _forum(con, days)
    agg_prev, _ = _forum(con, prev_days)
    vid, vid_asof = _video(con)
    mem_cnt, mem_set = _members(con)

    stale = _days_between(vid_asof, asof) if vid_asof else 9999
    # 视频源有效板块数：min-max 的致命毛病是「只有一个板块有数据时，它白拿满分」。
    # 实测就踩到了 —— 视频源只有 AI 有量（134），其余 0，AI 凭空多拿 10 分冲到榜首。
    # 故有效板块 < 3 时该源整体不参与，权重全额还给论坛。
    vid_valid = sum(1 for v in vid.values() if v > 0)
    if stale > VIDEO_STALE_DAYS or vid_valid < 3:
        w_v = 0.0
    else:
        w_v = W_VIDEO
    w_f = W_FORUM + (W_VIDEO - w_v)          # 视频降权/停用，差额还给论坛

    plates = set(agg) | set(vid)
    if not plates:
        return {"ok": False, "error": "窗口内两源均无板块数据"}

    tot_cur = _daily_total(con, days)
    tot_prev = _daily_total(con, prev_days)

    rows = []
    for p in plates:
        a = agg.get(p, {"posts": 0, "stocks": 0, "daily": []})
        members = mem_cnt.get(p, 0)
        breadth = (a["stocks"] / members) if members else 0.0
        # 占比口径：板块日帖 / 全站日帖，再取中位数。
        # 为什么不用绝对帖数：实测单日全站 71~824 帖（抓取覆盖不均，差 10 倍以上），
        # 10-21 一天爆到 824 就把「上周」撑高，任何绝对量环比都会算出全榜 -60%~-80% 的假降温。
        # 占比先把分母削掉，剩下的才是「这个板块在讨论里占多大份额」。
        sh_cur = _median(_shares(a.get("daily") or [], days, tot_cur))
        sh_prev = _median(_shares((agg_prev.get(p) or {}).get("daily") or [], prev_days, tot_prev))
        rows.append({
            "plate": p,
            "n_posts": a["posts"],
            "n_stocks": a["stocks"],
            "members": members,
            "breadth": round(breadth, 4),
            "n_videos": vid.get(p, 0),
            "share": round(sh_cur, 2),
            "share_prev": round(sh_prev, 2),
            "prev_posts": (agg_prev.get(p) or {}).get("posts", 0),
            "src": "+".join([s for s, ok in (("forum", a["posts"]), ("video", vid.get(p, 0))) if ok]) or "—",
        })

    # 三源各自 log1p → min-max。论坛侧用**占比**而非绝对帖数，尺度逐日可比。
    # 广度用 breadth（提及股数/成员数）而非提及股数的绝对值 ——
    # 用绝对值等于奖励大板块：AI 有 1074 只成员，48 只被提及只有 4.5%，
    # 而白酒 19 只里 7 只被提及是 37%，后者才叫「整个板块都在聊」。
    lf = [math.log1p(r["share"]) for r in rows]
    lv = [math.log1p(r["n_videos"]) for r in rows]
    lb = [math.log1p(r["breadth"] * 100) for r in rows]
    for r, a, b, c in zip(rows, _minmax(lf), _minmax(lv), _minmax(lb)):
        r["heat"] = round((w_f * a + w_v * b + W_BREADTH * c) * 100, 1)
    rows.sort(key=lambda r: (-r["heat"], -r["share"], r["plate"]))

    con.execute("DELETE FROM plate_heat WHERE asof_date = ?", (asof,))
    con.execute("DELETE FROM plate_heat_top WHERE asof_date = ?", (asof,))
    for i, r in enumerate(rows, 1):
        # 环比＝占比的**百分点变化**（不是百分比变化）：
        # 「从占讨论的 12% 升到 25%」说 +13pp，比「涨了 108%」更好懂，也不会被小基数放大。
        chg = None
        if r["share_prev"] >= MIN_CMP and r["share"] >= MIN_CMP:
            chg = round(r["share"] - r["share_prev"], 1)
        con.execute(
            "INSERT INTO plate_heat(asof_date,plate,n_posts,n_stocks,members,breadth,"
            " n_videos,share,heat,rank_no,prev_posts,chg_pp,src)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (asof, r["plate"], r["n_posts"], r["n_stocks"], r["members"], r["breadth"],
             r["n_videos"], r["share"], r["heat"], i, r["prev_posts"], chg, r["src"]))
        # 代表股：只在论坛源有的板块出（视频源没有个股粒度）
        tops = sorted(((v[1], k[1], v[0]) for k, v in detail.items() if k[0] == r["plate"]),
                      reverse=True)[:TOP_STOCKS]
        for n, code, name in tops:
            con.execute("INSERT OR REPLACE INTO plate_heat_top(asof_date,plate,code,name,n_mentions)"
                        " VALUES(?,?,?,?,?)", (asof, r["plate"], code, name, n))

    meta = {
        "asof": asof,
        "window": json_days(days),
        "prev_window": json_days(prev_days),
        "video_asof": vid_asof,
        "video_stale_days": str(stale if vid_asof else ""),
        "video_weight": str(w_v),
        "forum_weight": str(round(w_f, 2)),
        "video_valid_plates": str(vid_valid),
        "built_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "n_plates": str(len(rows)),
        "cmp_note": "环比＝「本板块帖数占全站帖数」的百分点变化（本窗口 vs 上一窗口，取中位日），"
                    "两边占比均≥%s%% 才计算；绝对帖数因每日抓取覆盖 71~824 帖波动过大，不用于环比" % MIN_CMP,
        "orphan_note": "论坛提及仅覆盖能关联到 posts 的部分（约 12%），其余为历史孤儿提及",
    }
    for k, v in meta.items():
        con.execute("INSERT OR REPLACE INTO plate_heat_meta(k,v) VALUES(?,?)", (k, str(v)))
    con.commit()
    return {"ok": True, "asof": asof, "n_plates": len(rows),
            "top": [(r["plate"], r["heat"]) for r in rows[:5]]}


def json_days(days: list[str]) -> str:
    return (days[-1] + "~" + days[0]) if days else ""


def payload(con: sqlite3.Connection, rebuild: bool = False) -> dict:
    """给前端：榜单 + 代表股 + 口径说明。无数据时返回可执行的提示而不是空壳。

    自愈：库里的 asof 落后于「最新有提及的交易日」时就地重算（实测 0.24s，可接受），
    免得页面永远停在前一天的榜单 —— 那种「看着是今天的数、其实是昨天的」最难被察觉。
    """
    asof_row = con.execute("SELECT MAX(asof_date) a FROM plate_heat").fetchone() \
        if _has_table(con, "plate_heat") else None
    asof = (asof_row["a"] if asof_row else None)
    latest = (_recent_days(con, 1) or [None])[0]
    if rebuild or not asof or (latest and asof < latest):
        r = build(con)
        if not r.get("ok"):
            return {"ok": False, "error": r.get("error"), "rows": []}
        asof = r["asof"]

    rows = [dict(x) for x in con.execute(
        "SELECT * FROM plate_heat WHERE asof_date = ? ORDER BY rank_no", (asof,))]
    tops: dict[str, list] = {}
    for t in con.execute("SELECT * FROM plate_heat_top WHERE asof_date = ?"
                         " ORDER BY n_mentions DESC", (asof,)):
        tops.setdefault(t["plate"], []).append(
            {"code": t["code"], "name": t["name"], "n": t["n_mentions"]})
    for r in rows:
        r["top_stocks"] = tops.get(r["plate"], [])

    meta = {m["k"]: m["v"] for m in con.execute("SELECT * FROM plate_heat_meta")}
    return {"ok": True, "asof": asof, "rows": rows, "meta": meta,
            "weights": {"forum": W_FORUM, "video": W_VIDEO, "breadth": W_BREADTH},
            "window": WINDOW}


def _has_table(con: sqlite3.Connection, name: str) -> bool:
    try:
        con.execute("SELECT 1 FROM %s LIMIT 1" % name).fetchone()
        return True
    except sqlite3.Error:
        return False


def run() -> dict:
    """给独立调度器（tools/sched_job.py）用的自连接入口。

    与 regime.run / blogger_score.run 同形：自己开库、自己关，不依赖 HTTP 进程的 DB 单例 ——
    调度器是独立进程，拿不到 server.py 里的 DB 对象。
    """
    con = sqlite3.connect(DB_PATH, timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA busy_timeout=30000")
    try:
        return build(con)
    finally:
        con.close()


if __name__ == "__main__":
    print(run())
