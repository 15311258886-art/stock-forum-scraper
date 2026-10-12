# -*- coding: utf-8 -*-
"""本机一键回填（A 部分）：复用已验证的 viewer.ingest 链路，把全站数据抓回 viewer/app.db。

为什么需要它：
- viewer/server.py 的「抓取并更新」按钮走的就是 Ingestor.ingest；本脚本把同一套逻辑
  变成「双击 bat 即可批量跑」，不依赖手动点界面。
- 雪球被阿里云 WAF 拦截，裸请求拿不到内容，ingest 内部已用 scraper.browser_fetch
  （puppeteer-core + 本机 Chromium）过 WAF——因此本机必须：
    ① 已安装 Chromium（路径见 scraper/browser_fetch.py 的 CHROME_EXE，或用环境变量
       XQ_CHROME_EXE 覆盖）；
    ② 已开启代理 127.0.0.1:53269（XQ_PROXY 可覆盖）。代理不可达时雪球会优雅跳过，不报错。
- 淘股吧需要登录 Cookie：先在本机执行
      .venv\Scripts\python.exe main.py credentials add-cookie taoguba --file cookies.json
  再跑本脚本；无凭据时淘股吧会被跳过并记录警告。

用法：
  .venv\Scripts\python.exe run_backfill_local.py [--sites taoguba,xueqiu]
日志：同目录 run_backfill_local.log（同时打印到控制台）。
"""
from __future__ import annotations

import argparse
import logging
import sys
import traceback
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# —— 日志：文件 + 控制台 ——
LOG_PATH = ROOT / "run_backfill_local.log"
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        logging.FileHandler(LOG_PATH, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger("backfill")

# 所有已配置且可用的站点；顺序：先公开站，再需登录/浏览器的站
ALL_SITES = ["eastmoney", "sina", "ths", "taoguba", "xueqiu"]


def main() -> int:
    ap = argparse.ArgumentParser(description="本机一键回填（复用 viewer.ingest）")
    ap.add_argument("--sites", default="", help="逗号分隔的站点，留空跑全部")
    ap.add_argument("--no-pool", action="store_true", help="跳过选手池重建")
    args = ap.parse_args()

    sites = [s.strip() for s in args.sites.split(",") if s.strip()] or ALL_SITES

    # 延迟导入，确保 sys.path 已就绪
    try:
        from viewer.db import Database
        from viewer.ingest import Ingestor
    except Exception as e:  # noqa: BLE001
        logger.exception("导入 viewer 模块失败（请在本工程根目录运行，且 .venv 已安装依赖）")
        return 2

    db = Database()
    ing = Ingestor(db)
    logger.info("== 开始回填，目标站点：%s ==", ", ".join(sites))
    logger.info("数据库：%s", db.path)

    summary = {}
    for site in sites:
        logger.info("---- 抓取站点：%s ----", site)
        try:
            stats = ing.ingest(site, "all")
            summary[site] = stats
            logger.info("站点 %s 完成：%s", site, stats)
        except Exception as e:  # noqa: BLE001
            logger.error("站点 %s 失败：%s", site, e)
            logger.debug(traceback.format_exc())
            summary[site] = {"error": str(e)}

    if not args.no_pool:
        try:
            pool = ing.rebuild_player_pool()
            logger.info("选手池重建：%s", pool)
            summary["_pool"] = pool
        except Exception as e:  # noqa: BLE001
            logger.error("选手池重建失败：%s", e)
            logger.debug(traceback.format_exc())

    try:
        db.close()
    except Exception:  # noqa: BLE001
        pass

    # 收尾汇总
    logger.info("==== 回填汇总（%s）====", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    for site, st in summary.items():
        if site.startswith("_"):
            continue
        err = st.get("error") if isinstance(st, dict) else None
        if err:
            logger.info("  %s: 失败 -> %s", site, err)
        else:
            logger.info(
                "  %s: raw_pages=%s news=%s trades=%s posts=%s players=%s positions=%s",
                site, st.get("raw_pages"), st.get("news"), st.get("trades"),
                st.get("posts"), st.get("players"), st.get("positions"),
            )
    pool = summary.get("_pool")
    if pool:
        logger.info("  选手池：total=%s derived=%s candidates=%s",
                    pool.get("total"), pool.get("derived"), pool.get("candidates"))
    logger.info("日志已落盘：%s", LOG_PATH)
    return 0


if __name__ == "__main__":
    sys.exit(main())
