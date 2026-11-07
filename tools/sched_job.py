# -*- coding: utf-8 -*-
"""把调度里的「重活」放到**独立进程**里跑（2026-10-15）。

为什么必须这么做（实测证据，不是推测）：
    查看器是单进程多线程（ThreadingHTTPServer）。任何一个**纯 Python** 的长任务都会长时间
    占住 GIL → 同进程**所有** HTTP 请求一起卡住。实测数据：
      · pillars_monday_refresh 每 10s 被调一次、单次 36.7s → 全站持续卡顿
        （缓存命中、本该 0.03s 的接口，实际 1.5~15s，并间歇冻结 30s+）
      · 重启后第一轮调度 110.9s：predictor 79.8s + 四支柱 22.4s + 博主回算 7.7s
      · hotspots 预热 138s、全市场日 K 预热 165s
    这就是进哥说的「点个股弹出来特别慢」的真实成因 —— 不是某只股票的接口慢，
    而是后台重活把整个服务冻住了。
    ⚠️ I/O 型任务（网络 / 文件）不会占 GIL，纯计算型才会；所以只挪纯计算型。

设计：
    · 子进程**直接复用原函数**，各任务自带的节流/幂等/失败重试逻辑一行都不用改；
    · 父进程只负责「同一任务别叠着跑」+ 「启动失败就记日志」（见 server.py 的 _spawn_offproc）；
    · 输出追加到 viewer/logs/sched-<job>.log；退出码 0=成功、1=失败（父进程记 WARNING）；
    · 失败时子进程同样不会写"已完成"标记 —— 保持"失败不算已处理"的纪律。

用法：python tools/sched_job.py <job>
"""
import io
import os
import sys
import time
import traceback

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _p in (ROOT, os.path.join(ROOT, "viewer")):
    if _p not in sys.path:
        sys.path.insert(0, _p)


def _con():
    from viewer.db import Database
    return Database()


def _predictor_daily():
    import predictor
    return predictor.maybe_daily(_con().conn)


def _pillars_daily():
    import pillars
    return pillars.maybe_daily(_con().conn)


def _lhb_daily():
    import lhb_daily
    # 2026-10-30 D33：必须调**同步**入口 `run_daily_sync`，不能用历史上的 `maybe_daily`
    # 的旧语义（起 daemon 线程后立即返回）。本文件是个**短命子进程**：print 完就 return 0 退出，
    # CPython 关解释器时会把 daemon 线程直接杀掉 → 一轮都没跑、record_result 从未执行。
    # 实测这个坑从 2026-10-19 起吃了 11 天（191 次 {'started'} 全是 0.1s，数据一直没更新）。
    # 返回值带 ok：本文件 main() 会把 `ok is False` 判成 rc=1，父进程据此退避重试。
    return lhb_daily.run_daily_sync(_con().conn)


def _resonance_build():
    import resonance
    return resonance.maybe_build(_con().conn)


def _selfpick_build():
    import selfpick
    return selfpick.maybe_build(_con().conn)


def _evolve_daily():
    import evolve
    return evolve.maybe_daily(_con().conn)


def _bili_scan():
    import bili_scan
    # 2026-10-30 D33：改调**同步**入口。原 `maybe_daily()` 有两层病：
    #   ① 函数体里赋值 `_THREAD` 却没写 `global` → 局部名 → 读它必抛 UnboundLocalError
    #      → 被 `except: return False` 吞掉 → 这个入口**从上线起一次都没能触发扫描**；
    #   ② 就算补上 global，它仍是「起 daemon 线程后立即 return」，而本文件是短命子进程，
    #      线程会被 CPython 关解释器时杀掉（与 lhb_daily 同一个病）。
    # 现在 `run_daily_sync()` 同步跑完（实测真实一轮约 1575s，_JOB_TIMEOUT 默认 3600s 兜底），
    # 返回值带 `ok`：本文件 main() 会把 `ok is False` 判成 rc=1，父进程据此退避重试。
    return bili_scan.run_daily_sync()


def _hotspots():
    """热点聚合（实测单次 138s 纯 Python）。

    父进程的 _TTL_CACHE 是进程内的，子进程填不进去 —— 所以这里把结果**落盘**，
    父进程启动预热时读这个文件补缓存（见 server.py 的 _hotspots_prewarm）。
    """
    import json
    from viewer.hotspots import build_hotspots
    payload = build_hotspots(_con())
    out = os.path.join(ROOT, "viewer", "data", "hotspots_cache.json")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    tmp = out + ".tmp"
    with io.open(tmp, "w", encoding="utf-8") as f:
        json.dump({"at": time.strftime("%Y-%m-%d %H:%M:%S"), "payload": payload},
                  f, ensure_ascii=False)
    os.replace(tmp, out)          # 原子替换：父进程不会读到半截文件
    return {"ok": True, "file": out}


def _a_shares_snapshot():
    """全市场日度报价+估值+成分宇宙快照（2026-10-26）：收盘后抓全 A 今日价/PE/PB/市值，落库+CSV。

    解决进哥「每日任务光弄 K 线、其它基本数据都没抓」的缺口；详见 tools/fetch_a_shares_snapshot.py。

    2026-10-29 加降级通道：东财 push2 连续两天报 "Remote end closed connection without response"
    （实测第 1 页就连不上，不是分页偶发），导致 stock_daily_snapshot 停在 10-27，
    而它同时是 AI 复盘「市场横截面」的唯一来源 —— 停更就等于每天拿陈旧的涨跌家数做判断。
    同刻实测腾讯 qt.gtimg.cn 5/5 成功，故东财失败时自动降级腾讯，而不是空手而归。
    """
    import fetch_a_shares_snapshot
    r = fetch_a_shares_snapshot.run()
    if isinstance(r, dict) and r.get("ok") is False:
        print("[a_shares_snapshot] 东财失败(%s)，降级腾讯通道" % str(r.get("error"))[:90], flush=True)
        try:
            import fetch_a_shares_tencent
            r2 = fetch_a_shares_tencent.run()
            if isinstance(r2, dict) and r2.get("ok"):
                r2["fallback"] = "tencent"
                r2["em_error"] = str(r.get("error"))[:150]
                return r2
            print("[a_shares_snapshot] 腾讯也失败：%s" % str(r2)[:150], flush=True)
        except Exception:  # noqa: BLE001
            import traceback
            traceback.print_exc()
    return r


def _kline_daily():
    """K 线全量日更（增量）：复用 tools/build_kline_db.py 主流程（无 --full = 仅补缺失日期）。

    进哥质疑「每日任务光弄 K 线」——其实是之前连 K 线增量都没接进调度；这里补上，
    让全市场 5550 只的日 K 每天收盘后自动向前补一根。
    """
    import build_kline_db
    _argv = sys.argv
    try:
        sys.argv = [os.path.basename(build_kline_db.__file__)]   # 无 --full → 增量
        return build_kline_db.main()
    finally:
        sys.argv = _argv


def _fast_daily():
    """快速日更：一次请求拿全市场当日一根 → 写前复权/后复权两库 → 接指标层（2026-11-03 D72）。

    为什么要有它：`kline_daily`（3362.8s）+ `kbase_daily`（1886.8s）合计 87 分钟，
    两个都是**逐只调新浪**（5550 次请求，且新浪限流敏感、workers 只能开 2）。
    本任务用通达信官网盘后包一个 zip 覆盖全市场当日一根，实测 13 秒写完两个库。

    ★ 它**不替代** `kline_daily` / `kbase_daily`，只是排在它们前面先把当日推进去：
      · 两个老任务的实现、默认路径、判据一行都没改；
      · 盘后包没发布时（当天收盘后不是马上有包，实测 20261103 当天 404）
        fast_grab 自己降级成腾讯快照并写 app.db，不会抛错，本任务照样走完；
      · 先跑本任务，老任务随后跑时看到的「末根已是最新交易日」会自动大量跳过，
        等于把 87 分钟摊薄成 13 秒 + 少量补差。

    为什么还要接 `indicators_daily`：它读的正是 data/kline_daily.db 的当日收盘，
    库没推进它就是空转。放同一个任务里串起来，保证「K 线推进了但因子没跟上」不会发生。
    `indicators_daily` 的增量/全量语义由 viewer/indicators.run(mode="daily") 自己决定，
    这里只是按原样调它，不传任何新参数。
    """
    import fast_grab
    r = fast_grab.run(write=True)
    if isinstance(r, dict) and r.get("ok") is False:
        # 抓取本身判失败就不再往下算指标：拿旧 K 线算出来的指标会被当成今天的结果。
        return r
    try:
        import viewer.indicators as ind
        ir = ind.run(mode="daily")
    except Exception as e:  # noqa: BLE001 —— 指标层失败不该让「K 线已推进」这个事实报废
        # 故意不让它把整轮判死：K 线已经写进去了，那才是主产物；
        # 指标层失败要留痕（ok=False + 原因），交给父进程退避重试。
        return {"ok": False, "fast_grab": r,
                "error": "indicators_daily 失败：%s: %s" % (type(e).__name__, str(e)[:160])}
    return {"ok": True, "fast_grab": r, "indicators": ir}


def _kbase_daily():
    """回测/四支柱的 K 线底座（viewer/kbase.db）增量补齐（2026-10-29 新增调度）。

    为什么单独一个任务：`kline_daily` 写的是 data/kline_daily.db（前复权·整数日期），
    而历史回测与四支柱预测读的是 viewer/kbase.db（hfq·字符串日期），两条管线互不相通。
    此前 kbase 只能手动跑 build_kbase.py，没人跑就冻结 → 回测周期判定全错。
    增量只补「末根落后于最新交易日」的票；Sina 限流敏感，workers 固定 2。
    """
    from viewer import kbase as kb
    return kb.get_base().incremental_update(datalen=10000, workers=2)


def _douyin_corpus():
    """抖音舆情语料每日刷新（2026-10-26）：补进每日调度，避免「舆论」缺口。"""
    import import_douyin_corpus
    _argv = sys.argv
    try:
        sys.argv = ["import_douyin_corpus.py"]   # 缺省两域全灌
        return import_douyin_corpus.main()
    finally:
        sys.argv = _argv


def _build_exclusion():
    """剔除池 / 干净宇宙日更（2026-10-26）：依 stock_names(ST) / blogger_overlap(实证负收益噪声)
    / 无K线，重建 exclusion_pool。进哥「不在垃圾堆里挑垃圾」——把已知该剔除的票集中管理，
    漏斗与四支柱都在【全A − 剔除池】的干净宇宙里选。需在快照/抖音之后跑（依赖最新 stock_names/blogger_overlap）。"""
    import build_exclusion
    # 2026-10-31 整改（测评 P0-B）：显式传空 argv。
    # 原为 `return build_exclusion.main()`，而 main() 内部 `ap.parse_args()` 读的是
    # **本进程的** sys.argv —— 里面带着 "build_exclusion" 这个词，对方参数表里没有它，
    # 于是 argparse 打用法后抛 SystemExit(2)。11 次执行 11 次退出码 2、0 次成功。
    return build_exclusion.main(["--writer", "sched_job"])


def _indicators_daily():
    """技术指标 / 因子层日更（2026-10-26 P1）：本地 data/kline_daily.db 全市场 K 线 → 纯 pandas 算
    MACD/KDJ/BOLL/RSI/WR/CCI/ATR/DMI/TRIX/VR/ROC/OBV/BIAS/DMA/TEMA/PPO/STOCHRSI/PSY/BRAR/SAR/
    Supertrend/ENE 等 50+ 因子（吸收 myhhub/stock + pythonstock 算法，talib→pandas 改写），
    落 stock_indicators_daily(历史) / stock_indicators(最新快照) / stock_signals(买卖信号)。
    沙箱可直读本地 K 线，不需出网；对齐 pythonstock guess_indicators_daily 的 (code,date) 联合主键范式。"""
    import viewer.indicators as ind
    return ind.run(mode="daily")


def _universe_scan():
    """全宇宙多因子扫描（2026-10-26 P2）：对干净宇宙全部标的做动量/量能/质量三轴打分，
    落 stock_universe_scan，直接回答『评估覆盖全不全』。依赖 stock_indicators（P1 因子层）。"""
    import viewer.universe_scan as us
    return us.run()


def _strategy_templates():
    """现成策略模板（2026-10-26 P5）：基于 P1 因子 + P2 全宇宙扫描的现成选股模板，
    落 stock_strategy_picks。依赖 stock_indicators + stock_universe_scan。"""
    import viewer.strategy_templates as st
    return st.run()


def _regime_daily():
    """市场状态日更（2026-10-29）：等权市场 20 日强弱 → market_regime。

    依赖 stock_indicators_daily 的当日收盘，所以必须在 indicators_daily 之后跑。
    全历史重算实测 1.9s，每天全量重算而不是增量——因为分位阈值是滚动的，
    增量算会让新旧口径混在一张表里（那是最难查的一类脏数据）。
    """
    import viewer.regime as rg
    r = rg.run(save_it=True)
    cur = r.get("current") or {}
    return {"ok": bool(r.get("ok")), "saved": r.get("saved"),
            "date": cur.get("date"), "regime": cur.get("regime"),
            "ret20": cur.get("ret20")}


def _lift_daily():
    """限售解禁事件日更（2026-10-30）：刷新未来 400 天内的解禁批次 → lift_event。

    进哥需求：股东临近释放期就会卖，提前提示。表里只存「公司公告的解禁批次」这类
    **事实**，风险分/剩几个交易日全部在 API 调用时按当日交易日历现算（见 viewer/lift.py）。
    所以这个任务只要保证「事件不漏」，晚跑一会儿不影响判档正确性。
    成本：8 页 HTTP（约 1600 条），实测 3~6s。
    """
    import viewer.lift as lift_mod
    r = lift_mod.refresh()
    return {"ok": bool(r.get("ok")), "fetched": r.get("fetched"),
            "saved": r.get("saved"), "range": r.get("range")}


def _ann_daily():
    """全市场公告日更（2026-10-30）：抓最近 3 天全市场公告 → stock_ann。

    为什么必须独立跑：在此之前 stock_ann 的唯一写入方是 watchdesk.scan()，而它只遍历
    **盯盘池**（淘股吧重仓池 + 自选，约 200 只）→ 全库日均仅 40.5 条，A 股每天实际
    上千条，**覆盖率约 3%**。这个缺陷让所有公告类指标失去外部效度（avoid.py 里
    「股东减持」就是：113 条样本、宽松口径 t=-5.66，可执行口径 t=-0.13）。

    只存命中规则表的公告（cat != '其他事务'），避免把「3% 覆盖率」换成「100% 噪声」。
    实测 120 页 ≈ 33s、命中规则 2000+ 条、新增千余条。重复抓由 (code,url) 去重兜住。
    """
    import sqlite3
    import viewer.ann_feed as af
    con = sqlite3.connect(af.DB_PATH, timeout=30)
    try:
        r = af.refresh(con)
    finally:
        con.close()
    return {"ok": bool(r.get("ok")), "pages": r.get("pages"), "kept": r.get("kept"),
            "added": r.get("added"),
            "range": "%s~%s" % (r.get("oldest") or "-", r.get("newest") or "-"),
            "err": r.get("err")}


def _harvest_daily():
    """收割视角日更（2026-10-31）：涨停/炸板/连板 → 局面状态 + 陷阱名单。

    读 data/kline_daily.db（1,700 万行日线），增量重算最近 400 个交易日即可
    （实测首次全量 158s，增量约 40~60s），写 market_structure_daily /
    harvest_trap_daily。必须排在 kline_daily 之后 —— kline 当天不补，这里算出来的
    就是昨天的盘面，而「今日陷阱名单」差一天就完全没用。
    """
    import sqlite3
    from viewer.db import Database
    import viewer.harvest as H
    db = Database()
    try:
        r = H.refresh_daily(db.conn)
    finally:
        try:
            db.conn.close()
        except Exception:  # noqa: BLE001
            pass
    return {"ok": bool(r.get("ok")), "inserted": r.get("inserted"),
            "traps": r.get("traps"), "range": r.get("range"), "err": r.get("error")}


def _oss_daily():
    """开源情报日更（2026-10-30）：GitHub 规则 → 我们的严格口径重测。

    与 harvest_daily 同理必须离进程：单条规则要扫 150 万行指标表，纯 Python 占 GIL
    会把整站拖死。日常开销只有「过期规则重测（通常 0 条）+ 长样本复核 37s」，
    首轮全量 11 条约 260s。
    """
    import sqlite3
    from viewer.db import Database
    import viewer.oss_mine as OSS
    db = Database()
    try:
        r = OSS.refresh(db.conn)
    finally:
        try:
            db.conn.close()
        except Exception:  # noqa: BLE001
            pass
    return {"ok": bool(r.get("ok")), "tested": r.get("tested"),
            "skipped": r.get("skipped"), "failed": r.get("failed"),
            "scanned": r.get("scanned"), "cross": r.get("cross"),
            "elapsed": r.get("elapsed")}


def _selection_daily():
    """选股方向日更（2026-10-30）：低波池每日重算，证据回测 30 天一次。

    池子只读最近 600 根日线（约 334 万行），实测 7.7s；
    证据回测扫 2013 年以来全量日线，实测 143s —— 所以只在超期时跑，
    否则每天白烧 2 分钟 SQLite IO。

    两条结论都来自本引擎实测，不接受外部说法：
      · 短线 T+1：超跌 / 涨停 / 放量突破全部负超额 → 只做风控，不做买入信号
      · 中长期：一直低波（前后两段振幅都 ≤45%）2 年 +24.7%、胜率 65.9%；
        「曾活跃后沉寂」2 年 −0.6%、胜率 35%，是最差的一组
    """
    from viewer.db import Database
    import viewer.selection as SEL
    db = Database()
    try:
        p = SEL.refresh_pool(db.conn)
        stale = True
        try:
            row = db.conn.execute(
                "SELECT v FROM sel_meta WHERE k='tested_at'").fetchone()
            if row and row[0]:
                import datetime as _dt
                t = _dt.datetime.strptime(row[0][:10], "%Y-%m-%d")
                stale = (_dt.datetime.now() - t).days >= 30
        except Exception:  # noqa: BLE001
            stale = True
        e = SEL.refresh_evidence(db.conn) if stale else {"ok": True, "skipped": True}
    finally:
        try:
            db.conn.close()
        except Exception:  # noqa: BLE001
            pass
    return {"ok": bool(p.get("ok")), "pool": p.get("n"), "tier": p.get("tier"),
            "evidence": e, "elapsed": p.get("elapsed")}


def _signal_backtest():
    """信号 alpha 回测（2026-10-27 C：季度重算，收敛进主工作流）。
    依赖 stock_indicators_daily（~135万行），落 signal_backtest。
    全量 ~30min，不进每日自动调度，仅通过 `sched_job.py signal_backtest` 低频/季度触发。"""
    import viewer.signal_backtest as sb
    return sb.backtest(lookback_days=250, forward_days=(5, 10, 20))


def _kline_prewarm():
    """全市场日 K 预热（实测 165s）；产物本来就落盘 decline_kcache.json。"""
    import screener
    pool = screener._market_pool()
    if not pool:
        return {"ok": False, "error": "全市场清单获取失败"}
    _bars, meta = screener._load_bars_map(pool, 30)
    return {"ok": True, "meta": meta}


def _plate_members():
    """C3：东财官方板块成分股采集（治漏斗 official 模式回退毒池，见 D3/D5）。
    沙箱东财被 RST 屏蔽，必须进哥本机 venv 跑；本机东财可达。
    备用同花顺源：tools/fetch_plate_members_ths.py（本机正常网络不被限流）。
    fetch_plate_members 用 argparse 的 main()，此处中和 sys.argv 后调用（默认抓全部 17 板块）。"""
    import fetch_plate_members
    saved = sys.argv
    sys.argv = ["fetch_plate_members.py"]   # 默认全板块，无需参数
    try:
        return fetch_plate_members.main()
    finally:
        sys.argv = saved


def _payload_cache(modname: str, fname: str):
    """把某面板的重算放到子进程，结果写 viewer/data/<fname>（2026-10-29）。

    为什么必须离进程（实测证据）：/api/quant 现算 >150s、/api/etf 复用同一引擎 >60s，
    而且都是纯 Python → 占 GIL 冻结全站（refresh_status 从 0.47s 拖到 4.9s、
    /api/backtest 直接 70s 超时）。改为「子进程算一次 + 落盘」，
    请求只读这份缓存，绝不再现算（缺失/过期才由服务端触发本任务）。
    用写连接：各 payload 内部 ensure_tables 需要建表。
    """
    import importlib
    import json
    mod = importlib.import_module(modname)
    d = mod.payload(_con().conn)
    out_dir = os.path.join(ROOT, "viewer", "data")
    os.makedirs(out_dir, exist_ok=True)
    out = os.path.join(out_dir, fname)
    tmp = out + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"at": time.strftime("%Y-%m-%d %H:%M:%S"), "payload": d},
                  f, ensure_ascii=False, default=str)
    os.replace(tmp, out)
    return {"ok": bool(d.get("ok")), "bytes": os.path.getsize(out)}


def _quant_cache():
    return _payload_cache("quant", "quant_payload.json")


def _etf_cache():
    return _payload_cache("etf", "etf_payload.json")


def _kronos_cache():
    return _payload_cache("kronos", "kronos_payload.json")


def _variant_payload():
    """2026-10-29：带参数的重量级面板（低位箱体 / 换手率榜）离进程计算并按参数签名落盘。

    为什么必须离进程：/api/screener_lowbox 实测 **33 秒**、/api/screener_turnover_top 在服务
    进程里也要 **14 秒**，全是纯 Python 数值循环 —— 放在请求线程里就是把用户钉在转圈上。
    两者参数可变，所以每个「参数组合」落一份结果（文件名带签名），同参秒出、换参另算。
    请求单 `viewer/data/variants/_req_<sig>.json` 由服务端写，本任务消费后即删。
    """
    import glob
    import json
    vdir = os.path.join(ROOT, "viewer", "data", "variants")
    os.makedirs(vdir, exist_ok=True)
    reqs = sorted(glob.glob(os.path.join(vdir, "_req_*.json")))
    if not reqs:
        return {"ok": False, "reason": "没有待算请求"}
    import viewer.server as _srv          # 复用面板实现（纯导入，不会起服务）
    done = []
    for rq in reqs:
        try:
            with open(rq, "r", encoding="utf-8") as f:
                spec = json.load(f)
            route = spec.get("route") or ""
            sub = spec.get("sub") or ""
            sig = spec.get("sig") or ""
            params = spec.get("params") or {}
            if not (route and sig and sub):
                os.remove(rq)
                continue
            if route == "/api/screener_lowbox":
                p = _srv.api_screener_lowbox(**params)
            elif route == "/api/screener_turnover_top":
                p = _srv.api_screener_turnover_top(**params)
            elif route == "/api/universe_box":
                # 2026-11-02 D65：中线全市场箱体波谷。实测 7~8 秒，参数只有 limit/min_dwell。
                p = _srv.api_universe_box(**params)
            elif route == "/api/toolbox/funnel":
                # 2026-11-03 工具包级联漏斗：参数是 steps(JSON 字符串) + limit。
                # 最重的一级（箱体谷底）全市场 7.6 秒，所以整条链一起离进程算。
                p = _srv.api_toolbox_funnel(**params)
            else:
                print("[variant] 未知路由 %r，丢弃请求单" % route)
                os.remove(rq)
                continue
            out = os.path.join(vdir, "%s_%s.json" % (sub, sig))
            tmp = out + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"at": time.strftime("%Y-%m-%d %H:%M:%S"), "route": route,
                           "sig": sig, "params": params, "payload": p}, f,
                          ensure_ascii=False, default=str)
            os.replace(tmp, out)
            os.remove(rq)                # 请求单用完即删，避免下次重复算
            done.append({"route": route, "sig": sig, "ok": bool(p.get("ok")),
                         "rows": len(p.get("rows") or [])})
        except Exception:  # noqa: BLE001
            import traceback
            traceback.print_exc()        # 失败保留请求单 → 下轮重试
    return {"ok": True, "done": done}


def _review_score_daily():
    """复盘回填 + 假说回测（2026-10-31）。

    这是「AI 复盘闭环」的下半段，做两件事：
      ① `review_score.score_pending` —— 把已到期（满 T+5/T+10）的大模型判断按真实收盘价
         回填命中率，让"它说得对不对"变成系统算得出的数字，而不是让它自己回忆；
      ② `hypothesis.test_open` —— 把模型提的可回测假说跑一遍，结论写回账本，并沉淀进
         evo_knowledge（下一轮复盘会读到）。

    为什么不放在复盘触发时做：复盘当天不可能有 T+5 结果，必须等时间过去。
    也不放在请求线程里：要扫全市场行情，几十秒级。故独立成日更任务。

    幂等：只处理 status='open' 的行，重复跑不会重复计分。
    """
    import sqlite3
    import viewer.review_score as RS
    import viewer.hypo as HP
    from viewer.ann_feed import DB_PATH as _DBP

    con = sqlite3.connect(_DBP, timeout=60)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA busy_timeout=60000")
    try:
        s = RS.score_pending(con)
        h = HP.test_open(con)
        out = {"score": s, "hypothesis": h}
        print("[review_score_daily] %s" % out)
        return out
    finally:
        try:
            con.close()
        except Exception:  # noqa: BLE001
            pass


def _board_flow_range_daily():
    """板块资金流日线（board_flow_daily）日更（2026-10-30 新增）。

    为什么会有这个任务：`board_flow_daily` 此前**没有任何调度**，唯一写入路径
    `board_flow_range.ensure_daily` 只由「区间资金面板请求」间接调用 →
    用户不打开那个面板，表就永远不更新（实测停更 21 个交易日，217 行全落在 2026-10-08）。

    为什么必须出进程：这条链是「HTTP 收数 + 结构解析」，虽然 IO 为主，但本项目铁律是
    **纯 Python 长任务一律不留在服务进程内**（会占 GIL 冻结所有接口）。
    真实开销：datacenter 报表「按日拉全板块」，一天 4 个请求（industry 1 页 / concept 3 页）。

    为什么调 `run_daily_sync` 而不是别的：本文件是短命子进程，`print` 完就 `return 0` 退出，
    "起 daemon 线程后立即返回"的写法会被 CPython 关解释器时腰斩（lhb_daily 为此骗了 11 天）。
    `run_daily_sync` 一路同步到底，返回体带 `ok`：`ok is False` → `main()` 判 rc=1 → 父进程退避重试。
    """
    import board_flow_range
    return board_flow_range.run_daily_sync(_con().conn)


def _factor_discover():
    """因子来源发现（2026-11-03 三期）：GitHub 扫可疑仓库 → 入待办清单。

    ★ 这个任务**不产出因子**，只产出"值得看的仓库"。把仓库代码自动编译成因子是不可靠的
    （各仓库复权/口径不同，编出来的 IC 假得像真的），所以"发现"自动、"编译"必须有人。
    自节流 7 天：调用方是每日任务，每天扫 GitHub 既浪费配额又没意义。

    离进程跑的理由和 oss_daily 一样：拿的是独立的 Database() 连接，不占服务端 GIL。
    """
    from viewer.db import Database
    import viewer.factor_import as FI
    db = Database()
    try:
        r = FI.discover(db.conn, days=30, per_query=6)
    finally:
        try:
            db.conn.close()
        except Exception:  # noqa: BLE001
            pass
    # 跳过（未到 7 天）不算失败；真扫失败才是失败
    if r.get("skipped"):
        return {"ok": True, "skipped": True, "note": r.get("note")}
    return {"ok": bool(r.get("ok")), "found": r.get("found"), "fresh": r.get("fresh"),
            "verdict": r.get("verdict"), "note": r.get("note")}


def _weibo_heat():
    """金十微博个股人气指数 日更（2026-11-07）。

    接 `fetch_weibo_heat.run_daily_sync`：打金十 `/weibo/list` 端点 → 名称反查 code
    → 落 `weibo_heat` 表，自带「当日 done」闸门，每天最多落一次。
    返回体带 `ok`：ok=False → main() 判 rc=1 → 父进程退避重试。
    """
    from viewer.db import Database
    import fetch_weibo_heat as F
    db = Database()
    try:
        return F.run_daily_sync(db.conn)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"{type(e).__name__}: {e}"[:300]}
    finally:
        try:
            db.conn.close()
        except Exception:  # noqa: BLE001
            pass


JOBS = {
    "weibo_heat": _weibo_heat,         # 2026-11-07：金十微博个股人气指数日更（注意力/热度信号）
    "quant": _quant_cache,             # 2026-10-29：量化面板全市场因子计算（离进程 + 落盘，供 /api/quant 读缓存）
    "etf_payload": _etf_cache,         # 2026-10-29：ETF 面板（左栏复用 quant 因子引擎，实测 >60s，必须离进程）
    "kronos_payload": _kronos_cache,   # 2026-10-29：Kronos 预测（实测现算 18s 且占 GIL）
    "variant_payload": _variant_payload,  # 2026-10-29：参数化重量级面板（低位箱体 33s / 换手榜 14s）离进程算
    "predictor_daily": _predictor_daily,
    "pillars_daily": _pillars_daily,
    "resonance_build": _resonance_build,
    "selfpick_build": _selfpick_build,
    "evolve_daily": _evolve_daily,
    "bili_scan": _bili_scan,
    "lhb_daily": _lhb_daily,
    "board_flow_range_daily": _board_flow_range_daily,   # 2026-10-30：板块资金流日线日更（18:30 后）
    "hotspots": _hotspots,
    "kline_prewarm": _kline_prewarm,
    "a_shares_snapshot": _a_shares_snapshot,   # 2026-10-26：全A报价+估值+宇宙日更
    "kline_daily": _kline_daily,               # 2026-10-26：K线增量日更
    "fast_daily": _fast_daily,                 # 2026-11-03 D72：盘后包一次拿全市场 → 两库 + 指标层
    "kbase_daily": _kbase_daily,               # 2026-10-29：回测K线底座 kbase.db 增量日更
    "douyin_corpus": _douyin_corpus,           # 2026-10-26：抖音舆情日更
    "build_exclusion": _build_exclusion,        # 2026-10-26：剔除池/干净宇宙日更
    "indicators_daily": _indicators_daily,      # 2026-10-26 P1：技术指标/因子层日更
    "universe_scan": _universe_scan,            # 2026-10-26 P2：全宇宙多因子扫描（依赖 indicators_daily）
    "regime_daily": _regime_daily,              # 2026-10-29：市场状态（依赖 indicators_daily 当日收盘）
    "lift_daily": _lift_daily,                  # 2026-10-30：限售解禁事件日更（只存事实，风险分现算）
    "ann_daily": _ann_daily,                    # 2026-10-30：全市场公告日更（覆盖 3% → 全市场）
    "review_score_daily": _review_score_daily,  # 2026-10-31：复盘回填 + 假说回测（AI 闭环下半段）
    "strategy_templates": _strategy_templates,  # 2026-10-26 P5：现成策略模板（依赖 indicators + universe_scan）
    "signal_backtest": _signal_backtest,        # 2026-10-27 C：信号 alpha 回测（季度重算，收敛进主工作流）
    "plate_members": _plate_members,            # 2026-10-27 C3：东财官方板块成分（本机出网，沙箱屏蔽）
    "harvest_daily": _harvest_daily,           # 2026-10-31：收割视角（局面+陷阱名单，依赖 kline_daily）
    "oss_daily": _oss_daily,                   # 2026-10-30：开源情报（GitHub 规则重测，只抽规则不抄代码）
    "selection_daily": _selection_daily,       # 2026-10-30：选股方向（低波池日更 + 证据月度回测）
    "factor_discover": _factor_discover,       # 2026-11-03 三期：因子来源发现（自节流 7 天，只发现不编译）
}


def main() -> int:
    if len(sys.argv) < 2 or sys.argv[1] not in JOBS:
        print("usage: python tools/sched_job.py <%s>" % "|".join(sorted(JOBS)))
        return 2
    name = sys.argv[1]
    t0 = time.time()
    print("[%s] start %s" % (name, time.strftime("%Y-%m-%d %H:%M:%S")), flush=True)
    # 2026-10-31（决策⑤）：任务的成败除了打日志，还要落一行 job_runs。
    # 理由见 viewer/health.py 的模块说明 —— 历史上两次"静默停摆"（build_exclusion
    # 11 次全失败、a_shares_snapshot 连失两天却记成完成）都不是没有日志，
    # 是日志没人看。"连续失败 N 次"才是一个能扫一眼就发现问题的数字。
    # 记录放在 finally 里，保证 正常返回 / 软失败 / SystemExit / 异常 四条路都留痕。
    rc, err = 0, ""
    try:
        r = JOBS[name]()
        # 2026-10-29：把「返回值 ok=False」也判为失败。
        # 此前只要不抛异常就 return 0，于是 a_shares_snapshot 连续两天抓取失败
        # （东财 Remote end closed connection）却在日志里回报成功 → 父进程据此写下
        # 当日「已完成」标记 → 当天不再重试 → 整条日更链静默停在 10-27。
        # 失败必须以非 0 退出，才能让父进程走退避重试，而不是把失败记成完成。
        if isinstance(r, dict) and r.get("ok") is False:
            print("[%s] FAILED(soft) %.1fs %s" % (name, time.time() - t0, str(r)[:400]), flush=True)
            rc, err = 1, "ok=False %s" % str(r)[:400]
        else:
            print("[%s] ok %.1fs %s" % (name, time.time() - t0, str(r)[:400]), flush=True)
    except SystemExit as e:  # noqa: BLE001
        # 2026-10-31 整改（测评 P0-B）：SystemExit 继承 **BaseException**，不被下面的
        # `except Exception` 捕获。后果不是"报错很难看"，是**彻底看不见**：
        # build_exclusion 的 argparse 抛 SystemExit(2) 时，这里既不打印 FAILED、
        # 也不打印 traceback，父进程只看到一行干净的 start ——
        # 自 2026-10-26 上线起 11 次执行 11 次失败、0 次成功，没有任何人发现。
        # 任何任务内部只要可能调用 argparse / sys.exit / raise SystemExit，都必须在此落成 FAILED。
        print("[%s] FAILED(SystemExit=%r) after %.1fs"
              % (name, getattr(e, "code", None), time.time() - t0), flush=True)
        traceback.print_exc()
        rc, err = 1, "SystemExit(%r)" % (getattr(e, "code", None),)
    except Exception as e:  # noqa: BLE001
        print("[%s] FAILED after %.1fs" % (name, time.time() - t0), flush=True)
        traceback.print_exc()
        rc, err = 1, "%s: %s" % (type(e).__name__, str(e)[:300])
    finally:
        # 记录本身坏掉不能连累任务结论 —— 写失败只打印，退出码照旧由 rc 决定。
        try:
            from viewer import health as _H
            _H.record_job_run(name, rc == 0, started=t0, rc=rc, error=err)
        except Exception as _e:  # noqa: BLE001
            print("[%s] 运行记录写入异常（不影响退出码）：%s" % (name, _e), flush=True)
    return rc


if __name__ == "__main__":
    sys.exit(main())
