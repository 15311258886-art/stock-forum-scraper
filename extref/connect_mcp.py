# -*- coding: utf-8 -*-
"""extref · B 档数据接口（MCP / 合规 API）桥接适配层。

定位（诚实说明）：
  · 真正的 MCP 协议握手要在 MCP 客户端（WorkBuddy / Trae / Cursor）里完成——
    本项目是独立 Python 服务，无法直接发起 MCP 握手。
  · 本模块做的是「取数 → 结构化入库」这一段：接收已通过 MCP / 官方接口拿到的
    **结构化荐股 JSON**，按各信源的字段映射解析，经 store.upsert_signal 入库，
    再交 backtest.backtest_all 回填收益。
  · 对暴露 HTTP/REST 的源（如配置 endpoint 的妙想 Skills），可主动拉取；
    对纯 MCP 协议的源（iFinD / Wind / 通达信 / 恒生聚源），由外部 MCP 客户端取数后，
    把返回 JSON 传给本模块的 connect() 即可入库——**不写任何假数据**。

配置：
  · 密钥/endpoint 放 extref/mcp_keys.json（运行时，已 gitignore）。
  · 提供 extref/mcp_keys.example.json 作为模板，不含真实密钥。
  · 缺失配置且未传入 data 时，connect() 返回 {ok:False, error:...}，零入库。

隔离纪律：只 import extref.store / extref.backtest / extref.collect（只读 _names_batch）；
  不碰任何既有业务表，所有写入经 store.upsert_signal 单表。
"""
from __future__ import annotations

import json
import os
import sys

# 确保本项目根（含 extref 包）在 sys.path，支持直接 `python extref/connect_mcp.py` 运行。
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from datetime import datetime

from extref import store as st

# —— 运行时配置目录（与本项目其它配置同目录）——
_META_DIR = os.path.dirname(os.path.abspath(__file__))
KEYS_PATH = os.path.join(_META_DIR, "mcp_keys.json")
KEYS_EXAMPLE = os.path.join(_META_DIR, "mcp_keys.example.json")

# —— B 档信源白名单 + 元信息 + 字段提示 ——
# field_hints：各源返回 JSON 里字段的可能键名（按优先级匹配），支持中文键。
MCP_SOURCES = {
    "ths-ifind-mcp": {
        "name": "同花顺 iFinD MCP", "kind": "mcp", "tier": "B",
        "url": "https://www.51ifind.com/",
        "protocol": "mcp",   # 纯 MCP 协议，需外部客户端取数后传入 data
        "hint": "在 MCP 客户端配置 iFinD 终端密钥，取数后把返回 JSON 传给 connect()。",
        "fields": {"code": ["code", "symbol", "sec_code", "股票代码", "标的代码"],
                   "name": ["name", "symbol_name", "sec_name", "股票名称", "标的名称"],
                   "date": ["date", "pub_date", "trading_date", "日期", "发布日期"],
                   "direction": ["direction", "view", "rating", "方向", "观点", "评级"],
                   "reason": ["reason", "logic", "rationale", "理由", "逻辑", "研判"],
                   "claim": ["claim", "conclusion", "summary", "结论", "原文", "摘要"]},
    },
    "wind-aifin": {
        "name": "万得 AIFin Market", "kind": "mcp", "tier": "B",
        "url": "https://www.wind.com.cn/",
        "protocol": "mcp",
        "hint": "Wind 账号 + 密钥；积分制。取数后把返回 JSON 传给 connect()。",
        "fields": {"code": ["code", "wind_code", "symbol", "股票代码"],
                   "name": ["name", "sec_name", "股票名称"],
                   "date": ["date", "pub_date", "日期"],
                   "direction": ["direction", "rating", "view", "方向", "评级", "观点"],
                   "reason": ["reason", "logic", "理由", "逻辑"],
                   "claim": ["claim", "conclusion", "结论", "摘要"]},
    },
    "em-miaoxiang-skills": {
        "name": "东方财富妙想 Skills", "kind": "mcp", "tier": "B",
        "url": "https://www.eastmoney.com/",
        "protocol": "http+mcp",  # 若配置 endpoint 可主动 HTTP 拉；否则走 MCP 传入
        "hint": "开通妙想 Skills 权限/密钥；配置 endpoint 后可由本项目主动拉取。",
        "fields": {"code": ["code", "symbol", "stock_code", "股票代码"],
                   "name": ["name", "stock_name", "股票名称"],
                   "date": ["date", "pub_date", "日期"],
                   "direction": ["direction", "view", "rating", "方向", "观点", "评级"],
                   "reason": ["reason", "logic", "理由", "逻辑"],
                   "claim": ["claim", "conclusion", "结论", "摘要"]},
    },
    "tdx-mcp": {
        "name": "通达信 MCP", "kind": "mcp", "tier": "B",
        "url": "https://www.tdx.com.cn/",
        "protocol": "mcp",
        "hint": "通达信 MCP 密钥；取数后把返回 JSON 传给 connect()。",
        "fields": {"code": ["code", "symbol", "股票代码"],
                   "name": ["name", "stock_name", "股票名称"],
                   "date": ["date", "pub_date", "日期"],
                   "direction": ["direction", "view", "方向", "观点"],
                   "reason": ["reason", "logic", "理由", "逻辑"],
                   "claim": ["claim", "conclusion", "结论"]},
    },
    "hsjuyuan-mcp": {
        "name": "恒生聚源 MCP", "kind": "mcp", "tier": "B",
        "url": "https://www.gildata.com/",
        "protocol": "mcp",
        "hint": "恒生聚源账号 + 密钥；取数后把返回 JSON 传给 connect()。",
        "fields": {"code": ["code", "symbol", "stock_code", "股票代码"],
                   "name": ["name", "sec_name", "股票名称"],
                   "date": ["date", "pub_date", "日期"],
                   "direction": ["direction", "rating", "方向", "评级"],
                   "reason": ["reason", "logic", "理由", "逻辑"],
                   "claim": ["claim", "conclusion", "结论"]},
    },
}


# ---------- 配置加载 ----------
def load_keys() -> dict:
    """读 mcp_keys.json；不存在返回 {}。结构：{src_key: {api_key, endpoint, extra}}。"""
    if not os.path.exists(KEYS_PATH):
        return {}
    try:
        with open(KEYS_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def ensure_example() -> str:
    """保证 mcp_keys.example.json 存在（首次部署给出模板）。返回路径。"""
    if not os.path.exists(KEYS_EXAMPLE):
        tpl = {
            "_comment": "复制本文件为 mcp_keys.json 并填入真实密钥（mcp_keys.json 已被 gitignore）。",
            "ths-ifind-mcp": {"api_key": "YOUR_IFIND_KEY", "endpoint": "", "extra": {}},
            "wind-aifin": {"api_key": "YOUR_WIND_KEY", "endpoint": "", "extra": {}},
            "em-miaoxiang-skills": {"api_key": "YOUR_EM_KEY", "endpoint": "", "extra": {}},
            "tdx-mcp": {"api_key": "YOUR_TDX_KEY", "endpoint": "", "extra": {}},
            "hsjuyuan-mcp": {"api_key": "YOUR_HS_KEY", "endpoint": "", "extra": {}},
        }
        with open(KEYS_EXAMPLE, "w", encoding="utf-8") as f:
            json.dump(tpl, f, ensure_ascii=False, indent=2)
    return KEYS_EXAMPLE


# ---------- 字段提取 ----------
def _get_path(obj: dict, keys: list[str], default=None):
    """按候选键名依次在 obj 顶层查找（不区分大小写），返回首个非空值。"""
    if not isinstance(obj, dict):
        return default
    low = {str(k).lower(): v for k, v in obj.items()}
    for k in keys:
        v = obj.get(k)
        if v in (None, ""):
            v = low.get(str(k).lower())
        if v not in (None, ""):
            return v
    return default


_DIR_MAP = {  # 方向归一（中文关键词 → 看多/看空/中性）
    "看多": "看多", "多": "看多", "买入": "看多", "买": "看多", "增持": "看多",
    "推荐": "看多", "看好": "看多", "强推": "看多", "买入评级": "看多", "bull": "看多", "buy": "看多",
    "看空": "看空", "空": "看空", "卖出": "看空", "卖": "看空", "减持": "看空",
    "回避": "看空", "中性": "中性", "持有": "中性", "观望": "中性", "neut": "中性",
}


def _norm_direction(raw) -> tuple[str, bool]:
    """返回 (方向, 是否由推断得出)。缺方向→中性（marked=True 表示非原文给出）。"""
    if raw is None:
        return "中性", True
    s = str(raw).strip()
    if not s:
        return "中性", True
    for kw, d in _DIR_MAP.items():
        if kw in s:
            return d, False
    return "中性", True


def _norm_date(raw, default: str) -> str:
    if not raw:
        return default
    s = str(raw).strip()
    # 接受 YYYY-MM-DD / YYYYMMDD
    if len(s) == 8 and s.isdigit():
        return "%s-%s-%s" % (s[:4], s[4:6], s[6:8])
    if len(s) >= 10 and s[4] == "-" and s[7] == "-":
        return s[:10]
    return default


def _extract_code(raw) -> str:
    """从任意形态抽取 6 位 A 股代码。"""
    if raw is None:
        return ""
    s = str(raw).strip()
    import re
    m = re.search(r"\b(\d{6})\b", s)
    return m.group(1) if m else ""


def _build_rec(src_key: str, item: dict, idx: int, default_date: str) -> dict | None:
    meta = MCP_SOURCES[src_key]
    f = meta["fields"]
    code = _extract_code(_get_path(item, f["code"]))
    if not code:
        return None  # 无代码 → 跳过，不造数据
    name = (_get_path(item, f["name"]) or "").strip()
    date = _norm_date(_get_path(item, f["date"]), default_date)
    direction, inferred = _norm_direction(_get_path(item, f["direction"]))
    reason = (_get_path(item, f["reason"]) or "").strip()
    claim = (_get_path(item, f["claim"]) or "").strip()
    sid = "extref-%s-%s-%s-%d" % (src_key, date.replace("-", ""), code, idx)
    note = "（方向由原文关键词推断）" if inferred else ""
    rec = {
        "sid": sid, "src_key": src_key, "src_name": meta["name"],
        "src_kind": meta["kind"], "src_tier": meta["tier"], "pub_date": date,
        "symbol": code, "symbol_name": name, "sector": "",
        "direction": direction, "strength": 3,
        "reason": (reason + note).strip(),
        "claim": claim or ("%s 经 %s 接口接入（%s）。" % (code, meta["name"], date)),
        "url": meta["url"], "hardness": "hard",
        "pnl_status": "pending",
        "tags": ["MCP接入", meta["name"], "收录" + date],
    }
    return rec


# ---------- 解析入口 ----------
def parse_payload(data) -> list[dict]:
    """把 MCP / 接口返回的原始数据归一为 item 列表。支持：
       list / {stocks:[...]} / {data:[...]} / {result:[...]} / {items:[...]}。
       返回原始 item dict 列表（不含 code 校验）。"""
    if data is None:
        return []
    if isinstance(data, list):
        return [x for x in data if isinstance(x, dict)]
    if isinstance(data, dict):
        for k in ("stocks", "data", "result", "items", "list", "rows"):
            v = data.get(k)
            if isinstance(v, list):
                return [x for x in v if isinstance(x, dict)]
        # 单条对象
        return [data]
    return []


def connect(conn, src_key: str, data=None, run_backtest: bool = True,
            default_date: str | None = None, keys: dict | None = None) -> dict:
    """B 档信源接通主入口。

    参数：
      src_key  必须是 MCP_SOURCES 白名单内。
      data     已通过 MCP/接口拿到的结构化荐股 JSON（list 或 dict）。
               若 None：尝试从配置读 endpoint 主动拉（仅 protocol 含 http 的源）；
               否则诚实返回 {ok:False, error}，**零入库**。
    返回：{ok, src_key, src_name, parsed_total, inserted, updated, skipped, rows[], backtest?}
    """
    if src_key not in MCP_SOURCES:
        return {"ok": False, "error": "非 B 档 MCP 信源：%s" % src_key}
    meta = MCP_SOURCES[src_key]
    default_date = default_date or datetime.now().strftime("%Y-%m-%d")

    # 1) 无 data → 尝试配置驱动主动拉（仅 http+mcp 源）
    if data is None:
        keys = load_keys()
        cfg = keys.get(src_key) or {}
        endpoint = (cfg.get("endpoint") or "").strip()
        if endpoint and "http" in meta["protocol"]:
            try:
                import urllib.request
                req = urllib.request.Request(endpoint, headers={
                    "User-Agent": "Mozilla/5.0",
                    "Authorization": "Bearer %s" % (cfg.get("api_key") or ""),
                })
                with urllib.request.urlopen(req, timeout=15) as r:
                    data = json.loads(r.read().decode("utf-8", "replace"))
            except Exception as e:
                return {"ok": False, "error": "主动拉取失败（%s）：%s"
                        % (src_key, type(e).__name__ + " " + str(e)[:120])}
        else:
            return {"ok": False, "error": "未传入数据且无法主动拉取（%s 为纯 MCP 协议，"
                                          "需在 MCP 客户端取数后传入 data）。" % src_key}

    items = parse_payload(data)
    if not items:
        return {"ok": False, "error": "解析后无有效条目（data 为空或非预期结构）。"}

    # 2) 名称补全（缺 name 的批量补）
    need = [(it, src_key) for it in items
            if not (_get_path(it, meta["fields"]["name"]) or "").strip()]
    names = {}
    if need:
        try:
            from extref.collect import _names_batch
            pairs = []
            for it in items:
                c = _extract_code(_get_path(it, meta["fields"]["code"]))
                mkt = "SH" if c.startswith(("6", "9")) else "SZ"
                if c:
                    pairs.append((mkt, c))
            names = _names_batch(pairs)
        except Exception:
            names = {}

    # 3) 逐条入库（幂等）
    inserted = updated = skipped = 0
    rows = []
    for i, it in enumerate(items, 1):
        rec = _build_rec(src_key, it, i, default_date)
        if rec is None:
            skipped += 1
            rows.append({"status": "skipped", "reason": "无 6 位代码"})
            continue
        if rec["symbol"] in names and not rec["symbol_name"]:
            rec["symbol_name"] = names[rec["symbol"]]
        try:
            res = st.upsert_signal(conn, rec)
            if res == "inserted":
                inserted += 1
            else:
                updated += 1
            rows.append({"status": res, "sid": rec["sid"], "symbol": rec["symbol"],
                         "name": rec["symbol_name"], "direction": rec["direction"]})
        except Exception as e:
            skipped += 1
            rows.append({"status": "error", "symbol": rec["symbol"],
                         "reason": type(e).__name__ + ": " + str(e)[:120]})

    report = {"ok": True, "src_key": src_key, "src_name": meta["name"],
              "parsed_total": len(items), "inserted": inserted,
              "updated": updated, "skipped": skipped, "rows": rows}

    # 4) 自动回测回填
    if run_backtest and (inserted or updated):
        try:
            from extref import backtest
            report["backtest"] = backtest.backtest_all(conn)
        except Exception as e:
            report["backtest_error"] = type(e).__name__ + ": " + str(e)[:160]
    return report


def connect_all(conn, run_backtest: bool = True) -> dict:
    """遍历 5 家 B 档源（仅对配置了 endpoint 的 http 源有效；纯 MCP 源需分别传 data）。"""
    out = {}
    for src_key in MCP_SOURCES:
        out[src_key] = connect(conn, src_key, data=None, run_backtest=run_backtest)
    return out


if __name__ == "__main__":
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    ensure_example()
    # 自检：用一份假数据验证解析+入库（不写真实库）
    demo = [{"code": "600519", "name": "贵州茅台", "date": "2026-09-25",
             "view": "看多", "reason": "估值修复", "conclusion": "维持推荐"},
            {"code": "000001", "date": "2026-09-25", "rating": "中性"}]
    print("PARSE:", len(parse_payload(demo)), "items")
    print("DIR check:", _norm_direction("买入"), _norm_direction("减持"))
    print("EXAMPLE at:", ensure_example())
