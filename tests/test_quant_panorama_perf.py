# -*- coding: utf-8 -*-
"""D32 回归测试：`/api/quant/panorama` 与 `/api/quant/select` 不得在请求线程里现算全市场。

背景（2026-10-30）：
  · `factor_panorama(code)` 原来第一行调 `_universe_bars()` —— 为了看**一只票**，把全市场
    5239 只 × 最近 260 根 K 线读进内存再逐只过滤。实测线上 `/api/quant/panorama` 连续两次
    45s 不返回（用户「量化板块 → 因子全景」输入代码点查询就卡死）。
  · `/api/quant/select` 同理直调 `multi_factor_select`（全池截面 + 120 个截面日算 IC）。

本文件的守门方式（为什么用这些断言，而不是只测「返回 200」）：
  1. **计时**：不存在的代码必须 < 1s 返回 —— 一旦有人把它改回全池加载，旧实现是数秒级，必红。
  2. **断源**：把 `quant._universe_bars` 换成「一调用就炸」的桩，`factor_panorama` 仍须正常出
     结果 —— 这是比计时更硬的约束（计时受机器负载影响，断源不受）。
  3. **口径**：单票取数结果必须与 `kbase.bars_of` 的「最近 260 根 + 剔空 + >= MIN_BARS」
     原口径逐根相等，防止「提速了但口径漂了」。
  4. **select 不现算**：把 `quant.multi_factor_select` 换成「一调用就炸」的桩，路由层
     `_quant_select_cached` 仍须在 1s 内给出结果（读 `/api/quant` 落盘缓存）。

全部测试直接 import 函数/模块调用，**不依赖服务在运行**。
"""
from __future__ import annotations

import importlib
import os
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "viewer") not in sys.path:
    # viewer/quant.py 里是 `import predictor as P`（与 server.py 的依赖方式一致）
    sys.path.insert(0, str(ROOT / "viewer"))

REAL_CODES = ["600519", "000001", "300750"]


@pytest.fixture(scope="module")
def quant_mod():
    return importlib.import_module("quant")


@pytest.fixture(scope="module")
def kbase_mod():
    return importlib.import_module("viewer.kbase")


def _base_if_available(kbase_mod):
    """kbase.db 不存在（干净环境 / CI）时跳过依赖真数据的用例，而不是伪造数据。"""
    base = kbase_mod.get_base()
    try:
        if not base.codes():
            pytest.skip("本机 kbase.db 无 K 线数据，跳过依赖真实行情的用例")
    except Exception:  # noqa: BLE001
        pytest.skip("本机 kbase.db 不可读，跳过依赖真实行情的用例")
    return base


# ---------------------------------------------------------------- ① 计时 + 断源
def test_panorama_unknown_code_is_fast_and_safe(quant_mod):
    """不存在的代码：ok=False、不抛异常、且耗时 < 1s（防被改回全池加载）。"""
    quant_mod.factor_panorama("999999")          # 预热（首调用含模块内惰性初始化）
    t0 = time.perf_counter()
    pano = quant_mod.factor_panorama("999999")
    elapsed = time.perf_counter() - t0
    assert pano.get("ok") is False
    assert pano.get("has_data") is False
    assert pano.get("code") == "999999"
    assert "K 线" in (pano.get("reason") or "")
    assert elapsed < 1.0, "不存在的代码耗时 %.2fs（>1s 说明又在请求线程里加载全市场了）" % elapsed


def test_panorama_never_touches_universe(quant_mod):
    """把 _universe_bars 换成爆雷桩：单票全景必须照常工作（这才是 45s 的真凶）。"""
    original = quant_mod._universe_bars

    def _boom(*_a, **_kw):
        raise AssertionError("factor_panorama 又调用了 _universe_bars（全池加载 = 45s 超时）")

    quant_mod._universe_bars = _boom
    try:
        pano = quant_mod.factor_panorama("999999")
        assert pano.get("ok") is False          # 不存在的代码：走退出分支，同样不许碰宇宙
        got = quant_mod.factor_panorama("600519")
    finally:
        quant_mod._universe_bars = original
    # 600519 在本机无数据时 got.ok 允许 False，但绝不许抛异常（桩先炸就说明调用了宇宙）
    assert got.get("code") == "600519"


# ---------------------------------------------------------------- ② 输出结构
def test_panorama_real_code_shape(quant_mod, kbase_mod):
    """真实票：字段集合 / 因子顺序 / 取值域与改造前一致。"""
    _base_if_available(kbase_mod)
    pano = quant_mod.factor_panorama(REAL_CODES[0])
    if not pano.get("ok"):
        pytest.skip("%s 在本机 K 线不足 %d 根，跳过结构断言" % (REAL_CODES[0], quant_mod.MIN_BARS))
    assert set(pano) == {"ok", "code", "name", "has_data", "data_date", "factors"}
    assert pano["has_data"] is True
    assert isinstance(pano["name"], str)
    assert len(pano["data_date"]) == 10 and pano["data_date"][4] == "-", pano["data_date"]
    factors = pano["factors"]
    assert [f["key"] for f in factors] == list(quant_mod.TECH_FACTORS)
    for f in factors:
        assert set(f) == {"key", "label", "family", "unit", "desc", "raw", "score"}
        assert isinstance(f["label"], str) and f["label"]
        assert f["raw"] is None or isinstance(f["raw"], float)
        assert isinstance(f["score"], (int, float))
        assert -1.0 <= f["score"] <= 1.0, "%s 展示分越界：%r" % (f["key"], f["score"])


# ---------------------------------------------------------------- ③ 口径
def test_single_bars_matches_original_window(quant_mod, kbase_mod):
    """单票取数 == 旧口径（kbase 全历史 → 窗口内最近 260 根 → 剔空 → >= MIN_BARS）。"""
    base = _base_if_available(kbase_mod)
    cal = base.distinct_dates()
    min_date = cal[-quant_mod._SERIES_TAIL] if len(cal) > quant_mod._SERIES_TAIL else ""
    checked = 0
    for code in REAL_CODES:
        want = []
        for b in base.bars_of(code):
            d = str(b.get("d"))[:10]
            if min_date and d < min_date:
                continue
            want.append((d, b.get("o"), b.get("h"), b.get("l"), b.get("c"), b.get("v")))
        want = [r for r in want if r[0] and r[4]][-quant_mod._SERIES_TAIL:]
        if len(want) < quant_mod.MIN_BARS:
            continue                      # 该票本就不在宇宙内
        got = quant_mod._single_bars(code)
        assert got is not None, "%s 旧口径有 %d 根，新实现却判无数据" % (code, len(want))
        assert got == want, "%s 单票取数与旧口径不一致" % code
        checked += 1
    if not checked:
        pytest.skip("本机没有 K 线足够长的样本票")


def test_single_bars_excludes_short_and_unknown(quant_mod, kbase_mod):
    """K 线不足 MIN_BARS / 不存在的代码 → None；非 A 股前缀直接 None。"""
    _base_if_available(kbase_mod)
    assert quant_mod._single_bars("999999") is None
    assert quant_mod._single_bars("") is None
    assert quant_mod._single_bars("113001") is None       # 6 位但非 A 股前缀
    # 全库里 K 线最少的票必然 < MIN_BARS（本机实测 301718 只有 6 根；不存在则跳过）
    base = kbase_mod.get_base()
    short = [c for c in ("301718", "601091", "301569", "301660") if len(base.bars_of(c)) < quant_mod.MIN_BARS]
    if short:
        assert quant_mod._single_bars(short[0]) is None


@pytest.mark.timeout(600)
@pytest.mark.skipif(os.environ.get("QUANT_PARITY_ALL") != "1",
                    reason="全量 5239 只等价性比对实测约 170s（每只票一次全历史读 + 一次 ohlcv_bulk，"
                           "各开一次 sqlite 连接），默认关闭；"
                           "需要复现 D32 全量证明时设 QUANT_PARITY_ALL=1 单独跑")
def test_full_universe_parity(quant_mod, kbase_mod):
    """D32 的最强证据：`_single_bars(code)` 与 `_universe_bars()[0].get(code)` 全量逐只一致。

    ⚠️ 必带 `@pytest.mark.timeout(600)`：`pytest.ini` 有全局 `timeout = 120`（P2-37 防挂死），
    而这条实测 **169.76s**（QA 独立复现）。没有这个 marker 时，按项目标准命令跑**必然是红的**
    ——而且是「超时打断」而不是「断言失败」，下一个人会误读成「D32 改出 bug 了」。
    而这条恰恰是**唯一**能抓出那 7 只退市票偏差的测试（20 只抽样全漏了），绝不能让它事实上不可运行。
    """
    _base_if_available(kbase_mod)
    bars_map, _ = quant_mod._universe_bars()
    base = kbase_mod.get_base()
    bad = []
    for code in base.codes():
        got = quant_mod._single_bars(code)
        want = bars_map.get(code)
        if want is None:
            if got is not None:
                bad.append("%s 应为 None，实际 %d 根" % (code, len(got)))
        elif got != want:
            bad.append("%s 应 %d 根，实际 %r" % (code, len(want), None if got is None else len(got)))
    assert bad == [], "不一致 %d 处（前 20）：%s" % (len(bad), bad[:20])


# ---------------------------------------------------------------- ④ select 不现算
def test_quant_select_route_reads_cache(quant_mod):
    """`/api/quant/select` 读落盘缓存：断掉 multi_factor_select 也必须 1s 内出结果。"""
    server_mod = importlib.import_module("viewer.server")
    original = quant_mod.multi_factor_select

    def _boom(*_a, **_kw):
        raise AssertionError("路由层又在请求线程里现算 multi_factor_select（45s 超时）")

    quant_mod.multi_factor_select = _boom
    try:
        t0 = time.perf_counter()
        out = server_mod._quant_select_cached(30)
        elapsed = time.perf_counter() - t0
    finally:
        quant_mod.multi_factor_select = original
    assert isinstance(out, dict)
    assert elapsed < 1.0, "_quant_select_cached 耗时 %.2fs（>1s 说明又在现算）" % elapsed
    if out.get("computing"):
        pytest.skip("量化面板落盘缓存尚未生成（首次部署/缓存被清），仅校验了不现算")
    assert out.get("ok") is True, out
    assert isinstance(out.get("items"), list)
    assert out.get("top_n") == 30


def test_quant_select_route_trims_top_n(quant_mod):
    """top_n 小于缓存条数时按需切片，且不额外计算。"""
    server_mod = importlib.import_module("viewer.server")
    full = server_mod._quant_select_cached(30)
    if not full.get("ok") or full.get("computing"):
        pytest.skip("量化面板落盘缓存尚未生成，跳过切片用例")
    n = len(full.get("items") or [])
    if n < 6:
        pytest.skip("缓存里清单太短，无法验证切片")
    cut = server_mod._quant_select_cached(5)
    assert cut.get("top_n") == 5
    assert len(cut["items"]) == 5
    assert [x["symbol"] for x in cut["items"]] == [x["symbol"] for x in full["items"][:5]]
