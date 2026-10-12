# -*- coding: utf-8 -*-
"""extref 人工导入器（Path A：把「荐股 App 里复制/导出的名单」结构化入库）。

设计目标（王总要求：不偷工、数据极致结构化、能跑通）：
  · 支持两种输入：CSV（带/不带表头、逗号/Tab/分号/中文分隔符/竖线/空格）与自由文本（每行一条）。
  · 自由文本自动抽取：6 位代码、股票名称、发布日期、方向（看多/看空/中性）、推荐理由。
  · 缺失项处理（诚实 + 极致）：
      - 代码缺失 / 非 6 位 → 该行 skip，报告标注「无法识别代码」。
      - 名称缺失 → 尽力用腾讯行情批量补全；补全失败留空（不编）。
      - 日期缺失 → 若整批给了 default_date 用之；否则按「今天」处理并显式标注，
        且不编造历史日期（回测会等窗口走完，pending 是诚实态）。
      - 方向缺失 → 默认「中性」并标注；中性不参与方向胜率，但仍可回测收益。
  · sid 幂等：<src_key>-<YYYYMMDD>-<code>-<n>，整批同输入同顺序 → 同 sid → upsert 覆盖，重导不乱增。
  · 逐行报告：每行给出 status(inserted/updated/skipped) + 解析字段 + warnings + error。
  · 隔离纪律：只 import extref.store，只写 extref_signals（经 upsert_signal），只读 extref_sources 取档案；
    不碰任何既有业务表，不 JOIN / 不外键。
"""
from __future__ import annotations

import re
from datetime import datetime

from extref import store as st

# 复用采集器的在线名称补全（腾讯行情批量取股票名），避免重复实现。
try:
    from extref.collect import _names_batch
except Exception:  # noqa: BLE001 —— 极端情况下 collect 不可用，降级为不补全
    _names_batch = None

CODE_RE = re.compile(r"(?<!\d)(\d{6})(?!\d)")
FULL_DATE_RE = re.compile(r"(\d{4})[-/年.](\d{1,2})[-/月.](\d{1,2})")
SHORT_DATE_RE = re.compile(r"(?<!\d)(\d{1,2})[-/月.](\d{1,2})日?")
CJK_RUN = re.compile(r"[\u4e00-\u9fff]{2,8}")

# 方向关键词
_LONG = ["看多", "做多", "买入", "增持", "推荐", "看好", "看涨", "加仓", "多", "多方", "买入评级", "强烈推荐"]
_SHORT = ["看空", "做空", "卖出", "减持", "看跌", "减仓", "空方", "沽空", "回避", "卖出评级"]

# CSV 表头字段别名
_HEADER_ALIASES = {
    "code": ["代码", "code", "symbol", "股票代码", "证券代码", "证券代码"],
    "name": ["名称", "name", "股票名称", "证券名称", "标的", "股票", "个股"],
    "date": ["日期", "date", "pub_date", "时间", "推荐日", "发布日", "日期"],
    "direction": ["方向", "dir", "direction", "观点", "多空", "评级"],
    "reason": ["理由", "reason", "逻辑", "备注", "说明", "原因", "推荐理由"],
    "sector": ["板块", "sector", "行业", "所属板块"],
    "strength": ["强度", "力度", "strength", "权重", "评分"],
    "claim": ["原文", "claim", "原话", "原始口径", "原始"],
    "url": ["链接", "url", "link", "来源", "url地址"],
}


def _market_of(code: str) -> str:
    if code.startswith(("60", "68", "90")):
        return "sh"
    if code.startswith(("4", "8")):
        return "bj"
    return "sz"


def _norm_date(raw: str | None, default_date: str | None, warnings: list) -> str:
    """把任意日期字符串规范为 YYYY-MM-DD；缺失则回退 default_date / 今天并标注。"""
    if raw:
        m = FULL_DATE_RE.search(raw)
        if m:
            return "%04d-%02d-%02d" % (int(m.group(1)), int(m.group(2)), int(m.group(3)))
        m = SHORT_DATE_RE.search(raw)
        if m:
            yr = datetime.now().year
            d = "%04d-%02d-%02d" % (yr, int(m.group(1)), int(m.group(2)))
            warnings.append("日期缺少年份，按当前年补全为 %s" % d)
            return d
    if default_date:
        return default_date
    today = datetime.now().strftime("%Y-%m-%d")
    warnings.append("未识别日期，默认按 %s（如需回测请补全日期）" % today)
    return today


def _norm_direction(raw: str | None, line: str, warnings: list) -> str:
    """raw 为显式方向字符串（CSV 列）；否则从文本行里猜。"""
    if raw:
        r = raw.strip()
        if any(w in r for w in ("多", "买", "增持", "看好", "推荐")) and "空" not in r and "卖" not in r:
            return "看多"
        if any(w in r for w in ("空", "卖", "减", "回避")):
            return "看空"
        if r in ("中性", "neut", "neutral"):
            return "中性"
        # 其他原样保留（如已写「看多」）
        return r or "中性"
    if any(w in line for w in _LONG):
        return "看多"
    if any(w in line for w in _SHORT):
        return "看空"
    warnings.append("未识别方向，默认记「中性」（不参与方向胜率，但可回测收益）")
    return "中性"


def _extract_name(line: str, code: str, code_span: tuple) -> str:
    """从一行里抽取股票名称：优先「名称(代码)」「代码 名称」「名称 代码」三种相邻形态。"""
    # 名称(代码) / 名称（代码）
    m = re.search(r"([\u4e00-\u9fff]{2,8})\s*[（(]\s*" + re.escape(code) + r"\s*[）)]", line)
    if m:
        return m.group(1)
    start, end = code_span
    after = line[end:]
    m = re.search(r"^[\s:：\-—#]*([\u4e00-\u9fff]{2,8})", after)
    if m:
        return m.group(1)
    before = line[:start]
    m = re.search(r"([\u4e00-\u9fff]{2,8})\s*[\s:：\-—#]*$", before)
    if m:
        return m.group(1)
    return ""


def _extract_reason(line: str, code: str, name: str, date_raw: str | None, dir_words: list) -> str:
    """抽取推荐理由：剔除代码 / 名称 / 日期 / 方向词 / 分隔符后的残余文本。"""
    r = line
    r = r.replace(code, " ")
    if name:
        r = r.replace(name, " ")
    if date_raw:
        r = r.replace(date_raw, " ")
    for w in dir_words:
        r = r.replace(w, " ")
    r = re.sub(r"[\s,，;；|｜\t]+", " ", r)
    r = re.sub(r"^[:：\-—\s]+|[，。\.、]+$", "", r).strip()
    return r


def parse_line(line: str, default_date: str | None) -> dict | None:
    """解析单行自由文本 → 结构化 dict；返回 None 表示该行无可识别代码（交由调用方 skip）。"""
    warnings: list = []
    m = CODE_RE.search(line)
    if not m:
        return None
    code = m.group(1)
    market = _market_of(code)
    date_raw = None
    dm = FULL_DATE_RE.search(line)
    if dm:
        date_raw = dm.group(0)
    else:
        sm = SHORT_DATE_RE.search(line)
        if sm:
            date_raw = sm.group(0)
    pub_date = _norm_date(date_raw, default_date, warnings)
    direction = _norm_direction(None, line, warnings)
    name = _extract_name(line, code, (m.start(), m.end()))
    reason = _extract_reason(line, code, name, date_raw, _LONG + _SHORT)
    return {
        "code": code, "market": market, "name": name, "pub_date": pub_date,
        "direction": direction, "reason": reason, "sector": "", "strength": 2,
        "claim": "", "url": "", "warnings": warnings,
    }


def _split_delimited(line: str) -> list:
    """按最可能的分隔符切分一行。"""
    if "\t" in line:
        return [x.strip() for x in line.split("\t") if x.strip() != ""]
    for sep in [",", ";", "；", "|", "｜"]:
        if sep in line:
            return [x.strip() for x in line.split(sep) if x.strip() != ""]
    # 空格分隔（中文/英文空格）
    return [x for x in re.split(r"\s+", line.strip()) if x]


def _detect_header(first_line: str) -> dict | None:
    low = first_line.lower()
    has_alias = any(a in low for al in _HEADER_ALIASES.values() for a in al)
    if not has_alias:
        return None
    cells = _split_delimited(first_line)
    mapping = {}
    for idx, cell in enumerate(cells):
        c = cell.lower().strip()
        for field, aliases in _HEADER_ALIASES.items():
            if c in aliases or any(a in c for a in aliases):
                mapping[field] = idx
                break
    return mapping if mapping else None


def parse_csv(text: str, default_date: str | None) -> list:
    """解析 CSV：有表头按列映射，无表头每行当自由文本。返回结构化 dict 列表（含无效行占位）。"""
    raw_lines = [ln for ln in text.splitlines() if ln.strip() != ""]
    if not raw_lines:
        return []
    header = _detect_header(raw_lines[0])
    records = []
    if header:
        for ln in raw_lines[1:]:
            cells = _split_delimited(ln)
            if not cells:
                continue
            rec = {f: (cells[idx] if idx < len(cells) else "") for f, idx in header.items()}
            code = (rec.get("code") or "").strip()
            cm = CODE_RE.search(code) or CODE_RE.search(" ".join(cells))
            if not cm:
                records.append({"__skip__": True, "raw": ln, "error": "该行无 6 位代码"})
                continue
            code = cm.group(1)
            warnings: list = []
            pub_date = _norm_date(rec.get("date"), default_date, warnings)
            direction = _norm_direction(rec.get("direction"), " ".join(cells), warnings)
            name = (rec.get("name") or "").strip()
            reason = (rec.get("reason") or "").strip()
            if not reason:
                # 没有专门理由列时，只把「未映射到任何元数据字段」的剩余列拼起来当理由，
                # 但排除代码 / 名称 / 日期 / 方向等已识别字段，避免污染（如把方向值当理由）。
                meta_idx = set(header.values())
                reason = " ".join(c for i, c in enumerate(cells)
                                 if i not in meta_idx and c and c != code and c != name
                                 and c != pub_date and c != (rec.get("direction") or ""))
            records.append({
                "code": code, "market": _market_of(code), "name": name,
                "pub_date": pub_date, "direction": direction, "reason": reason,
                "sector": (rec.get("sector") or "").strip(),
                "strength": _parse_strength(rec.get("strength")),
                "claim": (rec.get("claim") or "").strip(),
                "url": (rec.get("url") or "").strip(), "warnings": warnings,
            })
    else:
        for ln in raw_lines:
            rec = parse_line(ln, default_date)
            if rec is None:
                records.append({"__skip__": True, "raw": ln, "error": "无法识别 6 位代码"})
            else:
                records.append(rec)
    return records


def _parse_strength(raw: str | None) -> int:
    if not raw:
        return 2
    m = re.search(r"([1-5])", str(raw))
    return int(m.group(1)) if m else 2


def build_records(text: str, default_date: str | None, is_csv: bool | None = None) -> list:
    """入口：判断 CSV / 自由文本并产出记录列表。is_csv=None 时自动判断（含逗号/Tab 且像表格→CSV）。"""
    if is_csv is None:
        has_delim = ("," in text) or ("\t" in text) or (";" in text)
        looks_csv = has_delim and _detect_header(text.splitlines()[0] if text.splitlines() else "") is not None
        is_csv = looks_csv
    if is_csv:
        return parse_csv(text, default_date)
    # 自由文本：逐行 parse_line
    out = []
    for ln in text.splitlines():
        if not ln.strip():
            continue
        rec = parse_line(ln, default_date)
        if rec is None:
            out.append({"__skip__": True, "raw": ln, "error": "无法识别 6 位代码"})
        else:
            out.append(rec)
    return out


def import_text(conn, src_key: str, text: str, default_date: str | None = None,
                is_csv: bool | None = None) -> dict:
    """把一段文本（用户粘贴的荐股名单）结构化入库。返回完整报告。

    流程：解析 → 在线补全缺失名称 → 逐条 upsert_signal（幂等）→ 汇总。
    """
    src = st.get_source(conn, src_key)
    if not src:
        return {"ok": False, "error": "信源不存在：%s（请先从下拉选择已有信源）" % src_key}
    src_name = src.get("name") or src_key
    src_kind = src.get("kind") or "software"
    src_tier = src.get("tier") or "A"

    records = build_records(text, default_date, is_csv)
    # 离线统计前先补全名称（批量一次请求）
    need_name = [(_market_of(r["code"]), r["code"]) for r in records
                 if not r.get("__skip__") and not r.get("name")]
    name_map = {}
    if need_name and _names_batch:
        try:
            name_map = _names_batch(need_name)
        except Exception:  # noqa: BLE001 —— 补全失败不阻断入库
            name_map = {}
    for r in records:
        if not r.get("__skip__") and not r.get("name") and r["code"] in name_map:
            r["name"] = name_map[r["code"]]
            if name_map[r["code"]]:
                r["warnings"].append("名称已在线补全")

    inserted = updated = skipped = 0
    rows = []
    counters: dict = {}
    for idx, r in enumerate(records, start=1):
        if r.get("__skip__"):
            skipped += 1
            rows.append({
                "line_no": idx, "status": "skipped", "code": "",
                "name": "", "date": "", "direction": "", "reason": "",
                "warnings": [r.get("error", "无法识别 6 位代码")], "error": r.get("error"),
            })
            continue
        code = r["code"]
        pub = r["pub_date"]
        n = counters.get((pub, code), 0) + 1
        counters[(pub, code)] = n
        sid = "%s-%s-%s-%d" % (src_key, pub.replace("-", ""), code, n)
        rec = {
            "sid": sid, "src_key": src_key, "src_name": src_name, "src_kind": src_kind,
            "src_tier": src_tier, "pub_date": pub, "symbol": code, "symbol_name": r.get("name") or "",
            "sector": r.get("sector") or "", "direction": r.get("direction"),
            "strength": int(r.get("strength") or 2), "reason": r.get("reason") or "",
            "claim": r.get("claim") or ("人工导入：" + (r.get("reason") or "")), "url": r.get("url") or "",
            "hardness": "soft", "pnl_status": "pending",
            "tags": ["人工导入", (r.get("name") or code)],
        }
        try:
            res = st.upsert_signal(conn, rec)
        except Exception as e:  # noqa: BLE001
            skipped += 1
            rows.append({
                "line_no": idx, "status": "error", "code": code, "name": r.get("name") or "",
                "date": pub, "direction": r.get("direction"), "reason": (r.get("reason") or "")[:40],
                "warnings": r.get("warnings", []), "error": "%s: %s" % (type(e).__name__, e),
            })
            continue
        if res == "inserted":
            inserted += 1
        else:
            updated += 1
        rows.append({
            "line_no": idx, "status": res, "code": code, "name": r.get("name") or "(名称缺)",
            "date": pub, "direction": r.get("direction"), "reason": (r.get("reason") or "")[:60],
            "warnings": r.get("warnings", []), "error": None,
        })

    return {
        "ok": True, "src_key": src_key, "src_name": src_name,
        "parsed_total": len(records), "inserted": inserted, "updated": updated,
        "skipped": skipped, "rows": rows,
    }


if __name__ == "__main__":
    import json
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from viewer.db import Database  # noqa: E402

    c = Database().conn
    st.ensure_schema(c)
    sample = """# 演示：自由文本（可混贴 CSV / 纯文本）
600802 福建水泥 2026-09-10 看多 平台突破放量
000063 中兴通讯 2026-09-12 买入 5G 景气回升
002594 比亚迪 看空 估值偏高
300750 宁德时代 2026-09-15 推荐 储能订单超预期
同花顺 300033 2026-09-18 增持 AI 应用落地"""
    print(json.dumps(import_text(c, "ths-wencai", sample), ensure_ascii=False, indent=1))
