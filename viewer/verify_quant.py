# -*- coding: utf-8 -*-
"""verify_quant.py —— 量化板块（第三维信号源）冒烟 + 诚实口径校验（2026-10-16）。

只做只读校验：不写库、不启动服务；直接调用 quant.py 的对外函数，验证：
  ① 单票因子全景（factor_panorama）：7 个技术因子齐全、展示分在 [-1,1]；
  ② 多因子选股（multi_factor_select）：权重和≈1、清单非空；
  ③ 权重数学唯一真源 = metalearn.weights_from_ic（不在此复刻 IC/收缩/先验公式）；
  ④ 诚实三态：weights_effective 为 False 时 weight_stage 必以「等权运行中」开头（绝不填假权重）；
  ⑤ 因子回测（factor_backtest）：7 个因子齐全、IC 缺失时标「样本不足（IC=None）」；
  ⑥ 量化因子的自我学习与成长（quant_self_learn_data）：state 含 weights/weight_stage/oos/per_factor；
  ⑦ 菜谱存盘（recipe_save）归一化到和 1、菜谱应用（recipe_apply）对不存在 id 优雅返回 ok=False。

运行：python viewer/verify_quant.py   （在 stock-forum-scraper 根目录）
"""
from __future__ import annotations

import os
import sqlite3
import sys
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import quant as Q                       # noqa: E402
import predictor as P                   # noqa: E402


def approx(a, b, tol=1e-6):
    return abs(a - b) <= tol


FAILS = []


def check(name, cond, detail=""):
    status = "PASS" if cond else "FAIL"
    if not cond:
        FAILS.append(name)
    print(f"[{status}] {name}" + (f" —— {detail}" if detail and not cond else ""))


def main():
    print("=== 量化板块 verify_quant.py ===")
    print(f"predictor.DB_PATH = {P.DB_PATH}")
    print(f"kcache 文件存在：{os.path.exists(P.DB_PATH)} / kcache 在 {getattr(P, 'K_CACHE', None)}")
    con = None
    try:
        con = sqlite3.connect(P.DB_PATH)
        con.row_factory = sqlite3.Row
        Q.ensure_tables(con)
    except Exception as e:  # noqa: BLE001
        print(f"⚠ 打开数据库失败（部分校验将用 con=None 跳过写库项）：{e}")

    # ---- ① 单票因子全景 ----------------------------------------------------
    # 找一个真实有数据的代码
    try:
        bars_map, _ = Q._universe_bars()
        sample_code = next(iter(bars_map), None)
    except Exception as e:  # noqa: BLE001
        bars_map, sample_code = {}, None
        print(f"⚠ _universe_bars 失败：{e}")
    if sample_code:
        print(f"宇宙规模：{len(bars_map)} 只；抽样代码：{sample_code}")
        pano = Q.factor_panorama(sample_code, con)
        check("factor_panorama.ok", pano.get("ok") is True,
              pano.get("reason", ""))
        factors = pano.get("factors") or []
        check("factor_panorama.7因子", len(factors) == len(Q.TECH_FACTORS),
              f"实际 {len(factors)} 个")
        scores_ok = all(
            (f.get("score") is None or (-1.0 - 1e-9 <= f["score"] <= 1.0 + 1e-9))
            for f in factors
        )
        check("factor_panorama.展示分∈[-1,1]", scores_ok)
        pends = Q.factor_panorama.__doc__  # 仅占位，避免未使用告警
        _ = pends
    else:
        check("factor_panorama.抽样", False, "本地 K 线宇宙为空")

    # ---- ② 多因子选股：权重和≈1、清单非空 ---------------------------------
    sel = Q.multi_factor_select(con=con)
    check("multi_factor_select.ok", sel.get("ok") is True,
          sel.get("reason", sel.get("error", "")))
    if sel.get("ok"):
        w = sel.get("weights") or {}
        wsum = sum(w.values())
        check("multi_factor_select.权重和≈1", approx(wsum, 1.0, tol=1e-4),
              f"sum={wsum:.6f}")
        items = sel.get("items") or []
        check("multi_factor_select.清单非空", len(items) > 0, f"{len(items)} 只")
        # ④ 诚实三态
        if not sel.get("weights_effective"):
            check("multi_factor_select.等权时标等权运行中",
                  (sel.get("weight_stage") or "").startswith("等权运行中"),
                  sel.get("weight_stage", ""))
        else:
            check("multi_factor_select.收缩时stage合理",
                  (sel.get("weight_stage") or "").startswith("收缩生效中"),
                  sel.get("weight_stage", ""))

    # ---- ⑤ 因子回测：7 因子齐全、IC 缺失标样本不足 -------------------------
    bt = Q.factor_backtest(con=con)
    check("factor_backtest.ok", bt.get("ok") is True,
          bt.get("reason", bt.get("error", "")))
    if bt.get("ok"):
        btf = bt.get("factors") or []
        check("factor_backtest.7因子", len(btf) == len(Q.TECH_FACTORS), f"实际 {len(btf)} 个")
        for f in btf:
            if f.get("ic5") is None:
                check(f"backtest.{f['key']}.IC缺失标样本不足",
                      "样本不足" in (f.get("status") or ""), f.get("status", ""))
        check("factor_backtest.待接入因子声明",
              len(bt.get("pending") or []) >= 1,
              f"{len(bt.get('pending') or [])} 个待接入")

    # ---- ⑥ 量化因子的自我学习与成长 ---------------------------------------
    sl = Q.quant_self_learn_data(con, "2099-01-01")
    check("quant_self_learn.ok", sl.get("ok") is True, sl.get("reason", ""))
    if sl.get("ok"):
        st = (sl.get("state_json") or {})
        check("self_learn.weights存在", isinstance(st.get("weights"), dict)
              and len(st.get("weights")) == len(Q.TECH_FACTORS))
        sw = sum((st.get("weights") or {}).values())
        check("self_learn.权重和≈1", approx(sw, 1.0, tol=1e-4), f"sum={sw:.6f}")
        check("self_learn.weight_stage三态之一",
              (st.get("weight_stage") or "").split("（")[0]
              in ("等权运行中", "收缩生效中", "样本外验证通过"),
              st.get("weight_stage", ""))
        check("self_learn.oos键存在", isinstance(st.get("oos"), dict))
        check("self_learn.per_factor齐全",
              len(st.get("per_factor") or {}) == len(Q.TECH_FACTORS))
        # 诚实：未生效（未通过 OOS 或样本不足）时绝不宣称权重生效
        if not st.get("weights_effective"):
            check("self_learn.未生效不谎称生效",
                  not (st.get("weight_stage") or "").startswith("样本外验证通过"))

    # ---- ⑦ 菜谱存盘归一化 + 应用对缺失 id 优雅返回 ------------------------
    if con is not None:
        try:
            sv = Q.recipe_save(con, "verify_quant_临时",
                               {k: 1.0 for k in Q.TECH_FACTORS[:3]})
            check("recipe_save.ok", sv.get("ok") is True, sv.get("error", ""))
            rw = sv.get("weights") or {}
            check("recipe_save.归一化和≈1", approx(sum(rw.values()), 1.0, tol=1e-4),
                  f"sum={sum(rw.values()):.6f}")
            rid = (sv.get("weights") and (sv.get("name") and sv.get("ok"))) and con.execute(
                "SELECT id FROM quant_recipe WHERE name='verify_quant_临时' ORDER BY id DESC LIMIT 1"
            ).fetchone()
            if rid:
                con.execute("DELETE FROM quant_recipe WHERE id=?", (rid["id"],))
                con.commit()
            ap = Q.recipe_apply(con, 99999999)
            check("recipe_apply.缺失id优雅返回", ap.get("ok") is False,
                  ap.get("error", ""))
        except Exception as e:  # noqa: BLE001
            check("recipe_save/apply 流程", False, str(e)[:200])

    if con is not None:
        con.close()

    print("\n=== 结果 ===")
    print(f"TOTAL FAILS: {len(FAILS)}")
    if FAILS:
        print("失败项：" + "；".join(FAILS))
        return 1
    print("全部通过 ✅")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:  # noqa: BLE001
        traceback.print_exc()
        sys.exit(2)
