# -*- coding: utf-8 -*-
"""把「抖音博主语料」两个新域（情绪/舆情、技术/方法）灌进 viewer/app.db（幂等，可反复执行）。

【背景（2026-10-20 进哥需求）】
荐股追踪查看台（磁盘 = 博主数据查看台）的豆包日更，原先只有「博主荐股 + 参赛股」两域，
已被查看器消费；2026-10-20 新增两个内容域：
  · 情绪/舆情 ← sentiment_bloggers.json（32 博主 / 69 视频，video 带 category）
  · 技术/方法 ← tech_bloggers.json     （37 博主 / 2701 视频，ASR 摘要）
这两个域是「博主在讲什么观点/方法」的语料，与荐股数字（blogger_reco）异构，
故独立落两张表，**零 JOIN** 既有业务表（不动 trades / blogger_* / contest_*）。

【落库表】
  douyin_corpus_blogger  UNIQUE(domain, name)   —— 博主档案（粉丝/简介/标签/频率）
  douyin_corpus_video    PK(video_id)           —— 视频语料（标题/摘要/分类/来源）
  douyin_corpus_meta     PK(domain)             —— 数据截至/行数/导入时刻，供前端显示「数据日」

【幂等 / 镜像口径】
  以「源文件为准的镜像」：本次源里出现的视频 UPSERT；该 domain 下源里已消失的 DELETE。
  为什么用镜像而不是只 UPSERT：豆包日更会**覆盖写**整个 JSON，旧条目可能被裁掉；
  只 UPSERT 会让库里留一堆源里早没有的僵尸行，前端「共 N 条」与真实不符。
  删除范围严格限定在 `domain = ?` 之内，绝不跨域、不碰其它表。

【清洗 / 校验（沿用 docs/抖音数据清洗规则.md）】
  R4 schema 校验：videoId/title/author/publishDate 必填；publishDate 必须 %Y-%m-%d 且 ≤ 今日；
                  videoUrl 若存在必须 http(s)；summary 为空 → NULL（不写空串）。
  校验失败 → 该域整体跳过、**不动库**，并以非零码退出，便于调度器告警。
  R1/R2 只适用于荐股数值域（博客/参赛股），本脚本不涉及价格，故不适用。

【口径诚实】
  · src_updated 取**源 JSON 的 mtime**（不是导入时刻），反复跑不漂移。
  · summary 缺失一律 NULL，绝不写空串或「暂无」占位；前端显示「—」。
  · 只清理本域，不 DELETE 跨表行。

【用法】
  python tools/import_douyin_corpus.py                # 两域全灌
  python tools/import_douyin_corpus.py --domain tech   # 只灌技术域
  python tools/import_douyin_corpus.py --dry-run       # 只体检不写库
  python tools/import_douyin_corpus.py --quiet         # 供调度器调用，只回一行汇总
  # 源路径可用环境变量覆盖：DOUYIN_SENTIMENT_JSON / DOUYIN_TECH_JSON
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import sqlite3
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PROJ = os.path.dirname(HERE)
DB = os.environ.get("VIEWER_DB") or os.path.join(PROJ, "viewer", "app.db")

# 源目录会搬家（2026-10-11 已踩过一次），故候选列表 + 取最新，不写死单一绝对路径。
_DATA_DIRS = [
    os.path.join(r"F:\AI Agent部署知识库", "博主数据查看台", "public", "data"),
    os.path.join(r"F:\AI Agent部署知识库", "豆包每日抓取", "data"),
    os.path.join(r"F:\AI Agent部署知识库", "豆包每日抓取"),
]
_DOMAIN_FILES = {
    "sentiment": ("sentiment_bloggers.json", "DOUYIN_SENTIMENT_JSON"),
    "tech": ("tech_bloggers.json", "DOUYIN_TECH_JSON"),
}
_DOMAIN_LABEL = {"sentiment": "抖音情绪/舆情", "tech": "抖音技术/方法"}


def pick_source(domain: str) -> str | None:
    fname, env_key = _DOMAIN_FILES[domain]
    env = os.environ.get(env_key)
    if env and os.path.exists(env):
        return env
    cands = [os.path.join(d, fname) for d in _DATA_DIRS]
    cands = [c for c in cands if os.path.exists(c)]
    if not cands:
        return None
    return max(cands, key=lambda p: os.path.getmtime(p))


def _s(v) -> str | None:
    """字符串清洗：去首尾空白；空串 → None（口径：宁可 NULL 也不写空串）。"""
    if v is None:
        return None
    if not isinstance(v, str):
        v = str(v)
    v = v.strip()
    return v or None


def _date_ok(s: str | None, today: str) -> bool:
    if not s:
        return False
    try:
        d = datetime.datetime.strptime(s, "%Y-%m-%d").date()
    except ValueError:
        return False
    return d.isoformat() <= today


def validate(payload: dict, domain: str) -> tuple[list, list, dict]:
    """R4 schema 校验 + 投影清洗。返回 (bloggers, videos, stats)。"""
    errs: list[str] = []
    today = datetime.date.today().isoformat()
    bloggers_raw = payload.get("bloggers")
    videos_raw = payload.get("videos")
    if not isinstance(bloggers_raw, list):
        errs.append("bloggers 不是数组")
        bloggers_raw = []
    if not isinstance(videos_raw, list):
        errs.append("videos 不是数组")
        videos_raw = []

    bloggers = []
    for i, b in enumerate(bloggers_raw):
        if not isinstance(b, dict):
            errs.append(f"bloggers[{i}] 非对象")
            continue
        name = _s(b.get("name"))
        if not name:
            errs.append(f"bloggers[{i}] 缺 name")
            continue
        tags = b.get("tags")
        bloggers.append({
            "name": name,
            "douyin_id": _s(b.get("douyinId")),
            "profile_url": _s(b.get("profileUrl")),
            "followers": _s(b.get("followers")),
            "followers_note": _s(b.get("followersNote")),
            "intro": _s(b.get("intro")),
            "tags_json": json.dumps(tags, ensure_ascii=False) if isinstance(tags, list) else None,
            "update_freq": _s(b.get("updateFreq")),
        })

    videos, seen = [], set()
    for i, v in enumerate(videos_raw):
        if not isinstance(v, dict):
            errs.append(f"videos[{i}] 非对象")
            continue
        vid = _s(v.get("videoId"))
        if not vid:
            errs.append(f"videos[{i}] 缺 videoId")
            continue
        if vid in seen:
            errs.append(f"videos[{i}] videoId 重复 {vid}（保留首条）")
            continue
        title = _s(v.get("title"))
        author = _s(v.get("author"))
        pdate = _s(v.get("publishDate"))
        # 2026-10-20 实测：豆包产出里确有 15~17 条 publishDate=null（抓不到发布日期）、
        # 6 条 title="" 但有完整 summary。这些是**真实语料**，不是脏数据 ——
        # 早期版本直接丢行会让 69 条变 52 条、2701 条变 2680 条，白白损失内容。
        # 口径改为：日期不可知 → 存 NULL（前端显示「日期未知」、排序列末）；标题空 → 存 NULL
        # （前端降级取 summary 首句当标题）。两者都只记警告，不丢行。
        if pdate and not _date_ok(pdate, today):
            errs.append(f"videos[{i}] ({vid}) publishDate 非法({pdate!r})，按未知处理")
            pdate = None
        elif not pdate:
            errs.append(f"videos[{i}] ({vid}) publishDate 缺失，按未知处理")
        if not author:
            errs.append(f"videos[{i}] ({vid}) 缺 author，作者置未知")
        if not title and not _s(v.get("summary")):
            # 标题与摘要双空 → 这条没有任何可读内容，才真的丢弃
            errs.append(f"videos[{i}] ({vid}) title 与 summary 均空，丢弃")
            continue
        url = _s(v.get("videoUrl"))
        if url and not url.lower().startswith(("http://", "https://")):
            errs.append(f"videos[{i}] ({vid}) videoUrl 非 http(s)，已置空")
            url = None
        seen.add(vid)
        videos.append({
            "video_id": vid,
            "title": title,
            "publish_date": pdate,
            "author": author or "未知博主",
            "video_url": url,
            "category": _s(v.get("category")),
            "summary": _s(v.get("summary")),
            "source_of_summary": _s(v.get("sourceOfSummary")),
        })

    # 博主视频计数：按 author 实际命中数回填（不信源里可能的声明值）
    cnt: dict[str, int] = {}
    for v in videos:
        cnt[v["author"]] = cnt.get(v["author"], 0) + 1
    for b in bloggers:
        b["n_video"] = cnt.get(b["name"], 0)

    stats = {
        "bloggers": len(bloggers),
        "videos": len(videos),
        "errors": len(errs),
        "orphan_authors": sorted(set(cnt) - {b["name"] for b in bloggers}),
    }
    return bloggers, videos, {"stats": stats, "errs": errs}


DDL = """
CREATE TABLE IF NOT EXISTS douyin_corpus_blogger (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  domain TEXT NOT NULL,
  name TEXT NOT NULL,
  douyin_id TEXT,
  profile_url TEXT,
  followers TEXT,
  followers_note TEXT,
  intro TEXT,
  tags_json TEXT,
  update_freq TEXT,
  n_video INTEGER DEFAULT 0,
  src_updated TEXT,
  UNIQUE(domain, name)
);
CREATE TABLE IF NOT EXISTS douyin_corpus_video (
  video_id TEXT PRIMARY KEY,
  domain TEXT NOT NULL,
  title TEXT,
  publish_date TEXT,
  author TEXT,
  video_url TEXT,
  category TEXT,
  summary TEXT,
  source_of_summary TEXT,
  src_updated TEXT
);
CREATE INDEX IF NOT EXISTS idx_dcv_domain_date ON douyin_corpus_video(domain, publish_date DESC);
CREATE INDEX IF NOT EXISTS idx_dcv_domain_author ON douyin_corpus_video(domain, author);
CREATE TABLE IF NOT EXISTS douyin_corpus_meta (
  domain TEXT PRIMARY KEY,
  src_path TEXT,
  src_updated TEXT,
  n_blogger INTEGER DEFAULT 0,
  n_video INTEGER DEFAULT 0,
  imported_at TEXT
);
"""


def ensure_schema(con: sqlite3.Connection) -> None:
    con.executescript(DDL)


def load_domain(con: sqlite3.Connection, domain: str, dry_run: bool, quiet: bool) -> dict:
    src = pick_source(domain)
    label = _DOMAIN_LABEL[domain]
    if not src:
        msg = f"[{domain}] 源文件缺失：{_DOMAIN_FILES[domain][0]}"
        print(("!! " if not quiet else "") + msg, file=sys.stderr)
        return {"domain": domain, "ok": False, "reason": "missing_source"}
    with open(src, "r", encoding="utf-8") as f:
        payload = json.load(f)
    bloggers, videos, pack = validate(payload, domain)
    st, errs = pack["stats"], pack["errs"]
    src_updated = datetime.datetime.fromtimestamp(os.path.getmtime(src)).strftime("%Y-%m-%d %H:%M")

    # 严重错误（整域结构坏）→ 不动库
    fatal = [e for e in errs if "不是数组" in e]
    if fatal:
        print(f"!! [{domain}] schema 致命错误，未动库：{fatal}", file=sys.stderr)
        return {"domain": domain, "ok": False, "reason": "schema", "errs": fatal}

    if not quiet:
        print(f"\n=== {label} ({domain}) ===")
        print(f"源文件 : {src}")
        print(f"mtime  : {src_updated}")
        print(f"博主   : {st['bloggers']}  视频 : {st['videos']}  警告 : {st['errors']}")
        if st["orphan_authors"]:
            print(f"有视频无档案的博主({len(st['orphan_authors'])})：{st['orphan_authors'][:8]}")
        for e in errs[:10]:
            print(f"  ⚠ {e}")
        if len(errs) > 10:
            print(f"  … 其余 {len(errs) - 10} 条同类警告略")

    if dry_run:
        return {"domain": domain, "ok": True, "dry_run": True, **st}

    ensure_schema(con)
    cur = con.cursor()
    # 博主：UPSERT
    for b in bloggers:
        cur.execute("""
            INSERT INTO douyin_corpus_blogger
              (domain,name,douyin_id,profile_url,followers,followers_note,intro,tags_json,update_freq,n_video,src_updated)
            VALUES (:domain,:name,:douyin_id,:profile_url,:followers,:followers_note,:intro,:tags_json,:update_freq,:n_video,:src_updated)
            ON CONFLICT(domain,name) DO UPDATE SET
              douyin_id=excluded.douyin_id, profile_url=excluded.profile_url,
              followers=excluded.followers, followers_note=excluded.followers_note,
              intro=excluded.intro, tags_json=excluded.tags_json,
              update_freq=excluded.update_freq, n_video=excluded.n_video,
              src_updated=excluded.src_updated
        """, {**b, "domain": domain, "src_updated": src_updated})
    # 视频：UPSERT
    for v in videos:
        cur.execute("""
            INSERT INTO douyin_corpus_video
              (video_id,domain,title,publish_date,author,video_url,category,summary,source_of_summary,src_updated)
            VALUES (:video_id,:domain,:title,:publish_date,:author,:video_url,:category,:summary,:source_of_summary,:src_updated)
            ON CONFLICT(video_id) DO UPDATE SET
              domain=excluded.domain, title=excluded.title, publish_date=excluded.publish_date,
              author=excluded.author, video_url=excluded.video_url, category=excluded.category,
              summary=excluded.summary, source_of_summary=excluded.source_of_summary,
              src_updated=excluded.src_updated
        """, {**v, "domain": domain, "src_updated": src_updated})
    # 镜像清理：本域内、源里已不存在的行
    keep = [v["video_id"] for v in videos]
    removed = 0
    if keep:
        qs = ",".join("?" * len(keep))
        cur.execute(f"DELETE FROM douyin_corpus_video WHERE domain=? AND video_id NOT IN ({qs})", [domain, *keep])
        removed = cur.rowcount or 0
    else:
        cur.execute("DELETE FROM douyin_corpus_video WHERE domain=?", (domain,))
        removed = cur.rowcount or 0
    # 博主镜像清理
    names = [b["name"] for b in bloggers]
    if names:
        qs = ",".join("?" * len(names))
        cur.execute(f"DELETE FROM douyin_corpus_blogger WHERE domain=? AND name NOT IN ({qs})", [domain, *names])
    else:
        cur.execute("DELETE FROM douyin_corpus_blogger WHERE domain=?", (domain,))
    # meta
    cur.execute("""
        INSERT INTO douyin_corpus_meta (domain,src_path,src_updated,n_blogger,n_video,imported_at)
        VALUES (?,?,?,?,?,?)
        ON CONFLICT(domain) DO UPDATE SET
          src_path=excluded.src_path, src_updated=excluded.src_updated,
          n_blogger=excluded.n_blogger, n_video=excluded.n_video, imported_at=excluded.imported_at
    """, (domain, src, src_updated, st["bloggers"], st["videos"],
          datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
    con.commit()
    if not quiet:
        print(f"落库   : 博主 {st['bloggers']} / 视频 {st['videos']}，清理源中已消失 {removed} 条")
    return {"domain": domain, "ok": True, "removed": removed, **st}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--domain", choices=["sentiment", "tech"], help="只灌某一域（缺省两域）")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--quiet", action="store_true")
    a = ap.parse_args()

    domains = [a.domain] if a.domain else ["sentiment", "tech"]
    if not os.path.exists(DB):
        print(f"!! 数据库不存在：{DB}", file=sys.stderr)
        return 2
    con = sqlite3.connect(DB, timeout=30)
    con.execute("PRAGMA busy_timeout=30000")
    results = []
    try:
        for d in domains:
            results.append(load_domain(con, d, a.dry_run, a.quiet))
    finally:
        con.close()

    bad = [r for r in results if not r.get("ok")]
    if a.quiet:
        bits = [f"{r['domain']}:{'ok' if r.get('ok') else r.get('reason')}"
                + (f"({r.get('videos')}v)" if r.get("ok") else "") for r in results]
        print("douyin_corpus " + " ".join(bits))
    else:
        print("\n--- 汇总 ---")
        for r in results:
            print(f"  {r['domain']}: " + ("OK" if r.get("ok") else f"FAIL({r.get('reason')})")
                  + (f" 博主{r.get('bloggers')}/视频{r.get('videos')}" if r.get("ok") else ""))
    return 0 if not bad else 1


if __name__ == "__main__":
    sys.exit(main())
