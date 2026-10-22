"""本地 Web 服务（仅标准库）：为可视化查看器提供静态页与 JSON API。

运行：python viewer/server.py  （默认 http://127.0.0.1:8765）
- 纯本地、个人使用、无需登录鉴权。
- 复用 viewer/ingest.Ingestor 做登录检测与抓取入库。
"""
from __future__ import annotations

import json
import logging
import math
from logging.handlers import RotatingFileHandler
import os
import re
import socket
import subprocess
import sys
import threading
import sqlite3  # P2-02：ALTER 精确异常判定（OperationalError）
import time
import urllib.parse
import urllib.request
import concurrent.futures as cf
import contextlib
import hmac
import ctypes
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

# 写接口串行化：demo/overseas/fetch 等重写入若并发执行（多线程各自持有 DB 连接），
# 会互相抢 SQLite 写锁并抛 "database is locked"。GET 读请求不受影响（WAL 下读不阻塞）。
_WRITE_LOCK = threading.RLock()
# P1-39：重写入走此空上下文（不持 _WRITE_LOCK），互斥由 INGEST_LOCK + busy_timeout 保证。
_nullcontext = contextlib.nullcontext

# 抓取串行化：手动 /api/fetch 与后台自动调度线程共用 INGEST_LOCK，
# 保证两者不会同时对 SQLite 做写事务（避免 "database is locked" 与数据错乱）。
INGEST_LOCK = threading.RLock()

# 确保能 import 同目录模块与 scraper
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
# 2026-10-07 智能选股增强：screener_extra 在 viewer/ 下，确保可 import（无论 server 被谁拉起）
sys.path.insert(0, str(ROOT / "viewer"))
try:
    import screener_extra as se  # noqa: E402  # 全部第三方依赖已 try/except，沙箱无包也能加载
except Exception:  # noqa: BLE001
    se = None
# 2026-09 行业龙头全景：akshare 已在模块内 try/except，沙箱无包时 available:False（绝不编造）
try:
    import industry_leaders as il  # noqa: E402
except Exception:  # noqa: BLE001
    il = None
# 2026-10-21 行业龙头分级（金/银/铜牌）：纯标准库实现（东财数据中心 + 腾讯行情），无第三方依赖
try:
    import dragon as dragon_mod  # noqa: E402
except Exception:  # noqa: BLE001
    dragon_mod = None


def _setup_logging() -> None:
    """建议10 / P3 日志治理：server 独立启动（不经 desktop_app）也要有 UTF-8 落盘。

    桌面入口 desktop_app 已经配过一套（desktop-YYYYMMDD.log）；这里补 server 这一路，
    免得「直接 python viewer/server.py 起」时全靠 stderr 兜底、中文乱码、翻天就丢。

    产物：viewer/logs/server-YYYYMMDD.log（UTF-8，滚动 5MB × 5，保留 5 天内的滚动文件）。
    幂等：同一个日志文件已有 FileHandler 就不重复挂；也不抢走调用方已装的控制台 handler。
    """
    try:
        log_dir = os.path.join(ROOT, "viewer", "logs")
        os.makedirs(log_dir, exist_ok=True)
        log_path = os.path.join(log_dir, "server-%s.log" % time.strftime("%Y%m%d"))
        log_path_abs = os.path.abspath(log_path)
        root = logging.getLogger()
        if any(isinstance(h, logging.FileHandler) and getattr(h, "baseFilename", "") == log_path_abs
               for h in root.handlers):
            return
        fmt = logging.Formatter("%(asctime)s %(levelname)s: %(message)s", "%Y-%m-%d %H:%M:%S")
        fh = RotatingFileHandler(log_path, encoding="utf-8",
                                 maxBytes=5 * 1024 * 1024, backupCount=5)
        fh.setFormatter(fmt)
        fh.setLevel(logging.INFO)
        root.addHandler(fh)
        if root.level == logging.NOTSET or root.level > logging.INFO:
            root.setLevel(logging.INFO)
    except Exception as e:  # noqa: BLE001 —— 日志挂了不能拖垮启动
        print(f"[server] 日志初始化失败（不影响启动）：{e}", file=sys.stderr)


_setup_logging()

from scraper.config import get_site, load_config  # noqa: E402

from viewer.db import Database  # noqa: E402
from viewer.hotspots import build_hotspots  # noqa: E402
from viewer.ingest import Ingestor  # noqa: E402
from viewer import kb  # noqa: E402  —— 「跟我学习」知识库解析/重吸取
from viewer.stock_names import board_of, enrich, get_resolver  # noqa: E402
from geo import store as geo_store  # noqa: E402  —— 国际局势：全球事件驱动 A 股影响研判
from extref import store as extref_store  # noqa: E402  —— 外部参考：第三方判断信号汇聚与回测（独立隔离模块）
from extref import collect as extref_collect  # noqa: E402  —— 外部参考：公开榜单采集器（东财人气榜）
from extref import backtest as extref_backtest  # noqa: E402  —— 外部参考：日K自动回测回填
import watchlib  # noqa: E402  —— 自选/关注池 + 到价提醒（独立隔离模块，零 JOIN 既有业务表）
# 2026-09-27 增强功能（智能选股/资金流向/估值诊股/研报评级/多资产/资讯）：
# 全部走独立模块 + 通用 emapi 取数基座，零 JOIN 既有业务表，渲染层零改动。
import emapi  # noqa: E402
import moneyflow  # noqa: E402
import board_flow  # noqa: E402  2026-09-28 板块资金流：多源 + 采样累积（替代单一东财依赖）
import board_flow_range  # noqa: E402  2026-10-05 板块资金流「日期区间」聚合（累计流入可指定起止日期）
from viewer import predictor  # noqa: E402  —— 每日预测：六路信号合成 + 复盘自学习闭环（2026-09-28 新增）
import pillars  # noqa: E402  —— 四支柱每日预测引擎 A/B/C/D（2026-10-15 新增；仅后端定时任务跑，查看器只读）
import backtest  # noqa: E402  —— 历史回测（2026-10-15 新增）：预测名单 → T+N 真实走势 + 命中判定，只读 K 线缓存
import resonance  # noqa: E402  —— 博主共振融合引擎（2026-10-15 新增）：三支柱 个股共振/战法体系/信源画像；仅后端批量跑，查看器只读
import selfpick  # noqa: E402  —— 「自我提升」成果出口（2026-10-15）：系统自己按学出的权重+自生长规则选股，并按期回填验证
import evolve  # noqa: E402  —— 自我进化引擎（2026-10-15 新增）：权重自学习 + 规则挖掘 + 衰减预警 + 知识内化；学完反哺共振卡
import quant  # noqa: E402  —— 量化板块（2026-10-16 新增）：第三维信号源（因子全景/多因子选股/因子回测/菜谱预设/量化因子自我学习）；权重学走 metalearn 共享引擎
import etf  # noqa: E402  —— ETF 基金板块（2026-10-17 新增）：今日总览与量化/共振并列的顶层 NAV；左 A=量化多因子 ETF 候选（复用 quant 因子+metalearn 权重），右 B=ETF 专属投顾 6 维（待接入）；左右分离、绝不混叠
import screener  # noqa: E402
import valuation  # noqa: E402
import research  # noqa: E402
import multiquote  # noqa: E402
import newsfeed  # noqa: E402
import research_insight  # noqa: E402 —— 研报→结论聚合：六因子排序 + 一句话结论（2026-10-01）
import watchdesk  # noqa: E402  —— 主动盯盘 / 个股公告自动扫描 + 结构化预判（2026-10-01）
import blogger_perf  # noqa: E402  —— 博主荐股「荐股后 T+1/T+3/T+5/T+10」周期收益（2026-10-01）
import policydesk  # noqa: E402
import global_market  # noqa: E402  —— 全球大盘/海外市场（2026-10-17 新增·「消息与外围」）：美股指数 + SPDR 行业板块 + 国家 ETF 代理 + 港股；国内免费源、免 cookie
from viewer import newsclass  # noqa: E402  —— 新闻/公告分类规则唯一真源（前后端共用一份词表）
import auction  # noqa: E402  —— 集合竞价快照：9:25 定盘后扫全市场一次落库（今日总览第一屏）
import postdigest  # noqa: E402 —— 帖子情绪速览：把最新帖子归纳成「谁看涨谁看跌」的结论（2026-10-02）
import lhb_daily  # noqa: E402 —— 龙虎榜/板块扩散/主线研判 每日自动刷新（补齐 9-30 冻结缺口，2026-10-09）
import blogger_daily  # noqa: E402 —— 博主荐股/参赛股 每日自动刷新：直读源头 Excel，砍掉对原 App 的依赖（2026-10-09）
import blogger_rebuild  # noqa: E402 —— 博主汇总表(stats/overlap)从 reco 真源重算的唯一实现（读取时自愈用，2026-10-09）
import bili_methods  # noqa: E402  —— B站「技术·价值教学」方法卡片聚合（2026-10-11 王总需求）
import bili_scan  # noqa: E402  —— 「一键筛股」：知识库战法→当前全市场买点票（2026-10-11 王总需求）
import bili_consensus  # noqa: E402  —— B站博主共识结论：板块方向+个股热度（2026-10-12 王总：市场情绪面板先给结论）
from viewer import build_id as _buildid  # noqa: E402  —— 代码指纹（2026-10-16）：判断「正在监听的后端」跑的是不是当前磁盘代码

STATIC_DIR = Path(__file__).resolve().parent / "static"
DB = Database()
INGEST = Ingestor(DB)
PORT = int(os.environ.get("VIEWER_PORT", "8765"))


def _parse_dt_loose(s: str):
    """宽松解析时间串（支持 秒/分/日 三种精度），失败返回 None。"""
    s = (s or "").strip()
    if not s:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d", "%Y/%m/%d %H:%M", "%Y/%m/%d"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


def _hold_days(since: str, end: str | None = None) -> int | None:
    """持仓起始日 → 结束日（缺省今日）的天数；用于标注「已持有 N 天」。"""
    d0 = _parse_dt_loose(since)
    if not d0:
        return None
    d1 = _parse_dt_loose(end) if end else datetime.now()
    if not d1:
        d1 = datetime.now()
    return max((d1 - d0).days, 0)


def _truthy(v) -> bool:
    """宽松判定「二次确认」字段是否为真值。

    2026-09-22 新增（P0-2）：破坏性接口（/api/demo、reset 的 /api/overseas）
    必须由调用方显式确认后才执行。接受 true/1/"yes"/"y"/"on"（大小写不敏感），
    其余（含缺失、false、0、""、"no"）一律视为未确认。
    """
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return v != 0
    if isinstance(v, str):
        return v.strip().lower() in ("1", "true", "yes", "y", "on")
    return False


# 破坏性接口未获确认时的统一 409 载荷（P0-2）
_DESTRUCTIVE_HINT = "该操作会清空全部业务数据（含海外新闻、热点、持仓、帖子），需要二次确认"


def _destructive_payload() -> dict:
    return {"error": "destructive_operation", "hint": _DESTRUCTIVE_HINT, "require_confirm": True}


def _backup_failed_payload(detail: str = "") -> dict:
    """前置快照失败时的统一错误载荷（P0-2 第 4 点，2026-09-22）。

    破坏性操作需要「可回滚」才允许执行：快照失败即**中止，未改动任何数据**。
    这里刻意与「执行失败」分开一个 error code（backup_failed），让前端能说清是
    「没执行」而不是「执行到一半失败」。
    """
    p = {"ok": False, "error": "backup_failed",
         "hint": "已中止：备份失败，未改动任何数据"}
    if detail:
        p["detail"] = detail[:200]
    return p


def _named(symbol: str, fallback: str = "") -> dict:
    """把代码翻译成「简称 / 公司全称 / 板块」。库内已有名称优先，其次走解析器。"""
    info = get_resolver(db=DB).resolve(symbol, allow_online=False)
    name = info.get("name") or fallback or ""
    return {
        "symbol": symbol,
        "name": name,
        "company": info.get("company") or "",
        "market": info.get("market") or board_of(symbol),
        "name_source": info.get("source") or "none",
        "name_missing": not bool(name),
    }


def _sites() -> list[str]:
    try:
        d = load_config()
        return list(d.get("sites", {}).keys())
    except Exception:
        return ["taoguba"]


def _ingest_item_count(res) -> int:
    """汇总 Ingestor.ingest 的 stats 里的「本次处理条目数」。

    stats 形如 {"site":"taoguba","raw_pages":3,"news":0,"trades":12,
    "posts":8,"players":0,"positions":5}；把除 site 外的整数字段相加即可。
    仅作刷新状态里的参考计数，不参与任何业务判定，故取不到就回 0。
    """
    if not isinstance(res, dict):
        return 0
    return sum(v for k, v in res.items()
               if k != "site" and isinstance(v, int) and not isinstance(v, bool))


# ---------------- 刷新状态（Request G：最后更新列 / 实时刷新记录） ----------------
# 数据来源中文显示名（「最后更新」列的 tooltip 与刷新状态标识用）。
SITE_DISPLAY_NAMES = {
    "taoguba": "淘股吧", "eastmoney": "东方财富", "xueqiu": "雪球", "ths": "同花顺",
    "sina": "新浪股吧", "__all__": "全部来源",
}
# 自动刷新周期（秒）。前端默认每 5 分钟触发一次抓取；后端把该值回传，
# 使「倒计时」与「超时判定」共用同一口径，避免前后端各写一份而漂移。
REFRESH_INTERVAL_SECONDS = int(os.environ.get("VIEWER_REFRESH_INTERVAL", "300"))
# 「未按时更新」阈值（秒）：默认 2 倍周期（10 分钟）——一次漏刷不算超时，
# 连续两次未成功更新即视为该来源的数据已不新鲜。
# 2026-10-05（报告 P1 / 建议3）：超时阈值由刷新周期**派生**而不是写死 600。
# 这样一旦 VIEWER_REFRESH_INTERVAL 改小/改大，顶栏的「已超时」判定会跟着走，
# 不会再出现「调度明明在跑、顶栏却报超时」的口径漂移。
REFRESH_STALE_SECONDS = int(os.environ.get("VIEWER_STALE_SECONDS",
                                           str(REFRESH_INTERVAL_SECONDS * 2)))


def _kb_sources() -> list[dict]:
    """已启用的知识库来源配置（config/kb_sources.yaml）。配置缺失/损坏时退回空表。"""
    try:
        return kb.load_config()
    except Exception:  # noqa: BLE001 —— 缺 PyYAML / yaml 写错都不该让整个接口挂掉
        logging.exception("读取 kb_sources.yaml 失败")
        return []


def _kb_keys() -> list[str]:
    return [(c.get("doc_key") or "kb") for c in _kb_sources()]


def api_refresh_status() -> dict:
    """刷新状态汇总（Request G）。

    - last_ok_at：最近一次**整轮抓取**成功完成的时刻（= source_key「__all__」）
    - sources：逐来源的最近成功/尝试时刻与状态，供「最后更新」列逐行取用
    - tracked_sources：当前**已纳入刷新**的来源 = 抓取配置里的站点 + 知识库来源。
      未列入的来源不参与时效判定（前端显示「未纳入刷新」），避免误报超时。
    - interval_seconds / stale_seconds：与前端共享的刷新周期与超时阈值
    """
    rows = DB.get_refresh_status()
    by_key = {r["source_key"]: r for r in rows}
    allrow = by_key.get("__all__") or {}
    sources = []
    for key, r in by_key.items():
        if key == "__all__":
            continue
        sources.append({
            "source_key": key,
            "display_name": r.get("display_name") or SITE_DISPLAY_NAMES.get(key, key),
            "last_ok_at": r.get("last_ok_at"),
            "last_attempt_at": r.get("last_attempt_at"),
            "status": r.get("status") or "never",
            "item_count": r.get("item_count") or 0,
            "error": r.get("error") or "",
        })
    return {
        "last_ok_at": allrow.get("last_ok_at"),
        "last_attempt_at": allrow.get("last_attempt_at"),
        "status": allrow.get("status") or "never",
        "error": allrow.get("error") or "",
        "item_count": allrow.get("item_count") or 0,
        "sources": sources,
        "tracked_sources": _sites() + _kb_keys(),
        "interval_seconds": REFRESH_INTERVAL_SECONDS,
        "stale_seconds": REFRESH_STALE_SECONDS,
        # 2026-10-05（建议3）：顶栏拿「调度上次跑完时刻」与「整轮最后成功时刻」一起判新鲜度，
        # 避免调度在跑、__all__ 却没回写时把用户吓一跳（旧版就误报过「已超时 36 分钟」）。
        "scheduler_last_run_at": _sched_load_state().get("last_run_at"),
        "scheduler_enabled": bool(_sched_load_state().get("enabled", True)),
        "stale_basis": f"{REFRESH_STALE_SECONDS}s（= 刷新周期 {REFRESH_INTERVAL_SECONDS}s × 2）",
        # 2026-10-09：嵌入行情新鲜度（前端 loadRefreshStatus 已读 refresh_status.market）。
        # 仅在旧字段之外**新增**一个 key，向后兼容；用 api_market_freshness 的 2s 原子缓存，
        # 常态下几乎零成本、不拖慢本接口。取不到时 market.ok=False 并带 reason，绝不造假。
        "market": api_market_freshness().get("market"),
    }


def api_market_freshness() -> dict:
    """行情新鲜度：现抓基准标的（上证指数 sh000001）的**实时行情时间戳**，供顶栏如实显示。

    返回 {"market": {...}}：
      src          实际采纳的源（sina / tencent / eastmoney），谁先回用谁；
      symbol/name  基准标的（默认上证指数）；
      price        基准现值；
      quote_time   行情源自带的成交时间（秒级 YYYY-MM-DD HH:MM:SS）；
      server_now   服务端此刻时间；
      lag_seconds  server_now - quote_time（秒）。盘中正常 = 数秒~数十秒；
                   收盘后 = 自收盘到现在的时长（同时 is_trading=False，前端据此区分「延迟」与「休市」）；
      is_trading   当前是否盘中（工作日 + 9:30–11:30 / 13:00–15:00 + 权威交易日历确认）；
      ok           是否成功取到行情时间戳；失败时附 reason。

    设计：三源并发竞速（新浪 / 腾讯 / 东财），整体硬上限 3s；**一律现抓**，绝不用库里旧快照冒充；
    行情原子仅缓存 5s，且 lag/server_now/is_trading **每次现算**，不受缓存影响。
    """
    now_ts = time.time()
    atom = _MARKET_FRESH_CACHE.get("data")
    if atom is None or now_ts - float(_MARKET_FRESH_CACHE.get("ts") or 0.0) > _MARKET_FRESH_TTL:
        atom = emapi.race_first(
            [lambda: _bench_tencent(), lambda: _bench_sina(), lambda: _bench_em()],
            timeout=3.0)
        _MARKET_FRESH_CACHE["data"] = atom
        _MARKET_FRESH_CACHE["ts"] = now_ts
    market = {"symbol": _MARKET_BENCHMARK, "name": None, "price": None, "src": None,
              "quote_time": None, "server_now": time.strftime("%Y-%m-%d %H:%M:%S"),
              "lag_seconds": None, "is_trading": _is_trading_now(), "ok": False}
    if not atom or not atom.get("quote_time"):
        market["reason"] = "行情时间戳获取失败：新浪 / 腾讯 / 东财三源均不可达（本机网络或代理异常）"
        return {"market": market}
    qt = atom["quote_time"]
    try:
        lag = int(time.time() - time.mktime(time.strptime(qt, "%Y-%m-%d %H:%M:%S")))
        if lag < -60:          # 源时间快于本机时钟（时钟偏差）→ 不夸大延迟，按 0 计
            lag = 0
    except Exception:  # noqa: BLE001
        lag = None
    market.update({"name": atom.get("name"), "price": atom.get("price"), "src": atom.get("src"),
                   "quote_time": qt, "lag_seconds": lag, "ok": True})
    return {"market": market}


# ---------------- 学习知识库（「跟我学习 → 中短线炒股」） ----------------
KB_DEFAULT_KEY = "zdx_stock"
# 知识库刷新周期：上游每天导出一次，故默认 24 小时；超时阈值取 2 倍（48 小时）。
# 与行情数据（5 分钟）分开计时——学习内容本来就不该按分钟判新鲜度。
KB_INTERVAL_SECONDS = int(os.environ.get("VIEWER_KB_INTERVAL", str(24 * 3600)))
KB_STALE_SECONDS = int(os.environ.get("VIEWER_KB_STALE", str(48 * 3600)))


def api_kb_list() -> dict:
    """知识库索引：已吸取的文档 + 配置里声明但尚未吸取的来源，一并列出。

    `group` 与 `order`（左侧一级分类归属 + 横条内的排序）**只从 yaml 读、不落库**——
    它们属于展示配置而非内容，从配置读的好处是改一行 yaml 刷新页面即生效，
    不必为了挪个位置去重建整份知识库。

    2026-09-26：原 `learn`（跟我学习）与 `acquire`（知识获取）两个一级分类已合并为一个
    （前端 NAV 的 key 仍沿用 learn），故 group 现缺省一律为 learn；`order` 数值小的排前面
    （缺省 0，同值时保持库内既有顺序），用来固定「基础常识」等轨道的横条位置。
    """
    rows = DB.kb_list()
    cfgs = {c.get("doc_key") or "kb": c for c in _kb_sources()}

    def group_of(cfg: dict) -> str:
        return (str(cfg.get("group") or "").strip() or "learn")

    def order_of(cfg: dict) -> int:
        """轨道在横向子板块里的排序权重。写非数字/不写一律按 0 处理（而非报错整表挂掉）。"""
        try:
            return int(cfg.get("order") or 0)
        except (TypeError, ValueError):
            return 0

    for r in rows:
        cfg = cfgs.get(r["doc_key"]) or {}
        r["track"] = r.get("track") or cfg.get("track") or ""
        r["group"] = group_of(cfg)
        r["order"] = order_of(cfg)
        r["configured"] = bool(cfg)
        r["source_exists"] = bool(kb.resolve_source(cfg)) if cfg else False
        # 内容是否相对上次发生变化（kb_save 写入）：前端「有更新」角标用
        r["changed"] = bool(r.get("changed"))
    have = {r["doc_key"] for r in rows}
    for key, cfg in cfgs.items():
        if key not in have:
            rows.append({
                "doc_key": key, "track": cfg.get("track") or "",
                "group": group_of(cfg), "order": order_of(cfg),
                "title": cfg.get("title") or key, "version": "", "status": "never",
                "node_count": 0, "ingested_at": "", "source_path": kb.resolve_source(cfg) or "",
                "configured": True,
                "source_exists": bool(kb.resolve_source(cfg)), "changed": False,
            })
    return {"docs": rows, "default_key": KB_DEFAULT_KEY,
            "interval_seconds": KB_INTERVAL_SECONDS,
            "stale_seconds": KB_STALE_SECONDS}


def api_kb_doc(doc_key: str | None = None, light: bool = False) -> dict:
    """取一份知识库文档（**扁平结构**，前端不必在嵌套 JSON 里找键）。

    light=True 只回 meta + outline（几 KB），供汇总卡片/导航预检；
    默认回完整正文（约 250 KB），含 8 流派 / 50 博主 / 33 条回测明细。
    """
    d = DB.kb_get(doc_key or None)
    if not d:
        cfg = next((c for c in _kb_sources()
                    if (c.get("doc_key") or "kb") == (doc_key or KB_DEFAULT_KEY)), None)
        return {
            "doc_key": doc_key or KB_DEFAULT_KEY, "status": "empty",
            "track": (cfg or {}).get("track") or "", "title": (cfg or {}).get("title") or "",
            "version": "", "node_count": 0, "char_count": 0, "ingested_at": "",
            "source_path": kb.resolve_source(cfg) if cfg else "",
            "parser_version": 0, "parser_current": kb.PARSER_VERSION, "parser_stale": False,
            "meta": {}, "outline": [],
            "message": "尚未吸取知识库。点「立即吸取」从来源文件重建，"
                       "或到「学习轨道管理」检查来源路径。",
        }
    env = d.get("envelope") or {}
    body = d.get("doc") or {}
    out = {
        "doc_key": d.get("doc_key"),
        "track": d.get("track") or env.get("track") or "",
        "title": env.get("title") or d.get("title") or "",
        "version": d.get("version") or env.get("version") or "",
        "source_path": d.get("source_path") or env.get("source_path") or "",
        "source_mtime": d.get("source_mtime") or env.get("source_mtime") or "",
        "content_hash": d.get("content_hash") or "",
        "char_count": d.get("char_count") or 0,
        "node_count": d.get("node_count") or 0,
        "status": d.get("status") or env.get("status") or "ok",
        "error": d.get("error") or env.get("error") or "",
        "ingested_at": d.get("ingested_at") or "",
        "prev_hash": d.get("prev_hash") or "",
        "prev_ingested_at": d.get("prev_ingested_at") or "",
        "changed": bool(d.get("changed")),
        "reload_count": d.get("reload_count") or 0,
        # 解析器版本：库中存值与当前代码值不等 → 这份正文是旧解析逻辑的产物，
        # 点一次「立即吸取」即可按新逻辑重建（上游文件没变也照样重建）。
        "parser_version": int(d.get("parser_version") or env.get("parser_version") or 0),
        "parser_current": kb.PARSER_VERSION,
        "parser_stale": int(d.get("parser_version") or env.get("parser_version") or 0)
                        != kb.PARSER_VERSION,
        "meta": body.get("meta") or {},
        "outline": body.get("outline") or [],
    }
    if d.get("payload_error"):
        out["payload_error"] = d["payload_error"]
    if not light:
        # 正文各区块**按 payload 原样透传**。
        # 2026-09-26 修：原先是白名单式逐个列举（schools/conclusions/compare/pitfalls/
        # backtests/risks/sources），新增文档类型时会静默丢字段——例如词条型知识库
        # （kind="glossary"）的 groups / kind 都不在白名单里，结果是"数据已吸进库、
        # 接口也回 200、但页面一片空白"，排查时极难定位。改为除 meta/outline 外全透传。
        for _k, _v in body.items():
            if _k in ("meta", "outline"):
                continue
            out.setdefault(_k, _v)
    return out



def kb_reload(doc_key: str | None = None, force: bool = False) -> dict:
    """重新吸取知识库（幂等）。

    内容哈希未变则 `kb_save` 直接跳过、不写库、不动 ingested_at。
    无论成功/跳过/失败，都往 `refresh_status` 记一笔（source_key=doc_key），
    使学习板块的「最后更新 / 超时告警」复用与行情数据完全相同的机制。
    """
    cfgs = _kb_sources()
    if doc_key:
        cfgs = [c for c in cfgs if (c.get("doc_key") or "kb") == doc_key]
    if not cfgs:
        return {"results": [], "error": "未配置可用的知识库来源（检查 config/kb_sources.yaml）",
                "summary": api_kb_list()}
    results = []
    for cfg in cfgs:
        key = cfg.get("doc_key") or "kb"
        label = f"{cfg.get('track') or ''}知识库".strip() or key
        try:
            payload = kb.build_payload(cfg)
            if payload.get("status") != "ok":
                err = payload.get("error") or "吸取失败"
                DB.record_refresh(key, "error", 0, label, error=err)
                results.append({"doc_key": key, "status": "error", "error": err})
                continue
            r = DB.kb_save(payload, force=force)
            DB.record_refresh(key, "ok", int(payload.get("node_count") or 0), label)
            results.append({**r, "status": "ok", "version": payload.get("version"),
                            "node_count": payload.get("node_count"),
                            "source_mtime": payload.get("source_mtime")})
        except Exception as e:  # noqa: BLE001 —— 单库失败不影响其它库
            err = str(e)[:200]
            DB.record_refresh(key, "error", 0, label, error=err)
            results.append({"doc_key": key, "status": "error", "error": err})
    return {"results": results, "summary": api_kb_list()}



# ---------------- API 实现 ----------------
def _site_login_meta(site_key: str) -> dict:
    """从站点配置取登录相关的展示信息（登录网址 / 中文名 / 是否需要登录）。

    供「登录态检测」渲染可点击的登录入口用。配置缺失时回退到内置默认值，
    保证前端始终能拿到一个可点的网址，而不是空的。
    """
    display = SITE_DISPLAY_NAMES.get(site_key, site_key)
    url, requires = "", False
    try:
        data = load_config()
        cfg = get_site(data, site_key)
        requires = bool(cfg.get("requires_login"))
        # 登录网址优先级：login_url > register_url > base_url
        url = (cfg.get("login") or {}).get("url") or cfg.get("login_url") \
            or cfg.get("register_url") or cfg.get("base_url") or ""
    except Exception:
        pass
    # 兜底：淘股吧已迁移到 tgb.cn
    if not url and site_key == "taoguba":
        url = "https://www.tgb.cn"
    return {"display_name": display, "login_url": url, "requires_login": requires}


def _jl(v, dflt=None):
    """安全解析 JSON 字符串字段（落库时为 TEXT）。"""
    fallback = dflt if dflt is not None else []
    if not v:
        return fallback
    try:
        return json.loads(v)
    except Exception:  # noqa: BLE001
        return fallback


_api_ver_cache = {"mtime": -1.0, "version": None}



def api_version() -> dict:
    """前端版本号（唯一真源 = index.html 的 <meta name="app-version" content="...">）。

    2026-10-02 加：王总反馈「集合竞价功能做了但桌面窗口里看不到」。根因是**已在运行的页面不会重新加载自己**——
    页面里的 JS 只定时拉 /api/dashboard 刷数据，不会重新拉 index.html / app.js，所以昨晚开的窗口
    永远跑着昨晚那版代码，接口把新数据给过去它也渲染不出来。
    对策：前端每 60s 比一次版本，不一致就在顶部弹提示条让用户点一下刷新（比让他猜「要按 F5」可靠）。
    """
    # P3-19：index.html 的 app-version 用 st_mtime 缓存，避免 60s 轮询每次 read_text+regex。
    # 文件变动（F5 部署新前端）后 mtime 变化才重新解析。
    try:
        p = STATIC_DIR / "index.html"
        mt = p.stat().st_mtime
        if _api_ver_cache["mtime"] != mt:
            html = p.read_text(encoding="utf-8")
            m = re.search(r'<meta\s+name=["\']app-version["\']\s+content=["\']([^"\']+)["\']>', html)
            _api_ver_cache["mtime"] = mt
            _api_ver_cache["version"] = m.group(1) if m else None
    except (OSError, UnicodeDecodeError):
        pass
    # 2026-10-05（优化报告 建议1）：带上服务进程 pid。桌面端起新实例前可以用它
    # 判断「是不是撞上已经在跑的那一台」，避免再开一个窗口出来（1 server + N desktop）。
    return {"version": _api_ver_cache["version"], "served_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "pid": os.getpid(),
            # 2026-10-16：进程启动时定格的代码指纹 + 启动时刻。供 desktop_app 判断
            # 「8765 上跑的是不是当前磁盘上的代码」——是旧代码就必须接管重启，
            # 否则新加的路由在这个进程里根本不存在（表现为 404，界面看着还是新版）。
            "build_id": _BUILD_ID,
            "started_at": _STARTED_AT,
            # 2026-10-15：当前正在跑的调度任务名。用于现场诊断"整站被冻住"——
            # 若某个纯 Python 任务长时间占住 GIL，这个字段会一直停在该任务名上。
            "sched_task": globals().get("_SCHED_CURRENT", "-")}


# ── 2026-10-14 性能修复：重接口结果内存缓存 ──
# watch_list / dashboard 这类要重算十几秒的接口，结果缓存 300s，命中即返回 <1s；
# 任一会改变关注池 / 分组的写入成功后，由 _do_POST 统一清掉对应前缀的缓存（见 _do_POST 末尾）。
# 单线程 GIL 下 dict 操作本就原子，仍加锁以杜绝并发 put / invalidate 的竞态。
_api_cache: dict = {}
_api_cache_lock = threading.Lock()


def _api_cache_get(key: str):
    e = _api_cache.get(key)
    if e and (time.time() - e[0]) < e[2]:
        return e[1]
    return None


def _api_cache_put(key: str, val, ttl: int = 300):
    with _api_cache_lock:
        _api_cache[key] = (time.time(), val, ttl)


def _api_cache_invalidate(prefix: str = None):
    with _api_cache_lock:
        if prefix is None:
            _api_cache.clear()
        else:
            for k in [k for k in _api_cache if k.startswith(prefix)]:
                _api_cache.pop(k, None)


def api_dashboard(force=False) -> dict:
    """今日看板：一屏给出「今天什么值得看」——主线研判（方向/龙头/梯队/分支） → 共识 → 数据新鲜度。
    组织维度是「判断」而非「数据来源」，作为产品的决策入口（首页）。"""
    if not force:
        cached = _api_cache_get("dashboard")
        if cached is not None:
            return cached
    out: dict = {"generated_at": time.strftime("%Y-%m-%d %H:%M:%S")}

    # ---- 1) 龙虎榜：最新交易日 ----
    row = DB.conn.execute("SELECT MAX(trade_date) AS d FROM lhb_stocks").fetchone()
    lhb_date = row["d"] if row else None
    lhb: dict = {"date": lhb_date, "stock_count": 0, "net_total": 0.0,
                 "top_stocks": [], "inst_top": [], "dept_top": []}
    if lhb_date:
        merged: dict = {}
        for r in DB.conn.execute(
                "SELECT security_code, security_name, change_rate, net_amt, reason "
                "FROM lhb_stocks WHERE trade_date=?", (lhb_date,)).fetchall():
            k = r["security_code"]
            m = merged.get(k)
            if m is None:
                merged[k] = {"symbol": k, "name": r["security_name"],
                             "change_rate": _num(r["change_rate"]),
                             "net_amt": _num(r["net_amt"]) or 0.0,
                             "reasons": [r["reason"]] if r["reason"] else []}
            else:
                m["net_amt"] += _num(r["net_amt"]) or 0.0
                if r["reason"] and r["reason"] not in m["reasons"]:
                    m["reasons"].append(r["reason"])
        rows = sorted(merged.values(), key=lambda x: -x["net_amt"])
        lhb["stock_count"] = len(rows)
        lhb["net_total"] = round(sum(x["net_amt"] for x in rows), 2)
        lhb["top_stocks"] = rows[:8]
        lhb["inst_top"] = [
            {"symbol": r["security_code"], "name": r["security_name"],
             "net": _num(r["net_buy_amt"]), "buy_times": r["buy_times"],
             "sell_times": r["sell_times"], "ratio": _num(r["ratio"])}
            for r in DB.conn.execute(
                "SELECT security_code, security_name, net_buy_amt, buy_times, sell_times, ratio "
                "FROM lhb_inst WHERE trade_date=? AND net_buy_amt>0 "
                "ORDER BY net_buy_amt DESC LIMIT 6", (lhb_date,)).fetchall()]
        lhb["dept_top"] = [
            {"name": (r["org_name"] or r["operate_name"]), "net": _num(r["net"]),
             "stock_count": r["stk"], "appear": r["n"]}
            for r in DB.conn.execute(
                "SELECT operate_name, org_name, SUM(net_amt) AS net, "
                "COUNT(DISTINCT security_code) AS stk, COUNT(*) AS n "
                "FROM lhb_depts WHERE trade_date=? GROUP BY operate_name "
                "ORDER BY net DESC LIMIT 6", (lhb_date,)).fetchall()]
    out["lhb"] = lhb

    # ---- 2) 板块动向：最新日的多点共振 ----
    srow = DB.conn.execute("SELECT MAX(trade_date) AS d FROM lhb_sector_focus").fetchone()
    sdate = srow["d"] if srow else None
    sectors = []
    if sdate:
        for r in DB.conn.execute(
                "SELECT sector, net_amt, onlist_n, t1_avg, t3_avg, t1_win, bench_t1, bench_t3 "
                "FROM lhb_sector_focus WHERE trade_date=? "
                "ORDER BY net_amt DESC LIMIT 6", (sdate,)).fetchall():
            t1 = _num(r["t1_avg"])
            b1 = _num(r["bench_t1"])
            sectors.append({"sector": r["sector"], "net": _num(r["net_amt"]),
                            "onlist_n": r["onlist_n"], "t1_avg": t1, "t3_avg": _num(r["t3_avg"]),
                            "t1_win": _num(r["t1_win"]), "bench_t1": b1,
                            "excess_t1": (round(t1 - b1, 2) if t1 is not None and b1 is not None else None)})
    out["sectors"] = {"date": sdate, "rows": sectors}

    # ---- 2.5) 主线研判：方向 → 龙头 → 梯队 → 分支（决策口径，不做个股罗列） ----
    # 打分口径（可解释，全部来自真实数据）：
    #   资金强度 40（该方向净买 / 当日最强方向净买）
    # + 龙头强度 30（龙头当日涨幅：涨停 30 / ≥5% 20 / 其余 10）
    # + 共振广度 20（该方向上榜个股数 × 1.2，上限 20）
    # + 分支延伸 10（关联概念数 × 2，上限 10）
    # 评级：≥75 重点方向 / ≥55 跟进关注 / 其余 仅观察。
    # 「可推进」= 梯队中尚未涨停（<9.8%）的同方向个股——龙头已封板，能动的在这批里。
    mainlines: list[dict] = []
    cdate = None
    try:
        crow = DB.conn.execute("SELECT MAX(trade_date) AS d FROM sector_chain").fetchone()
        cdate = crow["d"] if crow else None
        if cdate:
            raw = DB.conn.execute(
                "SELECT sector, net_amt, onlist_n, leader_code, leader_name, leader_net, "
                "leader_chg, leader_pct_now, onlist_json, tiers_json, branches_json, "
                "t1_avg, t3_avg FROM sector_chain WHERE trade_date=? "
                "ORDER BY net_amt DESC", (cdate,)).fetchall()
            top_net = max((_num(r["net_amt"]) or 0.0) for r in raw) if raw else 0.0
            for r in raw:
                net = _num(r["net_amt"]) or 0.0
                lead_pct = _num(r["leader_pct_now"])
                n_on = r["onlist_n"] or 0
                onlist = _jl(r["onlist_json"])
                tiers = _jl(r["tiers_json"])
                branches = _jl(r["branches_json"])
                s_cap = 40.0 * (net / top_net) if top_net else 0.0
                lp = lead_pct or 0.0
                s_lead = 30.0 if lp >= 9.8 else (20.0 if lp >= 5.0 else 10.0)
                s_res = min(20.0, n_on * 1.2)
                s_br = min(10.0, len(branches) * 2.0)
                score = round(s_cap + s_lead + s_res + s_br, 1)
                if score >= 75:
                    rating, level = "重点方向", "hot"
                elif score >= 55:
                    rating, level = "跟进关注", "warm"
                else:
                    rating, level = "仅观察", "cool"
                forward = [t for t in tiers if (_num(t.get("pct")) or 0) < 9.8][:6]
                why = ("资金居前" if s_cap >= 30 else "资金中游") \
                    + "·" + ("龙头封板" if lp >= 9.8 else ("龙头走强" if lp >= 5 else "龙头偏弱")) \
                    + "·" + ("多点共振(%d家上榜)" % n_on if n_on >= 3 else "个股孤立") \
                    + ("·分支多(%d)" % len(branches) if len(branches) >= 4 else "")
                mainlines.append({
                    "sector": r["sector"], "net": net, "net_yi": round(net / 1e8, 2),
                    "onlist_n": n_on, "onlist": onlist[:10],
                    "leader_code": r["leader_code"], "leader_name": r["leader_name"],
                    "leader_net_yi": round((_num(r["leader_net"]) or 0.0) / 1e8, 2),
                    "leader_chg": _num(r["leader_chg"]), "leader_pct_now": lead_pct,
                    "tiers": tiers[:10], "forward": forward,
                    "branches": branches[:8],
                    "t1_avg": _num(r["t1_avg"]), "t3_avg": _num(r["t3_avg"]),
                    "score": score, "rating": rating, "level": level, "why": why,
                    "cap_pct": round(s_cap / 40.0 * 100, 0) if top_net else 0,
                })
    except Exception as e:  # noqa: BLE001 —— 看板不应因单个模块失败而整体不可用
        logging.debug("主线研判构建失败：%s", e)
    out["mainlines"] = {"date": cdate, "rows": mainlines,
                        "focus": [m for m in mainlines if m["level"] in ("hot", "warm")][:3]}

    # ---- 3) 共识：牛散同持 ----
    try:
        ni = api_niusan(min_hold=2, scope="free")
        sm = ni.get("summary") or {}
        out["consensus"] = {"star_count": sm.get("investor_total"),
                            "person_total": sm.get("person_total"),
                            "latest_date": sm.get("latest_date"),
                            "rows": (ni.get("cohold") or [])[:6]}
    except Exception:  # noqa: BLE001 —— 看板不应因单个模块失败而整体不可用
        out["consensus"] = {"star_count": None, "rows": []}

    # ---- 4) 数据新鲜度（一眼看出哪条数据陈旧） ----
    # 口径说明：持仓（当前持仓）源站不提供建仓时间，故此处不展示"最新持仓日"
    # （否则会拿少数带日期的社区帖冒充整体，造成误导）；改展示条数 + 最后抓取时间。
    fresh = []
    for label, sql in (
        ("龙虎榜", "SELECT MAX(trade_date) FROM lhb_stocks"),
        ("实盘交易", "SELECT MAX(post_time) FROM trades"),
        ("讨论帖", "SELECT MAX(post_time) FROM posts"),
        ("股东(季报)", "SELECT MAX(end_date) FROM stock_holders"),
        ("最后抓取", "SELECT MAX(fetched_at) FROM raw_pages"),
    ):
        try:
            v = DB.conn.execute(sql).fetchone()[0]
        except Exception:  # noqa: BLE001
            v = None
        fresh.append({"label": label, "latest": str(v)[:19] if v else "—"})
    try:
        fresh.append({"label": "持仓条数", "latest": str(
            DB.conn.execute("SELECT COUNT(*) FROM positions").fetchone()[0]) + " 条"})
    except Exception:  # noqa: BLE001
        pass
    out["freshness"] = fresh
    # 2026-10-01 集合竞价（方案 A）：今日总览第一屏 —— 开盘前先看「今天一开盘市场什么情绪」，
    # 开盘后再看当天竞价结果。随 dashboard 一起下发，前端不额外发请求（dashboard 本来就必拉）。
    try:
        import auction as _auc
        out["auction"] = _auc.summary()
    except Exception as e:  # noqa: BLE001 —— 这一块挂了不能拖垮整个看板
        out["auction"] = {"status": "error", "date": None, "counts": {}, "top_pct": [],
                          "top_amount": [], "top_ratio": [], "history": [],
                          "note": "竞价板块出错：%s" % str(e)[:120],
                          "generated_at": time.strftime("%Y-%m-%d %H:%M:%S")}
    _api_cache_put("dashboard", out, 300)
    return out


# ---------------------------------------------------------------- 今日决策（2026-10-12 体验审计 P0-A）
# 报告《查看器体验审计与改造方案》的第 0 步：把「今日看板」里已有的**一句话结论**写法推广成
# 全站首页 —— 一屏只答四件事：① 今天该盯哪个方向 ② 哪些票能上车（机会）
# ③ 哪里在退潮/要躲（坑） ④ 我自己的关注池今天有什么动静。
# 数据**全部复用既有口径**，不新造引擎（这是报告明确要求的前提）：
#   方向 = sector_chain 主线研判（与「今日看板」同源、同打分口径）
#   机会 = 主线的「可推进」名单（梯队里尚未涨停的同方向票）
#   风险 = 板块资金净流出（lhb_sector_focus）+ 近 24h 帖子情绪里被看空/分歧的个股（postdigest）
#   我的票 = watch_items / watch_alerts（到价提醒，未读优先）
# 诚实口径：市场级方向与资金来自真实数据；情绪来自帖子正文统计；到价提醒来自本地池。
#           所有判词都是方向参考，不构成买卖建议。
def api_today_decision() -> dict:
    gen = time.strftime("%Y-%m-%d %H:%M:%S")
    log = logging.getLogger(__name__)
    out: dict = {"ok": True, "generated_at": gen,
                 "directions": [], "opportunities": [], "risks": [],
                 "mywatch": {"total": 0, "unread": 0, "alerts": [], "near": []},
                 "data_date": None, "verdict": "", "verdict_tone": "mixed", "basis": ""}

    # ---- 1) 方向：主线研判（与 api_dashboard 同一套打分口径，避免两处口径漂移） ----
    cdate = None
    mrows: list = []
    try:
        crow = DB.conn.execute("SELECT MAX(trade_date) AS d FROM sector_chain").fetchone()
        cdate = crow["d"] if crow else None
        if cdate:
            mrows = DB.conn.execute(
                "SELECT sector, net_amt, onlist_n, leader_code, leader_name, leader_pct_now, "
                "tiers_json, branches_json, t1_avg FROM sector_chain WHERE trade_date=? "
                "ORDER BY net_amt DESC", (cdate,)).fetchall()
    except Exception as e:  # noqa: BLE001 —— 单块失败不拖垮整个首页
        log.warning("today_decision 主线读取失败: %s", e)
    out["data_date"] = cdate

    top_net = max((_num(r["net_amt"]) or 0.0) for r in mrows) if mrows else 0.0
    scored: list = []
    for r in mrows:
        net = _num(r["net_amt"]) or 0.0
        lp = _num(r["leader_pct_now"]) or 0.0
        n_on = r["onlist_n"] or 0
        score = (40.0 * (net / top_net) if top_net else 0.0) \
            + (30.0 if lp >= 9.8 else (20.0 if lp >= 5.0 else 10.0)) \
            + min(20.0, n_on * 1.2) + min(10.0, len(_jl(r["branches_json"])) * 2.0)
        if lp >= 9.8:
            stage, level = "加速（龙头封板）", "hot"
        elif lp >= 5.0:
            stage, level = "启动（龙头走强）", "warm"
        else:
            stage, level = "观察（龙头偏弱）", "cool"
        scored.append({"row": r, "net": net, "lp": lp, "n_on": n_on,
                       "score": round(score, 1), "stage": stage, "level": level})
    scored.sort(key=lambda x: -x["score"])

    for it in scored[:3]:
        r = it["row"]
        out["directions"].append({
            "sector": r["sector"], "net_yi": round(it["net"] / 1e8, 2),
            "onlist_n": it["n_on"], "leader_code": r["leader_code"],
            "leader_name": r["leader_name"], "leader_pct": it["lp"],
            "stage": it["stage"], "level": it["level"], "score": it["score"],
            "t1_avg": _num(r["t1_avg"]),
        })

    # ---- 1.5) ETF 融合信号回灌（A 栏日 K + B 栏投顾数据 → 四卡化学反应） ----
    try:
        etf_ov = etf.etf_decision_overlay()
        cmap = (etf_ov.get("confirm_map") or {})
        for d in out["directions"]:
            cm = cmap.get(d["sector"])
            if cm:
                d["etf_confirm"] = cm  # 方向卡挂「ETF 资金确认」等级
        cb = (etf_ov.get("cross_border") or {})
        if cb.get("risk"):  # 跨境 ETF 风险传导 → 风险卡置顶（最高否决权）
            for it in cb.get("items", []):
                if it.get("flag"):
                    out["risks"].insert(0, {
                        "type": "etf", "label": it.get("name") or it.get("code"),
                        "symbol": it.get("code"), "tone": "cross",
                        "detail": "跨境ETF %s（pct=%s%%，溢价=%s%%）"
                                  % (it.get("flag"), it.get("pct"), it.get("premium_rt")),
                    })
        out["etf_regime"] = (etf_ov.get("broad_based") or {}).get("regime")  # 宽基仓位 overlay
        out["etf_overlay"] = etf_ov
    except Exception as e:  # noqa: BLE001
        log.warning("today_decision ETF 融合失败: %s", e)

    # ---- 1.6) 博主共振 + 信息面并入（不再半融合：与四支柱/量化/ETF 同框） ----
    try:
        rp = resonance.payload(DB.conn)
        rsec = {}
        for c in (rp.get("cards") or []):
            s = c.get("sector") or ""
            if s:
                rsec[s] = rsec.get(s, 0) + 1
        out["resonance"] = {
            "market_state": (rp.get("market_state") or {}).get("state"),
            "top_sectors": sorted(rsec.items(), key=lambda x: -x[1])[:5],
            "build_date": rp.get("build_date"),
        }
    except Exception as e:  # noqa: BLE001
        log.warning("today_decision 博主共振失败: %s", e)
    try:
        ib = api_info_brief(hours=48)
        out["info_brief"] = {"items": (ib.get("items") or [])[:5],
                             "sectors": (ib.get("sectors") or [])[:8]}
    except Exception as e:  # noqa: BLE001
        log.warning("today_decision 信息面失败: %s", e)

    # ---- 2) 机会：主线「可推进」名单（梯队里尚未涨停的同方向票），跨方向去重取前 6 ----
    seen: set = set()
    for it in scored:
        for t in _jl(it["row"]["tiers_json"]):
            code = str(t.get("code") or "")
            pct = _num(t.get("pct"))
            if not code or code in seen or pct is None or pct >= 9.8:
                continue
            seen.add(code)
            out["opportunities"].append({
                "symbol": code, "name": t.get("name") or "", "sector": it["row"]["sector"],
                "pct": pct, "note": "同题材尚未大涨（主线「可推进」名单）",
            })
            if len(out["opportunities"]) >= 6:
                break
        if len(out["opportunities"]) >= 6:
            break

    # 备用机会源（2026-10-15 换源）：改用**四支柱**入选票补位。
    # 原实现读旧引擎 prediction_item 核心池、并标注"每日预测·核心" —— 那是旧引擎内容
    # 露在「今日决策」页上；按进哥"界面上把旧引擎消除"，改为新引擎（四支柱）。
    # 排序优先 A 政策漏斗 → C 超跌修复 → B 竞价冲浪 → D 共振，避免 D 的重复票抢占名额。
    if len(out["opportunities"]) < 4:
        try:
            prow = DB.conn.execute(
                "SELECT MAX(run_date) AS d FROM pillar_item").fetchone()
            pdate = prow["d"] if prow else None
            if pdate:
                rows = DB.conn.execute(
                    "SELECT i.symbol, i.name, i.pillar, r.title FROM pillar_item i "
                    "LEFT JOIN pillar_run r ON r.run_date=i.run_date AND r.pillar=i.pillar "
                    "WHERE i.run_date=? ORDER BY CASE i.pillar "
                    "  WHEN 'policy_funnel' THEN 1 WHEN 'dip_revert' THEN 2 "
                    "  WHEN 't1_surf' THEN 3 ELSE 4 END, i.rank", (pdate,)).fetchall()
                for p in rows:
                    if len(out["opportunities"]) >= 6:
                        break
                    code = str(p["symbol"] or "")
                    if not code or code in seen:
                        continue
                    seen.add(code)
                    _ptitle = str(p["title"] or p["pillar"] or "")
                    _ptitle = _ptitle.split("·")[-1].strip() or str(p["pillar"] or "")
                    out["opportunities"].append({
                        "symbol": code, "name": p["name"] or "", "sector": "",
                        "pct": None,
                        "note": "四支柱·%s（%s）" % (_ptitle, pdate),
                    })
        except Exception as e:  # noqa: BLE001
            log.warning("today_decision 四支柱补位失败: %s", e)

    # ---- 3) 风险：板块资金净流出 / 上榜次日跑输 + 情绪看空·分歧个股 ----
    sdate = None
    try:
        srow = DB.conn.execute("SELECT MAX(trade_date) AS d FROM lhb_sector_focus").fetchone()
        sdate = srow["d"] if srow else None
        if sdate:
            # ① 资金净流出（钱在撤）
            for r in DB.conn.execute(
                    "SELECT sector, net_amt FROM lhb_sector_focus WHERE trade_date=? "
                    "AND net_amt<0 ORDER BY net_amt ASC LIMIT 3", (sdate,)).fetchall():
                out["risks"].append({
                    "type": "sector", "label": r["sector"],
                    "detail": "资金净流出 %s 亿" % round((_num(r["net_amt"]) or 0.0) / 1e8, 2),
                })
            # ② 上榜方向次日跑输基准（资金进场没接住 = 退潮特征）
            if len(out["risks"]) < 3:
                for r in DB.conn.execute(
                        "SELECT sector, t1_avg, bench_t1, onlist_n FROM lhb_sector_focus "
                        "WHERE trade_date=? AND t1_avg IS NOT NULL ORDER BY t1_avg ASC LIMIT 5",
                        (sdate,)).fetchall():
                    t1 = _num(r["t1_avg"])
                    b1 = _num(r["bench_t1"])
                    if t1 is None or t1 >= 0 or (b1 is not None and t1 >= b1):
                        continue
                    if len(out["risks"]) >= 6:
                        break
                    out["risks"].append({
                        "type": "sector", "label": r["sector"],
                        "detail": "上榜 %s 家，次日 T+1 均 %s%%（资金没接住，注意退潮）"
                                  % (r["onlist_n"], round(t1, 2)),
                    })
    except Exception as e:  # noqa: BLE001
        log.warning("today_decision 板块风险读取失败: %s", e)

    # ③ 个股龙虎榜净卖出榜（钱从上榜票里撤） —— 本机 sector_focus 的 t1_avg 常为 NULL，
    #    所以这条「净卖出最狠的上榜票」是风险卡里最稳定、最真实的信号源。
    try:
        lrow = DB.conn.execute("SELECT MAX(trade_date) AS d FROM lhb_stocks").fetchone()
        ldate = lrow["d"] if lrow else None
        if ldate:
            for r in DB.conn.execute(
                    "SELECT security_code, security_name, SUM(net_amt) AS net, MIN(change_rate) AS chg "
                    "FROM lhb_stocks WHERE trade_date=? GROUP BY security_code "
                    "HAVING net<0 ORDER BY net ASC LIMIT 4", (ldate,)).fetchall():
                net = _num(r["net"]) or 0.0
                chg = _num(r["chg"])
                out["risks"].append({
                    "type": "stock", "label": r["security_name"] or r["security_code"],
                    "symbol": r["security_code"], "tone": "netout",
                    "detail": "龙虎榜净卖出 %s 亿%s（数据日 %s）"
                              % (abs(round(net / 1e8, 2)),
                                 ("，当日 %s%%" % round(chg, 2)) if chg is not None else "",
                                 ldate),
                })
    except Exception as e:  # noqa: BLE001
        log.warning("today_decision 龙虎榜净卖出读取失败: %s", e)

    try:
        pd = postdigest.build(hours=24)
    except Exception as e:  # noqa: BLE001
        pd = {}
        log.warning("today_decision 情绪速览失败: %s", e)
    if isinstance(pd, dict):
        for s in (pd.get("stocks") or []):
            if len(out["risks"]) >= 6:
                break
            vk = s.get("verdict_key")
            pos = s.get("pos") or 0
            neg = s.get("neg") or 0
            # 只收「真被看空」或「多空真分歧」的；把「中立（没明确观点）」这类无方向样本挡在门外 ——
            # 报告证据3 的教训：样本不足/没方向就不能硬凑成一条风险，否则风险卡全是噪音。
            if vk != "bear" and not (vk == "diverge" and pos > 0 and neg > 0):
                continue
            out["risks"].append({
                "type": "stock", "label": s.get("name") or s.get("key") or "",
                "symbol": s.get("key") or "", "tone": vk,
                "detail": "%s（近24h 看多 %d / 看空 %d）" % (s.get("verdict") or "", pos, neg),
            })

    # ---- 4) 我的票：关注池到价提醒 ----
    try:
        watchlib.ensure_schema(DB.conn)
        items = watchlib.list_items(DB.conn)
        alerts = watchlib.list_alerts(DB.conn, limit=50)
        unread = watchlib.count_unread(DB.conn)
        near: list = []
        for it in items:
            tp = _num(it.get("target_price"))
            lp = _num(it.get("last_price"))
            if tp is None or lp is None or tp <= 0:
                continue
            near.append({"symbol": it.get("symbol"), "name": it.get("name") or "",
                         "target_price": tp, "last_price": lp,
                         "dist_pct": round((lp - tp) / tp * 100.0, 2),
                         "dir": it.get("alert_dir") or ""})
        near.sort(key=lambda x: abs(x["dist_pct"]))
        out["mywatch"] = {
            "total": len(items), "unread": unread,
            "alerts": [{"symbol": a.get("symbol"), "name": a.get("name") or "",
                        "rule": a.get("rule"), "target_price": _num(a.get("target_price")),
                        "price": _num(a.get("triggered_price")),
                        "triggered_at": a.get("triggered_at"), "read": a.get("read")}
                       for a in alerts[:8]],
            "near": near[:6],
        }
    except Exception as e:  # noqa: BLE001
        log.warning("today_decision 关注池读取失败: %s", e)

    # ---- 5) 一句话结论（把四张卡压成一句） ----
    dirs = out["directions"]
    risks = out["risks"]
    mw = out["mywatch"]
    parts: list = []
    tone = "mixed"
    if dirs:
        d0 = dirs[0]
        parts.append("资金主攻 %s（净买 %s 亿 · %s）" % (d0["sector"], d0["net_yi"], d0["stage"]))
        tone = "bull"
    else:
        parts.append("今日暂无可确认的主攻方向")
    if risks:
        r0 = risks[0]
        t0 = r0.get("tone")
        if r0.get("type") == "stock" and t0 in ("bear", "diverge"):
            parts.append("情绪面 %s 被看空/分歧" % r0["label"])
        elif r0.get("type") == "stock":
            parts.append("%s（%s）" % (r0["label"], r0["detail"]))
        else:
            parts.append("%s%s" % (r0["label"], r0["detail"]))
        tone = "mixed" if dirs else "bear"
    if mw["unread"]:
        parts.append("关注池有 %d 条到价提醒待看" % mw["unread"])
    elif mw["near"]:
        parts.append("关注池 %d 只接近目标价" % len(mw["near"]))
    else:
        parts.append("关注池今日无新增到价")
    out["verdict"] = "；".join(parts) + "。"
    out["verdict_tone"] = tone
    out["basis"] = ("口径：方向=主线研判（sector_chain，资金强度+龙头强度+共振广度+分支延伸）；"
                    "机会=主线「可推进」名单（同题材尚未涨停）或每日预测核心；"
                    "风险=板块资金净流出 + 近 24 小时帖子情绪里被看空/分歧的个股；"
                    "我的票=关注池到价提醒。"
                    "融合信号：ETF 行业净申赎/涨跌（集思录+新浪）对方向卡做「资金确认」、跨境 ETF 对风险卡做"
                    "「海外传导」、宽基净申赎给出仓位 overlay；博主共振（resonance）给环境三态与板块共识；"
                    "信息面（info_brief）给消息 Top。以上均为方向参考，非买卖建议。")
    return out


# ---------------------------------------------------------------- 决策化改造 P0-D/E/F：三个「结论页」
# 报告《查看器体验审计与改造方案》要求给三个大类各置顶一个汇总结论，替掉
# 「6~9 个名单并列、得自己交叉比对」的现状。三者都**只读本地库、复用既有口径**：
#   P0-D 消息与外围 → /api/info_brief   「今日消息面 Top5 · 影响板块」
#   P0-E 别人怎么做 → /api/smart_money  「聪明钱共识榜」（多人同时买的方向 + 个股）
#   P0-F 我的票     → /api/watch_brief  「我的票 · 今日提示」
# 共同纪律（与全站一致）：**没数据就说没数据，不硬凑；每个来源标数据日，不把陈旧数据冒充"今日"。**

def _mainline_map() -> tuple:
    """(数据日, {code: {"sector","pct","forward"}})，取自 sector_chain 最新日。只读、秒回。
    `forward=True` 表示该票在同题材里尚未涨停（= 主线「可推进」名单的一员）。"""
    try:
        row = DB.conn.execute("SELECT MAX(trade_date) AS d FROM sector_chain").fetchone()
        d = row["d"] if row else None
        if not d:
            return None, {}
        m: dict = {}
        for r in DB.conn.execute(
                "SELECT sector, tiers_json FROM sector_chain WHERE trade_date=?", (d,)).fetchall():
            for t in _jl(r["tiers_json"]):
                c = str(t.get("code") or "")
                if not c or c in m:
                    continue
                pct = _num(t.get("pct"))
                m[c] = {"sector": r["sector"], "pct": pct,
                        "forward": (pct is not None and pct < 9.8)}
        return d, m
    except Exception as e:  # noqa: BLE001
        logging.getLogger(__name__).warning("_mainline_map 失败: %s", e)
        return None, {}


def _split_sectors(v) -> list:
    """政策/国际/海外的「影响板块」字段可能是 JSON 数组字符串，也可能逗号/顿号分隔。"""
    if not v:
        return []
    if isinstance(v, (list, tuple)):
        return [str(x).strip() for x in v if str(x).strip()]
    s = str(v).strip()
    if s.startswith("["):
        try:
            j = json.loads(s)
            if isinstance(j, list):
                return [str(x).strip() for x in j if str(x).strip()]
        except Exception:  # noqa: BLE001
            pass
    return [x.strip() for x in re.split(r"[,，、;；|]", s) if x.strip()]


def api_info_brief(hours: int = 48) -> dict:
    """「消息与外围」顶部汇总结论（P0-D）：9 个子项 → 一屏「今日消息面 Top5 · 影响板块」。
    分来源取高信号项（政策 importance / 国际 intensity / 海外 heat / 公告 tone·类别 / 快讯 / 社区热点），
    再按优先级与影响面收敛成 5 条；并汇总最常被点到的板块。**每源标数据日**，陈旧源不冒充今日。"""
    log = logging.getLogger(__name__)
    out: dict = {"ok": True, "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                 "window_hours": hours, "items": [], "sectors": [], "counts": {},
                 "freshness": [], "stale_warn": "", "basis": ""}
    cutoff = (datetime.now() - timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M")
    cutoff_d = cutoff[:10]
    cand: list = []

    # 先探各源数据日：**陈旧源不许排进 Top5**（例如国际局势可能停在数日前），只能当背景。
    # 这是「语义门」纪律：不能把旧闻混进"今日消息面"里冒充新消息。
    def _src_latest(tbl: str, col: str) -> str:
        try:
            return str(DB.conn.execute("SELECT MAX(%s) FROM %s" % (col, tbl)).fetchone()[0] or "")[:16]
        except Exception:  # noqa: BLE001
            return ""
    geo_latest = _src_latest("geo_events", "event_date")
    geo_stale = bool(geo_latest) and geo_latest[:10] < (datetime.now() - timedelta(days=3)).strftime("%Y-%m-%d")

    def _add(src, tag, title, sectors, note, date, symbols, prio):
        if not title:
            return
        cand.append({"src": src, "tag": tag or "", "title": str(title)[:150],
                     "sectors": sectors or [], "note": (note or "")[:120],
                     "date": str(date or "")[:16], "symbols": symbols or [], "prio": prio})

    # ① 政策 / 官方口径（importance 数字越小越重要）
    try:
        for r in DB.conn.execute(
                "SELECT pub_time, issuer, importance, title, sectors, transmission FROM policy_news "
                "WHERE pub_time>=? ORDER BY importance ASC, pub_time DESC LIMIT 12", (cutoff,)).fetchall():
            _add("政策", (r["issuer"] or "官方")[:20], r["title"], _split_sectors(r["sectors"]),
                 r["transmission"], r["pub_time"], [],
                 1 if (_num(r["importance"]) or 9) <= 1 else 2)
    except Exception as e:  # noqa: BLE001
        log.warning("info_brief 政策失败: %s", e)

    # ② 国际局势（intensity 越大越重要；注意该源当前可能停在更早的日期）
    try:
        for r in DB.conn.execute(
                "SELECT title, intensity, direction, sectors_up, decision, event_date, symbols_up "
                "FROM geo_events ORDER BY intensity DESC, event_date DESC LIMIT 6").fetchall():
            syms = _jl(r["symbols_up"]) if r["symbols_up"] else []
            _add("国际", "强度%s" % (r["intensity"] or "—"), r["title"],
                 _split_sectors(r["sectors_up"]), r["decision"] or r["direction"],
                 r["event_date"], [s for s in syms if isinstance(s, dict)][:4],
                 6 if geo_stale else (1 if (_num(r["intensity"]) or 0) >= 4 else 3))
    except Exception as e:  # noqa: BLE001
        log.warning("info_brief 国际失败: %s", e)

    # ③ 海外舆情（heat 热度）
    try:
        for r in DB.conn.execute(
                "SELECT publish_time, region, heat, title, sectors, sentiment FROM overseas_news "
                "WHERE publish_time>=? ORDER BY heat DESC LIMIT 8", (cutoff,)).fetchall():
            _add("海外", r["region"] or "", r["title"], _split_sectors(r["sectors"]),
                 (r["sentiment"] or ""), r["publish_time"], [],
                 2 if (_num(r["heat"]) or 0) >= 70 else 4)
    except Exception as e:  # noqa: BLE001
        log.warning("info_brief 海外失败: %s", e)

    # ④ 个股公告（tone 有明确多空 / 并购重组类优先）
    try:
        for r in DB.conn.execute(
                "SELECT notice_date, code, name, title, cat, tone FROM stock_ann "
                "WHERE notice_date>=? ORDER BY notice_date DESC LIMIT 80", (cutoff_d,)).fetchall():
            tone, cat = (r["tone"] or ""), (r["cat"] or "")
            prio = 2 if tone in ("good", "bad") else (3 if cat in ("并购重组", "业绩预告") else 5)
            _add("公告", "%s · %s" % (r["name"] or "", cat or "公告"), r["title"], [],
                 ("利好" if tone == "good" else ("利空" if tone == "bad" else "")),
                 r["notice_date"],
                 ([{"code": r["code"], "name": r["name"]}] if r["code"] else []), prio)
    except Exception as e:  # noqa: BLE001
        log.warning("info_brief 公告失败: %s", e)

    # ⑤ 全市场快讯（兜底，优先级最低）
    try:
        for r in DB.conn.execute(
                "SELECT publish_time, site, title FROM news WHERE publish_time>=? "
                "ORDER BY publish_time DESC LIMIT 20", (cutoff,)).fetchall():
            _add("快讯", r["site"] or "", r["title"], [], "", r["publish_time"], [], 5)
    except Exception as e:  # noqa: BLE001
        log.warning("info_brief 快讯失败: %s", e)

    # ⑥ 社区热点词（样本量）
    try:
        for r in DB.conn.execute(
                "SELECT topic, weight FROM topics ORDER BY weight DESC LIMIT 10").fetchall():
            _add("社区热点", "热度 %s" % round(_num(r["weight"]) or 0), r["topic"],
                 [r["topic"]], "帖子被高频提及的词", "", [], 6)
    except Exception as e:  # noqa: BLE001
        log.warning("info_brief 热点失败: %s", e)

    # 收敛成 5 条：按优先级、单来源最多 2 条（保证不全是同一源）
    picked: list = []
    per_src: dict = {}
    for c in sorted(cand, key=lambda x: (x["prio"], -len(x["sectors"]))):
        if len(picked) >= 5:
            break
        if per_src.get(c["src"], 0) >= 2:
            continue
        per_src[c["src"]] = per_src.get(c["src"], 0) + 1
        picked.append(c)
    out["items"] = picked

    # 影响板块云：统计全部候选里被点到的板块（比只看 5 条更能反映消息面重心）
    sec_cnt: dict = {}
    for c in cand:
        # 陈旧源（例如停在数日前的国际局势）不参与「影响板块」统计 ——
        # 否则一条旧闻的板块会顶到"今日重心"上，正是报告要根治的"时效断档"。
        if c["src"] == "国际" and geo_stale:
            continue
        for s in c["sectors"]:
            k = s.strip()
            if not k:
                continue
            e = sec_cnt.setdefault(k, {"sector": k, "n": 0, "srcs": set()})
            e["n"] += 1
            e["srcs"].add(c["src"])
    out["sectors"] = sorted(
        [{"sector": v["sector"], "n": v["n"], "srcs": sorted(v["srcs"])} for v in sec_cnt.values()],
        key=lambda x: -x["n"])[:10]

    # 各源数据日 + 陈旧提示（这是「语义门」：不能把 9-26 的国际局势当成今天的新消息）
    fresh: list = []
    for label, tbl, col in (("政策风向", "policy_news", "pub_time"),
                            ("国际局势", "geo_events", "event_date"),
                            ("海外舆情", "overseas_news", "publish_time"),
                            ("个股公告", "stock_ann", "notice_date"),
                            ("全市场快讯", "news", "publish_time"),
                            ("社区热点", "topics", "weight")):
        try:
            v = DB.conn.execute("SELECT MAX(%s) FROM %s" % (col, tbl)).fetchone()[0]
        except Exception:  # noqa: BLE001
            v = None
        if label == "社区热点":
            fresh.append({"label": label, "latest": "样本 %s 词条" % (len(DB.conn.execute(
                "SELECT 1 FROM topics LIMIT 30").fetchall()))})
            continue
        fresh.append({"label": label, "latest": str(v)[:16] if v else "—"})
    out["freshness"] = fresh
    stale = [f for f in fresh if f["label"] == "国际局势" and
             f["latest"] != "—" and f["latest"][:10] < (datetime.now() - timedelta(days=3)).strftime("%Y-%m-%d")]
    if stale:
        out["stale_warn"] = ("「国际局势」的最新数据停在 %s（不在本次 %s 小时窗口内）；"
                             "下面的外部消息以政策风向与海外舆情为准，国际条目仅供背景参考。"
                             % (stale[0]["latest"], hours))

    out["counts"] = {"candidates": len(cand), "policy": sum(1 for c in cand if c["src"] == "政策"),
                     "geo": sum(1 for c in cand if c["src"] == "国际"),
                     "overseas": sum(1 for c in cand if c["src"] == "海外"),
                     "ann": sum(1 for c in cand if c["src"] == "公告"),
                     "news": sum(1 for c in cand if c["src"] == "快讯"),
                     "topics": sum(1 for c in cand if c["src"] == "社区热点")}
    out["basis"] = ("口径：政策=官方/部委发布（importance 越小越重要）；国际=事件强度≥4；"
                    "海外=舆情热度≥70；公告=带多空 tone 或并购重组类；快讯/热点词兜底。"
                    "影响板块取自各源自带的 sectors 字段，未做二次推断。仅供方向参考，非投资建议。")
    return out


def api_smart_money(min_holders: int = 2, days: int = 5,
                     max_age_days: int | None = None) -> dict:
    """「别人怎么做」置顶「聪明钱共识榜」（P0-E）：6 个名单 → 合成 1 条结论。
    口径（沿用 2026-10-10 批准的信号拆分）：
      共识 = 当前持仓快照里**喊买入**的人数 buy_holders（不掺 HOLD/WATCH，避免"关注"虚增看多）；
      近期 = 近 N 日**同向买入**（动作发生时点）。两者互补，分别标注，不混成一个数字。"""
    log = logging.getLogger(__name__)
    out: dict = {"ok": True, "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                 "min_holders": min_holders, "days": days,
                 "consensus": [], "recent": [], "directions": [], "mainline_date": None,
                 "basis": ""}
    mdate, mmap = _mainline_map()
    out["mainline_date"] = mdate

    try:
        cons = DB.consensus_stocks(min_holders=min_holders,
                                   max_age_days=max_age_days) or []
        cons.sort(key=lambda x: (-(x.get("buy_holders") or 0), -(x.get("holders") or 0)))
        # 2026-10-15 P0-1：把 consensus 行携带的 data_asof 回带顶层，供前端角标。
        out["data_asof"] = max((str(x.get("data_asof") or "") for x in cons
                                if x.get("data_asof")), default="")
        out["positions_max_age_days"] = next(
            (x.get("positions_max_age_days") for x in cons if x.get("data_asof")), None)
        for x in cons[:12]:
            c = str(x.get("symbol") or "")
            ml = mmap.get(c) or {}
            out["consensus"].append({
                "symbol": c, "name": x.get("symbol_name") or "",
                "buy_holders": x.get("buy_holders") or 0,
                "hold_holders": x.get("hold_holders") or 0,
                "watch_holders": x.get("watch_holders") or 0,
                "holders": x.get("holders") or 0,
                "sector": ml.get("sector") or "", "pct": ml.get("pct"),
                "forward": bool(ml.get("forward")),
            })
    except Exception as e:  # noqa: BLE001
        log.warning("smart_money 共识失败: %s", e)

    try:
        co = DB.cohold_recent(days=days, min_holders=min_holders) or {}
        for x in (co.get("buy") or [])[:10]:
            c = str(x.get("symbol") or "")
            ml = mmap.get(c) or {}
            out["recent"].append({
                "symbol": c, "name": x.get("symbol_name") or "",
                "holders": x.get("holders") or len(x.get("players") or []),
                "buy_time": str(x.get("buy_time") or "")[:16],
                "sector": ml.get("sector") or "", "pct": ml.get("pct"),
            })
    except Exception as e:  # noqa: BLE001
        log.warning("smart_money 近期共买失败: %s", e)

    # 方向汇总：把共识个股按「概念板块」归堆（回答"多少人的共识票扎堆在同一方向"）。
    # 先用概念反向表（概念 → 代码）求每只票的概念，再只保留 **≥2 只共识票重合** 的概念；
    # 一个都没有时退回用今日主线方向标注（数据日见 mainline_date），不硬凑一个方向出来。
    c2s: dict = {}
    try:
        for concept, codes in (_sector_reverse() or {}).items():
            for c in codes:
                c2s.setdefault(str(c), []).append(concept)
    except Exception as e:  # noqa: BLE001
        log.warning("smart_money 概念反向表失败: %s", e)

    def _group(get_sector) -> dict:
        dm: dict = {}
        for x in out["consensus"]:
            for sec in (get_sector(x) or []):
                e = dm.setdefault(sec, {"sector": sec, "n": 0, "buy_holders": 0, "stocks": []})
                e["n"] += 1
                e["buy_holders"] += x["buy_holders"]
                e["stocks"].append({"symbol": x["symbol"], "name": x["name"],
                                    "buy_holders": x["buy_holders"]})
        return dm

    dirs = [v for v in _group(lambda x: (c2s.get(x["symbol"]) or [])[:8]).values() if v["n"] >= 2]
    if not dirs:
        dirs = list(_group(lambda x: [x["sector"] or "（不在今日主线内）"]).values())
    out["directions"] = sorted(dirs, key=lambda x: (-x["n"], -x["buy_holders"]))[:6]

    out["basis"] = ("口径：共识榜按「喊买入的人数」排序（buy_holders，来自 positions 快照 + 选手层级加权，"
                    "已剔除机器号与 HOLD/WATCH 干扰）；近期共买看的是近 %d 日**动作时点**（≥%d 人同向买入）。"
                    "方向归属取今日主线（数据日 %s）。它回答的是「别人在买什么」，不是买入建议。"
                    % (days, min_holders, mdate or "—"))
    return out


def api_watch_brief() -> dict:
    """「我的票」置顶「今日提示」（P0-F）：关注池今天有没有事，而不是再给一堆工具。
    五类提示：① 到价触发（未读优先）② 接近目标价 ③ 池内当日异动（现价/涨跌）④ 池内被看空·分歧
    ⑤ 池内落在今日主线的票。并显式报出「开了提醒却没设目标价」的票 —— 那些提醒永远不会响。"""
    log = logging.getLogger(__name__)
    out: dict = {"ok": True, "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                 "total": 0, "unread": 0, "groups": [], "alerts": [], "quotes": [],
                 "movers": [], "near": [], "no_target": [], "caution": [],
                 "on_mainline": [], "mainline_date": None, "quote_src": "", "basis": ""}
    mdate, mmap = _mainline_map()
    out["mainline_date"] = mdate

    items = []
    prices: dict = {}
    try:
        watchlib.ensure_schema(DB.conn)
        items = watchlib.list_items(DB.conn) or []
        out["total"] = len(items)
        out["unread"] = watchlib.count_unread(DB.conn)
        gmap: dict = {}
        for it in items:
            g = it.get("group_name") or "未分组"
            gmap[g] = gmap.get(g, 0) + 1
        out["groups"] = [{"name": k, "n": v} for k, v in sorted(gmap.items(), key=lambda x: -x[1])]
        for a in (watchlib.list_alerts(DB.conn, limit=50) or [])[:10]:
            out["alerts"].append({"symbol": a.get("symbol"), "name": a.get("name") or "",
                                  "rule": a.get("rule"), "target_price": _num(a.get("target_price")),
                                  "price": _num(a.get("triggered_price")),
                                  "triggered_at": a.get("triggered_at"), "read": a.get("read")})

        # 现价/当日涨跌：本地缓存的行情只覆盖极少数池内票（实测 3/14），所以走三源并发批量报价
        # （_watch_prices，整体硬上限 3s，失败即 None —— 前端显 «—»，绝不写 0）。
        codes = [str(it.get("symbol") or "") for it in items if it.get("symbol")]
        if codes:
            prices = _watch_prices(codes, budget=3.0) or {}
        out["quote_src"] = "多源并发" if prices else "未取到"
        for it in items:
            q = prices.get(str(it.get("symbol") or "")) or {}
            price, pct = _num(q.get("price")), _num(q.get("pct"))
            if price is None:
                continue
            out["quotes"].append({"symbol": it.get("symbol"), "name": it.get("name") or "",
                                  "price": price, "pct": pct})
        out["movers"] = sorted([q for q in out["quotes"] if q["pct"] is not None],
                               key=lambda x: -abs(x["pct"]))[:8]

        near = []
        for it in items:
            tp = _num(it.get("target_price"))
            if tp is None or tp <= 0:
                # 开了提醒却没有目标价 → 这条规则永远不可能触发。明确报出来，别让它静默失效。
                if it.get("alert_on"):
                    out["no_target"].append({"symbol": it.get("symbol"),
                                             "name": it.get("name") or ""})
                continue
            q = prices.get(str(it.get("symbol") or "")) or {}
            lp = _num(q.get("price"))
            if lp is None:
                lp = _num(it.get("last_price"))
            if lp is None:
                continue
            dist = round((lp - tp) / tp * 100.0, 2)
            near.append({"symbol": it.get("symbol"), "name": it.get("name") or "",
                         "target_price": tp, "last_price": lp, "dist_pct": dist,
                         "dir": it.get("alert_dir") or "", "group": it.get("group_name") or ""})
        near.sort(key=lambda x: abs(x["dist_pct"]))
        out["near"] = near[:8]
        # ④ 池内落在今日主线的票（+ 是否仍在「可推进」名单）
        for it in items:
            c = str(it.get("symbol") or "")
            ml = mmap.get(c)
            if not ml:
                continue
            out["on_mainline"].append({
                "symbol": c, "name": it.get("name") or "",
                "sector": ml.get("sector") or "", "pct": ml.get("pct"),
                "forward": bool(ml.get("forward")),
            })
        out["near"] = near[:8]
    except Exception as e:  # noqa: BLE001
        log.warning("watch_brief 关注池失败: %s", e)

    # ② 池内被看空/分歧的票（只认真分歧，挡掉"中立（没明确观点）"这类无方向样本）
    try:
        pd = postdigest.build(hours=72)
        vmap = {s.get("key"): s for s in (pd.get("stocks") or []) if s.get("key")}
        for it in items:
            s = vmap.get(str(it.get("symbol") or ""))
            if not s:
                continue
            pos, neg = s.get("pos") or 0, s.get("neg") or 0
            vk = s.get("verdict_key")
            if vk == "bear" or (vk == "diverge" and pos > 0 and neg > 0):
                out["caution"].append({
                    "symbol": it.get("symbol"), "name": it.get("name") or "",
                    "tone": vk, "text": s.get("verdict") or "",
                    "pos": pos, "neg": neg,
                })
    except Exception as e:  # noqa: BLE001
        log.warning("watch_brief 情绪交叉失败: %s", e)

    out["basis"] = ("口径：现价/涨跌=新浪·腾讯·东财三源并发批量报价（本机行情源不可达时显 «—»，不写 0）；"
                    "到价提醒来自关注池规则触发（未读优先）；接近目标价=|现价−目标价|/目标价；"
                    "看空/分歧取自近 72 小时帖子情绪（与「汇总·情绪速览」同一把尺子，只认真分歧）；"
                    "主线归属取 sector_chain 今日数据日 %s。提醒是提示，不是买卖建议。" % (mdate or "—"))
    return out


# ---------------------------------------------------------------- 分支透视
# 看板横条上「人工智能 18」这种，本质只是一个数字，点开什么都没有 —— 用户要的是
# 「都有谁 / 谁在买 / 大概会怎么走」。下面这个接口把分支展开成能直接判断的三块。
_CHAIN_Q: dict = {"mtime": None, "data": {}}
_SECTOR_REV: dict = {"mtime": None, "rev": {}}


def _chain_quotes() -> dict:
    """主线研判的行情快照（sector_chain_cache.json：code -> {name,price,pct,...}）。

    带 mtime 懒加载，文件没变就不重复解析（60KB+，每个弹层请求都读会明显拖慢）。
    """
    fp = ROOT / "sector_chain_cache.json"
    try:
        mt = fp.stat().st_mtime
    except OSError:
        return _CHAIN_Q["data"] or {}
    if _CHAIN_Q["mtime"] != mt:
        try:
            _CHAIN_Q["data"] = json.loads(fp.read_text(encoding="utf-8"))
            _CHAIN_Q["mtime"] = mt
        except Exception:  # noqa: BLE001 —— 缓存坏了就退回旧内容，不阻断弹层
            pass
    return _CHAIN_Q["data"] or {}


def _sector_reverse() -> dict:
    """sector_map_cache.json 反转：概念板块名 -> [股票代码]。

    只覆盖"被查过板块归属"的股票，故是**子集**；用途有二：
      ① 老数据没存分支成员名单时的离线兜底；
      ② 找出「该概念全市场范围内」被博主/选手提过的票（不限主线成员）。
    """
    fp = ROOT / "sector_map_cache.json"
    try:
        mt = fp.stat().st_mtime
    except OSError:
        return _SECTOR_REV["rev"] or {}
    if _SECTOR_REV["mtime"] != mt:
        rev: dict[str, list[str]] = {}
        try:
            for code, boards in (json.loads(fp.read_text(encoding="utf-8")) or {}).items():
                for b in (boards or []):
                    rev.setdefault(b, []).append(code)
        except Exception:  # noqa: BLE001
            rev = {}
        _SECTOR_REV["rev"] = rev
        _SECTOR_REV["mtime"] = mt
    return _SECTOR_REV["rev"] or {}


# 分支透视的实时行情兜底缓存（120s TTL）：全市场概念成员大多不在 sector_chain_cache
# 快照里（快照只覆盖主线成分 ~336 只），首次弹层拉一次批量行情，之后 2 分钟内秒开。
_BRANCH_LIVE: dict = {"t": 0.0, "data": {}}


def _branch_live_quotes(codes: list) -> dict:
    """成员股行情兜底：东财 ulist 批量（复用 _watch_prices，含腾讯逐只兜底）。

    · 只对**缺失**的代码发网络请求；主线弹层（快照全覆盖）依旧零网络、秒开。
    · 120s TTL：TTL 内重复弹层直接吃缓存；拿不到的也记一个空标记，避免每次打开都重试打上游。
    · 任何异常静默降级为空 dict——弹层宁可显示 «—»，不因网络卡死（诚实降级纪律）。
    """
    import time as _t
    now = _t.time()
    if now - _BRANCH_LIVE["t"] > 120:
        _BRANCH_LIVE["data"] = {}
        _BRANCH_LIVE["t"] = now
    out: dict = {}
    need: list = []
    for c in dict.fromkeys([str(x).strip() for x in (codes or []) if str(x).strip()]):
        x = _BRANCH_LIVE["data"].get(c)
        if x:
            out[c] = x
        else:
            need.append(c)
    if not need:
        return out
    try:
        fresh = _watch_prices(need)
    except Exception:  # noqa: BLE001
        fresh = {}
    for c in need:
        x = fresh.get(c)
        if _has_price(x):          # 只看有效价；0/缺失一律落空标记，TTL 内不重试
            _BRANCH_LIVE["data"][c] = x
            out[c] = x
        else:
            _BRANCH_LIVE["data"][c] = {}   # 空标记：TTL 内不重试
    return out


def api_branch_detail(sector: str, branch: str, date: str | None = None) -> dict:
    """分支透视：把看板上的分支数字展开成「都有谁 / 谁在买谁在说 / 大致判断」。

    口径（对用户必须诚实，故随包返回，前端照原样展示）：
      · members 来自 `sector_members(分支) ∩ 主线成分股`，东财 F10 单板块最多返回 300 只，
        概念越大越可能被截断 —— 重合数是「可见下限」，不是全市场真值。
      · rise_prob_3d 是**该营业部/机构席位的历史统计**（该席位过往买入后 3 日上涨占比），
        不是对这只票的预测，只作参照。
    · 博主/选手覆盖来自本地库 positions / trades / stock_mentions，与龙虎榜方向未必重叠；
      没有覆盖时明确返回空，不编。
    优先只读本地库与磁盘缓存（快照覆盖时零网络、弹层秒开）；仅当成员行情缺失时
    （全市场概念的成员不在快照内）才补一次批量行情（120s TTL），失败静默降级为 «—»。
    """
    row = None
    if date:
        row = DB.conn.execute(
            "SELECT * FROM sector_chain WHERE trade_date=? AND sector=?",
            (date, sector)).fetchone()
    if not row and sector:
        row = DB.conn.execute(
            "SELECT * FROM sector_chain WHERE sector=? ORDER BY trade_date DESC LIMIT 1",
            (sector,)).fetchone()

    no_mainline = False
    b = None
    if row:
        d = row["trade_date"]
        branches = _jl(row["branches_json"], []) or []
        b = next((x for x in branches if x.get("name") == branch), None)
    if not b:
        # 无主线（sector 留空/在 sector_chain 里找不到）或该主线本期无此分支 → 按全市场概念兜底：
        # 概念本身在磁盘缓存 sector_map_cache.json 有全市场归属，直接据此取成员。
        rev0 = _sector_reverse()
        if branch in rev0:
            if row:
                d = row["trade_date"]
            else:
                d = date or (DB.conn.execute(
                    "SELECT MAX(trade_date) AS d FROM lhb_stocks").fetchone()["d"])
            b = {"name": branch, "members": [{"code": c, "name": ""} for c in rev0[branch]]}
            src = "cache"
            no_mainline = True
        else:
            return {"available": False, "sector": sector, "branch": branch,
                    "date": (row["trade_date"] if row else None),
                    "message": ("还没有该方向的主线研判数据，请先运行 sector_chain.py。"
                                if not row else
                                "本期方向里没有这个分支（可能已轮换），刷新看板可看最新分支。")}

    members = b.get("members") or []
    if not no_mainline:
        src = "chain"
        if not members:
            members = [{"code": c, "name": ""} for c in _sector_reverse().get(branch, [])]
            src = "cache"
    codes = [m["code"] for m in members]

    # 全市场当日上榜池：用于算「市场共振」wide / 主线内上榜 reso 与共振名单
    lhb_pool = {}
    for r in DB.conn.execute(
            "SELECT security_code, security_name, change_rate, net_amt, reason "
            "FROM lhb_stocks WHERE trade_date=?", (d,)):
        net = _num(r["net_amt"]) or 0.0
        lhb_pool[r["security_code"]] = {
            "name": r["security_name"] or "",
            "pct": _num(r["change_rate"]),
            "net_yi": round(net / 1e8, 2),
            "reason": r["reason"],
        }
    lhb_all = set(lhb_pool.keys())
    rev = _sector_reverse()
    reso = len(set(codes) & lhb_all)
    wide_codes = sorted(set(rev.get(branch, [])) & lhb_all)
    wide = len(wide_codes)
    state = "已扩散" if wide >= 5 else ("局部共振" if wide >= 1 else "尚未启动")
    state_rule = ("全市场同概念当日上龙虎榜 ≥5 只＝已扩散；1–4 只＝局部共振；"
                  "0 只＝尚未启动（与「方向研判报告」口径一致）。")
    wide_list = []
    for c in wide_codes:
        x = lhb_pool[c]
        wide_list.append({
            "code": c, "name": x["name"], "pct": x["pct"], "net_yi": x["net_yi"],
            "reason": x["reason"], "on_mainline": (c in set(codes)),
        })
    wide_list.sort(key=lambda x: -(x["net_yi"] or 0))

    q = _chain_quotes()

    # ① 成员股：涨幅（本地行情快照）+ 是否上榜/净买/上榜原因
    stocks = []
    for m in members[:60]:
        c = m["code"]
        qv = q.get(c) or {}
        stocks.append({"code": c, "name": m.get("name") or qv.get("name") or "",
                       "pct": qv.get("pct"), "price": qv.get("price"),
                       "on_lhb": False, "net_yi": None, "reason": None, "lhb_chg": None})
    by_code = {s["code"]: s for s in stocks}
    if codes:
        ph = ",".join("?" * len(codes))
        for r in DB.conn.execute(
                f"""SELECT security_code, security_name, change_rate, net_amt, reason
                    FROM lhb_stocks WHERE trade_date=? AND security_code IN ({ph})""",
                (d, *codes)):
            s = by_code.get(r["security_code"])
            if not s:
                continue
            s["on_lhb"] = True
            s["net_yi"] = round((_num(r["net_amt"]) or 0.0) / 1e8, 2)
            s["reason"] = r["reason"]
            s["lhb_chg"] = _num(r["change_rate"])
            if not s["name"]:
                s["name"] = r["security_name"] or ""

    # 涨幅/名称兜底（2026-09-29）：行情快照 sector_chain_cache 只覆盖主线成分（~336 只），
    # 全市场概念（如「西部大开发」）的成员大多不在其中 → 涨幅与名称大面积空。
    # 两级兜底：① 龙虎榜自带当日涨跌幅（本地，零成本，上榜股即有）；
    #           ② 仍缺的走 _branch_live_quotes 实时批量行情（120s TTL，失败静默显示 «—»）。
    for s in stocks:
        if s["pct"] is None and s["lhb_chg"] is not None:
            s["pct"] = s["lhb_chg"]
    _miss = [s["code"] for s in stocks if s["pct"] is None or not s["name"]]
    if _miss:
        _live = _branch_live_quotes(_miss)
        for s in stocks:
            x = _live.get(s["code"]) or {}
            if s["pct"] is None and x.get("pct") is not None:
                s["pct"] = x["pct"]
                s["price"] = x.get("price")
            if not s["name"] and x.get("name"):
                s["name"] = x["name"]
    stocks.sort(key=lambda x: -(x["pct"] if x["pct"] is not None else -999))

    # ② 谁在买：成员股上的龙虎榜买方席位（净买 + 该席位历史 3 日上涨概率）
    buyers = []
    if codes:
        for r in DB.conn.execute(
                f"""SELECT security_code, operate_name, side, buy, net, rise_prob_3d
                    FROM lhb_seats WHERE trade_date=? AND security_code IN ({ph})
                    ORDER BY net DESC""", (d, *codes)):
            if (r["side"] or "") != "BUY":
                continue
            net = _num(r["net"]) or 0.0
            if net <= 0:
                continue
            s = by_code.get(r["security_code"]) or {}
            buyers.append({"stock": s.get("name") or r["security_code"],
                           "code": r["security_code"], "name": r["operate_name"] or "—",
                           "net_yi": round(net / 1e8, 2),
                           "buy_yi": round((_num(r["buy"]) or 0.0) / 1e8, 2),
                           "rise_prob_3d": _num(r["rise_prob_3d"])})
    buyers = buyers[:12]

    # ③ 谁在说：该概念（全市场范围）被博主/实盘选手关注、提及的，带原话与链接
    universe = sorted(set(codes) | set(_sector_reverse().get(branch, [])))
    advocates: list[dict] = []
    seen_adv: set = set()
    if universe:
        ph2 = ",".join("?" * len(universe))
        for who_col, tbl in (("player", "positions"), ("author", "trades")):
            for r in DB.conn.execute(
                    f"""SELECT {who_col} AS who, symbol, symbol_name, action, site,
                               post_url, raw_text
                        FROM {tbl} WHERE symbol IN ({ph2}) ORDER BY symbol LIMIT 60""",
                    universe):
                k = (r["who"], r["symbol"], r["action"])
                if k in seen_adv:
                    continue
                seen_adv.add(k)
                advocates.append({"who": r["who"] or "—", "symbol": r["symbol"],
                                  "name": r["symbol_name"] or "", "action": r["action"] or "",
                                  "site": r["site"] or "", "url": r["post_url"] or "",
                                  "text": r["raw_text"] or "", "kind": "持仓/操作",
                                  "on_mainline": r["symbol"] in by_code})
        for r in DB.conn.execute(
                f"""SELECT m.symbol, m.symbol_name, m.context, p.author AS who, p.site,
                           p.id AS pid
                    FROM stock_mentions m LEFT JOIN posts p ON p.id = m.post_id_ref
                    WHERE m.symbol IN ({ph2}) LIMIT 60""", universe):
            k = (r["who"], r["symbol"], "mention")
            if k in seen_adv:
                continue
            seen_adv.add(k)
            advocates.append({"who": r["who"] or "—", "symbol": r["symbol"],
                              "name": r["symbol_name"] or "", "action": "提及",
                              "site": r["site"] or "", "url": "", "text": r["context"] or "",
                              "kind": "讨论帖", "on_mainline": r["symbol"] in by_code})
    advocates = advocates[:24]

    # ④ 大致判断：全部来自上面几块的真实计数，不引入任何外部假设
    pcts = [s["pct"] for s in stocks if s["pct"] is not None]
    n_zt = sum(1 for p in pcts if p >= 9.8)
    n_up = sum(1 for p in pcts if p > 0)
    n_dn = sum(1 for p in pcts if p < 0)
    lhb_stocks = [s for s in stocks if s["on_lhb"]]
    net_sum = round(sum(s["net_yi"] or 0.0 for s in lhb_stocks), 2)
    probs = [x["rise_prob_3d"] for x in buyers if x["rise_prob_3d"] is not None]
    prob_avg = round(sum(probs) / len(probs), 1) if probs else None
    # 候选（还没怎么动的）：分支已启动、但它自己涨幅 <3%，属"同方向补涨"观察位
    cands = [s for s in stocks if (s["pct"] is not None and s["pct"] < 3)]
    if n_zt >= 2 or (n_zt >= 1 and len(lhb_stocks) >= 3):
        tone = "hot"
    elif n_up > n_dn or lhb_stocks:
        tone = "warm"
    else:
        tone = "cool"
    parts = [f"与「{sector}」重合 {b.get('overlap')} 只"]
    if n_zt:
        parts.append(f"其中 {n_zt} 只涨停")
    parts.append(f"{n_up} 涨 / {n_dn} 跌")
    if lhb_stocks:
        parts.append(f"{len(lhb_stocks)} 只上龙虎榜、合计净买 {net_sum} 亿")
    if prob_avg is not None:
        parts.append(f"买方席位历史 3 日上涨概率均值 {prob_avg}%")
    if not advocates:
        parts.append("该分支暂无博主/选手覆盖")
    judge = {"tone": tone, "text": "；".join(parts) + "。",
             "n_zt": n_zt, "n_up": n_up, "n_dn": n_dn, "n_lhb": len(lhb_stocks),
             "net_sum_yi": net_sum, "prob_avg": prob_avg,
             "n_candidates": len(cands),
             "candidates": [{"code": s["code"], "name": s["name"], "pct": s["pct"]}
                            for s in cands[:8]],
             "basis": [
                 "成员 = 该概念成分股 ∩ 主线成分股（东财 F10，单板块上限 300 只）",
                 "净买 / 上榜原因来自交易所龙虎榜",
                 "席位上涨概率 = 该营业部/机构的历史统计，非个股预测",
                 "博主覆盖来自本地库持仓与讨论帖，与龙虎榜方向未必重叠",
             ]}

    return {"available": True, "sector": sector, "branch": branch, "date": d,
            "sector_label": ("（全市场概念，无主线归属）" if no_mainline else sector),
            "overlap": (None if no_mainline else b.get("overlap")),
            "reso": reso, "wide": wide, "state": state, "state_rule": state_rule,
            "wide_list": wide_list,
            "members_source": src,
            "members_total": len(members), "stocks": stocks,
            "buyers": buyers, "advocates": advocates, "judge": judge,
            "note": "仅为方向参考，非买卖建议。"}


def api_summary() -> dict:
    counts = DB.counts()
    login = DB.get_login_status()
    # 给每条登录态补充「登录网址 + 中文名 + 是否需要登录」，使前端能渲染可点击的登录入口。
    login = [
        {**row, **_site_login_meta(row.get("site") or "")}
        for row in login
    ]
    last = DB.get_meta("last_ingest", "尚未抓取")
    return {"counts": counts, "login_status": login, "last_ingest": last,
            # 「知识获取 → 抓取与登录记录」用：最近一次抓取动作的执行时刻
            "last_fetch_at": DB.get_meta("last_fetch_at", ""),
            "sites": _sites(),
            # 2026-10-05（报告 P2 / 建议4）：把「选手榜 / 持仓采集」的配置可用性显性化。
            # 这两项从来没配全过，界面上却是完整面板 —— 得让用户看见它是半启用状态。
            "player_cfg": _player_cfg()}


def _player_cfg() -> dict:
    """选手榜 / 持仓采集的配置体检结果（供前端打「该源未配置，仅行为反推」角标）。

    players_enabled  = 至少一个已启用站点配了 targets.players（collect_players 会真跑）
    positions_enabled= 至少一个已启用站点配了 player_page_tpl（collect_positions 会真跑）
    两者任意为空时，对应面板的数据只能来自 rebuild_player_pool 的**行为反推**。
    """
    out = {"players_enabled": False, "positions_enabled": False,
           "players_sites": [], "positions_sites": []}
    try:
        # ⚠ load_config 的第一个参数是**文件路径**，不是站点名；要按站点取得用 get_site。
        data = load_config() or {}
    except Exception:  # noqa: BLE001
        data = {}
    try:
        for key in _sites():
            try:
                cfg = get_site(data, key) or {}
            except Exception:  # noqa: BLE001 —— 单站读失败不影响其它站
                continue
            tg = (cfg.get("targets") or {})
            if tg.get("players"):
                out["players_enabled"] = True
                out["players_sites"].append(key)
            if (cfg.get("player_page_tpl") or "").strip():
                out["positions_enabled"] = True
                out["positions_sites"].append(key)
    except Exception:  # noqa: BLE001
        logging.exception("读取 player 配置体检失败")
    return out


def api_raw_pages(limit: int = 60) -> dict:
    """原始存档（知识获取 → 原始存档）：抓取回来的原始页面清单。

    只暴露「存档元信息」——来源、URL、抓取时刻、字节数与正文长度；
    **不回正文**，避免把整页 HTML 塞进接口（面板只需要知道存了什么、什么时候存的）。
    """
    limit = max(1, min(int(limit or 60), 500))
    rows = DB.conn.execute(
        """SELECT id, site, url, fetched_at, http_status,
                  LENGTH(COALESCE(html,'')) AS bytes
           FROM raw_pages ORDER BY fetched_at DESC, id DESC LIMIT ?""",
        (limit,),
    ).fetchall()
    total = DB.conn.execute("SELECT COUNT(*) AS n FROM raw_pages").fetchone()["n"]
    by_site = {r["site"]: r["n"] for r in DB.conn.execute(
        "SELECT site, COUNT(*) AS n FROM raw_pages GROUP BY site ORDER BY n DESC")}
    return {"total": total, "shown": len(rows), "by_site": by_site,
            "pages": [dict(r) for r in rows]}


def api_stocks() -> list[dict]:
    # sources：该股讨论帖来自哪些来源站点（Request G：「最后更新」列/角标据此判定时效）
    rows = DB.conn.execute(
        """SELECT symbol, symbol_name, COUNT(*) AS mentions,
                  MAX(p.post_time) AS last_time,
                  GROUP_CONCAT(DISTINCT p.site) AS sources
           FROM stock_mentions sm JOIN posts p ON p.id = sm.post_id_ref
           GROUP BY symbol ORDER BY mentions DESC LIMIT 50"""
    ).fetchall()
    if not rows:
        # 回退：从 trades 取操作过的股票
        rows = DB.conn.execute(
            "SELECT symbol, symbol_name, COUNT(*) AS mentions, MAX(post_time) AS last_time, "
            "GROUP_CONCAT(DISTINCT site) AS sources "
            "FROM trades WHERE symbol IS NOT NULL GROUP BY symbol ORDER BY mentions DESC LIMIT 50"
        ).fetchall()
    out = []
    for r in rows:
        n = _named(r["symbol"], r["symbol_name"] or "")
        name = n["name"]
        out.append({
            "symbol": r["symbol"],
            "symbol_name": name,
            "name": name,               # 与 /api/sentiment 字段对齐，前端可统一渲染
            "company": n["company"],
            "market": n["market"],
            "name_missing": n["name_missing"],
            "sources": [s for s in (r["sources"] or "").split(",") if s],
            # label 供旧版前端/其他调用方兼容；新版界面按 name/code 分栏展示
            "label": f"{name}（{r['symbol']}）" if name else f"{r['symbol']}（名称待补全）",
            "mentions": r["mentions"],
            "last_time": r["last_time"] or "",
        })
    # 2026-10-12 体验审计 P0-C：「人气高」≠「被看好」—— 给每只票补「人群态度」三态。
    # 数据来自帖子情绪速览（postdigest，与「汇总」面板同一把尺子、近 72 小时窗口）。
    # 没被任何帖子明确表态的票 posture 为 null，前端只显示「人气」不硬贴标签（不编方向）。
    try:
        pd = postdigest.build(hours=72)
        vmap = {s.get("key"): s for s in (pd.get("stocks") or []) if s.get("key")}
    except Exception as e:  # noqa: BLE001
        logging.getLogger(__name__).warning("api_stocks 情绪三态附加失败: %s", e)
        vmap = {}
    for it in out:
        s = vmap.get(it["symbol"])
        if not s:
            continue
        it["posture"] = s.get("verdict_key")
        it["posture_text"] = s.get("verdict")
        it["posture_pos"] = s.get("pos") or 0
        it["posture_neg"] = s.get("neg") or 0
        it["posture_posts"] = s.get("posts") or 0
    return out


def _player_index() -> dict:
    """name -> {tier, platform}，供各列表统一标注「来源渠道」。"""
    idx: dict[str, dict] = {}
    for r in DB.conn.execute("SELECT name, site, platform, tier, channel FROM players").fetchall():
        if not r["name"]:
            continue
        idx[r["name"]] = {
            "tier": r["tier"] or r["channel"],
            "platform": r["platform"] or r["site"],
        }
    return idx


def api_operating() -> list[dict]:
    # 2026-09-24：多返回 post_url 和 post_id，前端据此识别 demo（demo_data 写入的 post_url 全是 example.local
    # 且 post_id 以 'demo-' 开头）。不返回 post_url 的话前端没法分辨 demo vs 真实。
    rows = DB.conn.execute(
        "SELECT id, site, author, post_id, post_url, post_time, action, symbol, symbol_name, "
        "price, quantity, raw_text "
        "FROM trades WHERE action IN ('BUY','SELL','HOLD') ORDER BY post_time DESC LIMIT 200"
    ).fetchall()
    idx = _player_index()
    out = []
    for r in rows:
        d = dict(r)
        meta = idx.get(d.get("author") or "", {})
        d["author_tier"] = meta.get("tier")
        d["author_platform"] = meta.get("platform") or d.get("site")
        out.append(d)
    return out


# 相关人物评分字段（来自 viewer/person_score.py 建的 person_scores 表）。
# 2026-09-25 增量：王总反馈「角色」列零信息量、「最后更新」对人物无意义，真正要看的是
# **历史战绩 + 打分排序**。故 api_persons 在保持老字段（name/mention_count/role/sites/tier/platform）
# 完全不变的前提下，LEFT JOIN person_scores 把评分字段一并返回。
# 关键：person_scores 表可能还没建（首次部署 / 未跑 --rebuild），此时必须**优雅降级**——
# 返回老结构 + 空的评分字段，绝不 500。
_PERSON_SCORE_COLUMNS = [
    "score", "score_tier", "is_natural_person", "person_kind", "in_ranking",
    "post_count", "timed_mention_count", "trade_count", "position_count",
    "first_seen", "last_seen", "active_days", "symbol_count",
    "win_rate_5d", "win_rate_20d", "avg_ret_5d", "avg_ret_20d",
    "best_call_ret", "best_call_symbol", "best_call_date",
    "evidence_level", "score_detail_json", "public_record_json",
]


def _load_json_or_none(raw):
    """把库里存 JSON 字符串的字段解析成对象；空/坏数据一律返回 None（前端按空处理）。"""
    if not raw:
        return None
    try:
        return json.loads(raw)
    except Exception:  # noqa: BLE001
        return None


def _person_site_map() -> dict[str, list[str]]:
    """一次聚合出「每个作者的发帖站点」——原来每人一条 DISTINCT 查询（170 人 = 170 次，典型 N+1）。"""
    out: dict[str, list[str]] = {}
    try:
        rows = DB.conn.execute(
            "SELECT author AS name, site FROM posts "
            "WHERE author IS NOT NULL AND author<>'' AND site IS NOT NULL AND site<>'' "
            "GROUP BY author, site ORDER BY author, site").fetchall()
    except Exception:  # noqa: BLE001 —— 表结构变了不该拖垮整个接口
        return out
    for r in rows:
        out.setdefault(r["name"], []).append(r["site"])
    return out


def api_persons(limit: int = 0, offset: int = 0) -> list[dict]:
    """选手榜：返回 list（不传 limit 时与从前完全一致，前端零改）。

    limit>0 = 分页（默认 0 = 全量，界面文案承诺「不遗漏任何一个名称」）；
    offset 配合 limit 使用。附带把「每人查一次站点」改成一次 GROUP BY 聚合（N+1 → 1）。
    """
    # 2026-09-22 修复 P2-5：原 LIMIT 100 会把 170 人截断成 100 人，
    # 而界面文案承诺「不遗漏任何一个名称」；上限放到 1000（仅兜底防爆表）。
    scored = True
    try:
        cols = ", ".join(f"s.{c} AS {c}" for c in _PERSON_SCORE_COLUMNS)
        rows = DB.conn.execute(
            "SELECT p.name AS name, p.mention_count AS mention_count, p.role AS role, "
            f"{cols} FROM persons p LEFT JOIN person_scores s ON s.name = p.name "
            "ORDER BY p.mention_count DESC LIMIT 1000"
        ).fetchall()
    except Exception:  # noqa: BLE001 —— person_scores 不存在（或列不全）时降级
        scored = False
        rows = DB.conn.execute(
            "SELECT name, mention_count, role FROM persons ORDER BY mention_count DESC LIMIT 1000"
        ).fetchall()
    idx = _player_index()
    site_map = _person_site_map()
    start = max(0, int(offset or 0))
    page = max(0, int(limit or 0))
    rows = rows[start: start + page] if page else rows
    out = []
    for r in rows:
        name = r["name"]
        sites = site_map.get(name) or []
        meta = idx.get(name or "", {})
        item = {
            "name": name,
            "mention_count": r["mention_count"],
            "role": r["role"],
            "sites": sites,
            "tier": meta.get("tier"),
            "platform": meta.get("platform"),
        }
        if scored:
            item.update({c: r[c] for c in _PERSON_SCORE_COLUMNS})
            # 明细/公开战绩解析成对象，前端直接展开；无表时这些键缺失，前端按空处理
            item["score_detail"] = _load_json_or_none(r["score_detail_json"])
            item["public_record"] = _load_json_or_none(r["public_record_json"])
        out.append(item)
    return out


# ============================================================ 策略实验室（T03）
# 2026-09-26 新增 /api/strategies：策略目录（strategy_data.STRATEGIES，唯一真源）
# LEFT JOIN 窗口指标（strategy_sim_window，唯一真源），组装设计文档 §2 决策 6 的 JSON。
# 关键约束：
#   1) 不可回测 / 口径受限 / 无数据的数字字段一律 None，**绝不用 0 冒充缺失**；
#   2) 表缺失或导入失败时优雅降级（不抛 500）；
#   3) 只读不写库，无需 INGEST_LOCK。
_STRATEGY_WINDOWS = [
    {"label": "mid", "name": "长周期(约2年)", "bars": 500},
    {"label": "y1", "name": "近一年", "bars": 250},
    {"label": "h1", "name": "近半年", "bars": 125},
]
# 择时/仓位切换类的判定（团队复核要求：显式标注，避免被误读为选股能力）：
#   school == "组合/仓位/择时" 或 family ∈ {vol_filter, vol_regime}
_STRATEGY_SWITCH_SCHOOL = "组合/仓位/择时"
_STRATEGY_SWITCH_FAMILIES = {"vol_filter", "vol_regime"}


def _strategy_switch_kind(family, school) -> str | None:
    """择时/仓位切换类的细分标签；非该类返回 None。"""
    if family in ("market_timing", "breadth", "regime"):
        return "择时"
    if family in ("vol_filter", "vol_target", "position_sizing"):
        return "仓位/波动率"
    if family == "portfolio":
        return "组合"
    if school == _STRATEGY_SWITCH_SCHOOL:
        return "组合/仓位/择时"
    return None


def _strategy_status_note(st: dict) -> str:
    """不可回测 / 口径受限的中文原因（界面直接展示，不使用任何数字）。"""
    if st.get("scope_limited"):
        return "该策略的仓位/横截面逻辑与『等权满仓』口径不符，不予回测与排名"
    needs = st.get("needs")
    if needs:
        return f"缺少{needs}，本次不可回测"
    return "缺少回测所需数据，本次不可回测"


def _strategy_row_get(row, key, default=None):
    """安全读取 sqlite Row 的列：列不存在（旧库未 ALTER）时返回默认值，不抛 KeyError。"""
    try:
        keys = row.keys()
    except Exception:  # noqa: BLE001
        keys = []
    return row[key] if key in keys else default


def _strategy_win_rate(row):
    """0 笔了结交易时胜率是「未定义」→ 返回 None（而非 0，避免被误读成"胜率 0%"）。"""
    trades = _strategy_row_get(row, "trades")
    if not trades:
        return None
    return _strategy_row_get(row, "win_rate")


def _strategy_window_obj(row) -> dict | None:
    """把一行 strategy_sim_window 组装为中栏 mid 对象；row 为 None → None（无数据）。"""
    if row is None:
        return None
    return {
        "window_label": row["window_label"],
        "bars": row["bars"],
        "start": row["start"],
        "end": row["end"],
        "rank": row["rank"],
        "total_return": row["total_return"],
        "annual_return": row["annual_return"],
        "max_dd": row["max_dd"],
        "win_rate": _strategy_win_rate(row),
        "trades": row["trades"],
        "open_at_end": bool(_strategy_row_get(row, "open_at_end")),
        "beat_bench": row["beat_bench"],
        "stocks": row["stocks"],
        "bench_return": row["bench_return"],
        "score": row["score"],
        "rating": row["rating"],
        "status": row["status"],
        "sample_note": row["sample_note"],
        # 2026-10-15 P0-2 / DEF-007：walk-forward 样本外字段透传（旧行缺字段→None）
        # 2026-10-15 修复（全站回测抓到）：row 是 sqlite3.Row，**没有 .get()**——
        # 之前这 6 个字段误写成 row.get(...)，导致 /api/strategies 整个 500，
        # 「策略实验室」面板实测直接显示"数据加载失败"（生产库同样复现）。
        # 统一改用本文件既有的 _strategy_row_get（列不存在返回 None，兼容旧库未 ALTER）。
        "walk_forward": _strategy_row_get(row, "walk_forward"),
        "in_sample_return": _strategy_row_get(row, "in_sample_return"),
        "oos_return": _strategy_row_get(row, "oos_return"),
        "oos_beat_bench": _strategy_row_get(row, "oos_beat_bench"),
        "oos_trades": _strategy_row_get(row, "oos_trades"),
        "oos_status": _strategy_row_get(row, "oos_status"),
        "oos_validated": bool(_strategy_row_get(row, "oos_validated")),
        "best": _load_json_or_none(row["best_json"]),
        "worst": _load_json_or_none(row["worst_json"]),
    }


def _strategy_side_window_obj(row, name: str) -> dict | None:
    """把一行组装为右栏双窗口（h1/y1）精简对象；row 为 None → None。"""
    if row is None:
        return None
    tr = row["total_return"]
    br = row["bench_return"]
    excess = (tr - br) if (tr is not None and br is not None) else None
    return {
        "window_label": row["window_label"],
        "name": name,
        "bars": row["bars"],
        "start": row["start"],
        "end": row["end"],
        "total_return": tr,
        "bench_return": br,
        "excess": excess,
        "max_dd": row["max_dd"],
        "win_rate": _strategy_win_rate(row),
        "trades": row["trades"],
        "open_at_end": bool(_strategy_row_get(row, "open_at_end")),
        "stocks": row["stocks"],
        "status": row["status"],
        "sample_note": row["sample_note"],
    }


def _fund_strat_brief(strat, key: str) -> dict:
    """策略目录真源里的简要信息（找不到也返回 key，供前端显示未知项）。"""
    if strat is None:
        return {"key": key, "name": key, "found": False}
    return {
        "key": key,
        "name": strat.get("name"),
        "school": strat.get("school"),
        "family": strat.get("family"),
        "desc": strat.get("desc"),
        "buy": strat.get("buy"),
        "source": strat.get("source"),
        "found": True,
    }


def api_fund_strategies(key: str | None) -> dict:
    """GET /api/fund_strategies?key=XXX —— 某策略对应的公募基金/ETF 关联（净口径）。

    读 fund_strategy_signal / fund_strategy_meta（引擎 fund_link.py 写，接口只读）。
    来源逐条在每行 fund 的 `source` 字段标注；表缺失 / 未生成 → 优雅降级提示。
    """
    # 1) 策略目录真源
    strat = None
    try:
        import strategy_data  # noqa: PLC0415
        for s in getattr(strategy_data, "STRATEGIES", []) or []:
            if s.get("key") == key:
                strat = s
                break
    except Exception:  # noqa: BLE001
        strat = None

    # 2) 关联数据是否已生成（表缺失 → 优雅降级）
    try:
        mrow = DB.conn.execute("SELECT * FROM fund_strategy_meta WHERE id=1").fetchone()
    except Exception:  # noqa: BLE001
        mrow = None
    if mrow is None:
        return {
            "available": False,
            "strategy": _fund_strat_brief(strat, key),
            "message": ("尚未生成基金关联数据。请在项目根目录运行 `python fund_link.py` "
                        "（需联网抓取东方财富基金净值）后重试。"),
        }
    meta = {k: mrow[k] for k in mrow.keys()}
    coverage = _load_json_or_none(meta.get("coverage_json")) or {}
    sources = _load_json_or_none(meta.get("sources_json")) or []
    cov = coverage.get(key)

    # 3) 该策略命中基金（按 距今天数↑、信号日至今收益↓ 排序，避免 SQL 方言差异）
    funds: list = []
    try:
        rows = DB.conn.execute(
            "SELECT fund_code,fund_name,fund_type,market,signal_date,days_ago,ret_since,ret_20d,"
            "ret_120d,ret_1y,nav_latest,nav_date,bars,caliber,caliber_note,source,computed_at "
            "FROM fund_strategy_signal WHERE strategy_key=?", (key,)
        ).fetchall()
        for r in rows:
            funds.append(dict(r))
        funds.sort(key=lambda d: (d.get("days_ago") is None, d.get("days_ago") or 0,
                                  -(d.get("ret_since") or -1e9)))
    except Exception:  # noqa: BLE001
        funds = []

    # 4) 可判定性 / 口径
    if cov is None:
        judge = {"status": "unknown", "reason": "该策略未纳入净口径判定范围"}
    else:
        judge = {"status": cov.get("status"), "caliber": cov.get("caliber"),
                 "hits": cov.get("hits", 0), "reason": cov.get("reason"),
                 "scope": cov.get("scope")}

    # 5) 每只基金被多少条策略共同命中（共识度）—— 一次聚合，前端可按此排序/分级
    strat_counts: dict = {}
    try:
        for r in DB.conn.execute(
            "SELECT fund_code, COUNT(DISTINCT strategy_key) AS n "
            "FROM fund_strategy_signal GROUP BY fund_code"
        ):
            strat_counts[r["fund_code"]] = r["n"]
    except Exception:  # noqa: BLE001
        pass
    total_strategies = 0
    try:
        total_strategies = DB.conn.execute(
            "SELECT COUNT(DISTINCT strategy_key) FROM fund_strategy_signal"
        ).fetchone()[0]
    except Exception:  # noqa: BLE001
        total_strategies = 0
    for f in funds:
        f["n_strategies"] = strat_counts.get(f["fund_code"], 0)

    return {
        "available": True,
        "strategy": _fund_strat_brief(strat, key),
        "generated_at": meta.get("generated_at"),
        "pool_note": meta.get("pool_note"),
        "nav_basis": meta.get("nav_basis"),
        "nav_end": meta.get("nav_end"),
        "nav_bars": meta.get("nav_bars"),
        "sources": sources,
        "judge": judge,
        "funds": funds,
        "fund_count": len(funds),
        "n_strategies_total": total_strategies,
    }


def api_strategies() -> dict:
    """GET /api/strategies —— 策略实验室一次全量数据（设计文档 §2 决策 6）。"""
    # 1) 策略目录（唯一真源）；导入失败也不 500，降级为空目录。
    try:
        import strategy_data  # noqa: PLC0415 —— 惰性导入，避免循环依赖

        catalog = list(getattr(strategy_data, "STRATEGIES", []) or [])
    except Exception:  # noqa: BLE001
        catalog = []

    # 2) 窗口指标（唯一真源）；表缺失 → 全部按「无数据」处理（优雅降级）。
    win_rows: dict = {}
    try:
        for r in DB.conn.execute("SELECT * FROM strategy_sim_window").fetchall():
            win_rows[(r["strategy_key"], r["window_label"])] = r
    except Exception:  # noqa: BLE001
        win_rows = {}

    # 3) meta / caliber；表缺失时给默认口径横幅。
    meta: dict = {}
    try:
        mrow = DB.conn.execute("SELECT * FROM strategy_sim_meta WHERE id=1").fetchone()
        if mrow is not None:
            meta = {k: mrow[k] for k in mrow.keys()}
    except Exception:  # noqa: BLE001
        meta = {}

    caliber = _load_json_or_none(meta.get("caliber_json")) if meta else None
    if not isinstance(caliber, dict):
        caliber = {
            "title": "回测口径",
            "text": ("等权单标的满仓、信号次日收盘成交、不计手续费与滑点；"
                     "用于策略相对强弱比较，不是可实现收益预测。"),
            "fill": "next_close",
            "limit_up_filter": True,
            "bench": {"code": "000001", "name": "上证指数"},
            "bars_fetch": int(meta.get("bars_fetch") or _STRATEGY_WINDOWS[0]["bars"]),
        }
    # 2026-09-26 补充：年化口径为 244 交易日/年（引擎 _annualize 依据，界面须标注）。
    caliber["annual_basis"] = "244 交易日/年"

    try:
        import strategy_sim  # noqa: PLC0415

        min_trades = int(getattr(strategy_sim, "MIN_TRADES", 30))
    except Exception:  # noqa: BLE001
        min_trades = 30

    n_not_bt = 0
    n_scope = 0
    n_ranked = 0
    schools: list = []
    strategies: list = []

    for st in catalog:
        key = st.get("key")
        school = st.get("school")
        family = st.get("family")
        if school and school not in schools:
            schools.append(school)

        backtestable = bool(st.get("backtestable", True))
        scope_limited = bool(st.get("scope_limited"))
        if not backtestable:
            n_not_bt += 1
            status = "not_backtestable"
        elif scope_limited:
            n_scope += 1
            status = "scope_limited"
        else:
            status = "ok"

        is_switch = (school == _STRATEGY_SWITCH_SCHOOL) or (family in _STRATEGY_SWITCH_FAMILIES)
        item = {
            "key": key,
            "name": st.get("name"),
            "school": school,
            "family": family,
            "desc": st.get("desc"),
            "buy": st.get("buy"),
            "sell": st.get("sell"),
            "risk": st.get("risk"),
            "source": st.get("source"),
            "params": st.get("params"),
            "backtestable": backtestable,
            "needs": st.get("needs"),
            "scope_limited": scope_limited,
            "status": status,
            "status_note": None if status == "ok" else _strategy_status_note(st),
            "is_timing_switch": is_switch,
            "switch_kind": _strategy_switch_kind(family, school) if is_switch else None,
            "mid": None,
            "windows": None,
        }

        if status == "ok":
            mid_row = win_rows.get((key, "mid"))
            item["mid"] = _strategy_window_obj(mid_row)
            if mid_row is not None:
                n_ranked += 1
            side = [
                _strategy_side_window_obj(win_rows.get((key, "h1")), "近半年"),
                _strategy_side_window_obj(win_rows.get((key, "y1")), "近一年"),
            ]
            # 两窗口均无数据 → 整体置 None（前端按空态处理，不用 0 冒充）。
            item["windows"] = side if any(x is not None for x in side) else None

        strategies.append(item)

    return {
        "generated_at": meta.get("generated_at") or datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "caliber": caliber,
        "meta": {
            "total": len(catalog),
            "backtestable": len(catalog) - n_not_bt,
            "not_backtestable": n_not_bt,
            "scope_limited": n_scope,
            "ranked": n_ranked,
            "min_trades": min_trades,
            "windows": _STRATEGY_WINDOWS,
            "schools": schools,
            "board_note": ("标的池含沪深主板/创业板/科创板/北交所；"
                           "涨跌停阈值按板块（主板10%/创科20%/北交30%/ST5%）。"),
        },
        "strategies": strategies,
    }


def api_public_records() -> list[dict]:
    """公开年赛战绩榜（外部参照）。

    背景：用户要看的核心是「谁用多少钱做到了多少钱」。这是本库里**唯一**带真实收益的
    公开战绩（淘股吧官方年赛 / 淘实盘榜单），但它与 persons 表**零交集**（竞赛账号 vs 本库
    抓取的新浪/东财用户），所以单独成榜、不参与本库人物评分排名。

    口径：
    - 每个昵称取**代表条**，规则（2026-09-26 QA 复核后修正）：
      ① **优先取「可核验」的那一期**（source_kind='article' 且有 source_url）；
      ② 在同类里取 as_of_date 最新的一期；同日期并列取收益更高者（保证幂等）。
      修正原因：原来纯按日期取最新，导致榜首 ra1en 展示 09-24 的 1177.26（搜索快照、无链接、
      不可核验），却把他有原文可核验的 09-21 = 941.47 藏起来——方向反了。用户明确要求
      「能查到的标注来源」，故宁可展示可核验的较早值。该人**完全没有任何带链接的记录**时，
      才退回到快照里的最新一期。
    - 「截至日期」列显示该代表条真实的 as_of_date，让口径透明（不让人误以为是今天的数据）。
    - 按 total_return 降序（无收益的排最后）。
    - has_link：只有「有稳定原文链接且来源不是搜索引擎快照」时才为 True，前端据此决定是否渲染外链。
      绝不造一个假链接——用户明确要求「能查到的标注来源，查不到就留空」。

    健壮性：public_records 表不存在时返回空数组（优雅降级，不 500）。
    """
    try:
        rows = DB.conn.execute(
            "SELECT nickname, platform, group_name, total_return, as_of_date, source_url, source_kind "
            "FROM public_records"
        ).fetchall()
    except Exception:  # noqa: BLE001 —— 表不存在时降级为空榜
        return []
    best: dict[str, tuple] = {}
    for r in rows:
        d = dict(r)
        nick = d.get("nickname")
        if not nick:
            continue
        # 可核验 = 有原文链接且非搜索快照
        verifiable = 1 if (d.get("source_url") and d.get("source_kind") == "article") else 0
        # 排序键：(可核验优先, as_of 越新越好, 收益越高越好)；三者全是可比较的确定值 → 幂等
        ret = d.get("total_return") if d.get("total_return") is not None else -1e18
        key = (verifiable, d.get("as_of_date") or "", ret)
        prev = best.get(nick)
        if prev is None or key > prev[0]:
            best[nick] = (key, d)
    out = [v[1] for v in best.values()]
    out.sort(key=lambda x: (x.get("total_return") is None, -(x.get("total_return") or 0)))
    for d in out:
        d["has_link"] = bool(d.get("source_url")) and (d.get("source_kind") != "search_snapshot")
    return out


def api_sentiment() -> dict:
    dist = {"看多": 0, "看空": 0, "中性": 0}
    for r in DB.conn.execute("SELECT sentiment, COUNT(*) n FROM posts GROUP BY sentiment").fetchall():
        dist[r["sentiment"]] = r["n"]
    # 情绪地图：按股票聚合多空（同时统计提及总数，便于判断"多0 空0"是中性还是无数据）
    by_stock = {}
    for r in DB.conn.execute(
        """SELECT sm.symbol, sm.symbol_name, p.sentiment
           FROM stock_mentions sm JOIN posts p ON p.id = sm.post_id_ref"""
    ).fetchall():
        s = r["symbol"]
        d = by_stock.setdefault(s, {"bull": 0, "bear": 0, "neu": 0, "total": 0,
                                    "name": r["symbol_name"] or ""})
        d["total"] += 1
        if r["sentiment"] == "看多":
            d["bull"] += 1
        elif r["sentiment"] == "看空":
            d["bear"] += 1
        else:
            d["neu"] += 1
    map_list = []
    for s, v in by_stock.items():
        n = _named(s, v["name"])
        map_list.append({
            "symbol": s,
            "name": n["name"],
            "company": n["company"],
            "market": n["market"],
            "name_missing": n["name_missing"],
            "bull": v["bull"], "bear": v["bear"], "neutral": v["neu"], "mentions": v["total"],
        })
    map_list.sort(key=lambda x: (x["bull"] + x["bear"], x["mentions"]), reverse=True)
    named = sum(1 for x in map_list if not x["name_missing"])
    return {
        "distribution": dist,
        "map": map_list[:30],
        # 名称解析覆盖率：界面据此提示"是否有代码未识别"
        "name_covered": named,
        "name_total": len(map_list),
        "legend": {
            "code": "6 位数字为股票代码（中国内地交易所，沪市 60/68 开头，深市 00/30 开头，北交所 43/83/87 开头）",
            "name": "卡片标题为该股票的股票简称，副标题为公司全称",
            "bull": "「多 N」= 该股被 N 条看多情绪的帖子提及",
            "bear": "「空 N」= 该股被 N 条看空情绪的帖子提及",
            "neutral": "「中性 N」= 情绪词库未判出明确多空的提及条数",
        },
    }


def api_topics() -> list[dict]:
    rows = DB.conn.execute("SELECT topic, weight FROM topics ORDER BY weight DESC LIMIT 30").fetchall()
    return [{"topic": r["topic"], "weight": r["weight"]} for r in rows]


def api_hotspots() -> dict:
    """国内·近期热点：热词 → 板块影响 分层卡片（事实层 + 投研判断层）。

    事实层（帖数/人数/多空/涉及标的）从 app.db 实时计算；判断层来自 viewer.hot_lexicon。
    按「政策与行业主线 / 交易节奏与资金情绪 / 风险预警 / 其他热词」四层输出，层内按证据强度排序。
    """
    return build_hotspots(DB)


# ---------------- 海外舆情（模块一 / 模块二） ----------------
# 「近期新闻」的区域配额口径：先给每个区域保底条数，某区不足时余量让给其他区。
NEWS_REGION_ORDER = ("US", "HK", "GLOBAL")


def _overseas_recent_news(total: int = 40, per_region: int = 12) -> list[dict]:
    """近期新闻：按区域配额取样，避免单一区域（US 新闻量最大）垄断全部名额。

    2026-09-22 修复 P2-6：原实现是 `ORDER BY 区域优先级, heat DESC LIMIT 40`，
    而 US 源条目最多，结果 40 条全为 US，HK / GLOBAL 永远进不来，
    界面「近期新闻」看起来像只有美股。改为按区域配额取样。
    """
    cnt = {r["region"]: r["n"] for r in DB.conn.execute(
        "SELECT region, COUNT(*) n FROM overseas_news GROUP BY region").fetchall()}
    if not cnt:
        return []
    rest = sorted((r for r in cnt if r not in NEWS_REGION_ORDER), key=lambda r: -cnt[r])
    order = [r for r in NEWS_REGION_ORDER if r in cnt] + rest
    take = {r: min(per_region, cnt[r]) for r in order}
    # 配额之和可能超过 total（区域多时），也可能不足（某区条目少时）——多余让出、不足补齐
    left = total - sum(take.values())
    while left > 0:
        progressed = False
        for r in order:
            if left <= 0:
                break
            if take[r] < cnt[r]:
                take[r] += 1
                left -= 1
                progressed = True
        if not progressed:
            break
    out: list[dict] = []
    for r in order:
        if take.get(r, 0) <= 0:
            continue
        rows = DB.conn.execute(
            "SELECT id, source_name, region, kind, title, url, publish_time, topic, sentiment, heat, sectors "
            "FROM overseas_news WHERE region=? ORDER BY heat DESC LIMIT ?",
            (r, take[r])).fetchall()
        out.extend(dict(x) for x in rows)
    return out[:total]


def _overseas_cfg() -> dict:
    """读取 config/overseas_sources.yaml 的「国外视角优先」配置（区域权重 + 主题）。"""
    try:
        from overseas.sources import load_overseas_config
        return load_overseas_config()
    except Exception:
        return {"region_weights": {"US": 1.0, "HK": 0.85, "GLOBAL": 0.8, "CN": 0.45},
                "topics": {}}


def api_global_market() -> dict:
    """全球大盘 / 海外市场（「消息与外围」）：美股指数 + 美股行业板块 + 主要经济体 +
    港股 + 跨资产风向标。读 viewer/global_market.py 的本地缓存（国内免费源、免 cookie）。

    口径诚实边界：美股为真实指数；日、韩、德、英、法、中国台湾、印度、巴西等为
    **美股上市国家 ETF 代理**（字段 proxy=True，含汇率与美股情绪因素），
    方向可参考、点位不等同本地指数。详见 global_market.py 模块头。
    """
    try:
        return global_market.payload(auto=True)
    except Exception as e:  # noqa: BLE001
        log.warning("global_market 接口失败: %s", e)
        return {"ok": False, "error": str(e)[:160], "indices": [], "sectors": [],
                "themes": [], "cross_asset": [], "countries": [], "hk": [], "summary": {}}


def api_overseas_sentiment() -> dict:
    """海外舆情监控（模块一）：整体情绪、按区域分布、按日×区域情绪序列（趋势）、
    数据源构成、重点标的、近期新闻（国外源优先排序）。"""
    rows = DB.conn.execute(
        "SELECT sentiment, COUNT(*) n, SUM(heat) heat FROM overseas_news GROUP BY sentiment").fetchall()
    dist = {"看多": 0, "看空": 0, "中性": 0}
    heat_total = 0.0
    for r in rows:
        dist[r["sentiment"]] = r["n"]
        heat_total += float(r["heat"] or 0)
    total = sum(dist.values())
    net = round((dist["看多"] - dist["看空"]) / total * 100, 1) if total else 0.0

    reg: dict[str, dict] = {}
    for r in DB.conn.execute(
        "SELECT region, COUNT(*) n, SUM(heat) heat FROM overseas_news GROUP BY region").fetchall():
        reg[r["region"]] = {"count": r["n"], "heat": round(float(r["heat"] or 0), 2)}

    daily = [dict(r) for r in DB.conn.execute(
        "SELECT day,region,bull,bear,neutral,total,heat,mood "
        "FROM overseas_sentiment_daily ORDER BY day, region").fetchall()]

    src = [dict(r) for r in DB.conn.execute(
        "SELECT source_name, region, kind, COUNT(*) n, SUM(heat) heat "
        "FROM overseas_news GROUP BY source_name, region, kind ORDER BY n DESC").fetchall()]

    # 近期新闻：按区域配额取样（US / HK / GLOBAL 均需出现），避免单区垄断
    news = _overseas_recent_news()

    symbols = DB.overseas_symbols()
    cfg = _overseas_cfg()
    return {
        "distribution": dist, "net_mood": net, "heat_total": round(heat_total, 2), "total": total,
        "region_dist": reg, "daily": daily, "source_mix": src, "news": news,
        "symbols": symbols, "region_weights": cfg.get("region_weights") or {},
        "topics": list((cfg.get("topics") or {}).keys()),
        # 决策导向的再组织：他们在说什么 / 注意力投向哪 / 做了什么 / 对内陆的影响与建议
        "insight": _overseas_insight(),
    }


# ---------- 海外热点：标题中文化 + 驱动源清洗（展示层） ----------
# 背景（2026-09-26 用户反馈"卡片全是英文，看不懂"）：
#   非「topic:: 聚合」类的热点，早期因 lexicon.TRANSMISSION_TABLE 未配中文事件名，
#   hotspots 生成时按 `title = tr.get("title") or lead["title"]` 回落到**英文新闻原标题**
#   当卡片标题，中文界面下既读不懂、也看不出是什么事件。
#   这里在展示层统一转中文（库内原始数据不动、可追溯）：
#     ① key 命中 lexicon.EVENT_TITLE_ZH → 用中文事件名；
#     ② 未命中 → 用中文主题名兜底（topic 恒为中文）；
#     ③ 英文原文另存 title_en，前端悬停可核对。
_HAN_RE = re.compile(r"[\u4e00-\u9fff]")


def _event_title_zh() -> dict:
    """读取 lexicon 的中文事件名表；取不到则降级为空表（仍有中文主题兜底）。"""
    try:
        from overseas import lexicon as _lx
        return {str(k): str(v) for k, v in (_lx.EVENT_TITLE_ZH or {}).items()}
    except Exception:  # noqa: BLE001
        return {}


_EVENT_TITLE_ZH = _event_title_zh()


def _zh_hotspot_title(key, title, topic) -> tuple:
    """返回 (中文标题, 英文原文)。标题本身已是中文时，原文为空串。"""
    t = str(title or "").strip()
    if t and _HAN_RE.search(t):
        return t, ""
    zh = _EVENT_TITLE_ZH.get(str(key or "")) or ("%s · 国外驱动热点" % (topic or "海外"))
    return zh, t


def api_overseas_hotspots() -> list[dict]:
    """近期热点（模块二）：由国外新闻驱动，自动汇总的事件 + 提升性影响分析（传导路径 + 放大/衰减）。"""
    rows = DB.conn.execute("SELECT * FROM overseas_hotspots ORDER BY heat DESC").fetchall()
    out: list[dict] = []
    for r in rows:
        d = dict(r)
        # 结构化 JSON 字段（evidence 为历史字段，保留解析以兼容旧库）
        # sectors_up_detail / sectors_down_detail：按细分领域分组的代表公司
        #   [{"sector":"AI算力","symbols":[{"code":"300308","name":"中际旭创"}]}]
        for k in ("evidence", "sectors_up_detail", "sectors_down_detail"):
            try:
                d[k] = json.loads(d.get(k) or "[]")
            except Exception:
                d[k] = []
        for k in ("driver_sources", "driver_regions", "domestic_sectors", "related_markets",
                  "related_symbols", "variables", "sectors_up", "sectors_down"):
            v = d.get(k) or ""
            d[k] = [x for x in v.split(",") if x] if isinstance(v, str) else (v or [])
        # ① 驱动源清洗：「示例源·xx」是结构化示例（种子），并非真实接入源。
        #    旧库已把它写进 driver_sources（实测 15/17 条热点都混着），展示层再剔一次，
        #    否则卡片上会显示 20+ 家"驱动源"，其中一半是假的。
        d["driver_sources"] = [x for x in (d.get("driver_sources") or [])
                               if "示例源" not in str(x)]
        # ② 标题中文化（英文原文落到 title_en，供悬停核对）
        _zh, _en = _zh_hotspot_title(d.get("key"), d.get("title"), d.get("topic"))
        d["title"], d["title_en"] = _zh, _en
        out.append(d)
    return out


# ================= 国际局势（geo）：全球事件驱动 A 股影响研判 =================
def api_geo_events(category: str | None = None, days: int = 14, panel: str | None = None,
                   news_days: int = 10, news_limit: int = 40) -> dict:
    """国际局势面板数据：研判事件（人工+AI 联网调研，自带方向）+ 国际新闻流（只分类不给方向）。

    诚实口径（硬性）：
      · events 里的 direction / sectors / symbols 是**研判**，不是事实，标注"研判"；
      · 带 cross_check 的事件给出国内外信源交叉印证（一致/分歧/待观察）；
      · news 是 overseas_news 里筛出的国际类条目，**只做机器初判分类**，明确标"未研判"，
        不对其下方向结论（避免把抓取噪声当成判断抛给用户）。
    """
    geo_store.ensure_schema(DB.conn)
    events = geo_store.query_events(DB.conn, category=category, days=days, panel=panel, limit=200)
    news = geo_store.recent_intl_news(DB.conn, days=news_days, limit=news_limit)
    return {
        "stats": geo_store.stats(DB.conn),
        "events": events,
        "news": news,
        "meta": {
            "honesty": ("数据获取（2026-10-20 起自动/半自动）：本地域自动抓取+自动调度；海外舆情每日自动安全刷新（联网成功才写入、失败保留旧数据）；"
                        "国际局势事件库每周一/四 09:30 由 AI 联网调研更新后入库——日常无需手动操作，兜底入口在顶栏「一键刷新」。"
                        "研判事件含方向判断，为观点而非事实，所列个股仅作代表性观察标的、不构成投资建议；"
                        "带\"印证\"标签的事件为国内外信源交叉核对结果；下方新闻流仅做机器分类、未做方向研判。"),
        },
    }


def api_geo_refresh() -> dict:
    """一键刷新：幂等重导 seed_events.json（按 eid upsert）+ 重建国际新闻流视图。

    说明（诚实）：本接口不自动联网爬取——国际研判事件由人工+AI 联网调研沉淀进 seed_events.json，
    更新该文件后点此接口即可重新入库；新闻流直接重读 overseas_news 现成数据。
    真正的"联网增量调研"是离线步骤（WebSearch 服务端通道），沉淀进 seed 后由本接口落库。
    """
    geo_store.ensure_schema(DB.conn)
    imp = geo_store.import_seed(DB.conn)
    stats = geo_store.stats(DB.conn)
    return {
        "ok": True,
        "imported": imp,
        "stats": stats,
        "note": "已幂等重导 seed_events.json；新闻流为重读 overseas_news。如需新增国际研判事件，"
                "请更新 geo/seed_events.json 后再次点此刷新（联网调研为离线步骤）。",
    }


# ================= 外部参考（extref）：第三方判断信号的汇聚与回测 =================
# 隔离声明（硬性）：本组接口**只读写 extref_sources / extref_signals**，不与 posts/trades/
# players/lhb_stocks/geo_events 等任何既有表发生 JOIN 或数据交换；前端也单开一级分类，
# 不复用「实盘选手」「行情研判」的渲染结构。目的是让"别人的判断"与"本项目自采数据"永不混搭。
def api_extref_signals(kind: str | None = None, tier: str | None = None,
                       direction: str | None = None, hardness: str | None = None,
                       days: int | None = None, status: str | None = None) -> dict:
    """外部参考面板数据：信源档案 + 信号样本 + 信源胜率榜。

    诚实口径（硬性）：
      · signals 里 direction/reason 是**被引用方的观点**，不是本项目的判断，逐条附 claim 原文与 url；
      · hardness=soft 的条目明确标注为「软信号·须回测」，其转述内容（如席位金额）未经原始数据核对；
      · 胜率榜只统计 pnl_status='done' 的信号，样本 <5 条不给胜率（显示"样本不足"）；
      · 未回填的收益一律为 null，**绝不写 0**（否则会被读成"零收益"）。
    """
    extref_store.ensure_schema(DB.conn)
    signals = extref_store.query_signals(DB.conn, src_kind=kind, tier=tier,
                                         direction=direction, hardness=hardness,
                                         days=days, limit=300)
    sources = extref_store.query_sources(DB.conn, tier=tier, kind=kind, status=status, limit=300)

    # 2026-10-02 王总「软件荐股雷达看不懂 / 让你导数据没意义」重做：
    # 原来信源状态直接信 extref_sources.status 字段，那是个**写死的静态标注**（清一色「待接入」），
    # 不管你有没有真数据，界面永远显示"待接入"——王总点了导入，导完状态还是待接入，于是认为这是废功能。
    # 改成用 extref_signals 的**实际入库条数**反推状态：有信号才叫「在供数」。
    _rows = DB.conn.execute(
        "SELECT src_key, COUNT(*) AS n, SUM(pnl_status='done') AS d, MAX(pub_date) AS last "
        "FROM extref_signals GROUP BY src_key").fetchall()
    _stat = {r[0]: r for r in _rows}
    for _s in sources:
        _r = _stat.get(_s.get("src_key") or "")
        _sig_n = int(_r[1]) if _r else 0
        _done_n = int(_r[2] or 0) if _r else 0
        _s["sig_n"] = _sig_n
        _s["done_n"] = _done_n
        _s["last_date"] = (str(_r[3]) if _r and _r[3] else "") or ""
        # live=能出真实收益 / pending=有信号但收益窗口还没走完 / none=一条都没有，只是开通清单
        _s["radar_state"] = "none" if _sig_n <= 0 else ("pending" if _done_n <= 0 else "live")
        # 保留原静态标注，前端可对比（避免覆盖掉档案里的人工开通说明）
        _s["access_label_keep"] = _s.get("access_label", "")
    st = extref_store.stats(DB.conn)

    # 面板级一句话结论 + 三个数（前端只渲染，不自己算口径）
    _live = [s for s in sources if s.get("radar_state") in ("live", "pending")]
    _none = [s for s in sources if s.get("radar_state") == "none"]
    _sig_all = sum(s.get("sig_n", 0) for s in sources)
    _done_all = sum(s.get("done_n", 0) for s in sources)
    _live_nm = "、".join((s.get("name") or s.get("src_key")) for s in _live) or "无"
    radar = {
        "sources_total": len(sources),
        "sources_live": len(_live),
        "sources_none": len(_none),
        "signals_total": _sig_all,
        "signals_done": _done_all,
        "live_names": _live_nm,
    }
    radar["verdict"] = (
        f"这 {len(sources)} 个信源里，真正在供数的只有 {len(_live)} 个（{_live_nm}）："
        f"共 {_sig_all} 条信号，其中 {_done_all} 条已回填真实收益。"
        f"其余 {len(_none)} 个目前一条信号都没有——它们只是「开通条件清单」，"
        f"不产生任何判断，别把它们当数据源看。"
    )
    # 2026-10-02 方案 C：整页重排成「外部荐股验证报告」——王总原话「只报状态不给判断的东西，
    # 留着就是占地方」。report 是**只由已回填真实行情算出来的结论**（见 extref.store.signal_verdict），
    # 前端只负责渲染，不自己算口径。
    return {
        "stats": st,
        "sources": sources,
        "signals": signals,
        "rank": extref_store.signal_rank(DB.conn),
        "report": extref_store.signal_verdict(DB.conn),
        "radar": radar,
        "meta": {
            "honesty": ("本板块汇总的是**外部主体（持牌投顾机构 / 数据平台 / 公开榜单 / 社区 / 机构研报）的判断**，"
                        "与本项目自采数据完全隔离、不混用；所列个股仅为信号记录，不构成投资建议。"
                        "标注「软信号」的条目为观点转述，须回测后才可采信。"),
            "isolation": ("数据隔离：extref_* 两张表独立，不与帖子/实盘/龙虎榜/国际局势任何表联查。"),
            "red_line": st.get("red_line"),
        },
    }


def api_extref_refresh() -> dict:
    """一键刷新：幂等重导 extref 的 seed_sources.json + seed_signals.json。

    说明（诚实）：本接口**不自动联网爬取**——软件类信源需注册或开通 MCP 后才能取数，
    取得的数据由离线步骤（WebSearch 服务端通道 / 人工导出）沉淀进 seed_signals.json，
    更新该文件后点此接口即可重新入库。真正打通实时取数需要先开通信源档案里标注的通道。
    """
    extref_store.ensure_schema(DB.conn)
    imp_src = extref_store.import_sources(DB.conn)
    imp_sig = extref_store.import_signals(DB.conn)
    return {
        "ok": True,
        "imported": {"sources": imp_src, "signals": imp_sig},
        "stats": extref_store.stats(DB.conn),
        "note": "已幂等重导 extref/seed_sources.json 与 extref/seed_signals.json。"
                "软件类信源的实时取数需先开通对应通道（见信源档案的 access/status 字段）。",
    }


def api_freshness() -> dict:
    """P0-3（2026-10-15）：统一「数据保鲜状态」接口。

    返回各**非实时**数据域的「最后成功更新时间」，供前端渲染「🕓 最后更新 X · N 天前」
    陈旧角标（>3 天变灰）。计算全部走 MAX(数据时间列)，表/列不存在时优雅返回 None，
    不影响其它域。注意：一律取「数据发生时间列」（event_date / pub_date / as_of_date / day）
    而非「入库时间列」（updated_at）——否则离线重导会把角标刷成假新鲜。
    """
    def _max(sql: str):
        try:
            r = DB.conn.execute(sql).fetchone()
            return (r[0] if r else None) or None
        except Exception:
            return None

    now = datetime.now()

    def _days_ago(v):
        if not v:
            return None
        s = str(v)[:10]
        try:
            d = datetime.strptime(s, "%Y-%m-%d").date()
            return (now.date() - d).days
        except Exception:
            return None

    domains = {
        "geo": {
            "label": "国际局势 / 全球政策 / 科技前沿",
            "updated_at": _max("SELECT MAX(event_date) FROM geo_events"),
            "source": "geo/seed_events.json（离线重导，联网调研为离线步骤）",
            "realtime": False,
            "action": "geo_refresh",
            "autofill": False,
            "hint": "国际局势事件库已由 WorkBuddy 每周一/四 09:30 自动联网调研更新（本机只需开机联网）；点此可立即重导最新事件库。",
        },
        "extref": {
            "label": "外部荐股验证 / 社区共识 / 信源胜率榜",
            "updated_at": _max("SELECT MAX(pub_date) FROM extref_signals"),
            "source": "extref/seed_signals.json（离线重导）",
            "realtime": False,
        },
        "public_records": {
            "label": "公开实盘战绩",
            "updated_at": _max("SELECT MAX(as_of_date) FROM public_records"),
            "source": "public_records_seed.json（离线重导）",
            "realtime": False,
        },
        "overseas_sentiment": {
            "label": "美股港股舆情",
            "updated_at": _max("SELECT MAX(day) FROM overseas_sentiment_daily"),
            "source": "overseas 采集（手动「刷新海外舆情(联网)」触发，现每日自动重试）",
            "realtime": False,
            "action": "overseas_collect",
            "autofill": True,
            "hint": "联网采集依赖 VPN；点击将重新抓取（联网不通会自动保留旧数据）。",
        },
        "overseas_hotspots": {
            "label": "国外驱动热点",
            "updated_at": _max("SELECT MAX(updated_at) FROM overseas_hotspots"),
            "source": "overseas 采集（手动「刷新海外舆情(联网)」触发，现每日自动重试）",
            "realtime": False,
            "action": "overseas_collect",
            "autofill": True,
            "hint": "联网采集依赖 VPN；点击将重新抓取（联网不通会自动保留旧数据）。",
        },
    }
    for k, v in domains.items():
        v["days_ago"] = _days_ago(v.get("updated_at"))
        v["stale"] = bool(v.get("days_ago") is not None and v["days_ago"] > 3)
    return {"domains": domains, "generated_at": now.strftime("%Y-%m-%d %H:%M:%S")}


_EXTREF_WEEK = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")


def _extref_pnl_expect(sig: dict) -> str:
    """一句话讲清该信号的 T+1/T+5/T+10 为什么是「—」、什么时候会有。

    2026-09-28 新增（王总反馈「外部信号里看不到 T+1/T+5/T+10」）：
    只做**本地日期推算**——不联网、不猜交易日历，只陈述可确定的事实（发布日星期几、
    基准日如何顺延、还差几个交易日），**不编造具体回填日期**。前端原样展示。
    """
    st = (sig.get("pnl_status") or "pending")
    pub = str(sig.get("pub_date") or "").strip()

    def _fmt(v):
        return "—" if v is None else ("%+.2f%%" % float(v))

    if st == "done":
        return ("已按口径回填（发布日收盘为基准、前复权日 K）：T+1 %s · T+5 %s · T+10 %s。"
                % (_fmt(sig.get("pnl_t1")), _fmt(sig.get("pnl_t5")), _fmt(sig.get("pnl_t10"))))
    if st == "na":
        return ("该代码在腾讯 / 新浪两个日 K 源都取不到行情（退市 / 代码无效 / 长期停牌），"
                "记为 na，不写 0；系统会定期自动重试，恢复行情后自动补回。")
    # 部分回填（2026-09-30）：T+1 已到、T+5/T+10 未到 —— 状态仍 pending，但已有数字。
    # 不区分的话会显示"待回填"却带着收益，口径自相矛盾。
    got = [(k, v) for k, v in (("T+1", sig.get("pnl_t1")), ("T+5", sig.get("pnl_t5")),
                               ("T+10", sig.get("pnl_t10"))) if v is not None]
    miss = [k for k, v in (("T+1", sig.get("pnl_t1")), ("T+5", sig.get("pnl_t5")),
                           ("T+10", sig.get("pnl_t10"))) if v is None]
    if got:
        return ("已部分回填：%s；%s 尚未到期（窗口未走完，不写 0）。到期后自动补齐。"
                % ("、".join("%s %s" % (k, _fmt(v)) for k, v in got), "、".join(miss)))
    parts = []
    try:
        dt = datetime.strptime(pub, "%Y-%m-%d")
    except Exception:
        dt = None
    if dt is not None:
        wd = _EXTREF_WEEK[dt.weekday()]
        if dt.weekday() >= 5:
            parts.append("发布日 %s（%s，非交易日）" % (pub, wd))
            parts.append("基准顺延至其后第一个交易日的收盘")
        else:
            parts.append("发布日 %s（%s）" % (pub, wd))
            parts.append("基准取发布日当天收盘")
    parts.append("T+1 / T+5 / T+10 分别要再走完 1 / 5 / 10 个交易日，未走完前一律为空（不写 0）")
    parts.append("到期后会自动回填（打开本面板、采集、运行回测任一动作都会触发）")
    return "；".join(parts) + "。"


def api_extref_signal_detail(sid: str) -> dict:
    """外部信号「详情抽屉」数据（2026-09-27 依王总要求新增：点开能看简介/收益/分析）。

    组成与口径（诚实 + 隔离）：
      · signal —— extref_signals 单表记录（**外部主体的观点**，非本项目判断）；
      · source —— 所属信源档案（含 role 作用 / value 价值 / howto 接入路径）；
      · community_ok —— 该信号 src_kind 是否属社区/榜单；
      · self_ref —— **本项目自采参照**：同代码的持仓/讨论摘要。
        这是**展示层的跨模块组合**（王总 2026-09-27 明确要求"显示但分区标注"）：
        extref 表仍零 JOIN / 零外键，前端把 self_ref 单独分区并标注来源，不与外部信号混排。
    公司简介 / 行情 / K 线属中性市场事实，由前端另调既有 /api/quote、/api/kline，
    本接口不重复网络逻辑，保证抽屉首屏快。
    """
    extref_store.ensure_schema(DB.conn)
    sig = extref_store.get_signal(DB.conn, sid)
    if not sig:
        return {"error": "信号不存在：%s" % sid}
    src = extref_store.get_source(DB.conn, sig.get("src_key") or "")
    sym = (sig.get("symbol") or "").strip()
    self_ref = None
    if len(sym) == 6 and sym.isdigit():
        try:
            s = api_stock(sym)
            self_ref = {
                "symbol": sym,
                "symbol_name": s.get("symbol_name") or "",
                "position_count": s.get("position_count", 0),
                "holders": s.get("holders", 0),
                "trade_count": s.get("trade_count", 0),
                "mention_count": s.get("mention_count", 0),
                "positions": [{"player": p.get("player"), "ratio": p.get("ratio"),
                               "hold_since": (p.get("hold_since") or "")[:10],
                               "buy_price": p.get("buy_price"), "current_price": p.get("current_price")}
                              for p in (s.get("positions") or [])][:30],
                "mentions": [{"author": m.get("author") or "匿名", "site": m.get("site") or "",
                              "post_time": (m.get("post_time") or "")[:16],
                              "text": (m.get("text") or "")[:180]}
                             for m in (s.get("mentions") or [])][:10],
            }
        except Exception as e:
            self_ref = {"error": "%s: %s" % (type(e).__name__, e)}
    return {
        "signal": sig,
        "source": src,
        "self_ref": self_ref,
        "pnl_expect": _extref_pnl_expect(sig),
        "meta": {
            "honesty": ("signal 的 direction/reason 是**被引用方的观点**，非本项目判断；"
                        "未回填的收益为 null，不写 0。"),
            "isolation": ("self_ref 为「本项目自采参照」，展示层单独分区标注，"
                          "extref_* 两张表本身不与任何既有表联查。"),
        },
    }


def api_extref_source_detail(src_key: str) -> dict:
    """信源「详情抽屉」数据：档案（含作用/价值/接入路径）+ 已入库信号清单 + 聚合。"""
    extref_store.ensure_schema(DB.conn)
    src = extref_store.get_source(DB.conn, src_key)
    if not src:
        return {"error": "信源不存在：%s" % src_key}
    sigs = extref_store.signals_of_source(DB.conn, src_key, limit=200)
    done = sum(1 for s in sigs if (s.get("pnl_status") or "") == "done")
    pend = sum(1 for s in sigs if (s.get("pnl_status") or "") == "pending")
    return {
        "source": src,
        "signals": sigs,
        "count": len(sigs),
        "done_n": done,
        "pending_n": pend,
    }


def api_extref_collect(src_key: str | None = None, page_size: int = 20) -> dict:
    """运行公开榜单采集器（当前已接入：东财股吧人气榜）。写 extref_signals（按 sid 幂等）。"""
    with _WRITE_LOCK:
        extref_store.ensure_schema(DB.conn)
        res = extref_collect.collect(DB.conn, src_key=src_key, page_size=page_size)
        # 2026-09-28 新增：采集完顺手把「窗口已走完」的信号回填 T+1/5/10。
        # 王总反馈「外部信号里 T+1/T+5/T+10 没有数据」——根因不是没算，是**没人触发回填**：
        # 采集（拿数据）与回测（算收益）原先是两个独立按钮，只点前者就永远看不到收益。
        bt_res = {}
        try:
            bt_res = extref_backtest.backtest_all(DB.conn)
            bt_res["detail"] = (bt_res.get("detail") or [])[:20]
        except Exception as e:
            bt_res = {"error": "%s: %s" % (type(e).__name__, e)}
        return {
            "ok": True,
            "result": res,
            "backtest": bt_res,
            "stats": extref_store.stats(DB.conn),
            "note": ("公开榜单已入库（幂等）。人气 = 关注度、非方向，故信号方向一律记「中性」；"
                     "雪球有 WAF 拦截、淘股吧/龙虎榜待接入。"),
        }


def api_extref_backtest() -> dict:
    """运行自动回测器：给窗口已走完的 pending 信号回填 t1/t5/t10。写 extref_signals。

    2026-09-29：改后台单飞执行（理由见 _spawn_extref_backtest）——本接口此前在请求内
    同步跑网络回测，会把全局写锁占住几分钟到几十分钟，拖死全部写接口。
    """
    extref_store.ensure_schema(DB.conn)
    bt = _spawn_extref_backtest()
    return {
        "ok": True,
        "backtest": bt,
        "stats": extref_store.stats(DB.conn),
        "rank": extref_store.signal_rank(DB.conn),
        "note": ("口径：以发布日收盘为基准、前复权日K、tN =（第 N 个交易日收盘 / 基准 − 1）。"
                 "窗口未走完的保持 pending；取不到行情的记 na，不写 0。"
                 + (("；" + bt.get("note", "")) if isinstance(bt, dict) else "")),
    }


# 2026-09-29：extref 回测后台单飞。回测是纯网络密集（每条 pending 一次日 K），
# 绝不能在 HTTP 请求内同步跑（会占死全局写锁，把所有写接口一起拖死）。
_BACKTEST_RUNNING = threading.Lock()


def _spawn_extref_backtest() -> dict:
    """后台线程跑 backtest_all（单飞：已在跑则跳过本次并如实告知）。立即返回，不阻塞调用方。"""
    if not _BACKTEST_RUNNING.acquire(blocking=False):
        return {"async": True, "started": False,
                "note": "上一轮回测仍在后台进行，本次未重复启动；完成后刷新「信源胜率榜」即可看到 t1/t5/t10。"}
    def _run() -> None:
        try:
            with _WRITE_LOCK:
                extref_backtest.backtest_all(DB.conn)
        except Exception:  # noqa: BLE001 —— 后台任务失败只记日志，不影响已入库结果
            logging.exception("extref 后台回测失败")
        finally:
            _BACKTEST_RUNNING.release()
    threading.Thread(target=_run, name="extref-backtest", daemon=True).start()
    return {"async": True, "started": True,
            "note": "回测已在后台执行（不阻塞入库）；完成后刷新「信源胜率榜」查看 t1/t5/t10。"}


def api_extref_manual_import(src_key: str, text: str, default_date: str | None = None,
                             is_csv: bool | None = None, run_backtest: bool = True) -> dict:
    """人工导入荐股名单（Path A：把「荐股 App 复制/导出的名单」结构化入库）。

    流程：解析（CSV / 自由文本，自动识别代码/名称/日期/方向/理由）→ 幂等入库 → 自动回测 → 返回逐行报告。
    诚实口径：方向缺失默认记「中性」、日期缺失默认按今天，均在逐行报告显式标注；缺失名称尽力在线补全。
    隔离纪律（硬性）：只写 extref_signals（经 store.upsert_signal）；只读 extref_sources 取档案；
    不碰 posts/trades/players/lhb_stocks/geo_events 任何表，零 JOIN / 零外键。
    """
    with _WRITE_LOCK:
        extref_store.ensure_schema(DB.conn)
        if not src_key:
            return {"ok": False, "error": "缺少 src_key（请先在列表中选择信源）"}
        if not text or not text.strip():
            return {"ok": False, "error": "缺少名单文本（请把荐股名单粘贴到文本框）"}
        from extref import import_manual  # noqa: E402  —— 延迟导入，避免启动期耦合
        rep = import_manual.import_text(DB.conn, src_key, text,
                                        default_date=default_date, is_csv=is_csv)
        if not rep.get("ok"):
            return rep
        bt = None
        if run_backtest:
            # 2026-09-29 修复「点了没反应」：backtest_all 会对每条 pending 信号打日 K 网络
            # （41 条 × 最坏 12s×3 次重试 ≈ 可跑几十分钟），此前在请求内同步执行，且 do_POST
            # 全局写锁包住整个 POST → 本请求挂起、后续所有写请求全部排队，前端表现为永久
            # 「入库中…」。现改为：入库即返回，回测放后台单飞线程（同一时刻最多一个）。
            bt = _spawn_extref_backtest()
        return {
            "ok": True,
            "report": rep,
            "backtest": bt,
            "stats": extref_store.stats(DB.conn),
            "rank": extref_store.signal_rank(DB.conn),
            "note": ("已入库（按 sid 幂等，重导不重复增）。人工导入信号 hardness=soft（外部观点，须回测后采信）；"
                     "方向缺失默认「中性」、日期缺失默认今天，均已在逐行报告标注；缺失名称已尽力在线补全。"),
        }



def api_extref_mcp_connect(src_key: str, payload: dict | None = None,
                           run_backtest: bool = True, api_key: str | None = None) -> dict:
    """B 档数据接口（MCP / 合规 API）接通（桥接适配层）。

    接收已通过 MCP / 官方接口拿到的**结构化荐股 JSON**（payload），按各信源字段映射
    解析 → store.upsert_signal 幂等入库 → backtest_all 回填。
    若仅传 api_key（无 payload）：尝试用配置 endpoint 主动拉取（仅 http+mcp 源有效）。
    诚实口径：未配置密钥/endpoint 且无 payload 时返回 {ok:False}，零入库；绝不写假数据。
    隔离纪律：只写 extref_signals（经 store.upsert_signal）；不碰任何既有业务表。
    """
    with _WRITE_LOCK:
        extref_store.ensure_schema(DB.conn)
        if not src_key:
            return {"ok": False, "error": "缺少 src_key"}
        from extref import connect_mcp  # noqa: E402
        if src_key not in connect_mcp.MCP_SOURCES:
            return {"ok": False, "error": "非 B 档 MCP 信源：%s" % src_key}
        injected = {src_key: {"api_key": api_key}} if api_key else None
        # 2026-09-29：回测与人工导入同口径改后台单飞（理由见 _spawn_extref_backtest），
        # connect() 本身只做解析入库（快），不再在请求内同步跑网络回测。
        rep = connect_mcp.connect(DB.conn, src_key, data=payload, run_backtest=False,
                                  keys=injected)
        if not rep.get("ok"):
            return rep
        bt = _spawn_extref_backtest() if run_backtest else None
        return {
            "ok": True,
            "report": rep,
            "backtest": bt,
            "stats": extref_store.stats(DB.conn),
            "rank": extref_store.signal_rank(DB.conn),
            "note": ("已通过桥接适配层入库（按 sid 幂等，重导不重复增）。MCP 协议握手需在 MCP 客户端"
                     "（WorkBuddy/Trae/Cursor）完成；本项目接收其返回 JSON 入库并自动回测。"
                     "数据接口信号 hardness=hard（官方接口，可信度高于 soft）。"),
        }


# ================= 自选 / 关注池 + 到价提醒（2026-09-27 新增，隔离模块 watchlib） =================
# 设计纪律（与 extref 同）：独立三张表 watch_groups / watch_items / watch_alerts，
# 零 JOIN、零外键，不与任何既有业务表混用；写入一律经 _WRITE_LOCK 串行化。
# 诚实口径：取价失败一律 None（前端显示 «—»），绝不写 0；无价 / 无目标价 / 无方向即不触发。
# 取价放在写锁**之外**（网络 I/O 不占写锁），只在落库时短暂持锁。

def _sina_symbol(code: str) -> str:
    """裸 6 位代码 → 新浪行情符号（sh/sz/bj 前缀），与 sector_chain.batch_quotes 同款映射。"""
    c = str(code).strip()
    if c[:1] in ("6", "5", "9"):
        return "sh" + c
    if c[:1] == "4" or c[:2] in ("83", "87", "92"):
        return "bj" + c
    return "sz" + c


def _sina_batch_quotes(codes: list, deadline: float = 0.0,
                       timeout: int = 5, retries: int = 0) -> dict:
    """新浪实时批量行情（hq.sinajs.cn）：返回 {裸代码: {"price", "pct", "name"}}。

    单请求覆盖最多 80 只缺口，绝不逐只阻塞；超时受 deadline 约束。
    复用 sector_chain.batch_quotes 的同款解析（gbk 解码 + hq_str_ 正则）。
    该源与东财相互独立——东财 push2 被本机风控掐连接时，新浪仍稳定可用。"""
    out: dict = {}
    todo = [c for c in dict.fromkeys(codes) if c]
    if not todo:
        return out
    for i in range(0, len(todo), 80):
        if deadline and time.time() >= deadline:
            break
        chunk = todo[i:i + 80]
        url = "https://hq.sinajs.cn/list=" + ",".join(_sina_symbol(c) for c in chunk)
        try:
            txt = _get_text(url, "https://finance.sina.com.cn/",
                            timeout=timeout, retries=retries, encoding="gbk")
        except Exception:  # noqa: BLE001
            txt = None
        if not txt:
            continue
        for line in txt.splitlines():
            m = re.match(r'var hq_str_([a-z]{2})(\d{6})="(.*)";', line.strip())
            if not m:
                continue
            code, body = m.group(2), m.group(3)
            f = body.split(",")
            if len(f) < 32 or not f[0]:
                continue
            try:
                prev = float(f[2]); price = float(f[3])
            except (ValueError, IndexError):
                price, prev = None, None
            # ⚠️ 停牌 / 未开盘 / 当日无成交时，新浪把当前价回成 0.000（实测 2026-10-09：*ST康佳A 停牌）。
            #    0 不是行情 —— 若当成有效价，会算出 pct=(0-昨收)/昨收 = -100%，
            #    界面就成了「价格归零、跌幅 100%」这种凭空捏造、且与事实相反的结论。
            #    统一按「取不到」处理：前端显 «—»，且不参与到价告警判定（见 _has_price）。
            if price is None or price <= 0:
                price = None
            pct = round((price - prev) / prev * 100, 2) if (price and prev and prev > 0) else None
            out[code] = {"name": f[0], "price": price, "pct": pct}
    return out


def _has_price(q) -> bool:
    """取价结果是否含**有效**价：缺失 / 0 / 负价一律视为「没取到」。

    0 价的来源是停牌、未开盘、当日无成交（新浪会对这类标的回 0.000）；
    把它当有效价会连锁出三个错：界面显示「0.00 / -100%」、到价告警被 0<=目标价 误触发、
    last_price 落成 0 污染库。故所有取价消费点（缺口判定、结果采用、缓存）统一走本判据。"""
    try:
        return float((q or {}).get("price") or 0) > 0
    except (TypeError, ValueError):
        return False


def _em_ulist_prices(secids: list, timeout: int = 3) -> dict:
    """东财 push2 ulist 批量快照 → {裸代码: {"price","pct","name"}}（停牌 price=None）。"""
    out: dict = {}
    if not secids:
        return out
    url = ("https://push2.eastmoney.com/api/qt/ulist.np/get?secids=" + ",".join(secids)
           + "&fields=f12,f13,f14,f2,f3&fltt=2&invt=2&ut=" + _EM_UT)
    j = _em_get_json(url, "https://quote.eastmoney.com/", timeout=timeout, retries=0)
    rows = (((j or {}).get("data") or {}).get("diff")) or []
    for r in rows:
        code = str(r.get("f12") or "").strip()
        if not code:
            continue
        _px = _num(r.get("f2"))
        if _px is None or _px <= 0:        # 停牌/无行情：东财 f2 回 "-" → _num=None
            _px, _pct = None, None
        else:
            _pct = _num(r.get("f3"))
        out[code] = {"price": _px, "pct": _pct, "name": r.get("f14") or ""}
    return out


def _tencent_batch_prices(norm: list, timeout: int = 3) -> dict:
    """腾讯**单请求**批量快照 → {裸代码: {"price","pct","name"}}。

    ⚠️ 腾讯对停牌 / 当日零成交的票会把「当前价」填成昨收、成交量填 0（看着像「今天平盘没动」，
    实测 2026-10-09 000016：price=昨收、volume=0）。故除有效价外还看成交量：
    量 0 = 当日无成交 → price 记 None（前端显 «—»，不参与告警判定）。"""
    out: dict = {}
    if not norm:
        return out
    syms, codemap = [], {}
    for c in norm:
        m, code, _ = _em_secid(c)
        sym = _tx_prefix(code, m) + code
        syms.append(sym)
        codemap[sym] = code
    txt = _get_text("https://qt.gtimg.cn/q=" + ",".join(syms), "https://gu.qq.com/",
                    timeout=timeout, retries=0, encoding="gbk")
    if not txt:
        return out
    for line in txt.splitlines():
        line = line.strip()
        if not line.startswith("v_") or "=" not in line:
            continue
        head, body = line.split("=", 1)
        code = codemap.get(head[2:].strip())
        if not code:
            continue
        parts = body.strip().strip(";").strip('"').split("~")
        if len(parts) < 46:
            continue

        def g(k):
            i = _TX_FIELD[k]
            return parts[i] if i < len(parts) else ""

        price, vol = _num(g("price")), _num(g("volume"))
        nm = parts[1] if len(parts) > 1 else ""
        if price is None or price <= 0 or (vol is not None and vol <= 0):
            out[code] = {"price": None, "pct": None, "name": nm}
        else:
            out[code] = {"price": price, "pct": _num(g("pct")), "name": nm}
    return out


def _watch_prices(codes: list, budget: float = 9.0) -> dict:
    """批量取最新价：新浪 / 腾讯 / 东财 **三源并发**，谁先回用谁（2026-10-09 改造）。

    ⚠️ 旧实现是「东财 ulist → 缺口切新浪 → 仍缺口逐只腾讯」的**串行降级**：
    东财被本机风控掐连接时，按 5s 级超时逐级阻塞，慢源把 /api/watch_list 拖到十几秒
    （王总：点哪刷哪、绝不 hang 住）。现改为三源**同时发**（emapi.race_merge），
    先回的先采纳、慢源到点即丢，整体硬上限 min(budget, 3s)。

    - `_merge` 有值不回退：已有有效价时，不被后续源的缺失值覆盖；
    - `_complete` 判据 = 全部代码都拿到有效价 → 立即收工，绝不多等一秒；
    - 停牌 / 全源无值：等三源都回或到 3s 上限即返回（price=None，前端显 «—»）。
    返回 {裸代码: {"price": float|None, "pct": float|None, "name": str|None}}。"""
    out: dict = {}
    norm: list = []
    secids: list = []
    for c in dict.fromkeys([str(x).strip() for x in (codes or []) if str(x).strip()]):
        _m, code, secid = _em_secid(c)
        if code:
            norm.append(code)
            secids.append(secid)
    if not norm:
        return out

    race_budget = min(float(budget or 3.0), 3.0)
    norm_set = set(norm)

    def _merge(acc: dict, result: dict) -> None:
        for c, q in (result or {}).items():
            if c not in acc or (_has_price(q) and not _has_price(acc.get(c))):
                acc[c] = q

    def _complete(acc: dict) -> bool:
        return all(_has_price(acc.get(c)) for c in norm_set)

    calls = [
        lambda: _em_ulist_prices(secids, timeout=3),
        lambda: _sina_batch_quotes(norm, deadline=time.time() + race_budget),
        lambda: _tencent_batch_prices(norm, timeout=3),
    ]
    return emapi.race_merge(calls, _merge, _complete, timeout=race_budget)


def api_watch_list(force=False) -> dict:
    """自选池全量：分组 + 标的（附最新价 / 涨跌 / 关注天数 / 距目标价 / 加入至今涨幅）+ 未读数。"""
    if not force:
        cached = _api_cache_get("watch_list")
        if cached is not None:
            return cached
    with _WRITE_LOCK:
        watchlib.ensure_schema(DB.conn)
        watchlib.seed_default_groups(DB.conn)
        groups = watchlib.list_groups(DB.conn)
        items = watchlib.list_items(DB.conn)
        unread = watchlib.count_unread(DB.conn)
    prices: dict = {}
    if items:
        try:
            prices = _watch_prices([it["symbol"] for it in items])
        except Exception:  # noqa: BLE001 —— 取价失败不拖垮列表，价格留空（前端显 «—»）
            logging.warning("自选池取价失败", exc_info=True)
    for it in items:
        p = prices.get(it["symbol"]) or {}
        price, tgt, pap = p.get("price"), it.get("target_price"), it.get("price_at_add")
        it["price"] = price
        it["pct"] = p.get("pct")
        if not it.get("name"):
            it["name"] = p.get("name") or ""
        it["to_target_pct"] = round((tgt / price - 1) * 100, 2) if (price and tgt) else None
        it["since_add_pct"] = round((price / pap - 1) * 100, 2) if (price and pap) else None
    # —— 关注池列整改（2026-10-12 进哥需求）：注入「入手值得率 / 博主提及 / 战法信号」三组数据 ——
    # 纪律：三个数据源各自独立 try/except，任一失败只吞异常、缺失即 None/[]，绝不拖垮整个列表。
    symbols = [it["symbol"] for it in items]

    # 1) 入手值得率：复用 screener 评分机器，返回 {code: 0~100 或 None}。按整批取（函数内部按交易日缓存）。
    worth_map: dict = {}
    try:
        worth_map = screener._worth_scores(symbols) or {}
    except Exception:  # noqa: BLE001 —— 评分失败不阻塞列表
        logging.warning("关注池入手值得率评分失败", exc_info=True)

    # 2) 博主提及：一次性取出 blogger_overlap，建 {code:(n_bloggers, [names...])} 映射；bloggers 字段容错解析。
    blogger_map: dict = {}
    try:
        rows = DB.conn.execute(
            "SELECT stock_code, n_bloggers, bloggers FROM blogger_overlap").fetchall()
        for r in rows:
            code = r["stock_code"]
            if not code:
                continue
            n = r["n_bloggers"]
            raw = r["bloggers"]
            names: list = []
            if raw:
                try:
                    parsed = json.loads(raw)
                    if isinstance(parsed, list):
                        names = [str(x) for x in parsed if x not in (None, "")]
                    elif isinstance(parsed, str):
                        # 「、」为中文列举号（真实落库数据多用它），「,」为 ASCII 逗号，两者皆兼容。
                        names = [s.strip() for s in re.split(r"[、,]", parsed) if s.strip()]
                except Exception:  # noqa: BLE001 —— JSON 解析失败则按「、或,」分隔兜底
                    names = [s.strip() for s in re.split(r"[、,]", str(raw)) if s.strip()]
            blogger_map[code] = (n, names)
    except Exception:  # noqa: BLE001 —— 读博主表失败不阻塞列表
        logging.warning("关注池博主提及读取失败", exc_info=True)

    # 3) 战法信号：为池内票单独复算（2026-10-14 修复匹配缺陷——此前只匹配 result 缓存
    #    Top200 截断榜，关注池只命中 1~5 条的票全被挡在榜外，界面战法列全显 «—»）。
    #    口径与全市场扫描同一份 build_signals，基于最新一根日K；signal_date 随接口
    #    下发到前端标注数据日期（盘中最后一根未收盘，信号以收盘复核为准）。
    strat_map: dict = {}
    signal_date = None
    try:
        sig = bili_scan.signals_for_watch(symbols)
        strat_map = sig.get("by_code") or {}
        signal_date = sig.get("signal_date")
    except Exception:  # noqa: BLE001 —— 战法复算失败不阻塞列表（缺失即 «—»）
        logging.warning("关注池战法信号复算失败", exc_info=True)

    for it in items:
        sym = it["symbol"]
        it["worth"] = worth_map.get(sym)
        b = blogger_map.get(sym)
        it["blogger_n"] = (b[0] if b else None)
        it["blogger_list"] = (b[1] if b else [])
        it["strat_hits"] = strat_map.get(sym, [])
    result = {"ok": True, "groups": groups, "items": items, "alerts_unread": unread,
              "checked_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
              "signal_date": signal_date}
    _api_cache_put("watch_list", result, 300)
    return result


def api_watch_item_add(payload: dict) -> dict:
    """加入自选池。名称 / 加入价缺失时用一次轻量取价补全（不阻断，失败留空）。"""
    sym = watchlib._norm_symbol(payload.get("symbol"))
    if not sym:
        return {"ok": False, "error": "缺少股票 / 基金代码"}
    prices: dict = {}
    try:
        prices = _watch_prices([sym])
    except Exception:  # noqa: BLE001
        pass
    q = prices.get(sym) or {}
    name = (payload.get("name") or "").strip() or q.get("name") or ""
    price_at_add = payload.get("price_at_add")
    if price_at_add is None:
        price_at_add = q.get("price")
    with _WRITE_LOCK:
        watchlib.ensure_schema(DB.conn)
        watchlib.seed_default_groups(DB.conn)
        res = watchlib.add_item(
            DB.conn, sym, name=name, kind=payload.get("kind") or "stock",
            group_id=payload.get("group_id"),
            groups=payload.get("groups") if isinstance(payload.get("groups"), list) else None,
            sector=payload.get("sector") or "",
            note=payload.get("note") or "", attention=payload.get("attention", 3),
            target_price=payload.get("target_price"), alert_dir=payload.get("alert_dir") or "",
            alert_on=payload.get("alert_on", 1), price_at_add=price_at_add)
        if res.get("ok"):
            watchlib.check_alerts(DB.conn, prices)     # 加入即刻判一次
    return res


def api_watch_item_update(payload: dict) -> dict:
    iid = payload.get("id")
    if not iid:
        return {"ok": False, "error": "缺少 id"}
    fields = {k: payload[k] for k in ("name", "kind", "group_id", "sector", "note",
                                      "attention", "target_price", "alert_dir", "alert_on")
              if k in payload}
    groups = payload.get("groups")
    with _WRITE_LOCK:
        watchlib.ensure_schema(DB.conn)
        res = watchlib.update_item(DB.conn, int(iid), **fields)
        # 多对多分组（2026-10-14）：groups = 全量设置（编辑弹窗多选）；空数组 → 回退默认组
        if res.get("ok") and isinstance(groups, list):
            gres = watchlib.set_groups(DB.conn, int(iid), groups)
            if not gres.get("ok"):
                return gres
        return res


def api_watch_item_reorder(payload: dict) -> dict:
    """组内排序：{group_id, ids:[...]} → 按给定顺序重写该组 sort_order（10,20,30…）。"""
    gid = payload.get("group_id")
    ids = payload.get("ids")
    if gid is None or not isinstance(ids, list):
        return {"ok": False, "error": "缺少 group_id / ids"}
    with _WRITE_LOCK:
        watchlib.ensure_schema(DB.conn)
        return watchlib.reorder_in_group(DB.conn, int(gid), ids)


def api_watch_item_group_remove(payload: dict) -> dict:
    """移出单个分组：{id, group_id} → 标的仍在池中（其他组保留；无剩余组回退默认组）。"""
    iid = payload.get("id")
    gid = payload.get("group_id")
    if not iid or gid is None:
        return {"ok": False, "error": "缺少 id / group_id"}
    with _WRITE_LOCK:
        watchlib.ensure_schema(DB.conn)
        return watchlib.remove_from_group(DB.conn, int(iid), int(gid))


def api_watch_item_remove(payload: dict) -> dict:
    iid = payload.get("id")
    if not iid:
        return {"ok": False, "error": "缺少 id"}
    with _WRITE_LOCK:
        watchlib.ensure_schema(DB.conn)
        return watchlib.remove_item(DB.conn, int(iid))


def api_watch_group_add(payload: dict) -> dict:
    with _WRITE_LOCK:
        watchlib.ensure_schema(DB.conn)
        return watchlib.add_group(DB.conn, payload.get("name") or "",
                                  kind=payload.get("kind") or "mix", color=payload.get("color") or "")


def api_watch_group_rename(payload: dict) -> dict:
    gid = payload.get("id")
    if not gid:
        return {"ok": False, "error": "缺少 id"}
    with _WRITE_LOCK:
        watchlib.ensure_schema(DB.conn)
        return watchlib.rename_group(DB.conn, int(gid), payload.get("name") or "")


def api_watch_group_remove(payload: dict) -> dict:
    gid = payload.get("id")
    if not gid:
        return {"ok": False, "error": "缺少 id"}
    rt = payload.get("reassign_to")
    with _WRITE_LOCK:
        watchlib.ensure_schema(DB.conn)
        return watchlib.remove_group(DB.conn, int(gid),
                                     reassign_to=(int(rt) if str(rt).lstrip("-").isdigit() else None))


def api_watch_group_reorder(payload: dict) -> dict:
    """分组排序（2026-10-14 进哥：分组管理弹窗 ↑/↓ 调序）。ids = 完整分组 id 列表（新顺序）。"""
    ids = payload.get("ids")
    if not isinstance(ids, list) or not ids:
        return {"ok": False, "error": "缺少 ids"}
    with _WRITE_LOCK:
        watchlib.ensure_schema(DB.conn)
        return watchlib.reorder_groups(DB.conn, ids)


def api_watch_check() -> dict:
    """到价检测：取最新价 → 命中规则则生成未读告警（同标的同方向同目标价不重复）。"""
    with _WRITE_LOCK:
        watchlib.ensure_schema(DB.conn)
        codes = sorted({it["symbol"] for it in watchlib.list_items(DB.conn)})
    prices: dict = {}
    if codes:
        try:
            prices = _watch_prices(codes)
        except Exception:  # noqa: BLE001
            logging.warning("自选池到价检测取价失败", exc_info=True)
    with _WRITE_LOCK:
        res = watchlib.check_alerts(DB.conn, prices)
        res["unread"] = watchlib.count_unread(DB.conn)
    res["ok"] = True
    return res


# ================= 博主荐股 / 当红参赛股（2026-10-01 新增）=================
# 数据来自博主数据查看台的静态产物（源头为人工维护的 Excel + tools/ 同步脚本），
# 由 tools/import_blogger_contest.py 幂等灌进本库。隔离纪律：本组接口**只读写
# blogger_* / contest_* 六张表**，不碰 posts/trades/prediction_* 等既有业务表。
#
# 口径诚实（硬性）：
#   · blogger_reco 里的「推荐以来涨跌幅」来自源表，是**荐股发布后的区间收益**，
#     不是本项目算的；接口原样透出，前端标注口径，不改写数字。
#   · contest_track 的逐日 change_pct 缺失时一律为 null，**绝不写 0**
#     （写 0 会被读成「当天没动」，与「没数据」是两回事）。
#   · 参赛者「空仓」这类也如实保留排名，不剔除、不补 0。
_BLOGGER_SORT_SQL = {
    "date": "rec_date DESC, seq ASC",                 # 推荐日期 新→旧（默认）
    "ret": "(ret_since IS NULL), ret_since DESC",     # 推荐以来涨幅 高→低（空值永远沉底）
    "code": "stock_code ASC",
}


def _blogger_payload_base() -> dict:
    r = DB.conn.execute("SELECT MAX(src_updated) AS u FROM blogger_reco").fetchone()
    return {"updated": (r["u"] if r else None)}


# 来源标注迁移（2026-10-11，王总要求 B站荐股博主与存量区分、且「推下周」的前瞻型单独可识别）：
#   blogger_reco 新增 3 列（幂等，缺了才加）：
#     · source  TEXT ''=存量（独股九剑/抖音跟踪）  'B站'=B站来源
#     · horizon TEXT ''=一般分享  'next_week'=明确「推下周」前瞻（够格对标当红参赛股语义）
#     · bvid    TEXT ''=无  B站来源视频 bvid（可回溯源视频）
# 注意：抖音每日自动化写 reco 用显式列名 INSERT，加默认列不影响其写入。
_BLOGGER_COLS_READY = False


def _blogger_ensure_columns() -> None:
    """blogger_reco 来源标注列的幂等迁移；首次 blogger 读接口调用时执行一次。"""
    global _BLOGGER_COLS_READY
    if _BLOGGER_COLS_READY:
        return
    try:
        have = {r["name"] for r in DB.conn.execute("PRAGMA table_info(blogger_reco)")}
        if have:  # 表已建立才迁移；表尚未建立时由导入脚本按新 DDL 建
            for col, ddl in (
                ("source", "ALTER TABLE blogger_reco ADD COLUMN source TEXT NOT NULL DEFAULT ''"),
                ("horizon", "ALTER TABLE blogger_reco ADD COLUMN horizon TEXT NOT NULL DEFAULT ''"),
                ("bvid", "ALTER TABLE blogger_reco ADD COLUMN bvid TEXT NOT NULL DEFAULT ''"),
            ):
                if col not in have:
                    DB.conn.execute(ddl)
            DB.conn.commit()
        _BLOGGER_COLS_READY = True
    except sqlite3.OperationalError as e:  # P2-02：仅「列已存在」类错误可忽略，其余照抛
        msg = str(e).lower()
        if "duplicate column" in msg or "already exists" in msg:
            pass
        else:
            raise
    except Exception:  # noqa: BLE001 —— 其它异常不阻塞读接口，按旧列集继续
        logging.exception("[blogger迁移] source/horizon/bvid 列检查失败")


# 读取时自愈（2026-10-09）：汇总表(blogger_stats/blogger_overlap)一旦比 blogger_reco 陈旧
# （抖音每日自动化直接写 reco，不经 Excel 导入），就在**读接口里同步重算一次**，
# 保证「跟日常任务的数据更新同步」，不用等下次 Excel 导入。
#   · 走 _WRITE_LOCK（与其它写接口同一把锁，避免抢 SQLite 写锁）；
#   · 模块级节流：同一张表最多每 60 秒重算一次（刷页面不会反复算）；
#   · 任何异常都吞掉 —— 自愈失败就返回旧数据照常出，绝不 500；
#   · blogger_perf 不做此自愈（要抓行情、太慢），维持手动/每日重算。
_BLOGGER_HEAL_MIN_INTERVAL = 60.0                 # 秒；每张表节流窗口
_BLOGGER_HEAL_LAST = {"stats": 0.0, "overlap": 0.0}   # 上次成功重算的时刻（time.time）


def _blogger_selfheal(kind: str) -> None:
    """kind ∈ {'stats','overlap'}：blogger_<kind> 比 blogger_reco 陈旧则同步重算一次。"""
    try:
        stale = (blogger_rebuild.is_stats_stale(DB.conn) if kind == "stats"
                 else blogger_rebuild.is_overlap_stale(DB.conn))
        if not stale:
            return
        now = time.time()
        if now - _BLOGGER_HEAL_LAST.get(kind, 0.0) < _BLOGGER_HEAL_MIN_INTERVAL:
            return                                     # 节流窗口内：先返回旧数据
        with _WRITE_LOCK:
            if kind == "stats":
                blogger_rebuild.rebuild_stats(DB.conn)
            else:
                blogger_rebuild.rebuild_overlap(DB.conn)
        _BLOGGER_HEAL_LAST[kind] = now
        logging.info("[blogger自愈] %s 汇总表陈旧，读取时已重算（reco 有新数据）", kind)
    except Exception:                                  # noqa: BLE001 —— 自愈失败不能拖垮接口
        logging.exception("[blogger自愈] %s 重算失败，已回退旧数据", kind)


def api_blogger_facets() -> dict:
    """博主荐股筛选项：博主名单（跟踪名单 ∪ 有数据的博主）+ 各博主条数。

    刻意**不硬编码博主名**（前端筛选 chip 由本接口驱动）——以后加/删博主只改名单表，前端不用跟着改。

    2026-10-09 对齐（王总要求「显示全部在跟踪的博主，无数据的标『待采集』」）：
      · 名单 = blogger_tracklist（抖音每日自动化跟踪的 21 位，见 tools/sync_blogger_tracklist.py）
               **∪** blogger_reco 里已有数据的博主；
      · 名单里的博主即使暂无落库荐股，也照样出现 → n=0、pending=true，前端标注「待采集」；
      · 有数据但已不在名单的（例如「A股化债人」）→ 仍然出现（pending=false），
        绝不因为「不在跟踪名单」就把有数据的博主藏起来。
    · kind / contest 来自名单表（缺省 null / false）；n / last_date / pending 来自 blogger_reco。
    · total 语义保持不变 = blogger_reco 总行数（**不是**并集条数）。
    """
    out = _blogger_payload_base()
    _blogger_ensure_columns()

    # 1) reco 里各博主的条数 + 最近推荐日期 + 来源（source 取该博主任一非空来源）
    reco: dict[str, dict] = {}
    for r in DB.conn.execute(
            "SELECT blogger, COUNT(*) AS n, MAX(rec_date) AS last_date, "
            "MAX(CASE WHEN source != '' THEN source ELSE NULL END) AS src "
            "FROM blogger_reco GROUP BY blogger"):
        reco[r["blogger"]] = {"n": r["n"], "last_date": r["last_date"], "source": r["src"] or ""}

    # 2) 跟踪名单（表可能尚未建立 / 尚未同步 → 稳健降级为空集，接口不因此报错）
    track: dict[str, dict] = {}
    try:
        for r in DB.conn.execute("SELECT blogger, kind, contest FROM blogger_tracklist"):
            track[r["blogger"]] = {"kind": r["kind"], "contest": r["contest"]}
    except Exception:  # noqa: BLE001 —— 名单表缺失不影响「有数据的博主」照常出
        track = {}

    # 3) 并集
    bloggers = []
    for name in set(reco) | set(track):
        n = (reco.get(name) or {}).get("n", 0)
        bloggers.append({
            "name": name,
            "n": n,
            "last_date": (reco.get(name) or {}).get("last_date"),
            "pending": n == 0,
            "kind": (track.get(name) or {}).get("kind"),
            "contest": bool((track.get(name) or {}).get("contest")),
            "source": (reco.get(name) or {}).get("source", ""),  # ''=存量来源 'B站'=B站来源
        })

    # 4) 排序：有数据的在前（按 n 降序），待采集的在后（按 name 升序）
    bloggers.sort(key=lambda b: (b["pending"], (-b["n"]) if not b["pending"] else 0, b["name"]))
    out["bloggers"] = bloggers
    out["tracked_total"] = len(track)
    # 「并集里有数据者」= 所有 non-pending 的博主（= 在 blogger_reco 里有行的博主数）。
    # 注意：某博主可能「不在跟踪名单、但有数据」（如 A股化债人）——它也算有数据者。
    out["tracked_with_data"] = sum(1 for b in bloggers if not b["pending"])

    total = DB.conn.execute("SELECT COUNT(*) AS n FROM blogger_reco").fetchone()
    out["total"] = total["n"] if total else 0
    return out


def api_blogger_reco(blogger: str | None = None, q: str | None = None,
                     sort: str = "date", limit: int = 300) -> dict:
    """博主荐股明细：可按博主筛选 / 按名称·代码搜索 / 按日期或涨幅排序。"""
    out = _blogger_payload_base()
    _blogger_ensure_columns()
    where, args = [], []
    if blogger:
        where.append("blogger = ?")
        args.append(blogger)
    if q:
        like = "%" + q + "%"
        where.append("(stock_name LIKE ? OR stock_code LIKE ? OR video_title LIKE ?)")
        args += [like, like, like]
    wsql = (" WHERE " + " AND ".join(where)) if where else ""
    tot = DB.conn.execute("SELECT COUNT(*) AS n FROM blogger_reco" + wsql, args).fetchone()
    out["total"] = tot["n"] if tot else 0
    order = _BLOGGER_SORT_SQL.get(sort, _BLOGGER_SORT_SQL["date"])
    lim = max(1, min(int(limit or 300), 2000))
    rows = DB.conn.execute(
        "SELECT seq, rec_date, stock_name, stock_code, price_at_rec, high_1w, close_latest,"
        " high_hist, drop_from_high, ret_since, video_title, note, blogger,"
        " source, horizon, bvid "
        "FROM blogger_reco" + wsql + " ORDER BY " + order + " LIMIT " + str(lim),
        args).fetchall()
    out["rows"] = [dict(r) for r in rows]
    out["sort"] = sort if sort in _BLOGGER_SORT_SQL else "date"
    return out


# ==================== 豆包整理的抖音博主语料（2026-10-20 进哥需求） ====================
# 数据源：荐股追踪查看台（博主数据查看台）豆包日更产出的两个**新域** JSON，
# 由 tools/import_douyin_corpus.py 幂等灌进 douyin_corpus_*（零 JOIN 既有业务表）。
# 注意：内容取自抖音博主视频，但由**豆包**逐条转写/翻译整理，故前端标注统一用「豆包整理」。
#   · sentiment = 豆包舆情（32 博主 / 69 视频）
#   · tech      = 豆包技术方法（37 博主 / 2701 视频，ASR 摘要）
# 面板落点（进哥 2026-10-20 拍板）：
#   · 情绪 → 塞进现有 p-sentiment（舆论情绪）**顶部独立框**「豆包舆情」；
#   · 技术 → 新建 p-tech 面板（选项 A），顶部独立框「豆包技术方法」。
# 之所以两者都做成「独立边框 + 来源徽标」，是因为它们与原有内容**异构**：
# 原有情绪是多空计数（论坛帖子），原有技术是 B站 markdown 轨道；豆包这两域是
# 「视频级语料（博主 + 标题 + 摘要）」，混排会让口径打架，必须靠边框和来源标签分开。
_DOUYIN_LABEL = {"sentiment": "豆包舆情", "tech": "豆包技术方法"}
_DOUYIN_BRIEF_LEN = 180


def _douyin_ready() -> bool:
    """表是否已由 loader 建好。未建时不抛错，让面板显示可执行的提示。"""
    try:
        DB.conn.execute("SELECT 1 FROM douyin_corpus_video LIMIT 1").fetchone()
        return True
    except Exception:  # noqa: BLE001 —— OperationalError: no such table
        return False


def _douyin_meta(domain: str) -> dict:
    try:
        r = DB.conn.execute(
            "SELECT src_updated, n_blogger, n_video, imported_at FROM douyin_corpus_meta"
            " WHERE domain = ?", (domain,)).fetchone()
        return dict(r) if r else {}
    except Exception:  # noqa: BLE001
        return {}


def api_douyin_facets(domain: str = "tech") -> dict:
    """某个域的面板头部数据：数据截至 / 规模 / 博主目录 / 分类分布 / 日期跨度。

    `bloggers` 以**视频表实际作者**为准并集档案表：豆包产出里存在「有视频、无档案」的
    作者（实测 sentiment 的 BRTV天下财经、tech 的 斌哥Bg），若只按档案表出目录，
    这些人会在筛选 chip 里凭空消失、他们的视频再也筛不出来。
    """
    domain = domain if domain in _DOUYIN_LABEL else "tech"
    out = {"ok": True, "domain": domain, "label": _DOUYIN_LABEL[domain],
           "src_updated": None, "n_blogger": 0, "n_video": 0,
           "bloggers": [], "categories": [], "date_range": [None, None],
           "no_date": 0, "with_summary": 0, "note": ""}
    if not _douyin_ready():
        out["ok"] = False
        out["note"] = "抖音语料表尚未建立：请先执行 python tools/import_douyin_corpus.py"
        return out

    m = _douyin_meta(domain)
    out["src_updated"] = m.get("src_updated")
    out["imported_at"] = m.get("imported_at")

    prof = {}
    for r in DB.conn.execute(
            "SELECT name, douyin_id, followers, intro, tags_json, n_video"
            " FROM douyin_corpus_blogger WHERE domain = ?", (domain,)):
        prof[r["name"]] = dict(r)

    agg = {}
    for r in DB.conn.execute(
            "SELECT author, COUNT(*) AS n, MAX(publish_date) AS last_date"
            " FROM douyin_corpus_video WHERE domain = ? GROUP BY author", (domain,)):
        agg[r["author"]] = {"n": r["n"], "last_date": r["last_date"]}

    bloggers = []
    for name in set(agg) | set(prof):
        p = prof.get(name) or {}
        a = agg.get(name) or {}
        tags = []
        if p.get("tags_json"):
            try:
                tags = json.loads(p["tags_json"]) or []
            except Exception:  # noqa: BLE001
                tags = []
        bloggers.append({
            "name": name,
            "n": a.get("n", 0),
            "last_date": a.get("last_date"),
            "pending": a.get("n", 0) == 0,
            "douyin_id": p.get("douyin_id"),
            "followers": p.get("followers"),
            "intro": p.get("intro"),
            "tags": tags,
        })
    bloggers.sort(key=lambda b: (b["n"] == 0, -b["n"], b["name"]))
    out["bloggers"] = bloggers

    out["categories"] = [
        {"name": r["cat"] or "未分类", "n": r["n"]}
        for r in DB.conn.execute(
            "SELECT COALESCE(NULLIF(category,''),'未分类') AS cat, COUNT(*) AS n"
            " FROM douyin_corpus_video WHERE domain = ? GROUP BY cat ORDER BY n DESC", (domain,))]
    out["categories"] = [c for c in out["categories"] if c["name"] != "未分类"] + \
                        [c for c in out["categories"] if c["name"] == "未分类"]

    r = DB.conn.execute(
        "SELECT COUNT(*) AS n, MIN(publish_date) AS dmin, MAX(publish_date) AS dmax,"
        " SUM(CASE WHEN publish_date IS NULL THEN 1 ELSE 0 END) AS nod,"
        " SUM(CASE WHEN summary IS NOT NULL AND summary <> '' THEN 1 ELSE 0 END) AS ws"
        " FROM douyin_corpus_video WHERE domain = ?", (domain,)).fetchone()
    if r:
        out["n_video"] = r["n"] or 0
        out["date_range"] = [r["dmin"], r["dmax"]]
        out["no_date"] = r["nod"] or 0
        out["with_summary"] = r["ws"] or 0
    out["n_blogger"] = len([b for b in bloggers if b["n"] > 0])
    return out


_DOUYIN_SORTS = {
    "date": "(publish_date IS NULL), publish_date DESC, video_id DESC",
    "old": "(publish_date IS NULL), publish_date ASC, video_id ASC",
    "author": "author ASC, (publish_date IS NULL), publish_date DESC",
}


def api_douyin_list(domain: str = "tech", q: str | None = None, blogger: str | None = None,
                    category: str | None = None, topic: str | None = None,
                    stance: str | None = None, offset: int = 0, limit: int = 30,
                    sort: str = "date") -> dict:
    """视频语料分页列表（后端分页 —— tech 域 2701 条绝不能一次性塞给浏览器）。

    列表**不返回完整摘要**（单条最长数千字，2701 条全量可达数 MB），只回
    `summary_brief`（前 180 字）与 `has_summary`；点开单条再走 /api/douyin/detail 取全文。
    """
    domain = domain if domain in _DOUYIN_LABEL else "tech"
    out = {"ok": True, "domain": domain, "label": _DOUYIN_LABEL[domain],
           "total": 0, "offset": 0, "limit": limit, "has_more": False, "rows": [], "note": ""}
    if not _douyin_ready():
        out["ok"] = False
        out["note"] = "抖音语料表尚未建立：请先执行 python tools/import_douyin_corpus.py"
        return out

    where, args = ["domain = ?"], [domain]
    if blogger:
        where.append("author = ?")
        args.append(blogger)
    if category:
        if category in ("__none__", "未分类"):
            where.append("(category IS NULL OR category = '')")
        else:
            where.append("category = ?")
            args.append(category)
    if topic:
        where.append("topic = ?")
        args.append(topic)
    if stance:
        where.append("stance = ?")
        args.append(stance)
    if q:
        kw = "%" + q.strip() + "%"
        where.append("(title LIKE ? OR summary LIKE ? OR author LIKE ?)")
        args += [kw, kw, kw]
    wsql = " WHERE " + " AND ".join(where)

    tot = DB.conn.execute("SELECT COUNT(*) AS n FROM douyin_corpus_video" + wsql, args).fetchone()
    total = tot["n"] if tot else 0
    lim = max(1, min(int(limit or 30), 200))
    off = max(0, int(offset or 0))
    order = _DOUYIN_SORTS.get(sort, _DOUYIN_SORTS["date"])
    rows = DB.conn.execute(
        "SELECT video_id, title, publish_date, author, video_url, category, summary,"
        " source_of_summary FROM douyin_corpus_video" + wsql +
        " ORDER BY " + order + " LIMIT ? OFFSET ?", args + [lim, off]).fetchall()

    items = []
    for r in rows:
        s = r["summary"] or ""
        items.append({
            "video_id": r["video_id"],
            "title": r["title"] or "",
            "publish_date": r["publish_date"],
            "author": r["author"],
            "video_url": r["video_url"],
            "category": r["category"],
            "summary_brief": (s[:_DOUYIN_BRIEF_LEN] + "…") if len(s) > _DOUYIN_BRIEF_LEN else s,
            "summary_len": len(s),
            "has_summary": bool(s),
            "source_of_summary": r["source_of_summary"],
        })
    out.update({"total": total, "offset": off, "limit": lim, "rows": items,
                "has_more": off + len(items) < total})
    return out


def api_douyin_detail(video_id: str) -> dict:
    """单条视频完整摘要（列表只给 brief，点开才取全文）。"""
    out = {"ok": False, "video_id": video_id, "row": None, "note": ""}
    if not video_id:
        out["note"] = "缺少 video_id"
        return out
    if not _douyin_ready():
        out["note"] = "抖音语料表尚未建立"
        return out
    r = DB.conn.execute(
        "SELECT video_id, domain, title, publish_date, author, video_url, category,"
        " summary, source_of_summary, src_updated FROM douyin_corpus_video WHERE video_id = ?",
        (video_id,)).fetchone()
    if not r:
        out["note"] = "未找到该视频"
        return out
    out["ok"] = True
    out["row"] = dict(r)
    out["label"] = _DOUYIN_LABEL.get(r["domain"], r["domain"])
    return out


def api_douyin_overview() -> dict:
    """两域概览（供前端首屏/面板头部一次拿全，避免两次请求）。"""
    out = {"ok": _douyin_ready(), "domains": {}}
    for d in ("sentiment", "tech"):
        f = api_douyin_facets(d)
        out["domains"][d] = {
            "label": f["label"], "src_updated": f.get("src_updated"),
            "n_blogger": f.get("n_blogger", 0), "n_video": f.get("n_video", 0),
            "no_date": f.get("no_date", 0), "with_summary": f.get("with_summary", 0),
            "date_range": f.get("date_range", [None, None]),
        }
    if not out["ok"]:
        out["note"] = "抖音语料表尚未建立：请先执行 python tools/import_douyin_corpus.py"
    return out


import json  # noqa: F401
# ==================== 豆包语料 · 整合层（2026-10-20 进哥：分域整合、产出高维价值） ====================
# 下列接口把"散料"升华为可学习结构：技术域→主题轨道、情绪域→观点类型、
# 博主分域画像、板块多维联动（化学反应）。严格分域，不与 B站/论坛既有内容混算。

_TRACK_DESC = {
    "趋势与龙头": "主升/连板/龙头/赛道/机构抱团——怎么选强、跟强",
    "量化与程序化": "QMT/PTrade/因子/回测/网格——用代码把纪律自动化",
    "仓位与风控": "仓位/止损/回撤/资金管理——活下来比赚得快重要",
    "K线与形态": "涨停/十字星/分型/中枢/出货形态——图形语言",
    "量价与盘口": "量价/盘口/分时/做T/支撑压力——供需的即时博弈",
    "均线指标": "均线/MACD/BOLL/指标——趋势的量化刻度",
    "估值与基本面": "估值/财报/价值/护城河——公司的内在锚",
    "交易认知": "心法/体系/思维/散户vs机构——决定上限的认知层",
    "缠论": "缠论/中枢/背驰/走势——结构化的市场几何",
    "综合与其他": "暂未归入上述轨道的综合技法",
}
_STANCE_DESC = {
    "看多": "明确偏多/看机会的观点",
    "看空": "明确偏空/提示调整的观点",
    "中性研判": "不喊方向、只做框架/事实研判",
    "风险提示": "提示风险/避雷/退潮的观点",
    "板块机会": "指向具体板块/主线/题材的方向性观点",
}


def api_douyin_topics() -> dict:
    """技术域 → 9 条学习方法论轨道聚合（视频数/代表博主/样例）。"""
    out = {"ok": _douyin_ready(), "tracks": []}
    if not out["ok"]:
        out["note"] = "抖音语料表尚未建立：请先执行 python tools/import_douyin_corpus.py"
        return out
    agg = {}
    for r in DB.conn.execute(
        "SELECT topic, author, title FROM douyin_corpus_video WHERE domain='tech' AND topic IS NOT NULL"):
        t, a, ti = r[0], r[1], r[2] or ""
        d = agg.setdefault(t, {"n": 0, "bloggers": {}, "titles": []})
        d["n"] += 1
        d["bloggers"][a] = d["bloggers"].get(a, 0) + 1
        if len(d["titles"]) < 4:
            d["titles"].append(ti[:40])
    tracks = []
    for t, d in sorted(agg.items(), key=lambda x: -x[1]["n"]):
        top = sorted(d["bloggers"].items(), key=lambda x: -x[1])[:5]
        tracks.append({"track": t, "desc": _TRACK_DESC.get(t, ""), "n": d["n"],
                       "top_bloggers": [{"name": b, "n": n} for b, n in top],
                       "samples": d["titles"]})
    out["tracks"] = tracks
    return out


def api_douyin_stances() -> dict:
    """情绪域 → 5 类观点类型聚合（数/样例）。"""
    out = {"ok": _douyin_ready(), "stances": []}
    if not out["ok"]:
        out["note"] = "抖音语料表尚未建立：请先执行 python tools/import_douyin_corpus.py"
        return out
    agg = {}
    for r in DB.conn.execute(
        "SELECT stance, author, title FROM douyin_corpus_video WHERE domain='sentiment' AND stance IS NOT NULL"):
        st, a, ti = r[0], r[1], r[2] or ""
        d = agg.setdefault(st, {"n": 0, "items": []})
        d["n"] += 1
        if len(d["items"]) < 6:
            d["items"].append({"blogger": a, "title": ti[:40]})
    out["stances"] = [{"stance": st, "desc": _STANCE_DESC.get(st, ""), "n": d["n"], "samples": d["items"]}
                      for st, d in sorted(agg.items(), key=lambda x: -x[1]["n"])]
    return out


def api_douyin_bloggers() -> dict:
    """博主分域画像：技术派 / 观点派两套群像，各含 tags + 主题(观点)分布。"""
    out = {"ok": _douyin_ready(), "tech": [], "sentiment": []}
    if not out["ok"]:
        out["note"] = "抖音语料表尚未建立：请先执行 python tools/import_douyin_corpus.py"
        return out
    for dom, key, dist_col in (("tech", "tech", "topic"), ("sentiment", "sentiment", "stance")):
        bloggers = {}
        for r in DB.conn.execute(
            "SELECT name, douyin_id, followers, intro, tags_json, n_video"
            " FROM douyin_corpus_blogger WHERE domain=?", (dom,)):
            name = r[0]
            bloggers[name] = {"name": name, "douyin_id": r[1], "followers": r[2], "intro": r[3] or "",
                              "tags": json.loads(r[4]) if r[4] else [], "n_video": r[5] or 0, "dist": {}}
        for r in DB.conn.execute(
            f"SELECT author, {dist_col}, COUNT(*) FROM douyin_corpus_video"
            f" WHERE domain=? AND {dist_col} IS NOT NULL GROUP BY author, {dist_col}", (dom,)):
            if r[0] in bloggers:
                bloggers[r[0]]["dist"][r[1]] = r[2]
        out[key] = sorted(bloggers.values(), key=lambda x: -x["n_video"])
    return out


def api_douyin_plate(plate: str = "") -> dict:
    """板块多维联动（化学反应核心）：同一板块 → 技术战法 + 情绪观点 + 涉及博主。"""
    out = {"ok": bool(plate), "plate": plate, "tech": None, "sentiment": None, "bloggers": []}
    if not plate:
        out["note"] = "缺少 plate 参数"
        return out
    vids = [r[0] for r in DB.conn.execute(
        "SELECT video_id FROM douyin_corpus_plate WHERE plate=?", (plate,)).fetchall()]
    if not vids:
        out["note"] = f"板块「{plate}」暂无关联语料"
        return out
    ph = ",".join("?" * len(vids))
    tech = {"n": 0, "tracks": {}, "bloggers": {}, "samples": []}
    sent = {"n": 0, "stances": {}, "bloggers": {}, "samples": []}
    for r in DB.conn.execute(
        f"SELECT domain, author, title, topic, stance FROM douyin_corpus_video WHERE video_id IN ({ph})", vids):
        dom, a, ti, tp, st = r[0], r[1], (r[2] or "")[:40], r[3], r[4]
        if dom == "tech":
            tech["n"] += 1
            tech["tracks"][tp] = tech["tracks"].get(tp, 0) + 1
            tech["bloggers"][a] = tech["bloggers"].get(a, 0) + 1
            if len(tech["samples"]) < 6:
                tech["samples"].append({"blogger": a, "title": ti})
        else:
            sent["n"] += 1
            sent["stances"][st] = sent["stances"].get(st, 0) + 1
            sent["bloggers"][a] = sent["bloggers"].get(a, 0) + 1
            if len(sent["samples"]) < 6:
                sent["samples"].append({"blogger": a, "title": ti})
    tech["tracks"] = sorted(tech["tracks"].items(), key=lambda x: -x[1])
    tech["top_bloggers"] = sorted(tech["bloggers"].items(), key=lambda x: -x[1])[:8]
    sent["stances"] = sorted(sent["stances"].items(), key=lambda x: -x[1])
    sent["top_bloggers"] = sorted(sent["bloggers"].items(), key=lambda x: -x[1])[:8]
    out["tech"] = tech
    out["sentiment"] = sent
    out["bloggers"] = sorted(set(list(tech["bloggers"].keys()) + list(sent["bloggers"].keys())))
    return out


_resonance_cache = {"ts": 0.0, "payload": None}


def api_douyin_resonance(limit: int = 40) -> dict:
    """豆包语料 x 主力合成层 共振引擎（打通孤岛的化学反应）。

    从 douyin_corpus_video 的标题+摘要抽取点名个股 -> 关联 res_stock_card（合成研究卡）
    / blogger_overlap（多博主共振）/ sig_unified（六源统一信号）-> 排序产出高可信共振标的；
    并把「豆包在讲、主力合成层还没覆盖」的票作为自进化盲区(gaps)回吐，供 evo 层学习。
    结果缓存 300s（豆包语料按日更新），首次调用约 10~20s 抽取，之后命中缓存。
    """
    import time
    from datetime import datetime as _dt
    now = time.time()
    global _resonance_cache
    if _resonance_cache.get("payload") and now - _resonance_cache["ts"] < 300:
        p = dict(_resonance_cache["payload"]); p["cached"] = True
        return p

    rows = DB.conn.execute(
        "SELECT code, name FROM res_stock WHERE name IS NOT NULL AND length(name)>=3").fetchall()
    name2code, code2name = {}, {}
    for code, name in rows:
        name2code.setdefault(name, code)
        code2name.setdefault(code, name)
    names = sorted(name2code.keys(), key=len, reverse=True)

    def extract(text):
        found = set()
        for nm in names:
            if nm in text:
                found.add(nm)
        return {n for n in found if not any(n != m and n in m for m in found)}

    hits = {}
    for r in DB.conn.execute(
            "SELECT video_id, domain, title, summary, author, stance, topic FROM douyin_corpus_video"):
        vid, dom, title, summary, author, stance, topic = r[0], r[1], r[2], r[3], r[4], r[5], r[6]
        text = (title or "") + " " + (summary or "")
        for nm in extract(text):
            code = name2code[nm]
            h = hits.setdefault(code, {"n": 0, "domains": set(), "authors": set(),
                                       "stances": set(), "samples": []})
            h["n"] += 1
            h["domains"].add(dom)
            if author:
                h["authors"].add(author)
            if stance:
                h["stances"].add(stance)
            if len(h["samples"]) < 2 and summary:
                h["samples"].append({"author": author, "text": (summary or "")[:70]})

    card = {r[0]: (r[1], r[2]) for r in DB.conn.execute(
        "SELECT code, tier, res_score FROM res_stock_card")}
    overlap = {r[0]: r[1] for r in DB.conn.execute(
        "SELECT stock_code, n_bloggers FROM blogger_overlap")}
    sig = {}
    for r in DB.conn.execute(
            "SELECT code, direction, COUNT(*) FROM sig_unified GROUP BY code, direction"):
        sig.setdefault(r[0], {})[r[1]] = sig.get(r[0], {}).get(r[1], 0) + r[2]

    out_rows, gaps = [], []
    for code, h in hits.items():
        t, rs = card.get(code, ("-", None))
        o = overlap.get(code, 0)
        s = sig.get(code, {})
        s_n = sum(s.values())
        sc = h["n"] + (5 if code in card else 0) + o + min(s_n, 5)
        if t in ("A", "S") and h["n"] >= 2:
            verdict = "强共振"
        elif code in card or o or s_n:
            verdict = "共振"
        else:
            verdict = "豆包独立关注"
            gaps.append({"code": code, "name": code2name.get(code), "douyin_n": h["n"]})
        out_rows.append({
            "code": code, "name": code2name.get(code),
            "douyin_n": h["n"], "domains": sorted(h["domains"]),
            "bloggers": sorted(h["authors"])[:6], "stances": sorted(h["stances"]),
            "card_tier": t, "card_score": rs, "overlap_n": o,
            "sig_n": s_n, "sig_dir": s, "verdict": verdict, "score": sc,
            "samples": h["samples"],
        })

    out_rows.sort(key=lambda x: -x["score"])
    out_rows = out_rows[:max(1, min(int(limit or 40), 60))]
    gaps.sort(key=lambda x: -x["douyin_n"])
    distinct = len(hits)
    resonance_n = sum(1 for c in hits if c in card or c in overlap or c in sig)
    payload = {
        "ok": True,
        "updated_at": _dt.now().strftime("%Y-%m-%d %H:%M"),
        "distinct_stocks": distinct,
        "resonance_n": resonance_n,
        "rows": out_rows,
        "gaps": gaps[:10],
    }
    _resonance_cache = {"ts": now, "payload": payload}
    return payload


def api_bili_pile(cat: str | None = None, blogger: str | None = None,
                  limit: int = 2000) -> dict:
    """B站视频分堆列表（technical/value/sentiment/weekly）。

    供前端三处使用：① 消息与外围→市场情绪（cat=sentiment）；
    ③ 复盘成长→技术·价值教学（cat=technical,value）；④ 博主荐股面板合并「下周当红」
    （cat=weekly）。reco 堆已在 blogger_reco，此处不重复。
    """
    out = {"total": 0, "rows": []}
    where, args = [], []
    if cat:
        cats = [c.strip() for c in str(cat).split(",") if c.strip()]
        if cats:
            ph = ",".join("?" * len(cats))
            where.append("cat IN (%s)" % ph)
            args += cats
    if blogger:
        where.append("blogger = ?")
        args.append(blogger)
    wsql = (" WHERE " + " AND ".join(where)) if where else ""
    tot = DB.conn.execute("SELECT COUNT(*) AS n FROM bili_pile" + wsql, args).fetchone()
    out["total"] = tot["n"] if tot else 0
    lim = max(1, min(int(limit or 2000), 5000))
    rows = DB.conn.execute(
        "SELECT cat, blogger, bvid, title, vdate, url FROM bili_pile" + wsql +
        " ORDER BY vdate DESC LIMIT " + str(lim), args).fetchall()
    out["rows"] = [dict(r) for r in rows]
    return out


def api_bili_methods(cat: str = "technical,value") -> dict:
    """B站「技术·价值教学」方法卡片聚合（2026-10-11 王总需求）。

    每个方法 = 短名 + 一句话总结 + 视频统计 + 映射到的已回测规则真实数字
    （strategy_sim_window·mid 近两年胜率/收益/评级）。映射不到规则的诚实标未回测。
    """
    return bili_methods.methods_payload(DB.conn, cat=cat)


def api_bili_scan(force: bool = False) -> dict:
    """一键筛股（2026-10-11 王总需求）：当前全市场哪些股票正走在知识库战法的买点上。

    结论为主、解释可下钻：返回命中票清单（股票 + 命中战法 + 该战法历史「入手值得率」），
    战法名可点开看解释。后端状态机单飞 + 当日缓存，前端 4s 轮询拿 warming/done。
    首次/缓存失效触发后台扫描（≈2~3 分钟），期间返回进度。
    """
    return bili_scan.scan_payload(DB.conn, force=bool(force))


def api_blogger_perf(code: str | None = None, blogger: str | None = None,
                     force: bool = False) -> dict:
    """荐股后固定周期收益：T+1（隔日）/ T+3 / T+5 / T+10，**基准价 = 荐股日收盘价**。

    口径硬约束（2026-10-01 与王总确认）
      · 老票四档齐全；不足 N 个交易日的（比如今天刚荐）→ null，前端渲染成「—」，
        **绝不渲染成 0**（0 会被读成「没赚没亏」，与「还没走完」是两回事）
      · 走独立表 blogger_perf，幂等 UPSERT，当天算完即缓存；force=1 可强制全量重算
        （237 条 8 并发实测约 27 秒，前端只在点「重算」时才传 force）。
    """
    out = _blogger_payload_base()
    try:
        if force:
            out["compute"] = blogger_perf.compute_all(force=True)
        out["rows"] = blogger_perf.get(code=code, blogger=blogger)
        out["ok"] = True
    except Exception as e:                      # 行情源挂了也要让面板出得来，只是没数字
        out["ok"] = False
        out["reason"] = str(e)[:200]
        out["rows"] = []
    return out


def api_blogger_overlap() -> dict:
    """重复推荐：同一只票被多位博主同时推（多空分歧小的方向，参考价值更高）。

    读取时自愈：blogger_overlap 比 blogger_reco 陈旧则先重算一次再返回（见 _blogger_selfheal）。"""
    _blogger_selfheal("overlap")
    out = _blogger_payload_base()
    rows = DB.conn.execute(
        "SELECT stock_code, stock_name, n_bloggers, bloggers, price_at_rec, close_latest,"
        " ret_since, note FROM blogger_overlap ORDER BY n_bloggers DESC, stock_code").fetchall()
    out["rows"] = [dict(r) for r in rows]
    return out


def api_blogger_stats() -> dict:
    """博主效果对比：各自推荐股数 / 待采集数 / 平均涨跌幅 / 平均一周涨幅 / 上涨比例。

    n_stock = 有效样本（有区间收益的行）；n_pending = 待采集（源表尚未给出收益的行）。
    （2026-10-09：新增 n_pending 列并透出，配合「待采集」口径；旧字段保持不变，纯增量。）

    读取时自愈：blogger_stats 比 blogger_reco 陈旧则先重算一次再返回（见 _blogger_selfheal）。"""
    _blogger_selfheal("stats")
    out = _blogger_payload_base()
    rows = DB.conn.execute(
        "SELECT blogger, n_stock, n_pending, avg_ret, avg_ret_1w, up_ratio, max_ret, min_ret "
        "FROM blogger_stats ORDER BY n_stock DESC").fetchall()
    out["rows"] = [dict(r) for r in rows]
    return out


def api_contest_rounds() -> dict:
    """当红参赛股：期数列表（前端据此做日期切换）。"""
    r = DB.conn.execute("SELECT MAX(updated_at) AS u FROM contest_round").fetchone()
    rows = DB.conn.execute(
        "SELECT video_id, title, publish_date, author, updated_at, "
        "(SELECT COUNT(*) FROM contest_participant p WHERE p.video_id = contest_round.video_id) AS n_part "
        "FROM contest_round ORDER BY publish_date DESC").fetchall()
    return {"updated": (r["u"] if r else None), "rows": [dict(x) for x in rows]}


def api_contest_detail(video_id: str | None = None) -> dict:
    """当红参赛股某期明细：排名 + 逐日追踪。
    dates 由实际落库数据 DISTINCT 出来，而不是写死日期区间 ——
    不同期的追踪窗口可能不一样（源数据逐日填充，尾端常缺）。"""
    con = DB.conn
    rnd = None
    if video_id:
        r = con.execute("SELECT * FROM contest_round WHERE video_id = ?", (video_id,)).fetchone()
        if r:
            rnd = dict(r)
    if rnd is None:
        r = con.execute("SELECT * FROM contest_round ORDER BY publish_date DESC LIMIT 1").fetchone()
        rnd = dict(r) if r else {}
    if not rnd:
        return {"round": {}, "dates": [], "rows": [], "updated": None}

    dates = [x[0] for x in con.execute(
        "SELECT DISTINCT trade_date FROM contest_track WHERE video_id = ? "
        "ORDER BY trade_date", (rnd.get("video_id"),)).fetchall()]

    rows = []
    for p in con.execute(
            "SELECT rank_no, participant, stock, code, week_gain, points "
            "FROM contest_participant WHERE video_id = ? ORDER BY rank_no",
            (rnd.get("video_id"),)).fetchall():
        d = dict(p)
        days = {}
        for t in con.execute(
                "SELECT trade_date, close, change_pct FROM contest_track "
                "WHERE video_id = ? AND participant = ?",
                (rnd.get("video_id"), d["participant"])).fetchall():
            days[t["trade_date"]] = {"close": t["close"], "change_pct": t["change_pct"]}
        # 补齐日期轴（缺失日补 None，前端据此渲染「灰-暂无数据」）
        d["days"] = [{"date": dt, "change_pct": (days.get(dt) or {}).get("change_pct"),
                      "close": (days.get(dt) or {}).get("close")} for dt in dates]
        rows.append(d)
    return {"round": rnd, "dates": dates, "rows": rows, "updated": rnd.get("updated_at")}


def api_watch_alerts(limit: int = 200, only_unread: bool = False) -> dict:
    with _WRITE_LOCK:
        watchlib.ensure_schema(DB.conn)
        return {"ok": True, "alerts": watchlib.list_alerts(DB.conn, limit=limit, only_unread=only_unread),
                "unread": watchlib.count_unread(DB.conn)}


def api_watch_alert_read(payload: dict) -> dict:
    with _WRITE_LOCK:
        watchlib.ensure_schema(DB.conn)
        if payload.get("all"):
            return watchlib.mark_all_read(DB.conn)
        aid = payload.get("id")
        if not aid:
            return {"ok": False, "error": "缺少 id"}
        return watchlib.mark_alert(DB.conn, int(aid), bool(payload.get("read", True)))


# ================= 增强功能② 资金流向 =================
def api_moneyflow_stock(symbol: str) -> dict:
    # 2026-09-29 修复：本机东财 fflow 接口常不可达，改走 _stock_moneyflow_any
    # （东财主源 → 新浪兜底），与 api_diag / api_board ③ 一致，避免个股资金流整页空。
    return _stock_moneyflow_any(symbol)

def api_moneyflow_stock_force(symbol: str, force: bool = True) -> dict:
    """个股资金流 + 强制重试（跳过 30 分钟失败负缓存）。"""
    return _stock_moneyflow_any(symbol, force=force)

def api_moneyflow_rank(pz: int = 30, force: bool = False) -> dict:
    """2026-10-01：force 透传 —— 前端「强制重试」要能绕过 30 分钟失败负缓存。"""
    return moneyflow.rank(pz=pz, force=force)

def api_moneyflow_board(pz: int = 30, force: bool = False) -> dict:
    """同上：force=True 跳过 moneyflow.board_rank 的失败负缓存。"""
    return moneyflow.board_rank(pz=pz, force=force)


# ================= 增强功能② 板块资金流（多源 / 采样累积 / 分时曲线）============
# 2026-09-28 王总反馈「接口异常：未取到板块资金流排行（东财接口不可达）」。
# 原实现唯一依赖东财 push2，本机会被其风控掐连接；改为：
#   快照=新浪主源 + 东财兜底；曲线=东财分钟实时 → 本机采样累积 → 东财库内快照。
def api_board_flow_snapshot(kind: str = "industry", pz: int = 60, force: int = 0) -> dict:
    """板块资金流排行快照（带数据来源标签）。"""
    board_flow.ensure_schema(DB.conn)
    kind = kind if kind in board_flow.KINDS else board_flow.DEFAULT_KIND
    return board_flow.snapshot(kind=kind, pz=pz, force=bool(force))


def api_board_flow_intraday(kind: str = "industry", top: int = 12, force: int = 0) -> dict:
    """板块资金流分时曲线（东财实时 / 采样累积 / 库内快照 三级兜底）。"""
    board_flow.ensure_schema(DB.conn)
    kind = kind if kind in board_flow.KINDS else board_flow.DEFAULT_KIND
    return board_flow.intraday(kind=kind, top=top, force=bool(force), conn=DB.conn)


# ---------------------------------------------------------------- 板块资金流：日期区间
_FLOW_RANGE_CACHE: dict = {}
_FLOW_RANGE_TTL = 60          # 秒；区间聚合要读库 + 可能后台补抓，60s 足够挡住反复刷新


def _flow_range_cached(key: str, data: dict) -> dict:
    now = time.time()
    hit = _FLOW_RANGE_CACHE.get(key)
    if hit and now - hit[0] < _FLOW_RANGE_TTL and not data.get("force"):
        return hit[1]
    _FLOW_RANGE_CACHE[key] = (now, data)
    return data


def api_board_flow_range(start: str = "", end: str = "", kind: str = "industry",
                         top: int = 16, force: int = 0) -> dict:
    """板块资金流「日期区间」聚合（2026-10-05，王总：累计流入要能指定起止日期）。

    与「单日分时曲线」是两套数据形态：这里是**日线历史**（每个交易日一个点），
    那里是**当日分钟累计**。两个 mode 在前端并存，互不串味。
    """
    try:
        board_flow_range.ensure_schema(DB.conn)
        res = board_flow_range.range_flow(
            DB.conn, start=start, end=end, kind=kind, top=top, force=bool(force))
        res["force"] = bool(force)
        return _flow_range_cached("range:%s:%s:%s:%d:%d" % (start, end, kind, top, force), res)
    except Exception as exc:  # noqa: BLE001
        logger.exception("板块资金流区间聚合异常")
        return {"ok": False, "error": "区间聚合失败：%s" % exc, "series": [], "pool": []}


def api_board_flow_dates(kind: str = "industry") -> dict:
    """区间日期框的可选范围（交易日历 ∪ 库内已有日线日期）。"""
    try:
        board_flow_range.ensure_schema(DB.conn)
        return board_flow_range.date_options(DB.conn, kind)
    except Exception as exc:  # noqa: BLE001
        logger.exception("板块资金流日期清单异常")
        return {"ok": False, "error": str(exc), "days": []}


def api_board_flow_status() -> dict:
    """板块资金流自检：两源连通性 + 采样存量 + 当前曲线来源。"""
    board_flow.ensure_schema(DB.conn)
    return board_flow.status(conn=DB.conn)

# ================= 总览③ 每日预测（六路信号 + 复盘自学习闭环，2026-09-28 新增）=====
# 引擎在 viewer/predictor.py：独立建表、不 JOIN 既有业务表；口径见该模块 docstring。
def api_prediction_today() -> dict:
    """今日推荐载荷：核心/观察池 + 命中信号 + 理由 + 复盘结果 + 权重调整痕迹。"""
    predictor.ensure_tables(DB.conn)
    return predictor.today_payload(DB.conn)


def api_prediction_scores() -> dict:
    """策略记分板：六路信号 + 九流派 + 逐策略的实盘命中率（T+1/T+3/T+5）。"""
    predictor.ensure_tables(DB.conn)
    return predictor.scores_payload(DB.conn)


def api_prediction_track() -> dict:
    """复盘轨迹：逐日命中率与累计表现（自我成长的证据链）。"""
    predictor.ensure_tables(DB.conn)
    return predictor.track_payload(DB.conn)


def api_growth_adjust_log() -> dict:
    """成长面板：权重演化时序 + 最近调整（纯读 predictor_adjust_log + predictor_score，幂等不写库）。"""
    predictor.ensure_tables(DB.conn)
    return predictor.growth_adjust_log_payload(DB.conn)


def api_prediction_status() -> dict:
    """预测引擎状态：是否在跑 / 当前阶段 / 上次错误 / 最近一次调度结果 / 缺日补跑进度。"""
    st = predictor.status_snapshot()
    st["last_daily"] = predictor._DAILY_DONE.get("last")
    st["catchup"] = predictor._catchup_status()
    return {"ok": True, **st}


def api_prediction_dates() -> dict:
    """可选预测日清单（最早=首份预测日）+ 每日三窗口摘要，供前端日期选择器。"""
    predictor.ensure_tables(DB.conn)
    return predictor.dates_payload(DB.conn)


def api_prediction_history(date: str = "", range_days: int = 0, window: int = 5) -> dict:
    """按日期回看 / 按区间汇总：含研判、六路信号、逐日曲线、复盘命中。"""
    predictor.ensure_tables(DB.conn)
    return predictor.history_payload(DB.conn, run_date=(date or None),
                                     range_days=int(range_days or 0), window=int(window or 5))


def api_prediction_run(force: int = 1) -> dict:
    """异步触发「复盘 + 生成」：立即返回，前端轮询 /api/prediction/status 看进度。"""
    st = predictor.status_snapshot()
    if st.get("running"):
        return {"ok": True, "already_running": True, "status": st}
    do_force = bool(force)

    def _bg() -> None:
        con = None
        try:
            con = predictor.connect()
            predictor.run_now(con, force=do_force)
        except Exception:  # noqa: BLE001
            logging.exception("predictor.run_now 失败")
        finally:
            if con is not None:
                try:
                    con.close()
                except Exception:  # noqa: BLE001
                    pass

    threading.Thread(target=_bg, name="predictor-run", daemon=True).start()
    return {"ok": True, "started": True}


def api_prediction_catchup() -> dict:
    """缺日补跑（异步）：几天没打开应用后，把漏掉的交易日逐日补齐。

    每个缺失交易日 d：asof 隔离生成（只看 d 及以前数据）→ 用实际走势复盘验证 → 权重更新。
    立即返回；前端轮询 /api/prediction/status 的 catchup 字段看进度。
    """
    cs = predictor._catchup_status()
    if cs.get("running"):
        return {"ok": True, "already_running": True, "catchup": cs}
    if predictor.status_snapshot().get("running"):
        return {"ok": True, "already_running": True, "catchup": cs}

    def _bg() -> None:
        con = None
        try:
            con = predictor.connect()
            predictor.catch_up(con)
        except Exception:  # noqa: BLE001
            logging.exception("predictor.catch_up 失败")
        finally:
            if con is not None:
                try:
                    con.close()
                except Exception:  # noqa: BLE001
                    pass

    threading.Thread(target=_bg, name="predictor-catchup", daemon=True).start()
    return {"ok": True, "started": True}


# ================= 增强功能② 智能选股 =================
def api_screener(screen: str = "value", limit: int = 40, params: dict = None) -> dict:
    """智能选股：screen=逗号分隔指标 key，params={key:阈值}（前端可调阈值）。"""
    return screener.run(screen=screen, limit=limit, params=params)


# ===== 智能选股扩展：板块分类 + 连续阴跌（2026-09-30，王总需求）=====

def api_screener_boards(kind: str = "industry", pz: int = 90) -> dict:
    """板块清单（新浪主源/东财兜底），供「板块分类」入口。"""
    return board_flow.board_list(kind=kind, pz=pz)


def api_screener_board_members(code: str = "") -> dict:
    """板块成分股 + 综合分排序（市值50%+成交额30%+换手率20%，知名度口径）。"""
    # 板块代码形如 new_ljhy（含下划线），只放行字母/数字/下划线——别把下划线剥掉
    code = "".join(ch for ch in str(code or "") if ch.isalnum() or ch == "_")[:24]
    return board_flow.members_rich(code)


_DECLINE_POOL_TTL = 600
_decline_pool_cache = {"at": 0.0, "pool": None}


def _decline_pool() -> dict:
    """阴跌扫描池（活跃股票池，10 分钟缓存）：
    智能选股 6 策略并集 + 关注池（watch_items）+ 近 10 个交易日龙虎榜。
    顺序即优先级（策略 > 关注 > 龙虎榜），dict 天然去重。
    只留沪深 A 股（60/68/00/30 开头）——北交所（92/43/8 开头）腾讯/新浪日 K 源
    取不到，留着只会每轮扫描白白重试。"""
    now = time.time()
    if _decline_pool_cache["pool"] is not None and now - _decline_pool_cache["at"] < _DECLINE_POOL_TTL:
        return _decline_pool_cache["pool"]
    pool = {}
    for key in screener.VAL_KEYS:
        try:
            for row in (screener.run(screen=key, limit=40).get("rows") or []):
                c = str(row.get("code") or "")
                if _is_sh_sz_a(c):
                    pool.setdefault(c, row.get("name") or "")
        except Exception:  # noqa: BLE001
            continue
    try:
        for r in DB.conn.execute("SELECT symbol, name FROM watch_items WHERE kind='stock'"):
            c = str(r["symbol"] or "")
            if _is_sh_sz_a(c):
                pool.setdefault(c, r["name"] or "")
    except Exception:  # noqa: BLE001
        pass
    try:
        dates = [r[0] for r in DB.conn.execute(
            "SELECT DISTINCT trade_date FROM lhb_stocks ORDER BY trade_date DESC LIMIT 10")]
        if dates:
            qm = ",".join("?" * len(dates))
            for r in DB.conn.execute(
                    "SELECT DISTINCT security_code, security_name FROM lhb_stocks "
                    "WHERE trade_date IN (%s)" % qm, dates):
                c = str(r[0] or "")
                if _is_sh_sz_a(c):
                    pool.setdefault(c, r[1] or "")
    except Exception:  # noqa: BLE001
        pass
    _decline_pool_cache["at"] = now
    _decline_pool_cache["pool"] = pool
    return pool


def _is_sh_sz_a(code: str) -> bool:
    """沪深 A 股前缀过滤（60/68/00/30），北交所与 B 股不进阴跌扫描池。"""
    c = str(code or "")
    return len(c) == 6 and c.isdigit() and c[:2] in ("60", "68", "00", "30")


def api_screener_decline(days: int = 30, down: int = 25, limit: int = 80,
                         sort: str = "down", force: int = 0) -> dict:
    """连续阴跌扫描（活跃池内，近 days 个交易日中下跌 >= down 天）。
    sort="score" 时按换手率策略「入手值得率」降序（2026-09-30，王总需求）。

    扫描要过活跃池 + 逐票日线，重复点同一组参数不该重算：**按参数键内存缓存 1 小时**，
    同样参数cache 命中就在载荷里标 `cached=True`（前端可据此省掉转圈）；`force=1` 强制重算。
    """
    key = (int(days), int(down), int(limit), sort or "down")
    now = time.time()
    cached = _DECLINE_CACHE.get(key)
    if cached and now - cached[0] < _DECLINE_TTL and not force:
        return dict(cached[1], cached=True)
    res = screener.decline_scan(_decline_pool(), days=days, down=down, limit=limit, sort=sort)
    _DECLINE_CACHE[key] = (now, res)
    return res


def api_stock_turnover(symbol: str, days: int = 30) -> dict:
    """GET /api/stock_turnover?symbol=XXXXXX —— 单票换手率实战评分（个股详情抽屉，2026-09-30）。"""
    return screener.turnover_single(symbol, days=days)


def api_stock_f10(symbol: str) -> dict:
    """GET /api/stock_f10?symbol=XXXXXX —— 个股 F10：公司简介 + 持股结构（机构/散户）+ 股东户数（2026-09-30 晚）。"""
    return screener.stock_f10(symbol)


def api_screener_turnover_top(days: int = 30, limit: int = 30) -> dict:
    """GET /api/screener_turnover_top —— 全市场换手率实战 Top N（按入手值得率降序，
    2026-09-30 王总需求：阴跌板块一键筛大盘前30）。"""
    return screener.turnover_top(days=days, limit=limit)


# ================= 板块二 · 长期低位震荡选股（2026-10-13 进哥需求）=================
#   方法论：达瓦斯箱体(本土化: 突破前埋伏) + 邱国鹭「便宜的好公司」+ 林奇 PEG + A股中特估/国家队底仓。
#   漏斗：全历史低位分位 → 箱体震荡判定 → 国资系股东加分 → 长窗值得率 → 综合排序。
#   MVP 仅用现有数据（全历史日K 复用 api_kline / _worth_scores / stock_f10 股东 / blogger_overlap）；
#   PE·ROE 等质量指标为增强期（诚实标 «—»，不编造）。指标定义见《长期低位震荡选股策略方案 v0.2》。
def _lowbox_metrics(klines: list, box_window: int, low_pct: float) -> dict:
    """从全历史日K(dict: date/open/close/high/low/volume) 算「长期低位 + 箱体震荡」量化指标。

    返回 {ok, current, pct_all, pct_3y, box_high, box_low, box_range_pct, cv_pct,
          slope_yr_pct, touches, box_years, n_all}。
      · pct_all / pct_3y —— 当前价在「上市以来 / 近3年」收盘价序列中的分位(0=最低,1=最高)，越低越低位；
      · box_*          —— 取近 box_window 日(默认250≈1年)的箱顶/箱底/振幅/时长；
      · cv_pct         —— 变异系数=std(close)/mean(close)×100，越低越平稳（横盘震荡）；
      · slope_yr_pct   —— 线性回归年化斜率%，越接近0越横盘（非单边下跌）；
      · touches        —— 价格落入箱顶/箱底3%内的触碰次数（箱体有效性代理）。
    """
    import statistics as _st
    if not klines or len(klines) < 60:
        return {"ok": False, "reason": "日K不足60根"}
    closes = [float(r["close"]) for r in klines if r.get("close") is not None]
    if len(closes) < 60 or closes[-1] <= 0:
        return {"ok": False, "reason": "收盘价缺失"}
    cur = closes[-1]
    n_all = len(closes)
    below = sum(1 for c in closes if c <= cur)
    pct_all = round(below / n_all * 100.0, 1)
    recent = closes[-750:] if len(closes) >= 750 else closes
    below3 = sum(1 for c in recent if c <= cur)
    pct_3y = round(below3 / len(recent) * 100.0, 1)
    win = klines[-box_window:] if len(klines) >= box_window else klines
    wcloses = [float(r["close"]) for r in win]
    hs = [float(r["high"]) for r in win if r.get("high") is not None]
    ls = [float(r["low"]) for r in win if r.get("low") is not None]
    box_high = round(max(hs), 2) if hs else round(max(wcloses), 2)
    box_low = round(min(ls), 2) if ls else round(min(wcloses), 2)
    vols = [float(r["volume"]) for r in win if r.get("volume") is not None]
    box_avg_vol = (sum(vols) / len(vols)) if vols else None
    last_vol = float(klines[-1].get("volume")) if klines[-1].get("volume") is not None else None
    box_range_pct = round((box_high / box_low - 1.0) * 100.0, 1) if box_low > 0 else None
    try:
        cv = (_st.pstdev(wcloses) / (sum(wcloses) / len(wcloses))) * 100.0 if wcloses else None
    except Exception:  # noqa: BLE001
        cv = None
    cv_pct = round(cv, 1) if cv is not None else None
    n = len(wcloses)
    xs = list(range(n))
    mx, my = sum(xs) / n, sum(wcloses) / n
    denom = sum((x - mx) ** 2 for x in xs)
    slope = (sum((xs[i] - mx) * (wcloses[i] - my) for i in range(n)) / denom) if denom else 0.0
    slope_yr_pct = round(slope * 252.0 / my * 100.0, 1) if my else None
    band = box_high - box_low
    touches = 0
    if band > 0:
        for c in wcloses:
            if (box_high - c) / band <= 0.03 or (c - box_low) / band <= 0.03:
                touches += 1
    box_years = round(len(win) / 252.0, 1)
    vol_ratio = round(last_vol / box_avg_vol, 2) if (last_vol and box_avg_vol) else None
    breakout_pct = round((cur / box_high - 1.0) * 100.0, 2) if box_high else None
    return {"ok": True, "current": round(cur, 2), "pct_all": pct_all, "pct_3y": pct_3y,
            "box_high": box_high, "box_low": box_low, "box_range_pct": box_range_pct,
            "cv_pct": cv_pct, "slope_yr_pct": slope_yr_pct, "touches": touches,
            "box_years": box_years, "n_all": n_all,
            "box_avg_vol": round(box_avg_vol, 0) if box_avg_vol else None,
            "last_vol": round(last_vol, 0) if last_vol else None,
            "vol_ratio": vol_ratio, "breakout_pct": breakout_pct}


_STATE_KEYWORDS = ["汇金", "证金", "国新", "社保", "养老", "中央", "国资", "央企", "国企",
                   "人保", "人寿", "平安", "泰康", "太保", "新华", "保险", "基金", "证投",
                   "香港中央结算", "北向", "QFII"]


def _state_backing_tag(holders: list) -> str:
    """从十大流通股东名单里打「国资系/机构」标记。命中即返回最高类别标签，同类不叠加。

    优先级（2026-10-13 进哥）：国家队 > 央国企 > 险资 > 基金/机构 > 北向——
    进哥强调「央国企开设的、以及它们投资的基金/险资都算」，故险资与基金/机构一并计入。
    """
    if not holders:
        return ""
    cats = set()
    for h in holders:
        name = str(h.get("name") or "")
        if any(k in name for k in ("汇金", "证金", "国新", "社保", "养老")):
            cats.add("国家队")
        elif any(k in name for k in ("国资", "央企", "国企", "中央", "国有")):
            cats.add("央国企")
        elif any(k in name for k in ("人保", "人寿", "平安", "泰康", "太保", "新华", "保险")):
            cats.add("险资")
        elif "基金" in name or "证投" in name:
            cats.add("基金/机构")
        elif any(k in name for k in ("香港中央结算", "北向", "QFII")):
            cats.add("北向")
    for c in ("国家队", "央国企", "险资", "基金/机构", "北向"):
        if c in cats:
            return c
    return ""


# ---------- 板块二·增强期数据源（2026-10-13 进哥：一次性做全，不再分期） ----------
# 财务主要指标  RPT_F10_FINANCE_MAINFINADATA（ROEJQ 加权ROE / ZCFZL 资产负债率 / 扣非 / 毛利率）
# 公司概况      RPT_F10_BASIC_ORGINFO（ACTUAL_HOLDER=实际控制人 → 国资实控打标）
# 分红明细      RPT_SHAREBONUS_DET（PRETAX_BONUS_RMB 每10股派息 → 自算 TTM 股息率）
# 估值不贵      valuation.score（PE/PB/PS/PEG/PCF 五因子 0~100）
_LOWBOX_CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              "data", "lowbox_fund_cache.json")
_LOWBOX_SCAN_CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                   "data", "lowbox_scan_cache.json")
_LOWBOX_BREAK_LOG = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                  "data", "lowbox_breakout_log.json")
_LOWBOX_LOCK = threading.Lock()


def _lowbox_cache_get(key: str, ttl_days: int):
    try:
        with open(_LOWBOX_CACHE, encoding="utf-8") as fh:
            dd = json.load(fh)
        ent = (dd or {}).get(key)
        if not ent or not ent.get("updated"):
            return None
        age = (datetime.now().date() - datetime.fromisoformat(str(ent["updated"])[:10]).date()).days
        if age > ttl_days or ent.get("data") is None:
            return None
        return ent["data"]
    except Exception:  # noqa: BLE001
        return None


def _lowbox_cache_put(key: str, data) -> None:
    try:
        with _LOWBOX_LOCK:
            try:
                with open(_LOWBOX_CACHE, encoding="utf-8") as fh:
                    dd = json.load(fh)
            except Exception:  # noqa: BLE001
                dd = {}
            dd[key] = {"updated": datetime.now().date().isoformat(), "data": data}
            tmp = _LOWBOX_CACHE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(dd, fh, ensure_ascii=False)
            os.replace(tmp, _LOWBOX_CACHE)
    except Exception:  # noqa: BLE001
        pass


def _secucode(code: str) -> str:
    """600519 → 600519.SH / 000001 → 000001.SZ / 83xxxx·43xxxx·87xxxx·92xxxx → BJ。"""
    c = re.sub(r"\D", "", str(code or ""))
    if not c:
        return ""
    if c.startswith(("43", "83", "87", "88", "92")):
        return c + ".BJ"
    if c.startswith(("6", "5", "9")):
        return c + ".SH"
    return c + ".SZ"


def _fund_metrics(code: str):
    """最新一期财务主要指标：ROE(加权)/扣非ROE/资产负债率/毛利率/营收·归母·扣非同比。TTL 7 天。"""
    hit = _lowbox_cache_get("fund|" + str(code), 7)
    if hit is not None:
        return hit
    sc = _secucode(code)
    if not sc:
        return None
    r = emapi.datacenter("RPT_F10_FINANCE_MAINFINADATA", flt='(SECUCODE="%s")' % sc,
                         sort_columns="REPORT_DATE", sort_types="-1", page_size=1)
    if not r.get("ok") or not r.get("rows"):
        return None
    x = r["rows"][0]
    data = {"roe": emapi._num(x.get("ROEJQ")), "roe_kf": emapi._num(x.get("ROEKCJQ")),
            "dr": emapi._num(x.get("ZCFZL")), "gm": emapi._num(x.get("XSMLL")),
            "rev_yoy": emapi._num(x.get("TOTALOPERATEREVETZ")),
            "np_yoy": emapi._num(x.get("PARENTNETPROFITTZ")),
            "kf_np_yoy": emapi._num(x.get("KCFJCXSYJLRTZ")),
            "report": str(x.get("REPORT_DATE_NAME") or "")}
    _lowbox_cache_put("fund|" + str(code), data)
    return data


def _org_profile(code: str):
    """公司概况：实际控制人(ACTUAL_HOLDER)/行业/上市日期。实控人极少变，TTL 30 天。"""
    hit = _lowbox_cache_get("org|" + str(code), 30)
    if hit is not None:
        return hit
    sc = _secucode(code)
    if not sc:
        return None
    r = emapi.datacenter("RPT_F10_BASIC_ORGINFO", flt='(SECUCODE="%s")' % sc, page_size=1)
    if not r.get("ok") or not r.get("rows"):
        return None
    x = r["rows"][0]
    data = {"actual_holder": (str(x.get("ACTUAL_HOLDER")) if x.get("ACTUAL_HOLDER") else ""),
            "industry": str(x.get("EM2016") or ""),
            "list_date": str(x.get("LISTING_DATE") or "")[:10]}
    _lowbox_cache_put("org|" + str(code), data)
    return data


def _dividend_ttm(code: str):
    """近 365 天「实施分配」的税前每股派息合计（元/股，10派X 折算 /10）。TTL 7 天。"""
    hit = _lowbox_cache_get("div|" + str(code), 7)
    if hit is not None:
        return hit
    sc = _secucode(code)
    if not sc:
        return None
    r = emapi.datacenter("RPT_SHAREBONUS_DET", flt='(SECUCODE="%s")' % sc,
                         sort_columns="EX_DIVIDEND_DATE", sort_types="-1", page_size=40)
    if not r.get("ok") or not r.get("rows"):
        return None
    cut = datetime.now().date() - timedelta(days=365)
    total, last_ex = 0.0, ""
    for x in r["rows"]:
        if str(x.get("ASSIGN_PROGRESS") or "") != "实施分配":
            continue
        ex = str(x.get("EX_DIVIDEND_DATE") or "")[:10]
        if not ex:
            continue
        try:
            if datetime.fromisoformat(ex).date() < cut:
                continue
        except ValueError:
            continue
        amt = emapi._num(x.get("PRETAX_BONUS_RMB"))
        if amt:
            total += amt / 10.0
            last_ex = last_ex or ex
    data = {"ttm_div": round(total, 4), "last_ex": last_ex}
    _lowbox_cache_put("div|" + str(code), data)
    return data


_HOLDER_STATE_KW = ("国资委", "国资", "国有", "人民政府", "财政部", "汇金",
                    "证金", "国新", "央企", "国企", "中央企业")


def _actual_holder_tag(holder_text: str) -> str:
    """实控人含国资关键词 → 「国资实控」（v0.2 国资系口径：实控人国家队/央国企都算）。"""
    t = str(holder_text or "")
    if not t:
        return ""
    return "国资实控" if any(k in t for k in _HOLDER_STATE_KW) else ""


def _valuation_cached(code: str):
    """估值五因子评分（valuation.score），日级缓存。"""
    hit = _lowbox_cache_get("val|" + str(code), 1)
    if hit is not None:
        return hit
    try:
        v = valuation.score(code)
    except Exception:  # noqa: BLE001
        return None
    if not (v and v.get("ok")):
        return None
    _lowbox_cache_put("val|" + str(code), v)
    return v


def _worth_from_klines(code: str, klines: list, days: int):
    """用全历史日K 直接跑换手率五维评分机（绕开 screener 139 根日K缓存上限）。"""
    try:
        if not klines or len(klines) < days + 2:
            return None
        bars = []
        for k in klines:
            if k.get("close") is None or k.get("volume") is None:
                continue
            bars.append((str(k.get("date") or ""), float(k["close"]),
                         float(k["high"]) if k.get("high") is not None else None,
                         float(k["low"]) if k.get("low") is not None else None,
                         float(k["volume"])))
        if len(bars) < days + 2 or not bars[-1][1] or not bars[-1][4]:
            return None
        ent = (screener._float_map([code]) or {}).get(code)
        if not ent:
            return None
        ev = screener._turnover_eval(bars, ent[0], ent[1], days,
                                     to_scale=screener._to_scale_of(code, ent))
        return ev.get("score") if ev else None
    except Exception:  # noqa: BLE001
        return None


def _long_worth(code: str, klines: list):
    """长窗值得率：250/500/750 三档、30:40:30 加权（缺档按可用档权重归一）。"""
    specs = [(250, 0.3), (500, 0.4), (750, 0.3)]
    got = []
    for days, w in specs:
        s = _worth_from_klines(code, klines, days)
        if s is not None:
            got.append((s, w, days))
    if not got:
        return None, None
    tw = sum(w for _s, w, _d in got)
    total = sum(s * w for s, w, _d in got) / tw
    return round(total, 1), {("w%d" % d): s for s, _w, d in got}


def _fund_score(fund, val):
    """基本面分(0~100)：估值五因子 50% + ROE 30% + 负债率 20%（缺项权重归一，不编造）。"""
    comps = []
    if val and val.get("score") is not None:
        comps.append((float(val["score"]), 0.5))
    if fund and fund.get("roe") is not None:
        roe = float(fund["roe"])
        comps.append((max(0.0, min(100.0, roe / 15.0 * 100.0)), 0.3))
    if fund and fund.get("dr") is not None:
        dr = float(fund["dr"])
        comps.append((max(0.0, min(100.0, (70.0 - dr) / 40.0 * 100.0)), 0.2))
    if not comps:
        return None
    tw = sum(w for _s, w in comps)
    return round(sum(s * w for s, w in comps) / tw, 1)


_QUALITY_MODES = {"loose": {"pe": 40.0, "roe": 6.0, "dr": 65.0},
                  "strict": {"pe": 25.0, "roe": 10.0, "dr": 50.0}}


def _quality_pass(fund, val, mode: str):
    """基本面硬过滤（v0.2 宽/严档）。数据缺失不误杀，返回 (是否通过, 命中项说明)。"""
    lim = _QUALITY_MODES.get(mode or "")
    if not lim:
        return True, ""
    pe = (val or {}).get("pe")
    roe = (fund or {}).get("roe")
    dr = (fund or {}).get("dr")
    bad = []
    if pe is not None:
        if pe <= 0:
            bad.append("亏损")
        elif pe > lim["pe"]:
            bad.append("PE%.0f" % pe)
    if roe is not None and roe < lim["roe"]:
        bad.append("ROE%.1f" % roe)
    if dr is not None and dr > lim["dr"]:
        bad.append("负债%.0f%%" % dr)
    return (not bad), ("、".join(bad))


_STATE_SCORE = {"国家队": 100, "央国企": 85, "险资": 70, "基金/机构": 50, "北向": 40}


def _lowbox_state_score(tag: str, holder_tag: str):
    s = _STATE_SCORE.get(tag or "", 0)
    if holder_tag == "国资实控":
        s = max(s, 90)
    return s


def _lowbox_scan_cache_load() -> dict:
    try:
        with open(_LOWBOX_SCAN_CACHE, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:  # noqa: BLE001
        return {}


def _lowbox_scan_cache_save(rows: list) -> None:
    try:
        os.makedirs(os.path.dirname(_LOWBOX_SCAN_CACHE), exist_ok=True)
        tmp = _LOWBOX_SCAN_CACHE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                        "rows": rows}, fh, ensure_ascii=False)
        os.replace(tmp, _LOWBOX_SCAN_CACHE)
    except Exception:  # noqa: BLE001
        pass


def _lowbox_bo_state(cur, box_high, vol_ratio):
    """突破初判：breakout=放量突破 / stand_top=站上箱顶量不足 / near=临近(±3%) / in_box。"""
    try:
        if not cur or not box_high:
            return ""
        if cur > box_high:
            if vol_ratio and vol_ratio >= 1.5:
                return "breakout"
            return "stand_top"
        if cur >= box_high * 0.97:
            return "near"
        return "in_box"
    except Exception:  # noqa: BLE001
        return ""


def _lowbox_watch_push(code: str, name: str, rule: str, ref_price, price) -> bool:
    """突破/回踩事件 → 关注池未读提醒（watch_alerts）。

    票已在池 → 直接挂提醒；不在池 → 自动加入「长期低位」分组（alert_on=0，
    免得进「开了提醒却没设目标价」的误报）。同规则已有未读不重复。
    """
    try:
        watchlib.ensure_schema(DB.conn)
        dup = DB.conn.execute(
            "SELECT id FROM watch_alerts WHERE symbol=? AND rule=? AND read=0 LIMIT 1",
            (code, rule)).fetchone()
        if dup:
            return False
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        row = DB.conn.execute("SELECT id FROM watch_items WHERE symbol=? LIMIT 1",
                              (code,)).fetchone()
        item_id = row["id"] if row else None
        if item_id is None:
            gid = None
            g = DB.conn.execute("SELECT id FROM watch_groups WHERE name='长期低位' LIMIT 1").fetchone()
            if g:
                gid = g["id"]
            else:
                try:
                    watchlib.add_group(DB.conn, "长期低位", kind="stock", color="#0e7490")
                except Exception:  # noqa: BLE001  并发重名等 → 回查
                    pass
                g = DB.conn.execute("SELECT id FROM watch_groups WHERE name='长期低位' LIMIT 1").fetchone()
                gid = g["id"] if g else None
            try:
                watchlib.add_item(DB.conn, code, name=name or "", kind="stock",
                                  group_id=gid, alert_on=0)
            except Exception:  # noqa: BLE001
                pass
            row = DB.conn.execute("SELECT id FROM watch_items WHERE symbol=? LIMIT 1",
                                  (code,)).fetchone()
            item_id = row["id"] if row else None
        DB.conn.execute(
            "INSERT INTO watch_alerts(item_id, symbol, name, rule, target_price,"
            " triggered_price, triggered_at, read) VALUES(?,?,?,?,?,?,?,0)",
            (item_id, code, name or "", rule, ref_price, price, now))
        DB.conn.commit()
        return True
    except Exception:  # noqa: BLE001
        logging.warning("lowbox 突破提醒入池失败 %s" % code, exc_info=True)
        return False


def _lowbox_break_log_load() -> list:
    try:
        with open(_LOWBOX_BREAK_LOG, encoding="utf-8") as fh:
            dd = json.load(fh)
        return dd.get("events") if isinstance(dd, dict) else []
    except Exception:  # noqa: BLE001
        return []


def _lowbox_break_log_append(ev: dict) -> None:
    try:
        os.makedirs(os.path.dirname(_LOWBOX_BREAK_LOG), exist_ok=True)
        with _LOWBOX_LOCK:
            try:
                with open(_LOWBOX_BREAK_LOG, encoding="utf-8") as fh:
                    dd = json.load(fh)
            except Exception:  # noqa: BLE001
                dd = {"events": []}
            evs = dd.setdefault("events", [])
            evs.append(ev)
            dd["events"] = evs[-500:]
            tmp = _LOWBOX_BREAK_LOG + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(dd, fh, ensure_ascii=False)
            os.replace(tmp, _LOWBOX_BREAK_LOG)
    except Exception:  # noqa: BLE001
        pass


def api_lowbox_breakouts() -> dict:
    """GET /api/lowbox_breakouts —— 突破雷达：复评最近一次「长期低位」扫描池的突破状态。

    口径（达瓦斯本土化 + 战法库 box_breakout_retest）：
      · 突破 = 收盘站上箱顶 且 当日量 ≥ 箱体日均量 × 1.5（原著参数）；
      · 回踩确认 = 近 15 日曾触发突破、现价回落至箱顶 ±3% 内（二次确认，降假突破）。
    突破/回踩触发 → 落事件日志 + 推关注池未读提醒（同票同规则当日/未读去重）。
    """
    snap = _lowbox_scan_cache_load()
    rows0 = snap.get("rows") or []
    if not rows0:
        return {"ok": False, "reason": "还没有扫描过：请先在「长期低位」跑一次扫描",
                "rows": [], "events": [], "pushed_n": 0}
    now = datetime.now()
    today = now.strftime("%Y-%m-%d")
    log = _lowbox_break_log_load()
    logged = {(e.get("code"), e.get("kind"), str(e.get("date") or "")[:10]) for e in log}
    last_break = {}
    for e in log:
        if e.get("kind") == "breakout":
            last_break[e.get("code")] = str(e.get("date") or "")[:10]

    def _one(r):
        code = r.get("code")
        try:
            kl = api_kline(code, "day", limit=130)
            if not kl or kl.get("error") or not kl.get("klines"):
                return None
            k = kl["klines"][-1]
            cur = float(k["close"]) if k.get("close") is not None else None
            vol = float(k["volume"]) if k.get("volume") is not None else None
            box_high = r.get("box_high")
            avg_vol = r.get("box_avg_vol")
            vr = round(vol / avg_vol, 2) if (vol and avg_vol) else None
            state = _lowbox_bo_state(cur, box_high, vr)
            # 回踩确认：近15日突破过 + 现价回到箱顶±3%
            if state == "in_box" and box_high and cur and cur >= box_high * 0.97:
                d0 = last_break.get(code)
                if d0:
                    try:
                        if 0 <= (now.date() - datetime.fromisoformat(d0).date()).days <= 15:
                            state = "retest"
                    except ValueError:
                        pass
            return {"code": code, "name": r.get("name") or "", "cur": cur,
                    "box_high": box_high, "vol_ratio": vr, "state": state,
                    "breakout_pct": round((cur / box_high - 1.0) * 100.0, 2)
                    if (cur and box_high) else None}
        except Exception:  # noqa: BLE001
            return None

    from concurrent.futures import ThreadPoolExecutor
    out, pushed = [], 0
    with ThreadPoolExecutor(max_workers=10) as ex:
        for res in ex.map(_one, rows0[:80]):
            if not res:
                continue
            out.append(res)
            if res["state"] in ("breakout", "retest"):
                kind = res["state"]
                if (res["code"], kind, today) in logged:
                    continue
                ev = {"code": res["code"], "name": res["name"], "kind": kind,
                      "date": today, "ts": now.strftime("%Y-%m-%d %H:%M:%S"),
                      "price": res["cur"], "box_high": res["box_high"],
                      "vol_ratio": res["vol_ratio"]}
                _lowbox_break_log_append(ev)
                logged.add((res["code"], kind, today))
                if _lowbox_watch_push(res["code"], res["name"],
                                      "box_breakout" if kind == "breakout" else "box_retest",
                                      res["box_high"], res["cur"]):
                    pushed += 1
    out.sort(key=lambda x: (0 if x["state"] in ("breakout", "retest") else
                            1 if x["state"] in ("stand_top", "near") else 2, -(x.get("cur") or 0)))
    return {"ok": True, "ts": now.strftime("%Y-%m-%d %H:%M:%S"), "rows": out,
            "events": log[-30:], "pushed_n": pushed}


def api_screener_lowbox(codes: str = "", limit: int = 30, box_window: int = 750,
                        low_pct: float = 30.0, quality: str = "off",
                        w_shape: float = 40.0, w_fund: float = 35.0,
                        w_worth: float = 25.0) -> dict:
    """GET /api/screener_lowbox —— 板块二：长期低位震荡 + 基本面 + 国资系，一次性全量版。

    codes 为空 → 关注池 ∪ 板块一Top30；否则用逗号分隔的自定义代码（自定义优先、去重）。
    逐只复用 api_kline(limit=0) 拉全历史日K（90s 缓存），并发线程池；首跑需补K线，二次秒出。
    2026-10-13 进哥「一次性都做进去」：
      · 基本面（增强期并入）：估值五因子(valuation.score) + ROE/负债率/毛利率/扣非(F10 财务)
        + TTM 股息率(分红明细自算) + 实控人国资打标(公司概况 ACTUAL_HOLDER)；
      · 长窗 worth：250/500/750 三档 30:40:30 加权（直接跑全历史 K，绕开 139 根缓存上限）；
      · 质量硬过滤 off/loose/strict（数据缺失不误杀，标 ⚠）；
      · 综合分 = 形态(w_shape) + 基本面(w_fund) + 长窗worth(w_worth) 权重柱（缺项按比例摊给其余柱）
        + 国资系加分 0~15（国家队15/央国企12/险资9/基金机构6/北向4；实控人国资再+5，封顶15）——
        加分制而非权重柱，因 v0.2 明确「股东是加分不是硬门槛」，非国资票不该被一票压死；
      · 扫描结果落 data/lowbox_scan_cache.json 供 /api/lowbox_breakouts 突破雷达复评。
    """
    import time as _t
    from concurrent.futures import ThreadPoolExecutor
    _t0 = _t.time()
    limit = max(5, min(int(limit or 30), 100))
    # 2026-10-21 进哥：箱体时长要能选 1/2/3/5/8 年与「上市以来」。box_window=0 表示全历史
    # （旧版最小 120 会把 0 夹成 120，导致「上市以来」实际只取半年，是这次一并修掉的坑）。
    _bw = int(box_window or 0)
    box_window = 99999 if _bw <= 0 else max(120, min(_bw, 3000))
    try:
        low_pct = max(5.0, min(float(low_pct or 30.0), 80.0))
    except (TypeError, ValueError):
        low_pct = 30.0
    try:
        w_shape = max(0.0, float(w_shape)); w_fund = max(0.0, float(w_fund)); w_worth = max(0.0, float(w_worth))
    except (TypeError, ValueError):
        w_shape, w_fund, w_worth = 40.0, 35.0, 25.0
    if w_shape + w_fund + w_worth <= 0:
        w_shape, w_fund, w_worth = 40.0, 35.0, 25.0
    quality = quality if quality in ("off", "loose", "strict") else "off"
    # —— 股票池 ——
    universe = []
    watch_set = set()
    try:
        watchlib.ensure_schema(DB.conn)
        items = watchlib.list_items(DB.conn) or []
        watch_set = {it["symbol"] for it in items}
        universe = list(watch_set)
    except Exception:  # noqa: BLE001
        universe = []
    if codes and codes.strip():
        extra = [c.strip() for c in re.split(r"[\s,，、]+", codes) if c.strip()]
        universe = list(dict.fromkeys(extra + universe))   # 自定义优先，去重保序
    else:
        try:
            top = screener.turnover_top(days=30, limit=30) or {}
            for r in (top.get("rows") or []):
                c = r.get("code")
                if c and c not in universe:
                    universe.append(c)
        except Exception:  # noqa: BLE001
            pass
    universe = universe[:80]   # 首跑全历史抓取成本约束，封顶 80 只
    if not universe:
        return {"ok": False, "reason": "股票池为空（关注池为空且自定义代码为空）",
                "rows": [], "universe_size": 0, "scanned": 0, "elapsed": 0, "params": {}}
    # —— 博主提及映射（一次取出，避免逐只查）——
    blogger_map = {}
    try:
        brows = DB.conn.execute(
            "SELECT stock_code, n_bloggers, bloggers FROM blogger_overlap").fetchall()
        for r in brows:
            code = r["stock_code"]
            if not code:
                continue
            n = r["n_bloggers"]
            raw = r["bloggers"]
            names = []
            if raw:
                try:
                    parsed = json.loads(raw)
                    if isinstance(parsed, list):
                        names = [str(x) for x in parsed if x not in (None, "")]
                    elif isinstance(parsed, str):
                        names = [s.strip() for s in re.split(r"[、,]", parsed) if s.strip()]
                except Exception:  # noqa: BLE001
                    names = [s.strip() for s in re.split(r"[、,]", str(raw)) if s.strip()]
            blogger_map[code] = (n, names)
    except Exception:  # noqa: BLE001
        blogger_map = {}
    # —— 逐只算指标（线程池并发拉全历史K；财务/估值/实控人/分红各自带缓存）——
    def _one(code):
        try:
            kl = api_kline(code, "day", limit=0)
            if not kl or kl.get("error") or not kl.get("klines"):
                return None
            m = _lowbox_metrics(kl["klines"], box_window, low_pct)
            if not m.get("ok"):
                return None
            name = ""
            tag = ""
            try:
                f10 = screener.stock_f10(code) or {}
                name = (f10.get("profile") or {}).get("name") or kl.get("name") or ""
                tag = _state_backing_tag(f10.get("holders") or [])
            except Exception:  # noqa: BLE001
                pass
            fund = _fund_metrics(code)
            org = _org_profile(code)
            div = _dividend_ttm(code)
            val = _valuation_cached(code)
            holder_tag = _actual_holder_tag((org or {}).get("actual_holder"))
            dy = None
            if div and div.get("ttm_div") and m.get("current"):
                dy = round(div["ttm_div"] / m["current"] * 100.0, 2)
            long_worth, lw_parts = _long_worth(code, kl["klines"])
            bo_state = _lowbox_bo_state(m.get("current"), m.get("box_high"), m.get("vol_ratio"))
            return {"code": code, "name": name, "current": m["current"],
                    "pct_all": m["pct_all"], "pct_3y": m["pct_3y"],
                    "box_high": m["box_high"], "box_low": m["box_low"],
                    "box_range_pct": m["box_range_pct"], "cv_pct": m["cv_pct"],
                    "slope_yr_pct": m["slope_yr_pct"], "touches": m["touches"],
                    "box_years": m["box_years"], "n_all": m["n_all"],
                    "box_avg_vol": m.get("box_avg_vol"), "vol_ratio": m.get("vol_ratio"),
                    "breakout_pct": m.get("breakout_pct"), "bo_state": bo_state,
                    "long_worth": long_worth, "lw_parts": lw_parts,
                    "state_tag": tag, "holder_tag": holder_tag,
                    "actual_holder": (org or {}).get("actual_holder") or "",
                    "industry": (org or {}).get("industry") or "",
                    "pe": (val or {}).get("pe"), "pb": (val or {}).get("pb"),
                    "val_score": (val or {}).get("score"),
                    "roe": (fund or {}).get("roe"), "roe_kf": (fund or {}).get("roe_kf"),
                    "dr": (fund or {}).get("dr"), "gm": (fund or {}).get("gm"),
                    "np_yoy": (fund or {}).get("np_yoy"), "kf_np_yoy": (fund or {}).get("kf_np_yoy"),
                    "report": (fund or {}).get("report") or "", "dy": dy,
                    "in_watch": code in watch_set,
                    "blogger_n": (blogger_map.get(code) or (None, []))[0],
                    "blogger_list": (blogger_map.get(code) or (None, []))[1]}
        except Exception:  # noqa: BLE001
            return None
    out_rows, scanned = [], 0
    with ThreadPoolExecutor(max_workers=10) as ex:
        for res in ex.map(_one, universe):
            scanned += 1
            if res:
                out_rows.append(res)
    # —— 综合分 v2：形态/基本面/长窗worth 权重柱（缺项摊派）+ 国资系 0~15 加分 ——
    #   形态 = 低位分位50% + 平稳度30% + 横盘20%；基本面 = 估值50%+ROE30%+负债20%；
    #   长窗worth = 250/500/750 三档 30:40:30。
    for r in out_rows:
        low_score = max(0.0, (low_pct - (r["pct_all"] if r["pct_all"] is not None else 100.0)) / max(1.0, low_pct)) * 100.0
        cv = r["cv_pct"] if r["cv_pct"] is not None else 40.0
        flat_score = max(0.0, (40.0 - cv) / 40.0) * 100.0
        slope = abs(r["slope_yr_pct"]) if r["slope_yr_pct"] is not None else 15.0
        slope_score = max(0.0, (15.0 - slope) / 15.0) * 100.0
        shape = 0.5 * low_score + 0.3 * flat_score + 0.2 * slope_score
        fund_s = _fund_score({"roe": r.get("roe"), "dr": r.get("dr")}, {"score": r.get("val_score")})
        worth_score = r.get("long_worth")
        # 2026-10-13 修复：某维度分数可能缺（如 long_worth 因 K 线不足为 None），
        # 直接 s*w 会 TypeError → 整次扫描 500（默认股票池偶发）。改为"缺项不计入权重柱、该维度得 0 分"（摊派给有数据维度）。
        pillars = []
        if shape is not None:
            pillars.append((shape, w_shape))
        if worth_score is not None:
            pillars.append((worth_score, w_worth))
        if fund_s is not None:
            pillars.append((fund_s, w_fund))
        tw = sum(w for _s, w in pillars)
        base = sum(float(s) * w for s, w in pillars) / tw if tw else 0.0
        bonus = 0.0
        bonus += {"国家队": 15.0, "央国企": 12.0, "险资": 9.0,
                  "基金/机构": 6.0, "北向": 4.0}.get(r.get("state_tag") or "", 0.0)
        if r.get("holder_tag") == "国资实控":
            bonus += 5.0
        bonus = min(bonus, 15.0)
        ok_q, q_bad = _quality_pass({"roe": r.get("roe"), "dr": r.get("dr")}, {"pe": r.get("pe")}, quality)
        r["fund_score"] = fund_s
        r["state_bonus"] = bonus
        r["quality_flag"] = q_bad or ("数据缺" if fund_s is None else "")
        r["score"] = round(min(100.0, base + bonus), 1)
        r["low_score"] = round(shape, 1)
        r["quality_pass"] = ok_q
    if quality != "off":
        out_rows = [r for r in out_rows if r["quality_pass"]]
    out_rows.sort(key=lambda x: -x["score"])
    _lowbox_scan_cache_save(out_rows)   # 供 /api/lowbox_breakouts 突破雷达复评
    out_rows = out_rows[:limit]
    return {"ok": True, "rows": out_rows, "universe_size": len(universe),
            "scanned": scanned, "elapsed": round(_t.time() - _t0, 1),
            "params": {"limit": limit, "box_window": box_window, "low_pct": low_pct,
                       "quality": quality,
                       "w_shape": w_shape, "w_fund": w_fund, "w_worth": w_worth,
                       "note": "综合分=形态%.0f%%+基本面%.0f%%+长窗worth(250/500/750·30:40:30)%.0f%%（缺项摊派）"
                               "+国资系加分0~15；质量档=%s；PE/ROE/负债率/股息率/实控人已接入"
                               % (w_shape, w_fund, w_worth, quality or "不限")}}


# ================= 板块三 · 长期底部埋伏（2026-10-21 进哥需求）=================
#   与「长期低位震荡」(api_screener_lowbox) 的分工，别混：
#     lowbox —— 近 N 日箱体 + 全历史分位，股票池 = 关注池 ∪ 板块一Top30（至多 80 只），看的是「一年上下」；
#     bottom —— 自适应底部平台（压缩比 / 近一年止跌 / 底部抬升），**全市场**扫描，
#               按 1 / 2 / 3 / 5 / 8 年分档，专挑「以前有过高峰、现在长期在底线徘徊」的埋伏票。
#   进哥原话（2026-10-21）：「有的股票自打上市以后，第一次的底比如 10 块，后来涨到 100 多，
#   那后来的底都平均在 20 了，你这个底就不能还是以 10 为标准」→ 故算法不用全历史最低价做锚。
#   数据底座：data/kline_daily.db（全市场前复权全历史日K，tools/build_kline_db.py 落库）；
#   算法实现：viewer/bottom.py；结果缓存：data/bottom_scan_cache.json（12 小时内复用）。
_BOTTOM_CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             "data", "bottom_scan_cache.json")
_BOTTOM_KDB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                           "data", "kline_daily.db")
_BOTTOM_CACHE_TTL_H = 12
_BOTTOM_TIERS = (1, 2, 3, 5, 8)


def _bottom_names() -> dict:
    """代码 → 名称。优先 market_pool_cache.json（一次读出最快），回退 DB.stock_names。"""
    p = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                     "data", "market_pool_cache.json")
    try:
        with open(p, encoding="utf-8") as fh:
            pool = (json.load(fh) or {}).get("pool") or {}
        if isinstance(pool, dict) and pool:
            return {str(k): str(v) for k, v in pool.items()}
    except Exception:  # noqa: BLE001
        pass
    out = {}
    try:
        for r in DB.conn.execute("SELECT code, name FROM stock_names LIMIT 8000"):
            out[str(r[0])] = str(r[1] or "")
    except Exception as e:  # noqa: BLE001 —— P2-03：源错误记录日志，不再静默吞
        logging.warning("stock_names 读取失败（接口将退化为空表）：%s", e)
    return out


def _bottom_cache_load(allow_stale: bool = False):
    """读扫描缓存。allow_stale=True 时忽略 12h TTL（用于先给旧数据、后台再刷）。"""
    try:
        if not os.path.exists(_BOTTOM_CACHE):
            return None
        with open(_BOTTOM_CACHE, encoding="utf-8") as fh:
            dd = json.load(fh)
        ts = str(dd.get("ts") or "")
        if not ts or not dd.get("rows"):
            return None
        age_h = (datetime.now() - datetime.fromisoformat(ts)).total_seconds() / 3600.0
        if age_h > _BOTTOM_CACHE_TTL_H and not allow_stale:
            return None
        return dd
    except Exception:  # noqa: BLE001
        return None


def _bottom_cache_save(rows: list, scanned: int, elapsed: float) -> None:
    try:
        os.makedirs(os.path.dirname(_BOTTOM_CACHE), exist_ok=True)
        tmp = _BOTTOM_CACHE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                       "scanned": scanned, "elapsed": elapsed, "rows": rows},
                      fh, ensure_ascii=False)
        os.replace(tmp, _BOTTOM_CACHE)
    except Exception:  # noqa: BLE001
        logging.warning("bottom 扫描缓存写入失败", exc_info=True)


def _bottom_scan_rows() -> dict:
    """全市场扫一遍：逐只读本地前复权日K → bottom.platform_metrics → 按埋伏分降序。

    每只 4~5ms（Python 计算 + sqlite 读），5574 只 × 8 线程约 5~10s；
    结果落缓存，之后 12 小时直接命中。
    """
    import sqlite3
    import bottom as BTM
    from concurrent.futures import ThreadPoolExecutor
    t0 = time.time()
    if not os.path.exists(_BOTTOM_KDB):
        raise RuntimeError("日K库不存在：请先跑 python tools/build_kline_db.py --full")
    uri = "file:%s?mode=ro" % _BOTTOM_KDB.replace("\\", "/").replace("\\", "/").replace("\\", "/")
    uri = uri.replace("\\", "/")
    names = _bottom_names()
    tl = threading.local()

    def _con():
        c = getattr(tl, "con", None)
        if c is None:
            c = sqlite3.connect(uri, uri=True, timeout=10)
            tl.con = c
        return c

    def _one(code):
        try:
            bars = _con().execute(
                "SELECT d,o,c,h,l,v FROM kline WHERE code=? ORDER BY d", (code,)).fetchall()
            if not bars:
                return None
            m = BTM.platform_metrics(bars)
            if not m.get("ok"):
                return None
            nm = names.get(code) or ""
            return {"code": code, "name": nm, "st": ("ST" in nm.upper()),
                    "cur": m["cur"], "score": m["score"], "years": m["platform_years"],
                    "box_low": m["box_low"], "box_high": m["box_high"],
                    "width_pct": m["box_width_pct"], "compress": m["compress"],
                    "pos": m["pos_in_box"], "pct_all": m["pct_all"],
                    "peak": m["peak"], "peak_ago": m["peak_ago_years"], "dd": m["dd_from_peak"],
                    "lift": m["lift_ratio"], "mid_ratio": m.get("box_mid_ratio"),
                    "slope": m["slope_yr_pct"], "slope_1y": m.get("slope_1y_pct"),
                    "cv": m["cv_pct"], "touches": m["touches"], "vol_ratio": m["vol_ratio"],
                    "state": BTM.classify_state(m), "started": m["started"],
                    "bo_state": m["bo_state"], "rise_from_floor": m["rise_from_floor"]}
        except Exception:  # noqa: BLE001
            return None

    c0 = sqlite3.connect(uri, uri=True, timeout=10)
    try:
        codes = [r[0] for r in c0.execute("SELECT DISTINCT code FROM kline")]
    finally:
        c0.close()
    # 单线程顺序扫：实测 8 线程反而 142s（GIL + 1.1GB sqlite 随机读的磁盘竞争），
    # 单线程约 24s 更快。真正的解是不让它在前台跑，见 _bottom_scan_async。
    rows, fail = [], 0
    total_n = len(codes)
    for i, code in enumerate(codes, 1):
        r = _one(code)
        if r:
            rows.append(r)
        else:
            fail += 1
        if i % 800 == 0:
            logging.info("bottom scan 进度 %d/%d 命中 %d", i, total_n, len(rows))
    rows.sort(key=lambda x: -x["score"])
    el = round(time.time() - t0, 1)
    logging.info("bottom scan: codes=%d hit=%d fail=%d %ss", len(codes), len(rows), fail, el)
    return {"rows": rows, "scanned": len(codes), "fail": fail, "elapsed": el}


_BOTTOM_SCAN_ST = {"running": False, "started_at": 0.0, "done_at": 0.0, "err": None, "hits": 0}
_BOTTOM_SCAN_LOCK = threading.Lock()


def _bottom_scan_async(force: bool = False) -> dict:
    """后台起一个扫描线程（同一时刻只允许一个），返回当前状态。

    为什么必须异步：单线程全市场约 24s，纯 Python 计算会持 GIL 把整个服务卡住
    （项目历史上 pillars_monday_refresh 36.7s、hotspots 138s 就是这么把接口冻住的，
    见 tools/sched_job.py 头部实测记录）。这里只回状态，前端轮询。
    """
    with _BOTTOM_SCAN_LOCK:
        if _BOTTOM_SCAN_ST["running"] and not force:
            return dict(_BOTTOM_SCAN_ST)
        if _BOTTOM_SCAN_ST["running"]:
            return dict(_BOTTOM_SCAN_ST)
        _BOTTOM_SCAN_ST.update({"running": True, "started_at": time.time(), "err": None})

    def _run():
        try:
            res = _bottom_scan_rows()
            _bottom_cache_save(res["rows"], res["scanned"], res["elapsed"])
            _BOTTOM_SCAN_ST.update({"done_at": time.time(), "hits": len(res["rows"])})
        except Exception as e:  # noqa: BLE001
            logging.exception("bottom scan 后台任务失败")
            _BOTTOM_SCAN_ST["err"] = str(e)
        finally:
            _BOTTOM_SCAN_ST["running"] = False

    threading.Thread(target=_run, name="bottom-scan", daemon=True).start()
    return dict(_BOTTOM_SCAN_ST)


def api_dragon_map() -> dict:
    """GET /api/dragon_map —— 全市场龙头分级的轻量映射 {code: 3金/2银/1铜} + meta。

    为什么只给 tier 不给明细：全市场 5500+ 只，明细带上六项原值有 1MB+；
    徽章只需要 tier，明细点开单只再取（见 api_dragon_one）。
    """
    if dragon_mod is None:
        return {"ok": False, "reason": "龙头分级模块未加载", "m": {}, "meta": {}}
    try:
        if dragon_mod.age_hours() > 24:
            dragon_mod.build_async()      # 过期先给旧数据，后台重建（不阻塞前端）
    except Exception:  # noqa: BLE001
        logging.warning("[dragon] 过期重建触发失败", exc_info=True)
    return dragon_mod.map_payload()


def api_dragon_one(code: str) -> dict:
    """GET /api/dragon?code=600519 —— 单只龙头明细（行业第几/共几家/六项分项排名）。"""
    if dragon_mod is None:
        return {"ok": False, "reason": "龙头分级模块未加载"}
    return dragon_mod.one(code)


def api_screener_bottom(min_years: float = 0, state: str = "all", pool: str = "market",
                        codes: str = "", limit: int = 100, refresh: int = 0,
                        include_st: int = 0, prefix: str = "", min_score: float = 0,
                        medal: str = "all") -> dict:
    """GET /api/screener_bottom —— 板块三：长期底部埋伏（全市场 · 自适应平台）。

    参数：
      min_years  底部时长下限（0=全部；1/2/3/5/8 对应进哥定的分档）
      state      all | dormant（仅埋伏中·未启动）| started（仅刚启动/突破）
      pool       market（全市场）| watch（只留关注池）| custom（只留 codes 指定）
      codes      逗号分隔代码（pool=custom 时生效）
      refresh    1=绕过缓存强制重扫
      prefix     2026-10-21 进哥需求：代码开头筛选（""=全部 / "0" / "3" / "6"）
      min_score  2026-10-21 进哥需求：埋伏分下限（0=全部；60/70/80…）
      medal      2026-10-21 进哥需求：奖牌等级（all=全部 / "3"金牌 / "2"银牌 / "1"铜牌 / "0"无牌），
                 口径=行业龙头分级（金牌=行业综合排名前5%、银=前10%、铜=前15%，与个股徽章同一套）
    返回 counts 为「≥N 年」的累计只数（在 pool/state 过滤之后统计），供前端分档按钮显示数量；
    medal_counts / prefix_counts 同口径（pool/state/st 之后、其余新筛选之前），供奖牌/前缀 chip 显示数量。
    """
    limit = max(5, min(int(limit or 100), 300))
    try:
        min_years = max(0.0, min(float(min_years or 0), 20.0))
    except (TypeError, ValueError):
        min_years = 0.0
    state = state if state in ("all", "dormant", "started") else "all"
    pool = pool if pool in ("market", "watch", "custom") else "market"
    # ── 2026-10-21 进哥需求：奖牌 / 代码前缀 / 埋伏分 三组新筛选（参数归一化）──
    prefix = str(prefix or "").strip()
    if prefix not in ("", "0", "3", "6"):
        prefix = ""
    medal = str(medal or "all").strip()
    if medal not in ("all", "3", "2", "1", "0"):
        medal = "all"
    try:
        min_score = max(0.0, float(min_score or 0))
    except (TypeError, ValueError):
        min_score = 0.0

    cached = False
    stale = False
    if not refresh:
        dd = _bottom_cache_load()
        if dd is None:
            # 过期缓存不丢弃：先返回旧数据（stale-while-revalidate），后台静默重扫。
            # 否则用户隔天打开面板，会先看到 20~30 秒空白「正在扫描」。
            dd = _bottom_cache_load(allow_stale=True)
            stale = bool(dd)
    else:
        dd = None
    if dd:
        rows = dd["rows"]
        scanned, elapsed, ts = dd.get("scanned"), dd.get("elapsed"), dd.get("ts")
        cached = True
        if stale:
            try:
                _bottom_scan_async(force=False)
            except Exception:  # noqa: BLE001
                logging.warning("bottom 后台重扫启动失败", exc_info=True)
    else:
        st = _bottom_scan_async(force=bool(refresh))
        if st.get("err"):
            return {"ok": False, "reason": "扫描失败：%s" % st["err"], "rows": [], "total": 0}
        return {"ok": False, "computing": True, "rows": [], "total": 0, "counts": {},
                "all_total": 0, "elapsed": round(time.time() - (st.get("started_at") or time.time()), 1),
                "reason": "首次扫描全市场前复权日K（约 20~30 秒），结果缓存 12 小时"}

    base = rows
    # 默认排除 ST/*ST：退市边缘的「长期底部」不是埋伏标的（实测 748 只里 23 只 ST，
    # 且分数集中在榜首——跌 85%+ 趴 8 年，形态符合但基本面是雷）。要看得手动开。
    if not include_st:
        base = [r for r in base if not r.get("st")]
    if pool == "watch":
        try:
            watchlib.ensure_schema(DB.conn)
            ws = {it["symbol"] for it in (watchlib.list_items(DB.conn) or [])}
            base = [r for r in base if r["code"] in ws]
        except Exception:  # noqa: BLE001
            base = []
    elif pool == "custom":
        want = {c.strip() for c in re.split(r"[\s,，、]+", codes or "") if c.strip()}
        base = [r for r in base if r["code"] in want] if want else []
    if state == "dormant":
        base = [r for r in base if not r["started"]]
    elif state == "started":
        base = [r for r in base if r["started"]]

    # ── 2026-10-21 进哥需求：奖牌 / 代码前缀 / 埋伏分 筛选 ──
    # 分档数量（counts / medal_counts / prefix_counts）统一在 pool/state/ST 之后、本组新筛选之前
    # 统计，与「≥N 年」chip 的既有口径一致：数量反映「当前池子里还有什么可切」，不随自身筛选归零。
    counts = {str(t): sum(1 for r in base if r["years"] >= t) for t in _BOTTOM_TIERS}
    prefix_counts = {p: sum(1 for r in base if str(r["code"]).startswith(p)) for p in ("0", "3", "6")}
    dmap = {}
    try:
        if dragon_mod is not None:
            dmap = (dragon_mod.map_payload() or {}).get("m") or {}
    except Exception:  # noqa: BLE001
        dmap = {}
    if dmap:
        _tiers = {str(r["code"]): int(dmap.get(str(r["code"]), 0)) for r in base}
        medal_counts = {"3": sum(1 for v in _tiers.values() if v == 3),
                        "2": sum(1 for v in _tiers.values() if v == 2),
                        "1": sum(1 for v in _tiers.values() if v == 1),
                        "0": sum(1 for v in _tiers.values() if v == 0)}
    else:
        medal_counts = {}
    # 应用三组新筛选（顺序：前缀 → 埋伏分 → 奖牌；奖牌筛选失败要给出明确原因，绝不静默放行）
    if prefix:
        base = [r for r in base if str(r["code"]).startswith(prefix)]
    if min_score:
        base = [r for r in base if r.get("score") is not None and float(r["score"]) >= min_score]
    if medal != "all":
        if not dmap:
            return {"ok": False,
                    "reason": "龙头分级数据尚未生成（奖牌筛选依赖它），约 1~2 分钟后自动重建，请稍候重试",
                    "rows": [], "total": 0, "counts": counts, "prefix_counts": prefix_counts,
                    "medal_counts": {}, "all_total": 0}
        want = int(medal)
        base = [r for r in base if int(dmap.get(str(r["code"]), 0)) == want]
    out = [r for r in base if r["years"] >= min_years] if min_years else list(base)
    total = len(out)
    out = out[:limit]
    return {"ok": True, "rows": out, "total": total, "counts": counts,
            "medal_counts": medal_counts, "prefix_counts": prefix_counts,
            "all_total": len(rows), "scanned": scanned, "elapsed": elapsed,
            "ts": ts, "cached": cached, "stale": stale, "computing": bool(stale),
            "params": {"min_years": min_years, "state": state, "pool": pool,
                       "prefix": prefix, "min_score": min_score, "medal": medal,
                       "limit": limit, "tiers": list(_BOTTOM_TIERS),
                       "note": "自适应底部平台：压缩比≤0.85 + 近一年止跌 + 箱内位置≤0.75；"
                               "埋伏分=时长32%%+低位22%%+回撤18%%+横盘18%%+启动10%%"}}


# ================= 入手概率 · 归档 / 时间轴（2026-10-05 王总需求）=================
def api_rate_snapshot(days: int = 30, limit: int = 30, rows: list = None,
                      src: str = "auto", trade_date: str = "") -> dict:
    """POST /api/rate/snapshot —— 把当前这版入手概率榜归档成当日快照（同日覆盖、幂等）。

    rows 由前端顺手带回（榜刚拉回来就存，秒回）；**不带 rows 才重跑全市场榜**（数分钟），
    那是给命令行/兜底用的慢路。归档的时间轴价值全在「存得勤」，故这条接口越轻越好。
    """
    if rows is None:
        d = screener.turnover_top(days=days, limit=limit)
        if not d.get("ok"):
            return d
        return dict(d, snapshot=screener.snapshot_top(d.get("rows") or [],
                                                      src=src,
                                                      trade_date=d.get("trade_date") or ""))
    rows = [r for r in (rows or []) if isinstance(r, dict)]
    return {"ok": True, "snapshot": screener.snapshot_top(rows, src=src,
                                                          trade_date=trade_date or "")}


def api_rate_timeline(days_window: int = 10, per_day: int = 5) -> dict:
    """GET /api/rate/timeline —— 时间轴：各归档日的 T+1/3/5/10 收盘价与入市价比值。"""
    return screener.rate_timeline(days_window=days_window, per_day=per_day)


# ================= 增强功能③ 估值 / 诊股评分 =================
def api_valuation(symbol: str) -> dict:
    return valuation.score(symbol)


# ================= 个股诊断（2026-09-29，合并「个股资金流 + 估值诊股」）=================
# 一次请求聚合：行情+财务+分红+公司概况（复用 api_quote，自带 60s/30min/6h 缓存）
# + 估值评分（valuation.score）+ 个股资金流（东财主源，失败切新浪兜底）
# + 最近公告（newsfeed.stock → emapi.announcement，加缓存）+ 专家综合分析（本地规则引擎）。
_MF_SINA_CACHE: dict[str, tuple[float, object]] = {}
_MF_SINA_TTL = 120        # 新浪资金流正面缓存 2 分钟（盘中口径）
_MF_SINA_NEG_TTL = 60     # 失败负缓存 1 分钟
_ANN_CACHE: dict[str, tuple[float, object]] = {}
_ANN_TTL = 30 * 60        # 公告正面缓存 30 分钟
_ANN_NEG_TTL = 120        # 公告失败负缓存 2 分钟（限流窗口内不再反复挂）
_DIAG_CACHE: dict[str, tuple[float, dict]] = {}
_DIAG_TTL = 60            # 聚合结果 60s（与 api_quote 行情缓存对齐）
_BOARD_CACHE: dict[tuple[str, str], tuple[float, dict]] = {}
_BOARD_TTL = 120         # 板块详情聚合 120s（成分股/资金/估值变化慢，重复查看瞬时）


def _sina_stock_moneyflow(code: str, market: str) -> dict | None:
    """新浪个股资金流（东财 push2 被限流时的兜底源，2026-09-29 实测稳定）。
    口径：netamount=当日主力净流入(元)、r0_net=超大单净额(元)、ratioamount=净占比(小数)、
    changeratio=涨跌幅(小数)、trade=收盘价。取最近 6 个交易日。"""
    pfx = "sh" if market == "1" else ("bj" if code[:1] in ("4", "8") else "sz")
    url = ("https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"
           f"MoneyFlow.ssl_qsfx_zjlrqs?page=1&num=6&sort=opendate&asc=0&daima={pfx}{code}")
    txt = _get_text(url, "https://finance.sina.com.cn/", timeout=6, retries=0)
    if not txt:
        return None
    try:
        raw = json.loads(txt)
    except ValueError:
        return None
    if not isinstance(raw, list) or not raw:
        return None
    rows = [r for r in raw if isinstance(r, dict) and r.get("opendate")]
    if not rows:
        return None
    last = rows[0]  # asc=0 → 第一条即最近交易日
    _d = str(last.get("opendate") or "")[:10]
    # 口径标注：仅当「最近交易日 == 今天且盘中」才算当日盘中值，否则为该交易日的日终值
    _intra = bool(_d == time.strftime("%Y-%m-%d") and _is_trading_now())
    return {
        "ok": True, "symbol": code, "source": "sina",
        "source_label": "新浪财经（东财限流，已自动兜底）",
        "date": _d, "data_date": _d,
        "data_time": (time.strftime("%Y-%m-%d %H:%M:%S") if _intra else None),
        "is_intraday": _intra,
        "price": _num(last.get("trade")),
        "pct": round((_num(last.get("changeratio")) or 0) * 100, 2),
        "main_net": _num(last.get("netamount")),
        "main_pct": round((_num(last.get("ratioamount")) or 0) * 100, 2),
        "huge_net": _num(last.get("r0_net")),
        "big_net": None, "mid_net": None, "small_net": None,
        "recent": [{"date": str(r.get("opendate") or "")[:10], "main_net": _num(r.get("netamount"))}
                   for r in rows],
    }


def _stock_moneyflow_any(symbol: str, force: bool = False) -> dict:
    """个股资金流：东财主源（moneyflow.stock，带明细四档）→ 失败切新浪兜底（三档省略）。
    两路都失败时返回东财的失败原因（描述更完整）。
    2026-10-01：force 透传给 moneyflow.stock，跳过其 30 分钟失败负缓存（强制重试用）。"""
    r = moneyflow.stock(symbol, force=force)
    if r.get("ok"):
        return r
    try:
        market, code, _ = _em_secid(symbol)
    except Exception:  # noqa: BLE001
        return r
    if not re.match(r"^\d{6}$", code or ""):
        return r
    hit = _MF_SINA_CACHE.get(code)
    if hit and time.time() - hit[0] < (_MF_SINA_TTL if hit[1] else _MF_SINA_NEG_TTL):
        s = hit[1]
    else:
        s = _sina_stock_moneyflow(code, market)
        _MF_SINA_CACHE[code] = (time.time(), s if isinstance(s, dict) else None)
    if isinstance(s, dict) and s.get("ok"):
        return s
    return r


def _announcements_cached(symbol: str, num: int = 8) -> dict:
    """最近公告（newsfeed.stock 复用 emapi.announcement），加进程缓存：
    正面 30min / 失败负缓存 2min —— 公告以天为单位，且避免限流时反复挂超时。"""
    code = re.sub(r"\D", "", symbol or "")
    if not code:
        return {"ok": False, "rows": [], "reason": "非 A 股数字代码"}
    hit = _ANN_CACHE.get(code)
    if hit and time.time() - hit[0] < (_ANN_TTL if hit[1].get("ok") else _ANN_NEG_TTL):
        return hit[1]
    r = newsfeed.stock(code, num=num)
    _ANN_CACHE[code] = (time.time(), r)
    return r


def _expert_verdict(fin: dict, val: dict, mf: dict, dv: dict | None) -> dict:
    """专家综合分析（本地规则引擎，零 LLM 依赖）：五因子加权 0–100 → 操作倾向 + 短/中/长期观点。
    每条结论都带真实数据依据；缺数据的因子如实剔除权重并在 coverage 里说明。"""
    parts: list[tuple[str, float, float]] = []   # (名称, 权重, 得分)
    reasons: list[dict] = []

    # ① 盈利质量（权重 0.30）：ROE 为主，毛利率/净利率辅助定性
    roe = fin.get("roe"); gm = fin.get("gross_margin")
    if roe is not None:
        sc = 100 if roe >= 15 else 80 if roe >= 10 else 55 if roe >= 5 else (35 if roe > 0 else 10)
        parts.append(("盈利质量", 0.30, sc))
        if roe >= 15:
            reasons.append({"tone": "good", "text": f"盈利能力强：ROE(加权) {roe:.1f}%"
                            + (f"，毛利率 {gm:.1f}%" if gm is not None else "")})
        elif roe >= 10:
            reasons.append({"tone": "good", "text": f"盈利中等偏上：ROE(加权) {roe:.1f}%"
                            + (f"，毛利率 {gm:.1f}%" if gm is not None else "")})
        elif roe > 0:
            reasons.append({"tone": "neutral", "text": f"盈利能力一般：ROE(加权) {roe:.1f}%"})
        else:
            reasons.append({"tone": "bad", "text": f"当期盈利弱：ROE(加权) {roe:.1f}%"})
    else:
        reasons.append({"tone": "neutral", "text": "财务指标暂缺（F10 未取到或被限流），盈利质量未计入评分"})

    # ② 成长性（0.20）：营收同比 + 净利同比均值
    rev = fin.get("revenue_yoy"); npf = fin.get("net_profit_yoy")
    gvals = [x for x in (rev, npf) if x is not None]
    if gvals:
        g = sum(gvals) / len(gvals)
        sc = 100 if g >= 20 else 80 if g >= 10 else 60 if g >= 0 else 25
        parts.append(("成长性", 0.20, sc))
        tone = "good" if g >= 10 else ("neutral" if g >= 0 else "bad")
        fmt1 = lambda v: ("—" if v is None else f"{v:.1f}%")  # noqa: E731
        reasons.append({"tone": tone, "text": f"成长性：营收同比 {fmt1(rev)}"
                        + f" / 净利同比 {fmt1(npf)}"})

    # ③ 估值（0.25）：直接复用估值诊股的透明五因子评分
    if val.get("ok"):
        parts.append(("估值", 0.25, float(val.get("score") or 0)))
        v_sc = float(val.get("score") or 0)
        v_tone = "good" if v_sc >= 60 else ("neutral" if v_sc >= 40 else "bad")
        f2 = lambda x: ("—" if x is None else f"{x:.2f}")  # noqa: E731
        reasons.append({"tone": v_tone,
                        "text": f"估值评分 {val.get('score')} 分（{val.get('verdict', '')}）："
                                f"PE {f2(val.get('pe'))} / PB {f2(val.get('pb'))} / PEG {f2(val.get('peg'))}"})

    # ④ 股息（0.10）：按完整会计年度口径的股息率
    dy = (dv or {}).get("yield_pct") if dv else None
    if dy is not None:
        sc = 100 if dy >= 4 else 85 if dy >= 3 else 60 if dy >= 2 else 40 if dy >= 1 else 20
        parts.append(("股息", 0.10, sc))
        reasons.append({"tone": "good" if dy >= 3 else "neutral",
                        "text": f"股息率 {dy}%（{(dv or {}).get('basis', '')}口径，税前）"})

    # ⑤ 资金面（0.15）：近 6 日主力净流入趋势
    recent = (mf.get("recent") or []) if mf.get("ok") else []
    if recent:
        nets = [x.get("main_net") for x in recent if x.get("main_net") is not None]
        if nets:
            sum6 = sum(nets)
            sum3 = sum(nets[:3])
            yi = lambda v: f"{v / 1e8:.2f} 亿"  # noqa: E731
            sc = (80 if (sum3 > 0 and sum6 > 0) else 65 if sum3 > 0
                  else 55 if sum6 > 0 else 30)
            parts.append(("资金面", 0.15, sc))
            if sum3 > 0:
                reasons.append({"tone": "good", "text": f"资金面偏强：近 3 日主力累计净流入 {yi(sum3)}"
                                + (f"、近 6 日 {yi(sum6)}" if len(nets) > 3 else "")})
            elif sum3 < 0 and sum6 < 0:
                reasons.append({"tone": "bad", "text": f"资金面偏弱：近 3 日主力累计净流出 {yi(-sum3)}"
                                + f"、近 6 日 {yi(-sum6)}"})
            else:
                reasons.append({"tone": "neutral",
                                "text": f"资金面反复：近 6 日主力累计净{'流入' if sum6 >= 0 else '流出'} {yi(abs(sum6))}"})

    if not parts:
        return {"ok": False, "reason": "五项数据全部缺失，无法生成分析（可能网络受限，稍后重试）"}

    tot_w = sum(w for _, w, _ in parts)
    total = round(sum(s * w for _, w, s in parts) / tot_w)
    n_all = 5
    coverage = f"{len(parts)}/{n_all} 项数据参与评分" + ("（部分因子缺数据已剔除权重）" if len(parts) < n_all else "")

    if total >= 70:
        action, tone = "偏多 · 可逢低分批关注", "good"
    elif total >= 58:
        action, tone = "中性偏多 · 持有为主", "mid"
    elif total >= 45:
        action, tone = "中性 · 观望", "neutral"
    else:
        action, tone = "偏空 · 谨慎，不宜追高", "bad"

    # 短/中/长期三段观点
    yld = dy
    if mf.get("ok") and recent:
        nets3 = [x.get("main_net") for x in recent[:3] if x.get("main_net") is not None]
        s3 = sum(nets3) if nets3 else None
        short = ("资金连续净流入，短期股价有支撑，回调可视为机会" if (s3 or 0) > 0
                 else "主力持续净流出，短期谨防惯性回调，不宜急于介入" if (s3 or 0) < 0
                 else "资金进出反复，短期方向不明，以观望为主")
    else:
        short = "资金流向数据暂缺，短期方向不明，建议观望等数据补齐"
    vsc = val.get("score") if val.get("ok") else None
    g = (sum(gvals) / len(gvals)) if gvals else None
    # 2026-10-15 修（全站回测抓到 · 真 500）：旧写法在 else 分支用 `vsc is not None or g is not None`
    # 决定要不要拼 `成长 {g:.1f}%` —— 只要 vsc 有值、g 为 None，就走进 f-string 对 None 做 .1f 格式化，
    # 抛 TypeError: unsupported format string passed to NoneType.__format__ → /api/diag 整个 500。
    # 改为：先按「各自是否有值」分别拼片段，缺一项就不提它（也顺带修掉 if 分支 `and` / else 分支 `or` 的不一致）。
    _dt = []
    if vsc is not None:
        _dt.append(f"估值 {vsc} 分")
    if g is not None:
        _dt.append(f"成长 {g:.1f}%")
    _dtxt = ("（" + "、".join(_dt) + "）") if _dt else ""
    mid = ("估值与业绩匹配度尚可" + _dtxt + "，1–3 个月维度有修复空间"
           if (vsc or 0) >= 55 and (g is None or g >= 0)
           else "估值与成长至少一项偏弱" + _dtxt + "，中期弹性有限，等待右侧信号")
    long = ""
    if roe is not None and yld is not None:
        long = (f"长期视角{'较好' if roe >= 10 else '一般'}：ROE {roe:.1f}%"
                + (f"、股息率 {yld}% 提供安全垫" if yld >= 3 else "，股息保护有限")
                + "；单期财务数据不代表长期趋势，建议跟踪 2–3 个报告期再下结论")
    else:
        long = "长期判断需要盈利与股息数据支撑，当前数据不全，暂不做长期结论"

    return {"ok": True, "score": total, "action": action, "action_tone": tone,
            "horizon": {"short": short, "mid": mid, "long": long},
            "reasons": reasons, "coverage": coverage,
            "disclaimer": "规则引擎基于公开数据的量化打分，非券商研报、不构成投资建议；"
                          "短/中/长期观点为规则推断，非预测承诺。"}


# ================= 增强功能③ 板块详情页（A1：2026-09-29 王总要求补下钻层级）============
def api_board(code: str, kind: str = "industry", name: str = "", force: int = 0) -> dict:
    """板块详情页聚合：成分股全列表 + 板块行情/资金 + 板块内个股资金对比 + 板块内龙虎榜 + 估值分位。

    数据全部复用现成稳定源（不新接脆弱接口、不触东财风控）：
      · 成分股：board_flow.board_members（新浪 getHQNodeData，单板块上限 100 只）
      · 板块行情/资金：board_flow.snapshot 按 board_code 取该行
      · 板块内个股资金对比：_stock_moneyflow_any 批量（东财主源 → 新浪兜底，前 20 只）
      · 板块内龙虎榜：本地 lhb_stocks（零网络）
      · 估值分位：valuation.score 批量（前 30 只，已有缓存）
    """
    if not code:
        return {"ok": False, "reason": "缺少板块代码（board_code）"}
    kind = kind if kind in board_flow.KINDS else board_flow.DEFAULT_KIND
    ck = (code, kind)
    if not force:
        _bh = _BOARD_CACHE.get(ck)
        if _bh and time.time() - _bh[0] < _BOARD_TTL:
            return _bh[1]
    # ① 成分股全列表
    members = board_flow.board_members(code, 100) or []
    if not members:
        return {"ok": False, "reason": "未取到该板块成分股（代码可能失效或新浪源不可达）", "code": code}
    codes = [m["code"] for m in members]
    # ② 板块行情/资金
    snap = board_flow.snapshot(kind=kind, pz=200, force=bool(force))
    brow = next((r for r in snap.get("rows", []) if r.get("board_code") == code), None)
    # ③ 板块内个股资金对比（前 20 只并发；新浪主源，东财仅作兜底——与本机 board_flow 架构一致，
    #    避免逐只打东财 fflow（本机东财常不可达，每只 ~4s 才失败）拖出 14s 冷启动）
    flow_top = members[:20]
    flow_map: dict = {}

    def _board_flow_one(m):
        sym = m["code"]
        try:
            mk, cd, _ = _em_secid(sym)
        except Exception:  # noqa: BLE001
            mk, cd = ("1" if str(sym).startswith(("6", "9")) else "0"), str(sym)
        s = _sina_stock_moneyflow(cd, mk)
        if isinstance(s, dict) and s.get("ok"):
            return s
        return _stock_moneyflow_any(sym)  # 东财兜底（含四档明细）

    try:
        with cf.ThreadPoolExecutor(max_workers=8) as ex:
            futs = {ex.submit(_board_flow_one, m): m["code"] for m in flow_top}
            for fut in futs:
                c = futs[fut]
                try:
                    flow_map[c] = fut.result(timeout=12) or {}
                except Exception:  # noqa: BLE001
                    flow_map[c] = {}
    except Exception:  # noqa: BLE001
        pass
    # ④ 板块内龙虎榜（最近交易日，本地库）
    _lhb_row = DB.conn.execute("SELECT MAX(trade_date) AS d FROM lhb_stocks").fetchone()
    lhb_date = _lhb_row["d"] if _lhb_row else None
    lhb_rows = []
    if lhb_date and codes:
        ph = ",".join("?" * len(codes))
        for r in DB.conn.execute(
                f"SELECT security_code, security_name, change_rate, net_amt, reason "
                f"FROM lhb_stocks WHERE trade_date=? AND security_code IN ({ph})",
                (lhb_date, *codes)):
            lhb_rows.append({"code": r["security_code"], "name": r["security_name"],
                             "pct": _num(r["change_rate"]),
                             "net_yi": round((_num(r["net_amt"]) or 0.0) / 1e8, 2),
                             "reason": r["reason"]})
        lhb_rows.sort(key=lambda x: -(x["net_yi"] or 0))
    # ⑤ 估值分位（前 30 只并发）
    val_map: dict = {}
    try:
        with cf.ThreadPoolExecutor(max_workers=8) as ex:
            futs = {ex.submit(valuation.score, m["code"]): m["code"] for m in members[:30]}
            for fut in futs:
                c = futs[fut]
                try:
                    v = fut.result(timeout=8) or {}
                    val_map[c] = {"score": v.get("score"), "pe": v.get("pe"), "roe": v.get("roe")}
                except Exception:  # noqa: BLE001
                    val_map[c] = {}
    except Exception:  # noqa: BLE001
        pass
    for m in members:
        c = m["code"]
        mf = flow_map.get(c) or {}
        m["main_net"] = mf.get("main_net")
        m["main_pct"] = mf.get("main_pct")
        m["val_score"] = (val_map.get(c) or {}).get("score")
    scores = [x for x in (m.get("val_score") for m in members[:30]) if isinstance(x, (int, float))]
    val_stat = {"count": len(scores),
                "avg": round(sum(scores) / len(scores), 1) if scores else None,
                "n_overweight": sum(1 for s in scores if s >= 60),
                "n_neutral": sum(1 for s in scores if 40 <= s < 60),
                "n_low": sum(1 for s in scores if s < 60)} if scores else {"count": 0}
    flow_sorted = sorted(
        [{"code": m["code"], "name": m["name"], "main_net": m.get("main_net"), "pct": m.get("pct")}
         for m in members if m.get("main_net") is not None],
        key=lambda x: -(x["main_net"] or 0))
    payload = {
        "ok": True, "code": code, "kind": kind,
        "board_name": name or (brow or {}).get("board_name") or code,
        "board": brow,
        "member_total": len(members),
        "members": members,
        "flow_compare": flow_sorted[:30],
        "lhb": {"date": lhb_date, "rows": lhb_rows},
        "valuation": val_stat,
        "note": "成分股取自新浪（单板块上限 100 只）；资金/估值为板块内前 N 只聚合，非全量精算；"
                "仅供研究参考，非投资建议。",
    }
    _BOARD_CACHE[ck] = (time.time(), payload)
    return payload


def api_diag(symbol: str, force: bool = False) -> dict:
    """个股诊断聚合接口：一次返回行情/财务/分红/概况/估值评分/资金流/公告/专家分析。"""
    key = (symbol or "").strip()
    if not re.match(r"^\d{6}$", key):
        return {"ok": False, "reason": "请输入 6 位 A 股数字代码（如 600519）"}
    if not force:
        hit = _DIAG_CACHE.get(key)
        if hit and time.time() - hit[0] < _DIAG_TTL:
            return hit[1]
    market, code, _ = _em_secid(key)
    with cf.ThreadPoolExecutor(max_workers=4) as ex:
        f_q = ex.submit(api_quote, key, bool(force))
        f_v = ex.submit(valuation.score, key)
        f_m = ex.submit(_stock_moneyflow_any, key)
        f_a = ex.submit(_announcements_cached, code, 8)
        q = f_q.result()
        v = f_v.result()
        m = f_m.result()
        ann = f_a.result()
    if not isinstance(q, dict) or q.get("error"):
        # 行情失败不整页报废：估值/资金流/公告仍可用，行情段如实标注
        q_err = (q or {}).get("error") if isinstance(q, dict) else "行情获取失败"
        q_fin: dict = {}
        q_dv = None
        q_name = ""
        q_quote = None
        q_org = None
    else:
        q_err = None
        q_fin = q.get("finance") or {}
        q_dv = q.get("dividend")
        q_name = q.get("name") or ""
        q_quote = q.get("quote")
        q_org = q.get("org")
    expert = _expert_verdict(q_fin, v if isinstance(v, dict) else {},
                             m if isinstance(m, dict) else {}, q_dv)
    payload = {
        "ok": True, "symbol": code, "name": q_name,
        "quote_error": q_err,
        "quote": q_quote, "qval": (q.get("valuation") if isinstance(q, dict) else None),
        "org": q_org, "finance": q_fin, "dividend": q_dv,
        "valuation": v if isinstance(v, dict) else {"ok": False},
        "moneyflow": m if isinstance(m, dict) else {"ok": False},
        "announcements": ann if isinstance(ann, dict) else {"ok": False},
        "expert": expert,
        "disclaimer": "本页评分与建议均由本地规则引擎基于公开数据生成，仅供研究参考，不构成投资建议。",
    }
    _DIAG_CACHE[key] = (time.time(), payload)
    return payload


# ================= 增强功能③ 研报 / 机构评级 =================
def api_research_reports(pz: int = 30, page: int = 1) -> dict:
    return research.report_list(pz=pz, page=page)

def api_research_rating(symbol: str) -> dict:
    return research.rating(symbol)

# ---- 研报结论聚合（2026-10-01）：600 份研报压成「哪只票能看」的排序 ----
# 真跑一次要 ~60 秒（抓分页研报 + 批量行情），所以后台预热 + 前端轮询，绝不阻塞页面。
_RI_LOCK = threading.Lock()
_RI_STATE = {"data": None, "running": False, "started": False}


def _ri_warm(days: int = 60):
    try:
        d = research_insight.build(days=days)
        if d.get("ok"):
            _RI_STATE["data"] = d
    except Exception:  # noqa: BLE001
        pass
    finally:
        _RI_STATE["running"] = False


def api_research_insight(days: int = 60, force: bool = 0) -> dict:
    with _RI_LOCK:
        # 关键：后台正在重算时，哪怕是「不带 force 的普通轮询」也必须返回 warming。
        # 否则前端 4 秒一问都拿到内存里的旧结论，永远进不了「生成中」提示，
        # 用户看到的是旧数字，等新结论出来又是另一套 —— 看不到过程也校不上。
        if force or _RI_STATE["running"] or _RI_STATE["data"] is None:
            if not _RI_STATE["running"]:
                _RI_STATE["running"] = True
                threading.Thread(target=_ri_warm, args=(days,), daemon=True).start()
            return {"ok": False, "warming": True, "reason": "结论生成中（需抓 600 份研报，约 1 分钟），请稍后重试"}
        d = dict(_RI_STATE["data"])
    d["cached"] = not force
    return d


def api_research_insight_rebuild(days: int = 60) -> dict:
    research_insight.insight(days=days, force=True)   # 同步重算并落缓存
    with _RI_LOCK:
        _RI_STATE["data"] = None
    with _RI_LOCK:
        _RI_STATE["data"] = research_insight.insight(days=days, force=True)
    return api_research_insight(days=days)

# ================= 增强功能④ 多资产行情 =================
def api_multiquote_preset() -> dict:
    return multiquote.preset()

def api_multiquote_lookup(symbol: str) -> dict:
    return multiquote.lookup(symbol)

# ================= 增强功能④ 资讯 / 公告 =================
# 2026-09-30 研判化改造：公告不再是裸列表，逐条规则分类（业绩/股东回报/增持减持/风险监管/例行事务）
# 聚合成「该股立场研判」+ 财务指向（营收/净利同比、ROE、每股现金流，复用 api_quote 缓存）。
# 分类规则本体在 viewer/newsclass.py（唯一真源：后端调 classify、前端读 news_rules.js），
# 原本地副本已删除 —— 改前此处与 app.js 的 newsClassify 是两份独立词表。


# 2026-10-01 口径自查：新闻/公告分类规则已收编到 viewer/newsclass.py（前后端唯一真源），
# 本地不再保留副本（改前 app.js 与这里各有一份独立词表，同一标题会分叉）。
_ANN_TITLE_RULES = newsclass.ANN_TITLE_RULES
_ann_classify = newsclass.classify


def _stock_news_verdict(symbol: str, rows: list[dict]) -> dict:
    """公告流 → 研判：立场 + 指向依据（公告分类聚合 + 财务指向，机器初判口径）。"""
    cls = [_ann_classify(r.get("title") or "") for r in rows]
    n_good = sum(1 for _, tn in cls if tn == "good")
    n_bad = sum(1 for _, tn in cls if tn == "bad")
    n_rows = len(rows)
    cats: dict[str, int] = {}
    for c, _ in cls:
        cats[c] = cats.get(c, 0) + 1
    # 立场：公告面为主（风险监管一票警惕；利好密集偏正面；其余中性）
    if n_bad >= 2 or any(c == "风险监管" and tn == "bad" for c, tn in cls):
        stance = "警惕 · 公告面存在风险项"
    elif n_good >= 3 and n_bad == 0:
        stance = "偏正面 · 利好类公告密集"
    elif n_good >= 1:
        stance = "中性偏正 · 有利好但密度不高"
    else:
        stance = "中性 · 例行公告为主"

    reasons: list[str] = []
    # 公告结构依据
    if n_rows:
        mix = "、".join(f"{k} {v} 条" for k, v in sorted(cats.items(), key=lambda x: -x[1]) if k != "其他事务")
        if mix:
            reasons.append(f"近 {n_rows} 条公告构成：{mix}" + (f"（其他 {n_rows - sum(v for k, v in cats.items() if k != '其他事务')} 条）" if n_rows - sum(v for k, v in cats.items() if k != "其他事务") else ""))
        if cats.get("股东回报"):
            reasons.append(f"股东回报类 {cats['股东回报']} 条（分红/回购/增持）——回报意愿持续")
        if cats.get("股东减持"):
            reasons.append(f"存在股东减持公告 {cats['股东减持']} 条——留意筹码供给压力")
    # 财务指向依据（api_quote 自带 60s 缓存，命中失败不阻塞）
    fin_line = ""
    try:
        q = api_quote(symbol)
        if isinstance(q, dict) and not q.get("error"):
            fin = q.get("finance") or {}
            rev, npf, roe, ocf = fin.get("revenue_yoy"), fin.get("net_profit_yoy"), fin.get("roe"), fin.get("ocf_ps")
            name = q.get("name") or ""
            def _f(v):  # noqa: E306
                return "—" if v is None else f"{v:.1f}%"
            if rev is not None or npf is not None:
                trend = "增长" if ((npf if npf is not None else rev or 0) > 0) else "承压"
                # 立场修正：公告面偏暖但财报业绩承压（净利同比<0）时降档，不给「偏正面」
                if stance.startswith("偏正面") and (npf if npf is not None else rev or 0) < 0:
                    stance = "中性偏正 · 公告面偏暖但业绩承压"
                reasons.append(f"最新报告期（{fin.get('report') or '—'}）业绩指向{trend}：营收同比 {_f(rev)} / 归母净利同比 {_f(npf)}")
            if roe is not None:
                reasons.append(f"盈利能力：ROE(加权) {roe:.1f}%" + (f"，每股经营现金流 {ocf:.2f} 元" + ("（现金流充足）" if (ocf or 0) > 0 else "") if ocf is not None else ""))
            return {"stance": stance, "reasons": reasons, "name": name,
                    "coverage": f"公告 {n_rows} 条 + 财务 {len([x for x in (rev, npf, roe, ocf) if x is not None])}/4 项"}
    except Exception:  # noqa: BLE001
        pass
    return {"stance": stance, "reasons": reasons, "name": "",
            "coverage": f"公告 {n_rows} 条（财务指向暂缺，行情源未命中）"}


def api_news_stock(symbol: str, num: int = 30) -> dict:
    d = newsfeed.stock(symbol, num=num)
    if d.get("ok") and symbol:
        try:
            d["verdict"] = _stock_news_verdict(symbol, d.get("rows") or [])
        except Exception as e:  # noqa: BLE001
            d["verdict"] = {"stance": "暂缺", "reasons": [], "name": "", "coverage": f"研判生成失败：{e}"}
    return d

def api_news_market(num: int = 30) -> dict:
    return newsfeed.market(num=num)


# ================= 主动盯盘 / 个股预判（2026-10-01，watchdesk）=================
# 定位：旧个股快讯是「用户输代码 → 现抓现算」的被动模式；这里改成系统自己扫盯盘池
# （博主重仓池 + 自选），扫完直接给结论：风险主导 / 机会窗口 / 多空交织 / 信息真空
# + 风险清单 + 机会清单 + 改判触发点。调度见 _watchdesk_maybe_run。
_DESK_STATE_KEY = "watchdesk_state"
_DESK_MIN_GAP = 900                 # 交易时段内最少 15 分钟扫一轮（王总 2026-10-01 拍板）
_DESK_LOCK = threading.Lock()
_DESK_RUNNING = {"on": False}


def _meta_put(key: str, value, *, _log: bool = True) -> None:
    """统一的 meta 写入封装（P2-01）：单条失败只记日志、绝不拖垮调用方。"""
    try:
        DB.set_meta(key, value)
    except Exception:  # noqa: BLE001
        if _log:
            logging.exception("meta 写入失败 key=%s", key)


def _desk_state() -> dict:
    try:
        s = json.loads(DB.get_meta(_DESK_STATE_KEY, "{}") or "{}")
    except Exception:  # noqa: BLE001
        s = {}
    return s if isinstance(s, dict) else {}


def _desk_state_update(**kw) -> None:
    cur = _desk_state()
    cur.update(kw)
    _meta_put(_DESK_STATE_KEY, json.dumps(cur, ensure_ascii=False))


def _desk_stale_seconds() -> float:
    """当前盯盘距上次扫描的秒数（没扫过返回 0，解析不了也返回 0 —— 交由调用方按自身语义兜底）。"""
    try:
        last = _desk_state().get("last_scan_at") or ""
        if not last:
            return 0.0
        return max(0.0, (datetime.now() - datetime.strptime(last, "%Y-%m-%d %H:%M:%S")).total_seconds())
    except Exception:  # noqa: BLE001
        return 0.0


def _desk_next_at(last: str | None) -> str | None:
    if not last:
        return None
    try:
        dt = datetime.strptime(last, "%Y-%m-%d %H:%M:%S")
        return (dt + timedelta(seconds=_DESK_MIN_GAP)).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:  # noqa: BLE001
        return None


def _desk_run_bg() -> None:
    """后台跑一轮盯盘扫描（实测 ~35s：203 只 × 0.15s）。带锁防重入，异常不外抛。"""
    with _DESK_LOCK:
        if _DESK_RUNNING["on"]:
            return
        _DESK_RUNNING["on"] = True
    st = _desk_state()
    _desk_state_update(running=True, started_at=time.strftime("%Y-%m-%d %H:%M:%S"))
    try:
        s = watchdesk.scan(DB.conn, window=watchdesk._WINDOW_DAYS)
        _desk_state_update(running=False, last_scan_at=time.strftime("%Y-%m-%d %H:%M:%S"),
                           last_stat=s)
    except Exception as e:  # noqa: BLE001
        _desk_state_update(running=False, last_err=str(e))
    finally:
        _DESK_RUNNING["on"] = False


def _watchdesk_maybe_run() -> None:
    """调度入口：距上次扫描满 15 分钟且不在扫描中就起一轮后台扫描；失败不影响调度循环。"""
    try:
        s = _desk_state()
        if s.get("running"):
            return
        last = s.get("last_scan_at")
        if last:
            try:
                if (datetime.now() - datetime.strptime(last, "%Y-%m-%d %H:%M:%S")).total_seconds() < _DESK_MIN_GAP:
                    return
            except Exception:  # noqa: BLE001
                pass
        threading.Thread(target=_desk_run_bg, name="watchdesk-scan", daemon=True).start()
    except Exception:  # noqa: BLE001
        pass


# 2026-10-05（报告 P2 / 建议6）盯盘心跳与自愈：
# 实测发现 meta.watchdesk_state 长期停在运行中（running=true），但 last_scan_at 停在 4 天前——
# 线程早死了，状态没人改。这里给一个统一的判活口径：距上次扫描超过 STALE 秒即判定为「假活」，
# 置 running=false 并在防抖窗口内自动补起一轮扫描（而不是让用户对着一个假状态牌干等）。
_WATCHDOG_STALE_SECONDS = 1800          # 30 分钟没扫过 = 线程已死
_WATCHDOG_HEAL_LOCK = threading.Lock()
_WATCHDOG_LAST_HEAL = {"at": 0.0}


def _watchdesk_heartbeat() -> None:
    """每轮调度调一次：查盯盘是否「假活」，是则置 running=false + 自动补扫。"""
    try:
        if _DESK_RUNNING["on"]:
            return                                   # 真在跑，不打扰
        s = _desk_state()
        last = s.get("last_scan_at") or ""
        if not last:
            return                                   # 从没扫过，交给 _watchdesk_maybe_run 正常触发
        age = (datetime.now() - datetime.strptime(last, "%Y-%m-%d %H:%M:%S")).total_seconds()
        if age < _WATCHDOG_STALE_SECONDS:
            return                                   # 心跳正常
        now = time.time()
        heal_gap_ok = (now - _WATCHDOG_LAST_HEAL["at"]) >= _DESK_MIN_GAP
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        if heal_gap_ok:
            _WATCHDOG_LAST_HEAL["at"] = now
            _desk_state_update(running=False, zombie_at=ts, zombie_scan_age=int(age))
            logging.warning("盯盘心跳异常：距上次扫描 %d 分钟，判定线程已死，running=false 并自动补扫",
                            int(age // 60))
            threading.Thread(target=_desk_run_bg, name="watchdesk-heal", daemon=True).start()
        else:
            # 刚自愈过一轮：只把状态标脏（前端据此显示「已停止（xx 未扫描）」），不重复补扫
            cur = _desk_state()
            if not cur.get("zombie_at"):
                _desk_state_update(running=False, zombie_at=ts, zombie_scan_age=int(age))
                logging.warning("盯盘心跳异常：距上次扫描 %d 分钟（自愈冷却中，本轮只标记不补扫）",
                                int(age // 60))
    except Exception:  # noqa: BLE001 —— 心跳本身绝不能拖垮调度
        logging.exception("盯盘心跳检查异常（已忽略）")


def api_watchdesk() -> dict:
    """盯盘总览。首屏尚未跑过扫描（state 空）时，用结论表里最后一次重算时间兜底，
    否则面板头会显示「上次扫描 —」，看起来像没干活；池子大小也直接取实际行数。"""
    d = watchdesk.list_desk(DB.conn, limit=200)
    s = _desk_state()
    rows_n = len(d.get("rows") or [])
    last = s.get("last_scan_at") or ""
    if not last:
        try:
            r = DB.conn.execute("SELECT MAX(scan_at) FROM stock_desk").fetchone()
            last = (r[0] if r and r[0] else "") or ""
        except Exception:  # noqa: BLE001
            last = ""
    st = d.get("stats")
    if isinstance(st, dict):
        st.update({
            "running": bool(s.get("running")),
            "last_scan_at": last,
            "next_scan_at": _desk_next_at(s.get("last_scan_at")) or _desk_next_at(last),
            "started_at": s.get("started_at") or "",
            "last_stat": s.get("last_stat") or {},
            "pool": (s.get("last_stat") or {}).get("scanned") or rows_n,
            # 2026-10-05（建议6）：把「假活」显式给前端，状态牌不用再猜
            "zombie": bool(s.get("zombie_at") or (not s.get("running") and _desk_stale_seconds() > _WATCHDOG_STALE_SECONDS)),
            "zombie_at": s.get("zombie_at") or "",
            "zombie_scan_age": int(_desk_stale_seconds()),
            "heartbeat_stale_seconds": _WATCHDOG_STALE_SECONDS,
        })
    return d


def api_watchdesk_detail(code: str) -> dict:
    return watchdesk.detail(DB.conn, code, window=watchdesk._WINDOW_DAYS)


def api_watchdesk_scan() -> dict:
    """前端「立即扫描」：不到冷却时间也放行（手动优先），后台线程跑，立刻返回。"""
    _desk_state_update(last_scan_at="")       # 清掉冷却起点，让本轮立刻触发
    threading.Thread(target=_desk_run_bg, name="watchdesk-scan-now", daemon=True).start()
    return {"ok": True, "accepted": True}


# ================= 政策风向（国家级政策desk，2026-09-30 替代旧「全市场快讯」）=================
def _safe_policy_refresh() -> None:
    """后台线程：刷新政策风向（带内部冷却/锁；失败不影响读旧数据）。

    刷新后顺带调用一次 payload() 预热「板块代表股」进程缓存（_sector_members 首次
    抓取约 2~3s），使首个用户请求落到热缓存、不再同步卡顿。
    """
    try:
        policydesk.maybe_refresh(DB.conn)
        policydesk.payload(DB.conn, num=10)   # 预热代表股缓存（结果丢弃，仅取副作用）
    except Exception:  # noqa: BLE001
        pass


def api_policy_desk(num: int = 80) -> dict:
    # 非阻塞：HTTP 线程只做库读，外部抓取（gov.cn + 中央级快讯筛选 + 板块代表股）甩给后台线程，
    # 首屏不再卡在冷抓取上；若库内尚无数据，payload 返回 warming=True，前端 3s 后补取。
    try:
        if time.time() - getattr(policydesk, "_LAST_REFRESH", 0.0) >= policydesk.REFRESH_TTL:
            threading.Thread(target=_safe_policy_refresh, name="policy-prewarm", daemon=True).start()
    except Exception:  # noqa: BLE001
        pass
    return policydesk.payload(DB.conn, num=num)



def _overseas_insight() -> dict:
    """海外舆情 → 决策报告：把已入库的「主题 / 热度 / 映射国内板块 / 驱动热点」
    重新组织成四段可解释内容，供「美股港股舆情」面板展示。

      ① 他们在说什么   topics —— 主题热度榜（占比）+ 每个主题下的真实标题（可点原链接）
      ② 注意力投向哪   sectors —— 国内板块按海外提及热度加权排序（横向条形）
      ③ 他们做了什么   moves  —— 热点事件的动向（方向 / 关联美股港股标的 / 驱动源）
      ④ 对内陆的影响与建议 impacts + bull / bear —— 主攻 / 回避板块 + 代表个股 + 传导路径 + 结论

    诚实口径（important）：库中**没有**海外机构的真实持仓或成交金额，
    因此这里所有「权重」都由 heat（关注度热度）加权得出，表示**注意力分布**，
    **不是资金金额**；③④ 的方向为聚合推断，不构成投资建议。
    """
    # ---------- ① 他们在说什么：主题热度榜 + 真实标题 ----------
    trows = DB.conn.execute(
        "SELECT topic, COUNT(*) n, SUM(heat) heat FROM overseas_news "
        "GROUP BY topic ORDER BY heat DESC").fetchall()
    tsum = sum(float(r["heat"] or 0) for r in trows) or 1.0
    topics = [{"topic": r["topic"] or "其他", "n": r["n"],
               "heat": round(float(r["heat"] or 0), 1),
               "share": round(float(r["heat"] or 0) / tsum * 100, 1)} for r in trows]
    heads: dict[str, list] = {}
    # 代表标题优先取「真实源」：示例源（example.local / 示例源·xx）排到最后，
    # 否则热度最高的那条常常是种子数据，展示出来对用户没有参考价值。
    for r in DB.conn.execute(
            "SELECT topic, title, url, source_name, region, publish_time, heat, sentiment, "
            "       CASE WHEN source_name LIKE '示例源%' OR url LIKE '%example.local%' "
            "            THEN 1 ELSE 0 END AS is_seed "
            "FROM overseas_news WHERE title IS NOT NULL AND title<>'' "
            "ORDER BY is_seed, heat DESC").fetchall():
        k = r["topic"] or "其他"
        lst = heads.setdefault(k, [])
        if len(lst) < 3:
            # 标题是来源站**原文**（库里 96% 为英文；翻译会失去可核对性，故保留原文），
            # 另附中文情感徽章（看多 / 看空 / 中性），让「这条新闻对国内偏多还是偏空」
            # 中文可读——这也是该行唯一需要读者判断的信息。
            lst.append({"title": r["title"], "url": r["url"], "source": r["source_name"],
                        "region": r["region"], "time": r["publish_time"],
                        "sentiment": r["sentiment"] or "中性",
                        "heat": round(float(r["heat"] or 0), 1)})
    for t in topics:
        t["headlines"] = heads.get(t["topic"], [])

    # ---------- ② 注意力投向哪些国内板块（拆 sectors 字段，heat 加权） ----------
    sec_heat: dict[str, float] = {}
    sec_n: dict[str, int] = {}
    sec_topics: dict[str, set] = {}
    for r in DB.conn.execute("SELECT sectors, topic, heat FROM overseas_news").fetchall():
        for name in [x.strip() for x in str(r["sectors"] or "").split(",") if x.strip()]:
            sec_heat[name] = sec_heat.get(name, 0.0) + float(r["heat"] or 0)
            sec_n[name] = sec_n.get(name, 0) + 1
            sec_topics.setdefault(name, set()).add(r["topic"] or "其他")
    sec_rank = sorted(sec_heat.items(), key=lambda kv: -kv[1])
    smax = sec_rank[0][1] if sec_rank else 1.0
    sectors = [{"sector": k, "heat": round(v, 1), "n": sec_n.get(k, 0),
                "w": round(v / (smax or 1) * 100, 1),
                "topics": sorted(sec_topics.get(k, set()))[:4]}
               for k, v in sec_rank[:16]]

    # ---------- ③④ 热点事件动向 + 对内陆的影响与建议 ----------
    def _is_none(s) -> bool:
        return "无显著" in str(s or "")

    moves: list[dict] = []
    impacts: list[dict] = []
    up_w: dict[str, float] = {}
    dn_w: dict[str, float] = {}
    for h in api_overseas_hotspots():
        heat = float(h.get("heat") or 0)
        moves.append({
            "title": h.get("title") or "",
            "title_en": h.get("title_en") or "",   # 海外媒体原文标题，前端悬停核对
            "topic": h.get("topic") or "",
            "direction": h.get("direction") or "",
            "regions": h.get("driver_regions") or [],
            "symbols": h.get("related_symbols") or [],
            "sources": (h.get("driver_sources") or [])[:5],
            "heat": round(heat, 1), "confidence": h.get("confidence"),
            "amp": h.get("amplification_factor"),
        })
        ups = [x for x in (h.get("sectors_up") or []) if x and not _is_none(x)]
        dns = [x for x in (h.get("sectors_down") or []) if x and not _is_none(x)]
        for x in ups:
            up_w[x] = up_w.get(x, 0.0) + heat
        for x in dns:
            dn_w[x] = dn_w.get(x, 0.0) + heat
        impacts.append({
            "title": h.get("title") or "", "topic": h.get("topic") or "",
            "direction": h.get("direction") or "",
            "sectors": [x for x in (h.get("domestic_sectors") or []) if not _is_none(x)],
            "decision": h.get("decision") or "",
            "transmission": h.get("transmission") or "",
            "up": [x for x in (h.get("sectors_up_detail") or [])
                   if x and not _is_none(x.get("sector"))],
            "down": [x for x in (h.get("sectors_down_detail") or [])
                     if x and not _is_none(x.get("sector"))],
            "confidence": h.get("confidence"), "heat": round(heat, 1),
        })
    bull = [k for k, _ in sorted(up_w.items(), key=lambda kv: -kv[1])][:8]
    bear = [k for k, _ in sorted(dn_w.items(), key=lambda kv: -kv[1])][:6]

    lead = topics[0] if topics else {"topic": "", "share": 0}
    basis = [
        "「权重」= 该主题 / 板块下所有海外条目的热度(heat)之和，表示**关注度**，不是成交金额——库中无海外真实持仓或资金流向数据。",
        "主题→国内板块的映射来自条目自带的 sectors 字段（一条可映射多个板块，故板块热度之和大于全库热度）。",
        "③④ 的事件、方向、主攻 / 回避与代表个股来自「国外驱动热点」表（自动聚合 + 传导分析）；代表个股用于定位观察方向。",
        "方向里的「国外舆情转弱 / 净空」指该主题在海外多空结构偏空，与「该主题对国内板块利多 / 利空」不是同一回事。",
        "事件卡标题用**中文事件名**（如「顶刊科研突破：技术路线与商业化预期重塑」）；原文是海外媒体报道标题，鼠标悬停可核对。",
        "「他们在说什么」里的新闻标题保留来源站**原文**（多为英文），以便逐条核对；行内另给中文多空标注。",
        "驱动源已剔除「示例源·xx」等结构化示例（种子数据），只列真实接入源。",
        "本报告为信息聚合与推断，不构成投资建议。",
    ]
    return {
        "topics": topics, "sectors": sectors, "moves": moves, "impacts": impacts,
        "bull": bull, "bear": bear, "basis": basis,
        "verdict": {
            "lead_topic": lead.get("topic", ""), "lead_share": lead.get("share", 0),
            "lead_sector": sectors[0]["sector"] if sectors else "",
            "topics_n": len(topics), "events_n": len(impacts),
        },
    }


def api_overseas_sources() -> dict:
    """数据源接入状态（界面如实展示「已接入 / 待激活」）与权重配置。"""
    rows = [dict(r) for r in DB.conn.execute(
        "SELECT * FROM overseas_sources_status ORDER BY region, name").fetchall()]
    cfg = _overseas_cfg()
    return {"sources": rows, "region_weights": cfg.get("region_weights") or {},
            "topics": list((cfg.get("topics") or {}).keys())}


def api_overseas_collect(live: bool = True, reset: bool = True) -> dict:
    """显式刷新海外舆情（可联网 live=True，或仅用结构化示例 live=False）。"""
    try:
        from overseas.collect import collect
        return collect(db=DB, include_seed=True, reset=reset, live=live)
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)[:200]}


def api_players(top_only: bool = False, channel: str | None = None,
                tier: str | None = None, visible_only: bool = False) -> list[dict]:
    """实盘选手列表。

    - top_only：仅入选核心池
    - channel：按来源渠道(A/B/C/D)过滤
    - tier：按层级(A~F)过滤；需求要求 E/F 低层级**一律露出**，所以默认不排除
      is_visible=0 的选手（当前写入时 is_visible 恒为 1）。
    - visible_only：是否只保留 is_visible=1（默认 False，展示全部层级）。
    """
    sql = "SELECT * FROM players"
    where, params = [], []
    if top_only:
        where.append("is_top_player=1")
    if channel:
        where.append("channel=?")
        params.append(channel)
    if tier:
        where.append("tier=?")
        params.append(tier)
    if visible_only:
        where.append("is_visible=1")
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY rank_position ASC"
    players = [dict(r) for r in DB.conn.execute(sql, params).fetchall()]

    # ---------- 当前持仓（2026-09-25 王总需求：实盘选手列表直接显示各自持有哪些票） ----------
    # 口径：positions 显式持仓优先，trades 操作流水兜底；按 (选手, 标的) 取最近一次动作。
    # 匹配键=选手昵称（与 player_detail 同口径）。抓取不到持仓的选手 holdings 为空，前端留白。
    # 2026-09-25 修正：仅 symbol 缺失不再整批丢弃——有股票名也可显示（标 name_only=待匹配代码）；
    #   来源 source_type 区分 bar_post(个股讨论页提及) / player_page(选手自己主页实盘记录)。
    sym_names: dict[str, str] = {}
    hold_map: dict[str, dict[tuple, dict]] = {}

    # 2026-09-25 修复：原实现对 sqlite3.Row 调用 r.get("source_type")，
    #   但 sqlite3.Row 没有 .get() 方法 → 直接抛 AttributeError，使 /api/players 整体 500。
    #   且 positions 表本就没有 source_type 列（只有 site），故改为「有该列则用，无则由 site 推导」。
    #   推导口径：淘股吧=选手主页实盘记录(player_page)；集思录/摩尔=组合晒单(community)；
    #            其余（雪球/东财/同花顺等讨论页）= bar_post(个股讨论提及)。
    _SITE_SRC = {"taoguba": "player_page", "tgb": "player_page",
                 "jisilu": "community", "moer": "community"}

    def _src_of(r) -> str:
        keys = r.keys() if hasattr(r, "keys") else []
        if "source_type" in keys:
            v = r["source_type"]
            if v:
                return str(v)
        site = (r["site"] if "site" in keys else "") or ""
        return _SITE_SRC.get(str(site).lower(), "bar_post")

    def _feed(rows) -> None:
        for r in rows:
            name = r["name"]
            sym = r["symbol"]
            sname = r["symbol_name"] or ""
            stype = _src_of(r)
            if not sym and not sname:
                continue
            if sym and sname:
                sym_names.setdefault(sym, sname)
            cur = hold_map.setdefault(name, {})
            key = (name, sym or sname)
            prev = cur.get(key)
            rec = {"symbol": sym or "", "symbol_name": sname,
                   "action": r["action"] or "", "post_time": r["post_time"],
                   "source_type": stype, "name_only": not bool(sym)}
            if prev is None or str(r["post_time"] or "") > str(prev["post_time"] or ""):
                cur[key] = rec

    _feed(DB.conn.execute(
        "SELECT player AS name, symbol, symbol_name, action, post_time, site FROM positions").fetchall())
    # 2026-09-25 修复：trades 表同样没有 source_type 列（PRAGMA 已核实），一并去掉，由 site 推导。
    _feed(DB.conn.execute(
        "SELECT author AS name, symbol, symbol_name, action, post_time, site FROM trades").fetchall())

    for p in players:
        items = list(hold_map.get(p["name"], {}).values())
        items.sort(key=lambda x: str(x["post_time"] or ""), reverse=True)
        for it in items:
            it["display"] = it["symbol_name"] or it["symbol"]
        p["holdings"] = items
    return players


def _lhb_recent_dates(anchor: str, days: int = 5, table: str = "lhb_depts") -> list[str]:
    """取截至 anchor（含）的最近 days 个有数据的交易日，用于滚动窗口聚合。"""
    return [r[0] for r in DB.conn.execute(
        f"SELECT DISTINCT trade_date FROM {table} WHERE trade_date <= ? "
        "ORDER BY trade_date DESC LIMIT ?", (anchor, days)).fetchall()]


def _lhb_depts_top(anchor: str, days: int = 5, limit: int = 20) -> list[dict]:
    """游资席位榜：以**营业部为轴**的滚动战绩（上榜次数/涉及股数/买卖净额/榜后胜率）。

    为什么必须单独做这一张榜（不能由 lhb_seats 推出）：
      lhb_seats 是「某股的前5买卖席位」，只能由股查席位；
      这张榜是「某营业部在扫哪些股、买完表现如何」，是游资追踪的主视角。
    D1/D3 为该营业部**当期买入个股在榜后的实际涨幅**（东财口径），不是预测值。
    """
    ds = _lhb_recent_dates(anchor, days, "lhb_depts")
    if not ds:
        return []
    ph = ",".join("?" for _ in ds)
    rows = DB.conn.execute(
        f"""SELECT operate_name, security_code, security_name, act_buy, act_sell,
                   net_amt, d1_chg, d3_chg
            FROM lhb_depts WHERE trade_date IN ({ph})""", ds).fetchall()
    agg: dict[str, dict] = {}
    for r in rows:
        nm = r["operate_name"] or "（未知席位）"
        a = agg.setdefault(nm, {
            "operate_name": nm, "appear": 0, "codes": set(),
            "buy": 0.0, "sell": 0.0, "net": 0.0,
            "n1": 0, "w1": 0, "s1": 0.0, "n3": 0, "w3": 0, "s3": 0.0,
            "stocks": {},
        })
        a["appear"] += 1
        a["codes"].add(r["security_code"])
        a["buy"] += r["act_buy"] or 0.0
        a["sell"] += r["act_sell"] or 0.0
        a["net"] += r["net_amt"] or 0.0
        for tag, v in (("1", r["d1_chg"]), ("3", r["d3_chg"])):
            if v is not None:
                a["n" + tag] += 1
                a["s" + tag] += v
                if v > 0:
                    a["w" + tag] += 1
        # 同一营业部同一只股若多日上榜，按代码合并（否则榜里会出现重复行）
        st = a["stocks"].setdefault(r["security_code"], {
            "code": r["security_code"], "name": r["security_name"] or "",
            "net": 0.0, "days": 0, "d3_chg": None,
        })
        st["net"] += r["net_amt"] or 0.0
        st["days"] += 1
        if not st["name"] and r["security_name"]:
            st["name"] = r["security_name"]
        if r["d3_chg"] is not None:
            st["d3_chg"] = r["d3_chg"]
    out = []
    for a in agg.values():
        out.append({
            "operate_name": a["operate_name"],
            "appear": a["appear"],
            "stock_count": len(a["codes"]),
            "buy": a["buy"], "sell": a["sell"], "net": a["net"],
            "win1": (a["w1"] * 100.0 / a["n1"]) if a["n1"] else None,
            "avg1": (a["s1"] / a["n1"]) if a["n1"] else None,
            "win3": (a["w3"] * 100.0 / a["n3"]) if a["n3"] else None,
            "avg3": (a["s3"] / a["n3"]) if a["n3"] else None,
            "top_stocks": sorted(a["stocks"].values(), key=lambda x: -(x["net"] or 0))[:6],
        })
    out.sort(key=lambda x: -(x["net"] or 0))
    return out[:limit]


def _lhb_inst_top(anchor: str, days: int = 5, limit: int = 20) -> list[dict]:
    """机构席位榜：滚动窗口内机构净买最多的个股（含量化口径的买/卖次数与占成交比）。"""
    ds = _lhb_recent_dates(anchor, days, "lhb_inst")
    if not ds:
        return []
    ph = ",".join("?" for _ in ds)
    rows = DB.conn.execute(
        f"""SELECT security_code, security_name, buy_times, sell_times,
                   buy_amt, sell_amt, net_buy_amt, ratio, d1_chg, d3_chg
            FROM lhb_inst WHERE trade_date IN ({ph})""", ds).fetchall()
    agg: dict[str, dict] = {}
    for r in rows:
        code = r["security_code"]
        a = agg.setdefault(code, {
            "security_code": code, "security_name": r["security_name"] or "",
            "buy_times": 0, "sell_times": 0, "buy_amt": 0.0, "sell_amt": 0.0,
            "net": 0.0, "ratios": [], "n3": 0, "s3": 0.0, "w3": 0,
        })
        a["buy_times"] += r["buy_times"] or 0
        a["sell_times"] += r["sell_times"] or 0
        a["buy_amt"] += r["buy_amt"] or 0.0
        a["sell_amt"] += r["sell_amt"] or 0.0
        a["net"] += r["net_buy_amt"] or 0.0
        if r["ratio"] is not None:
            a["ratios"].append(r["ratio"])
        if r["d3_chg"] is not None:
            a["n3"] += 1
            a["s3"] += r["d3_chg"]
            if r["d3_chg"] > 0:
                a["w3"] += 1
    out = []
    for a in agg.values():
        out.append({
            "security_code": a["security_code"], "security_name": a["security_name"],
            "buy_times": a["buy_times"], "sell_times": a["sell_times"],
            "buy_amt": a["buy_amt"], "sell_amt": a["sell_amt"], "net": a["net"],
            "ratio": (sum(a["ratios"]) / len(a["ratios"])) if a["ratios"] else None,
            "avg3": (a["s3"] / a["n3"]) if a["n3"] else None,
            "win3": (a["w3"] * 100.0 / a["n3"]) if a["n3"] else None,
        })
    out.sort(key=lambda x: -(x["net"] or 0))
    return out[:limit]


def api_lhb(date: str | None = None, window_days: int = 5) -> dict:
    """游资龙虎榜（4 维度）。

    数据来源：东方财富 datacenter-web 免鉴权接口（交易所盘后披露，T 日 16:30 后可取）。
    date 传 'auto'/空 = 返回库里最新交易日。window_days = 席位榜/机构榜的滚动窗口（交易日数）。

    返回：
      stocks    当日个股榜单（含 d1/d2/d5/d10/d20/d30 上榜后表现）
      seats     当日买卖前 5 席位明细
      depts_top 游资席位榜（以营业部为轴，滚动 window_days 日）
      inst_top  机构席位榜（以个股为轴，滚动 window_days 日）
    """
    dates = [r[0] for r in DB.conn.execute(
        "SELECT DISTINCT trade_date FROM lhb_stocks ORDER BY trade_date DESC LIMIT 15").fetchall()]
    if not dates:
        return {"dates": [], "date": "", "stocks": [], "seats": [], "empty": True}
    d = date if date in dates else dates[0]
    stocks = [dict(r) for r in DB.conn.execute(
        """SELECT trade_date, security_code, security_name, market, close_price,
                  change_rate, turnoverrate, buy_amt, sell_amt, net_amt,
                  deal_amt, deal_net_ratio, reason, explain,
                  d1_chg, d2_chg, d5_chg, d10_chg, deal_amount_ratio, net_bs_amt
           FROM lhb_stocks WHERE trade_date = ?
           ORDER BY net_amt DESC""", (d,)).fetchall()]
    seats = [dict(r) for r in DB.conn.execute(
        """SELECT trade_date, security_code, operate_code, operate_name, side,
                  buy, sell, net, rise_prob_3d
           FROM lhb_seats WHERE trade_date = ?
           ORDER BY security_code, side, net DESC""", (d,)).fetchall()]
    # 统计该日机构席位净买（供前端汇总条）
    inst = DB.conn.execute(
        """SELECT SUM(CASE WHEN side='BUY' THEN net ELSE 0 END) AS buy_net,
                  SUM(CASE WHEN side='SELL' THEN net ELSE 0 END) AS sell_net
           FROM lhb_seats WHERE trade_date = ? AND (operate_name LIKE '%机构专用%'
              OR operate_name LIKE '%基金%' OR operate_name LIKE '%社保%'
              OR operate_name LIKE '%QFII%' OR operate_name LIKE '%保险%')""", (d,)).fetchone()
    return {"dates": dates, "date": d, "stocks": stocks, "seats": seats,
            "depts_top": _lhb_depts_top(d, window_days),
            "inst_top": _lhb_inst_top(d, window_days),
            "window_days": window_days,
            "window_dates": _lhb_recent_dates(d, window_days, "lhb_depts"),
            "inst_buy_net": (inst["buy_net"] if inst else None) or 0,
            "inst_sell_net": (inst["sell_net"] if inst else None) or 0}


def api_lhb_sectors(date: str | None = None) -> dict:
    """龙虎榜「板块动向」——资金在哪些板块出现多点共振，及其扩散验证。

    与 lhb_stocks（个股榜）是两个独立口径：
      - 个股榜回答「哪只票被买」
      - 板块动向回答「资金是不是成片扫某个方向」
    板块聚集度（上榜股数多 + 净买额大）才有前瞻意义；单只票的上榜不构成方向。

    数据由 lhb_sector_link.py 离线计算后落库（lhb_sector_focus），本接口只读，不重算。
    """
    try:
        rows = [dict(r) for r in DB.conn.execute(
            """SELECT trade_date, sector, net_amt, onlist_n, onlist_json, members_n, sampled,
                      t1_avg, t3_avg, t1_win, bench_t1, bench_t3, updated_at
               FROM lhb_sector_focus ORDER BY trade_date DESC, net_amt DESC""").fetchall()]
    except Exception:  # noqa: BLE001 —— 表不存在时降级为空（首次尚未跑过分析脚本）
        return {"dates": [], "date": "", "rows": [], "summary": {}, "empty": True}
    if not rows:
        return {"dates": [], "date": "", "rows": [], "summary": {}, "empty": True}

    dates = sorted({r["trade_date"] for r in rows}, reverse=True)
    d = date if date in dates else dates[0]
    for r in rows:
        try:
            r["onlist"] = json.loads(r.pop("onlist_json") or "[]")
        except Exception:  # noqa: BLE001
            r["onlist"] = []
        if r.get("t1_avg") is not None and r.get("bench_t1") is not None:
            r["excess_t1"] = round(r["t1_avg"] - r["bench_t1"], 2)
        if r.get("t3_avg") is not None and r.get("bench_t3") is not None:
            r["excess_t3"] = round(r["t3_avg"] - r["bench_t3"], 2)
    cur = [r for r in rows if r["trade_date"] == d]
    summary = {}
    try:
        mv = DB.conn.execute("SELECT value FROM meta WHERE key='lhb_sector_summary'").fetchone()
        if mv and mv[0]:
            summary = json.loads(mv[0])
    except Exception:  # noqa: BLE001
        summary = {}
    out = {"dates": dates, "date": d, "rows": cur, "summary": summary,
           "basis": "板块内【未上榜】成分股在上榜日之后的 T+1/T+3 平均涨幅，对比同期上证",
           "updated_at": rows[0].get("updated_at") or ""}
    # 方向研判报告：三段（为什么涨 / 牵涉哪些板块 / 接下来看什么）供前端在表格下方分节渲染。
    out["outlook"] = _sector_outlook(d)
    return out


# ---------------------------------------------------------------- 方向研判报告
# 「板块动向」那张表只回答「资金扫了哪些方向」。用户要的是**分开写**的三段判断：
#   ① why   为什么涨 —— 资金结构成因（净买/上榜数 + 资金形态 + 买方席位结构 + 龙头）
#   ② spill 牵涉哪些板块 —— 概念交集分支，以及该分支当日有没有出现龙虎榜共振
#   ③ relay 接下来看什么 —— 梯队未涨停补涨候选 + 全市场「资金已聚集但未被本表重点列出」的方向
# 全部由本地库真实字段拼装；推断项用 kind="推断" 标注，不引入外部消息面假设、不编数据。
_OUTLOOK_STYLE_KW = (
    "风格", "昨日", "高送转", "预盈", "预亏", "破净", "融资融券", "转融通",
    "MSCI", "富时", "标普", "指数", "成分", "ST板块", "次新", "送转", "举牌",
    "增持", "回购", "解禁", "大宗交易", "龙虎榜", "百元股", "低价股", "微利股",
    "亏损股", "AH股", "科创板", "创业板", "主板", "中字头", "高市净率",
    "高市盈率", "低市盈率", "机构重仓", "基金重仓", "QFII", "社保重仓",
    "券商重仓", "B股", "上证50", "沪深300", "中证500", "茅指数", "宁组合",
)


def _outlook_is_style(name: str) -> bool:
    """统计/风格/指数口径的「伪板块」不该进接力方向；地域板块与题材概念要保留。"""
    n = name or ""
    return any(k in n for k in _OUTLOOK_STYLE_KW)


def _outlook_form(reasons) -> str:
    """把交易所披露的上榜原因归并成资金形态；一只票只归一类，按风险优先级取。

    坑：交易所话术「有价格涨跌幅限制的…」里含「跌幅」二字，直接子串匹配会把**上涨**上榜
    误判成下跌上榜（实测福建水泥 +9.97% 被标成下跌）。故先把固定话术「涨跌幅」并成「涨跌」再判。
    """
    s = " ".join([r for r in (reasons or []) if r]).replace("涨跌幅", "涨跌")
    if "跌幅" in s:
        return "下跌上榜"
    if "累计达到30%" in s or "累计达到20%" in s:
        return "连板加速"
    if "涨幅偏离值达到7%" in s or "涨幅达到15%" in s:
        return "首板打板"
    if "换手率" in s:
        return "高换手承接"
    if "振幅" in s:
        return "巨幅震荡"
    return "其他"


def _sector_outlook(date: str | None = None) -> dict:
    """把「板块动向」当日数据升级成一段可分节阅读的方向研判（对应用户「分开写」的要求）。

    诚实边界（随包返回 basis，前端照原样展示）：
      · 龙虎榜 d1/d5/d10 三期全为空 → 不做「会涨多少」的量化承诺；
      · 席位「历史 3 日上涨概率」是该席位的历史统计，不是对个股的预测；
      · 外溢靠概念交集（东财 F10 单板块上限 300 只，重合数=可见下限）；
      · 接力方向与补涨候选是**推断**（由「资金已聚集 + 梯队未涨停」外推），不是结论。
    只读本地库与磁盘缓存，不发网络请求。
    """
    if not date:
        try:
            date = DB.conn.execute("SELECT MAX(trade_date) AS d FROM lhb_sector_focus").fetchone()["d"]
        except Exception:  # noqa: BLE001
            date = None
    if not date:
        return {}
    try:
        focus = [dict(r) for r in DB.conn.execute(
            "SELECT sector, net_amt, onlist_n, onlist_json FROM lhb_sector_focus "
            "WHERE trade_date=? ORDER BY net_amt DESC", (date,)).fetchall()]
    except Exception:  # noqa: BLE001
        return {}
    if not focus:
        return {}

    chain = {}
    try:
        for r in DB.conn.execute(
                "SELECT sector, leader_code, leader_name, leader_net, leader_pct_now, "
                "tiers_json, branches_json FROM sector_chain WHERE trade_date=?",
                (date,)).fetchall():
            chain[r["sector"]] = dict(r)
    except Exception:  # noqa: BLE001
        chain = {}

    lhb: dict[str, list] = {}
    try:
        for r in DB.conn.execute(
                "SELECT security_code, security_name, change_rate, turnoverrate, net_amt, "
                "reason, explain FROM lhb_stocks WHERE trade_date=?", (date,)).fetchall():
            lhb.setdefault(r["security_code"], []).append(dict(r))
    except Exception:  # noqa: BLE001
        lhb = {}
    lhb_all = set(lhb.keys())
    rev = _sector_reverse()

    # ---- ③-b 全市场：当日龙虎榜票按概念聚合，找「资金已聚集但没被本表列为重点」的方向 ----
    focus_names = {f["sector"] for f in focus}
    hit_n: dict[str, int] = {}
    for name, codes in rev.items():
        if not name or name in focus_names or _outlook_is_style(name):
            continue
        n = len(lhb_all.intersection(codes))
        if n >= 4:
            hit_n[name] = n
    relay_dirs = [{"name": k, "n": v} for k, v in
                  sorted(hit_n.items(), key=lambda kv: (-kv[1], kv[0]))[:12]]

    secs: list[dict] = []
    leaders: dict[str, list] = {}
    hub_map: dict[str, list] = {}
    for f in focus:
        sector = f["sector"]
        net_yi = round((_num(f["net_amt"]) or 0.0) / 1e8, 2)
        try:
            onlist = json.loads(f["onlist_json"] or "[]")
        except Exception:  # noqa: BLE001
            onlist = []
        codes: list[str] = []
        seen: set = set()
        for o in onlist:
            c = (o or {}).get("code")
            if c and c not in seen:
                seen.add(c)
                codes.append(c)
        for c in codes:
            hub_map.setdefault(c, []).append(sector)

        # ① 资金形态 + 榜单评语
        forms: dict[str, int] = {}
        expl: list[str] = []
        fell: list[dict] = []
        for c in codes:
            rows = lhb.get(c) or []
            fm = _outlook_form([x.get("reason") for x in rows])
            forms[fm] = forms.get(fm, 0) + 1
            for x in rows:
                if x.get("explain") and x["explain"] not in expl:
                    expl.append(x["explain"])
            if fm == "下跌上榜":
                nm = (rows[0].get("security_name") if rows else "") or c
                cr = next((x.get("change_rate") for x in rows if x.get("change_rate") is not None), None)
                fell.append({"code": c, "name": nm, "pct": _num(cr)})

        # ① 买方席位结构（lhb_seats 每行=一个席位在一只票上，天然不重复计数）
        inst_n = conn_n = hot_n = 0
        inst_net = hot_net = 0.0
        probs: list[float] = []
        if codes:
            ph = ",".join("?" * len(codes))
            try:
                for r in DB.conn.execute(
                        f"SELECT operate_name, net, rise_prob_3d FROM lhb_seats "
                        f"WHERE trade_date=? AND side='BUY' AND security_code IN ({ph})",
                        (date, *codes)).fetchall():
                    on = r["operate_name"] or ""
                    netv = _num(r["net"]) or 0.0
                    if "机构专用" in on:
                        inst_n += 1
                        inst_net += netv
                    elif "股通" in on:
                        conn_n += 1
                    else:
                        hot_n += 1
                        hot_net += netv
                    pv = _num(r["rise_prob_3d"])
                    if pv is not None:
                        probs.append(pv)
            except Exception:  # noqa: BLE001
                pass
        prob_avg = round(sum(probs) / len(probs), 1) if probs else None

        ch = chain.get(sector) or {}
        leader = None
        if ch.get("leader_name"):
            leader = {"code": ch.get("leader_code"), "name": ch.get("leader_name"),
                      "pct": _num(ch.get("leader_pct_now")),
                      "net_yi": round((_num(ch.get("leader_net")) or 0.0) / 1e8, 2)}
            leaders.setdefault(ch["leader_name"], []).append(sector)

        # ② 外溢分支 + 当日共振（主线内上榜 / 全市场同概念上榜）
        spill = []
        for b in (_jl(ch.get("branches_json"), []) or [])[:6]:
            nm = b.get("name")
            if not nm:
                continue
            mem = {m.get("code") for m in (b.get("members") or []) if m.get("code")}
            wide = len(lhb_all.intersection(rev.get(nm) or []))
            spill.append({"name": nm, "overlap": b.get("overlap"),
                          "reso": len(mem.intersection(lhb_all)), "wide": wide,
                          "state": ("已扩散" if wide >= 5 else ("局部共振" if wide >= 1 else "尚未启动"))})

        # ③ 补涨候选：梯队里还没涨停的（龙头已封板，能接力的在这批）
        relay = []
        for t in (_jl(ch.get("tiers_json"), []) or []):
            pct = _num(t.get("pct"))
            if pct is not None and pct < 9.8:
                relay.append({"code": t.get("code"), "name": t.get("name"), "pct": round(pct, 2)})
        relay.sort(key=lambda x: -(x["pct"] or 0))

        # 成因一句话：全部由上面计数拼装，无外部假设
        if hot_n >= max(2, inst_n + conn_n):
            cap_kind = "游资营业部主导"
        elif inst_n >= 2 and inst_n >= hot_n:
            cap_kind = "机构席位主导"
        elif conn_n >= 1:
            cap_kind = "有北向 / 深沪股通参与"
        else:
            cap_kind = "席位分散"
        form_desc = "、".join(f"{k} {v} 只" for k, v in
                             sorted(forms.items(), key=lambda kv: -kv[1])) or "—"
        why = (f"{sector}：{len(codes)} 只上榜、净买 {net_yi} 亿。资金形态以 {form_desc} 为主；"
               f"买方结构为{cap_kind}（机构专用 {inst_n} 席 / 深沪股通 {conn_n} 席 / 营业部 {hot_n} 席）。")
        if leader:
            why += f"龙头 {leader['name']}（{leader['pct']}%）单只吸金 {leader['net_yi']} 亿，是这条线的资金锚。"

        secs.append({
            "sector": sector, "net_yi": net_yi, "onlist_n": f.get("onlist_n"),
            "codes_n": len(codes),
            "forms": [{"k": k, "n": v} for k, v in sorted(forms.items(), key=lambda kv: -kv[1])],
            "capital": {"inst_seats": inst_n, "connect_seats": conn_n, "hot_seats": hot_n,
                        "inst_net_yi": round(inst_net / 1e8, 2),
                        "hot_net_yi": round(hot_net / 1e8, 2),
                        "seat_prob_avg": prob_avg},
            "kind": cap_kind, "leader": leader, "why": why,
            "spill": spill, "relay": relay[:8], "relay_n": len(relay),
            "fell": fell, "notes": expl[:3],
        })

    hubs = [{"code": c, "name": (lhb.get(c) or [{}])[0].get("security_name") or "",
             "sectors": v} for c, v in hub_map.items() if len(v) >= 2]
    hubs.sort(key=lambda x: (-len(x["sectors"]), x["code"]))

    return {
        "date": date, "sectors": secs, "relay_dirs": relay_dirs, "hubs": hubs,
        "shared_leader": [{"name": k, "sectors": v} for k, v in leaders.items() if len(v) >= 2],
        "kind_note": "「事实」= 交易所龙虎榜与本地行情快照原样统计；「推断」= 由事实外推，不构成买卖建议。",
        "basis": [
            "为什么涨＝当日交易所龙虎榜披露口径（涨幅偏离 / 连续累计 / 换手 / 振幅）归并统计，不做消息面解读。",
            "买方席位结构取自龙虎榜席位明细：机构专用 / 深沪股通 / 营业部各计 1 席，同一席位买多只票分别计数。",
            "牵涉方向＝概念交集（东财 F10，单板块最多 300 只，重合数为可见下限）；「已扩散」指全市场同概念当日也有票上龙虎榜。",
            "接力方向与补涨候选为推断：由「资金已聚集」与「梯队尚未涨停」外推，不代表会涨；龙虎榜 d1/d5/d10 均为空，不做涨跌幅量化预测。",
        ],
    }


def _is_person(name: str) -> bool:
    """判断股东名是否像自然人（排除公司/基金/账户/计划等机构性名称）。"""
    n = (name or "").strip()
    if not (2 <= len(n) <= 5):
        return False
    for kw in ("公司", "基金", "账户", "计划", "委员会", "银行", "证券", "保险",
               "信托", "大学", "中心", "协会", "合伙", "结算", "投资", "集团",
               "控股", "有限", "事业", "研究院", "社保", "年金"):
        if kw in n:
            return False
    return True


def api_niusan(min_hold: int = 2, scope: str = "free") -> dict:
    """牛散追踪 + 股东协同（对标「看究竟·股东协同」/ Dataroma 共识持仓）。

    - investors：自然人大股东按「持股只数」降序（牛散榜），含每只股的仓位与变动
    - cohold：同一只股被 >=2 名自然人同时持有（多人同持 → 协同信号）
    - 只取每只股的最新披露期（scope='free' 十大流通股东 或 'total' 十大股东）
    """
    scope = scope if scope in ("free", "total") else "free"
    rows = DB.conn.execute(
        """SELECT h.symbol, h.symbol_name, h.end_date, h.holder_rank, h.holder_name,
                  h.hold_num, h.hold_ratio, h.hold_change, h.change_ratio, h.holder_kind
           FROM stock_holders h
           JOIN (SELECT symbol, MAX(end_date) mx FROM stock_holders
                 WHERE scope = ? GROUP BY symbol) t
             ON h.symbol = t.symbol AND h.end_date = t.mx
           WHERE h.scope = ?""",
        (scope, scope)).fetchall()

    # ---- 按股东聚合 ----
    people: dict[str, dict] = {}
    by_stock: dict[str, dict] = {}
    for r in rows:
        nm = (r["holder_name"] or "").strip()
        sym = r["symbol"]
        st = by_stock.setdefault(sym, {"symbol": sym, "symbol_name": r["symbol_name"] or "",
                                       "end_date": r["end_date"], "people": []})
        if not _is_person(nm):
            continue
        # 风险隔离（holders_tag.py 打标）：只有 star（跨多只股票）能进牛散口径；
        # family（父子/夫妻同持）、controlling（实控人）、single（单股散户）一律排除，
        # 避免"家族持股"被当成"牛散协同"而误导判断。
        if (r["holder_kind"] or "").strip() != "star":
            continue
        ch = str(r["hold_change"] or "").strip()
        if ch.replace(".", "").replace("-", "").isdigit():
            try:
                v = float(ch)
                ch = ("增持 " + _num2(abs(v) / 1e4) + "万股") if v > 0 else \
                     ("减持 " + _num2(abs(v) / 1e4) + "万股" if v else "不变")
            except ValueError:
                pass
        item = {"symbol": sym, "symbol_name": r["symbol_name"] or "",
                "end_date": r["end_date"], "rank": r["holder_rank"],
                "ratio": _num(r["hold_ratio"]), "hold_num": _num(r["hold_num"]),
                "change": ch, "change_ratio": _num(r["change_ratio"])}
        p = people.setdefault(nm, {"name": nm, "holdings": [], "stocks": set(),
                                   "total_ratio": 0.0, "new_cnt": 0, "cut_cnt": 0})
        p["holdings"].append(item)
        p["stocks"].add(sym)
        p["total_ratio"] += (item["ratio"] or 0)
        if ch.startswith("新进"):
            p["new_cnt"] += 1
        elif ch.startswith("减持"):
            p["cut_cnt"] += 1
        st["people"].append({"name": nm, "ratio": item["ratio"], "rank": r["holder_rank"],
                             "change": ch})

    investors = []
    for nm, p in people.items():
        if len(p["stocks"]) < min_hold:
            continue
        hs = sorted(p["holdings"], key=lambda x: -(x["ratio"] or 0))
        investors.append({
            "name": nm, "stock_count": len(p["stocks"]),
            "total_ratio": round(p["total_ratio"], 3),
            "new_cnt": p["new_cnt"], "cut_cnt": p["cut_cnt"],
            "latest_date": max((h["end_date"] or "" for h in hs), default=""),
            "holdings": hs,
        })
    investors.sort(key=lambda x: (-x["stock_count"], -x["total_ratio"]))

    # 协同 = 多名「牛散」（跨 >=min_hold 只股票者）同时持有一只股；
    # 单纯"自然人扎堆"多为一家人/创始团队（如朱大庆=朱兴荣同族持股），必须排除。
    star_names = {x["name"] for x in investors}
    cohold = []
    for sym, st in by_stock.items():
        ps = [x for x in st["people"] if x["name"] in star_names]
        if len(ps) < 2:
            continue
        ps = sorted(ps, key=lambda x: -(x["ratio"] or 0))
        cohold.append({"symbol": sym, "symbol_name": st["symbol_name"],
                       "end_date": st["end_date"], "people_count": len(ps), "people": ps,
                       "star_ratio_sum": round(sum((x["ratio"] or 0) for x in ps), 3)})
    cohold.sort(key=lambda x: (-x["people_count"], -x["star_ratio_sum"]))

    # 名称补全（缺名/仅有名的用本地名称库补）
    for x in investors:
        for h in x["holdings"]:
            n = _named(h["symbol"], h["symbol_name"])
            h["symbol_name"] = n["name"]
    for x in cohold:
        n = _named(x["symbol"], x["symbol_name"])
        x["symbol_name"] = n["name"]

    latest_all = max((r["end_date"] or "" for r in rows), default="")
    return {
        "investors": investors,
        "cohold": cohold,
        "summary": {
            "person_total": len(people), "investor_total": len(investors),
            "cohold_total": len(cohold), "stock_total": len(by_stock),
            "latest_date": latest_all, "scope": scope,
            "scope_label": "十大流通股东" if scope == "free" else "十大股东",
            "min_hold": min_hold,
        },
        "basis": ("自然人股东数据来自上市公司定期报告/临时公告披露（东财 F10）。"
                  "「持股只数」= 该自然人在最新披露期中进入十大流通股东榜的股票数；"
                  "多为长线牛散，披露有滞后（季报约 1 个月、临时公告约 1-2 日），"
                  "仅作方向参考，不构成交易信号。"),
    }


def api_star_moves(limit: int = 80, periods: int = 4, scope: str = "free") -> dict:
    """牛散动向：按报告期列出「star 级牛散」的持仓变动（新进 / 增持 / 减持 / 退出）。

    只取 holder_kind='star'（跨 ≥2 家上市公司的二级市场自然人），
    已隔离实控人 / 家族同持噪声，不会与「大股东持股」混淆。
    """
    scope = scope if scope in ("free", "total") else "free"
    rows = DB.conn.execute(
        """SELECT symbol, symbol_name, end_date, holder_name, holder_rank,
                  hold_ratio, hold_change, change_ratio
           FROM stock_holders
           WHERE scope = ? AND holder_kind = 'star'
           ORDER BY end_date DESC""", (scope,)).fetchall()
    if not rows:
        return {"empty": True, "periods": [], "rows": [], "summary": {}}

    all_dates = sorted({str(r["end_date"])[:10] for r in rows}, reverse=True)[:periods + 1]
    if not all_dates:
        return {"empty": True, "periods": [], "rows": [], "summary": {}}

    by_period: dict[str, dict[str, list[dict]]] = {}
    for r in rows:
        d = str(r["end_date"])[:10]
        nm = (r["holder_name"] or "").strip()
        by_period.setdefault(d, {}).setdefault(nm, []).append({
            "symbol": r["symbol"], "symbol_name": r["symbol_name"],
            "rank": r["holder_rank"], "ratio": _num(r["hold_ratio"]),
            "change": (r["hold_change"] or "").strip(),
        })

    def _kind(ch: str) -> str:
        c = (ch or "").strip()
        if c in ("新进", "新增", "新进股东"):
            return "new"
        if c[0] in ("-", "－", "−") if c else False:
            return "cut"
        if c[0] in ("+", "＋") if c else False:
            return "add"
        return "hold"

    out_rows = []
    for i, d in enumerate(all_dates[:-1]):
        prev = by_period.get(all_dates[i + 1], {})
        cur = by_period.get(d, {})
        new, add, cut, exit_ = [], [], [], []
        for nm, items in cur.items():
            for it in items:
                k = _kind(it["change"])
                rec = {"name": nm, "symbol": it["symbol"], "symbol_name": it["symbol_name"],
                       "ratio": it["ratio"], "rank": it["rank"], "change": it["change"]}
                if k == "new":
                    new.append(rec)
                elif k == "add":
                    add.append(rec)
                elif k == "cut":
                    cut.append(rec)
        for nm, items in prev.items():
            cur_syms = {x["symbol"] for x in cur.get(nm, [])}
            for it in items:
                if it["symbol"] not in cur_syms:
                    exit_.append({"name": nm, "symbol": it["symbol"],
                                  "symbol_name": it["symbol_name"], "ratio": it["ratio"],
                                  "rank": it["rank"], "change": "退出"})
        sk = lambda x: -(x["ratio"] or 0)
        out_rows.append({
            "date": d,
            "new": sorted(new, key=sk)[:limit],
            "add": sorted(add, key=sk)[:limit],
            "cut": sorted(cut, key=sk)[:limit],
            "exit": sorted(exit_, key=sk)[:limit],
            "counts": {"new": len(new), "add": len(add), "cut": len(cut), "exit": len(exit_)},
        })

    star_names = {nm for b in by_period.values() for nm in b}
    return {
        "empty": False, "periods": all_dates, "rows": out_rows,
        "summary": {"star_count": len(star_names), "period_count": len(out_rows),
                    "latest_date": all_dates[0], "scope": scope},
        "basis": "仅含跨 ≥2 家上市公司的自然人股东（已隔离实控人 / 家族同持）",
    }


def api_positions() -> list[dict]:
    rows = DB.conn.execute(
        "SELECT site, player, channel, symbol, symbol_name, ratio, action, post_url "
        "FROM positions WHERE symbol IS NOT NULL ORDER BY symbol, player"
    ).fetchall()
    return [dict(r) for r in rows]


def api_consensus(min_holders: int = 2, symbol: str | None = None,
                  only_qualified: bool = False,
                  max_age_days: int | None = None) -> list[dict]:
    """多人共同持仓共识：各股被多少人（跨渠道/平台）共同持有，按人数降序，
    融合讨论热度作为辅助参考。symbol 指定时只返回单只，避免每次点击重算全量。

    2026-10-10 口径改革：返回结构把「喊买入(BUY)」「喊持有(HOLD)」「持仓快照(WATCH)」
    分开计数 —— buy_holders / hold_holders / watch_holders，holders 为三者去重总数。
    only_qualified=True 时只统计 Tier A 入围选手（players.tier='A'）。

    2026-10-15 P0-1：透传 max_age_days（positions 时效闸，默认 90 天）；每行已携带
    data_asof / positions_max_age_days，前端取首行即可渲染「数据截至 YYYY-MM-DD」角标。"""
    out = DB.consensus_stocks(min_holders=min_holders, only_qualified=only_qualified,
                              max_age_days=max_age_days)
    for x in out:
        n = _named(x["symbol"], x.get("symbol_name") or "")
        x["symbol_name"] = n["name"]
        x["company"] = n["company"]
        x["market"] = n["market"]
        x["name_missing"] = n["name_missing"]
    if symbol:
        out = [x for x in out if x["symbol"] == symbol]
    return out


def api_cohold_recent(days: int = 5, min_holders: int = 2,
                      anchor: str | None = None, only_qualified: bool = False) -> dict:
    """近 N 日「多人同向操作」：共买 / 共卖 / 共持 三组，附时间、价格、数量、浮盈与持有状态。

    与 /api/consensus 的区别：那边看的是**当前持仓快照**（现在还有多少人拿着），
    这边看的是**最近 N 天谁同时动手了**（动作发生时点）。两者互补，不可互替。

    2026-10-10：新增独立 hold 组，把 HOLD 与 BUY 彻底分开（不再出现「持有混进买入」）。
    only_qualified=True 时只统计 Tier A 入围选手。

    名称补全在服务端统一做（与其它接口一致），避免前端各自回退出不一致的显示名。
    """
    d = DB.cohold_recent(days=days, min_holders=min_holders, anchor=anchor,
                         only_qualified=only_qualified)
    for grp in ("buy", "sell", "hold"):
        for x in d.get(grp) or []:
            n = _named(x["symbol"], x.get("symbol_name") or "")
            x["symbol_name"] = n["name"]
            x["company"] = n["company"]
            x["market"] = n["market"]
            x["name_missing"] = n["name_missing"]
            if n["name"]:
                x["label"] = f"{n['name']}（{x['symbol']}）"
            for p in x.get("players") or []:
                p["symbol_name"] = n["name"] or x["symbol_name"] or ""
    return d


def api_stock(symbol: str) -> dict:
    """单只股票明细：讨论帖 + 各选手持仓（含买入时间/买入价/当前股价/权限）+ 相关实盘交易。
    轻量接口（不含原始页面 HTML），供「当前讨论股票」卡片点击查看。"""
    if not symbol:
        return {"error": "symbol required"}
    mentions = [dict(r) for r in DB.conn.execute(
        "SELECT sm.context, p.site, p.author, p.post_time, p.sentiment, p.text "
        "FROM stock_mentions sm JOIN posts p ON p.id = sm.post_id_ref "
        "WHERE sm.symbol=? ORDER BY p.post_time DESC LIMIT 50", (symbol,)).fetchall()]
    trades = [dict(r) for r in DB.conn.execute(
        "SELECT id, site, author, post_time, action, symbol, symbol_name, price, quantity, "
        "buy_time, buy_price, current_price, sell_time, sell_price, hold_since, "
        "access_level, access_hint FROM trades "
        "WHERE symbol=? ORDER BY COALESCE(buy_time, post_time) DESC LIMIT 100", (symbol,)).fetchall()]
    positions = [dict(r) for r in DB.conn.execute(
        "SELECT player, channel, site, symbol, symbol_name, ratio, action, post_time, post_url, "
        "buy_time, buy_price, current_price, sell_time, sell_price, hold_since, "
        "access_level, access_hint FROM positions "
        "WHERE symbol=? ORDER BY ratio DESC", (symbol,)).fetchall()]

    # 持仓起始日期：该股最早的建仓时间（显式披露优先），并把「持有天数」一并算出，
    # 便于在「持仓选手」表中直接看到"各人从何时开始持有该股"。
    since = ""
    for it in positions + trades:
        v = (it.get("hold_since") or "").strip()
        if v and (not since or v < since):
            since = v
    if not since:
        for it in positions + trades:
            if (it.get("action") or "").upper() not in ("BUY", "HOLD"):
                continue
            v = (it.get("buy_time") or it.get("post_time") or "").strip()
            if v and (not since or v < since):
                since = v
    # 2026-09-22 修复 P2-3：原实现把「该条记录自身的 buy_time / sell_time」当作结束点，
    # 于是同一只股票各条记录算出 0/2/52/59… 等互不相同的天数，与旁边同一行的
    # 「持仓起始日期」(since) 自相矛盾（实测 600519 共 12 条、10 条显示"已持有 0 天"）。
    # 改为统一以「该标的持仓起始日 → 今日」计算，同一 symbol 的所有记录天数一致。
    hold_days = _hold_days(since)
    for it in positions + trades:
        it["hold_since"] = since
        it["hold_days"] = hold_days
        if (it.get("action") or "").upper() == "SELL":
            if not (it.get("sell_time") or "").strip():
                it["sell_time"] = it.get("post_time") or it.get("buy_time") or ""
            if it.get("sell_price") is None:
                it["sell_price"] = it.get("buy_price") if it.get("buy_price") is not None else it.get("price")
    name = next((r["symbol_name"] for r in trades if r.get("symbol_name")), "")
    if not name:
        row = DB.conn.execute(
            "SELECT symbol_name FROM positions WHERE symbol=? AND symbol_name IS NOT NULL LIMIT 1",
            (symbol,)).fetchone()
        name = (row["symbol_name"] if row else "") or ""
    named = _named(symbol, name)
    holders = {p["player"] for p in positions if p.get("player")}
    # 「多空观点明细」：把对该股的讨论帖按立场分组，逐条保留「谁 + 原话理由」
    views = {"bull": [], "bear": [], "neutral": []}
    for m in mentions:
        cat = {"看多": "bull", "看空": "bear"}.get(m.get("sentiment"), "neutral")
        views[cat].append({
            "author": m.get("author") or "匿名",
            "post_time": m.get("post_time") or "",
            "site": m.get("site") or "",
            "text": m.get("text") or m.get("context") or "",
        })
    view_summary = {k: len(v) for k, v in views.items()}
    return {"symbol": symbol, "symbol_name": named["name"], "company": named["company"],
            "market": named["market"], "name_missing": named["name_missing"],
            # 2026-09-22 修复（A 点）：显式声明本视图 hold_days 的基准口径——
            # symbol_first_build = 建仓起点取自「该标的全市场」最早建仓记录，
            # 所有记录共用一个 hold_days（与 /api/player 的 player_first_build 不同）。
            "hold_days_basis": "symbol_first_build",
            "hold_days": hold_days,
            "mentions": mentions, "views": views, "view_summary": view_summary,
            "trades": trades,
            "positions": positions, "holders": len(holders), "mention_count": len(mentions),
            "trade_count": len(trades), "position_count": len(positions)}


# ---------------------------------------------------------------------------
# 单只股票行情（K 线）代理：2026-09-25 王总需求
#   点任意一只推荐的股票 → 看它当前状态 / 历史走势 / K 线图。
#   数据源：东方财富 push2his（公开、无需登录、无需 cookie），服务端代理以避开
#   浏览器 CORS 限制，并做 90s 内存缓存减少重复外网请求。
#   字段顺序遵循东方财富 kline 接口约定（fields2=f51..f61）：
#     f51 日期 / f52 开 / f53 收 / f54 高 / f55 低 / f56 成交量(手) /
#     f57 成交额(元) / f58 振幅% / f59 涨跌幅% / f60 涨跌额 / f61 换手率%
# ---------------------------------------------------------------------------
_KLINE_CACHE: dict[tuple, tuple[float, dict]] = {}
# 连续阴跌扫描的内存缓存（2026-09…… 2026-10-05 建议8：大接口降负载，同参数 1 小时内复用）
_DECLINE_CACHE: dict[tuple, tuple[float, dict]] = {}
_DECLINE_TTL = 3600


def _eastmoney_kline(symbol: str, period: str, timeout: int = 5) -> dict | None:
    klt = {"day": "101", "week": "102", "month": "103"}.get(period, "101")
    sym = (symbol or "").strip().lower()
    code = sym
    if sym.startswith("sh"):
        market, code = "1", sym[2:]
    elif sym.startswith("sz"):
        market, code = "0", sym[2:]
    elif sym.startswith("bj"):
        market, code = "0", sym[2:]
    else:
        # 纯 6 位代码：6 开头=沪(1)，其余(0/2/3/4/8)=深/北(0)；拿不到再反查另一市场
        market = "1" if code[:1] == "6" else "0"
    secid = f"{market}.{code}"
    headers = {"User-Agent": "Mozilla/5.0", "Referer": "https://quote.eastmoney.com/"}
    url = ("https://push2his.eastmoney.com/api/qt/stock/kline/get"
           "?secid={secid}&fields1=f1,f2,f3,f4,f5,f6"
           "&fields2=f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61"
           "&klt={klt}&fqt=1&beg=0&end=20500101"
           "&ut=fa5fd1943c7b386f172d6893fbfba10b&invt=2").format(secid=secid, klt=klt)

    def _fetch(sid: str) -> dict | None:
        u = url.replace(f"&secid={secid}", f"&secid={sid}")
        # 2026-09-25：直连取数；超时立即失败（不放大等待），失败由腾讯 K 线兜底。
        return _em_get_json(u, "https://quote.eastmoney.com/", timeout=timeout, retries=0)

    data = _fetch(secid)
    # 反查另一市场：仅在「网络正常但该市场查无此码」时才做（北交所等边界情况）；
    # 网络失败（data is None）时不反查，避免成倍等待——直接交给腾讯兜底。
    if data is not None and not data.get("data"):
        data = _fetch(f"{'0' if market == '1' else '1'}.{code}")
    if not (data and data.get("data")):
        return None
    d = data["data"]
    rows: list[dict] = []
    for s in (d.get("klines") or []):
        parts = s.split(",")
        if len(parts) < 11:
            continue
        try:
            rows.append({
                "date": parts[0],
                "open": float(parts[1]), "close": float(parts[2]),
                "high": float(parts[3]), "low": float(parts[4]),
                "volume": float(parts[5]), "amount": float(parts[6]),
                "pct": float(parts[8]) if len(parts) > 8 else 0.0,
            })
        except (ValueError, IndexError):
            continue
    if not rows:
        return None
    last = rows[-1]
    prev = rows[-2]["close"] if len(rows) >= 2 else last["open"]
    return {
        "symbol": symbol, "code": d.get("code") or code, "name": d.get("name") or "",
        "period": period, "market": market, "count": len(rows), "klines": rows,
        "adjust": "qfq", "source": f"东方财富 push2his（前复权 · {len(rows)} 根）",
        "current": {
            "price": last["close"], "pct": last["pct"], "open": last["open"],
            "high": last["high"], "low": last["low"], "prev_close": prev,
            "volume": last["volume"], "amount": last["amount"], "date": last["date"],
        },
    }


def _tencent_kline_result(symbol: str, period: str, timeout: int = 12, retries: int = 2,
                          full_history: bool = False):
    """腾讯 K 线 → 与 _eastmoney_kline 同构的 payload（东财 push2his 不可用时兜底）。

    full_history=False（竞速快路径）只拿最近一页 → 必须带 full_history=False 标记，
    绝不允许前端把「近 120 根」当成上市至今（2026-10-13 进哥反馈的误判根源）。"""
    market, code, _ = _em_secid(symbol)
    rows = _tencent_kline(code, market, period, timeout=timeout, retries=retries,
                          full_history=full_history)
    if not rows:
        return None
    last = rows[-1]
    prev = rows[-2]["close"] if len(rows) >= 2 else last["open"]
    span = ("全历史 %d 根" if full_history else "近 %d 根") % len(rows)
    return {
        "symbol": symbol, "code": code, "name": "", "period": period,
        "market": market, "count": len(rows), "klines": rows, "adjust": "qfq",
        "full_history": bool(full_history),
        "amount_est": True,   # 腾讯 K 线不含成交额，按「收盘价 × 成交量」估算，前端以 ≈ 标注
        "first_date": rows[0]["date"], "last_date": last["date"],
        "source": "腾讯行情（东财不可用，已自动兜底 · %s）" % span,
        "current": {"price": last["close"], "pct": last["pct"], "open": last["open"],
                    "high": last["high"], "low": last["low"], "prev_close": prev,
                    "volume": last["volume"], "amount": last["amount"], "date": last["date"]},
    }


def _slice_kline(payload: dict, limit: int, offset: int) -> dict:
    """K 线降采样（2026-10-05 建议8）：一次 NET  thousands 根全历史没必要整包推给前端。

    limit>0 = 只取最近 limit 根（默认 250）；limit=0 = 全历史（前端点「拉全部历史」时才走）。
    earliest_date 永远保留**全历史首日**（= 上市日），前端补「上市日期」用这个，
    first_date 才是本次切片的首日 —— 两者混用会把上市日写成 250 根之前的那天。
    """
    rows = list(payload.get("klines") or [])
    out = dict(payload)
    out["earliest_date"] = rows[0].get("date", "") if rows else ""   # 全历史首日 = 上市日
    out["total"] = len(rows)
    limit = max(0, int(limit or 0))
    if limit <= 0:
        out["has_more"] = False
        out["count"] = len(rows)
        out["offset"] = 0
        out["first_date"] = rows[0].get("date", "") if rows else ""
        out["last_date"] = rows[-1].get("date", "") if rows else ""
        return out
    # offset 是「从最新根往回数的第几根」：offset=0 → 最近 limit 根，offset 递增 → 更早的一段。
    # （早先写成 rows[off:off+limit]，取到的是**最早**那 250 根，K 线图会画出上市初期的走势，已修正）
    n = len(rows)
    off = max(0, min(int(offset or 0), max(0, n - 1)))
    end = n - off
    start = max(0, end - limit)
    sub = rows[start:end]
    out["klines"] = sub
    out["count"] = len(sub)
    out["offset"] = off
    out["has_more"] = start > 0          # 前面还有更早的数据 → 前端给「拉全部历史」
    if sub:
        out["first_date"] = sub[0].get("date", "")
        out["last_date"] = sub[-1].get("date", "")
    else:                                 # 空切片也要把键补齐，别让前端读到 undefined
        out["first_date"] = ""
        out["last_date"] = ""
    return out


def api_kline(symbol: str, period: str = "day", limit: int = 250, offset: int = 0) -> dict:
    """单只股票 K 线：东财（前复权·全历史）→ 新浪（不复权·全历史）→ 腾讯（前复权·兜底）。
    返回 OHLCV + 最新价/涨跌幅 + 首末日期（供前端判断能否拖到上市首日）。

    **默认只回最近 250 根**（limit 默认 250）：几千根全历史推过去既慢又没人看；
    前端要「拖到上市首日 / 点全历史 / 缩到底」时传 limit=0 拉全量，缓存按 (symbol, period) 复用。

    2026-10-13（进哥）：limit=0 要的是「上市至今」**真·全历史**。若竞速只拿到腾讯单页兜底
    （~120 根，full_history=False），必须改用「新浪全量 → 腾讯分段」认真拉全历史重试，
    绝不允许把半年走势当成上市行情（此前误判、直接影响判断的根源）。东财/新浪本就返回全历史，
    直接打 full_history=True 标记。
    """
    if not symbol:
        return {"error": "symbol required"}
    period = period if period in ("day", "week", "month") else "day"
    key = (symbol, period)
    now = time.time()
    cached = _KLINE_CACHE.get(key)
    if cached and now - cached[0] < 90:
        cp = cached[1]
        # 要全历史但缓存里只有单页兜底 → 不命中缓存，走下面的「认真拉全历史」重试
        if limit == 0 and not cp.get("full_history"):
            pass
        else:
            return _slice_kline(cp, limit, offset)
    # 2026-10-09：K 线三源字段**不等价**——东财独有的「真实成交额 amount（新浪/腾讯只有估算值，
    # amount_est=True）」与「name」，且东财/腾讯为前复权、新浪为不复权。故用「东财优先 + 限时」：
    # 东财 2s 内回就用东财（字段最全）；否则立刻用先回的新浪/腾讯，绝不干等（上限 4.5s）。
    payload = emapi.race_priority(
        lambda: _eastmoney_kline(symbol, period, timeout=4),
        [lambda: _sina_kline_result(symbol, period, timeout=4, retries=0),
         lambda: _tencent_kline_result(symbol, period, timeout=4, retries=0)],
        grace=2.0, timeout=4.5)
    if payload is None:
        return {"error": "行情获取失败：代码可能无效，或本地网络无法访问行情源"
                         "（东方财富 / 新浪 / 腾讯均不可用）。",
                "symbol": symbol}
    # 东财 push2his（beg=0 全量）/ 新浪（全历史）本就是真·全历史 → 打标记，前端据此才准标「上市至今」
    src = payload.get("source", "")
    is_full = src.startswith("东方财富") or src.startswith("新浪")
    if is_full:
        payload["full_history"] = True
    # limit=0（要上市至今）但竞速只拿到腾讯单页兜底 → 认真拉全历史重试：新浪全量优先，
    # 腾讯分段（full_history=True）兜底。二者任一中即可拿到真·全历史。
    if limit == 0 and not is_full:
        full = emapi.race_priority(
            lambda: _sina_kline_result(symbol, period, timeout=10, retries=2),
            [lambda: _tencent_kline_result(symbol, period, timeout=10, retries=2,
                                           full_history=True)],
            grace=3.0, timeout=12.0)
        if full is not None:
            payload = full
            payload["full_history"] = True
    payload.setdefault("adjust", "qfq")
    if payload.get("klines"):
        payload.setdefault("first_date", payload["klines"][0].get("date", ""))
        payload.setdefault("last_date", payload["klines"][-1].get("date", ""))
    _KLINE_CACHE[key] = (now, payload)
    return _slice_kline(payload, limit, offset)


# ---------------------------------------------------------------------------
# 个股「重要信息」快照：2026-09-25 王总需求
#   点开一只股票 → 除 K 线外，还要直接看到：实时行情 / 估值(PE·PB·市值) / 财务(ROE·毛利率·营收·净利)。
#   数据源（均公开、免登录、免 cookie）：
#     · 东方财富 push2 快照                              —— 行情 + 估值
#     · 东方财富 F10 主财务指标 RPT_F10_FINANCE_MAINFINADATA —— 财务（字段名自解释 + 自带报告期）
#   字段语义已交叉校验（以 600519 贵州茅台实测，两源同值，故不存在“猜 f 编号”的隐患）：
#     F10.BPS 200.9898 = push2 f92 每股净资产 ｜ F10.ROEJQ 16.75 = f173 ROE
#     F10.TOTALOPERATEREVE = f183 营业总收入   ｜ F10.PARENTNETPROFIT = f105 归母净利润
#     F10.XSMLL 89.56% = f186 销售毛利率       ｜ F10.XSJLL 50.75% = f187 销售净利率
#   原则：财务数字一律取 F10 具名字段；push2 只取行情与估值，两处都不拼凑、不推算。
# ---------------------------------------------------------------------------
_QUOTE_CACHE: dict[str, tuple[float, dict]] = {}
_EM_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
_EM_UT = "fa5fd1943c7b386f172d6893dbfba10b"
# 直连优先：显式忽略环境代理。若本机存在失效的 http(s)_proxy（实测沙箱里就有），
# 走环境代理会直接 ProxyError；本机无代理需求时直连最稳，失败后自动回退默认 opener。
_EM_DIRECT = urllib.request.build_opener(urllib.request.ProxyHandler({}))

_QUOTE_FIELDS = ("f43,f44,f45,f46,f47,f48,f50,f57,f58,f60,f86,f92,f105,f116,f117,"
                 "f162,f163,f164,f167,f168,f169,f170,f171,f173,f183,f189")
_F10_COLS = ("SECUCODE,SECURITY_CODE,SECURITY_NAME_ABBR,REPORT_DATE,REPORT_DATE_NAME,"
             "EPSJB,EPSKCJB,BPS,TOTALOPERATEREVE,TOTALOPERATEREVETZ,"
             "PARENTNETPROFIT,PARENTNETPROFITTZ,KCFJCXSYJLR,ROEJQ,ROEKCJQ,"
             "XSMLL,XSJLL,ZZCJLL,MGJYXJJE,MGWFPLR,ZCFZL")


def _num(v):
    """东财常用占位符（'-'、'--'、''）统一折算为 None，避免前端拿到脏值。"""
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip().replace(",", "")
    if s in ("", "-", "--", "—", "None", "null"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _num2(v):
    """两位小数格式化（用于「万股」等展示，缺值返回 —）。"""
    n = _num(v)
    return "—" if n is None else f"{n:.2f}"


def _em_secid(symbol: str) -> tuple[str, str, str]:
    """代码 → (market, code, secid)。market: 1=沪市，0=深市/北交所。"""
    sym = (symbol or "").strip().lower()
    code = sym[2:] if sym.startswith(("sh", "sz", "bj")) else sym
    if sym.startswith("sh"):
        market = "1"
    elif sym.startswith(("sz", "bj")):
        market = "0"
    else:
        market = "1" if code[:1] == "6" else "0"
    return market, code, f"{market}.{code}"


_REQ_DEADLINE = threading.local()  # P2-09：请求级处理时限（monotonic 秒），do_GET/do_POST 写入


def _get_text(url: str, referer: str, timeout: int = 12, retries: int = 2,
              encoding: str = "utf-8-sig", deadline=None):
    """通用取文本：直连优先 → 回退系统代理；偶发 RemoteDisconnected/超时自动重试。
    实测某些行情主机（push2/push2his）在连续请求后会直接掐连接（RemoteDisconnected），
    故重试是必需项，而不是可选项。"""
    headers = {"User-Agent": _EM_UA, "Referer": referer, "Accept": "*/*"}
    last_err = None
    for attempt in range(retries + 1):
        for opener in (_EM_DIRECT, urllib.request.build_opener()):
            try:
                req = urllib.request.Request(url, headers=headers)
                eff_timeout = timeout
                _dl = deadline if deadline is not None else getattr(_REQ_DEADLINE, "value", None)
                if _dl is not None:
                    eff_timeout = max(1, int(min(timeout, _dl - time.monotonic())))
                with opener.open(req, timeout=eff_timeout) as resp:
                    return resp.read().decode(encoding, "ignore")
            except Exception as e:  # noqa: BLE001 —— 网络类异常统一重试
                last_err = e
                # 关键：超时（TimeoutError / URLError(reason=TimeoutError)）说明该主机整体不通，
                # 再换 opener 或再重试只是把等待时间成倍放大，故立即跳出交给上层兜底。
                if isinstance(e, TimeoutError) or isinstance(getattr(e, "reason", None), TimeoutError):
                    logging.debug("取数超时，放弃重试 %s", url[:90])
                    return None
        if attempt < retries:
            time.sleep(0.35 * (attempt + 1))
    logging.debug("取数失败 %s（%s）", url[:110], last_err)
    return None


def _em_get_json(url: str, referer: str, timeout: int = 12, retries: int = 2):
    txt = _get_text(url, referer, timeout=timeout, retries=retries)
    if not txt:
        return None
    try:
        return json.loads(txt)
    except Exception:  # noqa: BLE001
        return None


def _em_quote_raw(secid: str, timeout: int = 5, retries: int = 0):
    """东财 push2 快照。超时/重试刻意收紧：实测该主机常直接掐连接，
    快速失败后交给腾讯兜底（0.2s 级）比在这里反复重试更快。

    ⚠️ 2026-10-09 实测补充：push2 stock/get 对本机是「**配额式**」风控——连续约 6 次请求后
    该 IP 被限流，此后一段时间**短/长字段表一律 data=null**（2s 间隔重试也失败）。因此
    调用侧（api_quote）**不做重试**，只用「东财优先 + 限时」（emapi.race_priority）。"""
    url = ("https://push2.eastmoney.com/api/qt/stock/get"
           f"?secid={secid}&fields={_QUOTE_FIELDS}&ut={_EM_UT}&invt=2&fltt=2")
    j = _em_get_json(url, "https://quote.eastmoney.com/", timeout=timeout, retries=retries)
    return (j or {}).get("data") or None


def _em_finance(code: str, market: str):
    """F10 主财务指标（最近一个报告期）。字段名自解释，避免依赖 f 编号语义。

    2026-09-29 卡顿修复：① 超时 12s×重试1 → 6s×0（东财限流时挂住 24s 是抽屉卡顿主因，
    快速失败后估值/行情本就有腾讯兜底，财务三盏灯短暂显示 — 可接受）；
    ② 加进程缓存：正面 30min（财务以季报为单位，日内不变）、**失败负缓存 2min**
    —— 限流窗口内连续点不同票/重复点同一票，不再每只都重新挂 6s。
    """
    hit = _FIN_CACHE.get(code)
    if hit and time.time() - hit[0] < (_FIN_TTL if hit[1] else _FIN_NEG_TTL):
        return hit[1]
    suffix = "SH" if market == "1" else ("BJ" if code[:1] in ("4", "8") else "SZ")
    params = urllib.parse.urlencode({
        "reportName": "RPT_F10_FINANCE_MAINFINADATA",
        "columns": _F10_COLS,
        "filter": f'(SECUCODE="{code}.{suffix}")',
        "sortColumns": "REPORT_DATE", "sortTypes": "-1",
        "pageSize": "1", "pageNumber": "1", "source": "HSF10", "client": "PC",
    })
    j = _em_get_json("https://datacenter.eastmoney.com/securities/api/data/v1/get?" + params,
                     "https://emweb.securities.eastmoney.com/", timeout=6, retries=0)
    rows = (((j or {}).get("result") or {}).get("data") or [])
    res = rows[0] if rows else None
    _FIN_CACHE[code] = (time.time(), res)
    return res


# 2026-09-26 新增：公司概况（主营业务 / 公司简介 / 行业 / 经营范围）
#   王总要求：每只股票点开后的「重要信息」板块里，要能直接看懂「这家公司是干什么的」。
#   数据源：东财 F10 公司概况 RPT_F10_BASIC_ORGINFO —— 单次请求即含全部所需字段
#   （实测 600030/300059/601688/300122 四只全部命中：MAIN_BUSINESS/ORG_PROFILE/EM2016/…）。
#   缓存：进程内 24h + 本地库 stock_org 长期落库 —— 公司概况以「年」为单位才变，
#   没必要每次开抽屉都联网；落库后离线也能看。
_ORG_COLS = ("SECUCODE,SECURITY_CODE,SECURITY_NAME_ABBR,ORG_NAME,MAIN_BUSINESS,BUSINESS_SCOPE,"
             "ORG_PROFILE,EM2016,INDUSTRYCSRC1,REG_CAPITAL,FOUND_DATE,LISTING_DATE,ORG_WEB,EMP_NUM")
_ORG_CACHE: dict[str, tuple[float, dict]] = {}
_ORG_TTL = 24 * 3600

# 2026-09-29 卡顿修复：财务 / 分红两路的进程缓存（见 _em_finance / _dividend_with_yield 注释）。
# 正面缓存存成功结果；负缓存存 None（失败）——限流窗口内不再每点一只票就挂一轮超时。
_FIN_CACHE: dict[str, tuple[float, object]] = {}
_FIN_TTL = 30 * 60        # 财务指标正面缓存 30 分钟（季报口径，日内不变）
_FIN_NEG_TTL = 120        # 失败负缓存 2 分钟
_DIV_CACHE: dict[str, tuple[float, object]] = {}
_DIV_TTL = 6 * 3600       # 分红正面缓存 6 小时（年度口径）
_DIV_NEG_TTL = 120        # 失败负缓存 2 分钟


def _f10_text(v) -> str:
    """F10 长文本归一：去 NBSP/全角空格/换行，压成单段（东财原文常带前导空格与硬换行）。"""
    s = str(v or "").replace("\u3000", " ").replace("\xa0", " ")
    return re.sub(r"\s+", " ", s).strip()


def _em_org_info(code: str, market: str) -> dict | None:
    """东财 F10 公司概况 → 归一化 dict（取不到返回 None）。"""
    suffix = "SH" if market == "1" else ("BJ" if code[:1] in ("4", "8") else "SZ")
    params = urllib.parse.urlencode({
        "reportName": "RPT_F10_BASIC_ORGINFO",
        "columns": _ORG_COLS,
        "filter": f'(SECUCODE="{code}.{suffix}")',
        "pageSize": "1", "pageNumber": "1", "source": "HSF10", "client": "PC",
    })
    j = _em_get_json("https://datacenter.eastmoney.com/securities/api/data/v1/get?" + params,
                     "https://emweb.securities.eastmoney.com/", timeout=12, retries=1)
    rows = (((j or {}).get("result") or {}).get("data") or [])
    if not rows:
        return None
    r = rows[0]
    main_business = _f10_text(r.get("MAIN_BUSINESS"))
    profile = _f10_text(r.get("ORG_PROFILE"))
    if not main_business and not profile:
        return None
    return {
        "code": code,
        "name": _f10_text(r.get("SECURITY_NAME_ABBR")),
        "full_name": _f10_text(r.get("ORG_NAME")),
        "main_business": main_business,
        "profile": profile,
        "business_scope": _f10_text(r.get("BUSINESS_SCOPE")),
        "industry": _f10_text(r.get("EM2016")),
        "industry_csrc": _f10_text(r.get("INDUSTRYCSRC1")),
        "found_date": str(r.get("FOUND_DATE") or "")[:10],
        "list_date": str(r.get("LISTING_DATE") or "")[:10],
        "reg_capital": _num(r.get("REG_CAPITAL")),
        "emp_num": _num(r.get("EMP_NUM")),
        "web": _f10_text(r.get("ORG_WEB")),
        "source": "eastmoney_f10",
    }


def _org_info(code: str, market: str, force: bool = False) -> dict | None:
    """公司概况取数：进程缓存(24h) → 本地库 stock_org → 联网抓（成功即落库）。
    联网失败时回退已有缓存/库值 —— 避免「点一下刷新反而没数据」。"""
    hit = _ORG_CACHE.get(code)
    if hit and not force and time.time() - hit[0] < _ORG_TTL:
        return hit[1]
    info = _em_org_info(code, market)
    if info:
        _ORG_CACHE[code] = (time.time(), info)
        try:
            DB.org_put(code, info)
        except Exception:  # noqa: BLE001 —— 落库失败不影响本次展示
            logging.debug("公司概况落库失败 %s", code, exc_info=True)
        return info
    if hit:
        return hit[1]
    try:
        cached = DB.org_get(code)
    except Exception:  # noqa: BLE001
        cached = None
    if cached:
        _ORG_CACHE[code] = (time.time(), cached)
    return cached


# 2026-09-25 新增：分红送配 → 股息率（王总价值标准之一：股息率 > 3%）
#   口径已实测确认：RPT_SHAREBONUS_DET.PRETAX_BONUS_RMB = **每 10 股税前派息(元)**。
#   验证：600519 近 12 个月(按除权日) 280.2423 + 239.57 = 519.8123 元/10股
#         → 每股 51.98 元 / 1237.00 元 = 4.20%，与市场公布口径一致（若按"每股"解读会得 42%，明显失真）。
_DIV_COLS = ("SECUCODE,SECURITY_CODE,SECURITY_NAME_ABBR,REPORT_DATE,EX_DIVIDEND_DATE,"
             "PLAN_NOTICE_DATE,PRETAX_BONUS_RMB,BONUS_IT_RATIO,IT_RATIO,DIVIDENT_RATIO,"
             "ASSIGN_PROGRESS,IMPL_PLAN_PROFILE")


def _dividend_with_yield(code: str, market: str, price) -> dict | None:
    """分红带缓存包装：正面 6h / 失败负缓存 2min（东财限流时点开抽屉不再反复挂超时）。"""
    hit = _DIV_CACHE.get(code)
    if hit and time.time() - hit[0] < (_DIV_TTL if hit[1] else _DIV_NEG_TTL):
        return hit[1]
    r = _dividend_fetch(code, market, price)
    _DIV_CACHE[code] = (time.time(), r)
    return r


def _dividend_fetch(code: str, market: str, price) -> dict | None:
    """股息率：**最近一个已实施「年度分红」的完整会计年度**的每股分红合计 / 最新价。

    为什么不用"近 12 个月按除权日回推"：A 股大量公司已改一年两次分红，回推 12 个月会落入
    3 期（覆盖约 1.5 个年度）导致虚高——实测 600519 会得 6.44%（市场普遍口径 4.2%）、
    000001 得 7.48%（市场约 5.3%）。改为"按报告期归集到完整年度"后与市场口径一致：
      600519 FY2025 = 280.2423 + 239.57 = 519.8123 元/10股 → 每股 51.98 / 1237.00 = 4.20%
      000001 FY2025 = 3.6 + 2.36 = 5.96 元/10股        → 每股 0.596 / 11.30  = 5.27%
      601398 FY2025 = 1.689 + 1.414 = 3.103 元/10股      → 每股 0.310 / 7.40   = 4.19%
    取不到返回 None（前端显示 —）。
    """
    suffix = "SH" if market == "1" else ("BJ" if code[:1] in ("4", "8") else "SZ")
    params = urllib.parse.urlencode({
        "reportName": "RPT_SHAREBONUS_DET",
        "columns": _DIV_COLS,
        "filter": f'(SECUCODE="{code}.{suffix}")',
        "sortColumns": "REPORT_DATE", "sortTypes": "-1",
        "pageSize": "30", "pageNumber": "1", "source": "WEB", "client": "WEB",
        "ut": _EM_UT,
    })
    j = _em_get_json("https://datacenter-web.eastmoney.com/api/data/v1/get?" + params,
                     "https://data.eastmoney.com/", timeout=6, retries=0)
    rows = (((j or {}).get("result") or {}).get("data") or [])
    if not rows:
        return None
    # 只取「实施分配」，避免「预案」与「实施」同名报告期被重复计入
    done = [r for r in rows if "实施" in str(r.get("ASSIGN_PROGRESS") or "")]
    use = done or rows
    # 按 REPORT_DATE 年份归集（同一年度的「年度 + 中期」合并为该年度总派息）
    by_year: dict[str, list[dict]] = {}
    for r in use:
        rd = str(r.get("REPORT_DATE") or "")[:10]
        v = _num(r.get("PRETAX_BONUS_RMB"))
        if len(rd) < 7 or not v:
            continue
        by_year.setdefault(rd[:4], []).append({
            "ex_date": str(r.get("EX_DIVIDEND_DATE") or "")[:10],
            "report": rd, "per_share": round(v / 10.0, 4),
            "progress": r.get("ASSIGN_PROGRESS") or "",
        })
    if not by_year:
        return None
    # 只在「存在年度分红（报告期为 12 月）」的年份里取最新 → 保证是完整年度，不被当年中报带偏
    full_years = [y for y, its in by_year.items() if any(x["report"][5:7] == "12" for x in its)]
    y = max(full_years) if full_years else max(by_year)
    items = sorted(by_year[y], key=lambda x: x["report"])
    per_share = round(sum(x["per_share"] for x in items), 4)
    p = _num(price)
    return {
        "per_share": per_share,                            # 该年度每股税前派息合计（元）
        "per10": round(sum(x["per_share"] for x in items) * 10, 4),
        "yield_pct": round(per_share / p * 100, 2) if p else None,
        "fiscal_year": y,
        "basis": f"{y} 年度（含中期）" if len(items) > 1 else f"{y} 年度",
        "items": items[:6],
        "note": f"{y} 会计年度已实施分红合计 ÷ 最新价，税前",
    }


# --- 备用 K 线源：新浪（可一次返回「全历史」，支撑拖到上市首日）
#     用途：东财 push2his 实测会直接掐连接（RemoteDisconnected），此时腾讯兜底只给 120 根，
#           「拖到上市首日」就做不到。新浪一次可取数千根，实测 600519 = 6012 根
#           （2001-08-27 上市首日 → 最新交易日），耗时约 0.5s，返回**标准 JSON**。
#     口径：**不复权**（首日 close=35.550 即真实首日价）。故长期图上会出现除权跳空，
#           前端会标注「不复权」，与东财/腾讯的「前复权」区分显示，避免误判为大跌。
_SINA_KLINE_URL = ("https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/"
                   "CN_MarketData.getKLineData?symbol={sym}&scale=240&ma=no&datalen={n}")
_SINA_MAX = 12000


def _sina_kline_rows(symbol: str, period: str, timeout: int = 15, retries: int = 2):
    """新浪日线 → 与东财同构的行。新浪无周/月线接口，故周/月由日线本地聚合。

    2026-10-09：新增 timeout / retries 参数——竞速路径用 timeout=3、retries=0 快速失败，
    避免新浪抽风时把复用的竞速线程池长期占用。默认值保持原行为。"""
    market, code, _ = _em_secid(symbol)
    sym = _tx_prefix(code, market) + code
    txt = _get_text(_SINA_KLINE_URL.format(sym=sym, n=_SINA_MAX),
                    "https://finance.sina.com.cn/", timeout=timeout, retries=retries,
                    encoding="utf-8")
    if not txt or "day" not in txt:
        return None
    try:
        raw = json.loads(txt)
    except ValueError:
        return None
    if not isinstance(raw, list) or not raw:
        return None
    rows: list[dict] = []
    for r in raw:
        if not isinstance(r, dict):
            continue
        d = str(r.get("day") or "")[:10]
        o, h, lo, c = (_num(r.get("open")), _num(r.get("high")),
                       _num(r.get("low")), _num(r.get("close")))
        vol = _num(r.get("volume"))
        if not d or None in (o, h, lo, c):
            continue
        rows.append({"date": d, "open": o, "high": h, "low": lo, "close": c,
                     "volume": vol, "amount": (round(c * vol, 2) if vol else None),
                     "pct": None})
    if not rows:
        return None
    rows.sort(key=lambda x: x["date"])
    rows = _agg_kline(rows, period)
    for i, r in enumerate(rows):
        base = rows[i - 1]["close"] if i > 0 else r["open"]
        r["pct"] = round((r["close"] - base) / base * 100, 2) if base else 0.0
    return rows


def _agg_kline(rows: list[dict], period: str) -> list[dict]:
    """日线 → 周线/月线（本地聚合：开=首、高=max、低=min、收=末、量=sum）。"""
    if period == "day" or not rows:
        return rows
    import datetime as _dt
    buckets: dict[str, list[dict]] = {}
    order: list[str] = []
    for r in rows:
        try:
            d = _dt.date.fromisoformat(r["date"])
        except ValueError:
            continue
        if period == "month":
            k = f"{d.year:04d}-{d.month:02d}"
        else:  # week：ISO 年-周
            iso = d.isocalendar()
            k = f"{iso[0]:04d}-W{iso[1]:02d}"
        if k not in buckets:
            buckets[k] = []
            order.append(k)
        buckets[k].append(r)
    out: list[dict] = []
    for k in order:
        grp = buckets[k]
        vol = sum((g.get("volume") or 0) for g in grp)
        amts = [g.get("amount") for g in grp if g.get("amount") is not None]
        out.append({
            "date": grp[-1]["date"], "open": grp[0]["open"], "close": grp[-1]["close"],
            "high": max(g["high"] for g in grp), "low": min(g["low"] for g in grp),
            "volume": vol, "amount": (round(sum(amts), 2) if amts else None), "pct": None,
        })
    return out


def _sina_kline_result(symbol: str, period: str, timeout: int = 15, retries: int = 2):
    """新浪 → 与 _eastmoney_kline 同构的 payload。"""
    rows = _sina_kline_rows(symbol, period, timeout=timeout, retries=retries)
    if not rows:
        return None
    _, code, _ = _em_secid(symbol)
    last = rows[-1]
    prev = rows[-2]["close"] if len(rows) >= 2 else last["open"]
    return {
        "symbol": symbol, "code": code, "name": "", "period": period,
        "count": len(rows), "klines": rows, "adjust": "raw",
        "full_history": True,   # 新浪始终返回全历史
        "amount_est": True,   # 新浪不提供成交额，按「收盘价 × 成交量」估算，前端以 ≈ 标注
        "source": f"新浪行情（不复权 · 全历史 {len(rows)} 根）",
        "first_date": rows[0]["date"], "last_date": last["date"],
        "current": {"price": last["close"], "pct": last["pct"], "open": last["open"],
                    "high": last["high"], "low": last["low"], "prev_close": prev,
                    "volume": last["volume"], "amount": last["amount"], "date": last["date"]},
    }


# --- 备用行情源：腾讯（公开、稳定）
#     用途：东财 push2 / push2his 被限流或直接掐连接（实测 RemoteDisconnected）时自动兜底。
#     字段位置已用 600519 与东财真值逐项交叉校验（价/昨收/开/高/低/涨跌/PE三口径/PB/市值/换手/振幅/量比 全一致）。
_TX_FIELD = {
    "name": 1, "price": 3, "prev_close": 4, "open": 5, "volume": 6,
    "time": 30, "change": 31, "pct": 32, "high": 33, "low": 34,
    "amount_wan": 37, "turnover": 38, "pe_ttm": 39, "amplitude": 43,
    "float_mv_yi": 44, "total_mv_yi": 45, "pb": 46, "volume_ratio": 49,
    "pe_dyn": 52, "pe_static": 53,
}


def _tx_prefix(code: str, market: str) -> str:
    if market == "1":
        return "sh"
    if code[:1] in ("4", "8") or code.startswith("92"):
        return "bj"
    return "sz"


def _fmt_time_em(ts) -> str:
    try:
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(float(ts)))
    except Exception:  # noqa: BLE001
        return ""


def _fmt_time_tx(s: str) -> str:
    s = (s or "").strip()
    if len(s) >= 12 and s.isdigit():
        return f"{s[0:4]}-{s[4:6]}-{s[6:8]} {s[8:10]}:{s[10:12]}"
    return s


# ---------------------------------------------------------------------------
# 2026-10-09 新增：行情新鲜度（顶栏「数据时间」真正该看的东西）。
#   背景：顶栏此前显示的「X 分钟之前」= 论坛帖子抓取新鲜度（/api/refresh_status.last_ok_at），
#   跟**行情**毫无关系 —— 于是出现「显示 6 分钟前刷新过，点开却是昨天的数据」的错位。
#   本模块独立现抓一个基准标的（上证指数 sh000001，永不停牌）的**实时行情时间戳**，
#   让顶栏能如实显示「行情是几点几分几秒的」，与论坛抓取分开计时。
#   原则：一律**现抓**，绝不用库里旧数据冒充；三源并发竞速，谁先回用谁，整体 ≤3s。
# ---------------------------------------------------------------------------
_MARKET_BENCHMARK = "sh000001"          # 上证指数（永不停牌，最合适的基准）
_MARKET_FRESH_CACHE: dict = {"ts": 0.0, "data": None}
_MARKET_FRESH_TTL = 2.0                  # 行情原子缓存仅 2s（几乎等同现抓）；lag 每次现算


def _bench_tencent(sym: str = _MARKET_BENCHMARK):
    """腾讯快照的 (name, 行情时间, 价) —— parts[30]=yyyymmddHHMMSS。"""
    txt = _get_text(f"https://qt.gtimg.cn/q={sym}", "https://gu.qq.com/",
                    timeout=3, retries=0, encoding="gbk")
    if not txt or "=" not in txt:
        return None
    parts = txt.split("=", 1)[1].strip().strip(";").strip('"').split("~")
    if len(parts) < 33:
        return None
    t = parts[30] if len(parts) > 30 else ""
    if not (len(t) >= 14 and t[:14].isdigit()):
        return None
    qt = f"{t[0:4]}-{t[4:6]}-{t[6:8]} {t[8:10]}:{t[10:12]}:{t[12:14]}"
    return {"src": "tencent", "name": parts[1] or "", "quote_time": qt,
            "price": _num(parts[3])}


def _bench_sina(sym: str = _MARKET_BENCHMARK):
    """新浪快照的 (name, 行情时间, 价) —— 指数格式 f[30]=日期, f[31]=时间。"""
    txt = _get_text(f"https://hq.sinajs.cn/list={sym}", "https://finance.sina.com.cn/",
                    timeout=3, retries=0, encoding="gbk")
    if not txt or "=" not in txt:
        return None
    body = txt.split("=", 1)[1].strip().strip(";").strip('"')
    f = body.split(",")
    if len(f) < 32:
        return None
    d, tm = (f[30] or "").strip(), (f[31] or "").strip()
    if not d or not tm:
        return None
    return {"src": "sina", "name": f[0] or "", "quote_time": f"{d} {tm}",
            "price": _num(f[3])}


def _bench_em(secid: str = "1.000001"):
    """东财 push2 快照的 (name, 行情时间, 价) —— f86=行情时间戳(秒)。"""
    q = _em_quote_raw(secid, timeout=3, retries=0)
    if not q:
        return None
    ts = q.get("f86")
    try:
        qt = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(float(ts)))
    except (TypeError, ValueError):
        return None
    return {"src": "eastmoney", "name": q.get("f58") or "", "quote_time": qt,
            "price": _num(q.get("f43"))}


def _is_trading_now() -> bool:
    """当前是否处于 A 股连续竞价时段（且今天是交易日）。休市/节假日返回 False。"""
    now = time.localtime()
    if now.tm_wday >= 5:                      # 周末
        return False
    hm = now.tm_hour * 60 + now.tm_min
    in_hours = (9 * 60 + 30) <= hm <= (11 * 60 + 30) or (13 * 60) <= hm <= (15 * 60)
    if not in_hours:
        return False
    try:                                      # 交易时段内再确认「今天开市」（权威日历）
        return auction.is_trading_day() is not False
    except Exception:  # noqa: BLE001 —— 日历不可用时按工作日+时段判断
        return True


def _tencent_quote(code: str, market: str, timeout: int = 10, retries: int = 2):
    """腾讯快照 → 归一化 {name, quote, valuation}（无 ROE 等财务项，财务仍取东财 F10）。

    2026-10-09：新增 timeout / retries 参数——竞速路径用 timeout=3、retries=0 快速失败，
    避免本机偶尔抽风时把复用的竞速线程池长期占用。默认值保持原行为（10s / 2 次重试）。"""
    sym = _tx_prefix(code, market) + code
    txt = _get_text(f"https://qt.gtimg.cn/q={sym}", "https://gu.qq.com/",
                    timeout=timeout, retries=retries, encoding="gbk")
    if not txt or "=" not in txt:
        return None
    parts = txt.split("=", 1)[1].strip().strip(";").strip('"').split("~")
    if len(parts) < 46:
        return None

    def g(k):
        i = _TX_FIELD[k]
        return parts[i] if i < len(parts) else ""

    amt_wan = _num(g("amount_wan"))
    mv_f, mv_t = _num(g("float_mv_yi")), _num(g("total_mv_yi"))
    return {
        "name": parts[1] or "",
        "quote": {
            "price": _num(g("price")), "change": _num(g("change")), "pct": _num(g("pct")),
            "open": _num(g("open")), "high": _num(g("high")), "low": _num(g("low")),
            "prev_close": _num(g("prev_close")), "volume": _num(g("volume")),
            "amount": (amt_wan * 1e4) if amt_wan is not None else None,   # 万元 → 元
            "turnover": _num(g("turnover")), "amplitude": _num(g("amplitude")),
            "volume_ratio": _num(g("volume_ratio")),
            "time": _fmt_time_tx(g("time")), "list_date": "",
        },
        "valuation": {
            "pe_ttm": _num(g("pe_ttm")), "pe_dyn": _num(g("pe_dyn")),
            "pe_static": _num(g("pe_static")), "pb": _num(g("pb")),
            "total_mv": (mv_t * 1e8) if mv_t is not None else None,       # 亿元 → 元
            "float_mv": (mv_f * 1e8) if mv_f is not None else None,
            "bps": None,
        },
    }


def _tencent_kline(code: str, market: str, period: str, timeout: int = 12, retries: int = 2,
                   full_history: bool = False):
    """腾讯 K 线 → 与东财同构的行情行（date/open/close/high/low/volume/pct）。

    2026-10-09：新增 timeout / retries 参数——竞速路径用 timeout=3、retries=0。
    2026-10-13（进哥）：兜底单次只给 120 根，却被前端当成「上市至今」全历史，
    半年走势冒充上市行情、直接影响判断。新增 full_history=True 分段循环模式：
    fqkline 接口支持起止日期，按 640 根/页用「最早日-1天」往回翻，直到翻不动
    （封顶 40 页 ≈ 2.5 万根防失控）；竞速快路径保持单页不变。"""
    sym = _tx_prefix(code, market) + code
    key = {"day": "qfqday", "week": "qfqweek", "month": "qfqmonth"}.get(period, "qfqday")
    cnt = 120 if period == "day" else 160
    url_base = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"

    def _fetch(start: str, end: str, n: int) -> list:
        j = _em_get_json(f"{url_base}?param={sym},{period},{start},{end},{n},qfq",
                         "https://gu.qq.com/", timeout=timeout, retries=retries)
        d = ((j or {}).get("data") or {}).get(sym) or {}
        return list(d.get(key) or d.get(period) or [])

    raw = _fetch("", "", cnt)
    if full_history and raw:
        import datetime as _dt
        seen: set = set()
        for r in raw:
            if isinstance(r, (list, tuple)):
                seen.add(str(r[0])[:10])
        for _page in range(40):                      # 防失控上限
            earliest = min(seen) if seen else ""
            if not earliest or earliest <= "1991-01-01":   # 已到 A 股最早交易日 → 翻完了
                break
            try:
                end_dt = (_dt.date.fromisoformat(earliest)
                          - _dt.timedelta(days=1)).isoformat()
            except ValueError:
                break
            more = _fetch("1990-01-01", end_dt, 640)
            new = [r for r in more
                   if isinstance(r, (list, tuple)) and str(r[0])[:10] not in seen]
            if not new:
                break
            for r in new:
                seen.add(str(r[0])[:10])
            raw = new + raw                          # 新页更早，往前拼（两侧各自升序）
    rows = []
    for r in raw:
        if not isinstance(r, (list, tuple)) or len(r) < 5:
            continue
        vol = _num(r[5]) if len(r) > 5 else None
        close = _num(r[2])
        rows.append({"date": str(r[0])[:10], "open": _num(r[1]), "close": close,
                     "high": _num(r[3]), "low": _num(r[4]),
                     "volume": vol,
                     "amount": (round(close * vol, 2) if (close and vol) else None),
                     "pct": None})
    for i, r in enumerate(rows):
        base = rows[i - 1]["close"] if i > 0 else r["open"]
        r["pct"] = round((r["close"] - base) / base * 100, 2) if base else 0.0
    return rows or None


def _em_quote_norm(q) -> tuple[dict, dict]:
    """东财 push2 原始字段 → 归一化 (quote, valuation)。"""
    quote = {
        "price": _num(q.get("f43")), "change": _num(q.get("f169")), "pct": _num(q.get("f170")),
        "open": _num(q.get("f46")), "high": _num(q.get("f44")), "low": _num(q.get("f45")),
        "prev_close": _num(q.get("f60")),
        "volume": _num(q.get("f47")),         # 成交量（手）
        "amount": _num(q.get("f48")),         # 成交额（元）
        "turnover": _num(q.get("f168")),      # 换手率 %
        "amplitude": _num(q.get("f171")),     # 振幅 %
        "volume_ratio": _num(q.get("f50")),   # 量比
        "list_date": str(q.get("f189") or ""),  # 上市日期 YYYYMMDD
        "time": _fmt_time_em(q.get("f86")),     # 行情时间
    }
    valuation = {
        "pe_ttm": _num(q.get("f164")), "pe_dyn": _num(q.get("f162")),
        "pe_static": _num(q.get("f163")), "pb": _num(q.get("f167")),
        "total_mv": _num(q.get("f116")), "float_mv": _num(q.get("f117")),
        "bps": _num(q.get("f92")),
    }
    return quote, valuation


def _holders_payload(code: str) -> dict | None:
    """股东透视（本地库，法定披露口径）：
    - top10：最新一期十大流通股东（含自然人/机构、持股比例、增减、报告期）
    - num：股东户数趋势（筹码集中度）
    无数据返回 None（前端显示"暂无"，不编造）。
    数据由 holders_fetch.py 抓取入库（stock_holders / stock_holder_num）。"""
    try:
        rows = DB.conn.execute(
            """SELECT end_date, notice_date, holder_rank, holder_name, holder_type,
                      hold_num, hold_ratio, free_ratio, hold_change, change_ratio, scope
               FROM stock_holders WHERE symbol=?
               ORDER BY end_date DESC, scope, holder_rank""", (code,)).fetchall()
    except Exception:  # noqa: BLE001 —— 表不存在时静默降级
        return None
    if not rows:
        return None
    # 最新一期（按 end_date），优先 free（十大流通股东）口径
    latest = rows[0]["end_date"]
    free = [dict(r) for r in rows if r["end_date"] == latest and r["scope"] == "free"]
    total = [dict(r) for r in rows if r["end_date"] == latest and r["scope"] == "total"]
    pick = free or total
    if not pick:
        return None
    naturals = [r for r in pick if (r["holder_type"] or "") == "自然人"]
    orders = [r for r in pick if (r["holder_type"] or "") == "机构"]
    # 股东户数趋势（最近 6 期）
    nums: list[dict] = []
    try:
        for r in DB.conn.execute(
            """SELECT end_date, holder_total_num, total_num_ratio, avg_hold_amt, hold_focus
               FROM stock_holder_num WHERE symbol=? ORDER BY end_date DESC LIMIT 6""",
                (code,)).fetchall():
            nums.append({"end_date": r["end_date"], "num": r["holder_total_num"],
                         "chg_pct": r["total_num_ratio"], "avg_amt": r["avg_hold_amt"],
                         "focus": r["hold_focus"] or ""})
    except Exception:  # noqa: BLE001
        pass
    return {
        "report_date": latest,
        "scope": "free" if free else "total",
        "scope_label": "十大流通股东" if free else "十大股东",
        "notice_date": (pick[0].get("notice_date") or "")[:10],
        "top10": [{"rank": r["holder_rank"], "name": r["holder_name"] or "",
                   "type": r["holder_type"] or "", "num": r["hold_num"],
                   "ratio": r["hold_ratio"], "free_ratio": r["free_ratio"],
                   "change": r["hold_change"] or "", "change_ratio": r["change_ratio"]}
                  for r in pick[:10]],
        "natural_count": len(naturals),
        "org_count": len(orders),
        "num_trend": nums,
        "note": "法定披露口径（季报/年报），非实时持仓",
    }


def api_quote(symbol: str, force: bool = False) -> dict:
    """单只股票「重要信息」：行情 + 估值 + 财务（含报告期）。默认 60s 内存缓存。
    行情/估值：东财 push2，失败自动兜底腾讯行情；财务：东财 F10 具名字段。"""
    if not symbol:
        return {"error": "symbol required"}
    key = (symbol or "").strip()
    if not force:
        hit = _QUOTE_CACHE.get(key)
        if hit and time.time() - hit[0] < 60:
            return hit[1]
    market, code, secid = _em_secid(key)
    # 2026-10-09（收尾修正）：改为「东财优先 + 限时」的并发竞速（emapi.race_priority）——
    # 东财在 grace（1.8s）内回就用东财（字段最全，含 valuation.bps）；没回则立刻用先回的腾讯，
    # 绝不干等。既保住「东财健康时字段完整」，又保证「东财抽风时不拖慢」（上限 3s）。
    # 返回结构不变（仍从 quote/valuation/name/src_quote 拼 payload）。
    def _try_em() -> dict | None:
        # 单次尝试即可：push2 是「**配额式**」风控——实测连续 ~6 次后该 IP 被限流，
        # 且限流窗口内**短时间重试不会成功**（短/长字段表一起失败，2s 间隔也照样失败）。
        # 故绝不重试（重试只会白等、还更快触发限流）；东财 grace 窗口内没回就用先回的其它源。
        q = _em_quote_raw(secid, timeout=3, retries=0)
        if not q:
            return None
        _quote, _val = _em_quote_norm(q)
        return {"src_quote": "东方财富 push2", "quote": _quote, "valuation": _val,
                "name": q.get("f58") or ""}

    def _try_tx() -> dict | None:
        tx = _tencent_quote(code, market, timeout=3, retries=0)
        if not tx:
            return None
        return {"src_quote": "腾讯行情（东财 push2 不可用，已自动兜底）",
                "quote": tx["quote"], "valuation": tx["valuation"], "name": tx["name"]}

    # 东财优先 + 限时：东财 1.8s 内回 → 用东财（字段最全，含 valuation.bps）；
    # 1.8s 内没回（含被风控快速失败）→ 立刻用先回的其它源，绝不干等（整体上限 3s）。
    # 为何 1.8s：实测「东财健康时」这一路（长字段表）单次约 1.1s，故 1.4s 太紧、易漏；
    # 而「东财被限流」时是**快速**回空（~0.3s），此时并不在 grace 里干等 —— 故放宽 grace 几乎零成本。
    picked = emapi.race_priority(_try_em, [_try_tx], grace=1.8, timeout=3.0)
    if picked is None:
        return {"error": "重要信息获取失败：代码可能无效，或本机网络无法访问行情源"
                         "（东方财富 / 腾讯均不可用）。", "symbol": key}
    quote, valuation, name = picked["quote"], picked["valuation"], picked["name"]
    src_quote = picked["src_quote"]
    # 2026-09-26：财务 / 分红 / 公司概况三路并行取数。
    # 三者互不依赖，串行会把「首次打开一只没缓存的股票」拖到 3 秒以上；并行后约等于最慢一路。
    with cf.ThreadPoolExecutor(max_workers=3) as ex:
        f_fin = ex.submit(_em_finance, code, market)
        f_dv = ex.submit(_dividend_with_yield, code, market, quote.get("price"))
        f_org = ex.submit(_org_info, code, market, bool(force))
        fin = f_fin.result() or {}
        dv = f_dv.result()
        org = f_org.result()
    payload = {
        "symbol": code,
        "name": name or fin.get("SECURITY_NAME_ABBR") or "",
        "source": f"{src_quote} + 东方财富 F10 财务指标"
                  + (" + 分红送配（近 12 个月）" if dv else "")
                  + (" + F10 公司概况（主营 / 简介）" if org else ""),
        "quote": quote,
        "valuation": valuation,
        "dividend": dv,
        "org": org,
        "finance": {
            "report": fin.get("REPORT_DATE_NAME") or "",
            "report_date": str(fin.get("REPORT_DATE") or "")[:10],
            "eps": _num(fin.get("EPSJB")), "eps_deduct": _num(fin.get("EPSKCJB")),
            "bps": _num(fin.get("BPS")),
            "roe": _num(fin.get("ROEJQ")), "roe_deduct": _num(fin.get("ROEKCJQ")),
            "gross_margin": _num(fin.get("XSMLL")), "net_margin": _num(fin.get("XSJLL")),
            "roa": _num(fin.get("ZZCJLL")),
            "revenue": _num(fin.get("TOTALOPERATEREVE")),
            "revenue_yoy": _num(fin.get("TOTALOPERATEREVETZ")),
            "net_profit": _num(fin.get("PARENTNETPROFIT")),
            "net_profit_yoy": _num(fin.get("PARENTNETPROFITTZ")),
            "ocf_ps": _num(fin.get("MGJYXJJE")),
            # 2026-09-30：补资产负债率——「巴菲特口径」8 项筛选里的第七项（东财 F10 = ZCFZL）。
            # 个股诊断页要用它跟周报同口径展示，缺了会只剩 7 项。
            "debt_ratio": _num(fin.get("ZCFZL")),
        },
        "holders": _holders_payload(code),
    }
    _QUOTE_CACHE[key] = (time.time(), payload)
    return payload


def api_sync_names(symbols: list[str] | None = None, online: bool = True, force: bool = False) -> dict:
    """补全股票名称：离线表优先，缺失的联网（东方财富）查询后落库缓存，并回填历史记录。
    force=True 时对已识别的代码也重新联网核对（用于股票更名等场景）。"""
    codes = symbols or DB.all_symbols()
    res = get_resolver(db=DB).sync(codes, online=online) if force else \
        get_resolver(db=DB).backfill_missing(codes, online=online)
    return {"requested": res["requested"], "resolved": res["resolved"], "persisted": res["persisted"],
            "items": res["items"]}


def api_person(name: str) -> dict:
    """按姓名/昵称聚合该用户的信息（不加载任何原始页面 HTML，轻量快速）。"""
    if not name:
        return {"error": "name required"}
    row = DB.conn.execute("SELECT * FROM persons WHERE name=?", (name,)).fetchone()
    meta = dict(row) if row else {"name": name, "mention_count": 0, "role": "unknown"}
    posts = [dict(r) for r in DB.conn.execute(
        "SELECT id, site, post_time, text, sentiment FROM posts WHERE author=? ORDER BY post_time DESC LIMIT 50",
        (name,)).fetchall()]
    trades = [dict(r) for r in DB.conn.execute(
        "SELECT id, site, post_time, action, symbol, symbol_name, price, quantity, "
        "buy_time, sell_time, hold_since FROM trades WHERE author=? ORDER BY post_time DESC LIMIT 50",
        (name,)).fetchall()]
    positions = [dict(r) for r in DB.conn.execute(
        "SELECT channel, site, symbol, symbol_name, ratio, action, post_url, post_time, "
        "buy_time, buy_price, current_price, sell_time, sell_price, hold_since "
        "FROM positions WHERE player=? ORDER BY symbol",
        (name,)).fetchall()]
    # 2026-09-23 修复（一致性收尾）：标注每只标的的持仓起始日期与持有天数，并显式声明基准。
    # 原实现按「每条记录自身的 buy_time」逐行算 hold_days，既没标口径也没给 trades 赋值。
    # 现在与 /api/player(db.py player_detail 的 since_map / _enrich，473-530 行) 完全同口径：
    # 建仓起点只取自**该人自身**的 positions+trades，绝不取全市场最早（那是 /api/stock）。
    def _act(it) -> str:
        return (it.get("action") or "").strip().upper()

    records = positions + trades
    inferred: dict[str, str] = {}
    explicit_since: dict[str, str] = {}
    for it in records:
        sym = (it.get("symbol") or "").strip()
        if not sym:
            continue
        v = (it.get("hold_since") or "").strip()
        if v:
            explicit_since.setdefault(sym, v)
        if _act(it) in ("BUY", "HOLD"):
            ts = (it.get("buy_time") or it.get("post_time") or "").strip()
            if ts and (sym not in inferred or ts < inferred[sym]):
                inferred[sym] = ts
    since_map = {**inferred, **explicit_since}
    today = datetime.now().strftime("%Y-%m-%d %H:%M")
    for it in records:
        since = since_map.get((it.get("symbol") or "").strip()) or ""
        it["hold_since"] = since
        is_sell = _act(it) == "SELL"
        # 与 db.py:511-520 同口径：卖出记录算到卖出日（区间长度），在持记录算到今日（已持有）
        end = ((it.get("sell_time") or it.get("buy_time") or it.get("post_time") or "").strip()
               if is_sell else today)
        it["hold_days"] = _hold_days(since, end)
        # 与 db.py:529 同口径标注：closed=已了结 / open=仍在持（决定 hold_days 的终点）
        it["hold_days_mode"] = "closed" if is_sell else "open"
    # 顶层「本人首次建仓」口径的总天数：取该人自身最早的一笔建仓至今
    own_since = min((v for v in since_map.values() if v), default="")
    hold_days = _hold_days(own_since) if own_since else None
    for p in positions:
        # 2026-09-22 修复 P2-4：此前未返回 channel，前端 app.js 只能兜底成 'A'，
        # 导致人物抽屉的渠道徽章恒显示「A 实盘大赛」。这里如实带回（缺失时给空串，不留兜底）。
        p["channel"] = (p.get("channel") or "")
    # 提及过的股票（去重）
    mentioned = []
    seen = set()
    for p in posts:
        for code in ("600519", "300750", "000858", "002594", "601318", "000725", "603259",
                     "300059", "600036", "002230", "000333", "603501"):
            if code in (p.get("text") or "") and code not in seen:
                seen.add(code); mentioned.append(code)
    return {
        "name": name,
        # 2026-09-23 修复（一致性收尾，A 点）：显式声明本视图 hold_days 的基准口径。
        # person_first_build = 建仓起点取自「该人自身」最早的一笔建仓记录（positions+trades，
        # 与 /api/player 的 player_first_build 同为「本人」口径，故两处天数可对得上）；
        # 与 /api/stock 的 symbol_first_build（全市场最早建仓）不同，故个股页天数可能更大。
        "hold_days_basis": "person_first_build",
        "hold_days": hold_days,
        "meta": meta, "posts": posts, "trades": trades,
        "positions": positions, "mentioned_stocks": mentioned,
        "post_count": len(posts), "trade_count": len(trades),
        "position_count": len(positions),
    }


def api_player(name: str) -> dict:
    """实盘选手详情（富化）：渠道/名次/评分 + 持仓完整明细（买入时间/买入价/当前股价/权限）
    + 全部交易 + 近5日操作记录 + 受限项汇总。详情页核心接口，轻量（不含原始页面 HTML）。"""
    if not name:
        return {"error": "name required"}
    detail = DB.player_detail(name, recent_days=5)
    if detail is None:
        return {"error": "player not found"}
    return detail


def api_detail(kind: str, _id: int, full: bool = False) -> dict:
    if kind == "post":
        if full:
            # 仅在用户显式点击「查看原始文档」时返回完整原始页面 HTML（后台库完整保留）。
            row = DB.conn.execute(
                "SELECT p.id, p.site, p.author, p.post_id, p.post_url, p.post_time, p.text, "
                "p.sentiment, p.sentiment_score, rp.html AS raw_html "
                "FROM posts p LEFT JOIN raw_pages rp ON rp.id=p.raw_page_id WHERE p.id=?",
                (_id,),
            ).fetchone()
            return dict(row) if row else {"error": "not found"}
        # 默认：仅结构化字段，不返回可能数 MB 的原始页面，避免前端卡顿。
        row = DB.conn.execute(
            "SELECT id, site, author, post_id, post_url, post_time, text, sentiment, sentiment_score "
            "FROM posts WHERE id=?", (_id,),
        ).fetchone()
    elif kind == "trade":
        row = DB.conn.execute(
            "SELECT id, site, author, post_id, post_url, post_time, action, symbol, symbol_name, price, quantity, raw_text "
            "FROM trades WHERE id=?", (_id,)).fetchone()
    elif kind == "news":
        row = DB.conn.execute("SELECT * FROM news WHERE id=?", (_id,)).fetchone()
    else:
        return {"error": "unknown kind"}
    return dict(row) if row else {"error": "not found"}


# ---------------- 统计明细列表（让每个统计数字可点、可见其构成） ----------------
# 设计要点（2026-09-23，回应「统计只是数字罗列、没有实际价值」）：
#   * total 采用与 /api/summary(DB.counts) 完全相同的 COUNT(*) 口径，
#     从而保证「卡片上的数字」与「点进来的明细条数」严格一致；
#   * SQL 全部是常量，仅 limit/offset 走参数绑定，不拼接任何外部输入（无注入面）；
#   * columns 交给前端做通用渲染，drill 描述「点这一行能下钻到哪」。
_DETAIL_SPECS: dict[str, dict] = {
    "raw_pages": {
        "label": "原始文档(页)", "table": "raw_pages",
        "sql": "SELECT id, site, url, fetched_at, http_status FROM raw_pages ORDER BY id DESC",
        "columns": [
            {"key": "id", "label": "编号", "num": True},
            {"key": "site", "label": "站点", "site": True},
            {"key": "url", "label": "URL"},
            {"key": "fetched_at", "label": "抓取时间"},
            {"key": "http_status", "label": "HTTP", "num": True},
        ],
    },
    "posts": {
        "label": "帖子", "table": "posts",
        "sql": "SELECT id, site, author, post_time, sentiment, text FROM posts "
               "ORDER BY post_time DESC, id DESC",
        "columns": [
            {"key": "post_time", "label": "时间"},
            {"key": "author", "label": "作者"},
            {"key": "site", "label": "站点", "site": True},
            {"key": "sentiment", "label": "情绪", "sentiment": True},
            {"key": "text", "label": "内容", "clip": 90},
        ],
        "drill": {"to": "post", "id_key": "id"},
    },
    "trades": {
        "label": "实盘交易", "table": "trades",
        "sql": "SELECT id, site, author, post_time, action, symbol, symbol_name, price, quantity, "
               "buy_time, buy_price, sell_time, sell_price, current_price, access_level "
               "FROM trades ORDER BY COALESCE(buy_time, post_time) DESC, id DESC",
        "columns": [
            {"key": "post_time", "label": "时间"},
            {"key": "author", "label": "选手"},
            {"key": "action", "label": "方向", "action": True},
            # 2026-10-06 王总：明细表里「代码 / 股票」两列只是纯文本，点不开个股详情。
            # 这里显式声明 stock / stock_name，前端 statCell 据此挂全站统一的 codeLink 入口。
            {"key": "symbol", "label": "代码", "code": True, "stock": True, "sym_key": "symbol"},
            {"key": "symbol_name", "label": "股票", "stock_name": True, "sym_key": "symbol"},
            {"key": "price", "label": "成交价", "num": True},
            {"key": "quantity", "label": "数量", "num": True},
            {"key": "access_level", "label": "权限", "access": True},
        ],
        "drill": {"to": "trade", "id_key": "id"},
    },
    "news": {
        "label": "资讯", "table": "news",
        "sql": "SELECT id, site, title, url, publish_time, source, summary FROM news "
               "ORDER BY publish_time DESC, id DESC",
        "columns": [
            {"key": "publish_time", "label": "时间"},
            {"key": "title", "label": "标题"},
            {"key": "source", "label": "来源"},
            {"key": "site", "label": "站点", "site": True},
            {"key": "summary", "label": "摘要", "clip": 80},
        ],
        "drill": {"to": "news", "id_key": "id"},
    },
    "players": {
        "label": "入选选手", "table": "players",
        "sql": "SELECT id, rank_position, name, channel, tier, platform, is_certified, "
               "followers, ranking_score, is_top_player FROM players "
               "ORDER BY rank_position ASC, id ASC",
        "columns": [
            {"key": "rank_position", "label": "名次", "num": True},
            {"key": "name", "label": "选手", "strong": True},
            {"key": "tier", "label": "层级", "tier": True},
            {"key": "platform", "label": "平台", "platform": True},
            {"key": "is_certified", "label": "认证", "cert": True},
            {"key": "followers", "label": "粉丝", "num": True},
            {"key": "ranking_score", "label": "评分", "num": True},
        ],
        "drill": {"to": "player", "name_key": "name"},
    },
    "positions": {
        "label": "持仓记录", "table": "positions",
        "sql": "SELECT id, site, player, channel, symbol, symbol_name, ratio, action, post_time, "
               "buy_time, buy_price, current_price, sell_time, sell_price, hold_since, access_level "
               "FROM positions ORDER BY player ASC, symbol ASC, id ASC",
        "columns": [
            {"key": "player", "label": "选手", "strong": True},
            # 2026-10-06 与 trades 同一口径：代码 / 股票两列可点开个股详情（见 statCell）。
            {"key": "symbol", "label": "代码", "code": True, "stock": True, "sym_key": "symbol"},
            {"key": "symbol_name", "label": "股票", "stock_name": True, "sym_key": "symbol"},
            {"key": "ratio", "label": "仓位%", "num": True},
            {"key": "action", "label": "动作", "action": True},
            {"key": "buy_time", "label": "建仓时间"},
            {"key": "buy_price", "label": "买入价", "num": True},
            {"key": "current_price", "label": "现价", "num": True},
            {"key": "access_level", "label": "权限", "access": True},
        ],
        "drill": {"to": "stock", "symbol_key": "symbol"},
    },
    "persons": {
        "label": "人物", "table": "persons",
        # 2026-09-26：与「相关人物」面板一致，去掉恒为 author 的 role 列（零信息量，用户已明确指出）。
        "sql": "SELECT id, name, mention_count FROM persons "
               "ORDER BY mention_count DESC, id ASC",
        "columns": [
            {"key": "name", "label": "名称", "strong": True},
            {"key": "mention_count", "label": "提及次数", "num": True},
        ],
        "drill": {"to": "person", "name_key": "name"},
    },
    "stock_mentions": {
        "label": "讨论股票", "table": "stock_mentions",
        "sql": "SELECT sm.id, sm.symbol, sm.symbol_name, sm.context, p.author, p.post_time, p.site "
               "FROM stock_mentions sm LEFT JOIN posts p ON p.id = sm.post_id_ref "
               "ORDER BY p.post_time DESC, sm.id DESC",
        "columns": [
            {"key": "post_time", "label": "时间"},
            {"key": "symbol", "label": "代码", "code": True, "stock": True, "sym_key": "symbol"},
            {"key": "symbol_name", "label": "股票", "strong": True, "stock_name": True, "sym_key": "symbol"},
            {"key": "author", "label": "提及者"},
            {"key": "context", "label": "语境", "clip": 80},
        ],
        "drill": {"to": "stock", "symbol_key": "symbol"},
    },
    "topics": {
        "label": "热点词", "table": "topics",
        "sql": "SELECT id, topic, weight, sample FROM topics ORDER BY weight DESC, id ASC",
        "columns": [
            {"key": "topic", "label": "热词", "strong": True},
            {"key": "weight", "label": "权重", "num": True},
            {"key": "sample", "label": "示例", "clip": 80},
        ],
    },
}


def api_detail_list(kind: str, limit: int = 500, offset: int = 0) -> dict:
    """返回某个统计项对应的明细记录（供统计卡片点击查看）。

    total 与 /api/summary 的 counts 采用同一 COUNT(*) 口径，保证数字与列表一致。
    """
    spec = _DETAIL_SPECS.get(kind)
    if not spec:
        return {"error": "unknown kind", "kind": kind,
                "available": sorted(_DETAIL_SPECS.keys())}
    limit = max(1, min(int(limit), 1000))
    offset = max(0, int(offset))
    table = spec["table"]  # 常量，非外部输入
    total = DB.conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]
    rows = DB.conn.execute(spec["sql"] + " LIMIT ? OFFSET ?", (limit, offset)).fetchall()
    return {
        "kind": kind, "label": spec["label"], "total": total,
        "returned": len(rows), "limit": limit, "offset": offset,
        "columns": spec["columns"], "drill": spec.get("drill"),
        "rows": [dict(r) for r in rows],
    }


# ---------------- HTTP 处理 ----------------
class _BadRequest(Exception):
    """GET 参数非法（如 ?limit=abc）。由 do_GET 统一转成 400 + JSON。"""


# 回环来源白名单：只认可本机地址，绝不放任何跨站来源
_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}

# 2026-10-21 防御纵深（可选）：设了 VIEWER_API_TOKEN 才启用本地令牌。
# 启用后，所有非 GET 写接口必须携带匹配的 `local_token` Cookie（由本服务在加载首页时下发，
# 浏览器同源自动附带），否则 401。默认不启用（空串）= 完全无感，不改变既有行为。
# 2026-10-22 P1-45：优先读环境变量；否则读 config 落盘的随机 token（desktop_app 启动生成并落盘）。
# 无 token（首次 / import 作为库 / 测试）则保持关闭，向后兼容非浏览器客户端。
_LOCAL_TOKEN = (os.environ.get("VIEWER_API_TOKEN") or "").strip()
if not _LOCAL_TOKEN:
    try:
        _tok_path = os.path.join(ROOT, "config", "viewer_token.txt")
        if os.path.exists(_tok_path):
            with open(_tok_path, encoding="utf-8") as _tf:
                _LOCAL_TOKEN = _tf.read().strip()
    except OSError:
        pass


def _json_safe(o):
    """递归把 NaN / +-Infinity 换成 None，保证产物是合法 JSON。

    2026-10-12 修复：行情/回测里 `0/0`、缺数据等会产生 float('nan')，json.dumps 默认
    会把它原样写成 `NaN`（非标准 JSON），前端 JSON.parse 直接抛异常 → 整块面板空白。
    在统一出口先清洗，再用 allow_nan=False 兜底。
    2026-10-13 加固：覆盖 numpy 标量（np.float64/np.int64 不是 python float/int 的直接子类，
    且 math.isfinite 对 numpy 标量会抛 TypeError），否则「默认股票池扫描」偶发 500。
    """
    if o is None or isinstance(o, bool) or isinstance(o, str):
        return o
    # numpy 等带 dtype 的标量：先转回 python 原生再递归清洗
    if hasattr(o, "dtype") and hasattr(o, "item"):
        try:
            return _json_safe(o.item())
        except Exception:
            return str(o)
    if isinstance(o, float):
        try:
            return o if math.isfinite(o) else None
        except (TypeError, ValueError):
            return None
    if isinstance(o, int):
        return o
    if isinstance(o, dict):
        return {k: _json_safe(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_json_safe(v) for v in o]
    return o  # 其余（datetime/Decimal/bytes 等）交给 json.dumps 的 default=str


def _host_ok(host_header: str) -> bool:
    """Host 头必须是回环地址（防 DNS rebinding：外部域名解析到 127.0.0.1 时浏览器会带上外部 Host）。"""
    h = (host_header or "").strip().lower()
    if not h:
        return False
    if h.startswith("["):                      # IPv6 字面量：[::1]:8765
        i = h.find("]")
        if i < 0:
            return False
        return h[1:i] in _LOOPBACK_HOSTS
    host = h.rsplit(":", 1)[0] if ":" in h else h
    return host in _LOOPBACK_HOSTS


def _origin_ok(origin: str) -> bool:
    """Origin 必须是回环来源（http(s)://127.0.0.1[:port] / localhost[:port]）。"""
    o = (origin or "").strip()
    if not o:
        return False
    try:
        u = urllib.parse.urlparse(o)
    except ValueError:
        return False
    return u.scheme in ("http", "https") and (u.hostname or "").lower() in _LOOPBACK_HOSTS


def _parse_cookie(header: str) -> dict:
    """解析 Cookie 头为 {name: value}（P1-45：不再用子串匹配）。"""
    out = {}
    for part in (header or "").split(";"):
        part = part.strip()
        if "=" in part:
            k, v = part.split("=", 1)
            out[k.strip()] = v.strip()
    return out


def _token_ok(handler) -> bool:
    """P1-45：用 hmac.compare_digest 恒定时间比较 local_token Cookie，避免子串绕过。"
    未设 token（_LOCAL_TOKEN 为空）时一律放行，向后兼容。"""
    if not _LOCAL_TOKEN:
        return True
    ck = _parse_cookie(handler.headers.get("Cookie") or "")
    return hmac.compare_digest(ck.get("local_token", ""), _LOCAL_TOKEN)


# ---------------- 后台自动抓取调度器（白天交易时段定时刷新） ----------------
# 王总需求：白天 9:00-16:00（工作日）每 10-20 分钟自动 fetch taoguba + eastmoney 并入库真实数据。
# 实现：一个 daemon 线程，每 ~10s 唤醒一次做条件检查（粒度比 60s 更灵敏，验收要求 5-10s 内触发）：
#   · 调度开关开启（可由 /api/scheduler/toggle 关闭，持久化到 meta 表）
#   · 当前为工作日且处于 9:00-16:00 交易窗口
#   · 距上次成功调度 fetch ≥ 10 分钟（防抖，避免扎堆）
# 满足则调用 INGEST.ingest 抓取两个来源；全程 catch 异常，绝不拖垮 server 进程。
# 状态（enabled / last_run_at / run_count / errors_today）持久化到 meta 表 key=scheduler_state。

_SCHEDULER_STATE_KEY = "scheduler_state"
_SCHEDULER_STARTED = False
_SCHEDULER_LOCK = threading.Lock()       # 保护 _SCHEDULER_STARTED 防重入
_SCHED_START_MIN = 9 * 60                # 交易窗口起点 09:00
_SCHED_END_MIN = 16 * 60                 # 交易窗口终点 16:00
_SCHED_MIN_GAP = 600                     # 两次调度最小间隔 10 分钟（防抖）
_SCHED_WAKE = 10                         # 唤醒粒度（秒）；比 60s 更灵敏，验收 5-10s 内触发可达
_SCHED_SOURCES = ["taoguba", "eastmoney", "sina"]  # 仅抓国内舆情，不碰海外


def _sched_default_state() -> dict:
    return {"enabled": True, "last_run_at": None,
            "run_count": 0, "errors_today": 0, "errors_date": ""}


def _sched_load_state() -> dict:
    try:
        raw = DB.get_meta(_SCHEDULER_STATE_KEY)
        if raw:
            d = json.loads(raw)
            if isinstance(d, dict):
                base = _sched_default_state()
                base.update(d)
                return base
    except Exception:  # noqa: BLE001
        logging.exception("读取 scheduler_state 失败，退回默认")
    return _sched_default_state()


def _sched_save_state(state: dict) -> None:
    try:
        DB.set_meta(_SCHEDULER_STATE_KEY, json.dumps(state, ensure_ascii=False))
    except Exception:  # noqa: BLE001
        logging.exception("持久化 scheduler_state 失败")


def _sched_in_window(now: datetime) -> bool:
    """工作日（周一~周五）且处于 9:00-16:00。节假日暂未纳入，按工作日处理。"""
    if now.weekday() >= 5:          # 5=周六 6=周日
        return False
    m = now.hour * 60 + now.minute
    return _SCHED_START_MIN <= m < _SCHED_END_MIN


def _sched_next_window(now: datetime) -> datetime:
    """下一个交易窗口起点（今天 9:00 若未过，否则下一个工作日的 9:00）。"""
    cand = now.replace(hour=9, minute=0, second=0, microsecond=0)
    if cand <= now:
        cand = cand + timedelta(days=1)
    while cand.weekday() >= 5:       # 跳过周末
        cand = cand + timedelta(days=1)
    return cand


def _sched_run_once(state: dict) -> None:
    """执行一次自动抓取（两个来源）。用 INGEST_LOCK 与手动 /api/fetch 互斥。"""
    if not INGEST_LOCK.acquire(blocking=False):
        # 手动抓取进行中，本 tick 跳过，下个窗口再来（非阻塞，绝不死锁）。
        logging.info("调度抓取跳过：手动 /api/fetch 占用中")
        return
    try:
        errors = 0
        total_items = 0
        # 2026-09-25 优化：两来源并发抓取（DB 已 WAL + 每线程独立连接），缩短整轮耗时。
        max_workers = min(len(_SCHED_SOURCES), int(os.environ.get("INGEST_MAX_WORKERS", "4")))
        with cf.ThreadPoolExecutor(max_workers=max_workers) as ex:
            futs = {ex.submit(INGEST.ingest, s, "all"): s for s in _SCHED_SOURCES}
            for fut in cf.as_completed(futs):
                s = futs[fut]
                try:
                    res = fut.result()
                    n = _ingest_item_count(res)
                    total_items += n
                    DB.record_refresh(s, "ok", n, SITE_DISPLAY_NAMES.get(s, s))
                except Exception as e:  # noqa: BLE001 —— 单来源失败不影响另一个来源
                    errors += 1
                    err = str(e)[:200]
                    logging.exception("调度抓取 %s 失败", s)
                    DB.record_refresh(s, "error", 0, SITE_DISPLAY_NAMES.get(s, s), error=err)
        now = datetime.now()
        # 跨天则重置当日错误计数
        today = now.strftime("%Y-%m-%d")
        if state.get("errors_date") != today:
            state["errors_date"] = today
            state["errors_today"] = 0
        state["last_run_at"] = now.strftime("%Y-%m-%d %H:%M:%S")
        state["run_count"] = int(state.get("run_count", 0)) + 1
        state["errors_today"] = int(state.get("errors_today", 0)) + errors
        _sched_save_state(state)
        # 2026-10-05（报告 P1 / 建议3）统一口径：整轮抓取结束后把「__all__」也记一笔。
        # 旧行为里 __all__ 只有手动 /api/fetch 全成功才更新，调度每轮跑完不回写，
        # 于是顶栏拿一个十几分钟前的 last_ok_at 去比，长期误报「已超时」。
        # 这里按**至少一个来源成功**即记 ok（部分来源失败只是降级，不是数据停更）。
        _sched_record_all(errors, len(_SCHED_SOURCES), total_items)
        logging.info("调度抓取完成：sources=%s errors=%d run_count=%d",
                     _SCHED_SOURCES, errors, state["run_count"])
        # 方案 C 第 5 步：主抓完顺带长一次选股样本（内部 6 小时节流 + 全异常兜底）。
        _sched_maybe_extref()
    finally:
        INGEST_LOCK.release()


_EXTREF_STATE_KEY = "extref_collect_state"
_EXTREF_MIN_GAP_H = 6          # 外部榜单采集最小间隔（小时）：帖子每 10 分钟一轮，选股样本没必要跟这么紧
_EXTREF_SRC_KEY = "em-guba-rank"   # 唯一能自动跑的采集器（雪球有 WAF、淘股吧/龙虎榜待接入）


def _sched_extref_state() -> dict:
    try:
        raw = DB.get_meta(_EXTREF_STATE_KEY)
        if raw:
            d = json.loads(raw)
            if isinstance(d, dict):
                return d
    except Exception:  # noqa: BLE001
        logging.exception("读取 extref 采集状态失败，退回默认")
    return {"last_at": None}


def _sched_maybe_extref() -> None:
    """交易时段顺带跑一次外部榜单采集，并自动回填 T+1/5/10。

    王总方案 C 第 5 步：验证报告要能自己长出价值，样本不能指望手动点「继续采集」。
    挂在现有调度后面做兜底，6 小时节流一次；全程 try/except，失败只记日志，
    绝不拖垮主抓取调度（主抓失败照样照常推进）。
    """
    st = _sched_extref_state()
    last = st.get("last_at")
    if last:
        try:
            if (datetime.now() - datetime.strptime(str(last)[:19], "%Y-%m-%d %H:%M:%S")
                    ).total_seconds() < _EXTREF_MIN_GAP_H * 3600:
                return
        except Exception:  # noqa: BLE001
            pass                      # 时间解析不了就放行，让采集跑一次
    try:
        with _WRITE_LOCK:
            extref_store.ensure_schema(DB.conn)
            res = extref_collect.collect(DB.conn, src_key=_EXTREF_SRC_KEY, page_size=20)
            try:
                extref_backtest.backtest_all(DB.conn)   # 采集完顺手回填，样本才不断长
            except Exception:  # noqa: BLE001
                logging.exception("调度补回填失败")
        DB.set_meta(_EXTREF_STATE_KEY, json.dumps(
            {"last_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
             "res": str(res)[:200]}, ensure_ascii=False))
        logging.info("调度 extref 采集完成：%s", res)
    except Exception:  # noqa: BLE001
        logging.exception("调度 extref 采集失败")


def _sched_record_all(errors: int, total_sources: int, total_items: int = 0) -> None:
    """整轮抓取收尾时把「__all__」这一行也更新掉（报告 建议3 / P1）。

    口径：**至少一个来源成功 = ok**。理由见问题清单 P1——用户看的是「数据还在不还在
    更新」，不是「三个源是不是全绿」；东财 push2 挂了但淘股吧在跑，数据照样新鲜。
    全部来源都失败才记 error（不动 last_ok_at，保留最后一次成功时刻）。
    """
    try:
        if errors >= max(1, total_sources):
            DB.record_refresh("__all__", "error", 0, "全部来源",
                              error=f"全部 {total_sources} 个来源均失败（连续第 {errors} 次）")
            logging.warning("调度整轮全部失败，__all__ 记为 error（last_ok_at 保持上次成功时刻）")
            return
        DB.record_refresh("__all__", "ok", total_items, "全部来源")
        logging.info("调度整轮完成：%d/%d 成功，__all__ 已回写 last_ok_at（items=%d）",
                     total_sources - errors, total_sources, total_items)
    except Exception:  # noqa: BLE001 —— 状态回写失败不能连累调度本身
        logging.exception("回写 __all__ 刷新状态失败")


def _sched_maybe_run() -> None:
    state = _sched_load_state()
    if not state.get("enabled", True):
        return
    now = datetime.now()
    if not _sched_in_window(now):
        return
    last = _parse_dt_loose(state.get("last_run_at"))
    if last is not None and (now - last).total_seconds() < _SCHED_MIN_GAP:
        return                        # 防抖：距上次不足 10 分钟
    _sched_run_once(state)


# ---------------------------------------------------------------------------
# 全市场日K缓存预热（2026-10-06 提速）：每个交易日 15:35 后后台全量抓日K落盘，
# 让用户当晚及次日开盘前点开「全市场榜 / 阴跌扫描」直接读盘秒出。
# 复用 screener._load_bars_map 全量逻辑（非增量拼接，避免前复权在除权日重算历史导致价格错位）。
_KLINE_PREWARM_KEY = "kline_prewarm_state"


def _kline_prewarm_maybe() -> None:
    """交易日 15:35 后触发一次全市场日K预热；用 last_prewarm_date 防重复；后台线程执行。"""
    try:
        now = datetime.now()
        if now.weekday() >= 5:          # 周末不抓（周五已预热好，覆盖周一开盘前）
            return
        if now.hour < 15 or (now.hour == 15 and now.minute < 35):
            return
        today = now.strftime("%Y-%m-%d")
        st: dict = {}
        try:
            raw = DB.get_meta(_KLINE_PREWARM_KEY)
            if raw:
                d = json.loads(raw)
                if isinstance(d, dict):
                    st = d
        except Exception:  # noqa: BLE001
            pass
        if st.get("last_prewarm_date") == today:
            return
        st["last_prewarm_date"] = today
        st["last_prewarm_at"] = now.strftime("%Y-%m-%d %H:%M:%S")
        try:
            DB.set_meta(_KLINE_PREWARM_KEY, json.dumps(st, ensure_ascii=False))
        except Exception:  # noqa: BLE001
            pass
        # 2026-10-15：改子进程执行（实测 165s 纯 Python，在 HTTP 进程里跑会冻住所有接口）
        _spawn_offproc("kline_prewarm")
        logging.info("kline prewarm scheduled for %s", today)
    except Exception:  # noqa: BLE001
        logging.exception("kline prewarm 调度异常（已忽略）")


# ------------------------------------------------ 博主荐股周期收益自动回算（2026-10-10）
# 修「8 月荐股的 3/5/10 日收益永远是 —」：旧链路只在手动 force=1 时才算，新导入/走完窗口的
# 记录永不回填。现挂进调度循环：每小时至多一轮、后台线程执行（写锁纪律：HTTP 与调度线程
# 都不做网络 I/O），compute_all 自带增量口径（只算缺失行 + 任一窗口仍为 NULL 的行）。
_BLOGGER_PERF_STATE = {"running": False, "last_at": 0.0}
_BLOGGER_PERF_GAP = 6 * 3600      # 2026-10-15：1h → 6h（该回算纯 Python 且耗 46s，见 _blogger_perf_maybe）


def _blogger_perf_maybe() -> None:
    """博主荐股 T+1/3/5/10 回算（**纯 Python**，实测单次 46s）。

    2026-10-15 修复（"点个股弹出来特别慢"的第二个成因）：
      · 原口径「每 1 小时一次 + last_at 只存内存」→ ① 每小时冻住全站 ~46s；
        ② 每次重启进程后 last_at 归零 → **重启后立刻再冻一次**（实测 23:14:52 跑了 46.5s）。
      · 它的输入（博主持仓/日 K）一天只更新一次，小时级重算没有信息增益。
      · 现改为：① 只在 15:00 之后跑；② 上次跑过的时刻**落 meta**（跨重启有效）；
        ③ 最小间隔 6 小时 → 每天最多 2 次，且不再"重启即重算"。
        失败不写 meta，下轮自动重试（保持"失败不算已处理"的纪律）。
    """
    if _BLOGGER_PERF_STATE["running"]:
        return
    if datetime.now().hour < 15:          # 收盘前不跑（数据还没稳定）
        return
    last = _parse_dt_loose(DB.get_meta("blogger_perf_last") or "")
    if last is not None and (datetime.now() - last).total_seconds() < _BLOGGER_PERF_GAP:
        return
    # 2026-10-15：改为**子进程**执行（46s 纯 Python，在 HTTP 进程里跑会冻住所有接口）。
    # 「已完成」标记由 _spawn_offproc 在观察到子进程退出码 0 后落库（失败不落 → 下轮重试）。
    _BLOGGER_PERF_STATE["running"] = True
    try:
        _spawn_offproc("blogger_perf")
    finally:
        _BLOGGER_PERF_STATE["running"] = False


# ============ 2026-10-15 全站回测评测修复（性能）：读接口短期 TTL 缓存 ============
# 背景：若干重计算读接口每次请求都全量重算，实测 /api/screener_lowbox 53s、/api/smart_money 17s、
# /api/persons 6s、/api/lhb 5s、/api/players 3.3s —— 用户在面板间来回切换会反复卡住。
# 底层数据是「每日一次」更新（盘后落库），同一分钟内重复计算没有信息增益。
# 口径：① 仅下面白名单读接口参与；② 请求带 `t=` 或 `force=` 时绕过缓存（前端「立即刷新」走这条，
# 保证强制刷新一定能拿到最新）；③ 缓存的是接口**最终返回对象**，写入后不再被改动（纯幂等读）。
_TTL_CACHE: dict = {}
# hotspots 启动预热的延迟秒数（晚于首屏，避免与落地页请求争 GIL）。
_HOTSPOTS_PREWARM_DELAY = 20
_TTL_ROUTES: dict = {
    "/api/screener_lowbox": 300,
    "/api/screener": 300,
    "/api/screener_turnover_top": 300,
    "/api/lowbox_breakouts": 300,
    "/api/backtest": 300,
    "/api/blogger/perf": 300,
    "/api/blogger/reco": 300,
    # 2026-10-20：抖音语料是「每日一次」覆盖写，重复请求同一筛选组合没有信息增益。
    # detail 不进白名单（点开单条要最新全文）。缓存 key 含完整 query，故不同筛选各存一份。
    "/api/douyin/facets": 300,
    "/api/douyin/list": 300,
    "/api/douyin/overview": 300,

    "/api/douyin/topics": 300,

    "/api/douyin/stances": 300,

    "/api/douyin/bloggers": 300,

    "/api/douyin/plate": 300,
    "/api/persons": 300,
    "/api/players": 300,
    "/api/smart_money": 180,
    "/api/niusan": 180,
    "/api/consensus": 180,
    "/api/cohold_recent": 180,
    "/api/operating": 180,
    "/api/lhb": 120,
    # 2026-10-17 性能：补三个新增重计算读接口（实测 /api/quant 5962ms、/api/etf 3870ms、
    # /api/watch_list 4702ms 每次请求都全量重算，热态与冷态几乎无差 → 缓存未命中）。
    # 底层数据每日盘后落库，短期 TTL 即可消除重复计算，且前端「立即刷新」带 force= 会绕过。
    "/api/quant": 300,
    "/api/etf": 300,
    "/api/watch_list": 60,
    # 2026-10-15：hotspots 是纯 Python 热词聚合，实测首次要 40 秒（且持 GIL 会冻住其他 API），
    # 已从前端首屏 loadAll 挪为「进入面板才拉」。这里加 600s 缓存 + 启动预热，让用户点进去即命中。
    "/api/hotspots": 600,
    # 注意：**不缓存 /api/dashboard** —— 它含集合竞价 / 实时报价等「当下」语义，
    # 且前端存在不带 `t=` 的裸调用刷新路径（selectNav 与手动刷新都会打同一 URL），
    # 缓存会挡住刷新、让用户看到几十秒内的旧价。实时类接口一律不进白名单。
}


def _kline_prewarm_run() -> None:
    """后台真正抓全市场日K并落盘 decline_kcache.json。"""
    try:
        pool = screener._market_pool()
        if not pool:
            logging.warning("kline prewarm: 全市场清单获取失败，跳过")
            return
        t0 = time.time()
        _bars, meta = screener._load_bars_map(pool, 30)
        dt = time.time() - t0
        logging.info("kline prewarm done: pool=%d cached=%d fetched=%d fail=%d %ds",
                     meta.get("n_pool", 0), meta.get("n_cached", 0),
                     meta.get("n_fetched", 0), meta.get("n_fail", 0), int(dt))
    except Exception:  # noqa: BLE001
        logging.exception("kline prewarm 执行异常")


def _ext_refresh_maybe() -> None:
    """P0-3（2026-10-15）：把 geo / extref / public_records 纳入内部调度器**每日离线重导**。

    带 24h 冷却（meta 键 ext_refresh_last 记录最后刷新时刻）；每域独立 try/except 异常隔离，
    单域失败不影响其它域与主进程。说明：这三个域均为**离线种子重导**（联网调研是离线步骤、
    沉淀进 seed 后由本函数落库），因此「纳入每日刷新」意味着——seed 一旦被更新，24h 内自动入库。
    「数据保鲜角标」用数据时间列计算，故重导不会把角标刷成假新鲜。
    海外舆情(overseas) 不在此自动重导：其采集需联网，且 collect(reset=True) 会在抓取失败时清空
    新闻表，从调度器自动调用风险过高；保持由用户手动「刷新海外舆情(联网)」触发。
    """
    now = datetime.now()
    last = (DB.get_meta("ext_refresh_last") or "").strip()
    if last:
        try:
            prev = _parse_dt_loose(last)
            if prev is not None and (now - prev).total_seconds() < 24 * 3600:
                return
        except Exception:
            pass
    # geo：离线重导 seed_events.json（幂等 upsert）
    try:
        geo_store.ensure_schema(DB.conn)
        geo_store.import_seed(DB.conn)
    except Exception:
        logging.exception("调度-geo 刷新失败（已忽略）")
    # extref：离线重导 seed_sources/seed_signals（幂等）
    try:
        extref_store.ensure_schema(DB.conn)
        extref_store.import_sources(DB.conn)
        extref_store.import_signals(DB.conn)
    except Exception:
        logging.exception("调度-extref 刷新失败（已忽略）")
    # public_records：离线重导 public_records_seed.json
    try:
        import viewer.person_score as _ps
        _ps.load_public_records(DB.conn)
    except Exception:
        logging.exception("调度-public_records 刷新失败（已忽略）")
    try:
        DB.set_meta("ext_refresh_last", now.strftime("%Y-%m-%d %H:%M:%S"))
    except Exception:
        pass


def _overseas_autorefresh_maybe() -> None:
    """海外舆情安全自动刷新（2026-10-20）：24h 冷却；联网通才更新，不通保留旧数据。

    解决「手动按钮单点」问题——之前海外舆情必须人工点「刷新海外舆情(联网)」，
    电脑关机几天后无人点就一直陈旧。现纳入内部调度器：每天自动尝试一次，
    且调用 collect_autosafe（抓不到就保留旧数据，绝不越刷越空）。
    VPN 未通时仅返回 skipped，面板角标仍可手动点触发重试。
    """
    now = datetime.now()
    last = (DB.get_meta("overseas_auto_last") or "").strip()
    if last:
        try:
            prev = _parse_dt_loose(last)
            if prev is not None and (now - prev).total_seconds() < 24 * 3600:
                return
        except Exception:
            pass
    try:
        from overseas.collect import collect_autosafe
        out = collect_autosafe(DB.conn)
        if out.get("skipped"):
            logging.info("调度-overseas 自动刷新跳过(联网未通,保留旧数据)")
        else:
            logging.info("调度-overseas 自动刷新完成")
    except Exception:
        logging.exception("调度-overseas 自动刷新失败(忽略)")
    try:
        DB.set_meta("overseas_auto_last", now.strftime("%Y-%m-%d %H:%M:%S"))
    except Exception:
        pass


def _rotate_backups(keep: int = 7) -> None:
    """P1-4 / P3-1（2026-10-15）：备份轮转——viewer/backups 快照与 viewer/app.db.bak-* 各保留最新 keep 份。"""
    import glob as _glob
    import re as _re
    base = os.path.join(ROOT, "viewer")
    # 1) 快照目录（前缀 app.db.*-YYYYmmdd-HHMMSS）
    bdir = os.path.join(base, "backups")
    if os.path.isdir(bdir):
        snaps = [os.path.join(bdir, fn) for fn in os.listdir(bdir)
                 if os.path.isfile(os.path.join(bdir, fn)) and _re.search(r"-\d{8}-\d{6}", fn)]
        snaps.sort(key=os.path.getmtime, reverse=True)
        for fp in snaps[keep:]:
            try:
                os.remove(fp)
            except OSError:
                pass
    # 2) 根目录 app.db.bak-*（保留最新 keep 份，含 -shm/-wal 伴随文件）
    for fp in sorted(_glob.glob(os.path.join(base, "app.db.bak-*")), key=os.path.getmtime, reverse=True)[keep:]:
        try:
            for ext in ("", "-shm", "-wal"):
                if os.path.exists(fp + ext):
                    os.remove(fp + ext)
        except OSError:
            pass


def _db_housekeep_maybe() -> None:
    """P1-4（2026-10-15）：DB 容量治理 + 备份轮转。

    每日一次（24h 冷却，meta 键 db_housekeep_last）：
      · board_flow_samples / auction_snapshot 按时间列保留近 90 天热数据，更旧 DELETE（防膨胀）；
      · stock_mentions 无时间列，无法按龄安全清理，跳过（schema 需加 created_at 后补 DELETE）；
      · PRAGMA wal_checkpoint(TRUNCATE) 回收 WAL 占用；
      · viewer/backups 与 viewer/app.db.bak-* 各保留最新 7 份（轮转删除更旧的）。
    全程异常隔离，任何一步失败不影响其它步骤与主进程。
    """
    now = datetime.now()
    last = (DB.get_meta("db_housekeep_last") or "").strip()
    if last:
        try:
            prev = _parse_dt_loose(last)
            if prev is not None and (now - prev).total_seconds() < 24 * 3600:
                return
        except Exception:
            pass
    edge = (now - timedelta(days=90)).strftime("%Y-%m-%d")
    deleted: dict = {}
    try:
        cur = DB.conn.execute("DELETE FROM board_flow_samples WHERE trade_date < ?", (edge,))
        deleted["board_flow_samples"] = cur.rowcount
        DB.conn.commit()
    except Exception:
        logging.exception("治理-board_flow_samples 失败（已忽略）")
    try:
        cur = DB.conn.execute("DELETE FROM auction_snapshot WHERE date < ?", (edge,))
        deleted["auction_snapshot"] = cur.rowcount
        DB.conn.commit()
    except Exception:
        logging.exception("治理-auction_snapshot 失败（已忽略）")
    try:
        DB.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    except Exception:
        pass
    try:
        _rotate_backups(keep=7)
    except Exception:
        logging.exception("备份轮转失败（已忽略）")
    logging.info("DB 容量治理完成：%s", deleted)
    try:
        DB.set_meta("db_housekeep_last", now.strftime("%Y-%m-%d %H:%M:%S"))
    except Exception:
        pass


# ── 调度任务计时（2026-10-15）─────────────────────────────────────────────
# 背景：进哥反馈"点个股弹出来特别慢"。实测发现真凶不是个股接口，而是
# **整个 API 层被间歇性冻结**（缓存命中、正常只要 0.03s 的接口，一度连续两次各卡满 30s），
# 这是 Python 单进程的固有特性：任何一个纯 Python 任务长时间占住 GIL，
# 同进程的其它所有请求线程都得等 —— I/O 任务不会，纯计算任务会。
# 原实现是一长串裸调用，冻住了也看不出是谁干的。现在改成清单 + 逐任务计时：
#   · 单任务 ≥ _SCHED_SLOW_SEC → 记 WARNING 点名；
#   · 整轮 ≥ _SCHED_SLOW_TICK_SEC（多个中等任务叠加同样会冻接口）→ 记整轮明细。
# 名字同时通过 /api/version 的 sched_task 字段暴露，便于事发当场诊断。
_SCHED_SLOW_SEC = 1.0
_SCHED_SLOW_TICK_SEC = 3.0
_SCHED_CURRENT = "-"

# ── 重活出进程（2026-10-15，根治「点个股弹出来特别慢」）────────────────────────
# 为什么：下面这些任务都是**纯 Python** 长任务 —— 实测 predictor_daily 79.8s、
# pillars_daily 22.4s、blogger_perf 46s、hotspots 138s、全市场日K 165s。
# 在 HTTP 进程里跑，它们必然长时间占住 GIL，把同进程**所有**接口一起冻住
# （实测：缓存命中、本该 0.03s 的接口变成 1.5~15s，并间歇冻结 30s+）。
# 注意：I/O 型任务（网络/文件）不占 GIL，纯计算型才占 —— 所以只挪纯计算型。
# 做法：交给 tools/sched_job.py 的子进程跑（子进程复用原函数，节流/幂等逻辑一行不改），
# 父进程只负责「起进程 + 记日志 + 观察退出码」，不再参与计算。
_OFFPROC_GAP = {          # 同一任务两次启动的最小间隔（秒）：防每 tick 反复起进程
    "predictor_daily": 600, "pillars_daily": 600, "blogger_perf": 900,
    "resonance_build": 900, "selfpick_build": 900, "evolve_daily": 900,
    "bili_scan": 900, "kline_prewarm": 600, "hotspots": 600,
    "lhb_daily": 600, "blogger_daily": 600,   # 2026-10-19：夜刷编排同样出进程（实测 blogger_daily 单轮 525.5s 占 GIL 冻结全站）
    "intraday_snap": 3600,                    # 2026-10-21 dy9：盘中快照（窗口内每天一次，3600s 兜底防抖）
}
_JOB_META = {"blogger_perf": "blogger_perf_last"}   # 子进程成功退出后由父进程落盘的"已完成"标记
_JOB_PROCS: dict = {}
_JOB_LAST: dict = {}
_JOB_WARNED: set = set()   # 已告警过"上次失败"的任务（成功后清出，避免每 tick 刷屏）


def _spawn_offproc(job: str, logname: str | None = None) -> bool:
    """把某个重活交给子进程执行；同一任务同一时刻只允许一个子进程在跑。"""
    try:
        prev = _JOB_PROCS.get(job)
        if prev is not None:
            if prev.poll() is None:
                return False                      # 上一轮还没跑完 → 跳过（天然防叠进程）
            if prev.returncode:
                if job not in _JOB_WARNED:      # 同一次失败只告警一次，别每 10s 刷屏
                    logging.warning("[sched] 子进程 %s 上次退出码 %s（详见 viewer/logs/sched-%s.log）",
                                    job, prev.returncode, job)
                    _JOB_WARNED.add(job)
            else:
                _JOB_WARNED.discard(job)
                mk = _JOB_META.get(job)
                if mk:
                    try:
                        DB.set_meta(mk, datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
                    except Exception:  # noqa: BLE001
                        pass
        now = time.time()
        if _JOB_LAST.get(job) and now - _JOB_LAST[job] < _OFFPROC_GAP.get(job, 600):
            return False
        log_dir = os.path.join(ROOT, "viewer", "logs")
        os.makedirs(log_dir, exist_ok=True)
        fh = open(os.path.join(log_dir, "sched-%s.log" % (logname or job)), "a",
                  encoding="utf-8", errors="replace")
        fh.write("\n=== %s %s ===\n" % (job, time.strftime("%Y-%m-%d %H:%M:%S")))
        fh.flush()
        _JOB_WARNED.discard(job)
        _JOB_PROCS[job] = subprocess.Popen(
            [sys.executable, os.path.join(ROOT, "tools", "sched_job.py"), job],
            cwd=str(ROOT), stdout=fh, stderr=fh, stdin=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        _JOB_LAST[job] = now
        return True
    except Exception:  # noqa: BLE001
        logging.exception("[sched] 子进程 %s 启动失败（本轮跳过，下轮重试）", job)
        return False


_INTRADAY_KEY = "intraday_snap_state"


def _intraday_snap_maybe() -> None:
    """盘中快照（2026-10-21 dy9）：交易日 14:25~14:45 抓一次，每天最多一次。

    ★ 为什么必须卡在这个窗口（不是随便定个时间）：
      · 早了信号不全 —— 博主发帖/战法命中仍在累积，抓到的是一份残缺快照；
      · 晚了就退化成收盘价 —— 与「盘后重建」没有区别，失去对照价值。
      抓取产物落 intraday_snap 表（tools/intraday_snap.py），
      攒够 5 个交易日后才能回测「盘中价买入 vs 收盘价买入 vs 次日买入」，
      再用真实数据决定是否把 resonance_build 挪到盘中。**现在不改调度**。
    """
    try:
        now = datetime.now()
        m = now.hour * 60 + now.minute
        if not (14 * 60 + 25 <= m <= 14 * 60 + 45):
            return
        today = now.strftime("%Y-%m-%d")
        raw = DB.get_meta(_INTRADAY_KEY)
        if raw:
            try:
                st = json.loads(raw)
            except Exception:  # noqa: BLE001
                st = {}
            if st.get("date") == today:
                return                                  # 今天已抓过
        if _spawn_offproc("intraday_snap"):
            DB.set_meta(_INTRADAY_KEY, json.dumps(
                {"date": today, "at": now.strftime("%H:%M:%S")}, ensure_ascii=False))
    except Exception:  # noqa: BLE001
        logging.exception("[sched] intraday_snap 判定失败（本轮跳过，下轮重试）")


def _sched_tasks() -> list:
    """本轮调度任务清单：(名字, 无参可调用)。

    ⚠️ 顺序与原实现逐条一致，不得随意调整（任务之间有隐含的时间窗/冷却先后关系）。
    """
    return [
        ("sched_maybe_run", _sched_maybe_run),
        ("kline_prewarm", _kline_prewarm_maybe),
        ("board_flow_sample", lambda: board_flow.maybe_sample(DB.conn)),
        ("predictor_daily", lambda: _spawn_offproc("predictor_daily")),
        ("pillars_daily", lambda: _spawn_offproc("pillars_daily")),
        ("pillars_b_confirm", lambda: pillars.maybe_b_confirm_wrapper(DB.conn)),
        ("pillars_monday_refresh", _monday_refresh_maybe),
        ("policydesk_refresh", lambda: policydesk.maybe_refresh(DB.conn)),
        ("watchdesk_heartbeat", _watchdesk_heartbeat),
        ("watchdesk_run", _watchdesk_maybe_run),
        ("auction_snapshot", lambda: auction.maybe_snapshot(DB.conn)),
        ("lhb_daily", lambda: _spawn_offproc("lhb_daily")),
        ("blogger_daily", lambda: _spawn_offproc("blogger_daily")),
        ("blogger_perf", _blogger_perf_maybe),
        ("bili_scan", lambda: _spawn_offproc("bili_scan")),
        ("intraday_snap", _intraday_snap_maybe),          # dy9：14:25~14:45 盘中快照
        ("resonance_build", lambda: _spawn_offproc("resonance_build")),
        ("selfpick_build", lambda: _spawn_offproc("selfpick_build")),
        ("evolve_daily", lambda: _spawn_offproc("evolve_daily")),
        ("ext_refresh", _ext_refresh_maybe),
        ("overseas_autorefresh", _overseas_autorefresh_maybe),
        ("db_housekeep", _db_housekeep_maybe),
        ("etf_refresh", lambda: etf.maybe_refresh()),
        ("etf_klines", lambda: etf.maybe_refresh_klines()),  # A1 修复：ETF 日 K 进 kcache，随查看器自动保鲜
        ("global_market", lambda: global_market.maybe_refresh()),  # 2026-10-17：全球大盘随查看器自动保鲜（30 分钟冷却）
        ("dragon_tier", lambda: (dragon_mod.maybe_build() if dragon_mod else None)),  # 2026-10-21 龙头分级每日保鲜（24h TTL）
    ]


def _monday_refresh_maybe() -> None:
    """周一 16:30 后全量刷一次基本面 —— 每周只此一次。

    ⚠️ 2026-10-15 重大修复（这是「点个股弹出来特别慢」的真凶）：
    原实现把 pillars.monday_full_refresh 直接挂在调度循环里，每 ~10s 被调一次；
    而该函数**自身没有任何节流**（注释写"自带幂等"，实际是无条件执行
    SS.universe + fundamentals_refresh(full=True) —— 对 159 只票逐只联网抓 F10）。
    实测后果：
      · 全站每个接口被拖慢 1.5~2s（缓存命中、本该 0.03s 的接口也一样），并间歇冻结 30s+；
      · 它与其它写库任务抢 sqlite 写锁 → blogger_perf / extref 回测反复 "database is locked"。
    修法：加「只在周一 16:30 之后 + 当日 meta 标记只跑一次」两道闸。
    """
    try:
        now = datetime.now()
        if now.weekday() != 0:                       # 只在周一
            return
        if (now.hour, now.minute) < (16, 30):
            return
        day = now.strftime("%Y-%m-%d")
        if (DB.get_meta("pillars_monday_refresh_done") or "").strip() == day:
            return
        logging.info("周度基本面全量刷新开始（%s）", day)
        res = pillars.monday_full_refresh(DB.conn)
        DB.set_meta("pillars_monday_refresh_done", day)
        logging.info("周度基本面全量刷新完成：%s", str(res)[:200])
    except Exception:  # noqa: BLE001
        logging.exception("周度基本面全量刷新失败（已忽略，下周一再试）")


def _scheduler_loop() -> None:
    global _SCHED_CURRENT
    logging.info("scheduler thread running")
    while True:
        try:
            time.sleep(_SCHED_WAKE)   # 每 ~10s 唤醒检查一次
            tick0 = time.time()
            slow: list = []
            for name, fn in _sched_tasks():
                _SCHED_CURRENT = name
                t0 = time.time()
                try:
                    fn()
                except Exception:  # noqa: BLE001 —— 单个任务异常不能连累本轮其它任务
                    logging.exception("调度任务 %s 异常（已忽略，继续运行）", name)
                dt = time.time() - t0
                if dt >= _SCHED_SLOW_SEC:
                    slow.append("%s=%.1fs" % (name, dt))
            _SCHED_CURRENT = "-"
            tick_dt = time.time() - tick0
            if slow or tick_dt >= _SCHED_SLOW_TICK_SEC:
                logging.warning(
                    "[sched] 本轮 %.1fs｜慢任务（纯 Python 任务占 GIL 会冻结所有接口）：%s",
                    tick_dt, "，".join(slow) if slow else "无单任务超阈值")
        except Exception:  # noqa: BLE001 —— 任何异常都不能让后台线程崩
            logging.exception("调度线程异常（已忽略，继续运行）")


def _sched_ensure_logging() -> None:
    """模块导入阶段 logging 可能尚未 basicConfig；确保 INFO 可见（含 'scheduler started'）。"""
    if not logging.getLogger().handlers:
        logging.basicConfig(level=logging.INFO)


def _start_scheduler() -> None:
    """模块加载阶段启动调度线程（daemon），带重入保护。"""
    global _SCHEDULER_STARTED
    with _SCHEDULER_LOCK:
        if _SCHEDULER_STARTED:
            return
        _SCHEDULER_STARTED = True
    _sched_ensure_logging()
    t = threading.Thread(target=_scheduler_loop, name="autofetch-scheduler", daemon=True)
    t.start()
    logging.info("scheduler started")
    # 2026-10-15：后台预热 /api/hotspots（纯 Python 热词聚合，首次实测 ~40s，且会持 GIL）。
    # 该接口已从前端首屏 loadAll 移出，改为进入「近期热点」面板才拉；这里启动后延迟预热一次，
    # 写进 _TTL_CACHE，用户真正点开时即可命中缓存、不必干等 40 秒。异常隔离，失败不影响主进程。
    try:
        threading.Thread(target=_hotspots_prewarm, name="hotspots-prewarm", daemon=True).start()
    except Exception:  # noqa: BLE001
        logging.exception("hotspots 预热线程启动失败（已忽略）")


def _hotspots_prewarm() -> None:
    """延迟预热 /api/hotspots 并写入 TTL 缓存（见 _TTL_ROUTES）。"""
    try:
        time.sleep(_HOTSPOTS_PREWARM_DELAY)
        key = "/api/hotspots?"
        if _TTL_CACHE.get(key):
            return                        # 已有（用户已访问过）则不重复算
        # 2026-10-15：热点聚合是纯 Python 长任务（实测 138s），**不能**在本进程算
        # （会把所有接口一起冻住几十秒）。改为：① 先读子进程落盘的结果补缓存；
        # ② 文件不存在/太旧 → 让子进程去算，下次启动就能秒命中。
        fp = os.path.join(ROOT, "viewer", "data", "hotspots_cache.json")
        try:
            if os.path.exists(fp) and (time.time() - os.path.getmtime(fp)) < 48 * 3600:
                with open(fp, "r", encoding="utf-8") as f:
                    obj = json.load(f)
                if isinstance(obj, dict) and obj.get("payload"):
                    _TTL_CACHE[key] = (time.monotonic(), obj["payload"])
                    logging.info("hotspots 预热心智：已从落盘缓存加载（生成于 %s）", obj.get("at"))
                    return
        except Exception:  # noqa: BLE001
            logging.exception("读取 hotspots 落盘缓存失败（已忽略，转由子进程计算）")
        if _spawn_offproc("hotspots"):
            logging.info("hotspots 预热已交给子进程（本进程不再计算，避免冻结接口）")
    except Exception:  # noqa: BLE001
        logging.exception("hotspots prewarm 失败（已忽略，用户访问时仍会按需计算）")


def api_scheduler_status() -> dict:
    """/api/scheduler/status 返回：{enabled, last_run_at, next_run_at, run_count, errors_today}。"""
    state = _sched_load_state()
    now = datetime.now()
    enabled = bool(state.get("enabled", True))
    last_run = state.get("last_run_at")
    if not enabled:
        next_run = None
    elif not _sched_in_window(now):
        next_run = _sched_next_window(now).strftime("%Y-%m-%d %H:%M:%S")
    else:
        last = _parse_dt_loose(last_run)
        if last is None:
            next_run = now.strftime("%Y-%m-%d %H:%M:%S")   # 从未跑过，马上会跑
        else:
            cand = last + timedelta(seconds=_SCHED_MIN_GAP)
            next_run = (cand if cand > now else now).strftime("%Y-%m-%d %H:%M:%S")
    return {
        "enabled": enabled,
        "last_run_at": last_run,
        "next_run_at": next_run,
        "run_count": int(state.get("run_count", 0)),
        "errors_today": int(state.get("errors_today", 0)),
        "in_window": _sched_in_window(now),
        "sources": _SCHED_SOURCES,
    }


def boot() -> None:
    """启动期副作用收口（P0-05）。

    此前这段 ensure_schema + 启动调度 + 预热线程在**模块顶层**执行，导致任何
    `import viewer.server` 都会写生产库并拉起抓取线程（13 个外部工具 import 即变生产库写者）。
    现统一收口到 boot()，仅由 main() / desktop_app 在真正启动服务时调用；import 阶段保持纯净。
    """
    # P1-36：所有 schema 改动之前先跑迁移（幂等、带自动快照）
    try:
        from migrate import migrate as _migrate
        _migrate(DB.conn)
    except Exception:  # noqa: BLE001
        logging.exception("schema 迁移失败（继续启动）")
    # 模块加载即启动调度线程（daemon）。被 python -m viewer.server 或 desktop_app 导入时都会执行；
    # 重入保护确保同一进程只启动一份。
    board_flow.ensure_schema(DB.conn)    # 板块资金流采样表（幂等；供调度线程 maybe_sample 写入）
    board_flow_range.ensure_schema(DB.conn)   # 板块资金流日线区间表（幂等；供区间聚合读写）
    policydesk.ensure_schema(DB.conn)    # 政策风向落库表（幂等；零 JOIN 既有业务表）
    lhb_daily.ensure_schema(DB.conn)     # 龙虎榜/板块扩散/主线研判 每日刷新状态表（meta，幂等）
    blogger_daily.ensure_schema(DB.conn)   # 博主荐股/参赛股 每日刷新状态表（meta，幂等；直读源头 Excel）
    # P1-2（2026-10-15）：启动即建全量 schema，杜绝首启 500（历史「no such table: pillar_run」）。
    geo_store.ensure_schema(DB.conn)       # 国际局势表（幂等）
    extref_store.ensure_schema(DB.conn)    # 外部参考表（幂等）
    pillars.ensure_tables(DB.conn)         # 四支柱表（pillar_run/pillar_item 等，幂等）
    resonance.ensure_tables(DB.conn)       # 博主共振融合引擎表（sig_unified/res_stock_card 等，幂等；2026-10-15）
    selfpick.ensure_tables(DB.conn)   # 系统自选表（幂等；2026-10-15）
    evolve.ensure_tables(DB.conn)          # 自我进化引擎表（evo_weight/evo_rule/evo_knowledge 等，幂等；2026-10-15）
    _start_scheduler()
    # 启动即后台预拉取政策风向（表落库 + 板块代表股缓存预热），避免首屏冷抓取卡顿。
    try:
        threading.Thread(target=_safe_policy_refresh, name="policy-boot-prewarm", daemon=True).start()
    except Exception:  # noqa: BLE001
        pass
    # 一键筛股：服务启动 45s 后当日无结果则后台预热一次（防重启丢内存态）
    try:
        bili_scan.maybe_prewarm()
    except Exception:  # noqa: BLE001
        pass
    # 历史回测：启动即后台预热 K 线缓存 + 候选池。
    #   为什么必须预热：backtest 首次请求要解析 predict_kcache.json（201 只票）并取候选池 universe，
    #   冷态要数秒；而浏览器对慢响应会主动掐断 → 页面显示"回测加载失败"，用户以为功能没做出来
    #   （2026-10-15 实测踩到：刚重启后端后第一次打开就是这样）。预热后首请求降到 ~0.04s。
    try:
        threading.Thread(target=backtest.warm, name="backtest-boot-prewarm", daemon=True).start()

        # 2026-10-21：长期底部埋伏首次扫描预热。全市场约 24s 且是纯 Python（持 GIL），
        # 所以延迟 40s 起、错开首屏与落地页请求；扫完落缓存，用户点进面板即命中（否则要等一轮）。
        def _bottom_boot_warm():
            time.sleep(40)
            try:
                if not _bottom_cache_load():
                    _bottom_scan_async()
            except Exception:  # noqa: BLE001
                logging.exception("bottom 启动预热失败（已忽略）")
        threading.Thread(target=_bottom_boot_warm, name="bottom-boot-warm", daemon=True).start()
    except Exception:  # noqa: BLE001
        pass



class Handler(BaseHTTPRequestHandler):
    # 2026-09-22 修复 P3-1：BaseHTTPRequestHandler 默认会把
    # `Server: BaseHTTP/0.6 Python/3.13.14` 写进每个响应（含 send_error 生成的 404），
    # 对外泄露解释器与实现版本。这里覆写版本串，不再暴露 Python 版本。
    server_version = "LocalViewer"
    protocol_version = "HTTP/1.1"  # P3-06：启用 HTTP/1.1，避免并发请求全量重连（默认 HTTP/1.0）
    sys_version = ""

    def version_string(self) -> str:
        return self.server_version

    def _send_json(self, obj, code=200):
        # 2026-10-15（性能缓存）：本请求若命中 TTL 白名单 route，写出前把结果存进缓存（只存 200）。
        # 消费后立即清空 self._ttl_key，避免同请求的其它 _send_json（如 400 回包）误写同一 key。
        _ck = getattr(self, "_ttl_key", None)
        if _ck is not None:
            if code == 200:
                _TTL_CACHE[_ck] = (time.monotonic(), obj)
            self._ttl_key = None
        # 2026-10-12：先清洗 NaN/Infinity（见 _json_safe），再以标准 JSON 输出。
        body = json.dumps(_json_safe(obj), ensure_ascii=False,
                          allow_nan=False, default=str).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        # 2026-09-22 修复 P1-3：原先无条件发 `Access-Control-Allow-Origin: *`，
        # 任意站点都能读取本服务数据、并借浏览器发起写请求。改为仅在
        # 请求来源是回环地址时回显该 Origin（绝不再用 `*`），其余情况不发 CORS 头。
        origin = self.headers.get("Origin")
        if origin and _origin_ok(origin):
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
        self.end_headers()
        self.wfile.write(body)

    def _int_arg(self, q: dict, name: str, default: int, lo: int, hi: int) -> int:
        """读取整数查询参数：非整数 → 400（此前会抛 ValueError 掐断连接）；
        超范围 → 钳制到 [lo, hi]（此前 -1 / 999999999 会变成"返回全表"）。"""
        raw = q.get(name, [None])[0]
        if raw is None or str(raw).strip() == "":
            return default
        try:
            v = int(str(raw).strip())
        except (TypeError, ValueError):
            raise _BadRequest(f"参数 {name} 必须为整数")
        return max(lo, min(hi, v))

    def _str_arg(self, q: dict, name: str, default: str = "", maxlen: int = 64) -> str:
        """读取字符串查询参数并限制长度，超限 → 400（避免超长参数打爆日志/查询）。"""
        v = q.get(name, [None])[0]
        if v is None:
            return default
        v = str(v)
        if len(v) > maxlen:
            raise _BadRequest(f"参数 {name} 过长（上限 {maxlen} 字符）")
        return v

    def _float_arg(self, q: dict, name: str, default: float, lo: float, hi: float) -> float:
        """读取浮点查询参数：非数字 → 400；超范围 → 钳制到 [lo, hi]（仿 _int_arg）。"""
        raw = q.get(name, [None])[0]
        if raw is None or str(raw).strip() == "":
            return default
        try:
            v = float(str(raw).strip())
        except (TypeError, ValueError):
            raise _BadRequest(f"参数 {name} 必须为数字")
        return max(lo, min(hi, v))

    def _guard_write(self) -> bool:
        """写接口统一守卫（CSRF / DNS rebinding / 内容类型三道）。通过返回 True；
        不通过时已自行回响应，调用方直接 return 即可。"""
        if not _host_ok(self.headers.get("Host", "")):
            self._send_json({"error": "forbidden host"}, code=403)
            return False
        origin = self.headers.get("Origin")
        if origin and not _origin_ok(origin):
            self._send_json({"error": "forbidden origin"}, code=403)
            return False
        # 2026-10-12：危险端点（会拉起外部进程 / 改系统级状态）必须带合法 Origin——
        # 无 Origin 的非浏览器客户端一律拒绝，进一步收窄这类动作的触发面。
        if any(self.path.startswith(p) for p in ("/api/edge/restart", "/api/login/open")):
            if not origin or not _origin_ok(origin):
                self._send_json({"error": "origin required"}, code=403)
                return False
        # 2026-10-21 防御纵深：启用本地令牌后，非 GET 写接口必须带匹配 Cookie，否则 401。
        if _LOCAL_TOKEN and self.command != "GET":
            if not _token_ok(self):
                self._send_json({"error": "token required"}, code=401)
                return False
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if ctype != "application/json":
            self._send_json({"error": "unsupported media type, expect application/json"}, code=415)
            return False
        return True

    def _send_file(self, path: Path, ctype: str):
        """只允许发送 STATIC_DIR 内的普通文件。

        2026-09-22 修复 P0 路径穿越：原判据 `str(path).startswith(str(STATIC_DIR))`
        是对**未折叠** `..` 的字符串做前缀比较，而 `Path` 本身不会折叠 `..`，
        于是 `STATIC_DIR / "../server.py"` 能通过校验并被原样读出
        （实测可下载 server.py / viewer/app.db / 明文 Cookie 导出文件）。
        改为 `resolve()` 折叠后再判定是否仍在 STATIC_DIR 内；
        同时把「目录」「不存在」「读取失败」一并归为 404，避免异常直接掐断连接。
        """
        try:
            real = Path(path).resolve()
            root = STATIC_DIR.resolve()
        except OSError:
            self._send_json({"error": "not found"}, code=404)
            return
        if not real.is_file() or not real.is_relative_to(root):
            self._send_json({"error": "not found"}, code=404)
            return
        try:
            data = real.read_bytes()
        except OSError:
            self._send_json({"error": "not found"}, code=404)
            return
        # 2026-10-17 性能：文本类静态资源（app.js 973KB / app.css 284KB）gzip 压缩，传输量降到约 1/4。
        #   仅当客户端 Accept-Encoding 含 gzip 且压缩后更小才启用；不改变 no-cache 校验语义，
        #   故改完界面 F5 仍可见最新，不会回归「改了不刷新」的老问题。
        #   ⚠️ 2026-10-17 修复（双击打不开 · ERR_INVALID_HTTP_RESPONSE 根因）：
        #   send_header 必须一律在 send_response 之后调用——BaseHTTPRequestHandler 的
        #   _headers_buffer 是按调用顺序累积的，先 send_header 再 send_response 会把状态行
        #   塞进响应头中间，浏览器收到「首行不是 HTTP/1.x」→ 直接判无效响应。
        _ae = (self.headers.get("Accept-Encoding") or "")
        _is_text = ctype.startswith("text/") or ctype in ("application/javascript", "application/json")
        _gzipped = False
        if _is_text and "gzip" in _ae and len(data) > 1024:
            import gzip as _gzip
            _c = _gzip.compress(data, 6)
            if len(_c) < len(data):
                data = _c
                _gzipped = True
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        if _gzipped:
            self.send_header("Content-Encoding", "gzip")
            self.send_header("Vary", "Accept-Encoding")
        # 2026-09-28：静态文件禁用缓存（no-cache = 每次都回源校验，但仍可 304 复用）。
        #   原先一个缓存头都不发，WebView2/Chrome 会对 app.js / app.css 做启发式缓存，
        #   结果「后端与源码都改了、界面还是旧的」，非得 Ctrl+Shift+R 硬刷才生效。
        #   本地服务重传几百 KB 代价可忽略，换成每次校验，普通 F5 就能看到最新界面。
        self.send_header("Cache-Control", "no-cache, must-revalidate")
        # 2026-10-21：启用本地令牌时，仅在 HTML 页面下发 SameSite Cookie，供同源写接口校验。
        if _LOCAL_TOKEN and ctype.startswith("text/html"):
            self.send_header(
                "Set-Cookie",
                "local_token=%s; Path=/; SameSite=Lax; Max-Age=86400" % _LOCAL_TOKEN,
            )
        self.end_headers()
        self.wfile.write(data)

    def _archive_fm_snapshot(self, rel: str) -> None:
        """共识周快照「读取即存档」钩子（2026-10-20）。

        每次访问 /static/fund_masters/index.json 时，按其中的 basis_date 留底一份到
        fund_masters/snapshots/<basis_date>.json，并维护 snapshots/index.json 清单。
        同一 basis_date 先到先得：已存在则不覆盖，保证历史不可被后续刷新误改。
        失败一律非致命（仅记日志），绝不阻断 index.json 本身的正常返回。
        """
        if rel != "fund_masters/index.json":
            return
        try:
            src = STATIC_DIR / rel
            if not src.is_file():
                return
            data = json.loads(src.read_text(encoding="utf-8"))
            bd = data.get("basis_date") or data.get("updated_at") or data.get("generated_at")
            if not bd:
                return
            snap_dir = STATIC_DIR / "fund_masters" / "snapshots"
            snap_dir.mkdir(parents=True, exist_ok=True)
            snap_file = snap_dir / f"{bd}.json"
            idx_file = snap_dir / "index.json"
            if not snap_file.exists():
                snap_file.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
            manifest = []
            if idx_file.exists():
                try:
                    manifest = json.loads(idx_file.read_text(encoding="utf-8"))
                except Exception:
                    manifest = []
            if not any(isinstance(s, dict) and s.get("basis_date") == bd for s in manifest):
                manifest.append({
                    "basis_date": bd,
                    "file": f"{bd}.json",
                    "updated_at": data.get("updated_at"),
                    "generated_at": data.get("generated_at"),
                    "universe_n": data.get("universe_n"),
                    "consensus_n": len(data.get("consensus", {}).get("ranked", [])),
                    "archived_at": datetime.now().isoformat(timespec="seconds"),
                })
                manifest.sort(key=lambda x: str(x.get("basis_date", "")))
                idx_file.write_text(json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8")
        except Exception:  # noqa: BLE001
            logging.exception("共识快照存档失败（非致命，跳过）")

    def do_GET(self):
        _REQ_DEADLINE.value = time.monotonic() + 8  # P2-09 处理时限 8s
        """GET 统一异常兜底：任何未预期异常都回 500 JSON，绝不把 traceback / 文件路径给客户端。"""
        try:
            self._do_GET()
        except _BadRequest as e:
            self._send_json({"error": str(e)}, code=400)
        except Exception:  # noqa: BLE001
            logging.exception("GET %s 处理失败", self.path)   # 完整 traceback 只打到 stderr
            try:
                self._send_json({"error": "internal error"}, code=500)
            except Exception:  # noqa: BLE001 —— 响应已发出/连接已断，无力回天
                pass

    def _do_GET(self):
        # 2026-10-12：读接口也做回环 Host 校验（防 DNS rebinding——外部域名解析到 127.0.0.1 时
        # 浏览器会带外部 Host）。写接口由 _guard_write 守，此处补齐读接口，口径一致。
        # 浏览器 fetch 与本项目内部 urllib 自呼叫均自动携带 `Host: 127.0.0.1:8765`，不受影响。
        if not _host_ok(self.headers.get("Host", "")):
            self._send_json({"error": "forbidden host"}, code=403)
            return
        parsed = urllib.parse.urlparse(self.path)
        route = parsed.path

        # ── 2026-10-22 配置读取端点（设置面板读写）──
        if route == "/api/config/get":
            q = urllib.parse.parse_qs(parsed.query)
            key = (q.get("key") or [""])[0]
            return self._send_json(api_config_get(key))

        # 2026-10-15（性能）：重计算读接口的短期 TTL 缓存（白名单见 _TTL_ROUTES）。
        # 命中直接回缓存体；未命中记下 key，交给本次 _send_json 在写出前落缓存。
        # 带 `t=` / `force=` 的请求视为「要最新」，一律绕过（前端手动刷新走这条）。
        self._ttl_key = None
        if route in _TTL_ROUTES:
            _qs = parsed.query or ""
            if ("t=" not in _qs) and ("force=" not in _qs):
                _key = route + "?" + _qs
                _hit = _TTL_CACHE.get(_key)
                if _hit is not None and (time.monotonic() - _hit[0]) < _TTL_ROUTES[route]:
                    return self._send_json(_hit[1])
                self._ttl_key = _key
        if route == "/" or route == "/index.html":
            self._send_file(STATIC_DIR / "index.html", "text/html; charset=utf-8")
            return
        if route.startswith("/static/"):
            rel = route[len("/static/"):]
            self._archive_fm_snapshot(rel)  # 2026-10-20：共识周快照「读取即存档」（P2-24：已知 GET 带写副作用，已异常隔离）
            ctype = "text/css; charset=utf-8" if rel.endswith(".css") else (
                "application/javascript; charset=utf-8" if rel.endswith(".js") else "text/html; charset=utf-8")
            self._send_file(STATIC_DIR / rel, ctype)
            return

        # ---- 2026-10-22 P2：行情预存层（只读本地 market_quotes/market_kline；缺失/过期回退现抓）----
        if route.startswith("/api/market_cache/"):
            try:
                import sys as _sys
                _root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
                if _root not in _sys.path:
                    _sys.path.insert(0, _root)
                import market_cache as _mc
                _mc.ensure_schema(DB.conn)
                _q = urllib.parse.parse_qs(parsed.query)
                _sym = self._str_arg(_q, "symbol", "", maxlen=20) or self._str_arg(_q, "code", "", maxlen=20)
                if route == "/api/market_cache/quote":
                    _force = _q.get("force", ["0"])[0] == "1"
                    return self._send_json(_mc.read_quote(DB.conn, _sym, force=_force) if _sym else {"error": "symbol required"})
                if route == "/api/market_cache/symbols":
                    return self._send_json(_mc.list_symbols(DB.conn))
                if route == "/api/market_cache/kline":
                    _lim = self._int_arg(_q, "limit", 120, 1, 1000)
                    return self._send_json(_mc.read_kline(DB.conn, _sym, _lim) if _sym else {"error": "symbol required"})
                if route == "/api/market_cache/prefetch":
                    _cap = self._int_arg(_q, "cap", 250, 10, 2000)
                    return self._send_json(_mc.prefetch_all(DB.conn, cap=_cap))
                if route == "/api/market_cache/status":
                    return self._send_json(_mc.cache_status(DB.conn))
                return self._send_json({"error": "unknown market_cache route"})
            except Exception as e:  # noqa: BLE001
                logging.exception("market_cache 处理失败")
                return self._send_json({"error": str(e)[:200]}, code=500)
        if route == "/api/version":
            # 前端版本自检用：从 index.html 的 <meta name="app-version"> 解析（唯一真源，避免版本号写两份）。
            # 前端每 60s 拉一次跟页面里正在跑的版本比对，不一致就提示刷新。
            return self._send_json(api_version())
        if route == "/api/summary":
            return self._send_json(api_summary())
        if route == "/api/stocks":
            return self._send_json(api_stocks())
        if route == "/api/operating":
            return self._send_json(api_operating())
        if route == "/api/persons":
            # 2026-10-05 建议8：支持分页（limit/offset），不传 = 全量，行为与从前一致
            q = urllib.parse.parse_qs(parsed.query)
            return self._send_json(api_persons(limit=self._int_arg(q, "limit", 0, 0, 1000),
                                               offset=self._int_arg(q, "offset", 0, 0, 1000)))
        if route == "/api/strategies":
            # 2026-09-26 T03：策略实验室全量接口（目录 × 窗口指标 LEFT JOIN）。
            return self._send_json(api_strategies())
        if route == "/api/fund_strategies":
            # 2026-09-26：点击策略 → 关联公募基金/ETF（净口径，逐条标来源）。
            q = urllib.parse.parse_qs(parsed.query)
            key = self._str_arg(q, "key", "", maxlen=64) or None
            return self._send_json(api_fund_strategies(key))
        if route == "/api/public_records":
            return self._send_json(api_public_records())
        if route == "/api/sentiment":
            return self._send_json(api_sentiment())
        if route == "/api/topics":
            return self._send_json(api_topics())
        if route == "/api/hotspots":
            return self._send_json(api_hotspots())
        if route == "/api/refresh_status":
            return self._send_json(api_refresh_status())
        if route == "/api/market_freshness":
            # 2026-10-09 新增：现抓行情时间戳（与论坛抓取新鲜度分开计时），供顶栏如实显示数据时间
            return self._send_json(api_market_freshness())
        if route == "/api/scheduler/status":
            return self._send_json(api_scheduler_status())
        if route == "/api/kb/list":
            return self._send_json(api_kb_list())
        if route == "/api/raw_pages":
            q = urllib.parse.parse_qs(parsed.query)
            return self._send_json(api_raw_pages(self._int_arg(q, "limit", 60, 1, 500)))
        if route == "/api/kb/doc":
            q = urllib.parse.parse_qs(parsed.query)
            key = self._str_arg(q, "key", "", maxlen=40) or None
            light = q.get("light", ["0"])[0] == "1"
            return self._send_json(api_kb_doc(key, light=light))
        if route == "/api/players":
            q = urllib.parse.parse_qs(parsed.query)
            top = q.get("top", ["0"])[0] == "1"
            ch = self._str_arg(q, "channel", "", maxlen=8) or None
            tier = self._str_arg(q, "tier", "", maxlen=8) or None
            vis = q.get("visible", ["0"])[0] == "1"
            return self._send_json(api_players(top_only=top, channel=ch, tier=tier, visible_only=vis))
        if route == "/api/global_market":
            # 2026-10-17 新增：全球大盘（美股/日韩欧主要经济体 + 港股 + 跨资产）
            return self._send_json(api_global_market())
        if route == "/api/overseas_sentiment":
            return self._send_json(api_overseas_sentiment())
        if route == "/api/overseas_hotspots":
            return self._send_json(api_overseas_hotspots())
        if route == "/api/overseas_sources":
            return self._send_json(api_overseas_sources())
        if route == "/api/geo_events":
            q = urllib.parse.parse_qs(parsed.query)
            cat = self._str_arg(q, "category", "", maxlen=32) or None
            days = self._int_arg(q, "days", 14, 1, 180)
            panel = self._str_arg(q, "panel", "", maxlen=16) or None
            ndays = self._int_arg(q, "news_days", 10, 1, 60)
            nlim = self._int_arg(q, "news_limit", 40, 5, 100)
            return self._send_json(api_geo_events(category=cat, days=days, panel=panel,
                                                 news_days=ndays, news_limit=nlim))
        if route == "/api/geo_refresh":
            return self._send_json(api_geo_refresh())
        if route == "/api/extref_signals":
            q = urllib.parse.parse_qs(parsed.query)
            kd = self._str_arg(q, "kind", "", maxlen=16) or None
            tr = self._str_arg(q, "tier", "", maxlen=4) or None
            dr = self._str_arg(q, "direction", "", maxlen=8) or None
            hd = self._str_arg(q, "hardness", "", maxlen=8) or None
            stt = self._str_arg(q, "status", "", maxlen=12) or None
            dys = self._int_arg(q, "days", 0, 0, 3650)
            return self._send_json(api_extref_signals(kind=kd, tier=tr, direction=dr,
                                                      hardness=hd, days=(dys or None), status=stt))
        if route == "/api/extref_refresh":
            return self._send_json(api_extref_refresh())
        if route == "/api/freshness":
            return self._send_json(api_freshness())
        if route == "/api/extref_signal_detail":
            q = urllib.parse.parse_qs(parsed.query)
            sid = self._str_arg(q, "sid", "", maxlen=120)
            return self._send_json(api_extref_signal_detail(sid))
        if route == "/api/extref_source_detail":
            q = urllib.parse.parse_qs(parsed.query)
            sk = self._str_arg(q, "src_key", "", maxlen=64)
            return self._send_json(api_extref_source_detail(sk))
        if route == "/api/extref_collect":
            q = urllib.parse.parse_qs(parsed.query)
            sk = self._str_arg(q, "src_key", "", maxlen=64) or None
            n = self._int_arg(q, "page_size", 20, 3, 100)
            return self._send_json(api_extref_collect(src_key=sk, page_size=n))
        if route == "/api/extref_backtest":
            return self._send_json(api_extref_backtest())
        if route == "/api/positions":
            return self._send_json(api_positions())
        if route == "/api/lhb":
            q = urllib.parse.parse_qs(parsed.query)
            d = self._str_arg(q, "date", "", maxlen=10) or None
            wd = self._int_arg(q, "window_days", 5, 1, 30)
            return self._send_json(api_lhb(date=d, window_days=wd))
        if route == "/api/lhb_sectors":
            q = urllib.parse.parse_qs(parsed.query)
            d = self._str_arg(q, "date", "", maxlen=10) or None
            return self._send_json(api_lhb_sectors(date=d))
        if route == "/api/niusan":
            q = urllib.parse.parse_qs(parsed.query)
            mh = self._int_arg(q, "min_hold", 2, 1, 50)
            sc = self._str_arg(q, "scope", "free", maxlen=8) or "free"
            return self._send_json(api_niusan(min_hold=mh, scope=sc))
        if route == "/api/star_moves":
            q = urllib.parse.parse_qs(parsed.query)
            lim = self._int_arg(q, "limit", 80, 10, 300)
            per = self._int_arg(q, "periods", 4, 1, 8)
            sc = self._str_arg(q, "scope", "free", maxlen=8) or "free"
            return self._send_json(api_star_moves(limit=lim, periods=per, scope=sc))
        if route == "/api/dashboard":
            q = urllib.parse.parse_qs(parsed.query)
            force = q.get("refresh", ["0"])[0] == "1"
            return self._send_json(api_dashboard(force=force))
        if route == "/api/today_decision":
            # 2026-10-12 今日决策首页（体验审计 P0-A）：一屏四卡（方向/机会/风险/我的票）+ 一句话结论
            return self._send_json(api_today_decision())
        if route == "/api/info_brief":
            # P0-D 消息与外围顶部汇总结论：今日消息面 Top5 · 影响板块
            return self._send_json(api_info_brief())
        if route == "/api/smart_money":
            # P0-E 别人怎么做：聪明钱共识榜（多人同时买的方向 + 个股）
            q = urllib.parse.parse_qs(parsed.query)
            mad = self._int_arg(q, "max_age_days", 0, 0, 3650)
            return self._send_json(api_smart_money(max_age_days=(mad or None)))
        if route == "/api/watch_brief":
            # P0-F 我的票：今日提示（到价/接近目标价/池内看空分歧/池内在主线）
            return self._send_json(api_watch_brief())
        if route in ("/api/auction", "/api/auction/"):
            # 手动补采 / 补历史（今日总览里的竞价区块数据源，正常靠后台 9:25 自动采）
            #   force=1 强制重采当天；date=YYYY-MM-DD 指定交易日（留空=按行情时间戳自动校正）；
            #   backfill=1 用日 K 回填历史竞价定盘价（只补价，不补竞价量）
            q = urllib.parse.parse_qs(parsed.query)
            if self._int_arg(q, "backfill", 0, 0, 1):
                return self._send_json(auction.backfill(days=self._int_arg(q, "days", 10, 1, 40)))
            res = auction.collect(force=bool(self._int_arg(q, "force", 0, 0, 1)),
                                  date=self._str_arg(q, "date", "", maxlen=10) or None)
            if not res.get("ok"):
                return self._send_json(res)
            return self._send_json(auction.summary(date=self._str_arg(q, "date", "", maxlen=10) or None))
        if route == "/api/branch_detail":
            # 点看板上的分支 → 展开成员股 / 买方席位 / 博主覆盖 / 判断（全部本地数据，秒回）
            q = urllib.parse.parse_qs(parsed.query)
            sec = self._str_arg(q, "sector", "", maxlen=32)
            br = self._str_arg(q, "branch", "", maxlen=32)
            if not br:
                raise _BadRequest("branch 为必填（sector 可留空，表示无主线归属的全市场概念）")
            dt = self._str_arg(q, "date", "", maxlen=10) or None
            return self._send_json(api_branch_detail(sec, br, date=dt))
        if route == "/api/consensus":
            q = urllib.parse.parse_qs(parsed.query)
            mh = self._int_arg(q, "min_holders", 2, 1, 1000)
            sym = self._str_arg(q, "symbol", "") or None
            oq = _truthy(q.get("only_qualified", [None])[0])
            mad = self._int_arg(q, "max_age_days", 0, 0, 3650)
            return self._send_json(api_consensus(min_holders=mh, symbol=sym,
                                                  only_qualified=oq,
                                                  max_age_days=(mad or None)))
        if route == "/api/cohold_recent":
            q = urllib.parse.parse_qs(parsed.query)
            days = self._int_arg(q, "days", 5, 1, 60)
            mh = self._int_arg(q, "min_holders", 2, 2, 1000)
            # anchor：auto(默认，锚定数据最新日) / today / YYYY-MM-DD
            anchor = self._str_arg(q, "anchor", "", maxlen=20) or None
            # only_qualified：1/true/yes/on → 仅统计 Tier A 入围选手
            oq = _truthy(q.get("only_qualified", [None])[0])
            return self._send_json(api_cohold_recent(days=days, min_holders=mh,
                                                     anchor=anchor, only_qualified=oq))
        if route == "/api/posts":
            q = urllib.parse.parse_qs(parsed.query)
            # 2026-09-22 修复 P3-2：钳制到 [1, 500]。此前 limit=-1 / 999999999 都等于"返回全表"。
            limit = self._int_arg(q, "limit", 20, 1, 500)
            rows = DB.conn.execute(
                "SELECT id, site, author, post_time, text, sentiment FROM posts "
                "ORDER BY post_time DESC LIMIT ?", (limit,)
            ).fetchall()
            return self._send_json([dict(r) for r in rows])
        if route == "/api/postdigest":
            # 2026-10-02：王总「别给我帖子列表，直接给结论——大家在聊的股票/板块是看涨还是看跌」。
            # 所以这里返回的不是帖子，是**结论**：总论 + 按行业 + 按个股的情绪判断。
            q = urllib.parse.parse_qs(parsed.query)
            hours = self._int_arg(q, "hours", 24, 1, 168)
            # postdigest 只读 posts 正文，内部开独立的只读 sqlite 连接，与写锁无关，直接跑。
            return self._send_json(postdigest.build(hours=hours))
        if route == "/api/login_status":
            return self._send_json(DB.get_login_status())
        if route == "/api/person":
            q = urllib.parse.parse_qs(parsed.query)
            return self._send_json(api_person(self._str_arg(q, "name", "")))
        if route == "/api/player":
            q = urllib.parse.parse_qs(parsed.query)
            return self._send_json(api_player(self._str_arg(q, "name", "")))
        if route == "/api/stock":
            q = urllib.parse.parse_qs(parsed.query)
            return self._send_json(api_stock(self._str_arg(q, "symbol", "")))
        if route == "/api/kline":
            q = urllib.parse.parse_qs(parsed.query)
            sym = self._str_arg(q, "symbol", "")
            period = self._str_arg(q, "period", "day", maxlen=8)
            # 2026-10-05 建议8：默认 250 根（limit=0 才拉全历史），offset 做向上翻页
            return self._send_json(api_kline(
                sym, period,
                limit=self._int_arg(q, "limit", 250, 0, 5000),
                offset=self._int_arg(q, "offset", 0, 0, 5000)))
        if route == "/api/quote":
            # 个股重要信息（行情/估值/财务）；force=1 绕过 60s 缓存（刷新按钮用）
            q = urllib.parse.parse_qs(parsed.query)
            sym = self._str_arg(q, "symbol", "")
            force = q.get("force", ["0"])[0] == "1"
            return self._send_json(api_quote(sym, force=force))
        if route == "/api/stock_names":
            return self._send_json(DB.all_symbols())
        if route == "/api/detail":
            q = urllib.parse.parse_qs(parsed.query)
            kind = self._str_arg(q, "kind", "post", maxlen=16)
            _id = self._int_arg(q, "id", 0, 0, 2_000_000_000)
            full = q.get("full", ["0"])[0] == "1"
            return self._send_json(api_detail(kind, _id, full=full))
        if route == "/api/detail_list":
            # 统计卡片点击 → 明细列表。默认 500 条，最多 1000，避免大表一次性全量返回。
            q = urllib.parse.parse_qs(parsed.query)
            kind = self._str_arg(q, "kind", "posts", maxlen=32)
            limit = self._int_arg(q, "limit", 500, 1, 1000)
            offset = self._int_arg(q, "offset", 0, 0, 10_000_000)
            return self._send_json(api_detail_list(kind, limit=limit, offset=offset))
        if route == "/api/watch_list":
            q = urllib.parse.parse_qs(parsed.query)
            force = q.get("refresh", ["0"])[0] == "1"
            return self._send_json(api_watch_list(force=force))
        if route == "/api/watch_alerts":
            q = urllib.parse.parse_qs(parsed.query)
            lim = self._int_arg(q, "limit", 200, 1, 1000)
            only_unread = q.get("unread", ["0"])[0] == "1"
            return self._send_json(api_watch_alerts(limit=lim, only_unread=only_unread))
        # ---- 增强功能（2026-09-27 新增，只读，零 JOIN 既有业务表）----
        if route == "/api/moneyflow_stock":
            q = urllib.parse.parse_qs(parsed.query)
            return self._send_json(api_moneyflow_stock(self._str_arg(q, "symbol", "", maxlen=20)))
        if route == "/api/moneyflow_rank":
            q = urllib.parse.parse_qs(parsed.query)
            # force=1 绕过 30 分钟失败负缓存（前端「强制重试」）
            return self._send_json(api_moneyflow_rank(pz=self._int_arg(q, "pz", 30, 1, 100),
                                                      force=self._str_arg(q, "force", "", 1).lower() == "1"))
        if route == "/api/moneyflow_board":
            q = urllib.parse.parse_qs(parsed.query)
            return self._send_json(api_moneyflow_board(pz=self._int_arg(q, "pz", 30, 1, 100),
                                                       force=self._str_arg(q, "force", "", 1).lower() == "1"))
        if route == "/api/board_flow_snapshot":
            q = urllib.parse.parse_qs(parsed.query)
            return self._send_json(api_board_flow_snapshot(
                kind=self._str_arg(q, "kind", "industry", maxlen=16),
                pz=self._int_arg(q, "pz", 60, 1, 200),
                force=self._int_arg(q, "force", 0, 0, 1)))
        if route == "/api/board_flow_intraday":
            q = urllib.parse.parse_qs(parsed.query)
            return self._send_json(api_board_flow_intraday(
                kind=self._str_arg(q, "kind", "industry", maxlen=16),
                top=self._int_arg(q, "top", 12, 3, 20),
                force=self._int_arg(q, "force", 0, 0, 1)))
        if route == "/api/board_flow_range":
            q = urllib.parse.parse_qs(parsed.query)
            return self._send_json(api_board_flow_range(
                start=self._str_arg(q, "start", "", maxlen=16),
                end=self._str_arg(q, "end", "", maxlen=16),
                kind=self._str_arg(q, "kind", "industry", maxlen=16),
                top=self._int_arg(q, "top", 16, 4, 24),
                force=self._int_arg(q, "force", 0, 0, 1)))
        if route == "/api/board_flow_dates":
            q = urllib.parse.parse_qs(parsed.query)
            return self._send_json(api_board_flow_dates(
                kind=self._str_arg(q, "kind", "industry", maxlen=16)))
        if route == "/api/board_flow_status":
            q = urllib.parse.parse_qs(parsed.query)
            return self._send_json(api_board_flow_status())        # 每日预测（2026-09-28 新增）
        if route == "/api/board":
            q = urllib.parse.parse_qs(parsed.query)
            return self._send_json(api_board(
                self._str_arg(q, "code", "", maxlen=40),
                kind=self._str_arg(q, "kind", "industry", maxlen=16),
                name=self._str_arg(q, "name", "", maxlen=40),
                force=self._int_arg(q, "force", 0, 0, 1)))
        if route == "/api/prediction/today":
            return self._send_json(api_prediction_today())
        if route == "/api/prediction/scores":
            return self._send_json(api_prediction_scores())
        if route == "/api/prediction/track":
            return self._send_json(api_prediction_track())
        if route == "/api/growth/adjust_log":
            return self._send_json(api_growth_adjust_log())
        if route == "/api/prediction/status":
            return self._send_json(api_prediction_status())
        if route == "/api/prediction/dates":
            return self._send_json(api_prediction_dates())
        if route == "/api/prediction/history":
            q = urllib.parse.parse_qs(parsed.query)
            return self._send_json(api_prediction_history(
                date=self._str_arg(q, "date", ""),
                range_days=self._int_arg(q, "range", 0, 0, 9999),
                window=self._int_arg(q, "window", 5, 1, 60)))
        if route == "/api/prediction/run":
            q = urllib.parse.parse_qs(parsed.query)
            return self._send_json(api_prediction_run(force=self._int_arg(q, "force", 1, 0, 1)))
        if route == "/api/prediction/catchup":
            return self._send_json(api_prediction_catchup())
        # ── 2026-10-15 四支柱每日预测引擎（查看器只读）──
        if route == "/api/pillars":
            q = urllib.parse.parse_qs(parsed.query)
            return self._send_json(pillars.payload(DB.conn, date=self._str_arg(q, "date", "", maxlen=16)))
        if route == "/api/pillars/run":
            q = urllib.parse.parse_qs(parsed.query)
            return self._send_json(pillars.generate(DB.conn, force=bool(self._int_arg(q, "force", 1, 0, 1))))
        if route == "/api/pillars/status":
            return self._send_json(pillars.status_payload(DB.conn))
        # ── 2026-10-15 历史回测（只读：预测名单 × T+N 真实走势；离线读 K 线缓存，不联网）──
        if route == "/api/backtest":
            q = urllib.parse.parse_qs(parsed.query)
            return self._send_json(backtest.payload(
                DB.conn,
                source=self._str_arg(q, "source", "pillars", maxlen=16),
                date=self._str_arg(q, "date", "", maxlen=16),
                period=self._int_arg(q, "period", 0, 0, 999),
                # 2026-10-16：pillar = 当前页那根支柱 → 回测只统计它的票
                pillar=self._str_arg(q, "pillar", "", maxlen=24)))
        if route == "/api/backtest/status":
            return self._send_json(backtest.status(DB.conn))
        # ── 2026-10-15 博主共振融合引擎（三支柱：个股共振 / 战法体系 / 信源画像；查看器只读）──
        if route == "/api/selfpick":
            return self._send_json(selfpick.payload(DB.conn))
        if route == "/api/selfpick/status":
            return self._send_json(selfpick.status_payload(DB.conn))
        if route == "/api/resonance":
            return self._send_json(resonance.payload(DB.conn))
        if route == "/api/resonance/status":
            return self._send_json(resonance.status_payload(DB.conn))
        # ── 2026-10-21 dy9 避雷名单（进哥拍板）：低分卡显式归因，只做排除不做推荐 ──
        if route == "/api/avoid":
            return self._send_json(resonance.avoid_payload(DB.conn))
        # ── 2026-10-16 量化板块（第三维信号源：因子全景/多因子选股/因子回测/菜谱预设/量化因子自我学习；查看器只读）──
        if route == "/api/quant":
            return self._send_json(quant.payload(DB.conn))
        if route == "/api/quant/status":
            return self._send_json(quant.status_payload(DB.conn))
        if route == "/api/quant/panorama":
            q = urllib.parse.parse_qs(parsed.query)
            return self._send_json(quant.factor_panorama(
                self._str_arg(q, "code", "", maxlen=16), con=DB.conn))
        if route == "/api/quant/select":
            q = urllib.parse.parse_qs(parsed.query)
            return self._send_json(quant.multi_factor_select(
                top_n=self._int_arg(q, "top_n", 30, 5, 200), con=DB.conn))
        if route == "/api/quant/backtest":
            return self._send_json(quant.factor_backtest(con=DB.conn))
        if route == "/api/quant/recipe":
            return self._send_json(quant.recipe_list(DB.conn))
        # ── 2026-10-17 ETF 基金板块（今日总览顶层 NAV，与量化/共振并列；查看器只读，左右两栏分离）──
        if route == "/api/etf":
            return self._send_json(etf.payload(DB.conn))
        if route == "/api/etf/status":
            return self._send_json(etf.status_payload(DB.conn))
        # ── 2026-10-15 自我进化引擎（权重自学习 / 规则挖掘 / 衰减预警 / 知识内化；查看器只读）──
        if route == "/api/evolve":
            return self._send_json(evolve.payload(DB.conn))
        if route == "/api/evolve/status":
            return self._send_json(evolve.status_payload(DB.conn))
        if route == "/api/screener":
            q = urllib.parse.parse_qs(parsed.query)
            p_raw = q.get("p", [""])[0]
            params = None
            if p_raw:
                try:
                    params = json.loads(p_raw)
                    if not isinstance(params, dict):
                        params = None
                except Exception:  # noqa: BLE001
                    params = None
            return self._send_json(api_screener(screen=self._str_arg(q, "screen", "value", maxlen=96),
                                               limit=self._int_arg(q, "limit", 40, 1, 200),
                                               params=params))
        if route == "/api/screener_boards":
            q = urllib.parse.parse_qs(parsed.query)
            return self._send_json(api_screener_boards(kind=self._str_arg(q, "kind", "industry", maxlen=16),
                                                       pz=self._int_arg(q, "pz", 90, 1, 200)))
        if route == "/api/screener_board_members":
            q = urllib.parse.parse_qs(parsed.query)
            return self._send_json(api_screener_board_members(code=self._str_arg(q, "code", "", maxlen=16)))
        if route == "/api/screener_decline":
            q = urllib.parse.parse_qs(parsed.query)
            return self._send_json(api_screener_decline(days=self._int_arg(q, "days", 30, 5, 120),
                                                        down=self._int_arg(q, "down", 25, 1, 120),
                                                        limit=self._int_arg(q, "limit", 80, 1, 200),
                                                        sort=self._str_arg(q, "sort", "down", maxlen=8),
                                                        force=q.get("force", ["0"])[0] == "1"))
        # ── 2026-10-07 智能选股增强 B4/B7/B8 ──
        if route == "/api/screener/valuation":
            if se is None:
                return self._send_json({"ok": False, "error": "screener_extra 未加载"}, code=500)
            q = urllib.parse.parse_qs(parsed.query)
            codes = [c.strip() for c in (q.get("codes", [""])[0].split(",")) if c.strip()]
            data = se.valuation_pct(codes)
            if codes and all((d.get("ok") is False) for d in data.values()):
                return self._send_json({"ok": False,
                                        "error": "全部标的均未获取到真实估值指标（请检查 akshare 安装与网络）",
                                        "data": data})
            return self._send_json({"ok": True, "data": data})
        if route == "/api/screener/backtest":
            if se is None:
                return self._send_json({"ok": False, "error": "screener_extra 未加载"}, code=500)
            q = urllib.parse.parse_qs(parsed.query)
            codes = [c.strip() for c in (q.get("codes", [""])[0].split(",")) if c.strip()]
            start = self._str_arg(q, "start", "2024-01-01", maxlen=16)
            end = self._str_arg(q, "end", datetime.now().strftime("%Y-%m-%d"), maxlen=16)
            return self._send_json(se.backtest(codes, start, end))
        if route == "/api/screener/themes":
            if se is None:
                return self._send_json({"ok": False, "error": "screener_extra 未加载"}, code=500)
            data = se.themes_map()
            if not data:
                return self._send_json({"ok": False,
                                        "error": "未能获取真实概念板块数据（请检查 akshare 安装与网络）"})
            return self._send_json({"ok": True, "data": data})
        # ── 2026-09 行业龙头全景（王总拍板）：真实市值走 akshare；沙箱 available:False ──
        if route == "/api/industry_leaders":
            if il is None:
                return self._send_json({"ok": False, "error": "industry_leaders 未加载"}, code=500)
            q = urllib.parse.parse_qs(parsed.query)
            top = self._int_arg(q, "top", 5, 1, 30)
            return self._send_json(il.get_industry_leaders(top=top))
        if route == "/api/valuation_cache/status":
            if se is None:
                return self._send_json({"ok": False, "error": "screener_extra 未加载"}, code=500)
            return self._send_json(se.valuation_cache_status())
        if route == "/api/settings/deepseek_key":
            if se is None:
                return self._send_json({"ok": False, "error": "screener_extra 未加载"}, code=500)
            return self._send_json({"configured": bool(se.get_deepseek_key())})
        if route == "/api/stock_turnover":
            q = urllib.parse.parse_qs(parsed.query)
            return self._send_json(api_stock_turnover(symbol=self._str_arg(q, "symbol", "", maxlen=16),
                                                      days=self._int_arg(q, "days", 30, 5, 120)))
        if route == "/api/stock_f10":
            q = urllib.parse.parse_qs(parsed.query)
            return self._send_json(api_stock_f10(symbol=self._str_arg(q, "symbol", "", maxlen=16)))
        if route == "/api/screener_turnover_top":
            q = urllib.parse.parse_qs(parsed.query)
            return self._send_json(api_screener_turnover_top(days=self._int_arg(q, "days", 30, 5, 120),
                                                             limit=self._int_arg(q, "limit", 30, 10, 100)))
        # ── 2026-10-13 板块二：长期低位震荡选股（全量版：基本面+国资系+突破雷达）──
        if route == "/api/screener_lowbox":
            q = urllib.parse.parse_qs(parsed.query)
            return self._send_json(api_screener_lowbox(
                codes=self._str_arg(q, "codes", "", maxlen=400),
                limit=self._int_arg(q, "limit", 30, 5, 100),
                box_window=self._int_arg(q, "box_window", 750, 0, 3000),
                low_pct=self._float_arg(q, "low_pct", 30.0, 5.0, 80.0),
                quality=self._str_arg(q, "quality", "off", maxlen=8),
                w_shape=self._float_arg(q, "w_shape", 40.0, 0.0, 100.0),
                w_fund=self._float_arg(q, "w_fund", 35.0, 0.0, 100.0),
                w_worth=self._float_arg(q, "w_worth", 25.0, 0.0, 100.0)))
        # ── 2026-10-21 板块三：长期底部埋伏（全市场 · 自适应平台 · 1/2/3/5/8年分档）──
        if route == "/api/screener_bottom":
            q = urllib.parse.parse_qs(parsed.query)
            return self._send_json(api_screener_bottom(
                min_years=self._float_arg(q, "min_years", 0.0, 0.0, 20.0),
                state=self._str_arg(q, "state", "all", maxlen=12),
                pool=self._str_arg(q, "pool", "market", maxlen=10),
                codes=self._str_arg(q, "codes", "", maxlen=400),
                limit=self._int_arg(q, "limit", 100, 5, 300),
                refresh=self._int_arg(q, "refresh", 0, 0, 1),
                include_st=self._int_arg(q, "include_st", 0, 0, 1),
                prefix=self._str_arg(q, "prefix", "", maxlen=2),
                min_score=self._float_arg(q, "min_score", 0.0, 0.0, 100.0),
                medal=self._str_arg(q, "medal", "all", maxlen=4)))
        # ── 2026-10-21 行业龙头分级（金/银/铜牌，全站个股徽章）──
        if route == "/api/dragon_map":
            return self._send_json(api_dragon_map())
        if route == "/api/dragon":
            q = urllib.parse.parse_qs(parsed.query)
            return self._send_json(api_dragon_one(self._str_arg(q, "code", "", maxlen=12)))
        if route == "/api/lowbox_breakouts":
            return self._send_json(api_lowbox_breakouts())
        if route == "/api/rate/timeline":        # 2026-10-05 新板块「入手概率」时间轴
            q = urllib.parse.parse_qs(parsed.query)
            return self._send_json(api_rate_timeline(
                days_window=self._int_arg(q, "days_window", 10, 1, 60),
                per_day=self._int_arg(q, "per_day", 5, 1, 30)))
        if route == "/api/valuation":
            q = urllib.parse.parse_qs(parsed.query)
            return self._send_json(api_valuation(self._str_arg(q, "symbol", "", maxlen=20)))
        if route == "/api/diag":
            q = urllib.parse.parse_qs(parsed.query)
            return self._send_json(api_diag(self._str_arg(q, "symbol", "", maxlen=20),
                                            force=self._int_arg(q, "force", 0, 0, 1)))
        if route == "/api/research_reports":
            q = urllib.parse.parse_qs(parsed.query)
            return self._send_json(api_research_reports(pz=self._int_arg(q, "pz", 30, 1, 100),
                                                       page=self._int_arg(q, "page", 1, 1, 50)))
        if route == "/api/research_rating":
            q = urllib.parse.parse_qs(parsed.query)
            return self._send_json(api_research_rating(self._str_arg(q, "symbol", "", maxlen=20)))
        if route == "/api/research/insight":
            q = urllib.parse.parse_qs(parsed.query)
            return self._send_json(api_research_insight(days=self._int_arg(q, "days", 60, 7, 180),
                                                       force=self._int_arg(q, "force", 0, 0, 1)))
        if route == "/api/multiquote_preset":
            return self._send_json(api_multiquote_preset())
        if route == "/api/multiquote_lookup":
            q = urllib.parse.parse_qs(parsed.query)
            return self._send_json(api_multiquote_lookup(self._str_arg(q, "symbol", "", maxlen=20)))
        if route == "/api/watchdesk":
            return self._send_json(api_watchdesk())
        if route == "/api/watchdesk/detail":
            q = urllib.parse.parse_qs(parsed.query)
            return self._send_json(api_watchdesk_detail(self._str_arg(q, "code", "", maxlen=20)))
        if route == "/api/watchdesk/scan":
            return self._send_json(api_watchdesk_scan())
        if route == "/api/news_stock":
            q = urllib.parse.parse_qs(parsed.query)
            return self._send_json(api_news_stock(self._str_arg(q, "symbol", "", maxlen=20),
                                                 num=self._int_arg(q, "num", 30, 1, 100)))
        if route == "/api/news_market":
            q = urllib.parse.parse_qs(parsed.query)
            return self._send_json(api_news_market(num=self._int_arg(q, "num", 30, 1, 100)))
        if route == "/api/policy_desk":
            q = urllib.parse.parse_qs(parsed.query)
            return self._send_json(api_policy_desk(num=self._int_arg(q, "num", 80, 10, 200)))
        # ---- 博主荐股 / 当红参赛股（2026-10-01 新增）----
        if route == "/api/blogger/facets":
            return self._send_json(api_blogger_facets())
        if route == "/api/blogger/reco":
            q = urllib.parse.parse_qs(parsed.query)
            bl = self._str_arg(q, "blogger", "", maxlen=64) or None
            kw = self._str_arg(q, "q", "", maxlen=64) or None
            srt = self._str_arg(q, "sort", "", maxlen=8) or "date"
            lim = self._int_arg(q, "limit", 300, 1, 2000)
            return self._send_json(api_blogger_reco(blogger=bl, q=kw, sort=srt, limit=lim))
        if route == "/api/blogger/overlap":
            return self._send_json(api_blogger_overlap())
        if route == "/api/blogger/perf":
            q = urllib.parse.parse_qs(parsed.query)
            return self._send_json(api_blogger_perf(
                code=self._str_arg(q, "code", "", maxlen=16) or None,
                blogger=self._str_arg(q, "blogger", "", maxlen=64) or None,
                force=self._str_arg(q, "force", "", maxlen=8).lower() in ("1", "true", "yes"),
            ))
        if route == "/api/blogger/stats":
            return self._send_json(api_blogger_stats())
        # ---- 抖音博主语料（2026-10-20 进哥需求：情绪/技术两域并入查看器）----
        # 与 B站 bili_* 是**两套独立数据源**，接口也不复用（bili_pile 是分类堆，这里是视频语料分页）。
        if route == "/api/douyin/overview":
            return self._send_json(api_douyin_overview())
        if route == "/api/douyin/facets":
            q = urllib.parse.parse_qs(parsed.query)
            dm = self._str_arg(q, "domain", "tech", maxlen=16) or "tech"
            return self._send_json(api_douyin_facets(domain=dm))
        if route == "/api/douyin/list":
            q = urllib.parse.parse_qs(parsed.query)
            return self._send_json(api_douyin_list(
                domain=self._str_arg(q, "domain", "tech", maxlen=16) or "tech",
                q=self._str_arg(q, "q", "", maxlen=64) or None,
                blogger=self._str_arg(q, "blogger", "", maxlen=64) or None,
                category=self._str_arg(q, "category", "", maxlen=32) or None,
                topic=self._str_arg(q, "topic", "", maxlen=24) or None,
                stance=self._str_arg(q, "stance", "", maxlen=16) or None,
                offset=self._int_arg(q, "offset", 0, 0, 100000),
                limit=self._int_arg(q, "limit", 30, 1, 200),
                sort=self._str_arg(q, "sort", "date", maxlen=8) or "date",
            ))
        if route == "/api/douyin/detail":
            q = urllib.parse.parse_qs(parsed.query)
            return self._send_json(api_douyin_detail(
                self._str_arg(q, "video_id", "", maxlen=40) or ""))
        if route == "/api/douyin/topics":
            return self._send_json(api_douyin_topics())
        if route == "/api/douyin/stances":
            return self._send_json(api_douyin_stances())
        if route == "/api/douyin/bloggers":
            return self._send_json(api_douyin_bloggers())
        if route == "/api/douyin/plate":
            q = urllib.parse.parse_qs(parsed.query)
            return self._send_json(api_douyin_plate(
                plate=self._str_arg(q, "plate", "", maxlen=32) or ""))
        if route == "/api/douyin/resonance":
            q = urllib.parse.parse_qs(parsed.query)
            return self._send_json(api_douyin_resonance(
                limit=self._int_arg(q, "limit", 40, 1, 60)))

        # ---- B站视频分堆（2026-10-11 王总第三回合：全量吸取 + 市场情绪 + 下周同框）----
        if route == "/api/bili/pile":
            q = urllib.parse.parse_qs(parsed.query)
            ct = self._str_arg(q, "cat", "", maxlen=32) or None
            bl = self._str_arg(q, "blogger", "", maxlen=64) or None
            lim = self._int_arg(q, "limit", 2000, 1, 5000)
            return self._send_json(api_bili_pile(cat=ct, blogger=bl, limit=lim))
        # ---- B站方法卡片（2026-10-11 王总：方法起名 + 回测准确率上卡片）----
        if route == "/api/bili/methods":
            q = urllib.parse.parse_qs(parsed.query)
            ct = self._str_arg(q, "cat", "technical,value", maxlen=32) or "technical,value"
            return self._send_json(api_bili_methods(cat=ct))
        # ---- 一键筛股（2026-10-11 王总：给我最终结论，不要方法解释）----
        if route == "/api/bili/scan":
            q = urllib.parse.parse_qs(parsed.query)
            fc = self._str_arg(q, "force", "", maxlen=8)
            return self._send_json(api_bili_scan(
                force=fc.lower() in ("1", "true", "yes")))
        # ---- B站博主共识结论（2026-10-12 王总：市场情绪面板先给结论——板块方向+个股）----
        if route == "/api/bili/consensus":
            q = urllib.parse.parse_qs(parsed.query)
            ct = self._str_arg(q, "cat", "sentiment", maxlen=32) or "sentiment"
            dy = self._int_arg(q, "days", 30, 3, 365)
            return self._send_json(bili_consensus.digest(cat=ct, days=dy))
        if route == "/api/contest/rounds":
            return self._send_json(api_contest_rounds())
        if route == "/api/contest/detail":
            q = urllib.parse.parse_qs(parsed.query)
            vid = self._str_arg(q, "video_id", "", maxlen=40) or None
            return self._send_json(api_contest_detail(video_id=vid))
        # 2026-09-22 修复 P1-1：未匹配的 /api/... 此前直接 return、什么都不发，
        # 浏览器侧表现为请求挂起/连接被关；现在统一回 404 JSON。
        self._send_json({"error": "not found"}, code=404)

    # P1-39：重写入（含分钟级同步抓取 / 子进程拉起）不进 _WRITE_LOCK，避免加自选/改设置/归档
    # 被长抓取阻塞排队。其互斥已由 INGEST_LOCK + busy_timeout 保证；其余写请求仍串行化。
    HEAVY_POSTS = ("/api/fetch", "/api/login_check", "/api/login/open", "/api/edge/restart")

    def _do_shutdown(self):
        # POST /api/shutdown（P1-51）：先回 200，再异步关闭 serve_forever。
        self._send_json({"ok": True, "message": "正在停止后台服务…"})
        def _stop():
            time.sleep(0.2)
            try:
                self.server.shutdown()
            except Exception:  # noqa: BLE001
                pass
        threading.Thread(target=_stop, name="shutdown", daemon=True).start()

    def do_POST(self):
        _REQ_DEADLINE.value = time.monotonic() + 8  # P2-09 处理时限 8s
        route = urllib.parse.urlparse(self.path).path
        use_lock = route not in self.HEAVY_POSTS
        ctx = _WRITE_LOCK if use_lock else _nullcontext()
        # 串行化写请求：并发写是 SQLite 写锁冲突的根因，串行后各写事务依次完成
        try:
            with ctx:
                return self._do_POST()
        except Exception:  # noqa: BLE001 —— 与 GET 同口径：兜底 500 JSON，不泄露 traceback
            logging.exception("POST %s 处理失败", self.path)
            try:
                self._send_json({"error": "internal error"}, code=500)
            except Exception:  # noqa: BLE001
                pass

    def _do_POST(self):
        # 2026-09-22 修复 P1-3：5 个写接口统一守卫（Host/Origin/Content-Type）
        if not self._guard_write():
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            length = 0
        body = self.rfile.read(length) if length > 0 else b"{}"
        try:
            payload = json.loads(body.decode("utf-8")) if body else {}
        except Exception:  # noqa: BLE001
            # 2026-09-22 修复 P1-3（最危险的一处）：原先 json 解析失败会静默降级成 {} 后照常执行，
            # 即一个畸形请求体也会真实触发抓取/写库。现在如实回 400，不再执行任何动作。
            self._send_json({"error": "invalid json body"}, code=400)
            return
        if not isinstance(payload, dict):
            self._send_json({"error": "json body must be an object"}, code=400)
            return
        parsed = urllib.parse.urlparse(self.path)
        route = parsed.path

        # ── 2026-10-22 收口端点（C 前端已接调用 + 优雅降级）──
        if route == "/api/config/set":
            return self._send_json(api_config_set(payload.get("key"), payload.get("value")))
        if route == "/api/open_dir":
            return self._send_json(api_open_dir(payload.get("dir")))
        if route == "/api/backup":
            return self._send_json(api_backup())
        if route == "/api/backup/restore":
            return self._send_json(api_backup_restore(payload.get("path")))
        if route == "/api/lhb/backfill":
            return self._send_json(api_lhb_backfill(payload))
        if route == "/api/shutdown":
            return self._do_shutdown()

        # ── 2026-10-07 智能选股增强 B6：自然语言选股（server 代理 DeepSeek，key 不落前端）──
        if route == "/api/nl_screener":
            if se is None:
                return self._send_json({"ok": False, "error": "screener_extra 未加载"}, code=500)
            text = (payload.get("text") or "") if isinstance(payload, dict) else ""
            return self._send_json(se.nl_screener(text))
        if route == "/api/settings/deepseek_key":
            if se is None:
                return self._send_json({"ok": False, "error": "screener_extra 未加载"}, code=500)
            key = (payload.get("key") or "").strip()
            if not key:
                return self._send_json({"ok": False, "error": "key 为空"}, code=400)
            se.set_deepseek_key(key)
            return self._send_json({"ok": True})
        # 2026-10-06 修复 P1：/api/prediction/run 此前只注册在 _do_GET，而前端 predRun 用
        # POST 调用 → 「重新生成」按钮一直落 404 兜底（面板数据靠服务端内部调度才正常）。
        # 写接口走 POST 合理（有锁/写库），此处补注册，与 GET 分支同参。
        if route == "/api/prediction/run":
            q = urllib.parse.parse_qs(parsed.query)
            return self._send_json(api_prediction_run(force=self._int_arg(q, "force", 1, 0, 1)))
        if route == "/api/prediction/catchup":
            return self._send_json(api_prediction_catchup())
        # 2026-10-15 修复（10-06 那只坑的同类复发）：/api/pillars/run 之前只注册在 _do_GET，
        # 而前端 predRun（app.js）用 POST 调用 → 「每日预测·重新生成」按钮永远落 404 兜底。
        # 面板本身有数据（GET /api/pillars 正常），所以只表现为"点按钮没反应、顶部报加载失败"。
        # generate() 要写库（pillar_run/pillar_item），走 POST 才对（有 _WRITE_LOCK 串行化）。
        if route == "/api/pillars/run":
            q = urllib.parse.parse_qs(parsed.query)
            return self._send_json(pillars.generate(DB.conn, force=bool(self._int_arg(q, "force", 1, 0, 1))))
        # 2026-10-15 博主共振：build() 要写 7 张表，同样必须走 POST（有 _WRITE_LOCK 串行化）
        if route == "/api/resonance/run":
            q = urllib.parse.parse_qs(parsed.query)
            return self._send_json(resonance.build(DB.conn, force=bool(self._int_arg(q, "force", 1, 0, 1))))
        # 2026-10-16 量化板块：run 同步（与共振 build 同口径；内部含因子面板 + walk-forward，约数秒）
        if route == "/api/quant/run":
            q = urllib.parse.parse_qs(parsed.query)
            return self._send_json(quant.run(DB.conn, force=bool(self._int_arg(q, "force", 1, 0, 1))))
        if route == "/api/quant/recipe":
            return self._send_json(quant.recipe_save(
                DB.conn, payload.get("name") or "", payload.get("weights") or {}))
        # 2026-10-16 量化板块：菜谱「应用」——强制报 OOS 外样 Sharpe/胜率（不单给回测漂亮数字）
        if route == "/api/quant/recipe/apply":
            q = urllib.parse.parse_qs(parsed.query)
            rid = self._int_arg(q, "id", 0, 0, 10 ** 9)
            if rid <= 0:
                return self._send_json({"ok": False, "error": "id 非法"})
            return self._send_json(quant.recipe_apply(DB.conn, rid))
        # 2026-10-17 ETF 基金板块：run 同步（与量化 run 同口径；内部含 ETF 多因子候选计算，候选池空时为诚实空跑）
        if route == "/api/etf/run":
            q = urllib.parse.parse_qs(parsed.query)
            return self._send_json(etf.run(DB.conn, force=bool(self._int_arg(q, "force", 1, 0, 1))))
        # 2026-10-15 自我进化：走后台线程（learn 要跑 10~20 秒，同步会被网关超时掐成 502），
        # 前端启动后轮询 /api/evolve/status 直到 stage 归零。
        if route == "/api/evolve/run":
            q = urllib.parse.parse_qs(parsed.query)
            return self._send_json(evolve.run_async(DB.conn, force=bool(self._int_arg(q, "force", 1, 0, 1))))
        if route == "/api/login_check":
            sites = payload.get("sites") or _sites()
            results = [INGEST.login_check(s) for s in sites]
            return self._send_json({"results": results})
        if route == "/api/login/open":
            # 打开一个「受控登录窗口」：程序自己开的 Edge（带调试端口），用户在该窗口里
            # 登录，capture_login.py 会持续盯梢，等到真正登录成功就自动抓取 cookie 存库。
            # 与「普通新标签页」的根本区别：普通标签页的 cookie 在用户自己的浏览器里，
            # 程序读不到，所以「点此登录」必须走这条路，否则登录完状态永远不会更新。
            site = payload.get("site") or "taoguba"
            script = os.path.join(ROOT, "tools", "capture_login.py")
            if not os.path.exists(script):
                return self._send_json({"ok": False, "error": "capture_login.py 不存在"}, code=500)
            log = os.path.join(ROOT, "artifacts", f"capture_login_{site}.log")
            os.makedirs(os.path.dirname(log), exist_ok=True)
            # 2026-09-24 修复：原代码用 `with open(log, "wb") as fh:` 后 fh 立刻随 with 块
            # 关闭，子进程 stdout 写到关闭的句柄上、子进程立即崩溃 → 日志永远 0 字节、
            # 看起来"没启动"。改为：开一个不随父进程关闭的句柄；父进程退出不影响子进程。
            fh = open(log, "ab", buffering=0)
            try:
                subprocess.Popen(
                    [sys.executable, "-u", script],   # -u：强制无缓冲，让 print 立即 flush
                    cwd=ROOT, stdout=fh, stderr=subprocess.STDOUT,
                    stdin=subprocess.DEVNULL,
                    creationflags=0x00000008 if os.name == "nt" else 0,  # DETACHED_PROCESS
                )
            except Exception as e:  # noqa: BLE001
                return self._send_json({"ok": False, "error": str(e)[:200]}, code=500)
            finally:
                # 2026-10-12 修复句柄泄漏：子进程已继承句柄副本，父进程应立刻关自己的那份；
                # 原实现只在异常分支关闭，成功分支的 fh 一直挂着不释放（连点即泄漏）。
                fh.close()
            return self._send_json({"ok": True, "site": site,
                                    "message": "已打开受控登录窗口，请在其中完成登录；登录成功后会自动保存，窗口请勿关闭。",
                                    "log_path": log})
        if route == "/api/edge/restart":
            # 2026-09-24 新增：接管用户现有 Edge
            # 1) 杀所有 msedge.exe 进程
            # 2) 用用户 profile (--user-data-dir=%LOCALAPPDATA%\Microsoft\Edge\User Data)
            #    + --remote-debugging-port=9333 重启 Edge
            # 3) 同步启动 capture_login.py 盯梢（它会直接接管 9333 上已有的 Edge，不再另起临时 profile）
            # 这样用户在他自己的 Edge 标签页里扫码登录 → cookie 落到用户 profile → capture_login 自动捕获。
            site = (payload.get("site") or "taoguba").strip()[:40]
            with_launch_capture = bool(payload.get("with_capture", True))
            launch_script = os.path.join(ROOT, "tools", "launch_user_edge_with_debug.py")
            if not os.path.exists(launch_script):
                return self._send_json({"ok": False, "error": "launch_user_edge_with_debug.py 不存在"}, code=500)
            log_launch = os.path.join(ROOT, "artifacts", f"edge_restart_{site}.log")
            os.makedirs(os.path.dirname(log_launch), exist_ok=True)
            # 同步运行（最关键步骤，杀 Edge + 重启 + 等 CDP 就绪）：1 同步执行，约 5-10s
            try:
                fh = open(log_launch, "ab", buffering=0)
                proc = subprocess.run(
                    [sys.executable, "-u", launch_script],
                    cwd=ROOT, stdout=fh, stderr=subprocess.STDOUT,
                    stdin=subprocess.DEVNULL,
                    timeout=60,
                )
                fh.close()
            except Exception as e:  # noqa: BLE001
                return self._send_json({"ok": False, "error": str(e)[:200]}, code=500)
            try:
                log_tail = open(log_launch, encoding="utf-8", errors="ignore").read().strip().splitlines()
                last_line = log_tail[-1] if log_tail else ""
            except Exception:
                last_line = ""
            if proc.returncode != 0:
                return self._send_json({
                    "ok": False,
                    "error": f"Edge 重启失败（returncode={proc.returncode}）",
                    "log_tail": last_line[:300],
                }, code=500)
            # 启动 capture_login.py 后台盯梢（会接管 9333 上已启动的 Edge）
            if with_launch_capture:
                capture_script = os.path.join(ROOT, "tools", "capture_login.py")
                if os.path.exists(capture_script):
                    log_cap = os.path.join(ROOT, "artifacts", f"capture_login_{site}.log")
                    try:
                        fh = open(log_cap, "ab", buffering=0)
                        subprocess.Popen(
                            [sys.executable, "-u", capture_script],
                            cwd=ROOT, stdout=fh, stderr=subprocess.STDOUT,
                            stdin=subprocess.DEVNULL,
                            creationflags=0x00000008 if os.name == "nt" else 0,
                        )
                        fh.close()
                    except Exception:
                        pass  # 即使 capture 启动失败，也告诉用户 Edge 已接管成功，让他们手动点「打开登录窗口」
            return self._send_json({
                "ok": True,
                "site": site,
                "message": (
                    "Edge 已用您的 profile 重新启动并启用调试端口。"
                    "新 Edge 会保留您之前的标签页与登录态。"
                    "请到新开的 Edge 里登录淘股吧（微信扫码/短信），"
                    "登录完成后程序会自动捕获 cookie 存库。"
                ),
                "log_path": log_launch,
            })
        if route == "/api/login/import":
            # 2026-09-24 新增：手动导入 cookie（兜底方案）
            # 当 capture_login.py 启动的 Edge 窗口用户没用上（用户在自己的浏览器登录），
            # 或者 taoguba 的 SSO 会话过期需要换新 cookie，最稳的路径是：
            #   1) 用户在自己浏览器 DevTools → Application → Cookies 复制
            #   2) 或者从 "EditThisCookie / Cookie-Editor" 扩展导出 JSON
            #   3) 粘到这里的 textarea，程序解析后存库
            # 接受两种格式：JSON 数组（首选）+ Netscape cookies.txt（兜底）
            site = (payload.get("site") or "taoguba").strip()[:40]
            raw = (payload.get("cookies") or "").strip()
            if not raw:
                return self._send_json({"ok": False,
                                        "error": "缺少 cookies 字段（粘贴 DevTools 复制的 cookie 文本）"},
                                       code=400)
            try:
                from scraper.credentials import save_cookies
            except ImportError:
                return self._send_json({"ok": False, "error": "credentials 模块导入失败"}, code=500)
            # 解析：先试 JSON
            cookies = None
            try:
                parsed = json.loads(raw)
                if isinstance(parsed, list):
                    cookies = parsed
                elif isinstance(parsed, dict):
                    cookies = parsed.get("cookies") or parsed
            except Exception:
                cookies = None
            # 兜底：Netscape cookies.txt（每行：`domain<TAB>flag<TAB>path<TAB>secure<TAB>expiration<TAB>name<TAB>value`）
            if cookies is None:
                cookies = []
                for ln in raw.splitlines():
                    ln = ln.strip()
                    if not ln or ln.startswith("#"):
                        continue
                    parts = ln.split("\t")
                    if len(parts) >= 7:
                        cookies.append({
                            "domain": parts[0],
                            "path": parts[2],
                            "secure": parts[3] == "TRUE",
                            "expires": int(parts[4]) if parts[4].isdigit() else -1,
                            "name": parts[5],
                            "value": parts[6],
                        })
            if not cookies:
                return self._send_json({"ok": False,
                                        "error": "无法解析为 cookie（既不是 JSON 也不是 Netscape 格式）"},
                                       code=400)
            # 校验（每条至少要有 name+value）
            norm = []
            for c in cookies:
                if not isinstance(c, dict):
                    continue
                nm = c.get("name") or c.get("Name")
                vv = c.get("value") or c.get("Value")
                if not nm or vv is None:
                    continue
                item = {"name": str(nm), "value": str(vv)}
                for k in ("domain", "path", "expires", "secure", "httpOnly"):
                    if c.get(k) is not None and c.get(k) != "":
                        item[k] = c[k]
                norm.append(item)
            if not norm:
                return self._send_json({"ok": False,
                                        "error": "解析后没有任何含 name+value 的条目"},
                                       code=400)
            n = save_cookies(site, norm)
            # 立即做一次登录校验，让用户知道导入是否生效
            try:
                chk = INGEST.login_check(site)
                return self._send_json({
                    "ok": True,
                    "site": site,
                    "imported": n,
                    "login_status": chk.get("status"),
                    "login_detail": chk.get("detail", "")[:300],
                })
            except Exception as e:  # noqa: BLE001
                return self._send_json({"ok": True, "site": site, "imported": n,
                                        "warning": f"导入成功但登录校验失败：{e}"})
        if route == "/api/fetch":
            sites = payload.get("sites") or _sites()
            mode = payload.get("mode", "all")
            results = []
            with INGEST_LOCK:   # 与后台自动调度互斥，避免并发写库
                # 2026-09-25 优化：站点间改为并发抓取（DB 已 WAL + 每线程独立连接），
                # 避免最慢的雪球浏览器抓取串行阻塞同花顺等其它来源，整轮耗时≈最慢一站。
                max_workers = min(len(sites), int(os.environ.get("INGEST_MAX_WORKERS", "4")))
                with cf.ThreadPoolExecutor(max_workers=max_workers) as ex:
                    futs = {ex.submit(INGEST.ingest, s, mode): s for s in sites}
                    for fut in cf.as_completed(futs):
                        s = futs[fut]
                        try:
                            res = fut.result()
                            results.append(res)
                            # 逐来源记录「成功刷新」（Request G）——「最后更新」列的判定依据。
                            DB.record_refresh(s, "ok", _ingest_item_count(res),
                                              SITE_DISPLAY_NAMES.get(s, s))
                        except Exception as e:  # noqa: BLE001
                            err = str(e)[:200]
                            results.append({"site": s, "error": err})
                            # 失败也留痕：更新 last_attempt_at 与 error，但**保留上次成功时刻**
                            # （record_refresh 在 error 分支不动 last_ok_at）。
                            DB.record_refresh(s, "error", 0, SITE_DISPLAY_NAMES.get(s, s), error=err)
            # 选手池重建（行为证据版）：整轮抓取后跨站从真实发帖/交易/持仓
            # 推导补齐入选名单至 ~200（东财/同花顺无公开选手榜，只能行为反推）。
            try:
                pool = INGEST.rebuild_player_pool()
                results.append({"site": "__player_pool__", **pool})
                logging.info("选手池重建: %s", pool)
            except Exception as e:  # noqa: BLE001
                logging.error("选手池重建失败: %s", e)
            # 整轮汇总（__all__）：只要有任一来源失败即记为 error —— 代表这一轮抓取
            # 拿到的数据不完整地新鲜，「刷新状态标识」据此展示为「刷新失败」。
            failed = [r for r in results if isinstance(r, dict) and r.get("error")]
            DB.record_refresh(
                "__all__", "error" if failed else "ok", len(results),
                SITE_DISPLAY_NAMES["__all__"],
                error="；".join(f"{r.get('site')}: {r.get('error')}" for r in failed)[:300],
            )
            # 2026-09-22 修复（P0-2 第 4 点）：/api/fetch 不清空数据，故不加 confirm 门槛，
            # 但把执行时刻写进 meta，使「这次抓取是什么时候跑的」可被追溯（成功/失败都记）。
            DB.set_meta("last_fetch_at", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
            return self._send_json({"results": results, "summary": api_summary()})
        if route == "/api/rate/snapshot":        # 2026-10-05 入手概率榜归档（前端带回 rows，秒回）
            # 归档是纯写库，异常不外抛 traceback（外层 do_POST 已统一记账），只回可读原因。
            rows = payload.get("rows")
            rs = payload.get("src")
            td = payload.get("trade_date")
            try:
                return self._send_json(api_rate_snapshot(
                    rows=rows if isinstance(rows, list) else None,
                    src=str(rs)[:16] if rs else "auto",
                    trade_date=str(td)[:10] if td else ""))
            except Exception:  # noqa: BLE001
                return self._send_json({"ok": False, "reason": "归档失败，请查看服务端日志"}, code=500)
        if route == "/api/rate/buypoint":    # 2026-10-05 手填「最值得入手价」（失焦即存）
            code = payload.get("code")
            price = payload.get("price")
            try:
                price = None if price in (None, "", "null") else float(price)
            except Exception:  # noqa: BLE001
                return self._send_json({"ok": False, "reason": "买点价不是数字"}, code=400)
            if code and not isinstance(code, str):
                return self._send_json({"ok": False, "reason": "代码格式不对"}, code=400)
            r = screener.set_buy_manual(code, price)
            return self._send_json(r, code=200 if r.get("ok") else 400)
        if route == "/api/scheduler/toggle":
            enabled = _truthy(payload.get("enabled"))
            state = _sched_load_state()
            state["enabled"] = enabled
            _sched_save_state(state)
            return self._send_json(api_scheduler_status())
        if route == "/api/kb/reload":
            # 知识库是只读文档型数据，重吸取只覆盖 kb_doc 一张表（且哈希未变时连写都不写），
            # 不触碰任何行情/持仓数据，故与 /api/fetch 同级、无需 confirm 门槛。
            key = str(payload.get("key") or "").strip()[:40] or None
            return self._send_json(kb_reload(key, force=_truthy(payload.get("force"))))
        if route == "/api/overseas":
            live = bool(payload.get("live", True))
            reset = bool(payload.get("reset", True))
            if reset:
                # 重置海外舆情会清空海外业务数据（overseas_news/overseas_hotspots/
                # overseas_sources_status 等），因此要求显式 confirm，并在执行前做一份整库快照。
                if not _truthy(payload.get("confirm")):
                    return self._send_json(_destructive_payload(), code=409)
                try:
                    snap = DB.snapshot(prefix="app.db.pre-overseas", keep=5)
                    DB.set_meta("last_overseas_backup", snap)
                    DB.set_meta("last_overseas_at", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
                except Exception as e:  # noqa: BLE001
                    # 宁可不清空也不裸清：快照失败即中止本次破坏性操作（回「未执行」）。
                    logging.exception("海外舆情前置快照失败")
                    return self._send_json(_backup_failed_payload(str(e)), code=500)
            try:
                res = api_overseas_collect(live=live, reset=reset)
            except Exception as e:  # noqa: BLE001
                logging.exception("海外舆情抓取失败")
                return self._send_json({"ok": False, "error": f"海外舆情抓取失败：{e}"}, code=500)
            return self._send_json({"result": res, "summary": api_summary()})
        if route == "/api/sync_names":
            syms = payload.get("symbols") or None
            online = payload.get("online", True)
            force = payload.get("force", False)
            try:
                return self._send_json(api_sync_names(syms, online=bool(online), force=bool(force)))
            except Exception as e:  # noqa: BLE001
                logging.exception("补全股票名称失败")
                return self._send_json({"ok": False, "error": f"补全股票名称失败：{e}"}, code=500)
        if route == "/api/extref_manual_import":
            src_key = (payload.get("src_key") or "").strip()[:64]
            text = payload.get("text") or ""
            default_date = (payload.get("default_date") or "").strip()[:10] or None
            is_csv = payload.get("is_csv", None)
            run_backtest = bool(payload.get("run_backtest", True))
            return self._send_json(api_extref_manual_import(
                src_key=src_key, text=text, default_date=default_date,
                is_csv=is_csv, run_backtest=run_backtest))
        if route == "/api/extref_mcp_connect":
            src_key = (payload.get("src_key") or "").strip()[:64]
            data = payload.get("payload") or payload.get("data") or None
            run_backtest = bool(payload.get("run_backtest", True))
            api_key = (payload.get("api_key") or "").strip()[:256] or None
            return self._send_json(api_extref_mcp_connect(
                src_key=src_key, payload=data, run_backtest=run_backtest, api_key=api_key))
        # ---- 自选 / 关注池 + 到价提醒（2026-09-27 新增）----
        # 2026-10-14 性能修复：任一会改变关注池 / 分组的写入成功后，立即清掉 watch_list 缓存，
        # 保证下次加载（或前端乐观更新后的兜底刷新）拿到的是新数据，而非 5 分钟内的旧快照。
        if route in ("/api/watch_item_add", "/api/watch_item_update", "/api/watch_item_remove",
                     "/api/watch_item_reorder", "/api/watch_item_group_remove",
                     "/api/watch_group_add", "/api/watch_group_rename",
                     "/api/watch_group_remove", "/api/watch_group_reorder", "/api/watch_check"):
            handler = {
                "/api/watch_item_add": api_watch_item_add,
                "/api/watch_item_update": api_watch_item_update,
                "/api/watch_item_remove": api_watch_item_remove,
                "/api/watch_item_reorder": api_watch_item_reorder,
                "/api/watch_item_group_remove": api_watch_item_group_remove,
                "/api/watch_group_add": api_watch_group_add,
                "/api/watch_group_rename": api_watch_group_rename,
                "/api/watch_group_remove": api_watch_group_remove,
                "/api/watch_group_reorder": api_watch_group_reorder,
                "/api/watch_check": api_watch_check,
            }[route]
            r = handler(payload) if route != "/api/watch_check" else handler()
            if r.get("ok"):
                _api_cache_invalidate("watch_list")
            return self._send_json(r)
        if route == "/api/watch_alert_read":
            return self._send_json(api_watch_alert_read(payload))
        self._send_json({"error": "not found"}, code=404)

    def log_message(self, *args):
        pass


class _SingleInstanceHTTPServer(ThreadingHTTPServer):
    """禁止「同一端口被两个实例同时绑上」。

    2026-09-22 修复（P0-3）：此前实测有两个 `python -m viewer.server` 同时在跑、
    都成功绑定了 8765 —— Windows 的 SO_REUSEADDR 语义允许第二个套接字绑同一端口
    且不报错，于是两个进程都能收流量、各自持有 DB 连接，表现为「同一次刷新看到的
    数据在两次之间跳变」。Python 的 HTTPServer 默认 `allow_reuse_address = True`
    正是这个隐患的来源。

    这里（推荐方案）显式关闭地址复用，并在 Windows 上追加 SO_EXCLUSIVEADDRUSE
    （该选项本身就是为「独占绑定、拒绝他人复用同一端口」而设的最强保证）。
    选择它而非命名互斥体的理由：它直接在**绑定端口**这一步拦住冲突，语义与
    「端口被占用」完全一致，且不需要额外持有内核句柄、无 Windows API 依赖，
    在 Linux/macOS 上同样生效（allow_reuse_address=False 即报 EADDRINUSE）。
    """
    allow_reuse_address = False
    # 2026-10-15 全站回测评测修复（真·「偶发打不开 / 白屏」根因）：
    # socketserver 的 request_queue_size 默认只有 5（监听 backlog），而查看器首屏要**并发**
    # 拉 10+ 个静态资源（index.html / app.css / app.js / news_rules.js / screener_extra.js /
    # industry_leaders.js / bili_methods.css / visual-upgrade.css …，实测同屏 14 个 HTTP 请求）。
    # 超过 backlog 的连接会被操作系统**直接拒绝**，浏览器报 ERR_CONNECTION_REFUSED；
    # 而静态资源加载失败后浏览器**不重试** → 界面残缺 / 无样式 / 白屏，
    # 手动刷新一次又可能正常 —— 这正是「打开软件有时白屏、重开又好了」这类随机故障的来源。
    # 实测：并发 14 个请求稳定失败 9 个；提升 backlog 后 3 轮全成功。
    # 这也是本机既有「打不开 / 白屏」排查经验里最容易被忽略的一条：进程活着、端口在听，
    # 单发 curl 也 200，只有**并发**才暴露。故不改成 1 个都不失败不算修好。
    request_queue_size = 128

    def server_bind(self):
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):  # 仅 Windows 存在
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


def _port_listening(port: int) -> bool:
    """端口上是否**真的有服务在监听**（用于区分「被别人占用」与「处于 TIME_WAIT」）。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.6)
        return s.connect_ex(("127.0.0.1", port)) == 0


def create_server(host: str = "127.0.0.1", port: int | None = None) -> _SingleInstanceHTTPServer:
    """**同步**创建并绑定端口，返回已就绪的 server 实例；绑定失败抛 OSError。

    2026-09-22 收口（P0-3）：把「创建 + 绑定」从 main() 抽出来，供 main() 与
    desktop_app.py / launch.py 共用。这样 desktop 版可以在**主线程、开窗之前**
    先把端口绑好——绑定失败时直接提示并以退出码 1 结束，绝不出现「窗口已经开出来、
    后端却没起来」的空壳窗口（旧版把 bind 放在守护线程里，失败被静默吞掉）。

    重试策略（仅在「确实没人在听」时重试）：
      - bind 失败后先探测 `_port_listening(port)`：
          · 为真 → 端口确实被别的进程占用，**立刻上抛、不重试**（避免把真实冲突
            掩盖成偶发失败）；
          · 为假 → 大概率是自家上一个进程 Ctrl+C 后的 TIME_WAIT 尚未回收，重试
            3 次 × 0.5s，仍失败才上抛。
    """
    p = int(port if port is not None else PORT)
    last: OSError | None = None
    for attempt in range(3):
        try:
            return _SingleInstanceHTTPServer((host, p), Handler)
        except OSError as e:
            last = e
            if _port_listening(p):          # 真有人占着 → 真实冲突，不重试
                raise
            if attempt < 2:                 # 无人在听 → 多半 TIME_WAIT，短等再试
                time.sleep(0.5)
    assert last is not None
    raise last


def report_bind_failure(port: int, err: Exception) -> int:
    """统一打印「启动失败」中文提示（区分占用 / TIME_WAIT），返回退出码 1。

    main() 与 desktop_app.py 共用同一份文案与退出码，保证两条启动路径行为一致。
    """
    if _port_listening(port):
        print(f"[查看器] 启动失败：端口 {port} 已被占用（已有查看器实例或其它程序在监听）。")
        print(f"[查看器] 请先关闭已运行的实例，或结束占用 {port} 的进程；"
              f"也可换端口启动：  set VIEWER_PORT=8766 && python -m viewer.server")
    else:
        print(f"[查看器] 启动失败：绑定端口 {port} 失败，但当前无进程在监听"
              f"（多为系统 TIME_WAIT 回收未完成，已自动重试 3 次仍失败）。")
        print(f"[查看器] 请等待数秒后重试，或换端口启动："
              f"  set VIEWER_PORT=8766 && python -m viewer.server")
    print(f"[查看器] 系统错误：{err}")
    return 1


_SERVER_PID_FILE = os.path.join(ROOT, "viewer", ".server.pid")


def _setup_logging() -> None:
    """日志统一落盘 viewer/logs/server-YYYYMMDD.log（UTF-8，按日滚动 5MB×5 份）。

    修复（2026-09-28 体检报告 P1「日志丢失 + 中文乱码」）：
    旧实现仅 logging.basicConfig 挂 stderr，桌面静默启动（无控制台）时日志全部丢弃，
    且若被按 GBK 解读会乱码。改为强制 UTF-8 文件落盘，无控制台也不丢排查线索。
    """
    try:
        log_dir = os.path.join(ROOT, "viewer", "logs")
        os.makedirs(log_dir, exist_ok=True)
        log_path = os.path.join(log_dir, "server-%s.log" % datetime.now().strftime("%Y%m%d"))
        log_path_abs = os.path.abspath(log_path)
        fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s", "%Y-%m-%d %H:%M:%S")
        root = logging.getLogger()
        # 去重：已有等价文件 handler 则不重复添加（防止多次调用叠加）
        if any(isinstance(h, RotatingFileHandler) and getattr(h, "baseFilename", "") == log_path_abs
               for h in root.handlers):
            return
        fh = RotatingFileHandler(log_path, encoding="utf-8", maxBytes=5 * 1024 * 1024, backupCount=5)
        fh.setFormatter(fmt)
        fh.setLevel(logging.INFO)
        root.addHandler(fh)
        # 控制台：仅当确有 tty（非桌面静默启动）时挂 stderr，避免无控制台时噪音
        try:
            is_tty = sys.stderr is not None and os.isatty(sys.stderr.fileno())
        except Exception:
            is_tty = False
        has_console = any(isinstance(h, logging.StreamHandler) and not isinstance(h, RotatingFileHandler)
                          for h in root.handlers)
        if is_tty and not has_console:
            sh = logging.StreamHandler(sys.stderr)
            sh.setFormatter(fmt)
            sh.setLevel(logging.INFO)
            root.addHandler(sh)
        if root.level == logging.NOTSET or root.level > logging.INFO:
            root.setLevel(logging.INFO)
    except Exception as e:  # noqa: BLE001
        print(f"[查看器] 日志初始化失败（不影响服务）：{e}", file=sys.stderr)


# ══════════════════════════════════════════════════════════════════════════
# 2026-10-16 代码指纹 + 旧后端接管
#   治「改了代码、双击图标重启，实际仍跑旧代码」：desktop_app 原来只要端口有人听就
#   直接复用；而 /api/version 的 version 字段是实时读磁盘 index.html 得到的，永远显示
#   最新，判断不出后端代码是旧的。于是旧进程一直霸着端口，新增路由（如 /api/quant）
#   在老进程里不存在 → 404。
#   现在：进程启动时把「关键文件 (mtime,size) 指纹」定格并通过 /api/version 暴露；
#   新进程启动发现端口被占时，先探测对端指纹 —— 不同就终止旧后端并接管。
# ══════════════════════════════════════════════════════════════════════════
_BUILD_ID = _buildid.of_disk(ROOT)      # 本进程启动时刻的代码指纹（定格，不随文件变动）
_STARTED_AT = time.strftime("%Y-%m-%d %H:%M:%S")
_TAKEOVER_LOG = os.path.join(str(ROOT), "viewer", ".restart.log")


def _probe_running_server(port: int, timeout: float = 2.0) -> dict:
    """探测 port 上是否已有查看器后端在跑；返回其 /api/version 字典，失败返回 {}。

    能拿到 dict 即证明「对端是本项目的 server」（该响应形状是本地约定），
    因此可以据此安全地做「复用 / 接管」判断，而不是只看端口有没有人在听。
    """
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/version", timeout=timeout) as r:
            data = json.loads(r.read().decode("utf-8", "replace"))
        return data if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001 —— 探测失败一律当「不可识别的后端」
        return {}


def _pid_image_name(pid) -> str:
    """取 PID 的可执行文件名（仅 Windows 有意义）；失败返回空串。

    用途：接管旧后端前的最后一道保险 —— 只对 python 解释器进程动手，
    避免误杀恰好占了同端口的无关程序。
    """
    if os.name != "nt":
        return ""
    try:
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        h = k32.OpenProcess(0x1000, False, int(pid))   # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return ""
        try:
            buf = ctypes.create_unicode_buffer(1024)
            size = ctypes.c_uint(1024)
            if k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
                return os.path.basename(buf.value)
        finally:
            k32.CloseHandle(h)
    except Exception:  # noqa: BLE001
        pass
    return ""


def _terminate_pid(pid, wait_port=None, timeout: float = 8.0) -> bool:
    """终止指定 PID；wait_port 非空时再等端口释放。返回是否达成。"""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if not pid or pid == os.getpid():
        return False
    ok = False
    try:
        if os.name == "nt":
            k32 = ctypes.WinDLL("kernel32", use_last_error=True)
            h = k32.OpenProcess(0x0001, False, pid)    # PROCESS_TERMINATE
            if not h:
                return False
            try:
                ok = bool(k32.TerminateProcess(h, 1))
            finally:
                k32.CloseHandle(h)
        else:
            import signal
            os.kill(pid, signal.SIGTERM)
            ok = True
    except Exception:  # noqa: BLE001
        return False
    if not ok:
        return False
    if wait_port is None:
        return True
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not _port_in_use(wait_port):
            return True
        time.sleep(0.2)
    return not _port_in_use(wait_port)


def _takeover_stale_server() -> bool:
    """端口被占用时：若占用者是「跑着旧代码的自家后端」，终止它并让位，返回 True。

    判定与处置：
      ① 读对端 /api/version：
         · 拿不到 → 不是本项目的 server（或极旧版本）→ 不接管，返回 False（保留老行为）；
         · build_id == 本进程指纹 → 同一份代码，真·重复实例 → 不接管，返回 False；
         · 不一致（含对端还没有该字段）→ 旧代码 → 走 ②。
      ② 只对「镜像名含 python」的进程动手，终止后等端口释放。
    任何一步失败都回退为老行为（打印占用提示并退出），不阻断正常启动。
    排障时可设 VIEWER_NO_TAKEOVER=1 关闭接管。
    """
    if os.environ.get("VIEWER_NO_TAKEOVER"):
        return False
    info = _probe_running_server(PORT)
    if not info:
        return False
    if info.get("build_id") == _BUILD_ID:
        logging.info("[查看器] 端口 %s 上已是最新代码的后端（build=%s），本次退出。", PORT, _BUILD_ID)
        return False
    pid = info.get("pid")
    img = _pid_image_name(pid)
    if not pid or "python" not in img.lower():
        logging.warning("[查看器] 端口 %s 上的后端是旧代码（build=%s），但无法安全接管"
                        "（pid=%s 镜像=%r）—— 保留旧行为，请手工结束该进程。",
                        PORT, info.get("build_id"), pid, img or "未知")
        return False
    logging.warning("[查看器] 端口 %s 上跑的是旧代码（运行 build=%s ≠ 磁盘 build=%s，pid=%s），"
                    "正在接管重启…", PORT, info.get("build_id"), _BUILD_ID, pid)
    ok = _terminate_pid(pid, wait_port=PORT, timeout=8.0)
    try:
        with open(_TAKEOVER_LOG, "a", encoding="utf-8") as f:
            f.write("[%s] 接管旧后端：build %s -> %s，pid=%s，结果=%s\n"
                    % (time.strftime("%Y-%m-%d %H:%M:%S"), info.get("build_id"), _BUILD_ID, pid,
                       "成功" if ok else "失败"))
    except OSError:
        pass
    if ok:
        logging.warning("[查看器] 旧后端（pid=%s）已让位，本进程接管端口 %s。", pid, PORT)
    else:
        logging.warning("[查看器] 终止旧后端（pid=%s）失败或端口未释放，本次退出。", pid)
    return ok


def _port_in_use(port: int) -> bool:
    """本机 127.0.0.1:port 是否已有进程在监听（只读探测，无副作用）。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) == 0


def _acquire_server_instance() -> bool:
    """单实例守护：防止**同一端口**上出现两个查看器（体检报告 P0 双进程）。

    2026-10-12 修正：#06 整改后曾用「PID 文件里的进程是否存活」裁决，但 PID 文件是全局的、
    不分端口 —— 于是任何在**其它端口**拉起的第二实例（如护栏 verify_prediction 的 8813）
    都会被误挡，护栏报「后端起不来」。改为**按端口裁决**：只有「本进程要用的这个端口」
    已被监听时才退出。同端口同时启动的竞态由 bind 自身兜住（EADDRINUSE → report_bind_failure）。
    仍写 PID 文件，但仅作记录，不再据它决定生死（Windows 上 `os.kill(pid, 0)` 实为
    发送 CTRL_C_EVENT，不可用于探活，故已彻底弃用该写法）。
    返回 True=本进程可继续；False=本端口已有实例，应退出。
    """
    try:
        if _port_in_use(PORT):
            # 2026-10-16：端口被占用 ≠ 「已有最新实例」—— 可能是一个**跑着旧代码**的后端
            # （改了 .py 后没真正重启，新路由/新板块接口会 404）。先比对代码指纹：
            # 不是同一份代码就终止它并让位（详见 _takeover_stale_server）。
            if not _takeover_stale_server():
                print(f"[查看器] 端口 {PORT} 已有后端在监听，本次退出以避免重复实例抢端口。")
                return False
        with open(_SERVER_PID_FILE, "w", encoding="utf-8") as f:
            f.write(f"{os.getpid()}\n")
    except Exception:  # noqa: BLE001
        pass  # 锁文件写不了也不阻断启动
    return True


def _write_news_rules() -> None:
    """把 viewer/newsclass.py 的规则表重写出 static/news_rules.js（启动一次）。"""
    here = os.path.dirname(os.path.abspath(__file__))
    out = os.path.join(here, "static", "news_rules.js")
    newsclass.write_js(out)


def _check_remote_backup_dir() -> tuple:
    """D1 异地双副本（2026-10-13）启动校验：异地备份目录是否可写。

    异地副本配置读取优先级：环境变量 DB_BACKUP_REMOTE_DIR > 默认 D:/stock-backups
    （网盘/外接盘同步目录，盘挂不全丢）。tools/db_backup.py 会把同一份日备份
    同时写入本地 viewer/backups/ 与异地 BACKUP_MIRROR 目录，保留策略一致
    （30 天日备份 + 每周全量）。异地不可写时只告警不阻断启动 —— 数据仍有本地副本，
    避免「备份目录没挂载就拒绝启动」。
    返回 (writable, remote_dir)。
    """
    try:
        d = os.environ.get("DB_BACKUP_REMOTE_DIR", "D:/stock-backups")
        try:
            os.makedirs(d, exist_ok=True)
            writable = os.access(d, os.W_OK)
        except Exception:  # noqa: BLE001
            writable = False
        return writable, d
    except Exception:  # noqa: BLE001
        return False, ""




# ════════════════════════════════════════════════════════════════════════════
# 2026-10-22 收口端点实现（供 _do_GET / _do_POST 调用；C 前端已接调用 + 优雅降级）
# ════════════════════════════════════════════════════════════════════════════
import shutil as _shutil


def _cfg_key(key: str) -> str:
    return "cfg:" + (key or "")


def api_config_get(key: str) -> dict:
    """GET /api/config/get：读一项用户配置。先查用户覆盖（meta），回退 AppConfig 默认。"""
    key = (key or "").strip()
    if not key:
        return {"ok": False, "error": "key 为空"}
    try:
        from config import load_config
        cfg = load_config()
        raw = DB.get_meta(_cfg_key(key))
        default = cfg.as_dict().get(key, None)
        return {"ok": True, "key": key, "value": raw, "default": default}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)[:200]}


def api_config_set(key: str, value) -> dict:
    """POST /api/config/set：持久化一项用户配置到 meta（并同步 user_config.json）。"""
    key = (key or "").strip()
    if not key:
        return {"ok": False, "error": "key 为空"}
    try:
        DB.set_meta(_cfg_key(key), json.dumps(value, ensure_ascii=False))
        try:
            from config import USER_CONFIG_PATH
            overrides = {}
            if USER_CONFIG_PATH.exists():
                overrides = json.loads(USER_CONFIG_PATH.read_text(encoding="utf-8"))
            overrides[key] = value
            USER_CONFIG_PATH.write_text(json.dumps(overrides, ensure_ascii=False, indent=1),
                                        encoding="utf-8")
        except Exception:  # noqa: BLE001
            logging.exception("同步 user_config.json 失败（不影响 meta 落盘）")
        return {"ok": True, "key": key}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)[:200]}


def api_open_dir(dirname: str | None = None) -> dict:
    """POST /api/open_dir：打开日志/备份目录（P3-17）。"""
    target = ROOT / "viewer" / "logs"
    if dirname in ("backup", "backups"):
        target = ROOT / "viewer" / "backups"
    elif dirname in ("data",):
        target = ROOT / "data"
    try:
        target.mkdir(parents=True, exist_ok=True)
        if os.name == "nt":
            os.startfile(str(target))  # type: ignore[attr-defined]
        else:
            import subprocess as _sp
            _sp.Popen(["xdg-open", str(target)])
        return {"ok": True, "dir": str(target)}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)[:200]}


def api_backup() -> dict:
    """POST /api/backup：把当前 app.db（含 -wal/-shm）快照到 backups/。"""
    try:
        src_p = Path(DB.path)
        if not src_p.exists():
            return {"ok": False, "error": "app.db 不存在"}
        stamp = time.strftime("%Y%m%d-%H%M%S")
        out_dir = ROOT / "viewer" / "backups"
        out_dir.mkdir(parents=True, exist_ok=True)
        dst = out_dir / ("app.db.%s.bak" % stamp)
        _shutil.copy2(src_p, dst)
        for ext in ("-wal", "-shm"):
            pe = src_p.parent / (src_p.name + ext)
            if pe.exists():
                _shutil.copy2(pe, out_dir / (dst.name + ext))
        try:
            _mod = sys.modules.get("viewer.server", sys.modules[__name__])
            if hasattr(_mod, "_rotate_backups"):
                _mod._rotate_backups()
        except Exception:  # noqa: BLE001
            pass
        return {"ok": True, "path": str(dst), "size": dst.stat().st_size}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)[:200]}


def api_backup_restore(path: str | None = None) -> dict:
    """POST /api/backup/restore：从备份恢复 app.db（恢复前先自动备份当前）。"""
    if not path:
        return {"ok": False, "error": "未指定备份路径"}
    p = Path(path)
    allowed = ROOT / "viewer" / "backups"
    if not p.exists() or (allowed != p.parent and allowed not in p.parents):
        return {"ok": False, "error": "仅允许从 viewer/backups 恢复"}
    try:
        snap = api_backup()
        src_p = Path(DB.path)
        _shutil.copy2(p, src_p)
        for ext in ("-wal", "-shm"):
            pe = p.parent / (p.name + ext)
            if pe.exists():
                _shutil.copy2(pe, src_p.parent / (src_p.name + ext))
        return {"ok": True, "restored_from": str(p), "pre_restore_backup": snap.get("path")}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)[:200]}


def api_lhb_backfill(payload: dict | None = None) -> dict:
    """POST /api/lhb/backfill：后台回填龙虎榜 3 年历史（需联网；端点+任务骨架）。

    实际回填依赖东财 lhb 接口联网抓取；本端点只接收请求并启动后台任务，
    返回「已启动」。联网验证见 P1-29 / 体检报告 P1。
    """
    payload = payload or {}
    try:
        def _job():
            try:
                import lhb_daily as _lhb
                if hasattr(_lhb, "backfill_3y"):
                    _lhb.backfill_3y()
                elif hasattr(_lhb, "backfill"):
                    _lhb.backfill()
                else:
                    logging.warning("lhb_daily 无 backfill 入口，回填未执行（需确认接口）")
            except Exception:  # noqa: BLE001
                logging.exception("龙虎榜回填任务失败（需联网）")
        threading.Thread(target=_job, name="lhb-backfill", daemon=True).start()
        return {"ok": True, "started": True,
                "note": "龙虎榜 3 年历史回填已启动（后台；需联网，失败请查看日志）"}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)[:200]}

def main():
    _setup_logging()
    # P1-45：经 main() 启动的服务默认启用随机写接口 token
    #（import 作为库时不启用，保证测试 / 非浏览器客户端兼容）
    global _LOCAL_TOKEN
    if not _LOCAL_TOKEN:
        try:
            from config import generate_token, save_token as _save_tok
            _tok = generate_token()
            _save_tok(_tok)
            _LOCAL_TOKEN = _tok
        except Exception:  # noqa: BLE001
            logging.exception("生成写接口 token 失败（保持关闭）")
    if not _acquire_server_instance():
        sys.exit(0)
    boot()  # P0-05：启动期副作用（schema + 调度 + 预热）统一收口，仅由 main() 调用
    # 2026-10-01 口径自查：新闻/公告分类规则统一真源（viewer/newsclass.py）。
    # 每次启动把规则表重写出 static/news_rules.js，前端读它，避免前后端各维护一份。
    _write_news_rules()
    # D1 异地双副本（2026-10-13）：启动校验异地备份目录（默认 D:/stock-backups 网盘/外接盘同步目录）。
    # db_backup.daily_backup 会把每日备份同时镜像到 REMOTE_BACKUP/BACKUP_MIRROR 目录，分离本地与异地。
    _rmt_ok, _rmt_dir = _check_remote_backup_dir()
    if _rmt_ok:
        logging.info("[查看器] 异地备份目录可用：%s（REMOTE_BACKUP，每日备份将镜像到该 BACKUP_MIRROR）",
                     _rmt_dir)
    else:
        logging.warning("[查看器] 异地备份目录不可用：%s —— 数据仍保留本地 viewer/backups 副本，"
                        "请确认网盘/外接盘（stock-backups）已挂载后再重启，否则缺失异地副本",
                        _rmt_dir or "未配置")
    try:
        srv = create_server()
    except OSError as e:
        # WinError 10048 / EADDRINUSE：端口已被另一个查看器实例占用。
        # 明确提示并退出码 1，避免出现「两个实例各收一半流量、数据来回跳变」。
        sys.exit(report_bind_failure(PORT, e))
    logging.info("[查看器] 已启动： http://127.0.0.1:%s", PORT)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        srv.shutdown()


if __name__ == "__main__":
    main()
