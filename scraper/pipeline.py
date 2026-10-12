"""采集管线：加载配置 -> 登录 -> 逐 URL 抓取 -> 解析 -> 合规过滤 -> 存储 -> 累积存档。

只产出公开内容（实盘交易 + 资讯），并在落库前强制跑一次合规清洗，
即便解析器误抓了用户行为 / 画像字段也会被剔除。

新增能力：每次运行可顺带把增量写入一个持续维护的 Excel 存档
（见 scraper/archive.ExcelArchive），形成可累积查阅的历史记录。
"""
from __future__ import annotations

import logging
import os
from typing import Iterable, Optional

from . import parsers as parser_pkg
from . import storage
from .archive import ExcelArchive
from .auth import LoginError, ensure_login
from .compliance import filter_public_only
from .config import get_site, load_config
from .session import RateLimitedSession

logger = logging.getLogger(__name__)

# 新增站点解析器在此注册
PARSER_REGISTRY = {
    "taoguba": parser_pkg.taoguba.TaogubaParser,
    "eastmoney": parser_pkg.eastmoney.EastMoneyParser,
    "xueqiu": parser_pkg.xueqiu.XueqiuParser,
    "ths": parser_pkg.ths.ThsParser,
    "sina": parser_pkg.sina.SinaParser,
}


def build_parser(site_key: str, cfg: dict):
    cls = PARSER_REGISTRY.get(site_key)
    if not cls:
        raise ValueError(f"无解析器: {site_key}（请在 pipeline.PARSER_REGISTRY 注册）")
    return cls(cfg)


def run(
    site_key: str,
    mode: str = "all",
    out_dir: str = "./data",
    config_path: Optional[str] = None,
    fmt: str = "json",
    respect_robots: bool = True,
    no_login: bool = False,
    archive_path: Optional[str] = None,
    scrape_date: Optional[str] = None,
) -> tuple[int, int, int]:
    """返回 (总记录数, 新增交易数, 新增资讯数)。

    无论抓取成功与否，只要传入 archive_path，都会在「每日汇总」留下一条运行记录，
    便于后续核查每日执行情况。
    """
    data = load_config(config_path)
    cfg = get_site(data, site_key)
    cfg["_key"] = site_key

    sess = RateLimitedSession(
        user_agent=cfg.get("user_agent", ""),
        rate_limit_per_min=cfg.get("rate_limit_per_min", 20),
        timeout=cfg.get("timeout", 15),
        retry_max=cfg.get("retry_max", 3),
        respect_robots=respect_robots,
    )

    # 登录（失败不崩溃，记录状态后退出）
    status = "ok"
    note = ""
    if cfg.get("requires_login") and not no_login:
        try:
            ensure_login(sess, cfg)   # Cookie 注入优先，无 Cookie 再回退表单登录
        except LoginError as e:
            logger.error("登录失败: %s", e)
            status = "login_failed"
            note = str(e)[:200]
        except Exception as e:  # noqa: BLE001
            logger.error("登录异常: %s", e)
            status = "login_error"
            note = str(e)[:200]
        if status != "ok":
            if archive_path:
                ExcelArchive(archive_path).log_run(site_key, mode, 0, 0, status, note)
            return 0, 0, 0

    parser = build_parser(site_key, cfg)
    targets = cfg.get("targets", {})
    modes = ("trades", "news") if mode == "all" else (mode,)

    trades_clean: list = []
    news_clean: list = []
    for m in modes:
        for url in targets.get(m, []):
            logger.info("抓取 %s -> %s", m, url)
            try:
                resp = sess.get(url)
            except Exception as e:  # noqa: BLE001
                logger.error("抓取失败 %s: %s", url, e)
                status = "fetch_error"
                note = f"{url}: {e}"[:200]
                continue
            recs: Iterable = (
                parser.parse_news(resp.text, url)
                if m == "news"
                else parser.parse_trades(resp.text, url)
            )
            cleaned = filter_public_only([r.to_dict() for r in recs])
            if m == "news":
                news_clean.extend(cleaned)
            else:
                trades_clean.extend(cleaned)
            logger.info("  解析到 %d 条（合规清洗后）", len(cleaned))

    if status == "ok" and (len(trades_clean) + len(news_clean)) == 0 and any(targets.get(m) for m in modes):
        # 抓取到了页面但解析为空，多半是选择器未校准
        status = "empty_parse"
        note = "页面已抓取但解析为空，请运行 inspect 校准 selectors/targets"

    # 原始落盘（保持既有行为）
    os.makedirs(out_dir, exist_ok=True)
    all_records = trades_clean + news_clean
    out_path = os.path.join(out_dir, f"{site_key}_{mode}.{ 'db' if fmt == 'sqlite' else fmt }")
    if fmt == "json":
        storage.save_json(all_records, out_path)
    elif fmt == "csv":
        storage.save_csv(all_records, out_path)
    else:
        storage.save_sqlite(all_records, out_path, "records")
    logger.info("已写出 %d 条 -> %s", len(all_records), out_path)

    # 累积存档
    new_t = new_n = 0
    if archive_path:
        try:
            arc = ExcelArchive(archive_path)
            new_t = arc.append_trades(trades_clean, scrape_date)
            new_n = arc.append_news(news_clean, scrape_date)
            arc.log_run(site_key, mode, new_t, new_n, status, note)
            logger.info("存档更新：新增交易 %d、新增资讯 %d（状态 %s）", new_t, new_n, status)
        except Exception as e:  # noqa: BLE001
            logger.error("存档写入失败（不影响原始抓取）: %s", e)

    return len(all_records), new_t, new_n
