# -*- coding: utf-8 -*-
"""增强功能② 资金流向（东财 push2 fflow/kline + clist 主力排行）。

字段序（官方，2026-09-27 校正）：
  - 个股资金流 K 线（fflow/kline）：f51日期 / f52主力净额 / f53小单 / f54中单
    / f55大单 / f56超大单 / f57主力净占比 / f62收盘 / f63涨跌幅 / f64成交额
  - 全市场排行（clist）：f2最新价 / f3涨跌幅 / f12代码 / f14名称
    / f62今日主力净流入 / f184今日主力净占比 / f66超大单 / f72大单

v2 修正（2026-09-27 晚）：
  · clist 用排除停牌的 fs（含 +f:!2），避开 ST/退市等无效条目干扰；
  · emapi.clist/fflow 已内置多 ut 重试 + push2his 兜底；
  · 失败给出明确 reason 让前端显示"接口异常：..."而非裸"暂无数据"。

v3 负缓存（2026-10-01，BUG 报告 P0「东财 push2 全系列不可达 → 每次请求空转 3-14 秒」）：
  · **失败结果**落盘缓存 30 分钟（moneyflow_negcache.json），命中直接返回，不再真去打东财，
    用户不用盯着转圈 13 秒才看到一句"源不可达"；
  · 返回体带 `cached_at` / `stale=True`，前端可标「东财源本次不可达（负缓存，30 分钟内复用）」；
  · `force=True` 跳过负缓存（前端「强制重试」按钮走这条路，避免"永远被缓存挡住"）。
  · 只缓存**失败**结果，成功结果从不落缓存（资金流天天变，缓存成功会造成口径漂移）。
"""
from __future__ import annotations
import json
import os
import time
import emapi

# 排除停牌的沪深京 A 股全集（与 akshare 主力资金流口径一致）
_A_SHARE_FS = ("m:0+t:6+f:!2,m:0+t:13+f:!2,m:0+t:80+f:!2,"
               "m:1+t:2+f:!2,m:1+t:23+f:!2,m:0+t:7+f:!2,m:1+t:3+f:!2")

# 资金流排行字段（含最新价 + 涨跌幅 + 主力净额 + 主力占比 + 超大单 + 大单）
_RANK_FIELDS = "f2,f3,f12,f14,f62,f184,f66,f72"

# ---------- 失败负缓存 ----------
_NEG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         "moneyflow_negcache.json")
_NEG_TTL = 30 * 60          # 30 分钟
_NEG_MAX = 200              # 最多记 200 条，防磁盘无限胀


def _neg_load() -> dict:
    try:
        if os.path.exists(_NEG_PATH):
            with open(_NEG_PATH, encoding="utf-8") as fh:
                d = json.load(fh)
            if isinstance(d, dict):
                return d
    except Exception:  # noqa: BLE001
        pass
    return {}


def _neg_save(d: dict) -> None:
    try:
        tmp = _NEG_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(d, fh, ensure_ascii=False)
        os.replace(tmp, _NEG_PATH)   # 原子替换，避免写到一半被读成半个 json
    except Exception:  # noqa: BLE001
        pass


def _neg_get(key: str):
    """命中未过期的失败记录就返回 payload（带 stale 标记）；否则 None。"""
    d = _neg_load()
    item = d.get(key)
    if not isinstance(item, dict):
        return None
    if time.time() - float(item.get("ts") or 0) > _NEG_TTL:
        return None           # 过期，删掉脏条目
    p = dict(item.get("payload") or {})
    p["stale"] = True
    p["cached_at"] = time.strftime("%H:%M:%S", time.localtime(float(item.get("ts") or 0)))
    p["neg_min_left"] = int((_NEG_TTL - (time.time() - float(item.get("ts") or 0))) // 60)
    return p


def _neg_put(key: str, payload: dict) -> None:
    d = _neg_load()
    d[key] = {"ts": time.time(), "payload": payload}
    # 顺手清掉过期条目
    now = time.time()
    for k in [k for k, v in d.items()
              if isinstance(v, dict) and now - float(v.get("ts") or 0) > _NEG_TTL]:
        d.pop(k, None)
    while len(d) > _NEG_MAX:                     # 超量时先丢最老的
        try:
            k = min(d, key=lambda k: float(d[k].get("ts") or 0))
            d.pop(k, None)
        except Exception:  # noqa: BLE001
            break
    _neg_save(d)


def neg_stats() -> dict:
    """给面板/自测用的负缓存状态（条数、最早/最晚写入时间）。"""
    d = _neg_load()
    ts = [float(v.get("ts") or 0) for v in d.values() if isinstance(v, dict)]
    return {"path": _NEG_PATH, "ttl_min": _NEG_TTL // 60, "count": len(d),
            "oldest": (time.strftime("%H:%M:%S", time.localtime(min(ts))) if ts else None),
            "newest": (time.strftime("%H:%M:%S", time.localtime(max(ts))) if ts else None)}


# ---------- 个股资金流 ----------
def stock(symbol: str, force: bool = False) -> dict:
    """个股资金流（近 N 个交易日）。force=True 跳过 30 分钟负缓存（强制重试）。"""
    key = f"stock:{symbol}"
    if not force:
        hit = _neg_get(key)
        if hit is not None:
            hit.update({"ok": False, "symbol": symbol, "source": "eastmoney",
                        "negative_cache": True})
            return hit
    try:
        sid = emapi.secid(symbol)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "symbol": symbol, "source": "eastmoney",
                "reason": f"未取到 {symbol} 的资金流（代码解析失败：{e}）"}
    rows = emapi.fflow(sid, lmt=6)
    if not rows:
        payload = {"ok": False, "symbol": symbol, "source": "eastmoney",
                   "reason": f"未取到 {symbol} 的资金流（东财接口不可达或该代码无资金流数据）",
                   "tip": "东财 push2 对本机程序请求限流；可稍后点「强制重试」，或改用「板块资金流」标签页（数据来自新浪，稳定）。",
                   "negative_cache": True}
        _neg_put(key, payload)
        return dict(payload)
    last = rows[-1]
    return {
        "ok": True, "symbol": symbol, "date": last.get("date"),
        "main_net": last.get("main_net"), "main_pct": last.get("main_pct"),
        "huge_net": last.get("huge_net"), "big_net": last.get("big_net"),
        "mid_net": last.get("mid_net"), "small_net": last.get("small_net"),
        "recent": [{"date": r.get("date"), "main_net": r.get("main_net")} for r in rows],
    }


# ---------- 全市场主力净流入排行 ----------
def rank(pz: int = 30, force: bool = False) -> dict:
    key = f"rank:{pz}"
    if not force:
        hit = _neg_get(key)
        if hit is not None:
            hit.update({"ok": False, "source": "eastmoney", "negative_cache": True})
            return hit
    rows = emapi.clist(_A_SHARE_FS, _RANK_FIELDS, pz=pz, fid="f62", po=1)
    if not rows:
        payload = {"ok": False, "rows": [], "source": "eastmoney",
                   "reason": ("数据源暂不可用：个股主力净流入排行依赖东财 push2，"
                              "当前接口不可达（可能是机房限流/运营商屏蔽）。"
                              "主力净流入为东财独有委托流数据，新浪/腾讯均无此源，无法自动兜底——"
                              "请稍后重试，或改用「板块资金流」标签页（新浪源，稳定）。"),
                   "tip": "主力净流入榜依赖东财 push2，常被本机密集请求限流；"
                          "可改用「板块资金流」标签页（新浪源，稳定），或稍后重试。",
                   "negative_cache": True}
        _neg_put(key, payload)
        return dict(payload)
    out = []
    for x in rows:
        out.append({
            "code": x.get("f12"), "name": x.get("f14"),
            "price": emapi._num(x.get("f2")), "pct": emapi._num(x.get("f3")),
            "main_net": emapi._num(x.get("f62")),
            "main_pct": emapi._num(x.get("f184")),
            "huge_net": emapi._num(x.get("f66")),
            "big_net": emapi._num(x.get("f72")),
        })
    return {"ok": True, "rows": out}


# ---------- 板块资金流排行（旧兜底口径） ----------
def board_rank(pz: int = 30, force: bool = False) -> dict:
    key = f"board:{pz}"
    if not force:
        hit = _neg_get(key)
        if hit is not None:
            hit.update({"ok": False, "source": "eastmoney", "negative_cache": True})
            return hit
    rows = emapi.clist(emapi.FS_BOARD_FUND, "f12,f14,f3,f62,f184,f66",
                       pz=pz, fid="f62", po=1)
    if not rows:
        payload = {"ok": False, "rows": [], "source": "eastmoney",
                   "reason": ("数据源暂不可用：板块资金流排行依赖东财 push2，当前接口不可达。"
                              "请改用「板块资金流」标签页（api_board_flow_snapshot，新浪源，稳定），"
                              "或稍后重试。"),
                   "tip": "板块资金流已迁移到「板块资金流」标签页（api_board_flow_snapshot，新浪源，稳定）；旧 board_rank 仅作兜底。",
                   "negative_cache": True}
        _neg_put(key, payload)
        return dict(payload)
    out = []
    for x in rows:
        out.append({
            "board_code": x.get("f12"), "board_name": x.get("f14"),
            "pct": emapi._num(x.get("f3")),
            "main_net": emapi._num(x.get("f62")),
            "main_pct": emapi._num(x.get("f184")),
            "huge_net": emapi._num(x.get("f66")),
        })
    return {"ok": True, "rows": out}
