"""接入层：复用既有采集程序（credentials / auth / session / parser / compliance），
把抓取结果写入查看器数据库，并保留原始页面 HTML。

负责：
- 逐站「登录检测」：用本地加密凭据尝试登录，记录 可用/无凭据/失败 状态。
- 登录态复用：登录成功后缓存 requests.Session，供本次进程内实时抓取复用。
- 抓取 -> 解析 -> 合规清洗 -> 入库（raw_pages / posts / trades / news / players / positions + 派生表）。
- 实盘选手筛选（前 30% / 前 50）+ 渠道标注（A/B/C/D）+ 逐选手持仓比例采集。
"""
from __future__ import annotations

import io
import logging
import sys
import threading
from pathlib import Path
from typing import Optional

# 把工程根目录加入 sys.path，使 `from scraper import ...` 可用
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scraper import parsers as parser_pkg  # noqa: E402
from scraper.auth import LoginError, ensure_login  # noqa: E402
from scraper.compliance import filter_public_only  # noqa: E402
from scraper.config import get_site, load_config  # noqa: E402
from scraper.players import (  # noqa: E402
    Player,
    label_channels,
    select_top_players,
    select_by_evidence,
    TIER_WEIGHTS,
)
from scraper.session import RateLimitedSession  # noqa: E402
from scraper.browser_fetch import browser_get  # noqa: E402  # 雪球需浏览器过 WAF

from . import analytics  # noqa: E402
from .db import Database  # noqa: E402
from .stock_names import get_resolver, name_to_code  # noqa: E402

logger = logging.getLogger(__name__)

PARSER_REGISTRY = {
    "taoguba": parser_pkg.taoguba.TaogubaParser,
    "eastmoney": parser_pkg.eastmoney.EastMoneyParser,
    "xueqiu": parser_pkg.xueqiu.XueqiuParser,
    "ths": parser_pkg.ths.ThsParser,
    "sina": parser_pkg.sina.SinaParser,
}

# 2026-09-25：多站点并发抓取时，各站 ingest 末尾都会重算话题表 + 名称补全，
# 这两步读写的是「全站共享」状态（topics 整表清空重建 / resolver 缓存），并发会互相踩。
# 用一把锁把这两步串行化；站点各自的库表写入仍走 WAL + 每线程独立连接，保持并发。
_TOPIC_LOCK = threading.Lock()


class Ingestor:
    def __init__(self, db: Database, config_path: Optional[str] = None):
        self.db = db
        self.config_path = config_path
        self._sessions: dict[str, RateLimitedSession] = {}

    # ---------------- 登录检测与复用 ----------------
    def _login(self, site_key: str, cfg: dict, force: bool = False) -> Optional[RateLimitedSession]:
        if site_key in self._sessions and not force:
            return self._sessions[site_key]
        if not cfg.get("requires_login"):
            sess = RateLimitedSession(
                user_agent=cfg.get("user_agent", ""),
                rate_limit_per_min=cfg.get("rate_limit_per_min", 15),
                # 2026-09-24：站点配置里可显式关掉 robots（公开个人抓取场景常见，
                # 例如东方财富股吧的列表页 robots 限制严格，但实际合规义务需在
                # config/compliance.yaml 另行维护——这里只是机械跳过 robots 检查）。
                respect_robots=bool(cfg.get("respect_robots", True)),
                # 2026-09-24：站点配置里可传 Referer/Accept 等伪装头，用以绕过东财等
                # 站点的「身份核实」风控（无 Cookie / 无 Referer 时直接返回 2.8KB 验证页）。
                extra_headers=cfg.get("extra_headers") or None,
            )
            self._sessions[site_key] = sess
            return sess
        try:
            sess = RateLimitedSession(
                user_agent=cfg.get("user_agent", ""),
                rate_limit_per_min=cfg.get("rate_limit_per_min", 15),
                timeout=cfg.get("timeout", 15),
                # 同上：登录站点一般也可显式关掉 robots（登录后页面不在 robots 限制内）
                respect_robots=bool(cfg.get("respect_robots", True)),
            )
            method = ensure_login(sess, cfg)   # Cookie 注入优先，无 Cookie 再走表单登录
            logger.info("登录方式: %s（站点 %s）", method, site_key)
            self._sessions[site_key] = sess
            return sess
        except LoginError as e:
            self.db.set_login_status(site_key, "login_failed", str(e)[:200])
            return None
        except Exception as e:  # noqa: BLE001
            self.db.set_login_status(site_key, "error", str(e)[:200])
            return None

    def login_check(self, site_key: str) -> dict:
        data = load_config(self.config_path)
        cfg = get_site(data, site_key)
        cfg["_key"] = site_key
        if not cfg.get("requires_login"):
            self.db.set_login_status(site_key, "public", "该站点无需登录即可抓取公开页")
            return {"site": site_key, "status": "public"}
        try:
            from scraper.credentials import get_cookies, get_credential

            has_pw = bool(get_credential(site_key))
            cookies = get_cookies(site_key)
            if not (has_pw or cookies):
                self.db.set_login_status(
                    site_key, "no_credential",
                    "尚未登录：点右侧「登录」链接进入网站完成登录，登录后程序会自动抓取并复用 Cookie",
                )
                return {"site": site_key, "status": "no_credential"}
        except Exception:
            self.db.set_login_status(site_key, "no_credential", "凭据读取失败")
            return {"site": site_key, "status": "no_credential"}
        sess = self._login(site_key, cfg, force=True)
        if sess is None:
            return {"site": site_key, "status": "login_failed"}
        src = "Cookie" if cookies else "账号密码"
        try:
            from scraper.auth import verify_login

            verdict, msg = verify_login(sess, cfg)
            if verdict is False:
                self.db.set_login_status(site_key, "login_failed", f"{src} 登录态校验未通过：{msg}")
                return {"site": site_key, "status": "login_failed", "method": src}
            probe = (cfg.get("targets", {}).get("trades") or [cfg.get("base_url", "")])[0]
            r = sess.get(probe)
            if r.status_code < 400:
                self.db.set_login_status(
                    site_key, "ok", f"登录态可用（{src}）：{msg}；已访问 {probe} ({r.status_code})"
                )
                return {"site": site_key, "status": "ok", "method": src}
            self.db.set_login_status(site_key, "login_failed", f"受限页返回 {r.status_code}")
            return {"site": site_key, "status": "login_failed"}
        except Exception as e:  # noqa: BLE001
            self.db.set_login_status(site_key, "error", str(e)[:200])
            return {"site": site_key, "status": "error"}

    # ---------------- 实盘选手筛选 + 渠道标注 ----------------
    def collect_players(self, site_key: str, cfg: dict, sess, parser) -> int:
        """抓取选手排行榜 -> 按入围标准筛选 -> 渠道标注(A/B/C/D) -> 入库。
        返回新增/更新选手数。"""
        urls = cfg.get("targets", {}).get("players", [])
        if not urls:
            logger.warning("未配置 players 目标页，跳过选手筛选")
            return 0
        top_n = int(cfg.get("top_players", {}).get("top_n", 50))
        top_pct = float(cfg.get("top_players", {}).get("top_pct", 0.3))
        channel_rules = cfg.get("channel_rules", [])
        channel_keywords = (cfg.get("selection", {}) or {}).get("channel_keywords", None)
        # 合并入围评判规则（selection 段为主，规模截点取 top_players 段）
        sel = dict(cfg.get("selection", {}) or {})
        sel.setdefault("top_n", top_n)
        sel.setdefault("top_pct", top_pct)

        all_ranked = []
        for url in urls:
            try:
                resp = sess.get(url)
            except Exception as e:  # noqa: BLE001
                logger.error("选手页抓取失败 %s: %s", url, e)
                continue
            self.db.save_raw_page(site_key, url, resp.text, resp.status_code)
            for p in parser.parse_players(resp.text, url):
                p["source_url"] = p.get("source_url") or url
                all_ranked.append(p)

        if not all_ranked:
            return 0
        # 第一阶段：按名次/认证/收益/活跃度门槛筛选（此时持仓/交易尚未采集，真实性门槛暂不过滤）
        chosen = select_top_players(all_ranked, sel)
        chosen = label_channels(chosen, channel_rules, channel_keywords)
        for p in chosen:
            self.db.save_player(
                site=site_key, name=p.name, channel=p.channel,
                rank_position=p.rank_position, ranking_score=p.ranking_score,
                is_top_player=p.is_top_player, is_certified=p.is_certified,
                source_url=p.source_url,
            )
        logger.info("选手筛选完成：候选 %d -> 入选 %d（前%d名/前%.0f%%）",
                    len(all_ranked), len(chosen), top_n, top_pct * 100)
        return len(chosen)

    # ---------------- 逐选手持仓比例采集 ----------------
    def collect_positions(self, site_key: str, cfg: dict, sess, parser) -> int:
        """对"入选选手"逐人抓取持仓页，抽取持仓比例 -> 入库。
        返回新增持仓条数。
        二次校验：若开启真实性门槛且某选手既无持仓也无交易记录（已确认空谈），则降级 is_top_player。
        官方认证选手不受此降级影响（certify_auto_include）。"""
        tpl = cfg.get("player_page_tpl", "")
        if not tpl:
            logger.warning("未配置 player_page_tpl，跳过持仓采集")
            return 0
        players = self.db.get_players(top_only=True)
        if not players:
            logger.warning("无入选选手，先运行 players 采集")
            return 0
        require_verifiable = bool((cfg.get("selection", {}) or {}).get("require_verifiable", True))
        n = 0
        for pl in players:
            url = tpl.format(name=pl["name"], username=pl["name"])
            try:
                resp = sess.get(url)
            except Exception as e:  # noqa: BLE001
                logger.error("选手页抓取失败 %s: %s", url, e)
                continue
            self.db.save_raw_page(site_key, url, resp.text, resp.status_code)
            for pos in parser.parse_positions(resp.text, url, player=pl["name"]):
                if not pos.get("symbol"):
                    continue
                # 卖出流水写入 sell_time/sell_price，不占用 buy_* —— 否则「卖出价」会被
                # 前端当作「买入价」展示（历史遗留的语义错位）。
                is_sell = (pos.get("action") or "").upper() == "SELL"
                self.db.save_position(
                    site=site_key, player=pos.get("player") or pl["name"],
                    channel=pl["channel"], symbol=pos.get("symbol"),
                    symbol_name=pos.get("symbol_name"),
                    ratio=pos.get("ratio"), action=pos.get("action"),
                    post_time="", post_url=url, raw_text=pos.get("raw_text", ""),
                    buy_time="" if is_sell else (pos.get("buy_time") or ""),
                    buy_price=None if is_sell else pos.get("buy_price"),
                    sell_time=(pos.get("sell_time") or pos.get("trade_time") or "") if is_sell else "",
                    sell_price=(pos.get("sell_price") or pos.get("buy_price")) if is_sell else None,
                    hold_since=pos.get("hold_since") or pos.get("open_date") or "",
                    current_price=pos.get("current_price"),
                    access_level=pos.get("access_level") or "free",
                    access_hint=pos.get("access_hint") or "",
                )
                n += 1
            # 二次校验：真实性门槛
            if require_verifiable and not pl.get("is_certified"):
                pos_n = self.db.conn.execute(
                    "SELECT COUNT(*) FROM positions WHERE player=?", (pl["name"],)).fetchone()[0]
                tr_n = self.db.conn.execute(
                    "SELECT COUNT(*) FROM trades WHERE author=?", (pl["name"],)).fetchone()[0]
                if pos_n == 0 and tr_n == 0:
                    self.db.set_player_top(pl["name"], 0)
                    logger.info("真实性门槛未过，降级：%s（无持仓且无交易）", pl["name"])
        logger.info("持仓采集完成：%d 名选手 -> %d 条持仓记录", len(players), n)
        return n

    # ---------------- 跨站选手池重建（行为证据版，2026-09-25） ----------------
    def rebuild_player_pool(self, target_total: Optional[int] = None) -> dict:
        """跨站点重建选手池：真实发帖/交易/持仓作者 -> 行为证据入选 -> 收敛总量 ~200。

        背景：东财/同花顺无公开选手排行榜（parse_players 显式放弃）、雪球未配选手页，
        示例数据清理后选手池只剩淘股吧 68 人。本方法从库内**真实行为记录**跨站补齐：
          - 保留：已有名次（rank_position）或认证的既有选手（榜选结果，不参与重排）；
          - 候选：posts 表中的真实发帖作者（四站均有，来源多样性由此保证）；
          - 证据分 = 发帖 + 3×交易 + 5×持仓；按分补足至 target_total（默认取配置 200）；
          - 渠道推导：有交易/持仓→A，发帖≥20→C，其余→D；层级=tier(channel)，可见性恒 1。
        纯库内计算，不发任何网络请求；幂等（重复执行结果一致）。
        返回 {"total": 入选总数, "kept": 保留榜选数, "derived": 行为入选数, "candidates": 候选作者数}。
        """
        # 规模目标：默认取 taoguba 段 selection.top_n（200）；可显式覆盖
        if target_total is None:
            try:
                data = load_config(self.config_path)
                target_total = int(((get_site(data, "taoguba").get("selection") or {}).get("top_n")) or 200)
            except Exception:  # noqa: BLE001
                target_total = 200

        # ---- 候选聚合：四站发帖作者 + 各自交易/持仓计数 ----
        conn = self.db.conn
        posts = conn.execute(
            """SELECT site, author, COUNT(*) AS n, MAX(post_time) AS last_post
               FROM posts WHERE COALESCE(TRIM(author),'')<>'' GROUP BY site, author"""
        ).fetchall()
        trades = dict(
            ((s, a), n) for s, a, n in conn.execute(
                """SELECT site, author, COUNT(*) FROM trades
                   WHERE COALESCE(TRIM(author),'')<>'' GROUP BY site, author"""
            ).fetchall()
        )
        try:
            positions = dict(
                (p, n) for p, n in conn.execute(
                    "SELECT player, COUNT(*) FROM positions WHERE COALESCE(TRIM(player),'')<>'' GROUP BY player"
                ).fetchall()
            )
        except Exception:  # noqa: BLE001
            positions = {}

        # ---- 既有选手：榜选/认证者保留（keep），其余行保留原样但视为已入选（都是真实抓取所得） ----
        existing = self.db.get_players()
        keep_names = {
            r["name"] for r in existing
            if r.get("rank_position") or r.get("is_certified") or r.get("is_top_player")
        }
        # 全部既有行都直接保留（真实抓取所得，不删除），只统计 keep 口径
        existing_names = {r["name"] for r in existing}

        rows = []
        for site, author, n, last_post in posts:
            rows.append({
                "site": site, "name": author, "post_count": int(n),
                "trade_count": int(trades.get((site, author), 0)),
                "position_count": int(positions.get(author, 0)),
                "last_post": last_post or "",
            })
        chosen = select_by_evidence(rows, keep_names=keep_names, target_total=10 ** 9)

        # ---- 精确配额填充：既有行全部保留，行为入选只补足到 target_total，总量精确收敛 ----
        fill = max(0, int(target_total) - len(existing))
        derived = 0
        for item in chosen:
            if derived >= fill:
                break
            name = item["name"]
            if name in existing_names:
                continue  # 既有行不覆盖（保留原 rank/source_url），只补行为分
            self.db.save_player(
                site=item["site"], name=name, channel=item["channel"],
                rank_position=None, ranking_score=round(float(item["evidence"]), 2),
                is_top_player=1, is_certified=0, source_url="",
                tier=item["channel"], tier_weight=TIER_WEIGHTS.get(item["channel"], 0.45),
                influence=min(100.0, float(item["evidence"]) * 2.0),  # 证据分映射到 0~100 影响力
                is_visible=1, platform=item["site"], followers=0,
            )
            derived += 1
        total = self.db.conn.execute("SELECT COUNT(*) FROM players").fetchone()[0]
        kept = len(keep_names & {c["name"] for c in chosen})
        out = {"total": total, "kept": kept, "derived": derived, "candidates": len(rows)}
        logger.info("选手池重建完成：候选 %d -> 入选 %d（既有保留 %d + 行为入选 %d）",
                    len(rows), total, len(existing), derived)
        return out

    # ---------------- 抓取并入库 ----------------
    def ingest(self, site_key: str, mode: str = "all", respect_robots: bool = True) -> dict:
        data = load_config(self.config_path)
        cfg = get_site(data, site_key)
        cfg["_key"] = site_key
        sess = self._login(site_key, cfg)
        # 2026-09-24 修复：需要登录但 _login 返回 None 时**直接抛 LoginError**，
        # 不再静默兜底为无认证 session 去抓登录跳转页（之前会让 parser 在登录页上
        # 解析为空，导致库表看似没刷新、用户误以为程序不干活）。
        if sess is None and cfg.get("requires_login"):
            raise LoginError(
                f"{site_key} 需要登录但当前未登录。点击「点此登录」按钮，程序会自动"
                f"打开浏览器登录并保存 Cookie，然后再次「抓取并更新」即可拿到实时数据。"
            )
        parser = PARSER_REGISTRY[site_key](cfg)
        targets = cfg.get("targets", {})
        modes = ("news", "trades", "posts", "players", "positions") if mode == "all" else (mode,)

        stats = {"site": site_key, "raw_pages": 0, "news": 0, "trades": 0, "posts": 0,
                 "players": 0, "positions": 0}

        # 选手榜 / 持仓 走专门流程
        if "players" in modes:
            stats["players"] = self.collect_players(site_key, cfg, sess, parser)
        if "positions" in modes:
            stats["positions"] = self.collect_positions(site_key, cfg, sess, parser)

        generic = [m for m in modes if m in ("news", "trades", "posts")]
        for m in generic:
            urls = targets.get(m, [])
            if not urls and m == "posts":
                urls = targets.get("trades", [])  # 回退：用实盘页抽取讨论文本
            for url in urls:
                try:
                    # 雪球被阿里云 WAF + 滑块验证码拦截，裸 requests 拿不到内容；
                    # 必须用真实浏览器渲染后取 HTML（browser_fetch 封装 puppeteer-core）。
                    if site_key == "xueqiu":
                        resp = browser_get(url)
                    else:
                        resp = sess.get(url)
                    # 2026-09-25：新浪系列页面为 GBK 且 HTTP 头不声明 charset，
                    # requests 会按 ISO-8859-1 错解出乱码——必须在首次访问 resp.text
                    # 之前显式设好编码（save_raw_page 与 parser 共用同一文本）。
                    if site_key == "sina" and (resp.encoding or "").lower() == "iso-8859-1":
                        resp.encoding = "gb18030"
                except Exception as e:  # noqa: BLE001
                    logger.error("抓取失败 %s: %s", url, e)
                    continue
                page_id = self.db.save_raw_page(site_key, url, resp.text, resp.status_code)
                stats["raw_pages"] += 1

                if m == "news":
                    recs = [r.to_dict() for r in parser.parse_news(resp.text, url)]
                    for r in filter_public_only(recs):
                        self.db.save_news(site=site_key, title=r.get("title"), url=r.get("url"),
                                          # 兼容 datetime（有时刻）与 str（仅日期，源未给时刻）
                                          publish_time=(r.get("publish_time").strftime("%Y-%m-%d %H:%M:%S")
                                                        if getattr(r.get("publish_time"), "strftime", None)
                                                        else (r.get("publish_time") or "")),
                                          summary=r.get("summary", ""), source=r.get("author"))
                        stats["news"] += 1
                elif m == "trades":
                    recs = [r.to_dict() for r in parser.parse_trades(resp.text, url)]
                    def _ts(v):
                        """兼容 datetime 对象与字符串两种时间形态。"""
                        return v.strftime("%Y-%m-%d %H:%M:%S") if getattr(v, "strftime", None) else (v or "")

                    for r in filter_public_only(recs):
                        # 2026-09-29：淘股吧等源帖常只写名字不写代码 → 入库前本地反查（离线，
                        # 不阻塞抓取；未命中保持"有名无码"，由回填脚本/联网补全兜底）。
                        _sym = r.get("symbol")
                        if not _sym and r.get("symbol_name"):
                            _hit = name_to_code(r.get("symbol_name"), db=self.db, allow_online=False)
                            if _hit:
                                _sym = _hit["symbol"]
                        # 卖出流水的成交时间/价写 sell_*，买入流水写 buy_*（前端据此分列展示）
                        _is_sell = (r.get("action") or "").upper() == "SELL"
                        _trade_ts = _ts(r.get("buy_time")) or _ts(r.get("post_time"))
                        _trade_px = r.get("buy_price") if r.get("buy_price") is not None else r.get("price")
                        self.db.save_trade(site=site_key, author=r.get("author"), post_id=r.get("post_id"),
                                           post_url=r.get("post_url"),
                                           post_time=_ts(r.get("post_time")),
                                           action=r.get("action"), symbol=_sym,
                                           symbol_name=r.get("symbol_name"), price=r.get("price"),
                                           quantity=r.get("quantity"), raw_text=r.get("raw_text", ""),
                                           buy_time="" if _is_sell else _trade_ts,
                                           buy_price=None if _is_sell else _trade_px,
                                           sell_time=_trade_ts if _is_sell else "",
                                           sell_price=_trade_px if _is_sell else None,
                                           hold_since=r.get("hold_since") or "",
                                           current_price=r.get("current_price"),
                                           access_level=r.get("access_level") or "free",
                                           access_hint=r.get("access_hint") or "")
                        stats["trades"] += 1
                elif m == "posts":
                    for p in parser.parse_posts(resp.text, url):
                        sent, score = analytics.score_sentiment(p.get("text", ""))
                        pid = self.db.save_post(site=site_key, author=p.get("author"), post_id=p.get("post_id"),
                                                post_url=p.get("post_url"), post_time=p.get("post_time", ""),
                                                text=p.get("text", ""), sentiment=sent, sentiment_score=score,
                                                raw_page_id=page_id)
                        stats["posts"] += 1
                        # 讨论股票（6 位代码）：同时把代码翻译成股票名称，避免界面只剩裸代码
                        for code in set(analytics.extract_codes(p.get("text", ""))):
                            nm = get_resolver(db=self.db).resolve(code, allow_online=False).get("name", "")
                            self.db.save_stock_mention(pid, code, nm, p.get("text", "")[:120])
                        # 人物（作者 + @提及）
                        if p.get("author") and p.get("author") != "unknown":
                            self.db.upsert_person(p["author"], "author")
                        for who in analytics.extract_mentions(p.get("text", "")):
                            self.db.upsert_person(who, "mention")

        # 重算话题表 + 名称补全属于「全站共享」写操作，必须串行化（见 _TOPIC_LOCK）。
        with _TOPIC_LOCK:
            self._recompute_topics()
            # 本次抓取出现的代码：用内置表/配置做一次离线名称补全（不联网，避免阻塞抓取）。
            # 仍缺名的代码由界面「补全股票名称」按钮联网查询（见 /api/sync_names）。
            try:
                get_resolver(db=self.db).backfill_missing(online=False)
            except Exception as e:  # noqa: BLE001 名称补全失败不应影响抓取结果
                logger.warning("名称补全跳过：%s", e)
        self.db.set_meta("last_ingest", f"{site_key}:{mode}:{stats}")
        return stats

    def _recompute_topics(self):
        rows = self.db.conn.execute("SELECT text FROM posts WHERE text IS NOT NULL AND text<>''").fetchall()
        texts = [r["text"] for r in rows]
        self.db.clear_topics()
        for topic, weight in analytics.extract_topics(texts, top_n=25):
            self.db.save_topic(topic, float(weight), "")
