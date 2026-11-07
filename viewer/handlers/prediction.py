# -*- coding: utf-8 -*-
"""prediction 域 API 处理函数（从 viewer/server.py 物理抽取，2026-11-07 拆分重构）。

抽取规则：函数体逻辑零改动；依赖的 server 模块级符号经 `from viewer.server import (...)`
重定向进本地命名空间；标准库本地导入。server.py 末尾 re-export 保持命名空间兼容，
routes.py 与既有测试（S.api_*）无需改动。
"""
from __future__ import annotations

import logging, threading
from viewer.server import (  # noqa: E402,F401
    DB,
    _prediction_scores_compute,
    _prediction_today_compute,
    _swr_serve,
    predictor,
)

def api_prediction_today(force: bool = False) -> dict:
    """今日推荐载荷：核心/观察池 + 命中信号 + 理由 + 复盘结果 + 权重调整痕迹。

    2026-10-30：实测冷态 **120s**、热态 0.68s，且不落磁盘 —— 套 SWR（见 _swr_serve）：
    重启后第一次点击读盘快照（毫秒级、标 _stale），后台再重算；手动刷新走 force。
    """
    return _swr_serve("prediction_today", _prediction_today_compute, ttl=1800, force=force)
def api_prediction_scores(force: bool = False) -> dict:
    """策略记分板：六路信号 + 九流派 + 逐策略的实盘命中率（T+1/T+3/T+5）。

    2026-10-30：实测冷态 32s、热态 1.46s。记分板本质是「历史命中统计」，一天只变一次，
    故 TTL 给 7200s（比 today 更宽）。
    """
    return _swr_serve("prediction_scores", _prediction_scores_compute, ttl=7200, force=force)
def api_prediction_track() -> dict:
    """复盘轨迹：逐日命中率与累计表现（自我成长的证据链）。"""
    predictor.ensure_tables(DB.conn)
    return predictor.track_payload(DB.conn)
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
