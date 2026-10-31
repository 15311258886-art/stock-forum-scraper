# -*- coding: utf-8 -*-
"""M0 验收：共享元学习引擎 metalearn 与「原 evolve / pillars 手写公式」逐项等价。

证明：
  [1] metalearn.weights_from_ic 复现原 evolve 权重公式（caliber 红线，含不足分支）
  [2] pillars._sl_weights 改造后从 metalearn 取数，同一组候选 → 权重完全一致
  [3] evolve.learn_weights 改造后从 metalearn 取数（spy 证明调用 + 返回结构合法）

运行：python verify_metalearn_equivalence.py  （在 viewer/ 目录）
"""
import json
import random
import sys

HERE = "."
sys.path.insert(0, HERE)

import metalearn as M


# ---------- Oracle：原 evolve 权重公式（手工，作为红线参照）----------------
def oracle_weights(ic_by_dim, n_by_dim, dims, default_w, K=M.IC_SHRINK_K,
                   prior_mix=M.W_PRIOR_MIX, floor=M.W_FLOOR):
    shrunk = {}
    for d in dims:
        n = n_by_dim.get(d, 0)
        ic = ic_by_dim.get(d)
        shrunk[d] = (ic * (n / (n + K))) if (ic is not None and n >= M.MIN_IC_N) else None
    pos = {d: max(0.0, shrunk.get(d) or 0.0) for d in dims}
    tot = sum(pos.values())
    if tot <= 1e-9:
        return dict(default_w), shrunk
    learned = {d: round(pos[d] / tot, 4) for d in dims}
    mixed = {d: (1.0 - prior_mix) * learned[d] + prior_mix * default_w[d] for d in dims}
    for d in dims:
        if mixed[d] < floor:
            mixed[d] = floor
    s = sum(mixed.values())
    w = {d: round(mixed[d] / s, 4) for d in dims}
    top = max(w, key=lambda k: w[k])
    drift = round(1.0 - sum(w.values()), 4)
    w[top] = round(w[top] + drift, 4)
    for d in dims:
        if w[d] < floor:
            gap = round(floor - w[d], 4)
            w[d] = floor
            w[top] = round(w[top] - gap, 4)
    return w, shrunk


def approx_eq(a, b, tol=1e-9):
    if a is None or b is None:
        return a is None and b is None
    return abs(a - b) <= tol


def w_eq(w1, w2, dims):
    return all(approx_eq(w1.get(d), w2.get(d)) for d in dims)


def gen_scenarios(dims, n=3000):
    random.seed(20261016)
    out = []
    for _ in range(n):
        ic, nn = {}, {}
        for d in dims:
            if random.random() < 0.12:
                ic[d] = None
                nn[d] = random.randint(5, 90)
            else:
                ic[d] = round(random.uniform(-0.7, 0.7), 4)
                nn[d] = random.randint(5, 90)
        out.append((ic, nn))
    # 边界：全不足 / 全正但极小 / 单维主导
    out.append(({d: 0.01 for d in dims}, {d: 5 for d in dims}))
    out.append(({d: None for d in dims}, {d: 5 for d in dims}))
    out.append(({dims[0]: 0.6, dims[1]: -0.5, dims[2]: -0.4, dims[3]: -0.3},
                {d: 50 for d in dims}))
    return out


def main():
    fails = 0

    # ---------- [1] metalearn vs 原公式 ----------
    dims4 = ["a", "b", "c", "d"]
    default4 = {d: 0.25 for d in dims4}
    sc = gen_scenarios(dims4)
    trials = 0
    for ic, nn in sc:
        trials += 1
        w_meta, shrunk_meta, *_ = M.weights_from_ic(ic, nn, dims4, default4)
        w_ora, shrunk_ora = oracle_weights(ic, nn, dims4, default4)
        if not w_eq(w_meta, w_ora, dims4):
            fails += 1
            if fails <= 5:
                print("FAIL[1] weights", json.dumps({"ic": ic, "n": nn}), "meta", w_meta, "ora", w_ora)
        for d in dims4:
            if not approx_eq(shrunk_meta.get(d), shrunk_ora.get(d)):
                fails += 1
                if fails <= 5:
                    print("FAIL[1] shrunk", d, shrunk_meta.get(d), shrunk_ora.get(d))
    print("[1] metalearn vs 原手写公式：%d trials, %d fails" % (trials, fails))

    # ---------- [2] pillars._sl_weights 必须 == metalearn ----------
    try:
        import pillars as P
        psc = gen_scenarios(list(P.SELF_LEARN_PILLARS))
        pfails = 0
        for ic, nn in psc:
            w_pl = P._sl_weights(ic, nn)[0]
            w_meta, *_ = M.weights_from_ic(ic, nn, list(P.SELF_LEARN_PILLARS), P.DEFAULT_W_SELF)
            if not w_eq(w_pl, w_meta, list(P.SELF_LEARN_PILLARS)):
                pfails += 1
                if pfails <= 5:
                    print("FAIL[2] pillars", json.dumps({"ic": ic, "n": nn}), "pl", w_pl, "meta", w_meta)
        print("[2] pillars._sl_weights vs metalearn：%d fails" % pfails)
        fails += pfails
    except Exception as e:  # noqa: BLE001
        print("[2] pillars import failed:", repr(e))

    # ---------- [3] evolve.learn_weights 经 metalearn（spy 证明调用 + 结构合法） ----------
    try:
        import evolve as E
        # spy：确认 learn_weights 真的走了 metalearn.weights_from_ic
        orig = E.weights_from_ic
        spy_state = {"hit": False, "args": None}

        def spy(ic_by_dim, n_by_dim, dims, default_w, K=M.IC_SHRINK_K,
                prior_mix=M.W_PRIOR_MIX, floor=M.W_FLOOR):
            spy_state["hit"] = True
            spy_state["args"] = (ic_by_dim, n_by_dim, dims, default_w)
            return orig(ic_by_dim, n_by_dim, dims, default_w, K, prior_mix, floor)

        E.weights_from_ic = spy
        # 造一组有信号的数据（每维造 n 个近似线性点，pearson 接近 ±1）
        dims = list(E.DIMS)
        rows = []
        for d in dims:
            nn = 60
            slope = 0.5 if d != dims[1] else -0.3
            for i in range(nn):
                x = float(i)
                y = slope * x + random.uniform(-0.05, 0.05)
                rows.append({"f": {d: x}, "y": y, "mkt": "震荡"})
        res = E.learn_weights(rows)
        E.weights_from_ic = orig  # 还原
        assert spy_state["hit"], "evolve.learn_weights 未调用 metalearn.weights_from_ic"
        assert set(res.keys()) >= {"ic", "ic_adj", "w", "by_state", "note", "n"}, res.keys()
        assert set(res["w"].keys()) == set(dims), res["w"].keys()
        s = sum(res["w"].values())
        assert abs(s - 1.0) < 1e-6, ("权重和≠1", s)
        for d in dims:
            assert res["w"][d] >= M.W_FLOOR - 1e-9, ("跌破下限", d, res["w"][d])
        print("[3] evolve.learn_weights 委托 metalearn：OK（spy命中=%s, 权重和=%.6f）"
              % (spy_state["hit"], s))
    except Exception as e:  # noqa: BLE001
        print("[3] evolve 集成测试异常：", repr(e))
        fails += 1

    print("TOTAL FAILS:", fails)
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
