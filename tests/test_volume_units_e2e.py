# -*- coding: utf-8 -*-
"""成交量单位（手 / 股）端到端回归 —— 2026-10-31 测评 P0。

## 事故

600519 / 数据日 2026-10-30：`GET /api/kline?symbol=600519&period=day&limit=2` 返回
`volume = 5,992,729`，这个数的真实单位是**股**；行情区（东财 spot f47）显示
「5.99 万手」＝59,927 手（对）。抽屉 K 线区把同一个数当「手」格式化，显示
「599.27 万手」——**差整整 100 倍，且不报错**。

根因不是格式化函数写错，而是**接口边界没声明单位**：各源单位本来就不一致
（实测同一天：新浪/本地库给股、腾讯给手、东财科创板给股），前端只有一套「手」
格式化，只能猜。

## 本文件守三层

1. **纯函数层（不依赖网络）**：用实测 fixture 断言自证能把股/手判对；判不出必须给
   `None`，不许回退默认倍数（错 100 倍比「不知道」危险得多）。
2. **DTO 契约层**：`/api/kline` 与 `/api/quote` 必须自带 `volume_unit`，
   前端才有东西可读（而不是继续猜）。
3. **前端静态层**：`siVol` 必须接受第二个参数（单位），调用点不得只传一个参数。

网络类断言（真实站点）在拿不到网络时自动 skip —— 纯函数与静态断言永远会跑，
保证脱离网络也能复现/防回归。
"""
from __future__ import annotations

import copy
import io
import os
import re
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
VIEWER = os.path.join(ROOT, "viewer")
if VIEWER not in sys.path:
    sys.path.insert(0, VIEWER)

import units as UNITS                      # noqa: E402 —— viewer/units.py
from viewer import server as SRV           # noqa: E402
import screener as SCR                     # noqa: E402 —— 换手率评分（同一条根因的下游）

APP_JS = os.path.join(ROOT, "viewer", "static", "app.js")

# ---------------------------------------------------------------------------
# 实测 fixture（2026-10-30 收盘后从真实接口抓取，原文见交付说明）
#   新浪 K 线 600519：5,992,729（股）｜腾讯 K 线 600519：59,927（手）
#   新浪实时 hq 600519：volume=5,992,729(股) amount=8,257,381,778(元)
#   腾讯实时 600519：volume=59,927(手)
#   本地快照 600519：volume=59,927（自证=手）→ 股 = 5,992,700
# ---------------------------------------------------------------------------
SPOT_600519_HAND = 59927.0                  # 行情区显示的量（手）
ANCHOR_600519_SHARES = 5992700.0            # 同一交易日的真实成交量（股）

# 新浪/腾讯 K 线 payload 的形态：amount 是 close×volume 估算值（amount_est=True）
FIX_600519_SINA = {
    "symbol": "600519", "code": "600519", "name": "", "period": "day",
    "count": 2, "adjust": "raw", "amount_est": True, "full_history": True,
    "klines": [
        {"date": "2026-10-29", "open": 1349.99, "high": 1356.0, "low": 1330.01,
         "close": 1354.0, "volume": 4299231.0, "amount": 5821158774.0, "pct": 1.75},
        {"date": "2026-10-30", "open": 1363.0, "high": 1393.37, "low": 1352.98,
         "close": 1392.05, "volume": 5992729.0, "amount": 8342178404.45, "pct": 2.81},
    ],
    "current": {"price": 1392.05, "pct": 2.81, "open": 1363.0, "high": 1393.37,
                "low": 1352.98, "prev_close": 1354.0, "volume": 5992729.0,
                "amount": 8342178404.45, "date": "2026-10-30"},
}

# 腾讯 K 线 600519：量是**手**（与新浪同一天同票，差 100 倍）
FIX_600519_TENCENT = {
    "symbol": "600519", "period": "day", "amount_est": True,
    "klines": [
        {"date": "2026-10-30", "open": 1363.0, "high": 1393.37, "low": 1352.98,
         "close": 1392.05, "volume": 59927.0, "amount": 83421380.35, "pct": 2.81},
    ],
    "current": {"price": 1392.05, "volume": 59927.0, "date": "2026-10-30"},
}

# 科创板 688981 中芯国际：同一批源给的却是**股**（29,998,989），验证判据不看代码前缀
FIX_688981 = {
    "symbol": "688981", "period": "day", "amount_est": True,
    "klines": [
        {"date": "2026-10-30", "open": 99.95, "high": 100.76, "low": 96.36,
         "close": 96.79, "volume": 29998989.0, "amount": 2903602145.31, "pct": -4.17},
    ],
    "current": {"price": 96.79, "volume": 29998989.0, "date": "2026-10-30"},
}
ANCHOR_688981_SHARES = 29998989.0

# 北交所 920819 颖泰生物：源给的是**手**（43,778）
FIX_920819 = {
    "symbol": "920819", "period": "day", "amount_est": True,
    "klines": [
        {"date": "2026-10-30", "open": 2.72, "high": 2.77, "low": 2.65,
         "close": 2.65, "volume": 43778.0, "amount": 11601114.35, "pct": -2.21},
    ],
    "current": {"price": 2.65, "volume": 43778.0, "date": "2026-10-30"},
}
ANCHOR_920819_SHARES = 4377779.0


def _norm(payload: dict, anchor) -> dict:
    """跑出口归一（显式传锚点：不碰网络/DB，测试可离线复现）。"""
    return SRV._normalize_kline_volume(copy.deepcopy(payload), anchor=anchor)


# ---------------------------------------------------------------- 纯函数层
def test_600519_kline_units_are_shares_and_match_spot():
    """600519：K 线量必须归一到股，且换算成手后与行情区一致（相对误差 < 1%）。"""
    d = _norm(FIX_600519_SINA, ANCHOR_600519_SHARES)
    assert d["volume_unit"] == "share"
    assert d["volume_asof"] == "2026-10-30"
    last = d["klines"][-1]
    assert last["volume"] == pytest.approx(5992729.0)          # 原本就是股 → 不变
    assert last["volume_unit"] == "share"
    cur = d["current"]
    assert cur["volume_unit"] == "share"
    assert cur["volume_shares"] == pytest.approx(5992729.0)
    assert cur["volume_hand"] == pytest.approx(5992729.0 / 100.0)
    # 与行情区（手）对账：这正是当初差 100 倍的那一对数
    assert abs(cur["volume_hand"] - SPOT_600519_HAND) / SPOT_600519_HAND < 0.01
    assert 2e6 <= cur["volume_shares"] <= 2e7                  # 茅台单日 200 万~2000 万股


def test_600519_tencent_hand_is_converted_not_relabelled():
    """同一天腾讯给的是「手」：必须 ×100 换成股，而不是贴个「股」标签了事。"""
    d = _norm(FIX_600519_TENCENT, ANCHOR_600519_SHARES)
    last = d["klines"][-1]
    assert last["volume_native"] == pytest.approx(59927.0)
    assert last["volume_native_unit"] == "hand"
    assert last["volume"] == pytest.approx(5992700.0)           # 手 → 股
    assert last["volume_unit"] == "share"
    assert abs(d["current"]["volume_hand"] - SPOT_600519_HAND) / SPOT_600519_HAND < 0.01


def test_688_kline_units_do_not_depend_on_code_prefix():
    """科创板同一批源给的是股 —— 判据必须来自数据自证，不是「688 就按股」的映射表。"""
    d = _norm(FIX_688981, ANCHOR_688981_SHARES)
    assert d["volume_unit"] == "share"
    assert d["klines"][-1]["volume"] == pytest.approx(29998989.0)
    assert d["klines"][-1]["volume_native_unit"] == "share"
    assert d["current"]["volume_hand"] == pytest.approx(299989.89)


def test_920_kline_units_do_not_depend_on_code_prefix():
    """北交所 920 段是「最常被漏掉的一段」，判据同样只能靠自证。"""
    d = _norm(FIX_920819, ANCHOR_920819_SHARES)
    assert d["volume_unit"] == "share"
    assert d["klines"][-1]["volume"] == pytest.approx(4377800.0)   # 43,778 手 × 100
    assert d["klines"][-1]["volume_native"] == pytest.approx(43778.0)


def test_undecidable_unit_yields_null_not_guess():
    """判不出单位 → volume 置 None 并标 unknown；不许留原值，更不许当 0。"""
    payload = {
        "symbol": "999999", "period": "day", "amount_est": True,
        "klines": [{"date": "2026-10-30", "open": 1.0, "high": 1.1, "low": 0.9,
                    "close": 1.0, "volume": 123456.0, "amount": None}],
        "current": {"price": 1.0, "volume": 123456.0, "date": "2026-10-30"},
    }
    d = _norm(payload, None)          # 没有 amount、也没有锚点
    row = d["klines"][-1]
    assert row["volume"] is None
    assert row["volume_unit"] is None
    assert d["volume_unit"] is None
    assert d["volume_unit_unknown"] is True
    assert d["current"]["volume_shares"] is None
    # 源值仍在 volume_native 里（对账要用），但语义字段 volume 已清空
    assert row["volume_native"] == pytest.approx(123456.0)


def test_series_unit_from_real_amount():
    """有真实成交额时可自证：amount/close ≈ volume → 股；≈ volume/100 → 手。"""
    share_rows = [{"close": 10.0, "volume": 1.0e6, "amount": 1.0e7}] * 3
    hand_rows = [{"close": 10.0, "volume": 1.0e4, "amount": 1.0e7}] * 3
    assert UNITS.kline_series_unit(share_rows) == "share"
    assert UNITS.kline_series_unit(hand_rows) == "hand"
    # 没有 amount 的行不参与投票；全都没有 → 判不出
    assert UNITS.kline_series_unit([{"close": 10.0, "volume": 1.0e6, "amount": None}]) is None


def test_series_unit_majority_vote_not_per_row():
    """同一序列单位不会跳变：按最近 N 根众数判，个别脏行不能带偏整条序列。"""
    rows = [{"close": 10.0, "volume": 1.0e4, "amount": 1.0e7} for _ in range(19)]
    rows.append({"close": 10.0, "volume": 1.0e6, "amount": 9.0e7})   # 1 根脏行
    assert UNITS.kline_series_unit(rows) == "hand"
    # 打平（各 1 票）等于有争议 → 判不出，而不是任选一个
    assert UNITS.kline_series_unit(rows[-1:]) is None


def test_infer_unit_by_anchor_bands():
    """锚点比对：≈1 倍是股、≈1/100 倍是手、其它一律判不出。"""
    assert UNITS.infer_unit_by_anchor(5992729.0, 5992700.0) == "share"
    assert UNITS.infer_unit_by_anchor(59927.0, 5992700.0) == "hand"
    assert UNITS.infer_unit_by_anchor(300000.0, 5992700.0) is None      # 约 1/20 倍
    assert UNITS.infer_unit_by_anchor(None, 5992700.0) is None
    assert UNITS.infer_unit_by_anchor(5992729.0, None) is None


def test_normalize_is_idempotent():
    """归一只做一次：连跑两遍不能把「手→股」再乘一次 100（那会变成 1 万倍）。"""
    once = _norm(FIX_600519_TENCENT, ANCHOR_600519_SHARES)
    twice = SRV._normalize_kline_volume(once, anchor=ANCHOR_600519_SHARES)
    assert twice["klines"][-1]["volume"] == pytest.approx(once["klines"][-1]["volume"])
    assert twice["volume_unit"] == "share"


def test_aggregate_keeps_unit_and_never_sums_missing_as_zero():
    """周/月聚合：单位随日线继承；某日判不出时汇总置 None，而不是把缺失当 0 相加。"""
    bars = [
        {"date": "2026-10-26", "open": 1.0, "close": 1.1, "high": 1.2, "low": 0.9,
         "volume": 100.0, "amount": 110.0, "volume_unit": "share"},
        {"date": "2026-10-27", "open": 1.1, "close": 1.2, "high": 1.3, "low": 1.0,
         "volume": 200.0, "amount": 240.0, "volume_unit": "share"},
    ]
    agg = SRV._agg_kline(copy.deepcopy(bars), "week")
    assert agg[0]["volume"] == pytest.approx(300.0)
    assert agg[0]["volume_unit"] == "share"
    bars[1]["volume"] = None
    agg2 = SRV._agg_kline(copy.deepcopy(bars), "week")
    assert agg2[0]["volume"] is None


# ---------------------------------------------------------------- DTO 契约层
def test_week_month_volume_is_declared_unknown():
    """腾讯周/月线：聚合量 + 估算 amount，两条证据链都失效 → 必须如实说「不知道」。

    KNOWN_DEFECTS DEF-2026-011 的回归保护：
      ① amount_est=True（amount = 手 × 收盘价，自己证明自己，比值恒为 1）→ 自证禁用；
      ② period != day → 官方快照是**单日**成交股数，跟周/月汇总值不可比，锚点禁用。
    两条都堵死时只允许置空 + 标 unknown，**不许**猜个倍数把数填上。
    """
    week = {"symbol": "sh600519", "period": "week", "amount_est": True,
            "klines": [{"date": "2026-10-30", "open": 1325.4, "close": 1392.05,
                        "high": 1393.37, "low": 1322.6, "volume": 221992.0,
                        "amount": 309023963.6}]}
    out = SRV._normalize_kline_volume(copy.deepcopy(week), anchor=5992700.0)
    assert out["volume_unit"] is None
    assert out["volume_unit_unknown"] is True
    assert out["klines"][-1]["volume"] is None            # 语义字段留空，不放猜的值
    assert out["klines"][-1]["volume_native"] == 221992.0  # 源值仍可对账
    # 同一份数据若标成日线，锚点就是合法证据 → 能判出「手」并归一成股
    day = dict(week, period="day")
    out_day = SRV._normalize_kline_volume(copy.deepcopy(day), anchor=22199200.0)
    assert out_day["volume_unit"] == "share"
    assert out_day["klines"][-1]["volume"] == pytest.approx(22199200.0)


def test_quote_old_payload_without_unit_falls_back_on_frontend():
    """DEF-2026-012 的回归保护：L2 磁盘缓存可能吐出没有 volume_unit 的老 payload。

    契约：**字段缺失**（老 payload）→ 前端按已知单位兜底（行情=手），
    **字段为 null**（新 payload 明确判不出）→ 前端必须显示「单位未标注」而不是兜底。
    两者不能混为一谈，否则一个老缓存就能把 100 倍错误带回来。
    """
    src = io.open(os.path.join(ROOT, "viewer", "static", "app.js"), encoding="utf-8").read()
    seg = src[src.index("function volUnitOf"):src.index("function volUnitOf") + 400]
    assert "=== undefined" in seg, "缺失（老 payload）与 null（判不出）必须分开判定"
    assert "fallback" in seg, "缺失时须走调用方给的已知单位兜底"
    assert "—（单位未标注）" in src, "判不出时必须有可见的「未标注」提示"


def test_quote_declares_volume_unit_by_self_proof():
    """行情侧：手/股由 amount 自证（科创板实测是股），不是常量 'hand'。"""
    q600 = SRV._declare_quote_volume_unit(
        {"price": 1392.05, "volume": 59927.0, "amount": 8257380000.0})
    assert q600["volume_unit"] == "hand"
    assert q600["volume_shares"] == pytest.approx(5992700.0)
    assert q600["volume_hand"] == pytest.approx(59927.0)
    q688 = SRV._declare_quote_volume_unit(
        {"price": 96.79, "volume": 29998989.0, "amount": 2957500000.0})
    assert q688["volume_unit"] == "share"
    q_bad = SRV._declare_quote_volume_unit({"price": 10.0, "volume": 5000.0, "amount": None})
    assert q_bad["volume_unit"] is None and q_bad["volume_shares"] is None


def test_kline_current_dto_carries_units():
    """抽屉 K 线卡的 DTO 契约：current 必须能直接读 unit / shares / hand。"""
    d = _norm(FIX_600519_SINA, ANCHOR_600519_SHARES)
    for k in ("volume", "volume_unit", "volume_shares", "volume_hand"):
        assert k in d["current"], "current 缺字段 %s（前端又要猜单位了）" % k
    assert d["current"]["volume_hand"] == pytest.approx(d["current"]["volume_shares"] / 100.0)


# ---------------------------------------------------------------- 前端静态层
def _read(p: str) -> str:
    return io.open(p, encoding="utf-8").read()


def test_si_vol_accepts_unit_param():
    """siVol 必须接受第二个参数：单位是入参，不是函数里的默认假设。"""
    js = _read(APP_JS)
    m = re.search(r"function siVol\(([^)]*)\)", js)
    assert m, "app.js 找不到 siVol 定义"
    params = [p.strip() for p in m.group(1).split(",") if p.strip()]
    assert len(params) >= 2, "siVol 仍是单参数版本（前端只能猜单位 → 又会差 100 倍）"
    assert "unit" in params[1], "siVol 第二个参数必须是单位"
    # 语义包装也要在，避免调用点漏传时静默退化成「单位未标注」
    assert "function siVolHand(" in js and "function siVolShare(" in js


def test_all_si_vol_call_sites_pass_unit():
    """所有 siVol 调用点都必须带单位：行情区=手、K 线区=股（优先读 payload 的 volume_unit）。"""
    js = _read(APP_JS)
    body = js.replace("function siVol(v, unit)", "")
    calls = re.findall(r"siVol\(([^)]*)\)", body)
    calls = [c for c in calls if c.strip() not in ("v", "v, unit")]
    assert calls, "没扫到任何 siVol 调用点（静态扫描失效？）"
    bad = [c for c in calls if "," not in c]
    assert not bad, "这些 siVol 调用点没传单位：%s" % bad
    # K 线区的调用必须带 volume_unit（而不是硬编码成手）
    assert js.count("volume_unit") >= 6, "K 线/行情调用点没有读 payload 的 volume_unit"


# ---------------------------------------------------------------- 换手率口径（同一根因的下游）
# 官方快照 ent = (流通股本股, 流通市值亿, 官方换手率%, 官方成交量)。
# 官方成交量 f6 的单位**随板块变**：主板/北交=手、科创板=股 —— 所以标定前必须先自证它。
#   600519 主板  ：fs=1,250,081,532.97   to_rt=0.31%  vol_rt=38,805(手)     当日成交 5,992,729 股
#   688981 科创板：fs=2,000,588,903.81   to_rt=1.50%  vol_rt=29,998,989(股) 当日成交 29,998,989 股
#   920819 北交所：fs=1,211,698,113.21   to_rt=0.36%  vol_rt=43,778(手)     当日成交 4,377,779 股
#     （920819 的 fs 按 f44 流通市值 32.11 亿 ÷ 2.65 推得：screener._float_map 的 _prefix
#       不认 920 段取不到它，那是另一个既有缺陷，不影响本组量纲断言。）
TURNOVER_CASES = [
    ("600519", 1250081532.97, 16635.46, 0.31, 38805.0, 5992729.0),
    ("688981", 2000588903.81, 1936.37, 1.50, 29998989.0, 29998989.0),
    ("920819", 1211698113.21, 32.11, 0.36, 43778.0, 4377779.0),
]


def _bars(vol, n: int = 31, close: float = 10.0) -> list:
    """造 n 根等量等价的日线（date, close, high, low, vol），只关心量纲换算。"""
    return [("2026-10-%02d" % (i % 28 + 1), close, close, close, float(vol))
            for i in range(n)]


@pytest.mark.parametrize("code,fs,fmv,to_rt,vol_rt,day_shares", TURNOVER_CASES)
def test_turnover_kline_path_matches_official(code, fs, fmv, to_rt, vol_rt, day_shares):
    """两条路径算同一个换手率：K 线（已归一为股） vs 官方快照，相对误差 < 2%。

    修复前 600519 走 K 线路径得 47.87%（真实 0.48%）：to_scale 按「手」标定，
    却乘到「股」的 K 线上，正好放大 100 倍。三个板块各一条，防止再靠代码前缀。
    """
    ent = (fs, fmv, to_rt, vol_rt)
    ev = SCR._turnover_for(code, _bars(day_shares), ent, 30, vol_unit="share")
    to_code = ev["to_today"]
    to_official = day_shares / fs * 100.0
    assert to_code is not None and to_official > 0
    assert abs(to_code - to_official) / to_official < 0.02, (
        "%s 换手率两条路径不一致：K线路径=%.4f%% 官方口径=%.4f%%"
        % (code, to_code, to_official))
    assert ev["vol_unit"] == "share" and ev["unit_unknown"] is False


def test_turnover_hand_and_share_bars_give_same_value():
    """同一笔成交用「手」和「股」两种单位表达，换手率必须一样：单位是标签，不是倍数。"""
    ent = (1250081532.97, 16635.46, 0.31, 38805.0)
    ev_share = SCR._turnover_for("600519", _bars(5992729.0), ent, 30, vol_unit="share")
    ev_hand = SCR._turnover_for("600519", _bars(59927.29), ent, 30, vol_unit="hand")
    assert ev_share["to_today"] == pytest.approx(ev_hand["to_today"], rel=0.01)
    assert abs(ev_share["score"] - ev_hand["score"]) <= 1        # 换单位不该把评分换档


def test_turnover_unknown_unit_is_flagged_not_multiplied():
    """单位判不出 → score=None + unit_unknown=True；不许「默认乘 100」蒙一个像真的分数。

    两条分支都要守：① 既没声明、也没有官方锚点 → 明确标 unit_unknown；
    ② 没声明但有官方锚点 → 走一次自证，算出来的量级必须落在真值附近（不是 100 倍）。
    """
    ent = (1250081532.97, 16635.46, 0.31, 38805.0)
    # ① 完全判不出：官方快照缺失 → 自证也拿不到锚点
    blind = SCR._turnover_for("600519", _bars(5992729.0), (None, None, 0.0, 0.0), 30,
                              vol_unit=None)
    assert blind["unit_unknown"] is True
    assert blind["score"] is None and blind["to_today"] is None
    assert blind["vol_unit"] is None
    # ② 没声明但锚点在 → 自证成功，量级正确
    proved = SCR._turnover_for("600519", _bars(5992729.0), ent, 30, vol_unit=None)
    assert proved["unit_unknown"] is False and proved["vol_unit"] == "share"
    assert proved["to_today"] < 2.0        # 真值量级（0.48%），不是 48%
    # ③ 显式声明 share 与自证结果一致
    ok = SCR._turnover_for("600519", _bars(5992729.0), ent, 30, vol_unit="share")
    assert ok["to_today"] == pytest.approx(proved["to_today"], rel=0.01)
    assert ok["to_today"] < 2.0


def test_bars_vol_unit_self_proof():
    """产出方没声明单位时用官方快照当锚点自证；没有官方数据就返回 None（不猜）。"""
    ent_main = (1250081532.97, 16635.46, 0.31, 38805.0)
    assert SCR._bars_vol_unit(_bars(59927.0), ent_main) == "hand"
    assert SCR._bars_vol_unit(_bars(5992729.0), ent_main) == "share"
    ent_kc = (2000588903.81, 1936.37, 1.50, 29998989.0)
    assert SCR._bars_vol_unit(_bars(29998989.0), ent_kc) == "share"
    assert SCR._bars_vol_unit(_bars(5992729.0), (None, None, 0.0, 0.0)) is None


def test_turnover_eval_requires_explicit_unit():
    """契约：vol_unit 必填（漏传直接 TypeError），且「按代码前缀猜单位」的老表不许回来。"""
    with pytest.raises(TypeError):
        SCR._turnover_eval(_bars(1.0), 1e9, 100.0, 30)      # type: ignore[call-arg]
    src = io.open(os.path.join(ROOT, "screener.py"), encoding="utf-8").read()
    assert "_SPU_FALLBACK_PREFIX" not in src, \
        "代码前缀猜单位的老表又回来了（主板换手率会再错 100 倍）"


# ---------------------------------------------------------------- 板块口径（DEF-2026-013）
def test_tx_prefix_comes_from_security_master_not_a_code_table():
    """北交所 92 段必须拼成 bj：市场前缀的唯一来源是 security_master，不是又一张段表。

    DEF-2026-013 的回归保护。旧实现复用 `extref.backtest._prefix()`（sh: 60/68/90、
    bj: 4 开头/8 开头、其余 sz），它不认 2023 年启用的 920 段 → 920819 被拼成 sz920819
    打腾讯，整批返回空 → 取不到流通股本 → `turnover_single('920819')` 报「日K获取失败」，
    整个北交所板块的功能是断的。这里同时守两件事：映射对；且 screener 里不许再 import
    那张段表（换口径来源，而不是往表里补一个 "92"）。
    """
    cases = {"600519": "sh", "688981": "sh", "000001": "sz", "300750": "sz",
             "920819": "bj", "430047": "bj", "830799": "bj"}
    got = {c: SCR._tx_prefix(c) for c in cases}
    assert got == cases, "市场前缀映射错了：%r" % got
    src = io.open(os.path.join(ROOT, "screener.py"), encoding="utf-8").read()
    for line in src.splitlines():
        if line.startswith("from extref.backtest import"):
            assert "_prefix" not in line, \
                "又用回按代码段假定的 _prefix 表了（北交所会再被拼成 sz）"


def test_kline_daily_stale_guard_is_three_state():
    """停更守卫必须**三态**：False=新鲜 / True=滞后超阈值(+WARNING) / None=样本太小不下结论。

    DEF-2026-013 的第二个回归保护。在 kbase 收录 92 段之前，北交所日线只靠
    `data/kline_daily.db`；它一旦停更，北交所会**静默**变成「无数据」——这正是本项目
    最恨的失败形态（看起来像"今天没票"，其实是源断了）。所以守卫不能是二态的
    "有/没有"，必须有第三种"判不出"，并且要能透传到面板 payload。
    """
    if not os.path.exists(SCR._KLINE_DAILY_DB):
        pytest.skip("data/kline_daily.db 不存在（本机裁掉了本地仓）")
    codes = ["920819", "920002", "920008", "920099", "920058"]
    bars, info = SCR._kline_daily_bars(codes, 120)
    if not bars:
        pytest.skip("kline_daily 里没有北交所样本")
    # 契约：source_stale 必须与 days_behind/阈值 自洽（不依赖今天数据新不新）
    assert info["source_stale"] == (info["days_behind"] > SCR._KL_STALE_MAX_DAYS)
    assert info["last_date"] and info["source_stale"] in (True, False)
    # 样本 <5 只 → None：单只退市股自己最后一根就旧，不能拿它当「源停更」的证据
    _, info_small = SCR._kline_daily_bars(["920819"], 120)
    assert info_small["source_stale"] is None
    # 阈值调成 -1 模拟停更 → True，且必须能渲染出人话提示
    old = SCR._KL_STALE_MAX_DAYS
    try:
        SCR._KL_STALE_MAX_DAYS = -1
        _, info_stale = SCR._kline_daily_bars(codes, 120)
    finally:
        SCR._KL_STALE_MAX_DAYS = old
    assert info_stale["source_stale"] is True
    f = SCR._stale_fields({"kline_daily": info_stale})
    assert f["source_stale"] is True and "已停更" in f["source_stale_hint"]
    assert SCR._stale_fields({}) == {"source_stale": None, "source_stale_hint": ""}


# ---------------------------------------------------------------- 真实接口（有网才跑）
def _net_ok() -> bool:
    try:
        txt = SRV._get_text("https://hq.sinajs.cn/list=sh600519",
                            "https://finance.sina.com.cn/", timeout=3, retries=0,
                            encoding="gbk")
        return bool(txt and "600519" in txt)
    except Exception:  # noqa: BLE001 —— 没网就是 skip，不是失败
        return False


needs_net = pytest.mark.skipif(not _net_ok(), reason="拿不到行情站点（离线环境）")


@pytest.fixture()
def fresh(monkeypatch):
    """绕开磁盘/内存缓存，保证断言打到「当前代码 + 当前上游」。"""
    monkeypatch.setattr(SRV, "cu", None)
    SRV._KLINE_CACHE.clear()
    SRV._ANCHOR_CACHE.clear()
    yield
    SRV._KLINE_CACHE.clear()
    SRV._ANCHOR_CACHE.clear()


@needs_net
def test_live_600519_kline_matches_spot(fresh):
    """/api/kline 与 /api/quote 对同一交易日必须自洽（相对误差 < 1%）。"""
    k = SRV.api_kline("600519", "day", 2)
    assert k["current"]["volume_unit"] == "share"
    shares = k["current"]["volume_shares"]
    assert 2e6 <= shares <= 2e7, "茅台单日成交量不在 200 万~2000 万股区间：%r" % shares
    assert k["current"]["volume_hand"] == pytest.approx(shares / 100.0)
    q = SRV.api_quote("600519", force=True)
    assert q["quote"]["volume_unit"] == "hand", "行情区量的单位应为手"
    spot_hand = q["quote"]["volume_hand"]
    assert abs(k["current"]["volume_hand"] - spot_hand) / spot_hand < 0.01, (
        "K 线与行情区换算后仍不一致：kline=%.0f 手, spot=%.0f 手"
        % (k["current"]["volume_hand"], spot_hand))


@needs_net
def test_live_688_and_920_kline_units(fresh):
    """科创板和北交所走同一条自证链路（不靠代码前缀）。"""
    for sym, lo, hi in (("688981", 1e6, 1e8), ("920819", 1e5, 1e8)):
        d = SRV.api_kline(sym, "day", 1)
        assert d.get("volume_unit") == "share", "%s 的 K 线单位未归一到股" % sym
        v = d["current"]["volume_shares"]
        assert v is not None and lo <= v <= hi, "%s 成交量异常：%r" % (sym, v)
        assert d["current"]["volume_hand"] == pytest.approx(v / 100.0)


@needs_net
def test_live_bj_turnover_single_matches_official_no_regression():
    """三板块真实路径：`turnover_single` 非空、vol_unit=share、与官方 f38 相对误差 <2%。

    DEF-2026-013 的关闭条件。北交那只必须**真的走通**——不是"能算出一个数"，
    而是和官方换手率对得上；主板/科创同步复验，确保换口径来源没有把它们改坏。

    注意：`_float_map` 的磁盘缓存是 7 天，命中旧条目时官方快照日会**早于**最后一根 K 线，
    两个不同交易日的换手率本来就不该相等（实测 600519 缓存落后 5 个交易日：0.31% vs 0.48%）。
    所以这里用 `max_age_days=0` 强制重取快照，让两边落在同一天，比较才有意义——
    否则测的是"日期错位"，不是"单位错"。
    """
    for code in ("920819", "600519", "688981"):
        r = SCR.turnover_single(code, 30)
        assert r.get("ok") is True, "%s 换手率取数失败：%r" % (code, r.get("reason"))
        assert r.get("vol_unit") == "share", "%s 单位未声明为股：%r" % (code, r.get("vol_unit"))
        assert r.get("unit_unknown") is False, "%s 被误标 unit_unknown" % code
        # 三态字段必须出现在 payload 里（前端才能说清"是没数据还是源停了"）
        assert "source_stale" in r and r["source_stale"] in (True, False, None)
        assert "source_stale_hint" in r
        ent = SCR._float_map([code], max_age_days=0).get(code)   # 强制刷新 → 同一天口径
        assert ent and ent[2] > 0, "%s 拿不到官方换手率 f38" % code
        to, official = r.get("to_today"), ent[2]
        assert to is not None and abs(to - official) / official < 0.02, (
            "%s 换手率与官方不一致：K线路径=%s%% 官方 f38=%.4f%%"
            % (code, to, official))


if __name__ == "__main__":  # 兼容 `python tests/test_volume_units_e2e.py` 直跑
    sys.exit(pytest.main([__file__, "-q"]))
