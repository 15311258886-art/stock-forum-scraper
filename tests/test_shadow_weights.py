# -*- coding: utf-8 -*-
"""决策②：影子权重（2026-10-31）。

影子权重的全部价值在于"用来对比"，所以最致命的错是两件：
  ① 它悄悄影响了生产权重（那就不是影子了，是没经过同意的切换）；
  ② 同日重跑累加记录（"连续 20 个交易日"的判据会被一天三次跑成 3 天）。
①靠 `_compute_weights(prior_mode=...)` 是纯函数 + rescore 里另算一份来保证；
②靠 (asof, signal_key) 主键 + UPSERT 保证，下面直接测。
"""
import os
import sqlite3
import sys

# predictor.py 里有 `import kbase`（kbase.py 在 viewer/ 下），服务运行时 viewer/ 已
# 在 sys.path 上，测试里没有。**必须 append 而不是 insert**：insert 会让 viewer/
# 下的同名模块（config.py、strategy_sim.py）遮蔽仓库根的同名模块，行为静默改变。
sys.path.append(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "viewer"))

from viewer import predictor as PR  # noqa: E402
from viewer.kernel import params as P  # noqa: E402


def _mem():
    return sqlite3.connect(":memory:")


def _balanced():
    n = len(PR.SIGNAL_KEYS)
    return {k: 1.0 / n for k in PR.SIGNAL_KEYS}


def test_shadow_save_idempotent_same_day():
    con = _mem()
    a, b = _balanced(), _balanced()
    n1 = PR._save_shadow(con, a, b, {})
    n2 = PR._save_shadow(con, a, b, {})
    assert n1 == n2 == len(PR.SIGNAL_KEYS)
    cnt = con.execute("SELECT COUNT(*) FROM signal_weight_shadow").fetchone()[0]
    assert cnt == len(PR.SIGNAL_KEYS), "同日重跑必须覆盖，不能累加"


def test_shadow_diff_is_in_percentage_points():
    con = _mem()
    k = PR.SIGNAL_KEYS[0]
    prod = {k: 0.20}
    data = {k: 0.30}
    PR._save_shadow(con, prod, data, {})
    diff = con.execute("SELECT diff_pp FROM signal_weight_shadow WHERE signal_key=?",
                       (k,)).fetchone()[0]
    assert abs(diff - 10.0) < 1e-9, "0.20→0.30 应记为 +10pp，实际 %r" % diff


def test_shadow_stores_tier_and_n_obs():
    con = _mem()
    k = PR.SIGNAL_KEYS[1]
    PR._save_shadow(con, {k: 0.1}, {k: 0.2},
                    {k: {"n_obs": 320, "tier": P.TIER_PROVISIONAL}})
    n_obs, tier = con.execute(
        "SELECT n_obs, tier FROM signal_weight_shadow WHERE signal_key=?", (k,)).fetchone()
    assert n_obs == 320 and tier == P.TIER_PROVISIONAL


def test_load_shadow_empty_is_safe():
    r = PR.load_shadow(_mem())
    assert r["ok"] is True
    assert r["n_days"] == 0
    assert r["rows"] == []


def test_load_shadow_groups_by_signal():
    con = _mem()
    k1, k2 = PR.SIGNAL_KEYS[0], PR.SIGNAL_KEYS[1]
    PR._save_shadow(con, {k1: 0.1, k2: 0.2}, {k1: 0.15, k2: 0.18}, {})
    r = PR.load_shadow(con, days=20)
    assert r["n_days"] == 1
    assert len(r["rows"]) == 2
    top = r["rows"][0]
    assert top["signal_key"] == k1, "按差值降序：+5pp 应排在 −2pp 之前"
    assert abs(top["diff_pp_last"] - 5.0) < 1e-9


def test_load_shadow_carries_switch_rule():
    r = PR.load_shadow(_mem())
    assert "连续 20 个交易日" in r["switch_rule"]
    assert "税后超额为正" in r["switch_rule"]


def test_shadow_switch_default_on_and_documented():
    assert P.SHADOW_WEIGHTS_ENABLED is True
    assert "不参与" in P.SHADOW_WEIGHTS_NOTE


def test_data_only_mode_ignores_prior():
    """源码级确认：data_only 分支里 prior_eff 必须被钉成 1.0。

    这不是测试计算（需要整库），而是防止有人把这个分支改成
    `1.0 + (prior-1)*(1-eff_w)` —— 那样影子就变回生产，整件事失去意义。
    """
    import inspect
    src = inspect.getsource(PR._compute_weights)
    seg = src.split('if prior_mode == "data_only":')[1].split("else:")[0]
    assert "prior_eff = 1.0" in seg
