# -*- coding: utf-8 -*-
"""路由分发层（从 server.py 抽离，P1 重构）。

本模块只做一件事：把 `route`（路径）映射到对应的响应构造调用。
所有业务构造逻辑仍在 server.py 的 `api_*` 函数里，本模块不重复实现任何业务。

为什么单独成文件
    server.py 原 `_do_GET`/`_do_POST` 各近千行、纯 `if route == "..."` 扁平分支，
    是典型「上帝方法」。抽出来后：① server.py 体积下降一半以上；② 路由可被单测
    （见 tests/test_routes.py，用 FakeHandler 直接驱动 route_get/route_post）；
    ③ 路由清单（GET_ROUTES/POST_ROUTES）一处可查，新人一眼看懂有哪些端点。

行为等价保证
    路由匹配段是从 server.py **逐字搬来**的，未改任何判断逻辑。段内引用的 server
    模块级符号（api_* / DB / _wh / _TTL_* 等）通过 `_ensure_bound()` 惰性别名进本模块
    命名空间——首次请求时（server 已完全加载）才绑定，故无循环导入问题。
"""
from __future__ import annotations

import json
import time
import urllib.parse

# ── 惰性绑定：把 server 模块级符号别名进本模块命名空间（避免循环导入）──
_BOUND = False
_SRVER_NAMES = {'DB', 'INGEST', 'INGEST_LOCK', 'ROOT', 'api_hotspots', 'api_moneyflow_stock_force', 'api_research_insight_rebuild', 'api_screener_lowbox', 'api_screener_turnover_top', 'SITE_DISPLAY_NAMES', 'STATIC_DIR', '_BadRequest', '_TTL_CACHE', '_TTL_ROUTES', '_WRITE_LOCK', '_api_cache_invalidate', '_backup_failed_payload', '_destructive_payload', '_heavy_cached_payload', '_ingest_item_count', '_quant_select_cached', '_refresh_watch_list_cache_after_check', '_sched_load_state', '_sched_save_state', '_sites', '_subproc_env', '_swr_refresh_async', '_truthy', '_variant_cached', 'api_ai_picks', 'api_ai_provider_key', 'api_ai_provider_test', 'api_ai_review', 'api_ai_review_detail', 'api_ai_review_history', 'api_ai_review_run', 'api_avoid_flags', 'api_backup', 'api_backup_restore', 'api_bili_methods', 'api_bili_pile', 'api_bili_scan', 'api_board', 'api_board_flow_dates', 'api_board_flow_intraday', 'api_board_flow_range', 'api_board_flow_snapshot', 'api_board_flow_status', 'api_branch_detail', 'api_cohold_recent', 'api_config_get', 'api_config_set', 'api_consensus', 'api_cost_basis', 'api_cost_calib', 'api_dashboard', 'api_detail', 'api_detail_list', 'api_diag', 'api_douyin_bloggers', 'api_douyin_detail', 'api_douyin_facets', 'api_douyin_list', 'api_douyin_overview', 'api_douyin_plate', 'api_douyin_resonance', 'api_douyin_stances', 'api_douyin_topics', 'api_dragon_map', 'api_dragon_one', 'api_evidence', 'api_executability', 'api_freshness', 'api_fund_strategies', 'api_funnel_score', 'api_geo_events', 'api_geo_refresh', 'api_global_market', 'api_growth_adjust_log', 'api_harvest', 'api_health', 'api_hypothesis', 'api_indicators', 'api_indicators_signals', 'api_info_brief', 'api_kb_doc', 'api_kb_list', 'api_kline', 'api_lhb', 'api_lhb_backfill', 'api_lhb_sectors', 'api_lhb_weather', 'api_lift_flags', 'api_lowbox_breakouts', 'api_market_freshness', 'api_moneyflow_board', 'api_moneyflow_rank', 'api_moneyflow_stock', 'api_multiquote_lookup', 'api_multiquote_preset', 'api_news_market', 'api_news_stock', 'api_niusan', 'api_open_dir', 'api_operating', 'api_oss', 'api_overseas_collect', 'api_overseas_hotspots', 'api_overseas_sentiment', 'api_overseas_sources', 'api_params', 'api_person', 'api_persons', 'api_plate_heat', 'api_player', 'api_players', 'api_policy_desk', 'api_positions', 'api_prediction_catchup', 'api_prediction_dates', 'api_prediction_history', 'api_prediction_run', 'api_prediction_scores', 'api_prediction_status', 'api_prediction_today', 'api_prediction_track', 'api_public_records', 'api_quote', 'api_rate_snapshot', 'api_rate_timeline', 'api_raw_pages', 'api_refresh_status', 'api_research_insight', 'api_research_rating', 'api_research_reports', 'api_review_score', 'api_scheduler_status', 'api_screener', 'api_screener_board_members', 'api_screener_boards', 'api_screener_bottom', 'api_screener_decline', 'api_selection', 'api_sentiment', 'api_shadow_weights', 'api_signal_backtest', 'api_signal_combo', 'api_signal_evidence', 'api_signal_regime', 'api_smart_money', 'api_star_moves', 'api_stock', 'api_stock_f10', 'api_stock_turnover', 'api_stocks', 'api_strategies', 'api_strategy_templates', 'api_summary', 'api_sync_names', 'api_today_decision', 'api_topics', 'api_universe_scan', 'api_valuation', 'api_version', 'api_watch_alert_read', 'api_watch_alerts', 'api_watch_brief', 'api_watch_check', 'api_watch_group_add', 'api_watch_group_remove', 'api_watch_group_rename', 'api_watch_group_reorder', 'api_watch_item_add', 'api_watch_item_group_remove', 'api_watch_item_remove', 'api_watch_item_reorder', 'api_watch_item_update', 'api_watch_list', 'api_watchdesk', 'api_watchdesk_detail', 'api_watchdesk_scan', 'kb_reload'}


def _ensure_bound() -> None:
    global _BOUND
    if _BOUND:
        return
    import viewer.server as _srv
    g = globals()
    for _n in _SRVER_NAMES:
        g[_n] = getattr(_srv, _n, None)
    _BOUND = True


from viewer.recovery_handlers import (  # P1 抽取 + git checkout 事故后重建的审计期桥接端点
    api_ai_debate, api_ai_debate_run, api_ai_debate_single,
    api_ai_expert, api_fast_grab, api_fast_grab_status,
    api_factors_admit, api_factors_candidates, api_factors_source_status, api_factors_sources,
    api_router_context, api_router_decisions, api_router_explain, api_router_plan,
    api_router_policy_status, api_toolbox, api_toolbox_funnel, api_toolbox_stack, api_toolbox_status,
    api_universe_box,
)

# ── #4 路由注册表统一（分批迁移，2026-11-07）──
# 精确路由 → handler(self, parsed)。handler 内引用的 api_* 由 `_ensure_bound` 在每次
# route_get 时绑定进本模块全局命名空间，故函数体里的自由变量在「调用时」才解析，无需事先 import。
# 迁移原则：先从 route_get 的 if 链里摘出分支，登记到此表；静态/前缀/TTL/未迁移分支仍走 if 链，互不影响。
def _h_version(self, parsed):
    return self._send_json(api_version())


def _h_summary(self, parsed):
    return self._send_json(api_summary())


def _h_stocks(self, parsed):
    return self._send_json(api_stocks())


def _h_operating(self, parsed):
    return self._send_json(api_operating())


def _h_persons(self, parsed):
    q = urllib.parse.parse_qs(parsed.query)
    return self._send_json(api_persons(limit=self._int_arg(q, "limit", 0, 0, 1000),
                                   offset=self._int_arg(q, "offset", 0, 0, 1000)))


def _h_strategies(self, parsed):
    return self._send_json(api_strategies())


def _h_fund_strategies(self, parsed):
    q = urllib.parse.parse_qs(parsed.query)
    key = self._str_arg(q, "key", "", maxlen=64) or None
    return self._send_json(api_fund_strategies(key))


def _h_public_records(self, parsed):
    return self._send_json(api_public_records())


def _h_sentiment(self, parsed):
    return self._send_json(api_sentiment())


def _h_weibo_heat(self, parsed):
    q = urllib.parse.parse_qs(parsed.query)
    limit = self._int_arg(q, "limit", 60, 1, 500)
    return self._send_json(api_weibo_heat(limit))


def _h_snapshot(self, parsed):
    q = urllib.parse.parse_qs(parsed.query)
    return self._send_json(_wh.snapshot_response(
        (q.get("path") or [""])[0], (q.get("q") or [""])[0], allowed=set(_TTL_ROUTES)))

def _h_warehouse_stats(self, parsed):
    return self._send_json(_wh.stats())

def _h_topics(self, parsed):
    return self._send_json(api_topics())

def _h_hotspots(self, parsed):
    return self._send_json(_heavy_cached_payload("/api/hotspots"))

def _h_refresh_status(self, parsed):
    return self._send_json(api_refresh_status())

def _h_market_freshness(self, parsed):
    return self._send_json(api_market_freshness())

def _h_params(self, parsed):
    return self._send_json(api_params())

def _h_cost_basis(self, parsed):
    return self._send_json(api_cost_basis())

def _h_health(self, parsed):
    return self._send_json(api_health())

def _h_cost_calib(self, parsed):
    return self._send_json(api_cost_calib())

def _h_scheduler_status(self, parsed):
    return self._send_json(api_scheduler_status())

def _h_kb_list(self, parsed):
    return self._send_json(api_kb_list())

def _h_raw_pages(self, parsed):
    q = urllib.parse.parse_qs(parsed.query)
    return self._send_json(api_raw_pages(self._int_arg(q, "limit", 60, 1, 500)))

def _h_global_market(self, parsed):
    return self._send_json(api_global_market())

def _h_overseas_sentiment(self, parsed):
    return self._send_json(api_overseas_sentiment())

def _h_overseas_hotspots(self, parsed):
    return self._send_json(api_overseas_hotspots())

def _h_overseas_sources(self, parsed):
    return self._send_json(api_overseas_sources())

def _h_geo_refresh(self, parsed):
    return self._send_json(api_geo_refresh())

def _h_freshness(self, parsed):
    return self._send_json(api_freshness())

def _h_positions(self, parsed):
    return self._send_json(api_positions())

def _h_today_decision(self, parsed):
    q = urllib.parse.parse_qs(parsed.query)
    return self._send_json(api_today_decision(force=self._force_arg(q, parsed.query)))

def _h_info_brief(self, parsed):
    return self._send_json(api_info_brief())

def _h_watch_brief(self, parsed):
    return self._send_json(api_watch_brief())

def _h_login_status(self, parsed):
    return self._send_json(DB.get_login_status())

def _h_person(self, parsed):
    q = urllib.parse.parse_qs(parsed.query)
    return self._send_json(api_person(self._str_arg(q, "name", "")))

def _h_player(self, parsed):
    q = urllib.parse.parse_qs(parsed.query)
    return self._send_json(api_player(self._str_arg(q, "name", "")))

def _h_stock(self, parsed):
    q = urllib.parse.parse_qs(parsed.query)
    return self._send_json(api_stock(self._str_arg(q, "symbol", "")))

def _h_stock_names(self, parsed):
    return self._send_json(DB.all_symbols())

def _h_moneyflow_stock(self, parsed):
    q = urllib.parse.parse_qs(parsed.query)
    return self._send_json(api_moneyflow_stock(self._str_arg(q, "symbol", "", maxlen=20)))

def _h_moneyflow_rank(self, parsed):
    q = urllib.parse.parse_qs(parsed.query)
    return self._send_json(api_moneyflow_rank(pz=self._int_arg(q, "pz", 30, 1, 100),
                                              force=self._str_arg(q, "force", "", 1).lower() == "1"))

def _h_moneyflow_board(self, parsed):
    q = urllib.parse.parse_qs(parsed.query)
    return self._send_json(api_moneyflow_board(pz=self._int_arg(q, "pz", 30, 1, 100),
                                               force=self._str_arg(q, "force", "", 1).lower() == "1"))

def _h_board_flow_snapshot(self, parsed):
    q = urllib.parse.parse_qs(parsed.query)
    return self._send_json(api_board_flow_snapshot(
        kind=self._str_arg(q, "kind", "industry", maxlen=16),
        pz=self._int_arg(q, "pz", 60, 1, 200),
        force=self._int_arg(q, "force", 0, 0, 1)))

def _h_board_flow_intraday(self, parsed):
    q = urllib.parse.parse_qs(parsed.query)
    return self._send_json(api_board_flow_intraday(
        kind=self._str_arg(q, "kind", "industry", maxlen=16),
        top=self._int_arg(q, "top", 12, 3, 20),
        force=self._int_arg(q, "force", 0, 0, 1)))

def _h_board_flow_range(self, parsed):
    q = urllib.parse.parse_qs(parsed.query)
    return self._send_json(api_board_flow_range(
        start=self._str_arg(q, "start", "", maxlen=16),
        end=self._str_arg(q, "end", "", maxlen=16),
        kind=self._str_arg(q, "kind", "industry", maxlen=16),
        top=self._int_arg(q, "top", 16, 4, 24),
        force=self._int_arg(q, "force", 0, 0, 1)))

def _h_board_flow_dates(self, parsed):
    q = urllib.parse.parse_qs(parsed.query)
    return self._send_json(api_board_flow_dates(
        kind=self._str_arg(q, "kind", "industry", maxlen=16)))

def _h_board_flow_status(self, parsed):
    q = urllib.parse.parse_qs(parsed.query)
    return self._send_json(api_board_flow_status())        # 每日预测（2026-09-28 新增）

def _h_board(self, parsed):
    q = urllib.parse.parse_qs(parsed.query)
    return self._send_json(api_board(
        self._str_arg(q, "code", "", maxlen=40),
        kind=self._str_arg(q, "kind", "industry", maxlen=16),
        name=self._str_arg(q, "name", "", maxlen=40),
        force=self._int_arg(q, "force", 0, 0, 1)))

def _h_prediction_today(self, parsed):
    q = urllib.parse.parse_qs(parsed.query)
    return self._send_json(api_prediction_today(force=self._force_arg(q, parsed.query)))

def _h_prediction_scores(self, parsed):
    q = urllib.parse.parse_qs(parsed.query)
    return self._send_json(api_prediction_scores(force=self._force_arg(q, parsed.query)))

def _h_prediction_track(self, parsed):
    return self._send_json(api_prediction_track())

def _h_growth_adjust_log(self, parsed):
    return self._send_json(api_growth_adjust_log())

def _h_prediction_status(self, parsed):
    return self._send_json(api_prediction_status())

def _h_prediction_dates(self, parsed):
    return self._send_json(api_prediction_dates())

def _h_prediction_history(self, parsed):
    q = urllib.parse.parse_qs(parsed.query)
    return self._send_json(api_prediction_history(
        date=self._str_arg(q, "date", ""),
        range_days=self._int_arg(q, "range", 0, 0, 9999),
        window=self._int_arg(q, "window", 5, 1, 60)))

def _h_prediction_run(self, parsed):
    q = urllib.parse.parse_qs(parsed.query)
    return self._send_json(api_prediction_run(force=self._int_arg(q, "force", 1, 0, 1)))

def _h_prediction_catchup(self, parsed):
    return self._send_json(api_prediction_catchup())

def _h_pillars(self, parsed):
    q = urllib.parse.parse_qs(parsed.query)
    return self._send_json(pillars.payload(DB.conn, date=self._str_arg(q, "date", "", maxlen=16)))

def _h_pillars_run(self, parsed):
    q = urllib.parse.parse_qs(parsed.query)
    return self._send_json(pillars.generate(DB.conn, force=bool(self._int_arg(q, "force", 1, 0, 1))))

def _h_pillars_status(self, parsed):
    return self._send_json(pillars.status_payload(DB.conn))

def _h_backtest(self, parsed):
    q = urllib.parse.parse_qs(parsed.query)
    return self._send_json(backtest.payload(
        DB.conn,
        source=self._str_arg(q, "source", "pillars", maxlen=16),
        date=self._str_arg(q, "date", "", maxlen=16),
        period=self._int_arg(q, "period", 0, 0, 999),
        pillar=self._str_arg(q, "pillar", "", maxlen=24)))

def _h_backtest_status(self, parsed):
    return self._send_json(backtest.status(DB.conn))

def _h_selfpick(self, parsed):
    return self._send_json(selfpick.payload(DB.conn))

def _h_selfpick_status(self, parsed):
    return self._send_json(selfpick.status_payload(DB.conn))

def _h_resonance(self, parsed):
    return self._send_json(resonance.payload(DB.conn))

def _h_resonance_status(self, parsed):
    return self._send_json(resonance.status_payload(DB.conn))

def _h_avoid(self, parsed):
    return self._send_json(resonance.avoid_payload(DB.conn))

def _h_harvest(self, parsed):
    return self._send_json(api_harvest())

def _h_oss(self, parsed):
    return self._send_json(api_oss())

def _h_selection(self, parsed):
    return self._send_json(api_selection())

def _h_quant(self, parsed):
    return self._send_json(_heavy_cached_payload("/api/quant"))

def _h_quant_status(self, parsed):
    return self._send_json(quant.status_payload(DB.conn))

def _h_quant_panorama(self, parsed):
    q = urllib.parse.parse_qs(parsed.query)
    return self._send_json(quant.factor_panorama(
        self._str_arg(q, "code", "", maxlen=16), con=DB.conn))

def _h_quant_select(self, parsed):
    q = urllib.parse.parse_qs(parsed.query)
    return self._send_json(_quant_select_cached(
        self._int_arg(q, "top_n", 30, 5, 200)))

def _h_quant_backtest(self, parsed):
    return self._send_json(quant.factor_backtest(con=DB.conn))

def _h_quant_recipe(self, parsed):
    return self._send_json(quant.recipe_list(DB.conn))

def _h_kronos(self, parsed):
    return self._send_json(_heavy_cached_payload("/api/kronos"))

def _h_kronos_status(self, parsed):
    return self._send_json(kronos.status_payload(DB.conn))

def _h_etf(self, parsed):
    return self._send_json(_heavy_cached_payload("/api/etf"))

def _h_etf_status(self, parsed):
    return self._send_json(etf.status_payload(DB.conn))

def _h_evolve(self, parsed):
    return self._send_json(evolve.payload(DB.conn))

def _h_evolve_status(self, parsed):
    return self._send_json(evolve.status_payload(DB.conn))

def _h_screener_boards(self, parsed):
    q = urllib.parse.parse_qs(parsed.query)
    return self._send_json(api_screener_boards(kind=self._str_arg(q, "kind", "industry", maxlen=16),
                                               pz=self._int_arg(q, "pz", 90, 1, 200)))

def _h_screener_board_members(self, parsed):
    q = urllib.parse.parse_qs(parsed.query)
    return self._send_json(api_screener_board_members(code=self._str_arg(q, "code", "", maxlen=16)))

def _h_screener_decline(self, parsed):
    q = urllib.parse.parse_qs(parsed.query)
    return self._send_json(api_screener_decline(days=self._int_arg(q, "days", 30, 5, 120),
                                                down=self._int_arg(q, "down", 25, 1, 120),
                                                limit=self._int_arg(q, "limit", 80, 1, 200),
                                                sort=self._str_arg(q, "sort", "down", maxlen=8),
                                                force=q.get("force", ["0"])[0] == "1"))

def _h_ai_review(self, parsed):
    q = urllib.parse.parse_qs(parsed.query)
    return self._send_json(api_ai_review(
        date=self._str_arg(q, "date", "", maxlen=10),
        provider=self._str_arg(q, "provider", "", maxlen=16),
        force=self._int_arg(q, "force", 0, 0, 1),
        since_id=self._int_arg(q, "since_id", 0, 0, 10 ** 9)))

def _h_ai_review_history(self, parsed):
    q = urllib.parse.parse_qs(parsed.query)
    return self._send_json(api_ai_review_history(
        limit=self._int_arg(q, "limit", 30, 1, 200)))

def _h_ai_review_detail(self, parsed):
    q = urllib.parse.parse_qs(parsed.query)
    return self._send_json(api_ai_review_detail(
        review_id=self._int_arg(q, "id", 0, 0, 10 ** 9)))

def _h_stock_turnover(self, parsed):
    q = urllib.parse.parse_qs(parsed.query)
    return self._send_json(api_stock_turnover(symbol=self._str_arg(q, "symbol", "", maxlen=16),
                                              days=self._int_arg(q, "days", 30, 5, 120)))

def _h_stock_f10(self, parsed):
    q = urllib.parse.parse_qs(parsed.query)
    return self._send_json(api_stock_f10(symbol=self._str_arg(q, "symbol", "", maxlen=16)))

def _h_screener_bottom(self, parsed):
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

def _h_dragon_map(self, parsed):
    return self._send_json(api_dragon_map())

def _h_dragon(self, parsed):
    q = urllib.parse.parse_qs(parsed.query)
    return self._send_json(api_dragon_one(self._str_arg(q, "code", "", maxlen=12)))

def _h_lowbox_breakouts(self, parsed):
    return self._send_json(api_lowbox_breakouts())

def _h_valuation(self, parsed):
    q = urllib.parse.parse_qs(parsed.query)
    return self._send_json(api_valuation(self._str_arg(q, "symbol", "", maxlen=20)))

def _h_diag(self, parsed):
    q = urllib.parse.parse_qs(parsed.query)
    return self._send_json(api_diag(self._str_arg(q, "symbol", "", maxlen=20),
                                    force=self._int_arg(q, "force", 0, 0, 1)))

def _h_research_reports(self, parsed):
    q = urllib.parse.parse_qs(parsed.query)
    return self._send_json(api_research_reports(pz=self._int_arg(q, "pz", 30, 1, 100),
                                               page=self._int_arg(q, "page", 1, 1, 50)))

def _h_research_rating(self, parsed):
    q = urllib.parse.parse_qs(parsed.query)
    return self._send_json(api_research_rating(self._str_arg(q, "symbol", "", maxlen=20)))

def _h_research_insight(self, parsed):
    q = urllib.parse.parse_qs(parsed.query)
    return self._send_json(api_research_insight(days=self._int_arg(q, "days", 60, 7, 180),
                                               force=self._int_arg(q, "force", 0, 0, 1)))

def _h_multiquote_preset(self, parsed):
    return self._send_json(api_multiquote_preset())

def _h_multiquote_lookup(self, parsed):
    q = urllib.parse.parse_qs(parsed.query)
    return self._send_json(api_multiquote_lookup(self._str_arg(q, "symbol", "", maxlen=20)))

def _h_watchdesk(self, parsed):
    return self._send_json(api_watchdesk())

def _h_watchdesk_detail(self, parsed):
    q = urllib.parse.parse_qs(parsed.query)
    return self._send_json(api_watchdesk_detail(self._str_arg(q, "code", "", maxlen=20)))

def _h_watchdesk_scan(self, parsed):
    return self._send_json(api_watchdesk_scan())

def _h_news_stock(self, parsed):
    q = urllib.parse.parse_qs(parsed.query)
    return self._send_json(api_news_stock(self._str_arg(q, "symbol", "", maxlen=20),
                                         num=self._int_arg(q, "num", 30, 1, 100)))

def _h_news_market(self, parsed):
    q = urllib.parse.parse_qs(parsed.query)
    return self._send_json(api_news_market(num=self._int_arg(q, "num", 30, 1, 100)))

def _h_policy_desk(self, parsed):
    q = urllib.parse.parse_qs(parsed.query)
    return self._send_json(api_policy_desk(num=self._int_arg(q, "num", 80, 10, 200)))

def _h_douyin_overview(self, parsed):
    return self._send_json(api_douyin_overview())

def _h_douyin_list(self, parsed):
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

def _h_douyin_detail(self, parsed):
    q = urllib.parse.parse_qs(parsed.query)
    return self._send_json(api_douyin_detail(
        self._str_arg(q, "video_id", "", maxlen=40) or ""))

def _h_douyin_topics(self, parsed):
    return self._send_json(api_douyin_topics())

def _h_douyin_stances(self, parsed):
    return self._send_json(api_douyin_stances())

def _h_douyin_bloggers(self, parsed):
    return self._send_json(api_douyin_bloggers())

def _h_douyin_plate(self, parsed):
    q = urllib.parse.parse_qs(parsed.query)
    return self._send_json(api_douyin_plate(
        plate=self._str_arg(q, "plate", "", maxlen=32) or ""))

def _h_douyin_resonance(self, parsed):
    q = urllib.parse.parse_qs(parsed.query)
    return self._send_json(api_douyin_resonance(
        limit=self._int_arg(q, "limit", 40, 1, 60)))

def _h_funnel_score(self, parsed):
    q = urllib.parse.parse_qs(parsed.query)
    return self._send_json(api_funnel_score(
        plate=self._str_arg(q, "plate", "", 32) or "",
        mode=self._str_arg(q, "mode", "sentiment", 16) or "sentiment"))

def _h_avoid_flags(self, parsed):
    q = urllib.parse.parse_qs(parsed.query)
    return self._send_json(api_avoid_flags(
        codes=self._str_arg(q, "codes", "", 4000) or "",
        limit=self._int_arg(q, "limit", 100, 1, 500)))

def _h_lift_flags(self, parsed):
    q = urllib.parse.parse_qs(parsed.query)
    return self._send_json(api_lift_flags(
        codes=self._str_arg(q, "codes", "", 4000) or "",
        limit=self._int_arg(q, "limit", 100, 1, 500)))

def _h_evidence(self, parsed):
    return self._send_json(api_evidence())

def _h_review_score(self, parsed):
    return self._send_json(api_review_score())

def _h_ai_picks(self, parsed):
    q = urllib.parse.parse_qs(parsed.query)
    return self._send_json(api_ai_picks(
        date=self._str_arg(q, "date", "", maxlen=10),
        provider=self._str_arg(q, "provider", "", maxlen=16)))

def _h_hypothesis(self, parsed):
    q = urllib.parse.parse_qs(parsed.query)
    return self._send_json(api_hypothesis(
        limit=self._int_arg(q, "limit", 30, 1, 200)))

def _h_router_context(self, parsed):
    q = urllib.parse.parse_qs(parsed.query)
    return self._send_json(api_router_context(
        as_of=self._str_arg(q, "as_of", "", maxlen=16),
        horizon_days=self._int_arg(q, "horizon_days", 20, 1, 250),
        objective=self._str_arg(q, "objective", "research", maxlen=16),
        stock_code=self._str_arg(q, "stock_code", "", maxlen=10)))

def _h_router_plan(self, parsed):
    q = urllib.parse.parse_qs(parsed.query)
    return self._send_json(api_router_plan(
        as_of=self._str_arg(q, "as_of", "", maxlen=16),
        horizon_days=self._int_arg(q, "horizon_days", 20, 1, 250),
        objective=self._str_arg(q, "objective", "research", maxlen=16),
        stock_code=self._str_arg(q, "stock_code", "", maxlen=10),
        max_candidates=self._int_arg(q, "max_candidates", 30, 1, 200),
        risk_level=self._str_arg(q, "risk_level", "normal", maxlen=16)))

def _h_router_explain(self, parsed):
    q = urllib.parse.parse_qs(parsed.query)
    return self._send_json(api_router_explain(
        decision_id=self._str_arg(q, "decision_id", "", maxlen=40)))

def _h_router_explain_(self, parsed):
    q = urllib.parse.parse_qs(parsed.query)
    return self._send_json(api_router_explain(
        decision_id=self._str_arg(q, "decision_id", "", maxlen=40)))

def _h_router_decisions(self, parsed):
    q = urllib.parse.parse_qs(parsed.query)
    return self._send_json(api_router_decisions(
        limit=self._int_arg(q, "limit", 20, 1, 200)))

def _h_router_policy_status(self, parsed):
    return self._send_json(api_router_policy_status())

def _h_toolbox(self, parsed):
    return self._send_json(api_toolbox())

def _h_toolbox_funnel(self, parsed):
    q = urllib.parse.parse_qs(parsed.query)
    return self._send_json(api_toolbox_funnel(
        steps_json=self._str_arg(q, "steps", "", maxlen=20000),
        limit=self._int_arg(q, "limit", 200, 1, 500)))

def _h_toolbox_stack(self, parsed):
    q = urllib.parse.parse_qs(parsed.query)
    return self._send_json(api_toolbox_stack(
        limit=self._int_arg(q, "limit", 200, 1, 500),
        tools_json=self._str_arg(q, "tools", "", maxlen=20000)))

def _h_universe_box(self, parsed):
    q = urllib.parse.parse_qs(parsed.query)
    return self._send_json(api_universe_box(
        limit=self._int_arg(q, "limit", 40, 1, 500),
        min_dwell=self._float_arg(q, "min_dwell", 0.15, 0.0, 1.0),
        box_mode=self._str_arg(q, "box_mode", "", maxlen=16)))

def _h_ai_expert(self, parsed):
    q = urllib.parse.parse_qs(parsed.query)
    return self._send_json(api_ai_expert(
        date=self._str_arg(q, "date", "", maxlen=10),
        key=self._str_arg(q, "key", "", maxlen=32)))

def _h_ai_debate(self, parsed):
    q = urllib.parse.parse_qs(parsed.query)
    return self._send_json(api_ai_debate(
        mode=self._str_arg(q, "mode", "batch", maxlen=16),
        code=self._str_arg(q, "code", "", maxlen=8)))

def _h_factors_candidates(self, parsed):
    q = urllib.parse.parse_qs(parsed.query)
    return self._send_json(api_factors_candidates(
        include_rejected=self._str_arg(q, "include_rejected", "true", maxlen=8).lower() != "false"))

def _h_factors_sources(self, parsed):
    q = urllib.parse.parse_qs(parsed.query)
    return self._send_json(api_factors_sources(
        limit=self._int_arg(q, "limit", 40, 1, 200)))

def _h_fast_grab(self, parsed):
    q = urllib.parse.parse_qs(parsed.query)
    return self._send_json(api_fast_grab(date=self._str_arg(q, "date", "", maxlen=10)))

def _h_fast_grab_status(self, parsed):
    q = urllib.parse.parse_qs(parsed.query)
    return self._send_json(api_fast_grab_status(date=self._str_arg(q, "date", "", maxlen=10)))

ROUTE_HANDLERS = {
    "/api/version": _h_version,
    "/api/summary": _h_summary,
    "/api/stocks": _h_stocks,
    "/api/operating": _h_operating,
    "/api/persons": _h_persons,
    "/api/strategies": _h_strategies,
    "/api/fund_strategies": _h_fund_strategies,
    "/api/public_records": _h_public_records,
    "/api/sentiment": _h_sentiment,
    "/api/weibo_heat": _h_weibo_heat,
    "/api/snapshot": _h_snapshot,
    "/api/warehouse/stats": _h_warehouse_stats,
    "/api/topics": _h_topics,
    "/api/hotspots": _h_hotspots,
    "/api/refresh_status": _h_refresh_status,
    "/api/market_freshness": _h_market_freshness,
    "/api/params": _h_params,
    "/api/cost_basis": _h_cost_basis,
    "/api/health": _h_health,
    "/api/cost_calib": _h_cost_calib,
    "/api/scheduler/status": _h_scheduler_status,
    "/api/kb/list": _h_kb_list,
    "/api/raw_pages": _h_raw_pages,
    "/api/global_market": _h_global_market,
    "/api/overseas_sentiment": _h_overseas_sentiment,
    "/api/overseas_hotspots": _h_overseas_hotspots,
    "/api/overseas_sources": _h_overseas_sources,
    "/api/geo_refresh": _h_geo_refresh,
    "/api/freshness": _h_freshness,
    "/api/positions": _h_positions,
    "/api/today_decision": _h_today_decision,
    "/api/info_brief": _h_info_brief,
    "/api/watch_brief": _h_watch_brief,
    "/api/login_status": _h_login_status,
    "/api/person": _h_person,
    "/api/player": _h_player,
    "/api/stock": _h_stock,
    "/api/stock_names": _h_stock_names,
    "/api/moneyflow_stock": _h_moneyflow_stock,
    "/api/moneyflow_rank": _h_moneyflow_rank,
    "/api/moneyflow_board": _h_moneyflow_board,
    "/api/board_flow_snapshot": _h_board_flow_snapshot,
    "/api/board_flow_intraday": _h_board_flow_intraday,
    "/api/board_flow_range": _h_board_flow_range,
    "/api/board_flow_dates": _h_board_flow_dates,
    "/api/board_flow_status": _h_board_flow_status,
    "/api/board": _h_board,
    "/api/prediction/today": _h_prediction_today,
    "/api/prediction/scores": _h_prediction_scores,
    "/api/prediction/track": _h_prediction_track,
    "/api/growth/adjust_log": _h_growth_adjust_log,
    "/api/prediction/status": _h_prediction_status,
    "/api/prediction/dates": _h_prediction_dates,
    "/api/prediction/history": _h_prediction_history,
    "/api/prediction/run": _h_prediction_run,
    "/api/prediction/catchup": _h_prediction_catchup,
    "/api/pillars": _h_pillars,
    "/api/pillars/run": _h_pillars_run,
    "/api/pillars/status": _h_pillars_status,
    "/api/backtest": _h_backtest,
    "/api/backtest/status": _h_backtest_status,
    "/api/selfpick": _h_selfpick,
    "/api/selfpick/status": _h_selfpick_status,
    "/api/resonance": _h_resonance,
    "/api/resonance/status": _h_resonance_status,
    "/api/avoid": _h_avoid,
    "/api/harvest": _h_harvest,
    "/api/oss": _h_oss,
    "/api/selection": _h_selection,
    "/api/quant": _h_quant,
    "/api/quant/status": _h_quant_status,
    "/api/quant/panorama": _h_quant_panorama,
    "/api/quant/select": _h_quant_select,
    "/api/quant/backtest": _h_quant_backtest,
    "/api/quant/recipe": _h_quant_recipe,
    "/api/kronos": _h_kronos,
    "/api/kronos/status": _h_kronos_status,
    "/api/etf": _h_etf,
    "/api/etf/status": _h_etf_status,
    "/api/evolve": _h_evolve,
    "/api/evolve/status": _h_evolve_status,
    "/api/screener_boards": _h_screener_boards,
    "/api/screener_board_members": _h_screener_board_members,
    "/api/screener_decline": _h_screener_decline,
    "/api/ai_review": _h_ai_review,
    "/api/ai_review/history": _h_ai_review_history,
    "/api/ai_review/detail": _h_ai_review_detail,
    "/api/stock_turnover": _h_stock_turnover,
    "/api/stock_f10": _h_stock_f10,
    "/api/screener_bottom": _h_screener_bottom,
    "/api/dragon_map": _h_dragon_map,
    "/api/dragon": _h_dragon,
    "/api/lowbox_breakouts": _h_lowbox_breakouts,
    "/api/valuation": _h_valuation,
    "/api/diag": _h_diag,
    "/api/research_reports": _h_research_reports,
    "/api/research_rating": _h_research_rating,
    "/api/research/insight": _h_research_insight,
    "/api/multiquote_preset": _h_multiquote_preset,
    "/api/multiquote_lookup": _h_multiquote_lookup,
    "/api/watchdesk": _h_watchdesk,
    "/api/watchdesk/detail": _h_watchdesk_detail,
    "/api/watchdesk/scan": _h_watchdesk_scan,
    "/api/news_stock": _h_news_stock,
    "/api/news_market": _h_news_market,
    "/api/policy_desk": _h_policy_desk,
    "/api/douyin/overview": _h_douyin_overview,
    "/api/douyin/list": _h_douyin_list,
    "/api/douyin/detail": _h_douyin_detail,
    "/api/douyin/topics": _h_douyin_topics,
    "/api/douyin/stances": _h_douyin_stances,
    "/api/douyin/bloggers": _h_douyin_bloggers,
    "/api/douyin/plate": _h_douyin_plate,
    "/api/douyin/resonance": _h_douyin_resonance,
    "/api/funnel/score": _h_funnel_score,
    "/api/avoid_flags": _h_avoid_flags,
    "/api/lift_flags": _h_lift_flags,
    "/api/evidence": _h_evidence,
    "/api/review_score": _h_review_score,
    "/api/ai_picks": _h_ai_picks,
    "/api/hypothesis": _h_hypothesis,
    "/api/router/context": _h_router_context,
    "/api/router/plan": _h_router_plan,
    "/api/router/explain": _h_router_explain,
    "/api/router/explain/": _h_router_explain_,
    "/api/router/decisions": _h_router_decisions,
    "/api/router/policy/status": _h_router_policy_status,
    "/api/toolbox": _h_toolbox,
    "/api/toolbox/funnel": _h_toolbox_funnel,
    "/api/toolbox/stack": _h_toolbox_stack,
    "/api/universe_box": _h_universe_box,
    "/api/ai_expert": _h_ai_expert,
    "/api/ai_debate": _h_ai_debate,
    "/api/factors/candidates": _h_factors_candidates,
    "/api/factors/sources": _h_factors_sources,
    "/api/fast_grab": _h_fast_grab,
    "/api/fast_grab/status": _h_fast_grab_status,
}


class RouteDispatch:
    """路由分发（无状态，靠 self 访问 Handler 的辅助方法）。"""

    def route_get(self, parsed):
        _ensure_bound()
        route = parsed.path
        if route == "/api/config/get":
            q = urllib.parse.parse_qs(parsed.query)
            key = (q.get("key") or [""])[0]
            return self._send_json(api_config_get(key))

        self._ttl_key = None
        if route in _TTL_ROUTES:
            _qs = parsed.query or ""
            if ("t=" not in _qs) and ("force=" not in _qs):
                _key = route + "?" + _qs
                _hit = _TTL_CACHE.get(_key)
                _is_swr = (self.headers.get("X-SWR") == "1")
                if _hit is not None and (time.monotonic() - _hit[0]) < _TTL_ROUTES[route]:
                    return self._send_json(_hit[1])
                if _hit is not None and not _is_swr:
                    # ★ SWR：过期了也**先给旧值**，后台重算（见 _swr_refresh_async 文档串）。
                    #   绝不把「重聚合的 3 秒」压在用户这次点击上。
                    _swr_refresh_async(_key)
                    _resp = dict(_hit[1]) if isinstance(_hit[1], dict) else _hit[1]
                    if isinstance(_resp, dict):
                        _resp["_stale"] = True   # 诚实标注：本次是旧值，后台正在刷新
                    return self._send_json(_resp)
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
        # 2026-11-07（#4 路由注册表统一·分批）：以下路由已迁入 ROUTE_HANDLERS 精确查表，
        # 命中即返回。上方 _TTL_ROUTES 缓存拦截已先行执行，TTL 路由的缓存行为不变。
        _rh = ROUTE_HANDLERS.get(route)
        if _rh is not None:
            return _rh(self, parsed)
        if route == "/api/indicators/signals":  # P1：因子买卖信号候选
            q = urllib.parse.parse_qs(parsed.query)
            direction = self._str_arg(q, "dir", "buy", maxlen=4)
            limit = self._int_arg(q, "limit", 60, 1, 500)
            return self._send_json(api_indicators_signals(direction, limit))
        if route == "/api/indicators":          # P1：单只票最新因子快照
            q = urllib.parse.parse_qs(parsed.query)
            code = self._str_arg(q, "code", "", maxlen=8) or None
            return self._send_json(api_indicators(code))
        if route == "/api/universe/scan":        # P2：全宇宙多因子扫描结果
            q = urllib.parse.parse_qs(parsed.query)
            tier = self._str_arg(q, "tier", "", maxlen=1) or None
            limit = self._int_arg(q, "limit", 200, 1, 3000)
            return self._send_json(api_universe_scan(tier, limit))
        if route == "/api/strategy/templates":     # P5：现成策略模板选股结果
            q = urllib.parse.parse_qs(parsed.query)
            template = self._str_arg(q, "template", "", maxlen=24) or None
            limit = self._int_arg(q, "limit", 200, 1, 3000)
            return self._send_json(api_strategy_templates(template, limit))
        if route == "/api/signal/evidence":        # 2026-10-29：信号战绩（大样本回测 + 基线超额）
            _q_ev = urllib.parse.parse_qs(parsed.query)
            return self._send_json(api_signal_evidence(
                fwd=self._int_arg(_q_ev, "fwd", 5, 1, 20)))
        if route == "/api/signal/regime":          # 2026-10-29：分市况表现（当前市况下好不好用）
            _q_rg = urllib.parse.parse_qs(parsed.query)
            return self._send_json(api_signal_regime(
                fwd=self._int_arg(_q_rg, "fwd", 10, 1, 20)))
        if route == "/api/plate_heat":             # 2026-10-29：板块热度榜（荐股降级为注意力）
            _q_ph = urllib.parse.parse_qs(parsed.query)
            return self._send_json(api_plate_heat(
                rebuild=str((_q_ph.get("rebuild") or ["0"])[0]) not in ("", "0", "false", "False")))
        if route == "/api/lhb_weather":            # 2026-10-30 D35 阶段二：资金风向（只读表）
            _q_lw = urllib.parse.parse_qs(parsed.query)
            return self._send_json(api_lhb_weather(
                days=self._int_arg(_q_lw, "days", 120, 20, 400)))
        if route == "/api/signal/combo":
            # 2026-10-31 决策③：信号两两交集（只返回样本足够的行）
            _q_cb = urllib.parse.parse_qs(parsed.query)
            return self._send_json(api_signal_combo(
                fwd=self._int_arg(_q_cb, "fwd", 0, 0, 250) or None,
                limit=self._int_arg(_q_cb, "limit", 60, 1, 500)))
        if route == "/api/signal/backtest":        # P4：信号 alpha 回测实证（此前未暴露给前端）
            # 2026-10-29 bugfix：原来直接用了上面某个分支才赋值的 `q`，单独打这个接口会抛
            # UnboundLocalError（日志实证 500），这里自己解析 query，不再依赖别处。
            _q_sb = urllib.parse.parse_qs(parsed.query)
            return self._send_json(api_signal_backtest(limit=self._int_arg(_q_sb, "limit", 200, 1, 1000)))
        if route == "/api/executability":
            # 2026-10-31 新增：四字段 → 逐票可行性 + 容量区间。
            # 缺字段一律传 None，让引擎自己报 missing_required —— 绝不在这里补默认值：
            # 用默认 AUM/持有期算出来的"可行"，是给别人看的可行。
            _q_ex = urllib.parse.parse_qs(parsed.query)
            _nums, _bad = {}, None
            # 2026-10-31 决策④：days 不再强制转数字 —— 它支持 "3,10,20" 多值（三档对比）。
            # 原来这里对它做 float()，"3,10,20" 会直接变成 bad_number，
            # 三档功能在后端根本走不到。数值校验交给引擎（evaluate 内部会 int()）。
            for _k in ("aum", "n", "fee"):
                _raw = _q_ex.get(_k, [None])[0]
                if _raw is None or str(_raw).strip() == "":
                    _nums[_k] = None
                else:
                    try:
                        _nums[_k] = float(str(_raw).strip())
                    except (TypeError, ValueError):
                        _bad = _k
                        break
            if _bad:
                return self._send_json(
                    {"ok": False, "error": "bad_number", "field": _bad}, 400)
            _days = self._str_arg(_q_ex, "days", None, maxlen=40)
            return self._send_json(api_executability(
                self._str_arg(_q_ex, "codes", "", maxlen=800),
                _nums["aum"],
                None if _nums["n"] is None else int(_nums["n"]),
                _days,
                _nums["fee"]))
        if route == "/api/shadow_weights":
            # 2026-10-31 决策②：影子权重 vs 生产权重（只读，不生效）
            _q_sh = urllib.parse.parse_qs(parsed.query)
            return self._send_json(api_shadow_weights(self._int_arg(_q_sh, "days", 20, 1, 120)))
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
        if route == "/api/geo_events":
            q = urllib.parse.parse_qs(parsed.query)
            cat = self._str_arg(q, "category", "", maxlen=32) or None
            days = self._int_arg(q, "days", 14, 1, 180)
            panel = self._str_arg(q, "panel", "", maxlen=16) or None
            ndays = self._int_arg(q, "news_days", 10, 1, 60)
            nlim = self._int_arg(q, "news_limit", 40, 5, 100)
            return self._send_json(api_geo_events(category=cat, days=days, panel=panel,
                                                 news_days=ndays, news_limit=nlim))
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
        if route == "/api/smart_money":
            # P0-E 别人怎么做：聪明钱共识榜（多人同时买的方向 + 个股）
            q = urllib.parse.parse_qs(parsed.query)
            mad = self._int_arg(q, "max_age_days", 0, 0, 3650)
            return self._send_json(api_smart_money(max_age_days=(mad or None)))
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
        # ── 2026-10-24 供应链瓶颈猎手：主题 → 瓶颈地图 + 机会排名（纯标准库，KB 驱动）──
        if route == "/api/bottleneck":
            if bn is None:
                return self._send_json({"ok": False, "error": "bottleneck 未加载"}, code=500)
            q = urllib.parse.parse_qs(parsed.query)
            theme = q.get("theme", [""])[0]
            top = self._int_arg(q, "top", 0, 0, 50)
            return self._send_json(bn.get_bottleneck(theme=theme or None, top=top or None))
        if route == "/api/valuation_cache/status":
            if se is None:
                return self._send_json({"ok": False, "error": "screener_extra 未加载"}, code=500)
            return self._send_json(se.valuation_cache_status())
        if route == "/api/settings/deepseek_key":
            if se is None:
                return self._send_json({"ok": False, "error": "screener_extra 未加载"}, code=500)
            return self._send_json({"configured": bool(se.get_deepseek_key())})
        if route == "/api/screener_turnover_top":
            q = urllib.parse.parse_qs(parsed.query)
            _to_params = {"days": self._int_arg(q, "days", 30, 5, 120),
                          "limit": self._int_arg(q, "limit", 30, 10, 100)}
            # 2026-10-29：全市场 5200+ 只的换手评分，服务进程内实测 14 秒（独立跑 2.6 秒），
            # 改为「子进程算 + 按参数签名落盘 + 请求只读」，首次打开回「计算中」由前端轮询接上。
            return self._send_json(_variant_cached("/api/screener_turnover_top", _to_params))
        # ── 2026-10-13 板块二：长期低位震荡选股（全量版：基本面+国资系+突破雷达）──
        if route == "/api/screener_lowbox":
            q = urllib.parse.parse_qs(parsed.query)
            _lb_params = {
                "codes": self._str_arg(q, "codes", "", maxlen=400),
                "limit": self._int_arg(q, "limit", 30, 5, 100),
                "box_window": self._int_arg(q, "box_window", 750, 0, 3000),
                "low_pct": self._float_arg(q, "low_pct", 30.0, 5.0, 80.0),
                "quality": self._str_arg(q, "quality", "off", maxlen=8),
                "w_shape": self._float_arg(q, "w_shape", 40.0, 0.0, 100.0),
                "w_fund": self._float_arg(q, "w_fund", 35.0, 0.0, 100.0),
                "w_worth": self._float_arg(q, "w_worth", 25.0, 0.0, 100.0),
            }
            # 2026-10-29：改读落盘结果（子进程算），绝不在请求线程里跑 33 秒纯 Python
            return self._send_json(_variant_cached("/api/screener_lowbox", _lb_params))
        if route == "/api/rate/timeline":        # 2026-10-05 新板块「入手概率」时间轴
            q = urllib.parse.parse_qs(parsed.query)
            return self._send_json(api_rate_timeline(
                days_window=self._int_arg(q, "days_window", 10, 1, 60),
                per_day=self._int_arg(q, "per_day", 5, 1, 30)))
        if route == "/api/douyin/facets":
            q = urllib.parse.parse_qs(parsed.query)
            dm = self._str_arg(q, "domain", "tech", maxlen=16) or "tech"
            return self._send_json(api_douyin_facets(domain=dm))
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
        if route == "/api/bili/consensus":
            q = urllib.parse.parse_qs(parsed.query)
            ct = self._str_arg(q, "cat", "sentiment", maxlen=32) or "sentiment"
            dy = self._int_arg(q, "days", 30, 3, 365)
            return self._send_json(bili_consensus.digest(cat=ct, days=dy))
        self._send_json({"error": "not found"}, code=404)

    def route_post(self, parsed, payload):
        _ensure_bound()
        route = parsed.path
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
        # 2026-11-02：通用大脑 Key（deepseek / hunyuan / qwen），写完立刻测连通性
        if route == "/api/ai_review/provider_key":
            prov = (payload.get("provider") or "").strip()
            key = (payload.get("key") or "").strip()
            if not prov:
                return self._send_json({"ok": False, "err": "缺少 provider"}, code=400)
            return self._send_json(api_ai_provider_key(prov, key))
        if route == "/api/ai_review/provider_test":
            prov = (payload.get("provider") or "").strip()
            return self._send_json(api_ai_provider_test(prov))
        # 2026-10-29 进哥需求：强制重算 AI 复盘（走 POST，因为会写库 + 花 token）
        if route == "/api/ai_review/run":
            q = urllib.parse.parse_qs(parsed.query)
            return self._send_json(api_ai_review_run(
                provider=self._str_arg(q, "provider", "", maxlen=16),
                date=self._str_arg(q, "date", "", maxlen=10),
                since_id=self._int_arg(q, "since_id", 0, 0, 10 ** 9)))
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
        # 2026-10-24 Kronos K线预测：run 后台线程（CPU 推理是分钟级，同步必被浏览器/网关掐成超时）
        if route == "/api/kronos/run":
            q = urllib.parse.parse_qs(parsed.query)
            _pfx = [p for p in self._str_arg(q, "prefixes", "", 32).split(",")
                    if p in ("0", "3", "6")]
            _codes = [c.strip() for c in self._str_arg(q, "codes", "", 4096).split(",") if c.strip()]
            return self._send_json(kronos.run_async(
                DB.conn, force=bool(self._int_arg(q, "force", 1, 0, 1)),
                top_n=self._int_arg(q, "top_n", 30, 5, 300),
                prefixes=(_pfx or None),
                fast=bool(self._int_arg(q, "fast", 1, 0, 1)),
                only_codes=(_codes or None)))
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
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),  # 2026-10-25：别弹黑框
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
                            env=_subproc_env(),
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
                    # keep=2（2026-10-31）：库已 9GB，留 5 份等于 45GB。留最近两份足够回滚到上一次刷新前。
                    snap = DB.snapshot(prefix="app.db.pre-overseas", keep=2)
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
        # ---- 自选 / 关注池 + 到价提醒（2026-09-27 新增）----
        # 2026-10-31 修复（QA 实测：每 90s 的到价轮询会让下一次 watch_list 读取等 15.4s）：
        #   /api/watch_check 原先和分组/标的写操作并列在下面的"写后失效"名单里，于是
        #   startWatchPoll 每 90s 一次就把 watch_list 内层缓存整条删掉，紧随其后的读取要走
        #   全量重算（取价竞速三源 + 值得率复算 + 战法复算）。修复前这被 _TTL_ROUTES 那层
        #   HTTP 缓存掩盖（秒回旧 body），两层缓存拆掉后立刻暴露。
        #   watch_check 实际只写 watch_items 的 last_price / last_pct / last_checked_at 和
        #   watch_alerts 表 —— 既不动分组结构，也不是 watch_list 慢的原因。故把它单独拎出来：
        #   照旧检测，但**不整条删缓存**，只把被改动的几列就地补丁进缓存，字段不滞后、也不重算。
        if route == "/api/watch_check":
            r = api_watch_check()
            if r.get("ok"):
                _refresh_watch_list_cache_after_check()
            return self._send_json(r)
        # 2026-10-14 性能修复：任一会改变关注池 / 分组的写入成功后，立即清掉 watch_list 缓存，
        # 保证下次加载（或前端乐观更新后的兜底刷新）拿到的是新数据，而非 5 分钟内的旧快照。
        if route in ("/api/watch_item_add", "/api/watch_item_update", "/api/watch_item_remove",
                     "/api/watch_item_reorder", "/api/watch_item_group_remove",
                     "/api/watch_group_add", "/api/watch_group_rename",
                     "/api/watch_group_remove", "/api/watch_group_reorder"):
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
            }[route]
            r = handler(payload)
            if r.get("ok"):
                _api_cache_invalidate("watch_list")
            return self._send_json(r)
        if route == "/api/watch_alert_read":
            return self._send_json(api_watch_alert_read(payload))
        # ---- 重构恢复：审计期桥接端点（POST 状态变更类）----
        if route == "/api/toolbox/status":
            return self._send_json(api_toolbox_status(payload))
        if route == "/api/factors/admit":
            return self._send_json(api_factors_admit(payload))
        if route == "/api/factors/source_status":
            return self._send_json(api_factors_source_status(payload))
        if route == "/api/ai_debate/run":
            q = urllib.parse.parse_qs(parsed.query)
            return self._send_json(api_ai_debate_run(
                pool=self._int_arg(q, "pool", 0, 0, 200),
                final_min=self._int_arg(q, "final_min", 0, 0, 100),
                final_max=self._int_arg(q, "final_max", 0, 0, 100),
                provider=self._str_arg(q, "provider", "", maxlen=16)))
        if route == "/api/ai_debate/single":
            q = urllib.parse.parse_qs(parsed.query)
            return self._send_json(api_ai_debate_single(
                code=self._str_arg(q, "code", "", maxlen=8),
                provider=self._str_arg(q, "provider", "", maxlen=16)))
        self._send_json({"error": "not found"}, code=404)


def api_weibo_heat(limit: int = 60) -> dict:
    """只读：金十微博个股人气指数最新一期。

    返回 {ok, trade_date, timescale, items:[{code, name, rate, raw_name}]}。
    数据来自 weibo_heat 表（fetch_weibo_heat 落库），LEFT JOIN stock_names 取标准名。
    这是注意力/热度信号（非全文情绪），属数据输入层，不在此处假定其判据价值。
    """
    from viewer.db import get_main_conn
    try:
        conn = get_main_conn(read_only=True)
        try:
            row = conn.execute(
                "SELECT trade_date, timescale FROM weibo_heat "
                "ORDER BY trade_date DESC, fetched_at DESC LIMIT 1"
            ).fetchone()
            if not row:
                return {"ok": True, "trade_date": None, "timescale": None, "items": []}
            trade_date, timescale = row
            rows = conn.execute(
                "SELECT wh.code, wh.rate, wh.raw_name, sn.name "
                "FROM weibo_heat wh LEFT JOIN stock_names sn ON sn.code = wh.code "
                "WHERE wh.trade_date = ? AND wh.timescale = ? "
                "ORDER BY ABS(wh.rate) DESC, wh.rate DESC LIMIT ?",
                (trade_date, timescale, int(limit)),
            ).fetchall()
            items = [
                {"code": r[0], "rate": r[1], "raw_name": r[2], "name": r[3] or r[2]}
                for r in rows
            ]
            return {"ok": True, "trade_date": trade_date, "timescale": timescale, "items": items}
        finally:
            conn.close()
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"{type(e).__name__}: {e}"[:200]}


# ── 路由注册表（只读清单，供测试 / 文档 / 前端契约使用）──
GET_ROUTES = ['/api/ai_picks', '/api/ai_review', '/api/ai_review/detail', '/api/ai_review/history', '/api/auction', '/api/auction/', '/api/avoid', '/api/avoid_flags', '/api/backtest', '/api/backtest/status', '/api/bili/consensus', '/api/bili/methods', '/api/bili/pile', '/api/bili/scan', '/api/board', '/api/board_flow_dates', '/api/board_flow_intraday', '/api/board_flow_range', '/api/board_flow_snapshot', '/api/board_flow_status', '/api/bottleneck', '/api/branch_detail', '/api/cohold_recent', '/api/config/get', '/api/consensus', '/api/cost_basis', '/api/cost_calib', '/api/dashboard', '/api/detail', '/api/detail_list', '/api/diag', '/api/douyin/bloggers', '/api/douyin/detail', '/api/douyin/facets', '/api/douyin/list', '/api/douyin/overview', '/api/douyin/plate', '/api/douyin/resonance', '/api/douyin/stances', '/api/douyin/topics', '/api/dragon', '/api/dragon_map', '/api/etf', '/api/etf/status', '/api/evidence', '/api/evolve', '/api/evolve/status', '/api/executability', '/api/freshness', '/api/fund_strategies', '/api/funnel/score', '/api/geo_events', '/api/geo_refresh', '/api/global_market', '/api/growth/adjust_log', '/api/harvest', '/api/health', '/api/hotspots', '/api/hypothesis', '/api/indicators', '/api/indicators/signals', '/api/industry_leaders', '/api/info_brief', '/api/kb/doc', '/api/kb/list', '/api/kline', '/api/kronos', '/api/kronos/status', '/api/lhb', '/api/lhb_sectors', '/api/lhb_weather', '/api/lift_flags', '/api/login_status', '/api/lowbox_breakouts', '/api/market_cache/', '/api/market_cache/kline', '/api/market_cache/prefetch', '/api/market_cache/quote', '/api/market_cache/status', '/api/market_cache/symbols', '/api/market_freshness', '/api/moneyflow_board', '/api/moneyflow_rank', '/api/moneyflow_stock', '/api/multiquote_lookup', '/api/multiquote_preset', '/api/news_market', '/api/news_stock', '/api/niusan', '/api/operating', '/api/oss', '/api/overseas_hotspots', '/api/overseas_sentiment', '/api/overseas_sources', '/api/params', '/api/person', '/api/persons', '/api/pillars', '/api/pillars/run', '/api/pillars/status', '/api/plate_heat', '/api/player', '/api/players', '/api/policy_desk', '/api/positions', '/api/postdigest', '/api/posts', '/api/prediction/catchup', '/api/prediction/dates', '/api/prediction/history', '/api/prediction/run', '/api/prediction/scores', '/api/prediction/status', '/api/prediction/today', '/api/prediction/track', '/api/public_records', '/api/quant', '/api/quant/backtest', '/api/quant/panorama', '/api/quant/recipe', '/api/quant/select', '/api/quant/status', '/api/quote', '/api/rate/timeline', '/api/raw_pages', '/api/refresh_status', '/api/research/insight', '/api/research_rating', '/api/research_reports', '/api/resonance', '/api/resonance/status', '/api/review_score', '/api/scheduler/status', '/api/screener', '/api/screener/backtest', '/api/screener/themes', '/api/screener/valuation', '/api/screener_board_members', '/api/screener_boards', '/api/screener_bottom', '/api/screener_decline', '/api/screener_lowbox', '/api/screener_turnover_top', '/api/selection', '/api/selfpick', '/api/selfpick/status', '/api/sentiment', '/api/settings/deepseek_key', '/api/shadow_weights', '/api/signal/backtest', '/api/signal/combo', '/api/signal/evidence', '/api/signal/regime', '/api/smart_money', '/api/snapshot', '/api/star_moves', '/api/stock', '/api/stock_f10', '/api/stock_names', '/api/stock_turnover', '/api/stocks', '/api/strategies', '/api/strategy/templates', '/api/summary', '/api/today_decision', '/api/topics', '/api/universe/scan', '/api/valuation', '/api/valuation_cache/status', '/api/version', '/api/warehouse/stats', '/api/watch_alerts', '/api/watch_brief', '/api/watch_list', '/api/watchdesk', '/api/watchdesk/detail', '/api/watchdesk/scan', '/api/weibo_heat',
 '/api/router/context', '/api/router/decisions', '/api/router/explain', '/api/router/explain/',
 '/api/router/plan', '/api/router/policy/status', '/api/toolbox', '/api/toolbox/funnel',
 '/api/toolbox/stack', '/api/universe_box', '/api/ai_expert', '/api/ai_debate',
 '/api/factors/candidates', '/api/factors/sources', '/api/fast_grab', '/api/fast_grab/status']

POST_ROUTES = ['/api/ai_review/provider_key', '/api/ai_review/provider_test', '/api/ai_review/run', '/api/backup', '/api/backup/restore', '/api/config/set', '/api/edge/restart', '/api/etf/run', '/api/evolve/run', '/api/fetch', '/api/kb/reload', '/api/kronos/run', '/api/lhb/backfill', '/api/login/import', '/api/login/open', '/api/login_check', '/api/nl_screener', '/api/open_dir', '/api/overseas', '/api/pillars/run', '/api/prediction/catchup', '/api/prediction/run', '/api/quant/recipe', '/api/quant/recipe/apply', '/api/quant/run', '/api/rate/buypoint', '/api/rate/snapshot', '/api/resonance/run', '/api/scheduler/toggle', '/api/settings/deepseek_key', '/api/shutdown', '/api/sync_names', '/api/watch_alert_read', '/api/watch_check', '/api/watch_group_add', '/api/watch_group_remove', '/api/watch_group_rename', '/api/watch_group_reorder', '/api/watch_item_add', '/api/watch_item_group_remove', '/api/watch_item_remove', '/api/watch_item_reorder', '/api/watch_item_update',
 '/api/toolbox/status', '/api/factors/admit', '/api/factors/source_status', '/api/ai_debate/run', '/api/ai_debate/single']
