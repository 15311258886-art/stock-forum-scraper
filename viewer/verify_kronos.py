# viewer/verify_kronos.py
"""Kronos 板块自测（沙箱 / 桌面均可跑，无需 torch）。

覆盖：语法编译、路由注册（server.py / app.js 文本核对）、数据库落库回读、
回测指标数学、前瞻时间戳生成；若本机已装 torch + 权重，额外做真实推理冒烟。
不依赖 predictor / 查看器主后端，可独立运行。
"""
from __future__ import annotations

import compileall
import json
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

ROOT = HERE  # viewer 目录（含 server.py / static/app.js）
SERVER = ROOT / "server.py"
APPJS = ROOT / "static" / "app.js"

PASS, FAIL = [], []


def ok(name, detail=""):
    PASS.append(name)
    print(f"  ✅ {name}")


def bad(name, detail):
    FAIL.append((name, detail))
    print(f"  ❌ {name}: {detail}")


def check(cond, name, detail=""):
    (ok if cond else bad)(name, detail)


# ---------------------------------------------------------------- 1. 语法编译
def test_compile():
    print("\n[1] 语法编译")
    for f in (HERE / "kronos.py", HERE / "setup_kronos.py"):
        r = compileall.compile_file(str(f), quiet=1)
        check(r, f"py_compile {f.name}")


# ---------------------------------------------------------------- 2. 路由注册
def test_routes():
    print("\n[2] 路由 / 接线注册（文本核对）")
    sp = SERVER.read_text(encoding="utf-8", errors="ignore") if SERVER.exists() else ""
    for needle in [
        "import kronos  # noqa: E402",
        "/api/kronos\"",
        "/api/kronos/status\"",
        "kronos.ensure_tables(DB.conn)",
        "/api/kronos/run\"",
    ]:
        check(needle in sp, f"server.py 含 {needle!r}")

    aj = APPJS.read_text(encoding="utf-8", errors="ignore") if APPJS.exists() else ""
    for needle in [
        'tab: "kronos"',
        'if (tab === "kronos")',
        "function loadKronos(",
        "function renderKronos(",
        "function kronosRun(",
    ]:
        check(needle in aj, f"app.js 含 {needle!r}")


# ---------------------------------------------------------------- 3. DB 落库回读
def test_db():
    print("\n[3] 数据库 ensure_tables + 落库回读")
    import kronos
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    try:
        kronos.ensure_tables(con)
        con.execute(
            "INSERT INTO kronos_run (run_date,engine_ver,model_ver,data_date,status,"
            "n_stocks,elapsed,note,at) VALUES (?,?,?,?,?,?,?,?,?)",
            ("2026-10-24", kronos.ENGINE_VER, "NeoQuasar/Kronos-mini@main",
             "2026-10-23", "done", 2, 12.3, "", "2026-10-24 10:00:00"))
        con.execute(
            "INSERT INTO kronos_item (run_date,code,name,last_close,pred_close,"
            "pred_return,pred_dir,disp,bt_total_ret,bt_annual,bt_vol,bt_sharpe,"
            "bt_maxdd,bt_winrate,bt_trades,sample_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("2026-10-24", "600000", "浦发银行", 10.0, 10.5, 0.05, "up", 0.01,
             0.12, 0.20, 0.18, 1.1, -0.08, 0.55, 120, ""))
        con.commit()
        p = kronos.payload(con)
        check(p.get("ok") is True, "payload 返回 ok")
        check(p.get("n_stocks") == 1, "payload 回读出 1 条 item", str(p.get("n_stocks")))
        it = (p.get("items") or [{}])[0]
        check(abs(it.get("pred_return") - 0.05) < 1e-9, "pred_return 回读正确",
              str(it.get("pred_return")))
        s = kronos.status_payload(con)
        check(s.get("last_run") == "2026-10-24", "status 回读 last_run")
    finally:
        con.close()


# ---------------------------------------------------------------- 4. 回测指标数学
def test_metrics():
    print("\n[4] 回测指标数学（合成收益序列）")
    import kronos
    # 全涨 1%/日 → 总收益应为 (1.01)^n - 1，夏普为正，胜率 1.0
    n = 10
    rets = [0.01] * n
    m = kronos._metrics(rets)
    exp_total = (1.01 ** n) - 1
    check(abs(m["bt_total_ret"] - exp_total) < 1e-9, "总收益公式正确",
          f"{m['bt_total_ret']:.4f} vs {exp_total:.4f}")
    check(abs(m["bt_winrate"] - 1.0) < 1e-9, "胜率=1.0", str(m["bt_winrate"]))
    check(m["bt_trades"] == n, "交易次数=n", str(m["bt_trades"]))
    check(m["bt_maxdd"] == 0.0, "全涨无回撤", str(m["bt_maxdd"]))
    # 全跌 → 总收益为负、夏普为负、最大回撤为负
    m2 = kronos._metrics([-0.01] * n)
    check(m2["bt_total_ret"] < 0 and m2["bt_maxdd"] < 0, "全跌指标符号正确")
    # 空序列
    check(kronos._metrics([]) == {}, "空序列返回 {}")


def test_future_dates():
    print("\n[5] 前瞻交易日生成（跳过周末）")
    import kronos
    d = kronos._future_dates("2026-10-23", 5)  # 周五
    check(len(d) == 5, "生成 5 个日期", str(d))
    # 2026-10-23 是周五；未来 5 个交易日应是 10-26(一)..10-30(五)，无周末
    weekend = [x for x in d if __import__("datetime").datetime.strptime(x, "%Y-%m-%d").weekday() >= 5]
    check(not weekend, "不含周末", str(weekend))


# ---------------------------------------------------------------- 6. 真实推理冒烟（可选）
def test_real_inference():
    print("\n[6] 真实推理冒烟（若本机已装 torch + 权重）")
    try:
        import torch  # noqa: F401
    except Exception:  # noqa: BLE001
        print("  ⏭️  沙箱无 torch，跳过真实推理（装完 setup_kronos.py 后可在桌面重跑）")
        return
    import kronos
    ready = kronos._probe_ready()
    if not ready.get("ok"):
        print(f"  ⏭️  推理链未就绪：{ready.get('hint')}")
        return
    import pandas as pd
    # 造 500 根随机游走日 K，验证 predict 能吐出 PRED_LEN 根 close
    import numpy as np
    rng = np.random.default_rng(0)
    price = 10.0
    rows = []
    base = __import__("datetime").datetime(2024, 1, 1)
    for i in range(500):
        price *= (1 + rng.normal(0, 0.01))
        d = (base + __import__("datetime").timedelta(days=i)).strftime("%Y-%m-%d")
        rows.append((d, price * 0.99, price * 1.01, price * 0.98, price, 1e6))
    pred = kronos._predict_close(kronos._load_predictor(), rows, None,
                                 kronos.PRED_LEN, kronos._future_dates(rows[-1][0], kronos.PRED_LEN), 1)
    out, _ = pred
    check(len(out) == kronos.PRED_LEN, "predict 返回 PRED_LEN 根 close", str(len(out)))
    check(all(isinstance(x, float) for x in out), "预测值为 float")


def main():
    print("=" * 64)
    print("Kronos 板块自测")
    print("=" * 64)
    test_compile()
    test_routes()
    test_db()
    test_metrics()
    test_future_dates()
    test_real_inference()
    print("\n" + "=" * 64)
    print(f"结果：{len(PASS)} 通过 / {len(FAIL)} 失败")
    for name, d in FAIL:
        print(f"  ✗ {name}: {d}")
    print("=" * 64)
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
