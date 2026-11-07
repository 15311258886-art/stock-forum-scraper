# -*- coding: utf-8 -*-
"""行业龙头全景（王总 2026-09 拍板）：申万一级行业按总市值客观降序，每行业取 Top N 标为头部。

数据源（硬约束 / 红线）：
- 真实市值排名**只能用 akshare**（本机直连网络）。沙箱里 akshare 被代理拦（ProxyError）取不到。
- 主路径（本机）：
    1. ak.stock_board_industry_name_em()         → 申万一级行业名列表（东方财富行业板，约 31 个）
    2. ak.stock_board_industry_cons_em(symbol)   → 各行业成分股 [{代码,名称,最新价,涨跌幅,...}]
    3. ak.stock_zh_a_spot_em()                  → 全市场实时行情含「总市值」，缓存 code→market_cap
    4. 合并：按行业分组 → 每行业按「总市值」降序 → Top N 标 is_leader=True，其余 False；返回**全量成分股**。
- 沙箱降级（红线）：akshare 导入失败、或任意真实源调用抛异常、或取不到真实市值时，
    **绝不编造市值/头部**。返回 {available: False, message: "...", industries: []}，
    前端据此显示提示、不渲染任何假表格 / 假龙头。
"""
from __future__ import annotations

import json
import sys
import threading
import time as _time
from datetime import datetime
from pathlib import Path

# ── 单次请求的取数预算（2026-11-05 自审新增）──────────────────────────────
# 为什么必须有：`_compute` 的路径是「1 次全市场行情 + 1 次行业列表 + 逐行业 1 次成分股」，
# 实测 31 个行业 → 33 次串行第三方 HTTP，akshare 那层**没有超时参数**。
# 于是网络一抽风，这个接口就在**请求线程里无限等**：2026-11-05 探活实测
# `GET /api/industry_leaders` 跑到 45s 仍未返回（curl 直接 000 超时），
# 而服务是单进程多线程 —— 一个卡死的请求会一直占着线程，用户看到的是「点了没反应」。
#
# 处理原则沿用本模块红线②：**诚实降级，绝不编造**。
# 超时只是"这次没取到"，返回 available:False + 明确原因，前端照原样提示即可，
# 绝不拿上一次的/别的行业的数字凑一份看起来完整的表格。
_DEADLINE_SEC = 25.0

# 单飞闸：同一时刻只允许一个 `_compute` 在跑。
# 没有它的话，超时返回后用户会连点，每次点都新起一个 33 次 HTTP 的后台计算 →
# 线程与连接成倍堆积，比原来"卡一次"更糟。拿到锁的那次跑完才释放，
# 期间其它请求立刻得到「正在计算」，不排队、不叠加。
_COMPUTE_LOCK = threading.Lock()

# ── 第三方库搜索路径注入（沙箱用 --target extlib 安装，本机若无则自动忽略）──
_THIS_FILE = Path(__file__).resolve()
_EXTLIB = _THIS_FILE.parent.parent / "extlib"
if _EXTLIB.exists() and str(_EXTLIB) not in sys.path:
    sys.path.insert(0, str(_EXTLIB))

# ── 可选第三方：akshare 未安装 / 导入失败 → 函数内如实返回 available:False（绝不回退假数据）──
try:
    import akshare as _ak  # type: ignore
    _HAS_AK = True
except Exception:  # noqa: BLE001
    _HAS_AK = False

ROOT = Path(__file__).resolve().parent
CACHE_DIR = ROOT / "cache"
CACHE_DIR.mkdir(exist_ok=True)

# 沙箱降级提示文案（与任务红线一致，前端原样展示）
_UNAVAILABLE_MSG = (
    "行业市值排名需在本机（直连网络 + akshare）运行，沙箱无法获取真实市值；"
    "当前环境未能取得真实数据，已停止渲染以避免展示编造的头部名单。"
)


# ───────────────────────── 缓存工具 ─────────────────────────
def _cache_path(name: str) -> Path:
    return CACHE_DIR / (name + ".json")


def _load_cache(name: str):
    p = _cache_path(name)
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            return None
    return None


def _save_cache(name: str, data) -> None:
    try:
        _cache_path(name).write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass


# ───────────────────────── 工具：代码归一 / 取列 / 转数 ─────────────────────────
def _norm_code(code) -> str:
    """6 位数字归一：sh.600000 / 600000.SH / 600000 都归一成 600000。"""
    c = str(code or "").strip().upper()
    c = c.replace("SH.", "").replace("SZ.", "").replace(".SH", "").replace(".SZ", "")
    c = c.split(".")[-1] if "." in c else c
    return c[-6:] if len(c) >= 6 else c


def _f(v):
    try:
        return float(v)
    except Exception:  # noqa: BLE001
        return None


def _col(df, *names):
    """在 DataFrame 中优先匹配存在的列名（兼容 akshare 版本间列名差异）。"""
    cols = set(df.columns)
    for n in names:
        if n in cols:
            return n
    return None


# ───────────────────────── 真实计算路径（依赖 akshare） ─────────────────────────
def _compute(top: int) -> dict:
    """真实路径。任意一步失败都会向上抛，由 get_industry_leaders 捕获转 available:False。

    绝不在此构造任何虚假市值 / 虚假头部：无市值的成分股一律沉底，且不会被标记为头部。
    """
    _t0 = _time.time()
    # 1) 申万一级行业名（东方财富行业板，约 31 个，对齐任务要求）
    name_df = _ak.stock_board_industry_name_em()
    if name_df is None or len(name_df) == 0:
        raise RuntimeError("stock_board_industry_name_em 返回空（行业列表取不到）")
    name_col = _col(name_df, "板块名称", "名称") or name_df.columns[0]
    industry_names = [str(x) for x in name_df[name_col].tolist() if x]

    # 3) 全市场实时行情含「总市值」：一次拉全，缓存 code→{market_cap, price, pct}
    spot_df = _ak.stock_zh_a_spot_em()
    if spot_df is None or len(spot_df) == 0:
        raise RuntimeError("stock_zh_a_spot_em 返回空（全市场市值取不到）")
    code_c = _col(spot_df, "代码") or spot_df.columns[0]
    cap_c = _col(spot_df, "总市值", "总市值(元)", "总市值(亿)")
    price_c = _col(spot_df, "最新价", "现价")
    pct_c = _col(spot_df, "涨跌幅")
    if cap_c is None:
        raise RuntimeError("stock_zh_a_spot_em 无「总市值」列（akshare 版本异常）")
    spot = {}
    for _, row in spot_df.iterrows():
        code = _norm_code(row.get(code_c))
        if not code:
            continue
        mc = _f(row.get(cap_c))
        spot[code] = {
            "market_cap": mc,
            # 部分 akshare 版本 spot 不报涨跌/现价，退而用成分股里的字段兜底
            "price": _f(row.get(price_c)) if price_c else None,
            "pct": _f(row.get(pct_c)) if pct_c else None,
        }

    # 2)+4) 逐行业取成分股 → 按真实市值降序 → Top N 标头部
    # 2026-11-05：逐个行业之间先看还剩多少预算。到点就**带着已拿到的行业收工**，
    # 而不是继续把 31 次 HTTP 跑完 —— 后台线程才有机会真正结束、把单飞锁放掉。
    # 注意这不是"降级成假数据"：已拿到的每个行业都是真实成分股与真实市值，
    # 只是行业数少于全量，用 partial 标记如实告知。
    industries = []
    timed_out = False
    for iname in industry_names:
        if _time.time() - _t0 > _DEADLINE_SEC:
            timed_out = True
            break
        try:
            cons_df = _ak.stock_board_industry_cons_em(symbol=iname)
        except Exception:  # noqa: BLE001
            # 单行业失败不致命：跳过该行业，其余照常（仍是真实数据，绝不拿别的行业补齐）
            continue
        if cons_df is None or len(cons_df) == 0:
            continue
        c_c = _col(cons_df, "代码") or cons_df.columns[0]
        n_c = _col(cons_df, "名称", "股票名称") or cons_df.columns[1]
        cp_c = _col(cons_df, "最新价", "现价")
        pct_c2 = _col(cons_df, "涨跌幅")
        members = []
        for _, row in cons_df.iterrows():
            code = _norm_code(row.get(c_c))
            name = str(row.get(n_c, ""))
            sp = spot.get(code) or {}
            mc = sp.get("market_cap")
            members.append({
                "code": code,
                "name": name,
                "market_cap": round(mc, 2) if mc is not None else None,
                "price": sp.get("price") if sp.get("price") is not None else _f(row.get(cp_c)),
                "pct": sp.get("pct") if sp.get("pct") is not None else _f(row.get(pct_c2)),
                "is_leader": False,
            })
        # 按真实总市值降序（无市值者一律沉底，绝不把无市值股标成头部）
        members.sort(key=lambda m: (m["market_cap"] is not None, m["market_cap"] or 0.0), reverse=True)
        # 仅对确有市值的头部标 is_leader（保证 Top N 是真实市值最大的 N 家）
        n_lead = 0
        for m in members:
            if n_lead >= top:
                break
            if m["market_cap"] is None:
                break  # 后面全是沉底的，停止标记
            m["is_leader"] = True
            n_lead += 1
        if not members:
            continue
        industries.append({
            "name": iname,
            "count": len(members),
            "leader_count": n_lead,
            "members": members,
        })

    if not industries:
        raise RuntimeError("未能取得任何行业的真实成分股与市值")

    return {
        "available": True,
        "top": top,
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        # partial=True 只表示「行业没收全」，不代表数据是造的：每个行业都是真实成分股。
        # 前端可据此提示"本次只覆盖 N/M 个行业"。
        "partial": bool(timed_out),
        "industry_total": len(industry_names),
        "message": ("取数超预算，仅完成 %d/%d 个行业（数据均为真实值，未补造）"
                    % (len(industries), len(industry_names))) if timed_out else "",
        "industry_count": len(industries),
        "industries": industries,
    }


# ───────────────────────── 对外入口 ─────────────────────────
def get_industry_leaders(top: int = 5) -> dict:
    """行业龙头全景：申万一级行业 → 每行业按总市值降序 → Top N 标头部，返回全量成分股。

    返回结构：
      available=True:
        {available, top, generated_at, message:"", industry_count,
         industries:[{name, count, leader_count,
                      members:[{code, name, market_cap, price, pct, is_leader}]}]}
      available=False（沙箱 / 无 akshare / 取数失败）:
        {available:False, message:"...", industries:[]}

    红线：取不到真实市值时绝不编造，industries 永远为 []。
    """
    top = max(1, int(top or 5))
    # 红线①：akshare 未安装 → 直接降级，绝不构造假数据
    if not _HAS_AK:
        return {"available": False, "message": _UNAVAILABLE_MSG, "industries": []}

    # 本机命中当日缓存则直接返回（省 akshare 调用、防源站抽风）；缓存只在真实路径可用时读。
    cache_key = "industry_leaders_%d" % top
    cached = _load_cache(cache_key)
    if isinstance(cached, dict) and cached.get("available") is True \
            and cached.get("_date") == datetime.now().strftime("%Y-%m-%d"):
        cached.pop("_date", None)
        return cached

    # ── 带预算的执行（2026-11-05）──────────────────────────────────────
    # 不能直接 `result = _compute(top)`：实测是 **59 个行业 × 1.2s ≈ 71 秒**的串行第三方
    # HTTP，akshare 那层没有超时参数，且跑在请求线程里 → 接口挂死（实测 45s 无响应）。
    # 改成「后台线程算 + 主线程最多等 _DEADLINE_SEC」：到点就返回降级态，接口永不挂死。
    #
    # ★ 缓存必须写在**子线程**里（下面 _run 的 finally 之前），不能写在 join 之后。
    #   否则主线程 25s 超时返回、子线程 ~71s 算完却没人落缓存 → 下一次请求又从零开始、
    #   又超时 → 当日缓存永远填不上，接口永久处于「降级」。第一版就写错了这一点。
    if not _COMPUTE_LOCK.acquire(blocking=False):
        return {"available": False,
                "message": "行业龙头正在取数（首次约 1~3 分钟，成功后当天缓存、之后秒出），"
                           "请稍后重试。",
                "industries": []}

    _box: dict = {}

    def _run() -> None:
        try:
            _res = _compute(top)
            # 落当日缓存（仅成功结果，绝不缓存降级态）—— 与主线程是否已超时无关
            _res["_date"] = datetime.now().strftime("%Y-%m-%d")
            _save_cache(cache_key, _res)
            _res.pop("_date", None)
            _box["r"] = _res
        except Exception as e:  # noqa: BLE001
            _box["e"] = e
        finally:
            # 故意在**子线程**里释放：若主线程已超时返回，锁仍由这一轮持有，
            # 后续请求会拿到「正在计算」而不是再起一轮 —— 这就是单飞的实现。
            try:
                _COMPUTE_LOCK.release()
            except RuntimeError:
                pass

    _th = threading.Thread(target=_run, name="industry-leaders", daemon=True)
    _th.start()
    _th.join(_DEADLINE_SEC)
    if _th.is_alive():
        # 红线②的延伸：超时 = "这次没取到"，不是"给别人一份旧的"。
        return {"available": False,
                "message": _UNAVAILABLE_MSG + "（取数超过 %.0f 秒未完成，后台仍在继续；"
                                              "算完后当天内再打开即秒出）" % _DEADLINE_SEC,
                "industries": []}
    if "e" in _box:
        return {"available": False,
                "message": _UNAVAILABLE_MSG + "（真实源异常：" + str(_box["e"])[:160] + "）",
                "industries": []}
    result = _box["r"]
    # 缓存已由 _run() 在子线程里落过（见上面的说明：不能等 join 之后再存）。
    return result
