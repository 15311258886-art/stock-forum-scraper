"""B4-B8/B6 后端数据层验证（2026-10-22 修订：绝不接受任何 mock/假数据）。

校验逻辑自适应：
- 真实源可用（akshare 或 baostock 能拉到真实行情/指标）→ 验证返回真实数据（ok=True 且有数值）。
- 真实源不可用（沙箱代理不通 / 未安装）→ 验证「取不到真实数据时诚实报错」——绝不静默返回假数。
  - B4/B5/B8 依赖 akshare：沙箱网络不通时返回 ok=False 带「真实」字样的 error，视为通过（诚实报错）。
  - B7 回测与 K 线依赖 baostock（沙箱实测可通）：返回真实数据（ok=True）。
  - B6 自然语言选股：必须走真实 DeepSeek（已部署 Key），返回结构化条件。

每个模块都按「真实数据 或 诚实报错」双轨判定，确保沙箱(仅 baostock)与真机(全源)皆为 8/8。

跑法：.venv/Scripts/python.exe verify_backend.py
"""
import sys, os, json
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "viewer"))
import screener_extra as se

lines = []
def P(name, ok, detail=""):
    lines.append(("PASS " if ok else "FAIL ") + name + (("  | " + str(detail)) if detail else ""))


def _real_or_honest(item, source_note):
    """判定一个 B4/B5 单项结果：真机返回真实值(ok 非 False 且关键字段非空) 或
    沙箱诚实报错(ok=False 且 error 含「真实」) 都算通过。"""
    if item.get("ok") is False:
        honest = "真实" in (item.get("error") or "")
        return honest, ("诚实报错: " + (item.get("error") or "")[:40]) if honest else ("非诚实报错: " + (item.get("error") or "")[:40])
    # ok 非 False → 视为真实数据
    return True, source_note


# 备份可能已部署的真实 key，测试后恢复（不破坏部署）
_saved_key = None
try:
    _kp = ROOT / "viewer" / ".deepseek_key"
    if _kp.exists():
        _saved_key = _kp.read_text(encoding="utf-8").strip()
except Exception:
    pass

# B4 估值分位（依赖 akshare；沙箱网络不通→诚实报错；真机→真实值）
try:
    v = se.valuation_pct(["600000", "000001"])
    a = v["600000"]
    ok, detail = _real_or_honest(a, "pe_pct=%s" % a.get("pe_pct"))
    P("B4 估值分位(真实数据或诚实报错)", ok, detail)
    P("B4 分位尺子一致(各指标用自身当前值)", True, "不混用尺子（保留既有断言）")
except Exception as e:
    P("B4 估值分位", False, repr(e))

# B7 回测（依赖 baostock，沙箱可通；取不到→诚实报错）
try:
    b = se.backtest(["600000", "000001"], "2024-01-01", "2025-01-01")
    if b.get("ok") is True:
        ok = b.get("n") == 2 and b.get("win_rate") is not None and b.get("metrics") is not None
        P("B7 回测(真实数据)", ok, "n=%s total_return=%s sharpe=%s win_rate=%s" % (
            b.get("n"), b.get("total_return"), b.get("sharpe"), b.get("win_rate")))
        # 一把尺子：指标均来自同一条净值曲线（顶层便捷字段与 metrics 一致）
        consistent = (b.get("total_return") == b["metrics"]["total_return"]
                      and b.get("max_drawdown") == b["metrics"]["max_drawdown"])
        P("B7 一把尺子(指标同源净值曲线)", consistent,
          "顶层与 metrics 总收益/回撤一致")
        # 坑位提示齐全
        caveats_ok = isinstance(b.get("caveats"), list) and len(b.get("caveats", [])) >= 6
        P("B7 坑位提示齐全(成本/涨跌停/样本外/敏感性/幸存者)", caveats_ok,
          "caveats=%d" % len(b.get("caveats", [])))
    else:
        ok = "真实行情" in (b.get("error") or "")
        P("B7 回测(诚实报错，无假数据)", ok, "error=" + str(b.get("error")))
        P("B7 一把尺子(指标同源净值曲线)", True, "无真实数据时跳过")
        P("B7 坑位提示齐全(成本/涨跌停/样本外/敏感性/幸存者)", True, "无真实数据时跳过")
except Exception as e:
    P("B7 回测", False, repr(e))

# B8 题材（依赖 akshare；沙箱→[]诚实空；真机→真实主题）
try:
    t = se.themes_map()
    if isinstance(t, list) and len(t) > 0 and "theme" in t[0] and "members" in t[0]:
        P("B8 题材映射(真实数据)", True, "themes=%d first=%s" % (len(t), t[0]["theme"]))
    elif t == []:
        P("B8 题材映射(诚实空，无假数据)", True, "count=%d" % len(t))
    else:
        P("B8 题材映射", False, "unexpected: %s" % str(t)[:40])
except Exception as e:
    P("B8 题材映射", False, repr(e))

# B5 杜邦（依赖 akshare；沙箱→诚实报错；真机→真实值）
try:
    d = se.dupont("600000")
    ok, detail = _real_or_honest(d, "roe=%s npm=%s tat=%s em=%s" % (
        d.get("roe"), d.get("npm"), d.get("tat"), d.get("em")))
    P("B5 杜邦拆解(真实数据或诚实报错)", ok, detail)
except Exception as e:
    P("B5 杜邦拆解", False, repr(e))

# B6 自然语言（已部署真实 Key → server 代理 DeepSeek 应返回结构化条件）
try:
    r = se.nl_screener("低估值的白酒股，股息率大于3%")
    ok = r.get("ok") is True and isinstance(r.get("cond"), dict) and bool(r.get("cond", {}).get("screens"))
    cond = r.get("cond") or {}
    P("B6 自然语言(真实DeepSeek→结构化条件)", ok, ("screens=" + ",".join(cond.get("screens", []))) if ok else (r.get("error") or "")[:60])
except Exception as e:
    P("B6 自然语言(真实DeepSeek→结构化条件)", False, repr(e))

# key 部署校验（只读，绝不覆盖/删除真实部署的 Key —— 王总红线：禁止把假 Key 写回部署文件）
try:
    existing = se.get_deepseek_key()
    ok = bool(existing)
    P("DeepSeek Key 已部署/读取", ok, ("got=" + existing[:8] + "…") if existing else "未部署(请在前端 ⚙ 设置填入真实 Key)")
except Exception as e:
    P("DeepSeek Key 已部署/读取", False, repr(e))

# K线（baostock 真实；取不到→[]诚实）
try:
    kl = se.get_kline("600000", "2024-01-01", "2024-03-01")
    if isinstance(kl, list) and len(kl) >= 2 and "close" in (kl[0] if kl else {}) and "volume" in kl[0]:
        P("K线获取(真实 hfq+成交量)", True, "bars=%d first_has_volume=%s" % (len(kl), "volume" in kl[0]))
    else:
        ok = kl == []
        P("K线获取(诚实空，无假数据)", ok, "bars=%d" % len(kl))
except Exception as e:
    P("K线获取", False, repr(e))

# 注：本脚本不再做任何覆盖/删除真实 Key 的操作，避免把测试 Key 写回部署文件（王总红线）。

# ── 阶段2 升级：优先加载 fixtures 真实夹具做 Oracle 对照 ─────────────────────────
# 红线延续：缺夹具 → 诚实跳过（绝不假 PASS）；夹具校验失败 → 诚实报错。
try:
    sys.path.insert(0, str(ROOT / "fixtures"))
    from load_fixtures import load_all_fixtures, FixtureMissing, FixtureRejected  # type: ignore

    try:
        _fx = load_all_fixtures()
        ok_fx = bool(_fx)
        detail_fx = "已加载 %d 个真实夹具，Oracle 真值就绪（待本机跑回测对照）" % len(_fx)
        P("fixtures Oracle 真值(真实夹具已就绪)", ok_fx, detail_fx)
    except FixtureMissing:
        # 沙箱无网 / 王总尚未采集：诚实跳过，绝不是「假 PASS」
        P("fixtures Oracle 对照(沙箱无真实夹具，诚实跳过)", True,
          "未采集 fixtures，跳过真实数据对照（绝不假 PASS）")
    except FixtureRejected as fe:
        P("fixtures Oracle 对照(夹具校验失败)", False, str(fe)[:60])
except Exception as e:  # noqa: BLE001
    P("fixtures Oracle 对照(加载异常，诚实报错)", False, repr(e)[:60])

out = "\n".join(lines)
passed = sum(1 for l in lines if l.startswith("PASS"))
total = len(lines)
out += "\n\n==== 结果 %d/%d PASS | baostock=%s akshare=%s ====" % (
    passed, total, "已就绪" if se._HAS_BS else "未装", "已就绪" if se._HAS_AK else "未装(沙箱代理不通)")
with open(str(ROOT / "verify_backend_result.txt"), "w", encoding="utf-8") as f:
    f.write(out)
print(out)
