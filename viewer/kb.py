"""「跟我学习 → 中短线炒股」知识库：解析 + 重吸取。

设计要点
--------
1. **单一事实来源是本地 markdown 文件**（由上游工具每天覆盖写出），本模块只负责把它
   解析成结构化数据。不抓网页、不写死内容——上游改字不改结构即可继续工作。
2. **重吸取幂等**：先比 `source_mtime` + `content_hash`，两者都没变就**直接跳过**，
   不做任何写库动作。这样每天定时跑一次也不会产生无意义的重建。
3. 解析只依赖这几类**稳定记号**（已在 v5.0 全篇实测）：
   - `## 四.N 流派X · 名称（N 人）`  → 流派
   - `#### 博主：姓名`                → 博主档案块起点
   - `**字段名**` + `-` 列表           → 精读笔记字段（风格定位/选股条件/买入信号…）
   - `**【…】**`                      → 阶段切换（原帖补充 / 视频补充）
   - `#### 字段名`（视频阶段内）        → 视频笔记字段
   - `| a | b |` 表格                 → 回测结果表 / 信息来源表
4. v5.0 实测的几个**必须照顾到的特例**（照「理想格式」写会静默丢数据）：
   - 博主块有**两套记号**：15/50 位博主没有 `**字段**` 精读段，方法整理全部在
     `#### 字段` 下（视频笔记）；16 位连精读段都没有。故 notes/video 分开收集，
     不假设「必有精读」。
   - **七、附录A「信息来源清单」是表格**（流派|博主|主要来源/核验状态），不是条目列表。
     按行找 `博主：X` 会得到 0 条——必须按表解析（50 行）。
   - **八、附录B 的表题记号不统一**：B-1/B-2 用加粗行 `**表 B-n …**`，而 v5 新增的
     B-3 写在 `## 九、风险提示` 里面的 `### 附录B-3：…`。故策略表改为**全篇按列名收集**
     （列名含「策略」者），标题取最近的上方表题——这样既不漏 B-3（33 条），
     也不会把流派对比表误收进来。
   - 「九、风险提示」正文里夹着 B-3 表格：解析风险条目时必须跳过 `|` / `#` / `>` 行，
     否则表格内容会被并进上一条风险的正文里。
5. 结构化结果整体存进 `kb_doc.payload`（JSON）。之所以不做成多张规范化表：知识库是
   **只读、整篇替换**的文档型数据，读者永远是「一次取一份文档」，拆表只增加维护成本
   而不带来查询收益。

纯标准库实现，无需第三方依赖。
"""
from __future__ import annotations

import glob as _glob
import hashlib
import json
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

ROOT = Path(__file__).resolve().parent.parent

# 解析器版本：**改动本文件的解析逻辑时必须 +1**。
# 原因：文件内容哈希只能发现「上游改了文件」，发现不了「我们改了写法」——若只看哈希，
# 修好解析缺陷后旧结果会一直留在库里不被重建（静默过期）。故把版本号并入变化判定。
PARSER_VERSION = 3

# ---- 记号正则 ----
RE_H2 = re.compile(r"^##\s+(.*)$")
RE_H3 = re.compile(r"^###\s+(.*)$")
RE_H4 = re.compile(r"^####\s+(.*)$")
RE_BOLD = re.compile(r"^\*\*(.+?)\*\*\s*[：:]?\s*$")
RE_H1 = re.compile(r"^#\s+(.*)$")
RE_TABLE_SEP = re.compile(r"^\|[\s\-:|]+\|$")
# 流派标题：四.1 流派一 · 打板/涨停接力流（7 人）
RE_SCHOOL = re.compile(r"^四[.．](\d+)\s*(?:流派[一二三四五六七八九十]+)?\s*[·、]?\s*(.*?)\s*[（(]\s*(\d+)\s*人\s*[)）]\s*$")
RE_BLOGGER = re.compile(r"^博主[：:]\s*(.+?)\s*$")
RE_VERSION = re.compile(r"v(\d+(?:\.\d+)*)")
# 需从正文里剔除的噪声行（分隔线 / 空行 / 目录缩进符）
RE_DROP = re.compile(r"^(?:---|\*\*\*|___)\s*$")
# 表格标题行：`**表 B-1 …**` 或 `### 附录B-3：…` / `## 八、附录 B：…`
RE_CAPTION = re.compile(
    r"^(?:\*\*(表\s*B[-\d]*[^*]*)\*\*|#{2,4}\s*(附录\s*B[^\n]*)|(#{2,4}\s*八、[^\n]*))\s*$"
)
RE_STRATEGY_COL = re.compile(r"策略")


def _today() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# ---------------------------------------------------------------- 来源解析
def resolve_source(cfg: dict) -> Optional[str]:
    """按配置找到实际要读的 md 文件。

    优先 `path`（精确路径）；其次 `path_glob`（取最新修改的一个）。
    这样即便上游每天把文件导出到新的日期目录、或升版成 v5.1/v6，也能自动跟上。
    """
    p = (cfg.get("path") or "").strip()
    if p and Path(p).exists():
        return str(Path(p))
    pat = (cfg.get("path_glob") or "").strip()
    if pat:
        hits = [h for h in _glob.glob(pat, recursive=True) if os.path.isfile(h)]
        if hits:
            return max(hits, key=lambda h: os.path.getmtime(h))
    return None


def file_fingerprint(path: str) -> dict:
    """文件指纹：mtime + 内容哈希 + 字数。用于「要不要重吸取」的判定。"""
    st = os.stat(path)
    raw = Path(path).read_bytes()
    text = raw.decode("utf-8", errors="replace")
    return {
        "source_mtime": datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
        "content_hash": hashlib.sha256(raw).hexdigest(),
        "char_count": len(text),
    }


# ---------------------------------------------------------------- 小工具
def _bullets(lines: list[str]) -> list[str]:
    out = []
    for ln in lines:
        s = ln.strip()
        if not s or RE_DROP.match(s):
            continue
        m = re.match(r"^[-*]\s+(.*)$", s)
        if m:
            out.append(m.group(1).strip())
        elif s.startswith("|"):
            continue
        elif re.match(r"^\d+[.、]\s*", s):
            out.append(re.sub(r"^\d+[.、]\s*", "", s).strip())
        else:
            out.append(s)
    return [x for x in out if x]


def _split_h3(body: list[str]) -> list[tuple[str, list[str]]]:
    """把一段正文按 `### ` 切成 [(标题, 行)]。"""
    out: list[tuple[str, list[str]]] = []
    cur_t: Optional[str] = None
    buf: list[str] = []
    for ln in body:
        m = RE_H3.match(ln)
        if m:
            if cur_t is not None:
                out.append((cur_t, buf))
            cur_t, buf = m.group(1).strip(), []
        elif cur_t is not None:
            buf.append(ln)
    if cur_t is not None:
        out.append((cur_t, buf))
    return out


def _cells(row: str) -> list[str]:
    return [c.strip() for c in row.strip().strip("|").split("|")]


def _parse_tables(lines: list[str]) -> list[dict]:
    """解析 markdown 表格 → [{columns, rows}]（列数不一致的表各自独立成组）。

    空表（只有表头/分隔行、无数据行）直接丢弃——原稿里偶有残留的空表块。
    """
    tables: list[dict] = []
    i = 0
    while i < len(lines):
        if lines[i].strip().startswith("|"):
            block = []
            while i < len(lines) and lines[i].strip().startswith("|"):
                block.append(lines[i].strip())
                i += 1
            if len(block) >= 2:
                cols = _cells(block[0])
                rows = [_cells(r) for r in block[1:] if not RE_TABLE_SEP.match(r)]
                if cols and rows:
                    tables.append({"columns": cols, "rows": rows})
        else:
            i += 1
    return tables


def _collect_strategy_tables(lines: list[str]) -> list[dict]:
    """全篇收集「策略回测」表。

    判定标准：**列名里含「策略」二字**的表格。这样既不会把流派对比表误收进来，
    也天然兼容原稿里表题记号不统一的问题（表 B-1/B-2 用 `**表 B-n …**` 加粗行，
    v5 新增的 B-3 却写成 `### 附录B-3…` 标题行）。标题取该表**最近的上方表题**，
    没有表题就退回「策略回测」，绝不凭空造一个空表。
    """
    out: list[dict] = []
    caption = ""
    i = 0
    while i < len(lines):
        s = lines[i].strip()
        mcap = RE_CAPTION.match(s)
        if mcap:
            caption = (mcap.group(1) or mcap.group(2) or "").strip()
            i += 1
            continue
        if s.startswith("|"):
            block = []
            j = i
            while j < len(lines) and lines[j].strip().startswith("|"):
                block.append(lines[j].strip())
                j += 1
            cols = _cells(block[0]) if block else []
            rows = [_cells(r) for r in block[1:] if not RE_TABLE_SEP.match(r)]
            if any("策略" in c for c in cols) and rows:
                out.append({"title": caption or "策略回测", "columns": cols, "rows": rows})
            i = j
            caption = ""
            continue
        i += 1
    return out


# ---------------------------------------------------------------- 博主块
def _parse_blogger_block(name: str, body: list[str]) -> dict:
    """解析单个博主块。

    块内有两种记号体系共存，按「阶段」归位：
      phase=notes  → `**字段**` 精读笔记
      phase=source → `**【淘股吧原帖补充】**` 之后的来源链接
      phase=video  → `**【视频补充】…**` 之后的 `#### 字段` 视频笔记
    """
    notes: list[dict] = []
    video: list[dict] = []
    sources: list[str] = []
    misc: list[dict] = []
    phase = "notes"
    cur_head: Optional[str] = None
    cur_items: list[str] = []

    def flush():
        nonlocal cur_head, cur_items
        if not cur_items:
            cur_head, cur_items = None, []
            return
        # 关键：**没有标题的条目也不能丢**（2026-09-23 修）。
        # 实测两处会踩到：① 整块只有 `**【视频补充】**` + bullet，标题行本身是阶段标记
        # 而非字段名；② 开头直接是一段说明文字。原先 `if cur_head is None: return`
        # 会把这类内容静默丢掉（情绪周期流 6 位博主曾整块解析为空）。
        heading = cur_head or ("视频要点" if phase == "video" else "概述")
        rec = {"heading": heading, "items": cur_items}
        if phase == "video":
            video.append(rec)
        else:
            notes.append(rec)
        cur_head, cur_items = None, []

    for ln in body:
        s = ln.strip()
        if not s or RE_DROP.match(s):
            continue
        mb = RE_BOLD.match(s)
        if mb:
            # 标题归一：`**风格定位**：` 与 `**风格定位：**` 两种写法都要认（实测两种并存）
            title = mb.group(1).strip().rstrip("：:").strip()
            # 【…】 形式的加粗行是**阶段标记**，不是字段
            if title.startswith("【"):
                flush()
                if "视频" in title:
                    phase = "video"
                elif "原帖" in title or "来源" in title:
                    phase = "source"
                else:
                    phase = "misc"
                continue
            flush()
            if phase == "misc":
                phase = "notes"
            cur_head = title
            continue
        m4 = RE_H4.match(s)
        if m4:
            flush()
            if phase == "notes":
                phase = "video"      # 出现 #### 字段即进入视频笔记段
            cur_head = m4.group(1).strip()
            continue
        if s.startswith("|"):
            continue
        bullet = re.match(r"^[-*]\s+(.*)$", s)
        item = bullet.group(1).strip() if bullet else s
        if phase == "source":
            sources.append(item)
        elif phase == "misc":
            misc.append({"heading": "", "items": [item]})
        else:
            cur_items.append(item)
    flush()

    # 空字段（只有标题没有条目，多来自排版用的加粗行）一律丢弃，前端不再出现空卡片
    notes = [n for n in notes if n.get("items")]
    video = [v for v in video if v.get("items")]
    return {
        "name": name,
        "phase_notes": notes,
        "phase_video": video,
        "sources": sources,
        "misc": [x for x in misc if x.get("items")],
        "has_video": bool(video),
        "style": next((n["items"][0] for n in notes if "风格" in n["heading"]), ""),
    }


def _split_bloggers(lines: list[str]) -> list[dict]:
    """把流派正文按 `#### 博主：X` 切块。"""
    blocks: list[tuple[str, list[str]]] = []
    cur: Optional[str] = None
    buf: list[str] = []
    for ln in lines:
        m = RE_H4.match(ln)
        if m:
            name = RE_BLOGGER.match(m.group(1).strip())
            if name:
                if cur is not None:
                    blocks.append((cur, buf))
                cur, buf = name.group(1).strip(), []
                continue
        if cur is not None:
            buf.append(ln)
    if cur is not None:
        blocks.append((cur, buf))
    return [_parse_blogger_block(n, b) for n, b in blocks]


# ---------------------------------------------------------------- 主解析
def parse_document(text: str, source_path: str = "") -> dict:
    """把整篇 markdown 解析成结构化 dict（结构见模块 docstring）。"""
    lines = text.splitlines()

    title = ""
    for ln in lines:
        m = RE_H1.match(ln)
        if m:
            title = m.group(1).strip()
            break
    mv = RE_VERSION.search(title)
    version = ("v" + mv.group(1)) if mv else ""

    # 先按 ## 切段
    tops: list[tuple[str, list[str]]] = []
    cur_t: Optional[str] = None
    buf: list[str] = []
    for ln in lines:
        m = RE_H2.match(ln)
        if m:
            if cur_t is not None:
                tops.append((cur_t, buf))
            cur_t, buf = m.group(1).strip(), []
        elif cur_t is not None:
            buf.append(ln)
    if cur_t is not None:
        tops.append((cur_t, buf))

    schools: list[dict] = []
    conclusions: list[dict] = []
    compare: list[dict] = []
    pitfalls: list[dict] = []
    risks: list[dict] = []
    backtests: list[dict] = []
    sources: list[dict] = []
    outline: list[dict] = []
    strate_lines: list[str] = []   # 八、+ 九、 的正文，供策略回测表统一收集

    for t, body in tops:
        if t.startswith("目录"):
            continue
        ms = RE_SCHOOL.match(t)
        if ms:
            seq, name, cnt = int(ms.group(1)), ms.group(2).strip(), int(ms.group(3))
            # 流派共性方法体系（在 ① 小节里）
            sub = _split_h3(body)
            common = ""
            for st, sb in sub:
                if "共性" in st or "①" in st:
                    common = " ".join(_bullets(sb))[:1200]
                    break
            bloggers = _split_bloggers(body)
            schools.append({
                "key": f"school{seq}", "seq": seq, "name": name,
                "blogger_count": cnt, "declared_count": cnt,
                "actual_count": len(bloggers),
                "common_method": common,
                "bloggers": bloggers,
            })
            outline.append({"key": f"school{seq}", "title": name, "kind": "school",
                            "count": len(bloggers)})
            continue
        if t.startswith("三、"):
            for st, sb in _split_h3(body):
                conclusions.append({"key": st.split()[0] if st else st, "title": st,
                                    "items": _bullets(sb)})
            outline.append({"key": "conclusions", "title": "核心结论", "kind": "conclusion",
                            "count": len(conclusions)})
            continue
        if t.startswith("五、"):
            for st, sb in _split_h3(body):
                compare.append({"title": st, "items": _bullets(sb), "tables": _parse_tables(sb)})
            if not compare:
                compare.append({"title": "跨流派对比", "items": _bullets(body),
                                "tables": _parse_tables(body)})
            outline.append({"key": "compare", "title": "跨流派对比", "kind": "compare",
                            "count": len(compare)})
            continue
        if t.startswith("六、"):
            for st, sb in _split_h3(body):
                pitfalls.append({"key": st, "title": st, "items": _bullets(sb)})
            outline.append({"key": "pitfalls", "title": "常见误区", "kind": "pitfall",
                            "count": len(pitfalls)})
            continue
        if t.startswith("七、"):
            # 附录A：信息来源清单——原稿是**三列表格**（流派 | 博主 | 主要来源/核验状态），
            # 不是条目列表，故按表解析；同时摊平成一行一博主的记录便于前端检索。
            n = 0
            for tb in _parse_tables(body):
                if not tb["columns"] or not tb["rows"]:
                    continue
                sources.append({"title": t, "columns": tb["columns"], "rows": tb["rows"]})
                n += len(tb["rows"])
            outline.append({"key": "sources", "title": "信息来源", "kind": "source",
                            "count": n})
            continue
        if t.startswith("八、"):
            # 策略回测表统一交给 _collect_strategy_tables（见下方），此处只记大纲。
            # 注意：原稿把 v5 新增的「附录B-3」放在了 ## 九、 里，故表要连九、正文一起收。
            strate_lines.extend(body)
            outline.append({"key": "backtests", "title": "策略回测", "kind": "backtest",
                            "count": 0})   # 条数收集完后再回填
            continue
        if t.startswith("九、"):
            for ln in body:
                s = ln.strip()
                # 表格行 / 标题行 / 引用行不得并入上一条风险（否则风险正文会被表格糊掉）
                if (not s or RE_DROP.match(s) or s.startswith("|")
                        or s.startswith("#") or s.startswith(">")):
                    continue
                m = re.match(r"^(\d+)[.、]\s*(.*)$", s)
                if m:
                    risks.append({"index": int(m.group(1)), "text": m.group(2).strip()})
                elif risks:
                    risks[-1]["text"] += " " + s
            outline.append({"key": "risks", "title": "风险提示", "kind": "risk",
                            "count": len(risks)})
            strate_lines.extend(body)     # v5 的 附录B-3 表藏在这一段里
            continue

    # 策略回测表在全篇范围内统一收集（含藏在「九、风险提示」里的 附录B-3）
    backtests = _collect_strategy_tables(strate_lines)
    strategy_total = sum(len(b["rows"]) for b in backtests)
    for o in outline:
        if o.get("key") == "backtests":
            o["count"] = strategy_total

    blogger_total = sum(len(s["bloggers"]) for s in schools)
    source_total = sum(len(s["rows"]) for s in sources)
    node_total = (blogger_total
                  + sum(len(b.get("phase_notes", [])) + len(b.get("phase_video", []))
                        for s in schools for b in s["bloggers"])
                  + len(conclusions) + len(pitfalls) + len(risks)
                  + len(compare) + source_total + strategy_total)

    return {
        "meta": {
            "title": title,
            "version": version,
            "source_path": source_path,
            "school_total": len(schools),
            "blogger_total": blogger_total,
            "strategy_total": strategy_total,
            "source_total": source_total,
            "node_total": node_total,
            "parsed_at": _today(),
        },
        "outline": outline,
        "schools": schools,
        "conclusions": conclusions,
        "compare": compare,
        "pitfalls": pitfalls,
        "backtests": backtests,
        "risks": risks,
        "sources": sources,
    }


# ---------------------------------------------------------------- 词条型知识库（kind=glossary）
# 2026-09-26 新增。除「中短线炒股」那种**研究报告型**知识库外，还需要一种**工具书型**：
# 一个词条一张卡，固定回答四件事 —— 这是什么 / 怎么算 / 长期投资者（巴菲特）怎么看 /
# 常见误读在哪。两者共用同一套机制（本地 md 为唯一事实来源、按内容哈希幂等重吸取、
# 整篇替换落 kb_doc.payload），只是**记号不同**，故单独一个解析器：
# 改这里不会影响 zdx 那份文档的既有解析结果。
#
# 记号约定（写文档按这个格式；解析器对缺项一律容错，不会因为少写一行就整块丢失）：
#   # 标题 v1.0                       → 文档标题 / 版本
#   ## 一、分组名                      → 一节（分组）
#   - 出现在第一个 ### 之前的条目        → 本节导语（intro）
#   ### 毛利率 | Gross Margin          → 词条（竖线后为英文名，可省）
#   - **定义**：…                       → 词条字段（展示顺序见 FIELD_ORDER）
#   - **一句话**：…                     → 卡片顶部高亮句
#   - **别名**：… / **提示**：…         → 别名徽标 / 卡片底部小字
#   - 其它普通条目                      → 普通列表项
#   > 引文                             → 归入卡片底部提示
#   **表 1 常用指标速查**                → 表题（其上最近的加粗整行）
#   | a | b | 表格                      → 本节速查表
#   ## 风险提示 / 免责                  → 风险条目（按 1. 2. 3. 编号）
#   ## 资料来源                        → 来源表（进 sources）
RE_G_TERM = re.compile(r"^###\s+(.+?)\s*$")
RE_G_BOLDKV = re.compile(r"^\*\*(.+?)\*\*\s*[：:]\s*(.*)$")
# 字段归一：`**巴菲特怎么看（要点）**` 这类带括号补充的写法，取括号前的正名
RE_G_FIELD_ALIAS = {
    "一句话": "one_line", "一句话定义": "one_line", "概括": "one_line",
    "别名": "alias", "俗称": "alias", "又称": "alias",
    "提示": "tip", "备注": "tip", "注意": "tip",
}


def _g_classify(title: str) -> str:
    """把 `## ` 标题分成 分组 / 风险 / 来源 / 跳过（目录）。"""
    t = title or ""
    if "目录" in t:
        return "skip"
    if re.search(r"风险|免责|声明", t):
        return "risk"
    if re.search(r"资料来源|来源|出处|参考文献", t):
        return "source"
    return "group"


def parse_glossary(text: str, source_path: str = "") -> dict:
    """把词条型 markdown 解析成 {kind, meta, outline, groups, risks, sources}。"""
    lines = text.splitlines()
    title = ""
    for ln in lines:
        m = RE_H1.match(ln)
        if m:
            title = m.group(1).strip()
            break
    mv = RE_VERSION.search(title)
    version = ("v" + mv.group(1)) if mv else ""

    groups: list[dict] = []
    outline: list[dict] = []
    risks: list[dict] = []
    sources: list[dict] = []
    group_seq = 0
    mode = "skip"
    cur: Optional[dict] = None
    term: Optional[dict] = None
    table_raw: list[str] = []
    caption = ""

    def flush_table() -> None:
        nonlocal table_raw, caption
        if not table_raw:
            return
        for tb in _parse_tables(table_raw):
            rec = {"title": caption or "速查表", "columns": tb["columns"], "rows": tb["rows"]}
            if mode == "source":
                sources.append(rec)
            elif cur is not None:
                cur["tables"].append(rec)
        table_raw, caption = [], ""

    def flush_term() -> None:
        nonlocal term
        if term is not None and cur is not None:
            cur["terms"].append(term)
        term = None

    for raw in lines:
        s = raw.strip()
        # ---- 表格块：连续 `|` 行一次性收走 ----
        if s.startswith("|"):
            table_raw.append(s)
            continue
        flush_table()
        if not s or RE_DROP.match(s) or s.startswith("<!--"):
            continue
        if RE_H1.match(s):
            continue

        m2 = RE_H2.match(s)
        if m2:
            flush_term()
            t = m2.group(1).strip()
            kind = _g_classify(t)
            if kind == "skip":
                mode, cur = "skip", None
                continue
            if kind == "risk":
                mode, cur = "risk", None
                outline.append({"key": "risks", "title": "风险提示与免责",
                                "kind": "risk", "count": 0})
                continue
            if kind == "source":
                mode, cur = "source", None
                outline.append({"key": "sources", "title": "资料来源与口径",
                                "kind": "source", "count": 0})
                continue
            group_seq += 1
            cur = {"key": f"g{group_seq}", "title": t, "intro": [], "terms": [],
                   "tables": [], "notes": [], "plain": False}
            groups.append(cur)
            outline.append({"key": cur["key"], "title": t, "kind": "group", "count": 0})
            mode = "group"
            continue

        m3 = RE_G_TERM.match(s)
        if m3 and mode == "group" and cur is not None:
            flush_term()
            head = m3.group(1).strip()
            name, en = head, ""
            if "|" in head:
                name, en = [x.strip() for x in head.split("|", 1)]
            term = {"name": name, "en": en, "alias": "", "one_line": "",
                    "fields": [], "items": [], "tip": ""}
            continue

        # 整行加粗 → 表题（`**表 B-1 …**` 一类的写法）
        mb = RE_BOLD.match(s)
        if mb:
            caption = mb.group(1).strip()
            continue
        mcap = re.match(r"^#{3,4}\s*(.*)$", s)
        if mcap:
            caption = mcap.group(1).strip()
            continue

        # 引用行 → 卡片底部提示 / 节尾说明
        mbq = re.match(r"^>\s*(.*)$", s)
        if mbq:
            txt = mbq.group(1).strip()
            if term is not None:
                term["tip"] = (term["tip"] + " " + txt).strip()
            elif cur is not None:
                cur["notes"].append(txt)
            continue

        bullet = re.match(r"^[-*]\s+(.*)$", s)
        body_txt = bullet.group(1).strip() if bullet else s
        mf = RE_G_BOLDKV.match(body_txt)
        if mf:
            k = re.sub(r"[（(].*?[)）]", "", mf.group(1).strip()).strip()
            v = mf.group(2).strip()
            if term is not None:
                slot = RE_G_FIELD_ALIAS.get(k)
                if slot:
                    if slot == "tip":
                        term["tip"] = (term["tip"] + " " + v).strip()
                    else:
                        term[slot] = v
                else:
                    term["fields"].append({"k": k, "v": v})
                continue
            if cur is not None and mode == "group":
                cur["intro"].append(f"{k}：{v}")
                continue

        if mode == "risk":
            mr = re.match(r"^(\d+)[.、]\s*(.*)$", s)
            if mr:
                risks.append({"index": int(mr.group(1)), "text": mr.group(2).strip()})
            elif risks:
                risks[-1]["text"] += " " + s
            continue
        if mode == "source":
            # 来源段里除表格外的自由文字：作为说明挂到最近的来源表标题上
            if body_txt and sources:
                sources[-1]["title"] = (sources[-1].get("title") or "") + " " + body_txt
            continue
        if term is not None:
            if body_txt:
                term["items"].append(body_txt)
            continue
        if cur is not None and body_txt:
            cur["intro"].append(body_txt)

    flush_table()
    flush_term()

    term_total = 0
    field_total = 0
    for g in groups:
        g["plain"] = not g["terms"]          # 纯文字节（无可拆词条）→ 前端标注，避免看着像"解析失败"
        term_total += len(g["terms"])
        field_total += sum(len(t["fields"]) for t in g["terms"])
        for o in outline:
            if o.get("key") == g["key"]:
                o["count"] = len(g["terms"])
    for o in outline:
        if o.get("key") == "risks":
            o["count"] = len(risks)
        elif o.get("key") == "sources":
            o["count"] = sum(len(x["rows"]) for x in sources)

    node_total = (term_total + field_total + len(risks)
                  + sum(len(x["rows"]) for x in sources))
    return {
        "kind": "glossary",
        "meta": {
            "title": title,
            "version": version,
            "source_path": source_path,
            "group_total": len(groups),
            "term_total": term_total,
            "field_total": field_total,
            "node_total": node_total,
            "parsed_at": _today(),
        },
        "outline": outline,
        "groups": groups,
        "risks": risks,
        "sources": sources,
    }


# ---------------------------------------------------------------- 文章型知识库（kind=article）
# 2026-10-11 新增，专用于 B站 UP 主知识沉淀（以及任何「按作者自身知识体系组织的主题文章」）。
# 与 report（中短线炒股：结论/流派/回测/风险）和 glossary（词条卡）都不同：文章型是
# 「H2 分章 + H3 小节 + 要点/段落/表格」的论述体，强行套进上面两种会大面积静默丢内容
# （实测李大霄那份只被 report 解析器抓到 5 个知识点，其余 ~45 个全部丢失）。
# 记号约定（写文档按这个格式；缺项一律容错，不会因少写一行就整块丢失）：
#   # 标题 v1.0                          → 文档标题 / 版本
#   > 引用                                → 顶部来源标注 / 节内注记（归入 notes）
#   ## 一、章名 / ## 章名（无序号也可）     → 一章（section）
#   ### 小节名                           → 章内的小节（subsection）
#   - 要点                                → 列表项（嵌套会被拍平，避免丢内容）
#   **加粗**：正文                         → 段落（lead 段落）
#   | a | b | 表格                         → 表格
# 解析结果整体存进 kb_doc.payload（与另外两种共用同一张表、同一套落库/重吸取机制）。
RE_ART_TITLE_NUM = re.compile(r"^[一二三四五六七八九十]+\s*[.．、]\s*(.*)$")


def _extract_prose_block(lines: list[str]) -> dict:
    """把一段正文拆成 段落(paragraphs) / 列表项(items) / 注记(notes) / 表格(tables)。

    表格行单独成块解析；其余按行分类。嵌套列表拍平，确保不丢内容；空行/分隔线跳过。
    """
    paragraphs: list[str] = []
    items: list[str] = []
    notes: list[str] = []
    tables: list[dict] = []
    i = 0
    n = len(lines)
    while i < n:
        s = lines[i].strip()
        if s.startswith("|"):
            block = []
            while i < n and lines[i].strip().startswith("|"):
                block.append(lines[i].strip())
                i += 1
            if len(block) >= 2:
                cols = _cells(block[0])
                rows = [_cells(r) for r in block[1:] if not RE_TABLE_SEP.match(r)]
                if cols and rows:
                    tables.append({"columns": cols, "rows": rows})
            continue
        if not s or RE_DROP.match(s):
            i += 1
            continue
        if s.startswith(">"):
            notes.append(re.sub(r"^>\s*", "", s).strip())
            i += 1
            continue
        mb = re.match(r"^[-*]\s+(.*)$", s)
        if mb:
            items.append(mb.group(1).strip())
            i += 1
            continue
        # 其余视为段落（含 `**加粗 lead**：` 这类）
        paragraphs.append(s)
        i += 1
    return {"paragraphs": paragraphs, "items": items, "notes": notes, "tables": tables}


def parse_article(text: str, source_path: str = "") -> dict:
    """把「主题文章型」markdown 解析成结构化 dict（结构见模块 docstring）。"""
    lines = text.splitlines()

    title = ""
    for ln in lines:
        m = RE_H1.match(ln)
        if m:
            title = m.group(1).strip()
            break
    mv = RE_VERSION.search(title)
    version = ("v" + mv.group(1)) if mv else ""

    # 顶部来源标注（第一个 ## 之前的 > 引用块）—— 对 B站沉淀是 `@B站@UP名` 等溯源信息
    meta_notes: list[str] = []
    top_end = 0
    for idx, ln in enumerate(lines):
        if RE_H2.match(ln):
            top_end = idx
            break
        s = ln.strip()
        if s.startswith(">"):
            meta_notes.append(re.sub(r"^>\s*", "", s).strip())
    body = lines[top_end:] if top_end else lines

    # 按 ## 分章
    tops: list[tuple[str, list[str]]] = []
    cur_t: Optional[str] = None
    buf: list[str] = []
    for ln in body:
        m = RE_H2.match(ln)
        if m:
            if cur_t is not None:
                tops.append((cur_t, buf))
            cur_t, buf = m.group(1).strip(), []
        elif cur_t is not None:
            buf.append(ln)
    if cur_t is not None:
        tops.append((cur_t, buf))

    sections: list[dict] = []
    outline: list[dict] = []
    node_total = 0
    for si, (t, sb) in enumerate(tops, 1):
        mn = RE_ART_TITLE_NUM.match(t)
        sec_title = mn.group(1).strip() if mn else t
        # 章内按 ### 切小节（无 ### 时整段作为 lead 正文，不能丢）
        subs = _split_h3(sb)
        lead_body: dict = {"paragraphs": [], "items": [], "notes": [], "tables": []}
        subsecs: list[dict] = []
        if not subs:
            lead_body = _extract_prose_block(sb)
        else:
            for st, sl in subs:
                blk = _extract_prose_block(sl)
                if st is None:
                    lead_body = blk
                else:
                    subsecs.append({"title": st.strip(), **blk})
        section_node = (len(lead_body["paragraphs"]) + len(lead_body["items"])
                        + len(lead_body["notes"]) + len(lead_body["tables"]) + len(subsecs)
                        + sum(len(x["paragraphs"]) + len(x["items"]) + len(x["notes"])
                               + len(x["tables"]) for x in subsecs))
        node_total += section_node
        sections.append({
            "key": f"sec{si}", "seq": si, "title": sec_title,
            "paragraphs": lead_body["paragraphs"], "items": lead_body["items"],
            "notes": lead_body["notes"], "tables": lead_body["tables"],
            "subsections": subsecs,
        })
        outline.append({"key": f"sec{si}", "title": sec_title, "kind": "section",
                        "count": section_node})

    node_total += len(meta_notes)

    return {
        "kind": "article",
        "meta": {
            "title": title,
            "version": version,
            "source_path": source_path,
            "section_total": len(sections),
            "node_total": node_total,
            "parsed_at": _today(),
        },
        "outline": outline,
        "meta_notes": meta_notes,
        "sections": sections,
    }


def detect_kind(text: str, cfg: dict) -> str:
    """判定这份 md 属于哪种知识库。

    优先读配置里的 `kind`（显式声明最可靠）；没写就按记号自动判定：
    同时出现 `### 词条` 与 `- **定义**：` / `- **怎么算**：` 才算词条型，
    否则一律按原来的研究报告型解析——**保持既有文档行为不变**。
    """
    k = str(cfg.get("kind") or "").strip().lower()
    if k in ("glossary", "report", "article"):
        return k
    if (re.search(r"^###\s+\S", text, re.M)
            and re.search(r"^[-*]\s+\*\*(定义|怎么算|巴菲特怎么看|为什么重要)\*\*", text, re.M)):
        return "glossary"
    return "report"


def load_config(path: Optional[str] = None) -> list[dict]:
    """读取 kb_sources.yaml，返回启用的来源列表。"""
    import yaml  # 局部导入：本机 .venv 已装 PyYAML

    p = Path(path) if path else (ROOT / "config" / "kb_sources.yaml")
    if not p.exists():
        return []
    data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    items = data.get("kb_sources") or []
    return [x for x in items if isinstance(x, dict) and x.get("enabled", True)]


def build_payload(cfg: dict) -> dict:
    """按一份来源配置解析出完整 payload（含指纹）。不落库。"""
    doc_key = cfg.get("doc_key") or "kb"
    src = resolve_source(cfg)
    if not src:
        return {"doc_key": doc_key, "status": "error",
                "error": "来源文件未找到（检查 config/kb_sources.yaml 的 path / path_glob）",
                "parser_version": PARSER_VERSION,
                "meta": {"title": cfg.get("title") or doc_key}}
    fp = file_fingerprint(src)
    text = Path(src).read_text(encoding="utf-8", errors="replace")
    # 按文档类型选解析器：report = 研究报告型（结论/流派/回测/风险）；
    # glossary = 词条型（一个词条一张卡）。类型可由 kb_sources.yaml 的 kind 显式指定，
    # 也可由记号自动判定，避免配置漏写时把词条库当研究报告解析出满屏空白。
    kind = detect_kind(text, cfg)
    doc = (parse_glossary(text, src) if kind == "glossary"
           else parse_article(text, src) if kind == "article"
           else parse_document(text, src))
    payload = {
        "doc_key": doc_key,
        "track": cfg.get("track") or "中短线炒股",
        "title": cfg.get("title") or doc["meta"]["title"],
        "version": doc["meta"]["version"],
        "kind": kind,
        "source_path": src,
        "source_mtime": fp["source_mtime"],
        "content_hash": fp["content_hash"],
        "char_count": fp["char_count"],
        "node_count": doc["meta"]["node_total"],
        "ingested_at": _today(),
        "parser_version": PARSER_VERSION,
        "status": "ok",
        "error": "",
        "doc": doc,
    }
    return payload


# ---------------------------------------------------------------- CLI
# 为什么不只在网页上点「立即吸取」：网页那条路要先把可视化服务跑起来。
# 换机器、服务没起、或要批量重建时，命令行是更短的路径，也便于写进定时任务。
#   python -m viewer.kb --list                  # 看已吸取了哪些库
#   python -m viewer.kb --show value_basics     # 只解析、打印摘要，不落库（改文档后先自查）
#   python -m viewer.kb --reload value_basics   # 吸取指定库
#   python -m viewer.kb --reload --force        # 忽略哈希强制重建全部
def _cli() -> int:
    import argparse
    import json as _json

    ap = argparse.ArgumentParser(description="知识库吸取 CLI（不依赖 HTTP 服务）")
    ap.add_argument("--list", action="store_true", help="列出已吸取的知识库")
    ap.add_argument("--show", metavar="DOC_KEY", help="仅解析并打印摘要，不落库")
    ap.add_argument("--reload", nargs="*", metavar="DOC_KEY",
                    help="重新吸取（不给 doc_key 则全部已配置来源）")
    ap.add_argument("--force", action="store_true", help="忽略内容哈希强制重建")
    args = ap.parse_args()

    cfgs = load_config()
    if not cfgs:
        print("未找到启用的知识库来源（检查 config/kb_sources.yaml）")
        return 1

    def pick(keys):
        if not keys:
            return cfgs
        want = set(keys)
        return [c for c in cfgs if (c.get("doc_key") or "kb") in want]

    if args.show:
        """解析预览：改完 md 先跑这一步，确认词条数/分组数符合预期再落库。"""
        for cfg in pick([args.show]):
            payload = build_payload(cfg)
            doc = payload.get("doc") or {}
            meta = doc.get("meta") or {}
            print(f"文件：{payload.get('source_path')}")
            print(f"类型：{payload.get('kind')}  版本：{payload.get('version') or '—'}"
                  f"  知识点：{payload.get('node_count')}")
            if payload.get("kind") == "glossary":
                print(f"统计：分组 {meta.get('group_total')} · 词条 {meta.get('term_total')} · "
                      f"字段 {meta.get('field_total')} · 风险 {len(doc.get('risks') or [])}")
            else:
                print(f"统计：章节 {meta.get('section_total')} · 知识点 {meta.get('node_total')}")
            for o in doc.get("outline") or []:
                print(f"  - {o.get('title')}（{o.get('count')}）")
            if doc.get("groups") and doc["groups"][0].get("terms"):
                print("首个词条：" + _json.dumps(doc["groups"][0]["terms"][0], ensure_ascii=False)[:240])
        return 0

    if args.reload is None and not args.list:
        ap.print_help()
        return 0

    from viewer.db import Database
    db = Database()

    if args.list:
        for r in db.kb_list():
            print(f"{r['doc_key']:16s} {(r.get('track') or ''):8s} {r.get('version') or '—':8s} "
                  f"nodes={r.get('node_count'):5d} {r.get('status')}  {r.get('ingested_at') or '—'}")
        return 0

    rc = 0
    for cfg in pick(args.reload):
        key = cfg.get("doc_key") or "kb"
        payload = build_payload(cfg)
        if payload.get("status") != "ok":
            print(f"[FAIL] {key}: {payload.get('error')}")
            rc = 1
            continue
        r = db.kb_save(payload, force=args.force)
        # 与网页「立即吸取」（server.kb_reload）保持一致：也往 refresh_status 留一笔。
        # 不记的话，「跟我学习」里这条轨道会一直显示「未纳入刷新」——
        # 明明是刚吸进来的新内容，却看着像没生效。
        try:
            label = f"{cfg.get('track') or ''}知识库".strip() or key
            db.record_refresh(key, "ok", int(payload.get("node_count") or 0), label)
        except Exception:  # noqa: BLE001 记录刷新状态失败不影响吸取结果
            pass
        if r.get("skipped"):
            print(f"[SKIP] {key}: 内容未变，跳过（未产生无意义写入）")
        else:
            print(f"[OK]   {key}: {payload.get('kind')} · {payload.get('version') or '—'} · "
                  f"{payload.get('node_count')} 个知识点")
    return rc


if __name__ == "__main__":
    raise SystemExit(_cli())
